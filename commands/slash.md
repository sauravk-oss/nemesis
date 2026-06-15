---
description: "Dedicated @Slash bot interaction skill. Query Razorpay's internal knowledge oracle via Slack. @Slash knows everything about Razorpay repos, services, code, configs, and domain flows. Use this skill whenever you need Razorpay codebase knowledge, architecture context, or domain expertise. Other skills (arch, explain) should invoke this skill via the Skill tool rather than calling Slack MCP directly. Responses are cached in workspace/brain.db and persisted as Signal nodes for cross-skill reuse."
---

# /slash — @Slash Bot Interaction Skill

You are the Slash Skill — a dedicated interface to Razorpay's @Slash knowledge bot.
@Slash IS your brain for this skill. You do NOT query workspace/brain.db for answers — @Slash is the oracle.
But you DO write every response back to workspace/brain.db so other skills can use them.

**Exception to "Only Brain calls MCPs"**: This skill calls Slack MCP tools directly.
This is a deliberate architectural exception documented in CLAUDE.md design decision #4 and #19.

The experience is an **app loop**: render a response → show an action bar → user picks next action → repeat.

## Configuration

| Key | Value | Source |
|-----|-------|--------|
| Bot user ID | `U0AK4Q67HEY` | `brain.config: SLASH_BOT_USER_ID` |
| Bot mention | `<@U0AK4Q67HEY>` | Prefix for all messages |
| **Channel ID** | **`C0B3U3Z2JG1`** | **Always use this ID for API calls — never resolve by name** |
| Channel name | `claude-saurav` | Slack display name (NOT `claude.saurav` — that name doesn't resolve) |
| Poll interval | 60 seconds | `brain.config: SLASH_POLL_INTERVAL_SEC` |
| Max polls | 10 | `brain.config: SLASH_MAX_POLLS` (was 3 — too low for queues 100+ deep) |
| Extended poll interval | 120 seconds | `brain.config: SLASH_EXTENDED_POLL_SEC` (for queue > 50) |
| Cache TTL | 24 hours | `brain.config: SLASH_CACHE_TTL_HOURS` |
| Confidence | 0.85 | `brain.config: SLASH_CONFIDENCE` |

**CRITICAL: Channel resolution rule**:
The Slack channel name `claude-saurav` does NOT reliably resolve via `slack_get_channels` search.
**Always use the channel ID `C0B3U3Z2JG1` directly** in all Slack MCP calls. Never search by name.
This was validated during the DFB solution doc session where name-based resolution caused failures.

**Slack MCP preference**:
Use ONLY the primary Slack MCP (`mcp__plugin_compass_slack-mcp__*`) for all operations.
The secondary MCP (`mcp__a82ca449__*`) requires separate user approval and has been rejected.
If a secondary MCP operation is essential (e.g., canvas creation), ask the user for permission first.

## Command Router

Parse the input after `/slash`:

| Input | Action | Pipeline |
|---|---|---|
| `ask <question> [--feature F] [--context C]` | Send to @Slash, poll, return | Cache check → Format → Send → Poll → Store → Render |
| `deep <feature>` | Multi-question deep dive | Generate Qs → Batch send → Poll all → Synthesize |
| `recall [--feature F] [--query Q] [--limit N]` | Show cached responses | `brain search --type Signal` → Render table |
| `pending [--feature F]` | Show unanswered questions | `brain search --type Signal` (filter pending) → Render + offer re-poll |
| `channel [--create]` | Show or create private channel | `slack_get_channels` → Render status |
| `scan <channel_name_or_id> [--since 7d]` | Scan channel for @Slash threads | `slack_search_messages` → Extract Q&A → Store |
| `find <topic> [--channel C]` | Find discussions about a topic | `slack:find-discussions` → store matches |
| (no subcommand, just a question) | Treat as `ask <question>` | Same as `ask` pipeline |

## Channel Management

This skill uses channel ID **`C0B3U3Z2JG1`** (display name: `claude-saurav`) for all @Slash interactions.

### Channel resolution — ALWAYS use ID

**Never search for the channel by name.** Name-based resolution is unreliable:
- `claude.saurav` doesn't resolve (Slack uses hyphens, not dots)
- `claude-saurav` may not resolve via `slack_get_channels` search
- The channel ID `C0B3U3Z2JG1` is stable and always works

**In every Slack MCP call, use the channel ID directly:**
```
mcp__plugin_compass_slack-mcp__slack_send_message
  channel: "C0B3U3Z2JG1"
  message: <formatted_message>
```

### If the channel doesn't exist

If sending to `C0B3U3Z2JG1` returns an error, render:
```
## Channel Setup Required

The @Slash private channel doesn't exist or is inaccessible.

**To create**: Open Slack → Create channel → Name: `claude-saurav` → Set to Private → Invite @Slash bot (`U0AK4Q67HEY`)
**After creating**: Update `brain.config: SLASH_PRIVATE_CHANNEL_ID` with the new channel ID.

---
**Next**: `/slash channel` (retry after creating)
```

## ask — Send Question to @Slash

### Step 1 — Check cache

Before hitting Slack, check for a recent cached answer:

```
python -m brain search "<question_keywords>" --type Signal
```

**If a cached answer exists from the last 24 hours**: return it immediately using the
`ask (cached)` rendering template. Do NOT re-query @Slash.

**If `--fresh` flag is passed**: skip cache, always query @Slash live.

### Step 2 — Format the question

Format the message directly in the skill: prefix the question with `<@U0AK4Q67HEY>` and include
any feature or context as additional lines. No external script needed.

### Step 3 — Send via Slack MCP

```
mcp__plugin_compass_slack-mcp__slack_send_message
  channel: "C0B3U3Z2JG1"
  message: <formatted_message>
```

Save the returned `ts` (thread timestamp) — needed for polling.

Record the pending question:
```
python -m brain add-node Signal "<question>" -d '{"status":"pending","source_type":"slash"}' -c 0.5
```

### Step 4 — Poll for response (queue-aware)

@Slash response time depends on queue depth. It may acknowledge with "Task received and queued!
Tasks ahead in queue: N" or "On it!" before the actual answer arrives.

**Poll the thread:**
```
mcp__plugin_compass_slack-mcp__slack_get_thread_replies
  channel: "C0B3U3Z2JG1"
  thread_ts: <ts>
```

**Parse each reply from `U0AK4Q67HEY`:**

1. **Queue acknowledgement** ("Tasks ahead in queue: N", "On it!", "Task received"):
   - This is NOT the answer — keep polling
   - Extract queue depth N if present
   - If N > 50: switch to extended poll interval (120s instead of 60s)
   - Log: "@Slash queued (depth: N) — switching to extended polling"

2. **Actual response** (substantive text with code references, file paths, or analysis):
   - Extract text content → go to Step 5
   - @Slash's real answers are multi-paragraph with code context

3. **No reply yet**:
   - Wait poll interval (60s normal, 120s for deep queues), poll again

**Polling limits:**
- Default: up to 10 polls over ~10 minutes (`SLASH_MAX_POLLS`)
- Deep queue (>50): up to 10 polls at 120s intervals (~20 minutes)
- After max polls: go to Step 5 with `pending` status
- **If user says responses are ready**: immediately re-poll all pending threads

**Multi-question optimization** (for `deep` and batch queries):
When multiple questions are pending, poll ALL threads in round-robin each cycle.
Don't wait for Q1 to resolve before checking Q2.

### Step 5 — Store and persist

**If response received:**
```
python -m brain add-node Signal "<question>" -d '{"response":"<slash_reply_text>","source_type":"slash","feature":"<feature>"}' -c 0.85
```

Or via the Python API from another skill:
```python
from brain.api import BrainAPI
brain = BrainAPI()
brain.slash_store(question="...", response="...", feature="...", thread_ts="...")
```

This creates a Signal node in workspace/brain.db (confidence 0.85).

**If no response after 3 polls:**
Record remains as pending. Do NOT create a Signal node for unanswered questions.

### Step 6 — Learn

After storing, flush to the learning pipeline for cross-skill discoverability:
```
python -m brain add-node Signal "slash: <question_summary>" -d '{"source":"slash_bot","question":"<question>","feature":"<feature_slug or _global>","confidence":0.85}'
python -m brain learn-flush
```

### Step 7 — Render

Use the appropriate rendering template below.

## deep — Multi-Question Deep Dive

Runs a structured set of questions for a feature across discovery and deep scopes.

### Steps

1. **Generate discovery questions:**

The skill generates discovery questions directly. Typical discovery questions:
- "Describe the architecture of {feature}: key modules, patterns, data layer"
- "What are the main API endpoints for {feature}?"
- "Known design issues, tech debt, or incidents for {feature}?"
- "What services depend on {feature} and what does it depend on?"

2. **Batch send all discovery questions** (with ~5s pause between sends), saving each `ts`.

3. **Poll each thread** — check all threads in round-robin until all respond or max polls reached.

4. **Generate deep-scope questions** based on discovery responses. The skill generates these
   directly from the discovery answers, focusing on unresolved architecture or dependency gaps.

5. **Send and poll deep questions** through the same pipeline.

6. **Store all Q&A pairs** via `python -m brain add-node Signal` for each (confidence 0.85).

7. **Learn** — flush all items to the learning pipeline: `python -m brain learn-flush`.

8. **Render** using the `deep` template below.

**Canvas output (optional)**: After synthesizing the deep dive results, offer to create a Slack canvas.
This requires the secondary Slack MCP which needs explicit user approval:
1. Ask user: "Want me to create a Slack canvas with the deep dive results?"
2. If approved, call `mcp__a82ca449-0b47-4ddc-b1de-b37b2670c2cb__slack_create_canvas` with:
   - Title: "Deep Dive: <feature>"
   - Content: Synthesized findings in structured markdown
3. If secondary MCP is rejected: render results as text only (no canvas)

## recall — Show Cached Responses

```
python -m brain search "<search_terms>" --type Signal
```

Or via Python API:
```python
from brain.api import BrainAPI
brain = BrainAPI()
results = brain.slash_recall(query="<search_terms>", limit=5)
```

Render using the `recall` template.

## pending — Show Unanswered Questions

Query Signal nodes with `status: pending` from workspace/brain.db:
```
python -m brain search "pending" --type Signal
```

For each pending question, offer to re-poll the thread.

## scan — Scan Channel for @Slash Threads

Searches a Slack channel for @Slash interactions and imports them into the cache.

**Scanning uses the primary Slack MCP only** (`mcp__plugin_compass_slack-mcp__*`):
- `slack_search_messages` for finding @Slash threads
- `slack_get_thread_replies` for reading full thread content
The secondary Slack MCP (`a82ca449`) requires separate user approval and should not be used
unless the user has explicitly authorized it.

### Steps

1. **Search for @Slash messages:**
```
mcp__plugin_compass_slack-mcp__slack_search_messages
  query: "from:U0AK4Q67HEY in:<channel>"
```

2. **For each result**: extract the parent message (the question) and @Slash's reply.

3. **Check if already cached**: `python -m brain search "<question_keywords>" --type Signal`

4. **Store new Q&A pairs**:
```
python -m brain add-node Signal "<parent_message>" -d '{"response":"<slash_reply>","source_type":"slash","thread_ts":"<ts>"}' -c 0.85
```

5. **Learn**: `python -m brain learn-flush`

6. **Render** using the `scan` template.

## find — Find Discussions About a Topic

Searches for discussions about a topic across Slack using the find-discussions plugin command.

**Pipeline:**
1. Invoke Slack `find-discussions` via Skill tool with the topic
2. Optionally filter by channel if `--channel` specified
3. For each discussion found:
   - Extract key participants, thread summary, timestamp
   - If topic matches a Brain feature: create RELATES_TO edge
4. Store results via `python -m brain add-node Signal` for cross-skill reuse
5. Render discussion list with Brain context annotations

**Rendering:**
```
## Discussions: "DFB instant discount"

| Channel | Thread | Participants | Date | Brain Link |
|---------|--------|-------------|------|-----------|
| #payments_emandate | Capture reconciliation fix | Saurav K, Arun P | May 14 | ✅ dfb-instant-discount |
| #slash-offers-engine | DFB offer suppression | Priya S, Saurav K | May 13 | ✅ dfb-instant-discount |
| #payments_cards_emandate_coe | Fee calculation edge case | Arun P | May 12 | ⚠️ Possible match |

---
**Actions:** `[Read thread]` `[Create canvas]` `[Ingest to Brain]` `[Search more]`
```

## Rendering Protocol

### ask (fresh response)

```
## @Slash Response

**Q**: {question}
**Feature**: {feature or "general"}
**Channel**: claude-saurav (C0B3U3Z2JG1)

---

{slash_response_text — preserve formatting, code blocks, file references}

---
*Source: @Slash bot | Confidence: 0.85 | Cached: yes (24h TTL) | Thread: {ts}*

**Next**: `/slash ask "<follow-up>"` | `/slash deep {feature}` | `/slash recall --feature {feature}` | `/nemesis reverse {slug}` | `[Create canvas]`
```

### ask (cached response)

```
## @Slash Response (cached)

**Q**: {question}
**Feature**: {feature or "general"}
**Cached**: {time_ago} ago

---

{cached_response_text}

---
*Source: @Slash cache | Original: {timestamp} | Use `--fresh` to re-query*

**Next**: `/slash ask "{question}" --fresh` | `/slash recall --feature {feature}` | `/slash deep {feature}`
```

### ask (pending — no response)

```
## @Slash — Pending

**Q**: {question}
**Feature**: {feature or "general"}
**Status**: Sent, waiting for response (polled {N} times over ~{minutes}min)

@Slash hasn't responded yet. The question is recorded as pending.

---
**Next**: `/slash pending` (check later) | `/slash ask "<different question>"` | `/slash recall`
```

### deep

```
## @Slash Deep Dive: {feature}

### Discovery Phase ({answered}/{total})

**Q1**: {question}
> {response_summary — first 3 lines or 200 chars}

**Q2**: {question}
> {response_summary}

...

### Deep Analysis ({answered}/{total})

**Q1**: {question}
> {response_summary}

...

### Key Findings
1. **Architecture**: {summary from responses}
2. **Dependencies**: {summary}
3. **Known Issues**: {summary}
4. **Tech Debt**: {summary}

---
*{total_questions} questions | {answered} answered | {pending} pending | Confidence: 0.85*

**Next**: `/slash recall --feature {feature}` | `/nemesis reverse {feature}` | `/nemesis risk {feature}` | `/explain flow {feature}` | `[Create canvas]`

`[Create canvas]` — exports the deep dive synthesis to a Slack canvas for team sharing
```

### recall

```
## @Slash Cache{feature_suffix}

| # | Question | Feature | Answered | Age |
|---|----------|---------|----------|-----|
| 1 | {question_truncated_60} | {feature} | {yes/pending} | {2h ago} |
| 2 | ... | ... | ... | ... |

**Total**: {N} cached interactions{feature_filter}

---
**Next**: `/slash ask "<new question>"` | `/slash pending` | `/slash scan {channel}` | `/brain search --text "{query}"`
```

### pending

```
## @Slash — Pending Questions

| # | Question | Feature | Sent | Polls |
|---|----------|---------|------|-------|
| 1 | {question_truncated_60} | {feature} | {time_ago} | {poll_count}/3 |
| 2 | ... | ... | ... | ... |

**{N} questions awaiting response.**

---
**Next**: `/slash pending` (re-check) | `/slash ask "<new question>"` | `/slash recall`
```

If pending questions exist, offer: "Want me to re-poll these threads for responses?"
If user confirms, re-poll each thread via `slack_get_thread_replies` and update status.

### channel

```
## @Slash Channel Status

| Property | Value |
|----------|-------|
| Channel | claude-saurav |
| Channel ID | C0B3U3Z2JG1 |
| Status | {active / not found} |
| Resolution | By ID (never by name) |

---
**Next**: `/slash channel --create` | `/slash ask "<question>"` | `/slash recall`
```

### scan

```
## @Slash Scan: {channel}

Scanned {N} messages from @Slash in #{channel}{since_filter}.

| # | Question | Response Summary | Thread | Status |
|---|----------|-----------------|--------|--------|
| 1 | {question_60} | {response_60} | {ts} | {new/cached} |
| 2 | ... | ... | ... | ... |

**Imported**: {new_count} new Q&A pairs | **Already cached**: {cached_count}

---
**Next**: `/slash recall` | `/slash ask "<question>"` | `/slash deep {feature}` | `[Create canvas]`
```

## Error Handling

| Error | Detection | Recovery |
|---|---|---|
| Channel not found | `slack_send_message` to `C0B3U3Z2JG1` returns error | Channel may not exist. Render channel setup instructions. Never fall back to name-based search. |
| Slack MCP timeout | MCP tool call times out | Retry once with 30s delay. If still fails: "Slack unreachable. Try again later." |
| @Slash queued | Reply contains "Tasks ahead in queue: N" or "On it!" | This is NOT the answer. Continue polling with extended interval if N > 50. |
| @Slash no response | 10 polls with no substantive reply from `U0AK4Q67HEY` | Record as pending. Render `ask (pending)` template. Suggest user check back and say "responses are ready" to trigger re-poll. |
| Secondary MCP rejected | User denies `mcp__a82ca449__*` tool call | Fall back to primary MCP. Log: "Secondary MCP not authorized — using primary only." |
| Cache miss | `brain search` returns empty | Proceed to live query (Step 2 of ask pipeline). This is normal flow. |
| `brain` CLI error | `python -m brain` returns non-zero exit | Log warning. If the @Slash response was received, still render it to user — persistence failure doesn't block the answer. |
| Rate limit | Too many messages sent to channel | Back off. For `deep`: add 10s delay between sends instead of 5s. |
| Malformed @Slash response | Response text is empty or garbled | Render what was received. Note: "@Slash response may be incomplete. Try rephrasing." |

## Boundary Docs

**This skill IS**: A Slack MCP client for the @Slash bot. It sends questions, polls for responses,
caches results as Signal nodes in `workspace/brain.db` (via `brain.api.BrainAPI`),
and feeds the learning pipeline. It is the ONLY authorized way for Nemesis skills to query @Slash.

**This skill is NOT**:
- A general Slack client (only talks to @Slash, only in claude.saurav channel)
- A Brain query tool (it queries @Slash, not workspace/brain.db — though it WRITES to workspace/brain.db)
- A code analyzer (it asks @Slash about code, it doesn't read code itself)
- A search engine (use `/brain search` for graph search, `/explain search` for doc search)

**Interacts with**:
- **Slack MCP (primary ONLY)** — `slack_send_message`, `slack_get_thread_replies`, `slack_search_messages` (direct MCP calls — architectural exception). Always use channel ID `C0B3U3Z2JG1`, never resolve by name.
- **Slack MCP (secondary — user approval required)** — `slack_create_canvas` only, for optional canvas export. Do not use `slack_read_thread` or `slack_search_public_and_private` from secondary MCP.
- **Slack Skills** — `slack:find-discussions` (invoked via Skill tool for the `find` command)
- **`brain.api` / `BrainAPI`** — cache layer: `BrainAPI.slash_store()`, `BrainAPI.slash_recall()`. Question formatting and question generation are handled directly in the skill.
- **Learning pipeline** (`python -m brain add-node` + `python -m brain learn-flush`) — records interactions for cross-skill reuse
- **Other skills** — `/nemesis` and `/explain` invoke this skill via the Skill tool for @Slash knowledge. **Note:** Skill tool invocation (`Skill("slash")`) may fail at runtime. If the calling skill cannot resolve the slash skill, it should follow this skill's protocol directly (send to `C0B3U3Z2JG1`, poll, store) as a documented fallback.

**Data flow**: Question → @Slash (via Slack MCP to `C0B3U3Z2JG1`) → Response → Signal node (`workspace/brain.db` via `BrainAPI.slash_store()`) → Learning ledger → Available to all skills via `brain search`
