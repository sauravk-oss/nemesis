# Nemesis v2 — Skill Orchestrator

## Role

This is the orchestrator surface for Nemesis v2. Nemesis is a multi-skill engineering
system built on the **Living Index Brain** — a single cross-project knowledge graph stored
in `workspace/brain.db`. The brain is the ground truth for every skill (Ideation,
Solutioning, Tech Spec, Implementation, E2E, and the support skills).

**Only the skill layer (the LLM) calls MCP tools.** The `brain/` package and every
`scripts/*.py` are pure Python and never touch Slack, Gmail, Drive, Calendar, or GitHub
directly. Data flows in through the **Franco two-phase pattern**: Python prepares the
fetch parameters → the LLM makes the MCP/CLI call → Python ingests the returned payload.

## Architecture

```
User  ──►  /nemesis  (orchestrator — intent detection, phase routing)
                │
   ┌────────────┼─────────────────────────────────────────┐
   ▼            ▼                                           ▼
 Skills     BrainAPI (brain/api.py)                    MCP layer (LLM only)
 (LLM)          │                                      Slack / Drive / Gmail /
        ┌───────┼────────┐                             Calendar / GitHub / DevRev
        ▼       ▼        ▼
   GraphEngine  Context  MemoryEngine
   SQLite +     Retriever  learning_ledger
   NetworkX     (3-channel) sync_state
        │
        ▼
   workspace/brain.db   ← single Living Index (SQLite + FTS5)
```

## Design Principle

**Math is Code, Meaning is LLM.**
- Deterministic layer (Python / `brain/`): graph algorithms, scoring, BFS/PageRank, budget
  truncation, dedup, persistence.
- LLM layer (skills): interpretation, summarization, entity extraction, MCP calls.

---

## Skill Surface

All skills are Claude Code slash-commands defined in `commands/*.md`. Invoke another skill
via the `Skill` tool; if it fails to resolve, follow that skill's documented protocol
directly as a fallback.

| Skill | Purpose |
|-------|---------|
| `/nemesis` | **Orchestrator** — intent detection, phase routing, features dashboard, system commands |
| `/brain` | Knowledge-graph operations (the engine behind everything) |
| `/franco` | Universal data collector — any URL/ID/file → normalize → ingest |
| `/implement` | Phase 4 — code generation + tests + quality gates + gated PR |
| `/e2e` · `/devtest` | Phase 5 — end-to-end + interactive PR-driven debug testing |
| `/pipeline` | Pipeline status and control |
| `/review` | Code review and audit (5-skill + 8 Razorpay domain checks) |
| `/plan` | Planner |
| `/explain` | Payment-flow explainer |
| `/doc` · `/silencer` | Tech-spec document generation (.docx local / Google Doc) |
| `/diagram` · `/designer` | Architecture diagrams (Canva-first) / visual design |
| `/standup` | Daily standup + reports |
| `/tickets` | DevRev/Jira ticket management |
| `/scenario` | Test scenario generation |
| `/db-validator` | Payment-state + pre-deploy validation |
| `/slash` | @Slash bot interaction (channel `C0B3U3Z2JG1`) |

---

## `/nemesis` Command Surface

`/nemesis` is the single entry point. It parses free-form intent and routes to the right
phase or answers directly from the brain.

### System

```
/nemesis init                  # full bootstrap: validate → seed → live L1 ingest → report
/nemesis doctor                # health check (deps, gh, MCPs, brain.db, sources, experts)
```

### Features

```
/nemesis                       # features dashboard (no args)
/nemesis new <name>            # create a feature → Ideation
/nemesis new <name> <drive-link>   # PULL a shared feature from Drive + rebuild brain
/nemesis <slug>                # resume a feature at its next phase
/nemesis status <slug>         # detailed feature status
```

### Phases (immutable order)

```
Phase -1  →  Phase 1   →  Phase 2        →  Phase 3   →  Phase 4         →  Phase 5
Brain-First  Ideation     Solutioning       Tech Spec    Implementation     E2E
(mandatory)  overview     solution + risk   docs         code + gated PR    testing

/nemesis ideation <slug>
/nemesis solutioning <slug>
/nemesis techspec <slug>
/implement <slug>
/e2e <slug>
```

### Feature sharing (Google Drive)

```
/nemesis sync <slug>           # PUSH a feature's artifacts to Drive (idempotent)
/nemesis pull <drive-link>     # PULL a feature from Drive + rebuild brain locally
```

---

## Brain CLI Surface

`python3 -m brain <command>` — run with no args for the full list. These are the **real**
commands; nothing here calls an MCP.

### Lifecycle / bootstrap
```
brain init                     # dirs + schema + seed 45 services + 16-skill registry
brain register-sources         # DataSource nodes + RELATES_TO edges from config/sources.json
brain init-experts [--level N] # seed ProjectExpert nodes (default L1); idempotent
brain doctor                   # green/amber/red health table
brain seed                     # (re)seed the 45 projects + DEPENDS_ON edges
brain refresh                  # reload the in-memory NetworkX graph from edges
```

### Query
```
brain stats                            # node/edge counts, projects, experts
brain health <project>                 # A–F service health grade
brain search <query> [--type T]        # FTS5 text search
brain search-code <query> [-p P]       # search code bodies
brain context <target> [-b N] [-c C]   # budgeted retrieval (consumer: planner|arch|dev)
brain who-calls <fn> [-d N]            # callers (N hops)
brain what-calls <fn> [-d N]           # callees (N hops)
brain path <src> <dst>                 # shortest path between nodes
brain impact <fn1,fn2> [-d N]          # blast-radius analysis
brain dead-code <project>              # dead-code candidates
brain test-gaps <project>              # untested high-PageRank functions
```

### Learning pipeline (Franco two-phase)
```
brain ingest <source> [--feature F] [--project P] [--max-chars N]
      # phase 1: ingest a LOCAL file directly. Remote/MCP sources instead print a
      #          needs_fetch plan for the LLM to act on.
brain ingest-mcp <type> <id> --payload FILE [--feature F] [--project P]
      # phase 2: ingest an LLM-fetched payload (JSON) → learn → flush. Dedup on (type,id).
brain learn-status                     # staged-items state
brain learn-flush [--dry-run]          # flush staged items → nodes + edges
```

### Graph CRUD
```
brain add-node <type> <name> [-d JSON] [-p P] [-c F]
brain get-node <type> <name>
brain delete-node <type> <name>
brain add-edge <from_type> <from> <to_type> <to> <edge_type>
```

### Feature lifecycle
```
brain feature-create <name> [--owner O]
brain feature-update <name> --status S
brain feature-list [--status S]
brain feature-health <name>
```

### Migration
```
brain migrate-rubick <path>            # one-time import from legacy rubick.db
```

---

## Bootstrap Protocol (`/nemesis init`)

The brain is bootstrapped in **two steps**, then enriched by a bounded live ingest. This is
what `/nemesis init` orchestrates — there is no monolithic "clone-everything" init.

- **Phase A — Validate.** `./setup.sh --check` (read-only): Python deps, `gh auth`, and MCP
  connectors. Never installs, never writes.
- **Phase B — Seed (idempotent).**
  1. `brain init` — workspace dirs + schema + 45 seed services (with `DEPENDS_ON` graph) +
     the 16-skill registry. **Does not create experts.**
  2. `brain register-sources` — a `DataSource` node per entry in `config/sources.json`
     (Slack channels, Drive docs, repos, DevRev) + `RELATES_TO` edges to projects.
  3. `brain init-experts --level 1` — a `ProjectExpert` node per project at L1. Never
     downgrades an existing expert; merges on level-up.
- **Phase C — Bounded live L1 ingest (LLM-driven, Franco two-phase).** For each source
  flagged `l1: true`: Slack ~30 msgs / 7d, Drive top-5 docs (×8000 chars), GitHub
  README + top-10 files via `gh`, DevRev ~5 items. Each source checkpointed via a sync
  cursor (incremental on re-run). A disconnected MCP is skipped with a warning — init never
  fails as a whole.
- **Phase D — Report.** Sources registered, experts seeded, nodes ingested; persists an
  `init:<timestamp>` Signal node.

> Experts level up **L1 → L5** via XP earned from feature work (Decision #51) — they are
> **not** eagerly deep-read to L2 at init time. `brain init` alone seeds services + skills,
> not experts.

---

## Franco Ingestion (two-phase)

No skill makes raw MCP fetch calls. They invoke `/franco <source>` (or the brain ingest
commands), which detects the source type and routes:

- **Local files & direct CLI sources** (local path, GitHub via `gh`, DevRev) — fetched in
  phase 1 by `brain ingest`, normalized, and flushed.
- **MCP-backed sources** (Slack, Gmail, Drive, Calendar) — phase 1 emits a `needs_fetch`
  plan with the exact MCP tool + params; the **LLM** makes the call; phase 2
  (`brain ingest-mcp <type> <id> --payload FILE`) ingests the returned payload, dedupes on
  `(source_type, source_id)`, runs `learn()`, and flushes.

---

## Feature Sync (push / pull)

Feature artifacts are shared via Google Drive; **`brain.db` is never shipped**. Driven by
`scripts/feature_sync.py` (pure Python — computes *what* to move; the LLM performs the Drive
MCP I/O between phases).

- **PUSH** (`/nemesis sync <slug>`, and after each phase's `learn-flush`):
  `feature_sync.py status` diffs local files vs the per-feature manifest
  (`workspace/features/<slug>/.drive.json`, gitignored) → `push-plan` lists changed
  `.md`/`.html`/`.json` files (skip >2 MB, skip `*-logs/`) → LLM uploads to
  `nemesis/features/<slug>/` (the `implementation/` subdir flattened to `implementation__`)
  → `record-push` writes back `{file_id, sha256, size, mtime, pushed_at}`. Unchanged files
  are never re-uploaded.
- **PULL** (`/nemesis new <slug> <drive-link>` or `/nemesis pull <drive-link>`):
  `pull-plan --link <link>` parses the folder id → LLM `search_files` + `download_file_content`
  → `record-pull` writes files to `workspace/features/<slug>/` (un-flattening
  `implementation__`) → **brain rebuild**: `feature-create` → Franco ingest each artifact →
  `learn-flush`. `feature-health <slug>` should then be populated.

---

## Context Budget Protocol

When a skill needs context, it calls `brain context <target> -b <N> -c <consumer>` (or
`BrainAPI.context_for(...)`). The retriever runs a 3-channel hybrid — NetworkX graph walk +
FTS5 keyword match + (optional, lazy) LanceDB vectors — scores neighbors by edge relevance,
recency, and confidence, serializes to text, and truncates at the token budget.

Consumer profiles weight the three channels differently (`brain.config.HYBRID_WEIGHTS`):

| Consumer | Leans toward |
|----------|--------------|
| `planner` | graph (structure / dependencies) |
| `arch` | vector (semantic similarity) |
| `dev` | FTS5 (exact code/keyword) |
| `user` | vector (semantic) |

If vectors are unavailable, retrieval degrades gracefully to graph + FTS5.

---

## Learning Loop (self-improving confidence)

- Nodes are created at `confidence=0.7` (LLM-extracted).
- Multi-source confirmation / review validation bumps to `0.85`.
- Explicit user confirmation (or a materialized predicted risk) → `1.0`.
- Contradicted knowledge is penalized (expert XP −200, correction recorded).
- `context_for()` prefers high-confidence nodes; low-confidence nodes surface for review.

Every skill interaction stages knowledge via `brain.learn()` and persists with
`brain.flush()` / `brain learn-flush` — as nodes + typed edges (`HAS_REQUIREMENT`,
`HAS_RISK`, `HAS_USE_CASE`, `IMPLEMENTS_FEATURE`, `DECIDED_BY`, `SIGNAL_FOR`, …). Dedup on
`(type, name)` + FTS.

---

## Drive Storage

The **nemesis** Drive folder (`1u1vLNkGY9CM8G8eBDe0DtEAjcT3mjBa5`) holds backups and shared
feature artifacts. Rule: **`brain.db` is truth; Drive is backup.** New files always use
`DRIVE_STORAGE_FOLDER_ID` as the parent. Feature artifacts live under
`nemesis/features/<slug>/` (see Feature Sync above).

---

## Boundaries

- **No MCP from Python.** `brain/` and `scripts/*.py` never call MCPs. (Reading local Claude
  config JSON to *detect* connected MCPs is not an MCP call — that's allowed.)
- **`brain.db` is never shipped between machines** — rebuilt locally from pulled artifacts.
- **OAuth MCPs cannot be auto-connected by a script.** `setup.sh` validates and guides only;
  tokens are managed by Claude Code, never stored in the repo.
- **Never edit files without explicit user permission.** Exception: `brain.db` operations
  (reads/writes/upserts/flush) are always free.
- **Implementation safety:** never push to main/master, never force-push, never commit
  secrets; always use feature branches; the user must approve generated code before commit.
