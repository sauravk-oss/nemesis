---
name: rubick-graph-schema
description: Unified knowledge graph schema v4.0 for Nemesis v2 Rubick — 32 node types, 51 edge types, code bodies + vector search
version: "4.0"
---

# Graph Schema — Rubick Knowledge Graph (v4.0)

Single cross-project `rubick.db` replacing per-project graphify.db files.
Managed by `scripts/rubick_graph.py`.

---

## Node Types (31)

### Inherited from Nemesis v1/v2 (19)

#### 1. Project
Root node per analyzed project.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `slug` | string | yes | Project identifier |
| `name` | string | yes | Human-readable name |
| `path` | string | no | Filesystem path to repo |
| `url` | string | no | Git remote URL |
| `language` | string | no | Primary language |
| `framework` | string | no | Detected framework |
| `role` | string | no | "primary" or "ecosystem" |
| `schema_version` | string | no | Graph schema version |

#### 2. Function
A code function or method.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `name` | string | yes | Function name |
| `file` | string | yes | File path relative to repo root |
| `line` | int | no | Starting line number |
| `complexity` | int | no | Cyclomatic complexity |
| `params` | json | no | Parameter names and types |
| `returns` | json | no | Return types |
| `is_exported` | bool | no | Publicly accessible |
| `is_method` | bool | no | Method on a type |
| `receiver` | string | no | Receiver type (methods) |
| `project_slug` | string | no | Owning project |

#### 3. Class
A struct, class, interface, or enum.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `name` | string | yes | Type/class name |
| `file` | string | yes | File path |
| `line` | int | no | Starting line |
| `kind` | string | no | "struct", "class", "interface", "enum" |
| `fields` | json | no | Field names |
| `is_exported` | bool | no | Publicly accessible |
| `project_slug` | string | no | Owning project |

#### 4. Module
A package or import unit.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `name` | string | yes | Package/module name |
| `path` | string | no | Module path |
| `is_external` | bool | no | Third-party dependency |
| `version` | string | no | Version constraint |
| `project_slug` | string | no | Owning project |

#### 5. Endpoint
An API route or handler.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `method` | string | yes | HTTP method |
| `path` | string | yes | Route path |
| `handler` | string | no | Handler function name |
| `file` | string | no | File path |
| `auth_required` | bool | no | Auth enforced |
| `middleware` | json | no | Applied middleware |
| `project_slug` | string | no | Owning project |

#### 6. DataStore
A database table, collection, or cache.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `name` | string | yes | Table/collection name |
| `type` | string | no | "sql_table", "nosql_collection", "cache", "queue" |
| `engine` | string | no | "mysql", "postgres", "redis", "kafka" |
| `primary_key` | string | no | Primary key field |
| `indexes` | json | no | Index names |
| `project_slug` | string | no | Owning project |

#### 7. Config
A configuration entry.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `key` | string | yes | Configuration key |
| `default_value` | string | no | Default value |
| `source` | string | no | "env", "file", "flag", "remote" |
| `is_secret` | bool | no | Contains sensitive data |
| `project_slug` | string | no | Owning project |

#### 8. Test
A test function.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `name` | string | yes | Test function name |
| `file` | string | yes | Test file path |
| `line` | int | no | Starting line |
| `kind` | string | no | "unit", "integration", "e2e", "benchmark" |
| `targets` | json | no | Functions being tested |
| `project_slug` | string | no | Owning project |

#### 9. Person
A team member, email sender, Slack user, or git author.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `name` | string | yes | Display name |
| `email` | string | no | Email address |
| `slack_id` | string | no | Slack user ID |
| `role` | string | no | Inferred role |
| `first_seen` | datetime | no | First appearance |

#### 10. Task
A ticket, action item, or work item.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `title` | string | yes | Task title |
| `status` | string | no | "open", "in_progress", "done", "blocked" |
| `priority` | string | no | "P0"-"P3" |
| `source` | string | no | "jira", "linear", "email", "slack", "manual" |
| `due_date` | datetime | no | Due date |
| `estimated_hours` | float | no | Estimated work hours (default 1.0) |
| `urgency_score` | float | no | 0.0-1.0 (default 0.4) |
| `stakeholder_score` | float | no | 0.0-1.0 (default 0.5) |
| `action_type` | string | no | "blocks_others", "needs_response", "fyi" |
| `calendar_event_id` | string | no | Linked calendar event |
| `plan_status` | string | no | "scheduled", "deferred", "unschedulable", "completed" |
| `assignee` | string | no | Assignee email |
| `project_slug` | string | no | Owning project |

#### 11. Email
An email thread.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `thread_id` | string | yes | Gmail thread ID |
| `subject` | string | no | Thread subject |
| `date` | datetime | no | Most recent message date |
| `message_count` | int | no | Messages in thread |
| `has_decisions` | bool | no | Decisions extracted |
| `has_action_items` | bool | no | Action items extracted |
| `body` | string | no | Message body (stripped on archive) |
| `raw_metadata` | string | no | Platform-specific JSON (stripped on archive) |

#### 12. Commit
A git commit.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `hash` | string | yes | Full SHA |
| `short_hash` | string | no | 7-char hash |
| `subject` | string | no | Commit message subject |
| `date` | datetime | no | Commit date |
| `files_changed` | int | no | Files changed count |
| `insertions` | int | no | Lines added |
| `deletions` | int | no | Lines removed |
| `pr_ref` | string | no | Associated PR number |
| `diff` | string | no | Diff content (stripped on archive) |
| `raw_metadata` | string | no | Platform JSON (stripped on archive) |
| `project_slug` | string | no | Owning project |

#### 13. Meeting
A calendar event.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `title` | string | yes | Meeting title |
| `date` | datetime | no | Start time |
| `duration_minutes` | int | no | Duration |
| `recurrence` | string | no | Recurrence pattern |
| `type` | string | no | "standup", "planning", "review", "ad-hoc" |
| `participants` | json | no | Participant list (stripped on archive) |

#### 14. Document
An ingested document (Google Doc, PDF, etc.).

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `hash` | string | no | SHA-256 of content |
| `title` | string | yes | Document title |
| `source_url` | string | no | Original URL |
| `source_type` | string | no | "google_doc", "pdf", "sheet", "slide" |
| `ingested_at` | datetime | no | When ingested |
| `last_modified` | datetime | no | Last modified in source |
| `owner` | string | no | Owner email |

#### 15. Event
An audit log or operational event.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `type` | string | yes | "deploy", "incident", "config_change" |
| `timestamp` | datetime | no | When occurred |
| `actor` | string | no | Who triggered it |
| `description` | string | no | Human-readable description |
| `severity` | string | no | "info", "warning", "error", "critical" |
| `source` | string | no | Capture source |

#### 16. Plan
A generated plan snapshot. Forms chain via SUPERSEDES edges.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `plan_id` | string | yes | e.g. "plan_20260512_0917" |
| `scope` | string | no | "today", "week", "sprint" |
| `generated_at` | datetime | no | ISO-8601 timestamp |
| `capacity_status` | string | no | "healthy", "tight", "overcommitted" |
| `capacity_ratio` | float | no | task_hours / available_hours |
| `available_hours` | float | no | Free hours in scope |
| `task_hours` | float | no | Total estimated hours |
| `schedule_json` | string | no | Task-to-slot assignments (stripped on archive) |
| `conflicts_json` | string | no | Detected conflicts (stripped on archive) |
| `circular_deps_json` | string | no | Circular deps (stripped on archive) |

#### 17. Feature
A trackable feature/initiative. Never auto-archived.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `name` | string | yes | Feature name |
| `slug` | string | yes | URL-safe identifier |
| `status` | string | no | "proposed", "in_progress", "blocked", "shipped", "abandoned", "closed" |
| `started_at` | datetime | no | When work began |
| `shipped_at` | datetime | no | When deployed |
| `closed_at` | datetime | no | When closed |
| `abandoned_at` | datetime | no | When abandoned |
| `owner` | string | no | Primary owner email |
| `priority` | string | no | "P0"-"P3" |
| `description` | string | no | What this feature does |
| `acceptance_criteria` | string | no | Definition of done |
| `estimated_days` | float | no | Total estimated effort |
| `actual_days` | float | no | Actual effort |
| `project_slug` | string | no | Owning project |

#### 18. Decision
A recorded decision. Never auto-archived.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `title` | string | yes | Decision summary |
| `context` | string | no | Why needed |
| `outcome` | string | no | What was decided |
| `decided_at` | datetime | no | When |
| `decided_by` | string | no | Who |
| `source` | string | no | "meeting", "slack", "email", "pr_review" |
| `reversible` | bool | no | Can be undone |
| `project_slug` | string | no | Owning project |

#### 19. Signal
An ingested signal from any platform. 6-month retention.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `signal_type` | string | yes | "email", "slack_mention", "slack_thread", "git_commit", "pr_review", "calendar_event", "task_update" |
| `source_id` | string | yes | External ID (natural dedup key) |
| `content_summary` | string | no | LLM-generated 1-line summary |
| `raw_metadata` | string | no | Platform JSON (stripped on archive) |
| `timestamp` | datetime | no | When occurred |
| `urgency_score` | float | no | 0.0-1.0 |
| `action_required` | bool | no | Needs response |
| `processed` | bool | no | Planner acted on this |
| `project_slug` | string | no | Owning project |

---

### New in v3.0 (11)

#### 20. Branch
A git branch.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `name` | string | yes | Branch name |
| `base` | string | no | Base branch |
| `created_at` | datetime | no | Creation date |
| `status` | string | no | "active", "merged", "stale" |
| `raw_metadata` | string | no | Platform JSON (stripped on archive) |
| `project_slug` | string | no | Owning project |

#### 21. PR
A pull request.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `number` | int | yes | PR number |
| `title` | string | no | PR title |
| `state` | string | no | "open", "merged", "closed" |
| `author` | string | no | Author email/username |
| `created_at` | datetime | no | Creation date |
| `merged_at` | datetime | no | Merge date |
| `review_status` | string | no | "pending", "approved", "changes_requested" |
| `diff_summary` | string | no | Diff summary (stripped on archive) |
| `raw_metadata` | string | no | Platform JSON (stripped on archive) |
| `project_slug` | string | no | Owning project |

#### 22. WebPage
An ingested web page or URL.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `url` | string | yes | Page URL |
| `title` | string | no | Page title |
| `content_summary` | string | no | LLM summary |
| `raw_content` | string | no | Full content (stripped on archive) |
| `ingested_at` | datetime | no | When ingested |

#### 23. JiraIssue
A Jira/Atlassian issue.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `key` | string | yes | e.g. "PAY-1234" |
| `title` | string | no | Issue title |
| `status` | string | no | Jira status |
| `issue_type` | string | no | "story", "bug", "task", "epic" |
| `priority` | string | no | Jira priority |
| `assignee` | string | no | Assignee |
| `sprint` | string | no | Sprint name |
| `story_points` | float | no | Estimation |
| `raw_metadata` | string | no | Platform JSON (stripped on archive) |
| `project_slug` | string | no | Owning project |

#### 24. Requirement
A product/business requirement. Never auto-archived.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `title` | string | yes | Requirement title |
| `description` | string | no | Full description |
| `source` | string | no | Where extracted from |
| `priority` | string | no | "must", "should", "could", "wont" (MoSCoW) |
| `status` | string | no | "active", "implemented", "deprecated" |
| `project_slug` | string | no | Owning project |

#### 25. UseCase
A user flow or use case. Never auto-archived.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `title` | string | yes | Use case title |
| `actor` | string | no | Primary actor |
| `preconditions` | string | no | Entry conditions |
| `flow` | string | no | Main flow steps |
| `postconditions` | string | no | Exit conditions |
| `project_slug` | string | no | Owning project |

#### 26. BusinessLogic
An extracted business rule or domain invariant. Never auto-archived.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `rule` | string | yes | Rule statement |
| `source_file` | string | no | Where found in code |
| `source_line` | int | no | Line number |
| `domain` | string | no | Business domain |
| `confidence` | float | no | Extraction confidence 0.0-1.0 |
| `project_slug` | string | no | Owning project |

#### 27. RiskItem
A tracked risk or vulnerability. Never auto-archived.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `title` | string | yes | Risk title |
| `severity` | string | no | "critical", "high", "medium", "low" |
| `category` | string | no | "security", "performance", "reliability", "compliance" |
| `status` | string | no | "open", "mitigated", "accepted" |
| `mitigation` | string | no | Mitigation plan |
| `project_slug` | string | no | Owning project |

#### 28. EvolutionPlan
An architectural evolution plan. Never auto-archived.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `title` | string | yes | Plan title |
| `current_state` | string | no | Current architecture |
| `target_state` | string | no | Target architecture |
| `phases` | string | no | JSON of migration phases |
| `status` | string | no | "planned", "in_progress", "completed" |
| `project_slug` | string | no | Owning project |

#### 29. ArchDecision
An architecture decision record (ADR). Never auto-archived.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `title` | string | yes | ADR title |
| `status` | string | no | "proposed", "accepted", "deprecated", "superseded" |
| `context` | string | no | Context and problem |
| `decision` | string | no | Decision made |
| `consequences` | string | no | Positive/negative consequences |
| `alternatives` | string | no | Considered alternatives |
| `decided_at` | datetime | no | When decided |
| `project_slug` | string | no | Owning project |

#### 30. SlackChannel
A Slack channel tracked for signals.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `channel_id` | string | yes | Slack channel ID |
| `name` | string | no | Channel name (e.g. "#payments_emandate") |
| `purpose` | string | no | Channel purpose |
| `last_synced` | datetime | no | Last sync timestamp |

#### 31. ProjectExpert
A per-project expertise record. Never auto-archived. Grows with each feature analysis.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `project` | string | yes | Project slug (e.g. "pg-router") |
| `role` | string | yes | Domain role: "gateway", "payment-core", "platform", "monolith", "offers", "frontend", "infra", "comms", "risk", "settlements", "config", "domain", "recurring", "cross-border" |
| `level` | int | yes | Expertise level 1-5 |
| `xp` | int | yes | Experience points accumulated |
| `deep_read_at` | datetime | no | Last full deep-read timestamp |
| `expertise` | json | no | Structured knowledge: entry_points, routing_pattern, middleware_chain, config_mechanism, key_data_structures, response_pipelines, shared_utilities, upstream_contracts, downstream_contracts, splitz_gates, known_bugs, slash_insights, test_gaps, historical_decisions |
| `features_analyzed` | json | no | Array of feature slugs this expert contributed to |
| `contradictions_found` | int | no | Times expert knowledge was wrong (XP penalty events) |
| `confirmations` | int | no | Times expert knowledge was validated |

---

## Edge Types (49)

### Inherited from Nemesis v1/v2 — Code Layer (13)

| Edge | From | To | Description |
|------|------|----|-------------|
| `CALLS` | Function | Function | Direct function call |
| `IMPORTS` | Module | Module | Package import |
| `CONTAINS` | Class/Module | Function/Class | Containment |
| `IMPLEMENTS` | Class | Class | Interface implementation |
| `EXTENDS` | Class | Class | Inheritance/embedding |
| `ROUTES_TO` | Endpoint | Function | HTTP route → handler |
| `QUERIES` | Function | DataStore | Database operation |
| `TESTS` | Test | Function | Test covers function |
| `GATES` | Function | Endpoint | Auth/validation gate |
| `DEPENDS_ON` | Module/Function | Module/Function | Runtime dependency |
| `MODIFIED` | Commit | Function/Module | Code modification |
| `TRIGGERED` | Event | any | Event caused action |
| `SNAPSHOT_OF` | Event | any | Audit snapshot |

### Inherited — People & Communication (4)

| Edge | From | To | Description |
|------|------|----|-------------|
| `AUTHORED_BY` | Commit | Person | Commit authorship |
| `ASSIGNED_TO` | Task | Person | Task assignment |
| `ATTENDED` | Person | Meeting | Meeting attendance |
| `PERFORMED_BY` | Event | Person | Action performer |

### Inherited — Planning & Organization (5)

| Edge | From | To | Description |
|------|------|----|-------------|
| `BLOCKS` | Task | Task | Blocking relationship |
| `DUE_BEFORE` | Task | Task | Temporal ordering |
| `PART_OF` | any | Project | Project membership |
| `DISCUSSED_IN` | any | Email/Meeting | Topic in communication |
| `RELATES_TO` | any | any | General association |

### Inherited — Planner v2 (9)

| Edge | From | To | Description |
|------|------|----|-------------|
| `IMPLEMENTS_FEATURE` | Task | Feature | Task contributes to feature |
| `SPAWNED` | Feature | Task | Feature originated task |
| `DECIDED_BY` | Feature/Task | Decision | Decision in context |
| `SIGNAL_FOR` | Signal | Feature/Task | Signal relates to feature/task |
| `PLANNED_IN` | Task | Plan | Task scheduled in plan |
| `SUPERSEDES` | Plan | Plan | Newer plan replaces older |
| `BLOCKED_BY` | Feature/Task | Feature/Task | Blocked by dependency |
| `REVIEWED_IN` | Task | Signal | Code review signal |
| `MENTIONED_IN` | any | Signal | Entity mentioned in signal |

### New in v3.0 — Cross-Project & Architecture (18)

| Edge | From | To | Description |
|------|------|----|-------------|
| `EXPERT_ON` | ProjectExpert | Project | Expert specializes in this project |
| `ANALYZED_BY` | Feature | ProjectExpert | Feature was analyzed with this expert's knowledge |
| `CROSS_REF` | any | any | Cross-project reference (FTS5-detected) |
| `HAS_REQUIREMENT` | Feature/Project | Requirement | Feature/project has requirement |
| `HAS_RISK` | Feature/Project | RiskItem | Tracked risk |
| `HAS_USE_CASE` | Feature/Project | UseCase | Associated use case |
| `ENCODES` | Function/Class | BusinessLogic | Code encodes business rule |
| `GOVERNS` | BusinessLogic | Endpoint/DataStore | Rule governs resource |
| `OPENS_PR` | Person | PR | Person opened PR |
| `BRANCH_OF` | Branch | Project | Branch belongs to project |
| `TRACKS` | JiraIssue | Task/Feature | Jira tracks work item |
| `MONITORS` | SlackChannel | Project | Channel monitors project |
| `EVOLVES_TO` | ArchDecision | ArchDecision | ADR succession |
| `PLANS_EVOLUTION` | EvolutionPlan | Project | Evolution plan for project |
| `MITIGATES` | RiskItem | Feature/Endpoint | Risk mitigation target |
| `EXTRACTED_FROM` | BusinessLogic/Requirement | Document/WebPage | Source of extraction |
| `REFERENCES` | Document | any | Document references entity |
| `SYNCED_FROM` | Signal | SlackChannel | Signal came from channel |

---

## Edge Properties

All edges carry:

| Property | Type | Description |
|----------|------|-------------|
| `weight` | float | Relationship strength 0.0-1.0 |
| `created_at` | datetime | Edge creation time |
| `source` | string | Which skill/phase created this |
| `metadata` | json | Additional context |

### Type-Specific Edge Properties

**CALLS**: `call_count` (int), `is_conditional` (bool)
**QUERIES**: `operation` (read/write/delete/update), `query_type` (select/insert/update/delete/raw)
**TESTS**: `coverage` (full/partial/mock-only)
**SUPERSEDES**: `superseded_at` (datetime), `reason` (scheduled_refresh/manual_replan/conflict_resolved)
**SIGNAL_FOR**: `confidence` (float 0.0-1.0), `linked_by` (llm_classifier/manual/heuristic)
**CROSS_REF**: `similarity` (float 0.0-1.0), `detected_by` (fts5/manual)

---

## Internal Tables (non-graph)

### sync_state
Tracks incremental sync progress per source per project.

| Column | Type | Description |
|--------|------|-------------|
| `source_type` | string | "slack", "gmail", "calendar", "github", "jira" |
| `source_id` | string | Channel ID, label, repo slug, etc. |
| `project_slug` | string | Associated project (or "_global") |
| `last_sync_at` | datetime | Last successful sync |
| `cursor` | string | Platform-specific pagination cursor |
| `status` | string | "ok", "error", "rate_limited" |
| `error_msg` | string | Last error if any |

### provenance
Every node carries these columns for traceability:

| Column | Type | Description |
|--------|------|-------------|
| `source_type` | string | "ast", "slack", "gmail", "drive", "github", "jira", "manual", "llm" |
| `source_id` | string | External identifier |
| `ingested_at` | datetime | When inserted/updated |
| `confidence` | float | Extraction confidence 0.0-1.0 |

---

## Retention Policy

| Node Type | Archive After | Strip Fields |
|-----------|--------------|--------------|
| Plan | 30 days | schedule_json, conflicts_json, circular_deps_json |
| Signal | 180 days | raw_metadata |
| Task | 180 days | — |
| Meeting | 180 days | participants |
| Email | 180 days | body, raw_metadata |
| Commit | 365 days | diff, raw_metadata |
| Branch | 365 days | raw_metadata |
| PR | 365 days | diff_summary, raw_metadata |
| WebPage | 90 days | raw_content |
| JiraIssue | 365 days | raw_metadata |
| Feature | never | — |
| Decision | never | — |
| ArchDecision | never | — |
| Person | never | — |
| Project | never | — |
| Requirement | never | — |
| UseCase | never | — |
| BusinessLogic | never | — |
| RiskItem | never | — |
| EvolutionPlan | never | — |
| SlackChannel | never | — |
| ProjectExpert | never | — |

---

## Context Budget Weights

Edge types scored by relevance for `context_for()` retrieval:

| Edge | Weight | Rationale |
|------|--------|-----------|
| HAS_REQUIREMENT | 1.0 | Core to feature definition |
| HAS_RISK | 1.0 | Must surface risks |
| HAS_USE_CASE | 1.0 | Core to feature definition |
| IMPLEMENTS_FEATURE | 0.95 | Direct implementation link |
| TRACKS | 0.9 | Issue tracker link |
| DECIDED_BY | 0.85 | Decision context |
| SIGNAL_FOR | 0.8 | Recent signals |
| ENCODES | 0.8 | Business rule link |
| GOVERNS | 0.75 | Rule governance |
| DISCUSSED_IN | 0.7 | Communication context |
| SPAWNED | 0.7 | Feature → task origin |
| IMPLEMENTS | 0.7 | Interface implementation |
| OPENS_PR | 0.65 | PR authorship |
| BRANCH_OF | 0.6 | Branch context |
| MENTIONED_IN | 0.4 | Weak mention |
| EXPERT_ON | 0.9 | Project expertise link |
| ANALYZED_BY | 0.85 | Feature-expert analysis link |
| RELATES_TO | 0.3 | General association |
| PART_OF | 0.2 | Project membership |

Boosts: +0.2 for nodes modified in last 7 days, +0.3 if urgency_score >= 0.7.

---

## Schema Versioning

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-05-01 | Initial: 15 node types, 22 edge types |
| 2.0 | 2026-05-12 | Planner v2: +Plan/Feature/Decision/Signal nodes, +9 planning edges |
| 2.1 | 2026-05-13 | Feature lifecycle: +closed status, +closed_at/abandoned_at |
| 3.0 | 2026-05-13 | Rubick: single rubick.db, +11 node types (Branch, PR, WebPage, JiraIssue, Requirement, UseCase, BusinessLogic, RiskItem, EvolutionPlan, ArchDecision, SlackChannel), +16 edge types, sync_state table, provenance columns, context budget engine, cross-project refs |
| 3.1 | 2026-05-19 | Project Expert System: +1 node type (ProjectExpert), +2 edge types (EXPERT_ON, ANALYZED_BY). Per-project experts with leveling (1-5), XP tracking, structured expertise storage (routing patterns, response pipelines, shared utilities, cross-service contracts). Solutioning evolution: Step 1.5 (Summon Project Experts). |
| 4.0 | 2026-05-25 | Anti-hallucination: +4 tables (code_bodies, code_chunks, file_extract_cache, code_fts). 368K code bodies stored with SHA-256 dedup. Qdrant vector search (rubick_vectors.py) with embedded mode + sentence-transformers. Hybrid retrieval: context_for_v2() combines BFS + Qdrant + FTS5 with consumer-specific weights. Provenance chain on every retrieved snippet. Eager hero init: 46 ProjectExpert nodes at Level 2 via rubick_heroes.py. |

### New Tables (v4.0)

#### code_bodies
One row per Function/Class/Test node — stores actual source code.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| node_id | INTEGER FK | References nodes(id) |
| project_slug | TEXT | Project identifier |
| file_path | TEXT | File path relative to repo root |
| start_line | INTEGER | First line of body |
| end_line | INTEGER | Last line of body |
| language | TEXT | Source language |
| body | TEXT | Full source code text |
| body_hash | TEXT | SHA-256 for dedup/change detection |
| byte_length | INTEGER | Body size in bytes |
| extracted_at | TEXT | ISO timestamp |

#### code_chunks
Embedding-ready segments (~500 tokens each) with 2-line overlap.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| body_id | INTEGER FK | References code_bodies(id) |
| node_id | INTEGER FK | References nodes(id) |
| chunk_index | INTEGER | 0-based chunk order within body |
| content | TEXT | Chunk text |
| start_line | INTEGER | First line of chunk |
| end_line | INTEGER | Last line of chunk |
| token_estimate | INTEGER | Approximate token count |

#### file_extract_cache
Incremental extraction — skip unchanged files.

| Column | Type | Description |
|--------|------|-------------|
| file_path | TEXT PK | File path |
| project_slug | TEXT PK | Project identifier |
| content_hash | TEXT | SHA-256 of file content |
| mtime | REAL | File modification time |
| file_size | INTEGER | File size in bytes |
| extracted_at | TEXT | ISO timestamp |
| function_count | INTEGER | Functions extracted from file |

#### code_fts (FTS5)
Full-text search over code bodies. Separate from nodes_fts.

```sql
CREATE VIRTUAL TABLE code_fts USING fts5(body, content=code_bodies, content_rowid=id);
```

### Qdrant Vector Store (external)

Collection `rubick_code` in `workspace/qdrant_data/` (embedded mode, no Docker).

| Field | Type | Description |
|-------|------|-------------|
| node_id | int | rubick.db nodes.id |
| node_type | string | Function, Class, Endpoint, etc. |
| node_name | string | Qualified function name |
| project_slug | string | Project identifier |
| file_path | string | Relative path within repo |
| line_number | int | Starting line |
| language | string | Source language |
| commit_sha | string | Git HEAD at embed time |
| chunk_type | string | "signature" or "body" |
| text_preview | string | First 200 chars |
| embedded_at | string | ISO timestamp |

Migration from v2.1: Run `rubick_graph.py migrate <db>` — adds new tables, columns, and bumps schema_version.
