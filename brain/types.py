"""Unified type definitions for Nemesis Brain.

Merges v3 code intelligence types with v2 Rubick workflow types.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class NodeType(str, Enum):
    SERVICE = "Service"
    FILE = "File"
    FUNCTION = "Function"
    CLASS = "Class"
    MODULE = "Module"
    ENDPOINT = "Endpoint"
    DATASTORE = "DataStore"
    TEST = "Test"
    KAFKA_TOPIC = "KafkaTopic"
    FEATURE = "Feature"
    REQUIREMENT = "Requirement"
    RISK_ITEM = "RiskItem"
    ARCH_DECISION = "ArchDecision"
    USE_CASE = "UseCase"
    BUSINESS_LOGIC = "BusinessLogic"
    SIGNAL = "Signal"
    PROJECT_EXPERT = "ProjectExpert"
    DOCUMENT = "Document"
    TASK = "Task"
    PERSON = "Person"
    EMAIL = "Email"
    COMMIT = "Commit"
    MEETING = "Meeting"
    PLAN = "Plan"
    BRANCH = "Branch"
    PR = "PR"
    WEB_PAGE = "WebPage"
    JIRA_ISSUE = "JiraIssue"
    REVIEW_RESULT = "ReviewResult"
    EVOLUTION_PLAN = "EvolutionPlan"
    SLACK_CHANNEL = "SlackChannel"
    KNOWLEDGE_ENTITY = "KnowledgeEntity"
    BUSINESS_RULE = "BusinessRule"


class EdgeType(str, Enum):
    CALLS = "CALLS"
    CONTAINS_FUNC = "CONTAINS_FUNC"
    CONTAINS_CLASS = "CONTAINS_CLASS"
    CONTAINS_TEST = "CONTAINS_TEST"
    FILE_IN_SERVICE = "FILE_IN_SERVICE"
    IMPORTS = "IMPORTS"
    READS = "READS"
    WRITES = "WRITES"
    TESTS = "TESTS"
    DEPENDS_ON = "DEPENDS_ON"
    ROUTES_TO = "ROUTES_TO"
    PUBLISHES = "PUBLISHES"
    CONSUMES = "CONSUMES"
    IMPLEMENTS = "IMPLEMENTS"
    HAS_METHOD = "HAS_METHOD"
    HAS_REQUIREMENT = "HAS_REQUIREMENT"
    HAS_RISK = "HAS_RISK"
    HAS_USE_CASE = "HAS_USE_CASE"
    IMPLEMENTS_FEATURE = "IMPLEMENTS_FEATURE"
    DECIDED_BY = "DECIDED_BY"
    ENCODES = "ENCODES"
    GOVERNS = "GOVERNS"
    EXPERT_ON = "EXPERT_ON"
    SIGNAL_FOR = "SIGNAL_FOR"
    RELATES_TO = "RELATES_TO"
    MITIGATES = "MITIGATES"
    SPAWNED = "SPAWNED"
    ASSIGNED_TO = "ASSIGNED_TO"
    AUTHORED_BY = "AUTHORED_BY"
    MENTIONED_IN = "MENTIONED_IN"
    REFERENCES = "REFERENCES"
    VALIDATES = "VALIDATES"


class Language(str, Enum):
    GO = "go"
    PHP = "php"
    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"
    PYTHON = "python"
    JAVA = "java"
    RUST = "rust"
    PROTO = "proto"


class BrainName(str, Enum):
    GRAPH = "graph"
    SEMANTIC = "semantic"
    KNOWLEDGE = "knowledge"
    MEMORY = "memory"


class QueryType(str, Enum):
    STRUCTURAL = "structural"
    SEMANTIC = "semantic"
    KNOWLEDGE = "knowledge"
    HYBRID = "hybrid"
    IMPACT = "impact"


# ---------------------------------------------------------------------------
# AST Extraction
# ---------------------------------------------------------------------------
@dataclass
class ExtractedFunction:
    name: str
    qualified_name: str
    file_path: str
    line_start: int
    line_end: int
    language: str
    signature: str = ""
    body: str = ""
    receiver: str = ""
    params: str = ""
    returns: str = ""
    complexity: float = 0.0
    is_exported: bool = False
    is_test: bool = False
    calls: List[str] = field(default_factory=list)
    project: str = ""


@dataclass
class ExtractedClass:
    name: str
    qualified_name: str
    file_path: str
    line_start: int
    line_end: int
    language: str
    kind: str = "class"
    is_exported: bool = False
    project: str = ""


@dataclass
class ExtractedEndpoint:
    route: str
    method: str
    handler: str
    file_path: str = ""
    line: int = 0
    auth_required: bool = False
    project: str = ""


@dataclass
class ASTResult:
    project: str
    language: str
    functions: List[ExtractedFunction] = field(default_factory=list)
    classes: List[ExtractedClass] = field(default_factory=list)
    endpoints: List[ExtractedEndpoint] = field(default_factory=list)
    files_parsed: int = 0
    parse_errors: int = 0


# ---------------------------------------------------------------------------
# Query Results
# ---------------------------------------------------------------------------
@dataclass
class ContextResult:
    target: str
    text: str = ""
    tokens_used: int = 0
    budget: int = 0
    sources: List[str] = field(default_factory=list)
    graph_nodes: int = 0
    fts_hits: int = 0
    vector_hits: int = 0


@dataclass
class ImpactResult:
    changed_functions: List[str] = field(default_factory=list)
    direct_callers: int = 0
    total_impacted: int = 0
    impacted_services: List[str] = field(default_factory=list)
    risk_scores: Dict[str, float] = field(default_factory=dict)
    test_gaps: List[str] = field(default_factory=list)
    overall_risk: float = 0.0


@dataclass
class HealthReport:
    project: str
    grade: str = "C"
    score: float = 0.0
    metrics: Dict[str, Any] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)


@dataclass
class LearningItem:
    node_type: str
    node_name: str
    node_data: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.7
    edges: List[Dict[str, str]] = field(default_factory=list)
    project: str = ""


@dataclass
class FeatureRecord:
    slug: str
    name: str
    description: str = ""
    status: str = "proposed"
    services: List[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
