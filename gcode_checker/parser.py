"""G-code 词法解析（保守策略：不猜测、不补全）。

仅负责把一行 .nc 文本拆成词（word），不做任何执行解释：
- 支持圆括号注释 (...) 与分号注释 ;...
- 词的形式为 字母 + 数字（数字允许符号与小数点，如 X-12.5、F1000、G54）
- 行号 N 会被解析但在规范化时剔除
- 无法识别的字符 / 残缺数字（如 "X-"、"G1.A"）记为 malformed
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 本工具支持的 G 指令（其余一律列为“未支持指令”）
SUPPORTED_G = {
    "0": "rapid",        # 快速定位
    "1": "linear",       # 直线插补（切削）
    "2": "arc_cw",       # 顺时针圆弧
    "3": "arc_ccw",      # 逆时针圆弧
    "20": "inch",        # 英制
    "21": "mm",          # 公制
    "90": "absolute",    # 绝对定位
    "91": "relative",    # 增量定位
    "54": "wcs",         # 工件坐标系 1（偏移由作业配置给出）
}

# 本工具支持的 M 指令
SUPPORTED_M = {
    "3": "spindle_on_cw",  # 主轴正转
    "5": "spindle_off",    # 主轴停止
}

# 可以携带数值的地址字母
ADDRESS_LETTERS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

# 一个合法的词：字母后接有符号数（必须至少含一位数字）
_WORD_RE = re.compile(r"([A-Za-z])\s*([+-]?\d*\.?\d+)")
# 用于判断数字本身是否完全合法
_NUM_RE = re.compile(r"[+-]?\d*\.?\d+$")


@dataclass
class Word:
    """一个 G-code 词，例如 X-12.5。"""

    letter: str          # 大写地址字母
    raw_num: str         # 原始数字文本
    value: float         # 解析后的数值
    raw: str             # 原始词文本（大写）

    def to_dict(self) -> dict:
        return {"letter": self.letter, "value": self.value, "raw": self.raw}


@dataclass
class ParsedLine:
    """单行解析结果。"""

    line_no: int                     # 从 1 开始的物理行号
    source: str                      # 去注释前的原始行（已 strip）
    code_text: str                   # 去注释后的指令文本（大写）
    words: list[Word] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    malformed: list[str] = field(default_factory=list)  # 无法识别的片段
    is_blank: bool = False           # 去注释后无任何指令

    @property
    def g_words(self) -> list[Word]:
        return [w for w in self.words if w.letter == "G"]

    @property
    def m_words(self) -> list[Word]:
        return [w for w in self.words if w.letter == "M"]

    @property
    def f_words(self) -> list[Word]:
        return [w for w in self.words if w.letter == "F"]

    @property
    def s_words(self) -> list[Word]:
        return [w for w in self.words if w.letter == "S"]


def _strip_comments(line: str) -> tuple[str, list[str]]:
    """去掉圆括号注释与分号注释，返回 (指令文本, 注释列表)。

    圆括号在常见控制器上不嵌套；分号之后的内容全部视为注释。
    """
    comments: list[str] = []
    out: list[str] = []
    depth = 0
    buf: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == ";":
            rest = line[i + 1:]
            if rest.strip():
                comments.append(rest.strip())
            break
        if ch == "(":
            depth += 1
            buf = []
            i += 1
            continue
        if ch == ")" and depth:
            depth -= 1
            comments.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        if depth:
            buf.append(ch)
        else:
            out.append(ch)
        i += 1
    if depth:
        # 未闭合的括号：把括号内残余也当作注释，不视为语法错误
        comments.append("".join(buf).strip())
    return "".join(out), comments


def parse_line(line: str, line_no: int) -> ParsedLine:
    """解析单行文本。"""
    source = line.strip()
    code_text, comments = _strip_comments(source)
    code_text = code_text.strip().upper()
    pl = ParsedLine(
        line_no=line_no,
        source=source,
        code_text=code_text,
        comments=comments,
    )
    if not code_text:
        pl.is_blank = True
        return pl

    pos = 0
    text = code_text
    while pos < len(text):
        ch = text[pos]
        if ch.isspace():
            pos += 1
            continue
        if ch not in ADDRESS_LETTERS:
            # 游离的符号或数字，不属于任何词
            pl.malformed.append(ch)
            pos += 1
            continue
        m = _WORD_RE.match(text, pos)
        if not m or m.start() != pos:
            # 字母后面没有合法数字：把到下一个空白为止的残片整体记为残缺；
            # 同时尝试从残片中恢复合法词（如 "X-T5" 中的 T5），
            # 便于分析阶段完整列出未支持指令（残片所在行仍会被整段阻断）。
            j = pos + 1
            while j < len(text) and not text[j].isspace():
                j += 1
            frag = text[pos:j]
            pl.malformed.append(frag)
            for wm in _WORD_RE.finditer(frag):
                num_text = wm.group(2)
                pl.words.append(Word(
                    letter=wm.group(1),
                    raw_num=num_text,
                    value=float(num_text),
                    raw=wm.group(0).replace(" ", ""),
                ))
            pos = j
            continue
        letter = m.group(1)
        num_text = m.group(2)
        if not _NUM_RE.match(num_text):
            pl.malformed.append(m.group(0))
            pos = m.end()
            continue
        pl.words.append(
            Word(
                letter=letter,
                raw_num=num_text,
                value=float(num_text),
                raw=m.group(0).replace(" ", ""),
            )
        )
        pos = m.end()
    return pl


def parse_program(text: str) -> list[ParsedLine]:
    """解析整个程序（按物理行切分，保留行号）。"""
    # 容忍 CRLF；末尾空行不产生解析行
    return [parse_line(line, i + 1) for i, line in enumerate(text.splitlines())]


def g_code_key(word: Word) -> str:
    """规范化 G/M 代码数字键：54.0 -> "54"。"""

    return str(int(word.value)) if word.value == int(word.value) else str(word.value)
