"""基于 http.server 的本地 REST API（仅标准库，断网可用）。

路由概览：
  GET    /api/health                     健康检查
  GET    /api/dialect                    支持的指令方言 / 严重度定义
  GET    /api/docs                       API 文档（Markdown 文本）
  GET    /api/examples                   示例程序清单
  GET    /api/examples/<name>            下载示例 .nc/.json 文件
  GET/POST /api/machines                 列出 / 新建机床配置
  GET/PUT/DELETE /api/machines/<id>      查询 / 更新 / 删除配置
  POST   /api/analyze                    同步分析（不落库，立即返回报告）
  POST   /api/jobs                       创建分析作业（后台执行）
  GET    /api/jobs                       作业列表
  GET    /api/jobs/<id>                  查询作业进度与概要
  GET    /api/jobs/<id>/report           完整 JSON 报告（可按严重度/代码筛选）
  GET    /api/jobs/<id>/report/download  下载 JSON 报告（attachment）
  GET    /api/jobs/<id>/gcode            取作业原始 .nc 文本
  POST   /api/packages                   创建程序包静态展开作业
  GET    /api/packages                   程序包列表
  GET    /api/packages/<id>              查询展开状态/调用图/错误概要
  GET    /api/packages/<id>/report       完整报告（?source=O100 按来源筛选轨迹）
  GET    /api/packages/<id>/report/download  下载完整程序包 JSON
  GET    /api/packages/<id>/blocks       展开块分页预览
  GET    /api/packages/<id>/package      下载原始程序包 JSON
  POST   /api/package-compare            对比两个程序包（内联或两个已完成包）
  POST   /api/compare                    比较两个程序（内联文本或两个已完成作业）
  GET    /api/comparisons                对比记录列表
  GET    /api/comparisons/<id>           读取已保存的对比结果
"""

from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import __version__
from .analyzer import (
    ConfigError,
    MachineConfig,
    SEVERITY_ORDER,
    ISSUE_SEVERITY,
    WCS_NAMES,
    analyze_program,
)
from .database import JobStore
from .examples import get_example, list_examples
from .compare import compare_reports
from .packages import (
    PACKAGE_DIALECT,
    PackageSpecError,
    parse_package_spec,
)

API_PREFIX = "/api/"
MAX_BODY_BYTES = 4 * 1024 * 1024  # 单次请求体上限 4 MiB（.nc 文本）


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


# ---------------------------------------------------------------------------
# 报告筛选
# ---------------------------------------------------------------------------

def filter_report(report: dict, query: dict) -> dict:
    """按 severity / code / 行范围 / 循环类型 / 孔序 / 圆弧平面 / 坐标系
    筛选问题；其余统计同步重算。"""
    severities = _csv_param(query, "severity")
    codes = _csv_param(query, "code")
    line_from = _int_param(query, "line_from")
    line_to = _int_param(query, "line_to")
    cycles = _csv_param(query, "cycle")
    hole_from = _int_param(query, "hole_from")
    hole_to = _int_param(query, "hole_to")
    planes = _csv_param(query, "plane")
    wcs_list = _csv_param(query, "wcs")

    for s in severities:
        if s not in SEVERITY_ORDER:
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_QUERY",
                           f"未知严重度 {s!r}",
                           {"allowed": SEVERITY_ORDER})
    unknown_codes = [c for c in codes if c not in ISSUE_SEVERITY]
    if unknown_codes:
        raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_QUERY",
                       f"未知问题代码 {unknown_codes}",
                       {"allowed": sorted(ISSUE_SEVERITY)})
    cycles_upper = [c.upper() for c in cycles]
    bad_cycles = [c for c in cycles_upper if c not in ("G81", "G82", "G83")]
    if bad_cycles:
        raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_QUERY",
                       f"未知循环类型 {bad_cycles}",
                       {"allowed": ["G81", "G82", "G83"]})
    planes_upper = [p.upper() for p in planes]
    bad_planes = [p for p in planes_upper if p not in ("G17", "G18", "G19")]
    if bad_planes:
        raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_QUERY",
                       f"未知圆弧平面 {bad_planes}",
                       {"allowed": ["G17", "G18", "G19"]})
    wcs_upper = [w.upper() for w in wcs_list]
    bad_wcs = [w for w in wcs_upper if w not in WCS_NAMES]
    if bad_wcs:
        raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_QUERY",
                       f"未知工件坐标系 {bad_wcs}",
                       {"allowed": list(WCS_NAMES)})
    for name, v in (("hole_from", hole_from), ("hole_to", hole_to)):
        if v is not None and v < 1:
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_QUERY",
                           f"{name} 必须 >= 1")

    issues = report["issues"]
    if severities:
        issues = [i for i in issues if i["severity"] in severities]
    if codes:
        issues = [i for i in issues if i["code"] in codes]
    if line_from is not None:
        issues = [i for i in issues if i["line_no"] >= line_from]
    if line_to is not None:
        issues = [i for i in issues if i["line_no"] <= line_to]
    if cycles_upper:
        issues = [i for i in issues
                  if i.get("details", {}).get("cycle") in cycles_upper]
    if planes_upper:
        issues = [i for i in issues
                  if i.get("details", {}).get("plane") in planes_upper]
    if wcs_upper:
        issues = [i for i in issues
                  if i.get("details", {}).get("wcs") in wcs_upper]
    def _in_hole_range(i):
        no = i.get("details", {}).get("hole_no")
        if no is None:
            return True  # 非循环问题不受孔序筛选影响
        return ((hole_from is None or no >= hole_from)
                and (hole_to is None or no <= hole_to))
    if hole_from is not None or hole_to is not None:
        issues = [i for i in issues if _in_hole_range(i)]

    out = dict(report)
    out["issues"] = issues
    counts = {s: 0 for s in SEVERITY_ORDER}
    for i in issues:
        counts[i["severity"]] += 1
    out["risk"] = dict(report["risk"])
    out["risk"]["counts_by_severity"] = counts
    out["risk"]["total_issues"] = len(issues)
    cycle_filter = bool(cycles_upper or hole_from is not None
                        or hole_to is not None)
    plane_filter = bool(planes_upper)
    wcs_filter = bool(wcs_upper)
    out["filter"] = {
        "severity": severities, "code": codes,
        "line_from": line_from, "line_to": line_to,
        "cycle": cycles_upper,
        "hole_from": hole_from, "hole_to": hole_to,
        "plane": planes_upper,
        "wcs": wcs_upper,
        "matched": len(issues),
        "total_in_report": len(report["issues"]),
    }
    if (cycle_filter or wcs_filter) and "drill_cycles" in out:
        out["drill_cycles"] = _filter_drill_cycles(
            report["drill_cycles"], cycles_upper, hole_from, hole_to,
            wcs_upper)
    if plane_filter and "arcs" in out:
        out["arcs"] = _filter_arcs(report["arcs"], planes_upper, issues)
    if wcs_filter and "wcs" in out:
        out["wcs"] = _filter_wcs(report["wcs"], wcs_upper)
    # 逐行轨迹：默认随循环/平面/坐标系筛选裁剪；?trajectory=0 省略，
    # ?trajectory=all 不裁剪
    traj_flag = query.get("trajectory", ["1"])[0]
    if traj_flag in ("0", "false", "no"):
        out.pop("trajectory", None)
    elif traj_flag in ("all", "full"):
        pass
    else:
        if cycle_filter and "trajectory" in out:
            out["trajectory"] = _filter_trajectory_cycles(
                out["trajectory"], cycles_upper, hole_from, hole_to)
        if plane_filter and "trajectory" in out:
            out["trajectory"] = _filter_trajectory_planes(
                out["trajectory"], planes_upper)
        if wcs_filter and "trajectory" in out:
            out["trajectory"] = _filter_trajectory_wcs(
                out["trajectory"], wcs_upper)
    return out


def _hole_in(no, hole_from, hole_to) -> bool:
    return ((hole_from is None or no >= hole_from)
            and (hole_to is None or no <= hole_to))


def _summarize_holes(holes):
    """从命中孔记录重算孔数/钻深/暂停/展开路径（含孔间定位段）。"""
    drilled = [h for h in holes if h.get("status") == "drilled"]
    blocked = len(holes) - len(drilled)
    depth = round(sum(h.get("drill_depth_mm") or 0.0 for h in drilled), 6)
    dwell = round(sum(h.get("dwell_s") or 0.0 for h in drilled), 6)
    rapid = round(sum((h.get("expanded_path_mm") or {}).get("rapid", 0.0)
                      for h in drilled), 6)
    cutting = round(sum((h.get("expanded_path_mm") or {}).get("cutting", 0.0)
                        for h in drilled), 6)
    return {
        "holes": len(holes), "drilled": len(drilled), "blocked": blocked,
        "depth": depth, "dwell": dwell,
        "rapid": rapid, "cutting": cutting,
        "total": round(rapid + cutting, 6),
    }


def _filter_drill_cycles(dc: dict, cycles, hole_from, hole_to,
                         wcs=None) -> dict:
    """按循环类型/孔序/坐标系筛选固定循环段与孔记录；所有分组/明细/汇总
    只反映命中孔。"""
    groups = []
    for g in dc.get("groups", []):
        if cycles and g["cycle"] not in cycles:
            continue
        holes = [h for h in g.get("holes", [])
                 if _hole_in(h["hole_no"], hole_from, hole_to)
                 and (not wcs or h.get("wcs") in wcs)]
        if not holes:
            continue  # 整组无命中孔，直接剔除
        s = _summarize_holes(holes)
        ng = dict(g)
        ng["holes"] = holes
        ng["hole_count"] = s["holes"]
        ng["executed_holes"] = s["drilled"]
        ng["blocked_holes"] = s["blocked"]
        ng["total_drill_depth_mm"] = s["depth"]
        ng["total_dwell_s"] = s["dwell"]
        ng["expanded_path_mm"] = {"rapid": s["rapid"],
                                  "cutting": s["cutting"],
                                  "total": s["total"]}
        groups.append(ng)

    # 按循环类型重算（只含命中孔与保留分组）
    by_cycle: dict = {}
    for g in groups:
        d = by_cycle.setdefault(g["cycle"], {
            "groups": 0, "holes": 0, "drilled": 0, "blocked": 0,
            "drill_depth_mm": 0.0, "expanded_rapid_mm": 0.0,
            "expanded_cutting_mm": 0.0})
        d["groups"] += 1
        d["holes"] += g["hole_count"]
        d["drilled"] += g["executed_holes"]
        d["blocked"] += g["blocked_holes"]
        d["drill_depth_mm"] = round(
            d["drill_depth_mm"] + g["total_drill_depth_mm"], 6)
        d["expanded_rapid_mm"] = round(
            d["expanded_rapid_mm"] + g["expanded_path_mm"]["rapid"], 6)
        d["expanded_cutting_mm"] = round(
            d["expanded_cutting_mm"] + g["expanded_path_mm"]["cutting"], 6)

    total = sum(g["hole_count"] for g in groups)
    drilled = sum(g["executed_holes"] for g in groups)
    blocked = sum(g["blocked_holes"] for g in groups)
    depth = round(sum(g["total_drill_depth_mm"] for g in groups), 6)
    dwell = round(sum(g["total_dwell_s"] for g in groups), 6)
    rapid = round(sum(g["expanded_path_mm"]["rapid"] for g in groups), 6)
    cutting = round(sum(g["expanded_path_mm"]["cutting"] for g in groups), 6)
    out = dict(dc)
    out["groups"] = groups
    out["by_cycle"] = by_cycle
    out["summary"] = {
        "cycle_groups": len(groups),
        "holes_total": total,
        "holes_drilled": drilled,
        "holes_blocked": blocked,
        "total_drill_depth_mm": depth,
        "total_dwell_s": dwell,
        "expanded_path_mm": {"rapid": rapid, "cutting": cutting,
                             "total": round(rapid + cutting, 6)},
        "filtered": True,
    }
    return out


def _filter_arcs(arcs: dict, planes, issues) -> dict:
    """按平面筛选弧段汇总：by_plane 只保留命中平面，total 随之重算；
    blocked_count 按筛选后的问题列表重算。"""
    by_plane = {p: arcs.get("by_plane", {}).get(
        p, {"count": 0, "arc_length_mm": 0.0, "length_3d_mm": 0.0,
            "helical_count": 0, "full_circle_count": 0})
        for p in planes}
    total = {
        "count": sum(v["count"] for v in by_plane.values()),
        "arc_length_mm": round(sum(v["arc_length_mm"]
                                   for v in by_plane.values()), 6),
        "length_3d_mm": round(sum(v["length_3d_mm"]
                                  for v in by_plane.values()), 6),
        "helical_count": sum(v["helical_count"] for v in by_plane.values()),
        "full_circle_count": sum(v["full_circle_count"]
                                 for v in by_plane.values()),
    }
    return {
        "by_plane": by_plane,
        "total": total,
        "blocked_count": sum(1 for i in issues
                             if i["code"] == "ARC_NO_SOLUTION"),
        "filtered": True,
    }


def _filter_wcs(wcs_section: dict, wcs) -> dict:
    """按坐标系筛选 wcs 汇总节：by_wcs 只保留命中坐标系（未使用的坐标系
    补零值行，偏置信息取自配置），used 同步裁剪。"""
    by_wcs = {}
    for w in wcs:
        row = wcs_section.get("by_wcs", {}).get(w)
        if row is None:
            off = wcs_section.get("offsets_mm", {}).get(w)
            row = {
                "configured": off is not None,
                "offset_mm": off,
                "path_length_mm": {"rapid": 0.0, "cutting": 0.0,
                                   "total": 0.0, "unknown_segments": 0},
                "machine_bbox_mm": None,
                "issues": 0,
            }
        by_wcs[w] = row
    out = dict(wcs_section)
    out["by_wcs"] = by_wcs
    out["used"] = [w for w in wcs_section.get("used", []) if w in wcs]
    out["filtered"] = True
    return out


def _filter_trajectory_wcs(trajectory, wcs):
    """逐行轨迹按坐标系裁剪：轨迹段属于其他坐标系的条目剔除，
    无轨迹段的设定/注释行原样保留。"""
    out = []
    for e in trajectory:
        seg = e.get("segment")
        if seg is not None and seg.get("wcs") not in wcs:
            continue
        out.append(e)
    return out


def _filter_trajectory_planes(trajectory, planes):
    """逐行轨迹按圆弧平面裁剪：命中平面外的弧段条目剔除，其余行原样保留。"""
    out = []
    for e in trajectory:
        seg = e.get("segment")
        arc = (seg or {}).get("arc")
        if arc is not None and arc.get("plane_code") not in planes:
            continue
        out.append(e)
    return out


def _filter_trajectory_cycles(trajectory, cycles, hole_from, hole_to):
    """同步裁剪逐行轨迹中固定循环段的孔/动作明细。

    每个动作都带 hole_no（含孔间定位段 position），按命中孔过滤；
    段的孔数/长度/钻深按保留孔重算。非循环行原样保留。
    """
    out = []
    for e in trajectory:
        seg = e.get("segment")
        if seg is None or seg.get("kind") != "canned_cycle":
            out.append(e)
            continue
        if cycles and seg.get("cycle") not in cycles:
            continue
        holes = [h for h in seg.get("holes", [])
                 if _hole_in(h["hole_no"], hole_from, hole_to)]
        if not holes:
            continue
        keep_nos = {h["hole_no"] for h in holes}
        # 动作明细只保留命中孔（position/approach/peck/retract 均带 hole_no）
        moves = [m for m in seg.get("moves_mm", [])
                 if m.get("hole_no") in keep_nos]
        s = _summarize_holes(holes)
        ne = dict(e)
        ns = dict(seg)
        ns["holes"] = holes
        ns["hole_nos"] = [n for n in ns.get("hole_nos", []) if n in keep_nos]
        ns["moves_mm"] = moves
        ns["length_mm"] = s["total"]
        ns["rapid_length_mm"] = s["rapid"]
        ns["cutting_length_mm"] = s["cutting"]
        ns["drill_depth_mm"] = s["depth"]
        ns["dwell_s"] = s["dwell"]
        ne["segment"] = ns
        out.append(ne)
    return out


def filter_package_report(report: dict, query: dict) -> dict:
    """程序包报告筛选：?source=O100 按来源程序裁剪逐行轨迹与问题；
    ?trajectory=0 省略轨迹。顶层统计保持完整（块级统计另给 filter 说明）。"""
    out = dict(report)
    traj_flag = query.get("trajectory", ["1"])[0]
    if traj_flag in ("0", "false", "no"):
        out.pop("trajectory", None)
        out["filter"] = {"trajectory": "omitted"}
        return out

    sources = _csv_param(query, "source")
    if not sources:
        return out
    valid = {"main"} | {s["program"] for s in
                        report.get("package", {}).get("subprograms", [])
                        if s.get("program")}
    bad = [s for s in sources if s not in valid]
    if bad:
        raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_QUERY",
                       f"未知来源程序 {bad}", {"allowed": sorted(valid)})
    wanted = set(sources)
    issues = [i for i in report["issues"]
              if i.get("source_program", "main") in wanted]
    counts = {s: 0 for s in SEVERITY_ORDER}
    for i in issues:
        counts[i["severity"]] += 1
    traj = [e for e in report["trajectory"]
            if e.get("source_program", "main") in wanted]
    out["issues"] = issues
    out["trajectory"] = traj
    out["risk"] = dict(report["risk"])
    out["risk"]["counts_by_severity"] = counts
    out["risk"]["total_issues"] = len(issues)
    out["filter"] = {
        "source": sources,
        "matched_issues": len(issues),
        "matched_blocks": len(traj),
        "total_issues_in_report": len(report["issues"]),
        "total_blocks_in_report": len(report["trajectory"]),
    }
    return out


def _csv_param(query: dict, name: str) -> list[str]:
    vals = []
    for raw in query.get(name, []):
        vals.extend(v.strip() for v in raw.split(",") if v.strip())
    return vals


def _int_param(query: dict, name: str):
    if name not in query:
        return None
    try:
        return int(query[name][0])
    except ValueError:
        raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_QUERY",
                       f"{name} 必须是整数")


# ---------------------------------------------------------------------------
# 请求处理器
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = f"GcodeChecker/{__version__}"
    store: JobStore = None  # 由 make_server 注入

    # 日志走 stderr，保持安静可配置
    def log_message(self, fmt, *args):  # noqa: N802
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # -- 基础工具 ----------------------------------------------------------

    def _send_json(self, obj, status=HTTPStatus.OK, headers=None):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, content_type="text/plain; charset=utf-8",
                   status=HTTPStatus.OK, download_name=None):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if download_name:
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "EMPTY_BODY",
                           "请求体为空，期望 application/json")
        if length > MAX_BODY_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                           "BODY_TOO_LARGE",
                           f"请求体超过 {MAX_BODY_BYTES} 字节上限")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_JSON",
                           f"JSON 解析失败: {e}")
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_JSON",
                           "请求体必须是 JSON 对象")
        return data

    def _error(self, err: ApiError):
        self._send_json({
            "error": {"code": err.code, "message": err.message,
                      "details": err.details},
        }, status=err.status)

    # -- 路由 --------------------------------------------------------------

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self):  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self):  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if not path.startswith(API_PREFIX):
                if path in ("/", "/api"):
                    self._root()
                else:
                    raise ApiError(HTTPStatus.NOT_FOUND, "NOT_FOUND",
                                   f"路径不存在: {path}")
                return
            self._route(method, path[len(API_PREFIX):], query)
        except ApiError as e:
            self._error(e)
        except Exception as e:  # 服务端兜底，不泄露栈到客户端外
            if getattr(self.server, "verbose", False):
                import traceback
                traceback.print_exc()
            self._error(ApiError(HTTPStatus.INTERNAL_SERVER_ERROR,
                                 "INTERNAL", f"服务器内部错误: {e!r}"))

    def _root(self):
        self._send_json({
            "service": "gcode-checker",
            "version": __version__,
            "offline_only": True,
            "machine_control": "本服务不连接、不控制任何机床",
            "entrypoints": [
                "GET /api/health", "GET /api/dialect", "GET /api/docs",
                "GET /api/examples", "GET/POST /api/machines",
                "POST /api/jobs", "GET /api/jobs/<id>",
                "GET /api/jobs/<id>/report",
                "GET /api/jobs/<id>/report/download",
                "POST /api/packages", "GET /api/packages/<id>",
                "GET /api/packages/<id>/report",
                "GET /api/packages/<id>/report/download",
                "GET /api/packages/<id>/blocks",
                "POST /api/compare", "GET /api/comparisons",
            ],
        })

    def _route(self, method, rel, query):
        store = self.server.store
        parts = [p for p in rel.split("/") if p]

        if parts == ["health"] and method == "GET":
            self._send_json({"status": "ok", "version": __version__,
                             "offline": True})
        elif parts == ["dialect"] and method == "GET":
            from .analyzer import DIALECT, ISSUE_TITLE
            from .packages import (
                EXPANSION_ERROR_TITLE,
                EXPANSION_ERROR_SEVERITY,
            )
            self._send_json({
                "dialect": DIALECT,
                "issue_titles": ISSUE_TITLE,
                "issue_severity": ISSUE_SEVERITY,
                "package_dialect": PACKAGE_DIALECT,
                "expansion_error_titles": EXPANSION_ERROR_TITLE,
                "expansion_error_severity": EXPANSION_ERROR_SEVERITY,
            })
        elif parts == ["docs"] and method == "GET":
            from .docs import API_DOCS
            self._send_text(API_DOCS, "text/markdown; charset=utf-8")
        elif parts == ["examples"] and method == "GET":
            self._send_json({"examples": list_examples()})
        elif len(parts) == 2 and parts[0] == "examples" and method == "GET":
            name = parts[1]
            try:
                content, filename = get_example(name)
            except KeyError:
                raise ApiError(HTTPStatus.NOT_FOUND, "EXAMPLE_NOT_FOUND",
                               f"示例不存在: {name}")
            if filename.endswith(".json"):
                ctype = "application/json; charset=utf-8"
            else:
                ctype = "text/plain; charset=utf-8"
            self._send_text(content, ctype, download_name=filename)

        elif parts == ["machines"] and method == "GET":
            self._send_json({"machines": store.list_machines()})
        elif parts == ["machines"] and method == "POST":
            data = self._read_json()
            config = self._config_or_400(data)
            mid = store.save_machine(config)
            self._send_json({"id": mid, "config": config.to_dict()},
                            HTTPStatus.CREATED)
        elif len(parts) == 2 and parts[0] == "machines":
            self._machine_detail(method, parts[1])

        elif parts == ["analyze"] and method == "POST":
            self._analyze_inline()

        elif parts == ["jobs"] and method == "POST":
            self._create_job()
        elif parts == ["jobs"] and method == "GET":
            self._send_json({"jobs": store.list_jobs()})
        elif len(parts) == 2 and parts[0] == "jobs" and method == "GET":
            self._job_detail(parts[1])
        elif (len(parts) == 3 and parts[0] == "jobs"
              and parts[2] == "gcode" and method == "GET"):
            text = store.get_gcode(parts[1])
            if text is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "JOB_NOT_FOUND",
                               f"作业不存在: {parts[1]}")
            self._send_text(text, "text/plain; charset=utf-8",
                            download_name=f"{parts[1]}.nc")
        elif (len(parts) == 3 and parts[0] == "jobs"
              and parts[2] == "report" and method == "GET"):
            report = self._completed_report(parts[1])
            self._send_json(filter_report(report, query))
        elif (len(parts) == 4 and parts[0] == "jobs"
              and parts[2] == "report" and parts[3] == "download"
              and method == "GET"):
            report = self._completed_report(parts[1])
            payload = filter_report(report, query)
            self._send_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                "application/json; charset=utf-8",
                download_name=f"report_{parts[1]}.json")
        elif (len(parts) == 3 and parts[0] == "jobs"
              and parts[2] in ("report",) and method != "GET"):
            raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "METHOD_NOT_ALLOWED",
                           "仅支持 GET")

        elif parts == ["packages"] and method == "POST":
            self._create_package()
        elif parts == ["packages"] and method == "GET":
            self._send_json({"packages": store.list_packages()})
        elif len(parts) == 2 and parts[0] == "packages" and method == "GET":
            self._package_detail(parts[1])
        elif (len(parts) == 3 and parts[0] == "packages"
              and parts[2] == "report" and method == "GET"):
            report = self._completed_package_report(parts[1])
            self._send_json(filter_package_report(report, query))
        elif (len(parts) == 4 and parts[0] == "packages"
              and parts[2] == "report" and parts[3] == "download"
              and method == "GET"):
            report = self._completed_package_report(parts[1])
            payload = filter_package_report(report, query)
            self._send_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                "application/json; charset=utf-8",
                download_name=f"package_report_{parts[1]}.json")
        elif (len(parts) == 3 and parts[0] == "packages"
              and parts[2] == "blocks" and method == "GET"):
            self._package_blocks(parts[1], query)
        elif (len(parts) == 3 and parts[0] == "packages"
              and parts[2] == "package" and method == "GET"):
            self._package_download(parts[1])

        elif parts == ["package-compare"] and method == "POST":
            self._package_compare()

        elif parts == ["compare"] and method == "POST":
            self._compare()
        elif parts == ["comparisons"] and method == "GET":
            self._send_json({"comparisons": store.list_comparisons()})
        elif len(parts) == 2 and parts[0] == "comparisons" and method == "GET":
            result = store.get_comparison(parts[1])
            if result is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "COMPARISON_NOT_FOUND",
                               f"对比记录不存在: {parts[1]}")
            self._send_json(result)
        else:
            raise ApiError(HTTPStatus.NOT_FOUND, "NOT_FOUND",
                           f"路由不存在: /{rel}")

    # -- 机床 --------------------------------------------------------------

    def _machine_detail(self, method, mid):
        store = self.server.store
        row = store.get_machine(mid)
        if row is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "MACHINE_NOT_FOUND",
                           f"机床配置不存在: {mid}")
        if method == "GET":
            self._send_json(row)
        elif method == "PUT":
            data = self._read_json()
            config = self._config_or_400(data)
            store.save_machine(config, machine_id=mid)
            self._send_json({"id": mid, "config": config.to_dict()})
        elif method == "DELETE":
            store.delete_machine(mid)
            self._send_json({"deleted": mid})
        else:
            raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED,
                           "METHOD_NOT_ALLOWED", "仅支持 GET/PUT/DELETE")

    @staticmethod
    def _config_or_400(data: dict) -> MachineConfig:
        try:
            return MachineConfig.from_dict(data)
        except ConfigError as e:
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_CONFIG",
                           "机床配置校验失败", {"errors": e.errors})

    # -- 同步分析 ----------------------------------------------------------

    def _analyze_inline(self):
        data = self._read_json()
        config = self._resolve_body_config(data)
        gcode = self._extract_gcode(data)
        name = data.get("program_name")
        report = analyze_program(gcode, config, name)
        self._send_json(report)

    # -- 作业 --------------------------------------------------------------

    def _create_job(self):
        data = self._read_json()
        config = self._resolve_body_config(data)
        gcode = self._extract_gcode(data)
        job = self.server.store.create_job(
            gcode, config,
            program_name=data.get("program_name"),
            machine_id=data.get("machine_id")
            if data.get("config") is None else None)
        self._send_json(job, HTTPStatus.ACCEPTED,
                        {"Location": f"/api/jobs/{job['id']}"})

    def _job_detail(self, jid):
        job = self.server.store.get_job(jid)
        if job is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "JOB_NOT_FOUND",
                           f"作业不存在: {jid}")
        self._send_json(job)

    def _completed_report(self, jid):
        job = self.server.store.get_job(jid)
        if job is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "JOB_NOT_FOUND",
                           f"作业不存在: {jid}")
        if job["status"] != "completed":
            raise ApiError(HTTPStatus.CONFLICT, "JOB_NOT_READY",
                           f"作业状态为 {job['status']}，报告尚不可用",
                           {"status": job["status"],
                            "progress": job["progress"]})
        report = self.server.store.get_report(jid)
        if report is None:
            raise ApiError(HTTPStatus.CONFLICT, "JOB_NOT_READY",
                           "报告缺失")
        report["job_id"] = jid
        return report

    # -- 程序包：静态展开 --------------------------------------------------

    def _create_package(self):
        data = self._read_json()
        try:
            spec = parse_package_spec(data)
        except PackageSpecError as e:
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_PACKAGE",
                           "程序包结构校验失败", {"errors": e.errors})
        config = self._resolve_body_config(data)
        job = self.server.store.create_package(
            spec, config,
            machine_id=(data.get("machine_id")
                        if data.get("config") is None else None))
        self._send_json(job, HTTPStatus.ACCEPTED,
                        {"Location": f"/api/packages/{job['id']}"})

    def _package_detail(self, pid):
        pkg = self.server.store.get_package(pid)
        if pkg is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "PACKAGE_NOT_FOUND",
                           f"程序包不存在: {pid}")
        self._send_json(pkg)

    def _completed_package_report(self, pid):
        pkg = self.server.store.get_package(pid)
        if pkg is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "PACKAGE_NOT_FOUND",
                           f"程序包不存在: {pid}")
        if pkg["status"] not in ("completed", "blocked"):
            raise ApiError(HTTPStatus.CONFLICT, "PACKAGE_NOT_READY",
                           f"程序包状态为 {pkg['status']}，报告尚不可用",
                           {"status": pkg["status"],
                            "progress": pkg["progress"]})
        report = self.server.store.get_package_report(pid)
        if report is None:
            raise ApiError(HTTPStatus.CONFLICT, "PACKAGE_NOT_READY",
                           "报告缺失")
        report["package_id"] = pid
        return report

    def _package_blocks(self, pid, query):
        pkg = self.server.store.get_package(pid)
        if pkg is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "PACKAGE_NOT_FOUND",
                           f"程序包不存在: {pid}")
        if pkg["status"] != "completed":
            if pkg["status"] == "blocked":
                code, msg = "PACKAGE_BLOCKED", "程序包被展开错误阻断，无展开块"
            else:
                code, msg = "PACKAGE_NOT_READY", (
                    f"程序包状态为 {pkg['status']}，展开块尚不可用")
            raise ApiError(HTTPStatus.CONFLICT, code, msg,
                           {"status": pkg["status"]})
        limit = _int_param(query, "limit")
        limit = 100 if limit is None else limit
        offset = _int_param(query, "offset")
        offset = 0 if offset is None else offset
        if not (1 <= limit <= 1000):
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_QUERY",
                           "limit 必须为 1..1000")
        if offset < 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_QUERY",
                           "offset 必须 >= 0")
        source = query.get("source", [None])[0]
        if source:
            report = self.server.store.get_package_report(pid)
            valid = {"main"} | {
                s["program"]
                for s in report.get("package", {}).get("subprograms", [])
                if s.get("program")}
            if source not in valid:
                raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_QUERY",
                               f"未知来源程序 {source!r}",
                               {"allowed": sorted(valid)})
        result = self.server.store.get_package_blocks(
            pid, limit, offset, source)
        result["package_id"] = pid
        result["source"] = source
        self._send_json(result)

    def _package_download(self, pid):
        spec = self.server.store.get_package_spec_json(pid)
        if spec is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "PACKAGE_NOT_FOUND",
                           f"程序包不存在: {pid}")
        self._send_text(
            json.dumps(spec, ensure_ascii=False, indent=2),
            "application/json; charset=utf-8",
            download_name=f"package_{pid}.json")

    def _package_compare(self):
        data = self._read_json()
        store = self.server.store
        if "package_a_id" in data or "package_b_id" in data:
            pa, pb = data.get("package_a_id"), data.get("package_b_id")
            if not pa or not pb:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST, "BAD_REQUEST",
                    "按程序包对比需要同时提供 package_a_id 与 package_b_id")
            try:
                result = store.compare_packages(pa, pb)
            except KeyError as e:
                raise ApiError(HTTPStatus.NOT_FOUND, "PACKAGE_NOT_FOUND",
                               str(e))
            except ValueError as e:
                raise ApiError(HTTPStatus.CONFLICT, "PACKAGE_NOT_COMPARABLE",
                               str(e))
        else:
            if not isinstance(data.get("package_a"), dict) or \
                    not isinstance(data.get("package_b"), dict):
                raise ApiError(
                    HTTPStatus.BAD_REQUEST, "MISSING_PACKAGE",
                    "需要 package_a 与 package_b 两个程序包对象，"
                    "或 package_a_id/package_b_id")
            config = self._resolve_body_config(data)
            try:
                spec_a = parse_package_spec(data["package_a"])
                spec_b = parse_package_spec(data["package_b"])
            except PackageSpecError as e:
                raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_PACKAGE",
                               "程序包结构校验失败", {"errors": e.errors})
            la = data.get("label_a", spec_a.name)
            lb = data.get("label_b", spec_b.name)
            try:
                result = store.compare_packages_inline(
                    spec_a, spec_b, config, la, lb)
            except ValueError as e:
                raise ApiError(HTTPStatus.CONFLICT, "PACKAGE_BLOCKED",
                               str(e))
            if data.get("save", False):
                cid = store.save_comparison(
                    None, None, la, lb, result, compare_type="package")
                result["comparison_id"] = cid
        self._send_json(result)

    # -- 对比 --------------------------------------------------------------

    def _compare(self):
        data = self._read_json()
        store = self.server.store
        if "job_a_id" in data or "job_b_id" in data:
            ja, jb = data.get("job_a_id"), data.get("job_b_id")
            if not ja or not jb:
                raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_REQUEST",
                               "按作业对比需要同时提供 job_a_id 与 job_b_id")
            try:
                result = store.compare_jobs(ja, jb)
            except KeyError as e:
                raise ApiError(HTTPStatus.NOT_FOUND, "JOB_NOT_FOUND",
                               str(e))
            except ValueError as e:
                raise ApiError(HTTPStatus.CONFLICT, "CONFIG_MISMATCH", str(e))
        else:
            config = self._resolve_body_config(data)
            a = data.get("gcode_a")
            b = data.get("gcode_b")
            if not isinstance(a, str) or not isinstance(b, str):
                raise ApiError(HTTPStatus.BAD_REQUEST, "MISSING_GCODE",
                               "需要 gcode_a 与 gcode_b 两段 .nc 文本，"
                               "或 job_a_id/job_b_id")
            la = data.get("label_a", "program_a")
            lb = data.get("label_b", "program_b")
            result = store.compare_inline(a, b, config, la, lb)
            if data.get("save", False):
                cid = store.save_comparison(None, None, la, lb, result)
                result["comparison_id"] = cid
        self._send_json(result)

    # -- 请求体公共解析 ----------------------------------------------------

    def _resolve_body_config(self, data: dict) -> MachineConfig:
        store = self.server.store
        if "config" in data and data["config"] is not None:
            return self._config_or_400(data["config"])
        mid = data.get("machine_id")
        if not mid:
            raise ApiError(HTTPStatus.BAD_REQUEST, "MISSING_CONFIG",
                           "需要提供 machine_id 或内联 config")
        row = store.get_machine(mid)
        if row is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "MACHINE_NOT_FOUND",
                           f"机床配置不存在: {mid}")
        return MachineConfig.from_dict(row["config"])

    @staticmethod
    def _extract_gcode(data: dict) -> str:
        gcode = data.get("gcode")
        if not isinstance(gcode, str) or not gcode.strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "MISSING_GCODE",
                           "需要非空的 gcode 字段（.nc 文本）")
        return gcode


# ---------------------------------------------------------------------------
# 服务装配
# ---------------------------------------------------------------------------

def make_server(host: str, port: int, db_path: str,
                verbose: bool = False) -> ThreadingHTTPServer:
    store = JobStore(db_path)

    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    srv = _Server((host, port), Handler)
    srv.store = store
    srv.verbose = verbose
    return srv
