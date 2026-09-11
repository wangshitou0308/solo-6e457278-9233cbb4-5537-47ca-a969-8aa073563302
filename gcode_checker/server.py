"""基于 http.server 的本地 REST API（仅标准库，断网可用）。

路由概览：
  GET    /api/health                     健康检查
  GET    /api/dialect                    支持的指令方言 / 严重度定义
  GET    /api/docs                       API 文档（Markdown 文本）
  GET    /api/examples                   示例程序清单
  GET    /api/examples/<name>            下载示例 .nc 文件
  GET/POST /api/machines                 列出 / 新建机床配置
  GET/PUT/DELETE /api/machines/<id>      查询 / 更新 / 删除配置
  POST   /api/analyze                    同步分析（不落库，立即返回报告）
  POST   /api/jobs                       创建分析作业（后台执行）
  GET    /api/jobs                       作业列表
  GET    /api/jobs/<id>                  查询作业进度与概要
  GET    /api/jobs/<id>/report           完整 JSON 报告（可按严重度/代码筛选）
  GET    /api/jobs/<id>/report/download  下载 JSON 报告（attachment）
  GET    /api/jobs/<id>/gcode            取作业原始 .nc 文本
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
    analyze_program,
)
from .database import JobStore
from .examples import get_example, list_examples
from .compare import compare_reports

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
    """按 severity / code / 行范围筛选问题；其余统计同步重算。"""
    severities = _csv_param(query, "severity")
    codes = _csv_param(query, "code")
    line_from = _int_param(query, "line_from")
    line_to = _int_param(query, "line_to")

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

    issues = report["issues"]
    if severities:
        issues = [i for i in issues if i["severity"] in severities]
    if codes:
        issues = [i for i in issues if i["code"] in codes]
    if line_from is not None:
        issues = [i for i in issues if i["line_no"] >= line_from]
    if line_to is not None:
        issues = [i for i in issues if i["line_no"] <= line_to]

    out = dict(report)
    out["issues"] = issues
    counts = {s: 0 for s in SEVERITY_ORDER}
    for i in issues:
        counts[i["severity"]] += 1
    out["risk"] = dict(report["risk"])
    out["risk"]["counts_by_severity"] = counts
    out["risk"]["total_issues"] = len(issues)
    out["filter"] = {
        "severity": severities, "code": codes,
        "line_from": line_from, "line_to": line_to,
        "matched": len(issues),
        "total_in_report": len(report["issues"]),
    }
    if "trajectory" in query:
        # 默认携带逐行轨迹，体量较大；?trajectory=0 可省略
        if query.get("trajectory", ["1"])[0] in ("0", "false", "no"):
            out.pop("trajectory", None)
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
            self._send_json({
                "dialect": DIALECT,
                "issue_titles": ISSUE_TITLE,
                "issue_severity": ISSUE_SEVERITY,
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
            self._send_text(content, "text/plain; charset=utf-8",
                            download_name=filename)

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
