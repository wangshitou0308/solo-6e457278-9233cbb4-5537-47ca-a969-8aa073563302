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


if __name__ == "__main__":
    unittest.main(verbosity=2)
