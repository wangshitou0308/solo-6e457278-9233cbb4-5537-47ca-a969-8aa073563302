"""HTTP API 端到端测试：真实启动 ThreadingHTTPServer + urllib 客户端。

运行：python3 -m tests.test_api
"""

import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile  # noqa: F401  (保留：未来打包示例时使用)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gcode_checker.server import make_server

CONFIG = {
    "name": "e2e",
    "travel_x": [0, 300], "travel_y": [0, 200], "travel_z": [-50, 60],
    "safe_z": 2,
    "max_feed_mm_min": 3000, "max_spindle_rpm": 12000,
}

GCODE_BAD = """G54
G1 X5
G21 G90
M3 S1000
G0 Z20
G0 X0 Y0
G1 Z-2 F300
M5
G1 X10 Y10 F9000
G2 X30 Y30 I0 J0
G0 Z-5
G55 X-
"""

GCODE_GOOD = """G21 G90 G54
M3 S6000
G0 Z20
G0 X0 Y0
G1 Z-1 F500
G1 X20 Y5
G0 Z20
M5
"""


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db = os.path.join(cls.tmp.name, "test.db")
        cls.server = make_server("127.0.0.1", 0, db)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    # -- 工具 --------------------------------------------------------------

    def req(self, method, path, body=None, expect=None, raw=False,
            headers=None):
        data = None
        hdrs = headers or {}
        if body is not None:
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        r = urllib.request.Request(self.base + path, data=data,
                                   headers=hdrs, method=method)
        try:
            resp = urllib.request.urlopen(r, timeout=10)
        except urllib.error.HTTPError as e:
            payload = e.read().decode()
            if expect is not None and e.code == expect:
                return e, payload
            self.fail(f"{method} {path} -> {e.code}: {payload}")
        payload = resp.read()
        if expect is not None:
            self.assertEqual(resp.status, expect)
        if raw:
            return resp, payload
        return resp, json.loads(payload.decode())

    def wait_job(self, jid, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, job = self.req("GET", f"/api/jobs/{jid}")
            if job["status"] in ("completed", "failed"):
                return job
            time.sleep(0.02)
        self.fail("作业超时未完成")

    # -- 测试 --------------------------------------------------------------

    def test_01_health_dialect_docs_examples(self):
        _, h = self.req("GET", "/api/health")
        self.assertTrue(h["offline"])
        _, d = self.req("GET", "/api/dialect")
        self.assertIn("G21", d["dialect"]["supported_g"])
        resp, text = self.req("GET", "/api/docs", raw=True)
        self.assertIn("text/markdown", resp.headers["Content-Type"])
        self.assertIn("G-code", text.decode())
        _, ex = self.req("GET", "/api/examples")
        names = [e["name"] for e in ex["examples"]]
        self.assertEqual(set(names),
                         {"safe_demo", "problems_demo", "inch_demo",
                          "arc_demo"})
        resp, nc = self.req("GET", "/api/examples/safe_demo", raw=True)
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        self.assertIn(b"G21", nc)

    def test_02_machine_crud_and_validation(self):
        _, m = self.req("POST", "/api/machines", CONFIG, expect=201)
        self.__class__.machine_id = m["id"]
        _, one = self.req("GET", f"/api/machines/{m['id']}")
        self.assertEqual(one["config"]["x_max"], 300)
        bad = dict(CONFIG, max_feed_mm_min=-1)
        _, err = self.req("POST", "/api/machines", bad, expect=400)
        self.assertEqual(json.loads(err)["error"]["code"], "BAD_CONFIG")
        _, missing = self.req("POST", "/api/analyze", {"gcode": "G1 X1"},
                              expect=400)
        self.assertEqual(json.loads(missing)["error"]["code"],
                         "MISSING_CONFIG")
        self.req("DELETE", f"/api/machines/{m['id']}", expect=200)
        # 重建供后续使用
        _, m = self.req("POST", "/api/machines", CONFIG, expect=201)
        self.__class__.machine_id = m["id"]

    def test_03_sync_analyze_inline(self):
        _, r = self.req("POST", "/api/analyze",
                        {"config": CONFIG, "gcode": GCODE_BAD})
        codes = [i["code"] for i in r["issues"]]
        self.assertIn("SPINDLE_NOT_RUNNING", codes)
        self.assertIn("FEED_OVER_LIMIT", codes)
        self.assertIn("RAPID_BELOW_SAFE_Z", codes)
        # G55 X-：残缺 + 未支持 同时出现
        self.assertIn("MALFORMED_LINE", codes)
        self.assertIn("UNSUPPORTED_INSTRUCTION", codes)
        line = [i for i in r["issues"] if i["code"] == "MALFORMED_LINE"][0]
        self.assertIn("state_in", line)
        self.assertIn("state_out", line)
        self.assertTrue(line["basis"])

    def test_04_job_lifecycle_and_filter(self):
        body = {"machine_id": self.machine_id,
                "program_name": "bad.nc", "gcode": GCODE_BAD}
        _, job = self.req("POST", "/api/jobs", body, expect=202)
        jid = job["id"]
        self.assertIn(job["status"], ("queued", "running"))
        done = self.wait_job(jid)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["progress"], 100)
        self.assertGreater(done["risk"]["total_issues"], 0)

        # 全量报告
        _, full = self.req("GET", f"/api/jobs/{jid}/report")
        total = len(full["issues"])

        # 按严重度筛选
        _, crit = self.req(
            "GET", f"/api/jobs/{jid}/report?severity=critical,error")
        self.assertTrue(all(i["severity"] in ("critical", "error")
                            for i in crit["issues"]))
        self.assertLess(len(crit["issues"]), total)
        self.assertEqual(crit["filter"]["matched"], len(crit["issues"]))

        # 按代码筛选 + 去掉轨迹
        _, one_kind = self.req(
            "GET", f"/api/jobs/{jid}/report?code=OUT_OF_BOUNDS"
                   "&trajectory=0")
        self.assertTrue(all(i["code"] == "OUT_OF_BOUNDS"
                            for i in one_kind["issues"]))
        self.assertNotIn("trajectory", one_kind)

        # 非法筛选值
        self.req("GET", f"/api/jobs/{jid}/report?severity=bogus",
                 expect=400)

        # 下载
        resp, blob = self.req(
            "GET", f"/api/jobs/{jid}/report/download?severity=critical",
            raw=True)
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        dl = json.loads(blob)
        self.assertTrue(all(i["severity"] == "critical"
                            for i in dl["issues"]))

        # 原始 gcode
        resp, nc = self.req("GET", f"/api/jobs/{jid}/gcode", raw=True)
        self.assertIn(b"G55", nc)

        # 未就绪 / 不存在
        self.req("GET", "/api/jobs/nope/report", expect=404)
        self.__class__.bad_job = jid

    def test_05_compare_inline_and_jobs(self):
        # 内联对比
        _, cmp = self.req("POST", "/api/compare", {
            "machine_id": self.machine_id,
            "label_a": "bad", "gcode_a": GCODE_BAD,
            "label_b": "good", "gcode_b": GCODE_GOOD,
            "save": True})
        self.assertGreater(cmp["issue_counts"]["resolved"], 0)
        self.assertEqual(cmp["issue_counts"]["introduced"], 0)
        self.assertLess(cmp["risk"]["score_delta"], 0)
        self.assertIn("by_code", cmp)
        cid = cmp["comparison_id"]
        _, saved = self.req("GET", f"/api/comparisons/{cid}")
        self.assertEqual(saved["comparison_id"], cid)
        _, lst = self.req("GET", "/api/comparisons")
        self.assertTrue(lst["comparisons"])

        # 按作业对比：先给好程序建作业
        _, good_job = self.req("POST", "/api/jobs", {
            "machine_id": self.machine_id,
            "program_name": "good.nc", "gcode": GCODE_GOOD}, expect=202)
        self.wait_job(good_job["id"])
        _, cmp2 = self.req("POST", "/api/compare", {
            "job_a_id": self.bad_job, "job_b_id": good_job["id"]})
        self.assertEqual(cmp2["issue_counts"]["introduced"], 0)

    def test_06_last_modal_gcode_same_line(self):
        nc = "G20 G21 G90 G91 G54\nG90 G0 X0\nG1 X10\n"
        _, r = self.req("POST", "/api/analyze",
                        {"config": CONFIG, "gcode": nc})
        st = r["final_state"]
        self.assertEqual(st["unit"], "mm")
        self.assertEqual(st["distance_mode"], "absolute")
        self.assertEqual(r["trajectory"][0]["normalized"], "G21 G91 G54")
        self.assertAlmostEqual(st["x"]["value_mm"], 10.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
