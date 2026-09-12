"""SQLite 持久化与后台作业管理。

表：
- machines:    机床配置
- jobs:        分析作业（含 .nc 原文、状态、进度、完整 JSON 报告）
- packages:    程序包静态展开作业（主程序 + O 号子程序集、调用图、
                展开状态、展开块分页、分析结果）
- comparisons: 双程序/双程序包风险对比结果

作业分析在后台线程执行（单工作线程，保证 sqlite 写入串行）。
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading

from .analyzer import (
    MachineConfig,
    ConfigError,
    analyze_program,
    new_id,
    utc_now_iso,
)
from .compare import compare_reports, compare_package_reports
from .packages import (
    PackageSpec,
    PackageSpecError,
    analyze_package,
    parse_package_spec,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    program_name TEXT,
    machine_id   TEXT,
    config_json  TEXT NOT NULL,
    gcode_text   TEXT NOT NULL,
    status       TEXT NOT NULL,
    progress     INTEGER NOT NULL DEFAULT 0,
    report_json  TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS packages (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    machine_id    TEXT,
    config_json   TEXT NOT NULL,
    spec_json     TEXT NOT NULL,
    status        TEXT NOT NULL,
    progress      INTEGER NOT NULL DEFAULT 0,
    report_json   TEXT,
    error         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS package_blocks (
    package_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    program    TEXT NOT NULL,
    file       TEXT,
    line_no    INTEGER NOT NULL,
    source_line TEXT NOT NULL,
    depth      INTEGER NOT NULL,
    repeat_index INTEGER NOT NULL,
    repeat_total INTEGER NOT NULL,
    call_stack_json TEXT NOT NULL,
    PRIMARY KEY (package_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_package_blocks_program
    ON package_blocks (package_id, program, seq);
CREATE TABLE IF NOT EXISTS comparisons (
    id          TEXT PRIMARY KEY,
    label_a     TEXT NOT NULL,
    label_b     TEXT NOT NULL,
    job_a_id    TEXT,
    job_b_id    TEXT,
    compare_type TEXT NOT NULL DEFAULT 'program',
    result_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""

VALID_STATUSES = {"queued", "running", "completed", "failed", "blocked"}


class JobStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()
        # 队列元素 (kind, id)：("job", id) | ("package", id)
        self._queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._worker = threading.Thread(
            target=self._worker_loop, name="gcode-worker", daemon=True)
        self._worker.start()

    # -- 基础 --------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            # 旧库迁移：comparisons 增加 compare_type
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(comparisons)")}
            if cols and "compare_type" not in cols:
                conn.execute(
                    "ALTER TABLE comparisons ADD COLUMN compare_type TEXT "
                    "NOT NULL DEFAULT 'program'")

    # -- 机床配置 ----------------------------------------------------------

    def save_machine(self, config: MachineConfig, machine_id: str | None = None
                     ) -> str:
        mid = machine_id or new_id()
        now = utc_now_iso()
        cfg_json = json.dumps(config.to_dict(), ensure_ascii=False)
        with self._lock, self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM machines WHERE id=?", (mid,)).fetchone()
            if exists:
                conn.execute(
                    "UPDATE machines SET name=?, config_json=?, updated_at=? "
                    "WHERE id=?", (config.name, cfg_json, now, mid))
            else:
                conn.execute(
                    "INSERT INTO machines (id,name,config_json,created_at,"
                    "updated_at) VALUES (?,?,?,?,?)",
                    (mid, config.name, cfg_json, now, now))
        return mid

    def get_machine(self, machine_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM machines WHERE id=?", (machine_id,)).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "config": json.loads(row["config_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_machines(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,name,config_json,created_at,updated_at FROM machines "
                "ORDER BY created_at").fetchall()
        return [{
            "id": r["id"], "name": r["name"],
            "config": json.loads(r["config_json"]),
            "created_at": r["created_at"], "updated_at": r["updated_at"],
        } for r in rows]

    def delete_machine(self, machine_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM machines WHERE id=?", (machine_id,))
            return cur.rowcount > 0

    def resolve_config(self, machine_id: str | None,
                       config_dict: dict | None) -> MachineConfig:
        """machine_id 与内联 config 二选一（同时给则内联优先）。"""
        if config_dict is not None:
            return MachineConfig.from_dict(config_dict)
        if machine_id:
            row = self.get_machine(machine_id)
            if not row:
                raise KeyError(f"机床配置不存在: {machine_id}")
            return MachineConfig.from_dict(row["config"])
        raise ConfigError(["必须提供 machine_id 或内联 config"])

    # -- 作业 --------------------------------------------------------------

    def create_job(self, gcode_text: str, config: MachineConfig,
                   program_name: str | None = None,
                   machine_id: str | None = None) -> dict:
        jid = new_id()
        now = utc_now_iso()
        cfg_json = json.dumps(config.to_dict(), ensure_ascii=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id,program_name,machine_id,config_json,"
                "gcode_text,status,progress,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,0,?,?)",
                (jid, program_name, machine_id, cfg_json, gcode_text,
                 "queued", now, now))
        self._queue.put(("job", jid))
        return self.get_job(jid)

    def get_job(self, job_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?",
                               (job_id,)).fetchone()
        if not row:
            return None
        return self._job_summary(row)

    def list_jobs(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [self._job_summary(r) for r in rows]

    def get_report(self, job_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT report_json,status FROM jobs WHERE id=?",
                (job_id,)).fetchone()
        if not row:
            return None
        if row["status"] != "completed" or not row["report_json"]:
            return None
        return json.loads(row["report_json"])

    def get_gcode(self, job_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT gcode_text FROM jobs WHERE id=?",
                (job_id,)).fetchone()
        return row["gcode_text"] if row else None

    @staticmethod
    def _job_summary(row: sqlite3.Row) -> dict:
        d = {
            "id": row["id"],
            "program_name": row["program_name"],
            "machine_id": row["machine_id"],
            "status": row["status"],
            "progress": row["progress"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if row["report_json"]:
            report = json.loads(row["report_json"])
            d["risk"] = report["risk"]
            d["program"] = report["program"]
        return d

    # -- 后台执行 ----------------------------------------------------------

    def _worker_loop(self):
        while True:
            kind, item_id = self._queue.get()
            try:
                if kind == "job":
                    self._run_job(item_id)
                else:
                    self._run_package(item_id)
            except Exception as e:  # pragma: no cover - 兜底
                self._set_failed(
                    item_id, f"内部错误: {e!r}",
                    table="packages" if kind == "package" else "jobs")
            finally:
                self._queue.task_done()

    def _run_job(self, job_id: str):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?",
                               (job_id,)).fetchone()
        if not row:
            return
        self._update_status(job_id, status="running", progress=1)
        config = MachineConfig.from_dict(json.loads(row["config_json"]))

        last_progress = [-1]

        def on_progress(pct: int):
            # 节流：每变化 >=2% 写一次库
            if pct - last_progress[0] >= 2 or pct == 100:
                last_progress[0] = pct
                self._update_status(job_id, progress=pct)

        try:
            report = analyze_program(
                row["gcode_text"], config,
                program_name=row["program_name"], progress=on_progress)
        except ConfigError as e:
            self._set_failed(job_id, "配置无效: " + "; ".join(e.errors))
            return
        except Exception as e:  # pragma: no cover
            self._set_failed(job_id, f"分析失败: {e!r}")
            return

        report_json = json.dumps(report, ensure_ascii=False)
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status='completed',progress=100,"
                "report_json=?,updated_at=? WHERE id=?",
                (report_json, now, job_id))

    def _update_status(self, job_id: str, status: str | None = None,
                       progress: int | None = None, table: str = "jobs"):
        sets, params = [], []
        if status is not None:
            sets.append("status=?")
            params.append(status)
        if progress is not None:
            sets.append("progress=?")
            params.append(progress)
        sets.append("updated_at=?")
        params.append(utc_now_iso())
        params.append(job_id)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE id=?",
                         params)

    def _set_failed(self, job_id: str, error: str, table: str = "jobs"):
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE {table} SET status='failed',error=?,updated_at=? "
                "WHERE id=?",
                (error, utc_now_iso(), job_id))

    # -- 程序包：静态展开作业 ----------------------------------------------

    def create_package(self, spec: PackageSpec, config: MachineConfig,
                       machine_id: str | None = None) -> dict:
        pid = new_id()
        now = utc_now_iso()
        spec_json = json.dumps(self._spec_to_json(spec), ensure_ascii=False)
        cfg_json = json.dumps(config.to_dict(), ensure_ascii=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO packages (id,name,machine_id,config_json,spec_json,"
                "status,progress,created_at,updated_at) "
                "VALUES (?,?,?,?,?, 'queued',0,?,?)",
                (pid, spec.name, machine_id, cfg_json, spec_json, now, now))
        self._queue.put(("package", pid))
        return self.get_package(pid)

    @staticmethod
    def _spec_to_json(spec: PackageSpec) -> dict:
        return {
            "name": spec.name,
            "main": spec.main_text,
            "main_file": spec.main_file,
            "subprograms": spec.subprograms,
            "max_depth": spec.max_depth,
        }

    def get_package(self, package_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM packages WHERE id=?",
                               (package_id,)).fetchone()
        if not row:
            return None
        return self._package_summary(row)

    def list_packages(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM packages ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [self._package_summary(r) for r in rows]

    @staticmethod
    def _package_summary(row: sqlite3.Row) -> dict:
        d = {
            "id": row["id"],
            "name": row["name"],
            "machine_id": row["machine_id"],
            "status": row["status"],
            "progress": row["progress"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if row["report_json"]:
            report = json.loads(row["report_json"])
            pkg = report.get("package") or {}
            expansion = pkg.get("expansion", {})
            d["expansion"] = expansion
            d["call_graph"] = pkg.get("call_graph")
            if report.get("blocked"):
                d["blocked"] = True
                d["expansion_errors"] = report.get("expansion_errors", [])
            else:
                d["risk"] = report["risk"]
        return d

    def get_package_report(self, package_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT report_json,status FROM packages WHERE id=?",
                (package_id,)).fetchone()
        if not row or not row["report_json"]:
            return None
        return json.loads(row["report_json"])

    def get_package_spec_json(self, package_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT spec_json FROM packages WHERE id=?",
                               (package_id,)).fetchone()
        if not row:
            return None
        return json.loads(row["spec_json"])

    def get_package_blocks(self, package_id: str, limit: int, offset: int,
                           source: str | None = None) -> dict:
        """分页读取展开块预览（只含来源/调用栈元数据，不附状态快照）。"""
        where = "WHERE package_id=?"
        params: list = [package_id]
        if source:
            where += " AND program=?"
            params.append(source)
        with self._connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM package_blocks {where}",
                params).fetchone()[0]
            rows = conn.execute(
                f"SELECT seq,program,file,line_no,source_line,depth,"
                f"repeat_index,repeat_total,call_stack_json "
                f"FROM package_blocks {where} ORDER BY seq LIMIT ? OFFSET ?",
                params + [limit, offset]).fetchall()
        blocks = [{
            "seq": r["seq"],
            "program": r["program"],
            "file": r["file"],
            "line_no": r["line_no"],
            "source_line": r["source_line"],
            "depth": r["depth"],
            "repeat_index": r["repeat_index"],
            "repeat_total": r["repeat_total"],
            "call_stack": json.loads(r["call_stack_json"]),
        } for r in rows]
        return {"total": total, "limit": limit, "offset": offset,
                "count": len(blocks),
                "has_more": offset + len(blocks) < total,
                "blocks": blocks}

    def _run_package(self, package_id: str):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM packages WHERE id=?",
                               (package_id,)).fetchone()
        if not row:
            return
        self._update_status(package_id, status="running", progress=1,
                            table="packages")
        config = MachineConfig.from_dict(json.loads(row["config_json"]))
        try:
            spec = parse_package_spec(json.loads(row["spec_json"]))
        except PackageSpecError as e:
            self._set_failed(package_id, "程序包无效: " + "; ".join(e.errors),
                             table="packages")
            return

        # 展开与预检查（通常很快）；展开块分析在 Analyzer 中按块报进度
        self._update_status(package_id, progress=5, table="packages")
        last_progress = [5]

        def on_progress(pct: int):
            scaled = 5 + int(pct * 0.95)
            if scaled - last_progress[0] >= 2 or scaled == 100:
                last_progress[0] = scaled
                self._update_status(package_id, progress=scaled,
                                    table="packages")

        try:
            report = analyze_package(spec, config, progress=on_progress)
        except ConfigError as e:
            self._set_failed(package_id,
                             "配置无效: " + "; ".join(e.errors),
                             table="packages")
            return
        except Exception as e:  # pragma: no cover
            self._set_failed(package_id, f"展开失败: {e!r}",
                             table="packages")
            return

        status = "blocked" if report.get("blocked") else "completed"
        report_json = json.dumps(report, ensure_ascii=False)
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE packages SET status=?,progress=100,report_json=?,"
                "updated_at=? WHERE id=?",
                (status, report_json, now, package_id))
            if status == "completed":
                self._save_package_blocks(conn, package_id, spec, report)

    def _save_package_blocks(self, conn, package_id: str, spec: PackageSpec,
                             report: dict):
        """落库展开块（来源/原行/调用栈），供分页预览接口使用。"""
        conn.execute("DELETE FROM package_blocks WHERE package_id=?",
                     (package_id,))
        rows = []
        for seq, e in enumerate(report.get("trajectory", [])):
            rows.append((
                package_id,
                seq,
                e.get("source_program", "main"),
                e.get("source_file"),
                e["line_no"], e["source_line"],
                e.get("depth", 0),
                e.get("repeat_index", 0),
                e.get("repeat_total", 1),
                json.dumps(e.get("call_stack", []), ensure_ascii=False),
            ))
        conn.executemany(
            "INSERT INTO package_blocks (package_id,seq,program,file,line_no,"
            "source_line,depth,repeat_index,repeat_total,call_stack_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)

    def compare_packages(self, pkg_a_id: str, pkg_b_id: str,
                         save: bool = True) -> dict:
        """用两个已完成程序包的报告对比（同一机床配置）。"""
        r_a, r_b = (self.get_package_report(pkg_a_id),
                    self.get_package_report(pkg_b_id))
        if r_a is None or r_b is None:
            raise KeyError("两个程序包必须存在且已完成展开")
        if r_a.get("blocked") or r_b.get("blocked"):
            raise ValueError("存在被展开错误阻断的程序包，无法对比安全结论")
        cfg_a = json.dumps(r_a["machine"], sort_keys=True)
        cfg_b = json.dumps(r_b["machine"], sort_keys=True)
        if cfg_a != cfg_b:
            raise ValueError("两个程序包的机床配置不一致，对比必须使用同一配置")
        la = r_a.get("package", {}).get("name") or pkg_a_id
        lb = r_b.get("package", {}).get("name") or pkg_b_id
        result = compare_package_reports(r_a, r_b, la, lb)
        result["package_a_id"] = pkg_a_id
        result["package_b_id"] = pkg_b_id
        if save:
            cid = self.save_comparison(pkg_a_id, pkg_b_id, la, lb, result,
                                       compare_type="package")
            result["comparison_id"] = cid
        return result

    def compare_packages_inline(self, spec_a: PackageSpec,
                                spec_b: PackageSpec,
                                config: MachineConfig,
                                label_a: str, label_b: str) -> dict:
        """内联程序包直接对比（同步执行）。被阻断的程序包按 409 返回由
        服务层处理。"""
        r_a = analyze_package(spec_a, config)
        r_b = analyze_package(spec_b, config)
        if r_a.get("blocked") or r_b.get("blocked"):
            raise ValueError("存在被展开错误阻断的程序包，无法对比安全结论")
        return compare_package_reports(r_a, r_b, label_a, label_b)

    # -- 对比 --------------------------------------------------------------

    def compare_inline(self, text_a: str, text_b: str,
                       config: MachineConfig,
                       label_a: str = "program_a",
                       label_b: str = "program_b") -> dict:
        """同一配置、内联文本直接对比（同步执行）。"""
        r_a = analyze_program(text_a, config, label_a)
        r_b = analyze_program(text_b, config, label_b)
        return compare_reports(r_a, r_b, label_a, label_b)

    def compare_jobs(self, job_a_id: str, job_b_id: str,
                     save: bool = True) -> dict:
        """用两个已完成作业的报告对比。"""
        r_a, r_b = self.get_report(job_a_id), self.get_report(job_b_id)
        if r_a is None or r_b is None:
            raise KeyError("两个作业必须存在且已完成分析")
        cfg_a = json.dumps(r_a["machine"], sort_keys=True)
        cfg_b = json.dumps(r_b["machine"], sort_keys=True)
        if cfg_a != cfg_b:
            raise ValueError("两个作业的机床配置不一致，风险对比必须使用同一配置")
        la = r_a.get("program", {}).get("name") or job_a_id
        lb = r_b.get("program", {}).get("name") or job_b_id
        result = compare_reports(r_a, r_b, la, lb)
        result["job_a_id"] = job_a_id
        result["job_b_id"] = job_b_id
        if save:
            self.save_comparison(job_a_id, job_b_id, la, lb, result)
        return result

    def save_comparison(self, job_a_id, job_b_id, label_a, label_b,
                        result: dict, compare_type: str = "program") -> str:
        cid = new_id()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO comparisons (id,label_a,label_b,job_a_id,job_b_id,"
                "compare_type,result_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (cid, label_a, label_b, job_a_id, job_b_id, compare_type,
                 json.dumps(result, ensure_ascii=False), utc_now_iso()))
        return cid

    def get_comparison(self, comparison_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM comparisons WHERE id=?",
                               (comparison_id,)).fetchone()
        if not row:
            return None
        result = json.loads(row["result_json"])
        result["comparison_id"] = row["id"]
        result["created_at"] = row["created_at"]
        result["compare_type"] = (row["compare_type"]
                                  if "compare_type" in row.keys()
                                  else "program")
        return result

    def list_comparisons(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,label_a,label_b,job_a_id,job_b_id,compare_type,"
                "created_at FROM comparisons ORDER BY created_at DESC"
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if "compare_type" not in d:
                d["compare_type"] = "program"
            out.append(d)
        return out
