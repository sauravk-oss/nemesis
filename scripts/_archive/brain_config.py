"""Central configuration for Nemesis v2 Rubick (Knowledge Graph).

Single source of truth for all tunable constants. No hardcoded values elsewhere.
Extends nemesis v1 planner_config with context budgets, ingestion, and cross-project settings.

Rubick — the memory agent that absorbs signals from Slack, Gmail, GitHub, Calendar,
Drive, and code — storing everything in a unified knowledge graph (rubick.db).
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

NEMESIS_ROOT: Path = Path(
    os.environ.get("NEMESIS_V2_ROOT", Path.home() / "Projects" / "Agents" / "nemesis_v2")
)
WORKSPACE: Path = NEMESIS_ROOT / "workspace"
SCRIPTS_DIR: Path = NEMESIS_ROOT / "scripts"
FEATURES_DIR: Path = WORKSPACE / "features"
LOG_DIR: Path = Path(
    os.environ.get("NEMESIS_LOG_DIR", Path.home() / ".nemesis_v2" / "logs")
)
TMP_DIR: Path = Path(os.environ.get("NEMESIS_TMP_DIR", "/tmp"))

RUBICK_DB_PATH: Path = WORKSPACE / "rubick.db"
WORKSPACE_JSON: Path = WORKSPACE / "workspace.json"

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_VERSION: str = "4.0"

# ---------------------------------------------------------------------------
# Priority Scoring Weights
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS: dict[str, float] = {
    "time_proximity": 0.35,
    "urgency_signals": 0.30,
    "action_required": 0.20,
    "stakeholder": 0.15,
}

WEIGHT_MIN: float = 0.10
WEIGHT_MAX: float = 0.50
WEIGHT_MAX_STEP: float = 0.05

ACTION_SCORES: dict[str, float] = {
    "blocks_others": 1.0,
    "needs_response": 0.7,
    "fyi": 0.2,
}

PRIORITY_LABELS: list[tuple[float, str]] = [
    (0.8, "Critical"),
    (0.6, "High"),
    (0.4, "Medium"),
    (0.0, "Low"),
]

TIME_PROXIMITY_BUCKETS: list[tuple[float, float]] = [
    (2.0, 1.0),
    (24.0, 0.6),
    (48.0, 0.3),
    (168.0, 0.1),
]

# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------

CAPACITY_HEALTHY_RATIO: float = 0.80
CAPACITY_TIGHT_RATIO: float = 1.00

# ---------------------------------------------------------------------------
# Slot Matching
# ---------------------------------------------------------------------------

SLOT_MIN_DURATION_MIN: int = 30
DEEP_WORK_MIN_DURATION_MIN: int = 60
PLAN_TASK_LIMIT: int = 200

# ---------------------------------------------------------------------------
# Working Hours
# ---------------------------------------------------------------------------

WORKING_HOURS_START: str = "09:00"
WORKING_HOURS_END: str = "19:00"
DEFAULT_TIMEZONE: str = "Asia/Kolkata"

# ---------------------------------------------------------------------------
# Refresh & Sync
# ---------------------------------------------------------------------------

MIN_REFRESH_INTERVAL_MIN: int = 60
PLAN_STALE_THRESHOLD_MIN: int = 120

SYNC_INTERVAL_QUICK_MIN: int = 60
SYNC_INTERVAL_FULL_MIN: int = 360

# ---------------------------------------------------------------------------
# Vector Search (Qdrant embedded mode)
# ---------------------------------------------------------------------------

QDRANT_DATA_PATH: Path = WORKSPACE / "qdrant_data"
QDRANT_COLLECTION: str = "rubick_code"
QDRANT_EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"
QDRANT_EMBEDDING_DIM: int = 384
QDRANT_BATCH_SIZE: int = 256
QDRANT_SEARCH_LIMIT: int = 20
QDRANT_SCORE_THRESHOLD: float = 0.35
QDRANT_MAX_BODY_CHARS: int = 2048

# Hybrid retrieval weights
HYBRID_WEIGHT_VECTOR: float = 0.4
HYBRID_WEIGHT_BFS: float = 0.35
HYBRID_WEIGHT_FTS5: float = 0.25

HYBRID_CONSUMER_WEIGHTS: dict[str, dict[str, float]] = {
    "planner": {"vector": 0.2, "bfs": 0.5, "fts5": 0.3},
    "arch":    {"vector": 0.5, "bfs": 0.3, "fts5": 0.2},
    "dev":     {"vector": 0.3, "bfs": 0.3, "fts5": 0.4},
    "user":    {"vector": 0.4, "bfs": 0.35, "fts5": 0.25},
}

# ---------------------------------------------------------------------------
# Context Budgets (tokens, approximate)
# ---------------------------------------------------------------------------

CONTEXT_BUDGET_DEFAULT: int = 2000
CONTEXT_BUDGET_PLANNER: int = 1500
CONTEXT_BUDGET_ARCH_INIT: int = 4000
CONTEXT_BUDGET_ARCH_PHASE: int = 1000
CONTEXT_BUDGET_DEV: int = 3000
CONTEXT_BUDGET_USER: int = 2000

TOKENS_PER_NODE_ESTIMATE: int = 80
BUDGET_HARD_CAP_RATIO: float = 1.1

# Context retrieval: edge type relevance weights for scoring
EDGE_RELEVANCE: dict[str, float] = {
    "HAS_REQUIREMENT": 1.0,
    "HAS_RISK": 1.0,
    "HAS_USE_CASE": 1.0,
    "IMPLEMENTS_FEATURE": 0.95,
    "TRACKS": 0.9,
    "DECIDED_BY": 0.85,
    "SIGNAL_FOR": 0.8,
    "ENCODES": 0.8,
    "GOVERNS": 0.75,
    "DISCUSSED_IN": 0.7,
    "SPAWNED": 0.7,
    "IMPLEMENTS": 0.7,
    "OPENS_PR": 0.65,
    "BRANCH_OF": 0.6,
    "MENTIONED_IN": 0.4,
    "RELATES_TO": 0.3,
    "PART_OF": 0.2,
}

FRANCO_SCHEDULED_SOURCES: list[dict] = [
    {"source_type": "slack_channel", "source_id": "C0B3U3Z2JG1", "interval_hours": 12},
    {"source_type": "slack_channel", "source_id": "#payments_emandate", "interval_hours": 6},
    {"source_type": "slack_channel", "source_id": "#payments_cards_emandate_coe", "interval_hours": 6},
    {"source_type": "slack_channel", "source_id": "#emandate_alerts", "interval_hours": 4},
    {"source_type": "slack_channel", "source_id": "#slash-offers-engine", "interval_hours": 6},
    {"source_type": "slack_channel", "source_id": "#debugging-offers-with-slash", "interval_hours": 6},
    {"source_type": "github_repo", "source_id": "razorpay/emandate-service", "interval_hours": 12},
    {"source_type": "github_repo", "source_id": "razorpay/offers-engine", "interval_hours": 12},
    {"source_type": "github_repo", "source_id": "razorpay/pg-router", "interval_hours": 24},
    {"source_type": "github_repo", "source_id": "razorpay/checkout-service", "interval_hours": 24},
]

RECENCY_BOOST_DAYS: int = 7
RECENCY_BOOST_SCORE: float = 0.2
URGENCY_BOOST_THRESHOLD: float = 0.7
URGENCY_BOOST_SCORE: float = 0.3

# ---------------------------------------------------------------------------
# Feature Lifecycle
# ---------------------------------------------------------------------------

FEATURE_STATUSES: list[str] = [
    "proposed", "in_progress", "blocked", "shipped", "abandoned", "closed",
]

FEATURE_VALID_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"in_progress", "abandoned", "closed"},
    "in_progress": {"blocked", "shipped", "abandoned", "closed"},
    "blocked": {"in_progress", "abandoned", "closed"},
    "shipped": {"closed"},
    "abandoned": {"proposed"},
    "closed": set(),
}

# ---------------------------------------------------------------------------
# Sync Limits
# ---------------------------------------------------------------------------

MAX_NEW_TASKS_PER_SYNC: int = 3
MAX_ACTIVE_TASK_EVENTS: int = 5
BRAIN_TASK_PREFIX: str = "[Brain Task]"

# ---------------------------------------------------------------------------
# Retention & Archival
# ---------------------------------------------------------------------------

RETENTION_DAYS: int = 180

ARCHIVE_AFTER_DAYS: dict[str, int] = {
    "Plan": 30,
    "Signal": 180,
    "Task": 180,
    "Meeting": 180,
    "Email": 180,
    "Commit": 365,
    "Branch": 365,
    "PR": 365,
    "WebPage": 90,
    "JiraIssue": 365,
    # Permanent (institutional memory) — -1 means never archive
    "Feature": -1,
    "Decision": -1,
    "ArchDecision": -1,
    "Person": -1,
    "Project": -1,
    "Requirement": -1,
    "UseCase": -1,
    "BusinessLogic": -1,
    "RiskItem": -1,
    "EvolutionPlan": -1,
    "SlackChannel": -1,
}

ARCHIVE_STRIP_FIELDS: dict[str, list[str]] = {
    "Plan": ["schedule_json", "conflicts_json", "circular_deps_json"],
    "Signal": ["raw_metadata"],
    "Meeting": ["participants"],
    "Email": ["body", "raw_metadata"],
    "Commit": ["diff", "raw_metadata"],
    "Branch": ["raw_metadata"],
    "PR": ["diff_summary", "raw_metadata"],
    "WebPage": ["raw_content"],
    "JiraIssue": ["raw_metadata"],
}

# ---------------------------------------------------------------------------
# Conflict Detection
# ---------------------------------------------------------------------------

NO_BREAK_CHAIN_MIN_MEETINGS: int = 3
NO_BREAK_CHAIN_MAX_GAP_MIN: int = 15

# ---------------------------------------------------------------------------
# LLM Defaults
# ---------------------------------------------------------------------------

DEFAULT_URGENCY_SCORE: float = 0.4
DEFAULT_STAKEHOLDER_SCORE: float = 0.5
DEFAULT_ESTIMATED_HOURS: float = 1.0

# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

INGEST_LLM_BUDGET_TOKENS: int = 200
INGEST_MAX_BATCH: int = 50
INGEST_DEDUP_KEY_FIELDS: tuple[str, ...] = ("source_type", "source_id")

SOURCE_PATTERNS: dict[str, str] = {
    "slack_thread": r"(?:https?://)?[\w.-]*slack\.com/archives/\w+/p\d+",
    "slack_channel": r"(?:https?://)?[\w.-]*slack\.com/(?:archives|channels?)/\w+/?$",
    "drive_doc": r"(?:https?://)?docs\.google\.com/document/d/[\w-]+",
    "drive_file": r"(?:https?://)?drive\.google\.com/(?:file/d|open\?id=)[\w-]+",
    "drive_folder": r"(?:https?://)?drive\.google\.com/drive/(?:u/\d+/)?folders/[\w-]+",
    "gmail_thread": r"(?:https?://)?mail\.google\.com/mail/.*#(?:inbox|all|sent)/[\w]+",
    "github_pr": r"(?:https?://)?github\.com/[\w.-]+/[\w.-]+/pull/\d+",
    "github_issue": r"(?:https?://)?github\.com/[\w.-]+/[\w.-]+/issues/\d+",
    "github_commit": r"(?:https?://)?github\.com/[\w.-]+/[\w.-]+/commit/[\da-f]+",
    "jira_issue": r"(?:https?://)?[\w.-]*atlassian\.net/browse/[\w]+-\d+",
    "devrev_task": r"(?:https?://)?app\.devrev\.ai/[\w.-]+/(?:tasks|works)/[\w-]+",
    "devrev_id": r"^(?:ISS|TKT|TASK)-\d+$",
    "jira_id": r"^[A-Z][A-Z0-9]+-\d+$",
}

URGENCY_KEYWORDS: list[str] = [
    "urgent", "blocker", "blocked", "P0", "hotfix", "ASAP", "asap",
    "today", "deadline", "critical", "incident", "outage", "downtime",
    "production", "prod issue", "breaking",
]

# ---------------------------------------------------------------------------
# Connectors
# ---------------------------------------------------------------------------

AVAILABLE_CONNECTORS: dict[str, dict] = {
    "calendar": {"name": "Google Calendar", "mcp_prefix": "list_events", "auth": "zero"},
    "gmail": {"name": "Gmail", "mcp_prefix": "search_threads", "auth": "zero"},
    "slack": {"name": "Slack", "mcp_prefix": "slack_search", "auth": "zero"},
    "drive": {"name": "Google Drive", "mcp_prefix": "read_file_content", "auth": "zero"},
    "github": {"name": "GitHub", "cli": "gh", "auth": "gh_cli"},
    "devrev": {"name": "DevRev", "cli": "gh", "auth": "gh_cli"},
    "linear": {"name": "Linear", "mcp_prefix": "linear_", "auth": "oauth"},
}

# ---------------------------------------------------------------------------
# GitHub — full org access via `gh` CLI
# ---------------------------------------------------------------------------

GITHUB_ORG: str = "razorpay"
GITHUB_CLONE_BASE: Path = NEMESIS_ROOT / "workspace" / "repos"
GITHUB_PR_FETCH_LIMIT: int = 25
GITHUB_ISSUE_FETCH_LIMIT: int = 15
GITHUB_PR_LOOKBACK_DAYS: int = 14

# ---------------------------------------------------------------------------
# DevRev — ticket tracking (replaces Jira)
# ---------------------------------------------------------------------------

DEVREV_ORG: str = "razorpay"
DEVREV_BASE_URL: str = "https://app.devrev.ai/razorpay"
DEVREV_TASKS_URL: str = "https://app.devrev.ai/razorpay/tasks"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FORMAT: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT: str = "%Y-%m-%dT%H:%M:%S"
LOG_MAX_DAYS: int = 30

# ---------------------------------------------------------------------------
# Workspace Seed (from parallel Slack/Drive/Gmail fetch)
# ---------------------------------------------------------------------------

SEED_PROJECTS: list[dict[str, str]] = [
    # --- Primary (Emandate/Recurring pod) ---
    {"slug": "emandate-service", "url": "https://github.com/razorpay/emandate-service", "role": "primary", "lang": "go"},
    {"slug": "offers-engine", "url": "https://github.com/razorpay/offers-engine", "role": "primary", "lang": "go"},
    {"slug": "rpc", "url": "https://github.com/razorpay/rpc", "role": "primary", "lang": "proto"},
    {"slug": "payments-mandate", "url": "https://github.com/razorpay/payments-mandate", "role": "primary", "lang": "go"},
    # --- Core Payment Flow (checkout → pg-router → method services → mozart → gateway) ---
    {"slug": "checkout-service", "url": "https://github.com/razorpay/checkout-service", "role": "core", "lang": "go"},
    {"slug": "pg-router", "url": "https://github.com/razorpay/pg-router", "role": "core", "lang": "go"},
    {"slug": "payments-card", "url": "https://github.com/razorpay/payments-card", "role": "core", "lang": "go"},
    {"slug": "payments-upi", "url": "https://github.com/razorpay/payments-upi", "role": "core", "lang": "go"},
    {"slug": "mozart", "url": "https://github.com/razorpay/mozart", "role": "core", "lang": "go"},
    {"slug": "terminals", "url": "https://github.com/razorpay/terminals", "role": "core", "lang": "go"},
    {"slug": "shield", "url": "https://github.com/razorpay/shield", "role": "core", "lang": "go"},
    {"slug": "api", "url": "https://github.com/razorpay/api", "role": "core", "lang": "php"},
    # --- Infrastructure ---
    {"slug": "goutils", "url": "https://github.com/razorpay/goutils", "role": "infra", "lang": "go"},
    {"slug": "integrations-go", "url": "https://github.com/razorpay/integrations-go", "role": "infra", "lang": "go"},
    {"slug": "integrations-utils", "url": "https://github.com/razorpay/integrations-utils", "role": "infra", "lang": "go"},
    {"slug": "ledger", "url": "https://github.com/razorpay/ledger", "role": "infra", "lang": "go"},
    {"slug": "splitz", "url": "https://github.com/razorpay/splitz", "role": "infra", "lang": "go"},
    {"slug": "stork", "url": "https://github.com/razorpay/stork", "role": "infra", "lang": "go"},
    {"slug": "raven", "url": "https://github.com/razorpay/raven", "role": "infra", "lang": "go"},
    {"slug": "metro", "url": "https://github.com/razorpay/metro", "role": "infra", "lang": "go"},
    {"slug": "vault", "url": "https://github.com/razorpay/vault", "role": "infra", "lang": "go"},
    # --- Domain Services ---
    {"slug": "scrooge", "url": "https://github.com/razorpay/scrooge", "role": "domain", "lang": "go"},
    {"slug": "settlements", "url": "https://github.com/razorpay/settlements", "role": "domain", "lang": "go"},
    {"slug": "charge-collections", "url": "https://github.com/razorpay/charge-collections", "role": "domain", "lang": "go"},
    {"slug": "subscriptions", "url": "https://github.com/razorpay/subscriptions", "role": "domain", "lang": "go"},
    {"slug": "reminders", "url": "https://github.com/razorpay/reminders", "role": "domain", "lang": "go"},
    {"slug": "magic-checkout-service", "url": "https://github.com/razorpay/magic-checkout-service", "role": "domain", "lang": "go"},
    {"slug": "payments-cross-border", "url": "https://github.com/razorpay/cross-border", "role": "domain", "lang": "go"},
    {"slug": "payments-bank-transfer", "url": "https://github.com/razorpay/payments-bank-transfer", "role": "domain", "lang": "go"},
    {"slug": "payment-methods", "url": "https://github.com/razorpay/payment-methods", "role": "domain", "lang": "go"},
    {"slug": "tokens", "url": "https://github.com/razorpay/tokens", "role": "domain", "lang": "go"},
    {"slug": "downtime-manager", "url": "https://github.com/razorpay/downtime-manager", "role": "domain", "lang": "go"},
    {"slug": "optimizer-core", "url": "https://github.com/razorpay/optimizer-core", "role": "domain", "lang": "go"},
    # --- Edge / Gateway ---
    {"slug": "edge", "url": "https://github.com/razorpay/edge", "role": "gateway", "lang": "go"},
    {"slug": "relay", "url": "https://github.com/razorpay/relay", "role": "gateway", "lang": "go"},
    {"slug": "dcs", "url": "https://github.com/razorpay/dcs", "role": "gateway", "lang": "go"},
    {"slug": "route", "url": "https://github.com/razorpay/route", "role": "gateway", "lang": "go"},
    {"slug": "cms", "url": "https://github.com/razorpay/cms", "role": "gateway", "lang": "go"},
    {"slug": "bin-service", "url": "https://github.com/razorpay/bin-service", "role": "gateway", "lang": "go"},
    {"slug": "apm-service", "url": "https://github.com/razorpay/apm-service", "role": "gateway", "lang": "go"},
    # --- Support Services ---
    {"slug": "cps", "url": "https://github.com/razorpay/cps", "role": "support", "lang": "go"},
    {"slug": "customer-service", "url": "https://github.com/razorpay/customer-service", "role": "support", "lang": "go"},
    {"slug": "governor-executor", "url": "https://github.com/razorpay/governor-executor", "role": "support", "lang": "go"},
    # --- Frontend ---
    {"slug": "dashboard", "url": "https://github.com/razorpay/dashboard", "role": "frontend", "lang": "ts"},
    {"slug": "checkout", "url": "https://github.com/razorpay/checkout", "role": "frontend", "lang": "ts"},
    # --- Ecosystem (not actively crawled for code) ---
    {"slug": "batch", "url": "https://github.com/razorpay/batch", "role": "ecosystem"},
    {"slug": "mock-gateway", "url": "https://github.com/razorpay/mock-gateway", "role": "ecosystem"},
]

# Service dependency graph: from → [to, ...] (discovered from go.mod + config TOML + @Slash)
SERVICE_DEPENDENCIES: dict[str, list[str]] = {
    "checkout-service": ["pg-router", "rpc", "splitz", "goutils", "ledger", "shield", "terminals",
                         "stork", "magic-checkout-service", "cps", "metro", "payments-card",
                         "payments-upi", "emandate-service", "offers-engine", "vault",
                         "payment-methods", "tokens", "customer-service", "subscriptions",
                         "route", "edge", "downtime-manager", "apm-service"],
    "pg-router": ["rpc", "goutils", "splitz", "ledger", "shield", "terminals", "mozart",
                  "payments-card", "payments-upi", "emandate-service", "payments-mandate",
                  "scrooge", "settlements", "charge-collections", "payments-cross-border",
                  "payments-bank-transfer", "vault", "stork", "raven", "optimizer-core",
                  "bin-service", "reminders", "tokens", "dcs", "cms", "edge", "relay",
                  "governor-executor", "integrations-go"],
    "payments-card": ["rpc", "goutils", "mozart", "shield", "terminals", "ledger", "splitz", "vault"],
    "payments-upi": ["rpc", "goutils", "mozart", "shield", "terminals", "ledger", "splitz",
                     "stork", "customer-service", "payments-mandate", "bin-service", "vault"],
    "emandate-service": ["rpc", "goutils", "payments-mandate", "splitz", "stork", "ledger"],
    "offers-engine": ["rpc", "goutils", "api", "splitz", "checkout-service", "ledger"],
    "mozart": ["rpc", "goutils", "integrations-go", "terminals"],
    "shield": ["rpc", "goutils", "splitz", "bin-service"],
    "terminals": ["rpc", "goutils", "splitz", "shield", "bin-service", "vault", "pg-router"],
    "settlements": ["rpc", "goutils", "ledger", "splitz", "scrooge"],
    "scrooge": ["rpc", "goutils", "ledger"],
    "integrations-go": ["integrations-utils"],
    "stork": ["rpc", "goutils", "metro", "raven"],
    "dashboard": ["api"],
}

SEED_CHANNELS: list[str] = [
    "#payments_emandate",
    "#payments_cards_emandate_coe",
    "#emandate_alerts",
    "#slash-offers-engine",
    "#debugging-offers-with-slash",
    "#recurring_alerts",
]

# ---------------------------------------------------------------------------
# @slash Bot Integration
# ---------------------------------------------------------------------------

SLASH_CHANNEL_ID: str = "C0B3U3Z2JG1"  # claude-saurav channel (used for both sending and ingestion)
SLASH_PRIVATE_CHANNEL_ID: str = "C0B3U3Z2JG1"  # always use this ID for Slack API calls — never resolve by name
SLASH_PRIVATE_CHANNEL_NAME: str = "claude-saurav"  # actual Slack channel name (NOT claude.saurav)
SLASH_BOT_USER_ID: str = "U0AK4Q67HEY"
SLASH_POLL_INTERVAL_SEC: int = 60
SLASH_MAX_POLLS: int = 10  # was 3; @Slash queue can be 100+ deep, need ~10min window
SLASH_QUEUE_DEPTH_THRESHOLD: int = 50  # if queue > 50, switch to extended polling
SLASH_EXTENDED_POLL_SEC: int = 120  # longer interval for deep queues
SLASH_CACHE_TTL_HOURS: int = 24
SLASH_CONFIDENCE: float = 0.85
SLASH_PREFER_PRIMARY_MCP: bool = True  # always use primary Slack MCP; secondary was rejected

# ---------------------------------------------------------------------------
# Tech Spec Template (16-section Razorpay format)
# ---------------------------------------------------------------------------

TECH_SPEC_SECTIONS: list[str] = [
    "Problem Statement",
    "Introduction & Scope",
    "Out of Scope",
    "Futuristic Scope",
    "Assumptions / Goals / Non-Goals",
    "Domain Design",
    "Current HLD",
    "Final Approach",
    "Non-Functional Requirements",
    "Feature Dependencies",
    "Testing Plan",
    "Go-Live",
    "Monitoring",
    "Milestones",
    "Glossary",
    "Appendix",
]

# ---------------------------------------------------------------------------
# Explainer Docs (payment flow step-by-step)
# ---------------------------------------------------------------------------

EXPLAINER_DOCS: list[str] = [
    str(Path.home() / "Documents" / "step0_order_creation_explained.md"),
    str(Path.home() / "Documents" / "step1_checkout_initialization_explained.md"),
    str(Path.home() / "Documents" / "step2_payment_creation_explained.md"),
    str(Path.home() / "Documents" / "step3_authorization_explained.md"),
    str(Path.home() / "Documents" / "step4_capture_explained.md"),
    str(Path.home() / "Documents" / "step5_webhooks_explained.md"),
]

# ---------------------------------------------------------------------------
# Learning Pipeline
# ---------------------------------------------------------------------------

LEARNING_DEFAULT_CONFIDENCE: float = 0.7
LEARNING_MULTI_SOURCE_CONFIDENCE: float = 0.85
LEARNING_MAX_ITEMS_PER_INTERACTION: int = 30
LEARNING_CONFIDENCE_DECAY_DAYS: int = 90
LEARNING_CONFIDENCE_DECAY_FACTOR: float = 0.1

# ---------------------------------------------------------------------------
# Arch Brain-First Query
# ---------------------------------------------------------------------------

ARCH_BRAIN_SUFFICIENT_NODES: int = 3
ARCH_BRAIN_CONFIDENCE_THRESHOLD: float = 0.7

# ---------------------------------------------------------------------------
# Scheduled Tasks (Brain refresh + confidence decay)
# ---------------------------------------------------------------------------

BRAIN_REFRESH_CRON: str = "0 9 * * 1-5"  # weekdays 9am IST
BRAIN_DECAY_CRON: str = "0 0 1 * *"  # 1st of each month

# ---------------------------------------------------------------------------
# Diagram Rendering
# ---------------------------------------------------------------------------

DIAGRAM_DEFAULT_RENDERER: str = "mermaid"  # mermaid | excalidraw
DIAGRAM_EXPORT_FORMAT: str = "png"
DIAGRAM_MAX_PARTICIPANTS: int = 15  # sequence diagram limit
DIAGRAM_MAX_MESSAGES: int = 30  # sequence diagram limit
DIAGRAM_MAX_ENTITIES: int = 20  # ER diagram limit

# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

REVIEW_CHECKLIST_CATEGORIES: list[str] = [
    "code_quality",
    "api_standards",
    "test_coverage",
    "security",
    "performance",
    "razorpay_domain",
]
REVIEW_DEFAULT_CONFIDENCE: float = 0.85  # ReviewResult nodes
REVIEW_MAX_CHECKLIST_ITEMS: int = 20  # per category

# ---------------------------------------------------------------------------
# Standup
# ---------------------------------------------------------------------------

STANDUP_SLACK_ACTIVITY_LIMIT: int = 50
STANDUP_GITHUB_PR_LIMIT: int = 10
STANDUP_GITHUB_REVIEW_LIMIT: int = 5
STANDUP_DEFAULT_CHANNELS: list[str] = SEED_CHANNELS

# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

TICKETS_DEVREV_PREFIX: str = "TKT"
TICKETS_JIRA_PREFIX: str = "ISS"
TICKETS_MAX_TRIAGE_RESULTS: int = 10
TICKETS_SYNC_BATCH_SIZE: int = 25

RESET_PATHS: list[Path] = [
    RUBICK_DB_PATH,
    WORKSPACE / "features",
    LOG_DIR,
]

DRIVE_STORAGE_FOLDER_ID: str = "1u1vLNkGY9CM8G8eBDe0DtEAjcT3mjBa5"
DRIVE_STORAGE_FOLDER_URL: str = "https://drive.google.com/drive/folders/1u1vLNkGY9CM8G8eBDe0DtEAjcT3mjBa5"

SEED_DRIVE_DOCS: dict[str, str] = {
    "Rubick Notebook": "1HDFURcA3TsW64Xt-rALjERFfcCmBNPMnBGNgPwfxWCo",
    "Rubick Sync Log": "1kHCSs21KeDE_qIIliLuMZubz9QoaXf097tqW918yrfQ",
    "Nemesis Notebook": "1PdjM-wMTmikjKEeKvoZmByokxlJTK9Ode759sdwagnQ",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def priority_label(score: float) -> str:
    for threshold, label in PRIORITY_LABELS:
        if score >= threshold:
            return label
    return "Low"


def ensure_workspace() -> Path:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    FEATURES_DIR.mkdir(parents=True, exist_ok=True)
    GITHUB_CLONE_BASE.mkdir(parents=True, exist_ok=True)
    return WORKSPACE


def ensure_log_dir() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR


def feature_dir(slug: str) -> Path:
    d = FEATURES_DIR / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Service Discovery (for init crawl)
# ---------------------------------------------------------------------------

SERVICE_DISCOVERY_DEPTH: int = 2  # how many hops from seed to crawl
SERVICE_DISCOVERY_SKIP_PATTERNS: list[str] = [
    "vendor", "third_party", "testdata", "mock", "example",
]

# AST Extraction: supported languages and their file extensions
AST_SUPPORTED_LANGUAGES: dict[str, list[str]] = {
    "go": [".go"],
    "php": [".php"],
    "typescript": [".ts", ".tsx"],
    "proto": [".proto"],
}

# AST skip directories per language
AST_SKIP_DIRS: dict[str, list[str]] = {
    "go": ["vendor", "testdata", "third_party", "mock", ".git"],
    "php": ["vendor", "storage", "bootstrap", "node_modules", ".git"],
    "typescript": ["node_modules", "dist", "build", ".next", "coverage", ".git"],
    "proto": ["vendor", ".git"],
}

# Route detection frameworks (for endpoint extraction)
ROUTE_FRAMEWORKS: list[str] = [
    "chi",         # Go: r.Get("/path", handler)
    "gin",         # Go: r.GET("/path", handler)
    "net_http",    # Go: http.HandleFunc("/path", handler)
    "spine",       # Razorpay internal: AddRoute("method", "path", handler)
    "grpc",        # Go: pb.RegisterXServiceServer(s, &server{})
    "laravel",     # PHP: Route::get("/path", [Controller::class, "method"])
    "express",     # TS: app.get("/path", handler)
    "nextjs",      # TS: export default function handler(req, res)
]

# Projects where endpoints are known to be 0 (accepted, not a bug)
KNOWN_ZERO_ENDPOINT_PROJECTS: list[str] = [
    "goutils",     # shared library, no HTTP server
    "integrations-utils",  # helper library
]

# Init pipeline phases for progress tracking
INIT_PHASES: list[str] = [
    "db_setup",           # Create rubick.db + schema
    "seed_projects",      # Seed all SEED_PROJECTS
    "service_discovery",  # go.mod + config crawl + @Slash
    "clone_repos",        # gh repo clone for all discovered services
    "ast_extraction",     # Multi-language AST parsing
    "ast_import",         # Import AST JSON to graph
    "cross_link",         # DEPENDS_ON + RELATES_TO edges
    "arch_seed",          # ArchDecision nodes per service
    "signal_ingestion",   # Slack + Gmail + Calendar + GitHub PRs
    "drive_sync",         # Drive docs backup
    "verify",             # Stats + health check
]

# ---------------------------------------------------------------------------
# Franco — Universal Data Collector
# ---------------------------------------------------------------------------

FRANCO_FETCH_MAP: dict[str, dict] = {
    "slack_thread":   {"method": "mcp", "mcp": "plugin_compass_slack-mcp", "tool": "slack_get_thread_replies"},
    "slack_channel":  {"method": "mcp", "mcp": "plugin_compass_slack-mcp", "tool": "slack_get_channel_messages"},
    "slack_search":   {"method": "mcp", "mcp": "plugin_compass_slack-mcp", "tool": "slack_search_messages"},
    "drive_doc":      {"method": "mcp", "mcp": "plugin_compass_google-workspace", "tool": "get_doc_content"},
    "drive_file":     {"method": "mcp", "mcp": "e20283d0", "tool": "read_file_content"},
    "drive_folder":   {"method": "mcp", "mcp": "e20283d0", "tool": "search_files"},
    "gmail_thread":   {"method": "mcp", "mcp": "f22d0c2f", "tool": "get_thread"},
    "gmail_search":   {"method": "mcp", "mcp": "f22d0c2f", "tool": "search_threads"},
    "github_pr":      {"method": "cli", "cmd": "gh pr view {number} --repo {owner}/{repo} --json title,body,files,comments,reviews"},
    "github_issue":   {"method": "cli", "cmd": "gh issue view {number} --repo {owner}/{repo} --json title,body,comments,labels"},
    "github_search":  {"method": "cli", "cmd": "gh search code '{query}' --json path,textMatches --limit 20"},
    "github_commit":  {"method": "cli", "cmd": "gh api repos/{owner}/{repo}/commits/{sha}"},
    "devrev_task":    {"method": "cli", "cmd": "gh api --method GET 'https://app.devrev.ai/api/v1/works/{id}'"},
    "devrev_id":      {"method": "cli", "cmd": "gh api --method GET 'https://app.devrev.ai/api/v1/works/{id}'"},
    "jira_issue":     {"method": "cli", "cmd": "gh api --method GET 'https://app.devrev.ai/api/v1/works/{id}'"},
    "jira_id":        {"method": "cli", "cmd": "gh api --method GET 'https://app.devrev.ai/api/v1/works/{id}'"},
    "figma":          {"method": "mcp", "mcp": "f39bd90b", "tool": "get_design_context"},
    "calendar":       {"method": "mcp", "mcp": "d285de92", "tool": "get_event"},
    "gsheet":         {"method": "mcp", "mcp": "plugin_compass_google-workspace", "tool": "read_sheet_values"},
    "slides":         {"method": "mcp", "mcp": "plugin_compass_google-workspace", "tool": "get_presentation"},
    "web_url":        {"method": "webfetch"},
    "local_file":     {"method": "read"},
    "local_dir":      {"method": "glob_read"},
    "rubick_context": {"method": "internal", "fn": "context_for_v2"},
    "expert_knowledge": {"method": "internal", "fn": "query_expert"},
    "code_body":      {"method": "internal", "fn": "get_code_body"},
    "repo_skill":     {"method": "read", "path": "workspace/repos/{project}/.agents/skills/{skill}/SKILL.md"},
}

# ---------------------------------------------------------------------------
# Designer — Visual Design Agent MCP Priority
# ---------------------------------------------------------------------------

DESIGNER_MCP_PRIORITY: list[dict] = [
    {"name": "Canva",      "prefix": "mcp__dde94166__",                  "best_for": "professional polished output"},
    {"name": "Mermaid",    "prefix": "mcp__7428c252__",                  "best_for": "structural diagrams"},
    {"name": "Figma",      "prefix": "mcp__f39bd90b__",                  "best_for": "reference existing designs"},
    {"name": "Excalidraw", "prefix": "mcp__3000b99d__",                  "best_for": "whiteboard sketches"},
    {"name": "Blade",      "prefix": "mcp__plugin_compass_blade-mcp__", "best_for": "Razorpay design system"},
]
