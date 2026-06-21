#!/usr/bin/env python3
"""Test Report — render workspace/features/<slug>/test-report.md from test artifacts.

Pure Python, stdlib only. This script NEVER runs `go` and NEVER calls an MCP.
The LLM (skill layer, /implement Step 6.5e) runs the toolchain:

    go test -json ./...                 > results.json
    go test -coverprofile=coverage.out ./...
    go tool cover -func=coverage.out    > coverage-func.txt   (optional)

then feeds those machine outputs here. The per-test -> feature mapping needs
semantic understanding, so the LLM assembles a *tests manifest*; this script
only parses the machine outputs and renders the markdown report.

Subcommands
-----------
  parse-go-coverage <coverage.out | coverage-func.txt>
        -> JSON: {format, total_pct, by_file, by_func}

  parse-go-test <go-test-json>
        -> JSON: {total, passed, failed, skipped, incomplete,
                  tests[], packages{}, failures[]}

  build-report --feature <slug> --tests <manifest.json>
               [--coverage <coverage.out|coverage-func.txt>]
               [--results <go-test-json>] [--out <path>]
        -> writes test-report.md, prints a summary JSON

Tests manifest schema (assembled by the LLM during Step 6.5):
{
  "feature": "gpay-bifrost-account-matching",   # optional; --feature wins
  "devrev":  "ENH-18653",
  "service": "payments-upi",
  "coverage": "coverage.out",                   # optional path (or inline dict)
  "results":  "results.json",                   # optional path (or inline dict)
  "tests": [
    {
      "name":   "TestRegisterWithBifrost_NotIntentFlow",
      "file":   "internal/app/payment/processor/initiate/initiate_test.go",
      "type":   "unit",                          # unit | slit
      "covers": "Eligibility gate 1 (intent flow)",
      "issue":  "ENH-18653",
      "asserts":"non-intent flow returns before any Bifrost call",
      "why":    "guards gate 1; a non-intent payment must never register"
    }
  ],
  "gaps": [ {"area": "Hystrix sizing", "reason": "deferred (infra)"} ]
}
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make the repo root importable so `brain.config` resolves when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# brain.config is preferred for the workspace path, but keep this script usable
# in a bare CI checkout where brain may not import — fall back to <repo>/workspace.
try:
    from brain.config import BrainConfig  # noqa: E402

    _WORKSPACE = Path(BrainConfig().workspace)
except Exception:  # pragma: no cover - defensive fallback
    _WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"

FEATURES = _WORKSPACE / "features"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------------
# parse-go-coverage
# ----------------------------------------------------------------------------
# Profile line:  <file>:<sl>.<sc>,<el>.<ec> <numStmts> <count>
_PROFILE_RE = re.compile(r"^(.+):(\d+)\.(\d+),(\d+)\.(\d+)\s+(\d+)\s+(\d+)$")
# `go tool cover -func` line:  <file>:<line>:\t<Func>\t<pct>%
_FUNC_RE = re.compile(r"^(\S+):(\d+):\s+(\S+)\s+([\d.]+)%$")


def parse_go_coverage(path: str) -> dict:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return {"format": "empty", "total_pct": None, "by_file": {}, "by_func": []}

    if lines[0].strip().startswith("mode:"):
        return _parse_profile(lines[1:])
    return _parse_func(lines)


def _parse_profile(body: list) -> dict:
    by_file: dict = {}
    tot_cov = tot_all = 0
    for ln in body:
        m = _PROFILE_RE.match(ln.strip())
        if not m:
            continue
        f = m.group(1)
        n = int(m.group(6))
        cnt = int(m.group(7))
        rec = by_file.setdefault(f, {"covered_stmts": 0, "total_stmts": 0})
        rec["total_stmts"] += n
        tot_all += n
        if cnt > 0:
            rec["covered_stmts"] += n
            tot_cov += n
    for rec in by_file.values():
        rec["pct"] = round(100.0 * rec["covered_stmts"] / rec["total_stmts"], 1) if rec["total_stmts"] else 0.0
    total_pct = round(100.0 * tot_cov / tot_all, 1) if tot_all else 0.0
    return {"format": "profile", "total_pct": total_pct, "by_file": by_file, "by_func": []}


def _parse_func(lines: list) -> dict:
    by_func: list = []
    total_pct = None
    for ln in lines:
        s = ln.rstrip()
        if s.startswith("total:"):
            mt = re.search(r"([\d.]+)%\s*$", s)
            if mt:
                total_pct = float(mt.group(1))
            continue
        m = _FUNC_RE.match(s)
        if not m:
            continue
        by_func.append({
            "file": m.group(1),
            "line": int(m.group(2)),
            "func": m.group(3),
            "pct": float(m.group(4)),
        })
    # Roll func rows up into a per-file average so build-report can show components.
    by_file: dict = {}
    grouped: dict = {}
    for r in by_func:
        grouped.setdefault(r["file"], []).append(r["pct"])
    for f, pcts in grouped.items():
        by_file[f] = {"pct": round(sum(pcts) / len(pcts), 1), "funcs": len(pcts)}
    if total_pct is None and by_func:
        total_pct = round(sum(r["pct"] for r in by_func) / len(by_func), 1)
    return {"format": "func", "total_pct": total_pct, "by_file": by_file, "by_func": by_func}


# ----------------------------------------------------------------------------
# parse-go-test  (newline-delimited JSON from `go test -json`)
# ----------------------------------------------------------------------------
def parse_go_test(path: str) -> dict:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    tests: dict = {}      # (pkg, test) -> {status, elapsed}
    packages: dict = {}   # pkg -> status
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw or not raw.startswith("{"):
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        action = ev.get("Action")
        pkg = ev.get("Package", "")
        test = ev.get("Test")
        if test:
            key = (pkg, test)
            if action in ("pass", "fail", "skip"):
                tests[key] = {"status": action, "elapsed": ev.get("Elapsed")}
            elif action == "run" and key not in tests:
                tests[key] = {"status": "incomplete", "elapsed": None}
        elif action in ("pass", "fail", "skip"):
            packages[pkg] = action

    rows = []
    counts = {"pass": 0, "fail": 0, "skip": 0, "incomplete": 0}
    for (pkg, test), info in sorted(tests.items()):
        st = info["status"]
        counts[st] = counts.get(st, 0) + 1
        rows.append({"package": pkg, "test": test, "status": st, "elapsed": info["elapsed"]})
    failures = [f"{r['package']}.{r['test']}" for r in rows if r["status"] in ("fail", "incomplete")]
    return {
        "total": len(rows),
        "passed": counts["pass"],
        "failed": counts["fail"],
        "skipped": counts["skip"],
        "incomplete": counts["incomplete"],
        "tests": rows,
        "packages": packages,
        "failures": failures,
    }


# ----------------------------------------------------------------------------
# build-report
# ----------------------------------------------------------------------------
def _coerce(obj, parser):
    """A manifest field may be an inline dict or a path string to parse."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, str):
        return parser(obj)
    return None


def _status_mark(status: str) -> str:
    return {"pass": "PASS", "fail": "FAIL", "skip": "SKIP",
            "incomplete": "INCOMPLETE"}.get(status, status or "-")


def _match_status(test_name: str, results: dict) -> str:
    if not results:
        return ""
    # Match on exact test name or on the leaf of a pkg-qualified/subtest name.
    for r in results.get("tests", []):
        rn = r["test"]
        if rn == test_name or rn.split("/")[0] == test_name or rn.endswith("/" + test_name):
            return r["status"]
    return ""


def _md_escape(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


def build_report(feature: str, manifest: dict, coverage=None, results=None,
                 out_path: Path = None) -> dict:
    service = manifest.get("service", "")
    devrev = manifest.get("devrev", "")
    tests = manifest.get("tests", []) or []
    gaps = manifest.get("gaps", []) or []

    cov = coverage if coverage is not None else _coerce(manifest.get("coverage"), parse_go_coverage)
    res = results if results is not None else _coerce(manifest.get("results"), parse_go_test)

    unit = [t for t in tests if (t.get("type") or "unit").lower() == "unit"]
    slit = [t for t in tests if (t.get("type") or "").lower() == "slit"]

    # Resolve per-test status from results when the manifest didn't pin one.
    for t in tests:
        if not t.get("status"):
            t["status"] = _match_status(t.get("name", ""), res)

    # Gate verdict: GREEN only when results exist and nothing failed/incomplete.
    if res is None:
        verdict = "UNKNOWN — no test results provided; gate CANNOT pass"
    elif res["failed"] == 0 and res["incomplete"] == 0:
        verdict = "GREEN — all tests pass (gate 6.5b/6.5c satisfied)"
    else:
        n = res["failed"] + res["incomplete"]
        verdict = f"RED — {n} failing/incomplete test(s); PR creation BLOCKED (Step 6.5d retrigger)"

    md: list = []
    md.append(f"# Test Report — {feature}\n")
    meta = [f"Generated {_now_iso()}"]
    if service:
        meta.append(f"Service: {service}")
    if devrev:
        meta.append(f"DevRev: {devrev}")
    md.append("> " + " · ".join(meta))
    md.append("> Gate: Step 6.5 Pre-PR — SLIT + Unit + Review must be green before PR (Step 7).\n")

    # 1. Summary
    md.append("## 1. Summary\n")
    md.append("| Metric | Value |")
    md.append("|--------|-------|")
    md.append(f"| Tests documented | {len(tests)} |")
    md.append(f"| Unit tests | {len(unit)} |")
    md.append(f"| SLIT tests | {len(slit)} |")
    if res is not None:
        md.append(f"| Executed (total) | {res['total']} |")
        md.append(f"| Passed | {res['passed']} |")
        md.append(f"| Failed | {res['failed']} |")
        md.append(f"| Skipped | {res['skipped']} |")
        if res["incomplete"]:
            md.append(f"| Incomplete | {res['incomplete']} |")
    if cov is not None and cov.get("total_pct") is not None:
        md.append(f"| Coverage (total) | {cov['total_pct']}% |")
    md.append("")
    md.append(f"**Gate status: {verdict}**\n")

    # 2. Tests Added
    md.append("## 2. Tests Added\n")
    if tests:
        md.append("| # | Test | Type | File | Covers (requirement / issue) | Asserts | Why | Status |")
        md.append("|---|------|------|------|------------------------------|---------|-----|--------|")
        for i, t in enumerate(tests, 1):
            covers = _md_escape(t.get("covers", ""))
            issue = _md_escape(t.get("issue", ""))
            covers_cell = covers + (f" ({issue})" if issue else "")
            md.append("| {n} | {name} | {ty} | {file} | {cov} | {asserts} | {why} | {st} |".format(
                n=i,
                name=_md_escape(t.get("name", "")),
                ty=_md_escape((t.get("type") or "unit")),
                file=_md_escape(t.get("file", "")),
                cov=covers_cell or "-",
                asserts=_md_escape(t.get("asserts", "")) or "-",
                why=_md_escape(t.get("why", "")) or "-",
                st=_status_mark(t.get("status", "")),
            ))
    else:
        md.append("_No tests documented in the manifest._")
    md.append("")

    # 3. Coverage by Component
    md.append("## 3. Coverage by Component\n")
    if cov and cov.get("by_file"):
        by_file = cov["by_file"]
        if cov.get("format") == "profile":
            md.append("| File | Coverage | Covered/Total stmts |")
            md.append("|------|----------|---------------------|")
            for f in sorted(by_file, key=lambda k: by_file[k]["pct"]):
                r = by_file[f]
                md.append(f"| {f} | {r['pct']}% | {r['covered_stmts']}/{r['total_stmts']} |")
        else:
            md.append("| File | Coverage (avg of funcs) | Funcs |")
            md.append("|------|-------------------------|-------|")
            for f in sorted(by_file, key=lambda k: by_file[k]["pct"]):
                r = by_file[f]
                md.append(f"| {f} | {r['pct']}% | {r.get('funcs', '-')} |")
    else:
        md.append("_No coverage profile provided._")
    md.append("")

    # 4. SLIT vs Unit breakdown
    md.append("## 4. SLIT vs Unit Breakdown\n")
    md.append("| Type | Documented | Passed | Failed |")
    md.append("|------|-----------|--------|--------|")
    for label, group in (("Unit", unit), ("SLIT", slit)):
        p = sum(1 for t in group if t.get("status") == "pass")
        fl = sum(1 for t in group if t.get("status") in ("fail", "incomplete"))
        md.append(f"| {label} | {len(group)} | {p} | {fl} |")
    md.append("")

    # 5. Gaps / Deferred
    md.append("## 5. Gaps / Deferred\n")
    if gaps:
        md.append("| Area | Reason |")
        md.append("|------|--------|")
        for g in gaps:
            md.append(f"| {_md_escape(g.get('area', ''))} | {_md_escape(g.get('reason', ''))} |")
    else:
        md.append("_None recorded._")
    md.append("")

    # Failures appendix (drives the 6.5d retrigger loop)
    if res and res.get("failures"):
        md.append("## Failures (retrigger loop — Step 6.5d)\n")
        for f in res["failures"]:
            md.append(f"- {f}")
        md.append("")

    content = "\n".join(md).rstrip() + "\n"

    if out_path is None:
        out_path = FEATURES / feature / "test-report.md"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")

    return {
        "feature": feature,
        "out": str(out_path),
        "bytes": len(content),
        "tests_documented": len(tests),
        "unit": len(unit),
        "slit": len(slit),
        "coverage_pct": (cov or {}).get("total_pct"),
        "executed": (res or {}).get("total"),
        "failed": (res or {}).get("failed"),
        "verdict": verdict,
    }


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def _load_json_arg(val: str):
    """A --tests/--results/--coverage value may be a file path or inline JSON."""
    p = Path(val)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return json.loads(val)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="test_report.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_cov = sub.add_parser("parse-go-coverage", help="parse coverage.out or `go tool cover -func` output")
    p_cov.add_argument("path")

    p_test = sub.add_parser("parse-go-test", help="parse `go test -json` output")
    p_test.add_argument("path")

    p_rep = sub.add_parser("build-report", help="render test-report.md from a tests manifest")
    p_rep.add_argument("--feature")
    p_rep.add_argument("--tests", required=True, help="manifest file path or inline JSON")
    p_rep.add_argument("--coverage", help="coverage.out / coverage-func.txt (overrides manifest)")
    p_rep.add_argument("--results", help="go test -json file (overrides manifest)")
    p_rep.add_argument("--out", help="output path (default workspace/features/<slug>/test-report.md)")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "parse-go-coverage":
        print(json.dumps(parse_go_coverage(args.path), indent=2, default=str))
        return 0

    if args.cmd == "parse-go-test":
        print(json.dumps(parse_go_test(args.path), indent=2, default=str))
        return 0

    if args.cmd == "build-report":
        manifest = _load_json_arg(args.tests)
        feature = args.feature or manifest.get("feature")
        if not feature:
            print("Error: --feature is required (or set \"feature\" in the manifest)", file=sys.stderr)
            return 2
        coverage = parse_go_coverage(args.coverage) if args.coverage else None
        results = parse_go_test(args.results) if args.results else None
        out_path = Path(args.out) if args.out else None
        summary = build_report(feature, manifest, coverage=coverage, results=results, out_path=out_path)
        print(json.dumps(summary, indent=2, default=str))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
