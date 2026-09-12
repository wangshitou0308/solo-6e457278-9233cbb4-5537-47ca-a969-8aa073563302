"""程序包静态展开：主程序 + O 号子程序集的内联展开。

语义（Fanuc 风格，静态预检采用）：
- 主程序与子程序分别给出；子程序以 O 号行开头、以 M99 结尾。
- M98 P<n> L<k> 调用 O<n> 子程序 k 次（L 默认 1，正整数）；调用时继承
  当前全部模态，M99 返回后从调用点的下一行继续；重复调用之间不重置
  任何状态（同一段连续模态流）。
- M99 为子程序返回；M2/M30 结束整个程序（主/子程序中均终止全部执行）。
- 每个展开块保留来源程序、原行、调用栈（每级含调用行与重复序号）。

下列情况在展开阶段整体阻断，**不生成部分安全结论**（不运行轨迹/安全
检查），只返回调用图与展开错误：
重复 O 号、M98 目标不存在、主程序中的 M99、子程序末尾缺 M99、递归调用、
调用深度超限、展开量超过 MAX_BLOCKS_LIMIT 块、动态 P（P#.. / P[..]）、
变量表达式（# / []）、M98 P/L 非法、O 号缺失/重复/与声明不一致、
主程序中出现 O 号、M99 P 返回跳转不支持。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .parser import ParsedLine, g_code_key, parse_program

# 硬上限：展开块数超过该值（>100,000）即阻断
MAX_BLOCKS_LIMIT = 100_000
DEFAULT_MAX_DEPTH = 50
MAX_DEPTH_CAP = 1000
MAIN_KEY = "main"

# 程序流 M 代码（程序包方言）
FLOW_M = {"2", "30", "98", "99"}


# ---------------------------------------------------------------------------
# 展开错误代码
# ---------------------------------------------------------------------------

EXPANSION_ERROR_TITLE = {
    "PACKAGE_DUPLICATE_O": "重复 O 号",
    "PACKAGE_SUBPROGRAM_NOT_FOUND": "M98 目标子程序不存在",
    "PACKAGE_M99_IN_MAIN": "主程序中出现 M99",
    "PACKAGE_SUBPROGRAM_MISSING_M99": "子程序末尾缺少 M99",
    "PACKAGE_RECURSIVE_CALL": "递归调用",
    "PACKAGE_DEPTH_LIMIT": "调用深度超限",
    "PACKAGE_BLOCK_LIMIT": "展开块数超过 100,000",
    "PACKAGE_DYNAMIC_P": "动态子程序号（不支持）",
    "PACKAGE_VARIABLE_EXPRESSION": "变量/表达式（不支持）",
    "PACKAGE_INVALID_P": "M98 P 子程序号非法",
    "PACKAGE_INVALID_L": "M98 L 重复次数非法",
    "PACKAGE_O_NUMBER_MISMATCH": "子程序 O 号异常",
    "PACKAGE_MAIN_HAS_O": "主程序中出现 O 号",
    "PACKAGE_UNSUPPORTED_RETURN": "M99 P 返回跳转（不支持）",
}

# 所有展开错误都在展开阶段整体阻断，按 error 级对待
EXPANSION_ERROR_SEVERITY = {code: "error" for code in EXPANSION_ERROR_TITLE}


class PackageSpecError(ValueError):
    """程序包请求结构非法（API 层映射为 400）。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class PackageSpec:
    name: str
    main_text: str
    main_file: str
    # [{"file": "o100.nc", "text": "...", "o_number": 100 | None}]
    subprograms: list[dict] = field(default_factory=list)
    max_depth: int = DEFAULT_MAX_DEPTH


@dataclass
class ProgramUnit:
    """一个来源程序（主程序或某个 O 号子程序）。"""

    key: str                    # "main" | "O100"
    file: str
    text: str
    lines: list[ParsedLine]
    o_number: int | None = None
    reachable: bool = False


@dataclass
class Block:
    """一个展开块：某来源程序的一行，在某次调用展开中的实例。"""

    seq: int                    # 从 0 开始的展开序号
    program: str                # "main" | "O100"
    file: str
    line: ParsedLine
    depth: int                  # 调用嵌套深度（主程序为 0）
    repeat_index: int           # 所属 M98 调用的第几次重复（主程序块为 0）
    repeat_total: int
    call_stack: list[dict]      # 上级调用帧（主程序块为 []）


@dataclass
class ExpansionError:
    code: str
    message: str
    source_program: str | None = None
    source_file: str | None = None
    line_no: int | None = None
    source_line: str | None = None
    call_stack: list[dict] | None = None
    repeat_index: int = 0
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "title": EXPANSION_ERROR_TITLE[self.code],
            "severity": EXPANSION_ERROR_SEVERITY[self.code],
            "message": self.message,
            "source_program": self.source_program,
            "source_file": self.source_file,
            "line_no": self.line_no,
            "source_line": self.source_line,
            "call_stack": self.call_stack or [],
            "repeat_index": self.repeat_index,
            "details": self.details,
        }


@dataclass
class CallSite:
    target: int
    repeats: int
    line_no: int
    source_line: str


@dataclass
class _Frame:
    """展开遍历的显式栈帧（避免依赖 Python 递归深度）。"""

    key: str
    idx: int
    frame_info: dict | None     # 主程序帧为 None
    repeat_index: int
    repeat_total: int


# ---------------------------------------------------------------------------
# 请求解析
# ---------------------------------------------------------------------------

def parse_package_spec(data: dict) -> PackageSpec:
    errors: list[str] = []
    if not isinstance(data, dict):
        raise PackageSpecError(["请求体必须是 JSON 对象"])

    name = str(data.get("name") or "未命名程序包")

    main = data.get("main")
    if not isinstance(main, str) or not main.strip():
        errors.append("需要非空的 main 字段（主程序 .nc 文本）")
        main = ""
    main_file = str(data.get("main_file") or data.get("main_name")
                    or "main.nc")

    raw_subs = data.get("subprograms", [])
    subs: list[dict] = []
    if raw_subs is not None:
        if not isinstance(raw_subs, list):
            errors.append("subprograms 必须是数组，每项形如 "
                          '{"name": "o100.nc", "content": "O100 ..."}')
        else:
            for i, item in enumerate(raw_subs):
                if not isinstance(item, dict):
                    errors.append(f"subprograms[{i}] 必须是对象")
                    continue
                content = item.get("content", item.get("text"))
                if not isinstance(content, str) or not content.strip():
                    errors.append(f"subprograms[{i}] 需要非空 content（.nc 文本）")
                    continue
                fname = str(item.get("name") or item.get("file")
                            or f"sub_{i}.nc")
                o_decl = item.get("o_number")
                o_int: int | None = None
                if o_decl is not None:
                    if (not isinstance(o_decl, int) or isinstance(o_decl, bool)
                            or o_decl <= 0):
                        errors.append(
                            f"subprograms[{i}].o_number 必须为正整数")
                    else:
                        o_int = o_decl
                subs.append({"file": fname, "text": content,
                             "o_number": o_int})

    max_depth = DEFAULT_MAX_DEPTH
    if "max_depth" in data and data["max_depth"] is not None:
        v = data["max_depth"]
        if (not isinstance(v, int) or isinstance(v, bool)
                or not (1 <= v <= MAX_DEPTH_CAP)):
            errors.append(f"max_depth 必须为 1..{MAX_DEPTH_CAP} 的整数")
        else:
            max_depth = v

    if errors:
        raise PackageSpecError(errors)
    return PackageSpec(name=name, main_text=main, main_file=main_file,
                       subprograms=subs, max_depth=max_depth)


# ---------------------------------------------------------------------------
# 词法层面的程序流识别（预检查与遍历共用）
# ---------------------------------------------------------------------------

def _flow_m_codes(pl: ParsedLine) -> set[str]:
    return {g_code_key(w) for w in pl.m_words} & FLOW_M


def _o_words(pl: ParsedLine):
    return [w for w in pl.words if w.letter == "O"]


def _is_pos_int(v: float) -> bool:
    return isinstance(v, (int, float)) and v > 0 and float(v).is_integer()


def _has_variable(pl: ParsedLine) -> bool:
    """变量/宏表达式特征：# 或方括号（注释已在解析时剥离）。"""
    return "#" in pl.code_text or "[" in pl.code_text or "]" in pl.code_text


def _m98_words(pl: ParsedLine):
    p_words = [w for w in pl.words if w.letter == "P"]
    l_words = [w for w in pl.words if w.letter == "L"]
    return p_words, l_words


def _check_malformed_m98(unit, pl: ParsedLine, err) -> None:
    """处理词法残缺行上的 M98：动态 P（P#../P[..]）或无法恢复的 P。"""
    m98_count = sum(1 for w in pl.m_words if g_code_key(w) == "98")
    if m98_count != 1:
        err("PACKAGE_INVALID_P", unit, pl,
            "同一程序段出现多个程序流指令（M98/M99/M2/M30 不得混用）",
            {"reason": "conflicting_flow"})
        return
    # 在去注释文本中直接找 P 词（P#100、P[100+1] 无法被词法恢复为数字词）
    mtokens = re.findall(r"P\s*([+-]?[\w.\[\]#+\-*/]+)", pl.code_text)
    ltokens = re.findall(r"L\s*([+-]?[\d.]+)", pl.code_text)
    p_words, l_words = _m98_words(pl)
    if mtokens and not p_words:
        tok = mtokens[0]
        if "#" in tok or "[" in tok or "]" in tok:
            err("PACKAGE_DYNAMIC_P", unit, pl,
                f"M98 P{tok} 为动态子程序号（变量/表达式），静态展开不支持",
                {"reason": "dynamic_p", "p_token": tok})
        else:
            err("PACKAGE_INVALID_P", unit, pl,
                f"M98 P 子程序号无法解析：P{tok}",
                {"reason": "p_unparseable", "p_token": tok})
    elif not mtokens:
        err("PACKAGE_INVALID_P", unit, pl,
            "M98 需要恰好一个 P 子程序号（正整数），如 M98 P100 L2",
            {"reason": "p_count", "p_count": 0})
    if len(ltokens) > 1 or (l_words and not _is_pos_int(l_words[0].value)):
        err("PACKAGE_INVALID_L", unit, pl,
            "M98 L 重复次数必须为正整数",
            {"reason": "l_not_positive_int"})


# ---------------------------------------------------------------------------
# 展开
# ---------------------------------------------------------------------------

def expand_package(spec: PackageSpec) -> dict:
    """静态展开程序包。

    返回 {"ok", "units", "blocks", "errors", "graph", "summary"}。
    ok=False 时 blocks 为空，调用方不得运行安全检查。
    """
    errors: list[ExpansionError] = []
    units: dict[str, ProgramUnit] = {}

    def err(code, unit: ProgramUnit | None, pl: ParsedLine | None,
            message, details=None):
        errors.append(ExpansionError(
            code=code,
            message=message,
            source_program=unit.key if unit is not None else None,
            source_file=unit.file if unit is not None else None,
            line_no=pl.line_no if pl is not None else None,
            source_line=pl.source if pl is not None else None,
            details=details or {}))

    # 1) 解析主程序与全部子程序，登记 O 号
    main_unit = ProgramUnit(
        key=MAIN_KEY, file=spec.main_file, text=spec.main_text,
        lines=parse_program(spec.main_text), o_number=None)
    units[MAIN_KEY] = main_unit

    # O 号 -> 子程序（重复在登记阶段报出）
    o_seen: dict[int, ProgramUnit] = {}
    # 已解析但因结构错误未登记的单元（用于元信息展示）
    parsed_units: list[ProgramUnit] = []
    for item in spec.subprograms:
        unit = ProgramUnit(
            key="", file=item["file"], text=item["text"],
            lines=parse_program(item["text"]), o_number=None)
        parsed_units.append(unit)
        o_words_all = [w for pl in unit.lines for w in _o_words(pl)]
        # 非正整数 O（如 O0/O-1/O1.5）：直接判非法
        non_pos = [w for w in o_words_all if not _is_pos_int(w.value)]
        valid_o = sorted({int(w.value) for w in o_words_all
                          if _is_pos_int(w.value)})
        o_numbers = valid_o
        declared = item["o_number"]

        # O 号结构校验
        first_code = next((pl for pl in unit.lines if not pl.is_blank), None)
        if non_pos:
            bad_line = next(pl for pl in unit.lines
                            if any(w in non_pos for w in _o_words(pl)))
            err("PACKAGE_O_NUMBER_MISMATCH", unit, bad_line,
                f"O 号必须为正整数：{non_pos[0].raw}",
                {"reason": "o_not_positive_int"})
            continue
        if not o_numbers:
            err("PACKAGE_O_NUMBER_MISMATCH", unit, first_code,
                f"子程序 {unit.file} 缺少 O 号行（应以 O<n> 开头）",
                {"reason": "missing_o"})
            continue
        if declared is not None and declared not in o_numbers:
            err("PACKAGE_O_NUMBER_MISMATCH", unit, first_code,
                f"子程序 {unit.file} 声明 O{declared}，但内容 O 号为 "
                f"O{'/O'.join(str(n) for n in o_numbers)}",
                {"reason": "declared_mismatch", "declared": declared,
                 "found": o_numbers})
            continue
        on = o_numbers[0]
        if len(o_numbers) > 1:
            bad_line = next(pl for pl in unit.lines if len(_o_words(pl)) > 1
                            or any(int(w.value) != on
                                   for w in _o_words(pl)
                                   if _is_pos_int(w.value)))
            err("PACKAGE_O_NUMBER_MISMATCH", unit, bad_line,
                f"子程序 {unit.file} 含多个/不一致 O 号："
                f"O{'/O'.join(str(n) for n in o_numbers)}",
                {"reason": "multiple_o", "found": o_numbers})
            continue
        if first_code is None or not _o_words(first_code):
            err("PACKAGE_O_NUMBER_MISMATCH", unit, first_code,
                f"子程序 O{on} 必须以 O{on} 行作为第一条指令",
                {"reason": "o_not_first"})
            continue
        if on in o_seen:
            err("PACKAGE_DUPLICATE_O", unit, first_code,
                f"O{on} 与子程序 {o_seen[on].file} 重复",
                {"o_number": on,
                 "first_file": o_seen[on].file})
            continue
        unit.o_number = on
        unit.key = f"O{on}"
        o_seen[on] = unit
        units[unit.key] = unit

    # 2) 逐行静态检查 + 收集调用点
    # (caller_key, line_no) -> CallSite（仅合法 M98）
    sites: dict[tuple[str, int], CallSite] = {}
    site_o_order: list[tuple[str, int]] = []

    def check_m98(unit, pl):
        codes = _flow_m_codes(pl)
        p_words, l_words = _m98_words(pl)
        # 同一程序段多个程序流指令
        if len(codes) > 1 or sum(1 for w in pl.m_words
                                 if g_code_key(w) == "98") > 1:
            err("PACKAGE_INVALID_P", unit, pl,
                "同一程序段出现多个程序流指令（M98/M99/M2/M30 不得混用）",
                {"reason": "conflicting_flow"})
            return
        if len(p_words) != 1:
            err("PACKAGE_INVALID_P", unit, pl,
                "M98 需要恰好一个 P 子程序号（正整数），如 M98 P100 L2",
                {"reason": "p_count", "p_count": len(p_words)})
            return
        pv = p_words[0].value
        if not _is_pos_int(pv):
            err("PACKAGE_DYNAMIC_P" if not float(pv).is_integer()
                else "PACKAGE_INVALID_P", unit, pl,
                f"M98 P 子程序号必须为正整数：P{p_words[0].raw_num}",
                {"reason": "p_not_positive_int", "p_raw": p_words[0].raw_num})
            return
        if len(l_words) > 1:
            err("PACKAGE_INVALID_L", unit, pl,
                f"M98 L 重复次数只能给一个：{len(l_words)} 个",
                {"reason": "l_count"})
            return
        if l_words and not _is_pos_int(l_words[0].value):
            err("PACKAGE_INVALID_L", unit, pl,
                f"M98 L 重复次数必须为正整数：L{l_words[0].raw_num}",
                {"reason": "l_not_positive_int",
                 "l_raw": l_words[0].raw_num})
            return
        key = (unit.key, pl.line_no)
        sites[key] = CallSite(
            target=int(pv),
            repeats=int(l_words[0].value) if l_words else 1,
            line_no=pl.line_no, source_line=pl.source)
        site_o_order.append(key)

    for unit in [main_unit, *o_seen.values()]:
        for pl in unit.lines:
            if pl.is_blank:
                continue
            # 变量/表达式：任何程序任何行出现都阻断整个程序包
            if _has_variable(pl):
                err("PACKAGE_VARIABLE_EXPRESSION", unit, pl,
                    "程序含变量/宏表达式（# 变量或 [] 表达式），"
                    "静态展开不支持动态值",
                    {"reason": "variable_or_expression"})
            if pl.malformed:
                # 残缺行常规上交给分析器按 MALFORMED_LINE 处理；但若残缺行
                # 携带 M98，其动态/非法 P 必须在展开阶段阻断（不承认调用）
                if any(g_code_key(w) == "98" for w in pl.m_words):
                    _check_malformed_m98(unit, pl, err)
                continue
            o_here = _o_words(pl)
            codes = _flow_m_codes(pl)

            if unit.key == MAIN_KEY:
                if o_here:
                    err("PACKAGE_MAIN_HAS_O", unit, pl,
                        "主程序中不能出现 O 号（O 号仅用于子程序集）",
                        {"reason": "o_in_main"})
                if "99" in codes:
                    err("PACKAGE_M99_IN_MAIN", unit, pl,
                        "主程序中出现 M99（M99 仅用于子程序返回，"
                        "主程序结束应使用 M2/M30）",
                        {"reason": "m99_in_main"})

            # O 行不得夹带程序流指令
            if o_here and codes:
                err("PACKAGE_O_NUMBER_MISMATCH", unit, pl,
                    "O 号行不能与 M98/M99/M2/M30 出现在同一程序段",
                    {"reason": "o_with_flow"})

            if "98" in codes:
                check_m98(unit, pl)
            elif "99" in codes:
                p_words, _ = _m98_words(pl)
                if p_words:
                    err("PACKAGE_UNSUPPORTED_RETURN", unit, pl,
                        "M99 Pn 返回跳转不支持（子程序只允许 M99 返回到调用点）",
                        {"reason": "m99_p_jump"})

    # 3) 子程序末尾必须有 M99（最后一条非空指令）
    for unit in o_seen.values():
        last = next((pl for pl in reversed(unit.lines)
                     if not pl.is_blank), None)
        if last is None or last.malformed or "99" not in _flow_m_codes(last):
            err("PACKAGE_SUBPROGRAM_MISSING_M99", unit, last,
                f"子程序 O{unit.o_number}（{unit.file}）末尾缺少 M99 返回",
                {"reason": "last_line_not_m99"})

    # 4) 调用图（含未定义目标节点）
    adjacency: dict[str, list[CallSite]] = {}
    node_meta: dict[str, dict] = {}
    for key, unit in units.items():
        node_meta[key] = {
            "program": key,
            "o_number": unit.o_number,
            "file": unit.file,
            "defined": True,
            "physical_lines": sum(1 for pl in unit.lines if not pl.is_blank),
            "reachable": False,
        }
        adjacency.setdefault(key, [])
    for (caller, _line), site in sites.items():
        adjacency.setdefault(caller, []).append(site)
        tgt = f"O{site.target}"
        if tgt not in node_meta:
            node_meta[tgt] = {
                "program": tgt, "o_number": site.target, "file": None,
                "defined": False, "physical_lines": 0, "reachable": False}
            adjacency.setdefault(tgt, [])

    # M98 目标不存在（静态全量检查，含不可达子程序中的调用）
    missing_seen: set[tuple] = set()
    for (caller, line_no), site in sites.items():
        tgt = f"O{site.target}"
        if not node_meta[tgt]["defined"]:
            sig = (caller, line_no, site.target)
            if sig in missing_seen:
                continue
            missing_seen.add(sig)
            pl = next(pl for pl in units[caller].lines
                      if pl.line_no == line_no)
            err("PACKAGE_SUBPROGRAM_NOT_FOUND", units[caller], pl,
                f"M98 P{site.target} 调用的 O{site.target} 子程序"
                "不在程序包中",
                {"target_program": tgt, "o_number": site.target,
                 "repeats": site.repeats})

    # 5) 递归检测（调用图 DFS 找全部环，含不可达子程序）
    cycles = _find_cycles(adjacency)
    for cyc in cycles:
        # 定位环上第一条调用边的源行
        edge_line = None
        for a, b in zip(cyc, cyc[1:] + cyc[:1]):
            st = next((s for s in adjacency.get(a, [])
                       if f"O{s.target}" == b), None)
            if st is not None:
                edge_line = (a, st.line_no)
                break
        unit = units.get(edge_line[0]) if edge_line else None
        pl = None
        if unit is not None and edge_line is not None:
            pl = next((p for p in unit.lines
                       if p.line_no == edge_line[1]), None)
        err("PACKAGE_RECURSIVE_CALL", unit, pl,
            "调用图存在递归环：" + " -> ".join(cyc + [cyc[0]]),
            {"cycle": cyc})

    # 6) 遍历展开（即使已有静态错误也尽力走一遍，收集深度/块数错误；
    #    最终只要有任何错误就不产出安全结论）
    blocks: list[Block] = []
    edge_stat: dict[tuple[str, str], dict] = {}
    max_depth_reached = 0
    call_executions = 0
    block_limit_hit = False

    def add_edge_stat(caller: str, callee: str, site: CallSite,
                      reachable: bool):
        e = edge_stat.setdefault((caller, callee), {
            "caller": caller, "callee": callee,
            "o_number": site.target,
            "sites": set(), "executions": 0, "invocations": 0})
        e["sites"].add(site.line_no)
        e["executions"] += 1
        if reachable:
            e["invocations"] += site.repeats

    stack: list[_Frame] = [_Frame(MAIN_KEY, 0, None, 0, 1)]
    units[MAIN_KEY].reachable = True
    depth_err_seen: set[tuple] = set()
    stop_all = False

    def push_error(code, unit, pl, message, frame_infos, details):
        errors.append(ExpansionError(
            code=code, message=message,
            source_program=unit.key, source_file=unit.file,
            line_no=pl.line_no, source_line=pl.source,
            call_stack=list(frame_infos),
            repeat_index=(frame_infos[-1]["repeat_index"]
                          if frame_infos else 0),
            details=details))

    while stack and not stop_all and not block_limit_hit:
        fr = stack[-1]
        unit = units.get(fr.key)
        if unit is None or fr.idx >= len(unit.lines):
            # 调用帧的剩余重复：复位指针后再展开一遍（模态连续、不重置）
            if (fr.frame_info is not None
                    and fr.repeat_index < fr.repeat_total):
                fr.repeat_index += 1
                fr.idx = 0
                fr.frame_info = {**fr.frame_info,
                                 "repeat_index": fr.repeat_index}
                continue
            stack.pop()
            continue
        pl = unit.lines[fr.idx]
        fr.idx += 1

        if len(blocks) >= MAX_BLOCKS_LIMIT:
            push_error(
                "PACKAGE_BLOCK_LIMIT", unit, pl,
                f"展开块数达到 {MAX_BLOCKS_LIMIT} 上限后仍有未展开内容"
                f"（来自 {unit.key} 第 {pl.line_no} 行），按保守策略阻断",
                [f.frame_info for f in stack[1:] if f.frame_info],
                {"limit": MAX_BLOCKS_LIMIT,
                 "generated": len(blocks)})
            block_limit_hit = True
            break

        frame_infos = [f.frame_info for f in stack[1:] if f.frame_info]
        blocks.append(Block(
            seq=len(blocks), program=unit.key, file=unit.file, line=pl,
            depth=len(stack) - 1, repeat_index=fr.repeat_index,
            repeat_total=fr.repeat_total, call_stack=list(frame_infos)))

        if pl.is_blank or pl.malformed:
            continue
        codes = _flow_m_codes(pl)
        if "2" in codes or "30" in codes:
            stop_all = True
            continue
        if "99" in codes:
            # 子程序返回：L>1 时复位到子程序开头再展开一遍（状态不重置）
            if (fr.frame_info is not None
                    and fr.repeat_index < fr.repeat_total):
                fr.repeat_index += 1
                fr.idx = 0
                fr.frame_info = {**fr.frame_info,
                                 "repeat_index": fr.repeat_index}
            else:
                stack.pop()
            continue
        if "98" not in codes:
            continue

        site = sites.get((unit.key, pl.line_no))
        if site is None:
            # 非法 M98（预检查已记录）：不产生调用
            continue
        call_executions += 1
        tgt_key = f"O{site.target}"
        tgt = units.get(tgt_key)
        add_edge_stat(unit.key, tgt_key, site, reachable=bool(tgt))
        if tgt is None:
            continue  # 目标不存在：预检查已阻断
        active_keys = [f.key for f in stack]
        if tgt_key in active_keys:
            # 递归环已在静态调用图阶段报出；这里防御性跳过，不再重复记录
            continue
        new_depth = len(stack)  # 压入后深度
        if new_depth > spec.max_depth:
            sig = (unit.key, tgt_key, pl.line_no, spec.max_depth)
            if sig not in depth_err_seen:
                depth_err_seen.add(sig)
                push_error(
                    "PACKAGE_DEPTH_LIMIT", unit, pl,
                    f"调用 {tgt_key} 将使嵌套深度达到 {new_depth}，"
                    f"超过上限 {spec.max_depth}",
                    frame_infos,
                    {"depth": new_depth, "max_depth": spec.max_depth,
                     "target_program": tgt_key})
            continue
        tgt.reachable = True
        # 只压入一帧；帧结束时按 repeat_total 自动重复（状态连续不重置）
        info = {
            "program": tgt_key,
            "o_number": tgt.o_number,
            "file": tgt.file,
            "caller": unit.key,
            "call_line_no": pl.line_no,
            "call_source_line": pl.source,
            "repeat_index": 1,
            "repeat_total": site.repeats,
            "depth": new_depth,
        }
        stack.append(_Frame(tgt_key, 0, info, 1, site.repeats))

    # 块内最大深度即调用嵌套深度（主程序块为 0）
    max_depth_reached = max((b.depth for b in blocks), default=0)

    for m in node_meta.values():
        u = units.get(m["program"])
        m["reachable"] = bool(u and u.reachable)

    edges = _build_edges(sites, edge_stat, node_meta)
    call_sites_total = len(sites)
    call_invocations = sum(e["invocations"] for e in edges)
    original_lines = sum(len(u.lines) for u in units.values()
                         if node_meta[u.key]["defined"])
    summary = {
        "status": "completed",
        "expanded_blocks": len(blocks),
        "original_physical_lines": sum(
            1 for u in units.values() for pl in u.lines if not pl.is_blank),
        "source_programs": len([m for m in node_meta.values()
                                if m["defined"]]),
        "subprograms_defined": len(o_seen),
        "max_depth": max_depth_reached,
        "call_sites": call_sites_total,
        "call_executions": call_executions,
        "call_invocations": call_invocations,
        "repeat_invocations": max(0, call_invocations - call_executions),
        "limits": {"max_depth": spec.max_depth,
                   "max_blocks": MAX_BLOCKS_LIMIT},
    }
    graph = {"nodes": [node_meta[k] for k in
                       [MAIN_KEY, *sorted(k for k in node_meta
                                          if k != MAIN_KEY)]],
             "edges": edges}

    ok = not errors
    if not ok:
        blocks = []
        summary["status"] = "blocked"
    return {"ok": ok, "units": units, "blocks": blocks,
            "errors": errors, "graph": graph, "summary": summary,
            "main_unit": main_unit, "subs": list(o_seen.values()),
            "parsed_subs": parsed_units}


def _find_cycles(adjacency: dict[str, list[CallSite]]) -> list[list[str]]:
    """DFS 找出调用图中的全部环（按旋转归一化去重）。"""
    color: dict[str, int] = {k: 0 for k in adjacency}
    stack: list[str] = []
    found: dict[tuple, list[str]] = {}

    def dfs(u):
        color[u] = 1
        stack.append(u)
        for st in adjacency.get(u, []):
            v = f"O{st.target}"
            if v not in color:
                continue  # 未定义节点
            if color[v] == 1:
                i = stack.index(v)
                cyc = stack[i:]
                # 旋转归一化（不处理反向同构，调用环方向有意义）
                rotations = [tuple(cyc[k:] + cyc[:k])
                             for k in range(len(cyc))]
                key = min(rotations)
                found.setdefault(key, list(cyc))
            elif color[v] == 0:
                dfs(v)
        stack.pop()
        color[u] = 2

    for k in adjacency:
        if color[k] == 0:
            dfs(k)
    return list(found.values())


def _build_edges(sites, edge_stat, node_meta) -> list[dict]:
    """汇总调用边：静态调用点 + 遍历中的执行次数/调用次数。"""
    static: dict[tuple[str, str], dict] = {}
    for (_caller, _line), st in sites.items():
        callee = f"O{st.target}"
        e = static.setdefault((_caller, callee), {
            "caller": _caller, "callee": callee, "o_number": st.target,
            "static_sites": [], "defined": node_meta[callee]["defined"]})
        e["static_sites"].append(
            {"line_no": st.line_no, "source_line": st.source_line,
             "repeats": st.repeats})
    edges = []
    for key in sorted(set(static) | set(edge_stat)):
        st0 = static.get(key)
        dyn = edge_stat.get(key)
        edges.append({
            "caller": key[0],
            "callee": key[1],
            "o_number": (st0 or dyn)["o_number"],
            "defined": node_meta[key[1]]["defined"],
            "sites": len(st0["static_sites"]) if st0 else 0,
            "site_lines": sorted(s["line_no"] for s in st0["static_sites"])
                          if st0 else [],
            "executions": dyn["executions"] if dyn else 0,
            "invocations": dyn["invocations"] if dyn else 0,
        })
    return edges


# ---------------------------------------------------------------------------
# 展开 + 安全分析
# ---------------------------------------------------------------------------

def analyze_package(spec: PackageSpec, config, progress=None) -> dict:
    """展开程序包；成功则交给现有轨迹/安全分析，失败只返回展开错误。

    成功返回标准分析报告并附加 package 节；阻断返回 {"blocked": True,
    "package": ..., "call_graph": ..., "expansion_errors": [...]}，
    不含任何轨迹/风险结论。
    """
    result = expand_package(spec)
    package_meta = _package_meta(spec, result)

    if not result["ok"]:
        return {
            "blocked": True,
            "package": package_meta,
            "call_graph": result["graph"],
            "expansion_errors": [e.to_dict() for e in result["errors"]],
        }

    # 延迟导入避免循环依赖
    from .analyzer import Analyzer

    az = Analyzer(config, spec.name, progress=progress, package_mode=True)
    report = az.run_blocks(result["blocks"])

    # 用展开信息扩充程序节
    report["program"]["name"] = spec.name
    report["program"]["expanded_blocks"] = result["summary"]["expanded_blocks"]
    report["program"]["source_programs"] = result["summary"]["source_programs"]
    report["program"]["max_call_depth"] = result["summary"]["max_depth"]
    report["program"]["call_invocations"] = result["summary"]["call_invocations"]
    report["package"] = package_meta
    report["expansion_errors"] = []
    return report


def _package_meta(spec: PackageSpec, result: dict) -> dict:
    units = result["units"]
    summary = result["summary"]
    registered = {u.file: u for u in result.get("subs", [])}
    parsed = result.get("parsed_subs", [])

    def unit_lines(key):
        return sum(1 for pl in units[key].lines if not pl.is_blank)

    subprograms = []
    for idx, item in enumerate(spec.subprograms):
        fname = item["file"]
        matched = registered.get(fname)
        if matched is not None:
            subprograms.append({
                "program": matched.key,
                "o_number": matched.o_number,
                "file": matched.file,
                "physical_lines": unit_lines(matched.key),
            })
        else:
            # 结构非法（重复 O/缺 O 等）：尽力从内容 O 词或声明给出 O 号
            o_num = item.get("o_number")
            pu = parsed[idx] if idx < len(parsed) else None
            if o_num is None and pu is not None:
                o_words = [w for pl in pu.lines for w in _o_words(pl)]
                pos = [int(w.value) for w in o_words
                       if _is_pos_int(w.value)]
                if len(set(pos)) == 1:
                    o_num = pos[0]
            subprograms.append({
                "program": f"O{o_num}" if o_num else None,
                "o_number": o_num,
                "file": fname, "physical_lines": 0})
    return {
        "name": spec.name,
        "main": {"program": MAIN_KEY, "file": spec.main_file,
                 "physical_lines": unit_lines(MAIN_KEY)},
        "subprograms": subprograms,
        "expansion": summary,
        "call_graph": result["graph"],
    }


# ---------------------------------------------------------------------------
# 方言说明
# ---------------------------------------------------------------------------

PACKAGE_DIALECT = {
    "scope": "程序包静态展开（主程序 + O 号子程序集，POST /api/packages）",
    "flow_m": {
        "O<n>": "子程序号行，必须是子程序第一条指令",
        "M98": "调用子程序：P<n> 子程序号，L<k> 重复次数（默认 1，正整数）",
        "M99": "子程序返回，回到调用点下一程序段；子程序最后一条指令必须为 M99",
        "M2": "程序结束（主/子程序中出现都终止整个程序）",
        "M30": "程序结束（同 M2）",
    },
    "semantics": [
        "调用时继承调用点的全部模态（单位/定位/坐标系/平面/运动/进给/主轴/"
        "固定循环等）",
        "M99 返回后从 M98 调用点的下一程序段继续",
        "M98 L<k> 的 k 次调用共享同一段连续模态流，重复调用不重置状态",
        "每个展开块保留来源程序、原行号、原行文本、调用栈（含每级调用行与"
        "重复序号）与重复序号",
        "子程序在 M99 之前提前返回时，其后的程序段不展开",
    ],
    "unsupported": [
        "动态 P：M98 P#100 / M98 P[..]（变量或表达式子程序号）",
        "变量与宏表达式：任何 # 变量或 [] 表达式",
        "M99 Pn 行号返回跳转",
    ],
    "limits": {
        "max_depth_default": DEFAULT_MAX_DEPTH,
        "max_depth_cap": MAX_DEPTH_CAP,
        "max_blocks": MAX_BLOCKS_LIMIT,
    },
    "block_rules": [
        "重复 O 号 -> PACKAGE_DUPLICATE_O",
        "M98 目标不在程序包 -> PACKAGE_SUBPROGRAM_NOT_FOUND",
        "主程序中出现 M99 -> PACKAGE_M99_IN_MAIN",
        "子程序末尾缺少 M99 -> PACKAGE_SUBPROGRAM_MISSING_M99",
        "调用图存在递归环 -> PACKAGE_RECURSIVE_CALL",
        "调用嵌套深度超过 max_depth（默认 50）-> PACKAGE_DEPTH_LIMIT",
        "展开块数超过 100,000 -> PACKAGE_BLOCK_LIMIT",
        "M98 P 缺失/非正整数/多个 -> PACKAGE_INVALID_P；"
        "动态 P#.. / P[..] -> PACKAGE_DYNAMIC_P",
        "M98 L 非正整数或多个 -> PACKAGE_INVALID_L",
        "子程序缺 O 号 / 多个 O 号 / O 号与声明不一致 / O 行非首行 / "
        "O 行夹带程序流 -> PACKAGE_O_NUMBER_MISMATCH",
        "主程序中出现 O 号 -> PACKAGE_MAIN_HAS_O",
        "变量/宏表达式 -> PACKAGE_VARIABLE_EXPRESSION",
        "M99 Pn 返回跳转 -> PACKAGE_UNSUPPORTED_RETURN",
    ],
}
