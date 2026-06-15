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

### Prerequisites

- [Claude Code](https://claude.ai/code) (CLI or Desktop)
- Python 3.9+
- `gh` CLI (GitHub)

### Setup

```bash
# Clone
git clone https://github.com/sauravk-oss/nemesis.git
cd nemesis

# Install Python deps
pip3 install -r requirements.txt

# Initialize brain (creates workspace dirs, brain.db, seeds services)
python3 -m brain init

# Verify
python3 -m brain stats
```

### Run with Claude Code

```bash
# Open in Claude Code
claude

# Start Nemesis — shows features dashboard
/nemesis

# Create a new feature
/nemesis new <feature-name>

# Run specific phases
/nemesis ideation <slug>
/nemesis solutioning <slug>
/nemesis techspec <slug>

# Implementation (code gen + PR)
/implement <slug>

# E2E testing
/e2e <slug>
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

```bash
# Stats
python3 -m brain stats

# Search
python3 -m brain search "payment" --type Function --limit 10
python3 -m brain search-code "handlePayment" --project emandate-service

# Context (budget-limited retrieval)
python3 -m brain context "emandate payment flow" --budget 4000

# Impact analysis
python3 -m brain impact emandate-service --cross-service

# Health check
python3 -m brain health

# Migration (from old rubick.db)
python3 -m brain migrate-rubick workspace/rubick.db
```

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
