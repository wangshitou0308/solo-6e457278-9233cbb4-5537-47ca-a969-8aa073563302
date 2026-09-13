"""G-code 模态还原、几何解算与安全检查。

设计原则（与需求一致）：
- 逐行按模态还原刀具位置与主轴/进给状态；
- 未支持 / 无法解析 / 几何无解的程序段一律阻断，不猜测执行，不改变状态；
- 单位 / 定位模式 / 工件坐标系不明时，相关检查显式报出而非按默认值蒙算；
- 所有内部长度单位为 mm（G20 输入在读取时乘 25.4）。
"""

from __future__ import annotations

import copy
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
from .cutter import (
    JoinError,
    Primitive,
    Connector,
    offset_line,
    offset_arc,
    side_normal,
    join_primitives,
    sample_primitive,
    primitive_bbox_2d,
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
    "UNKNOWN_WCS": "warning",              # G54-G59 未建立或偏置未配置，无法做行程检查
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
    "CYCLE_PLANE_NOT_G17": "error",        # 固定循环只允许在 G17(XY) 平面展开
    "LENGTH_COMP_MISSING_H": "error",      # G43/G44 未给 H
    "LENGTH_COMP_H_NOT_FOUND": "error",    # H 非正整数或不在 H 寄存器偏置表
    "LENGTH_COMP_CONFLICT": "error",       # 同段补偿指令冲突（G43/G44/G49 混用等）
    "CUTTER_COMP_MISSING_D": "error",      # G41/G42 未同段给 D
    "CUTTER_COMP_D_NOT_FOUND": "error",    # D 非正整数或未登记
    "CUTTER_COMP_CONFLICT": "error",       # G40/G41/G42 同段冲突/重复
    "CUTTER_APPROACH_INVALID": "error",    # 切入段不是非零平面内 G1
    "CUTTER_EXIT_INVALID": "error",        # G40 退出段不是非零平面内 G1
    "CUTTER_ARC_RADIUS": "critical",       # 补偿后圆弧有效半径非正
    "CUTTER_COMP_DISCONTINUOUS": "critical",  # 相邻偏置段无法连续/G0/固定循环/换平面
}

ISSUE_TITLE = {
    "MALFORMED_LINE": "程序段无法解析",
    "UNSUPPORTED_INSTRUCTION": "未支持指令（已阻断该段）",
    "NO_MOTION_MODE": "缺少模态运动指令",
    "UNKNOWN_UNITS": "单位模式不明（未见 G20/G21）",
    "UNKNOWN_DISTANCE_MODE": "定位模式不明（未见 G90/G91）",
    "UNKNOWN_WCS": "工件坐标系不明或未配置（G54-G59）",
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
    "CYCLE_PLANE_NOT_G17": "固定循环仅允许在 G17(XY) 平面展开（对应孔已阻断）",
    "LENGTH_COMP_MISSING_H": "刀长补偿指令缺少 H 号（该段已阻断）",
    "LENGTH_COMP_H_NOT_FOUND": "刀长补偿 H 号非法或不在偏置表（该段已阻断）",
    "LENGTH_COMP_CONFLICT": "同程序段补偿指令冲突（该段已阻断）",
    "CUTTER_COMP_MISSING_D": "半径补偿指令缺少 D 号（该段已阻断）",
    "CUTTER_COMP_D_NOT_FOUND": "半径补偿 D 号非法或未在半径表登记（该段已阻断）",
    "CUTTER_COMP_CONFLICT": "同程序段半径补偿指令冲突（该段已阻断）",
    "CUTTER_APPROACH_INVALID": "半径补偿切入段无效：必须为非零平面内 G1（相关轮廓已阻断）",
    "CUTTER_EXIT_INVALID": "半径补偿退出段无效：G40 必须用非零平面内 G1 退出（相关轮廓已阻断）",
    "CUTTER_ARC_RADIUS": "半径补偿后圆弧有效半径非正（相关轮廓已阻断）",
    "CUTTER_COMP_DISCONTINUOUS": "半径补偿轨迹无法连续（相关轮廓已阻断）",
}

ALLOWED_LETTERS = {"G", "M", "X", "Y", "Z", "I", "J", "K", "R", "F", "S", "N",
                   "Q", "P", "L", "H", "D"}
MOTION_G = {"0": "rapid", "1": "linear", "2": "arc_cw", "3": "arc_ccw"}
MOTION_CN = {"rapid": "快速", "linear": "直线",
             "arc_cw": "顺时针圆弧", "arc_ccw": "逆时针圆弧"}
# 刀长补偿 G 代码 -> 模态键（G43 加、G44 减、G49 取消）
LENGTH_COMP_G = {"43": "plus", "44": "minus", "49": "cancel"}
LENGTH_COMP_CN = {"plus": "G43 加", "minus": "G44 减", "cancel": "G49 取消"}
# 刀具半径补偿 G 代码 -> 模态键（G41 左、G42 右、G40 取消）
CUTTER_COMP_G = {"41": "left", "42": "right", "40": "cancel"}
CUTTER_COMP_CN = {"left": "G41 左", "right": "G42 右", "cancel": "G40 取消"}
# H 寄存器偏置必须为正整数（H0 等在控制器上另有含义，本预检不使用）
SETTING_G_UNIT = {"20": "inch", "21": "mm"}
SETTING_G_MODE = {"90": "absolute", "91": "relative"}
# 工件坐标系模态 G54-G59（偏置由机床配置 wcs_offsets 给出）
WCS_G = {str(n): f"G{n}" for n in range(54, 60)}
WCS_NAMES = tuple(WCS_G.values())
# 圆弧平面选择模态 G17/G18/G19
PLANE_G = {"17": "G17", "18": "G18", "19": "G19"}
# 平面 -> (平面轴 u 下标, 平面轴 v 下标, 垂直轴下标, u 圆心词, v 圆心词, 标签)
# u/v 取右手系（u × v = 垂直轴），G2/G3 旋向按“从垂直轴正向看向平面”判定
PLANE_SPEC = {
    "G17": (0, 1, 2, "I", "J", "G17(XY)"),
    "G18": (2, 0, 1, "K", "I", "G18(XZ)"),
    "G19": (1, 2, 0, "J", "K", "G19(YZ)"),
}
PLANE_AXIS_NAMES = ("X", "Y", "Z")
CENTER_WORD_ORDER = {"I": 0, "J": 1, "K": 2}

# 程序包模式下的程序流指令（见 packages.py；普通单程序分析中它们仍属于
# 未支持指令，保持旧行为）
PACKAGE_FLOW_M = {"2", "30", "98", "99"}
# 程序包模式下允许的额外地址词
PACKAGE_ALLOWED_LETTERS = ALLOWED_LETTERS | {"O"}


def plane_center_words(plane: str) -> list[str]:
    """当前平面接受的圆心词（按 I/J/K 惯例顺序，如 G18 -> ['I', 'K']）。"""
    return sorted((PLANE_SPEC[plane][3], PLANE_SPEC[plane][4]),
                  key=CENTER_WORD_ORDER.get)
# 固定钻孔循环返回平面模态 G98/G99 的中文名
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
    # 旧字段：G54 的 X/Y/Z 偏置（与 wcs_offsets["G54"] 保持一致）
    offset_x: float = 0.0
    offset_y: float = 0.0
    offset_z: float = 0.0
    # G54-G59 各自的工件坐标偏置 {"G54": {"x":..,"y":..,"z":..}, ...}
    # 未出现在表中的坐标系视为“未配置”：程序引用时机床坐标结论标为未知
    wcs_offsets: dict = field(default_factory=dict)
    # H 寄存器刀长偏置表 {h号(正整数): 偏置 mm}，G43 加 / G44 减
    length_offsets: dict = field(default_factory=dict)
    # D 寄存器刀具半径表 {d号(正整数): 刀具半径 mm}，G41 左 / G42 右
    radius_offsets: dict = field(default_factory=dict)

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

        # 多工件坐标系偏置：wcs_offsets={"G55": {"x":..,"y":..,"z":..}}；
        # 非法值报错时定位到坐标系与字段（如 wcs_offsets.G55.x）
        wcs_offsets: dict[str, dict[str, float]] = {}
        raw_wcs = d.get("wcs_offsets")
        if raw_wcs is not None:
            if not isinstance(raw_wcs, dict):
                errors.append(
                    "wcs_offsets 必须是对象，形如 "
                    '{"G55": {"x": 0, "y": 0, "z": 0}}')
            else:
                for wname, off in raw_wcs.items():
                    wcs = str(wname).upper()
                    if wcs not in WCS_NAMES:
                        errors.append(
                            f"wcs_offsets.{wname} 不是支持的工件坐标系"
                            f"（仅支持 {'/'.join(WCS_NAMES)}）")
                        continue
                    if not isinstance(off, dict):
                        errors.append(
                            f"wcs_offsets.{wcs} 必须是对象 "
                            '{"x":..,"y":..,"z":..}')
                        continue
                    extra = sorted(set(off) - {"x", "y", "z"})
                    if extra:
                        errors.append(
                            f"wcs_offsets.{wcs} 含未知字段 {extra}"
                            "（仅支持 x/y/z）")
                    entry = {}
                    for ax in ("x", "y", "z"):
                        v = off.get(ax, 0.0)
                        try:
                            entry[ax] = float(v)
                        except (TypeError, ValueError):
                            errors.append(
                                f"wcs_offsets.{wcs}.{ax} 必须是数值"
                                f"（坐标系 {wcs} 的 {ax.upper()} 轴偏置）")
                            entry[ax] = 0.0
                    wcs_offsets[wcs] = entry
        # 旧配置中的 offset_x/offset_y/offset_z 归入 G54；
        # 显式 wcs_offsets.G54 优先于旧字段
        if "G54" not in wcs_offsets:
            wcs_offsets["G54"] = {"x": ox, "y": oy, "z": oz}
        g54 = wcs_offsets["G54"]

        # 刀长补偿 H 寄存器偏置表 length_offsets={"1": 12.5, 2: -3.0}；
        # 键必须为正整数（H0 不接受），值必须为数值（mm），非法时定位到
        # 具体寄存器（如 length_offsets.H3 必须是数值），拒绝保存。
        length_offsets: dict[int, float] = {}
        raw_offsets = d.get("length_offsets")
        if raw_offsets is not None:
            if not isinstance(raw_offsets, dict):
                errors.append(
                    "length_offsets 必须是对象，形如 "
                    '{"1": 12.5, "2": -3.0}（H 号 -> 刀长偏置 mm）')
            else:
                for hk, hv in raw_offsets.items():
                    hs = str(hk).strip().upper()
                    if hs.startswith("H"):
                        hs = hs[1:]
                    try:
                        hf = float(hs)
                    except (TypeError, ValueError):
                        hf = None
                    if hf is None or not hf.is_integer() or hf <= 0:
                        errors.append(
                            f"length_offsets.{hk}：H 号必须为正整数"
                            f"（如 H1），收到 {hk!r}")
                        continue
                    hno = int(hf)
                    try:
                        length_offsets[hno] = float(hv)
                    except (TypeError, ValueError):
                        errors.append(
                            f"length_offsets.H{hno} 必须是数值（刀长偏置 mm），"
                            f"收到 {hv!r}")

        # 刀具半径补偿 D 寄存器半径表 radius_offsets={"1": 5.0, 2: 3.0}；
        # 键必须为正整数（D0 不接受），值必须为非负数值（mm），非法时
        # 定位到具体寄存器（如 radius_offsets.D3 必须是数值），拒绝保存。
        radius_offsets: dict[int, float] = {}
        raw_radii = d.get("radius_offsets")
        if raw_radii is not None:
            if not isinstance(raw_radii, dict):
                errors.append(
                    "radius_offsets 必须是对象，形如 "
                    '{"1": 5.0, "2": 3.0}（D 号 -> 刀具半径 mm）')
            else:
                for dk, dv in raw_radii.items():
                    ds = str(dk).strip().upper()
                    if ds.startswith("D"):
                        ds = ds[1:]
                    try:
                        df = float(ds)
                    except (TypeError, ValueError):
                        df = None
                    if df is None or not df.is_integer() or df <= 0:
                        errors.append(
                            f"radius_offsets.{dk}：D 号必须为正整数"
                            f"（如 D1），收到 {dk!r}")
                        continue
                    dno = int(df)
                    try:
                        rv = float(dv)
                    except (TypeError, ValueError):
                        errors.append(
                            f"radius_offsets.D{dno} 必须是数值（刀具半径 mm），"
                            f"收到 {dv!r}")
                        continue
                    if rv < 0:
                        errors.append(
                            f"radius_offsets.D{dno} 刀具半径不能为负"
                            f"（收到 {rv}）")
                        continue
                    radius_offsets[dno] = rv

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
            offset_x=g54["x"], offset_y=g54["y"], offset_z=g54["z"],
            wcs_offsets=wcs_offsets,
            length_offsets=dict(sorted(length_offsets.items())),
            radius_offsets=dict(sorted(radius_offsets.items())),
        )

    def offset_for(self, wcs: str | None):
        """取坐标系的 (x, y, z) 偏置；未建立/未配置返回 None。"""
        if not wcs:
            return None
        off = self.wcs_offsets.get(wcs)
        if off is None:
            return None
        return (off["x"], off["y"], off["z"])

    def length_offset_for(self, h: int | None) -> float | None:
        """取 H 寄存器的刀长偏置（mm）；未登记返回 None。"""
        if h is None:
            return None
        return self.length_offsets.get(h)

    def radius_offset_for(self, d: int | None) -> float | None:
        """取 D 寄存器的刀具半径（mm）；未登记返回 None。"""
        if d is None:
            return None
        return self.radius_offsets.get(d)

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
            "wcs_offsets": {w: dict(off)
                            for w, off in sorted(self.wcs_offsets.items())},
            "length_offsets": {str(h): self.length_offsets[h]
                               for h in sorted(self.length_offsets)},
            "radius_offsets": {str(d): self.radius_offsets[d]
                               for d in sorted(self.radius_offsets)},
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
    wcs: str | None = None             # 'G54'..'G59' | None
    motion_mode: str | None = None     # rapid/linear/arc_cw/arc_ccw
    plane: str = "G17"                 # G17/G18/G19（上电默认 G17）
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
    # 刀长补偿模态：direction 'plus'(G43)/'minus'(G44)/None(G49/上电)；
    # h 为生效的 H 号，signed 为代数值（G43=+表值，G44=-表值）。
    # state.x/y/z 始终表示刀尖工件坐标；主轴基准点机床坐标按
    # tip_machine + (0,0,signed) 计算（Z 行程按基准点判定）。
    comp_direction: str | None = None
    comp_h: int | None = None
    comp_signed: float = 0.0
    comp_apply_line: int | None = None
    # 刀具半径补偿（G41/G42 + D）：
    # phase 'inactive' 未启用 | 'pending_in' 已写 G41/G42 Dn，等待非零平面内
    #   G1 切入 | 'active' 补偿中 | 'pending_out' 已写 G40，等待非零 G1 退出 |
    #   'broken' 相邻段无法连续后的断链状态（只接受重新 G41/G42 或 G40）
    # cutter_side 'left'(G41)/'right'(G42)；cutter_d 为 D 号、
    # cutter_r 为半径表值（mm）；cutter_plane 为建立补偿时锁定的平面。
    cutter_phase: str = "inactive"
    cutter_side: str | None = None
    cutter_d: int | None = None
    cutter_r: float = 0.0
    cutter_plane: str | None = None
    cutter_apply_line: int | None = None
    cutter_cancel_line: int | None = None

    def clone(self) -> "State":
        return State(
            unit=self.unit,
            distance_mode=self.distance_mode,
            wcs=self.wcs,
            motion_mode=self.motion_mode,
            plane=self.plane,
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
            comp_direction=self.comp_direction,
            comp_h=self.comp_h,
            comp_signed=self.comp_signed,
            comp_apply_line=self.comp_apply_line,
            cutter_phase=self.cutter_phase,
            cutter_side=self.cutter_side,
            cutter_d=self.cutter_d,
            cutter_r=self.cutter_r,
            cutter_plane=self.cutter_plane,
            cutter_apply_line=self.cutter_apply_line,
            cutter_cancel_line=self.cutter_cancel_line,
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
            "plane": self.plane,
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
            "tool_length_compensation": {
                "active": self.comp_direction is not None,
                "code": ("G49" if self.comp_direction is None
                         else ("G43" if self.comp_direction == "plus"
                               else "G44")),
                "direction": self.comp_direction,
                "h": self.comp_h,
                "offset_mm": round6(abs(self.comp_signed)),
                "signed_offset_mm": round6(self.comp_signed),
                "applied_line_no": self.comp_apply_line,
            },
            "tool_radius_compensation": {
                "phase": self.cutter_phase,
                "active": self.cutter_phase == "active",
                "code": (None if self.cutter_side is None
                         else ("G41" if self.cutter_side == "left"
                               else "G42")),
                "side": self.cutter_side,
                "d": self.cutter_d,
                "radius_mm": round6(self.cutter_r) if self.cutter_d else None,
                "plane": self.cutter_plane,
                "applied_line_no": self.cutter_apply_line,
                "cancel_line_no": self.cutter_cancel_line,
            },
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
    # 程序包模式下的来源信息（单程序分析时为 None）
    source_program: str | None = None
    source_file: str | None = None
    call_stack: list | None = None
    depth: int | None = None
    repeat_index: int = 0
    repeat_total: int = 1
    block_seq: int | None = None

    def to_dict(self) -> dict:
        out = {
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
        if self.source_program is not None:
            out["source_program"] = self.source_program
            out["source_file"] = self.source_file
            out["source_line_no"] = self.line_no
            out["call_stack"] = self.call_stack
            out["depth"] = self.depth
            out["repeat_index"] = self.repeat_index
            out["repeat_total"] = self.repeat_total
            out["block_seq"] = self.block_seq
        return out


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
              r_word: float | None,
              words: tuple[str, str] = ("I", "J")) -> dict:
    """解算当前平面内的二维圆弧。圆心词（I/J、I/K 或 J/K）优先于 R 的
    旧策略已废弃：调用方须先拒绝混用；抛 ArcError 表示无解。

    words 为当前平面的两个圆心词名（仅用于报错文本）。
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
            raise ArcError(
                f"{words[0]}/{words[1]} 指定的圆心与起点重合，半径为 0")
        r_end = math.hypot(x2 - cx, y2 - cy)
        tol = max(GEOM_TOL, radius * 1e-4)
        if abs(r_end - radius) > tol:
            raise ArcError(
                f"终点到圆心距离 {r_end:.6g} 与起点半径 {radius:.6g} 不一致"
                f"（起终半径差 {abs(r_end - radius):.6g}）"
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
            raise ArcError(
                f"R 编程圆弧的起点与终点重合（整圆请用 {words[0]}/{words[1]}）")
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
        raise ArcError(
            f"圆弧段缺少圆心参数 {words[0]}/{words[1]} 或半径 R")

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


def arc_extreme_angles(a0: float, sweep: float) -> list[float]:
    """真实弧线上平面轴取极值的角度集合（起点/终点 + 扫过的象限角）。

    平面内两轴的极值只可能出现在端点或角度为 k·π/2 的位置；
    垂直轴随扫角线性联动，极值恒在端点。
    """
    angles = [a0, a0 + sweep]
    lo, hi = (a0 + sweep, a0) if sweep < 0 else (a0, a0 + sweep)
    step = math.pi / 2
    k0 = math.ceil(lo / step - 1e-9)
    k1 = math.floor(hi / step + 1e-9)
    for k in range(k0, k1 + 1):
        angles.append(k * step)
    return angles


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
                 progress=None, package_mode: bool = False):
        self.cfg = config
        self.program_name = program_name
        self.progress = progress
        self.package_mode = package_mode
        # 程序包模式下当前展开块（由 run_blocks 设置），用于给轨迹条目与
        # 问题附来源程序/调用栈
        self.current_block = None
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
        # 多坐标系统计：程序引用过的坐标系、分坐标系路径长度与机床包围盒
        self.wcs_used: set[str] = set()
        self.wcs_path: dict[str, dict] = {}
        self.wcs_mbbox: dict[str, list] = {}
        self.length_rapid = 0.0
        self.length_cutting = 0.0
        self.unknown_length_segments = 0
        self.length_cycle_rapid = 0.0
        self.length_cycle_cutting = 0.0
        # 已执行弧段的分平面统计（报告 arcs 节）
        self.arc_stats = {
            p: {"count": 0, "arc_length_mm": 0.0, "length_3d_mm": 0.0,
                "helical_count": 0, "full_circle_count": 0}
            for p in PLANE_G.values()
        }
        self._snap_in: dict = {}
        self._snap_in_cycle: CycleDef | None = None
        self._snap_in_return = "initial"
        self._snap_in_return_line: int | None = None
        self._snap_in_return_source: str | None = None
        self._snap_in_return_default = True
        # 刀长补偿：G43/G44/G49 事件流、按 H 号的路径/钻孔汇总、
        # 当前行补偿信息（由 _apply_length_comp 设置：起点用旧补偿、
        # 段内其余点用新补偿；补偿-only 行用于重算刀尖 Z）。
        self.length_events: list[dict] = []
        self.comp_path: dict = {}
        self._line_comp: dict | None = None
        self._snap_in_line_no: int | None = None
        # 刀具半径补偿：G40/G41/G42 事件、按 D 的轮廓/刀心路径统计、
        # 待连接的上一偏置段（_cutter_pending）、本行补偿信息
        # （_line_cutter：token/bare_d/event）。
        self.cutter_events: list[dict] = []
        self.cutter_path: dict = {}
        self.cutter_segments: list[dict] = []
        self.cutter_swept_bmin = [math.inf] * 3
        self.cutter_swept_bmax = [-math.inf] * 3
        self.cutter_swept_mbmin = [math.inf] * 3
        self.cutter_swept_mbmax = [-math.inf] * 3
        self.cutter_swept_wcs_known = True
        self._cutter_pending: dict | None = None
        self._line_cutter: dict | None = None
        # (line_no, code) -> 已登记问题索引：循环展开动作的工艺问题按行去重
        self._line_dedup: dict[tuple[int, str], int] = {}

    # -- 问题记录 ----------------------------------------------------------

    # 同一触发行内按代码去重的工艺问题：G83 一个孔有多次进给动作，
    # 主轴未转/无进给只报一次（以触发行而非每个展开动作计）
    LINE_DEDUPE_CODES = {"SPINDLE_NOT_RUNNING", "FEED_UNSET"}

    def _issue(self, code: str, pl: ParsedLine, basis: str,
               details: dict | None = None, normalized: str = "",
               line_dedupe: bool = False) -> int:
        # 程序包模式下按展开块实例去重：同一源行（如 L 次重复调用的子程序
        # 行）在不同块中的问题都要分别报告
        dedup_id = (self.current_block.seq if self.current_block is not None
                    else None)
        if line_dedupe:
            key = (dedup_id, pl.line_no, code)
            existing = self._line_dedup.get(key)
            if existing is not None:
                return existing
        details = dict(details or {})
        # 每个问题都归属到触发时的工件坐标系（未建立则为 None），
        # 报告可按 wcs=G54..G59 筛选
        details.setdefault("wcs", self.state.wcs)
        # 同时归属到触发时生效的刀长补偿 H 号（未补偿为 None）；
        # 补偿指令自身的问题可在 details 里显式给出请求的 h。
        details.setdefault("h", self.state.comp_h)
        iss = Issue(
            code=code,
            severity=ISSUE_SEVERITY[code],
            line_no=pl.line_no,
            source=pl.source,
            normalized=normalized,
            state_in=self._snap_in,
            state_out=None,
            basis=basis,
            details=details,
        )
        if self.current_block is not None:
            self._annotate_issue(iss, self.current_block)
        self.issues.append(iss)
        idx = len(self.issues) - 1
        if line_dedupe:
            self._line_dedup[key] = idx
        return idx

    @staticmethod
    def _block_provenance(block) -> dict:
        return {
            "source_program": block.program,
            "source_file": block.file,
            "source_line_no": block.line.line_no,
            "source_line": block.line.source,
            "call_stack": [dict(f) for f in block.call_stack],
            "depth": block.depth,
            "repeat_index": block.repeat_index,
            "repeat_total": block.repeat_total,
        }

    def _annotate_entry(self, entry: dict, block=None):
        if block is None:
            return
        entry.update(self._block_provenance(block))

    def _annotate_issue(self, iss: Issue, block):
        p = self._block_provenance(block)
        iss.source_program = p["source_program"]
        iss.source_file = p["source_file"]
        iss.call_stack = p["call_stack"]
        iss.depth = p["depth"]
        iss.repeat_index = p["repeat_index"]
        iss.repeat_total = p["repeat_total"]
        iss.block_seq = block.seq

    # -- 坐标系 / 包围盒 / 行程 -----------------------------------------------

    def _current_offset(self):
        """当前坐标系的 (x, y, z) 偏置；未建立/未配置返回 None。"""
        return self.cfg.offset_for(self.state.wcs)

    def _switch_wcs(self, new_wcs: str):
        """切换工件坐标系：刀具的机床位置不动，工件坐标随新偏置重新换算。

        旧坐标系未建立/未配置或新坐标系未配置时，无法确定新工件坐标，
        位置标记为未知（不沿用上一偏置蒙算）。
        """
        self.wcs_used.add(new_wcs)
        if self.state.wcs == new_wcs:
            return
        old_off = self.cfg.offset_for(self.state.wcs)
        new_off = self.cfg.offset_for(new_wcs)
        for i, letter in enumerate("XYZ"):
            ax = getattr(self.state, letter.lower())
            if (ax.known and ax.value is not None
                    and old_off is not None and new_off is not None):
                machine = ax.value + old_off[i]
                setattr(self.state, letter.lower(),
                        Axis(machine - new_off[i], True))
            else:
                setattr(self.state, letter.lower(), Axis(None, False))
        self.state.wcs = new_wcs

    def _apply_length_comp(self, pl: ParsedLine,
                            issue_indexes: list[int]) -> bool:
        """处理本行刀长补偿 G43(加)/G44(减)/G49(取消) 与 H 词。

        - H 只在与 G43/G44 同段时生效；G49 段上的 H 不生效（不报错）；
          无补偿指令行上的 H 不生效（不报错，仅规范化标注）。
        - 同行运动使用新补偿：补偿改变且本行无 Z 词时（补偿-only 或仅
          XY 运动），主轴基准点保持不动，按新补偿重算刀尖工件 Z。
        - 缺 H / H 非正整数 / H 不在偏置表 / 同段补偿指令冲突：整段阻断，
          返回 False（不沿用旧值）。
        成功时把本行补偿信息存入 self._line_comp（含起点旧补偿 signed_old
        与段内新补偿 signed_new）。
        """
        keys = [g_code_key(w) for w in pl.g_words]
        comp_keys = [k for k in keys if k in LENGTH_COMP_G]
        h_words = [w for w in pl.words if w.letter == "H"]
        st = self.state

        # 无补偿指令：H 不生效；段沿用当前补偿（起终点一致）
        if not comp_keys:
            bare_h = " ".join(f"H{fmt_num(w.value)}" for w in h_words)
            self._line_comp = {
                "changed": False, "token": "",
                "bare_h": bare_h,
                "signed_old": st.comp_signed, "signed_new": st.comp_signed,
                "h": st.comp_h, "direction": st.comp_direction}
            return True

        # 同段冲突：G43/G44/G49 出现多个，或同一补偿码重复
        if len(comp_keys) > 1 or len(set(comp_keys)) != len(comp_keys):
            issue_indexes.append(self._issue(
                "LENGTH_COMP_CONFLICT", pl,
                f"同一程序段出现冲突的刀长补偿指令 "
                f"{'/'.join('G' + k for k in keys if k in LENGTH_COMP_G)}"
                "（G43/G44/G49 同段互斥）；该段阻断，不沿用旧补偿值",
                {"comp_codes": ["G" + k for k in comp_keys]}))
            return False
        code = comp_keys[0]
        direction = None if code == "49" else LENGTH_COMP_G[code]
        token = f"G{code}"
        event = None

        if direction is None:
            # G49：取消补偿；同行 H 不生效。记录被取消的 H/方向，
            # 供按 H 筛选时只保留结束该 H 补偿段的 G49 事件。
            event = {"code": "G49", "direction": "cancel", "h": None,
                     "offset_mm": None, "signed_offset_mm": 0.0,
                     "cancels_h": st.comp_h,
                     "cancels_direction": st.comp_direction}
        else:
            if not h_words:
                issue_indexes.append(self._issue(
                    "LENGTH_COMP_MISSING_H", pl,
                    f"G{code} 刀长补偿需要同一程序段给出 H 寄存器号"
                    "（如 G43 H1）；该段阻断，不沿用旧补偿值",
                    {"comp_code": f"G{code}", "h": None}))
                return False
            if len(h_words) > 1:
                issue_indexes.append(self._issue(
                    "LENGTH_COMP_CONFLICT", pl,
                    f"同一程序段给出 {len(h_words)} 个 H 号"
                    f"（{'/'.join('H' + fmt_num(w.value) for w in h_words)}），"
                    "刀长补偿只能指定一个 H；该段阻断",
                    {"comp_codes": ["G" + code],
                     "h_words": [fmt_num(w.value) for w in h_words]}))
                return False
            hw = h_words[0]
            h_int = (int(hw.value) if float(hw.value).is_integer() else None)
            table = self.cfg.length_offset_for(h_int)
            if h_int is None or h_int <= 0 or table is None:
                issue_indexes.append(self._issue(
                    "LENGTH_COMP_H_NOT_FOUND", pl,
                    f"刀长补偿 H{fmt_num(hw.value)} 非法或不在机床配置 "
                    "length_offsets 偏置表中（H 必须为正整数且已登记）；"
                    "该段阻断，不沿用旧补偿值",
                    {"comp_code": f"G{code}",
                     "h": h_int if (h_int is not None and h_int > 0) else None,
                     "h_raw": hw.value,
                     "registered_h": sorted(self.cfg.length_offsets)}))
                return False
            signed = self._comp_sign(direction) * table
            token += f" H{h_int}"
            event = {"code": f"G{code}", "direction": direction,
                     "h": h_int, "offset_mm": round6(abs(table)),
                     "signed_offset_mm": round6(signed)}

        signed_old = st.comp_signed
        dir_old = st.comp_direction
        signed_new = event["signed_offset_mm"]
        new_h = event["h"]
        new_dir = None if event["direction"] == "cancel" else event["direction"]

        # 同行运动使用新补偿：本行没有 Z 词时，主轴基准点保持不动，
        # 重算刀尖工件坐标 Z（新刀尖 = 基准点 - 新补偿，基准点按旧补偿固定）
        has_z = any(w.letter == "Z" for w in pl.words)
        tip_adjusted = False
        if signed_new != signed_old and not has_z and st.z.known \
                and st.z.value is not None:
            st.z = Axis(st.z.value + signed_old - signed_new, True)
            tip_adjusted = True

        st.comp_direction = new_dir
        st.comp_h = new_h
        st.comp_signed = signed_new
        st.comp_apply_line = pl.line_no

        event.update({
            "line_no": pl.line_no, "source_line": pl.source,
            "wcs": st.wcs, "tip_z_recomputed": tip_adjusted,
            "spindle_z_fixed": tip_adjusted,
            "tip_z_mm": (round6(st.z.value)
                         if st.z.known and st.z.value is not None else None),
        })
        self.length_events.append(event)
        self._line_comp = {
            "changed": signed_new != signed_old or new_dir != dir_old,
            "token": token, "bare_h": "",
            "event": event,
            "signed_old": signed_old, "signed_new": signed_new,
            "h": new_h, "direction": new_dir, "tip_adjusted": tip_adjusted}
        return True

    def _snapshot(self) -> dict:
        """模态快照（附当前坐标系偏置，便于逐行轨迹直接读取）。"""
        snap = self.state.snapshot()
        off = self._current_offset()
        snap["wcs_configured"] = self.state.wcs is not None and off is not None
        snap["wcs_offset_mm"] = self._offset_out(off)
        return snap

    @staticmethod
    def _offset_out(off):
        if off is None:
            return None
        return {"x": round6(off[0]), "y": round6(off[1]), "z": round6(off[2])}

    def _machine_point(self, p, off=None):
        """工件坐标 -> 机床坐标（叠加当前坐标系偏置）；偏置未知返回全 None。"""
        off = self._current_offset() if off is None else off
        if off is None:
            return [None, None, None]
        return [p[0] + off[0] if p[0] is not None else None,
                p[1] + off[1] if p[1] is not None else None,
                p[2] + off[2] if p[2] is not None else None]

    # -- 刀长补偿：刀尖工件坐标 <-> 主轴基准点机床坐标 ---------------------
    # 约定：state.x/y/z 始终是刀尖工件坐标；G43 时代数补偿为 +H 表值，
    # G44 时为 -H 表值。主轴基准点机床坐标 = 刀尖工件 + 工件偏置
    # + (0, 0, 代数补偿)。安全 Z 按刀尖工件 Z 判定；Z 轴行程按基准点判定。

    @staticmethod
    def _comp_sign(direction: str | None) -> float:
        if direction == "plus":
            return 1.0
        if direction == "minus":
            return -1.0
        return 0.0

    def _signed_comp(self, direction: str | None, h: int | None) -> float:
        off = self.cfg.length_offset_for(h)
        if off is None or direction is None:
            return 0.0
        return self._comp_sign(direction) * off

    # -- 刀具半径补偿：G40/G41/G42 + D ------------------------------------

    def _cutter_block_unknown(self, pl, motion_mode, issue_indexes):
        """单位/定位模式不明导致位置未知时的半径补偿阻断登记。
        不改补偿状态（调用方回滚到行前快照）。"""
        st = self.state
        if st.cutter_phase == "pending_in":
            issue_indexes.append(self._issue(
                "CUTTER_APPROACH_INVALID", pl,
                "半径补偿切入段位置未知（单位/定位模式不明），无法解算刀心"
                "轨迹；该段阻断，不猜测轨迹，待切入状态保持",
                {"reason": "position_unknown", "plane": st.plane,
                 "d": st.cutter_d}))
        elif st.cutter_phase == "pending_out":
            issue_indexes.append(self._issue(
                "CUTTER_EXIT_INVALID", pl,
                "G40 退出段位置未知（单位/定位模式不明），无法解算刀心"
                "轨迹；该段阻断，补偿状态保持",
                {"reason": "position_unknown", "plane": st.plane,
                 "d": st.cutter_d}))
        elif st.cutter_phase in ("active", "broken"):
            issue_indexes.append(self._issue(
                "CUTTER_COMP_DISCONTINUOUS", pl,
                "半径补偿段位置未知（单位/定位模式不明），刀心轨迹无法连续；"
                "该段阻断，不猜测轨迹",
                {"reason": "position_unknown", "plane": st.plane,
                 "d": st.cutter_d}))

    def _accumulate_cutter(self, pl, motion_mode, segment, issue_indexes):
        """半径补偿段的刀心路径长度、扫掠包围盒累计与机床行程检查。"""
        cc = segment.get("cutter_compensation")
        if cc is None or not cc.get("continuous", True):
            return
        ui, vi, pi = PLANE_SPEC[cc.get("plane") or self.state.plane][:3]
        d = cc.get("d")
        pts = cc.get("center_points_mm") or []
        # 外角补弧链节（刀心折线的一部分）
        connector_pts = []
        for conn in segment.get("_cutter_connectors", []):
            conn3 = self._connector_3d(conn, pi, segment)
            connector_pts.extend(conn3)
        clen = 0.0
        chain = list(pts) + connector_pts
        for a, b in zip(chain, chain[1:]):
            if any(v is None for v in a + b):
                continue
            clen += math.sqrt(sum((bv - av) ** 2 for av, bv in zip(a, b)))
        bucket = self._cutter_path_bucket(d)
        bucket["segments"] += 1
        bucket["center_length"] += clen
        if cc.get("mode") == "tangent_engage":
            bucket["engages"] += 1
        if cc.get("mode") == "tangent_exit":
            bucket["exits"] += 1
        r_d = cc.get("radius_mm") or 0.0
        # 刀具扫掠：刀心折线（含外角补弧）平面两轴按半径 r_d 膨胀
        self._grow_cutter_swept(chain, (ui, vi), r_d, machine=False,
                                issue_indexes=issue_indexes, pl=pl, cc=cc)
        if self.state.wcs is not None and self._current_offset() is not None:
            self._grow_cutter_swept(chain, (ui, vi), r_d, machine=True,
                                    issue_indexes=issue_indexes, pl=pl, cc=cc)
        else:
            self.cutter_swept_wcs_known = False
        self.cutter_segments.append(cc)

    def _cutter_path_bucket(self, d):
        key = d if d is not None else "__none__"
        return self.cutter_path.setdefault(key, {
            "segments": 0, "center_length": 0.0,
            "engages": 0, "exits": 0})

    def _grow_cutter_swept(self, pts, plane_axes, r_d: float, machine: bool,
                           issue_indexes=None, pl=None, cc=None):
        """把刀心点（平面两轴按刀具半径膨胀）并入扫掠包围盒；
        machine=True 时叠加工件偏置与刀长补偿做机床行程检查/机床扫掠盒。"""
        ui, vi = plane_axes
        off = self._current_offset()
        signed = self.state.comp_signed
        if machine:
            if off is None:
                self.cutter_swept_wcs_known = False
                return
            bmin, bmax = self.cutter_swept_mbmin, self.cutter_swept_mbmax
        else:
            bmin, bmax = self.cutter_swept_bmin, self.cutter_swept_bmax
        c = self.cfg
        axis_lim = ((0, c.x_min, c.x_max, "X"),
                    (1, c.y_min, c.y_max, "Y"),
                    (2, c.z_min, c.z_max, "Z"))
        for p in pts:
            for i, v in enumerate(p):
                if v is None:
                    continue
                rad = r_d if i in (ui, vi) else 0.0
                lo, hi = v - rad, v + rad
                if machine:
                    zcomp = signed if i == 2 else 0.0
                    lo = lo + off[i] + zcomp
                    hi = hi + off[i] + zcomp
                if lo < bmin[i]:
                    bmin[i] = lo
                if hi > bmax[i]:
                    bmax[i] = hi
        if machine and issue_indexes is not None and pl is not None:
            # 逐段点列（而非全局累计盒）做行程检查，避免重复报告
            seg_min = [math.inf] * 3
            seg_max = [-math.inf] * 3
            for p in pts:
                for i, v in enumerate(p):
                    if v is None:
                        continue
                    rad = r_d if i in (ui, vi) else 0.0
                    zcomp = signed if i == 2 else 0.0
                    lo = v - rad + off[i] + zcomp
                    hi = v + rad + off[i] + zcomp
                    seg_min[i] = min(seg_min[i], lo)
                    seg_max[i] = max(seg_max[i], hi)
            for i, lo_lim, hi_lim, axis_name in axis_lim:
                if not math.isfinite(seg_min[i]):
                    continue
                over_lo = lo_lim - seg_min[i]
                over_hi = seg_max[i] - hi_lim
                if over_lo > MM_EPS:
                    v = {"value_mm": round(seg_min[i], 6),
                         "bound_mm": lo_lim,
                         "overshoot_mm": round(over_lo, 6), "side": "min",
                         "axis": axis_name,
                         "plane": (cc or {}).get("plane"),
                         "d": (cc or {}).get("d"),
                         "checked_path": "tool_swept_envelope"}
                    issue_indexes.append(self._issue(
                        "OUT_OF_BOUNDS", pl,
                        f"{axis_name} 轴刀具扫掠范围（刀心±刀具半径，并叠加"
                        f"{self.state.wcs} 偏置与刀长补偿）机床坐标最小 "
                        f"{fmt_num(v['value_mm'])} mm 越出行程下限 "
                        f"{fmt_num(lo_lim)} mm（超程 "
                        f"{fmt_num(v['overshoot_mm'])} mm；半径补偿扫掠）",
                        v))
                elif over_hi > MM_EPS:
                    v = {"value_mm": round(seg_max[i], 6),
                         "bound_mm": hi_lim,
                         "overshoot_mm": round(over_hi, 6), "side": "max",
                         "axis": axis_name,
                         "plane": (cc or {}).get("plane"),
                         "d": (cc or {}).get("d"),
                         "checked_path": "tool_swept_envelope"}
                    issue_indexes.append(self._issue(
                        "OUT_OF_BOUNDS", pl,
                        f"{axis_name} 轴刀具扫掠范围（刀心±刀具半径，并叠加"
                        f"{self.state.wcs} 偏置与刀长补偿）机床坐标最大 "
                        f"{fmt_num(v['value_mm'])} mm 越出行程上限 "
                        f"{fmt_num(hi_lim)} mm（超程 "
                        f"{fmt_num(v['overshoot_mm'])} mm；半径补偿扫掠）",
                        v))

    def _apply_cutter_comp(self, pl: ParsedLine,
                           issue_indexes: list[int]) -> bool:
        """处理本行半径补偿 G41(左)/G42(右)/G40(取消) 与 D 词。

        - G41/G42 必须同段给一个正整数且已登记的 D；纯设定行不移动刀具，
          进入 pending_in，等待下一非零平面内 G1 作为切入段。
        - G40 取消：纯设定行进入 pending_out 等待非零 G1 退出；
          G41/G42 与 G0/G2/G3 同段等切入无效情况由运动流程另行判定。
        - 同段冲突（G41/G42/G40 混用、重复、多个 D）整段阻断返回 False。
        """
        keys = [g_code_key(w) for w in pl.g_words]
        crc_keys = [k for k in keys if k in CUTTER_COMP_G]
        d_words = [w for w in pl.words if w.letter == "D"]
        st = self.state

        if not crc_keys:
            bare_d = " ".join(f"D{fmt_num(w.value)}" for w in d_words)
            self._line_cutter = {
                "token": "", "bare_d": bare_d, "event": None,
                "code": None, "side": st.cutter_side, "d": st.cutter_d,
                "r": st.cutter_r}
            return True

        if len(crc_keys) > 1 or len(set(crc_keys)) != len(crc_keys):
            issue_indexes.append(self._issue(
                "CUTTER_COMP_CONFLICT", pl,
                f"同一程序段出现冲突的半径补偿指令 "
                f"{'/'.join('G' + k for k in crc_keys)}"
                "（G40/G41/G42 同段互斥）；该段阻断，补偿状态不变",
                {"comp_codes": ["G" + k for k in crc_keys],
                 "plane": st.plane, "d": st.cutter_d}))
            return False
        if len(d_words) > 1:
            issue_indexes.append(self._issue(
                "CUTTER_COMP_CONFLICT", pl,
                f"同一程序段给出 {len(d_words)} 个 D 号"
                f"（{'/'.join('D' + fmt_num(w.value) for w in d_words)}），"
                "半径补偿只能指定一个 D；该段阻断，补偿状态不变",
                {"comp_codes": ["G" + crc_keys[0]],
                 "d_words": [fmt_num(w.value) for w in d_words],
                 "plane": st.plane, "d": st.cutter_d}))
            return False

        code = crc_keys[0]
        token = f"G{code}"
        event = None

        if code == "40":
            # G40 行上的 D 不生效（不报错，仅规范化标注）
            bare_d = " ".join(f"D{fmt_num(w.value)}" for w in d_words)
            if st.cutter_phase == "inactive" or st.cutter_phase == "broken":
                # 未补偿或轮廓已断：G40 直接取消，不要求退出段、不登记事件
                if st.cutter_phase == "broken":
                    st.cutter_phase = "inactive"
                    st.cutter_side = None
                    st.cutter_d = None
                    st.cutter_r = 0.0
                    st.cutter_plane = None
                    st.cutter_cancel_line = pl.line_no
                self._line_cutter = {
                    "token": token, "bare_d": bare_d, "event": None,
                    "code": "G40", "side": None, "d": None, "r": 0.0}
                return True
            if st.cutter_phase == "pending_out":
                # 幂等：保持待退出
                self._line_cutter = {
                    "token": token, "bare_d": bare_d, "event": None,
                    "code": "G40", "side": st.cutter_side,
                    "d": st.cutter_d, "r": st.cutter_r}
                return True
            if st.cutter_phase == "pending_in":
                # 取消本次未完成的切入：回到 inactive
                cancels_d = st.cutter_d
                event = {"code": "G40", "side": None, "d": None,
                         "radius_mm": None, "cancels_d": cancels_d,
                         "cancels_side": st.cutter_side,
                         "line_no": pl.line_no, "source_line": pl.source,
                         "plane": st.plane, "wcs": st.wcs}
                self.cutter_events.append(event)
                st.cutter_phase = "inactive"
                st.cutter_side = None
                st.cutter_d = None
                st.cutter_r = 0.0
                st.cutter_plane = None
                st.cutter_apply_line = None
                st.cutter_cancel_line = pl.line_no
                self._line_cutter = {
                    "token": token, "bare_d": bare_d, "event": event,
                    "code": "G40", "side": None, "d": None, "r": 0.0}
                return True
            event = {"code": "G40", "side": None, "d": None,
                     "radius_mm": None, "cancels_d": st.cutter_d,
                     "cancels_side": st.cutter_side,
                     "line_no": pl.line_no, "source_line": pl.source,
                     "plane": st.plane, "wcs": st.wcs}
            self.cutter_events.append(event)
            st.cutter_phase = "pending_out"
            st.cutter_cancel_line = pl.line_no
            self._line_cutter = {
                "token": token, "bare_d": bare_d, "event": event,
                "code": "G40", "side": st.cutter_side, "d": st.cutter_d,
                "r": st.cutter_r}
            return True

        # G41 / G42
        side = CUTTER_COMP_G[code]
        if not d_words:
            issue_indexes.append(self._issue(
                "CUTTER_COMP_MISSING_D", pl,
                f"G{code} 半径补偿必须在同一程序段给出 D 寄存器号"
                "（如 G41 D1）；该段阻断，补偿状态不变",
                {"comp_code": f"G{code}", "plane": st.plane,
                 "d": None}))
            return False
        dw = d_words[0]
        d_int = int(dw.value) if float(dw.value).is_integer() else None
        table = self.cfg.radius_offset_for(d_int)
        if d_int is None or d_int <= 0 or table is None:
            issue_indexes.append(self._issue(
                "CUTTER_COMP_D_NOT_FOUND", pl,
                f"半径补偿 D{fmt_num(dw.value)} 非法或未在机床配置 "
                "radius_offsets 半径表中登记（D 必须为正整数且已登记）；"
                "该段阻断，补偿状态不变",
                {"comp_code": f"G{code}",
                 "d": (d_int if d_int is not None and d_int > 0 else None),
                 "d_raw": dw.value,
                 "registered_d": sorted(self.cfg.radius_offsets),
                 "plane": st.plane}))
            return False
        token += f" D{d_int}"
        # 重新启用（含断链后重新 G41/G42）：登记待切入；active 中重复写
        # 同向同 D 视为幂等设定（纯设定/运动均不再产生切入），换侧或换 D
        # 按冲突阻断。
        if st.cutter_phase == "active" and st.cutter_d == d_int \
                and st.cutter_side == side:
            self._line_cutter = {
                "token": token, "bare_d": "", "event": None,
                "code": f"G{code}", "side": side, "d": d_int, "r": table}
            return True
        if st.cutter_phase == "broken":
            # 断链后允许重新 G41/G42 切入（旧轮廓已不连续，新启用另起一段）
            pass
        elif st.cutter_phase in ("active", "pending_out") \
                or (st.cutter_phase == "pending_in"
                    and (st.cutter_d != d_int or st.cutter_side != side)):
            issue_indexes.append(self._issue(
                "CUTTER_COMP_CONFLICT", pl,
                f"半径补偿已生效或待退出（{st.cutter_phase}），"
                f"不能直接改用 G{code} D{d_int}；须先用 G40 经非零 G1 退出后"
                "再重新切入；该段阻断，补偿状态不变",
                {"comp_codes": [f"G{code}"], "d": d_int,
                 "active_d": st.cutter_d, "active_side": st.cutter_side,
                 "phase": st.cutter_phase, "plane": st.plane}))
            return False
        event = {"code": f"G{code}", "side": side, "d": d_int,
                 "radius_mm": round6(table), "line_no": pl.line_no,
                 "source_line": pl.source, "plane": st.plane,
                 "wcs": st.wcs}
        self.cutter_events.append(event)
        st.cutter_phase = "pending_in"
        st.cutter_side = side
        st.cutter_d = d_int
        st.cutter_r = table
        st.cutter_plane = st.plane
        st.cutter_apply_line = pl.line_no
        self._line_cutter = {
            "token": token, "bare_d": "", "event": event,
            "code": f"G{code}", "side": side, "d": d_int, "r": table}
        return True

    def _cutter_block_motion(self, pl: ParsedLine, motion_key: str | None,
                             issue_indexes: list[int]) -> bool:
        """运动归属确定后、几何解算前的半径补偿阻断检查（当前只查 G0；
        平面切换与固定循环在主流程中更早拦截）。

        返回 True 表示本行必须按半径补偿问题阻断（已登记问题）。
        阻断不改补偿状态：主流程回滚到行前快照，待切入仍可由后续非零 G1
        完成切入。
        """
        st = self.state
        if motion_key != "0" or st.cutter_phase == "inactive":
            return False
        if st.cutter_phase == "pending_in":
            code = "CUTTER_APPROACH_INVALID"
            basis = ("半径补偿切入段必须是非零平面内 G1 直线切削段，"
                     "G0 快速移动不能作为切入段；该段阻断，待切入状态保持，"
                     "后续非零 G1 仍可切入")
        elif st.cutter_phase == "pending_out":
            code = "CUTTER_EXIT_INVALID"
            basis = ("G40 退出段必须是非零平面内 G1 直线切削段，"
                     "G0 快速移动不能作为退出段；该段阻断，补偿状态保持")
        else:
            code = "CUTTER_COMP_DISCONTINUOUS"
            basis = ("半径补偿进行中不允许 G0 快速移动（会使偏置轨迹"
                     "不连续）；该段阻断，不猜测刀心轨迹")
        issue_indexes.append(self._issue(
            code, pl, basis,
            {"reason": "rapid_move", "plane": st.plane,
             "phase": st.cutter_phase, "d": st.cutter_d}))
        return True

    def _cutter_reset_pending_in(self):
        """切入失败：撤销本次未完成的 G41/G42 启用（回到 inactive）。"""
        st = self.state
        if self.cutter_events and self.cutter_events[-1].get(
                "line_no") == st.cutter_apply_line \
                and self.cutter_events[-1]["code"] in ("G41", "G42"):
            self.cutter_events.pop()
        st.cutter_phase = "inactive"
        st.cutter_side = None
        st.cutter_d = None
        st.cutter_r = 0.0
        st.cutter_plane = None
        st.cutter_apply_line = None

    def _cutter_plane_axes(self):
        spec = PLANE_SPEC[self.state.cutter_plane or self.state.plane]
        return spec[0], spec[1], spec[2]  # u_idx, v_idx, perp_idx

    def _in_plane_disp(self, start_pt, end_pt) -> float:
        ui, vi, _ = self._cutter_plane_axes()
        du = (end_pt[ui] or 0.0) - (start_pt[ui] or 0.0)
        dv = (end_pt[vi] or 0.0) - (start_pt[vi] or 0.0)
        return math.hypot(du, dv)

    def _apply_cutter_to_segment(self, pl: ParsedLine, segment: dict,
                                 motion_mode: str,
                                 issue_indexes: list[int]) -> bool:
        """几何解算成功后，把半径补偿应用到本段。

        返回 False 表示本行因半径补偿问题阻断（已登记问题，调用方回滚）。
        - pending_in：只接受非零平面内 G1 切入；active 接受 G1/G2/G3
          （含螺旋、平面内整圆、纯垂直 G1）；pending_out 只接受非零 G1 退出。
        - 纯垂直 G1（平面内零位移）：刀心 XY 保持偏置、Z 联动，不断开轮廓。
        """
        st = self.state
        phase = st.cutter_phase
        lc = self._line_cutter or {}
        line_code = lc.get("code")
        if phase == "inactive" and line_code is None:
            return True

        ui, vi, pi = self._cutter_plane_axes()
        start_pt, end_pt = segment["start"], segment["end"]
        plane_disp = self._in_plane_disp(start_pt, end_pt)
        r_d = st.cutter_r or 0.0
        is_vertical = plane_disp <= MM_EPS
        crc_plane = st.cutter_plane or st.plane

        def _fail(code, basis, details=None):
            d = {"plane": crc_plane, "phase": phase,
                 "d": st.cutter_d, "in_plane_displacement_mm":
                     round6(plane_disp)}
            if details:
                d.update(details)
            issue_indexes.append(self._issue(code, pl, basis, d))
            return False

        # 纯设定行（无轴词）不进入本函数；这里只处理运动段。
        if phase == "pending_in":
            if motion_mode != "linear" or is_vertical:
                return _fail(
                    "CUTTER_APPROACH_INVALID",
                    "半径补偿切入段必须是非零平面内 G1 直线切削段（刀具沿"
                    "该段建立完整偏置）；当前段为"
                    + ("纯垂直移动（平面内零位移）" if is_vertical
                       else MOTION_CN.get(motion_mode, motion_mode))
                    + "，不能作为切入段；该段阻断，待切入状态保持")
            return self._cutter_engage(pl, segment, issue_indexes)

        if phase == "pending_out":
            if motion_mode != "linear" or is_vertical:
                return _fail(
                    "CUTTER_EXIT_INVALID",
                    "G40 退出段必须是非零平面内 G1 直线切削段（刀具沿该段"
                    "法向撤销偏置）；当前段为"
                    + ("纯垂直移动（平面内零位移）" if is_vertical
                       else MOTION_CN.get(motion_mode, motion_mode))
                    + "；该段阻断，保持补偿状态")
            return self._cutter_exit(pl, segment, issue_indexes)

        if phase == "active":
            if motion_mode == "linear":
                if is_vertical:
                    return self._cutter_vertical(pl, segment, issue_indexes)
                return self._cutter_contour(pl, segment, arc=False,
                                            issue_indexes=issue_indexes)
            # G2/G3（含螺旋）
            return self._cutter_contour(pl, segment, arc=True,
                                        issue_indexes=issue_indexes)

        # broken：本段不生成刀心轨迹（保守不猜测），允许 G40/G41/G42
        # 设定行（在 _apply_cutter_comp 处理）；运动段标记不连续。
        return _fail(
            "CUTTER_COMP_DISCONTINUOUS",
            "半径补偿轮廓此前已断开，须重新 G41/G42 切入或 G40 退出；"
            "该段不生成刀心轨迹",
            {"reason": "chain_broken"})

    def _uv(self, pt, ui, vi):
        return (pt[ui] if pt[ui] is not None else 0.0,
                pt[vi] if pt[vi] is not None else 0.0)

    def _map_3d(self, uv, perp_value, ui, vi, pi) -> list:
        p = [None, None, None]
        p[ui], p[vi] = uv[0], uv[1]
        p[pi] = perp_value
        return p

    def _ramp_primitive(self, start_uv, end_uv, side, r_d):
        """构造切入/退出段的满偏置直线 prim（刀心尾/首段，方向=程序方向）。

        起点为程序起点按侧法向偏置 r_d（满偏置），终点为程序终点同法向
        偏置；窗口 t∈[0, 弦长]，程序端点记原始段端点以便连接校验。
        """
        dx, dy = end_uv[0] - start_uv[0], end_uv[1] - start_uv[1]
        length = math.hypot(dx, dy)
        d = (dx / length, dy / length)
        nrm = side_normal(side, d)
        shift = (r_d * nrm[0], r_d * nrm[1])
        p_off_s = (start_uv[0] + shift[0], start_uv[1] + shift[1])
        return Primitive(
            kind="line", prog_start=start_uv, prog_end=end_uv,
            p0=p_off_s, direction=d, win0=0.0, win1=length)

    def _cutter_engage(self, pl, segment, issue_indexes) -> bool:
        """切入：非零平面内 G1，刀心从程序起点（无偏置）斜变到满偏置终点。

        尾段为满偏置直线 prim，与下一偏置段做正常内角裁切/外角补弧。
        """
        st = self.state
        ui, vi, pi = self._cutter_plane_axes()
        s, e = segment["start"], segment["end"]
        u0, v0 = self._uv(s, ui, vi)
        u1, v1 = self._uv(e, ui, vi)
        prim = self._ramp_primitive((u0, v0), (u1, v1),
                                    st.cutter_side, st.cutter_r)
        tail_uv = prim.point(prim.win1)
        perp0, perp1 = s[pi], e[pi]
        center_points = [self._map_3d((u0, v0), perp0, ui, vi, pi),
                         self._map_3d(tail_uv, perp1, ui, vi, pi)]
        self._cutter_attach(
            segment, prim, center_points,
            {"mode": "tangent_engage", "line_no": pl.line_no})
        segment["cutter_compensation"]["ramp"] = {
            "kind": "engage",
            "start_mm": [round6(u0), round6(v0)],
            "end_mm": [round6(tail_uv[0]), round6(tail_uv[1])]}
        self._cutter_pending = {"segment": segment, "prim": prim,
                                "line_no": pl.line_no}
        st.cutter_phase = "active"
        return True

    def _cutter_exit(self, pl, segment, issue_indexes) -> bool:
        """退出：非零平面内 G1，刀心从满偏置起点斜变到程序终点（无偏置）。

        首段为满偏置直线 prim，与上一偏置段做正常内角裁切/外角补弧。
        """
        st = self.state
        ui, vi, pi = self._cutter_plane_axes()
        s, e = segment["start"], segment["end"]
        u0, v0 = self._uv(s, ui, vi)
        u1, v1 = self._uv(e, ui, vi)
        prim = self._ramp_primitive((u0, v0), (u1, v1),
                                    st.cutter_side, st.cutter_r)
        head_uv = prim.point(prim.win0)
        perp0, perp1 = s[pi], e[pi]
        center_points = [self._map_3d(head_uv, perp0, ui, vi, pi),
                         self._map_3d((u1, v1), perp1, ui, vi, pi)]
        self._cutter_attach(segment, prim, center_points,
                            {"mode": "tangent_exit", "line_no": pl.line_no})
        segment["cutter_compensation"]["active"] = False
        segment["cutter_compensation"]["code"] = "G40"
        segment["cutter_compensation"]["d"] = st.cutter_d
        segment["cutter_compensation"]["ramp"] = {
            "kind": "exit",
            "start_mm": [round6(head_uv[0]), round6(head_uv[1])],
            "end_mm": [round6(u1), round6(v1)]}
        if not self._cutter_join(pl, segment, prim, issue_indexes,
                                 exit_mode=True):
            return False
        # 完成退出
        st.cutter_phase = "inactive"
        st.cutter_side = None
        st.cutter_d = None
        st.cutter_r = 0.0
        st.cutter_plane = None
        st.cutter_cancel_line = pl.line_no
        self._cutter_pending = None
        return True

    def _cutter_contour(self, pl, segment, arc: bool,
                        issue_indexes) -> bool:
        """active 中的偏置轮廓段（G1 直线 / G2-G3 圆弧含螺旋）。"""
        st = self.state
        ui, vi, pi = self._cutter_plane_axes()
        s, e = segment["start"], segment["end"]
        u0, v0 = self._uv(s, ui, vi)
        u1, v1 = self._uv(e, ui, vi)
        r_d = st.cutter_r
        side = st.cutter_side
        try:
            if not arc:
                prim = offset_line((u0, v0), (u1, v1), side, r_d)
            else:
                arcinf = segment["arc"]
                cu, cv = arcinf["center_mm"]
                # arc center_mm 为当前平面 uv 坐标（见 _build_arc）
                radius = arcinf["radius_mm"]
                a0 = math.atan2(v0 - cv, u0 - cu)
                sweep = math.radians(arcinf["sweep_deg"])
                clockwise = arcinf["direction"] == "CW"
                prim = offset_arc(
                    (cu, cv), radius, a0, sweep, clockwise, side, r_d,
                    prog_start=(u0, v0), prog_end=(u1, v1))
        except JoinError as ex:
            issue_indexes.append(self._issue(
                "CUTTER_ARC_RADIUS", pl,
                f"半径补偿后圆弧有效半径非正：{ex}；不猜测刀心轨迹，"
                "该段阻断（回滚本行，补偿状态保持）",
                {"reason": "arc_radius_nonpositive",
                 "plane": st.cutter_plane, "d": st.cutter_d,
                 "tool_radius_mm": round6(r_d),
                 "program_radius_mm": (segment["arc"]["radius_mm"]
                                       if arc else None)}))
            return False

        # 初始刀心点（窗口裁切前），join 成功后按窗口重采样
        win_points_uv = sample_primitive(prim, prim.win0, prim.win1)
        perp_points = self._segment_perp_values(segment, len(win_points_uv),
                                                pi)
        center_points = [self._map_3d(uv, perp_points[k], ui, vi, pi)
                         for k, uv in enumerate(win_points_uv)]
        info = {"mode": "contour", "line_no": pl.line_no,
                "inward": getattr(prim, "offset_inward", False)}
        self._cutter_attach(segment, prim, center_points, info)
        if not self._cutter_join(pl, segment, prim, issue_indexes):
            return False
        self._cutter_pending = {"segment": segment, "prim": prim,
                                "line_no": pl.line_no}
        return True

    def _cutter_vertical(self, pl, segment, issue_indexes) -> bool:
        """active 中的纯垂直 G1：刀心平面坐标保持偏置，仅垂直轴联动。"""
        st = self.state
        ui, vi, pi = self._cutter_plane_axes()
        s, e = segment["start"], segment["end"]
        u0, v0 = self._uv(s, ui, vi)
        dx = dy = 0.0
        # 沿用上一偏置段终点作为刀心 XY
        pend = self._cutter_pending
        if pend is not None:
            cuv = pend["prim"].point(pend["prim"].win1)
        else:
            cuv = (u0, v0)  # 理论上不会发生（active 必有 pending）
        cp0 = self._map_3d(cuv, s[pi], ui, vi, pi)
        cp1 = self._map_3d(cuv, e[pi], ui, vi, pi)
        self._cutter_attach(segment, None, [cp0, cp1],
                            {"mode": "vertical_hold", "line_no": pl.line_no})
        return True

    def _segment_perp_values(self, segment, n, pi) -> list:
        """从段采样点取垂直轴序列（线性联动）；端点精确。"""
        pts = segment["points"]
        p0 = pts[0][pi]
        p1 = pts[-1][pi]
        if p0 is None or p1 is None:
            return [None] * n
        if n == 1:
            return [p1]
        return [p0 + (p1 - p0) * (k / (n - 1)) for k in range(n)]

    def _cutter_attach(self, segment, prim, center_points, info):
        """把刀心轨迹元信息挂到运动段（尚未裁切，join 后重写）。"""
        st = self.state
        segment["_cutter_prim"] = prim
        segment["cutter_compensation"] = {
            "active": True,
            "code": ("G41" if st.cutter_side == "left" else "G42"),
            "side": st.cutter_side,
            "d": st.cutter_d,
            "radius_mm": round6(st.cutter_r),
            "plane": st.cutter_plane or st.plane,
            "mode": info["mode"],
            "center_points_mm": [[round6(c) for c in p]
                                 for p in center_points],
            "junction": None,
            "continuous": True,
        }

    def _cutter_attach_exit(self, segment, center_points, info):
        segment["_cutter_prim"] = None
        segment["cutter_compensation"] = {
            "active": False,
            "code": "G40",
            "side": None,
            "d": None,
            "radius_mm": 0.0,
            "plane": self.state.cutter_plane or self.state.plane,
            "mode": info["mode"],
            "center_points_mm": [[round6(c) for c in p]
                                 for p in center_points],
            "junction": None,
            "continuous": True,
        }

    def _cutter_join(self, pl, cur_seg, cur_prim, issue_indexes,
                     exit_mode: bool = False) -> bool:
        """把当前偏置段与上一偏置段按几何关系连接（内角裁切/外角补弧）。

        失败 => 登记 CUTTER_COMP_DISCONTINUOUS 并阻断当前段（调用方回滚）。
        exit_mode 时当前段为 G40 退出斜切段（首段满偏置）。
        """
        st = self.state
        pend = self._cutter_pending
        if pend is None:
            return True  # 无前段（不应发生，切入段不调用本函数）
        prev_seg, prev_prim = pend["segment"], pend["prim"]
        try:
            result = join_primitives(prev_prim, cur_prim, st.cutter_r)
        except JoinError as ex:
            issue_indexes.append(self._issue(
                "CUTTER_COMP_DISCONTINUOUS", pl,
                f"相邻偏置段无法连续：{ex}；不猜测刀心轨迹，该段阻断"
                "（回滚本行，补偿状态保持）",
                {"reason": "join_discontinuous",
                 "plane": st.cutter_plane, "d": st.cutter_d,
                 "prev_line_no": pend["line_no"]}))
            return False
        self._apply_join_result(pl, result, prev_seg, prev_prim,
                                cur_seg, cur_prim, exit_mode)
        return True

    def _apply_join_result(self, pl, result, prev_seg, prev_prim,
                           cur_seg, cur_prim, exit_mode=False):
        """按连接结果裁切两段窗口、重采样刀心点，并在段上记录 junction。"""
        pi = self._cutter_plane_axes()[2]
        kind = result.kind
        if kind == "inner":
            # 前段：从其窗口起点裁到交点
            self._resample_prim_segment(prev_seg, prev_prim,
                                        prev_prim.win0, result.prev_t)
            prev_prim.win1 = result.prev_t
            # 当前段：从交点裁到窗口终点
            self._resample_prim_segment(cur_seg, cur_prim,
                                        result.next_t, cur_prim.win1,
                                        exit_mode=exit_mode)
            cur_prim.win0 = result.next_t
            point_uv = cur_prim.point(result.next_t)
            junction_at = {"kind": "inner_trim", "at_line_no": pl.line_no,
                           "point_mm": [round6(v) for v in point_uv]}
            prev_seg["cutter_compensation"]["junction"] = {
                "kind": "inner_trim", "with_line_no": pl.line_no,
                "point_mm": junction_at["point_mm"]}
            cur_seg["cutter_compensation"]["junction"] = junction_at
        elif kind == "outer":
            conn = result.connector
            conn_pts = self._connector_3d(conn, pi, prev_seg)
            junction_out = {
                "kind": "outer_arc",
                "at_line_no": pl.line_no,
                "center_mm": [round6(conn.center[0]),
                              round6(conn.center[1])],
                "radius_mm": round6(conn.radius),
                "sweep_deg": round(math.degrees(conn.sweep), 6),
                "points_mm": [[round6(c) for c in p] for p in conn_pts],
            }
            prev_junction = dict(junction_out)
            prev_junction["with_line_no"] = pl.line_no
            prev_seg["cutter_compensation"]["junction"] = prev_junction
            cur_seg["cutter_compensation"]["junction"] = junction_out
            # 外角补弧作为独立刀心链节挂到当前段（在角点处生成）
            cur_seg.setdefault("_cutter_connectors", []).append(conn)
        else:  # collinear
            prev_seg["cutter_compensation"]["junction"] = {
                "kind": "collinear", "with_line_no": pl.line_no}
            cur_seg["cutter_compensation"]["junction"] = {
                "kind": "collinear", "at_line_no": pl.line_no}

    def _connector_3d(self, conn, pi, prev_seg):
        """外角接弧三维点：垂直轴取角点处程序垂直值。"""
        p_perp = prev_seg["end"][pi]
        ui, vi, _ = self._cutter_plane_axes()
        out = []
        for uv in conn.points:
            p = [None, None, None]
            p[ui], p[vi], p[pi] = uv[0], uv[1], p_perp
            out.append(p)
        return out

    def _resample_prim_segment(self, seg, prim, t0, t1,
                               exit_mode=False):
        """按新窗口重写段上的刀心采样点（垂直轴按程序点线性映射）。

        切入/退出斜切段（ramp）刀心折线起点为无偏置程序端点：切入时尾部
        窗口起点用 ramp.start 锚定，退出时头部窗口终点用 ramp.end 锚定。
        """
        ui, vi, pi_idx = self._cutter_plane_axes()
        uv_pts = sample_primitive(prim, t0, t1)
        pts = seg["points"]
        p0 = pts[0][pi_idx]
        p1 = pts[-1][pi_idx]
        n = len(uv_pts)
        cc = seg["cutter_compensation"]
        ramp = cc.get("ramp")
        center = []
        if ramp is not None and ramp["kind"] == "engage":
            # 保留无偏置起点，替换尾部满偏置采样
            start_uv = tuple(ramp["start_mm"])
            for k, uv in enumerate(uv_pts):
                frac = 0.0 if n == 1 else k / (n - 1)
                perp = (p0 + (p1 - p0) * frac if (p0 is not None
                                                  and p1 is not None) else None)
                center.append(self._map_3d(uv, perp, ui, vi, pi_idx))
            center = [self._map_3d(start_uv, p0, ui, vi, pi_idx)] + center
        elif ramp is not None and ramp["kind"] == "exit":
            # 保留无偏置终点，替换头部满偏置采样
            end_uv = tuple(ramp["end_mm"])
            for k, uv in enumerate(uv_pts):
                frac = 0.0 if n == 1 else k / (n - 1)
                perp = (p0 + (p1 - p0) * frac if (p0 is not None
                                                  and p1 is not None) else None)
                center.append(self._map_3d(uv, perp, ui, vi, pi_idx))
            center = center + [self._map_3d(end_uv, p1, ui, vi, pi_idx)]
        else:
            for k, uv in enumerate(uv_pts):
                frac = 0.0 if n == 1 else k / (n - 1)
                perp = (p0 + (p1 - p0) * frac if (p0 is not None
                                                  and p1 is not None) else None)
                center.append(self._map_3d(uv, perp, ui, vi, pi_idx))
        cc["center_points_mm"] = [[round6(c) for c in p] for p in center]

    def _cutter_finalize_eof(self):
        """程序结束时半径补偿仍未完成切入/退出：定位到 G41/G42 或 G40
        所在原行报告（不改动状态）。"""
        st = self.state
        if st.cutter_phase in ("inactive", "broken"):
            return
        if st.cutter_phase == "pending_in":
            line_no = st.cutter_apply_line
            src = self._source_for_line(line_no)
            self.issues.append(Issue(
                code="CUTTER_APPROACH_INVALID",
                severity=ISSUE_SEVERITY["CUTTER_APPROACH_INVALID"],
                line_no=line_no or 0, source=src, normalized="",
                state_in=self._snapshot(), state_out=self._snapshot(),
                basis=("程序结束时仍未给出非零平面内 G1 切入段：G41/G42 Dn "
                       "只登记了待切入状态，刀补未生效"),
                details={"reason": "eof_no_approach",
                         "plane": st.cutter_plane, "d": st.cutter_d}))
            return
        if st.cutter_phase == "pending_out":
            line_no = st.cutter_cancel_line
            src = self._source_for_line(line_no)
            self.issues.append(Issue(
                code="CUTTER_EXIT_INVALID",
                severity=ISSUE_SEVERITY["CUTTER_EXIT_INVALID"],
                line_no=line_no or 0, source=src, normalized="",
                state_in=self._snapshot(), state_out=self._snapshot(),
                basis=("程序结束时 G40 之后缺少非零平面内 G1 退出段，"
                       "半径补偿未正常退出"),
                details={"reason": "eof_no_exit", "plane": st.cutter_plane,
                         "d": st.cutter_d, "g40_line_no": line_no}))
            return
        # active：补偿中程序结束
        line_no = st.cutter_apply_line
        src = self._source_for_line(line_no)
        self.issues.append(Issue(
            code="CUTTER_EXIT_INVALID",
            severity=ISSUE_SEVERITY["CUTTER_EXIT_INVALID"],
            line_no=line_no or 0, source=src, normalized="",
            state_in=self._snapshot(), state_out=self._snapshot(),
            basis=("程序结束时半径补偿仍处于激活状态，缺少 G40 与非零平面内 "
                   "G1 退出段；相关刀心轨迹不完整"),
            details={"reason": "eof_active", "plane": st.cutter_plane,
                     "d": st.cutter_d, "engage_line_no": line_no}))

    def _source_for_line(self, line_no):
        if not line_no:
            return ""
        for e in reversed(self.entries):
            if e["line_no"] == line_no:
                return e["source_line"]
        return ""

    def _comp_out(self):
        """当前刀长补偿（供轨迹输出）：未生效返回 None。"""
        if self.state.comp_direction is None:
            return None
        return {
            "code": ("G43" if self.state.comp_direction == "plus" else "G44"),
            "direction": self.state.comp_direction,
            "h": self.state.comp_h,
            "offset_mm": round6(abs(self.state.comp_signed)),
            "signed_offset_mm": round6(self.state.comp_signed),
        }

    def _length_event_out(self, ev: dict, tip_z, off) -> dict:
        """补偿事件（供轨迹 length_compensation 条目与报告 length_comp 节）。"""
        signed = ev["signed_offset_mm"]
        spindle_z = None
        if tip_z is not None and off is not None:
            spindle_z = round6(tip_z + off[2] + signed)
        out = {
            "line_no": ev["line_no"],
            "source_line": ev["source_line"],
            "code": ev["code"],
            "direction": ev["direction"],
            "h": ev["h"],
            "offset_mm": ev["offset_mm"],
            "signed_offset_mm": signed,
            "wcs": ev.get("wcs"),
            "tip_z_workpiece_mm": round6(tip_z),
            "spindle_z_machine_mm": spindle_z,
            "tip_z_recomputed": ev.get("tip_z_recomputed", False),
        }
        if ev.get("cancels_h") is not None:
            out["cancels_h"] = ev["cancels_h"]
        return out

    def _spindle_point(self, p, off=None, signed: float | None = None):
        """刀尖工件坐标 -> 主轴基准点机床坐标（工件偏置 + Z 向刀长补偿）。"""
        off = self._current_offset() if off is None else off
        if off is None:
            return [None, None, None]
        if signed is None:
            signed = self.state.comp_signed
        return [p[0] + off[0] if p[0] is not None else None,
                p[1] + off[1] if p[1] is not None else None,
                p[2] + off[2] + signed if p[2] is not None else None]

    def _spindle_out(self, p, off, signed: float | None = None):
        """轨迹输出用主轴基准点机床坐标；工件偏置未知时整体为 None。"""
        if off is None:
            return None
        if signed is None:
            signed = self.state.comp_signed
        out = []
        for i in range(3):
            if p[i] is None:
                out.append(None)
            elif i == 2:
                out.append(round6(p[i] + off[i] + signed))
            else:
                out.append(round6(p[i] + off[i]))
        return out

    def _grow_bbox(self, pts, machine: bool, comps=None):
        if machine:
            off = self._current_offset()
            if off is None:
                return
            wbox = self.wcs_mbbox.setdefault(
                self.state.wcs, [[math.inf] * 3, [-math.inf] * 3])
            for k, p in enumerate(pts):
                signed = (comps[k] if comps is not None
                          and k < len(comps) else None)
                mp = self._spindle_point(p, off, signed)
                for i, v in enumerate(mp):
                    if v is None:
                        continue
                    if v < self.mbmin[i]:
                        self.mbmin[i] = v
                    if v > self.mbmax[i]:
                        self.mbmax[i] = v
                    if v < wbox[0][i]:
                        wbox[0][i] = v
                    if v > wbox[1][i]:
                        wbox[1][i] = v
            return
        for p in pts:
            for i, v in enumerate(p):
                if v is None:
                    continue
                if v < self.bmin[i]:
                    self.bmin[i] = v
                if v > self.bmax[i]:
                    self.bmax[i] = v

    def _bounds_violations(self, pts, comps=None) -> list[dict]:
        c = self.cfg
        limits = (("X", c.x_min, c.x_max), ("Y", c.y_min, c.y_max),
                  ("Z", c.z_min, c.z_max))
        worst: dict[str, dict] = {}
        off = self._current_offset()
        for k, p in enumerate(pts):
            signed = (comps[k] if comps is not None
                      and k < len(comps) else None)
            mp = self._spindle_point(p, off, signed)
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
    def _collect_unsupported(pl: ParsedLine, g_words=None, m_words=None,
                             package_mode: bool = False) -> list[str]:
        """列出本行全部未支持指令（保持行内出现顺序，去重）。

        用于正常段阻断；词法残缺时也调用，以便把残缺片段中恢复出的
        合法词（如 `G55 X-` 中的 G55）一并显式列出。
        程序包模式下 O/M98/M99/M2/M30 是程序流指令（由展开器解释），
        不再列为未支持指令。
        """
        g_words = g_words if g_words is not None else pl.g_words
        m_words = m_words if m_words is not None else pl.m_words
        allowed = (PACKAGE_ALLOWED_LETTERS if package_mode
                   else ALLOWED_LETTERS)
        out: list[str] = []
        for w in pl.words:
            if w.letter == "G":
                tok = "G" + fmt_num(w.value)
                if g_code_key(w) not in SUPPORTED_G and tok not in out:
                    out.append(tok)
            elif w.letter == "M":
                key = g_code_key(w)
                if (key not in SUPPORTED_M
                        and not (package_mode and key in PACKAGE_FLOW_M)
                        and ("M" + fmt_num(w.value)) not in out):
                    out.append("M" + fmt_num(w.value))
            elif w.letter not in allowed:
                tok = f"{w.letter}{fmt_num(w.value)}"
                if tok not in out:
                    out.append(tok)
        return out

    @staticmethod
    def _normalized(applied_g: list[str], m_words: list[Word],
                    motion_g: str | None,
                    coord_words: list[tuple[str, float]],
                    f_val: float | None, s_val: float | None,
                    motion_is_modal: bool = False,
                    comp_token: str | None = None,
                    bare_h: str = "",
                    crc_token: str | None = None,
                    bare_d: str = "") -> str:
        """G(单位/模式/平面/WCS/刀补/运动) -> M -> XYZIJKR -> F S 的规范顺序。"""
        out: list[str] = []
        unit_g = next((g for g in applied_g if g in SETTING_G_UNIT), None)
        mode_g = next((g for g in applied_g if g in SETTING_G_MODE), None)
        plane_g = next((g for g in applied_g if g in PLANE_G), None)
        wcs_g = next((g for g in applied_g if g in WCS_G), None)
        if unit_g:
            out.append(f"G{unit_g}")
        if mode_g:
            out.append(f"G{mode_g}")
        if plane_g:
            out.append(f"G{plane_g}")
        if wcs_g:
            out.append(f"G{wcs_g}")
        if comp_token:
            out.append(comp_token)
        if crc_token:
            out.append(crc_token)
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
        text = " ".join(out)
        notes = []
        if bare_h:
            notes.append(f"{bare_h}(无 G43/G44，不生效)")
        if bare_d:
            notes.append(f"{bare_d}(无 G41/G42，不生效)")
        if notes:
            note = "  ".join(notes)
            text = f"{text}  {note}" if text else note
        return text

    # -- 主流程 ------------------------------------------------------------

    def run(self, text: str) -> dict:
        lines = parse_program(text)
        total = max(len(lines), 1)
        for pl in lines:
            self._process_line(pl)
            if self.progress:
                self.progress(min(99, int(pl.line_no / total * 100)))
        self._cutter_finalize_eof()
        if self.progress:
            self.progress(100)
        return self._build_report(len(lines))

    def run_blocks(self, blocks: list) -> dict:
        """程序包模式：按展开块顺序执行分析（块自带来源/调用栈）。"""
        total = max(len(blocks), 1)
        for n, block in enumerate(blocks, start=1):
            self.current_block = block
            self._process_line(block.line)
            if self.progress and (n % 2000 == 0 or n == total):
                self.progress(min(99, int(n / total * 100)))
        self.current_block = None
        self._cutter_finalize_eof()
        if self.progress:
            self.progress(100)
        physical = sum(1 for b in blocks if not b.line.is_blank)
        return self._build_report(physical)

    def _process_line(self, pl: ParsedLine):
        self._snap_in = self._snapshot()
        # 圆弧无解回滚时需要原样恢复固定循环定义（snapshot 只含可读副本）
        self._snap_in_cycle = (self.state.cycle.clone()
                               if self.state.cycle is not None else None)
        self._snap_in_return = self.state.pending_return
        self._snap_in_return_line = self.state.pending_return_line
        self._snap_in_return_source = self.state.pending_return_source
        self._snap_in_return_default = self.state.pending_return_default
        self._line_comp = None
        self._line_cutter = None
        self._snap_in_line_no = pl.line_no
        # 半径补偿回滚所需的行前状态（待连接段/路径累计/扫掠包围盒）
        self._snap_in_cutter_pending = self._cutter_pending
        self._snap_in_cutter_path = copy.deepcopy(self.cutter_path)
        self._snap_in_cutter_bmin = list(self.cutter_swept_bmin)
        self._snap_in_cutter_bmax = list(self.cutter_swept_bmax)
        self._snap_in_cutter_mbmin = list(self.cutter_swept_mbmin)
        self._snap_in_cutter_mbmax = list(self.cutter_swept_mbmax)
        self._snap_in_cutter_wcs_known = self.cutter_swept_wcs_known
        self._snap_in_prev_seg = (self._cutter_pending["segment"]
                                  if self._cutter_pending is not None else None)

        if pl.is_blank:
            self.blank_count += 1
            entry = {
                "line_no": pl.line_no,
                "source_line": pl.source,
                "normalized": "",
                "type": "blank_or_comment",
                "executed": False,
                "comments": pl.comments,
                "state_in": self._snap_in,
                "state_out": self._snap_in,
            }
            self._annotate_entry(entry, self.current_block)
            self.entries.append(entry)
            return

        # 程序包模式：程序流指令（O/M98/M99/M2/M30）由展开器解释
        if self.package_mode:
            flow = self._package_flow_code(pl)
            if flow is not None:
                self._process_package_flow(pl, flow)
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
            unsupported = self._collect_unsupported(
                pl, package_mode=self.package_mode)
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
        unsupported = self._collect_unsupported(
            pl, g_words, m_words, package_mode=self.package_mode)
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
        # 半径补偿进行中的平面切换必须在应用平面之前阻断（连同本行全部
        # 模态改动一起回滚）。
        pre_keys = [g_code_key(w) for w in g_words]
        line_plane_pre = next((k for k in reversed(pre_keys)
                               if k in PLANE_G), None)
        if (line_plane_pre is not None
                and self.state.cutter_phase in ("pending_in", "pending_out",
                                                "active")):
            issue_indexes = []
            issue_indexes.append(self._issue(
                "CUTTER_COMP_DISCONTINUOUS", pl,
                f"半径补偿进行中（{self.state.cutter_phase}，"
                f"{self.state.cutter_plane}）不能切换到 "
                f"{PLANE_G[line_plane_pre]}；须先在原平面用 G40 经非零 G1 "
                "退出；该段阻断，补偿状态不变",
                {"reason": "plane_change",
                 "plane": PLANE_G[line_plane_pre],
                 "crc_plane": self.state.cutter_plane,
                 "phase": self.state.cutter_phase,
                 "d": self.state.cutter_d}))
            self._rollback_to(self._snap_in)
            normalized = self._blocked_normalized(pl)
            self._finish_line(pl, "blocked", normalized, executed=False,
                              block_reason="cutter_comp",
                              issue_indexes=issue_indexes)
            self.blocked_count += 1
            return

        applied_g: list[str] = []
        last_unit = last_mode = last_plane = last_wcs = None
        for w in g_words:
            key = g_code_key(w)
            if key in SETTING_G_UNIT:
                self.state.unit = SETTING_G_UNIT[key]
                last_unit = key
            elif key in SETTING_G_MODE:
                self.state.distance_mode = SETTING_G_MODE[key]
                last_mode = key
            elif key in PLANE_G:
                self.state.plane = PLANE_G[key]
                last_plane = key
            elif key in WCS_G:
                # 换系：机床位置不动，工件坐标按新偏置重新换算
                self._switch_wcs(WCS_G[key])
                last_wcs = key
        applied_g = [g for g in (last_unit, last_mode, last_plane, last_wcs)
                     if g is not None]

        issue_indexes: list[int] = []

        # 3.5) 刀长补偿 G43/G44/G49（H 只随 G43/G44 生效）。
        # 补偿指令非法（缺 H / H 不存在 / 同段冲突）时整段阻断：连同本行
        # 单位/模式/平面/工件坐标系等模态改动一起回滚，不沿用旧补偿值。
        if not self._apply_length_comp(pl, issue_indexes):
            self._rollback_to(self._snap_in)
            normalized = self._blocked_normalized(pl)
            self._finish_line(pl, "blocked", normalized, executed=False,
                              block_reason="length_comp",
                              issue_indexes=issue_indexes)
            self.blocked_count += 1
            return
        comp_token = (self._line_comp or {}).get("token")

        # 3.6) 刀具半径补偿 G40/G41/G42（D 只随 G41/G42 生效）。
        # 缺 D / D 非法或未登记 / 同段冲突时整段阻断并回滚本行模态改动。
        crc_issue_indexes: list[int] = []
        if not self._apply_cutter_comp(pl, crc_issue_indexes):
            issue_indexes.extend(crc_issue_indexes)
            self._rollback_to(self._snap_in)
            normalized = self._blocked_normalized(pl)
            self._finish_line(pl, "blocked", normalized, executed=False,
                              block_reason="cutter_comp",
                              issue_indexes=issue_indexes)
            self.blocked_count += 1
            return
        crc_token = (self._line_cutter or {}).get("token")

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
        # 半径补偿进行中（待切入/补偿中/待退出）不允许 G0 快速移动：
        # 在改任何运动/循环模态前阻断并回滚。
        if last_group == "0":
            crc_block = []
            if self._cutter_block_motion(pl, "0", crc_block):
                issue_indexes.extend(crc_block)
                self._rollback_to(self._snap_in)
                normalized = self._blocked_normalized(pl)
                self._finish_line(pl, "blocked", normalized, executed=False,
                                  block_reason="cutter_comp",
                                  issue_indexes=issue_indexes)
                self.blocked_count += 1
                return
        # 半径补偿进行中不允许定义/触发固定钻孔循环
        if (line_cycle_key is not None
                and self.state.cutter_phase in ("pending_in", "pending_out",
                                                "active")):
            code = ("CUTTER_APPROACH_INVALID"
                    if self.state.cutter_phase == "pending_in"
                    else "CUTTER_COMP_DISCONTINUOUS")
            basis = ("半径补偿待切入状态不允许固定循环，切入段必须是非零"
                     "平面内 G1" if self.state.cutter_phase == "pending_in"
                     else "半径补偿进行中不允许固定钻孔循环（会使偏置轨迹"
                          "不连续）；该段阻断，补偿轮廓断开")
            issue_indexes.append(self._issue(
                code, pl, basis,
                {"reason": "canned_cycle",
                 "cycle": CYCLE_G[line_cycle_key],
                 "plane": self.state.plane,
                 "phase": self.state.cutter_phase,
                 "d": self.state.cutter_d}))
            self._rollback_to(self._snap_in)
            normalized = self._blocked_normalized(pl)
            self._finish_line(pl, "blocked", normalized, executed=False,
                              block_reason="cutter_comp",
                              issue_indexes=issue_indexes)
            self.blocked_count += 1
            return
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
        ijk_words = {w.letter: w.value for w in pl.words if w.letter in "IJK"}
        r_word = next((w.value for w in pl.words if w.letter == "R"), None)
        q_word = next((w.value for w in pl.words if w.letter == "Q"), None)
        p_word = next((w.value for w in pl.words if w.letter == "P"), None)
        l_word = next((w.value for w in pl.words if w.letter == "L"), None)
        coord_words = [(w.letter, w.value) for w in pl.words
                       if w.letter in ("X", "Y", "Z", "I", "J", "K", "R", "Q",
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

        # 6) 无轴坐标词 => 纯设定段（即使本行写了 G0-G3 也不产生位移）。
        # 例外：G2/G3 带圆心词 I/J/K（或 R）而无 XYZ 终点词时，终点即起点，
        # 按完整弧段解算（圆心编程整圆；R 编程整圆等非法情形仍走阻断），
        # 避免这类整圆绕过预检。
        arc_no_endpoint = (
            not axis_words
            and self.state.motion_mode in ("arc_cw", "arc_ccw")
            and (ijk_words or r_word is not None))
        if not axis_words and not arc_no_endpoint:
            normalized = self._normalized(
                applied_g, m_words, line_motion_key, coord_words, f_raw, s_raw,
                comp_token=comp_token,
                crc_token=crc_token,
                bare_h=(self._line_comp or {}).get("bare_h", ""),
                bare_d=(self._line_cutter or {}).get("bare_d", ""))
            if "80" in keys:
                normalized = (normalized + " " if normalized else "") + "G80(取消循环)"
            if line_return_key is not None:
                normalized = (normalized + " " if normalized else "") + (
                    f"G{line_return_key}(返回{RETURN_CN[RETURN_G[line_return_key]]})")
            crc_ev = (self._line_cutter or {}).get("event")
            if crc_ev is not None and crc_ev["code"] in ("G41", "G42"):
                entry_type = "cutter_compensation"
            elif crc_ev is not None and crc_ev["code"] == "G40":
                entry_type = "cutter_compensation"
            else:
                entry_type = ("length_compensation"
                              if (self._line_comp or {}).get("event")
                              else "setting")
            self._finish_line(pl, entry_type, normalized, executed=True,
                              issue_indexes=issue_indexes)
            return

        # 有轴坐标词但没有任何运动模态 -> 不猜测运动
        if self.state.motion_mode is None:
            normalized = self._normalized(
                applied_g, m_words, None, coord_words, f_raw, s_raw,
                comp_token=comp_token,
                crc_token=crc_token,
                bare_h=(self._line_comp or {}).get("bare_h", ""),
                bare_d=(self._line_cutter or {}).get("bare_d", ""))
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

        # 6) 单位 / 定位模式不明的运动（无 XYZ 的圆心整圆不涉及绝对/增量
        # 歧义，不要求 G90/G91；I/J/K/R 仍需单位换算）
        if self.state.unit_factor() is None:
            issue_indexes.append(self._issue(
                "UNKNOWN_UNITS", pl,
                "运动发生在任何 G20/G21 之前，物理尺寸无法确定；"
                "刀具位置标记为未知，跳过行程/包围盒/长度计算", {}))
        if axis_words and self.state.distance_mode is None:
            issue_indexes.append(self._issue(
                "UNKNOWN_DISTANCE_MODE", pl,
                "出现轴坐标词，但 G90/G91 尚未建立，无法判定绝对/增量定位；"
                "刀具位置标记为未知",
                {"axis_words":
                     [f"{k}{fmt_num(v)}" for k, v in axis_words.items()]}))

        start_pt = self._current_point()
        segment = None
        crc_active_motion = self.state.cutter_phase != "inactive" \
            or (self._line_cutter or {}).get("code") in ("G41", "G42", "G40")
        if (self.state.unit_factor() is None
                or (axis_words and self.state.distance_mode is None)):
            # 位置整体退化为未知，只保留主轴/进给等模态检查
            for letter in axis_words:
                self._set_axis(letter, Axis(None, False))
            self._note_unknown_length()
            if crc_active_motion:
                # 半径补偿中的运动若几何未知：待切入取消启用，其余断开轮廓
                self._cutter_block_unknown(pl, motion_mode, issue_indexes)
                self._rollback_to(self._snap_in)
                normalized = self._blocked_normalized(pl)
                self._finish_line(pl, "blocked", normalized,
                                  executed=False,
                                  block_reason="cutter_comp",
                                  issue_indexes=issue_indexes)
                self.blocked_count += 1
                return
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
                    pl, motion_mode, start_pt, end_pt, ijk_words, r_word,
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
                    self._note_unknown_length()
                segment = {
                    "kind": motion_mode,
                    "start": start_pt,
                    "end": end_pt,
                    "points": [start_pt, end_pt],
                    "length_mm": length,
                }
            segment["_line_no"] = pl.line_no

            # 半径补偿几何：在程序段解算成功后、段级检查/累计之前应用。
            # 失败（切入/退出无效、补偿后圆弧半径非正、相邻段无法连续）
            # 整段阻断并回滚本行全部状态改动，不猜测刀心轨迹。
            if self.state.cutter_phase != "inactive" \
                    or (self._line_cutter or {}).get("code") in (
                            "G41", "G42", "G40"):
                if not self._apply_cutter_to_segment(
                        pl, segment, motion_mode, issue_indexes):
                    self._rollback_to(self._snap_in)
                    normalized = self._blocked_normalized(pl)
                    self._finish_line(pl, "blocked", normalized,
                                      executed=False,
                                      block_reason="cutter_comp",
                                      issue_indexes=issue_indexes)
                    self.blocked_count += 1
                    return

            self._attach_segment_comp(segment)
            self._run_segment_checks(pl, motion_mode, segment, issue_indexes)
            self._accumulate(motion_mode, segment)
            self._accumulate_cutter(pl, motion_mode, segment, issue_indexes)

        normalized = self._normalized(
            applied_g, m_words, motion_g,
            coord_words, f_raw, s_raw, motion_is_modal,
            comp_token=comp_token,
            crc_token=crc_token,
            bare_h=(self._line_comp or {}).get("bare_h", ""),
            bare_d=(self._line_cutter or {}).get("bare_d", ""))
        self._finish_line(
            pl, motion_mode, normalized, executed=True,
            segment=self._segment_out(segment),
            physical_known=segment is not None,
            issue_indexes=issue_indexes)
        self.executed_count += 1

    # -- 程序包：程序流指令 -----------------------------------------------

    def _package_flow_code(self, pl: ParsedLine) -> str | None:
        """识别本行的程序流指令（同段混用在展开预检查阶段已阻断，这里
        取第一个）；O 行总是按子程序号处理（其余内容照常执行）。"""
        from .packages import _o_words, _flow_m_codes
        if _o_words(pl):
            return "O"
        codes = _flow_m_codes(pl)
        if codes:
            # 展开预检查保证同段不混用；防御性取行内第一个
            order = {"98": 0, "99": 1, "2": 2, "30": 2}
            return min(codes, key=lambda c: order.get(c, 9))
        return None

    def _flow_entry(self, pl: ParsedLine, type_: str, normalized: str,
                    details: dict | None = None):
        """登记一条程序流轨迹条目（不改变模态、不产生位移）。"""
        snap_out = self._snapshot()
        entry = {
            "line_no": pl.line_no,
            "source_line": pl.source,
            "normalized": normalized,
            "type": type_,
            "executed": True,
            "physical_known": True,
            "comments": pl.comments,
            "state_in": self._snap_in,
            "state_out": snap_out,
        }
        if details:
            entry["flow"] = details
        self._annotate_entry(entry, self.current_block)
        self.entries.append(entry)

    @staticmethod
    def _virtual_line(pl: ParsedLine, drop_letters=(), drop_pred=None):
        """复制一行并丢弃指定词，得到供常规分析的虚拟行（同一来源行）。"""
        new = ParsedLine(
            line_no=pl.line_no, source=pl.source, code_text=pl.code_text,
            comments=list(pl.comments), malformed=list(pl.malformed),
            is_blank=False)
        for w in pl.words:
            if w.letter in drop_letters:
                continue
            if drop_pred is not None and drop_pred(w):
                continue
            new.words.append(w)
        return new

    def _process_package_flow(self, pl: ParsedLine, flow: str):
        if flow == "O":
            from .packages import _o_words
            # O 号行：O 词本身是声明（无模态效果），其余内容照常执行
            ow = _o_words(pl)[0]
            rest = self._virtual_line(pl, drop_letters={"O"})
            if rest.words:
                self._process_line(rest)
                entry = self.entries[-1]
            else:
                self._flow_entry(
                    pl, "subprogram_label",
                    f"O{fmt_num(ow.value)}(子程序号)",
                    {"kind": "label", "o_number": int(ow.value)})
                return
            entry["normalized"] = (
                entry.get("normalized", "")
                + (f"  O{fmt_num(ow.value)}(子程序号)")).strip()
            return

        if flow == "98":
            # M98：剥离 M98 与本行的 P/L（子程序号/重复次数由展开器解释；
            # 预检查已确认它们不与固定循环参数语义混用），其余内容先执行，
            # 再登记调用条目；子程序块随后由展开器内联送入。
            rest = self._virtual_line(
                pl, drop_pred=lambda w: (
                    w.letter in ("P", "L")
                    or (w.letter == "M" and g_code_key(w) == "98")))
            if rest.words:
                self._process_line(rest)
            p_word = next((w for w in pl.words if w.letter == "P"), None)
            l_word = next((w for w in pl.words if w.letter == "L"), None)
            reps = int(l_word.value) if l_word is not None else 1
            norm = "M98 P" + fmt_num(p_word.value) if p_word else "M98"
            if reps != 1:
                norm += f" L{reps}"
            block = self.current_block
            self._flow_entry(
                pl, "subprogram_call", norm,
                {"kind": "call",
                 "target_program": f"O{int(p_word.value)}" if p_word else None,
                 "repeats": reps,
                 "depth": (block.depth + 1 if block is not None else 1)})
            return

        if flow == "99":
            rest = self._virtual_line(
                pl, drop_pred=lambda w: (
                    w.letter == "P"
                    or (w.letter == "M" and g_code_key(w) == "99")))
            if rest.words:
                self._process_line(rest)
            self._flow_entry(pl, "subprogram_return", "M99(返回调用点)",
                             {"kind": "return"})
            return

        # M2 / M30：程序结束，其余内容不执行（实际控制器也是停止）
        code = "M2" if flow == "2" else "M30"
        self._flow_entry(pl, "program_end", f"{code}(程序结束)",
                         {"kind": "end", "code": code})

    def _rollback_to(self, snapshot: dict):
        """圆弧无解时把状态恢复到进入本行前的快照。"""
        def ax(d):
            return Axis(d["value_mm"], d["known"])

        lc = snapshot.get("tool_length_compensation", {})
        rc = snapshot.get("tool_radius_compensation", {})
        s = State(
            unit=snapshot["unit"],
            distance_mode=snapshot["distance_mode"],
            wcs=snapshot["wcs"],
            motion_mode=snapshot["motion_mode"],
            plane=snapshot["plane"],
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
            comp_direction=lc.get("direction"),
            comp_h=lc.get("h"),
            comp_signed=lc.get("signed_offset_mm") or 0.0,
            comp_apply_line=lc.get("applied_line_no"),
            cutter_phase=rc.get("phase", "inactive"),
            cutter_side=rc.get("side"),
            cutter_d=rc.get("d"),
            cutter_r=(rc.get("radius_mm") or 0.0),
            cutter_plane=rc.get("plane"),
            cutter_apply_line=rc.get("applied_line_no"),
            cutter_cancel_line=rc.get("cancel_line_no"),
        )
        self.state = s
        # 撤销本行登记的刀长补偿事件（圆弧无解回滚 / 补偿段阻断时）
        if self._snap_in_line_no is not None and self.length_events \
                and self.length_events[-1].get(
                    "line_no") == self._snap_in_line_no:
            self.length_events.pop()
        # 撤销本行登记的半径补偿事件（G41/G42/G40 设定行阻断/运动行回滚）
        if self._snap_in_line_no is not None and self.cutter_events \
                and self.cutter_events[-1].get(
                    "line_no") == self._snap_in_line_no:
            self.cutter_events.pop()
        # 恢复本行待连接偏置段（join 失败/圆弧半径非正回滚时）
        self._cutter_pending = self._snap_in_cutter_pending
        # 回滚后刀心路径累计与扫掠包围盒恢复到行前
        self.cutter_path = self._snap_in_cutter_path
        self.cutter_swept_bmin = list(self._snap_in_cutter_bmin)
        self.cutter_swept_bmax = list(self._snap_in_cutter_bmax)
        self.cutter_swept_mbmin = list(self._snap_in_cutter_mbmin)
        self.cutter_swept_mbmax = list(self._snap_in_cutter_mbmax)
        self.cutter_swept_wcs_known = self._snap_in_cutter_wcs_known
        # 上一已完成段在阻断行之前，其上的 junction/connector 标注也要撤销
        if self._snap_in_prev_seg is not None:
            prev = self._snap_in_prev_seg
            prev_cc = prev.get("cutter_compensation")
            if prev_cc is not None:
                prev_cc["junction"] = None
            prev.pop("_cutter_connectors", None)

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

        # 阻断条件六：固定循环只允许在 G17(XY) 平面展开
        if self.state.plane != "G17":
            block_codes.append("CYCLE_PLANE_NOT_G17")
            issue_indexes.append(self._issue(
                "CYCLE_PLANE_NOT_G17", pl,
                f"固定循环 {cd.cycle} 只允许在 G17(XY) 平面展开，当前平面为 "
                f"{self.state.plane}；本行对应孔全部阻断（循环模态仍登记，"
                "G17 恢复后后续孔位可正常触发）",
                {"cycle": cd.cycle, "plane": self.state.plane,
                 "definition_line_no": cd.def_line_no}, normalized))

        g = self._group_for(cd)
        g["parameters"] = cd.params_out()
        if not math.isnan(cd.initial_z):
            g["initial_plane_z_mm"] = round6(cd.initial_z)

        blocked = trigger and (
            bool(block_codes) or unknown_unit or unknown_mode)

        # 阻断孔不产生位移：若本行刀长补偿改变（补偿-only 重算过刀尖 Z），
        # 撤销该重算——补偿模态仍按本行 G43/G44/G49 生效，但主轴基准点
        # 与刀尖工件坐标都保持在行前位置。
        lc = self._line_comp or {}
        if blocked and lc.get("tip_adjusted"):
            self.state.z = Axis(
                self.state.z.value - lc["signed_old"] + lc["signed_new"], True)
            lc["tip_adjusted"] = False

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
            wcs_off = self._current_offset()
            comp_signed = self.state.comp_signed
            hole = {
                "hole_no": no,
                "cycle": cd.cycle,
                "repeat_index": k + 1,
                "trigger_line_no": pl.line_no,
                "trigger_source_line": pl.source,
                "definition_line_no": cd.def_line_no,
                "wcs": self.state.wcs,
                "h": self.state.comp_h,
                "tool_compensation": self._comp_out(),
                "x_mm": round6(tgt_xy[0]) if not blocked else None,
                "y_mm": round6(tgt_xy[1]) if not blocked else None,
                "machine_x_mm": (round6(tgt_xy[0] + wcs_off[0])
                                 if not blocked and wcs_off is not None
                                 and tgt_xy[0] is not None else None),
                "machine_y_mm": (round6(tgt_xy[1] + wcs_off[1])
                                 if not blocked and wcs_off is not None
                                 and tgt_xy[1] is not None else None),
                # 孔底主轴基准点 Z 机床坐标（工件偏置 + 刀长补偿）
                "spindle_bottom_z_machine_mm": (
                    round6(cd.z.value + wcs_off[2] + comp_signed)
                    if not blocked and wcs_off is not None else None),
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
            # 展开轨迹并入全局与分坐标系路径长度
            # （行程/包围盒在逐动作检查时已累计）
            self.length_rapid += rapid_len
            self.length_cutting += cut_len
            wst = self._wcs_path_stat()
            if wst is not None:
                wst["rapid"] += rapid_len
                wst["cutting"] += cut_len
            # 按 H 号统计展开路径（钻削工艺）
            hb = self._comp_path_bucket()
            hb["cycle_rapid"] += rapid_len
            hb["cycle_cutting"] += cut_len
            hb["drill_depth"] += depth_sum

        # 循环段与每个展开动作都记录坐标系、偏置、刀长补偿 H 与
        # 主轴基准点机床坐标（Z 已叠加刀长补偿）
        wcs_off = self._current_offset()
        comp_signed = self.state.comp_signed
        for mv in all_moves:
            mv["h"] = self.state.comp_h
            mv["tool_compensation"] = self._comp_out()
            mv["start_machine_mm"] = self._spindle_out(
                mv["start_mm"], wcs_off, comp_signed)
            mv["end_machine_mm"] = self._spindle_out(
                mv["end_mm"], wcs_off, comp_signed)
        snap_z = self._snap_in["z"]["value_mm"]
        segment = {
            "kind": "canned_cycle",
            "cycle": cd.cycle,
            "wcs": self.state.wcs,
            "wcs_configured": wcs_off is not None,
            "offset_mm": self._offset_out(wcs_off),
            "h": self.state.comp_h,
            "tool_compensation": self._comp_out(),
            "comp_start_signed_mm": (self._line_comp or {}).get(
                "signed_old", comp_signed),
            "comp_end_signed_mm": comp_signed,
            "hole_nos": hole_nos,
            "start_mm": ([round6(v) for v in
                          (start_xy[0], start_xy[1], snap_z)]
                         if start_xy[0] is not None
                         and start_xy[1] is not None
                         and snap_z is not None else None),
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
        c = self.state.comp_signed
        seg = {
            "kind": kind,
            "start": pts[0], "end": pts[1], "points": pts,
            "length_mm": mv["length_mm"],
            # 循环展开期间刀长补偿恒定（触发行起点也已按新补偿重算）
            "comp_start_signed_mm": c, "comp_end_signed_mm": c,
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
                f"固定循环孔间快速定位到达/经过刀尖 Z={fmt_num(z_ref)} mm"
                f"（刀尖工件坐标，孔序 {hole_no}），低于安全 Z "
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
            "CYCLE_PLANE_NOT_G17": "固定循环仅允许在 G17(XY) 平面展开",
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
        """循环行的规范化文本（含刀长补偿、循环代号、返回平面、本行词与
        继承标注）。"""
        out: list[str] = []
        comp_token = (self._line_comp or {}).get("token")
        if comp_token:
            out.append(comp_token)
        if line_cycle_key is not None:
            out.append(CYCLE_G[line_cycle_key])
        ret_g = "G98" if cd.return_mode == "initial" else "G99"
        if not cd.return_mode_default:
            out.append(ret_g)
        for w in pl.words:
            if w.letter in ("N", "G", "H"):
                continue
            if w.letter in ("X", "Y", "Z", "R", "Q", "P", "L", "F", "S"):
                out.append(f"{w.letter}{fmt_num(w.value)}")
        # 同行但不随补偿生效的 H（G49 行或无 G43/G44）
        bare_h = (self._line_comp or {}).get("bare_h", "")
        if bare_h:
            out.append(f"{bare_h}(无 G43/G44，不生效)")
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

    # -- 圆弧（G17/G18/G19 + 垂直轴联动螺旋） -------------------------------

    def _build_arc(self, pl, motion_mode, start_pt, end_pt, ijk_words, r_word,
                   issue_indexes):
        plane = self.state.plane
        ui, vi, pi, uw, vw, plane_label = PLANE_SPEC[plane]
        u_name = PLANE_AXIS_NAMES[ui]
        v_name = PLANE_AXIS_NAMES[vi]
        perp_name = PLANE_AXIS_NAMES[pi]
        factor = self.state.unit_factor() or 1.0
        blocked_norm = self._blocked_normalized(pl)

        def fail(basis, details):
            issue_indexes.append(self._issue(
                "ARC_NO_SOLUTION", pl, basis, details, blocked_norm))
            return None

        center_words = {w: v for w, v in ijk_words.items()}
        # 阻断一：圆心参数混用（I/J/K 与 R 同时出现）
        if r_word is not None and center_words:
            mixed = sorted(center_words) + ["R"]
            return fail(
                f"圆心参数混用：{'/'.join(mixed)} 同时出现在 {plane_label} "
                f"圆弧段（圆心编程与 R 编程二选一）；整段不执行，"
                "本行模态改动全部回滚",
                {"reason": "mixed_center_params",
                 "plane": plane,
                 "mixed_words": mixed,
                 "start_mm": [round6(v) for v in start_pt],
                 "end_mm": [round6(v) for v in end_pt]})
        # 阻断二：圆心词不属于当前平面
        wrong = [w for w in sorted(center_words) if w not in (uw, vw)]
        if wrong:
            return fail(
                f"圆心参数 {'/'.join(wrong)} 不属于当前平面 {plane_label}"
                f"（该平面接受 {'/'.join(plane_center_words(plane))} 或 R）；"
                "整段不执行，本行模态改动全部回滚",
                {"reason": "center_word_not_in_plane",
                 "plane": plane,
                 "invalid_words": wrong,
                 "plane_center_words": plane_center_words(plane),
                 "start_mm": [round6(v) for v in start_pt],
                 "end_mm": [round6(v) for v in end_pt]})

        # 平面内起终点必须已知（垂直轴允许未知，退化为长度未知）
        u0, v0, u1, v1 = (start_pt[ui], start_pt[vi],
                          end_pt[ui], end_pt[vi])
        if any(v is None for v in (u0, v0, u1, v1)):
            return fail(
                f"圆弧起点或终点的 {u_name}/{v_name} 坐标未知"
                "（此前位置未建立），无法解算几何；整段不执行并回滚本行状态",
                {"reason": "position_unknown",
                 "plane": plane,
                 "start_mm": [round6(v) for v in start_pt],
                 "end_mm": [round6(v) for v in end_pt]})

        u_off = center_words.get(uw)
        v_off = center_words.get(vw)
        u_off = u_off * factor if u_off is not None else None
        v_off = v_off * factor if v_off is not None else None
        r_mm = r_word * factor if r_word is not None else None
        try:
            sol = solve_arc(
                    (u0, v0), (u1, v1),
                    clockwise=(motion_mode == "arc_cw"),
                    i=u_off, j=v_off, r_word=r_mm, words=(uw, vw))
        except ArcError as e:
            return fail(
                f"圆弧几何无解：{e}；整段不执行，本行模态改动全部回滚",
                {"reason": str(e),
                 "plane": plane,
                 "start_mm": [round6(v) for v in start_pt],
                 "end_mm": [round6(v) for v in end_pt],
                 "i_mm": round6(ijk_words.get("I") and
                                ijk_words["I"] * factor),
                 "j_mm": round6(ijk_words.get("J") and
                                ijk_words["J"] * factor),
                 "k_mm": round6(ijk_words.get("K") and
                                ijk_words["K"] * factor),
                 "r_mm": round6(r_mm)})

        # 垂直当前平面的联动轴：随扫角线性插补（螺旋）
        p0, p1 = start_pt[pi], end_pt[pi]
        cu, cv = sol["center"]
        sweep = sol["sweep"]
        radius = sol["radius"]

        def to_3d(su, sv, frac):
            pt = [None, None, None]
            pt[ui] = su
            pt[vi] = sv
            if p0 is not None and p1 is not None:
                pt[pi] = p0 + (p1 - p0) * frac
            return pt

        n = len(sol["samples"])
        points = []
        for k, (su, sv) in enumerate(sol["samples"], start=1):
            points.append(to_3d(su, sv, k / n))
        points[-1] = list(end_pt)

        # 真实弧线的极值点（端点 + 扫过的象限角），用于精确包围盒/行程检查
        a0 = _angle(cu, cv, u0, v0)
        check_points = [list(start_pt)]
        for a in arc_extreme_angles(a0, sweep)[1:]:
            frac = 0.0 if abs(sweep) < GEOM_TOL else (a - a0) / sweep
            check_points.append(to_3d(cu + radius * math.cos(a),
                                      cv + radius * math.sin(a), frac))
        check_points.append(list(end_pt))  # 终点精确（重复无妨）

        arc_len = sol["length_xy"]
        perp_change = (p1 - p0) if (p0 is not None and p1 is not None) else None
        length = None
        if perp_change is not None:
            length = math.sqrt(arc_len ** 2 + perp_change ** 2)
        else:
            self._note_unknown_length()
        center_3d = [None, None, None]
        center_3d[ui] = round(cu, 6)
        center_3d[vi] = round(cv, 6)
        if p0 is not None:
            center_3d[pi] = round(p0, 6)  # 螺旋轴线过起点高度，仅供定位
        full_circle = abs(abs(sweep) - 2 * math.pi) < 1e-6
        return {
            "kind": motion_mode,
            "start": start_pt,
            "end": end_pt,
            "points": [list(start_pt)] + points,
            "check_points": check_points,
            "length_mm": length,
            "arc": {
                "plane": plane_label,
                "plane_code": plane,
                "direction": "CW" if motion_mode == "arc_cw" else "CCW",
                "programming": ("/".join(plane_center_words(plane))
                                if center_words else "R"),
                "center_mm": [round(cu, 6), round(cv, 6)],
                "center_axes": [u_name, v_name],
                "center_3d_mm": center_3d,
                "radius_mm": round(radius, 6),
                "sweep_deg": round(math.degrees(sweep), 6),
                "full_circle": full_circle,
                "helical": (perp_change is not None
                            and abs(perp_change) > MM_EPS),
                "perp_axis": perp_name,
                "perp_change_mm": (round(perp_change, 6)
                                   if perp_change is not None else None),
                "arc_length_mm": round(arc_len, 6),
            },
        }

    # -- 段级检查 ----------------------------------------------------------

    def _attach_segment_comp(self, segment: dict):
        """给普通运动段逐点挂刀长补偿（mm 代数值）：起点用进入本行前的
        旧补偿，其余点用本行新补偿（补偿不改变时二者相同）。"""
        lc = self._line_comp or {}
        old = lc.get("signed_old", self.state.comp_signed)
        new = lc.get("signed_new", self.state.comp_signed)
        comps = []
        for k in range(len(segment["points"])):
            comps.append(old if k == 0 else new)
        segment["comp_signed_mm"] = comps
        segment["comp_start_signed_mm"] = old
        segment["comp_end_signed_mm"] = new

    def _run_segment_checks(self, pl, motion_mode, segment, issue_indexes,
                            cycle_context: bool = False,
                            line_dedupe: bool = False):
        # 圆弧段用真实弧线的精确极值点做行程/包围盒；其余段用轨迹点
        points = segment.get("check_points") or segment["points"]
        # 逐点刀长补偿（代数值 mm）：行程/机床包围盒按主轴基准点判定
        if "comp_signed_mm" in segment:
            src = segment["comp_signed_mm"]
            if segment.get("check_points") is not None and \
                    len(src) == len(segment["points"]):
                # 圆弧：极值点落在真实弧线上，按其在采样序列中的位置取补偿
                comps = []
                src_pts = segment["points"]
                for p in points:
                    if p is src_pts[0]:
                        comps.append(src[0])
                    else:
                        comps.append(src[-1])
            else:
                comps = src
        else:
            # 固定循环展开动作：整段补偿恒定
            c = segment.get("comp_end_signed_mm", self.state.comp_signed)
            comps = [c] * len(points)
        # 弧段产生的问题带上平面信息，报告可按 plane=G17/G18/G19 筛选
        arc_plane = (segment.get("arc") or {}).get("plane_code")
        # 半径补偿轮廓段：机床行程按刀具扫掠包围盒（刀心±半径）在
        # _accumulate_cutter 中统一判定，此处不再按编程刀尖轨迹重复检查。
        cutter_cc = segment.get("cutter_compensation")
        skip_travel_bounds = (cutter_cc is not None
                              and cutter_cc.get("continuous", True)
                              and cutter_cc.get("mode") in (
                                  "tangent_engage", "contour",
                                  "tangent_exit"))

        def _details(d):
            if arc_plane is not None:
                return {**d, "plane": arc_plane}
            return d

        # 行程检查需要已配置偏置的工件坐标系
        if self.state.wcs is None:
            self.all_moves_wcs_known = False
            issue_indexes.append(self._issue(
                "UNKNOWN_WCS", pl,
                "运动发生在 G54-G59 建立之前，缺少工件坐标偏置映射，"
                "跳过行程检查",
                _details({"reason": "wcs_not_established"})))
        elif self._current_offset() is None:
            # 引用了未配置偏置的坐标系：不沿用上一坐标系偏置，
            # 相关机床坐标、行程及包围盒结论标为未知
            self.all_moves_wcs_known = False
            issue_indexes.append(self._issue(
                "UNKNOWN_WCS", pl,
                f"坐标系 {self.state.wcs} 未在机床配置 wcs_offsets 中设置"
                "偏置；不沿用上一坐标系偏置，跳过行程检查，本段的机床坐标、"
                "行程与包围盒结论标记为未知",
                _details({"reason": "wcs_not_configured",
                          "wcs": self.state.wcs})))
        else:
            if not skip_travel_bounds:
                for v in self._bounds_violations(points, comps):
                    c = self.cfg
                    bound = {"X": (c.x_min, c.x_max),
                             "Y": (c.y_min, c.y_max),
                             "Z": (c.z_min, c.z_max)}[v["axis"]]
                    comp_note = ""
                    if v["axis"] == "Z" and abs(self.state.comp_signed) > MM_EPS:
                        comp_note = (f"与 {self.state.wcs} 偏置及刀长补偿"
                                     f"H{self.state.comp_h}"
                                     f"（{fmt_num(self.state.comp_signed)} mm）")
                    else:
                        comp_note = f"已叠加 {self.state.wcs} 偏置"
                    issue_indexes.append(self._issue(
                        "OUT_OF_BOUNDS", pl,
                        f"{v['axis']} 轴主轴基准点机床坐标 "
                        f"{fmt_num(v['value_mm'])} mm 越出行程"
                        f"边界 {fmt_num(v['bound_mm'])} mm（超程 "
                        f"{fmt_num(v['overshoot_mm'])} mm；{comp_note}）",
                        _details(v)))
            self._grow_bbox(points, machine=True, comps=comps)

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
                    f"快速移动到达/经过刀尖 Z={fmt_num(z_ref)} mm"
                    f"（刀尖工件坐标），"
                    f"低于安全 Z {fmt_num(self.cfg.safe_z)} mm（低 "
                    f"{fmt_num(self.cfg.safe_z - z_ref)} mm）",
                    _details({"ref_z_mm": round(z_ref, 6),
                              "safe_z_mm": self.cfg.safe_z,
                              "below_mm": round(self.cfg.safe_z - z_ref, 6),
                              "end_below_safe_z": end_below,
                              "horizontal_travel_below_safe_z": horiz_below})))

        if motion_mode in ("linear", "arc_cw", "arc_ccw"):
            if not self.state.spindle_on:
                idx = self._issue(
                    "SPINDLE_NOT_RUNNING", pl,
                    f"{MOTION_CN[motion_mode]}切削发生时主轴处于停止状态"
                    f"（spindle_on=false，最近 S={self.state.spindle_rpm}）",
                    _details({"spindle_on": False,
                              "last_s_rpm": self.state.spindle_rpm}),
                    line_dedupe=line_dedupe)
                issue_indexes.append(idx)
            if not self.state.feed.known:
                idx = self._issue(
                    "FEED_UNSET", pl,
                    f"{MOTION_CN[motion_mode]}切削前未建立有效进给 F"
                    "（单位不明或从未给定）",
                    _details({"feed_known": False}),
                    line_dedupe=line_dedupe)
                issue_indexes.append(idx)

    # -- 累计与输出 --------------------------------------------------------

    def _wcs_path_stat(self) -> dict | None:
        """当前坐标系的路径统计桶（未建立坐标系时不计）。"""
        w = self.state.wcs
        if w is None:
            return None
        return self.wcs_path.setdefault(
            w, {"rapid": 0.0, "cutting": 0.0, "unknown_segments": 0})

    def _note_unknown_length(self):
        self.unknown_length_segments += 1
        st = self._wcs_path_stat()
        if st is not None:
            st["unknown_segments"] += 1

    def _comp_path_bucket(self) -> dict:
        """当前生效刀长补偿 H 号的路径统计桶（未补偿归到 "__none__"）。"""
        key = self.state.comp_h if self.state.comp_h is not None else "__none__"
        return self.comp_path.setdefault(key, {
            "rapid": 0.0, "cutting": 0.0,
            "cycle_rapid": 0.0, "cycle_cutting": 0.0,
            "drill_depth": 0.0})

    def _accumulate(self, motion_mode, segment):
        length = segment["length_mm"]
        st = self._wcs_path_stat()
        hb = self._comp_path_bucket()
        if length is not None:
            if motion_mode == "rapid":
                self.length_rapid += length
                hb["rapid"] += length
                if st is not None:
                    st["rapid"] += length
            else:
                self.length_cutting += length
                hb["cutting"] += length
                if st is not None:
                    st["cutting"] += length
        arc = segment.get("arc")
        if arc is not None:
            st = self.arc_stats[arc["plane_code"]]
            st["count"] += 1
            st["arc_length_mm"] += arc["arc_length_mm"]
            if length is not None:
                st["length_3d_mm"] += length
            if arc["helical"]:
                st["helical_count"] += 1
            if arc["full_circle"]:
                st["full_circle_count"] += 1

    def _current_point(self):
        def g(a: Axis):
            return a.value if a.known else None
        return [g(self.state.x), g(self.state.y), g(self.state.z)]

    def _set_axis(self, letter: str, ax: Axis):
        setattr(self.state, letter.lower(), ax)

    def _finish_line(self, pl, type_, normalized, executed, segment=None,
                     physical_known=True, block_reason=None,
                     issue_indexes=None):
        snap_out = self._snapshot()
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
        # 刀长补偿段（G43/G44/G49）：记录补偿事件与刀尖/主轴基准点坐标
        lc = self._line_comp or {}
        ev = lc.get("event")
        if ev is not None:
            off = self._current_offset()
            tip_z = self.state.z.value if self.state.z.known else None
            entry["tool_length_event"] = self._length_event_out(ev, tip_z, off)
        # 半径补偿段（G40/G41/G42 纯设定行）：记录事件
        crc = self._line_cutter or {}
        crc_ev = crc.get("event")
        if crc_ev is not None:
            entry["tool_radius_event"] = {
                "line_no": crc_ev["line_no"],
                "source_line": crc_ev["source_line"],
                "code": crc_ev["code"],
                "side": crc_ev.get("side"),
                "d": crc_ev.get("d"),
                "radius_mm": crc_ev.get("radius_mm"),
                "plane": crc_ev.get("plane"),
                "wcs": crc_ev.get("wcs"),
                **({"cancels_d": crc_ev["cancels_d"]}
                   if "cancels_d" in crc_ev else {}),
            }
        if issue_indexes:
            entry["issue_codes"] = [self.issues[i].code for i in issue_indexes]
        self._annotate_entry(entry, self.current_block)
        self.entries.append(entry)
        for i in issue_indexes or []:
            iss = self.issues[i]
            if not iss.normalized:
                iss.normalized = normalized
            iss.state_out = snap_out

    def _segment_out(self, segment):
        if segment is None:
            return None
        off = self._current_offset()
        comps = segment.get("comp_signed_mm")
        s0 = segment.get("comp_start_signed_mm", self.state.comp_signed)
        s1 = segment.get("comp_end_signed_mm", self.state.comp_signed)

        def spindle(p, signed):
            if off is None or p is None:
                return None
            return self._spindle_out(p, off, signed)

        out = {
            "kind": segment["kind"],
            "wcs": self.state.wcs,
            "wcs_configured": off is not None,
            "offset_mm": self._offset_out(off),
            "h": segment.get("h", self.state.comp_h),
            "tool_compensation": segment.get("tool_compensation",
                                             self._comp_out()),
            "comp_start_signed_mm": round6(s0),
            "comp_end_signed_mm": round6(s1),
            "start_mm": [round6(v) for v in segment["start"]],
            "end_mm": [round6(v) for v in segment["end"]],
            # 主轴基准点机床坐标（Z 叠加刀长补偿；行程按它判定）
            "start_machine_mm": self._spindle_out(segment["start"], off, s0),
            "end_machine_mm": self._spindle_out(segment["end"], off, s1),
            "length_mm": round6(segment["length_mm"]),
            "points_mm": [[round6(c) for c in p] for p in segment["points"]],
            "points_machine_mm": (
                [self._spindle_out(p, off,
                                   comps[k] if comps is not None
                                   and k < len(comps) else s1)
                 for k, p in enumerate(segment["points"])]
                if off is not None else None),
        }
        if "arc" in segment:
            out["arc"] = segment["arc"]
        cc = segment.get("cutter_compensation")
        if cc is not None:
            out["cutter_compensation"] = self._cutter_comp_out(segment, cc,
                                                                off)
        return out

    def _cutter_comp_out(self, segment, cc: dict, off) -> dict:
        """段上的半径补偿输出：刀心轨迹、连接方式、刀具扫掠包围盒。"""
        center = cc.get("center_points_mm")
        r_d = cc.get("radius_mm") or 0.0
        ui, vi, pi = (PLANE_SPEC[cc["plane"]] if cc.get("plane") in PLANE_SPEC
                      else PLANE_SPEC[self.state.plane])[:3]

        def spindle(p):
            if off is None or p is None:
                return None
            return self._spindle_out(p, off, self.state.comp_signed)

        # 外角补弧（刀心链节）展开到输出
        connector_out = None
        all_center = list(center) if center else []
        for conn in segment.get("_cutter_connectors", []):
            pts3 = self._connector_3d(conn, pi, segment)
            cp = [[round6(c) for c in p] for p in pts3]
            all_center.extend(cp)
            connector_out = {
                "kind": "outer_arc",
                "center_mm": [round6(conn.center[0]),
                              round6(conn.center[1])],
                "radius_mm": round6(conn.radius),
                "sweep_deg": round(math.degrees(conn.sweep), 6),
                "center_points_mm": cp,
            }
        swept_bbox = None
        if all_center:
            bmin = [math.inf] * 3
            bmax = [-math.inf] * 3
            for p in all_center:
                for i, v in enumerate(p):
                    if v is None:
                        continue
                    rad = r_d if i in (ui, vi) else 0.0
                    bmin[i] = min(bmin[i], v - rad)
                    bmax[i] = max(bmax[i], v + rad)
            if not any(math.isinf(v) for v in bmin):
                swept_bbox = {
                    "x_mm": [round(bmin[0], 6), round(bmax[0], 6)],
                    "y_mm": [round(bmin[1], 6), round(bmax[1], 6)],
                    "z_mm": [round(bmin[2], 6), round(bmax[2], 6)],
                    "size_mm": [round(bmax[0] - bmin[0], 6),
                                round(bmax[1] - bmin[1], 6),
                                round(bmax[2] - bmin[2], 6)]}
        out = {
            "active": cc.get("active", True),
            "code": cc.get("code"),
            "side": cc.get("side"),
            "d": cc.get("d"),
            "radius_mm": cc.get("radius_mm"),
            "plane": cc.get("plane"),
            "mode": cc.get("mode"),
            "continuous": cc.get("continuous", True),
            "ramp": cc.get("ramp"),
            "junction": cc.get("junction"),
            "connector": connector_out,
            "center_path_mm": [[round6(c) for c in p]
                               for p in (center or [])],
            "center_path_machine_mm": (
                [spindle(p) for p in center] if off is not None else None),
            "swept_bbox_program_mm": swept_bbox,
        }
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

    def _arcs_out(self) -> dict:
        """分平面弧段统计（仅已执行弧段；阻断弧见 blocked_count）。"""
        by_plane = {}
        total = {"count": 0, "arc_length_mm": 0.0, "length_3d_mm": 0.0,
                 "helical_count": 0, "full_circle_count": 0}
        for plane, st in self.arc_stats.items():
            row = {
                "count": st["count"],
                "arc_length_mm": round(st["arc_length_mm"], 6),
                "length_3d_mm": round(st["length_3d_mm"], 6),
                "helical_count": st["helical_count"],
                "full_circle_count": st["full_circle_count"],
            }
            by_plane[plane] = row
            total["count"] += st["count"]
            total["arc_length_mm"] += st["arc_length_mm"]
            total["length_3d_mm"] += st["length_3d_mm"]
            total["helical_count"] += st["helical_count"]
            total["full_circle_count"] += st["full_circle_count"]
        total["arc_length_mm"] = round(total["arc_length_mm"], 6)
        total["length_3d_mm"] = round(total["length_3d_mm"], 6)
        blocked = sum(1 for i in self.issues if i.code == "ARC_NO_SOLUTION")
        return {"by_plane": by_plane, "total": total,
                "blocked_count": blocked}

    def _wcs_out(self) -> dict:
        """分工件坐标系汇总：偏置、路径长度、机床坐标包围盒与问题数。"""
        issue_counts: dict[str, int] = {}
        for iss in self.issues:
            w = iss.details.get("wcs")
            if w is not None:
                issue_counts[w] = issue_counts.get(w, 0) + 1
        by_wcs = {}
        for w in sorted(self.wcs_used):
            off = self.cfg.offset_for(w)
            path = self.wcs_path.get(
                w, {"rapid": 0.0, "cutting": 0.0, "unknown_segments": 0})
            mb = self.wcs_mbbox.get(w)
            by_wcs[w] = {
                "configured": off is not None,
                "offset_mm": self._offset_out(off),
                "path_length_mm": {
                    "rapid": round(path["rapid"], 6),
                    "cutting": round(path["cutting"], 6),
                    "total": round(path["rapid"] + path["cutting"], 6),
                    "unknown_segments": path["unknown_segments"],
                },
                "machine_bbox_mm": (
                    self._bbox_out(mb[0], mb[1]) if mb is not None else None),
                "issues": issue_counts.get(w, 0),
            }
        return {
            "offsets_mm": {w: dict(off) for w, off
                           in sorted(self.cfg.wcs_offsets.items())},
            "used": sorted(self.wcs_used),
            "by_wcs": by_wcs,
        }

    def _length_comp_out(self) -> dict:
        """刀长补偿汇总：H 寄存器表、G43/G44/G49 事件、按 H 号的路径/
        钻孔/问题汇总，以及主轴基准点 Z 轴行程占用（机床坐标）。"""
        # 每个孔（含阻断孔）归属的 H 号
        holes_by_h: dict = {}
        depth_by_h: dict = {}
        for grp in self.cycle_groups:
            for h in grp["holes"]:
                key = h.get("h") if h.get("h") is not None else "__none__"
                d = holes_by_h.setdefault(key, {"drilled": 0, "blocked": 0})
                if h.get("status") == "drilled":
                    d["drilled"] += 1
                    depth_by_h[key] = depth_by_h.get(key, 0.0) + (
                        h.get("drill_depth_mm") or 0.0)
                else:
                    d["blocked"] += 1
        issue_by_h: dict = {}
        for iss in self.issues:
            h = iss.details.get("h")
            key = h if h is not None else "__none__"
            issue_by_h[key] = issue_by_h.get(key, 0) + 1

        def bucket_for(key):
            b = self.comp_path.get(
                key, {"rapid": 0.0, "cutting": 0.0,
                      "cycle_rapid": 0.0, "cycle_cutting": 0.0,
                      "drill_depth": 0.0})
            hd = holes_by_h.get(key, {"drilled": 0, "blocked": 0})
            return {
                "path_length_mm": {
                    "rapid": round(b["rapid"], 6),
                    "cutting": round(b["cutting"], 6),
                    "total": round(b["rapid"] + b["cutting"], 6),
                    "canned_cycle_rapid": round(b["cycle_rapid"], 6),
                    "canned_cycle_cutting": round(b["cycle_cutting"], 6),
                },
                "holes_drilled": hd["drilled"],
                "holes_blocked": hd["blocked"],
                "total_drill_depth_mm": round(
                    b.get("drill_depth", depth_by_h.get(key, 0.0)), 6),
                "issues": issue_by_h.get(key, 0),
            }

        used_h = sorted(h for h in self.comp_path if isinstance(h, int))
        by_h = {f"H{h}": dict(bucket_for(h), h=h,
                              offset_mm=self.cfg.length_offsets.get(h))
                for h in used_h}
        # G43/G44 事件中出现但无路径累计的 H 也列出
        for ev in self.length_events:
            if ev["h"] is not None and f"H{ev['h']}" not in by_h:
                by_h[f"H{ev['h']}"] = dict(
                    bucket_for(ev["h"]), h=ev["h"],
                    offset_mm=self.cfg.length_offsets.get(ev["h"]))
        no_comp = bucket_for("__none__")
        events = []
        for ev in self.length_events:
            off = self.cfg.offset_for(ev.get("wcs"))
            events.append(self._length_event_out(
                ev, ev.get("tip_z_mm"), off))
        z_travel = None
        if not any(math.isinf(v) for v in self.mbmin + self.mbmax) \
                and self.all_moves_wcs_known:
            z_travel = {
                "spindle_z_machine_mm": [round(self.mbmin[2], 6),
                                         round(self.mbmax[2], 6)],
                "note": "主轴基准点 Z 机床坐标（工件偏置 + 刀长补偿）",
            }
        return {
            "supported": {"G43": "刀长补偿加：主轴基准点 = 刀尖 + H 偏置",
                          "G44": "刀长补偿减：主轴基准点 = 刀尖 - H 偏置",
                          "G49": "取消刀长补偿",
                          "H": "H 号只在与 G43/G44 同段时生效，"
                               "偏置取自机床配置 length_offsets"},
            "offsets_mm": {f"H{h}": self.cfg.length_offsets[h]
                           for h in sorted(self.cfg.length_offsets)},
            "events": events,
            "by_h": by_h,
            "without_compensation": no_comp,
            "spindle_z_travel": z_travel,
            "issues": {
                "LENGTH_COMP_MISSING_H": sum(
                    1 for i in self.issues
                    if i.code == "LENGTH_COMP_MISSING_H"),
                "LENGTH_COMP_H_NOT_FOUND": sum(
                    1 for i in self.issues
                    if i.code == "LENGTH_COMP_H_NOT_FOUND"),
                "LENGTH_COMP_CONFLICT": sum(
                    1 for i in self.issues
                    if i.code == "LENGTH_COMP_CONFLICT"),
            },
        }

    def _cutter_comp_section_out(self) -> dict:
        """半径补偿（G40/G41/G42 + D）汇总：D 半径表、事件流、按 D 汇总、
        刀心路径与刀具扫掠包围盒。"""
        issue_by_d: dict = {}
        for iss in self.issues:
            d = iss.details.get("d")
            if d is not None:
                issue_by_d[d] = issue_by_d.get(d, 0) + 1

        by_d = {}
        for d in sorted(set(self.cutter_path) |
                        {ev["d"] for ev in self.cutter_events
                         if ev.get("d") is not None}):
            if d == "__none__":
                continue
            b = self.cutter_path.get(d, {"segments": 0, "center_length": 0.0,
                                        "engages": 0, "exits": 0})
            by_d[f"D{d}"] = {
                "d": d,
                "radius_mm": self.cfg.radius_offsets.get(d),
                "compensated_segments": b["segments"],
                "engages": b["engages"],
                "exits": b["exits"],
                "center_path_length_mm": round(b["center_length"], 6),
                "issues": issue_by_d.get(d, 0),
            }
        events = []
        for ev in self.cutter_events:
            events.append({
                "line_no": ev["line_no"],
                "source_line": ev["source_line"],
                "code": ev["code"],
                "side": ev.get("side"),
                "d": ev.get("d"),
                "radius_mm": ev.get("radius_mm"),
                "plane": ev.get("plane"),
                "wcs": ev.get("wcs"),
                **({"cancels_d": ev["cancels_d"],
                    "cancels_side": ev.get("cancels_side")}
                   if "cancels_d" in ev else {}),
            })
        return {
            "supported": {
                "G40": "取消刀具半径补偿（须用非零平面内 G1 退出）",
                "G41": "左侧半径补偿（按平面正法向 n=u×v 判定左/右）",
                "G42": "右侧半径补偿",
                "D": "D 号只在与 G41/G42 同段时生效，刀具半径取自机床"
                     "配置 radius_offsets（D 为正整数且已登记）",
            },
            "offsets_mm": {f"D{d}": self.cfg.radius_offsets[d]
                          for d in sorted(self.cfg.radius_offsets)},
            "events": events,
            "by_d": by_d,
            "center_path_total_mm": round(
                sum(b["center_length"]
                    for d, b in self.cutter_path.items()
                    if d != "__none__"), 6),
            "swept_bbox_program_mm": self._bbox_out(
                self.cutter_swept_bmin, self.cutter_swept_bmax),
            "swept_bbox_machine_mm": (
                self._bbox_out(self.cutter_swept_mbmin,
                              self.cutter_swept_mbmax)
                if self.cutter_swept_wcs_known else None),
            "issues": {code: sum(1 for i in self.issues if i.code == code)
                       for code in ("CUTTER_COMP_MISSING_D",
                                    "CUTTER_COMP_D_NOT_FOUND",
                                    "CUTTER_COMP_CONFLICT",
                                    "CUTTER_APPROACH_INVALID",
                                    "CUTTER_EXIT_INVALID",
                                    "CUTTER_ARC_RADIUS",
                                    "CUTTER_COMP_DISCONTINUOUS")},
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
            "final_state": self._snapshot(),
            "drill_cycles": self._drill_cycles_out(),
            "arcs": self._arcs_out(),
            "wcs": self._wcs_out(),
            "length_compensation": self._length_comp_out(),
            "cutter_compensation": self._cutter_comp_section_out(),
            "bbox_program_mm": self._bbox_out(self.bmin, self.bmax),
            "bbox_machine_mm": (
                self._bbox_out(self.mbmin, self.mbmax)
                if self.all_moves_wcs_known else None),
            "cutter_swept_bbox_program_mm": self._bbox_out(
                self.cutter_swept_bmin, self.cutter_swept_bmax),
            "cutter_swept_bbox_machine_mm": (
                self._bbox_out(self.cutter_swept_mbmin,
                              self.cutter_swept_mbmax)
                if self.cutter_swept_wcs_known else None),
            "machine_bbox_note": (
                None if self.all_moves_wcs_known
                else "存在坐标系未建立或未配置偏置的运动，"
                     "无法给出完整机床坐标包围盒"),
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
                "wcs": "支持 G54-G59 模态切换，偏置取自机床配置 wcs_offsets"
                       "（旧字段 offset_x/y/z 归入 G54）；换系时机床位置不动、"
                       "工件坐标按新偏置重新换算；引用未配置偏置的坐标系时"
                       "不沿用上一偏置，相关机床坐标/行程/包围盒结论标为未知；"
                       "安全 Z 按当前工件坐标判定",
                "arc": "G17(XY，默认)/G18(XZ)/G19(YZ) 模态平面；圆心词随平面 "
                       "I/J、I/K、J/K 或 R（R 负=优弧），垂直轴随扫角线性联动"
                       "（螺旋）；圆心词与 R 混用、圆心词不属于当前平面、"
                       "R 编程整圆或起终半径不一致均整段阻断并回滚；"
                       "包围盒/行程按真实弧线极值点计算",
                "block": "含未支持指令或无法解析的程序段整段阻断，"
                         "不改变任何模态",
                "subprogram": ("程序包模式（POST /api/packages）下，"
                               "O/M98 Pn Lk/M99/M2/M30 由静态展开解释："
                               "调用继承模态、M99 返回调用点、重复调用不重置"
                               "状态；展开块保留来源程序、原行、调用栈与重复"
                               "序号。单程序分析不支持这些指令。"),
                "safe_z": "安全 Z 按工件(程序)坐标的刀尖 Z 判定",
                "length_compensation": (
                    "G43 刀长补偿加（主轴基准点=刀尖+H 偏置）、G44 减、"
                    "G49 取消；H 只随 G43/G44 生效，偏置取自机床配置 "
                    "length_offsets（H 为正整数、偏置为数值，否则拒绝保存）；"
                    "同行运动使用新补偿；只有补偿指令时主轴基准点保持不动、"
                    "按新补偿重算刀尖工件 Z；G43/G44 缺 H、H 非法/不在表、"
                    "同段补偿指令冲突时整段阻断，不沿用旧值；安全 Z 按刀尖"
                    "工件 Z 判定，Z 轴行程按叠加工件偏置与刀长补偿后的主轴"
                    "基准点机床 Z 判定；直线/圆弧/螺旋/固定钻孔循环均记录 H 号、"
                    "补偿方向与数值、刀尖工件坐标及主轴基准点机床坐标"),
                "cutter_compensation": (
                    "G41 左/G42 右半径补偿，左/右按当前圆弧平面（G17/G18/"
                    "G19）正法向 n=u×v 判定（G41 左法向 n×d、G42 右法向 "
                    "d×n）；D 只在与 G41/G42 同段生效，刀具半径取自机床配置 "
                    "radius_offsets（D 为正整数、半径非负，否则拒绝保存）；"
                    "G41/G42 与 G40 均为纯设定行不移动刀具，切入/退出必须"
                    "是非零平面内 G1（切向斜变到满偏置）；直线偏置为平行线、"
                    "圆弧为同心圆（G41+G3/G42+G2 内偏置 R-r_d，另两种组合"
                    "外偏置 R+r_d）；相邻偏置段按几何关系做内角裁切或外角"
                    "补弧，无法连续不猜测轨迹；D 非法/未登记、切入退出无效、"
                    "补偿后圆弧半径非正、相邻段不连续等定位原行阻断；"
                    "每段同时输出编程轮廓、刀心轨迹（center_path_mm）、"
                    "连接方式（junction）与刀具扫掠包围盒"
                    "（swept_bbox_program_mm）；机床行程按刀心±半径叠加工件"
                    "偏置与刀长补偿的扫掠范围判定，越界报 OUT_OF_BOUNDS；"
                    "程序在待切入/激活/待退出状态结束时在 G41/G42 或 G40 "
                    "原行报告；报告可按 d=D1 筛选，对比列出偏置路径与扫掠"
                    "包围盒变化"),
                "feed": "F 按出现时的单位换算为 mm/min 后模态保持",
                "canned_cycle": (
                    "G81/G82/G83 为模态固定循环，G80 或 G0-G3 取消；"
                    "Z/R/Q/P 模态继承，L 为孔位重复次数（默认 1，正整数）；"
                    "G90 下 Z/R 绝对、L 为同位重复，G91 下 Z 相对 R、R 相对初始"
                    "平面、L 沿 XY 增量展开连续孔；G98 返回初始平面（默认），"
                    "G99 返回 R 平面；首次启用缺 Z/R、G83 的 Q 非正、P/L 非法"
                    "或孔底高于 R 时阻断对应孔；固定循环仅允许在 G17(XY) 平面"
                    "展开，G18/G19 下触发阻断对应孔；G83 循环内部排屑快速移动"
                    "豁免安全 Z 告警，孔间定位仍检查"),
            },
        }


def analyze_program(text: str, config: MachineConfig,
                    program_name: str | None = None,
                    progress=None) -> dict:
    return Analyzer(config, program_name, progress).run(text)


DIALECT = {
    "supported_g": {
        "G0": "快速定位", "G1": "直线插补",
        "G2": "顺时针圆弧（当前平面，圆心词随平面 I/J、I/K、J/K 或 R）",
        "G3": "逆时针圆弧（同 G2，旋向相反）",
        "G17": "圆弧平面 XY（上电默认），垂直联动轴 Z",
        "G18": "圆弧平面 XZ，垂直联动轴 Y",
        "G19": "圆弧平面 YZ，垂直联动轴 X",
        "G43": "刀长补偿加（主轴基准点 = 刀尖工件 + H 偏置；须同行给 H）",
        "G44": "刀长补偿减（主轴基准点 = 刀尖工件 - H 偏置；须同行给 H）",
        "G49": "取消刀长补偿",
        "G40": "取消刀具半径补偿（须用非零平面内 G1 退出）",
        "G41": "刀具半径补偿左侧（按平面正法向 n=u×v 判定；须同行给 D）",
        "G42": "刀具半径补偿右侧（同 G41，方向相反；须同行给 D）",
        "G20": "英制单位", "G21": "公制单位",
        "G90": "绝对定位", "G91": "增量定位",
        "G54": "工件坐标系 1（偏置由配置 wcs_offsets 提供）",
        "G55": "工件坐标系 2（偏置由配置 wcs_offsets 提供）",
        "G56": "工件坐标系 3（同上）",
        "G57": "工件坐标系 4（同上）",
        "G58": "工件坐标系 5（同上）",
        "G59": "工件坐标系 6（同上）",
        "G80": "取消固定钻孔循环",
        "G81": "钻孔循环（快速到 R，进给到孔底，快速退回）",
        "G82": "锪孔循环（同 G81，孔底暂停 P）",
        "G83": "深孔啄钻（按 Q 分步下钻，每步退回 R 排屑）",
        "G98": "固定循环后返回初始平面（默认）",
        "G99": "固定循环后返回 R 平面",
    },
    "wcs": {
        "systems": list(WCS_NAMES),
        "offsets": "每个坐标系的 X/Y/Z 偏置由机床配置 wcs_offsets 提供；"
                   "旧字段 offset_x/offset_y/offset_z 归入 G54",
        "switching": "G54-G59 为模态切换：换系时刀具的机床位置不动，"
                     "工件坐标随新偏置重新换算；后续直线/圆弧/螺旋/固定"
                     "钻孔循环都按当前坐标系生成机床轨迹",
        "unconfigured": "程序引用未在 wcs_offsets 中设置偏置的坐标系时，"
                        "不沿用上一坐标系偏置：报 UNKNOWN_WCS，跳过行程"
                        "检查，相关机床坐标、行程及包围盒结论标为未知",
        "safe_z": "安全 Z 始终按当前工件（程序）坐标判定",
    },
    "arcs": {        "planes": {
            "G17": "XY 平面（默认），圆心词 I/J，垂直联动轴 Z",
            "G18": "XZ 平面，圆心词 I/K，垂直联动轴 Y",
            "G19": "YZ 平面，圆心词 J/K，垂直联动轴 X",
        },
        "direction": "G2 顺圆 / G3 逆圆，按“从垂直轴正向看向平面”判定",
        "center_programming": "圆心词为起点到圆心的增量；起终点重合时"
                              "（圆心词编程）为整圆；省略 XYZ 终点词时"
                              "终点即起点，同样按整圆执行",
        "r_programming": "R 正=劣弧（扫角<=180°），R 负=优弧；"
                         "R 不能编程整圆",
        "helical": "垂直当前平面的轴随扫角线性联动，段长为三维螺旋长度",
        "block_rules": [
            "圆心词与 R 混用 -> ARC_NO_SOLUTION，整段阻断并回滚",
            "圆心词不属于当前平面（如 G17 下给 K）-> 阻断并回滚",
            "R 编程整圆（起终点重合）-> 阻断并回滚",
            "圆心词编程时起终半径不一致 -> 阻断并回滚",
        ],
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
        "plane_restriction": "固定循环仅允许在 G17(XY) 平面展开；"
                             "G18/G19 下定义或触发时对应孔阻断"
                             "（CYCLE_PLANE_NOT_G17），循环模态仍登记",
        "block_rules": [
            "首次启用缺少 Z 或 R（G83 还需正的 Q）-> 阻断对应孔",
            "G83 的 Q<=0、P 为负、L 非正整数 -> 阻断对应孔",
            "孔底高于 R 平面 -> CYCLE_PLANE_CONFLICT，阻断对应孔",
            "后续孔位缺少可继承的 XY/初始平面状态 -> 阻断对应孔",
            "G18/G19 平面下展开 -> CYCLE_PLANE_NOT_G17，阻断对应孔",
        ],
    },
    "length_compensation": {
        "G43": "刀长补偿加：主轴基准点机床 Z = 刀尖工件 Z + 工件 Z 偏置 + H 偏置",
        "G44": "刀长补偿减：主轴基准点机床 Z = 刀尖工件 Z + 工件 Z 偏置 - H 偏置",
        "G49": "取消刀长补偿（代数值归零）",
        "H": "H 寄存器号（正整数），只在与 G43/G44 同段时生效；"
             "无 G43/G44 的 H 不生效；G49 同行的 H 不生效",
        "offset_table": "H 偏置由机床配置 length_offsets 提供（mm），"
                        "如 {\"1\": 12.5, \"2\": -3}；H 号非正整数或偏置非"
                        "数值时定位字段并拒绝保存",
        "same_line_motion": "G43/G44/G49 与运动同段时，该段起点用旧补偿、"
                            "其余点用新补偿（同行运动使用新补偿）",
        "comp_only_line": "只有补偿指令（无 Z 词）时主轴基准点保持不动，"
                          "按新补偿重算刀尖工件 Z：新刀尖 = 基准点 - 新补偿",
        "safe_z": "安全 Z 始终按刀尖工件 Z 判定",
        "z_travel": "Z 轴行程按主轴基准点机床 Z（工件偏置 + 刀长补偿）判定",
        "records": "直线/圆弧/螺旋/固定钻孔循环均记录 H 号、补偿方向与数值、"
                   "刀尖工件坐标（start_mm/end_mm/points_mm）与主轴基准点"
                   "机床坐标（start_machine_mm/end_machine_mm/"
                   "points_machine_mm）；孔记录与每个展开动作同样记录",
        "block_rules": [
            "G43/G44 缺 H -> LENGTH_COMP_MISSING_H，整段阻断",
            "H 非正整数或不在 length_offsets -> LENGTH_COMP_H_NOT_FOUND，"
            "整段阻断",
            "G43/G44/G49 同段混用、同一补偿码重复或同段多个 H -> "
            "LENGTH_COMP_CONFLICT，整段阻断",
            "阻断段不沿用旧补偿值：连同本行其他模态改动一起回滚",
        ],
        "report_filter": "报告可按 h=H1 或 h=1 筛选问题、轨迹、孔与按 H 汇总；"
                         "对比结果列出各 H 的补偿使用、路径/钻孔与 Z 行程变化",
    },
    "cutter_compensation": {
        "G40": "取消刀具半径补偿（必须用非零平面内 G1 退出）",
        "G41": "刀具半径补偿左侧：按当前平面正法向 n=u×v 判定"
               "（G17 看 +Z、G18 看 +Y、G19 看 +X，左侧为 n×d）",
        "G42": "刀具半径补偿右侧（d×n，与 G41 相反）",
        "D": "D 寄存器号（正整数），只在与 G41/G42 同段时生效；"
             "无 G41/G42 的 D 不生效；G40 同行的 D 不生效",
        "offset_table": "刀具半径由机床配置 radius_offsets 提供（mm），"
                        "如 {\"1\": 5.0, \"2\": 3.0}；D 号必须为正整数、"
                        "半径必须为非负数值，否则定位字段并拒绝保存",
        "planes": "左/右按 G17(XY)/G18(XZ)/G19(YZ) 的正法向判定；"
                  "补偿在建立时锁定平面，补偿中不允许切换平面（须先 G40 退出）",
        "engage_exit": "G41/G42 Dn 为纯设定行（不移动刀具），下一段必须是"
                       "非零平面内 G1 切入；G40 同样为纯设定行，其后必须用"
                       "非零平面内 G1 退出；切入/退出段刀心从程序点斜变到"
                       "满偏置点（切向切入/切向退出）",
        "offset_rules": "直线偏置为平行直线；圆弧偏置仍为同心圆："
                        "G41+逆圆(G3)、G42+顺圆(G2) 为内偏置（R-r_d），"
                        "G41+顺圆(G2)、G42+逆圆(G3) 为外偏置（R+r_d）；"
                        "内偏置后半径非正 -> CUTTER_ARC_RADIUS，整段阻断",
        "junction": "相邻偏置段按几何关系连接：偏置线相交且都在角点前为"
                    "内角裁切（两段裁到交点）；偏置线分离为外角补弧"
                    "（以程序角点为圆心、r_d 为半径补一段圆角）；"
                    "180° 折返、交点越过段范围（干涉）等无法连续 -> "
                    "CUTTER_COMP_DISCONTINUOUS，不猜测轨迹",
        "segments": "每段同时输出编程轮廓（start_mm/end_mm/points_mm）、"
                    "刀心轨迹（cutter_compensation.center_path_mm，"
                    "切入/退出含无偏置斜切端点）、连接方式（junction："
                    "inner_trim/outer_arc/collinear）与刀具扫掠包围盒"
                    "（swept_bbox_program_mm，刀心±半径）；螺旋段垂直轴"
                    "不偏移，纯垂直 G1 刀心平面坐标保持",
        "travel": "机床行程按刀具扫掠范围（刀心轨迹±刀具半径，并叠加"
                  "工件坐标系偏置与刀长补偿）判定，越界报 OUT_OF_BOUNDS"
                  "（details.checked_path=tool_swept_envelope）",
        "block_rules": [
            "G41/G42 缺 D -> CUTTER_COMP_MISSING_D，整段阻断",
            "D 非正整数或不在 radius_offsets -> CUTTER_COMP_D_NOT_FOUND，"
            "整段阻断",
            "G40/G41/G42 同段混用、补偿码重复、同段多个 D 或补偿中直接"
            "换侧/换 D -> CUTTER_COMP_CONFLICT，整段阻断",
            "切入/退出段不是非零平面内 G1（G0/G2/G3/纯垂直移动/固定循环）"
            "-> CUTTER_APPROACH_INVALID / CUTTER_EXIT_INVALID",
            "补偿后圆弧有效半径非正 -> CUTTER_ARC_RADIUS，该段阻断",
            "补偿中 G0、固定循环、切换平面或相邻偏置段无法连续 -> "
            "CUTTER_COMP_DISCONTINUOUS",
            "程序结束时仍待切入/激活/待退出 -> 在 G41/G42 或 G40 原行"
            "报告 CUTTER_APPROACH_INVALID/CUTTER_EXIT_INVALID",
        ],
        "report_filter": "报告可按 d=D1 或 d=1 筛选问题、轨迹与按 D 汇总；"
                         "对比结果列出 D 半径表变化、各 D 偏置路径/切入退出/"
                         "问题与刀具扫掠包围盒变化",
    },
    "supported_m": {"M3": "主轴正转", "M5": "主轴停止"},
    "package_flow_m": {
        "O": "子程序号行（仅程序包模式 POST /api/packages）",
        "M98": "调用子程序：P 子程序号、L 重复次数（仅程序包模式）",
        "M99": "子程序返回（仅程序包模式）",
        "M2": "程序结束（仅程序包模式）",
        "M30": "程序结束（仅程序包模式）",
    },
    "package_note": "O/M98/M99/M2/M30 只在程序包静态展开（POST /api/packages）"
                    "中支持；单独提交给 /api/analyze、/api/jobs 时仍按未支持"
                    "指令处理。变量/宏表达式（#、[]）在任何模式下均不支持。",
    "supported_words": ["X", "Y", "Z", "I", "J", "K", "R", "F", "S", "N(忽略)",
                        "H(刀长补偿寄存器号，随 G43/G44 生效)",
                        "D(半径补偿寄存器号，随 G41/G42 生效)",
                        "Q(固定循环步进)", "P(固定循环暂停)",
                        "L(固定循环重复次数)",
                        "O(子程序号，仅程序包模式)"],
    "comments": ["(圆括号注释)", ";分号注释"],
    "unsupported_policy": "任何未列出的 G/M 指令及其他地址词均显式报告，"
                          "并整段阻断，不猜测执行",
    "unsupported_examples": [
        "G28/G30 回零",
        "G54.1 附加工件坐标系",
        "G84-G89 其他固定循环（仅支持 G80-G83）",
        "M2/M30 程序结束（仅程序包模式支持）", "M4 反转", "M6 换刀",
        "M7-M9 冷却",
        "T 刀号",
    ],
    "severity_levels": SEVERITY_ORDER,
}
