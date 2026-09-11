"""G-code 模态还原、几何解算与安全检查。

设计原则（与需求一致）：
- 逐行按模态还原刀具位置与主轴/进给状态；
- 未支持 / 无法解析 / 几何无解的程序段一律阻断，不猜测执行，不改变状态；
- 单位 / 定位模式 / 工件坐标系不明时，相关检查显式报出而非按默认值蒙算；
- 所有内部长度单位为 mm（G20 输入在读取时乘 25.4）。
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .parser import (
    ParsedLine,
    Word,
    SUPPORTED_G,
    SUPPORTED_M,
    g_code_key,
    parse_program,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SEVERITY_ORDER = ["critical", "error", "warning", "info"]
SEVERITY_WEIGHT = {"critical": 25, "error": 10, "warning": 3, "info": 1}

# 问题代码 -> 默认严重度
ISSUE_SEVERITY = {
    "MALFORMED_LINE": "critical",          # 残缺/无法解析的程序段
    "UNSUPPORTED_INSTRUCTION": "error",    # 未支持指令（整段阻断）
    "NO_MOTION_MODE": "warning",           # 有轴坐标词但没有模态 G0-G3
    "UNKNOWN_UNITS": "warning",            # G20/G21 未建立
    "UNKNOWN_DISTANCE_MODE": "warning",    # G90/G91 未建立
    "UNKNOWN_WCS": "warning",              # G54 未建立，无法做行程检查
    "ARC_NO_SOLUTION": "critical",         # 圆弧几何无解（整段阻断）
    "OUT_OF_BOUNDS": "critical",           # 越出机床行程
    "FEED_OVER_LIMIT": "error",            # 进给超限
    "FEED_UNSET": "warning",               # 切削段未给进给
    "SPINDLE_OVER_LIMIT": "error",         # 主轴转速超限
    "SPINDLE_NOT_RUNNING": "error",        # 主轴未启动即切削
    "RAPID_BELOW_SAFE_Z": "error",         # 低于安全 Z 的快速移动
}

ISSUE_TITLE = {
    "MALFORMED_LINE": "程序段无法解析",
    "UNSUPPORTED_INSTRUCTION": "未支持指令（已阻断该段）",
    "NO_MOTION_MODE": "缺少模态运动指令",
    "UNKNOWN_UNITS": "单位模式不明（未见 G20/G21）",
    "UNKNOWN_DISTANCE_MODE": "定位模式不明（未见 G90/G91）",
    "UNKNOWN_WCS": "工件坐标系不明（未见 G54）",
    "ARC_NO_SOLUTION": "圆弧几何无解（已阻断该段）",
    "OUT_OF_BOUNDS": "越出机床行程",
    "FEED_OVER_LIMIT": "进给速度超过机床上限",
    "FEED_UNSET": "切削运动未指定进给 F",
    "SPINDLE_OVER_LIMIT": "主轴转速超过机床上限",
    "SPINDLE_NOT_RUNNING": "主轴未启动即发生切削",
    "RAPID_BELOW_SAFE_Z": "快速移动低于安全 Z 高度",
}

ALLOWED_LETTERS = {"G", "M", "X", "Y", "Z", "I", "J", "R", "F", "S", "N"}
MOTION_G = {"0": "rapid", "1": "linear", "2": "arc_cw", "3": "arc_ccw"}
MOTION_CN = {"rapid": "快速", "linear": "直线",
             "arc_cw": "顺时针圆弧", "arc_ccw": "逆时针圆弧"}
SETTING_G_UNIT = {"20": "inch", "21": "mm"}
SETTING_G_MODE = {"90": "absolute", "91": "relative"}

GEOM_TOL = 1e-6      # 几何相对容差
MM_EPS = 1e-5


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def fmt_num(v) -> str:
    """规范数字：整数去 .0，其余保留 6 位小数并去尾零。"""
    if v is None:
        return ""
    if isinstance(v, float) and abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    if isinstance(v, int):
        return str(v)
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s


def round6(v):
    if v is None:
        return None
    return round(v, 6)


# ---------------------------------------------------------------------------
# 机床配置
# ---------------------------------------------------------------------------

class ConfigError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass
class MachineConfig:
    name: str = "未命名机床"
    x_min: float = 0.0
    x_max: float = 0.0
    y_min: float = 0.0
    y_max: float = 0.0
    z_min: float = 0.0
    z_max: float = 0.0
    safe_z: float = 0.0
    max_feed_mm_min: float = 0.0
    max_spindle_rpm: float = 0.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    offset_z: float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "MachineConfig":
        errors: list[str] = []

        def num(key: str, default=0.0):
            if key not in d or d[key] is None:
                return default
            try:
                return float(d[key])
            except (TypeError, ValueError):
                errors.append(f"{key} 必须是数值")
                return default

        d = dict(d)
        for ax in ("x", "y", "z"):
            travel = d.get(f"travel_{ax}")
            if travel is not None:
                if isinstance(travel, (list, tuple)):
                    if len(travel) != 2:
                        errors.append(f"travel_{ax} 必须是 [min, max]")
                        continue
                    d[f"{ax}_min"], d[f"{ax}_max"] = travel
                else:
                    hi = float(travel)
                    d[f"{ax}_min"], d[f"{ax}_max"] = (hi, 0.0) if hi < 0 else (0.0, hi)

        x_min, x_max = num("x_min"), num("x_max")
        y_min, y_max = num("y_min"), num("y_max")
        z_min, z_max = num("z_min"), num("z_max")
        safe_z = num("safe_z")
        max_feed = num("max_feed_mm_min")
        max_rpm = num("max_spindle_rpm")
        ox, oy, oz = num("offset_x"), num("offset_y"), num("offset_z")
        name = str(d.get("name") or "未命名机床")

        for lo, hi, ax in ((x_min, x_max, "X"), (y_min, y_max, "Y"),
                           (z_min, z_max, "Z")):
            if hi <= lo:
                errors.append(f"{ax} 行程上限必须大于下限（{lo}..{hi}）")
        if max_feed <= 0:
            errors.append("max_feed_mm_min 必须为正数")
        if max_rpm <= 0:
            errors.append("max_spindle_rpm 必须为正数")
        if errors:
            raise ConfigError(errors)

        return cls(
            name=name,
            x_min=x_min, x_max=x_max,
            y_min=y_min, y_max=y_max,
            z_min=z_min, z_max=z_max,
            safe_z=safe_z,
            max_feed_mm_min=max_feed,
            max_spindle_rpm=max_rpm,
            offset_x=ox, offset_y=oy, offset_z=oz,
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "x_min": self.x_min, "x_max": self.x_max,
            "y_min": self.y_min, "y_max": self.y_max,
            "z_min": self.z_min, "z_max": self.z_max,
            "safe_z": self.safe_z,
            "max_feed_mm_min": self.max_feed_mm_min,
            "max_spindle_rpm": self.max_spindle_rpm,
            "offset_x": self.offset_x,
            "offset_y": self.offset_y,
            "offset_z": self.offset_z,
            "travel_x": [self.x_min, self.x_max],
            "travel_y": [self.y_min, self.y_max],
            "travel_z": [self.z_min, self.z_max],
        }


# ---------------------------------------------------------------------------
# 运行状态
# ---------------------------------------------------------------------------

@dataclass
class Axis:
    """一个物理量：数值（内部 mm）与是否物理已知。"""
    value: float | None = None
    known: bool = False


@dataclass
class State:
    unit: str | None = None            # 'mm' | 'inch' | None
    distance_mode: str | None = None   # 'absolute' | 'relative' | None
    wcs: str | None = None             # 'G54' | None
    motion_mode: str | None = None     # rapid/linear/arc_cw/arc_ccw
    x: Axis = field(default_factory=Axis)
    y: Axis = field(default_factory=Axis)
    z: Axis = field(default_factory=Axis)
    feed: Axis = field(default_factory=Axis)  # mm/min
    spindle_rpm: float | None = None
    spindle_on: bool = False

    def clone(self) -> "State":
        return State(
            unit=self.unit,
            distance_mode=self.distance_mode,
            wcs=self.wcs,
            motion_mode=self.motion_mode,
            x=Axis(self.x.value, self.x.known),
            y=Axis(self.y.value, self.y.known),
            z=Axis(self.z.value, self.z.known),
            feed=Axis(self.feed.value, self.feed.known),
            spindle_rpm=self.spindle_rpm,
            spindle_on=self.spindle_on,
        )

    def unit_factor(self) -> float | None:
        if self.unit == "mm":
            return 1.0
        if self.unit == "inch":
            return 25.4
        return None

    def snapshot(self) -> dict:
        def ax(a: Axis) -> dict:
            return {"value_mm": round6(a.value), "known": a.known}

        return {
            "unit": self.unit,
            "distance_mode": self.distance_mode,
            "wcs": self.wcs,
            "motion_mode": self.motion_mode,
            "x": ax(self.x),
            "y": ax(self.y),
            "z": ax(self.z),
            "feed_mm_per_min": ax(self.feed),
            "spindle_rpm": self.spindle_rpm,
            "spindle_on": self.spindle_on,
        }


# ---------------------------------------------------------------------------
# 问题记录
# ---------------------------------------------------------------------------

@dataclass
class Issue:
    code: str
    severity: str
    line_no: int
    source: str
    normalized: str
    state_in: dict
    state_out: dict | None
    basis: str
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "title": ISSUE_TITLE[self.code],
            "severity": self.severity,
            "line_no": self.line_no,
            "source_line": self.source,
            "normalized": self.normalized,
            "state_in": self.state_in,
            "state_out": self.state_out,
            "basis": self.basis,
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# 圆弧几何
# ---------------------------------------------------------------------------

class ArcError(Exception):
    pass


def _angle(cx: float, cy: float, px: float, py: float) -> float:
    return math.atan2(py - cy, px - cx)


def _signed_sweep(a0: float, a1: float, clockwise: bool) -> float:
    """从 a0 到 a1 的带符号扫角（非整圆，结果落在 (-2π, 2π)）。"""
    if clockwise:
        d = a0 - a1
        while d <= 0:
            d += 2 * math.pi
        while d > 2 * math.pi:
            d -= 2 * math.pi
        return -d
    d = a1 - a0
    while d <= 0:
        d += 2 * math.pi
    while d > 2 * math.pi:
        d -= 2 * math.pi
    return d


def solve_arc(start, end, clockwise: bool,
              i: float | None, j: float | None,
              r_word: float | None) -> dict:
    """解算 G17 平面圆弧。I/J 优先于 R；抛 ArcError 表示无解。

    返回 {center, radius, sweep, samples, length_xy}。
    """
    x1, y1 = start
    x2, y2 = end
    chord = math.hypot(x2 - x1, y2 - y1)

    if i is not None or j is not None:
        i = i or 0.0
        j = j or 0.0
        cx, cy = x1 + i, y1 + j
        radius = math.hypot(i, j)
        if radius < GEOM_TOL:
            raise ArcError("I/J 指定的圆心与起点重合，半径为 0")
        r_end = math.hypot(x2 - cx, y2 - cy)
        tol = max(GEOM_TOL, radius * 1e-4)
        if abs(r_end - radius) > tol:
            raise ArcError(
                f"终点到圆心距离 {r_end:.6g} 与半径 {radius:.6g} 不一致"
            )
        if chord < tol:
            sweep = -2 * math.pi if clockwise else 2 * math.pi  # 整圆
        else:
            sweep = _signed_sweep(
                _angle(cx, cy, x1, y1), _angle(cx, cy, x2, y2), clockwise
            )
    elif r_word is not None:
        r = r_word
        if chord < GEOM_TOL:
            raise ArcError("R 编程圆弧的起点与终点重合（整圆请用 I/J）")
        if abs(r) * 2 < chord - GEOM_TOL:
            raise ArcError(
                f"弦长 {chord:.6g} 大于 2R={2 * abs(r):.6g}，圆弧不存在"
            )
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        h = math.sqrt(max(r * r - (chord / 2) ** 2, 0.0))
        ux, uy = (x2 - x1) / chord, (y2 - y1) / chord
        # 弦的两个垂直方向上的候选圆心
        c1 = (mx - uy * h, my + ux * h)
        c2 = (mx + uy * h, my - ux * h)
        want_major = r < 0
        cx = cy = sweep = None
        for cand in (c1, c2):
            s = _signed_sweep(
                _angle(cand[0], cand[1], x1, y1),
                _angle(cand[0], cand[1], x2, y2), clockwise)
            if h < GEOM_TOL:  # 半圆，c1 与 c2 重合（弦中点），取第一个
                cx, cy, sweep = cand[0], cand[1], s
                break
            # 两个候选圆心分别给出优弧与劣弧，按扫角大小直接匹配 R 符号
            if (abs(s) > math.pi) == want_major:
                cx, cy, sweep = cand[0], cand[1], s
                break
        if cx is None:
            raise ArcError("R 圆弧圆心候选均不满足旋向/半径符号约束")
        radius = abs(r)
    else:
        raise ArcError("圆弧段缺少圆心参数 I/J 或半径 R")

    n = int(min(512, max(16, math.ceil(abs(sweep) / (2 * math.pi) * 96))))
    a0 = _angle(cx, cy, x1, y1)
    samples = []
    for k in range(1, n + 1):
        a = a0 + sweep * (k / n)
        samples.append((cx + radius * math.cos(a),
                        cy + radius * math.sin(a)))
    samples[-1] = (x2, y2)  # 强制终点精确
    return {
        "center": (cx, cy),
        "radius": radius,
        "sweep": sweep,
        "samples": samples,
        "length_xy": abs(sweep) * radius,
    }


def _dist3(a, b) -> float | None:
    """两点距离；任一坐标未知返回 None。"""
    if any(v is None for v in a + b):
        return None
    return math.sqrt(sum((bv - av) ** 2 for av, bv in zip(a, b)))


# ---------------------------------------------------------------------------
# 分析器
# ---------------------------------------------------------------------------

class Analyzer:
    def __init__(self, config: MachineConfig, program_name: str | None = None,
                 progress=None):
        self.cfg = config
        self.program_name = program_name
        self.progress = progress
        self.state = State()
        self.issues: list[Issue] = []
        self.entries: list[dict] = []
        self.blank_count = 0
        self.executed_count = 0
        self.blocked_count = 0
        self.bmin = [math.inf] * 3
        self.bmax = [-math.inf] * 3
        self.mbmin = [math.inf] * 3
        self.mbmax = [-math.inf] * 3
        self.all_moves_wcs_known = True
        self.length_rapid = 0.0
        self.length_cutting = 0.0
        self.unknown_length_segments = 0
        self._snap_in: dict = {}

    # -- 问题记录 ----------------------------------------------------------

    def _issue(self, code: str, pl: ParsedLine, basis: str,
               details: dict | None = None, normalized: str = "") -> int:
        iss = Issue(
            code=code,
            severity=ISSUE_SEVERITY[code],
            line_no=pl.line_no,
            source=pl.source,
            normalized=normalized,
            state_in=self._snap_in,
            state_out=None,
            basis=basis,
            details=details or {},
        )
        self.issues.append(iss)
        return len(self.issues) - 1

    # -- 包围盒 / 行程 -----------------------------------------------------

    def _machine_point(self, p):
        c = self.cfg
        return [p[0] + c.offset_x if p[0] is not None else None,
                p[1] + c.offset_y if p[1] is not None else None,
                p[2] + c.offset_z if p[2] is not None else None]

    def _grow_bbox(self, pts, machine: bool):
        lo = self.mbmin if machine else self.bmin
        hi = self.mbmax if machine else self.bmax
        for p in pts:
            mp = self._machine_point(p) if machine else p
            for i, v in enumerate(mp):
                if v is None:
                    continue
                if v < lo[i]:
                    lo[i] = v
                if v > hi[i]:
                    hi[i] = v

    def _bounds_violations(self, pts) -> list[dict]:
        c = self.cfg
        limits = (("X", c.x_min, c.x_max), ("Y", c.y_min, c.y_max),
                  ("Z", c.z_min, c.z_max))
        worst: dict[str, dict] = {}
        for p in pts:
            mp = self._machine_point(p)
            for i, (axis, lo, hi) in enumerate(limits):
                v = mp[i]
                if v is None:
                    continue
                if v < lo - MM_EPS:
                    over = lo - v
                    if axis not in worst or over > worst[axis]["overshoot_mm"]:
                        worst[axis] = {"value_mm": round(v, 6), "bound_mm": lo,
                                       "overshoot_mm": round(over, 6),
                                       "side": "min"}
                elif v > hi + MM_EPS:
                    over = v - hi
                    if axis not in worst or over > worst[axis]["overshoot_mm"]:
                        worst[axis] = {"value_mm": round(v, 6), "bound_mm": hi,
                                       "overshoot_mm": round(over, 6),
                                       "side": "max"}
        return [{"axis": ax, **d} for ax, d in sorted(worst.items())]

    # -- 规范化文本 --------------------------------------------------------

    @staticmethod
    def _blocked_normalized(pl: ParsedLine) -> str:
        """阻断段只做词面规范化（清理数字、去 N、去注释），不补模态。"""
        return " ".join(f"{w.letter}{fmt_num(w.value)}" for w in pl.words
                        if w.letter != "N")

    @staticmethod
    def _collect_unsupported(pl: ParsedLine, g_words=None, m_words=None) -> list[str]:
        """列出本行全部未支持指令（保持行内出现顺序，去重）。

        用于正常段阻断；词法残缺时也调用，以便把残缺片段中恢复出的
        合法词（如 `G55 X-` 中的 G55）一并显式列出。
        """
        g_words = g_words if g_words is not None else pl.g_words
        m_words = m_words if m_words is not None else pl.m_words
        out: list[str] = []
        for w in pl.words:
            if w.letter == "G":
                tok = "G" + fmt_num(w.value)
                if g_code_key(w) not in SUPPORTED_G and tok not in out:
                    out.append(tok)
            elif w.letter == "M":
                tok = "M" + fmt_num(w.value)
                if g_code_key(w) not in SUPPORTED_M and tok not in out:
                    out.append(tok)
            elif w.letter not in ALLOWED_LETTERS:
                tok = f"{w.letter}{fmt_num(w.value)}"
                if tok not in out:
                    out.append(tok)
        return out

    @staticmethod
    def _normalized(applied_g: list[str], m_words: list[Word],
                    motion_g: str | None,
                    coord_words: list[tuple[str, float]],
                    f_val: float | None, s_val: float | None,
                    motion_is_modal: bool = False) -> str:
        """G(单位/模式/WCS/运动) -> M -> XYZIJR -> F S 的规范顺序。"""
        out: list[str] = []
        unit_g = next((g for g in applied_g if g in SETTING_G_UNIT), None)
        mode_g = next((g for g in applied_g if g in SETTING_G_MODE), None)
        if unit_g:
            out.append(f"G{unit_g}")
        if mode_g:
            out.append(f"G{mode_g}")
        if "54" in applied_g:
            out.append("G54")
        if motion_g is not None:
            out.append(f"G{motion_g}" + ("(模态)" if motion_is_modal else ""))
        for w in m_words:
            out.append("M" + fmt_num(w.value))
        for letter, v in coord_words:
            out.append(f"{letter}{fmt_num(v)}")
        if f_val is not None:
            out.append("F" + fmt_num(f_val))
        if s_val is not None:
            out.append("S" + fmt_num(s_val))
        return " ".join(out)

    # -- 主流程 ------------------------------------------------------------

    def run(self, text: str) -> dict:
        lines = parse_program(text)
        total = max(len(lines), 1)
        for pl in lines:
            self._process_line(pl)
            if self.progress:
                self.progress(min(99, int(pl.line_no / total * 100)))
        if self.progress:
            self.progress(100)
        return self._build_report(len(lines))

    def _process_line(self, pl: ParsedLine):
        self._snap_in = self.state.clone().snapshot()

        if pl.is_blank:
            self.blank_count += 1
            self.entries.append({
                "line_no": pl.line_no,
                "source_line": pl.source,
                "normalized": "",
                "type": "blank_or_comment",
                "executed": False,
                "comments": pl.comments,
                "state_in": self._snap_in,
                "state_out": self._snap_in,
            })
            return

        # 1) 词法残缺 -> 整段阻断；残缺片段中恢复出的词仍参与
        # “未支持指令”检查，两类问题都要列出
        if pl.malformed:
            normalized = self._blocked_normalized(pl)
            issue_indexes = [self._issue(
                "MALFORMED_LINE", pl,
                f"存在无法识别的片段 {pl.malformed}；按保守策略整段不执行、"
                "不改变任何模态状态",
                {"malformed_tokens": pl.malformed}, normalized)]
            unsupported = self._collect_unsupported(pl)
            if unsupported:
                issue_indexes.append(self._issue(
                    "UNSUPPORTED_INSTRUCTION", pl,
                    "同一程序段还包含本工具不支持的指令；即使语法可修复，"
                    "该段也必须按未支持指令处理，不猜测执行",
                    {"unsupported_tokens": unsupported}, normalized))
            self._finish_line(pl, "blocked", normalized, executed=False,
                              block_reason="malformed",
                              issue_indexes=issue_indexes)
            self.blocked_count += 1
            return

        g_words = pl.g_words
        m_words = pl.m_words

        # 2) 未支持指令 -> 整段阻断
        unsupported = self._collect_unsupported(pl, g_words, m_words)
        if unsupported:
            normalized = self._blocked_normalized(pl)
            idx = self._issue(
                "UNSUPPORTED_INSTRUCTION", pl,
                "该段包含本工具不支持的指令，按保守策略整段不执行、"
                "不改变任何模态状态",
                {"unsupported_tokens": unsupported}, normalized,
            )
            self._finish_line(pl, "blocked", normalized, executed=False,
                              block_reason="unsupported", issue_indexes=[idx])
            self.blocked_count += 1
            return

        # 3) 应用模态设定（同组多个取同行最后一个，如 G20 G21 -> mm）
        applied_g: list[str] = []
        last_unit = last_mode = None
        wcs_g = False
        for w in g_words:
            key = g_code_key(w)
            if key in SETTING_G_UNIT:
                self.state.unit = SETTING_G_UNIT[key]
                last_unit = key
            elif key in SETTING_G_MODE:
                self.state.distance_mode = SETTING_G_MODE[key]
                last_mode = key
            elif key == "54":
                self.state.wcs = "G54"
                wcs_g = True
        applied_g = [g for g in (last_unit, last_mode, "54" if wcs_g else None)
                     if g is not None]

        line_motion_keys = [g_code_key(w) for w in g_words
                            if g_code_key(w) in MOTION_G]
        line_motion_key = line_motion_keys[-1] if line_motion_keys else None
        if line_motion_key is not None:
            self.state.motion_mode = MOTION_G[line_motion_key]

        for w in m_words:  # 同行多个 M 按出现顺序执行
            key = g_code_key(w)
            if key == "3":
                self.state.spindle_on = True
            elif key == "5":
                self.state.spindle_on = False

        # 4) F / S
        f_raw = pl.f_words[-1].value if pl.f_words else None
        s_raw = pl.s_words[-1].value if pl.s_words else None
        issue_indexes: list[int] = []
        if f_raw is not None:
            factor = self.state.unit_factor()
            if factor is None:
                self.state.feed = Axis(None, False)
                issue_indexes.append(self._issue(
                    "UNKNOWN_UNITS", pl,
                    f"F{fmt_num(f_raw)} 出现在任何 G20/G21 之前，无法换算 "
                    "mm/min 并与进给上限比较；进给模态标记为未知",
                    {"f_program_units": f_raw}))
            else:
                feed_mm = f_raw * factor
                self.state.feed = Axis(feed_mm, True)
                if feed_mm > self.cfg.max_feed_mm_min + MM_EPS:
                    issue_indexes.append(self._issue(
                        "FEED_OVER_LIMIT", pl,
                        f"进给 {fmt_num(feed_mm)} mm/min 超过机床上限 "
                        f"{fmt_num(self.cfg.max_feed_mm_min)} mm/min（超出 "
                        f"{fmt_num(feed_mm - self.cfg.max_feed_mm_min)}）",
                        {"feed_mm_per_min": round(feed_mm, 6),
                         "limit_mm_per_min": self.cfg.max_feed_mm_min,
                         "exceed_mm_per_min":
                             round(feed_mm - self.cfg.max_feed_mm_min, 6)}))
        if s_raw is not None:
            self.state.spindle_rpm = s_raw
            if s_raw > self.cfg.max_spindle_rpm + MM_EPS:
                issue_indexes.append(self._issue(
                    "SPINDLE_OVER_LIMIT", pl,
                    f"主轴转速 {fmt_num(s_raw)} rpm 超过机床上限 "
                    f"{fmt_num(self.cfg.max_spindle_rpm)} rpm（超出 "
                    f"{fmt_num(s_raw - self.cfg.max_spindle_rpm)}）",
                    {"rpm": s_raw,
                     "limit_rpm": self.cfg.max_spindle_rpm,
                     "exceed_rpm":
                         round(s_raw - self.cfg.max_spindle_rpm, 6)}))

        axis_words = {w.letter: w.value for w in pl.words if w.letter in "XYZ"}
        ij_words = {w.letter: w.value for w in pl.words if w.letter in "IJ"}
        r_word = next((w.value for w in pl.words if w.letter == "R"), None)
        coord_words = [(w.letter, w.value) for w in pl.words
                       if w.letter in ("X", "Y", "Z", "I", "J", "R")]

        # 5) 无轴坐标词 => 纯设定段（即使本行写了 G0-G3 也不产生位移）
        if not axis_words:
            normalized = self._normalized(
                applied_g, m_words, line_motion_key, coord_words, f_raw, s_raw)
            self._finish_line(pl, "setting", normalized, executed=True,
                              issue_indexes=issue_indexes)
            return

        # 有轴坐标词但没有任何运动模态 -> 不猜测运动
        if self.state.motion_mode is None:
            normalized = self._normalized(
                applied_g, m_words, None, coord_words, f_raw, s_raw)
            issue_indexes.append(self._issue(
                "NO_MOTION_MODE", pl,
                "出现轴坐标词，但本行与此前都没有 G0-G3；不猜测运动，"
                "轴坐标不更新",
                {"axis_words":
                     [f"{k}{fmt_num(v)}" for k, v in axis_words.items()]},
                normalized))
            self._finish_line(pl, "setting", normalized, executed=False,
                              block_reason="no_motion_mode",
                              issue_indexes=issue_indexes)
            return

        motion_mode = self.state.motion_mode
        modal_key = {v: k for k, v in MOTION_G.items()}[motion_mode]
        motion_is_modal = line_motion_key is None
        motion_g = line_motion_key or modal_key

        # 6) 单位 / 定位模式不明的运动
        if self.state.unit_factor() is None:
            issue_indexes.append(self._issue(
                "UNKNOWN_UNITS", pl,
                "运动发生在任何 G20/G21 之前，物理尺寸无法确定；"
                "刀具位置标记为未知，跳过行程/包围盒/长度计算", {}))
        if self.state.distance_mode is None:
            issue_indexes.append(self._issue(
                "UNKNOWN_DISTANCE_MODE", pl,
                "出现轴坐标词，但 G90/G91 尚未建立，无法判定绝对/增量定位；"
                "刀具位置标记为未知",
                {"axis_words":
                     [f"{k}{fmt_num(v)}" for k, v in axis_words.items()]}))

        start_pt = self._current_point()
        segment = None
        if self.state.unit_factor() is None or self.state.distance_mode is None:
            # 位置整体退化为未知，只保留主轴/进给等模态检查
            for letter in axis_words:
                self._set_axis(letter, Axis(None, False))
            self.unknown_length_segments += 1
        else:
            # 解算目标坐标（G91 下若起点轴未知，该轴目标未知）
            factor = self.state.unit_factor()
            cur = {"X": self.state.x, "Y": self.state.y, "Z": self.state.z}
            for letter, raw in axis_words.items():
                if self.state.distance_mode == "absolute":
                    self._set_axis(letter, Axis(raw * factor, True))
                elif cur[letter].known and cur[letter].value is not None:
                    self._set_axis(
                        letter, Axis(cur[letter].value + raw * factor, True))
                else:
                    self._set_axis(letter, Axis(None, False))
            end_pt = self._current_point()

            if motion_mode in ("arc_cw", "arc_ccw"):
                arc = self._build_arc(
                    pl, motion_mode, start_pt, end_pt, ij_words, r_word,
                    issue_indexes)
                if arc is None:
                    # 几何无解：回滚本行全部状态改动，整段阻断
                    self._rollback_to(self._snap_in)
                    normalized = self._blocked_normalized(pl)
                    self._finish_line(pl, "blocked", normalized,
                                      executed=False,
                                      block_reason="arc_no_solution",
                                      issue_indexes=issue_indexes)
                    self.blocked_count += 1
                    return
                segment = arc
            else:
                length = _dist3(start_pt, end_pt)
                if length is None:
                    self.unknown_length_segments += 1
                segment = {
                    "kind": motion_mode,
                    "start": start_pt,
                    "end": end_pt,
                    "points": [start_pt, end_pt],
                    "length_mm": length,
                }

            self._run_segment_checks(pl, motion_mode, segment, issue_indexes)
            self._accumulate(motion_mode, segment)

        normalized = self._normalized(
            applied_g, m_words, motion_g,
            coord_words, f_raw, s_raw, motion_is_modal)
        self._finish_line(
            pl, motion_mode, normalized, executed=True,
            segment=self._segment_out(segment),
            physical_known=segment is not None,
            issue_indexes=issue_indexes)
        self.executed_count += 1

    def _rollback_to(self, snapshot: dict):
        """圆弧无解时把状态恢复到进入本行前的快照。"""
        def ax(d):
            return Axis(d["value_mm"], d["known"])

        s = State(
            unit=snapshot["unit"],
            distance_mode=snapshot["distance_mode"],
            wcs=snapshot["wcs"],
            motion_mode=snapshot["motion_mode"],
            x=ax(snapshot["x"]), y=ax(snapshot["y"]), z=ax(snapshot["z"]),
            feed=ax(snapshot["feed_mm_per_min"]),
            spindle_rpm=snapshot["spindle_rpm"],
            spindle_on=snapshot["spindle_on"],
        )
        self.state = s

    # -- 圆弧 --------------------------------------------------------------

    def _build_arc(self, pl, motion_mode, start_pt, end_pt, ij_words, r_word,
                   issue_indexes):
        if any(v is None for v in start_pt[:2] + end_pt[:2]):
            issue_indexes.append(self._issue(
                "ARC_NO_SOLUTION", pl,
                "圆弧起点或终点 XY 坐标未知（此前位置未建立），"
                "无法解算几何；整段不执行并回滚本行状态",
                {"start_mm": [round6(v) for v in start_pt],
                 "end_mm": [round6(v) for v in end_pt]},
                self._blocked_normalized(pl)))
            return None

        factor = self.state.unit_factor() or 1.0
        i = ij_words.get("I")
        j = ij_words.get("J")
        i = i * factor if i is not None else None
        j = j * factor if j is not None else None
        r_mm = r_word * factor if r_word is not None else None
        try:
            sol = solve_arc(
                (start_pt[0], start_pt[1]), (end_pt[0], end_pt[1]),
                clockwise=(motion_mode == "arc_cw"), i=i, j=j, r_word=r_mm)
        except ArcError as e:
            issue_indexes.append(self._issue(
                "ARC_NO_SOLUTION", pl,
                f"圆弧几何无解：{e}；整段不执行，本行模态改动全部回滚",
                {"reason": str(e),
                 "start_mm": [round6(v) for v in start_pt],
                 "end_mm": [round6(v) for v in end_pt],
                 "i_mm": round6(i), "j_mm": round6(j),
                 "r_mm": round6(r_mm)},
                self._blocked_normalized(pl)))
            return None

        z0, z1 = start_pt[2], end_pt[2]
        n = len(sol["samples"])
        points = []
        for k, (sx, sy) in enumerate(sol["samples"], start=1):
            if z0 is not None and z1 is not None:
                points.append([sx, sy, z0 + (z1 - z0) * (k / n)])
            else:
                points.append([sx, sy, None])
        points[-1] = list(end_pt)
        xy_len = sol["length_xy"]
        length = None
        if z0 is not None and z1 is not None:
            length = math.sqrt(xy_len ** 2 + (z1 - z0) ** 2)
        else:
            self.unknown_length_segments += 1
        return {
            "kind": motion_mode,
            "start": start_pt,
            "end": end_pt,
            "points": [start_pt] + points,
            "length_mm": length,
            "arc": {
                "plane": "G17(XY)",
                "programming": "I/J" if (i is not None or j is not None) else "R",
                "center_mm": [round(sol["center"][0], 6),
                              round(sol["center"][1], 6)],
                "radius_mm": round(sol["radius"], 6),
                "sweep_deg": round(math.degrees(sol["sweep"]), 6),
                "helical": (z0 is not None and z1 is not None
                            and abs(z1 - z0) > MM_EPS),
                "z_change_mm": (round(z1 - z0, 6)
                                if z0 is not None and z1 is not None else None),
            },
        }

    # -- 段级检查 ----------------------------------------------------------

    def _run_segment_checks(self, pl, motion_mode, segment, issue_indexes):
        points = segment["points"]

        # 行程检查需要工件坐标系
        if self.state.wcs is None:
            self.all_moves_wcs_known = False
            issue_indexes.append(self._issue(
                "UNKNOWN_WCS", pl,
                "运动发生在 G54 建立之前，缺少工件坐标偏置映射，跳过行程检查",
                {}))
        else:
            for v in self._bounds_violations(points):
                c = self.cfg
                bound = {"X": (c.x_min, c.x_max),
                         "Y": (c.y_min, c.y_max),
                         "Z": (c.z_min, c.z_max)}[v["axis"]]
                issue_indexes.append(self._issue(
                    "OUT_OF_BOUNDS", pl,
                    f"{v['axis']} 轴机床坐标 {fmt_num(v['value_mm'])} mm 越出行程"
                    f"边界 {fmt_num(v['bound_mm'])} mm（超程 "
                    f"{fmt_num(v['overshoot_mm'])} mm；已叠加 G54 偏置）",
                    v))
            self._grow_bbox(points, machine=True)

        self._grow_bbox(points, machine=False)

        if motion_mode == "rapid":
            zs = [p[2] for p in points if p[2] is not None]
            s0, s1 = segment["start"], segment["end"]
            xy_known = all(v is not None for v in
                           (s0[0], s0[1], s1[0], s1[1]))
            xy_move = (xy_known and
                       math.hypot(s1[0] - s0[0], s1[1] - s0[1]) > MM_EPS)
            end_below = s1[2] is not None and s1[2] < self.cfg.safe_z - MM_EPS
            horiz_below = (xy_move and zs
                           and min(zs) < self.cfg.safe_z - MM_EPS)
            # 纯垂直抬刀必然经过当前低 Z，不报警；
            # 报警条件：快速终点低于安全 Z，或安全 Z 以下存在水平快速移动
            if end_below or horiz_below:
                z_ref = s1[2] if end_below else min(zs)
                issue_indexes.append(self._issue(
                    "RAPID_BELOW_SAFE_Z", pl,
                    f"快速移动到达/经过 Z={fmt_num(z_ref)} mm（工件坐标），"
                    f"低于安全 Z {fmt_num(self.cfg.safe_z)} mm（低 "
                    f"{fmt_num(self.cfg.safe_z - z_ref)} mm）",
                    {"ref_z_mm": round(z_ref, 6),
                     "safe_z_mm": self.cfg.safe_z,
                     "below_mm": round(self.cfg.safe_z - z_ref, 6),
                     "end_below_safe_z": end_below,
                     "horizontal_travel_below_safe_z": horiz_below}))

        if motion_mode in ("linear", "arc_cw", "arc_ccw"):
            if not self.state.spindle_on:
                issue_indexes.append(self._issue(
                    "SPINDLE_NOT_RUNNING", pl,
                    f"{MOTION_CN[motion_mode]}切削发生时主轴处于停止状态"
                    f"（spindle_on=false，最近 S={self.state.spindle_rpm}）",
                    {"spindle_on": False,
                     "last_s_rpm": self.state.spindle_rpm}))
            if not self.state.feed.known:
                issue_indexes.append(self._issue(
                    "FEED_UNSET", pl,
                    f"{MOTION_CN[motion_mode]}切削前未建立有效进给 F"
                    "（单位不明或从未给定）",
                    {"feed_known": False}))

    # -- 累计与输出 --------------------------------------------------------

    def _accumulate(self, motion_mode, segment):
        length = segment["length_mm"]
        if length is not None:
            if motion_mode == "rapid":
                self.length_rapid += length
            else:
                self.length_cutting += length

    def _current_point(self):
        def g(a: Axis):
            return a.value if a.known else None
        return [g(self.state.x), g(self.state.y), g(self.state.z)]

    def _set_axis(self, letter: str, ax: Axis):
        setattr(self.state, letter.lower(), ax)

    def _finish_line(self, pl, type_, normalized, executed, segment=None,
                     physical_known=True, block_reason=None,
                     issue_indexes=None):
        snap_out = self.state.snapshot()
        entry = {
            "line_no": pl.line_no,
            "source_line": pl.source,
            "normalized": normalized,
            "type": type_,
            "executed": executed,
            "physical_known": physical_known,
            "comments": pl.comments,
            "state_in": self._snap_in,
            "state_out": snap_out,
        }
        if block_reason:
            entry["block_reason"] = block_reason
        if segment is not None:
            entry["segment"] = segment
        if issue_indexes:
            entry["issue_codes"] = [self.issues[i].code for i in issue_indexes]
        self.entries.append(entry)
        for i in issue_indexes or []:
            iss = self.issues[i]
            if not iss.normalized:
                iss.normalized = normalized
            iss.state_out = snap_out

    def _segment_out(self, segment):
        if segment is None:
            return None
        out = {
            "kind": segment["kind"],
            "start_mm": [round6(v) for v in segment["start"]],
            "end_mm": [round6(v) for v in segment["end"]],
            "length_mm": round6(segment["length_mm"]),
            "points_mm": [[round6(c) for c in p] for p in segment["points"]],
        }
        if "arc" in segment:
            out["arc"] = segment["arc"]
        return out

    def _bbox_out(self, bmin, bmax):
        if any(math.isinf(v) for v in bmin):
            return None
        return {
            "x_mm": [round(bmin[0], 6), round(bmax[0], 6)],
            "y_mm": [round(bmin[1], 6), round(bmax[1], 6)],
            "z_mm": [round(bmin[2], 6), round(bmax[2], 6)],
            "size_mm": [round(bmax[0] - bmin[0], 6),
                        round(bmax[1] - bmin[1], 6),
                        round(bmax[2] - bmin[2], 6)],
        }

    def _build_report(self, physical_lines: int) -> dict:
        counts = {s: 0 for s in SEVERITY_ORDER}
        for iss in self.issues:
            counts[iss.severity] += 1
        score = min(100, sum(SEVERITY_WEIGHT[s] * n
                             for s, n in counts.items()))
        level = ("none" if score == 0 else "low" if score <= 10
                 else "medium" if score <= 30 else "high" if score <= 60
                 else "critical")

        return {
            "program": {
                "name": self.program_name,
                "physical_lines": physical_lines,
                "blank_or_comment_lines": self.blank_count,
                "executed_lines": self.executed_count,
                "blocked_lines": self.blocked_count,
            },
            "machine": self.cfg.to_dict(),
            "final_state": self.state.snapshot(),
            "bbox_program_mm": self._bbox_out(self.bmin, self.bmax),
            "bbox_machine_mm": (
                self._bbox_out(self.mbmin, self.mbmax)
                if self.all_moves_wcs_known else None),
            "machine_bbox_note": (
                None if self.all_moves_wcs_known
                else "存在 G54 建立之前的运动，无法给出完整机床坐标包围盒"),
            "path_length_mm": {
                "rapid": round(self.length_rapid, 6),
                "cutting": round(self.length_cutting, 6),
                "total": round(self.length_rapid + self.length_cutting, 6),
                "reliable": self.unknown_length_segments == 0,
                "unknown_segments": self.unknown_length_segments,
            },
            "risk": {
                "score": score,
                "level": level,
                "counts_by_severity": counts,
                "total_issues": len(self.issues),
            },
            "issues": [i.to_dict() for i in self.issues],
            "trajectory": self.entries,
            "policies": {
                "units": "G21=mm，G20=inch（内部乘 25.4 换算 mm）；"
                         "G20/G21 出现前的物理检查显式报 UNKNOWN_UNITS",
                "distance": "G90 绝对 / G91 增量；未建立时位置标记未知",
                "wcs": "仅支持 G54，偏置取自作业配置；G54 前跳过行程检查",
                "arc": "仅 G17(XY) 平面，I/J 优先于 R（R 负=优弧）；"
                       "几何无解整段阻断并回滚",
                "block": "含未支持指令或无法解析的程序段整段阻断，"
                         "不改变任何模态",
                "safe_z": "安全 Z 按工件(程序)坐标判定",
                "feed": "F 按出现时的单位换算为 mm/min 后模态保持",
            },
        }


def analyze_program(text: str, config: MachineConfig,
                    program_name: str | None = None,
                    progress=None) -> dict:
    return Analyzer(config, program_name, progress).run(text)


DIALECT = {
    "supported_g": {
        "G0": "快速定位", "G1": "直线插补",
        "G2": "顺时针圆弧（G17，I/J 或 R）",
        "G3": "逆时针圆弧（G17，I/J 或 R）",
        "G20": "英制单位", "G21": "公制单位",
        "G90": "绝对定位", "G91": "增量定位",
        "G54": "工件坐标系 1（偏置由配置提供）",
    },
    "supported_m": {"M3": "主轴正转", "M5": "主轴停止"},
    "supported_words": ["X", "Y", "Z", "I", "J", "R", "F", "S", "N(忽略)"],
    "comments": ["(圆括号注释)", ";分号注释"],
    "unsupported_policy": "任何未列出的 G/M 指令及其他地址词均显式报告，"
                          "并整段阻断，不猜测执行",
    "unsupported_examples": [
        "G17/G18/G19 平面选择", "G28/G30 回零",
        "G40-G43 刀补", "G54.1/G55-G59 其他工件坐标系",
        "G80-G89 固定循环", "圆弧 K 参数（仅 G17，用 I/J）",
        "M2/M30 程序结束", "M4 反转", "M6 换刀", "M7-M9 冷却",
        "T 刀号", "H/D 刀补号", "P/Q/L 等参数",
    ],
    "severity_levels": SEVERITY_ORDER,
}
