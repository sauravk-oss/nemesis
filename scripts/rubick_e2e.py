#!/usr/bin/env python3
"""E2E Test Orchestration & Rubick Enrichment.

Coordinates E2E test execution via e2e-test-orchestrator (primary, Twirp HTTP)
or ROAST (fallback, Docker), parses results, and enriches rubick.db with
TestResult nodes + Feature updates.
"""

import json
import subprocess
import time
import logging
import os
import concurrent.futures
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
import requests

log = logging.getLogger(__name__)


# Actual Twirp paths from razorpay/e2e-test-orchestrator proto
_TESTCASE_API = "rzp.e2e_test_orchestrator.testcase.v1.TestcaseAPI"
_EXEC_API = "rzp.e2e_test_orchestrator.test_execution.v1.TestExecutionAPI"
_SUITE_API = "rzp.e2e_test_orchestrator.suite_execution.v1.SuiteExecutionAPI"

# ROAST TestNG group mapping: service slug → group names
ROAST_GROUPS = {
    "emandate-service": "EMANDATE,MANDATE,RECURRING",
    "offers-engine": "OFFER,OFFERS,INSTANT_DISCOUNT",
    "payments-card": "PAYMENT,CARD,CARDPAYMENT",
    "payments-upi": "PAYMENT,UPI",
    "payments-mandate": "MANDATE,EMANDATE",
    "pg-router": "PAYMENT",
    "checkout-service": "CHECKOUT",
    "subscriptions": "SUBSCRIPTION",
    "settlements": "SETTLEMENT,SCROOGE",
    "refunds": "REFUND",
    "scrooge": "SETTLEMENT,SCROOGE",
}


def _twirp(base_url: str, service: str, method: str, body: dict, timeout: int = 10) -> dict:
    """Make a Twirp JSON HTTP call."""
    url = f"{base_url}/twirp/{service}/{method}"
    try:
        resp = requests.post(url, headers={"Content-Type": "application/json"}, json=body, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"error": str(e)}


def e2e_health_check(base_url: str = "http://localhost:8080") -> dict:
    """Ping e2e-test-orchestrator by listing testcases.

    Returns: {"healthy": bool, "error": str or None}
    """
    try:
        resp = _twirp(base_url, _TESTCASE_API, "List", {"count": 1})
        healthy = "error" not in resp or "testcases" in resp
        return {"healthy": healthy, "error": resp.get("error")}
    except Exception as e:
        return {"healthy": False, "error": str(e)}


def list_testcases(service_name: str = "", count: int = 50, base_url: str = "http://localhost:8080") -> dict:
    """List registered testcases, optionally filtered by service name."""
    return _twirp(base_url, _TESTCASE_API, "List", {"count": count, "service_name": service_name})


def create_testcase(
    name: str, service_list: List[str], parent_service: str = "",
    owner: str = "saurav.k@razorpay.com", branch_ref: str = "main",
    base_url: str = "http://localhost:8080"
) -> dict:
    """Register a new testcase with the orchestrator."""
    return _twirp(base_url, _TESTCASE_API, "Create", {
        "name": name,
        "service_list": service_list,
        "parent_service": parent_service or (service_list[0] if service_list else ""),
        "owner": owner,
        "end_to_end_tests_branch_ref": branch_ref,
        "priority": 2,  # P1
    })


def create_test_execution(
    testcase_id: str, execution_id: str = "", base_url: str = "http://localhost:8080"
) -> dict:
    """Create a test execution via Twirp TestExecutionAPI.Create.

    Returns: {"id": str, "testcase_id": str, "status": str, "error": str or None}
    """
    result = _twirp(base_url, _EXEC_API, "Create", {
        "testcase_id": testcase_id,
        "execution_id": execution_id,
    })
    if "error" in result:
        return {"execution_id": None, "error": result["error"]}
    return {"execution_id": result.get("id"), "error": None, "raw": result}


def get_test_execution(
    execution_id: str, base_url: str = "http://localhost:8080"
) -> dict:
    """Fetch test execution status & results via TestExecutionAPI.Get."""
    return _twirp(base_url, _EXEC_API, "Get", {"id": execution_id})


def poll_execution(
    execution_id: str, base_url: str = "http://localhost:8080", max_wait: int = 300
) -> dict:
    """Poll until execution completes or timeout.

    Returns: result dict from get_test_execution, or {"error": str}
    """
    start = time.time()
    while time.time() - start < max_wait:
        result = get_test_execution(execution_id, base_url)
        if "error" in result:
            return result
        status = result.get("status", "").lower()
        if status in ["completed", "failed", "error"]:
            return result
        time.sleep(5)
    return {"error": f"Poll timeout after {max_wait}s"}


def run_roast(env: str, groups: Optional[List[str]] = None) -> dict:
    """Run ROAST tests via Docker.

    Returns: {"passed": int, "failed": int, "skipped": int, "duration_s": float, "error": str or None}
    """
    try:
        groups_str = ",".join(groups) if groups else "smoke"
        cmd = [
            "docker", "run", "--rm",
            "-e", f"ENV={env}",
            "-e", f"INCLUDE_GROUPS={groups_str}",
            "roast:master"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0:
            # Parse ROAST output (JSON or TAP format)
            try:
                data = json.loads(result.stdout)
                return {
                    "passed": data.get("passed", 0),
                    "failed": data.get("failed", 0),
                    "skipped": data.get("skipped", 0),
                    "duration_s": data.get("duration_s", 0),
                    "error": None
                }
            except:
                # Fallback: assume success if returncode is 0
                return {"passed": 1, "failed": 0, "skipped": 0, "duration_s": 0, "error": None}
        else:
            return {
                "passed": 0, "failed": 1, "skipped": 0, "duration_s": 0,
                "error": result.stderr or "ROAST exited with non-zero code"
            }
    except subprocess.TimeoutExpired:
        return {"passed": 0, "failed": 1, "skipped": 0, "duration_s": 600, "error": "ROAST timeout"}
    except Exception as e:
        return {"passed": 0, "failed": 1, "skipped": 0, "duration_s": 0, "error": str(e)}


def parse_e2e_results(raw: dict) -> dict:
    """Normalize results from either backend.

    Returns: {
        "passed": int,
        "failed": int,
        "skipped": int,
        "duration_s": float,
        "status": "passed" | "failed" | "partial",
        "failures": [{"test": str, "error": str}, ...]
    }
    """
    if "error" in raw:
        return {
            "passed": 0,
            "failed": 1,
            "skipped": 0,
            "duration_s": 0,
            "status": "failed",
            "failures": [{"test": "orchestration", "error": raw["error"]}]
        }

    passed = raw.get("passed", 0)
    failed = raw.get("failed", 0)
    skipped = raw.get("skipped", 0)
    duration = raw.get("duration_s", 0)
    failures = raw.get("failures", [])

    if failed > 0:
        status = "failed"
    elif passed > 0 and failed == 0:
        status = "passed"
    else:
        status = "partial"

    return {
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "duration_s": duration,
        "status": status,
        "failures": failures
    }


def enrich_rubick_with_e2e(slug: str, service: str, results: dict) -> None:
    """Write TestResult nodes + update Feature nodes in rubick.db.

    Creates:
    - TestResult node with test execution data
    - Links Feature -> VALIDATED_BY -> TestResult
    - For each failure: Signal -> RiskItem (if not exists)
    - Updates Feature.data.e2e_status = "passed" | "failed" | "partial"
    """
    from scripts.rubick_graph import db, upsert_node, upsert_edge, learn_node
    from datetime import datetime
    import json

    c = db()
    try:
        now = datetime.utcnow().isoformat()
        test_result_name = f"e2e:{slug}:{service}:{int(datetime.utcnow().timestamp())}"

        # Create TestResult node
        test_result_data = {
            "feature_slug": slug,
            "service": service,
            "passed": results["passed"],
            "failed": results["failed"],
            "skipped": results["skipped"],
            "duration_s": results["duration_s"],
            "status": results["status"],
            "run_at": now,
            "failures": results.get("failures", [])
        }

        upsert_node(
            "TestResult",
            test_result_name,
            json.dumps(test_result_data),
            source_type="e2e",
            source_id=slug,
            confidence=0.85
        )

        # Link Feature -> VALIDATED_BY -> TestResult
        feature_name = None
        feature_rows = c.execute(
            "SELECT id, name FROM nodes WHERE type='Feature' AND data LIKE ?",
            (f'%"{slug}"%',)
        ).fetchall()
        if feature_rows:
            feature_name = feature_rows[0]['name']
            feature_id = feature_rows[0]['id']
            test_result_id = c.execute(
                "SELECT id FROM nodes WHERE name=? AND type='TestResult'",
                (test_result_name,)
            ).fetchone()['id']
            upsert_edge(feature_id, test_result_id, "VALIDATED_BY")

        # Process failures -> Signal/RiskItem
        for failure in results.get("failures", []):
            test_name = failure.get("test", "unknown")
            error_msg = failure.get("error", "")

            # Create Signal node
            signal_name = f"signal:e2e-failure:{slug}:{test_name}:{int(datetime.utcnow().timestamp())}"
            signal_data = {
                "phase": "e2e",
                "type": "test_failure",
                "test": test_name,
                "error": error_msg,
                "feature_slug": slug,
                "service": service
            }
            upsert_node(
                "Signal",
                signal_name,
                json.dumps(signal_data),
                source_type="e2e",
                source_id=slug,
                confidence=0.8
            )

            # Link to or create RiskItem
            risk_item_name = f"risk:e2e-{test_name}"
            existing_risk = c.execute(
                "SELECT id FROM nodes WHERE name=? AND type='RiskItem'",
                (risk_item_name,)
            ).fetchone()

            if not existing_risk:
                risk_data = {
                    "title": f"E2E test failure: {test_name}",
                    "description": error_msg,
                    "severity": "high",
                    "category": "testing",
                    "confidence": 0.8
                }
                upsert_node(
                    "RiskItem",
                    risk_item_name,
                    json.dumps(risk_data),
                    source_type="e2e",
                    source_id=slug,
                    confidence=0.8
                )

            # Link Signal -> RiskItem
            signal_id = c.execute(
                "SELECT id FROM nodes WHERE name=?",
                (signal_name,)
            ).fetchone()['id']
            risk_id = c.execute(
                "SELECT id FROM nodes WHERE name=? AND type='RiskItem'",
                (risk_item_name,)
            ).fetchone()['id']
            upsert_edge(signal_id, risk_id, "INDICATES")

        # Update Feature.e2e_status
        if feature_name:
            feature_row = c.execute(
                "SELECT data FROM nodes WHERE name=? AND type='Feature'",
                (feature_name,)
            ).fetchone()
            if feature_row:
                data = json.loads(feature_row['data']) if feature_row['data'] else {}
                data['e2e_status'] = results['status']
                c.execute(
                    "UPDATE nodes SET data=? WHERE name=? AND type='Feature'",
                    (json.dumps(data), feature_name)
                )
                c.commit()

        log.info(f"E2E enrichment complete for {slug}:{service} — {results['status']}")
    except Exception as e:
        log.error(f"E2E enrichment failed for {slug}: {e}")
    finally:
        c.close()


# ── Local runner ──────────────────────────────────────────────────────────────

REPOS_DIR = Path(__file__).parent.parent / "workspace" / "repos"

# Services with their own e2e/ test dirs (detected from repo scan)
SERVICES_WITH_E2E_DIR = {
    "api", "checkout-service", "dashboard", "dcs", "edge", "offers-engine",
    "payment-methods", "payments-bank-transfer", "payments-nb-wallet",
    "shield", "stork", "subscriptions", "tokenhq-e2e-tests",
}

# Devstack base URLs per service (APP_ENV=e2e config)
DEVSTACK_HOSTS = {
    "offers-engine":        "https://offers-engine.dev.razorpay.in",
    "emandate-service":     "https://emandate-service.dev.razorpay.in",
    "checkout-service":     "https://checkout-service.dev.razorpay.in",
    "pg-router":            "https://pg-router.dev.razorpay.in",
    "payments-card":        "https://payments-card.dev.razorpay.in",
    "payments-upi":         "https://payments-upi.dev.razorpay.in",
    "payments-mandate":     "https://payments-mandate.dev.razorpay.in",
    "api":                  "https://api.dev.razorpay.in",
    "shield":               "https://shield.dev.razorpay.in",
    "dcs":                  "https://dcs.dev.razorpay.in",
    "subscriptions":        "https://subscriptions.dev.razorpay.in",
    "stork":                "https://stork.dev.razorpay.in",
    "edge":                 "https://edge.dev.razorpay.in",
}


def detect_local_e2e_method(service_slug: str) -> dict:
    """Detect the best local E2E runner for a service.

    Returns: {
        "method": "go_test" | "roast" | "orchestrator" | "none",
        "repo_path": str or None,
        "roast_groups": str or None,
        "has_e2e_dir": bool,
        "requires_devstack": bool,
    }
    """
    repo_path = REPOS_DIR / service_slug
    has_e2e_dir = (repo_path / "e2e").exists() and any((repo_path / "e2e").glob("*_test.go"))
    roast_groups = ROAST_GROUPS.get(service_slug)

    if has_e2e_dir:
        return {
            "method": "go_test",
            "repo_path": str(repo_path),
            "roast_groups": roast_groups,
            "has_e2e_dir": True,
            "requires_devstack": True,
            "run_cmd": f"cd {repo_path} && APP_ENV=e2e go test ./e2e/... -v -timeout 300s",
        }
    elif roast_groups:
        return {
            "method": "roast",
            "repo_path": None,
            "roast_groups": roast_groups,
            "has_e2e_dir": False,
            "requires_devstack": False,
            "run_cmd": f"docker run --rm -e ENV=test -e MODE=intg -e INCLUDE_GROUPS={roast_groups} {ROAST_GROUPS.get('_image','roast:master')}",
        }
    else:
        return {
            "method": "none",
            "repo_path": str(repo_path) if repo_path.exists() else None,
            "roast_groups": None,
            "has_e2e_dir": False,
            "requires_devstack": False,
            "run_cmd": None,
        }


def run_local_e2e(service_slug: str, env: str = "e2e", timeout: int = 300,
                  devstack_label: str = "") -> dict:
    """Run E2E tests locally for a single service.

    Uses go test ./e2e/... for services with e2e/ dir,
    falls back to ROAST docker for others.

    Returns: parse_e2e_results-compatible dict.
    """
    method_info = detect_local_e2e_method(service_slug)
    method = method_info["method"]
    t0 = time.time()

    if method == "go_test":
        repo_path = method_info["repo_path"]
        env_vars = os.environ.copy()
        env_vars["APP_ENV"] = env
        if devstack_label:
            env_vars["DEVSTACK_LABEL"] = devstack_label

        try:
            result = subprocess.run(
                ["go", "test", "./e2e/...", "-v", f"-timeout={timeout}s", "-json"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=timeout + 30,
                env=env_vars,
            )
            return _parse_go_test_json(result.stdout, result.stderr, time.time() - t0)
        except subprocess.TimeoutExpired:
            return parse_e2e_results({"error": f"go test timeout after {timeout}s"})
        except FileNotFoundError:
            return parse_e2e_results({"error": "go not found — install Go toolchain"})

    elif method == "roast":
        return run_roast(
            env="test",
            groups=method_info["roast_groups"].split(",") if method_info["roast_groups"] else None,
        )

    else:
        return {
            "passed": 0, "failed": 0, "skipped": 0, "duration_s": 0,
            "status": "skipped",
            "failures": [],
            "note": f"No local E2E available for {service_slug}. "
                    "Add e2e/ tests or a ROAST group mapping.",
        }


def _parse_go_test_json(stdout: str, stderr: str, duration: float) -> dict:
    """Parse `go test -json` output into normalized results."""
    passed = failed = skipped = 0
    failures = []

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        action = event.get("Action", "")
        test_name = event.get("Test", "")

        if action == "pass" and test_name:
            passed += 1
        elif action == "fail" and test_name:
            failed += 1
            output = event.get("Output", "")
            failures.append({"test": test_name, "error": output.strip()})
        elif action == "skip" and test_name:
            skipped += 1

    # Fallback: parse non-JSON output
    if passed == 0 and failed == 0:
        for line in (stdout + stderr).splitlines():
            if line.startswith("--- PASS"):
                passed += 1
            elif line.startswith("--- FAIL"):
                failed += 1
                failures.append({"test": line.split()[-1] if len(line.split()) > 2 else "unknown", "error": ""})
            elif line.startswith("--- SKIP"):
                skipped += 1

    status = "failed" if failed > 0 else ("passed" if passed > 0 else "partial")
    return {
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "duration_s": round(duration, 2),
        "status": status,
        "failures": failures,
    }


# ── Service-specific parallel pipeline ───────────────────────────────────────

def run_service_pipeline(
    feature_slug: str,
    services: List[str],
    env: str = "e2e",
    devstack_label: str = "",
    timeout_per_service: int = 300,
    max_workers: int = 4,
) -> dict:
    """Run E2E tests for all impacted services in parallel.

    Each service gets its own test run. Results are aggregated into a
    per-service breakdown + overall status.

    Returns: {
        "overall_status": "passed" | "failed" | "partial",
        "services": {
            "<service>": {
                "method": "go_test" | "roast" | "none",
                "status": "passed" | "failed" | "skipped",
                "passed": int, "failed": int, "skipped": int,
                "duration_s": float,
                "failures": [...],
            }
        },
        "total_passed": int,
        "total_failed": int,
        "total_skipped": int,
        "duration_s": float,
    }
    """
    t0 = time.time()
    service_results: Dict[str, dict] = {}

    def _run_one(svc: str) -> Tuple[str, dict]:
        method_info = detect_local_e2e_method(svc)
        result = run_local_e2e(svc, env=env, timeout=timeout_per_service,
                                devstack_label=devstack_label)
        result["method"] = method_info["method"]
        result["requires_devstack"] = method_info.get("requires_devstack", False)
        return svc, result

    log.info(f"Running service pipeline for {feature_slug}: {services}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_run_one, svc): svc for svc in services}
        for future in concurrent.futures.as_completed(futures):
            svc, result = future.result()
            service_results[svc] = result
            log.info(f"  {svc}: {result['status']} ({result['passed']}p/{result['failed']}f)")

    total_passed = sum(r["passed"] for r in service_results.values())
    total_failed = sum(r["failed"] for r in service_results.values())
    total_skipped = sum(r["skipped"] for r in service_results.values())
    any_failed = any(r["status"] == "failed" for r in service_results.values())
    all_passed = all(r["status"] in ("passed", "skipped") for r in service_results.values())

    overall = "failed" if any_failed else ("passed" if all_passed else "partial")

    # Enrich rubick for each service
    for svc, result in service_results.items():
        try:
            enrich_rubick_with_e2e(feature_slug, svc, result)
        except Exception as e:
            log.warning(f"Brain enrichment failed for {svc}: {e}")

    return {
        "overall_status": overall,
        "services": service_results,
        "total_passed": total_passed,
        "total_failed": total_failed,
        "total_skipped": total_skipped,
        "duration_s": round(time.time() - t0, 2),
    }


def service_pipeline_report(feature_slug: str, pipeline_result: dict) -> str:
    """Generate a markdown e2e-report.md from service pipeline results."""
    lines = [
        f"# E2E Test Results — {feature_slug}",
        "",
        f"**Overall Status**: {'✅ PASSED' if pipeline_result['overall_status'] == 'passed' else ('❌ FAILED' if pipeline_result['overall_status'] == 'failed' else '⚠️ PARTIAL')}",
        f"**Duration**: {pipeline_result['duration_s']}s",
        f"**Services Tested**: {len(pipeline_result['services'])}",
        "",
        "## Per-Service Results",
        "",
        "| Service | Method | Status | Passed | Failed | Skipped | Duration |",
        "|---------|--------|--------|--------|--------|---------|----------|",
    ]

    for svc, r in sorted(pipeline_result["services"].items()):
        status_icon = {"passed": "✅", "failed": "❌", "partial": "⚠️", "skipped": "⏭️"}.get(r["status"], "?")
        method = r.get("method", "?")
        lines.append(
            f"| {svc} | {method} | {status_icon} {r['status']} | "
            f"{r['passed']} | {r['failed']} | {r['skipped']} | {r.get('duration_s', 0)}s |"
        )

    # Failures section
    any_failures = any(r["failures"] for r in pipeline_result["services"].values())
    if any_failures:
        lines += ["", "## Failures", ""]
        for svc, r in pipeline_result["services"].items():
            if r["failures"]:
                lines.append(f"### {svc}")
                for f in r["failures"]:
                    lines.append(f"- **{f['test']}**: `{f['error'][:200]}`")
                lines.append("")

    # Skipped / no-E2E services
    no_e2e = [svc for svc, r in pipeline_result["services"].items() if r.get("method") == "none"]
    if no_e2e:
        lines += ["", "## Services Without E2E Coverage", ""]
        for svc in no_e2e:
            lines.append(f"- `{svc}` — add e2e/ tests or ROAST group mapping")

    lines += [
        "",
        "## Summary",
        f"- Total passed: {pipeline_result['total_passed']}",
        f"- Total failed: {pipeline_result['total_failed']}",
        f"- Total skipped: {pipeline_result['total_skipped']}",
    ]

    return "\n".join(lines)
