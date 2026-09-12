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
from .cycles import (
    CYCLE_G,
    RETURN_G,
    RETURN_CN,
    CycleDef,
    CycleParam,
    expand_hole,
    positioning_move,
    resolve_r,
    resolve_z,
    PECK_APPROACH_MM,
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
    "CYCLE_MISSING_PARAMS": "error",       # 固定循环缺少 Z/R（G83 含 Q）
    "CYCLE_BAD_PARAM": "error",            # Q<=0、P 或 L 非法、平面顺序矛盾
    "CYCLE_PLANE_CONFLICT": "error",       # 孔底与 R 平面 / 初始平面顺序矛盾
    "CYCLE_NO_INHERITABLE_STATE": "error",  # 后续孔位没有可继承的循环/位置状态
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
    "CYCLE_MISSING_PARAMS": "固定循环缺少必要参数（对应孔已阻断）",
    "CYCLE_BAD_PARAM": "固定循环参数非法（对应孔已阻断）",
    "CYCLE_PLANE_CONFLICT": "固定循环平面顺序矛盾（对应孔已阻断）",
    "CYCLE_NO_INHERITABLE_STATE": "后续孔位没有可继承的循环状态（该孔已阻断）",
}

ALLOWED_LETTERS = {"G", "M", "X", "Y", "Z", "I", "J", "R", "F", "S", "N",
                   "Q", "P", "L"}
MOTION_G = {"0": "rapid", "1": "linear", "2": "arc_cw", "3": "arc_ccw"}
MOTION_CN = {"rapid": "快速", "linear": "直线",
             "arc_cw": "顺时针圆弧", "arc_ccw": "逆时针圆弧"}
SETTING_G_UNIT = {"20": "inch", "21": "mm"}
SETTING_G_MODE = {"90": "absolute", "91": "relative"}
# 固定循环返回平面模态 G98/G99 的中文名
RETURN_MODE_G = {"98": "G98", "99": "G99"}

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
    # 固定钻孔循环：激活时为 CycleDef，G80/G0-G3/新循环定义时变更
    cycle: CycleDef | None = None
    # G98/G99 返回平面偏好（循环外也模态保持，默认 G98）
    pending_return: str = "initial"
    pending_return_line: int | None = None
    pending_return_source: str | None = None
    pending_return_default: bool = True

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
            cycle=self.cycle.clone() if self.cycle is not None else None,
            pending_return=self.pending_return,
            pending_return_line=self.pending_return_line,
            pending_return_source=self.pending_return_source,
            pending_return_default=self.pending_return_default,
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
            "canned_cycle": (self.cycle.params_out()
                             if self.cycle is not None else None),
            "cycle_return_plane": ("G98" if self.pending_return == "initial"
                                   else "G99"),
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
        # 固定循环统计
        self.cycle_groups: list[dict] = []
        self._cycle_group_map: dict[int, dict] = {}
        self.hole_seq = 0          # 全程序孔序（从 1 开始，阻断孔也占位）
        self.hole_ok = 0
        self.hole_blocked = 0
        self.length_cycle_rapid = 0.0
        self.length_cycle_cutting = 0.0
        self.bmin = [math.inf] * 3
        self.bmax = [-math.inf] * 3
        self.mbmin = [math.inf] * 3
        self.mbmax = [-math.inf] * 3
        self.all_moves_wcs_known = True
        self.length_rapid = 0.0
        self.length_cutting = 0.0
        self.unknown_length_segments = 0
        self.length_cycle_rapid = 0.0
        self.length_cycle_cutting = 0.0
        self._snap_in: dict = {}
        self._snap_in_cycle: CycleDef | None = None
        self._snap_in_return = "initial"
        self._snap_in_return_line: int | None = None
        self._snap_in_return_source: str | None = None
        self._snap_in_return_default = True
        # (line_no, code) -> 已登记问题索引：循环展开动作的工艺问题按行去重
        self._line_dedup: dict[tuple[int, str], int] = {}

    # -- 问题记录 ----------------------------------------------------------

    # 同一触发行内按代码去重的工艺问题：G83 一个孔有多次进给动作，
    # 主轴未转/无进给只报一次（以触发行而非每个展开动作计）
    LINE_DEDUPE_CODES = {"SPINDLE_NOT_RUNNING", "FEED_UNSET"}

    def _issue(self, code: str, pl: ParsedLine, basis: str,
               details: dict | None = None, normalized: str = "",
               line_dedupe: bool = False) -> int:
        if line_dedupe:
            key = (pl.line_no, code)
            existing = self._line_dedup.get(key)
            if existing is not None:
                return existing
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
        idx = len(self.issues) - 1
        if line_dedupe:
            self._line_dedup[(pl.line_no, code)] = idx
        return idx

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
        # 圆弧无解回滚时需要原样恢复固定循环定义（snapshot 只含可读副本）
        self._snap_in_cycle = (self.state.cycle.clone()
                               if self.state.cycle is not None else None)
        self._snap_in_return = self.state.pending_return
        self._snap_in_return_line = self.state.pending_return_line
        self._snap_in_return_source = self.state.pending_return_source
        self._snap_in_return_default = self.state.pending_return_default

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

        issue_indexes: list[int] = []

        # 分类本行 G 词（G80-G83、G98/G99 为固定循环组，G0-G3 为运动组）
        keys = [g_code_key(w) for w in g_words]
        line_cycle_key = next((k for k in reversed(keys) if k in CYCLE_G), None)
        line_return_key = next((k for k in reversed(keys) if k in RETURN_G),
                               None)
        line_motion_keys = [k for k in keys if k in MOTION_G]
        line_motion_key = line_motion_keys[-1] if line_motion_keys else None

        # G98/G99：返回平面偏好（循环内外都模态保持）
        if line_return_key is not None:
            self.state.pending_return = RETURN_G[line_return_key]
            self.state.pending_return_line = pl.line_no
            self.state.pending_return_source = pl.source
            self.state.pending_return_default = False
            if self.state.cycle is not None:
                self.state.cycle.return_mode = RETURN_G[line_return_key]
                self.state.cycle.return_mode_line = pl.line_no
                self.state.cycle.return_mode_source = pl.source
                self.state.cycle.return_mode_default = False

        # 运动组（G0-G3/G80-G83）按同行最后一个判定归属
        group_keys = [(i, k) for i, k in enumerate(keys)
                      if k in MOTION_G or k in CYCLE_G or k == "80"]
        last_group = group_keys[-1][1] if group_keys else None
        if last_group in MOTION_G:
            # G0-G3 收尾 => 取消激活的固定循环，进入普通运动
            if self.state.cycle is not None:
                self._close_active_cycle(pl)
            self.state.cycle = None
            self.state.motion_mode = MOTION_G[last_group]
            line_motion_key = last_group
        elif last_group == "80":
            # G80：取消固定循环（不建立 G0/G1 运动模态）
            if self.state.cycle is not None:
                self._close_active_cycle(pl)
            self.state.cycle = None
            self.state.motion_mode = None
        elif line_cycle_key is not None:
            self.state.motion_mode = None
            self._activate_cycle(pl, CYCLE_G[line_cycle_key], issue_indexes)

        for w in m_words:  # 同行多个 M 按出现顺序执行
            key = g_code_key(w)
            if key == "3":
                self.state.spindle_on = True
            elif key == "5":
                self.state.spindle_on = False

        # 4) F / S
        f_raw = pl.f_words[-1].value if pl.f_words else None
        s_raw = pl.s_words[-1].value if pl.s_words else None
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
        q_word = next((w.value for w in pl.words if w.letter == "Q"), None)
        p_word = next((w.value for w in pl.words if w.letter == "P"), None)
        l_word = next((w.value for w in pl.words if w.letter == "L"), None)
        coord_words = [(w.letter, w.value) for w in pl.words
                       if w.letter in ("X", "Y", "Z", "I", "J", "R", "Q",
                                       "P", "L")]

        # 5) 固定循环处理（本行定义/重定义循环，或在激活循环上给出触发词）
        # 触发词：X/Y（新孔位）或 L（重复孔位，即使没有轴词）；单独的
        # Z/R/Q/P 只是模态参数更新，不立即钻孔
        # （循环定义行即使无 X/Y/L 也在当前位置执行）
        cycle_trigger_words = {"X", "Y", "L"}
        is_definition = line_cycle_key is not None
        has_l_trigger = l_word is not None
        has_trigger = self.state.cycle is not None and (
            any(w.letter in ("X", "Y") for w in pl.words) or has_l_trigger)
        if is_definition or has_trigger:
            if line_motion_key is not None:
                # G81 G0 X.. 这类混合段以运动组最后者为准；
                # 最后者为 G0-G3 时走普通运动（上面已取消循环）
                pass
            else:
                self._handle_cycle_line(
                    pl, applied_g, m_words, line_cycle_key,
                    axis_words, r_word, q_word, p_word, l_word,
                    f_raw, s_raw, coord_words, issue_indexes,
                    trigger=(has_trigger or is_definition))
                return

        # 激活循环上仅给参数（Z/R/Q/P）而无孔位/重复触发词：
        # 只更新模态参数，不触发孔加工
        if (self.state.cycle is not None and not is_definition
                and not has_trigger
                and any(w.letter in ("Z", "R", "Q", "P") for w in pl.words)):
            factor = self.state.unit_factor() or 1.0
            bad = self._apply_cycle_words(pl, self.state.cycle, factor,
                                          issue_indexes,
                                          first_activation=False)
            if bad:
                issue_indexes.append(self._issue(
                    "CYCLE_BAD_PARAM", pl,
                    "固定循环参数非法：" + self._bad_param_text(bad)
                    + "；非法参数不登记，循环定义保持不变",
                    {"cycle": self.state.cycle.cycle, "bad": bad,
                     "definition_line_no": self.state.cycle.def_line_no}))
            normalized = self._cycle_normalized(
                pl, self.state.cycle, None, f_raw, s_raw)
            self._finish_line(pl, "setting", normalized, executed=True,
                              issue_indexes=issue_indexes)
            return

        # 6) 无轴坐标词 => 纯设定段（即使本行写了 G0-G3 也不产生位移）
        if not axis_words:
            normalized = self._normalized(
                applied_g, m_words, line_motion_key, coord_words, f_raw, s_raw)
            if "80" in keys:
                normalized = (normalized + " " if normalized else "") + "G80(取消循环)"
            if line_return_key is not None:
                normalized = (normalized + " " if normalized else "") + (
                    f"G{line_return_key}(返回{RETURN_CN[RETURN_G[line_return_key]]})")
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
            cycle=(self._snap_in_cycle.clone()
                   if self._snap_in_cycle is not None else None),
            pending_return=self._snap_in_return,
            pending_return_line=self._snap_in_return_line,
            pending_return_source=self._snap_in_return_source,
            pending_return_default=self._snap_in_return_default,
        )
        self.state = s

    # -- 固定钻孔循环 ------------------------------------------------------

    def _group_for(self, cd: CycleDef) -> dict:
        """取循环定义对应的组记录（同一 def 生命周期共享）。

        以 CycleDef.uid 为稳定主键：不能用 id(cd)，旧定义被 GC 后
        id 会被新定义复用，导致 300 次交替定义时分组串组。
        """
        g = self._cycle_group_map.get(cd.uid)
        if g is None:
            g = {
                "uid": cd.uid,
                "cycle": cd.cycle,
                "definition_line_no": cd.def_line_no,
                "definition_source_line": cd.def_source_line,
                "cancel_line_no": None,
                "cancel_source_line": None,
                "initial_plane_z_mm": round6(cd.initial_z),
                "parameters": cd.params_out(),
                "holes": [],
                "hole_count": 0,
                "executed_holes": 0,
                "blocked_holes": 0,
                "total_drill_depth_mm": 0.0,
                "total_dwell_s": 0.0,
                "expanded_path_mm": {"rapid": 0.0, "cutting": 0.0,
                                     "total": 0.0},
            }
            self._cycle_group_map[cd.uid] = g
            self.cycle_groups.append(g)
        return g

    def _close_active_cycle(self, pl: ParsedLine | None = None):
        """G80/G0-G3 关闭当前循环组（记录取消行，不改变已展开轨迹）。"""
        cd = self.state.cycle
        if cd is None:
            return
        g = self._group_for(cd)
        g["parameters"] = cd.params_out()
        g["initial_plane_z_mm"] = round6(cd.initial_z)
        if pl is not None:
            g["cancel_line_no"] = pl.line_no
            g["cancel_source_line"] = pl.source

    def _activate_cycle(self, pl: ParsedLine, cycle: str,
                        issue_indexes: list[int]):
        """处理本行的 G81/G82/G83（建立/重定义循环，参数合并由
        _handle_cycle_line 统一完成，避免重复登记来源）。"""
        prev = self.state.cycle
        cur_z = self.state.z.value if self.state.z.known else None
        if prev is not None:
            self._close_active_cycle(pl)
            # 同族参数模态继承（Z/R/P 对所有循环，Q 对 G83 有意义）
            cd = CycleDef(
                cycle=cycle, def_line_no=pl.line_no,
                def_source_line=pl.source,
                initial_z=(prev.initial_z if cur_z is None else cur_z))
            if prev.z is not None:
                cd.z = self._inherit_param(prev.z)
            if prev.r is not None:
                cd.r = self._inherit_param(prev.r)
            if prev.p is not None:
                cd.p = self._inherit_param(prev.p)
            if cycle == "G83" and prev.q is not None:
                cd.q = self._inherit_param(prev.q)
            cd.return_mode = prev.return_mode
            cd.return_mode_line = prev.return_mode_line
            cd.return_mode_source = prev.return_mode_source
            cd.return_mode_default = prev.return_mode_default
        else:
            cd = CycleDef(
                cycle=cycle, def_line_no=pl.line_no,
                def_source_line=pl.source,
                initial_z=(cur_z if cur_z is not None else math.nan))
        # G98/G99 以当前返回平面偏好为准（可能在循环外预先指定）
        cd.return_mode = self.state.pending_return
        cd.return_mode_line = self.state.pending_return_line
        cd.return_mode_source = self.state.pending_return_source
        cd.return_mode_default = self.state.pending_return_default
        self.state.cycle = cd
        self._group_for(cd)

    @staticmethod
    def _inherit_param(p: CycleParam) -> CycleParam:
        return CycleParam(value=p.value, line_no=p.line_no,
                          source_line=p.source_line,
                          history=[dict(h) for h in p.history])

    def _apply_cycle_words(self, pl, cd: CycleDef, factor: float,
                           issue_indexes: list[int],
                           first_activation: bool) -> dict:
        """把本行的 Z/R/Q/P 合并进循环定义（mm/s），返回非法参数信息。"""
        bad: dict = {}
        mode = self.state.distance_mode
        words = {w.letter: w for w in pl.words}

        def set_param(name, val_mm, raw, history_note=False):
            old = getattr(cd, name)
            prog = None if self.state.unit_factor() is None else raw
            if old is None:
                setattr(cd, name, CycleParam.from_word(
                    val_mm, pl.line_no, pl.source, program_value=prog))
            else:
                old.update(val_mm, pl.line_no, pl.source, program_value=prog)

        if "R" in words and mode is not None:
            raw_r = words["R"].value
            init_z = cd.initial_z if not math.isnan(cd.initial_z) else (
                self.state.z.value if self.state.z.known else 0.0)
            r_abs = resolve_r(raw_r, factor, mode, init_z)
            set_param("r", r_abs, raw_r)
        if "Z" in words and mode is not None:
            raw_z = words["Z"].value
            r_abs = cd.r.value if cd.r is not None else None
            init_z = (cd.initial_z if not math.isnan(cd.initial_z)
                      else (self.state.z.value if self.state.z.known else None))
            z_abs = resolve_z(raw_z, factor, mode, r_abs=r_abs,
                              initial_z=init_z)
            set_param("z", z_abs, raw_z)
        if "Q" in words:
            raw_q = words["Q"].value
            q_mm = raw_q * factor
            if q_mm <= 0:
                bad["q"] = {"program_value": raw_q, "value_mm": q_mm}
            else:
                set_param("q", q_mm, raw_q)
        if "P" in words:
            raw_p = words["P"].value
            if raw_p < 0:
                bad["p"] = {"program_value": raw_p,
                            "reason": "P 必须为非负数（整数按 ms、小数按 s）"}
            else:
                p_s = raw_p / 1000.0 if raw_p >= 1 else raw_p
                set_param("p", p_s, raw_p)
        return bad

    def _handle_cycle_line(self, pl, applied_g, m_words, line_cycle_key,
                           axis_words, r_word, q_word, p_word, l_word,
                           f_raw, s_raw, coord_words, issue_indexes,
                           trigger: bool = True):
        """循环定义行/触发行：合并参数并按 L 展开孔位。"""
        cd = self.state.cycle
        factor = self.state.unit_factor() or 1.0
        bad = self._apply_cycle_words(
            pl, cd, factor, issue_indexes,
            first_activation=(cd.def_line_no == pl.line_no))

        # L：默认 1；必须为正整数
        reps = 1
        if l_word is not None:
            if l_word <= 0 or abs(l_word - round(l_word)) > MM_EPS:
                bad["l"] = {"program_value": l_word,
                            "reason": "L 必须为正整数（重复孔位数）"}
            else:
                reps = int(round(l_word))

        normalized = self._cycle_normalized(
            pl, cd, line_cycle_key, f_raw, s_raw)

        # 阻断条件一：非法参数（Q<=0 / P<0 / L 非法）
        block_codes: list[str] = []
        if bad:
            block_codes.append("CYCLE_BAD_PARAM")
            issue_indexes.append(self._issue(
                "CYCLE_BAD_PARAM", pl,
                "固定循环参数非法：" + self._bad_param_text(bad)
                + "；按保守策略本行对应孔全部阻断，原程序不变",
                {"cycle": cd.cycle, "bad": bad,
                 "definition_line_no": cd.def_line_no}, normalized))

        # 阻断条件二：孔底与 R 平面顺序矛盾
        plane_conflict = (
            cd.z is not None and cd.r is not None
            and cd.z.value > cd.r.value + MM_EPS)
        if plane_conflict:
            block_codes.append("CYCLE_PLANE_CONFLICT")
            issue_indexes.append(self._issue(
                "CYCLE_PLANE_CONFLICT", pl,
                f"孔底 Z={fmt_num(cd.z.value)} 高于 R 平面 Z={fmt_num(cd.r.value)}，"
                "平面顺序矛盾（必须 孔底 <= R 平面）；本行对应孔全部阻断",
                {"cycle": cd.cycle,
                 "z_bottom_mm": round6(cd.z.value),
                 "r_plane_mm": round6(cd.r.value),
                 "definition_line_no": cd.def_line_no}, normalized))

        # 阻断条件三：缺少 Z/R（G83 还需给过正的 Q；Q<=0 已在条件一报告）
        missing = [m for m in cd.missing()
                   if not (m == "Q" and "q" in bad)]
        if cd.cycle == "G83" and cd.q is not None and cd.q.value > 0:
            missing = [m for m in missing if m != "Q"]
        if missing:
            block_codes.append("CYCLE_MISSING_PARAMS")
            params_detail = {}
            for name, p in (("Z", cd.z), ("R", cd.r)):
                params_detail[name] = (
                    {"line_no": p.line_no, "source_line": p.source_line}
                    if p is not None else None)
            issue_indexes.append(self._issue(
                "CYCLE_MISSING_PARAMS", pl,
                f"{cd.cycle} 首次启用缺少必要参数 {'/'.join(missing)}"
                "（循环模态已登记，可在后续程序段补齐参数后再执行）；"
                "本行对应孔全部阻断，原程序不变",
                {"cycle": cd.cycle, "missing": missing,
                 "definition_line_no": cd.def_line_no,
                 "param_sources": params_detail}, normalized))

        # 阻断条件四：单位 / 定位模式不明
        unknown_unit = self.state.unit_factor() is None
        unknown_mode = self.state.distance_mode is None
        if unknown_unit:
            issue_indexes.append(self._issue(
                "UNKNOWN_UNITS", pl,
                f"{cd.cycle} 固定循环发生在任何 G20/G21 之前，物理尺寸无法"
                "确定；本行对应孔阻断，不展开轨迹、不更新刀具位置",
                {}, normalized))
        if unknown_mode:
            issue_indexes.append(self._issue(
                "UNKNOWN_DISTANCE_MODE", pl,
                f"{cd.cycle} 固定循环在 G90/G91 建立之前触发，无法判定孔位"
                "绝对/增量定位；本行对应孔阻断",
                {}, normalized))

        # 阻断条件五：孔位 / 初始平面不可继承
        inherit_bad = (trigger and not unknown_unit and not unknown_mode
                       and not block_codes
                       and (not self.state.x.known or not self.state.y.known
                            or not self.state.z.known
                            or math.isnan(cd.initial_z)))
        if inherit_bad:
            why = []
            if not self.state.x.known:
                why.append("X 位置")
            if not self.state.y.known:
                why.append("Y 位置")
            if not self.state.z.known:
                why.append("当前 Z（初始平面）")
            if math.isnan(cd.initial_z):
                why.append("循环初始平面")
            issue_indexes.append(self._issue(
                "CYCLE_NO_INHERITABLE_STATE", pl,
                f"后续孔位没有可继承的状态：{'、'.join(why)}未知；"
                "无法确定孔位与初始平面，本行对应孔全部阻断",
                {"cycle": cd.cycle, "unknown": why,
                 "definition_line_no": cd.def_line_no}, normalized))
            block_codes.append("CYCLE_NO_INHERITABLE_STATE")

        g = self._group_for(cd)
        g["parameters"] = cd.params_out()
        if not math.isnan(cd.initial_z):
            g["initial_plane_z_mm"] = round6(cd.initial_z)

        blocked = trigger and (
            bool(block_codes) or unknown_unit or unknown_mode)

        # 展开孔位（即便阻断也登记孔记录，写明依据；阻断不产生位移）。
        # 工艺问题（主轴未转/无进给）在 _issue 层按触发行去重，
        # G83 多次进给动作不会重复报告。
        holes_info = self._expand_trigger_holes(
            pl, cd, reps, axis_words, blocked, block_codes, issue_indexes,
            normalized, l_word, do_holes=trigger)

        entry_type = "setting"
        if trigger:
            entry_type = "cycle_blocked" if blocked else (
                "cycle_definition" if line_cycle_key is not None
                else "cycle_trigger")

        self._finish_line(
            pl, entry_type, normalized, executed=not blocked,
            segment=holes_info["segment"],
            physical_known=not blocked and not unknown_unit and not unknown_mode,
            block_reason=(";".join(block_codes) if blocked else None),
            issue_indexes=issue_indexes)
        if blocked:
            self.blocked_count += 1
        else:
            self.executed_count += 1

    def _expand_trigger_holes(self, pl, cd: CycleDef, reps: int, axis_words,
                              blocked, block_codes, issue_indexes,
                              normalized, l_word, do_holes: bool = True) -> dict:
        """按 G90/G91 与 L 计算孔位，逐孔展开；返回轨迹 segment 汇总。

        do_holes=False 时只登记/合并参数，不占用孔序（纯参数行）。
        """
        mode = self.state.distance_mode
        factor = self.state.unit_factor() or 1.0
        start_xy = (self.state.x.value if self.state.x.known else None,
                    self.state.y.value if self.state.y.known else None)

        if not do_holes:
            return {"segment": None}

        # 解算本行目标 XY（G91 下相对当前位置）
        tgt_xy: list[float | None] = [start_xy[0], start_xy[1]]
        if not blocked and mode is not None:
            for i, letter in enumerate(("X", "Y")):
                if letter in axis_words:
                    raw = axis_words[letter]
                    if mode == "absolute":
                        tgt_xy[i] = raw * factor
                    elif start_xy[i] is not None:
                        tgt_xy[i] = start_xy[i] + raw * factor
                    else:
                        tgt_xy[i] = None

        holes: list[dict] = []
        all_moves: list[dict] = []
        g = self._group_for(cd)
        last_xy = start_xy
        last_z = self.state.z.value if self.state.z.known else None
        rapid_len = cut_len = depth_sum = dwell_sum = 0.0
        hole_nos: list[int] = []

        for k in range(reps):
            self.hole_seq += 1
            no = self.hole_seq
            hole_nos.append(no)
            g["hole_count"] += 1
            if mode == "relative" and k > 0 and not blocked:
                # G91 L>1：连续孔沿 XY 增量重复
                if "X" in axis_words and tgt_xy[0] is not None:
                    tgt_xy[0] = tgt_xy[0] + axis_words["X"] * factor
                if "Y" in axis_words and tgt_xy[1] is not None:
                    tgt_xy[1] = tgt_xy[1] + axis_words["Y"] * factor

            pos_known = (not blocked and tgt_xy[0] is not None
                         and tgt_xy[1] is not None and last_z is not None)
            hole = {
                "hole_no": no,
                "cycle": cd.cycle,
                "repeat_index": k + 1,
                "trigger_line_no": pl.line_no,
                "trigger_source_line": pl.source,
                "definition_line_no": cd.def_line_no,
                "x_mm": round6(tgt_xy[0]) if not blocked else None,
                "y_mm": round6(tgt_xy[1]) if not blocked else None,
                "l_repeat": (int(round(l_word)) if l_word is not None else 1),
                "status": "blocked" if blocked else "drilled",
                "block_codes": block_codes if blocked else [],
                "moves": [],
                "parameter_sources": self._cycle_param_sources(cd),
            }

            if blocked:
                self.hole_blocked += 1
                g["blocked_holes"] += 1
                hole["basis"] = self._block_basis_text(block_codes)
                holes.append(hole)
                g["holes"].append(hole)
                continue

            # 孔间定位段（从上个孔返回高度到新孔位 XY），归入本孔
            entry_z = last_z
            hole_moves: list[dict] = []
            pos_len = 0.0
            if last_xy is not None:
                pm = positioning_move(last_xy, (tgt_xy[0], tgt_xy[1]), entry_z)
                pm["hole_no"] = no
                self._check_cycle_move(pl, pm, issue_indexes, no,
                                       internal=False)
                all_moves.append(pm)
                hole_moves.append(pm)
                pos_len = pm["length_mm"]
                rapid_len += pos_len

            exp = expand_hole(cd, (tgt_xy[0], tgt_xy[1]), entry_z)
            for mv in exp["moves"]:
                mv["hole_no"] = no
                internal = bool(mv.get("internal_cycle"))
                self._check_cycle_move(pl, mv, issue_indexes, no,
                                       internal=internal)
            all_moves.extend(exp["moves"])
            hole_moves.extend(exp["moves"])
            rapid_len += exp["rapid_len_mm"]
            cut_len += exp["cutting_len_mm"]
            depth_sum += exp["drill_depth_mm"]
            dwell_sum += exp["dwell_s"]

            # 定位动作计入本孔轨迹与展开路径（单孔轨迹完整）
            hole_rapid = round(exp["rapid_len_mm"] + pos_len, 6)
            hole["moves"] = hole_moves
            hole["initial_plane_z_mm"] = round6(cd.initial_z)
            hole["r_plane_z_mm"] = round6(cd.r.value)
            hole["z_bottom_mm"] = round6(cd.z.value)
            hole["return_plane"] = ("G98" if cd.return_mode == "initial"
                                    else "G99")
            hole["drill_depth_mm"] = exp["drill_depth_mm"]
            hole["dwell_s"] = exp["dwell_s"]
            hole["retract_z_mm"] = round6(exp["retract_z"])
            hole["expanded_path_mm"] = {
                "rapid": hole_rapid,
                "cutting": exp["cutting_len_mm"],
                "total": round(hole_rapid + exp["cutting_len_mm"], 6)}
            holes.append(hole)
            g["holes"].append(hole)
            self.hole_ok += 1
            g["executed_holes"] += 1

            # 模态位置更新到孔位 + 返回高度
            self.state.x = Axis(tgt_xy[0], True)
            self.state.y = Axis(tgt_xy[1], True)
            self.state.z = Axis(exp["retract_z"], True)
            last_xy = (tgt_xy[0], tgt_xy[1])
            last_z = exp["retract_z"]

        g["total_drill_depth_mm"] = round(
            g["total_drill_depth_mm"] + depth_sum, 6)
        g["total_dwell_s"] = round(g["total_dwell_s"] + dwell_sum, 6)
        g["expanded_path_mm"]["rapid"] = round(
            g["expanded_path_mm"]["rapid"] + rapid_len, 6)
        g["expanded_path_mm"]["cutting"] = round(
            g["expanded_path_mm"]["cutting"] + cut_len, 6)
        g["expanded_path_mm"]["total"] = round(
            g["expanded_path_mm"]["rapid"] + g["expanded_path_mm"]["cutting"],
            6)

        if not blocked:
            self.length_cycle_rapid += rapid_len
            self.length_cycle_cutting += cut_len
            # 展开轨迹并入全局路径长度（行程/包围盒在逐动作检查时已累计）
            self.length_rapid += rapid_len
            self.length_cutting += cut_len

        segment = {
            "kind": "canned_cycle",
            "cycle": cd.cycle,
            "hole_nos": hole_nos,
            "start_mm": ([round6(v) for v in
                          (start_xy[0], start_xy[1],
                           self._snap_in["z"]["value_mm"])]
                         if start_xy[0] is not None
                         and start_xy[1] is not None else None),
            "holes": holes,
            "moves_mm": all_moves,
            "length_mm": round(rapid_len + cut_len, 6),
            "rapid_length_mm": round(rapid_len, 6),
            "cutting_length_mm": round(cut_len, 6),
            "drill_depth_mm": round(depth_sum, 6),
            "dwell_s": round(dwell_sum, 6),
        }
        return {"segment": segment}

    def _check_cycle_move(self, pl, mv: dict, issue_indexes, hole_no: int,
                          internal: bool):
        """对一个展开动作复用行程/包围盒/安全Z/进给/主轴检查。

        切削动作的主轴/进给工艺问题按触发行去重（G83 一孔多次进给）；
        越界等几何问题仍逐动作报告。
        """
        pts = [tuple(mv["start_mm"]), tuple(mv["end_mm"])]
        kind = "rapid" if mv["motion"] == "rapid" else "linear"
        seg = {
            "kind": kind,
            "start": pts[0], "end": pts[1], "points": pts,
            "length_mm": mv["length_mm"],
        }
        before = len(issue_indexes)
        # 循环内部快速动作（G83 排屑回退/再下钻、到 R 的垂直接近）豁免
        # 安全 Z 告警，但仍做行程/包围盒检查；切削动作做主轴/进给检查。
        self._run_segment_checks(
            pl, kind, seg, issue_indexes,
            cycle_context=(internal or kind == "rapid"),
            line_dedupe=True)
        # 孔间定位段（非内部快速）补充安全 Z 检查
        # （G99 在 R 平面横移可能低于安全 Z）
        if not internal and kind == "rapid":
            self._rapid_safe_z_check(pl, seg, issue_indexes, hole_no)
        # 去重可能返回已登记问题索引（同触发行的其他孔已报告）：
        # 去掉重复索引，再给问题补孔序/动作（仅首次）
        tail = list(dict.fromkeys(issue_indexes[before:]))
        del issue_indexes[before:]
        issue_indexes.extend(tail)
        for idx in tail:
            iss = self.issues[idx]
            iss.details.setdefault(
                "cycle", self.state.cycle.cycle if self.state.cycle else None)
            iss.details.setdefault("hole_no", hole_no)
            iss.details.setdefault("cycle_action", mv.get("action"))

    def _rapid_safe_z_check(self, pl, seg, issue_indexes, hole_no):
        """循环孔间定位段的安全 Z 检查（G99 在 R 平面横移可能低于安全 Z）。"""
        s0, s1 = seg["start"], seg["end"]
        if any(v is None for v in (s0[0], s0[1], s1[0], s1[1], s1[2])):
            return
        xy_move = math.hypot(s1[0] - s0[0], s1[1] - s0[1]) > MM_EPS
        if not xy_move:
            return
        zs = [p[2] for p in seg["points"] if p[2] is not None]
        end_below = s1[2] < self.cfg.safe_z - MM_EPS
        horiz_below = min(zs) < self.cfg.safe_z - MM_EPS
        if end_below or horiz_below:
            z_ref = s1[2] if end_below else min(zs)
            issue_indexes.append(self._issue(
                "RAPID_BELOW_SAFE_Z", pl,
                f"固定循环孔间快速定位到达/经过 Z={fmt_num(z_ref)} mm"
                f"（工件坐标，孔序 {hole_no}），低于安全 Z "
                f"{fmt_num(self.cfg.safe_z)} mm"
                f"（低 {fmt_num(self.cfg.safe_z - z_ref)} mm；"
                f"通常因 G99 在 R 平面横移导致）",
                {"ref_z_mm": round(z_ref, 6),
                 "safe_z_mm": self.cfg.safe_z,
                 "below_mm": round(self.cfg.safe_z - z_ref, 6),
                 "end_below_safe_z": end_below,
                 "horizontal_travel_below_safe_z": horiz_below,
                 "hole_no": hole_no,
                 "in_canned_cycle": True}))

    @staticmethod
    def _block_basis_text(block_codes) -> str:
        parts = {
            "CYCLE_BAD_PARAM": "循环参数非法（Q<=0、P 为负或 L 非正整数）",
            "CYCLE_PLANE_CONFLICT": "孔底与 R 平面顺序矛盾",
            "CYCLE_MISSING_PARAMS": "首次启用缺少 Z/R（G83 还需 Q）",
            "CYCLE_NO_INHERITABLE_STATE": "后续孔位没有可继承的状态",
        }
        return "；".join(parts.get(c, c) for c in block_codes)

    @staticmethod
    def _bad_param_text(bad: dict) -> str:
        bits = []
        if "q" in bad:
            bits.append(f"Q={fmt_num(bad['q']['program_value'])}（G83 每步"
                        "深度必须为正数）")
        if "p" in bad:
            bits.append(f"P={fmt_num(bad['p']['program_value'])}（暂停时间"
                        "必须为非负数）")
        if "l" in bad:
            bits.append(f"L={fmt_num(bad['l']['program_value'])}（重复次数"
                        "必须为正整数）")
        return "；".join(bits)

    def _cycle_param_sources(self, cd: CycleDef) -> dict:
        """逐孔参数来源（定义行 / 最近触发行 / 默认值）。"""
        def src(p):
            if p is None:
                return None
            return {"value_mm": round6(p.value), "line_no": p.line_no,
                    "source_line": p.source_line}

        out = {
            "Z_bottom": src(cd.z),
            "R_plane": src(cd.r),
            "Q_peck": src(cd.q),
            "P_dwell_s": src(cd.p),
            "initial_plane_z_mm": (round6(cd.initial_z)
                                   if not math.isnan(cd.initial_z) else None),
            "return_plane": {
                "code": "G98" if cd.return_mode == "initial" else "G99",
                "line_no": cd.return_mode_line,
                "source_line": cd.return_mode_source,
                "default": cd.return_mode_default,
            },
        }
        return out

    def _cycle_normalized(self, pl, cd: CycleDef, line_cycle_key,
                          f_raw, s_raw) -> str:
        """循环行的规范化文本（含循环代号、返回平面、本行词与继承标注）。"""
        out: list[str] = []
        if line_cycle_key is not None:
            out.append(CYCLE_G[line_cycle_key])
        ret_g = "G98" if cd.return_mode == "initial" else "G99"
        if not cd.return_mode_default:
            out.append(ret_g)
        for w in pl.words:
            if w.letter in ("N", "G"):
                continue
            if w.letter in ("X", "Y", "Z", "R", "Q", "P", "L", "F", "S"):
                out.append(f"{w.letter}{fmt_num(w.value)}")
        for w in pl.m_words:
            out.append("M" + fmt_num(w.value))
        # 继承参数标注
        inherited = []
        if cd.z is not None and cd.z.line_no != pl.line_no:
            inherited.append(f"Z(继承L{cd.z.line_no})")
        if cd.r is not None and cd.r.line_no != pl.line_no:
            inherited.append(f"R(继承L{cd.r.line_no})")
        if cd.cycle == "G83" and cd.q is not None and cd.q.line_no != pl.line_no:
            inherited.append(f"Q(继承L{cd.q.line_no})")
        if cd.p is not None and cd.p.line_no != pl.line_no:
            inherited.append(f"P(继承L{cd.p.line_no})")
        if cd.return_mode_default:
            inherited.append(f"{ret_g}(默认)")
        text = " ".join(out)
        if inherited:
            text += "  [" + "，".join(inherited) + "]"
        return text

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

    def _run_segment_checks(self, pl, motion_mode, segment, issue_indexes,
                            cycle_context: bool = False,
                            line_dedupe: bool = False):
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

        if motion_mode == "rapid" and not cycle_context:
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
            # 报警条件：快速终点低于安全 Z，或安全 Z 以下存在水平快速移动。
            # 固定循环内部（G83 排屑回退、下到 R 等）属于钻削工艺动作，
            # 不在此列；循环间定位段仍参与检查。
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
                idx = self._issue(
                    "SPINDLE_NOT_RUNNING", pl,
                    f"{MOTION_CN[motion_mode]}切削发生时主轴处于停止状态"
                    f"（spindle_on=false，最近 S={self.state.spindle_rpm}）",
                    {"spindle_on": False,
                     "last_s_rpm": self.state.spindle_rpm},
                    line_dedupe=line_dedupe)
                issue_indexes.append(idx)
            if not self.state.feed.known:
                idx = self._issue(
                    "FEED_UNSET", pl,
                    f"{MOTION_CN[motion_mode]}切削前未建立有效进给 F"
                    "（单位不明或从未给定）",
                    {"feed_known": False},
                    line_dedupe=line_dedupe)
                issue_indexes.append(idx)

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
        if segment.get("kind") == "canned_cycle":
            return self._cycle_segment_out(segment)
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

    def _cycle_segment_out(self, segment) -> dict:
        def move_out(mv):
            return {
                "action": mv.get("action"),
                "motion": mv.get("motion"),
                "internal_cycle": bool(mv.get("internal_cycle")),
                "note": mv.get("note"),
                "hole_no": mv.get("hole_no"),
                "start_mm": list(mv["start_mm"]),
                "end_mm": list(mv["end_mm"]),
                "points_mm": [list(p) for p in mv["points_mm"]],
                "length_mm": mv["length_mm"],
                "dwell_s": mv.get("dwell_s"),
            }

        return {
            "kind": "canned_cycle",
            "cycle": segment["cycle"],
            "hole_nos": segment["hole_nos"],
            "start_mm": segment["start_mm"],
            "length_mm": segment["length_mm"],
            "rapid_length_mm": segment["rapid_length_mm"],
            "cutting_length_mm": segment["cutting_length_mm"],
            "drill_depth_mm": segment["drill_depth_mm"],
            "dwell_s": segment["dwell_s"],
            "holes": segment["holes"],
            "moves_mm": [move_out(m) for m in segment["moves_mm"]],
        }

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

    def _drill_cycles_out(self) -> dict:
        groups = []
        for g in self.cycle_groups:
            groups.append({
                "cycle": g["cycle"],
                "definition_line_no": g["definition_line_no"],
                "definition_source_line": g["definition_source_line"],
                "cancel_line_no": g["cancel_line_no"],
                "cancel_source_line": g["cancel_source_line"],
                "initial_plane_z_mm": g["initial_plane_z_mm"],
                "parameters": g["parameters"],
                "hole_count": g["hole_count"],
                "executed_holes": g["executed_holes"],
                "blocked_holes": g["blocked_holes"],
                "total_drill_depth_mm": g["total_drill_depth_mm"],
                "total_dwell_s": g["total_dwell_s"],
                "expanded_path_mm": g["expanded_path_mm"],
                "holes": g["holes"],
            })

        def agg(field_):
            return round(sum(grp[field_] for grp in self.cycle_groups), 6)

        def agg_expanded(groups, key):
            return round(sum(grp["expanded_path_mm"][key] for grp in groups),
                         6)

        by_cycle: dict = {}
        for grp in self.cycle_groups:
            d = by_cycle.setdefault(grp["cycle"], {
                "groups": 0, "holes": 0, "drilled": 0, "blocked": 0,
                "drill_depth_mm": 0.0, "expanded_rapid_mm": 0.0,
                "expanded_cutting_mm": 0.0})
            d["groups"] += 1
            d["holes"] += grp["hole_count"]
            d["drilled"] += grp["executed_holes"]
            d["blocked"] += grp["blocked_holes"]
            d["drill_depth_mm"] = round(
                d["drill_depth_mm"] + grp["total_drill_depth_mm"], 6)
            d["expanded_rapid_mm"] = round(
                d["expanded_rapid_mm"] + grp["expanded_path_mm"]["rapid"], 6)
            d["expanded_cutting_mm"] = round(
                d["expanded_cutting_mm"] + grp["expanded_path_mm"]["cutting"],
                6)

        return {
            "supported_cycles": {
                "G80": "取消固定循环（不建立运动模态）",
                "G81": "钻孔循环：快速到 R，进给到孔底，快速退回",
                "G82": "锪孔循环：同 G81，孔底暂停 P（整数 ms/小数 s）",
                "G83": "深孔啄钻：按 Q 分步进给，每步退回 R 排屑",
                "G98": "孔后返回初始平面（未写明时的默认）",
                "G99": "孔后返回 R 平面",
            },
            "summary": {
                "cycle_groups": len(self.cycle_groups),
                "holes_total": self.hole_seq,
                "holes_drilled": self.hole_ok,
                "holes_blocked": self.hole_blocked,
                "total_drill_depth_mm": agg("total_drill_depth_mm"),
                "total_dwell_s": agg("total_dwell_s"),
                "expanded_path_mm": {
                    "rapid": agg_expanded(self.cycle_groups, "rapid"),
                    "cutting": agg_expanded(self.cycle_groups, "cutting"),
                    "total": agg_expanded(self.cycle_groups, "total"),
                },
            },
            "by_cycle": by_cycle,
            "groups": groups,
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
                "drill_holes": self.hole_ok,
                "drill_holes_blocked": self.hole_blocked,
                "drill_holes_total": self.hole_seq,
                "drill_cycle_groups": len(self.cycle_groups),
            },
            "machine": self.cfg.to_dict(),
            "final_state": self.state.snapshot(),
            "drill_cycles": self._drill_cycles_out(),
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
                "canned_cycle_rapid": round(self.length_cycle_rapid, 6),
                "canned_cycle_cutting": round(self.length_cycle_cutting, 6),
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
                "canned_cycle": (
                    "G81/G82/G83 为模态固定循环，G80 或 G0-G3 取消；"
                    "Z/R/Q/P 模态继承，L 为孔位重复次数（默认 1，正整数）；"
                    "G90 下 Z/R 绝对、L 为同位重复，G91 下 Z 相对 R、R 相对初始"
                    "平面、L 沿 XY 增量展开连续孔；G98 返回初始平面（默认），"
                    "G99 返回 R 平面；首次启用缺 Z/R、G83 的 Q 非正、P/L 非法"
                    "或孔底高于 R 时阻断对应孔；G83 循环内部排屑快速移动豁免"
                    "安全 Z 告警，孔间定位仍检查"),
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
        "G80": "取消固定钻孔循环",
        "G81": "钻孔循环（快速到 R，进给到孔底，快速退回）",
        "G82": "锪孔循环（同 G81，孔底暂停 P）",
        "G83": "深孔啄钻（按 Q 分步下钻，每步退回 R 排屑）",
        "G98": "固定循环后返回初始平面（默认）",
        "G99": "固定循环后返回 R 平面",
    },
    "canned_cycles": {
        "G81": {"params": "X Y Z R F L",
                "action": "定位 -> 快速到 R -> 进给到 Z -> 快速退回"},
        "G82": {"params": "X Y Z R P F L",
                "action": "同 G81，孔底暂停 P（整数=ms，小数=s）"},
        "G83": {"params": "X Y Z R Q F L",
                "action": "按 Q 分步啄钻，每步快速退回 R 排屑，"
                          "再快速下到距上次孔底 0.1 mm 后进给"},
        "G98": "孔后返回初始平面（未写明 G98/G99 时的默认）",
        "G99": "孔后返回 R 平面（连续孔间在 R 高度横移）",
        "L": "孔位重复次数，默认 1，必须为正整数；G91 下沿 XY 增量"
             "展开为连续孔，G90 下为同位置重复",
        "R_semantics": "G90 绝对 R 坐标；G91 相对循环建立时的初始平面",
        "Z_semantics": "G90 绝对孔底坐标；G91 相对 R 平面的孔底增量",
        "Q_semantics": "G83 每步进给深度，恒为正的无符号增量（mm）",
        "P_semantics": "G82 孔底暂停：整数按毫秒、小数按秒；负数非法",
        "block_rules": [
            "首次启用缺少 Z 或 R（G83 还需正的 Q）-> 阻断对应孔",
            "G83 的 Q<=0、P 为负、L 非正整数 -> 阻断对应孔",
            "孔底高于 R 平面 -> CYCLE_PLANE_CONFLICT，阻断对应孔",
            "后续孔位缺少可继承的 XY/初始平面状态 -> 阻断对应孔",
        ],
    },
    "supported_m": {"M3": "主轴正转", "M5": "主轴停止"},
    "supported_words": ["X", "Y", "Z", "I", "J", "R", "F", "S", "N(忽略)",
                        "Q(固定循环步进)", "P(固定循环暂停)",
                        "L(固定循环重复次数)"],
    "comments": ["(圆括号注释)", ";分号注释"],
    "unsupported_policy": "任何未列出的 G/M 指令及其他地址词均显式报告，"
                          "并整段阻断，不猜测执行",
    "unsupported_examples": [
        "G17/G18/G19 平面选择", "G28/G30 回零",
        "G40-G43 刀补", "G54.1/G55-G59 其他工件坐标系",
        "G84-G89 其他固定循环（仅支持 G80-G83）",
        "圆弧 K 参数（仅 G17，用 I/J）",
        "M2/M30 程序结束", "M4 反转", "M6 换刀", "M7-M9 冷却",
        "T 刀号", "H/D 刀补号",
    ],
    "severity_levels": SEVERITY_ORDER,
}
