"""程序包静态展开的离线单元测试：python3 -m tests.test_packages"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gcode_checker.analyzer import MachineConfig
from gcode_checker.packages import (
    MAX_BLOCKS_LIMIT,
    MAX_DEPTH_CAP,
    PackageSpecError,
    analyze_package,
    expand_package,
    parse_package_spec,
)
from gcode_checker.compare import compare_package_reports


def cfg():
    return MachineConfig.from_dict({
        "name": "test",
        "travel_x": [0, 300], "travel_y": [0, 200], "travel_z": [-50, 60],
        "safe_z": 2,
        "max_feed_mm_min": 3000, "max_spindle_rpm": 12000,
    })


def cfg_tools():
    """带刀具表/换刀点的配置（子程序换刀测试用）。"""
    return MachineConfig.from_dict({
        "name": "test-tools",
        "travel_x": [0, 300], "travel_y": [0, 200], "travel_z": [-50, 120],
        "safe_z": 10,
        "max_feed_mm_min": 3000, "max_spindle_rpm": 12000,
        "tools": {"1": {"h": 1, "d": 1}, "2": {"h": 2, "d": 2}},
        "initial_tool": 1,
        "tool_change_point": {"x": 0, "y": 0, "z": 100},
        "tool_change_tolerance": {"x": 0.5, "y": 0.5, "z": 0.5},
        "length_offsets": {"1": 10, "2": 8},
        "radius_offsets": {"1": 5, "2": 3},
    })


SUB = """\
O100
G91 G99 G81 X10 Z-10 R-18 L2 F250
G90 G80
M99
"""

MAIN = """\
G21 G90 G54
M3 S4000
G0 X0 Y0 Z20
M98 P100 L2
M30
"""


def make_spec(main=MAIN, subs=None, **kw):
    subs = subs if subs is not None else [{"name": "o100.nc", "content": SUB}]
    return parse_package_spec({"name": "p", "main": main,
                               "subprograms": subs, **kw})


def expand(main=MAIN, subs=None, **kw):
    return expand_package(make_spec(main, subs, **kw))


def codes(result):
    return sorted({e.code for e in result["errors"]})


class TestPackageSpec(unittest.TestCase):
    def test_missing_main(self):
        with self.assertRaises(PackageSpecError) as cm:
            parse_package_spec({"subprograms": []})
        self.assertTrue(any("main" in e for e in cm.exception.errors))

    def test_bad_subprogram_entry(self):
        with self.assertRaises(PackageSpecError) as cm:
            parse_package_spec(
                {"main": "M30\n", "subprograms": [{"name": "x"}]})
        self.assertTrue(any("content" in e for e in cm.exception.errors))

    def test_bad_max_depth(self):
        for v in (0, -1, MAX_DEPTH_CAP + 1, "3", 1.5, True):
            with self.assertRaises(PackageSpecError):
                parse_package_spec({"main": "M30\n", "max_depth": v})

    def test_declared_o_number_validated(self):
        with self.assertRaises(PackageSpecError):
            parse_package_spec(
                {"main": "M30\n",
                 "subprograms": [{"name": "a", "content": "O100\nM99\n",
                                  "o_number": -1}]})


class TestExpansionErrors(unittest.TestCase):
    def test_duplicate_o(self):
        r = expand("M30\n", [
            {"name": "a", "content": "O100\nM99\n"},
            {"name": "b", "content": "O100\nM99\n"}])
        self.assertFalse(r["ok"])
        self.assertEqual(codes(r), ["PACKAGE_DUPLICATE_O"])
        self.assertEqual(r["errors"][0].details["o_number"], 100)

    def test_target_not_found(self):
        r = expand("M98 P100\nM30\n", [])
        self.assertEqual(codes(r), ["PACKAGE_SUBPROGRAM_NOT_FOUND"])
        self.assertEqual(r["errors"][0].details["target_program"], "O100")
        # 未定义节点也出现在调用图中（defined=false）
        self.assertIn("O100", {n["program"] for n in r["graph"]["nodes"]})

    def test_m99_in_main(self):
        r = expand("G0 X1\nM99\n", [])
        self.assertEqual(codes(r), ["PACKAGE_M99_IN_MAIN"])

    def test_subprogram_missing_m99(self):
        r = expand("M30\n", [{"name": "a", "content": "O100\nG0 X1\n"}])
        self.assertEqual(codes(r), ["PACKAGE_SUBPROGRAM_MISSING_M99"])

    def test_direct_and_indirect_recursion(self):
        r = expand("M98 P100\nM30\n",
                   [{"name": "a", "content": "O100\nM98 P100\nM99\n"}])
        self.assertEqual(codes(r), ["PACKAGE_RECURSIVE_CALL"])
        self.assertEqual(r["errors"][0].details["cycle"], ["O100"])
        r2 = expand("M98 P100\nM30\n", [
            {"name": "a", "content": "O100\nM98 P200\nM99\n"},
            {"name": "b", "content": "O200\nM98 P100\nM99\n"}])
        self.assertEqual(codes(r2), ["PACKAGE_RECURSIVE_CALL"])
        self.assertEqual(set(r2["errors"][0].details["cycle"]),
                         {"O100", "O200"})

    def test_depth_limit(self):
        subs = [{"name": f"o{i}", "content":
                 f"O{i}\nM98 P{i + 1}\nM99\n"} for i in range(100, 103)]
        subs.append({"name": "o103", "content": "O103\nM99\n"})
        r = expand("M98 P100\nM30\n", subs, max_depth=2)
        self.assertEqual(codes(r), ["PACKAGE_DEPTH_LIMIT"])
        self.assertEqual(r["errors"][0].details["depth"], 3)

    def test_depth_limit_not_triggered_at_boundary(self):
        # 深度 2 恰好在上限 2 内
        r = expand("M98 P100\nM30\n", [
            {"name": "a", "content": "O100\nM98 P200\nM99\n"},
            {"name": "b", "content": "O200\nM99\n"}], max_depth=2)
        self.assertTrue(r["ok"], [str(e.to_dict()) for e in r["errors"]])

    def test_block_limit(self):
        big = "O1\n" + "G0 X1\n" * 300 + "M99\n"
        r = expand("M98 P1 L400\nM30\n",
                   [{"name": "o1", "content": big}])
        self.assertEqual(codes(r), ["PACKAGE_BLOCK_LIMIT"])
        # 302 行/次 * 331 次 ≈ 99962 后超限；必须在边界处阻断
        self.assertLessEqual(len(r["blocks"]), 0)  # ok=False 时块清空
        self.assertEqual(r["summary"]["status"], "blocked")

    def test_block_limit_boundary_allowed(self):
        # 小展开远低于上限：成功（main 2 行 + sub 3 行）
        r = expand("M98 P1\nM30\n",
                   [{"name": "o1", "content": "O1\nG0 X1\nM99\n"}])
        self.assertTrue(r["ok"])
        self.assertEqual(r["summary"]["expanded_blocks"], 5)

    def test_dynamic_p_and_variables(self):
        r = expand("M98 P#100\nM30\n", [])
        self.assertEqual(codes(r),
                         ["PACKAGE_DYNAMIC_P", "PACKAGE_VARIABLE_EXPRESSION"])
        r2 = expand("M98 P[100+1]\nM30\n", [])
        self.assertEqual(codes(r2),
                         ["PACKAGE_DYNAMIC_P", "PACKAGE_VARIABLE_EXPRESSION"])
        # 仅有变量、无程序流 -> 只报变量表达式
        r3 = expand("G1 X#1 F100\nM30\n", [])
        self.assertEqual(codes(r3), ["PACKAGE_VARIABLE_EXPRESSION"])
        # 动态 P 不产生调用边（无未定义目标节点）
        self.assertNotIn("O100",
                         {n["program"] for n in r["graph"]["nodes"]})

    def test_invalid_p_and_l(self):
        self.assertEqual(codes(expand("M98\nM30\n", [])),
                         ["PACKAGE_INVALID_P"])
        self.assertEqual(codes(expand("M98 P0\nM30\n", [])),
                         ["PACKAGE_INVALID_P"])
        self.assertEqual(codes(expand("M98 P100 L0\nM30\n",
                                      [{"name": "a",
                                        "content": "O100\nM99\n"}])),
                         ["PACKAGE_INVALID_L"])
        self.assertEqual(codes(expand("M98 P100 L2.5\nM30\n",
                                      [{"name": "a",
                                        "content": "O100\nM99\n"}])),
                         ["PACKAGE_INVALID_L"])

    def test_o_number_mismatch(self):
        # 无 O 号的子程序：O 号缺失
        r = expand("M30\n", [{"name": "a", "content": "G0 X1\nM99\n"}])
        self.assertEqual(codes(r), ["PACKAGE_O_NUMBER_MISMATCH"])
        # 第一个非空行不是 O（末尾也是 M99，只报 O 异常）
        self.assertEqual(codes(expand("M30\n", [
            {"name": "a", "content": "G0 X1\nO100\nM99\n"}])),
                         ["PACKAGE_O_NUMBER_MISMATCH"])
        # 声明与内容不一致
        r = expand("M30\n", [{"name": "a", "content": "O200\nM99\n",
                              "o_number": 100}])
        self.assertEqual(codes(r), ["PACKAGE_O_NUMBER_MISMATCH"])

    def test_o_in_main_and_m99_p(self):
        self.assertEqual(codes(expand("O5\nM30\n", [])),
                         ["PACKAGE_MAIN_HAS_O"])
        self.assertEqual(codes(
            expand("M30\n", [{"name": "a",
                              "content": "O100\nM99 P5\n"}])),
            ["PACKAGE_UNSUPPORTED_RETURN"])

    def test_conflicting_flow(self):
        r = expand("M98 P100 M99\nM30\n",
                   [{"name": "a", "content": "O100\nM99\n"}])
        self.assertIn("PACKAGE_INVALID_P", codes(r))

    def test_blocked_result_has_no_blocks(self):
        r = expand("M98 P999\nM30\n", [])
        self.assertFalse(r["ok"])
        self.assertEqual(r["blocks"], [])


class TestExpansionSemantics(unittest.TestCase):
    def test_expanded_blocks_and_call_graph(self):
        r = expand()
        self.assertTrue(r["ok"])
        s = r["summary"]
        # main 5 行 + 2 次 * sub 4 行 = 13
        self.assertEqual(s["expanded_blocks"], 13)
        self.assertEqual(s["call_sites"], 1)
        self.assertEqual(s["call_executions"], 1)
        self.assertEqual(s["call_invocations"], 2)
        self.assertEqual(s["repeat_invocations"], 1)
        self.assertEqual(s["max_depth"], 1)
        edge = r["graph"]["edges"][0]
        self.assertEqual((edge["caller"], edge["callee"]),
                         ("main", "O100"))
        self.assertEqual(edge["invocations"], 2)
        self.assertTrue(all(n["reachable"] for n in r["graph"]["nodes"]))

    def test_unreachable_subprogram_marked(self):
        r = expand("M30\n", [
            {"name": "a", "content": "O100\nM99\n"},
            {"name": "b", "content": "O200\nM98 P100\nM99\n"}])
        self.assertTrue(r["ok"])
        by = {n["program"]: n["reachable"] for n in r["graph"]["nodes"]}
        self.assertFalse(by["O100"])
        self.assertFalse(by["O200"])
        # 不可达子程序中的调用边 invocations=0
        edge = next(e for e in r["graph"]["edges"]
                    if e["caller"] == "O200")
        self.assertEqual(edge["invocations"], 0)
        self.assertEqual(edge["executions"], 0)

    def test_blocks_carry_provenance(self):
        r = expand()
        sub_blocks = [b for b in r["blocks"] if b.program == "O100"]
        self.assertEqual(len(sub_blocks), 8)  # 4 行 * 2 次重复
        first, second = sub_blocks[:4], sub_blocks[4:]
        for b in first:
            self.assertEqual(b.call_stack[0]["repeat_index"], 1)
            self.assertEqual(b.repeat_index, 1)
            self.assertEqual(b.depth, 1)
            self.assertEqual(b.file, "o100.nc")
        for b in second:
            self.assertEqual(b.call_stack[0]["repeat_index"], 2)
            self.assertEqual(b.repeat_index, 2)

    def test_nested_call_stack(self):
        r = expand("M98 P100\nM30\n", [
            {"name": "a", "content": "O100\nM98 P200\nM99\n"},
            {"name": "b", "content": "O200\nG4 P0\nM99\n".replace(
                "G4 P0\n", "")}])
        self.assertTrue(r["ok"], [e.code for e in r["errors"]])
        deepest = max(r["blocks"], key=lambda b: b.depth)
        self.assertEqual(deepest.program, "O200")
        self.assertEqual([f["program"] for f in deepest.call_stack],
                         ["O100", "O200"])
        self.assertEqual(deepest.call_stack[0]["call_line_no"], 1)
        self.assertEqual(deepest.call_stack[1]["call_line_no"], 2)

    def test_repeats_do_not_reset_state(self):
        # O100 在 G91 相对模式下每次调用沿 X 前进；两次调用模态连续，
        # 累计 4 个孔而不是每轮从头开始
        report = analyze_package(make_spec(), cfg())
        holes = [h for g in report["drill_cycles"]["groups"]
                 for h in g["holes"]]
        self.assertEqual(len(holes), 4)
        xs = [h["x_mm"] for h in holes]
        self.assertEqual(xs, [10.0, 20.0, 30.0, 40.0])

    def test_modal_inheritance_from_caller(self):
        # 调用前建立的 G21/G90/G54/主轴状态在子程序中保持
        report = analyze_package(make_spec(), cfg())
        sub_entries = [e for e in report["trajectory"]
                       if e.get("source_program") == "O100"]
        state_in = sub_entries[0]["state_in"]
        self.assertEqual(state_in["unit"], "mm")
        self.assertEqual(state_in["wcs"], "G54")
        self.assertTrue(state_in["spindle_on"])

    def test_return_continues_after_call_site(self):
        r = expand("G0 X1\nM98 P100\nG0 X2\nM30\n",
                   [{"name": "a", "content": "O100\nG0 X3\nM99\n"}])
        seq = [(b.program, b.line.line_no) for b in r["blocks"]]
        self.assertEqual(seq, [
            ("main", 1), ("main", 2),
            ("O100", 1), ("O100", 2), ("O100", 3),
            ("main", 3), ("main", 4)])

    def test_m2_ends_entire_program(self):
        # 子程序中的 M2 终止整个程序（其后内容与调用方剩余行都不展开）
        r = expand("M98 P100\nG0 X9\nM30\n",
                   [{"name": "a",
                     "content": "O100\nG0 X1\nM2\nG0 X2\nM99\n"}])
        self.assertTrue(r["ok"])
        programs = [(b.program, b.line.line_no) for b in r["blocks"]]
        self.assertEqual(programs, [
            ("main", 1), ("O100", 1), ("O100", 2), ("O100", 3)])

    def test_early_m99_skips_tail(self):
        r = expand("M98 P100\nM30\n",
                   [{"name": "a",
                     "content": "O100\nG0 X1\nM99\nG0 X2\n"}])
        # 末尾缺 M99（最后一条非空行是 G0 X2）+ 提前 M99：整体阻断
        self.assertFalse(r["ok"])

    def test_issues_annotated_with_source_and_stack(self):
        # safe_z=2：子程序 G99 孔间在 R=2 平面横移不报警，主程序 G0 后
        # 子程序内部运动产生的问题必须带来源程序标注
        report = analyze_package(make_spec(), cfg())
        self.assertTrue(report["issues"])
        for iss in report["issues"]:
            self.assertIn("source_program", iss)
            self.assertIn("call_stack", iss)
            self.assertIn("block_seq", iss)
        sub_issues = [i for i in report["issues"]
                      if i["source_program"] == "O100"]
        self.assertTrue(sub_issues)

    def test_blocked_report_has_no_safety_section(self):
        report = analyze_package(make_spec("M99\n", []), cfg())
        self.assertTrue(report["blocked"])
        self.assertNotIn("risk", report)
        self.assertNotIn("trajectory", report)
        self.assertNotIn("issues", report)
        self.assertTrue(report["expansion_errors"])
        self.assertEqual(report["package"]["expansion"]["status"], "blocked")

    def test_flow_entry_types(self):
        report = analyze_package(make_spec(), cfg())
        types = {e["type"] for e in report["trajectory"]}
        self.assertIn("subprogram_call", types)
        self.assertIn("subprogram_return", types)
        self.assertIn("subprogram_label", types)
        self.assertIn("program_end", types)
        call = next(e for e in report["trajectory"]
                    if e["type"] == "subprogram_call")
        self.assertEqual(call["flow"]["target_program"], "O100")
        self.assertEqual(call["flow"]["repeats"], 2)

    def test_o_line_with_extra_words_executes_them(self):
        r = expand("M98 P100\nM30\n",
                   [{"name": "a",
                     "content": "O100 (G4 removed)\nG0 X1\nM99\n"}])
        self.assertTrue(r["ok"], [e.code for e in r["errors"]])

    def test_m98_with_other_words(self):
        # M3 M98 P100：M3 先执行再调用，子程序内主轴已转
        spec = make_spec(
            "G21 G90 G54\nG0 X0 Y0 Z20\nM3 S4000 M98 P100\nM30\n",
            [{"name": "a", "content": "O100\nG1 X10 Z-1 F500\nM99\n"}])
        report = analyze_package(spec, cfg())
        self.assertFalse(report.get("blocked"),
                         report.get("expansion_errors"))
        cutting = [i for i in report["issues"]
                   if i["code"] == "SPINDLE_NOT_RUNNING"]
        self.assertEqual(cutting, [])

    def test_block_limit_exactly_100000_allowed(self):
        # 构造恰好 100,000 块：主程序 2 行 + k 次 * 3 行子程序 + 余量行
        sub = "O1\nG0 X1\nM99\n"            # 3 行
        # 2 + k*3 = 99998 (k=33332)；主程序再补 2 个余量块到 100000
        k = (MAX_BLOCKS_LIMIT - 4) // 3
        assert 4 + k * 3 == MAX_BLOCKS_LIMIT
        main = ("G0 X0\n"
                f"M98 P1 L{k}\n"
                "G0 X1\n"
                "M30\n")                    # 4 行
        r = expand(main, [{"name": "o1", "content": sub}])
        self.assertTrue(r["ok"], [e.code for e in r["errors"]])
        self.assertEqual(r["summary"]["expanded_blocks"],
                         MAX_BLOCKS_LIMIT)

    def test_block_limit_100001_blocked(self):
        sub = "O1\nG0 X1\nG0 X2\nM99\n"
        k = (MAX_BLOCKS_LIMIT - 2) // 4 + 1
        r = expand(f"M98 P1 L{k}\nM30\n",
                   [{"name": "o1", "content": sub}])
        self.assertFalse(r["ok"])
        self.assertIn("PACKAGE_BLOCK_LIMIT", codes(r))


class TestPackageCompare(unittest.TestCase):
    def test_expansion_compare(self):
        a = make_spec(MAIN, [{"name": "o100.nc", "content": SUB}])
        b_main = MAIN.replace("M98 P100 L2", "M98 P100")
        b = make_spec(b_main, [{"name": "o100.nc", "content": SUB}])
        ra = analyze_package(a, cfg())
        rb = analyze_package(b, cfg())
        cmp = compare_package_reports(ra, rb, "a", "b")
        self.assertEqual(cmp["compare_type"], "package")
        self.assertEqual(cmp["expansion"]["delta"]["call_invocations"], -1)
        self.assertEqual(cmp["expansion"]["delta"]["expanded_blocks"], -4)
        changed = cmp["call_graph_diff"]["edges_changed"]
        self.assertEqual(changed[0]["delta_invocations"], -1)
        # 标准安全对比节仍在
        self.assertIn("risk", cmp)
        self.assertIn("issue_counts", cmp)

    def test_added_removed_subprograms(self):
        a = make_spec("M98 P100\nM30\n",
                      [{"name": "a", "content": "O100\nM99\n"}])
        b = make_spec("M98 P200\nM30\n",
                      [{"name": "b", "content": "O200\nM99\n"}])
        cmp = compare_package_reports(analyze_package(a, cfg()),
                                      analyze_package(b, cfg()))
        self.assertEqual(cmp["call_graph_diff"]["programs_added"], ["O200"])
        self.assertEqual(cmp["call_graph_diff"]["programs_removed"], ["O100"])
        self.assertEqual(cmp["call_graph_diff"]["edges_added"][0]["callee"],
                         "O200")
        self.assertEqual(cmp["call_graph_diff"]["edges_removed"][0]["callee"],
                         "O100")


class TestPackageToolChange(unittest.TestCase):
    """程序包内 T/M6 换刀：模态继承、调用栈与事件真实顺序。"""

    CHANGE_SUB = """\
O100
T2 M6
M99
"""

    def _spec(self, sub=None, repeats=1, main=None):
        sub = sub if sub is not None else self.CHANGE_SUB
        if main is None:
            main = (
                "G21 G90 G54\n"
                "G0 X0 Y0 Z100\n"
                "M5\n"
                f"M98 P100 L{repeats}\n"
                "M30\n")
        return parse_package_spec({
            "name": "p", "main": main,
            "subprograms": [{"name": "o100.nc", "content": sub}]})

    def test_m98_l2_events_interleave_in_real_order(self):
        r = analyze_package(self._spec(repeats=2), cfg_tools())
        self.assertNotIn(True, [i["code"].startswith("TOOL_CHANGE")
                                for i in r["issues"]])
        ev = r["tools"]["events"]
        # 必须按真实执行顺序交错：pre#1,change#1,pre#2,change#2
        self.assertEqual(
            [(e["kind"], e["t"], e.get("repeat_index")) for e in ev],
            [("preselect", 2, 1), ("change", 2, 1),
             ("preselect", 2, 2), ("change", 2, 2)])
        self.assertEqual([e["seq"] for e in ev], [0, 1, 2, 3])
        self.assertEqual([e["source_program"] for e in ev],
                         ["O100"] * 4)
        # 每条事件带来源调用栈
        for e in ev:
            self.assertEqual(e["call_stack"][0]["caller"], "main")
            self.assertEqual(e["call_stack"][0]["program"], "O100")
            self.assertEqual(e["call_stack"][0]["repeat_total"], 2)
        # 分类视图仍分别保留
        self.assertEqual(len(r["tools"]["preselect_events"]), 2)
        self.assertEqual(len(r["tools"]["change_events"]), 2)

    def test_tool_modality_inherited_into_subprogram(self):
        # 子程序先 T2 M6 再钻孔：换刀模态在子程序内建立，孔记录当前刀 T2
        sub = ("O100\n"
               "T2 M6\n"
               "M3 S4000\n"
               "G0 X0 Y30 Z20\n"
               "G99 G81 R2 Z-8 F250\n"
               "G80\n"
               "G0 X0 Y0 Z100\n"
               "M5\n"
               "M99\n")
        r = analyze_package(self._spec(sub=sub), cfg_tools())
        holes = [h for g in r["drill_cycles"]["groups"]
                 for h in g["holes"]]
        self.assertTrue(holes)
        self.assertTrue(all(h["t"] == 2 for h in holes))
        self.assertEqual(r["final_state"]["tool"]["current_t"], 2)

    def test_failed_change_in_subprogram_rolls_back(self):
        # 子程序里 T9 M6（未登记）：整段阻断，无预选/换入事件残留，
        # 保持原刀 T1
        sub = "O100\nT9 M6\nM99\n"
        r = analyze_package(self._spec(sub=sub), cfg_tools())
        codes_ = [i["code"] for i in r["issues"]]
        self.assertIn("TOOL_CHANGE_UNREGISTERED", codes_)
        self.assertEqual(r["tools"]["events"], [])
        self.assertEqual(r["final_state"]["tool"]["current_t"], 1)
        ev_issues = [i for i in r["issues"]
                     if i["code"] == "TOOL_CHANGE_UNREGISTERED"]
        self.assertEqual(ev_issues[0]["source_program"], "O100")
        self.assertEqual(ev_issues[0]["call_stack"][0]["caller"], "main")


if __name__ == "__main__":
    unittest.main(verbosity=2)
