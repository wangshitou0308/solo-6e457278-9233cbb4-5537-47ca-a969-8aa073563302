"""两个 G-code 程序的风险对比（同一机床配置）。

把问题按指纹（代码 + 规范化指令 + 关键细节）做多重集匹配，分为：
- resolved: 旧程序有、新程序没有
- introduced: 新程序新增
- unchanged: 两边都有（按出现次数配对）

另给风险分 / 严重度计数 / 路径长度 / 包围盒的数值变化。
"""

from __future__ import annotations

from collections import Counter

from .analyzer import SEVERITY_ORDER, MOTION_CN  # noqa: F401


def _fingerprint(issue: dict) -> tuple:
    d = issue.get("details", {})
    key = ["axis", "unsupported_tokens", "malformed_tokens", "reason",
           "below_mm", "overshoot_mm", "exceed_mm_per_min", "exceed_rpm",
           "cycle", "hole_no", "missing", "bad", "unknown", "plane", "wcs",
           "h"]
    detail_fp = tuple((k, str(d.get(k))) for k in key if k in d)
    return (issue["code"], issue.get("normalized", ""), detail_fp)


def _pick(issue: dict) -> dict:
    return {
        "code": issue["code"],
        "title": issue.get("title"),
        "severity": issue["severity"],
        "line_no": issue["line_no"],
        "source_line": issue.get("source_line"),
        "normalized": issue.get("normalized"),
        "basis": issue.get("basis"),
        "details": issue.get("details", {}),
    }


def compare_reports(baseline: dict, candidate: dict,
                    baseline_label: str = "baseline",
                    candidate_label: str = "candidate") -> dict:
    old = Counter(_fingerprint(i) for i in baseline["issues"])
    new = Counter(_fingerprint(i) for i in candidate["issues"])
    old_map = {}
    new_map = {}
    for i in baseline["issues"]:
        old_map.setdefault(_fingerprint(i), []).append(i)
    for i in candidate["issues"]:
        new_map.setdefault(_fingerprint(i), []).append(i)

    unchanged_keys = set(old) & set(new)
    resolved, introduced, unchanged = [], [], []

    for fp in sorted(set(old) | set(new), key=lambda x: str(x)):
        o, n = old.get(fp, 0), new.get(fp, 0)
        if n > o:
            # 新增 = 超出旧数量的部分；其余视为未变化
            for i in new_map[fp][o:]:
                introduced.append(_pick(i))
            for i in new_map[fp][:o]:
                unchanged.append(_pick(i))
        elif o > n:
            for i in old_map[fp][n:]:
                resolved.append(_pick(i))
            for i in new_map.get(fp, []):
                unchanged.append(_pick(i))
        else:
            for i in new_map[fp]:
                unchanged.append(_pick(i))

    def sev_count(report):
        return report["risk"]["counts_by_severity"]

    oc, nc = sev_count(baseline), sev_count(candidate)
    code_counter = lambda report: Counter(i["code"] for i in report["issues"])
    old_codes, new_codes = code_counter(baseline), code_counter(candidate)
    code_changes = sorted(
        {*(old_codes | new_codes)},
        key=lambda c: (SEVERITY_ORDER.index(_sev_of(c, baseline, candidate)), c),
    )
    by_code = [{
        "code": c,
        "baseline_count": old_codes.get(c, 0),
        "candidate_count": new_codes.get(c, 0),
        "delta": new_codes.get(c, 0) - old_codes.get(c, 0),
    } for c in code_changes]

    def len_of(r):
        return r["path_length_mm"]

    def bbox_of(r):
        return r.get("bbox_program_mm")

    def drill_of(r):
        return r.get("drill_cycles", {}).get("summary", {})

    ds_a, ds_b = drill_of(baseline), drill_of(candidate)
    drill_compare = {
        "baseline": {
            "cycle_groups": ds_a.get("cycle_groups", 0),
            "holes_total": ds_a.get("holes_total", 0),
            "holes_drilled": ds_a.get("holes_drilled", 0),
            "holes_blocked": ds_a.get("holes_blocked", 0),
            "total_drill_depth_mm": ds_a.get("total_drill_depth_mm", 0.0),
            "expanded_path_mm": ds_a.get("expanded_path_mm",
                                        {"rapid": 0.0, "cutting": 0.0,
                                         "total": 0.0}),
        },
        "candidate": {
            "cycle_groups": ds_b.get("cycle_groups", 0),
            "holes_total": ds_b.get("holes_total", 0),
            "holes_drilled": ds_b.get("holes_drilled", 0),
            "holes_blocked": ds_b.get("holes_blocked", 0),
            "total_drill_depth_mm": ds_b.get("total_drill_depth_mm", 0.0),
            "expanded_path_mm": ds_b.get("expanded_path_mm",
                                        {"rapid": 0.0, "cutting": 0.0,
                                         "total": 0.0}),
        },
    }
    drill_compare["delta"] = {
        "holes_total": (drill_compare["candidate"]["holes_total"]
                        - drill_compare["baseline"]["holes_total"]),
        "holes_drilled": (drill_compare["candidate"]["holes_drilled"]
                          - drill_compare["baseline"]["holes_drilled"]),
        "holes_blocked": (drill_compare["candidate"]["holes_blocked"]
                          - drill_compare["baseline"]["holes_blocked"]),
        "total_drill_depth_mm": round(
            drill_compare["candidate"]["total_drill_depth_mm"]
            - drill_compare["baseline"]["total_drill_depth_mm"], 6),
        "expanded_path_total_mm": round(
            drill_compare["candidate"]["expanded_path_mm"]["total"]
            - drill_compare["baseline"]["expanded_path_mm"]["total"], 6),
    }
    # 按循环类型（G81/G82/G83）的孔数变化
    by_cycle = {}
    ba = baseline.get("drill_cycles", {}).get("by_cycle", {})
    bb = candidate.get("drill_cycles", {}).get("by_cycle", {})
    for cyc in sorted(set(ba) | set(bb)):
        a, b = ba.get(cyc, {}), bb.get(cyc, {})
        by_cycle[cyc] = {
            "baseline_holes": a.get("holes", 0),
            "candidate_holes": b.get("holes", 0),
            "delta_holes": b.get("holes", 0) - a.get("holes", 0),
            "baseline_blocked": a.get("blocked", 0),
            "candidate_blocked": b.get("blocked", 0),
            "delta_blocked": b.get("blocked", 0) - a.get("blocked", 0),
        }
    drill_compare["by_cycle"] = by_cycle

    # 按平面（G17/G18/G19）的弧段数、弧长与问题增减
    def arcs_of(r):
        return r.get("arcs", {})

    def _plane_of(issue):
        return issue.get("details", {}).get("plane")

    plane_issues_a = Counter(
        p for p in (_plane_of(i) for i in baseline["issues"]) if p)
    plane_issues_b = Counter(
        p for p in (_plane_of(i) for i in candidate["issues"]) if p)
    plane_resolved = Counter(
        p for p in (_plane_of(i) for i in resolved) if p)
    plane_introduced = Counter(
        p for p in (_plane_of(i) for i in introduced) if p)

    arc_a, arc_b = arcs_of(baseline), arcs_of(candidate)
    pa = arc_a.get("by_plane", {})
    pb = arc_b.get("by_plane", {})
    arc_by_plane = {}
    for plane in sorted(set(pa) | set(pb) | set(plane_issues_a)
                        | set(plane_issues_b)):
        a, b = pa.get(plane, {}), pb.get(plane, {})
        na, nb = plane_issues_a.get(plane, 0), plane_issues_b.get(plane, 0)
        arc_by_plane[plane] = {
            "baseline_count": a.get("count", 0),
            "candidate_count": b.get("count", 0),
            "delta_count": b.get("count", 0) - a.get("count", 0),
            "baseline_arc_length_mm": a.get("arc_length_mm", 0.0),
            "candidate_arc_length_mm": b.get("arc_length_mm", 0.0),
            "delta_arc_length_mm": round(
                b.get("arc_length_mm", 0.0) - a.get("arc_length_mm", 0.0), 6),
            "baseline_helical_count": a.get("helical_count", 0),
            "candidate_helical_count": b.get("helical_count", 0),
            "delta_helical_count": (b.get("helical_count", 0)
                                    - a.get("helical_count", 0)),
            "baseline_issues": na,
            "candidate_issues": nb,
            "delta_issues": nb - na,
            "resolved_issues": plane_resolved.get(plane, 0),
            "introduced_issues": plane_introduced.get(plane, 0),
        }
    ta, tb = arc_a.get("total", {}), arc_b.get("total", {})
    arc_compare = {
        "by_plane": arc_by_plane,
        "total": {
            "baseline_count": ta.get("count", 0),
            "candidate_count": tb.get("count", 0),
            "delta_count": tb.get("count", 0) - ta.get("count", 0),
            "baseline_arc_length_mm": ta.get("arc_length_mm", 0.0),
            "candidate_arc_length_mm": tb.get("arc_length_mm", 0.0),
            "delta_arc_length_mm": round(
                tb.get("arc_length_mm", 0.0) - ta.get("arc_length_mm", 0.0),
                6),
            "baseline_issues": sum(plane_issues_a.values()),
            "candidate_issues": sum(plane_issues_b.values()),
            "delta_issues": (sum(plane_issues_b.values())
                             - sum(plane_issues_a.values())),
            "resolved_issues": sum(plane_resolved.values()),
            "introduced_issues": sum(plane_introduced.values()),
        },
        "blocked": {
            "baseline": arc_a.get("blocked_count", 0),
            "candidate": arc_b.get("blocked_count", 0),
            "delta": (arc_b.get("blocked_count", 0)
                      - arc_a.get("blocked_count", 0)),
        },
    }

    # 按工件坐标系（G54-G59）的路径、机床行程（包围盒）与问题增减
    def wcs_of(r):
        return r.get("wcs", {}).get("by_wcs", {})

    def _wcs_of(issue):
        return issue.get("details", {}).get("wcs")

    wcs_issues_a = Counter(
        w for w in (_wcs_of(i) for i in baseline["issues"]) if w)
    wcs_issues_b = Counter(
        w for w in (_wcs_of(i) for i in candidate["issues"]) if w)
    wcs_resolved = Counter(
        w for w in (_wcs_of(i) for i in resolved) if w)
    wcs_introduced = Counter(
        w for w in (_wcs_of(i) for i in introduced) if w)

    wa, wb = wcs_of(baseline), wcs_of(candidate)
    wcs_by_wcs = {}
    for w in sorted(set(wa) | set(wb) | set(wcs_issues_a)
                    | set(wcs_issues_b)):
        a, b = wa.get(w, {}), wb.get(w, {})
        pa = a.get("path_length_mm", {})
        pb = b.get("path_length_mm", {})

        def _path(p):
            return {"rapid": p.get("rapid", 0.0),
                    "cutting": p.get("cutting", 0.0),
                    "total": p.get("total", 0.0)}

        p_a, p_b = _path(pa), _path(pb)
        na, nb = wcs_issues_a.get(w, 0), wcs_issues_b.get(w, 0)
        wcs_by_wcs[w] = {
            "baseline_path_mm": p_a,
            "candidate_path_mm": p_b,
            "delta_path_mm": {
                "rapid": round(p_b["rapid"] - p_a["rapid"], 6),
                "cutting": round(p_b["cutting"] - p_a["cutting"], 6),
                "total": round(p_b["total"] - p_a["total"], 6),
            },
            "baseline_machine_bbox_mm": a.get("machine_bbox_mm"),
            "candidate_machine_bbox_mm": b.get("machine_bbox_mm"),
            "baseline_issues": na,
            "candidate_issues": nb,
            "delta_issues": nb - na,
            "resolved_issues": wcs_resolved.get(w, 0),
            "introduced_issues": wcs_introduced.get(w, 0),
        }
    wcs_compare = {
        "by_wcs": wcs_by_wcs,
        "total": {
            "baseline_issues": sum(wcs_issues_a.values()),
            "candidate_issues": sum(wcs_issues_b.values()),
            "delta_issues": (sum(wcs_issues_b.values())
                             - sum(wcs_issues_a.values())),
            "resolved_issues": sum(wcs_resolved.values()),
            "introduced_issues": sum(wcs_introduced.values()),
        },
    }

    # 刀长补偿（G43/G44/G49 + H）：补偿使用、按 H 路径/钻孔、问题与
    # 主轴基准点 Z 行程的两侧值与变化
    length_compare = _length_comp_compare(
        baseline, candidate, resolved, introduced)

    return {
        "labels": {"baseline": baseline_label, "candidate": candidate_label},
        "machine": candidate["machine"],
        "drill_cycles": drill_compare,
        "arcs": arc_compare,
        "wcs": wcs_compare,
        "length_compensation": length_compare,
        "risk": {
            "baseline": baseline["risk"],
            "candidate": candidate["risk"],
            "score_delta": candidate["risk"]["score"] - baseline["risk"]["score"],
            "level_change": {
                "baseline": baseline["risk"]["level"],
                "candidate": candidate["risk"]["level"],
            },
            "severity_count_delta": {
                s: nc.get(s, 0) - oc.get(s, 0) for s in SEVERITY_ORDER},
        },
        "path_length_mm": {
            "baseline": len_of(baseline),
            "candidate": len_of(candidate),
            "total_delta": round(
                len_of(candidate)["total"] - len_of(baseline)["total"], 6),
            "cutting_delta": round(
                len_of(candidate)["cutting"] - len_of(baseline)["cutting"], 6),
            "rapid_delta": round(
                len_of(candidate)["rapid"] - len_of(baseline)["rapid"], 6),
        },
        "bbox_program_mm": {
            "baseline": bbox_of(baseline),
            "candidate": bbox_of(candidate),
        },
        "issue_counts": {
            "baseline_total": len(baseline["issues"]),
            "candidate_total": len(candidate["issues"]),
            "resolved": len(resolved),
            "introduced": len(introduced),
            "unchanged": len(unchanged),
        },
        "by_code": by_code,
        "resolved_issues": resolved,
        "introduced_issues": introduced,
        "unchanged_issues": unchanged,
    }


def _sev_of(code: str, r1: dict, r2: dict) -> str:
    for r in (r1, r2):
        for i in r["issues"]:
            if i["code"] == code:
                return i["severity"]
    return "info"


def _lc_section(report: dict) -> dict:
    return report.get("length_compensation", {})


def _h_issue_counts(report: dict) -> dict:
    c: dict = {}
    for i in report["issues"]:
        h = i.get("details", {}).get("h")
        if h is None:
            continue
        c[h] = c.get(h, 0) + 1
    return c


def _length_comp_compare(baseline: dict, candidate: dict,
                         resolved: list, introduced: list) -> dict:
    """刀长补偿对比：H 表变化、G43/G44/G49 事件、按 H 的路径/钻孔/问题，
    以及主轴基准点 Z 行程变化。"""
    la, lb = _lc_section(baseline), _lc_section(candidate)
    table_a = la.get("offsets_mm", {})
    table_b = lb.get("offsets_mm", {})
    h_labels = sorted(set(table_a) | set(table_b),
                      key=lambda t: int(t[1:]) if t[1:].isdigit() else 0)
    table_changes = []
    for label in h_labels:
        va, vb = table_a.get(label), table_b.get(label)
        if va != vb:
            table_changes.append({"h": label, "baseline_mm": va,
                                  "candidate_mm": vb,
                                  "delta_mm": (round(vb - va, 6)
                                              if va is not None and vb is not None
                                              else None)})

    def events(sec):
        out = {"G43": 0, "G44": 0, "G49": 0}
        for e in sec.get("events", []):
            code = e.get("code")
            if code in out:
                out[code] += 1
        return out

    ea, eb = events(la), events(lb)
    by_h_a, by_h_b = la.get("by_h", {}), lb.get("by_h", {})
    ia, ib = _h_issue_counts(baseline), _h_issue_counts(candidate)
    res_h = Counter(i.get("details", {}).get("h")
                    for i in resolved if i.get("details", {}).get("h") is not None)
    int_h = Counter(i.get("details", {}).get("h")
                    for i in introduced
                    if i.get("details", {}).get("h") is not None)
    by_h = {}
    for label in sorted(set(by_h_a) | set(by_h_b)):
        a, b = by_h_a.get(label, {}), by_h_b.get(label, {})
        hno = (a.get("h") if a.get("h") is not None
               else b.get("h"))
        pa, pb = (a.get("path_length_mm", {}),
                  b.get("path_length_mm", {}))
        by_h[label] = {
            "h": hno,
            "offset_baseline_mm": table_a.get(label),
            "offset_candidate_mm": table_b.get(label),
            "baseline_path_mm": {
                "rapid": pa.get("rapid", 0.0),
                "cutting": pa.get("cutting", 0.0),
                "total": pa.get("total", 0.0)},
            "candidate_path_mm": {
                "rapid": pb.get("rapid", 0.0),
                "cutting": pb.get("cutting", 0.0),
                "total": pb.get("total", 0.0)},
            "delta_path_total_mm": round(
                pb.get("total", 0.0) - pa.get("total", 0.0), 6),
            "baseline_holes": a.get("holes_drilled", 0),
            "candidate_holes": b.get("holes_drilled", 0),
            "delta_holes": (b.get("holes_drilled", 0)
                            - a.get("holes_drilled", 0)),
            "baseline_drill_depth_mm": a.get("total_drill_depth_mm", 0.0),
            "candidate_drill_depth_mm": b.get("total_drill_depth_mm", 0.0),
            "delta_drill_depth_mm": round(
                b.get("total_drill_depth_mm", 0.0)
                - a.get("total_drill_depth_mm", 0.0), 6),
            "baseline_issues": ia.get(hno, 0),
            "candidate_issues": ib.get(hno, 0),
            "delta_issues": ib.get(hno, 0) - ia.get(hno, 0),
            "resolved_issues": res_h.get(hno, 0),
            "introduced_issues": int_h.get(hno, 0),
        }

    za = (la.get("spindle_z_travel") or {}).get("spindle_z_machine_mm")
    zb = (lb.get("spindle_z_travel") or {}).get("spindle_z_machine_mm")
    z_travel = {"baseline_spindle_z_machine_mm": za,
                "candidate_spindle_z_machine_mm": zb}
    if za is not None and zb is not None:
        z_travel["delta_min_mm"] = round(zb[0] - za[0], 6)
        z_travel["delta_max_mm"] = round(zb[1] - za[1], 6)
    comp_codes = ("LENGTH_COMP_MISSING_H", "LENGTH_COMP_H_NOT_FOUND",
                  "LENGTH_COMP_CONFLICT")
    ca = Counter(i["code"] for i in baseline["issues"]
                 if i["code"] in comp_codes)
    cb = Counter(i["code"] for i in candidate["issues"]
                 if i["code"] in comp_codes)
    block_counts = {c: {"baseline": ca.get(c, 0), "candidate": cb.get(c, 0),
                        "delta": cb.get(c, 0) - ca.get(c, 0)}
                    for c in comp_codes}
    return {
        "offset_table_changes": table_changes,
        "events": {
            "baseline": ea, "candidate": eb,
            "delta": {c: eb[c] - ea[c] for c in ("G43", "G44", "G49")},
        },
        "by_h": by_h,
        "spindle_z_travel": z_travel,
        "block_issue_counts": block_counts,
    }


# ---------------------------------------------------------------------------
# 程序包对比（展开调用图 + 展开块变化，外加标准报告对比）
# ---------------------------------------------------------------------------

def _expansion_of(report: dict) -> dict:
    return report.get("package", {}).get("expansion", {})


def _call_edges(report: dict) -> dict:
    """(caller, callee) -> 边汇总（含调用次数与展开中的调用执行次数）。"""
    graph = report.get("package", {}).get("call_graph", {})
    return {(e["caller"], e["callee"]): e for e in graph.get("edges", [])}


def compare_package_reports(baseline: dict, candidate: dict,
                            baseline_label: str = "baseline",
                            candidate_label: str = "candidate") -> dict:
    """两个程序包报告的对比：标准安全对比 + 调用与展开块变化。"""
    result = compare_reports(baseline, candidate,
                             baseline_label, candidate_label)
    ea, eb = _expansion_of(baseline), _expansion_of(candidate)

    def _num(d, key, default=0):
        return d.get(key, default)

    result["compare_type"] = "package"
    result["expansion"] = {
        "baseline": {
            "subprograms_defined": _num(ea, "subprograms_defined"),
            "source_programs": _num(ea, "source_programs"),
            "expanded_blocks": _num(ea, "expanded_blocks"),
            "original_physical_lines": _num(ea, "original_physical_lines"),
            "call_sites": _num(ea, "call_sites"),
            "call_executions": _num(ea, "call_executions"),
            "call_invocations": _num(ea, "call_invocations"),
            "repeat_invocations": _num(ea, "repeat_invocations"),
            "max_depth": _num(ea, "max_depth"),
        },
        "candidate": {
            "subprograms_defined": _num(eb, "subprograms_defined"),
            "source_programs": _num(eb, "source_programs"),
            "expanded_blocks": _num(eb, "expanded_blocks"),
            "original_physical_lines": _num(eb, "original_physical_lines"),
            "call_sites": _num(eb, "call_sites"),
            "call_executions": _num(eb, "call_executions"),
            "call_invocations": _num(eb, "call_invocations"),
            "repeat_invocations": _num(eb, "repeat_invocations"),
            "max_depth": _num(eb, "max_depth"),
        },
    }
    b, c = result["expansion"]["baseline"], result["expansion"]["candidate"]
    result["expansion"]["delta"] = {
        "subprograms_defined": c["subprograms_defined"] - b["subprograms_defined"],
        "source_programs": c["source_programs"] - b["source_programs"],
        "expanded_blocks": c["expanded_blocks"] - b["expanded_blocks"],
        "original_physical_lines": (c["original_physical_lines"]
                                    - b["original_physical_lines"]),
        "call_sites": c["call_sites"] - b["call_sites"],
        "call_executions": c["call_executions"] - b["call_executions"],
        "call_invocations": c["call_invocations"] - b["call_invocations"],
        "repeat_invocations": c["repeat_invocations"] - b["repeat_invocations"],
        "max_depth": c["max_depth"] - b["max_depth"],
    }

    # 调用图边的新增 / 删除 / 调用次数变化
    ga = baseline.get("package", {}).get("call_graph", {})
    gb = candidate.get("package", {}).get("call_graph", {})
    ea_map, eb_map = _call_edges(baseline), _call_edges(candidate)
    nodes_a = {n["program"]: n for n in ga.get("nodes", [])}
    nodes_b = {n["program"]: n for n in gb.get("nodes", [])}
    edges_added, edges_removed, edges_changed = [], [], []
    for key in sorted(set(ea_map) | set(eb_map)):
        e0, e1 = ea_map.get(key), eb_map.get(key)
        if e0 is None:
            edges_added.append({
                "caller": key[0], "callee": key[1],
                "o_number": e1["o_number"],
                "sites": e1["sites"], "invocations": e1["invocations"]})
        elif e1 is None:
            edges_removed.append({
                "caller": key[0], "callee": key[1],
                "o_number": e0["o_number"],
                "sites": e0["sites"], "invocations": e0["invocations"]})
        elif (e0["invocations"] != e1["invocations"]
              or e0["executions"] != e1["executions"]
              or e0["sites"] != e1["sites"]):
            edges_changed.append({
                "caller": key[0], "callee": key[1],
                "o_number": e1["o_number"],
                "baseline_sites": e0["sites"],
                "candidate_sites": e1["sites"],
                "baseline_invocations": e0["invocations"],
                "candidate_invocations": e1["invocations"],
                "delta_invocations": e1["invocations"] - e0["invocations"],
                "baseline_executions": e0["executions"],
                "candidate_executions": e1["executions"],
            })
    result["call_graph_diff"] = {
        "programs_added": sorted(set(nodes_b) - set(nodes_a)),
        "programs_removed": sorted(set(nodes_a) - set(nodes_b)),
        "edges_added": edges_added,
        "edges_removed": edges_removed,
        "edges_changed": edges_changed,
    }
    return result
