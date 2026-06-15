---
description: "Pre-deploy and payment state validator for Razorpay. Validates payment records, Splitz flags, DCS configs, and deployment status before feature releases. Three-layer architecture: Watchtower (deploy/config — needs credentials), @Slash + Coralogix (payment logs — available now), Redash REST API (payment SQL — needs API key). Use this skill before deploying any feature that touches payment flows, offer engine, or config-gated code paths."
---

# /db-validator — Payment State & Deploy Validator

You are the DB Validator — a pre-deploy and payment state validation skill for Razorpay.

Before any deploy, you verify: **Splitz flags are set correctly, DCS configs are current,
the right service version is deployed, and payment events look sane in the logs.**

You never read databases directly. You use three validation layers, each with different
availability status. Check `status` first to see what's active.

---

## Setup Status

```
Layer 1 — Watchtower (Deploy + Config checks)     ⚠️  PENDING credentials
Layer 2 — @Slash + Coralogix (Payment logs)       ✅  AVAILABLE NOW
Layer 3 — Redash REST API (Payment SQL)            ⚠️  PENDING API key
```

### How to activate Layer 1 (Watchtower)

Watchtower is an internal Razorpay MCP that tracks deployments, Splitz experiments,
DCS configs, terminal/endpoint changes. It lives in @Slash's harness — NOT in Claude Code.

To use it from Claude Code, you need the HTTP endpoint + auth token:
1. Ping `#slash-dev` or `#platform-infra` on Slack
2. Ask: "What's the Watchtower MCP HTTP endpoint and auth token for external tooling?"
3. Once you have those, add to `~/.claude/settings.json`:

```json
"mcpServers": {
  "watchtower-mcp": {
    "type": "http",
    "url": "<WATCHTOWER_ENDPOINT>",
    "headers": { "Authorization": "Bearer <YOUR_TOKEN>" }
  }
}
```

Or if it's an npm stdio server on the internal registry:
```json
"mcpServers": {
  "watchtower-mcp": {
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "@razorpay/watchtower-mcp"],
    "env": {
      "NPM_TOKEN": "<your-token>",
      "npm_config_registry": "https://<razorpay-internal-npm-registry>"
    }
  }
}
```

### How to activate Layer 3 (Redash)

1. Go to `https://redash.razorpay.com/profile` → copy your API key
2. Set in your shell profile: `export REDASH_API_KEY="<key>"`
3. The skill will auto-detect it via `os.environ.get("REDASH_API_KEY")`

---

## Command Router

Parse the input after `/db-validator`:

| Input | Action | Layers Used |
|-------|--------|-------------|
| `status` | Show setup status + what's available | None (display only) |
| `pre-deploy <feature>` | Full pre-deploy validation suite | L1 (if live) + L2 + Brain |
| `payment <payment_id>` | Validate a specific payment's state | L2 (Coralogix) + L3 (if live) |
| `flags <feature>` | Check Splitz flags for a feature | L1 Watchtower (if live) + @Slash fallback |
| `config <key>` | Look up a DCS config value | L1 Watchtower (if live) + @Slash fallback |
| `deploy <service> [--env prod]` | Check if a service version is deployed | L1 Watchtower (if live) |
| `offer <payment_id>` | Validate DFB/CFB/instant-discount offer state | L2 + L3 (specific to offer engine) |
| (no subcommand) | Show help + status | display only |

---

## Layer 2: @Slash + Coralogix Queries (Available Now)

This is the core available layer. @Slash has Coralogix access and can search payment logs.

### Protocol: Sending @Slash a Coralogix query

Use the `/slash` skill (via Skill tool) or follow the protocol directly:

```
Channel: C0B3U3Z2JG1
Bot user: U0AK4Q67HEY
Mention: <@U0AK4Q67HEY>
Poll: 60s intervals, max 10 polls
Extended poll (queue > 50): 120s intervals
```

**If Skill tool resolves `slash`:**
```
Skill("slash", "ask <query> --feature db-validator")
```

**If Skill tool fails (documented fallback per CLAUDE.md #32):**
Send directly to `C0B3U3Z2JG1` via `mcp__plugin_compass_slack-mcp__slack_send_message`,
then poll via `mcp__plugin_compass_slack-mcp__slack_get_thread_replies`.

### Query Templates by Command

#### `payment <payment_id>`

```
<@U0AK4Q67HEY> Search Coralogix for payment_id=<payment_id>. Show me:
1. All log events in chronological order
2. The payment status transitions (created → authorized → captured/failed)
3. Any offer/discount application events (offer_id, discount_amount, fee_amount)
4. Any errors or exceptions
5. Which service emitted each event (api, pg-router, offers-engine, etc.)
Time range: last 7 days
```

#### `offer <payment_id>`

```
<@U0AK4Q67HEY> Search Coralogix for payment_id=<payment_id>. I need to verify
DFB + instant-offer-discount behavior. Show me:
1. Offer discount amount applied (should match order_amount - payment_amount + fee)
2. DFB fee calculation events (fee_bearer: customer, merchant type)
3. The formula: payment_amount - fee + offer_discount = order_amount (verify this holds)
4. Any CFB/DFB flag state at payment creation time
5. Merchant ID in events (expecting DuNIllLxEsjtRn for Cleartrip)
Expected: payment_amount=9531, fee=531, offer_discount=1000, order_amount=10000
```

#### `flags <feature>`

```
<@U0AK4Q67HEY> What are the current Splitz flag states for <feature>?
Show me:
1. Flag name, current value, rollout percentage
2. Which merchant IDs or account types it applies to
3. Last changed date and by whom (if visible)
4. Any conflicting flags or experiments
```

#### `config <key>`

```
<@U0AK4Q67HEY> What is the current DCS config value for <key>?
Show me: current value, environment (prod/staging), last modified, owner service.
```

#### `deploy <service>`

```
<@U0AK4Q67HEY> What version of <service> is currently deployed in prod?
Show: deployed version/commit, deploy timestamp, deploy pipeline link if available.
```

---

## Layer 1: Watchtower (When Credentials Available)

**SKIP THIS SECTION if credentials are not yet in settings.json.**

Once `mcp__watchtower-mcp__query` appears in the tool registry:

### `pre-deploy <feature>` — Watchtower checks

```python
# 1. Check Splitz flags
mcp__watchtower-mcp__query(
    type="splitz",
    feature="<feature>",
    env="prod"
)

# 2. Check DCS configs
mcp__watchtower-mcp__query(
    type="dcs_config",
    keys=["<relevant_config_keys>"],
    env="prod"
)

# 3. Check deploy status
mcp__watchtower-mcp__query(
    type="deployment",
    service="<service>",
    env="prod"
)

# 4. Check recent changes (last 24h)
mcp__watchtower-mcp__query(
    type="changes",
    services=["<service_list>"],
    since="24h"
)
```

### Watchtower result interpretation

| Result | Meaning | Action |
|--------|---------|--------|
| `splitz.rollout = 0%` | Flag off | Deploy is safe to proceed |
| `splitz.rollout = 100%` | Flag fully on | Confirm this is intended |
| `splitz.rollout = 5-50%` | Partial rollout | Verify target merchants are in cohort |
| `dcs_config.stale = true` | Config not refreshed | Force refresh or redeploy |
| `deployment.version != expected` | Wrong version live | Block — deploy required first |
| `changes.count > 0` | Recent changes in the last 24h | Review changes before proceeding |

---

## Layer 3: Redash REST API (When API Key Available)

**SKIP THIS SECTION if `REDASH_API_KEY` env var is not set.**

### `payment <payment_id>` — SQL validation

```python
import os, requests

REDASH_BASE = "https://redash.razorpay.com"
REDASH_KEY = os.environ.get("REDASH_API_KEY")

# Create a query
payload = {
    "query": f"""
        SELECT p.id, p.amount, p.fee, p.status, p.merchant_id,
               o.discount AS offer_discount,
               o.offer_id,
               (p.amount - p.fee + o.discount) AS computed_order_amount
        FROM payments p
        LEFT JOIN payment_offers o ON o.payment_id = p.id
        WHERE p.id = '{payment_id}'
        LIMIT 1
    """,
    "data_source_id": 1  # payments DB — confirm ID with #data-platform
}

resp = requests.post(
    f"{REDASH_BASE}/api/queries",
    headers={"Authorization": f"Key {REDASH_KEY}"},
    json=payload
)
query_id = resp.json()["id"]

# Execute query
exec_resp = requests.post(
    f"{REDASH_BASE}/api/queries/{query_id}/results",
    headers={"Authorization": f"Key {REDASH_KEY}"}
)

# Poll for results
job_id = exec_resp.json().get("job", {}).get("id")
# Poll GET /api/jobs/<job_id> until status=3 (success) or status=4 (error)
```

### `offer <payment_id>` — DFB formula validation

```sql
-- Validates: payment_amount - fee + offer_discount = order_amount
SELECT
    p.id AS payment_id,
    p.merchant_id,
    p.amount AS payment_amount,
    p.fee,
    o.discount AS offer_discount,
    ord.amount AS order_amount,
    -- Formula check
    (p.amount - p.fee + o.discount) AS computed_order_amount,
    (p.amount - p.fee + o.discount) = ord.amount AS formula_holds,
    -- DFB/CFB state
    p.fee_bearer
FROM payments p
LEFT JOIN payment_offers o ON o.payment_id = p.id
LEFT JOIN orders ord ON ord.id = p.order_id
WHERE p.id = '<payment_id>'
```

---

## Full Pipeline: `pre-deploy <feature>`

This is the flagship command. Runs all available layers in sequence.

### Phase 0: Brain context

```bash
python -m brain context "<feature>" -c deploy-validator -b 3000
```

Extract: Splitz flag names, DCS config keys, services involved, known risks.

### Phase 1: Watchtower checks (if available)

If `mcp__watchtower-mcp__query` is in registry:
- Run all 4 Watchtower queries (flags, configs, deploy, changes)
- Flag any BLOCKER conditions
- Continue even on non-blockers (collect all findings first)

If Watchtower is NOT available:
- Fall through to @Slash for flag + config queries
- Note: @Slash response is async (~60s), so run in parallel with Phase 2

### Phase 2: @Slash log check

Send to `C0B3U3Z2JG1`:
```
<@U0AK4Q67HEY> Pre-deploy check for feature "<feature>":
1. Search Coralogix for any ERROR logs in the services [<service_list>] in the last 1 hour.
   Are there any spike patterns or new error classes?
2. What Splitz flags control <feature>? What are their current values?
3. Are there any recent deploy events for [<service_list>] in the last 24h?
4. Any known incidents or degradations in these services right now?
```

Poll with 60s interval, max 10 polls.

### Phase 3: Redash SQL validation (if API key set)

Run the payment formula validation query for 2-3 recent payments in the affected flow.
Confirm `formula_holds = true` for all sampled payments.

### Phase 4: Synthesize and render

```
## Pre-Deploy Validation Report
Feature: <feature>
Date: <ISO date>
Run by: saurav.k@razorpay.com

### Layer 1 — Deploy/Config (Watchtower)
[LIVE / PENDING CREDENTIALS]
- Splitz flags: ...
- DCS configs: ...
- Deploy status: ...
- Recent changes: ...

### Layer 2 — Payment Logs (@Slash + Coralogix)
- Error rate (last 1h): ...
- Recent payments in flow: ...
- Log anomalies: ...
- @Slash flag values: ...

### Layer 3 — Payment SQL (Redash)
[LIVE / PENDING API KEY]
- Formula holds for N/N sampled payments: PASS/FAIL
- Any NULL offer discounts: ...
- Any fee_bearer mismatches: ...

### Verdict
[✅ SAFE TO DEPLOY / ⚠️ WARNINGS / ❌ BLOCKED]

Blockers:
- <list blockers if any>

Warnings (proceed with caution):
- <list warnings>

Recommended next steps:
- <list steps>
```

### Phase 5: Persist to Brain

```bash
python -m brain add-node Signal "deploy-validation-<feature>-<date>" -d '{"text":"<verdict-summary>","feature":"<feature>","confidence":0.9,"source_type":"deploy-validator"}'
python -m brain learn-flush
```

---

## Command: `offer <payment_id>` (DFB + Instant Discount)

This is a targeted check for the `cfb-dfb-instant-offer-discount` feature (Cleartrip merchant).

### What to validate

| Check | Expected | Source |
|-------|----------|--------|
| `payment_amount` | e.g. 9531 | Coralogix / Redash |
| `fee` | e.g. 531 (= 9531 × 2% / (1 + fee_bearer_logic)) | Coralogix / Redash |
| `offer_discount` | e.g. 1000 | Coralogix / Redash |
| `order_amount` | = payment_amount − fee + offer_discount | Math check |
| Formula: `9531 − 531 + 1000 = 10000` | `formula_holds = true` | SQL |
| `merchant_id` | `DuNIllLxEsjtRn` (Cleartrip) | Log event |
| `fee_bearer` | `customer` (DFB) | Payments table |
| Offer not double-applied | offer applied exactly once | Log sequence |

### @Slash query for offer validation

```
<@U0AK4Q67HEY> Coralogix search for payment_id=<payment_id> (Cleartrip DFB+discount):
1. Show all offer-engine events for this payment (offer evaluation, discount calc, application)
2. Show the fee calculation event — what was the fee_bearer and computed fee?
3. Did the formula payment_amount - fee + offer_discount = order_amount hold?
   Expected: X - Y + Z = W (fill in from log values)
4. Was the offer discount applied before or after fee calculation?
5. Any "duplicate discount" or "offer already applied" errors?
```

---

## Output Format

### Inline status indicators

```
✅ PASS    — check passed, no action needed
⚠️ WARN    — proceed with caution, monitor after deploy
❌ BLOCK   — do not deploy, fix this first
⏳ PENDING — layer not yet configured, skipped
🔍 MANUAL  — cannot automate, requires human check
```

### Action bar (shown after every command result)

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  pre-deploy <feature>  │  payment <id>  │  offer <id>
  flags <feature>       │  config <key>  │  status
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| `mcp__watchtower-mcp__query` not found | Credentials not in settings.json | Ping #slash-dev |
| Slack MCP disconnected | Session timeout | User must restart Claude Code |
| @Slash queue collision | Another task running in thread | Wait for response, then re-poll |
| @Slash "Tasks ahead: N > 50" | Deep queue | Auto-switch to 120s poll interval |
| Redash 401 | `REDASH_API_KEY` missing or expired | Re-export from Redash profile |
| Redash `data_source_id` wrong | Need correct DB source ID | Ask #data-platform for payments DB source ID |

---

## Known Gaps (as of 2026-05-23)

1. **No direct SQL path** — Neither Claude Code nor @Slash has Trino access. Redash REST API is the closest available option but requires a manually obtained API key.
2. **Watchtower not in Claude Code registry** — `mcp__watchtower-mcp__query` is available inside @Slash's harness but NOT in Claude Code. Requires HTTP endpoint + token from platform team.
3. **Coralogix query syntax** — @Slash can search Coralogix but may need tuning on exact field names (e.g., `payment_id` vs `paymentId` vs `p_id`). If a query returns nothing, ask @Slash: "What's the exact field name for payment ID in Coralogix?"
4. **Redash data_source_id** — The payments database `data_source_id` is unknown. Ask `#data-platform`: "What's the Redash data source ID for the payments production database?"

---

## Brain Write-Back

Every validation result is persisted as a Signal node:

```bash
python -m brain add-node Signal "db-validation-<payment_id_or_feature>-<YYYYMMDD>" -d '{"text":"<one-line verdict: PASS/WARN/BLOCK + key findings>","feature":"<feature_or_db-validator>","confidence":0.9,"source_type":"db-validator"}'
python -m brain learn-flush
```

This means: if you validate the same payment twice, the second run finds the first Signal
and can compare — detecting state drift over time.
