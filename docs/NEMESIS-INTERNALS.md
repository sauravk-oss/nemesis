# Nemesis v2 — Internal Architecture & Design Decisions

> **Purpose**: How Nemesis actually works — storage internals, the Living Index Brain,
> why each technology was chosen, what was evaluated and rejected, and the full
> processing pipeline from raw source to AI-driven feature analysis.

---

## Table of Contents

1. [The One-Sentence Idea](#1-the-one-sentence-idea)
2. [System Overview](#2-system-overview)
3. [The Living Index Brain](#3-the-living-index-brain)
4. [Knowledge Sources & Ingestion](#4-knowledge-sources--ingestion)
5. [Storage Stack — What's Used and Why](#5-storage-stack--whats-used-and-why)
6. [The Franco Two-Phase Pattern](#6-the-franco-two-phase-pattern)
7. [Context Budget Engine](#7-context-budget-engine)
8. [The 5-Phase Feature Pipeline](#8-the-5-phase-feature-pipeline)
9. [Project Expert System](#9-project-expert-system)
10. [Skill Architecture](#10-skill-architecture)
11. [Models and Embedding Stack](#11-models-and-embedding-stack)
12. [Design Decisions — What Was Evaluated and Rejected](#12-design-decisions--what-was-evaluated-and-rejected)
13. [Graph Schema — Node and Edge Types](#13-graph-schema--node-and-edge-types)
14. [The Learning Pipeline](#14-the-learning-pipeline)
15. [Discovery Plugin Integration](#15-discovery-plugin-integration)
16. [Python Package Structure](#16-python-package-structure)

---

## 1. The One-Sentence Idea

Nemesis is an AI assistant that builds a persistent, queryable index of your entire codebase
and team knowledge — like IntelliJ's project index, but cross-service, cross-communication
(Slack, email, PRs), and enriched with human decisions — then routes every engineering task
(feature analysis, code review, tech spec, code generation, E2E testing) through that index
instead of starting from scratch.

---

## 2. System Overview

```
                      User input (Claude Code CLI)
                               │
                    ┌──────────▼──────────┐
                    │   /nemesis          │
                    │   Intent Detector   │
                    │   Phase Router      │
                    └──────┬──────────────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                 ▼
   Phase 1: Ideation  Phase 2: Solutioning  Phase 3: Tech Spec
   Phase 4: Implement Phase 5: E2E
          │                │                 │
          └────────────────┼─────────────────┘
                           │
                    ┌──────▼──────────┐
                    │   BrainAPI      │   ← brain/api.py — single entry point
                    │   (Python only) │
                    └──┬───┬──────┬───┘
                       │   │      │
              ┌────────┘   │      └──────────┐
              ▼            ▼                  ▼
        GraphEngine   ContextRetriever   MemoryEngine
        SQLite WAL    3-channel hybrid   learning_ledger
        NetworkX      graph+FTS5+vector  sync_state
        in-memory                        slash_interactions
              │
              ▼
       workspace/brain.db       (single SQLite file, ~500-700 MB)
       workspace/lance/         (LanceDB, lazy-loaded)
       workspace/repos/         (cloned repos, ~2.5 GB)
```

**The critical design principle**: "Math is Code, Meaning is LLM."

- The Python layer (`brain/`) handles all deterministic operations: graph traversal,
  scoring, BFS/PageRank, budget truncation, FTS5 queries, node/edge CRUD, dedup.
- The LLM layer (skills in `commands/*.md`) handles all interpretation: MCP calls,
  entity extraction, summarization, code generation, decision-making.
- **The Python layer never calls MCPs**. It can't — MCPs are LLM-native tools, not
  Python functions. This boundary is enforced by design, not convention.

---

## 3. The Living Index Brain

The Brain is a **single SQLite database** (`workspace/brain.db`) with four logical layers:

### Layer 1: Typed Code Tables (SQLite)

Nine tables with explicit schemas for code entities extracted from 44+ repos:

| Table | Contents | Row Count (typical) |
|-------|---------|---------------------|
| `functions` | Every function/method in every repo | 526,000+ |
| `classes` | Structs, classes, interfaces, enums | 37,000+ |
| `tests` | Test functions | 117,000+ |
| `files` | Source files | ~40,000 |
| `endpoints` | HTTP API routes | 1,400+ |
| `datastores` | DB tables, caches, queues | 2,800+ |
| `kafka_topics` | Kafka topics | varies |
| `modules` | Package imports | 32,000+ |
| `services` | The 45 seed services | 45 |

These are typed (not JSON blobs) for SQL query efficiency: `WHERE project = 'emandate-service'
AND is_test = FALSE AND is_exported = TRUE` hits an index in microseconds across 526K rows.

### Layer 2: Generic Workflow Nodes (SQLite)

A single `nodes(type, name, data JSON, confidence, ...)` table holds non-code entities:

Features, Requirements, ArchDecisions, RiskItems, Signals, ProjectExperts, PRs,
JiraIssues, Documents, SlackChannels, BusinessLogic, ReviewResults, etc.

These don't need typed tables because they're accessed by name/type, not by structural
queries. JSON `data` field is flexible but indexed via FTS5.

### Layer 3: In-Memory Graph (NetworkX DiGraph)

At startup, the Brain loads every edge from SQLite into a `networkx.DiGraph` in RAM (~2s
for 733K edges, ~100-150 MB). All graph algorithms (BFS, PageRank, shortest path, blast
radius, dead code, test gaps) run in-memory against this DiGraph — not SQL.

Why? SQL `WITH RECURSIVE` BFS is slow past 3-4 hops and scales poorly with fan-out.
NetworkX BFS is ~5ms for 5-hop traversal over 733K edges. PageRank takes ~400ms once
and is cached. Dead code detection (find all nodes with zero in-degree) takes ~20ms.

The NetworkX graph is refreshed via `brain.refresh_graph()` after bulk learning flushes.

### Layer 4: FTS5 Full-Text Search (SQLite virtual tables)

Two FTS5 virtual tables are maintained by triggers:

- **`nodes_fts`**: Indexes `name`, `data`, `type` of every generic node (715K entries)
- **`code_fts`**: Indexes full function/class bodies stored in `code_bodies` table (368K entries)

FTS5 makes cross-service text search instant: `search "handleRecurringMandate"` scans
all 715K nodes in milliseconds via the inverted index.

The `code_fts` table enables searching inside function bodies — not just names. This is
what lets `/nemesis` find all functions that contain `"offer_discount"` in their body,
even if the function is named something unrelated.

### Layer 5: Vector Search (LanceDB, lazy-loaded)

LanceDB is stored at `workspace/lance/` and loaded only when `semantic_search()` is
called. At startup: **0 MB RAM**. On first vector query: ~50-200 MB loaded via mmap.

The embedding model is `all-MiniLM-L6-v2` (384 dimensions). Vectors are built from
function body chunks (~500 tokens, 2-line overlap). ANN index enables ~2ms queries.

This is **optional** — the brain degrades gracefully to graph+FTS5 if LanceDB is not
installed or the collection is empty.

---

## 4. Knowledge Sources & Ingestion

The Brain absorbs knowledge from seven source categories. All ingestion goes through
the **Franco two-phase pattern** (see Section 6).

### Code (primary source)

Code is extracted via **Graphify** (tree-sitter based, 36 languages) from cloned repos
in `workspace/repos/`. Graphify runs a two-pass call-graph extraction (EXTRACTED edges
from explicit calls, INFERRED edges from heuristics), Leiden clustering for modules.

```bash
python3 -m brain ingest-code --project emandate-service --repo workspace/repos/emandate-service
```

This populates the typed tables (functions, classes, tests, endpoints, datastores) and
writes code bodies to `code_bodies`. FTS5 triggers auto-index.

### Slack (via Slack MCP)

Channels and threads. Stored as Signal nodes with urgency scoring. Supports:
- Named channels by ID (never by name — names are unreliable)
- Thread replies with all participants
- @Slash bot interactions (dedicated `slash_interactions` table)

### Google Drive & Docs (via Drive/Workspace MCP)

Design docs, tech specs, PRDs. Extracted as Document nodes with EXTRACTED_FROM edges
back to requirements and arch decisions found in them.

### Gmail (via Gmail MCP)

Email threads. Stored with subjects, decisions-found flag, action-items-found flag.
Body stripped after 180 days (retention policy).

### GitHub (via `gh` CLI, not MCP)

PRs, issues, commits, code search. GitHub uses the `gh` CLI because:
1. `gh` runs locally and returns structured JSON
2. No OAuth MCP required — already authenticated via `gh auth login`
3. `gh search code` can search the entire Razorpay org, not just cloned repos

### DevRev / Jira (via CLI/API)

Tickets stored as JiraIssue nodes. The `source_type: "devrev"` field distinguishes from
classic Jira. DevRev API calls are made by the LLM layer.

### Google Calendar (via Calendar MCP)

Meetings, events. Used by `/standup` to aggregate daily context. Stored as Meeting nodes.

### Sync Cursors (Idempotency)

Every source has a `sync_state` record:
```
sync_state(source_type, source_id, project_slug, last_cursor, last_sync)
```

`last_cursor` is a SHA-256 digest of the last-fetched content. On re-run, if the digest
matches, the source is skipped. This makes `/nemesis init` safe to run multiple times.

---

## 5. Storage Stack — What's Used and Why

### What's in Use

| Layer | Technology | Why |
|-------|-----------|-----|
| Primary store | SQLite 3.x (WAL mode) | Zero-infra, single file, ACID, FTS5 built-in, ~700 MB for 715K nodes |
| Graph algorithms | NetworkX 3.x (in-memory DiGraph) | ~5ms BFS over 733K edges; PageRank; dead-code; test gaps — all in Python |
| Full-text search | FTS5 (SQLite virtual table) | BM25 keyword search, no external process, trigger-maintained |
| Vector search | LanceDB (lazy) | Disk-based mmap, no server, 0 MB at startup, ~2ms ANN queries |
| Embeddings | `all-MiniLM-L6-v2` via sentence-transformers | 384-dim, fast, good code semantic similarity |
| Code extraction | Graphify (tree-sitter, 36 langs) | Production-grade AST, handles Go receivers, PHP namespaces, TS generics |
| MCP access | Claude Code MCP layer (LLM only) | Slack, Drive, Gmail, Calendar, Canva — OAuth managed by Claude Code |

### What's NOT Used (and why)

See Section 12 for full details. Short version:
- No Redis / Memcache — SQLite WAL + NetworkX in-memory IS the cache
- No Neo4j / graph database — SQLite + NetworkX does everything at this scale with zero infra
- No Qdrant server — replaced by LanceDB (simpler, no process, lazy-loaded)
- No Flask/FastAPI web server — Claude Code is a CLI; a web server adds nothing
- No custom AST parsers — replaced by Graphify (tree-sitter handles edge cases correctly)

---

## 6. The Franco Two-Phase Pattern

This is the core ingestion architecture. It exists because MCPs are LLM-native tools —
Python code cannot call `mcp__slack__get_messages()`. Only the LLM can.

```
Source URL / ID / Path
        │
┌───────▼────────────────────────────────────────────────┐
│  Phase 1: Python (brain.ingest)                        │
│  - Classify source type from URL/ID patterns           │
│  - For LOCAL sources (files, GitHub via gh, DevRev):   │
│    fetch directly → normalize → stage in ledger → done │
│  - For MCP-backed sources (Slack, Drive, Gmail, etc.): │
│    compute exact MCP tool + params → return needs_fetch│
└───────────────────────────────────────────────────────┘
                    │ (if needs_fetch)
                    ▼
         LLM calls the MCP tool with the params
                    │
                    ▼
┌───────────────────────────────────────────────────────┐
│  Phase 2: Python (brain.ingest_mcp_response)          │
│  - Receive the JSON payload from the LLM              │
│  - Normalize to FrancoDocument schema                 │
│  - Dedup on SHA-256 digest of content                 │
│  - stage via brain.learn() into learning_ledger       │
│  - brain.flush() → SQLite nodes + edges               │
└───────────────────────────────────────────────────────┘
```

Source detection uses regex patterns in `brain/config.py`:

```python
SOURCE_PATTERNS = {
    "slack_thread":  r"slack\.com/archives/\w+/p\d+",
    "slack_channel": r"slack\.com/(?:archives|channels?)/\w+",
    "drive_doc":     r"docs\.google\.com/document/d/[\w-]+",
    "github_pr":     r"github\.com/[\w.-]+/[\w.-]+/pull/\d+",
    "jira_id":       r"^[A-Z][A-Z0-9]+-\d+$",
    # ... 9 more patterns
}
```

Each source type maps to a node type: slack_thread → Signal, github_pr → PR, etc.

**CLI equivalent**:
```bash
# Phase 1 (returns needs_fetch plan if MCP-backed):
python3 -m brain ingest "https://razorpay.slack.com/archives/C0B3U3Z2JG1/p1716543200"

# LLM fetches the thread and saves JSON to /tmp/payload.json

# Phase 2 (ingest the payload):
python3 -m brain ingest-mcp slack "C0B3U3Z2JG1/p1716543200" --payload /tmp/payload.json --feature dfb-instant-discount
```

---

## 7. Context Budget Engine

The heart of Phase -1 (Brain-First). Every skill call that needs codebase context
runs `brain context <target> -b <budget> -c <consumer>`.

### Three-Channel Hybrid Retrieval

```
brain.context_for("payment discount flow", budget=4000, consumer="arch")
          │
          ├── Channel 1: NetworkX Graph Walk
          │   - Resolve "payment discount flow" → nearest node qname
          │   - BFS up to depth=3 from that node
          │   - Score each neighbor: confidence × PageRank boost × distance decay
          │   - Collect up to 100 nodes
          │
          ├── Channel 2: FTS5 Keyword Search
          │   - nodes_fts: search "payment discount flow" → BM25 ranked results
          │   - code_fts: search function bodies for same query
          │   - Dedup against Channel 1 results
          │
          └── Channel 3: LanceDB Vector Search (if available)
              - Encode query with all-MiniLM-L6-v2
              - ANN search in lance/ collection
              - Dedup against Channels 1+2
                    │
                    ▼
          Merge + Score across all channels
                    │
                    ▼
          Truncate to budget (4000 tokens = ~50 nodes at 80 tok/node)
                    │
                    ▼
          Serialize to markdown with provenance:
          ## [Function] HandleDiscount
          // source: emandate-service/internal/discount.go:142 @abc1234
          func HandleDiscount(ctx Context, req DiscountReq) ...
```

### Consumer Profiles

Different skills need different retrieval emphasis:

| Consumer | Graph | FTS5 | Vector | Use Case |
|----------|-------|------|--------|---------|
| `planner` | 50% | 30% | 20% | Structure/dependencies for planning |
| `arch` | 30% | 20% | 50% | Semantic similarity for architecture questions |
| `dev` | 30% | 40% | 30% | Keyword-heavy for code search |
| `user` | 35% | 25% | 40% | General queries |
| `default` | 40% | 30% | 30% | Balanced |

### Scoring Formula

Each node gets a relevance score:
```
score = (0.5 + confidence × 0.5)     # confidence base
      × max(0.2, 1.0 - distance × 0.15)  # distance decay (closer = better)
      + pagerank[node] × 1000            # importance boost
```

Nodes modified in the last 7 days get +0.2. Nodes with `urgency_score >= 0.7` get +0.3.

---

## 8. The 5-Phase Feature Pipeline

Every feature goes through up to 5 phases. Each phase is gated on the previous phase's
artifact file existing in `workspace/features/<slug>/`.

```
Phase -1: Brain-First (mandatory)
  │   python3 -m brain context "<feature>" -b 4000
  │   python3 -m brain search "<feature>" --type Feature
  │   If >= 3 high-confidence nodes exist → use Brain as primary context
  │
  ▼
Phase 1: Ideation
  │   Inputs: Slack threads, Drive docs, PRs, verbal brief, uploaded files
  │   Outputs: overview.md, overview.html (Mermaid diagrams)
  │   Brain writes: Feature, Requirement, ArchDecision, Signal, RiskItem nodes
  │   Gate for next: overview.md or overview.html exists
  │
  ▼
Phase 2: Solutioning
  │   Inputs: overview.md + live codebase (grep, file reads, git)
  │   Outputs: solution.md (code-level change specs + risk register)
  │   Key step: @Slash consulted before AND after solution design
  │   Brain writes: BusinessLogic, ArchDecision (0.85), RiskItem (with RPN)
  │   Gate for next: solution.md or solution.html exists
  │
  ▼
Phase 3: Tech Spec
  │   Inputs: overview + solution artifacts
  │   Outputs: tech-spec.md (15-section Razorpay format)
  │   Brain writes: Document node with REFERENCES edges to Requirements
  │   Gate for next: tech-spec.md exists
  │
  ▼
Phase 4: Implementation
  │   Inputs: solution.md → code diffs per service
  │   Outputs: feature branch commits + GitHub PR
  │   Quality gates: go fmt, go vet, go test, golangci-lint (Go); eslint, tsc (TS)
  │   Brain writes: PR node, Signal nodes per quality gate result
  │   Gate for next: implementation/ directory exists
  │
  ▼
Phase 5: E2E
    Inputs: PR + devstack
    Outputs: devtest-report.md + test artifacts
    Brain writes: Signal nodes with pass/fail per scenario
```

### The Open Questions Ledger

A key feature of Phase 1 output: every `overview.md` includes an "Open Questions
(Next Iteration)" ledger — unresolved questions with working assumptions and
suggested resolvers. When Solutioning runs, it consumes this ledger and marks
items resolved or escalates them as RiskItems.

### Mandatory Phase -1 (Brain-First)

Before ANY live codebase analysis, the brain is queried first:
```bash
python3 -m brain context "<topic>" -c arch -b 4000
python3 -m brain search "<topic>"
python3 -m brain search "" --type Feature
python3 -m brain search "" --type ProjectExpert
```

If 3+ high-confidence nodes (confidence ≥ 0.85) exist for the topic, the Brain
is the primary context source and live re-analysis is skipped or reduced. This
dramatically reduces the token cost on repeated analysis of the same features.

---

## 9. Project Expert System

Each of the 45 services has a **ProjectExpert** node in the brain. Experts start at
Level 1 (seeded by `brain init-experts`) and level up via XP earned from feature work.

### Expert Levels

| Level | Name | XP Required | What They Know |
|-------|------|-------------|---------------|
| L1 | Apprentice | 0 | Service exists, basic routing, language |
| L2 | Journeyman | 500 | Entry points, middleware chain, config flags, key data structures |
| L3 | Practitioner | 1,500 | Response pipelines, error patterns, ALL callers of shared utilities |
| L4 | Expert | 3,000 | Cross-service contracts, Splitz gate topology, deployment topology |
| L5 | Grand Master | 5,000 | @Slash tribal knowledge, all known bugs, historical decisions |

### XP Economy

```python
EXPERT_XP_REWARDS = {
    "deep_read":         +300,  # initial deep-read of the codebase
    "feature_analysis":  +200,  # used in an Ideation
    "solution_design":   +300,  # used in Solutioning
    "risk_finding":      +150,  # surfaced a risk that was real
    "user_confirmation": +100,  # user confirmed the expert's knowledge
    "slash_validation":  +50,   # @Slash agreed with expert's model
    "contradiction":     -200,  # code contradicted expert's claim
}
```

### Expert-Guided Code Tracing

At L3+, Solutioning uses expert knowledge to **start** the code trace instead of
grepping blind. The expert's `routing_pattern`, `response_pipelines`, and
`shared_utilities` fields pre-load the key code paths. Code still wins if it
contradicts the expert (−200 XP), but the trace is faster.

### Expert Storage (in brain.db)

ProjectExpert nodes store a JSON `expertise` blob:
```json
{
  "routing_pattern": "POST /v1/payments → pg-router native vs proxy based on Splitz",
  "middleware_chain": ["AuthMiddleware", "RateLimitMiddleware", "LoggingMiddleware"],
  "config_mechanism": "Splitz (feature flags), Razorx (A/B), DCS (config service)",
  "key_data_structures": { "PaymentEntity": "payments table, 47 columns" },
  "response_pipelines": { "CreatePayment": "pg-router → api PHP → checkout-service" },
  "shared_utilities": { "GetOffer": ["offers-engine", "checkout-service", "api"] },
  "splitz_gates": { "pg-router.native-payment": "controls native vs proxy mode" },
  "known_bugs": [...],
  "slash_insights": [...],
  "historical_decisions": [...]
}
```

---

## 10. Skill Architecture

Nemesis has 19 skills, all defined as markdown files in `commands/`. Each skill is
a system prompt loaded into Claude Code as a slash command.

```
commands/
├── nemesis.md      # Orchestrator (phases, intent detection, dashboard)
├── brain.md        # Knowledge graph operations CLI wrapper
├── franco.md       # Universal data collector
├── review.md       # Multi-skill code review pipeline
├── devtest.md      # Live S2S E2E test runner
├── standup.md      # Daily standup + reports
├── implement.md    # Code generation + PR
├── explain.md      # Payment flow explainer
├── diagram.md      # Architecture diagrams (Canva → Mermaid → Excalidraw)
├── designer.md     # Full visual design workflow
├── silencer.md     # Google Doc tech spec generator
├── doc.md          # .docx generation (python-docx)
├── slash.md        # @Slash bot interaction
├── plan.md         # Feature planning
├── tickets.md      # DevRev/Jira ticket management
├── db-validator.md # Payment state + pre-deploy validation
├── scenario.md     # Test scenario generator
├── pipeline.md     # Pipeline orchestration controller
└── devtest.md      # Interactive E2E debug testing orchestrator
```

### Skill Invocation Chain

Skills invoke each other via the `Skill` tool. If resolution fails at runtime,
each skill documents a fallback protocol (direct MCP calls, CLI commands).

```
/nemesis (orchestrator)
    └── invokes: Skill("franco", "<source>")        # data collection
    └── invokes: Skill("slash", "<question>")       # @Slash queries
    └── invokes: Skill("engineering:code-review")   # code review
    └── invokes: Skill("engineering:system-design") # system design validation
    └── invokes: Skill("pre-mortem", "<context>")   # risk analysis
    └── invokes: Skill("implement", "<slug>")       # code generation
```

### The 15 Sub-Agents

Parallel sub-agents are spawned for heavy-lift work:

| Agent | Spawned By | Purpose |
|-------|-----------|---------|
| `brain-ingest-agent` | Brain, Franco | Parallel multi-source fetch without blocking |
| `nemesis-agent` | Nemesis | Heavy analysis, multi-skill pipelines |
| `project-expert-agent` | Solutioning | Deep codebase read per service |
| `review-agent` | Review | Parallel code review (one per dimension) |
| `silencer-agent` | Silencer | Parallel section generation for Google Docs |
| `standup-agent` | Standup | Parallel data collection (Slack + Cal + GitHub) |
| `implement-agent` | Implement | Per-service code generation |
| `test-gen-agent` | Implement | Test generation per service |
| `devtest-runner-agent` | DevTest | S2S execution with per-curl confirmation |
| `devtest-observer-agent` | DevTest | Parallel pod log watcher |
| `learn-agent` | Brain | Batch knowledge flush |
| `e2e-expert-agent` | E2E | E2E test design specialist |

---

## 11. Models and Embedding Stack

### LLM (Orchestration & Skills)

**Claude Sonnet** is the primary model for all skill execution. Nemesis skills are
loaded as system prompts in Claude Code — the model powering them is whatever model
the user has selected in their Claude Code session (typically Claude Sonnet 4.6 or 4.8).

No model version is hardcoded in the Python codebase (`brain/`). The Python layer
is model-agnostic — it only handles storage, retrieval, and algorithms.

Specific model IDs available in Claude Code:
- `claude-sonnet-4-6` — cost-efficient alternative for simpler tasks
- `claude-haiku-4-5` — cheap sub-agent work (light agents)

### Embedding Model (Vectors)

**`all-MiniLM-L6-v2`** via `sentence-transformers`:
- 384-dimensional embeddings
- Fast inference (~10ms per chunk on CPU)
- Good semantic similarity for code: function names, comments, variable names
- Model loaded from HuggingFace on first use; cached locally by sentence-transformers

Config:
```python
embedding_model: str = "all-MiniLM-L6-v2"
embedding_dim: int = 384
```

Embeddings are stored in LanceDB (`workspace/lance/`). The collection is named
`nemesis_code`. Each entry: `{node_id, node_type, chunk_text, vector[384]}`.

### @Slash (External Oracle)

@Slash is the Razorpay internal AI assistant (Slack bot user `U0AK4Q67HEY`, channel
`C0B3U3Z2JG1`). It's accessed via Slack MCP and has tools not available to Nemesis:
Coralogix (log search), AWS CLI, kubectl, Splitz, Watchtower (deploy tracker).

@Slash responses are stored as Signal nodes with confidence 0.85 — higher than raw LLM
extraction (0.7) because @Slash has codebase access tools, but lower than user-confirmed
facts (1.0) because @Slash is not infallible.

---

## 12. Design Decisions — What Was Evaluated and Rejected

This section documents the key architectural choices at each layer and explains what
was tried first, what was rejected, and why the current approach was chosen.

### 12.1 Knowledge Graph Storage: SQLite vs. Dedicated Graph DB

**Evaluated**: Neo4j, DGraph, ArangoDB

**Rejected because**:
- Neo4j requires a separate server process (JVM, ports, Docker)
- Cypher is a different query language — skills would need to embed both SQL and Cypher
- At 700K nodes and 733K edges, Neo4j's query performance advantage over SQLite+NetworkX
  is minimal (< 10ms difference), but the operational overhead is constant
- Migrations are harder: changing a Neo4j schema requires Cypher migration scripts
- `brain.db` is a single file that can be backed up with `cp`; Neo4j is a directory

**Chosen**: SQLite (WAL mode) + NetworkX (in-memory).

SQLite handles persistence, SQL queries, FTS5, and ACID. NetworkX handles graph
algorithms. The split is clean: SQLite is the source of truth, NetworkX is a
derived in-memory view rebuilt at startup (~2s). If NetworkX is out of sync,
`brain refresh` re-reads from SQLite.

WAL mode (Write-Ahead Logging) allows concurrent reads during writes — critical
because multiple skills may be reading context while a learning flush is writing new nodes.

### 12.2 Vector Search: Qdrant vs. LanceDB

**Evaluated**: Qdrant (embedded mode), Weaviate, Pinecone, ChromaDB

**Qdrant was used first** (see `scripts/rubick_vectors.py`):
- Qdrant embedded mode worked but still runs a separate gRPC/REST server in-process
- Startup adds ~500ms even in embedded mode
- Collection management requires explicit schema creation
- `qdrant-client` is a heavier dependency (~50 MB)

**Rejected Qdrant because**:
- Nemesis is invoked per-session (not a persistent server); paying 500ms startup every time
- Most skills never need vectors; they use FTS5 and graph walk
- Operational complexity: Qdrant data files in `qdrant_data/` required backup management

**Chosen**: LanceDB with lazy loading.
- Pure file-based (Arrow/Lance format), no server process
- 0 MB RAM at startup — only loaded on first `semantic_search()` call
- mmap-based ANN index: ~50-200 MB when loaded, ~2ms queries
- Single dependency: `lancedb>=0.4` (commented out in requirements.txt — opt-in)
- Files at `workspace/lance/` — backed up with the same `cp brain.db lance/` workflow

### 12.3 In-Memory Cache: Redis/Memcache vs. NetworkX

**Evaluated**: Redis, in-process LRU dict cache, Memcache

**Redis/Memcache were considered** for caching graph traversal results:
- Cache `who_calls("HandlePayment")` → `["ProcessOffer", "ApplyDiscount", ...]`
- This seemed attractive: repeated BFS over the same starting nodes is common

**Rejected because**:
- Redis requires a server; Memcache requires a server
- The real bottleneck was not BFS time (NetworkX is ~5ms) but cold-start graph load
- Once the NetworkX DiGraph is loaded at startup (~2s), every BFS is < 10ms in Python
- The graph changes as the brain learns new edges — cache invalidation with Redis
  adds complexity without meaningful speedup over already-fast in-memory operations
- SQLite WAL handles concurrent reads natively

**Chosen**: NetworkX DiGraph loaded into RAM at startup. The entire 733K-edge graph
fits in ~150 MB. Every subsequent graph query (BFS, PageRank, dead code, impact) runs
in Python on the in-memory graph. No external cache process needed.

**Key insight**: The graph is not a cache of SQL results — it IS the query surface.
NetworkX is a proper graph library, not a key-value cache. PageRank, connected
components, and shortest-path algorithms do not have SQL equivalents.

### 12.4 Context Retrieval: BFS-Only vs. Hybrid

**Old approach** (`scripts/rubick_context.py`): Pure graph BFS from a seed node.
This worked but had a major blind spot: **disconnected but lexically related nodes**
were invisible.

Example: A function named `applyInstantDiscount` in `offers-engine` might not be
connected to a Feature node for "instant discount" via any graph edge (because the
feature was created manually, not from code analysis). BFS from the feature node
would never surface the function.

**Chosen**: 3-channel hybrid (graph walk + FTS5 + vector):
- Channel 1 (Graph): finds structurally connected nodes — the code that implements the feature
- Channel 2 (FTS5): finds lexically related nodes — anything with "discount" in name/body
- Channel 3 (Vector): finds semantically related nodes — functions that do similar things
  even with different names

The three channels are merged with consumer-specific weights and truncated to budget.


### 12.5 Code Extraction: Custom AST Parser vs. Graphify

**Old approach** (`scripts/ast_extractor.py`, 32KB):
- Custom regex + heuristic parsers for Go and PHP
- Hand-written Go function boundary detection (matched `func ` + braces)
- PHP class/method detection via `class ` / `function ` regex

**Problems encountered**:
- Go: anonymous functions, closures, function literals, multiline signatures — all broke
  the regex approach
- Go: method receivers (`func (r *Router) Handle(...)`) required special-casing
- PHP: PHP namespace resolution, trait usage, magic methods — edge cases everywhere
- TypeScript: generics, decorators, arrow functions — effectively impossible with regex

**Chosen**: Graphify (tree-sitter based, 36 languages):
- True AST parsing — handles all language constructs correctly
- Two-pass call graph: EXTRACTED (explicit calls found in AST) + INFERRED (type-based)
- Leiden clustering for automatic module grouping
- Outputs structured JSON that maps directly to Nemesis typed tables

### 12.6 Node Identity: Integer IDs vs. (type, name) Pairs

**Old approach** (`rubick.db`): Nodes had integer `id` PKs. Edges stored as
`(from_node_id INT, to_node_id INT)`.

**Problems**:
- To know what a node was, you had to JOIN `edges` with `nodes` on both sides
- Migration from rubick.db to brain.db required remapping thousands of integer IDs
- String `node_id` like `Function:emandate-service.HandleRecurring` is self-describing;
  integer `14821` is not

**Chosen**: `(type, name)` as the logical identity for all edges:
```sql
edges(from_type, from_name, to_type, to_name, edge_type)
```

No JOIN required to know what an edge connects — it's all in the edge row itself.
Human-readable, survives schema migrations without ID remapping.

### 12.7 Separate Scripts vs. Unified Package

**Old approach**: 15+ standalone `rubick_*.py` scripts (~600KB total):
- `rubick_graph.py` (141KB) — graph operations
- `rubick_context.py` (33KB) — context retrieval
- `rubick_vectors.py` (15KB) — vector search
- `rubick_learn.py` (20KB) — learning pipeline
- `rubick_ingest.py` (27KB) — data ingestion
- etc.

**Problems**:
- Each script imported `brain_config` and initialized its own SQLite connection — multiple
  connections to the same database risked WAL lock contention
- Circular imports were common (rubick_context imported rubick_graph which imported rubick_context)
- No single entry point — skills had to know which script to call for each operation
- Testing was nearly impossible — each script was 500+ lines with mixed logic and CLI

**Chosen**: The `brain/` Python package with `BrainAPI` as the single entry point:
- One SQLite connection per process (thread-safe with `check_same_thread=False`)
- Clean module boundaries: `graph/engine.py`, `context/retrieval.py`, `memory/engine.py`
- Single import: `from brain.api import BrainAPI`
- CLI via `python3 -m brain <command>` — all commands route through `brain/cli.py`

### 12.8 Per-Skill MCP Calls vs. Franco Centralization

**Old approach**: Each skill made its own MCP calls directly. `/standup` called Slack
MCP; `/review` called GitHub MCP; `/ideation` called Drive MCP.

**Problems**:
- No dedup — the same Slack thread could be fetched by multiple skills, creating
  duplicate Signal nodes
- No provenance — skills didn't always record what they fetched into the brain
- Sync cursors weren't checked — the same email thread was re-processed on every run
- Different skills used different normalization logic for the same source

**Chosen**: Franco (`/franco`) as the single ingestion entry point, using the
two-phase pattern. All data collection goes through Franco:
- Dedup on `(source_type, source_id)` digest
- Provenance recorded in every node
- Sync cursors prevent re-processing
- Normalized schema (FrancoDocument) regardless of source

### 12.9 The @Slash Discovery

A non-technical but important decision: @Slash was discovered to be the most reliable
source for cross-project Razorpay knowledge. During the DFB analysis (offer discounts),
@Slash found a BLOCKER (the pg-router proxy path fix, Fix 5) that manual code review
missed entirely.

This validated the "query @Slash before AND after solutioning" protocol — not optional,
not a nice-to-have, but a mandatory step in Solutioning Phase 2. @Slash has observability
tools (Coralogix, Watchtower) that the LLM does not, giving it visibility into production
behavior that code review cannot replicate.

**Key lesson**: @Slash can't run SQL against the payments DB (Trino), but can answer
"what code paths touch this endpoint" and "what changed recently" — which is what
Solutioning actually needs.

---

## 13. Graph Schema — Node and Edge Types

### Node Types (33)

**Code Entities** (from Graphify / AST extraction):
`Function`, `Class`, `Module`, `Endpoint`, `DataStore`, `Test`, `KafkaTopic`, `File`

**People & Communications**:
`Person`, `Email`, `Meeting`, `Commit`

**Planning & Features**:
`Feature`, `Requirement`, `ArchDecision`, `BusinessLogic`, `RiskItem`, `UseCase`,
`Signal`, `Task`, `Plan`

**Documents & External**:
`Document`, `WebPage`, `PR`, `Branch`, `JiraIssue`

**System**:
`SlackChannel`, `ProjectExpert`, `ReviewResult`, `EvolutionPlan`, `KnowledgeEntity`,
`BusinessRule`

### Edge Types (32+)

**Code**: `CALLS`, `IMPORTS`, `CONTAINS_FUNC`, `CONTAINS_CLASS`, `CONTAINS_TEST`,
`FILE_IN_SERVICE`, `ROUTES_TO`, `READS`, `WRITES`, `TESTS`, `DEPENDS_ON`,
`PUBLISHES`, `CONSUMES`, `IMPLEMENTS`, `HAS_METHOD`

**Feature/Planning**: `HAS_REQUIREMENT`, `HAS_RISK`, `HAS_USE_CASE`, `IMPLEMENTS_FEATURE`,
`DECIDED_BY`, `SIGNAL_FOR`, `SPAWNED`, `ASSIGNED_TO`, `PLANNED_IN`

**Architecture**: `ENCODES`, `GOVERNS`, `EXPERT_ON`, `ANALYZED_BY`, `EVOLVES_TO`,
`PLANS_EVOLUTION`

**Cross-Reference**: `RELATES_TO`, `MENTIONED_IN`, `REFERENCES`, `VALIDATES`,
`MITIGATES`, `EXTRACTED_FROM`, `AUTHORED_BY`, `MONITORS`

### Retention Policy

| Node Type | Archived After |
|-----------|---------------|
| Feature, ArchDecision, Requirement, UseCase, BusinessLogic, RiskItem, ProjectExpert | Never |
| Commit, PR, Branch, JiraIssue | 365 days |
| Signal, Task, Meeting, Email | 180 days |
| WebPage | 90 days |
| Plan | 30 days |

---

## 14. The Learning Pipeline

Every skill interaction that discovers new knowledge stages it via `brain.learn()` and
persists it via `brain.flush()`. This is the write path for all non-code knowledge.

```
Skill discovers: "pg-router has a proxy path for payments"
         │
         ▼
brain.learn("nemesis", [LearningItem(
    node_type="ArchDecision",
    node_name="pg-router proxy mode",
    node_data={"decision": "...", "rationale": "..."},
    confidence=0.7,
    edges=[{"to_type": "Service", "to_name": "pg-router", "edge_type": "DECIDED_BY"}]
)])
         │  (staged in learning_ledger, status='staged')
         │
         ▼ (on flush)
brain.flush()
         │
         ├── Check: does "ArchDecision:pg-router proxy mode" already exist?
         │   If YES: merge data, MAX confidence, record confirmation
         │   If NO: create node
         │
         ├── Multi-source confidence bump:
         │   Source 1 (LLM extraction): confidence=0.7
         │   Source 2 (@Slash confirms): confidence=0.85
         │   Source 3 (user confirms): confidence=1.0
         │
         └── Add all edges
         │
         ▼
NetworkX cache refresh (if new edges were added)
```

### Confidence Lifecycle

```
0.7   → Initial LLM extraction (single source)
0.85  → Multi-source confirmation (LLM + @Slash, or LLM + code analysis)
1.0   → User explicitly confirms, or predicted risk materialized in production

Special cases:
0.5   → Disputed by user or contradicting evidence
0.2   → Rejected / known wrong
```

Confidence never decreases on upsert — the MAX is kept. To lower confidence, a
separate `update_node()` call is needed (e.g., when a user says "that's wrong").

### Decay

Nodes with `confidence < 0.85` older than 90 days are flagged by `brain.decay_report()`
for review. They're not auto-deleted — just surfaced so the user can confirm or dismiss.

---

## 15. Discovery Plugin Integration

The **Compass Discovery plugin** (or Claude Code's Discovery feature) is not currently
integrated into the Brain's ingestion pipeline as a source.

**What Discovery provides**: Automatically surfaces code changes, PRs, and related context
from connected repositories based on the current conversation.

**Current state**: The Brain uses direct GitHub CLI (`gh`) for code and PR data. Discovery
provides a similar capability but at the MCP layer — the LLM can see Discovery results
directly without them being persisted to `brain.db`.

**Integration path** (not yet implemented):

To load Discovery results into the Brain, the Franco two-phase pattern would apply:

```python
# Phase 1: Brain detects the source type
brain ingest "discovery://<session-id>/<result-id>"
# → returns: needs_fetch via mcp__discovery__get_results

# LLM fetches the Discovery results as JSON

# Phase 2: Ingest into brain
brain ingest-mcp discovery "<session-id>" --payload /tmp/discovery.json --feature my-feature
```

The key gap: Discovery results are session-scoped (they surface for the current
conversation, not persisted anywhere). For Brain integration, the LLM would need to
capture Discovery outputs and explicitly hand them to Franco.

**Recommendation**: If Discovery is actively surfacing useful context in your sessions,
use `/franco` to capture it into the Brain:

```text
/franco https://discovery-result-url --feature my-feature
```

Franco will detect the source type, fetch the content, normalize it, and persist
it as a Document or Signal node with appropriate edges.

**Pull-based vs. push-based**: The Brain is currently pull-based (you tell it what to
ingest). A push-based integration (Discovery automatically writes to brain.db) is
theoretically possible but would require the Discovery MCP to expose a write interface
— which it doesn't currently do.

---

## 16. Python Package Structure

```
brain/
├── __init__.py         # v2.0.0, "Nemesis Living Index Brain"
├── __main__.py         # Entry: python3 -m brain
│                       # Routes commands to cli.py
│
├── api.py              # BrainAPI — single entry point
│                       # Owns: engine, nxc, memory, context_retriever
│                       # Never instantiated twice per process
│
├── cli.py              # All CLI commands
│                       # ~700 lines, argparse-based
│                       # Commands: init, stats, search, context, impact,
│                       #           health, learn-flush, feature-*, doctor, ...
│
├── config.py           # BrainConfig dataclass + all constants
│                       # SEED_PROJECTS, SERVICE_DEPS, HYBRID_WEIGHTS,
│                       # EDGE_WEIGHTS, EXPERT_XP_*, SKILL_REGISTRY,
│                       # SOURCE_PATTERNS, REQUIRED_MCP, DRIVE_STORAGE_FOLDER_ID
│
├── types.py            # Enums + dataclasses
│                       # NodeType, EdgeType, Language, BrainName, QueryType
│                       # ExtractedFunction, ExtractedClass, ExtractedEndpoint
│                       # ContextResult, ImpactResult, HealthReport, LearningItem
│
├── graph/
│   ├── engine.py       # GraphEngine — all SQLite operations
│   │                   # Typed table upserts (batch), generic nodes/edges,
│   │                   # code_bodies, FTS5 search, stats
│   ├── schema.py       # DDL (CREATE TABLE / INDEX / TRIGGER / FTS5)
│   │                   # Schema version: "2.0.0"
│   │                   # WAL mode, 20K page cache
│   ├── networkx_cache.py # NetworkXCache — in-memory DiGraph
│   │                   # Methods: bfs_tree, callers, callees, pagerank,
│   │                   #          blast_radius, connected_components, refresh
│   └── algorithms.py  # impact_analysis, service_health, dead_code, test_gaps
│                       # Pure functions — take engine+nxc, return dataclasses
│
├── context/
│   └── retrieval.py    # ContextRetriever — 3-channel hybrid
│                       # _graph_walk, _fts_search, _vector_search
│                       # _score_node, _serialize_node, _resolve_target
│                       # Returns ContextResult with sources/tokens/text
│
├── memory/
│   └── engine.py       # MemoryEngine — learning + sync + slash
│                       # record(), flush(), status(), decay_report()
│                       # get_sync_cursor(), update_sync_cursor()
│                       # store_slash(), recall_slash()
│
├── semantic/
│   └── __init__.py     # LanceDB lazy loader (stub, loaded on demand)
│
├── ingest/
│   └── __init__.py     # Ingestion coordinator (stub)
│
└── migration/
    └── migrate.py      # rubick.db → brain.db
                        # 6-phase migration: services, functions/classes/tests,
                        # modules/endpoints/datastores, workflow nodes, edges, code bodies
```

### Key Design Constraints

1. **`brain/` never calls MCPs** — only the LLM skill layer does
2. **Single SQLite connection per process** — `check_same_thread=False`, WAL mode handles concurrency
3. **`brain.db` is write-free by default** — reads are zero-cost; writes only on explicit `learn/flush/add`
4. **Confidence never decreases on upsert** — `MAX(existing, new)` is kept; only explicit updates lower it
5. **Dedup by `(type, name)`** — the same node discovered twice merges, never duplicates
6. **NetworkX is derived state** — rebuilt from SQLite; if corrupt, `brain refresh` resets it
7. **LanceDB is optional** — all skills work without it; vectors add semantic similarity but don't replace graph+FTS5

---

*Last updated: 2026-06-25 | Schema: v4.0 | Brain package: v2.0.0*
