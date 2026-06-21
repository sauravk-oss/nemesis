#!/usr/bin/env python3
"""Change Report — render workspace/features/<slug>/change-report.md.

Answers the question /implement Step 6.5 leaves open: *after review + test-run +
fix, what changed, why, which tests were added and pass, and which tests still
need to pass before we can fully ramp?*

Pure Python, stdlib only. This script NEVER runs `go`, `git`, or any MCP. The LLM
(skill layer) runs the toolchain and assembles a *change manifest* describing the
semantic story (what/why/covers/resolution — none of which a parser can infer);
this script parses the machine test outputs and renders the markdown report.

It reuses the `go test -json` / coverage parsers from test_report.py so the test
status shown here is the *same* status the Pre-PR gate enforced — one source of truth.

Subcommands
-----------
  build --feature <slug> --changes <manifest.json>
        [--results <go-test-json>] [--coverage <coverage.out|func.txt>]
        [--out <path>]
        -> writes change-report.md, prints a summary JSON

  verdict --changes <manifest.json> [--results <go-test-json>]
        -> prints just the merge verdict JSON (no file written) — handy for gates

Change manifest schema (assembled by the LLM):
{
  "feature": "gpay-bifrost-account-matching",   # optional; --feature wins
  "devrev":  "ENH-18653",
  "service": "payments-upi",
  "branch":  "feature/gpay-bifrost-account-matching",
  "summary": "One paragraph: what changed and why (the business + technical why).",
  "changes": [
    {
      "file":  "internal/app/payment/processor/initiate/initiate.go",
      "kind":  "modify",                         # add | modify | delete
      "what":  "fire-and-forget goroutine after callGatewayInitiate succeeds",
      "why":   "register order with Google Bifrost so GPay shows only the KYC account (SEBI TPV)",
      "covers":"FR-1 / ENH-18653",               # requirement / risk / issue it satisfies
      "loc":   "+120/-3"                          # optional
    }
  ],
  "tests_added": [
    {"name":"TestRegisterWithBifrost_HappyPath","type":"unit",
     "file":"...initiate_test.go","covers":"happy path","why":"all 4 gates pass -> RegisterOrder called",
     "status":"passing"}                          # passing | pending | failing (resolved from --results if omitted)
  ],
  "pending_tests": [
    {"name":"TestBifrost_RealCredentials","reason":"blocked on Q1 Google enrollment",
     "needed_for":"production ramp (mock=false)"}
  ],
  "review": [
    {"id":"P0-1","severity":"P0","finding":"bank fields logged at DEBUG",
     "status":"resolved","resolution":"removed PII from all log/metric labels"}
  ],
  "results":  "results.json",                    # optional path or inline dict
  "coverage": "coverage.out"                      # optional path or inline dict
}
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make the repo root importable so `brain.config` and `test_report` resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Reuse the gate's own parsers — same test status the Pre-PR gate enforced.
from test_report import parse_go_coverage, parse_go_test, _match_status  # noqa: E402

try:
    from brain.config import BrainConfig  # noqa: E402

    _WORKSPACE = Path(BrainConfig().workspace)
except Exception:  # pragma: no cover - bare CI checkout fallback
    _WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"

FEATURES = _WORKSPACE / "features"

# Severities that block a merge until resolved.
_BLOCKING = {"p0", "critical", "blocker"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _md_escape(s) -> str:
    return (str(s) if s is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def _coerce(obj, parser):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, str):
        return parser(obj)
    return None


def _norm_status(s: str) -> str:
    s = (s or "").lower()
    if s in ("pass", "passing", "passed", "green", "ok"):
        return "passing"
    if s in ("fail", "failing", "failed", "red"):
        return "failing"
    if s in ("pending", "todo", "blocked", "deferred", "skip", "skipped", "incomplete"):
        return "pending"
    return s or "pending"


def compute_verdict(manifest: dict, results=None) -> dict:
    """Decide whether the change is mergeable and why.

    Mergeable when: no failing test AND no unresolved blocking review finding.
    'pending' tests do NOT block the merge (they're often gated on external
    dependencies, e.g. credentials) — they're tracked as ramp blockers instead.
    """
    res = results if results is not None else _coerce(manifest.get("results"), parse_go_test)

    tests = manifest.get("tests_added", []) or []
    for t in tests:
        if not t.get("status"):
            t["status"] = _match_status(t.get("name", ""), res) or "pending"
        t["_status"] = _norm_status(t["status"])

    failing = [t for t in tests if t["_status"] == "failing"]
    pending = [t for t in tests if t["_status"] == "pending"]
    # Executed failures (from go test -json) also block, even if not in tests_added.
    exec_failed = (res or {}).get("failed", 0) + (res or {}).get("incomplete", 0)

    review = manifest.get("review", []) or []
    open_blockers = [
        r for r in review
        if (r.get("severity", "").lower() in _BLOCKING)
        and (r.get("status", "").lower() not in ("resolved", "closed", "fixed", "done"))
    ]

    ramp_blockers = list(pending) + list(manifest.get("pending_tests", []) or [])

    if failing or exec_failed or open_blockers:
        reasons = []
        if failing:
            reasons.append(f"{len(failing)} failing test(s) in manifest")
        if exec_failed:
            reasons.append(f"{exec_failed} failing/incomplete in test run")
        if open_blockers:
            reasons.append(f"{len(open_blockers)} unresolved P0/Critical review finding(s)")
        verdict = "BLOCKED — " + "; ".join(reasons)
        mergeable = False
    elif ramp_blockers:
        verdict = (f"MERGEABLE — code + tests green; {len(ramp_blockers)} test(s) still "
                   f"PENDING before full ramp (non-blocking for merge)")
        mergeable = True
    else:
        verdict = "READY — all changes covered, all tests pass, no open blockers"
        mergeable = True

    return {
        "mergeable": mergeable,
        "verdict": verdict,
        "failing": len(failing),
        "pending": len(pending),
        "exec_failed": exec_failed,
        "open_blockers": len(open_blockers),
        "ramp_blockers": len(ramp_blockers),
    }


def build_report(feature: str, manifest: dict, results=None, coverage=None,
                 out_path: Path = None) -> dict:
    service = manifest.get("service", "")
    devrev = manifest.get("devrev", "")
    branch = manifest.get("branch", "")
    summary = manifest.get("summary", "")
    changes = manifest.get("changes", []) or []
    tests = manifest.get("tests_added", []) or []
    pending_tests = manifest.get("pending_tests", []) or []
    review = manifest.get("review", []) or []

    res = results if results is not None else _coerce(manifest.get("results"), parse_go_test)
    cov = coverage if coverage is not None else _coerce(manifest.get("coverage"), parse_go_coverage)

    v = compute_verdict(manifest, results=res)  # also normalizes t["_status"]

    passing = [t for t in tests if t.get("_status") == "passing"]
    failing = [t for t in tests if t.get("_status") == "failing"]
    pending = [t for t in tests if t.get("_status") == "pending"]

    md: list = []
    md.append(f"# Change Report — {feature}\n")
    meta = [f"Generated {_now_iso()}"]
    if service:
        meta.append(f"Service: {service}")
    if devrev:
        meta.append(f"DevRev: {devrev}")
    if branch:
        meta.append(f"Branch: {branch}")
    md.append("> " + " · ".join(meta) + "\n")

    # 1. Summary
    md.append("## 1. Summary\n")
    md.append((summary.strip() if summary else "_No summary provided._") + "\n")
    md.append(f"**Verdict: {v['verdict']}**\n")

    # 2. Changes (what + why)
    md.append("## 2. Changes Made (what + why)\n")
    if changes:
        md.append("| # | File | Change | What | Why | Covers | LOC |")
        md.append("|---|------|--------|------|-----|--------|-----|")
        for i, c in enumerate(changes, 1):
            md.append("| {n} | {f} | {k} | {what} | {why} | {cov} | {loc} |".format(
                n=i,
                f=_md_escape(c.get("file", "")),
                k=_md_escape(c.get("kind", "modify")),
                what=_md_escape(c.get("what", "")) or "-",
                why=_md_escape(c.get("why", "")) or "-",
                cov=_md_escape(c.get("covers", "")) or "-",
                loc=_md_escape(c.get("loc", "")) or "-",
            ))
    else:
        md.append("_No changes recorded in the manifest._")
    md.append("")

    # 3. Tests Added (passing)
    md.append("## 3. Tests Added\n")
    md.append(f"**{len(passing)} passing · {len(pending)} pending · {len(failing)} failing** "
              f"(of {len(tests)} documented)\n")
    if tests:
        md.append("| # | Test | Type | File | Covers | Why | Status |")
        md.append("|---|------|------|------|--------|-----|--------|")
        for i, t in enumerate(tests, 1):
            md.append("| {n} | {name} | {ty} | {file} | {cov} | {why} | {st} |".format(
                n=i,
                name=_md_escape(t.get("name", "")),
                ty=_md_escape(t.get("type") or "unit"),
                file=_md_escape(t.get("file", "")),
                cov=_md_escape(t.get("covers", "")) or "-",
                why=_md_escape(t.get("why", "")) or "-",
                st=t.get("_status", "pending").upper(),
            ))
    else:
        md.append("_No tests documented._")
    md.append("")

    # 4. Pending Tests (still need to pass)
    md.append("## 4. Pending Tests (still need to pass)\n")
    combined_pending = [{"name": t.get("name", ""),
                         "reason": t.get("reason", "not yet passing"),
                         "needed_for": t.get("needed_for", "")} for t in pending_tests]
    # Also surface manifest tests that resolved to 'pending'/'failing' as work remaining.
    for t in pending + failing:
        combined_pending.append({"name": t.get("name", ""),
                                 "reason": ("failing" if t.get("_status") == "failing"
                                            else "pending"),
                                 "needed_for": _md_escape(t.get("covers", ""))})
    if combined_pending:
        md.append("| # | Test | Reason | Needed for |")
        md.append("|---|------|--------|------------|")
        for i, t in enumerate(combined_pending, 1):
            md.append(f"| {i} | {_md_escape(t['name'])} | {_md_escape(t['reason'])} "
                      f"| {_md_escape(t['needed_for']) or '-'} |")
    else:
        md.append("_None — every documented test passes._")
    md.append("")

    # 5. Review Findings
    md.append("## 5. Review Findings Resolved\n")
    if review:
        md.append("| ID | Severity | Finding | Status | Resolution |")
        md.append("|----|----------|---------|--------|------------|")
        for r in review:
            md.append("| {id} | {sev} | {find} | {st} | {res} |".format(
                id=_md_escape(r.get("id", "")),
                sev=_md_escape(r.get("severity", "")),
                find=_md_escape(r.get("finding", "")),
                st=_md_escape(r.get("status", "")),
                res=_md_escape(r.get("resolution", "")) or "-",
            ))
    else:
        md.append("_No review findings recorded._")
    md.append("")

    # 6. Coverage (optional)
    if cov is not None and cov.get("total_pct") is not None:
        md.append("## 6. Coverage\n")
        md.append(f"Total: **{cov['total_pct']}%**")
        if res is not None:
            md.append(f" · Executed {res['total']} ({res['passed']} pass / "
                      f"{res['failed']} fail / {res['skipped']} skip)")
        md.append("")
        md.append("See `test-report.md` for per-component coverage.\n")

    content = "\n".join(md).rstrip() + "\n"

    if out_path is None:
        out_path = FEATURES / feature / "change-report.md"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")

    return {
        "feature": feature,
        "out": str(out_path),
        "bytes": len(content),
        "changes": len(changes),
        "tests_passing": len(passing),
        "tests_pending": len(pending) + len(pending_tests),
        "tests_failing": len(failing),
        "review_findings": len(review),
        "mergeable": v["mergeable"],
        "verdict": v["verdict"],
    }


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def _load_json_arg(val: str):
    p = Path(val)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return json.loads(val)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="change_report.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="render change-report.md from a change manifest")
    p_build.add_argument("--feature")
    p_build.add_argument("--changes", required=True, help="manifest file path or inline JSON")
    p_build.add_argument("--results", help="go test -json file (overrides manifest)")
    p_build.add_argument("--coverage", help="coverage.out / func.txt (overrides manifest)")
    p_build.add_argument("--out", help="output path (default workspace/features/<slug>/change-report.md)")

    p_verdict = sub.add_parser("verdict", help="print merge verdict JSON only (no file written)")
    p_verdict.add_argument("--changes", required=True, help="manifest file path or inline JSON")
    p_verdict.add_argument("--results", help="go test -json file (overrides manifest)")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "verdict":
        manifest = _load_json_arg(args.changes)
        results = parse_go_test(args.results) if args.results else None
        print(json.dumps(compute_verdict(manifest, results=results), indent=2, default=str))
        return 0

    if args.cmd == "build":
        manifest = _load_json_arg(args.changes)
        feature = args.feature or manifest.get("feature")
        if not feature:
            print("Error: --feature is required (or set \"feature\" in the manifest)", file=sys.stderr)
            return 2
        results = parse_go_test(args.results) if args.results else None
        coverage = parse_go_coverage(args.coverage) if args.coverage else None
        out_path = Path(args.out) if args.out else None
        summary = build_report(feature, manifest, results=results, coverage=coverage, out_path=out_path)
        print(json.dumps(summary, indent=2, default=str))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
