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
