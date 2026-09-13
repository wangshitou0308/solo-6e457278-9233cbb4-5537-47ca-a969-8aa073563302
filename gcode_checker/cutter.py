"""刀具半径补偿（G41/G42 + D）二维几何。

保守策略下的半径刀补口径（不猜测轨迹）：
- 补偿只在当前圆弧平面（G17 XY / G18 XZ / G19 YZ）的 (u, v) 二维坐标内
  作用；垂直轴（螺旋联动轴）不偏移，只做逐点三维映射。
- 左/右侧按平面正法向 n（u × v，右手系）判定：G41 左侧法向为 n×d，
  G42 右侧为 d×n（d 为行进方向单位向量）。
- 直线偏置为平行直线；圆弧偏置仍为同心圆：G41 顺圆（凹腔）与
  G42 逆圆（凸台外廓）为内偏置，有效半径 r_eff=R-r_d；
  另两种组合为外偏置，r_eff=R+r_d。r_eff<=0 无解（过切）。
- 相邻偏置段按几何关系连接：偏置线相交且两条都在角点前截断为内角裁切；
  偏置线分离（外角）时补一段以角点为圆心、半径 r_d 的圆角接弧；
  反向相接、偏置线在段后相交（干涉）等无法连续的情况抛 JoinError，
  由分析器定位原行并阻断（不猜测轨迹）。

输入/输出长度均为 mm（由分析器完成 G20 inch 换算后调用）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

GEOM_EPS = 1e-7
SWEEP_EPS = 1e-9


class JoinError(Exception):
    """相邻偏置段无法连续（反向相接/干涉/内角裁切越界等）。"""


@dataclass
class Primitive:
    """一条已偏置的平面曲线（直线或圆弧）。

    直线：p(t)=p0 + t*d（t∈ℝ，窗口 t0..t1 为保留区间）；
    圆弧：p(a)=center + r_eff*(cos a, sin a)（角度窗口 a0..a1，
    sweep 带符号，与程序圆弧扫角同号；offset_inward 标注内偏置）。
    """

    kind: str                       # 'line' | 'arc'
    # 程序（未偏置）段端点，用于相邻连续性校验与外角圆心
    prog_start: tuple[float, float] | None = None
    prog_end: tuple[float, float] | None = None
    # line
    p0: tuple[float, float] | None = None
    direction: tuple[float, float] | None = None
    # arc
    center: tuple[float, float] | None = None
    radius: float | None = None
    a0: float | None = None
    a1: float | None = None
    sweep: float | None = None
    offset_inward: bool = False
    # 窗口（分析器按连接结果就地截断）
    win0: float = 0.0
    win1: float = 1.0

    @property
    def is_line(self) -> bool:
        return self.kind == "line"

    def point(self, t: float) -> tuple[float, float]:
        if self.is_line:
            return (self.p0[0] + t * self.direction[0],
                    self.p0[1] + t * self.direction[1])
        return (self.center[0] + self.radius * math.cos(t),
                self.center[1] + self.radius * math.sin(t))

    def tangent(self, t: float) -> tuple[float, float]:
        """有向切向（单位向量），沿曲线行进方向。"""
        if self.is_line:
            return self.direction
        sgn = 1.0 if self.sweep > 0 else -1.0
        return (-sgn * math.sin(t), sgn * math.cos(t))


def _unit(v: tuple[float, float]) -> tuple[float, float]:
    n = math.hypot(v[0], v[1])
    return (v[0] / n, v[1] / n)


def side_normal(side: str, d: tuple[float, float]) -> tuple[float, float]:
    """行进方向 d 的左/右单位法向（平面 (u,v) 内，正法向 n=u×v）。"""
    du, dv = _unit(d)
    if side == "left":  # n × d = (dv, -du)
        return (dv, -du)
    return (-dv, du)   # d × n = (-dv, du)


def offset_line(start: tuple[float, float], end: tuple[float, float],
                side: str, r_d: float) -> Primitive:
    """直线段按左/右侧偏置 r_d：窗口 t∈[0, 弦长]。"""
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if length < GEOM_EPS:
        raise JoinError("平面内零长度直线无法建立偏置")
    d = (dx / length, dy / length)
    nrm = side_normal(side, d)
    shift = (r_d * nrm[0], r_d * nrm[1])
    return Primitive(
        kind="line",
        prog_start=start, prog_end=end,
        p0=(start[0] + shift[0], start[1] + shift[1]),
        direction=d,
        win0=0.0, win1=length)


def offset_arc(center: tuple[float, float], radius: float,
               a0: float, sweep: float, clockwise: bool,
               side: str, r_d: float,
               prog_start: tuple[float, float] | None = None,
               prog_end: tuple[float, float] | None = None) -> Primitive:
    """圆弧按左/右侧偏置。

    G41(左)+顺圆 或 G42(右)+逆圆 => 内偏置（有效半径 R-r_d）；
    其余组合为外偏置（R+r_d）。内偏置后半径 <= 0 抛 JoinError。
    """
    inward = ((clockwise and side == "left")
              or ((not clockwise) and side == "right"))
    r_eff = radius - r_d if inward else radius + r_d
    if r_eff <= GEOM_EPS:
        raise JoinError(
            f"补偿后圆弧有效半径 {r_eff:.6g} 非正（程序半径 {radius:.6g}，"
            f"刀具半径 {r_d:.6g}，{'内' if inward else '外'}偏置）")
    sweep_eff = sweep  # 偏置不改扫角
    a1 = a0 + sweep_eff
    if prog_start is None:
        prog_start = _arc_point(center, radius, a0)
    if prog_end is None:
        prog_end = _arc_point(center, radius, a1)
    return Primitive(
        kind="arc", center=center, radius=r_eff,
        prog_start=prog_start, prog_end=prog_end,
        a0=a0, a1=a1, sweep=sweep_eff, offset_inward=inward,
        win0=a0, win1=a1)


def _arc_point(center, r, a):
    return (center[0] + r * math.cos(a), center[1] + r * math.sin(a))


def _line_line(p_a, d_a, p_b, d_b):
    """两直线交点参数 (ta, tb)；平行/重合返回 None。"""
    det = d_a[0] * d_b[1] - d_a[1] * d_b[0]
    if abs(det) < GEOM_EPS:
        return None
    ex, ey = p_b[0] - p_a[0], p_b[1] - p_a[1]
    ta = (ex * d_b[1] - ey * d_b[0]) / det
    tb = (ex * d_a[1] - ey * d_a[0]) / det
    return ta, tb


def _line_arc(line_p, line_d, center, r):
    """直线与整圆的交点 -> [(直线参数 t, 角度 a)]（0/1/2 个）。"""
    fx, fy = line_p[0] - center[0], line_p[1] - center[1]
    b = 2 * (fx * line_d[0] + fy * line_d[1])
    c = fx * fx + fy * fy - r * r
    disc = b * b - 4 * c
    if disc < -GEOM_EPS:
        return []
    disc = max(disc, 0.0)
    sq = math.sqrt(disc)
    out = []
    for t in ((-b - sq) / 2, (-b + sq) / 2):
        x = fx + t * line_d[0]
        y = fy + t * line_d[1]
        out.append((t, math.atan2(y, x)))
    return out


def _arc_arc(c1, r1, c2, r2):
    """两整圆交点 -> [(圆1角度 a1, 圆2角度 a2)]（0/1/2 个）。"""
    dx, dy = c2[0] - c1[0], c2[1] - c1[1]
    d = math.hypot(dx, dy)
    if d < GEOM_EPS:
        return []  # 同心圆不相交
    if d > r1 + r2 + GEOM_EPS or d < abs(r1 - r2) - GEOM_EPS:
        return []
    a = (r1 * r1 - r2 * r2 + d * d) / (2 * d)
    h2 = r1 * r1 - a * a
    h = math.sqrt(max(h2, 0.0))
    ux, uy = dx / d, dy / d
    mx, my = c1[0] + a * ux, c1[1] + a * uy
    out = []
    for sgn in (-1.0, 1.0):
        px, py = mx + sgn * (-uy) * h, my + sgn * ux * h
        out.append((math.atan2(py - c1[1], px - c1[0]),
                    math.atan2(py - c2[1], px - c2[0])))
    return out


def _norm_da(a: float, a0: float, sweep: float) -> float:
    """把交点角度 a 归一到以 a0 为起点、与 sweep 同方向的参数（弧度）。"""
    s = 1.0 if sweep >= 0 else -1.0
    da = s * (a - a0)
    while da < -GEOM_EPS:
        da += 2 * math.pi
    while da > 2 * math.pi + GEOM_EPS:
        da -= 2 * math.pi
    return s * da  # 带符号（负号只可能是容差噪声）


@dataclass
class Connector:
    """外角补接圆角（以程序角点为圆心、半径 r_d）。"""

    center: tuple[float, float]
    radius: float
    a_from: float
    a_to: float
    sweep: float
    points: list = field(default_factory=list)


@dataclass
class JoinResult:
    kind: str            # 'inner' | 'outer' | 'collinear'
    prev_t: float        # 前段窗口新终点
    next_t: float        # 后段窗口新起点
    connector: Connector | None = None


def join_primitives(prev: Primitive, nxt: Primitive,
                    r_d: float) -> JoinResult:
    """连接两条相邻偏置曲线，返回裁切/补接结果。

    失败（不猜测轨迹）统一抛 JoinError：
    - 切向反向相接（两段法线相对）；
    - 内角交点落在前段起点之前或后段终点之后（过切/干涉）；
    - 外角两偏置曲线无可达交点（理论上不应出现，出现即几何不连续）。
    """
    p_corner = prev.prog_end
    n_corner = nxt.prog_start
    if math.hypot(p_corner[0] - n_corner[0],
                  p_corner[1] - n_corner[1]) > 1e-5:
        raise JoinError("相邻段程序端点不连续（前段终点与后段起点不重合）")

    t_prev_end, t_next_start = prev.win1, nxt.win0
    d_prev = prev.tangent(t_prev_end)
    d_next = nxt.tangent(t_next_start)
    cross = d_prev[0] * d_next[1] - d_prev[1] * d_next[0]
    dot = d_prev[0] * d_next[0] + d_prev[1] * d_next[1]

    # 切向平行（含共线）：同向直接相接，反向无法连续
    if abs(cross) < 1e-6:
        if dot < 0:
            raise JoinError("相邻偏置段切向相反（180° 折返），无法连续")
        gap = math.hypot(*(nxt.point(t_next_start)[i]
                           - prev.point(t_prev_end)[i]
                           for i in (0, 1)))
        if gap > 1e-5:
            raise JoinError(
                f"相邻平行偏置段间距 {gap:.6g}（应共线相接），无法连续")
        return JoinResult("collinear", t_prev_end, t_next_start)

    # 候选交点（参数对）。arc 参数为绝对角度（已按扫向选圈），
    # line 参数为 p0+t*direction 上的有向距离。
    if prev.is_line and nxt.is_line:
        hit = _line_line(prev.p0, prev.direction, nxt.p0, nxt.direction)
        cands = [hit] if hit else []
    elif prev.is_line:
        cands = [(t, nxt.a0 + _norm_da(a, nxt.a0, nxt.sweep))
                 for (t, a) in
                 _line_arc(prev.p0, prev.direction, nxt.center, nxt.radius)]
    elif nxt.is_line:
        cands = [(prev.a0 + _norm_da(a, prev.a0, prev.sweep), t)
                 for (t, a) in
                 _line_arc(nxt.p0, nxt.direction, prev.center, prev.radius)]
    else:
        cands = [(prev.a0 + _norm_da(a1, prev.a0, prev.sweep),
                  nxt.a0 + _norm_da(a2, nxt.a0, nxt.sweep))
                 for (a1, a2) in
                 _arc_arc(prev.center, prev.radius, nxt.center, nxt.radius)]

    def _within(t, prim):
        # t 是否落在曲线（有向）窗口内
        if prim.is_line:
            return min(prim.win0, prim.win1) - 1e-6 <= t <= \
                max(prim.win0, prim.win1) + 1e-6
        lo = prim.a0 + min(0.0, prim.sweep)
        hi = prim.a0 + max(0.0, prim.sweep)
        return lo - 1e-6 <= t <= hi + 1e-6

    inner = None
    outer = None
    for tp, tn in cands:
        before_p = _param_le(prev, tp, t_prev_end)
        after_n = _param_ge(nxt, tn, t_next_start)
        if before_p and after_n and _within(tp, prev) and _within(tn, nxt):
            # 多个内角候选取最靠近程序角点者
            if inner is None or _corner_dist(prev, tp, p_corner) < \
                    _corner_dist(prev, inner[0], p_corner):
                inner = (tp, tn)
        elif not before_p and not after_n:
            if outer is None or _corner_dist(prev, tp, p_corner) < \
                    _corner_dist(prev, outer[0], p_corner):
                outer = (tp, tn)

    if inner is not None:
        return JoinResult("inner", inner[0], inner[1])

    if outer is not None:
        conn = _make_connector(prev, nxt, p_corner, r_d,
                               outer[0], outer[1])
        return JoinResult("outer", t_prev_end, t_next_start, connector=conn)

    # 交点落在段范围外（过切干涉）或无交点：不猜测轨迹
    if cands:
        raise JoinError(
            "偏置曲线交点超出段范围（相邻段几何干涉/过切），无法连续")
    raise JoinError("相邻偏置段既无内角交点也无外角补接可能，无法连续")


def _param_le(prim: Primitive, t: float, ref: float) -> bool:
    """有向参数 t 是否在 ref 之前（含容差）。"""
    if prim.is_line:
        return t <= ref + 1e-6
    s = 1.0 if prim.sweep >= 0 else -1.0
    return s * (t - ref) <= 1e-6


def _param_ge(prim: Primitive, t: float, ref: float) -> bool:
    if prim.is_line:
        return t >= ref - 1e-6
    s = 1.0 if prim.sweep >= 0 else -1.0
    return s * (t - ref) >= -1e-6


def _corner_dist(prim: Primitive, t: float,
                 corner: tuple[float, float]) -> float:
    p = prim.point(t)
    return math.hypot(p[0] - corner[0], p[1] - corner[1])


def _make_connector(prev: Primitive, nxt: Primitive,
                    corner: tuple[float, float], r_d: float,
                    tp: float, tn: float) -> Connector:
    """外角补接圆角：圆心为程序角点，半径 r_d，连接前段偏置终点到
    后段偏置起点。起止角度由两偏置曲线在角点处的位置确定，扫向取
    与两段切向转动一致的短弧。"""
    p_from = prev.point(prev.win1)
    p_to = nxt.point(nxt.win0)
    a_from = math.atan2(p_from[1] - corner[1], p_from[0] - corner[0])
    a_to = math.atan2(p_to[1] - corner[1], p_to[0] - corner[0])
    d_prev = prev.tangent(prev.win1)
    d_next = nxt.tangent(nxt.win0)
    turn = d_prev[0] * d_next[1] - d_prev[1] * d_next[0]
    # 圆角行进方向：左转(cross>0)用 CCW，右转用 CW；取该方向上的短弧
    sgn = 1.0 if turn > 0 else -1.0
    if sgn > 0:
        sweep = a_to - a_from
        while sweep <= SWEEP_EPS:
            sweep += 2 * math.pi
    else:
        sweep = a_to - a_from
        while sweep >= -SWEEP_EPS:
            sweep -= 2 * math.pi
    n = max(4, int(math.ceil(abs(sweep) / (math.pi / 2) * 8)))
    pts = []
    for k in range(n + 1):
        a = a_from + sweep * (k / n)
        pts.append((corner[0] + r_d * math.cos(a),
                    corner[1] + r_d * math.sin(a)))
    pts[-1] = p_to
    return Connector(center=corner, radius=r_d, a_from=a_from, a_to=a_to,
                     sweep=sweep, points=pts)


def sample_primitive(prim: Primitive, t0: float, t1: float,
                     max_steps: int = 96) -> list[tuple[float, float]]:
    """按窗口 [t0, t1] 采样偏置曲线（直线取端点，圆弧加密）。"""
    if prim.is_line:
        return [prim.point(t0), prim.point(t1)]
    sweep = t1 - t0
    n = int(min(max_steps, max(4,
                math.ceil(abs(sweep) / (2 * math.pi) * 96))))
    pts = []
    for k in range(n + 1):
        a = t0 + sweep * (k / n)
        pts.append(prim.point(a))
    end = prim.point(t1)
    pts[-1] = end
    return pts


def primitive_bbox_2d(prim: Primitive, t0: float, t1: float,
                      r_sweep: float = 0.0) -> tuple[list, list]:
    """偏置曲线（含刀具扫掠半径膨胀）在平面内的包围盒 [[umin,vmin],
    [umax,vmax]]。

    直线取两端点按 r_sweep 膨胀；圆弧取端点 + 扫过的象限角，圆心方向
    按 r_eff±r_sweep（内偏置时内缘半径可能为 0）膨胀。
    """
    if prim.is_line:
        p_a, p_b = prim.point(t0), prim.point(t1)
        umin = min(p_a[0], p_b[0]) - r_sweep
        umax = max(p_a[0], p_b[0]) + r_sweep
        vmin = min(p_a[1], p_b[1]) - r_sweep
        vmax = max(p_a[1], p_b[1]) + r_sweep
        return [umin, vmin], [umax, vmax]

    sweep = t1 - t0
    angles = [t0, t1]
    lo, hi = (t1, t0) if sweep < 0 else (t0, t1)
    k0 = math.ceil(lo / (math.pi / 2) - 1e-9)
    k1 = math.floor(hi / (math.pi / 2) + 1e-9)
    angles = [t0] + [k * math.pi / 2 for k in range(k0, k1 + 1)] + [t1]
    r_out = prim.radius + r_sweep
    r_in = max(prim.radius - r_sweep, 0.0)
    # 外缘按象限角，内缘只在该角度被弧扫过时取反向极值
    umin = math.inf
    umax = -math.inf
    vmin = math.inf
    vmax = -math.inf
    for a in angles:
        c, s = math.cos(a), math.sin(a)
        umin = min(umin, prim.center[0] + r_out * c)
        umax = max(umax, prim.center[0] + r_out * c)
        vmin = min(vmin, prim.center[1] + r_out * s)
        vmax = max(vmax, prim.center[1] + r_out * s)
    # 内缘：刀具扫掠覆盖到偏置弧的内侧 r_in 圆（仅影响靠近圆心一侧）
    if r_in < prim.radius - GEOM_EPS:
        for a in angles:
            c, s = math.cos(a), math.sin(a)
            umin = min(umin, prim.center[0] + r_in * c)
            umax = max(umax, prim.center[0] + r_in * c)
            vmin = min(vmin, prim.center[1] + r_in * s)
            vmax = max(vmax, prim.center[1] + r_in * s)
    return [umin, vmin], [umax, vmax]
