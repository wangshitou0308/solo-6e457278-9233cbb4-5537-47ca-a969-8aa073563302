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
        report = analyze_program("G60 X-\n", cfg())
        c = codes(report)
        self.assertIn("MALFORMED_LINE", c)
        self.assertIn("UNSUPPORTED_INSTRUCTION", c)
        unsup = [i for i in report["issues"]
                 if i["code"] == "UNSUPPORTED_INSTRUCTION"][0]
        self.assertIn("G60", unsup["details"]["unsupported_tokens"])
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


class TestPlaneArcs(unittest.TestCase):
    """G17/G18/G19 多平面圆弧与螺旋插补。"""

    HEADER = "G21 G90 G54\nM3 S1000\n"

    def _arc_entries(self, report):
        return [t for t in report["trajectory"]
                if "arc" in (t.get("segment") or {})]

    def test_plane_modal_and_normalized(self):
        r = analyze_program(
            self.HEADER + "G0 X30 Y10 Z20\nG18\nG2 X10 Z20 I-10 K0 F500\n",
            cfg())
        st = r["final_state"]
        self.assertEqual(st["plane"], "G18")
        # 平面行本身为纯设定段
        plane_line = [t for t in r["trajectory"] if t["line_no"] == 4][0]
        self.assertEqual(plane_line["type"], "setting")
        self.assertEqual(plane_line["normalized"], "G18")
        # 未写平面时默认 G17
        r2 = analyze_program(self.HEADER + "G0 X0 Y0 Z5\n", cfg())
        self.assertEqual(r2["final_state"]["plane"], "G17")

    def test_g18_arc_geometry_and_exact_bbox(self):
        # G18(XZ)：u=Z, v=X；圆心词 I(X)/K(Z)
        r = analyze_program(
            self.HEADER + "G0 X30 Y10 Z20\n"
            "G18 G2 X10 Z20 I-10 K0 F500\n", cfg())
        self.assertEqual(codes(r), [])
        arc = self._arc_entries(r)[0]["segment"]["arc"]
        self.assertEqual(arc["plane"], "G18(XZ)")
        self.assertEqual(arc["plane_code"], "G18")
        self.assertEqual(arc["direction"], "CW")
        self.assertEqual(arc["programming"], "I/K")
        self.assertEqual(arc["center_axes"], ["Z", "X"])
        self.assertEqual(arc["center_mm"], [20.0, 20.0])   # (Z, X)
        self.assertEqual(arc["center_3d_mm"], [20.0, 10.0, 20.0])
        self.assertEqual(arc["radius_mm"], 10.0)
        self.assertAlmostEqual(arc["sweep_deg"], -180.0)
        self.assertEqual(arc["perp_axis"], "Y")
        self.assertFalse(arc["helical"])
        seg = self._arc_entries(r)[0]["segment"]
        self.assertAlmostEqual(seg["length_mm"], 10 * math.pi, places=4)
        # 真实弧线包围盒：弧顶 Z=30（弦线两端都是 Z=20）
        self.assertEqual(r["bbox_program_mm"]["z_mm"], [20.0, 30.0])
        self.assertEqual(r["bbox_program_mm"]["x_mm"], [10.0, 30.0])
        self.assertEqual(r["bbox_program_mm"]["y_mm"], [10.0, 10.0])

    def test_g18_helical_y(self):
        r = analyze_program(
            self.HEADER + "G0 X30 Y10 Z20\n"
            "G18 G2 X10 Z20 I-10 K0 Y30 F500\n", cfg())
        self.assertEqual(codes(r), [])
        seg = self._arc_entries(r)[0]["segment"]
        arc = seg["arc"]
        self.assertTrue(arc["helical"])
        self.assertEqual(arc["perp_axis"], "Y")
        self.assertEqual(arc["perp_change_mm"], 20.0)
        self.assertAlmostEqual(
            seg["length_mm"],
            math.sqrt((10 * math.pi) ** 2 + 20 ** 2), places=4)

    def test_g19_arc_geometry(self):
        # G19(YZ)：u=Y, v=Z；圆心词 J(Y)/K(Z)
        r = analyze_program(
            self.HEADER + "G0 X10 Y30 Z20\n"
            "G19 G3 Y10 Z20 J-10 K0 F500\n", cfg())
        self.assertEqual(codes(r), [])
        arc = self._arc_entries(r)[0]["segment"]["arc"]
        self.assertEqual(arc["plane_code"], "G19")
        self.assertEqual(arc["direction"], "CCW")
        self.assertEqual(arc["programming"], "J/K")
        self.assertEqual(arc["center_axes"], ["Y", "Z"])
        self.assertEqual(arc["center_mm"], [20.0, 20.0])   # (Y, Z)
        self.assertEqual(arc["center_3d_mm"], [10.0, 20.0, 20.0])
        self.assertAlmostEqual(arc["sweep_deg"], 180.0)
        self.assertEqual(arc["perp_axis"], "X")
        self.assertEqual(r["bbox_program_mm"]["z_mm"], [20.0, 30.0])

    def test_g18_full_circle_with_helical(self):
        r = analyze_program(
            self.HEADER + "G0 X30 Y10 Z20\n"
            "G18 G2 X30 Z20 I-10 K0 Y15 F500\n", cfg())
        self.assertEqual(codes(r), [])
        seg = self._arc_entries(r)[0]["segment"]
        arc = seg["arc"]
        self.assertTrue(arc["full_circle"])
        self.assertAlmostEqual(abs(arc["sweep_deg"]), 360.0)
        self.assertTrue(arc["helical"])
        self.assertEqual(arc["perp_change_mm"], 5.0)
        self.assertAlmostEqual(
            seg["length_mm"],
            math.sqrt((20 * math.pi) ** 2 + 25), places=4)
        # 整圆包围盒覆盖整个圆
        self.assertEqual(r["bbox_program_mm"]["x_mm"], [10.0, 30.0])
        self.assertEqual(r["bbox_program_mm"]["z_mm"], [10.0, 30.0])

    def test_r_minor_major_in_g18(self):
        r = analyze_program(
            self.HEADER + "G0 X10 Y5 Z10\nG18\n"
            "G3 X20 Z20 R10 F500\n"   # 劣弧
            "G3 X10 Z10 R-10\n",      # 优弧（回到起点）
            cfg())
        arcs = [e["segment"]["arc"] for e in self._arc_entries(r)]
        self.assertEqual(len(arcs), 2)
        self.assertAlmostEqual(abs(arcs[0]["sweep_deg"]), 90.0)
        self.assertAlmostEqual(abs(arcs[1]["sweep_deg"]), 270.0)
        self.assertAlmostEqual(
            abs(arcs[0]["sweep_deg"]) + abs(arcs[1]["sweep_deg"]), 360.0)
        self.assertEqual(arcs[0]["programming"], "R")

    def test_mixed_center_params_blocks_and_rolls_back(self):
        r = analyze_program(
            self.HEADER + "G0 X0 Y0 Z20\n"
            "G18 G2 X10 Z20 I5 K0 R8 F500\n"
            "G2 X10 Y0 I5 J0 F500\n", cfg())
        iss = [i for i in r["issues"] if i["code"] == "ARC_NO_SOLUTION"]
        self.assertEqual(len(iss), 1)
        self.assertEqual(iss[0]["details"]["reason"], "mixed_center_params")
        self.assertEqual(iss[0]["details"]["plane"], "G18")
        self.assertEqual(iss[0]["line_no"], 4)
        # 回滚：阻断行的 state_out 与进入前一致（平面 G17、位置不动、模态 G0）
        blocked = [t for t in r["trajectory"]
                   if t.get("block_reason") == "arc_no_solution"]
        self.assertEqual(len(blocked), 1)
        out = blocked[0]["state_out"]
        self.assertEqual(out["plane"], "G17")
        self.assertEqual(out["motion_mode"], "rapid")
        self.assertEqual((out["x"]["value_mm"], out["z"]["value_mm"]), (0, 20))
        # 阻断行后的 G2 在 G17 下正常解算
        arcs = self._arc_entries(r)
        self.assertEqual(len(arcs), 1)
        self.assertEqual(arcs[0]["segment"]["arc"]["plane_code"], "G17")

    def test_center_word_not_in_plane(self):
        cases = [
            ("G2 X10 Y0 K5 F500\n", "G17", ["K"]),
            ("G18\nG2 X10 Z0 J5 F500\n", "G18", ["J"]),
            ("G19\nG2 Y10 Z0 I5 F500\n", "G19", ["I"]),
        ]
        for body, plane, bad in cases:
            r = analyze_program(
                self.HEADER + "G0 X0 Y0 Z20\n" + body, cfg())
            iss = [i for i in r["issues"] if i["code"] == "ARC_NO_SOLUTION"]
            self.assertEqual(len(iss), 1, body)
            d = iss[0]["details"]
            self.assertEqual(d["reason"], "center_word_not_in_plane")
            self.assertEqual(d["invalid_words"], bad)
            self.assertEqual(d["plane"], plane)

    def test_r_full_circle_and_radius_mismatch_blocked(self):
        r = analyze_program(
            self.HEADER + "G0 X10 Y10 Z20\nG2 X10 Y10 R5 F500\n", cfg())
        iss = [i for i in r["issues"] if i["code"] == "ARC_NO_SOLUTION"]
        self.assertEqual(len(iss), 1)
        self.assertIn("整圆", iss[0]["details"]["reason"])
        # 起终半径不一致
        r2 = analyze_program(
            self.HEADER + "G0 X0 Y0 Z20\nG19\nG2 Y30 Z20 J10 K0 F500\n",
            cfg())
        iss2 = [i for i in r2["issues"] if i["code"] == "ARC_NO_SOLUTION"]
        self.assertEqual(len(iss2), 1)
        self.assertIn("不一致", iss2[0]["details"]["reason"])
        self.assertEqual(iss2[0]["details"]["plane"], "G19")

    def test_true_arc_travel_check(self):
        # 弦两端都在行程内，但真实弧线鼓出 Y 上限
        c = MachineConfig.from_dict({
            "travel_x": [0, 100], "travel_y": [0, 15], "travel_z": [-10, 60],
            "safe_z": 2, "max_feed_mm_min": 3000, "max_spindle_rpm": 12000})
        r = analyze_program(
            self.HEADER + "G0 X40 Y10 Z10\nG2 X60 Y10 I10 J0 F500\n", c)
        oob = [i for i in r["issues"] if i["code"] == "OUT_OF_BOUNDS"]
        self.assertTrue(oob)
        self.assertEqual(oob[0]["details"]["axis"], "Y")
        self.assertAlmostEqual(oob[0]["details"]["value_mm"], 20.0)

    def test_arcs_summary_and_blocked_count(self):
        r = analyze_program(
            self.HEADER + "G0 X0 Y0 Z20\n"
            "G2 X0 Y0 I10 J0 F500\n"          # G17 整圆
            "G3 X20 Y0 I10 J0 Z15\n"          # G17 螺旋
            "G18\nG2 X0 Z15 I-10 K0\n"        # G18
            "G17\nG2 X50 Y0 I5 J0 R8\n",      # 阻断
            cfg())
        a = r["arcs"]
        self.assertEqual(a["by_plane"]["G17"]["count"], 2)
        self.assertEqual(a["by_plane"]["G17"]["helical_count"], 1)
        self.assertEqual(a["by_plane"]["G17"]["full_circle_count"], 1)
        self.assertEqual(a["by_plane"]["G18"]["count"], 1)
        self.assertEqual(a["by_plane"]["G19"]["count"], 0)
        self.assertEqual(a["total"]["count"], 3)
        self.assertEqual(a["blocked_count"], 1)
        self.assertAlmostEqual(
            a["by_plane"]["G17"]["arc_length_mm"],
            20 * math.pi + 10 * math.pi, places=4)

    def test_cycle_only_expands_in_g17(self):
        r = analyze_program(
            self.HEADER + "G0 X0 Y0 Z20\n"
            "G18\n"
            "G81 R2 Z-5 F200\n"   # G18 下定义：孔阻断，循环仍登记
            "G17\n"
            "X10\nX20\n"          # G17 恢复后正常钻孔
            "G80\n", cfg())
        c = codes(r)
        self.assertEqual(c.count("CYCLE_PLANE_NOT_G17"), 1)
        s = r["drill_cycles"]["summary"]
        self.assertEqual((s["holes_total"], s["holes_drilled"],
                          s["holes_blocked"]), (3, 2, 1))
        hole = r["drill_cycles"]["groups"][0]["holes"][0]
        self.assertEqual(hole["status"], "blocked")
        self.assertEqual(hole["block_codes"], ["CYCLE_PLANE_NOT_G17"])
        self.assertIn("G17", hole["basis"])
        # 触发行也阻断
        r2 = analyze_program(
            self.HEADER + "G0 X0 Y0 Z20\nG81 R2 Z-5 F200\n"
            "G19\nX10\nG80\n", cfg())
        self.assertIn("CYCLE_PLANE_NOT_G17", codes(r2))
        self.assertEqual(r2["drill_cycles"]["summary"]["holes_drilled"], 1)

    def test_compare_arcs_by_plane(self):
        a = (self.HEADER + "G0 X0 Y0 Z20\nG2 X10 Y0 I5 J0 F500\n")
        b = (self.HEADER + "G0 X0 Y0 Z20\nG2 X10 Y0 I5 J0 F500\n"
             "G18\nG2 X0 Z20 I-5 K0\n")
        cmp = compare_reports(analyze_program(a, cfg()),
                              analyze_program(b, cfg()), "a", "b")
        arcs = cmp["arcs"]
        self.assertEqual(arcs["by_plane"]["G17"]["delta_count"], 0)
        self.assertEqual(arcs["by_plane"]["G18"]["baseline_count"], 0)
        self.assertEqual(arcs["by_plane"]["G18"]["candidate_count"], 1)
        self.assertEqual(arcs["by_plane"]["G18"]["delta_count"], 1)
        self.assertGreater(arcs["by_plane"]["G18"]["delta_arc_length_mm"], 0)
        self.assertEqual(arcs["total"]["delta_count"], 1)
        self.assertEqual(arcs["blocked"]["delta"], 0)


class TestNoEndpointArcs(unittest.TestCase):
    """无 XYZ 终点词的圆心整圆（G2 I10 J0）必须生成完整弧段。"""

    HEADER = "G21 G90 G54\nM3 S1000\n"

    def test_full_circle_without_endpoint_words(self):
        r = analyze_program(
            self.HEADER + "G0 X20 Y20 Z5\nG2 I10 J0 F500\n", cfg())
        self.assertEqual(codes(r), [])
        e = [t for t in r["trajectory"] if t["line_no"] == 4][0]
        self.assertEqual(e["type"], "arc_cw")
        self.assertTrue(e["executed"])
        arc = e["segment"]["arc"]
        self.assertTrue(arc["full_circle"])
        self.assertAlmostEqual(abs(arc["sweep_deg"]), 360.0)
        self.assertAlmostEqual(e["segment"]["length_mm"], 20 * math.pi,
                               places=4)
        # 切削长度与弧段统计均计入，不再绕过预检
        self.assertAlmostEqual(r["path_length_mm"]["cutting"],
                               20 * math.pi, places=4)
        self.assertEqual(r["arcs"]["total"]["count"], 1)
        self.assertEqual(r["arcs"]["total"]["full_circle_count"], 1)
        # 终点即起点，位置不变
        st = r["final_state"]
        self.assertEqual((st["x"]["value_mm"], st["y"]["value_mm"]), (20, 20))
        # 包围盒按真实弧线覆盖整圆（圆心 (30,20)，半径 10）
        self.assertEqual(r["bbox_program_mm"]["x_mm"], [20.0, 40.0])
        self.assertEqual(r["bbox_program_mm"]["y_mm"], [10.0, 30.0])

    def test_modal_arc_without_endpoint_words(self):
        r = analyze_program(
            self.HEADER + "G0 X20 Y20 Z5\nG2\nI10 J0 F500\n", cfg())
        e = [t for t in r["trajectory"] if t["line_no"] == 5][0]
        self.assertEqual(e["type"], "arc_cw")
        self.assertIn("模态", e["normalized"])
        self.assertTrue(e["segment"]["arc"]["full_circle"])

    def test_no_endpoint_full_circle_in_g18(self):
        r = analyze_program(
            self.HEADER + "G0 X20 Y20 Z20\nG18\nG2 I10 K0 F500\n", cfg())
        self.assertEqual(codes(r), [])
        e = [t for t in r["trajectory"] if t["line_no"] == 5][0]
        self.assertEqual(e["segment"]["arc"]["plane_code"], "G18")
        self.assertTrue(e["segment"]["arc"]["full_circle"])
        self.assertEqual(r["bbox_program_mm"]["x_mm"], [20.0, 40.0])
        self.assertEqual(r["bbox_program_mm"]["z_mm"], [10.0, 30.0])

    def test_r_without_endpoint_blocked(self):
        # R 编程整圆（无终点词时终点即起点）仍阻断
        r = analyze_program(
            self.HEADER + "G0 X20 Y20 Z5\nG2 R10 F500\n", cfg())
        self.assertIn("ARC_NO_SOLUTION", codes(r))
        e = [t for t in r["trajectory"] if t["line_no"] == 4][0]
        self.assertEqual(e["type"], "blocked")

    def test_mixed_without_endpoint_blocked(self):
        r = analyze_program(
            self.HEADER + "G0 X20 Y20 Z5\nG2 I5 R10 F500\n", cfg())
        iss = [i for i in r["issues"] if i["code"] == "ARC_NO_SOLUTION"]
        self.assertEqual(len(iss), 1)
        self.assertEqual(iss[0]["details"]["reason"], "mixed_center_params")

    def test_bare_motion_g_still_setting(self):
        # 只有 G2（无圆心词/R）仍是纯设定段
        r = analyze_program("G21 G90 G54\nG2\n", cfg())
        e = [t for t in r["trajectory"] if t["line_no"] == 2][0]
        self.assertEqual(e["type"], "setting")

    def test_no_endpoint_arc_process_checks(self):
        # 无 M3/F 的整圆：主轴/进给检查照常，且带平面信息
        r = analyze_program("G21 G90 G54\nG0 X20 Y20 Z5\nG2 I10 J0\n", cfg())
        c = codes(r)
        self.assertIn("SPINDLE_NOT_RUNNING", c)
        self.assertIn("FEED_UNSET", c)
        for code in ("SPINDLE_NOT_RUNNING", "FEED_UNSET"):
            iss = [i for i in r["issues"] if i["code"] == code][0]
            self.assertEqual(iss["details"]["plane"], "G17")


class TestPlaneIssueTagging(unittest.TestCase):
    """弧段问题带平面信息；对比给出分平面问题增减。"""

    OOB_CFG = {
        "travel_x": [0, 100], "travel_y": [0, 100], "travel_z": [-10, 14],
        "safe_z": 2, "max_feed_mm_min": 3000, "max_spindle_rpm": 12000}
    BAD = ("G21 G90 G54\nM3 S1000\nG0 X50 Y10 Z5\n"
           "G18\nG2 X30 Z5 I-10 K0 F500\n")   # 弧顶 Z=15 越出 z_max=14
    GOOD = ("G21 G90 G54\nM3 S1000\nG0 X50 Y10 Z5\n"
            "G18\nG2 X40 Z5 I-5 K0 F500\n")  # 弧顶 Z=10，合规

    def _cfg(self):
        return MachineConfig.from_dict(dict(self.OOB_CFG))

    def test_out_of_bounds_on_arc_carries_plane(self):
        r = analyze_program(self.BAD, self._cfg())
        oob = [i for i in r["issues"] if i["code"] == "OUT_OF_BOUNDS"]
        self.assertEqual(len(oob), 1)
        self.assertEqual(oob[0]["details"]["plane"], "G18")
        self.assertAlmostEqual(oob[0]["details"]["value_mm"], 15.0)
        # 直线段问题不带平面
        r2 = analyze_program("G21 G90 G54\nG1 X10\n", cfg())
        for i in r2["issues"]:
            self.assertNotIn("plane", i["details"])

    def test_compare_arcs_issue_deltas(self):
        cmp = compare_reports(analyze_program(self.BAD, self._cfg()),
                              analyze_program(self.GOOD, self._cfg()),
                              "bad", "good")
        g18 = cmp["arcs"]["by_plane"]["G18"]
        self.assertEqual(g18["baseline_issues"], 1)
        self.assertEqual(g18["candidate_issues"], 0)
        self.assertEqual(g18["delta_issues"], -1)
        self.assertEqual(g18["resolved_issues"], 1)
        self.assertEqual(g18["introduced_issues"], 0)
        total = cmp["arcs"]["total"]
        self.assertEqual(total["resolved_issues"], 1)
        self.assertEqual(total["introduced_issues"], 0)
        # 反向：好 -> 坏，新增 1
        cmp2 = compare_reports(analyze_program(self.GOOD, self._cfg()),
                               analyze_program(self.BAD, self._cfg()),
                               "good", "bad")
        g18b = cmp2["arcs"]["by_plane"]["G18"]
        self.assertEqual(g18b["delta_issues"], 1)
        self.assertEqual(g18b["introduced_issues"], 1)
        self.assertEqual(g18b["resolved_issues"], 0)


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


class TestMultiWcs(unittest.TestCase):
    """G54-G59 多工件坐标系：配置、模态切换、未配置坐标系、分系统计。"""

    BASE = {
        "name": "wcs-test",
        "travel_x": [0, 300], "travel_y": [0, 200], "travel_z": [-50, 60],
        "safe_z": 10, "max_feed_mm_min": 3000, "max_spindle_rpm": 12000,
    }

    def cfg_wcs(self):
        return MachineConfig.from_dict(dict(
            self.BASE,
            wcs_offsets={"G54": {"x": 0, "y": 0, "z": 0},
                         "G55": {"x": 100, "y": 50, "z": 0}}))

    # -- 配置解析 ------------------------------------------------------------

    def test_legacy_offsets_fold_into_g54(self):
        c = MachineConfig.from_dict(dict(
            self.BASE, offset_x=-5, offset_y=2, offset_z=1))
        self.assertEqual(c.wcs_offsets["G54"], {"x": -5.0, "y": 2.0, "z": 1.0})
        self.assertEqual((c.offset_x, c.offset_y, c.offset_z), (-5, 2, 1))
        self.assertEqual(c.offset_for("G54"), (-5.0, 2.0, 1.0))
        self.assertIsNone(c.offset_for("G56"))     # 未配置
        self.assertIsNone(c.offset_for(None))
        # to_dict 回读（已有作业的配置仍可读取）
        c2 = MachineConfig.from_dict(c.to_dict())
        self.assertEqual(c2.wcs_offsets["G54"]["x"], -5.0)

    def test_explicit_g54_overrides_legacy(self):
        c = MachineConfig.from_dict(dict(
            self.BASE, offset_x=-5,
            wcs_offsets={"G54": {"x": 7}, "G56": {"y": 3}}))
        self.assertEqual(c.offset_x, 7.0)          # 显式 wcs_offsets.G54 优先
        self.assertEqual(c.wcs_offsets["G54"], {"x": 7.0, "y": 0.0, "z": 0.0})
        self.assertEqual(c.wcs_offsets["G56"], {"x": 0.0, "y": 3.0, "z": 0.0})

    def test_invalid_offset_locates_wcs_and_field(self):
        with self.assertRaises(ConfigError) as cm:
            MachineConfig.from_dict(dict(
                self.BASE, wcs_offsets={"G55": {"x": "abc"}}))
        self.assertTrue(any("wcs_offsets.G55.x" in e
                            for e in cm.exception.errors),
                        cm.exception.errors)
        # 未知坐标系名
        with self.assertRaises(ConfigError) as cm2:
            MachineConfig.from_dict(dict(
                self.BASE, wcs_offsets={"G60": {"x": 1}}))
        self.assertTrue(any("G60" in e for e in cm2.exception.errors))
        # 未知字段
        with self.assertRaises(ConfigError) as cm3:
            MachineConfig.from_dict(dict(
                self.BASE, wcs_offsets={"G54": {"w": 1}}))
        self.assertTrue(any("wcs_offsets.G54" in e
                            for e in cm3.exception.errors))

    # -- 模态切换 ------------------------------------------------------------

    def test_switch_recomputes_work_coords_machine_fixed(self):
        nc = ("G21 G90 G54\n"
              "G0 X10 Y20 Z30\n"   # 工件(10,20,30)，机床(10,20,30)
              "G55\n"              # 机床不动 -> 工件(-90,-30,30)
              "G0 X0 Y0\n")        # 工件(0,0,30) -> 机床(100,50,30)
        r = analyze_program(nc, self.cfg_wcs())
        self.assertEqual(codes(r), [])
        sw = [t for t in r["trajectory"] if t["line_no"] == 3][0]
        self.assertEqual(sw["type"], "setting")
        self.assertEqual(sw["normalized"], "G55")
        out = sw["state_out"]
        self.assertEqual(out["wcs"], "G55")
        self.assertEqual((out["x"]["value_mm"], out["y"]["value_mm"],
                          out["z"]["value_mm"]), (-90.0, -30.0, 30.0))
        self.assertEqual(out["wcs_offset_mm"], {"x": 100, "y": 50, "z": 0})
        st = r["final_state"]
        self.assertEqual(st["wcs"], "G55")
        self.assertEqual((st["x"]["value_mm"], st["y"]["value_mm"]), (0, 0))
        # 机床包围盒覆盖两个坐标系下的真实机床位置
        self.assertEqual(r["bbox_machine_mm"]["x_mm"], [10.0, 100.0])
        self.assertEqual(r["bbox_machine_mm"]["y_mm"], [20.0, 50.0])
        self.assertEqual(r["bbox_machine_mm"]["z_mm"], [30.0, 30.0])

    def test_segment_records_wcs_offset_and_machine_coords(self):
        nc = ("G21 G90 G54\n"
              "G0 X10 Y20 Z30\n"
              "G55\n"
              "G0 X0 Y0\n")
        r = analyze_program(nc, self.cfg_wcs())
        seg = [t for t in r["trajectory"] if t["line_no"] == 4][0]["segment"]
        self.assertEqual(seg["wcs"], "G55")
        self.assertTrue(seg["wcs_configured"])
        self.assertEqual(seg["offset_mm"], {"x": 100, "y": 50, "z": 0})
        self.assertEqual(seg["start_mm"], [-90.0, -30.0, 30.0])
        self.assertEqual(seg["end_mm"], [0.0, 0.0, 30.0])
        self.assertEqual(seg["start_machine_mm"], [10.0, 20.0, 30.0])
        self.assertEqual(seg["end_machine_mm"], [100.0, 50.0, 30.0])
        self.assertEqual(seg["points_machine_mm"][1], [100.0, 50.0, 30.0])
        # G54 段
        seg54 = [t for t in r["trajectory"] if t["line_no"] == 2][0]["segment"]
        self.assertEqual(seg54["wcs"], "G54")
        self.assertEqual(seg54["end_machine_mm"], [10.0, 20.0, 30.0])

    def test_out_of_bounds_uses_current_wcs_offset(self):
        # G55 偏置 (100,50,0)：工件 X250 -> 机床 X350 越出 x_max=300
        r = analyze_program(
            "G21 G90 G55\nG0 X250 Y0 Z20\n", self.cfg_wcs())
        oob = [i for i in r["issues"] if i["code"] == "OUT_OF_BOUNDS"]
        self.assertEqual(len(oob), 1)
        self.assertEqual(oob[0]["details"]["axis"], "X")
        self.assertAlmostEqual(oob[0]["details"]["value_mm"], 350.0)
        self.assertEqual(oob[0]["details"]["wcs"], "G55")
        self.assertIn("G55", oob[0]["basis"])

    # -- 未配置坐标系 ----------------------------------------------------------

    def test_unconfigured_wcs_marks_machine_unknown(self):
        nc = ("G21 G90 G55\n"        # G55 已配置 (100,50,0)
              "G0 X10 Y10 Z20\n"
              "G56\n"               # G56 未配置偏置
              "G0 X250 Y10 Z20\n"   # 若沿用 G55 偏置 -> 机床 X350 越界
              "G0 X200 Y0\n")
        r = analyze_program(nc, self.cfg_wcs())
        c = codes(r)
        self.assertIn("UNKNOWN_WCS", c)
        self.assertNotIn("OUT_OF_BOUNDS", c)   # 不沿用上一偏置
        unk = [i for i in r["issues"] if i["code"] == "UNKNOWN_WCS"]
        self.assertTrue(all(i["details"]["reason"] == "wcs_not_configured"
                            for i in unk))
        self.assertTrue(all(i["details"]["wcs"] == "G56" for i in unk))
        # 机床包围盒整体不可用并说明原因
        self.assertIsNone(r["bbox_machine_mm"])
        self.assertIn("未配置", r["machine_bbox_note"])
        # 分系统计：G56 未配置、机床包围盒未知，但工件路径仍累计
        g56 = r["wcs"]["by_wcs"]["G56"]
        self.assertFalse(g56["configured"])
        self.assertIsNone(g56["offset_mm"])
        self.assertIsNone(g56["machine_bbox_mm"])
        self.assertEqual(g56["issues"], 2)
        self.assertAlmostEqual(g56["path_length_mm"]["rapid"],
                               (50 ** 2 + 10 ** 2) ** 0.5, places=4)
        self.assertEqual(g56["path_length_mm"]["unknown_segments"], 1)
        # G55 段机床包围盒正常
        g55 = r["wcs"]["by_wcs"]["G55"]
        self.assertTrue(g55["configured"])
        self.assertEqual(g55["machine_bbox_mm"]["x_mm"], [110.0, 110.0])
        # 未配置坐标系段的机床坐标为 null
        seg = [t for t in r["trajectory"] if t["line_no"] == 5][0]["segment"]
        self.assertEqual(seg["wcs"], "G56")
        self.assertFalse(seg["wcs_configured"])
        self.assertIsNone(seg["offset_mm"])
        self.assertIsNone(seg["end_machine_mm"])
        self.assertIsNone(seg["points_machine_mm"])

    def test_wcs_report_section(self):
        nc = ("G21 G90 G54\nM3 S1000\nG0 X0 Y0 Z20\nG1 X10 F500\n"
              "G55\nG0 X0 Y0\nG1 X5 F500\n")
        r = analyze_program(nc, self.cfg_wcs())
        w = r["wcs"]
        self.assertEqual(w["used"], ["G54", "G55"])
        self.assertEqual(w["offsets_mm"]["G55"], {"x": 100, "y": 50, "z": 0})
        self.assertGreater(w["by_wcs"]["G54"]["path_length_mm"]["cutting"], 0)
        self.assertGreater(w["by_wcs"]["G55"]["path_length_mm"]["total"], 0)
        # 分系路径合计 = 全局
        tot = sum(w["by_wcs"][k]["path_length_mm"]["total"]
                  for k in ("G54", "G55"))
        self.assertAlmostEqual(tot, r["path_length_mm"]["total"], places=6)

    # -- 圆弧 / 螺旋 / 固定循环按当前坐标系生成机床轨迹 -------------------------

    def test_arc_machine_bbox_uses_current_offset(self):
        nc = ("G21 G90 G55\nM3 S1000\nG0 X0 Y0 Z20\n"
              "G2 X0 Y0 I10 J0 F500\n")   # 整圆：工件 x[0,20] y[-10,10]
        r = analyze_program(nc, self.cfg_wcs())
        self.assertEqual(codes(r), [])
        self.assertEqual(r["bbox_machine_mm"]["x_mm"], [100.0, 120.0])
        self.assertEqual(r["bbox_machine_mm"]["y_mm"], [40.0, 60.0])
        seg = [t for t in r["trajectory"]
               if t.get("segment", {}).get("arc")][0]["segment"]
        self.assertEqual(seg["wcs"], "G55")
        self.assertIsNotNone(seg["points_machine_mm"])

    def test_cycle_under_g55(self):
        nc = ("G21 G90 G55\nM3 S3000\nG0 X0 Y0 Z20\n"
              "G81 R2 Z-5 F200\nX10\nG80\n")
        r = analyze_program(nc, self.cfg_wcs())
        self.assertEqual(codes(r), [])
        holes = r["drill_cycles"]["groups"][0]["holes"]
        self.assertEqual([h["wcs"] for h in holes], ["G55", "G55"])
        self.assertEqual(holes[0]["machine_x_mm"], 100.0)
        self.assertEqual(holes[1]["machine_x_mm"], 110.0)
        # 循环段与展开动作带坐标系与机床坐标
        seg = [t for t in r["trajectory"]
               if (t.get("segment") or {}).get("kind")
               == "canned_cycle"][0]["segment"]
        self.assertEqual(seg["wcs"], "G55")
        mv = seg["moves_mm"][0]
        self.assertIn("start_machine_mm", mv)
        self.assertEqual(mv["start_machine_mm"][0],
                         mv["start_mm"][0] + 100)
        # 机床包围盒含 G55 偏置后的孔位
        self.assertEqual(r["bbox_machine_mm"]["x_mm"], [100.0, 110.0])
        # 分系统计含循环展开路径
        g55 = r["wcs"]["by_wcs"]["G55"]
        self.assertGreater(g55["path_length_mm"]["cutting"], 0)

    def test_safe_z_judged_in_current_work_coords(self):
        # G55 Z 偏置 +20：工件 Z5（机床 Z25）仍按工件坐标判定低于 safe_z=10
        c = MachineConfig.from_dict(dict(
            self.BASE, wcs_offsets={"G55": {"x": 0, "y": 0, "z": 20}}))
        r = analyze_program(
            "G21 G90 G55\nG0 X0 Y0 Z20\nG0 Z5\n", c)
        iss = [i for i in r["issues"] if i["code"] == "RAPID_BELOW_SAFE_Z"]
        self.assertEqual(len(iss), 1)
        self.assertAlmostEqual(iss[0]["details"]["ref_z_mm"], 5.0)
        self.assertEqual(iss[0]["details"]["wcs"], "G55")

    # -- 对比 ------------------------------------------------------------------

    def test_compare_wcs_section(self):
        a = "G21 G90 G54\nM3 S1000\nG0 X0 Y0 Z20\nG1 X10 F500\n"
        b = ("G21 G90 G54\nM3 S1000\nG0 X0 Y0 Z20\nG1 X10 F500\n"
             "G55\nG0 X0 Y0\nG1 X5 F500\n")
        cmp = compare_reports(analyze_program(a, self.cfg_wcs()),
                              analyze_program(b, self.cfg_wcs()), "a", "b")
        g55 = cmp["wcs"]["by_wcs"]["G55"]
        self.assertEqual(g55["baseline_path_mm"]["total"], 0.0)
        self.assertGreater(g55["candidate_path_mm"]["total"], 0.0)
        self.assertGreater(g55["delta_path_mm"]["total"], 0)
        self.assertIsNotNone(g55["candidate_machine_bbox_mm"])
        self.assertIsNone(g55["baseline_machine_bbox_mm"])
        self.assertIn("total", cmp["wcs"])


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
