# Nemesis v2 — Setup & Usage Guide

## What is this?

Nemesis v2 is a **4-agent AI system** built on Claude Code. It has a single SQLite knowledge graph
(`rubick.db`) that all agents share. Think of it as your second brain — it ingests signals from
Slack, Gmail, Calendar, GitHub, and Drive, then lets you query that knowledge through natural
language commands.

```
                    ┌──────────────┐
                    │  Rubick  │  ← Memory agent (this repo)
                    │  rubick.db     │
                    └──────┬───────┘
                           │
                  ┌────────┼────────┐
                  ▼        ▼        ▼
               Planner  Nemesis      Developer
               (/plan)  (/nemesis)     (next)
```

**Agents built so far:**
- **Brain** (`/brain`) — Memory + knowledge graph. Ingests from 6 platforms. Only agent that calls external MCPs.
- **Planner** (`/plan`) — Interactive daily planner, task manager, focus sessions, weekly reviews.
- **Nemesis** (`/nemesis`) — Intelligent orchestrator. 3-phase lifecycle: Ideation (overview) → Solutioning (solution + risk) → Tech Spec (document). Supports Slack/doc ingestion, As-Is/To-Be analysis, full pipeline mode, and auto-persists all discoveries back to Rubick.

---

## Quick Start (New Team Member)

1. **Clone repo**: `git clone <url> && cd nemesis_v2`
2. **Download brain**: Get `nemesis-brain.tar.gz` from the team → place in `workspace/`
3. **Import**: `python3 scripts/rubick_graph.py import-shareable workspace/nemesis-brain.tar.gz`
4. **Clone repos** (optional, for deep code context): `python3 scripts/rubick_heroes.py clone-all workspace/rubick.db`
5. **Start UI**: `python3 scripts/rubick_ui.py` → open http://localhost:5555
6. **Or CLI**: Type `/nemesis` in Claude Code

### Export Your Brain (for sharing)
```bash
python3 scripts/rubick_graph.py export-shareable workspace/rubick.db --output nemesis-brain.tar.gz
```
This creates a ~280MB compressed archive with all service knowledge, experts, and code bodies. Personal data (chat sessions, signals, emails) is stripped.

---

## Prerequisites

| Tool | Required | Check |
|------|----------|-------|
| **Claude Code** | yes | You're reading this, so you have it |
| **Python 3.9+** | yes | `python3 --version` |
| **GitHub CLI** | yes | `gh auth status` (must be logged in to razorpay org) |
| **SQLite3** | yes | Ships with Python, nothing to install |
| **Claude Code MCP connectors** | yes | Slack, Gmail, Calendar, Drive must be connected |

**No pip installs needed.** All scripts use Python stdlib only (sqlite3, json, re, os, sys, datetime, pathlib, logging, tempfile, argparse, textwrap, hashlib, collections).

---

## Setup (5 minutes)

### Step 1: Clone or copy the repo

```bash
# If you have a git remote:
git clone <your-remote> ~/Projects/Agents/nemesis_v2
cd ~/Projects/Agents/nemesis_v2

# Or if copying from an existing machine:
cp -r /path/to/nemesis_v2 ~/Projects/Agents/nemesis_v2
```

### Step 2: Verify the directory structure

```
nemesis_v2/
├── CLAUDE.md                   # Agent config (Claude reads this automatically)
├── SKILL.md                    # Brain skill orchestrator
├── SETUP.md                    # This file
├── schemas/
│   └── graph-schema.md         # v3.0: 30 node types, 47 edge types
├── scripts/
│   ├── brain_config.py         # Central configuration
│   ├── rubick_graph.py           # Core graph engine + planner
│   ├── rubick_context.py         # Context retrieval with budget
│   ├── rubick_ingest.py          # Universal ingestion pipeline
│   ├── rubick_planner.py         # Interactive planner engine
│   └── ast_extractor.py        # AST parsing (Go, Python, JS/TS)
├── agents/
│   ├── brain-ingest-agent.md   # Parallel ingestion sub-agent
│   └── nemesis-agent.md        # Nemesis analysis sub-agent
├── commands/
│   ├── brain.md                # /brain command router
│   ├── plan.md                 # /plan command router
│   └── nemesis.md              # /nemesis command router
├── .claude-plugin/
│   └── plugin.json             # Plugin registration
└── workspace/
    ├── rubick.db                 # Knowledge graph (created on init)
    ├── features/               # Per-feature working directories
    └── repos/                  # Clone-on-demand repos
```

### Step 3: Initialize the database

Open Claude Code in the nemesis_v2 directory and run:

```
/brain init
```

This creates `workspace/rubick.db` with schema v3.0 and starts a 3-month historical bootstrap:
- Fetches all Slack messages from seed channels (last 90 days)
- Fetches Gmail threads (last 90 days)
- Fetches Calendar events (90 days back + 30 days ahead)
- Discovers all razorpay GitHub repos (~1500)
- Fetches open PRs from seed repos

**This takes 10-30 minutes.** Go grab coffee. You only run this once.

After init, verify:
```
/brain stats
```
You should see ~1500+ nodes (mostly Project nodes from GitHub repos, plus Signals from Slack/Gmail).

### Step 4: Install global slash commands

The plugin auto-registers `/brain`, `/plan`, and `/nemesis` when you open Claude Code in the
nemesis_v2 directory. To use `/plan` and `/nemesis` from ANY directory:

```bash
# These are already created if you followed the build process:
ls ~/.claude/commands/
# Should show: nemesis.md  plan.md

# If missing, create them:
cd ~/Projects/Agents/nemesis_v2
sed 's|scripts/|'$(pwd)'/scripts/|g; s|workspace/|'$(pwd)'/workspace/|g' commands/plan.md > ~/.claude/commands/plan.md
sed 's|scripts/|'$(pwd)'/scripts/|g; s|workspace/|'$(pwd)'/workspace/|g' commands/nemesis.md > ~/.claude/commands/nemesis.md
```

Now `/plan` and `/nemesis` work from any project directory.

---

## Daily Workflow

### Morning startup (2 minutes)

```
/brain refresh        # Fetch new signals since last sync
/plan                 # See your dashboard
```

The dashboard shows: alerts, calendar, tasks (priority-scored), missed communications, features, capacity bar.

### Throughout the day

| What you want | Command |
|---|---|
| See dashboard | `/plan` |
| Add a task | `/plan add "implement retry handler" --priority high --feature emandate-retry --hours 2` |
| Mark task done | `/plan done "implement retry handler"` |
| Check missed comms | `/plan missed` |
| Focus session | `/plan focus 3` (3-hour block, top tasks by priority score) |
| Search anything | `/plan search "emandate timeout"` |
| Smart schedule | `/plan smart-plan` (DAG + CPM + time-blocked schedule) |
| Feature deep-dive | `/plan feature emandate-retry` |

### End of week

```
/plan weekly          # Weekly review: done vs carried over, metrics
```

---

## Architecture Workflow

### First time: bootstrap code intelligence

```
/nemesis bootstrap --project emandate-service
```

This clones the repo, runs AST extraction, and imports Functions/Classes/Endpoints/DataStores/Tests
as graph nodes. Repeat for other repos you work on.

### Analyze a codebase

```
/nemesis reverse emandate-service
```

Outputs: architecture overview, key patterns, API surface, data layer, complexity hotspots,
security gaps, untested functions. Delegates to `engineering:architecture` and `engineering:tech-debt`.

### Work on a feature

```
# 1. Extract requirements from a PRD
/nemesis requirements "Emandate Retry Flow PRD"

# 2. Identify risks
/nemesis risk emandate-retry-flow

# 3. Generate implementation doc
/nemesis impl-doc emandate-retry-flow

# 4. Get code skeleton for one repo
/nemesis implement emandate-retry-flow --repo emandate-service

# 5. Review before merge
/nemesis review razorpay/emandate-service#123
```

Each step writes knowledge back to rubick.db. Step 2 benefits from step 1's requirements.
Step 3 benefits from both. The graph gets smarter with every command.

### Validate extracted knowledge

```
/nemesis validate "Must retry failed mandates within 24h" --correct
/nemesis validate "Retry latency < 100ms" --wrong
```

This updates confidence scores. Future queries rank confirmed nodes higher.

### Check cross-project impact

```
/nemesis impact "changing mandate callback format in emandate-service"
```

Shows which repos, requirements, and risks are affected.

### See coverage and learning stats

```
/nemesis status           # Feature coverage: requirements, risks, decisions per feature
/nemesis learn            # Confidence distribution, validation rate, accuracy
```

---

## Brain Operations

### Ingesting new signals

```
# Auto-detect source type from URL:
/brain ingest https://razorpay.slack.com/archives/C01234/p1234567890
/brain ingest https://mail.google.com/mail/u/0/#inbox/FMfcgzQXKJJGvzKtRplBjFJnBxhMZVLr
/brain ingest https://github.com/razorpay/emandate-service/pull/123

# Platform-specific:
/brain ingest-slack payments_emandate 1234567890.123456
/brain ingest-email <thread_id>
/brain ingest-doc "Emandate Retry Design"
```

### Querying the graph

```
/brain stats                              # Node/edge counts
/brain search --text "emandate retry"     # Full-text search
/brain query --type Feature               # All features
/brain query --type Signal                # Recent signals
/brain impact --type Function --name HandleRetry  # Impact analysis
/brain cross-refs --text "mandate"        # Cross-project references
/brain context-for "emandate-retry" --consumer arch --budget 4000  # Budgeted context
/brain recall "what was decided about retry backoff"  # Semantic recall
/brain decisions                          # All recorded decisions
/brain timeline "emandate-retry"          # Chronological events
```

### GitHub operations

```
/brain github-prs --repo emandate-service --state open
/brain github-issues --repo emandate-service
/brain github-search "emandate retry"     # Search across all razorpay repos
/brain github-clone emandate-service      # Clone to workspace/repos/
```

### Maintenance

```
/brain refresh          # Incremental sync (run daily)
/brain health           # DB health report
/brain orphans          # Disconnected nodes
/brain stale-signals    # Unprocessed signals > 7 days old
/brain archive          # Clean up old nodes per retention policy
```

---

## How the Graph Works

Everything is stored as **nodes** and **edges** in a single SQLite database.

### Node types you'll encounter most

| Type | What it is | Created by |
|---|---|---|
| Signal | A Slack message, email, PR notification | Brain (auto-ingestion) |
| Task | A work item you're tracking | Planner (`/plan add`) |
| Feature | A trackable initiative | Brain (`/brain feature-create`) |
| Decision | A recorded decision | Brain or Nemesis |
| Requirement | An extracted requirement | Nemesis (`/nemesis requirements`) |
| RiskItem | An identified risk | Nemesis (`/nemesis risk`) |
| ArchDecision | An architecture decision | Nemesis (`/nemesis reverse`, `/nemesis impl-doc`) |
| Function | A code function (from AST) | Nemesis (`/nemesis bootstrap`) |
| Endpoint | An API route (from AST) | Nemesis (`/nemesis bootstrap`) |
| Project | A GitHub repo | Brain (`/brain init`) |
| Person | A team member | Brain (auto from Slack/GitHub) |

### Edge types that matter

| Edge | Meaning |
|---|---|
| HAS_REQUIREMENT | Feature/Document -> Requirement |
| HAS_RISK | Feature -> RiskItem |
| IMPLEMENTS_FEATURE | Task -> Feature |
| BLOCKS | Task -> Task (dependency) |
| RELATES_TO | Cross-project relationship |
| SIGNAL_FOR | Signal -> Project |
| OPENS_PR | Person -> PR |

### Confidence scoring (Nemesis agent)

Nodes extracted by Nemesis carry a confidence score:
- **0.7** — LLM-extracted, not yet validated
- **0.85** — Validated by a PR review or cross-reference
- **1.0** — Explicitly confirmed by user (`/nemesis validate --correct`)
- **0.5** — Disputed (conflicting evidence)
- **0.2** — Rejected by user (`/nemesis validate --wrong`)

Higher confidence nodes rank higher in context retrieval.

---

## Configuration

All configuration lives in `scripts/brain_config.py`. Key constants:

| Constant | Default | What it controls |
|---|---|---|
| `RUBICK_DB_PATH` | `workspace/rubick.db` | Database location |
| `CONTEXT_BUDGET_ARCH_INIT` | 4000 | Token budget for arch context |
| `CONTEXT_BUDGET_PLANNER` | 1500 | Token budget for planner context |
| `SEED_PROJECTS` | 9 repos | Which repos to auto-track |
| `SEED_CHANNELS` | 6 channels | Which Slack channels to sync |
| `SYNC_INTERVAL_QUICK_MIN` | 60 | Minutes between quick syncs |
| `MAX_NEW_TASKS_PER_SYNC` | 3 | Max auto-created tasks per sync |
| `DRIVE_STORAGE_FOLDER_ID` | `1u1v...` | Google Drive folder for backups |

### Seed projects

| Repo | Role |
|---|---|
| emandate-service | primary |
| offers-engine | primary |
| rpc | primary |
| payments-mandate | primary |
| api | ecosystem |
| pg-router | ecosystem |
| checkout-service | ecosystem |
| batch | ecosystem |
| mock-gateway | ecosystem |

### Seed Slack channels

- #payments_emandate
- #payments_cards_emandate_coe
- #emandate_alerts
- #slash-offers-engine
- #debugging-offers-with-slash
- #recurring_alerts

To add more projects or channels, edit `brain_config.py` and run `/brain refresh`.

---

## Skills that Nemesis Delegates To

The `/nemesis` agent is an intelligent orchestrator that delegates to these skills:

| Skill | Used by | What it does |
|---|---|---|
| `engineering:architecture` | `reverse` | Identifies architectural patterns |
| `engineering:tech-debt` | `reverse` | Categorizes technical debt |
| `engineering:code-review` | `review` | Code quality analysis |
| `engineering:system-design` | `impl-doc`, `implement` | System architecture design |
| `engineering:testing-strategy` | `risk`, `review` | Test coverage gaps |
| `engineering:deploy-checklist` | `risk` | Deployment risk assessment |
| `engineering:documentation` | `impl-doc` | Documentation structure |
| `engineering:incident-response` | `risk` | Incident pattern matching |
| `compass:razorpay-api-review` | `review` | Razorpay API convention validation |
| Blade MCP tools | `implement` | Razorpay UI component patterns |

These skills are invoked automatically — you don't need to call them manually.

---

## Troubleshooting

### "rubick.db not found"

Run `/brain init` to create the database, or check that `workspace/` directory exists.

### "No meetings found" / empty dashboard

Run `/brain refresh` to sync latest data from Calendar, Slack, Gmail.

### Tasks all have the same priority score

Run `/plan backfill` to recalculate scoring fields on existing tasks.

### Nemesis commands say "no arch nodes found"

Run `/nemesis bootstrap --project <slug>` to import code intelligence nodes first.

### Slow performance

The database uses WAL mode for concurrent reads. If it gets large (>50MB):
```
/brain archive          # Archive old nodes per retention policy
/brain health           # Check database health
```

### MCP tools not working

Verify your Claude Code MCP connectors are active:
- Slack connector: should see `slack_search_channels` available
- Gmail connector: should see `search_threads` available
- Calendar connector: should see `list_events` available
- Drive connector: should see `read_file_content` available

### GitHub CLI not authenticated

```bash
gh auth status          # Check
gh auth login           # Fix
```

---

## Quick Reference Card

```
/plan                           # Dashboard
/plan add "title" --priority P  # Add task
/plan done "title"              # Complete task
/plan missed                    # Missed comms
/plan focus 3                   # 3-hour focus block
/plan smart-plan                # DAG-scheduled plan
/plan weekly                    # Weekly review

/nemesis bootstrap --project slug  # Import code intelligence
/nemesis reverse slug              # Reverse-engineer codebase
/nemesis requirements "doc"        # Extract requirements
/nemesis risk feature              # Identify risks
/nemesis impl-doc feature          # Implementation document
/nemesis implement feature --repo  # Code skeleton
/nemesis review feature_or_pr      # Review checklist
/nemesis validate node --correct   # Confirm extracted knowledge
/nemesis impact "change desc"      # Cross-project impact
/nemesis status                    # Coverage dashboard

/brain refresh                  # Sync new data
/brain stats                    # Graph stats
/brain search --text "query"    # Full-text search
/brain recall "question"        # Semantic recall
/brain ingest <url>             # Ingest from any source
```
