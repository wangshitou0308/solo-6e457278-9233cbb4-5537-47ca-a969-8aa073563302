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
G54.1 X-
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

PACKAGE_GOOD = {
    "name": "pkg_good",
    "main": (
        "G21 G90 G54\n"
        "M3 S4000\n"
        "G0 X0 Y0 Z20\n"
        "M98 P100 L2\n"
        "G0 X0 Y60 Z20\n"
        "M98 P200\n"
        "G0 Z50\n"
        "M30\n"),
    "subprograms": [
        {"name": "o100.nc",
         "content": ("O100 (drill row)\n"
                     "G91 G99 G81 X20 Z-10 R-18 L3 F250\n"
                     "G90 G80\n"
                     "M99\n")},
        {"name": "o200.nc",
         "content": ("O200 (second station, nested call)\n"
                     "G0 X100 Y60\n"
                     "M98 P100\n"
                     "G0 X0 Y60\n"
                     "M99\n")},
    ],
}

PACKAGE_BAD = {
    "name": "pkg_bad",
    "main": (
        "G21 G90 G54\n"
        "M99\n"                  # M99 in main
        "M98 P100\n"
        "M98 P999\n"             # missing target
        "M98 P#200\n"            # dynamic P
        "M30\n"),
    "subprograms": [
        {"name": "o100.nc", "content": "O100\nM98 P100\nM99\n"},
        {"name": "o300a.nc", "content": "O300\nM99\n"},
        {"name": "o300b.nc", "content": "O300\nM99\n"},
    ],
}


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
        self.assertIn("G55", d["dialect"]["supported_g"])
        self.assertIn("G59", d["dialect"]["supported_g"])
        resp, text = self.req("GET", "/api/docs", raw=True)
        self.assertIn("text/markdown", resp.headers["Content-Type"])
        self.assertIn("G-code", text.decode())
        _, ex = self.req("GET", "/api/examples")
        names = [e["name"] for e in ex["examples"]]
        self.assertEqual(set(names),
                         {"safe_demo", "problems_demo", "inch_demo",
                          "arc_demo", "plane_arc_demo", "drill_cycle_demo",
                          "wcs_demo", "length_comp_demo", "subprogram_demo",
                          "subprogram_errors_demo"})
        kinds = {e["name"]: e["kind"] for e in ex["examples"]}
        self.assertEqual(kinds["subprogram_demo"], "package")
        self.assertEqual(kinds["safe_demo"], "program")
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
        # G54.1 X-：残缺 + 未支持 同时出现
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
        self.assertIn(b"G54.1", nc)

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

    def test_11_multi_wcs(self):
        cfg = {"name": "wcs", "travel_x": [0, 300], "travel_y": [0, 200],
               "travel_z": [-50, 60], "safe_z": 10,
               "max_feed_mm_min": 3000, "max_spindle_rpm": 12000,
               "wcs_offsets": {"G54": {"x": 0, "y": 0, "z": 0},
                               "G55": {"x": 100, "y": 50, "z": 0}}}
        nc = ("G21 G90 G54\nM3 S5000\nG0 Z20\nG0 X10 Y10\n"
              "G1 Z-2 F300\nG1 X30 Y30 F600\nG0 Z20\n"
              "G55\nG0 X10 Y10\nG1 Z-2 F300\nG1 X30 Y30 F600\n"
              "G0 Z-1\nG0 Z20\n"
              "G59\nG0 X0 Y0\nG1 Z-2 F300\nG1 X20 Y20 F600\nG0 Z20\n"
              "G54\nG0 X0 Y0 Z50\nM5\n")
        # 同步分析：分系统计、未配置坐标系、段级坐标系/偏置/机床坐标
        _, r = self.req("POST", "/api/analyze", {"config": cfg, "gcode": nc})
        self.assertEqual(r["wcs"]["used"], ["G54", "G55", "G59"])
        self.assertEqual(r["wcs"]["offsets_mm"]["G55"],
                         {"x": 100, "y": 50, "z": 0})
        self.assertFalse(r["wcs"]["by_wcs"]["G59"]["configured"])
        self.assertIsNone(r["wcs"]["by_wcs"]["G59"]["machine_bbox_mm"])
        self.assertIsNone(r["bbox_machine_mm"])   # G59 未配置 -> 整体未知
        self.assertIn("未配置", r["machine_bbox_note"])
        unk = [i for i in r["issues"] if i["code"] == "UNKNOWN_WCS"]
        self.assertTrue(unk)
        self.assertTrue(all(i["details"]["wcs"] == "G59" for i in unk))
        self.assertTrue(all(i["details"]["reason"] == "wcs_not_configured"
                            for i in unk))
        # G55 段带偏置与机床坐标；安全 Z 按工件坐标判定（G0 Z-1 告警）
        seg55 = [t["segment"] for t in r["trajectory"]
                 if (t.get("segment") or {}).get("wcs") == "G55"]
        self.assertTrue(seg55)
        self.assertTrue(all(s["offset_mm"] == {"x": 100, "y": 50, "z": 0}
                            for s in seg55))
        self.assertTrue(all(s["end_machine_mm"] is not None for s in seg55))
        rsz = [i for i in r["issues"] if i["code"] == "RAPID_BELOW_SAFE_Z"]
        self.assertEqual(len(rsz), 1)
        self.assertEqual(rsz[0]["details"]["wcs"], "G55")
        self.assertAlmostEqual(rsz[0]["details"]["ref_z_mm"], -1.0)

        # 建作业走 filter_report：按坐标系筛选
        _, job = self.req("POST", "/api/jobs", {"config": cfg, "gcode": nc},
                          expect=202)
        self.wait_job(job["id"])
        _, f = self.req("GET", f"/api/jobs/{job['id']}/report?wcs=G55")
        self.assertEqual(f["filter"]["wcs"], ["G55"])
        self.assertEqual(list(f["wcs"]["by_wcs"]), ["G55"])
        self.assertTrue(f["wcs"]["filtered"])
        self.assertEqual([i["code"] for i in f["issues"]],
                         ["RAPID_BELOW_SAFE_Z"])
        segs = [t["segment"] for t in f["trajectory"] if t.get("segment")]
        self.assertTrue(segs)
        self.assertTrue(all(s["wcs"] == "G55" for s in segs))
        # 未使用的坐标系补零值行；非法坐标系 400
        _, f2 = self.req("GET", f"/api/jobs/{job['id']}/report?wcs=G56")
        self.assertEqual(f2["wcs"]["by_wcs"]["G56"]["issues"], 0)
        self.assertFalse(f2["wcs"]["by_wcs"]["G56"]["configured"])
        self.req("GET", f"/api/jobs/{job['id']}/report?wcs=G60", expect=400)

        # 固定循环分组按孔的坐标系裁剪
        drill = ("G21 G90 G54\nM3 S3000\nG0 X0 Y0 Z20\n"
                 "G81 R2 Z-5 F200\nX10\n"
                 "G55\nX10\nX20\nG80\n")
        _, jr = self.req("POST", "/api/analyze",
                         {"config": cfg, "gcode": drill})
        holes = [h["wcs"] for g in jr["drill_cycles"]["groups"]
                 for h in g["holes"]]
        self.assertEqual(holes, ["G54", "G54", "G55", "G55"])
        _, jjob = self.req("POST", "/api/jobs",
                           {"config": cfg, "gcode": drill}, expect=202)
        self.wait_job(jjob["id"])
        _, fd = self.req("GET", f"/api/jobs/{jjob['id']}/report?wcs=G55")
        self.assertEqual(fd["drill_cycles"]["summary"]["holes_total"], 2)
        self.assertTrue(all(h["wcs"] == "G55"
                            for g in fd["drill_cycles"]["groups"]
                            for h in g["holes"]))

        # 对比：分坐标系的路径/问题/行程（机床包围盒）变化
        _, cmp = self.req("POST", "/api/compare", {
            "config": cfg, "label_a": "multi", "gcode_a": nc,
            "label_b": "g54only",
            "gcode_b": "G21 G90 G54\nM3 S5000\nG0 Z20\nG0 X10 Y10\n"
                       "G1 Z-2 F300\nG1 X30 Y30 F600\nG0 Z20\nM5\n"})
        self.assertIn("wcs", cmp)
        g55 = cmp["wcs"]["by_wcs"]["G55"]
        self.assertGreater(g55["baseline_path_mm"]["total"], 0)
        self.assertEqual(g55["candidate_path_mm"]["total"], 0.0)
        self.assertLess(g55["delta_path_mm"]["total"], 0)
        self.assertIsNotNone(g55["baseline_machine_bbox_mm"])
        self.assertIsNone(g55["candidate_machine_bbox_mm"])
        g59 = cmp["wcs"]["by_wcs"]["G59"]
        self.assertLess(g59["delta_issues"], 0)      # G59 的问题被解决
        self.assertGreater(g59["resolved_issues"], 0)

        # 配置校验：非法偏置定位到坐标系与字段
        bad = dict(cfg, wcs_offsets={"G55": {"x": "oops"}})
        _, err = self.req("POST", "/api/machines", bad, expect=400)
        payload = json.loads(err)
        self.assertEqual(payload["error"]["code"], "BAD_CONFIG")
        self.assertTrue(any("wcs_offsets.G55.x" in e
                            for e in payload["error"]["details"]["errors"]))

        # 旧配置（仅 offset_x/y/z）归入 G54，已有作业可读取
        legacy = {"name": "legacy", "travel_x": [0, 300],
                  "travel_y": [0, 200], "travel_z": [-50, 60], "safe_z": 10,
                  "max_feed_mm_min": 3000, "max_spindle_rpm": 12000,
                  "offset_x": -5, "offset_y": 2, "offset_z": 1}
        _, m = self.req("POST", "/api/machines", legacy, expect=201)
        _, one = self.req("GET", f"/api/machines/{m['id']}")
        self.assertEqual(one["config"]["wcs_offsets"]["G54"],
                         {"x": -5.0, "y": 2.0, "z": 1.0})
        _, rr = self.req("POST", "/api/analyze",
                         {"machine_id": m["id"],
                          "gcode": "G21 G90 G54\nG0 X0 Y0 Z20\n"})
        self.assertEqual(rr["machine"]["wcs_offsets"]["G54"]["x"], -5.0)

        # 多坐标系示例可下载
        resp, nc_text = self.req("GET", "/api/examples/wcs_demo", raw=True)
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        self.assertIn(b"G55", nc_text)

    def test_12_length_compensation(self):
        cfg_l = dict(CONFIG, safe_z=10,
                     length_offsets={"1": 10, "2": -3, "3": 2})
        # 配置校验：H 号非正整数 / 偏置非数值 -> 400 BAD_CONFIG 并定位字段
        _, err = self.req("POST", "/api/machines",
                          dict(cfg_l, length_offsets={"H0": 1}), expect=400)
        errors = json.loads(err)["error"]["details"]["errors"]
        self.assertTrue(any("length_offsets.H0" in e for e in errors))
        _, err = self.req("POST", "/api/machines",
                          dict(cfg_l, length_offsets={"3": "x"}), expect=400)
        errors = json.loads(err)["error"]["details"]["errors"]
        self.assertTrue(any("length_offsets.H3 必须是数值" in e
                            for e in errors))

        nc = (
            "G21 G90 G54\nM3 S6000\nG0 X0 Y0 Z20\n"
            "G43 H1\n"
            "G1 X20 Z5 F500\n"
            "G99 G81 R2 Z-8 F250\nX40\nG80\n"
            "G49\n"
            "G43\n"                 # 缺 H：阻断
            "G43 H9\n"              # H9 不在表：阻断
            "M5\n")
        _, r = self.req("POST", "/api/analyze", {"config": cfg_l, "gcode": nc})
        codes = [i["code"] for i in r["issues"]]
        self.assertIn("LENGTH_COMP_MISSING_H", codes)
        self.assertIn("LENGTH_COMP_H_NOT_FOUND", codes)
        lc = r["length_compensation"]
        self.assertEqual(lc["offsets_mm"],
                         {"H1": 10.0, "H2": -3.0, "H3": 2.0})
        ev43 = [e for e in lc["events"] if e["code"] == "G43"][0]
        self.assertEqual(ev43["tip_z_workpiece_mm"], 10.0)
        self.assertEqual(ev43["spindle_z_machine_mm"], 20.0)
        # 循环孔带 H 与孔底主轴基准 Z（-8 + 10 = 2）
        cyc = [t["segment"] for t in r["trajectory"]
               if t.get("segment", {}).get("kind") == "canned_cycle"][0]
        self.assertEqual(cyc["h"], 1)
        self.assertEqual(cyc["holes"][0]["spindle_bottom_z_machine_mm"], 2.0)

        # 建作业走 filter_report：按 H 筛选
        _, job = self.req("POST", "/api/jobs",
                          {"config": cfg_l, "gcode": nc}, expect=202)
        self.wait_job(job["id"])
        _, f = self.req("GET", f"/api/jobs/{job['id']}/report?h=H1")
        self.assertEqual(f["filter"]["h"], ["H1"])
        # 缺 H / H 不存在的阻断问题不归属任何已建立 H，被筛掉
        fcodes = [i["code"] for i in f["issues"]]
        self.assertNotIn("LENGTH_COMP_MISSING_H", fcodes)
        self.assertNotIn("LENGTH_COMP_H_NOT_FOUND", fcodes)
        self.assertEqual(list(f["length_compensation"]["by_h"]), ["H1"])
        segs = [t["segment"] for t in f["trajectory"] if t.get("segment")]
        self.assertTrue(all(s.get("h") == 1 for s in segs))
        # 钻孔分组按孔的 h 裁剪
        self.assertTrue(all(h["h"] == 1
                            for g in f["drill_cycles"]["groups"]
                            for h in g["holes"]))
        # h 接受纯数字；非法 h 400
        _, f2 = self.req("GET", f"/api/jobs/{job['id']}/report?h=1")
        self.assertEqual(f2["filter"]["h"], ["H1"])
        self.req("GET", f"/api/jobs/{job['id']}/report?h=H0", expect=400)
        self.req("GET", f"/api/jobs/{job['id']}/report?h=abc", expect=400)

        # 汇总与筛选一致：问题计数重算（缺 H/H 不存在不归属任何已建立 H）、
        # G49 只保留结束该 H 补偿段的取消事件
        f3, body = self.req(
            "GET", f"/api/jobs/{job['id']}/report?h=H1&trajectory=all")
        lc = body["length_compensation"]
        self.assertEqual(lc["issues"], {
            "LENGTH_COMP_MISSING_H": 0,
            "LENGTH_COMP_H_NOT_FOUND": 0,
            "LENGTH_COMP_CONFLICT": 0})
        self.assertEqual([e["code"] for e in lc["events"]],
                         ["G43", "G49"])
        self.assertEqual(lc["events"][-1].get("cancels_h"), 1)
        self.assertEqual(body["risk"]["total_issues"], len(body["issues"]))

        # 对比：length_compensation 节列出补偿与 Z 行程变化
        nc2 = ("G21 G90 G54\nM3 S6000\nG0 X0 Y0 Z20\n"
               "G43 H3\nG1 X20 Z5 F500\nG0 Z20\nG49\nM5\n")
        _, cmp = self.req("POST", "/api/compare", {
            "config": cfg_l, "label_a": "h1", "gcode_a": nc,
            "label_b": "h3", "gcode_b": nc2})
        lcmp = cmp["length_compensation"]
        self.assertIn("H1", lcmp["by_h"])
        self.assertIn("H3", lcmp["by_h"])
        self.assertEqual(lcmp["events"]["delta"]["G43"], 0)  # 都是 1 次
        # H1=10 在 H3=2 不在表差异里（两侧表相同，故无表变化）
        self.assertEqual(lcmp["offset_table_changes"], [])
        self.assertIsNotNone(
            lcmp["spindle_z_travel"]["baseline_spindle_z_machine_mm"])
        # 阻断问题计数
        self.assertGreaterEqual(
            lcmp["block_issue_counts"]["LENGTH_COMP_MISSING_H"]["baseline"], 1)

    def test_13_length_comp_example_download(self):
        resp, text = self.req("GET", "/api/examples/length_comp_demo",
                              raw=True)
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        self.assertIn(b"G43", text)
        listing, meta = self.req("GET", "/api/examples")
        self.assertIn("length_comp_demo",
                      [e["name"] for e in meta["examples"]])


class PackageApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db = os.path.join(cls.tmp.name, "test_pkg.db")
        cls.server = make_server("127.0.0.1", 0, db)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        _, m = cls.req_static("POST", "/api/machines", CONFIG)
        cls.machine_id = m["id"]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    @staticmethod
    def _do(method, path, body=None, expect=None, raw=False):
        data = None
        hdrs = {}
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        r = urllib.request.Request(f"http://127.0.0.1:{PackageApiTest.port}"
                                   + path, data=data, headers=hdrs,
                                   method=method)
        try:
            resp = urllib.request.urlopen(r, timeout=10)
        except urllib.error.HTTPError as e:
            payload = e.read().decode()
            if expect is not None and e.code == expect:
                return e, (payload if raw else json.loads(payload))
            raise AssertionError(f"{method} {path} -> {e.code}: {payload}")
        payload = resp.read()
        if expect is not None:
            assert resp.status == expect
        return resp, (payload if raw else json.loads(payload.decode()))

    @classmethod
    def req_static(cls, method, path, body=None, expect=None, raw=False):
        return cls._do(method, path, body, expect, raw)

    def req(self, method, path, body=None, expect=None, raw=False):
        return self._do(method, path, body, expect, raw)

    def wait_package(self, pid, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, pkg = self.req("GET", f"/api/packages/{pid}")
            if pkg["status"] in ("completed", "failed", "blocked"):
                return pkg
            time.sleep(0.02)
        self.fail("程序包超时未完成")

    def create_package(self, package, machine="id", expect=202):
        body = {"name": package["name"], "main": package["main"],
                "subprograms": package["subprograms"]}
        if machine == "id":
            body["machine_id"] = self.machine_id
        elif machine == "inline":
            body["config"] = CONFIG
        _, created = self.req("POST", "/api/packages", body, expect=expect)
        return created

    # -- 测试 --------------------------------------------------------------

    def test_01_dialect_and_examples(self):
        _, d = self.req("GET", "/api/dialect")
        self.assertIn("package_dialect", d)
        self.assertIn("M98", d["package_dialect"]["flow_m"])
        self.assertIn("PACKAGE_RECURSIVE_CALL", d["expansion_error_titles"])
        _, ex = self.req("GET", "/api/examples")
        pkg_ex = [e for e in ex["examples"] if e["kind"] == "package"]
        self.assertEqual({e["name"] for e in pkg_ex},
                         {"subprogram_demo", "subprogram_errors_demo"})
        resp, blob = self.req("GET", "/api/examples/subprogram_demo",
                              raw=True)
        self.assertIn("application/json", resp.headers["Content-Type"])
        parsed = json.loads(blob)
        self.assertIn("main", parsed)
        self.assertEqual(len(parsed["subprograms"]), 2)

    def test_02_bad_spec_400(self):
        _, err = self.req("POST", "/api/packages",
                          {"name": "x", "machine_id": self.machine_id},
                          expect=400)
        self.assertEqual(err["error"]["code"], "BAD_PACKAGE")
        self.assertTrue(err["error"]["details"]["errors"])

    def test_03_good_package_lifecycle(self):
        created = self.create_package(PACKAGE_GOOD)
        self.assertIn(created["status"], ("queued", "running"))
        pid = created["id"]
        detail = self.wait_package(pid)
        self.assertEqual(detail["status"], "completed")
        self.assertEqual(detail["progress"], 100)
        exp = detail["expansion"]
        self.assertEqual(exp["subprograms_defined"], 2)
        self.assertEqual(exp["call_invocations"], 4)
        self.assertEqual(exp["max_depth"], 2)
        # 调用图：main->O100(L2)、main->O200、O200->O100
        edges = {(e["caller"], e["callee"]): e
                 for e in detail["call_graph"]["edges"]}
        self.assertEqual(edges[("main", "O100")]["invocations"], 2)
        self.assertEqual(edges[("O200", "O100")]["invocations"], 1)
        self.assertEqual(edges[("main", "O200")]["invocations"], 1)

        # 完整报告
        _, report = self.req("GET", f"/api/packages/{pid}/report")
        self.assertFalse(report.get("blocked"))
        self.assertEqual(report["package"]["name"], "pkg_good")
        self.assertEqual(report["program"]["expanded_blocks"],
                         exp["expanded_blocks"])
        self.assertTrue(report["issues"])
        # 每个轨迹条目与问题都带来源程序/调用栈
        for e in report["trajectory"]:
            self.assertIn("source_program", e)
            self.assertIn("call_stack", e)
        # 子程序孔位问题归属到来源程序
        hole_issues = [i for i in report["issues"]
                       if i["source_program"].startswith("O")]
        self.assertTrue(hole_issues)

        # 下载
        resp, blob = self.req(
            "GET", f"/api/packages/{pid}/report/download", raw=True)
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        dl = json.loads(blob)
        self.assertEqual(dl["package_id"], pid)

        # 列表
        _, lst = self.req("GET", "/api/packages")
        self.assertTrue(any(p["id"] == pid for p in lst["packages"]))

        # 不存在
        self.req("GET", "/api/packages/nope", expect=404)
        self.req("GET", "/api/packages/nope/report", expect=404)

        self.__class__.good_pid = pid

    def test_04_blocks_pagination(self):
        pid = self.good_pid
        _, page = self.req(
            "GET", f"/api/packages/{pid}/blocks?limit=10&offset=0")
        self.assertEqual(len(page["blocks"]), 10)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["blocks"][0]["seq"], 0)
        self.assertIn("call_stack", page["blocks"][0])
        _, page2 = self.req(
            "GET", f"/api/packages/{pid}/blocks?limit=10&offset=10")
        self.assertEqual(page2["blocks"][0]["seq"], 10)
        # 末页
        total = page["total"]
        _, last = self.req(
            "GET", f"/api/packages/{pid}/blocks?limit=100&offset=0")
        self.assertEqual(len(last["blocks"]), total)
        self.assertFalse(last["has_more"])
        # 按来源筛选
        _, o100 = self.req(
            "GET", f"/api/packages/{pid}/blocks?source=O100&limit=1000")
        self.assertTrue({b["program"] for b in o100["blocks"]} <= {"O100"})
        self.assertTrue(o100["total"] > 0)
        # 深度为 2 的块来自 O100（经 O200 调用）
        deep = [b for b in o100["blocks"] if b["depth"] == 2]
        self.assertTrue(deep)
        self.assertEqual(deep[0]["call_stack"][0]["program"], "O200")
        # 非法分页/来源
        self.req("GET", f"/api/packages/{pid}/blocks?limit=0", expect=400)
        self.req("GET", f"/api/packages/{pid}/blocks?limit=99999",
                 expect=400)
        self.req("GET", f"/api/packages/{pid}/blocks?offset=-1",
                 expect=400)
        self.req("GET", f"/api/packages/{pid}/blocks?source=NOPE",
                 expect=400)

    def test_05_report_source_filter(self):
        pid = self.good_pid
        _, full = self.req("GET", f"/api/packages/{pid}/report")
        total_blocks = len(full["trajectory"])
        _, flt = self.req(
            "GET", f"/api/packages/{pid}/report?source=O100")
        self.assertTrue({e["source_program"] for e in flt["trajectory"]}
                        <= {"O100"})
        self.assertLess(len(flt["trajectory"]), total_blocks)
        self.assertEqual(flt["filter"]["matched_blocks"],
                         len(flt["trajectory"]))
        self.assertEqual(flt["filter"]["total_blocks_in_report"],
                         total_blocks)
        # 风险计数随筛选重算
        self.assertEqual(flt["risk"]["total_issues"], len(flt["issues"]))
        # 多来源
        _, two = self.req(
            "GET", f"/api/packages/{pid}/report?source=main,O200")
        self.assertTrue({e["source_program"] for e in two["trajectory"]}
                        <= {"main", "O200"})
        # 省略轨迹
        _, notraj = self.req(
            "GET", f"/api/packages/{pid}/report?trajectory=0")
        self.assertNotIn("trajectory", notraj)
        # 顶层 package/call_graph 仍然保留
        self.assertIn("call_graph", notraj["package"])

    def test_05b_length_comp_h_filter_in_package(self):
        # 主程序 G43 H1，子程序切削；length_offsets 随内联配置提供
        config = dict(CONFIG, safe_z=10,
                      length_offsets={"1": 10, "2": 2})
        package = {
            "name": "pkg_lcomp",
            "main": ("G21 G90 G54\nM3 S4000\nG0 X0 Y0 Z20\n"
                     "G43 H1\nM98 P100\nG49\nM30\n"),
            "subprograms": [
                {"name": "o100.nc",
                 "content": "O100\nG1 X10 Z5 F500\nG0 Z20\nM99\n"}],
        }
        body = dict(package, config=config)
        _, created = self.req("POST", "/api/packages", body, expect=202)
        pid = created["id"]
        detail = self.wait_package(pid)
        self.assertEqual(detail["status"], "completed")
        _, full = self.req("GET", f"/api/packages/{pid}/report")
        # 子程序切削段继承 H1，主轴基准 Z = 刀尖 + 10
        sub_seg = [e["segment"] for e in full["trajectory"]
                   if e.get("source_program") == "O100"
                   and e.get("segment", {}).get("kind") == "linear"][0]
        self.assertEqual(sub_seg["h"], 1)
        self.assertEqual(sub_seg["end_machine_mm"][2], 15.0)
        # 报告含 length_compensation 节与 G43 事件
        lc = full["length_compensation"]
        self.assertTrue(any(e["code"] == "G43" for e in lc["events"]))
        # h 筛选：保留 H1 段（含 G49 取消事件），问题计数重算
        _, flt = self.req("GET", f"/api/packages/{pid}/report?h=H1")
        segs = [e["segment"] for e in flt["trajectory"] if e.get("segment")]
        self.assertTrue(all(s.get("h") == 1 for s in segs))
        self.assertEqual(flt["filter"]["h"], ["H1"])
        self.assertEqual(
            list(flt["length_compensation"]["by_h"]), ["H1"])
        # h 与 source 叠加
        _, both = self.req(
            "GET", f"/api/packages/{pid}/report?h=H1&source=O100")
        self.assertTrue({e.get("source_program") for e in both["trajectory"]}
                        <= {"O100"})
        # 非法 h 400
        self.req("GET", f"/api/packages/{pid}/report?h=H0", expect=400)

    def test_05c_h_filter_applies_with_trajectory_omitted(self):
        # 多 H 程序：H1（子程序）与 H2（主程序），验证 trajectory=0 时
        # h 筛选仍然作用于问题与汇总节
        config = dict(CONFIG, safe_z=-50,
                      length_offsets={"1": 10, "2": 2})
        package = {
            "name": "pkg_lcomp2",
            "main": ("G21 G90 G54\nM3 S4000\nG0 X0 Y0 Z20\n"
                     "G43 H1\nM98 P100\nG49\n"
                     "G43 H2\nG1 X30 Z5 F500\nG49\nM30\n"),
            "subprograms": [
                {"name": "o100.nc",
                 "content": "O100\nG1 X10 Z5 F500\nG0 Z20\nM99\n"}],
        }
        _, created = self.req("POST", "/api/packages",
                              dict(package, config=config), expect=202)
        pid = created["id"]
        self.wait_package(pid)
        _, f = self.req(
            "GET", f"/api/packages/{pid}/report?h=H1&trajectory=0")
        # 轨迹已省略，但筛选仍生效并完整回显
        self.assertNotIn("trajectory", f)
        self.assertEqual(f["filter"]["h"], ["H1"])
        self.assertEqual(f["filter"]["trajectory"], "omitted")
        self.assertIn("source", f["filter"])
        lc = f["length_compensation"]
        self.assertTrue(lc["filtered"])
        self.assertEqual(list(lc["by_h"]), ["H1"])
        # 只保留 H1 的 G43 与结束 H1 的 G49（H2 段不混入）
        self.assertEqual([(e["code"], e.get("h"), e.get("cancels_h"))
                          for e in lc["events"]],
                         [("G43", 1, None), ("G49", None, 1)])
        self.assertEqual(f["risk"]["total_issues"], len(f["issues"]))
        # 仅 trajectory=0 不带筛选：回显不包含 h，汇总完整
        _, f0 = self.req(
            "GET", f"/api/packages/{pid}/report?trajectory=0")
        self.assertEqual(f0["filter"], {"trajectory": "omitted"})
        self.assertIn("H1", f0["length_compensation"]["by_h"])
        self.assertIn("H2", f0["length_compensation"]["by_h"])

    def test_06_blocked_package(self):
        created = self.create_package(PACKAGE_BAD)
        pid = created["id"]
        detail = self.wait_package(pid)
        self.assertEqual(detail["status"], "blocked")
        codes = sorted({e["code"] for e in detail["expansion_errors"]})
        self.assertIn("PACKAGE_M99_IN_MAIN", codes)
        self.assertIn("PACKAGE_DUPLICATE_O", codes)
        self.assertIn("PACKAGE_RECURSIVE_CALL", codes)
        self.assertIn("PACKAGE_SUBPROGRAM_NOT_FOUND", codes)
        self.assertIn("PACKAGE_DYNAMIC_P", codes)
        # 阻断报告无安全结论
        _, report = self.req("GET", f"/api/packages/{pid}/report")
        self.assertTrue(report["blocked"])
        self.assertNotIn("risk", report)
        self.assertNotIn("trajectory", report)
        self.assertNotIn("issues", report)
        self.assertTrue(report["expansion_errors"])
        # 阻断后没有展开块
        self.req("GET", f"/api/packages/{pid}/blocks", expect=409)
        # 程序包原文仍可下载
        resp, blob = self.req(
            "GET", f"/api/packages/{pid}/package", raw=True)
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        self.assertIn(b"M98 P999", blob)

    def test_07_inline_config_package(self):
        created = self.create_package(PACKAGE_GOOD, machine="inline")
        detail = self.wait_package(created["id"])
        self.assertEqual(detail["status"], "completed")

    def test_08_package_compare(self):
        a = self.create_package(PACKAGE_GOOD)
        self.wait_package(a["id"])
        pkg_b = dict(PACKAGE_GOOD, name="pkg_good_v2",
                     main=PACKAGE_GOOD["main"].replace("M98 P100 L2",
                                                       "M98 P100"))
        b = self.create_package(pkg_b)
        self.wait_package(b["id"])

        # 按 ID 对比
        _, cmp = self.req("POST", "/api/package-compare", {
            "package_a_id": a["id"], "package_b_id": b["id"]})
        self.assertEqual(cmp["compare_type"], "package")
        self.assertEqual(cmp["expansion"]["delta"]["call_invocations"], -1)
        self.assertTrue(cmp["call_graph_diff"]["edges_changed"])
        self.assertIn("risk", cmp)
        self.assertIn("resolved_issues", cmp)

        # 内联对比
        _, cmp2 = self.req("POST", "/api/package-compare", {
            "machine_id": self.machine_id,
            "package_a": PACKAGE_GOOD, "label_a": "v1",
            "package_b": pkg_b, "label_b": "v2", "save": True})
        self.assertEqual(cmp2["expansion"]["delta"]["expanded_blocks"] < 0,
                         True)
        cid = cmp2["comparison_id"]
        _, saved = self.req("GET", f"/api/comparisons/{cid}")
        self.assertEqual(saved["compare_type"], "package")

        # 阻断程序包不能对比
        blocked = self.create_package(PACKAGE_BAD)
        self.wait_package(blocked["id"])
        _, err = self.req("POST", "/api/package-compare", {
            "package_a_id": blocked["id"], "package_b_id": a["id"]},
            expect=409)
        self.assertEqual(err["error"]["code"], "PACKAGE_NOT_COMPARABLE")
        # 不存在
        self.req("POST", "/api/package-compare",
                 {"package_a_id": "nope", "package_b_id": a["id"]},
                 expect=404)

    def test_09_depth_limit_and_block_limit(self):
        nested = [
            {"name": f"o{i}.nc",
             "content": f"O{i}\nM98 P{i + 1}\nM99\n"}
            for i in range(100, 103)]
        nested.append({"name": "o103.nc", "content": "O103\nM99\n"})
        body = {"name": "deep", "machine_id": self.machine_id,
                "main": "M98 P100\nM30\n", "subprograms": nested,
                "max_depth": 2}
        _, created = self.req("POST", "/api/packages", body)
        detail = self.wait_package(created["id"])
        self.assertEqual(detail["status"], "blocked")
        self.assertTrue(any(e["code"] == "PACKAGE_DEPTH_LIMIT"
                            for e in detail["expansion_errors"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
