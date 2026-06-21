"""Central configuration for Nemesis Brain.

Merges v2 brain_config.py (Razorpay workflows) with v3 BrainConfig (code intelligence).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List


def _detect_workspace() -> str:
    if "BRAIN_WORKSPACE" in os.environ:
        return os.environ["BRAIN_WORKSPACE"]
    here = Path(__file__).resolve().parent.parent / "workspace"
    if here.exists():
        return str(here)
    return str(Path.cwd() / "workspace")


DEFAULT_WORKSPACE = _detect_workspace()


@dataclass
class BrainConfig:
    workspace: str = DEFAULT_WORKSPACE

    # SQLite
    db_path: str = ""

    # LanceDB (lazy-loaded vectors)
    lance_path: str = ""
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dim: int = 384

    # Repos
    repos_path: str = ""

    # Context budgets (tokens)
    default_budget: int = 4000
    max_budget: int = 16000

    # Graph traversal
    max_bfs_depth: int = 5
    max_fanout_per_hop: int = 50

    # NetworkX edge types to load at startup
    networkx_edge_types: List[str] = field(default_factory=lambda: [
        "CALLS", "DEPENDS_ON", "TESTS", "IMPORTS", "ROUTES_TO",
        "PUBLISHES", "CONSUMES", "IMPLEMENTS", "HAS_METHOD",
        "CONTAINS_FUNC", "CONTAINS_CLASS", "FILE_IN_SERVICE",
    ])

    # Ingestion
    batch_insert_size: int = 500
    skip_dirs: List[str] = field(default_factory=lambda: [
        "vendor", "node_modules", ".git", "testdata", "mock",
        "mocks", "__pycache__", ".venv", "dist", "build",
    ])

    def __post_init__(self):
        ws = Path(self.workspace)
        if not self.db_path:
            self.db_path = str(ws / "brain.db")
        if not self.lance_path:
            self.lance_path = str(ws / "lance")
        if not self.repos_path:
            self.repos_path = str(ws / "repos")


# ---------------------------------------------------------------------------
# Hybrid retrieval weights (consumer-specific)
# ---------------------------------------------------------------------------
HYBRID_WEIGHTS: Dict[str, Dict[str, float]] = {
    "planner": {"graph": 0.5, "fts5": 0.3, "vector": 0.2},
    "arch":    {"graph": 0.3, "fts5": 0.2, "vector": 0.5},
    "dev":     {"graph": 0.3, "fts5": 0.4, "vector": 0.3},
    "user":    {"graph": 0.35, "fts5": 0.25, "vector": 0.4},
    "default": {"graph": 0.4, "fts5": 0.3, "vector": 0.3},
}

EDGE_WEIGHTS: Dict[str, float] = {
    "CALLS": 1.0, "CONTAINS_FUNC": 0.8, "CONTAINS_CLASS": 0.8,
    "CONTAINS_TEST": 0.7, "FILE_IN_SERVICE": 0.6, "IMPORTS": 0.6,
    "READS": 0.9, "WRITES": 0.9, "TESTS": 0.7, "DEPENDS_ON": 0.8,
    "ROUTES_TO": 0.9, "PUBLISHES": 0.8, "CONSUMES": 0.8,
    "IMPLEMENTS": 0.7, "HAS_METHOD": 0.7,
    "HAS_REQUIREMENT": 1.0, "HAS_RISK": 1.0, "HAS_USE_CASE": 1.0,
    "IMPLEMENTS_FEATURE": 0.95, "DECIDED_BY": 0.85, "SIGNAL_FOR": 0.8,
    "ENCODES": 0.8, "GOVERNS": 0.75, "EXPERT_ON": 0.7,
    "RELATES_TO": 0.3, "MENTIONED_IN": 0.4,
}

TOKENS_PER_NODE: int = 80

# ---------------------------------------------------------------------------
# Retention (days, -1 = permanent)
# ---------------------------------------------------------------------------
RETENTION: Dict[str, int] = {
    "Feature": -1, "Requirement": -1, "ArchDecision": -1,
    "UseCase": -1, "BusinessLogic": -1, "RiskItem": -1,
    "Person": -1, "ProjectExpert": -1, "SlackChannel": -1,
    "Signal": 180, "Task": 180, "Meeting": 180, "Email": 180,
    "Plan": 30, "WebPage": 90,
    "Commit": 365, "Branch": 365, "PR": 365, "JiraIssue": 365,
}

# ---------------------------------------------------------------------------
# Learning pipeline
# ---------------------------------------------------------------------------
LEARNING_DEFAULT_CONFIDENCE: float = 0.7
LEARNING_MULTI_SOURCE_CONFIDENCE: float = 0.85
LEARNING_DECAY_DAYS: int = 90
LEARNING_DECAY_FACTOR: float = 0.1

# ---------------------------------------------------------------------------
# Expert system
# ---------------------------------------------------------------------------
EXPERT_XP_THRESHOLDS: Dict[int, int] = {1: 0, 2: 500, 3: 1500, 4: 3000, 5: 5000}
EXPERT_XP_REWARDS: Dict[str, int] = {
    "deep_read": 300, "feature_analysis": 200, "solution_design": 300,
    "risk_finding": 150, "user_confirmation": 100, "slash_validation": 50,
    "contradiction": -200,
}

# ---------------------------------------------------------------------------
# Feature lifecycle
# ---------------------------------------------------------------------------
FEATURE_TRANSITIONS: Dict[str, set] = {
    "proposed": {"in_progress", "abandoned", "closed"},
    "in_progress": {"blocked", "shipped", "abandoned", "closed"},
    "blocked": {"in_progress", "abandoned", "closed"},
    "shipped": {"closed"},
    "abandoned": {"proposed"},
    "closed": set(),
}

# ---------------------------------------------------------------------------
# @Slash bot
# ---------------------------------------------------------------------------
SLASH_CHANNEL_ID: str = "C0B3U3Z2JG1"
SLASH_BOT_USER_ID: str = "U0AK4Q67HEY"

# ---------------------------------------------------------------------------
# Seed projects (45)
# ---------------------------------------------------------------------------
SEED_PROJECTS: List[Dict[str, str]] = [
    {"slug": "emandate-service", "role": "primary", "lang": "go"},
    {"slug": "offers-engine", "role": "primary", "lang": "go"},
    {"slug": "rpc", "role": "primary", "lang": "proto"},
    {"slug": "payments-mandate", "role": "primary", "lang": "go"},
    {"slug": "checkout-service", "role": "core", "lang": "go"},
    {"slug": "pg-router", "role": "core", "lang": "go"},
    {"slug": "payments-card", "role": "core", "lang": "go"},
    {"slug": "payments-upi", "role": "core", "lang": "go"},
    {"slug": "mozart", "role": "core", "lang": "go"},
    {"slug": "terminals", "role": "core", "lang": "go"},
    {"slug": "shield", "role": "core", "lang": "go"},
    {"slug": "api", "role": "core", "lang": "php"},
    {"slug": "goutils", "role": "infra", "lang": "go"},
    {"slug": "integrations-go", "role": "infra", "lang": "go"},
    {"slug": "integrations-utils", "role": "infra", "lang": "go"},
    {"slug": "ledger", "role": "infra", "lang": "go"},
    {"slug": "splitz", "role": "infra", "lang": "go"},
    {"slug": "stork", "role": "infra", "lang": "go"},
    {"slug": "raven", "role": "infra", "lang": "go"},
    {"slug": "metro", "role": "infra", "lang": "go"},
    {"slug": "vault", "role": "infra", "lang": "go"},
    {"slug": "scrooge", "role": "domain", "lang": "go"},
    {"slug": "settlements", "role": "domain", "lang": "go"},
    {"slug": "charge-collections", "role": "domain", "lang": "go"},
    {"slug": "subscriptions", "role": "domain", "lang": "go"},
    {"slug": "reminders", "role": "domain", "lang": "go"},
    {"slug": "magic-checkout-service", "role": "domain", "lang": "go"},
    {"slug": "payments-cross-border", "role": "domain", "lang": "go"},
    {"slug": "payments-bank-transfer", "role": "domain", "lang": "go"},
    {"slug": "payment-methods", "role": "domain", "lang": "go"},
    {"slug": "tokens", "role": "domain", "lang": "go"},
    {"slug": "downtime-manager", "role": "domain", "lang": "go"},
    {"slug": "optimizer-core", "role": "domain", "lang": "go"},
    {"slug": "edge", "role": "gateway", "lang": "go"},
    {"slug": "relay", "role": "gateway", "lang": "go"},
    {"slug": "dcs", "role": "gateway", "lang": "go"},
    {"slug": "route", "role": "gateway", "lang": "go"},
    {"slug": "cms", "role": "gateway", "lang": "go"},
    {"slug": "bin-service", "role": "gateway", "lang": "go"},
    {"slug": "apm-service", "role": "gateway", "lang": "go"},
    {"slug": "cps", "role": "support", "lang": "go"},
    {"slug": "customer-service", "role": "support", "lang": "go"},
    {"slug": "governor-executor", "role": "support", "lang": "go"},
    {"slug": "dashboard", "role": "frontend", "lang": "ts"},
    {"slug": "checkout", "role": "frontend", "lang": "ts"},
]

SERVICE_DEPS: Dict[str, List[str]] = {
    "checkout-service": ["pg-router", "rpc", "splitz", "goutils", "ledger", "shield",
                         "terminals", "stork", "payments-card", "payments-upi",
                         "emandate-service", "offers-engine", "vault"],
    "pg-router": ["rpc", "goutils", "splitz", "ledger", "shield", "terminals", "mozart",
                  "payments-card", "payments-upi", "emandate-service", "payments-mandate",
                  "scrooge", "settlements", "vault", "stork", "raven", "optimizer-core"],
    "emandate-service": ["rpc", "goutils", "payments-mandate", "splitz", "stork", "ledger"],
    "offers-engine": ["rpc", "goutils", "api", "splitz", "checkout-service", "ledger"],
    "mozart": ["rpc", "goutils", "integrations-go", "terminals"],
    "payments-card": ["rpc", "goutils", "mozart", "shield", "terminals", "ledger", "splitz", "vault"],
    "payments-upi": ["rpc", "goutils", "mozart", "shield", "terminals", "ledger", "splitz", "vault"],
    "settlements": ["rpc", "goutils", "ledger", "splitz", "scrooge"],
    "stork": ["rpc", "goutils", "metro", "raven"],
}

# ---------------------------------------------------------------------------
# Razorpay Skill Registry (16) — loaded at `brain init` and by /nemesis Step 0.
# Skills resolve dynamically via the Skill tool (not Python imports); this map
# is the source of truth for phase bindings + fallback chains.
# fallback chain per skill: Razorpay skill > Brain context > @Slash > proceed.
# ---------------------------------------------------------------------------
SKILL_REGISTRY: List[Dict[str, str]] = [
    {"skill": "product-management:brainstorm", "phases": "Ideation",
     "fallback": "Brain context > @Slash"},
    {"skill": "product-management:write-spec", "phases": "Tech Spec",
     "fallback": "Brain context > manual sections"},
    {"skill": "compass:reviewing-strategy", "phases": "Ideation, Solutioning",
     "fallback": "Brain context > @Slash"},
    {"skill": "compass:razorpay-api-review", "phases": "Tech Spec, Review",
     "fallback": "Brain context > manual API review"},
    {"skill": "engineering:system-design", "phases": "Solutioning, Tech Spec",
     "fallback": "Brain context > @Slash"},
    {"skill": "engineering:architecture", "phases": "Tech Spec, Scout",
     "fallback": "Brain context > expert nodes"},
    {"skill": "engineering:code-review", "phases": "Solutioning, Impl, Review",
     "fallback": "Brain context > manual review"},
    {"skill": "engineering:testing-strategy", "phases": "Solutioning, Impl, Review",
     "fallback": "Brain context > manual strategy"},
    {"skill": "engineering:tech-debt", "phases": "Tech Spec",
     "fallback": "Brain context > skip section"},
    {"skill": "engineering:documentation", "phases": "Tech Spec",
     "fallback": "Brain context > manual docs"},
    {"skill": "engineering:deploy-checklist", "phases": "Tech Spec, Impl, Review",
     "fallback": "Brain context > manual checklist"},
    {"skill": "quality-engineer", "phases": "Impl, E2E",
     "fallback": "Brain context > manual tests"},
    {"skill": "gatekeeper", "phases": "Impl",
     "fallback": "manual merge criteria"},
    {"skill": "slit-generator-v2", "phases": "Impl",
     "fallback": "manual SLIT tests"},
    {"skill": "pre-mortem", "phases": "Solutioning, Review",
     "fallback": "RPN scoring only"},
    {"skill": "tech-spec-generator", "phases": "Tech Spec",
     "fallback": "TECH_SPEC_TEMPLATE direct"},
]

# ---------------------------------------------------------------------------
# Franco — universal data collector
# ---------------------------------------------------------------------------
# Source-detection regexes (ported from scripts/brain_config.py so the brain
# package is self-contained). BrainAPI.ingest() / detect_source() use these to
# classify a URL / ID / path. The brain package NEVER calls an MCP itself — for
# MCP-backed sources detect_source returns status=needs_fetch and the LLM (skill
# layer) fetches the payload, then hands it back via ingest_mcp_response().
SOURCE_PATTERNS: Dict[str, str] = {
    "slack_thread":  r"(?:https?://)?[\w.-]*slack\.com/archives/\w+/p\d+",
    "slack_channel": r"(?:https?://)?[\w.-]*slack\.com/(?:archives|channels?)/\w+/?$",
    "drive_doc":     r"(?:https?://)?docs\.google\.com/document/d/[\w-]+",
    "drive_file":    r"(?:https?://)?drive\.google\.com/(?:file/d|open\?id=)[\w-]+",
    "drive_folder":  r"(?:https?://)?drive\.google\.com/drive/(?:u/\d+/)?folders/[\w-]+",
    "gmail_thread":  r"(?:https?://)?mail\.google\.com/mail/.*#(?:inbox|all|sent)/[\w]+",
    "github_pr":     r"(?:https?://)?github\.com/[\w.-]+/[\w.-]+/pull/\d+",
    "github_issue":  r"(?:https?://)?github\.com/[\w.-]+/[\w.-]+/issues/\d+",
    "github_commit": r"(?:https?://)?github\.com/[\w.-]+/[\w.-]+/commit/[\da-f]+",
    "jira_issue":    r"(?:https?://)?[\w.-]*atlassian\.net/browse/[\w]+-\d+",
    "devrev_task":   r"(?:https?://)?app\.devrev\.ai/[\w.-]+/(?:tasks|works)/[\w-]+",
    "devrev_id":     r"^(?:ISS|TKT|TASK)-\d+$",
    "jira_id":       r"^[A-Z][A-Z0-9]+-\d+$",
}

# Which node type each detected source maps to when ingested.
SOURCE_NODE_TYPE: Dict[str, str] = {
    "slack_thread": "Signal", "slack_channel": "SlackChannel",
    "drive_doc": "Document", "drive_file": "Document", "drive_folder": "Document",
    "gmail_thread": "Email", "github_pr": "PR", "github_issue": "JiraIssue",
    "github_commit": "Commit", "jira_issue": "JiraIssue", "devrev_task": "JiraIssue",
    "devrev_id": "JiraIssue", "jira_id": "JiraIssue",
    "local_file": "Document", "web_url": "WebPage", "unknown": "Signal",
}


def _extract_source_id(source_type: str, url: str) -> str:
    """Extract a stable id from a URL for the given source type.

    Mirrors scripts/rubick_ingest._extract_source_id. Returns the raw url when
    no pattern matches (e.g. bare ids like ISS-1234).
    """
    import re
    rules = {
        "slack_thread":  (r"/archives/(\w+)/p(\d+)", lambda m: f"{m.group(1)}:{m.group(2)}"),
        "slack_channel": (r"/(?:archives|channels?)/(\w+)", lambda m: m.group(1)),
        "drive_doc":     (r"/document/d/([\w-]+)", lambda m: m.group(1)),
        "drive_file":    (r"(?:file/d/|open\?id=)([\w-]+)", lambda m: m.group(1)),
        "drive_folder":  (r"folders/([\w-]+)", lambda m: m.group(1)),
        "gmail_thread":  (r"#(?:inbox|all|sent)/([\w]+)", lambda m: m.group(1)),
        "github_pr":     (r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)",
                          lambda m: f"{m.group(1)}/{m.group(2)}#{m.group(3)}"),
        "github_issue":  (r"github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)",
                          lambda m: f"{m.group(1)}/{m.group(2)}#{m.group(3)}"),
        "github_commit": (r"github\.com/([\w.-]+)/([\w.-]+)/commit/([\da-f]+)",
                          lambda m: f"{m.group(1)}/{m.group(2)}@{m.group(3)[:12]}"),
        "jira_issue":    (r"/browse/([\w]+-\d+)", lambda m: m.group(1)),
        "devrev_task":   (r"/(?:tasks|works)/([\w-]+)", lambda m: m.group(1)),
    }
    if source_type in rules:
        pat, fn = rules[source_type]
        m = re.search(pat, url)
        if m:
            return fn(m)
    return url


def detect_source(input_str: str) -> Dict[str, str]:
    """Detect {source_type, source_id, platform, ...} from a URL, id, or path.

    Pure aside from a local-path existence check. No MCP / network I/O.
    """
    import hashlib
    import os
    import re
    s = (input_str or "").strip()
    for stype, pattern in SOURCE_PATTERNS.items():
        if re.match(pattern, s):
            return {"source_type": stype, "source_id": _extract_source_id(stype, s),
                    "platform": stype.split("_")[0],
                    "url": s if s.startswith("http") else None, "raw_input": s}
    if os.path.exists(s):
        return {"source_type": "local_file",
                "source_id": hashlib.sha256(s.encode()).hexdigest()[:16],
                "platform": "local", "path": s, "raw_input": s}
    if s.startswith("http"):
        return {"source_type": "web_url",
                "source_id": hashlib.sha256(s.encode()).hexdigest()[:16],
                "platform": "web", "url": s, "raw_input": s}
    return {"source_type": "unknown", "source_id": s,
            "platform": "unknown", "raw_input": s}


# ---------------------------------------------------------------------------
# MCP servers required for full Franco / init / feature-sync functionality.
# setup.sh and `brain doctor` VALIDATE availability only — they never store
# tokens. OAuth MCPs must be connected interactively by the user.
# ---------------------------------------------------------------------------
# Each entry's `aliases` are matched (case-insensitive substring, both directions)
# against BOTH local `mcpServers` keys in .claude/settings.json AND the hosted
# connector names in ~/.claude.json `claudeAiMcpEverConnected` (e.g. "claude.ai Slack").
# MCPs in this environment are typically hosted OAuth connectors, not local servers,
# so the aliases are what make the doctor/setup probe report them as present.
REQUIRED_MCP: List[Dict[str, object]] = [
    {"key": "slack", "server": "plugin_compass_slack-mcp", "auth": "oauth",
     "purpose": "Slack channel/thread ingest",
     "aliases": ["claude.ai Slack", "slack"]},
    {"key": "google-workspace", "server": "plugin_compass_google-workspace", "auth": "oauth",
     "purpose": "Drive docs read + feature-sync push/pull",
     "aliases": ["claude.ai Google Drive", "google-workspace", "google drive", "workspace"]},
    {"key": "drive", "server": "e20283d0", "auth": "oauth",
     "purpose": "Drive file/folder read + upload",
     "aliases": ["claude.ai Google Drive", "google drive", "drive"]},
    {"key": "gmail", "server": "f22d0c2f", "auth": "oauth",
     "purpose": "Gmail thread ingest",
     "aliases": ["claude.ai Gmail", "gmail"]},
    {"key": "calendar", "server": "d285de92", "auth": "oauth",
     "purpose": "Calendar context for standup",
     "aliases": ["claude.ai Google Calendar", "google calendar", "calendar"]},
]

# ---------------------------------------------------------------------------
# Drive feature-sync (PUSH after each pipeline step / PULL on `/nemesis new`).
# ---------------------------------------------------------------------------
DRIVE_STORAGE_FOLDER_ID: str = "1u1vLNkGY9CM8G8eBDe0DtEAjcT3mjBa5"
DRIVE_STORAGE_FOLDER_URL: str = (
    "https://drive.google.com/drive/folders/1u1vLNkGY9CM8G8eBDe0DtEAjcT3mjBa5")
SYNC_ALLOWLIST_EXT: List[str] = [".md", ".html", ".json"]
SYNC_MAX_FILE_BYTES: int = 2 * 1024 * 1024  # skip files larger than 2 MB
SYNC_SKIP_DIR_SUFFIXES: List[str] = ["-logs"]  # skip *-logs/ directories

# ---------------------------------------------------------------------------
# Data-source registry — single source of truth for `/nemesis init`.
# ---------------------------------------------------------------------------
SOURCES_PATH: str = str(Path(__file__).resolve().parent.parent / "config" / "sources.json")


def load_sources() -> Dict:
    """Load config/sources.json (the data-source registry). {} if absent/bad."""
    import json
    p = Path(SOURCES_PATH)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}
