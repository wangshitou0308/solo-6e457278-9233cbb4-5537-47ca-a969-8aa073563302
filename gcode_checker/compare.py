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
           "cycle", "hole_no", "missing", "bad", "unknown", "plane"]
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

    return {
        "labels": {"baseline": baseline_label, "candidate": candidate_label},
        "machine": candidate["machine"],
        "drill_cycles": drill_compare,
        "arcs": arc_compare,
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
