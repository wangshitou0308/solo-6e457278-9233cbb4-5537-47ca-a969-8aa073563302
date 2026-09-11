"""SQLite 持久化与后台作业管理。

表：
- machines:    机床配置
- jobs:        分析作业（含 .nc 原文、状态、进度、完整 JSON 报告）
- comparisons: 双程序风险对比结果

作业分析在后台线程执行（单工作线程，保证 sqlite 写入串行）。
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
from typing import Callable

from .analyzer import (
    MachineConfig,
    ConfigError,
    analyze_program,
    new_id,
    utc_now_iso,
)
from .compare import compare_reports

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
CREATE TABLE IF NOT EXISTS comparisons (
    id          TEXT PRIMARY KEY,
    label_a     TEXT NOT NULL,
    label_b     TEXT NOT NULL,
    job_a_id    TEXT,
    job_b_id    TEXT,
    result_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""

VALID_STATUSES = {"queued", "running", "completed", "failed"}


class JobStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()
        self._queue: "queue.Queue[str]" = queue.Queue()
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
        self._queue.put(jid)
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
            jid = self._queue.get()
            try:
                self._run_job(jid)
            except Exception as e:  # pragma: no cover - 兜底
                self._set_failed(jid, f"内部错误: {e!r}")
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
                       progress: int | None = None):
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
            conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?",
                         params)

    def _set_failed(self, job_id: str, error: str):
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status='failed',error=?,updated_at=? WHERE id=?",
                (error, utc_now_iso(), job_id))

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
                        result: dict) -> str:
        cid = new_id()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO comparisons (id,label_a,label_b,job_a_id,job_b_id,"
                "result_json,created_at) VALUES (?,?,?,?,?,?,?)",
                (cid, label_a, label_b, job_a_id, job_b_id,
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
        return result

    def list_comparisons(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,label_a,label_b,job_a_id,job_b_id,created_at "
                "FROM comparisons ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
