"""High-level graph analysis algorithms built on NetworkX cache.

Impact analysis, service-level risk propagation, dead code detection,
test gap analysis, and health scoring.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from brain.graph.engine import GraphEngine
from brain.graph.networkx_cache import NetworkXCache
from brain.types import HealthReport, ImpactResult


def impact_analysis(nxc: NetworkXCache, engine: GraphEngine,
                    functions: List[str], max_depth: int = 5) -> ImpactResult:
    result = ImpactResult(changed_functions=functions)

    all_callers = set()
    for fn in functions:
        callers = nxc.callers(fn, depth=1)
        all_callers.update(callers)
    result.direct_callers = len(all_callers)

    all_impacted = nxc.impact_set(functions, max_depth)
    result.total_impacted = len(all_impacted)

    services = set()
    for node in all_impacted:
        fn = engine.get_function(node)
        if fn and fn.get("project"):
            services.add(fn["project"])
    result.impacted_services = sorted(services)

    for svc in services:
        svc_funcs = [n for n in all_impacted
                     if (f := engine.get_function(n)) and f.get("project") == svc]
        total_tests = engine.conn.execute(
            "SELECT COUNT(*) FROM tests WHERE project=?", (svc,)).fetchone()[0]
        total_funcs = engine.count_functions(svc)
        coverage = total_tests / max(total_funcs, 1)
        pr = nxc.pagerank()
        avg_pr = sum(pr.get(f, 0) for f in svc_funcs) / max(len(svc_funcs), 1)
        risk = avg_pr * (1 - coverage) * len(svc_funcs)
        result.risk_scores[svc] = round(risk, 6)

    untested = []
    for fn in all_impacted:
        edges = engine.get_edges_to("Function", fn, edge_type="TESTS")
        if not edges:
            untested.append(fn)
    result.test_gaps = untested[:50]
    result.overall_risk = sum(result.risk_scores.values())
    return result


def service_health(engine: GraphEngine, nxc: NetworkXCache,
                   project: str) -> HealthReport:
    report = HealthReport(project=project)
    funcs = engine.count_functions(project)
    tests = engine.conn.execute(
        "SELECT COUNT(*) FROM tests WHERE project=?", (project,)).fetchone()[0]
    endpoints = engine.conn.execute(
        "SELECT COUNT(*) FROM endpoints WHERE project=?", (project,)).fetchone()[0]
    classes = engine.conn.execute(
        "SELECT COUNT(*) FROM classes WHERE project=?", (project,)).fetchone()[0]

    test_ratio = tests / max(funcs, 1)
    pr = nxc.pagerank()
    project_funcs = engine.find_functions(project=project, limit=10000)
    avg_complexity = 0.0
    if project_funcs:
        complexities = [f.get("complexity", 0) or 0 for f in project_funcs]
        avg_complexity = sum(complexities) / len(complexities)

    high_pr_funcs = sorted(
        [(f["qname"], pr.get(f["qname"], 0)) for f in project_funcs],
        key=lambda x: x[1], reverse=True)[:10]

    score = 0.0
    score += min(test_ratio, 1.0) * 40
    score += max(0, 20 - avg_complexity) * 2
    score += min(endpoints / max(funcs, 1) * 100, 10)
    score = min(score, 100)

    if score >= 90: report.grade = "A"
    elif score >= 75: report.grade = "B"
    elif score >= 60: report.grade = "C"
    elif score >= 40: report.grade = "D"
    else: report.grade = "F"

    report.score = round(score, 1)
    report.metrics = {
        "functions": funcs, "tests": tests, "test_ratio": round(test_ratio, 2),
        "endpoints": endpoints, "classes": classes,
        "avg_complexity": round(avg_complexity, 2),
        "high_pagerank": high_pr_funcs[:5],
    }

    if test_ratio < 0.3:
        report.recommendations.append(f"Test coverage is {test_ratio:.0%} — aim for >50%")
    if avg_complexity > 10:
        report.recommendations.append(f"Average complexity is {avg_complexity:.1f} — refactor complex functions")

    return report


def dead_code_candidates(engine: GraphEngine, nxc: NetworkXCache,
                         project: str) -> List[Dict]:
    funcs = engine.find_functions(project=project, limit=50000)
    dead = []
    for f in funcs:
        qname = f["qname"]
        if f.get("is_test") or f.get("is_exported"):
            continue
        callers = nxc.callers(qname, depth=1)
        if not callers:
            edges_to = engine.get_edges_to("Function", qname, edge_type="ROUTES_TO")
            if not edges_to:
                dead.append({
                    "qname": qname, "file": f.get("file_path"),
                    "line": f.get("line_start"), "project": project,
                })
    return dead


def test_gaps(engine: GraphEngine, nxc: NetworkXCache,
              project: str) -> List[Dict]:
    funcs = engine.find_functions(project=project, limit=50000)
    pr = nxc.pagerank()
    gaps = []
    for f in funcs:
        if f.get("is_test"):
            continue
        qname = f["qname"]
        test_edges = engine.get_edges_to("Function", qname, edge_type="TESTS")
        if not test_edges:
            gaps.append({
                "qname": qname, "file": f.get("file_path"),
                "pagerank": pr.get(qname, 0), "project": project,
            })
    gaps.sort(key=lambda x: x["pagerank"], reverse=True)
    return gaps[:100]
