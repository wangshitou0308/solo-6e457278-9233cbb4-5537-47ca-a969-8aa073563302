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

GCODE_DRILL = """G21 G90 G54
M3 S4000
G0 X0 Y0 Z20
G99 G81 R2 Z-8 F250
X20 Y0
X40 Y0
G80
G0 X0 Y40 Z20
G83 R2 Z-11 Q3 F180
X40 Y40
G80
M5
"""

GCODE_PLANES = """G21 G90 G54
M3 S6000
G0 Z20
G0 X40 Y40
G1 Z-2 F300
G2 X40 Y40 I20 J0
G3 X80 Y40 I20 J0 Z-6
G18
G2 X82 Z-4 I0 K2
G3 X122 Z-4 R20 Y80
G19
G3 Y82 Z-6 J2 K0
G3 Y122 Z-6 R20 X160
G17
G2 X200 Y122 I10 J0 R15
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
                          "arc_demo", "plane_arc_demo", "drill_cycle_demo"})
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

    def test_07_drill_cycle_filter_and_example(self):
        # 同步分析钻孔程序
        _, r = self.req("POST", "/api/analyze",
                        {"config": CONFIG, "gcode": GCODE_DRILL})
        dc = r["drill_cycles"]
        self.assertEqual(dc["summary"]["holes_total"], 5)
        self.assertEqual(len(dc["groups"]), 2)
        # 定位动作归入孔
        h2 = dc["groups"][0]["holes"][1]
        self.assertEqual(h2["moves"][0]["action"], "position")
        self.assertEqual(h2["moves"][0]["hole_no"], h2["hole_no"])

        # 建作业以便走 filter_report
        _, job = self.req("POST", "/api/jobs", {
            "machine_id": self.machine_id,
            "program_name": "drill.nc", "gcode": GCODE_DRILL}, expect=202)
        self.wait_job(job["id"])

        # 按孔序筛选 2..3（都在 G81 组）
        _, f = self.req(
            "GET", f"/api/jobs/{job['id']}/report?hole_from=2&hole_to=3")
        s = f["drill_cycles"]["summary"]
        self.assertEqual(s["holes_total"], 2)
        self.assertEqual(s["cycle_groups"], 1)   # G83 组无命中，已剔除
        self.assertEqual(set(f["drill_cycles"]["by_cycle"]), {"G81"})
        g = f["drill_cycles"]["groups"][0]
        self.assertEqual([h["hole_no"] for h in g["holes"]], [2, 3])
        # 汇总钻深 = 2 孔 * 10
        self.assertAlmostEqual(s["total_drill_depth_mm"], 20.0)
        # 轨迹只含孔 2/3 的行与动作
        cyc_entries = [e for e in f["trajectory"]
                       if (e.get("segment") or {}).get("kind")
                       == "canned_cycle"]
        nos = {n for e in cyc_entries
               for n in e["segment"]["hole_nos"]}
        self.assertEqual(nos, {2, 3})
        for e in cyc_entries:
            seg = e["segment"]
            self.assertTrue(all(m.get("hole_no") in {2, 3}
                                for m in seg["moves_mm"]))
            for h in seg["holes"]:
                self.assertTrue(any(m["action"] == "position"
                                    for m in h["moves"]))

        # 按循环类型筛选 G83
        _, fg = self.req(
            "GET", f"/api/jobs/{job['id']}/report?cycle=G83&trajectory=0")
        self.assertEqual(
            [g["cycle"] for g in fg["drill_cycles"]["groups"]], ["G83"])
        self.assertNotIn("trajectory", fg)

        # 非法筛选值
        self.req("GET", f"/api/jobs/{job['id']}/report?cycle=G99",
                 expect=400)
        self.req("GET", f"/api/jobs/{job['id']}/report?hole_from=0",
                 expect=400)

        # 钻孔示例可下载，含固定循环
        resp, nc = self.req("GET", "/api/examples/drill_cycle_demo",
                            raw=True)
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        self.assertIn(b"G83", nc)

    def test_08_drill_compare(self):
        _, cmp = self.req("POST", "/api/compare", {
            "machine_id": self.machine_id,
            "label_a": "d1", "gcode_a": GCODE_DRILL,
            "label_b": "d2",
            "gcode_b": GCODE_DRILL.replace("X40 Y0", "X40 Y0\nX60 Y0")})
        self.assertIn("drill_cycles", cmp)
        dc = cmp["drill_cycles"]
        self.assertEqual(dc["delta"]["holes_total"], 1)
        self.assertEqual(dc["by_cycle"]["G81"]["delta_holes"], 1)

    def test_09_plane_filter_and_arcs(self):
        # 同步分析多平面程序：各平面弧段统计与阻断计数
        _, r = self.req("POST", "/api/analyze",
                        {"config": CONFIG, "gcode": GCODE_PLANES})
        arcs = r["arcs"]
        self.assertEqual(arcs["by_plane"]["G17"]["count"], 2)
        self.assertEqual(arcs["by_plane"]["G18"]["count"], 2)
        self.assertEqual(arcs["by_plane"]["G19"]["count"], 2)
        self.assertEqual(arcs["total"]["helical_count"], 3)
        self.assertEqual(arcs["total"]["full_circle_count"], 1)
        self.assertEqual(arcs["blocked_count"], 1)   # I/J 与 R 混用
        self.assertEqual(r["final_state"]["plane"], "G17")
        codes = [i["code"] for i in r["issues"]]
        self.assertEqual(codes, ["ARC_NO_SOLUTION"])

        # 建作业走 filter_report
        _, job = self.req("POST", "/api/jobs", {
            "machine_id": self.machine_id,
            "program_name": "planes.nc", "gcode": GCODE_PLANES}, expect=202)
        self.wait_job(job["id"])

        # 按平面筛选 G18：arcs 汇总只剩 G18，轨迹中其他平面弧段被剔除
        _, f = self.req(
            "GET", f"/api/jobs/{job['id']}/report?plane=G18")
        self.assertEqual(list(f["arcs"]["by_plane"]), ["G18"])
        self.assertEqual(f["arcs"]["total"]["count"], 2)
        self.assertTrue(f["arcs"]["filtered"])
        self.assertEqual(f["filter"]["plane"], ["G18"])
        arc_entries = [e for e in f["trajectory"]
                       if (e.get("segment") or {}).get("arc")]
        self.assertEqual(
            {e["segment"]["arc"]["plane_code"] for e in arc_entries},
            {"G18"})
        # 阻断弧的问题带平面信息，随筛选保留（G17 的混用问题被滤掉）
        self.assertEqual(f["issues"], [])

        # 多平面 + trajectory=all 不裁剪
        _, f2 = self.req(
            "GET", f"/api/jobs/{job['id']}/report?plane=G18,G19"
                   "&trajectory=all")
        self.assertEqual(set(f2["arcs"]["by_plane"]), {"G18", "G19"})
        self.assertEqual(len(f2["trajectory"]), len(r["trajectory"]))

        # 非法平面
        self.req("GET", f"/api/jobs/{job['id']}/report?plane=G20",
                 expect=400)

        # 对比：候选程序删掉 G18/G19 段，弧段数按平面减少
        _, cmp = self.req("POST", "/api/compare", {
            "machine_id": self.machine_id,
            "label_a": "planes", "gcode_a": GCODE_PLANES,
            "label_b": "g17only",
            "gcode_b": GCODE_PLANES.split("G18")[0] + "G0 Z20\nM5\n"})
        ac = cmp["arcs"]
        self.assertEqual(ac["by_plane"]["G18"]["delta_count"], -2)
        self.assertEqual(ac["by_plane"]["G19"]["delta_count"], -2)
        self.assertEqual(ac["by_plane"]["G17"]["delta_count"], 0)
        self.assertLess(ac["total"]["delta_arc_length_mm"], 0)
        self.assertEqual(ac["blocked"]["delta"], -1)

        # 多平面示例可下载
        resp, nc = self.req("GET", "/api/examples/plane_arc_demo", raw=True)
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        self.assertIn(b"G18", nc)
        self.assertIn(b"G19", nc)

    def test_10_no_endpoint_full_circle_and_plane_issue_filter(self):
        # 无 XYZ 终点词的圆心整圆生成完整弧段
        _, r = self.req("POST", "/api/analyze", {
            "config": CONFIG,
            "gcode": "G21 G90 G54\nM3 S1000\nG0 X20 Y20 Z5\nG2 I10 J0 F500\n"})
        e = [t for t in r["trajectory"] if t["line_no"] == 4][0]
        self.assertEqual(e["type"], "arc_cw")
        self.assertTrue(e["segment"]["arc"]["full_circle"])
        self.assertAlmostEqual(e["segment"]["length_mm"],
                               2 * 3.141592653589793 * 10, places=4)
        self.assertEqual(r["arcs"]["total"]["full_circle_count"], 1)

        # 含 OUT_OF_BOUNDS 的 G18 作业：平面筛选后问题与风险计数保留
        cfg2 = {"name": "oob", "travel_x": [0, 100], "travel_y": [0, 100],
                "travel_z": [-10, 14], "safe_z": 2,
                "max_feed_mm_min": 3000, "max_spindle_rpm": 12000}
        bad = ("G21 G90 G54\nM3 S1000\nG0 X50 Y10 Z5\n"
               "G18\nG2 X30 Z5 I-10 K0 F500\n")   # 弧顶 Z=15 越出 z_max=14
        _, job = self.req("POST", "/api/jobs",
                          {"config": cfg2, "gcode": bad}, expect=202)
        self.wait_job(job["id"])
        _, full = self.req("GET", f"/api/jobs/{job['id']}/report")
        self.assertEqual(full["risk"]["counts_by_severity"]["critical"], 1)

        _, f = self.req("GET", f"/api/jobs/{job['id']}/report?plane=G18")
        self.assertEqual([i["code"] for i in f["issues"]], ["OUT_OF_BOUNDS"])
        self.assertEqual(f["issues"][0]["details"]["plane"], "G18")
        self.assertEqual(f["risk"]["counts_by_severity"]["critical"], 1)
        self.assertEqual(f["risk"]["total_issues"], 1)
        arc_entries = [e for e in f["trajectory"]
                       if (e.get("segment") or {}).get("arc")]
        self.assertEqual(arc_entries[0]["issue_codes"], ["OUT_OF_BOUNDS"])

        # 滤其他平面：该弧段问题不出现
        _, f2 = self.req("GET", f"/api/jobs/{job['id']}/report?plane=G17")
        self.assertEqual(f2["issues"], [])

        # 对比的分平面问题增减（坏 -> 好：解决 1；好 -> 坏：新增 1）
        good = ("G21 G90 G54\nM3 S1000\nG0 X50 Y10 Z5\n"
                "G18\nG2 X40 Z5 I-5 K0 F500\n")
        _, cmp = self.req("POST", "/api/compare", {
            "config": cfg2, "label_a": "bad", "gcode_a": bad,
            "label_b": "good", "gcode_b": good})
        g18 = cmp["arcs"]["by_plane"]["G18"]
        self.assertEqual(g18["resolved_issues"], 1)
        self.assertEqual(g18["introduced_issues"], 0)
        self.assertEqual(g18["delta_issues"], -1)
        self.assertEqual(cmp["arcs"]["total"]["resolved_issues"], 1)
        _, cmp2 = self.req("POST", "/api/compare", {
            "config": cfg2, "label_a": "good", "gcode_a": good,
            "label_b": "bad", "gcode_b": bad})
        self.assertEqual(cmp2["arcs"]["by_plane"]["G18"]["introduced_issues"],
                         1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
