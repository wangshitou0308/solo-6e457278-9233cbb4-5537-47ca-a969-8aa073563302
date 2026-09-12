"""分析器/对比/解析器的离线单元测试：python3 -m tests.test_analyzer"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gcode_checker.analyzer import (
    MachineConfig, analyze_program, ConfigError, solve_arc,
)
from gcode_checker.parser import parse_program, parse_line
from gcode_checker.compare import compare_reports


def cfg():
    return MachineConfig.from_dict({
        "name": "test",
        "travel_x": [0, 300], "travel_y": [0, 200], "travel_z": [-50, 60],
        "safe_z": 2,
        "max_feed_mm_min": 3000, "max_spindle_rpm": 12000,
        "offset_x": 0, "offset_y": 0, "offset_z": 0,
    })


def codes(report):
    return [i["code"] for i in report["issues"]]


class TestParser(unittest.TestCase):
    def test_comments_and_words(self):
        pl = parse_line("G1 X-12.5 F1000 (cut) ; trailing", 1)
        self.assertEqual(pl.comments, ["cut", "trailing"])
        self.assertEqual([w.raw for w in pl.words],
                         ["G1", "X-12.5", "F1000"])
        self.assertEqual(pl.malformed, [])

    def test_malformed_recovers_unsupported(self):
        # G55 X- ：G55 是合法词（未支持），X- 残缺
        pl = parse_line("G55 X-", 1)
        self.assertIn("X-", pl.malformed)
        letters = [(w.letter, w.value) for w in pl.words]
        self.assertIn(("G", 55.0), letters)

    def test_g20_g21_same_line_state_last_wins(self):
        report = analyze_program("G20 G21 G90 G91\n", cfg())
        st = report["final_state"]
        self.assertEqual(st["unit"], "mm")
        self.assertEqual(st["distance_mode"], "relative")
        traj = report["trajectory"][0]
        self.assertEqual(traj["normalized"], "G21 G91")
        self.assertEqual(traj["state_in"]["unit"], None)
        self.assertEqual(traj["state_out"]["unit"], "mm")

    def test_malformed_line_lists_unsupported(self):
        report = analyze_program("G55 X-\n", cfg())
        c = codes(report)
        self.assertIn("MALFORMED_LINE", c)
        self.assertIn("UNSUPPORTED_INSTRUCTION", c)
        unsup = [i for i in report["issues"]
                 if i["code"] == "UNSUPPORTED_INSTRUCTION"][0]
        self.assertIn("G55", unsup["details"]["unsupported_tokens"])
        # 整段阻断，状态无痕
        self.assertIsNone(report["final_state"]["unit"])
        traj = report["trajectory"][0]
        self.assertFalse(traj["executed"])
        self.assertEqual(traj["block_reason"], "malformed")


class TestBasicAnalysis(unittest.TestCase):
    def test_clean_program_no_issues(self):
        nc = """G21 G90 G54
M3 S6000
G0 Z50
G0 X0 Y0
G0 Z5
G1 Z-2 F300
G1 X40 Y10 F600
G0 Z50
M5
"""
        r = analyze_program(nc, cfg())
        self.assertEqual(codes(r), [])
        self.assertEqual(r["risk"]["level"], "none")
        self.assertEqual(r["bbox_program_mm"]["x_mm"], [0.0, 40.0])
        self.assertEqual(r["bbox_program_mm"]["z_mm"], [-2.0, 50.0])
        self.assertGreater(r["path_length_mm"]["cutting"], 0)
        self.assertGreater(r["path_length_mm"]["rapid"], 0)

    def test_spindle_not_running_and_feed_unset(self):
        r = analyze_program("G21 G90 G54\nG1 X10 Y10\n", cfg())
        c = codes(r)
        self.assertIn("SPINDLE_NOT_RUNNING", c)
        self.assertIn("FEED_UNSET", c)

    def test_feed_and_spindle_limits(self):
        r = analyze_program(
            "G21 G90 G54\nM3 S20000\nG1 X10 F4000\n", cfg())
        c = codes(r)
        self.assertIn("SPINDLE_OVER_LIMIT", c)
        self.assertIn("FEED_OVER_LIMIT", c)

    def test_rapid_below_safe_z(self):
        # 快速定位终点低于安全 Z
        r = analyze_program("G21 G90 G54\nG0 Z-5\n", cfg())
        self.assertIn("RAPID_BELOW_SAFE_Z", codes(r))
        # 安全 Z 以下的水平快速移动（即便终点抬回，也有横移风险）
        r2 = analyze_program(
            "G21 G90 G54\nG0 X0 Y0 Z-5\nG0 X20 Y-5\n", cfg())
        self.assertIn("RAPID_BELOW_SAFE_Z", codes(r2))
        # 纯垂直抬刀经过低 Z 不报警（先用切削下到低位）
        r3 = analyze_program(
            "G21 G90 G54\nM3 S1000\nG1 X0 Y0 Z-5 F500\nG0 Z20\n", cfg())
        self.assertNotIn("RAPID_BELOW_SAFE_Z", codes(r3))

    def test_out_of_bounds_with_offset(self):
        c = MachineConfig.from_dict({
            "travel_x": [0, 100], "travel_y": [0, 100], "travel_z": [-10, 10],
            "safe_z": 5, "max_feed_mm_min": 1000, "max_spindle_rpm": 10000,
            "offset_x": -50})
        r = analyze_program("G21 G90 G54\nG0 X160\n", c)
        self.assertIn("OUT_OF_BOUNDS", codes(r))
        v = [i for i in r["issues"] if i["code"] == "OUT_OF_BOUNDS"][0]
        self.assertEqual(v["details"]["axis"], "X")
        self.assertAlmostEqual(v["details"]["value_mm"], 110.0)

    def test_inch_conversion(self):
        c = cfg()
        r = analyze_program(
            "G20 G90 G54\nM3 S1000\nG1 X1 F30\n", c)  # 1in=25.4mm, 30ipm=762
        self.assertNotIn("FEED_OVER_LIMIT", codes(r))
        st = r["final_state"]
        self.assertAlmostEqual(st["x"]["value_mm"], 25.4)
        self.assertAlmostEqual(st["feed_mm_per_min"]["value_mm"], 762.0)

    def test_unknown_units_and_mode(self):
        r = analyze_program("G54\nG1 X10\n", cfg())
        c = codes(r)
        self.assertIn("UNKNOWN_UNITS", c)
        self.assertIn("UNKNOWN_DISTANCE_MODE", c)
        # 位置未知，不进包围盒
        self.assertIsNone(r["bbox_program_mm"])
        self.assertFalse(r["path_length_mm"]["reliable"])

    def test_no_motion_mode(self):
        r = analyze_program("G21 G90 G54\nX10\n", cfg())
        self.assertIn("NO_MOTION_MODE", codes(r))
        self.assertIsNone(r["final_state"]["x"]["value_mm"])

    def test_unsupported_blocks_state(self):
        r = analyze_program(
            "G21 G90 G54\nM8 M3 S1000\nG1 X5 F500\n", cfg())
        # M8 未支持 -> 整段阻断，M3/S1000 均不生效
        self.assertFalse(r["final_state"]["spindle_on"])
        self.assertIn("SPINDLE_NOT_RUNNING", codes(r))
        unsup = [i for i in r["issues"]
                 if i["code"] == "UNSUPPORTED_INSTRUCTION"][0]
        self.assertIn("M8", unsup["details"]["unsupported_tokens"])

    def test_modal_motion_continues(self):
        r = analyze_program(
            "G21 G90 G54\nM3 S1000\nG1 X0 F500\nX10\nY10\n", cfg())
        self.assertNotIn("NO_MOTION_MODE", codes(r))
        self.assertAlmostEqual(r["final_state"]["y"]["value_mm"], 10.0)

    def test_incremental(self):
        r = analyze_program(
            "G21 G90 G54\nG0 X0 Y0\nG91\nG1 X10 Y5 F500\n", cfg())
        st = r["final_state"]
        self.assertEqual((st["x"]["value_mm"], st["y"]["value_mm"]), (10, 5))


class TestArcs(unittest.TestCase):
    def test_full_circle_ij(self):
        sol = solve_arc((0, 0), (0, 0), clockwise=True, i=-10, j=0, r_word=None)
        self.assertAlmostEqual(abs(sol["sweep"]), 2 * math.pi, places=9)
        self.assertAlmostEqual(sol["length_xy"], 20 * math.pi, places=6)

    def test_endpoint_not_on_circle(self):
        with self.assertRaises(Exception):
            solve_arc((0, 0), (30, 0), clockwise=True, i=10, j=0, r_word=None)

    def test_r_too_short(self):
        with self.assertRaises(Exception):
            solve_arc((0, 0), (20, 0), clockwise=True,
                      i=None, j=None, r_word=5)

    def test_r_half_circle(self):
        sol = solve_arc((0, 0), (20, 0), clockwise=True,
                        i=None, j=None, r_word=10)
        # 半圆的圆心即弦中点
        self.assertAlmostEqual(sol["center"][0], 10.0, places=6)
        self.assertAlmostEqual(sol["center"][1], 0.0, places=6)
        self.assertAlmostEqual(sol["length_xy"], 10 * math.pi, places=6)

    def test_r_minor_major_side(self):
        # 弦 (0,0)->(10,10)，R=10：候选圆心 c1=(0,10) 与 c2=(10,0)
        # CCW 劣弧 / CW 优弧走 c1；CCW 优弧 / CW 劣弧走 c2
        sol = solve_arc((0, 0), (10, 10), clockwise=False,
                        i=None, j=None, r_word=10)
        self.assertLess(abs(sol["sweep"]), math.pi)
        self.assertGreater(sol["sweep"], 0)
        self.assertAlmostEqual(sol["center"][0], 0.0, places=6)
        self.assertAlmostEqual(sol["center"][1], 10.0, places=6)
        # CCW 优弧（R 负）
        sol_major = solve_arc((0, 0), (10, 10), clockwise=False,
                              i=None, j=None, r_word=-10)
        self.assertGreater(abs(sol_major["sweep"]), math.pi)
        self.assertAlmostEqual(sol_major["center"][0], 10.0, places=6)
        self.assertAlmostEqual(sol_major["center"][1], 0.0, places=6)
        # CW 劣弧同样走 c2
        sol_cw = solve_arc((0, 0), (10, 10), clockwise=True,
                           i=None, j=None, r_word=10)
        self.assertLess(abs(sol_cw["sweep"]), math.pi)
        self.assertAlmostEqual(sol_cw["center"][0], 10.0, places=6)
        self.assertAlmostEqual(sol_cw["center"][1], 0.0, places=6)
        # 弧长一致性：优弧+劣弧=整圆
        self.assertAlmostEqual(
            abs(sol_major["sweep"]) + abs(sol["sweep"]),
            2 * math.pi, places=6)

    def test_arc_no_solution_rolls_back(self):
        nc = ("G21 G90 G54\nM3 S1000\nG0 X0 Y0 Z0\n"
              "G2 X30 Y0 I10 J0\n")
        r = analyze_program(nc, cfg())
        self.assertIn("ARC_NO_SOLUTION", codes(r))
        # 回滚：位置停在 G0 的 (0,0,0)，motion_mode 也回到 G0
        st = r["final_state"]
        self.assertEqual((st["x"]["value_mm"], st["y"]["value_mm"]), (0, 0))
        self.assertEqual(st["motion_mode"], "rapid")
        # 阻断段不计入 executed
        blocked = [t for t in r["trajectory"]
                   if t.get("block_reason") == "arc_no_solution"]
        self.assertEqual(len(blocked), 1)

    def test_arc_length_and_bbox(self):
        nc = ("G21 G90 G54\nM3 S1000\nG0 X0 Y0 Z5\n"
              "G2 X20 Y0 I10 J0 F500\n")
        r = analyze_program(nc, cfg())
        self.assertEqual(codes(r), [])
        arc = [t for t in r["trajectory"] if t["type"] == "arc_cw"][0]
        self.assertAlmostEqual(arc["segment"]["length_mm"], 10 * math.pi,
                               places=4)
        self.assertAlmostEqual(r["bbox_program_mm"]["y_mm"][1], 10.0,
                               places=4)


class TestIssueShape(unittest.TestCase):
    def test_issue_carries_states_and_basis(self):
        r = analyze_program("G21 G90 G54\nG1 X10 F9999\n", cfg())
        iss = [i for i in r["issues"] if i["code"] == "FEED_OVER_LIMIT"][0]
        self.assertTrue(iss["source_line"])
        self.assertIn("G1 X10 F9999", iss["normalized"])
        self.assertEqual(iss["state_in"]["spindle_on"], False)
        self.assertEqual(iss["state_out"]["motion_mode"], "linear")
        self.assertIn("超过机床上限", iss["basis"])


class TestConfig(unittest.TestCase):
    def test_bad_config(self):
        with self.assertRaises(ConfigError) as cm:
            MachineConfig.from_dict(
                {"travel_x": [10, 0], "travel_y": [0, 1],
                 "travel_z": [0, 1], "max_feed_mm_min": 0,
                 "max_spindle_rpm": 0})
        self.assertTrue(cm.exception.errors)


class TestCompare(unittest.TestCase):
    def test_introduced_resolved(self):
        base = analyze_program(
            "G21 G90 G54\nG1 X10\n", cfg())  # 主轴未转 + 进给未设
        good = analyze_program(
            "G21 G90 G54\nM3 S1000\nG1 X10 F500\n", cfg())
        cmp = compare_reports(base, good, "v1", "v2")
        self.assertEqual(cmp["issue_counts"]["baseline_total"], 2)
        self.assertEqual(cmp["issue_counts"]["candidate_total"], 0)
        self.assertEqual(cmp["issue_counts"]["resolved"], 2)
        self.assertEqual(cmp["issue_counts"]["introduced"], 0)
        self.assertLess(cmp["risk"]["score_delta"], 0)

    def test_drill_compare_summary(self):
        a = ("G21 G90 G54\nM3 S1000\nG0 X0 Y0 Z20\n"
             "G81 R2 Z-5 F200\nX10\nX20\nG80\n")
        b = ("G21 G90 G54\nM3 S1000\nG0 X0 Y0 Z20\n"
             "G81 R2 Z-5 F200\nX10\nG80\n"
             "G0 X20 Y0 Z20\nG83 R2 Z-8 Q0\nG80\n")
        cmp = compare_reports(analyze_program(a, cfg()),
                              analyze_program(b, cfg()), "a", "b")
        dc = cmp["drill_cycles"]
        self.assertEqual(dc["delta"]["holes_total"], 0)      # 3 -> 3
        self.assertEqual(dc["delta"]["holes_blocked"], 1)    # 0 -> 1
        self.assertIn("G83", dc["by_cycle"])
        self.assertEqual(dc["by_cycle"]["G83"]["candidate_blocked"], 1)
        self.assertIn("total_drill_depth_mm", dc["delta"])
        self.assertIn("expanded_path_total_mm", dc["delta"])


class TestCannedCycles(unittest.TestCase):
    DRILL_HEADER = ("G21 G90 G54\nM3 S3000 F250\n"
                    "G0 X0 Y0 Z20\n")

    def _drill(self, body):
        return analyze_program(self.DRILL_HEADER + body + "\nG80\n", cfg())

    def test_g81_basic_expansion_and_counts(self):
        r = self._drill("G98 G81 R2 Z-5\nX20 Y0\nX40 Y0")
        s = r["drill_cycles"]["summary"]
        self.assertEqual(s["holes_total"], 3)
        self.assertEqual(s["holes_blocked"], 0)
        g = r["drill_cycles"]["groups"][0]
        self.assertEqual(g["cycle"], "G81")
        h1, h2 = g["holes"][0], g["holes"][1]
        # 每孔含定位（首孔为零长度）/到R/进给/回退
        self.assertEqual([m["action"] for m in h1["moves"]],
                         ["position", "approach_r", "drill_feed",
                          "retract_initial"])
        self.assertEqual(h1["moves"][0]["length_mm"], 0.0)
        # 孔间定位长度归入对应孔：孔2 有 20mm 水平定位
        self.assertEqual(h2["moves"][0]["action"], "position")
        self.assertAlmostEqual(h2["moves"][0]["length_mm"], 20.0)
        # G98 下快速 = 定位20 + 孔2在初始20到R2(18) + Z-5回初始20(25)
        self.assertAlmostEqual(h2["expanded_path_mm"]["rapid"],
                               20.0 + 18.0 + 25.0, places=5)
        self.assertEqual(s["total_drill_depth_mm"], 21.0)
        # 所有动作都带 hole_no
        for h in g["holes"]:
            self.assertTrue(all(m.get("hole_no") == h["hole_no"]
                                for m in h["moves"]))

    def test_g82_dwell_and_g83_peck(self):
        r = self._drill("G82 R2 Z-5 P500\nX10\n")
        h = r["drill_cycles"]["groups"][0]["holes"][0]
        self.assertAlmostEqual(h["dwell_s"], 0.5)
        self.assertIn("dwell_bottom", [m["action"] for m in h["moves"]])

        r2 = self._drill("G83 R2 Z-8 Q3\nX10\n")
        h2 = r2["drill_cycles"]["groups"][0]["holes"][0]
        actions = [m["action"] for m in h2["moves"]]
        self.assertEqual(actions.count("peck_drill_1"), 1)
        self.assertIn("peck_retract_1", actions)
        # 进给长度 = 钻深 + 每步 0.1mm 预留补回
        self.assertGreater(h2["expanded_path_mm"]["cutting"], 10.0)

    def test_g91_l_repeats_and_g90_l_same_position(self):
        r = self._drill("G91 G99 G81 X20 L3 R-18 Z-7")
        holes = r["drill_cycles"]["groups"][0]["holes"]
        self.assertEqual([h["x_mm"] for h in holes], [20.0, 40.0, 60.0])
        self.assertTrue(all(h["return_plane"] == "G99" for h in holes))

        r2 = self._drill("G90 G98 G81 R2 Z-5 L2")
        holes2 = r2["drill_cycles"]["groups"][0]["holes"]
        self.assertEqual([(h["x_mm"], h["y_mm"]) for h in holes2],
                         [(0.0, 0.0), (0.0, 0.0)])

    def test_g99_positioning_below_safe_z_flagged_once_per_hole(self):
        c = MachineConfig.from_dict({
            "name": "t", "travel_x": [0, 300], "travel_y": [0, 200],
            "travel_z": [-50, 60], "safe_z": 10,
            "max_feed_mm_min": 3000, "max_spindle_rpm": 12000})
        r = analyze_program(
            self.DRILL_HEADER + "G99 G81 R2 Z-5\nX20 Y0\nX40 Y0\nG80\n", c)
        codes = [i["code"] for i in r["issues"]]
        # 孔2、孔3 在 R=2 横移各报一次，孔1无横移不报
        self.assertEqual(codes.count("RAPID_BELOW_SAFE_Z"), 2)
        self.assertTrue(all(i["details"].get("in_canned_cycle")
                            for i in r["issues"]
                            if i["code"] == "RAPID_BELOW_SAFE_Z"))

    def test_blocking_missing_params_q_p_l_plane(self):
        cases = [
            ("G81 Z-5\n", "CYCLE_MISSING_PARAMS"),
            ("G83 R2 Z-8 Q0\n", "CYCLE_BAD_PARAM"),
            ("G82 R2 Z-5 P-1\n", "CYCLE_BAD_PARAM"),
            ("G81 R2 Z-5 L0\n", "CYCLE_BAD_PARAM"),
            ("G81 R-8 Z-5\n", "CYCLE_PLANE_CONFLICT"),
        ]
        for body, code in cases:
            r = analyze_program(
                self.DRILL_HEADER + body + "G80\n", cfg())
            self.assertIn(code, [i["code"] for i in r["issues"]], body)
            self.assertEqual(r["drill_cycles"]["summary"]["holes_drilled"], 0)

    def test_params_completed_later_and_l_without_xy_triggers(self):
        r = self._drill("G81\nR2\nZ-5\nX10\nX20 L0")
        # 定义行缺 Z/R 阻断 1 孔；纯 R/Z 参数行不钻孔；补齐后 X10 钻 1 孔；
        # L0 无 XY 也触发并阻断
        s = r["drill_cycles"]["summary"]
        self.assertEqual((s["holes_total"], s["holes_drilled"],
                          s["holes_blocked"]), (3, 1, 2))

    def test_no_inheritable_state(self):
        r = analyze_program(
            "G21 G90 G54\nM3 S2000\nG0 Z20\n"
            "G81 R2 Z-5 F200\nG80\n", cfg())  # XY 从未建立
        self.assertIn("CYCLE_NO_INHERITABLE_STATE",
                      [i["code"] for i in r["issues"]])

    def test_cycle_process_issues_deduped_per_trigger_line(self):
        # 不给 M3/F：一个 G83 孔多个进给动作，只报 1 条主轴 + 1 条进给
        r = analyze_program(
            "G21 G90 G54\nG0 X0 Y0 Z20\nG83 R2 Z-11 Q2\nG91 X10 L2\nG80\n",
            cfg())
        codes = [i["code"] for i in r["issues"]]
        # 定义行孔1 + 触发行孔2：每条触发行各 1 次，不因啄钻步数翻倍
        self.assertEqual(codes.count("SPINDLE_NOT_RUNNING"), 2)
        self.assertEqual(codes.count("FEED_UNSET"), 2)

    def test_300_alternating_definitions_stable_groups(self):
        lines = ["G21 G90 G54", "M3 S2000 F200", "G0 X0 Y0 Z20"]
        for i in range(300):
            cyc = "G81" if i % 2 == 0 else "G83"
            lines.append(f"{cyc} R2 Z-5 Q2 X{i} Y0")
            lines.append("G80")
        r = analyze_program("\n".join(lines) + "\nM5\n", cfg())
        groups = r["drill_cycles"]["groups"]
        self.assertEqual(len(groups), 300)
        self.assertEqual(r["drill_cycles"]["summary"]["holes_total"], 300)
        # 每组 cycle 与组内孔一致，不串组
        for g in groups:
            self.assertTrue(all(h["cycle"] == g["cycle"] for h in g["holes"]))

    def test_cancel_with_g80_and_motion_g(self):
        r = analyze_program(
            self.DRILL_HEADER + "G81 R2 Z-5\nX10\n"
            "G0 X30\nG1 X40 F100\n", cfg())
        self.assertIsNone(r["final_state"]["canned_cycle"])
        self.assertEqual(r["final_state"]["motion_mode"], "linear")
        # G80 显式取消
        r2 = analyze_program(
            self.DRILL_HEADER + "G81 R2 Z-5\nX10\nG80\n", cfg())
        self.assertIsNone(r2["final_state"]["canned_cycle"])
        self.assertIsNone(r2["final_state"]["motion_mode"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
