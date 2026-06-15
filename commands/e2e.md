---
description: "E2E testing orchestrator for Nemesis v2 Phase 5. Automated test generation, execution via e2e-test-orchestrator (Twirp) or ROAST (Docker), SLIT test integration, auto-code-gen for failures, coverage review, gatekeeper merge criteria, and workspace/brain.db enrichment. Spawns e2e-expert-agent for complex generation tasks."
---

# /e2e — E2E Test Expert

**Phase 5 of the Nemesis feature pipeline.** Runs after Implementation is complete (or after Tech Spec if Implementation is skipped).

## MCP Tools Available

These tools are registered in Claude Desktop via the `e2e-orchestrator` MCP server:

| Tool | Purpose |
|------|---------|
| `e2e_health_check` | Check if e2e-test-orchestrator is running |
| `e2e_detect_local_method` | Detect best runner for each service (go_test/roast/none) |
| `e2e_run_service_pipeline` | **Run all impacted services in parallel** (primary tool) |
| `e2e_run_local` | Run E2E for a single service locally |
| `e2e_list_testcases` | List registered testcases by service |
| `e2e_create_testcase` | Register a new testcase |
| `e2e_run_testcase` | Execute via orchestrator by testcase ID |
| `e2e_get_execution` | Get execution status/results |
| `e2e_get_execution_history` | Recent execution history |
| `e2e_run_suite` | Run a suite of testcases |
| `e2e_run_roast` | Run ROAST (Java/Docker) test suite directly |
| `e2e_ingest_results` | Persist results to workspace/brain.db |

## Command Router

| Input | Action |
|-------|--------|
| `/e2e status` | Check orchestrator health + local env status |
| `/e2e run <slug>` | Full Phase 4: detect services → run each → aggregate → ingest |
| `/e2e pipeline <slug> [svc1,svc2,...]` | Run service-specific pipelines in parallel |
| `/e2e local <slug>` | Run all services locally (no devstack needed for ROAST path) |
| `/e2e write <slug> <service>` | Generate E2E test code for a service |
| `/e2e review <slug>` | Coverage gap analysis |
| `/e2e report <slug>` | Show latest e2e-report.md |
| `/e2e list <service>` | List testcases for a service |
| `/e2e roast <service> [env]` | Run ROAST tests for a service |
| `/e2e ingest <slug>` | Re-ingest existing results to workspace/brain.db |

---

## Local Runner Decision Tree

For every service, the system picks the best available runner automatically:

```
service has e2e/ dir?
  YES → go test ./e2e/... -v (requires devstack or APP_ENV=e2e config)
  NO  → service in ROAST_GROUPS?
          YES → docker run roast:master -e INCLUDE_GROUPS=<groups>
          NO  → generate test code + skip (report as "no coverage")
```

**Services with `e2e/` dirs** (go test path):
`api`, `checkout-service`, `dashboard`, `dcs`, `edge`, `offers-engine`,
`payment-methods`, `payments-bank-transfer`, `payments-nb-wallet`, `shield`, `stork`, `subscriptions`

**Services covered by ROAST** (docker path — works without devstack):
`emandate-service` → `EMANDATE,MANDATE,RECURRING`
`offers-engine` → `OFFER,OFFERS,INSTANT_DISCOUNT`
`payments-card` → `PAYMENT,CARD`
`payments-upi` → `PAYMENT,UPI`
`payments-mandate` → `MANDATE,EMANDATE`
`pg-router` → `PAYMENT`
`subscriptions` → `SUBSCRIPTION`
`checkout-service` → `CHECKOUT`
`settlements/scrooge` → `SETTLEMENT,SCROOGE`

---

## `/e2e pipeline <slug> [svc1,svc2,...]` — Service-Specific Parallel Pipeline

**This is the primary E2E command for multi-service features.**

```
1. Detect services (from solution.html or explicit list)
2. e2e_detect_local_method {services: [...]} → method map per service
3. e2e_run_service_pipeline {
     feature_slug: slug,
     services: [...],
     env: "e2e",           # or "test" for ROAST
     timeout_per_service: 300
   }
   → Runs ALL services in parallel (concurrent.futures ThreadPoolExecutor)
   → Each service uses its detected method (go_test or roast)
   → Results aggregated per service
4. Write workspace/features/<slug>/e2e-report.md
5. Ingest all results to brain (one TestResult node per service)
```

**Example for a feature touching offers-engine + emandate-service + pg-router:**

```
e2e_run_service_pipeline {
  feature_slug: "dfb-instant-discount",
  services: ["offers-engine", "emandate-service", "pg-router"],
  env: "e2e"
}
```

Runs in parallel:
- `offers-engine` → `go test ./e2e/...` (has e2e/ dir) + ROAST OFFER group
- `emandate-service` → `docker run roast:master -e INCLUDE_GROUPS=EMANDATE,MANDATE`
- `pg-router` → `docker run roast:master -e INCLUDE_GROUPS=PAYMENT`

Report per service:
```
| Service | Method | Status | Passed | Failed | Skipped | Duration |
|---------|--------|--------|--------|--------|---------|----------|
| offers-engine | go_test | ✅ passed | 12 | 0 | 1 | 45s |
| emandate-service | roast | ✅ passed | 38 | 0 | 0 | 120s |
| pg-router | roast | ⚠️ partial | 22 | 3 | 0 | 95s |
```

---

## `/e2e local <slug>` — Run Locally Without Devstack

For ROAST-covered services this runs without any devstack requirement.
For `e2e/` services it needs devstack or a locally running service.

```
1. Detect services from solution.html
2. Separate: ROAST services (runnable now) vs go_test services (need devstack)
3. Run ROAST services immediately
4. For go_test services: show command to run + devstack label needed
5. Report immediately runnable results
```

**Shell equivalent** (from the setup script):
```bash
# Check local environment
./scripts/start_e2e_local.sh status

# Run single service (go test - needs devstack)
./scripts/start_e2e_local.sh service offers-engine e2e saurav.k

# Run via ROAST (no devstack needed)
./scripts/start_e2e_local.sh roast emandate-service test intg

# Run full service pipeline
./scripts/start_e2e_local.sh pipeline dfb-fix offers-engine,emandate-service e2e
```

---

## `/e2e status`

```
1. e2e_health_check → show orchestrator URL + health
2. e2e_get_execution_history {count:10} → last 10 runs
3. python -m brain search "" --type TestResult   (shows last 5 TestResult nodes)
```

Output format:
```
E2E Orchestrator: ✓ HEALTHY (http://localhost:8080)
Recent runs: 3 passed, 1 failed (last 24h)

TestResult nodes in Brain: 12
- e2e:dfb-fix:offers-engine (passed, 2026-05-24)
- e2e:cfb-fix:payments-card (passed, 2026-05-23)
```

---

## `/e2e run <slug>` — Full Phase 4 Pipeline

**Prerequisites:** `workspace/features/<slug>/solution.html` must exist (Tech Spec complete).

### Step 1 — Load context
```python
from pathlib import Path
import re

feat_dir = Path(f"workspace/features/{slug}")
solution_html = (feat_dir / "solution.html").read_text()

# Extract impacted services from solution (look for service names)
from brain.config import SEED_PROJECTS
services = [s for s in SEED_PROJECTS if s.lower() in solution_html.lower()]
```

### Step 2 — Load expert test patterns
```python
import sqlite3, json
conn = sqlite3.connect("workspace/brain.db")
# Get expert node IDs for impacted services
experts = conn.execute(
    "SELECT id, name, data FROM nodes WHERE type='ProjectExpert' AND name IN ({})".format(
        ",".join("?" * len(services))), services
).fetchall()

# Load test patterns via expert_tests
for expert in experts:
    tests = conn.execute(
        "SELECT test_name, functions_tested, assertion_count FROM expert_tests WHERE expert_node_id=? LIMIT 20",
        (expert['id'],)
    ).fetchall()
```

### Step 3 — Check if testcases registered
```
e2e_list_testcases {service_name: "<service>"} for each impacted service
```
If not registered: go to Step 4. If registered: skip to Step 5.

### Step 4 — Register testcases
```
For each impacted service:
  e2e_create_testcase {
    name: "<slug>-<service>-test",
    service_list: [service, ...dependent_services],
    parent_service: service,
    branch_ref: "main"
  }
```

### Step 5 — Execute
```
For each registered testcase:
  result = e2e_run_testcase {testcase_id: <id>}
  execution_id = result.id
  
  # Poll every 10s until done
  while true:
    status = e2e_get_execution {id: execution_id}
    if status.status in [Passed, Failed]: break
    sleep 10
```

If orchestrator unhealthy → fallback to `/e2e roast <service>`.

### Step 6 — Generate e2e-report.md
```markdown
# E2E Test Results — {feature_name}

**Status**: PASSED | FAILED | PARTIAL
**Services**: offers-engine, emandate-service
**Duration**: 87s
**Timestamp**: 2026-05-26T14:30:00Z

## Results Summary
| Service | Passed | Failed | Skipped |
|---------|--------|--------|---------|
| offers-engine | 14 | 0 | 1 |

## Failures
(none)

## Coverage
All impacted endpoints validated.
```

### Step 7 — Ingest to Brain
```
e2e_ingest_results {
  feature_slug: slug,
  service: service,
  passed: N, failed: N, skipped: N,
  duration_s: N,
  failures: [...]
}
```
This creates TestResult nodes, links Feature → VALIDATED_BY → TestResult, creates RiskItem nodes for failures.

### Step 8 — Record cost
```bash
python -m brain add-node Signal "e2e:cost:<slug>:<date>" -d '{"phase":"e2e","input_tokens":<N>,"output_tokens":<N>,"cost_usd":<N>,"feature_slug":"<slug>"}'
```

---

## `/e2e write <slug> <service>` — Generate Test Code

1. Load `workspace/features/<slug>/solution.html` — what changed
2. Query `expert_tests` for `service` — existing test patterns
3. Query `expert_functions` for new endpoints in solution
4. Generate Go E2E tests following existing patterns:

```go
// Pattern from expert_tests for {service}
func Test{FeatureName}(t *testing.T) {
    suite.Run(t, new({FeatureName}TestSuite))
}

type {FeatureName}TestSuite struct {
    suite.Suite
    // deps from solution context
}

func (s *{FeatureName}TestSuite) TestHappyPath() {
    // derived from solution.html success flow
}

func (s *{FeatureName}TestSuite) TestEdgeCases() {
    // derived from expert_tests patterns + solution risks
}
```

Output: test code ready to add to the service's `e2e/` directory.

---

## `/e2e review <slug>` — Coverage Gap Analysis

1. Extract impacted services from `solution.html`
2. For each service:
   - Load `expert_functions` → all functions
   - Load `expert_tests` → tested functions (via `functions_tested` JSON array)
   - Compute: `untested = all_functions - tested_functions`
3. Filter to functions touched by this feature (mentioned in solution.html)
4. Report:

```
Coverage Gap Report — {slug}

Service: offers-engine
New/changed functions: 8
Tested by existing E2E: 3
GAPS (5 untested):
  - applyInstantDiscount() — critical path, no test
  - validateOfferEligibility() — called by 3 endpoints, no test
  - ...

Recommended test cases:
  1. POST /offers/apply — happy path + expired offer edge case
  2. GET /offers/validate — eligibility check boundary
```

---

## `/e2e roast <service> [env]` — ROAST Runner

Maps service slug to ROAST TestNG groups:

| Service | Groups |
|---------|--------|
| emandate-service | `EMANDATE,MANDATE,RECURRING` |
| offers-engine | `OFFER,OFFERS,INSTANT_DISCOUNT` |
| payments-card | `PAYMENT,CARD` |
| payments-upi | `PAYMENT,UPI` |
| pg-router | `PAYMENT` |
| subscriptions | `SUBSCRIPTION` |
| checkout-service | `CHECKOUT` |

```
e2e_run_roast {
  env: "test",
  mode: "intg",
  include_groups: "<groups for service>",
  timeout_seconds: 600
}
```

Parse stdout for TestNG results (PASS/FAIL counts).

---

## `/e2e report <slug>`

Read and display `workspace/features/<slug>/e2e-report.md`.

Also show:
```bash
python -m brain search "<slug>" --type TestResult
```

---

## ROAST Available Suites (from xmlRunners/)

| Suite | Focus |
|-------|-------|
| paymentsCoreBVT | Core payment BVT |
| subscription | Subscription flows |
| scrooge | Settlement |
| appsChargeAtWillBvtDevstack | Charge-at-will |
| upiCollectIntentTpv | UPI TPV |
| governorUI | Governor UI |

---

## Brain Enrichment Protocol

Every E2E run MUST write to workspace/brain.db:
- `TestResult` node: `{feature_slug, service, passed, failed, duration_s, run_at, status}`
- Edge: `Feature → VALIDATED_BY → TestResult`
- On failure: `Signal → RiskItem` at confidence 0.8
- Update `Feature.data.e2e_status` = "passed" | "failed" | "partial"

Solutioning Phase 2 will pick up TestResult context via `python -m brain context` for future features touching the same services.

---

## Cost Tracking

E2E phase records a Signal node in workspace/brain.db:
- `Signal` node: `{phase:"e2e", input_tokens, output_tokens, cost_usd}`
- Visible at `/api/features/<slug>/costs` in the UI

---

---

## Auto-Code-Gen Integration (NEW)

When E2E tests fail, automatically invoke `/implement` to generate fixes:

### Failure Analysis Pipeline

```
E2E Failure → Analyze failure type → Route to fix

Types:
  assertion_failure  → Logic bug in changed code → /implement fix <slug>
  compilation_error  → Missing imports/types → auto-fix + re-run
  timeout            → Performance issue → flag as RiskItem, don't auto-fix
  env_unavailable    → Infra issue → skip, report
```

### Auto-Fix Protocol

1. Parse test failure output to identify the failing assertion and code path
2. Query Brain for the relevant function and its solution.md specification:
   ```bash
   python -m brain search "<failing_function>" --type Function
   python -m brain context "<failing_function>" -c dev -b 2000
   ```
3. If the failure is a logic bug in newly generated code:
   - Invoke `/implement fix <slug>` to generate a targeted fix
   - Re-run the failing test
   - If it passes: commit the fix, update PR
   - If it still fails: report to user as unresolved
4. Store fix attempt as a Signal node:
   ```bash
   python -m brain add-node Signal "e2e:autofix:<slug>:<test>" \
       -d '{"failure":"<description>","fix_applied":<bool>,"result":"<pass/fail>"}'
   ```

### User Approval Gate

Auto-fixes are NEVER committed without user approval:
- Present the fix diff
- ASK "E2E test <name> failed. Proposed fix: <diff>. Apply?"
- Only commit if user approves

---

## SLIT Test Integration (NEW)

SLIT (Service Level Integration Tests) run alongside E2E tests for Go services.

### SLIT Execution

For each Go service with SLIT tests:
```bash
cd workspace/repos/<service>
go test -tags slit ./... -count=1 -timeout 300s -v
```

### SLIT in E2E Report

Add a SLIT section to e2e-report.md:
```markdown
## SLIT Test Results
| Service | SLIT Tests | Passed | Failed | Duration |
|---------|-----------|--------|--------|----------|
| emandate-service | 8 | 8 | 0 | 15s |
| offers-engine | 5 | 4 | 1 | 12s |
```

### SLIT Generation from E2E Gaps

When E2E review finds untested code paths, generate SLIT tests:
```
Skill("slit-generator-v2", "<untested function signatures + service context>")
```

Output: SLIT test files ready for `workspace/repos/<service>/slit/` directory.

---

## Gatekeeper Integration (NEW)

Before marking E2E as complete, verify merge criteria:

```
Skill("gatekeeper", "<PR URLs + E2E results + SLIT results>")
```

### Merge Criteria Checklist

| Criteria | Source | Required |
|----------|--------|----------|
| E2E tests pass | e2e-report.md | Yes |
| SLIT tests pass | SLIT results | Yes (Go services) |
| Coverage > 80% | Coverage gap analysis | Yes |
| No critical failures | Failure analysis | Yes |
| Deploy checklist complete | Implementation phase | Yes |
| RiskItem mitigations in place | Brain graph | Yes |

If any criteria fail, report to user with specific remediation steps.

---

## Brain Learning Loop (NEW)

Every E2E run enriches the Brain graph for future features:

### What Gets Stored

1. **TestResult nodes**: One per service per run, with pass/fail/skip counts
2. **Signal nodes**: Test patterns discovered, common failure modes
3. **RiskItem updates**: Failed tests → bump RiskItem confidence to 0.9
4. **Feature updates**: Update feature's e2e_status field

### Trend Tracking

```bash
# Query historical E2E results for a service
python -m brain search "e2e:<service>" --type TestResult
```

Track pass/fail trends over time. If a service consistently fails certain test types,
flag it as a reliability concern for future features.

### Cross-Feature Learning

E2E results from one feature inform risk assessment for future features:
- If `offers-engine` ROAST tests failed for feature A, warn when feature B touches offers-engine
- If a specific test pattern consistently catches bugs, weight it higher in generation

---

## Fallback Chain

```
e2e-test-orchestrator (healthy?) → Yes: Twirp API
                                 → No: ROAST Docker
                                       → available? → Yes: docker run
                                                    → No: stub report (env unavailable)
```
