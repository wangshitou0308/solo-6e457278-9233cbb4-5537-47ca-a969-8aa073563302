"""固定钻孔循环（G81/G82/G83）的展开计算。

纯几何/运动学模块：给定循环定义参数、进入孔位前的刀具位置与定位模式，
输出每个孔的展开轨迹（快速定位、接近 R 平面、进刀、暂停、分步下钻、
回退、返回初始平面或 R 平面）。所有输入/输出长度均为 mm。

展开口径（Fanuc 风格，静态预检采用）：
- G98 孔后返回初始平面，G99 孔后返回 R 平面；未写明时按 G98 默认处理，
  参数来源中显式标注 default。
- G90：Z/R 为绝对坐标；G91：Z 相对当前孔位上方初始高度，R 相对
  “本次进入 R 移动时的起点高度”（即初始平面）。
- Q 恒为无符号正的每步进给量（增量语义，与 G90/G91 无关）。
- G83 啄钻：每次切深 Q，快速回退到 R 平面排屑，再快速下到
  上次孔底之上（预留 0.1 mm 接近量），以进给走完最后一步。
  循环内部的快速回退属于钻削工艺动作，不参与“安全 Z 以下快速移动”告警。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 循环 G 代码 -> 键
CYCLE_G = {"81": "G81", "82": "G82", "83": "G83"}
RETURN_G = {"98": "initial", "99": "r"}
RETURN_CN = {"initial": "初始平面", "r": "R 平面"}
# G83 排屑后重新快速下钻时，距上次孔底的接近预留量（mm）
PECK_APPROACH_MM = 0.1


@dataclass
class CycleParam:
    """一个循环参数（mm 或 s）及其来源行。"""

    value: float
    line_no: int           # 最近一次赋值的源程序行
    source_line: str
    history: list[dict] = field(default_factory=list)  # 全部赋值记录

    @classmethod
    def from_word(cls, value_mm: float, line_no: int, source_line: str,
                  program_value: float | None = None) -> "CycleParam":
        p = cls(value=value_mm, line_no=line_no, source_line=source_line)
        p.history.append({
            "value_mm": round(value_mm, 6),
            "program_value": program_value,
            "line_no": line_no,
            "source_line": source_line,
        })
        return p

    def update(self, value_mm: float, line_no: int, source_line: str,
               program_value: float | None = None):
        self.value = value_mm
        self.line_no = line_no
        self.source_line = source_line
        self.history.append({
            "value_mm": round(value_mm, 6),
            "program_value": program_value,
            "line_no": line_no,
            "source_line": source_line,
        })

    def to_dict(self) -> dict:
        return {
            "value_mm": round(self.value, 6),
            "line_no": self.line_no,
            "source_line": self.source_line,
            "history": self.history,
        }


@dataclass
class CycleDef:
    """当前模态固定循环定义（G80 或 G0-G3 出现时清除）。"""

    cycle: str                       # 'G81' | 'G82' | 'G83'
    def_line_no: int
    def_source_line: str
    initial_z: float                 # 初始平面（mm，循环建立时锁定）
    z: CycleParam | None = None      # 孔底 Z
    r: CycleParam | None = None      # R 平面 Z
    q: CycleParam | None = None      # G83 每步深度（mm）
    p: CycleParam | None = None      # 孔底暂停（s）
    return_mode: str = "initial"     # 'initial'（G98） | 'r'（G99）
    return_mode_line: int | None = None
    return_mode_source: str | None = None
    return_mode_default: bool = True

    def clone(self) -> "CycleDef":
        c = CycleDef(
            cycle=self.cycle,
            def_line_no=self.def_line_no,
            def_source_line=self.def_source_line,
            initial_z=self.initial_z,
            z=self.z, r=self.r, q=self.q, p=self.p,
            return_mode=self.return_mode,
            return_mode_line=self.return_mode_line,
            return_mode_source=self.return_mode_source,
            return_mode_default=self.return_mode_default,
        )
        # CycleParam 可变，深拷贝
        for name in ("z", "r", "q", "p"):
            p = getattr(c, name)
            if p is not None:
                q = CycleParam(value=p.value, line_no=p.line_no,
                               source_line=p.source_line,
                               history=[dict(h) for h in p.history])
                setattr(c, name, q)
        return c

    def ready(self) -> bool:
        """参数是否已足以执行孔加工。"""
        if self.z is None or self.r is None:
            return False
        if self.cycle == "G83" and (self.q is None or self.q.value <= 0):
            return False
        return True

    def missing(self) -> list[str]:
        miss = []
        if self.z is None:
            miss.append("Z")
        if self.r is None:
            miss.append("R")
        if self.cycle == "G83" and (self.q is None or self.q.value <= 0):
            miss.append("Q")
        return miss

    def params_out(self) -> dict:
        out = {
            "cycle": self.cycle,
            "definition_line_no": self.def_line_no,
            "definition_source_line": self.def_source_line,
            "initial_plane_z_mm": round(self.initial_z, 6),
            "return_mode": ("G98" if self.return_mode == "initial" else "G99"),
            "return_mode_source": {
                "line_no": self.return_mode_line,
                "source_line": self.return_mode_source,
                "default": self.return_mode_default,
            },
            "ready": self.ready(),
        }
        for name, label in (("z", "Z_bottom"), ("r", "R_plane"),
                            ("q", "Q_peck"), ("p", "P_dwell_s")):
            p = getattr(self, name)
            out[label] = p.to_dict() if p is not None else None
        return out


def resolve_r(program_r: float, factor: float, mode: str,
              initial_z: float) -> float:
    """把程序中的 R 词解析为绝对 R 平面（mm）。

    G90 直接换算；G91 相对进入循环时锁定的初始平面。
    """
    if mode == "absolute":
        return program_r * factor
    return initial_z + program_r * factor


def resolve_z(program_z: float, factor: float, mode: str,
              r_abs: float | None = None,
              initial_z: float | None = None) -> float:
    """把程序中的 Z 词解析为绝对孔底 Z（mm）。

    - G90：Z 为绝对坐标；
    - G91：Z 为相对 R 平面的增量（R 已知时相对 R，否则退回相对初始平面）。
    """
    if mode == "absolute":
        return program_z * factor
    base = r_abs if r_abs is not None else initial_z
    return base + program_z * factor


def _move(action: str, start, end, rapid: bool, note: str = "",
          dwell_s: float | None = None, internal_cycle: bool = False) -> dict:
    return {
        "action": action,
        "motion": "rapid" if rapid else "feed",
        "internal_cycle": internal_cycle,
        "note": note,
        "start_mm": [round(v, 6) if v is not None else None for v in start],
        "end_mm": [round(v, 6) if v is not None else None for v in end],
        "points_mm": [
            [round(v, 6) if v is not None else None for v in start],
            [round(v, 6) if v is not None else None for v in end]],
        "length_mm": round(_dist(start, end), 6),
        "dwell_s": round(dwell_s, 6) if dwell_s is not None else None,
    }


def _dist(a, b) -> float:
    return sum((bv - av) ** 2 for av, bv in zip(a, b)
               if av is not None and bv is not None) ** 0.5


def expand_hole(definition: CycleDef, hole_xy: tuple[float, float],
                entry_z: float) -> dict:
    """展开单个孔的全部动作。

    entry_z 为执行该孔前刀具所在 Z（mm）；XY 为孔位。
    返回 {moves, retract_z, drill_depth_mm, dwell_s, cutting_len, rapid_len}。
    """
    cd = definition
    r_z = cd.r.value
    z_bot = cd.z.value
    x, y = hole_xy
    moves: list[dict] = []

    # 标准展开（孔间定位段由 analyzer 追加）：
    # A. 快速到 R 平面（若当前 Z < R 则先抬初始平面再下，保守按直接快速到 R）
    if abs(entry_z - r_z) > 1e-9:
        moves.append(_move(
            "approach_r", [x, y, entry_z], [x, y, r_z], rapid=True,
            note=f"快速接近 R 平面 Z={round(r_z, 6)}"))
    cur_z = r_z

    dwell = cd.p.value if (cd.p is not None and cd.cycle == "G82") else 0.0

    if cd.cycle == "G83":
        q = cd.q.value
        # 分步啄钻
        step = 1
        while cur_z - z_bot > 1e-9 + 1e-6:
            next_z = max(cur_z - q, z_bot)
            moves.append(_move(
                f"peck_drill_{step}", [x, y, cur_z], [x, y, next_z],
                rapid=False, note=f"第 {step} 次进给下钻 Q={round(q, 6)}"))
            cur_z = next_z
            if cur_z > z_bot + 1e-9:
                # 排屑：快速退回 R
                moves.append(_peck_retract([x, y, cur_z], [x, y, r_z], step))
                # 快速下到上次孔底之上预留量
                approach_z = cur_z + PECK_APPROACH_MM
                if approach_z > r_z:
                    approach_z = r_z
                moves.append(_move(
                    f"peck_reapproach_{step}", [x, y, r_z],
                    [x, y, approach_z], rapid=True, internal_cycle=True,
                    note=f"快速下钻至距上次孔底 {PECK_APPROACH_MM} mm"))
                cur_z_hold = cur_z
                # 进给走完预留量
                moves.append(_move(
                    f"peck_feed_approach_{step}", [x, y, approach_z],
                    [x, y, cur_z_hold], rapid=False,
                    note="进给走完接近预留量"))
            step += 1
    else:
        # G81 / G82：一次进给到孔底
        moves.append(_move(
            "drill_feed", [x, y, cur_z], [x, y, z_bot], rapid=False,
            note="进给下钻到孔底"))
        cur_z = z_bot
        if cd.cycle == "G82" and dwell > 1e-9:
            moves.append({
                "action": "dwell_bottom",
                "motion": "dwell",
                "note": f"孔底暂停 {round(dwell, 6)} s",
                "start_mm": [x, y, z_bot], "end_mm": [x, y, z_bot],
                "points_mm": [[x, y, z_bot], [x, y, z_bot]],
                "length_mm": 0.0, "dwell_s": round(dwell, 6),
            })

    # 回退
    retract_z = cd.initial_z if cd.return_mode == "initial" else r_z
    moves.append(_move(
        "retract_initial" if cd.return_mode == "initial" else "retract_r",
        [x, y, z_bot], [x, y, retract_z], rapid=True,
        note=("快速返回初始平面" if cd.return_mode == "initial"
              else "快速返回 R 平面")))

    cutting_len = sum(m["length_mm"] for m in moves
                      if m["motion"] == "feed")
    rapid_len = sum(m["length_mm"] for m in moves
                    if m["motion"] == "rapid")
    return {
        "moves": moves,
        "retract_z": retract_z,
        "drill_depth_mm": round(r_z - z_bot, 6),
        "dwell_s": round(dwell, 6),
        "cutting_len_mm": round(cutting_len, 6),
        "rapid_len_mm": round(rapid_len, 6),
    }


def _peck_retract(start, end, step):
    return {
        "action": f"peck_retract_{step}",
        "motion": "rapid",
        "internal_cycle": True,
        "note": "快速退回 R 平面排屑（循环内部动作）",
        "start_mm": [round(v, 6) for v in start],
        "end_mm": [round(v, 6) for v in end],
        "points_mm": [[round(v, 6) for v in start],
                      [round(v, 6) for v in end]],
        "length_mm": round(abs(end[2] - start[2]), 6),
        "dwell_s": None,
    }


def positioning_move(start_xy, end_xy, z_height) -> dict:
    """孔间快速定位段（analyzer 在孔前追加）。"""
    start = [start_xy[0], start_xy[1], z_height]
    end = [end_xy[0], end_xy[1], z_height]
    return {
        "action": "position",
        "motion": "rapid",
        "note": "快速定位到下一孔位（返回高度平面）",
        "start_mm": [round(v, 6) for v in start],
        "end_mm": [round(v, 6) for v in end],
        "points_mm": [[round(v, 6) for v in start],
                      [round(v, 6) for v in end]],
        "length_mm": round(_dist(start, end), 6),
        "dwell_s": None,
    }
