# Nemesis v2 — Living Index Brain

The Living Index Brain absorbs knowledge from every source (Slack, Gmail, GitHub, Calendar, Drive, code)
and stores it in a unified knowledge graph — like IntelliJ's project index, but persistent, cross-service,
and enriched with human knowledge.

## PM Compass Profile
- **Role**: IC — Backend Engineer
- **BU**: Domestic Online Payments
- **Group**: Payments
- **POD**: Emandate / Recurring Payments
- **Products**: emandate-service, offers-engine, Nemesis Agent
- **Manager**: --
- **Email**: saurav.k@razorpay.com
- **Timezone**: Asia/Kolkata
- **Schema**: v3.1
- **Initialized**: 2026-05-13

## Architecture

Nemesis v2 is a multi-skill system powered by the **Living Index Brain** (`brain/` package).

```
                    BrainAPI (brain/api.py)
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
   GraphEngine        ContextRetriever    MemoryEngine
   (SQLite+NetworkX)  (3-channel hybrid)  (learning+sync)
        │                  │                  │
   Typed tables       Graph walk          learning_ledger
   + generic nodes    + FTS5 search       slash_interactions
   + edges            + LanceDB (lazy)    sync_state
   + FTS5 indexes     Consumer weights    confidence evolution
   NetworkX DiGraph
   PageRank/BFS
```

```
workspace/brain.db   ← Single Living Index (~500-700 MB)
                       715K+ nodes, 733K+ edges, 45 projects
       │ context_for(budget=N)
  ┌────┼──────────┐
  ▼    ▼          ▼
/plan  /nemesis   /review   /explain   /standup   /franco   ...
```

**Only Brain calls MCPs** for data ingestion. Other skills query via `from brain.api import BrainAPI` or `python -m brain`.

## Directory Structure
```
nemesis_v2/
├── CLAUDE.md                   # This file
├── SKILL.md                    # Skill orchestrator
├── brain/                      # Living Index Brain package
│   ├── __init__.py             # Package init (v2.0.0)
│   ├── __main__.py             # `python -m brain <command>` entry point
│   ├── api.py                  # BrainAPI — single entry point for all operations
│   ├── cli.py                  # CLI: stats, search, context, impact, health, CRUD, learn, etc.
│   ├── config.py               # Central config (45 projects, service deps, budgets, weights)
│   ├── types.py                # Enums (33 node types, 32 edge types) + dataclasses
│   ├── graph/
│   │   ├── engine.py           # GraphEngine: SQLite typed tables + generic nodes + edges + FTS5
│   │   ├── schema.py           # DDL: 9 typed code tables + generic nodes + edges + FTS5 + memory
│   │   ├── networkx_cache.py   # In-memory NetworkX DiGraph (BFS, PageRank, impact, paths)
│   │   └── algorithms.py       # impact_analysis, service_health, dead_code, test_gaps
│   ├── context/
│   │   └── retrieval.py        # 3-channel hybrid retrieval (graph + FTS5 + vector)
│   ├── memory/
│   │   └── engine.py           # Learning pipeline, sync state, slash interactions
│   ├── semantic/               # LanceDB vector search (lazy-loaded)
│   ├── knowledge/              # Knowledge triplets (future)
│   ├── ingest/                 # Graphify adapter + ingestion pipeline (future)
│   └── migration/
│       └── migrate.py          # rubick.db → brain.db migration (5-phase)
├── schemas/
│   └── graph-schema.md         # v3.1: 33 node types, 32 edge types
├── scripts/
│   ├── rubick_doc.py           # Tech spec .docx generator (python-docx) — still active
│   └── _archive/               # Archived rubick_*.py scripts (replaced by brain/)
├── agents/
│   ├── brain-ingest-agent.md   # Parallel ingestion sub-agent
│   ├── nemesis-agent.md        # Nemesis analysis sub-agent
│   ├── learn-agent.md          # Batch knowledge flush sub-agent
│   ├── review-agent.md         # Parallel code review sub-agent
│   ├── silencer-agent.md       # Parallel doc section generation sub-agent
│   ├── standup-agent.md        # Parallel standup data collection sub-agent
│   ├── project-expert-agent.md # Per-project expert (deep-read + Brain storage)
│   ├── implement-agent.md      # Per-service code generation sub-agent
│   └── test-gen-agent.md       # Automated test generation sub-agent
├── commands/
│   ├── brain.md                # Brain Living Index skill (/brain) — central knowledge engine
│   ├── plan.md                 # Planner skill (/plan)
│   ├── nemesis.md              # Nemesis orchestrator (/nemesis)
│   ├── slash.md                # @Slash bot interaction (/slash)
│   ├── doc.md                  # Tech spec .docx generator (/doc)
│   ├── explain.md              # Payment flow explainer (/explain)
│   ├── silencer.md             # Google Doc tech spec generator (/silencer)
│   ├── review.md               # Code review & audit (/review)
│   ├── diagram.md              # Visual architecture diagrams (/diagram)
│   ├── standup.md              # Daily standup & reports (/standup)
│   ├── tickets.md              # Jira/DevRev ticket management (/tickets)
│   ├── db-validator.md         # Payment state & pre-deploy validator (/db-validator)
│   ├── franco.md               # Franco universal data collector (/franco)
│   ├── designer.md             # Designer visual design agent (/designer)
│   ├── scenario.md             # Test scenario generator (/scenario)
│   ├── devtest.md              # Interactive E2E debug testing orchestrator (/devtest)
│   ├── pipeline.md             # Pipeline orchestration controller (/pipeline)
│   └── implement.md            # Implementation engine — code gen + tests + PR (/implement)
├── config/
│   └── experts.json            # Project Expert config (17 roles, 45+ project mappings)
└── workspace/
    ├── brain.db                # Single Living Index knowledge graph
    ├── rubick.db               # Archived old graph (read-only backup)
    ├── lance/                  # LanceDB vector store (lazy-loaded)
    ├── features/               # Per-feature working directories
    └── repos/                  # 44+ cloned repos (~2.5 GB)
```

## Graph Stats (Current — Living Index v2)
```
Storage:   SQLite (typed code tables + generic workflow nodes) + NetworkX (in-memory) + LanceDB (lazy)
Nodes:     715,606    (524K functions, 117K tests, 37K classes, 32K modules,
                       2.8K datastores (554 with ER schema), 1.4K endpoints,
                       46 project experts, 103 arch decisions)
Edges:     733,278    (379K CONTAINS, 126K IMPORTS, 117K TESTS, 109K CALLS,
                       1.6K ROUTES_TO, 177 DEPENDS_ON, 66 CALLS_SERVICE,
                       290 KAFKA_TOPIC, 86 IMPORTS_LIB, 46 EXPERT_ON)
Code:      368,635 bodies, 397,643 chunks (stored in code_bodies + code_chunks tables)
FTS5:      code_fts (368K entries) + nodes_fts (715K entries) — separate virtual tables
Vectors:   LanceDB (lazy-loaded, workspace/lance/) — replaces Qdrant
NetworkX:  ~733K edges loaded at startup (~2s), all graph algorithms in-memory (~5ms)
Projects:  46         (4 primary, 8 core, 7 infra, 11 domain, 5 gateway,
                       3 support, 2 frontend, 2 ecosystem + 3 meta)
Experts:   46 at Level 2 — enriched with endpoints, handlers, tables, test coverage
Languages: Go (40 repos), PHP (1: api), TypeScript (2: dashboard, checkout), Proto (1: rpc)
DB Size:   ~500-700 MB (brain.db, single file)
RAM:       ~150 MB startup (SQLite + NetworkX), ~800 MB peak (with LanceDB loaded)
CLI:       `python -m brain <command>` — all operations
API:       `from brain.api import BrainAPI` — programmatic access from all skills
```

## Drive Storage
All persistent files live in the **nemesis** Google Drive folder:
- **Folder**: [nemesis](https://drive.google.com/drive/folders/1u1vLNkGY9CM8G8eBDe0DtEAjcT3mjBa5) (`1u1vLNkGY9CM8G8eBDe0DtEAjcT3mjBa5`)
- **Rubick Notebook**: `1HDFURcA3TsW64Xt-rALjERFfcCmBNPMnBGNgPwfxWCo` — human-readable graph backup
- **Rubick Sync Log**: `1kHCSs21KeDE_qIIliLuMZubz9QoaXf097tqW918yrfQ` — append-only sync history
- **Rule**: brain.db is truth; Drive is backup. New files → always use `DRIVE_STORAGE_FOLDER_ID` as parentId.

## Key Design Decisions
1. **Single brain.db** — one Living Index across all projects. SQLite typed code tables (9) + generic workflow nodes + edges + FTS5. Replaces rubick.db.
2. **Math is Code, Meaning is LLM** — Python (`brain/`) handles graph algorithms, scoring, BFS, budget truncation. LLM handles interpretation, entity extraction, summarization.
3. **Context budget engine** — `brain.context_for(target, budget=N)` returns relevance-scored text within token limit. 3-channel hybrid: graph walk + FTS5 + vector search.
4. **Only Brain calls MCPs** — Other skills query the graph via `from brain.api import BrainAPI` or `python -m brain`, never touching Slack/Gmail/Drive directly. **Exceptions**: `/slash` skill calls Slack MCP directly (dedicated @Slash interaction channel `claude.saurav`); `/doc` skill uses python-docx locally (no MCP needed for core function)
5. **Provenance on every node** — source_type, source_id, ingested_at, confidence
6. **sync_state table** — incremental sync with cursors per source per project
7. **Cross-project via FTS5** — `nodes_fts` + `code_fts` detect references across projects
8. **Retention per type** — Features/Decisions permanent; Signals 6mo; Plans 30d
9. **Drive as backup** — nemesis/ folder stores Notebook + Sync Log; brain.db is always the source of truth
10. **Full GitHub org access** — `gh` CLI fetches PRs/issues/code from any razorpay repo, not just seed repos. Clone-on-demand to `workspace/repos/` for deep analysis.
11. **DevRev for tickets** — replaces Jira. ISS-*/TKT-* IDs ingested as JiraIssue nodes with `source_type: "devrev"`
12. **brain refresh** — non-destructive re-seed (keeps permanent nodes, refreshes ephemeral data)
13. **brain reset** — factory reset to stage zero (destructive, requires confirmation)
14. **Nemesis is the orchestrator** — `/nemesis` is an intelligent natural-language orchestrator that routes to Ideation (overview), Solutioning (solution + risk), Tech Spec (docs), and Brain (knowledge). 3-phase feature lifecycle: Ideation (overview) → Solutioning (solution + risk analysis) → Tech Spec (docs). Delegates to specialists, synthesizes outputs, writes structured knowledge back to brain.db.
15. **Code extraction via Graphify** — `graphify` (36 tree-sitter languages) replaces custom AST parsers. Two-pass call graph (EXTRACTED + INFERRED), Leiden clustering. `python -m brain ingest-code` imports to typed tables.
16. **Self-learning via confidence** — Arch nodes created at confidence 0.7 (LLM-extracted), bump to 0.85 (review-validated), 1.0 (user-confirmed). context_for() prefers high-confidence nodes. `/nemesis validate` and `/nemesis learn` manage the feedback loop.
17. **Cross-project intelligence** — Shared DataStore/API detection, RELATES_TO edges across repos, impact propagation, Razorpay domain flow mapping (mandate lifecycle, offer evaluation, settlement, recurring payments).
18. **@Slash bot integration** — Brain `slash_store()`/`slash_recall()` manages interactions with @Slash (Razorpay knowledge bot, user `U0AK4Q67HEY`) via Slack channel `C0B3U3Z2JG1`. Queries cached in `slash_interactions` table + persisted as Signal nodes. Confidence 0.85 (higher than LLM, lower than user-confirmed).
19. **Slash Skill** — `/slash` is a dedicated reusable skill for @Slash bot interaction. Uses channel ID `C0B3U3Z2JG1` (display name: `claude-saurav`) — always by ID, never by name. Other skills invoke `/slash` via Skill tool; if Skill tool fails to resolve, they follow the /slash protocol directly as a documented fallback. Responses persist to brain.db via `BrainAPI.slash_store()`.
20. **Doc Creation Skill** — `/doc` generates .docx files locally using `scripts/rubick_doc.py` (python-docx). Razorpay Tech Spec 16-section template. Never uploads to Drive. Can generate from `/nemesis` output, Brain context, or manual content.
21. **Explainer Skill** — `/explain` answers payment flow questions using step0-5 explainer docs + Brain context. Generates .docx via Doc Skill. Persists UseCase/BusinessLogic nodes to Brain via learning pipeline.
22. **Learning Pipeline** — `brain.memory.engine` + `learning_ledger` table. Every skill interaction stages knowledge items via `brain.learn()`, then flushes to brain.db via `brain.flush()` as nodes + edges. Multi-source confirmation bumps confidence to 0.85. Dedup via (type, name) + FTS.
23. **Brain-First Query (Phase -1)** — All `/nemesis` commands check pre-existing Brain knowledge before @Slash or live analysis. If >= 3 high-confidence nodes exist, Brain is primary context and redundant work is skipped.
24. **Context Saving Protocol** — All `/nemesis` interactions persist knowledge back to Brain via `brain.learn()` + `brain.flush()`. Mandatory, not optional. Every invocation creates a Signal node.
25. **Review Skill** — `/review` is a comprehensive code review and audit agent. Orchestrates `engineering:code-review`, `compass:razorpay-api-review`, `engineering:testing-strategy`, `engineering:deploy-checklist`, and `atlassian` skills. Supports 6 commands: `pr`, `diff`, `audit`, `triage`, `checklist`, `security`. Applies 8 Razorpay domain checks automatically (idempotency, reconciliation, amount precision, callback ordering, PCI, rate limiting, timeouts, feature flags). Persists ReviewResult nodes and updates Requirement/RiskItem confidence via learning pipeline.
26. **Diagram Skill** — `/diagram` generates visual architecture diagrams from Brain graph data using **Canva MCP** (primary — professional polished output), Mermaid MCP (secondary — structural diagrams), and Excalidraw MCP (whiteboard). Supports 8 commands: `flow`, `arch`, `entity`, `impact`, `timeline`, `whiteboard`, `class`, `export`. Other skills (nemesis, doc, explain, silencer) invoke `/diagram` via Skill tool for embedded visuals.
27. **Standup Skill** — `/standup` auto-generates daily standups, weekly reports, and meeting prep by aggregating Slack plugin commands (standup, channel-digest, summarize-channel, find-discussions, draft-announcement), Calendar MCP, GitHub CLI, Google Tasks, and Brain context. Documented Slack exception (same pattern as /slash). Spawns `standup-agent` for parallel data collection.
28. **Tickets Skill** — `/tickets` is the primary consumer of all 5 Atlassian skills (spec-to-backlog, triage-issue, generate-status-report, capture-tasks-from-meeting-notes, search-company-knowledge). Bridges Brain knowledge graph with Jira/DevRev ticket tracking. Supports 9 commands: `create`, `from-spec`, `from-meeting`, `triage`, `status`, `search`, `milestone`, `link`, `sync`.
29. **Channel ID over name** — All Slack channel references use channel ID directly (e.g., `C0B3U3Z2JG1`), never search by name. Slack name resolution is unreliable: `claude.saurav` doesn't match the actual name `claude-saurav`, and search may not find private channels. Learned from DFB session channel_not_found failures.
30. **Primary MCP only** — Always prefer primary Slack MCP (`mcp__plugin_compass_slack-mcp__*`). Secondary MCP (`mcp__a82ca449__*`) requires separate user approval per call and has been rejected. Only attempt secondary for optional features (canvas creation) after explicit user consent.
31. **Queue-aware @Slash polling** — @Slash responds with queue acknowledgements ("Tasks ahead: N") before actual answers. Polling must: (a) distinguish acknowledgements from real answers, (b) extend intervals for deep queues (>50: 120s vs 60s), (c) support up to 10 polls (~10-20min window), (d) allow user-triggered re-poll when "responses are ready".
32. **Skill tool fallback** — `Skill("slash")` may fail to resolve at runtime. When this happens, the calling skill (arch, explain, review) follows the /slash protocol directly as an architectural exception: send to `C0B3U3Z2JG1`, poll, store via `BrainAPI.slash_store()`. This is documented, not a hack.
33. **Solution doc template** — `/doc solution` generates professional .docx with embedded Mermaid PNGs (rendered via kroki.io/mermaid.ink), @Slash citations, code diffs, cross-project impact tables. Different from tech spec — 15 sections focused on fix analysis, not feature design.
34. **@Slash-first validated** — @Slash found the pg-router BLOCKER (Fix 5) during DFB analysis that manual code review missed. Cross-project questions to @Slash are mandatory in /nemesis Phase 0, not optional.
35. **45-service graph** — Full Razorpay backend mapped via go.mod + config TOML + @Slash discovery. 117 DEPENDS_ON edges. Service roles: primary, core, infra, domain, gateway, support, frontend, ecosystem.
36. **Service dependency config** — `brain.config.SERVICE_DEPS` stores the known dependency graph. Used by `brain.seed_services()` to auto-create DEPENDS_ON edges without re-crawling.
37. **Ideation** — Feature understanding engine inside `/nemesis`. Takes Slack threads, doc links, verbal descriptions → produces As-Is flow, To-Be requirements, edge cases, open questions, domain risks, complexity estimate. Outputs `overview.md` + `overview.html` (Mermaid visual). Auto-persists Feature, Requirement, RiskItem, ArchDecision, UseCase, BusinessLogic, Signal nodes to Brain.
38. **Tech Spec** — Document generation engine, `/nemesis` Phase 3. Always creates `tech-spec.md` in `workspace/features/<slug>/` — **never a Google Doc**. Uses the 15-section format from the Razorpay reference doc ([DFB/CFB Instant Offer Discounts](https://docs.google.com/document/d/1KRGsDjmSD_djtC9dXm22IIe63kaoc-qz) — `tech-spec-dfb-instant-discount.docx`). Section 7 (Final Approach) is the deepest section with per-component sub-sections (7.x code diffs, API payloads, config blocks, DB schemas). Razorpay-first tool priority. Diagrams via **Canva MCP first** (professional quality), Mermaid fallback for structural diagrams.
39. **@Slash Pre/Post Validation** — Solutioning queries @Slash BEFORE and AFTER analysis. 5 pre-solution queries (discovery) + 4 post-solution queries (validation) with contradiction rule. Risk analysis is now integrated into Solutioning's output (solution.html includes risk register, amendments, rollout plan). Results stored in solution artifacts.
40. **Solutioning includes risk analysis** — Solutioning outputs solution.html containing both the solution design AND risk analysis (ER impact, risk register, required amendments, rollout plan). Single artifact = complete context for Tech Spec.
43. **Tech Spec @Slash verification** — Tech Spec runs 3-5 @Slash fact-check queries during doc generation (Step 0.5, before section content generation). Queries target Razorpay standards (tech spec template, NFRs, monitoring, testing requirements). Results enrich sections 9/11/13 and are cited in section 15 (Appendix). Not optional — verified facts > assumed facts.
44. **TECH_SPEC_TEMPLATE has 15 sections** — 15 sections matching the Razorpay reference doc format ([DFB/CFB Instant Offer Discounts](https://docs.google.com/document/d/1KRGsDjmSD_djtC9dXm22IIe63kaoc-qz)). Section mapping: 1.Problem Statement, 2.Introduction & Scope (2.1 Functional Req → 2.1.1 User Stories → 2.1.1.x with acceptance criteria), 3.NFRs, 4.Impact Assessment, 5.Assumptions & Dependencies, 6.As-Is Architecture, 7.Final Approach (7.x sub-sections per component), 8.DB Touchpoints, 9.Testing Strategy, 10.Observability & Monitoring, 11.Rollout Plan, 12.Rollback Plan, 13.Open Questions, 14.Risk Register, 15.Appendix. Output is always `workspace/features/<slug>/tech-spec.md`. Tech Spec uses this template for feature specs; SOLUTION_DOC_TEMPLATE (15 sections, 35 sub-sections) is reserved for bug-fix analysis docs only.
45. **Image embedding via embed-images command** — `rubick_doc.py embed-images` adds images directly to the real document, bypassing the cross-document rId bug in `add-section`. The `add-section` temp-doc-copy approach breaks image relationships. Always use `embed-images` AFTER all `add-section` calls, BEFORE `finalize`.
46. **Nemesis (Orchestrator)** — `/beastmaster` renamed to `/nemesis`. Nemesis is the intelligent natural-language orchestrator that routes to three specialist phases: Ideation (overview), Solutioning (solution + risk), Tech Spec (doc generation). Replaces the old interactive menu with intent detection from free-form input. Chat window IS the Nemesis interface — users speak naturally, Nemesis dispatches to the correct phase. Both CLI (Claude Code) and Web UI (Flask :5555) share the same orchestrator.
47. **Canva MCP is PRIMARY for diagrams** — All polished diagram outputs (architecture, flow, dependency, service map) use Canva MCP (`mcp__dde94166__generate-design`) as primary tool. Mermaid MCP is secondary for quick structural diagrams (sequence, ER, class, gantt). Excalidraw for whiteboard brainstorms only. Previous Mermaid-first approach produced basic, low-quality diagrams (12-32KB). Canva produces professional-grade visuals suitable for tech spec review.
48. **Two doc templates** — `TECH_SPEC_TEMPLATE` (15 sections, MD file at `workspace/features/<slug>/tech-spec.md`, **never a Google Doc**, for feature design specs) and `SOLUTION_DOC_TEMPLATE` (15 sections, for bug fix analysis). Tech Spec defaults to TECH_SPEC_TEMPLATE. Use `--template solution` only for targeted fix analysis docs. Both templates have rich sub-sections. The TECH_SPEC_TEMPLATE matches the DFB/CFB Razorpay reference doc format ([`1KRGsDjmSD_djtC9dXm22IIe63kaoc-qz`](https://docs.google.com/document/d/1KRGsDjmSD_djtC9dXm22IIe63kaoc-qz)).
49. **Project Expert System** — Per-project expert agents that deeply read codebases and store structured expertise in Brain as `ProjectExpert` nodes. Experts mapped by service role (gateway, payment-core, platform, etc.) covering all 45+ projects. Experts level up (L1 → L5) via XP earned from feature analyses. Config in `config/experts.json`, agent template in `agents/project-expert-agent.md`. Solutioning's Step 1.5 (Summon Project Experts) loads expert knowledge before code tracing — experts replace evolved Ideation steps at L3+.
50. **Solutioning Four Pillars** — Solutioning uses 4 context pillars: Brain Context (pre-existing knowledge), Code Tracing (live codebase analysis), Cross-Project Intel (@Slash + dependency graph), and Expert Knowledge (ProjectExpert prior knowledge). Expert Knowledge accelerates Code Tracing by front-loading routing patterns, response pipelines, shared utility callers, and Splitz gates. Code still wins if it contradicts expert knowledge (contradiction = −200 XP penalty + expert correction). Steps modified: 1.5 (Summon), 2 (Expert-Guided Trace), 4 (Expert-Aware Design), 5 (Expert Contracts for Blast Radius), 6e (Expert Growth after analysis).
51. **Expert leveling via XP** — ProjectExpert nodes track XP (0→5000+) and auto-level: L1 (0), L2 (500), L3 (1500), L4 (3000), L5 (5000). XP earned from: initial deep-read (+300), feature analysis (+200), solution design (+300), risk findings (+150), user confirmation (+100), @Slash validation (+50). XP lost from: contradicted knowledge (−200). L3+ experts can replace evolved Ideation steps (endpoint ownership, response construction, all-callers).
52. **@Slash has NO Trino access** — @Slash's data tools are observability/infra-focused: Coralogix (logs), AWS CLI, kubectl, Splitz, Watchtower (change tracker). @Slash cannot run SQL queries against the payments database. The original assumption that @Slash could fetch payment records via Trino was incorrect (validated 2026-05-23). Payment SQL validation requires either: Redash REST API (needs `REDASH_API_KEY`) or a Trino HTTP endpoint (not yet available).
53. **Watchtower MCP context** — Watchtower is a **deploy/config change tracker** (deployments, Splitz experiments, DCS configs, terminal/endpoint changes). It is NOT a payments DB query tool. It lives in @Slash's MCP harness — NOT in Claude Code's registry. To use from Claude Code, obtain HTTP endpoint + auth token from `#slash-dev` or `#platform-infra` and add `"mcpServers": {"watchtower-mcp": {...}}` to `~/.claude/settings.json`. The `@razorpay/watchtower-mcp` npm package is on Razorpay's internal registry (404 on public npmjs.org).
54. **DB Validator Skill** — `/db-validator` is a three-layer pre-deploy and payment state validation skill. Layer 1: Watchtower (deploy status, Splitz flags, DCS configs — PENDING credentials). Layer 2: @Slash + Coralogix (payment event logs — AVAILABLE NOW via `/slash` protocol to `C0B3U3Z2JG1`). Layer 3: Redash REST API (payment SQL formula validation — PENDING API key from `redash.razorpay.com/profile`). The `offer` subcommand specifically validates DFB + instant-discount formula: `payment_amount − fee + offer_discount = order_amount`. All validation results persisted as Signal nodes.
55. **Code bodies stored, not discarded** — Code extraction retains full function/class/test bodies in `code_bodies` table (368K bodies, 397K chunks). SHA-256 dedup prevents duplicate storage. Bodies enriched into context via `brain.context_for()`.
56. **Hybrid retrieval (brain.context_for)** — `brain.context.retrieval.ContextRetriever` combines NetworkX graph walk + FTS5 keyword match + LanceDB vector search. Consumer-specific weights: planner=graph-heavy, arch=vector-heavy, dev=FTS5-heavy. Graceful degradation to graph+FTS5 if vectors unavailable.
57. **LanceDB lazy vector search** — Replaces Qdrant. Disk-based mmap, ANN indexed (~2ms queries), ~50-200MB RAM. Lazy-loaded on first `semantic_search()` call — 0 MB at startup. Model: `all-MiniLM-L6-v2` (384 dims). Storage: `workspace/lance/`.
58. **Provenance chain on every snippet** — Every code snippet in `brain.context_for()` carries `// source: {file}:{line} @{commit_sha[:7]}`. Stale snippets get warnings, not invented code.
59. **Two-step bootstrap (experts are NOT seeded by `brain init`)** — `brain init` sets up dirs + schema, seeds the 45 services (DEPENDS_ON graph), and loads the 16-skill registry. It does **not** create ProjectExpert nodes. Experts are seeded separately by **`brain init-experts --level 1`** (idempotent: never downgrades an existing expert; merges data on level-up). Data sources from `config/sources.json` are registered by **`brain register-sources`**. The full bootstrap — `init` → `register-sources` → `init-experts --level 1` → bounded live L1 ingest — is orchestrated by **`/nemesis init`** (see commands/nemesis.md "System Command: /nemesis init"). Experts level up L1→L5 via XP from feature work (Decision #51); they are not eagerly deep-read at init time.
60. **code_fts separate from nodes_fts** — Code body FTS5 search (`code_fts`) is a separate virtual table from `nodes_fts`. Managed by triggers on `code_bodies` inserts/deletes. Both live in brain.db.
61. **Franco (Data Collector)** — `/franco` is the universal data collector. Takes any URL, ID, query, or file path → auto-detects source type → fetches via MCP/CLI/internal → normalizes → ingests via `brain.learn()` + `brain.flush()`. Two-phase fetch: Python prepares params, LLM makes MCP calls. Dedup on `(source_type, source_id)`. Other skills invoke Franco instead of making raw MCP fetch calls. Skill: `commands/franco.md`. Config: `brain.config`.
62. **Designer (Visual Architect)** — `/designer` is the full creative design workflow agent. MCP priority: Canva (professional) > Mermaid (structural) > Figma (reference) > Excalidraw (whiteboard) > Blade (Razorpay components). Supports iterative editing via Canva transactions, Figma imports, UI mockup generation, Blade component reference. Unlike `/diagram` (quick one-shot), Designer handles multi-round iteration with export + persistence. Skill: `commands/designer.md`.
63. **Franco two-phase fetch** — MCP-backed sources (Slack, Gmail, Drive, Figma, Calendar) use two phases: Phase 1 (Python) detects source and builds MCP tool params; Phase 2 (LLM) makes the actual MCP call and passes response back for normalization + ingestion via Brain API. CLI sources (GitHub, DevRev) and local files fetch directly in Phase 1.
64. **Franco as single entry point** — No skill should make raw MCP fetch calls for data collection. Ideation, Solutioning, Standup, Review all invoke `/franco` via `Skill("franco", "<source>")`. Franco handles detection, normalization, dedup, and persistence uniformly.
65. **DevTest Skill** — `/devtest` is an interactive PR-driven E2E debug testing orchestrator. Takes PR numbers → detects services + scenarios → deploys to sandbox → launches parallel agents (1 runner per scenario + 1 log observer + 1 validator) in a single message → streams results as they arrive → generates unified debug report. Primary test engine: `mcp__e2e-orchestrator__*`. Fallback: Chrome MCP checkout JS execution. Log capture: Kubernetes MCP (multi-pod) + @Slash Coralogix. 7 strict rules: no live keys, human gates at every phase, parallel launch, fail loud, log everything, report always. Input: `pr <repo>#<N> [...]`. Skill: `commands/devtest.md`. Agents: `agents/devtest-runner-agent.md`, `agents/devtest-observer-agent.md`.
66. **Implementation Phase (Phase 4)** — `/implement` is the code generation engine. 9-step pipeline: parse solution → drift detection → code generation → test generation → quality gates → integration check → PR creation → deploy checklist → brain persistence. Supports Go, PHP, TypeScript. Spawns per-service `implement-agent` and `test-gen-agent` for parallel execution. 5 interactive checkpoints. 10 safety rules (never push to main, never force-push, never commit secrets). Quality gates: `go fmt`, `go vet`, `go test`, `golangci-lint` (Go); `php -l`, `phpcs` (PHP); `eslint`, `tsc --noEmit` (TS). SLIT tests via `slit-generator-v2` skill. Gatekeeper skill for PR merge criteria.
67. **Continuous Dialogue Protocol** — All Nemesis phases use mandatory pause points with specific questions at decision points. Confidence < 0.7 triggers mandatory question. All Q&A stored as Signal nodes with `SIGNAL_FOR` edges to Feature. Per-phase question budgets: Ideation (5+3), Solutioning (4+4), Tech Spec (3+2), Implementation (5+2), E2E (2+1). Future feature runs recall prior Q&A to avoid re-asking.
68. **Razorpay Skill Ecosystem Integration** — Nemesis integrates 13+ Razorpay skills across phases: `product-management:brainstorm` (Ideation), `compass:reviewing-strategy` (Ideation+Solutioning), `engineering:system-design` (Solutioning), `engineering:code-review` (Solutioning+Implementation), `engineering:testing-strategy` (Solutioning+Implementation), `pre-mortem` (Solutioning risk analysis), `engineering:deploy-checklist` (Implementation), `quality-engineer` (Implementation+E2E), `gatekeeper` (Implementation PR validation), `slit-generator-v2` (Implementation SLIT tests), `tech-spec-generator` (Tech Spec validation).
69. **5-Phase Pipeline** — Nemesis pipeline expanded from 3 to 5 phases: Ideation → Solutioning → Tech Spec → Implementation → E2E. Implementation phase generates code, tests (unit + SLIT), runs quality gates, creates mergeable GitHub PRs. E2E enhanced with auto-code-gen for failure fixes, SLIT integration, Brain learning loop, gatekeeper merge criteria.
70. **RPN Risk Scoring** — Solutioning uses formal Risk Priority Number scoring: Severity (1-10) x Probability (1-10) x Detectability (1-10) = RPN. RPN > 200 requires mandatory mitigation. RPN > 500 blocks deployment. Pre-mortem skill invoked for structured risk discovery.

## Nemesis Mode Protocol

These rules apply **permanently** whenever `/nemesis` is active in a session. They are not optional and take precedence over any general Claude Code default behavior.

### Phase Order (immutable)
```
Phase -1  →  Phase 1    →  Phase 2          →  Phase 3    →  Phase 4
Brain-First  Ideation       Solutioning          Tech Spec      Implementation
(MANDATORY)  (Overview)   (Solution + Risk)      (Docs)         (Code + PR)
```

### Enforcement Rules
1. **Phase -1 is mandatory** — always query `brain.db` via `python -m brain context` before ANY live codebase analysis or @Slash query. If >= 3 high-confidence nodes exist, Brain is the primary context source.
2. **Sequential phases** — if the user requests a later phase without completing the prior one, check for the prerequisite artifact first:
   - Solutioning requires `workspace/features/<slug>/overview.md` or `overview.html` to exist
   - Tech Spec requires `workspace/features/<slug>/solution.html` or `solution.md` to exist
   - Implementation requires `workspace/features/<slug>/solution.md` or `solution.html` to exist
   - If the artifact is missing, surface it clearly and offer to run the prerequisite phase first.
3. **No uncommitted phases** — every completed phase MUST flush its outputs to `brain.db` via `python -m brain learn-flush` before moving to the next phase. Persisting is not optional.
4. **Redirect general questions** — if the user asks a general coding or architecture question while `/nemesis` is active, identify which Nemesis phase handles it and route there. Do not answer directly outside the pipeline.
5. **Phase HUD in responses** — every Nemesis response must include a one-line phase status indicator showing which phase is active and which phases are complete.
6. **NEVER edit files without permission** — Nemesis NEVER writes, edits, or modifies any file without explicit user confirmation first. Always present the proposed change and ask "Should I apply this?" before touching any file. This applies to: command files, agent files, CLAUDE.md, workspace markdown artifacts (overview.md, solution.md, etc.), scripts, and any source code. **Exception: brain.db is always free** — all brain.db operations (reads, writes, node/edge upserts, learning pipeline flushes) never require permission and run automatically as part of any phase.
7. **Continuous dialogue** — at every mandatory pause point in every phase, ask a specific question about the decision at hand. Store all Q&A as Signal nodes. See Continuous Dialogue Protocol in nemesis.md.
8. **Implementation safety** — NEVER push to main/master. NEVER force-push. NEVER commit secrets. Always create feature branches. User MUST approve generated code before committing.

## Available MCP Tools (organized by category)
- **Slack**: 12 primary tools (`mcp__plugin_compass_slack-mcp__*`) + 16 secondary tools (`mcp__a82ca449__*` — canvas, rich thread reading)
- **Gmail**: 11 tools (`mcp__f22d0c2f__*` — search, get, draft, labels, threads)
- **Google Calendar**: 8 tools (`mcp__d285de92__*` — list, create, update, delete, suggest)
- **Google Drive**: 7 tools (`mcp__e20283d0__*` — read, search, create, copy, metadata)
- **Google Workspace**: 80+ tools (`mcp__plugin_compass_google-workspace__*` — Docs, Sheets, Slides, Forms, Tasks)
- **Canva**: 30+ tools (`mcp__dde94166__*` — generate-design, generate-design-structured, export-design, etc.) — PRIMARY diagram tool for professional output
- **Mermaid**: 1 tool (`mcp__7428c252__validate_and_render_mermaid_diagram`) — structural diagram rendering (secondary)
- **Excalidraw**: 2 tools (`mcp__3000b99d__create_view`, `read_me`) — whiteboard diagrams
- **Figma**: 15 tools (`mcp__f39bd90b__*` — design context, screenshots, libraries, code connect)
- **Blade MCP**: 8 tools (`mcp__plugin_compass_blade-mcp__*` — Razorpay design system)
- **Kubernetes**: 20 tools (`mcp__Kubernetes_MCP_Server__*` — get, describe, logs, apply, scale)
- **PowerPoint**: 10 tools (`mcp__PowerPoint__By_Anthropic__*` — create, add slides, export PDF)
- **Word**: 9 tools (`mcp__Word__By_Anthropic__*` — create, insert, format, export PDF)
- **PDF Viewer**: 7 tools (`mcp__plugin_pdf-viewer_pdf__*` — display, interact, save)
- **Scheduled Tasks**: 3 tools (`mcp__scheduled-tasks__*` — create, list, update)
- **Claude Preview**: 12 tools (`mcp__Claude_Preview__*` — browser preview for frontend verification)
- **Apple Notes**: 4 tools (`mcp__Read_and_Write_Apple_Notes__*`)
- **Canva**: 30+ tools (`mcp__dde94166__*` — design generation, editing, export)

## CLI Tools
- GitHub: `gh` CLI — PRs, issues, repos, code search, clone across entire razorpay org
- DevRev: `https://app.devrev.ai/razorpay/tasks` — ticket tracking (ISS-*/TKT-* IDs)

## Seed Projects (45 — from brain.config.SEED_PROJECTS)
| Role | Projects |
|------|----------|
| primary | emandate-service, offers-engine, rpc, payments-mandate |
| core | checkout-service, pg-router, payments-card, payments-upi, mozart, terminals, shield, api |
| infra | goutils, integrations-go, integrations-utils, ledger, splitz, stork, raven, metro, vault |
| domain | scrooge, settlements, charge-collections, subscriptions, reminders, magic-checkout-service, payments-cross-border, payments-bank-transfer, payment-methods, tokens, downtime-manager, optimizer-core |
| gateway | edge, relay, dcs, route, cms, bin-service, apm-service |
| support | cps, customer-service, governor-executor |
| frontend | dashboard (TS), checkout (TS) |
| ecosystem | batch, mock-gateway |

## Seed Slack Channels
- #payments_emandate
- #payments_cards_emandate_coe
- #emandate_alerts
- #slash-offers-engine
- #debugging-offers-with-slash
- #recurring_alerts
- **C0B3U3Z2JG1** (`claude-saurav`) — @Slash bot private channel. Always use ID, never search by name.
