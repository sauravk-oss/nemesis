# Nemesis v2 — Living Index Brain

An AI-powered multi-skill system for software engineering at scale. Nemesis absorbs knowledge from every source (Slack, Gmail, GitHub, Calendar, Drive, code) and stores it in a unified knowledge graph — like IntelliJ's project index, but persistent, cross-service, and enriched with human knowledge.

## What It Does

Nemesis is a **5-phase feature pipeline** powered by a **Living Index Brain**:

```
Phase -1       Phase 1       Phase 2           Phase 3       Phase 4        Phase 5
Brain-First → Ideation    → Solutioning     → Tech Spec   → Implementation → E2E
(knowledge)   (overview)    (solution+risk)   (docs)        (code+PR)        (testing)
```

Each phase is **interactive** — Nemesis asks questions at every decision point, stores your answers, and uses them in future runs.

## Quick Start

### Easiest path (TL;DR — 3 commands + 1 click)

```bash
git clone https://github.com/sauravk-oss/nemesis.git && cd nemesis
./setup.sh            # installs deps, checks gh, validates MCPs, runs brain init
```
Then inside **Claude Code**: connect the OAuth MCPs when prompted (Slack / Drive / Gmail /
Calendar — one click each), then run `/nemesis init` and `/nemesis doctor`. Done — you have
a working brain. Everything below is the same path with detail.

### Prerequisites

- [Claude Code](https://claude.ai/code) (CLI or Desktop)
- Python 3.9+
- `gh` CLI (GitHub), authenticated (`gh auth login`)

### Setup (4 steps)

```bash
# 1. Clone
git clone https://github.com/sauravk-oss/nemesis.git
cd nemesis

# 2. Run the idempotent setup script
#    - installs Python deps, checks gh auth, validates MCP connectors,
#      creates .env, and runs `brain init`
./setup.sh
#    Read-only diagnostics (no installs, no writes):  ./setup.sh --check
```

```text
# 3. Connect MCPs inside Claude Code (one-time, OAuth — guided)
#    setup.sh CANNOT connect OAuth MCPs; it only tells you which are missing.
#    Open Claude Code and connect: Slack, Google Drive, Gmail, Google Calendar.
#    Tokens are managed by Claude Code — never stored in this repo.

# 4. Bootstrap + health-check the brain, inside Claude Code:
/nemesis init      # seed services + sources + experts (L1) + bounded live L1 ingest
/nemesis doctor    # green/amber/red health table with remediation hints
```

> Full walkthrough — MCP OAuth, env vars, troubleshooting — is in **[INSTALL.md](INSTALL.md)**.

### Day-to-day with Claude Code

```text
/nemesis                       # features dashboard (no args)
/nemesis new <name>            # create a new feature → Ideation
/nemesis new <name> <drive-link>   # PULL a shared feature from Drive + rebuild brain
/nemesis <slug>                # resume a feature at its next phase

# Phases
/nemesis ideation <slug>       # Phase 1: overview (As-Is → To-Be)
/nemesis solutioning <slug>    # Phase 2: solution design + risk analysis
/nemesis techspec <slug>       # Phase 3: tech spec document
/implement <slug>              # Phase 4: code gen + tests + gated PR
/e2e <slug>                    # Phase 5: end-to-end testing

# Feature sharing (Google Drive)
/nemesis sync <slug>           # PUSH a feature's artifacts to Drive
/nemesis pull <drive-link>     # PULL a feature from Drive + rebuild brain locally

# Reports
/nemesis report <slug>         # regenerate the AI-pipeline HTML (collapsible tree, brain-powered)

# System
/nemesis init                  # bootstrap brain (sources + experts + live L1)
/nemesis doctor                # health check (deps, gh, MCPs, brain.db, sources)
```

### Reports & artifacts

Every feature run produces structured artifacts under `workspace/features/<slug>/`:

| Artifact | Produced by | What it is |
|----------|-------------|------------|
| `overview.md` / `.html` | Ideation | As-Is → To-Be, edge cases, and an **Open Questions (Next Iteration)** ledger — every unresolved question with its working assumption and who can resolve it. Re-running Ideation flips answered ones to resolved. |
| `solution.md` / `.html` | Solutioning | Solution design + risk register; consumes the open-questions ledger. |
| `tech-spec.md` | Tech Spec | 15-section Razorpay tech spec. |
| `test-report.md` | Implementation 6.5e | Each test → the requirement / RiskItem / issue it covers, what it asserts, why it exists. |
| `change-report.md` | Implementation 6.5f | What changed and **why**, tests added & passing, **pending tests still to pass** before full ramp, review findings resolved, and the merge **verdict**. |
| `pipeline-report.html` | Implementation 9b / `/nemesis report` | Single self-contained HTML of the **whole AI pipeline** as collapsible tree nodes — every doc, skill used + its input/output, each iteration input→output, the embedded test report, the archive of superseded artifacts, a Brain-powered knowledge node, and a redirect Drive URL for more details. |

### Sharing a feature with a teammate

Feature artifacts (overview, solution, tech-spec, implementation, test-report) are
pushed to a Google Drive folder after each phase. `brain.db` is **never** copied — a
teammate rebuilds it locally from the pulled artifacts:

```text
# You (after working a feature):
/nemesis sync my-feature              # pushes artifacts to nemesis/features/my-feature/

# Teammate (fresh clone, after ./setup.sh + MCP connect):
/nemesis new my-feature <drive-link>  # pulls every artifact, rebuilds brain + feature state
```

## Architecture

```
                    BrainAPI (brain/api.py)
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
   GraphEngine        ContextRetriever    MemoryEngine
   (SQLite+NetworkX)  (3-channel hybrid)  (learning+sync)
```

### Brain (Knowledge Graph)

- **SQLite** typed tables for code entities (functions, classes, tests, endpoints, datastores)
- **NetworkX** in-memory graph for fast algorithms (BFS, PageRank, impact analysis)
- **FTS5** full-text search across 700K+ code entities
- **LanceDB** vector search (lazy-loaded, optional)

### Skills (20 commands)

| Skill | Purpose |
|-------|---------|
| `/nemesis` | Orchestrator — routes to phases, shows features dashboard |
| `/brain` | Knowledge graph operations |
| `/implement` | Code generation + tests + PR creation |
| `/pipeline` | Pipeline status and control |
| `/review` | Code review and audit |
| `/explain` | Payment flow explainer |
| `/doc` | Tech spec document generation |
| `/silencer` | Google Doc tech spec generator |
| `/franco` | Universal data collector |
| `/diagram` | Architecture diagrams |
| `/designer` | Visual design agent |
| `/standup` | Daily standup and reports |
| `/tickets` | Ticket management |
| `/scenario` | Test scenario generator |
| `/devtest` | Interactive E2E debug testing |
| `/e2e` | End-to-end testing orchestrator |
| `/plan` | Planner skill |
| `/slash` | @Slash bot interaction |
| `/db-validator` | Payment state validator |

### Agents (12 sub-agents)

Parallel sub-agents for heavy-lift work: code review, implementation, test generation, data ingestion, standup collection, etc.

## Brain CLI

The brain is pure Python — `python3 -m brain <command>`. It **never calls MCPs**;
only the LLM (skill layer) makes MCP calls and hands payloads back to the brain
(the Franco two-phase pattern). Run `python3 -m brain` with no args for the full list.

```bash
# --- Bootstrap (what /nemesis init orchestrates) ---
python3 -m brain init                  # dirs + schema + seed 45 services + 16 skills
python3 -m brain register-sources      # DataSource nodes from config/sources.json
python3 -m brain init-experts --level 1 # seed ProjectExpert nodes (idempotent)
python3 -m brain doctor                # green/amber/red health table

# --- Query ---
python3 -m brain stats
python3 -m brain search "payment" --type Function
python3 -m brain search-code "handlePayment" -p emandate-service
python3 -m brain context "emandate payment flow" -b 4000
python3 -m brain impact emandate-service
python3 -m brain health emandate-service

# --- Learning pipeline (Franco two-phase ingest) ---
python3 -m brain ingest <local-file> --feature my-feature    # phase 1: local/direct
python3 -m brain ingest-mcp slack <channel-id> --payload msgs.json  # phase 2: LLM payload
python3 -m brain learn-flush           # flush staged items → nodes + edges

# --- Feature lifecycle ---
python3 -m brain feature-create "My Feature"
python3 -m brain feature-list
python3 -m brain feature-health "My Feature"

# --- Migration (from legacy rubick.db) ---
python3 -m brain migrate-rubick workspace/rubick.db
```

> `brain init` seeds services + the skill registry but **not** experts. Experts are
> seeded separately by `brain init-experts` and level up (L1→L5) via XP from feature
> work. The full bootstrap is orchestrated by `/nemesis init`.

## Directory Structure

```
nemesis/
├── brain/                  # Living Index Brain package
│   ├── api.py              # BrainAPI — single entry point
│   ├── cli.py              # CLI: python -m brain <command>
│   ├── config.py           # 45 projects, service deps, weights
│   ├── types.py            # 33 node types, 32 edge types
│   ├── graph/              # SQLite + NetworkX + algorithms
│   ├── context/            # 3-channel hybrid retrieval
│   ├── memory/             # Learning pipeline + sync
│   └── migration/          # rubick.db → brain.db migration
├── commands/               # 20 skill definitions (markdown)
├── agents/                 # 12 sub-agent templates
├── config/                 # Expert configs
├── schemas/                # Graph schema docs
├── scripts/                # Legacy scripts + archive
└── workspace/              # Runtime data (gitignored)
    ├── brain.db            # Knowledge graph (created by init)
    ├── features/           # Per-feature working directories
    └── repos/              # Cloned repos (on demand)
```

## License

Private — Razorpay internal use.
