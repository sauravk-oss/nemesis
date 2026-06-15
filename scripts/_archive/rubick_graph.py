#!/usr/bin/env python3
"""Rubick Graph Engine — single cross-project SQLite knowledge graph.

Extends nemesis v1 graphify_engine with:
- Single rubick.db replacing per-project graphify.db files
- 30 node types, 47 edge types (schema v3.0)
- sync_state table for incremental platform syncing
- Provenance columns (source_type, source_id, ingested_at, confidence)
- Cross-project intelligence via FTS5 similarity
- Context budget retrieval (delegated to rubick_context.py)
- Full planner engine: DAG, topo sort, CPM, priority scoring, capacity, slot matching
- Feature lifecycle tracking with health metrics and timelines
- Archive with configurable per-type retention and field stripping
"""

import sys
import json
import sqlite3
import os
import re
import hashlib
import logging
import argparse
from datetime import datetime, timezone, timedelta
from collections import deque
from pathlib import Path
from typing import Any, Optional, Union

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import brain_config as cfg
except ImportError:
    cfg = None  # type: ignore[assignment]

logger = logging.getLogger("rubick_graph")

# ---------------------------------------------------------------------------
# Config resolution (safe fallbacks if brain_config unavailable)
# ---------------------------------------------------------------------------

_DEFAULT_WEIGHTS = (
    cfg.DEFAULT_WEIGHTS if cfg else
    {"time_proximity": 0.35, "urgency_signals": 0.30, "action_required": 0.20, "stakeholder": 0.15}
)
_ACTION_SCORES = (
    cfg.ACTION_SCORES if cfg else
    {"blocks_others": 1.0, "needs_response": 0.7, "fyi": 0.2}
)
_TIME_BUCKETS = (
    cfg.TIME_PROXIMITY_BUCKETS if cfg else
    [(2.0, 1.0), (24.0, 0.6), (48.0, 0.3), (168.0, 0.1)]
)
_DEFAULT_ESTIMATED_HOURS = cfg.DEFAULT_ESTIMATED_HOURS if cfg else 1.0
_DEFAULT_URGENCY = cfg.DEFAULT_URGENCY_SCORE if cfg else 0.4
_DEFAULT_STAKEHOLDER = cfg.DEFAULT_STAKEHOLDER_SCORE if cfg else 0.5
_PLAN_TASK_LIMIT = cfg.PLAN_TASK_LIMIT if cfg else 200
_CAPACITY_HEALTHY = cfg.CAPACITY_HEALTHY_RATIO if cfg else 0.80
_CAPACITY_TIGHT = cfg.CAPACITY_TIGHT_RATIO if cfg else 1.00
_SCHEMA_VERSION = cfg.SCHEMA_VERSION if cfg else "3.0"

_ARCHIVE_AFTER_DAYS = (
    cfg.ARCHIVE_AFTER_DAYS if cfg else
    {"Plan": 30, "Signal": 180, "Task": 180, "Meeting": 180,
     "Email": 180, "Commit": 365, "Branch": 365, "PR": 365,
     "WebPage": 90, "JiraIssue": 365,
     "Feature": -1, "Decision": -1, "ArchDecision": -1,
     "Person": -1, "Project": -1, "Requirement": -1,
     "UseCase": -1, "BusinessLogic": -1, "RiskItem": -1,
     "EvolutionPlan": -1, "SlackChannel": -1}
)
_ARCHIVE_STRIP_FIELDS = (
    cfg.ARCHIVE_STRIP_FIELDS if cfg else
    {"Plan": ["schedule_json", "conflicts_json", "circular_deps_json"],
     "Signal": ["raw_metadata"], "Meeting": ["participants"],
     "Email": ["body", "raw_metadata"],
     "Commit": ["diff", "raw_metadata"],
     "Branch": ["raw_metadata"],
     "PR": ["diff_summary", "raw_metadata"],
     "WebPage": ["raw_content"],
     "JiraIssue": ["raw_metadata"]}
)

_EDGE_RELEVANCE = (
    cfg.EDGE_RELEVANCE if cfg else
    {"HAS_REQUIREMENT": 1.0, "HAS_RISK": 1.0, "HAS_USE_CASE": 1.0,
     "IMPLEMENTS_FEATURE": 0.95, "TRACKS": 0.9, "DECIDED_BY": 0.85,
     "SIGNAL_FOR": 0.8, "ENCODES": 0.8, "GOVERNS": 0.75,
     "DISCUSSED_IN": 0.7, "SPAWNED": 0.7, "IMPLEMENTS": 0.7,
     "OPENS_PR": 0.65, "BRANCH_OF": 0.6, "MENTIONED_IN": 0.4,
     "RELATES_TO": 0.3, "PART_OF": 0.2}
)

_SCOPE_HOURS = {"today": 24, "week": 168, "sprint": 336}

_FEATURE_TASK_EDGES = ("IMPLEMENTS_FEATURE", "SPAWNED")
_FEATURE_SIGNAL_EDGES = ("SIGNAL_FOR", "DISCUSSED_IN", "MENTIONED_IN")
_FEATURE_DECISION_EDGES = ("DECIDED_BY",)

_FEATURE_VALID_TRANSITIONS: dict[str, set[str]] = (
    cfg.FEATURE_VALID_TRANSITIONS if cfg else {
        "proposed": {"in_progress", "abandoned", "closed"},
        "in_progress": {"blocked", "shipped", "abandoned", "closed"},
        "blocked": {"in_progress", "abandoned", "closed"},
        "shipped": {"closed"},
        "abandoned": {"proposed"},
        "closed": set(),
    }
)

# All valid node types in schema v4.0
VALID_NODE_TYPES = frozenset({
    "Project", "Function", "Class", "Module", "Endpoint", "DataStore",
    "Config", "Test", "Person", "Task", "Email", "Commit", "Meeting",
    "Document", "Event", "Plan", "Feature", "Decision", "Signal",
    "Branch", "PR", "WebPage", "JiraIssue", "Requirement", "UseCase",
    "BusinessLogic", "RiskItem", "EvolutionPlan", "ArchDecision", "SlackChannel",
    "ProjectExpert", "TestResult",
})

VALID_EDGE_TYPES = frozenset({
    "CALLS", "IMPORTS", "CONTAINS", "IMPLEMENTS", "EXTENDS",
    "ROUTES_TO", "QUERIES", "TESTS", "GATES", "DEPENDS_ON",
    "MODIFIED", "TRIGGERED", "SNAPSHOT_OF",
    "AUTHORED_BY", "ASSIGNED_TO", "ATTENDED", "PERFORMED_BY",
    "BLOCKS", "DUE_BEFORE", "PART_OF", "DISCUSSED_IN", "RELATES_TO",
    "IMPLEMENTS_FEATURE", "SPAWNED", "DECIDED_BY", "SIGNAL_FOR",
    "PLANNED_IN", "SUPERSEDES", "BLOCKED_BY", "REVIEWED_IN", "MENTIONED_IN",
    "CROSS_REF", "HAS_REQUIREMENT", "HAS_RISK", "HAS_USE_CASE",
    "ENCODES", "GOVERNS", "OPENS_PR", "BRANCH_OF", "TRACKS",
    "MONITORS", "EVOLVES_TO", "PLANS_EVOLUTION", "MITIGATES",
    "EXTRACTED_FROM", "REFERENCES", "SYNCED_FROM",
    "EXPERT_ON", "ANALYZED_BY", "VALIDATED_BY", "INDICATES",
})


# ============================================================================
# Core DB Layer
# ============================================================================

def get_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str, workspace_name: str = "omni") -> None:
    """Initialize rubick.db with extended schema: nodes, edges, sync_state, FTS5."""
    conn = get_db(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            name TEXT NOT NULL,
            data TEXT DEFAULT '{}',
            source_type TEXT DEFAULT 'manual',
            source_id TEXT DEFAULT '',
            ingested_at TEXT DEFAULT (datetime('now')),
            confidence REAL DEFAULT 1.0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(type, name)
        );
        CREATE TABLE IF NOT EXISTS edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_node_id INTEGER NOT NULL REFERENCES nodes(id),
            to_node_id INTEGER NOT NULL REFERENCES nodes(id),
            edge_type TEXT NOT NULL,
            data TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(from_node_id, to_node_id, edge_type)
        );
        CREATE TABLE IF NOT EXISTS sync_state (
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            project_slug TEXT NOT NULL DEFAULT '_global',
            last_sync_at TEXT,
            cursor TEXT DEFAULT '',
            status TEXT DEFAULT 'ok',
            error_msg TEXT DEFAULT '',
            PRIMARY KEY(source_type, source_id, project_slug)
        );
        CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
        CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
        CREATE INDEX IF NOT EXISTS idx_nodes_source ON nodes(source_type, source_id);
        CREATE INDEX IF NOT EXISTS idx_nodes_updated ON nodes(updated_at);
        CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_node_id);
        CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_node_id);
        CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type);

        CREATE TABLE IF NOT EXISTS learning_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            interaction_id TEXT NOT NULL,
            interaction_type TEXT NOT NULL,
            source_skill TEXT NOT NULL,
            project_slug TEXT DEFAULT '_global',
            node_type TEXT NOT NULL,
            node_name TEXT NOT NULL,
            node_data TEXT DEFAULT '{}',
            confidence REAL DEFAULT 0.7,
            edges TEXT DEFAULT '[]',
            status TEXT DEFAULT 'staged',
            created_at TEXT DEFAULT (datetime('now')),
            flushed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ll_status ON learning_ledger(status);
        CREATE INDEX IF NOT EXISTS idx_ll_interaction ON learning_ledger(interaction_id);

        CREATE TABLE IF NOT EXISTS learning_provenance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            interaction_id TEXT NOT NULL,
            source_skill TEXT NOT NULL,
            node_id INTEGER REFERENCES nodes(id),
            action TEXT DEFAULT 'created',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_lp_node ON learning_provenance(node_id);

        -- Code body storage (v4.0)
        CREATE TABLE IF NOT EXISTS code_bodies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
            project_slug TEXT NOT NULL,
            file_path TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            language TEXT NOT NULL,
            body TEXT NOT NULL,
            body_hash TEXT NOT NULL,
            byte_length INTEGER NOT NULL,
            extracted_at TEXT,
            UNIQUE(node_id)
        );
        CREATE INDEX IF NOT EXISTS idx_cb_project ON code_bodies(project_slug);
        CREATE INDEX IF NOT EXISTS idx_cb_file ON code_bodies(file_path, project_slug);
        CREATE INDEX IF NOT EXISTS idx_cb_lines ON code_bodies(file_path, start_line, end_line);
        CREATE INDEX IF NOT EXISTS idx_cb_hash ON code_bodies(body_hash);

        CREATE TABLE IF NOT EXISTS code_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            body_id INTEGER NOT NULL REFERENCES code_bodies(id) ON DELETE CASCADE,
            node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            token_estimate INTEGER NOT NULL,
            UNIQUE(body_id, chunk_index)
        );
        CREATE INDEX IF NOT EXISTS idx_cc_node ON code_chunks(node_id);
        CREATE INDEX IF NOT EXISTS idx_cc_body ON code_chunks(body_id);

        CREATE TABLE IF NOT EXISTS file_extract_cache (
            file_path TEXT NOT NULL,
            project_slug TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            mtime REAL NOT NULL,
            file_size INTEGER NOT NULL,
            extracted_at TEXT,
            function_count INTEGER DEFAULT 0,
            PRIMARY KEY(file_path, project_slug)
        );

        CREATE TABLE IF NOT EXISTS reset_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reset_type TEXT NOT NULL,
            nodes_deleted INTEGER DEFAULT 0,
            edges_deleted INTEGER DEFAULT 0,
            tables_truncated TEXT,
            dirs_removed TEXT,
            timestamp TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS interaction_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            query TEXT,
            nodes_used TEXT,
            experts_consulted TEXT,
            response_quality TEXT DEFAULT 'unknown',
            tokens_used INTEGER DEFAULT 0,
            phase TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS expert_functions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expert_node_id INTEGER REFERENCES nodes(id),
            function_node_id INTEGER REFERENCES nodes(id),
            function_name TEXT,
            file_path TEXT,
            line_number INTEGER,
            callers TEXT,
            callees TEXT,
            tested_by TEXT,
            complexity REAL,
            body_hash TEXT,
            UNIQUE(expert_node_id, function_node_id)
        );

        CREATE TABLE IF NOT EXISTS expert_tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expert_node_id INTEGER REFERENCES nodes(id),
            test_node_id INTEGER REFERENCES nodes(id),
            test_name TEXT,
            file_path TEXT,
            functions_tested TEXT,
            assertion_count INTEGER DEFAULT 0,
            edge_cases TEXT,
            UNIQUE(expert_node_id, test_node_id)
        );

        CREATE TABLE IF NOT EXISTS feature_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feature_slug TEXT NOT NULL,
            phase TEXT NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            model TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_fc_slug ON feature_costs(feature_slug);
        CREATE INDEX IF NOT EXISTS idx_fc_phase ON feature_costs(feature_slug, phase);
    """)

    # FTS5 virtual table for full-text search
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts "
            "USING fts5(name, data, content=nodes, content_rowid=id)"
        )
    except sqlite3.OperationalError as e:
        logger.warning("FTS5 unavailable (search falls back to LIKE): %s", e)

    try:
        conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS nodes_ai AFTER INSERT ON nodes BEGIN
                INSERT INTO nodes_fts(rowid, name, data) VALUES (new.id, new.name, new.data);
            END;
            CREATE TRIGGER IF NOT EXISTS nodes_ad AFTER DELETE ON nodes BEGIN
                INSERT INTO nodes_fts(nodes_fts, rowid, name, data) VALUES('delete', old.id, old.name, old.data);
            END;
            CREATE TRIGGER IF NOT EXISTS nodes_au AFTER UPDATE ON nodes BEGIN
                INSERT INTO nodes_fts(nodes_fts, rowid, name, data) VALUES('delete', old.id, old.name, old.data);
                INSERT INTO nodes_fts(rowid, name, data) VALUES (new.id, new.name, new.data);
            END;
        """)
    except sqlite3.OperationalError as e:
        logger.warning("FTS5 triggers not created: %s", e)

    # FTS5 for code body search (separate from node name/metadata search)
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS code_fts "
            "USING fts5(body, content=code_bodies, content_rowid=id)"
        )
        conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS cb_ai AFTER INSERT ON code_bodies BEGIN
                INSERT INTO code_fts(rowid, body) VALUES (new.id, new.body);
            END;
            CREATE TRIGGER IF NOT EXISTS cb_ad AFTER DELETE ON code_bodies BEGIN
                INSERT INTO code_fts(code_fts, rowid, body) VALUES('delete', old.id, old.body);
            END;
            CREATE TRIGGER IF NOT EXISTS cb_au AFTER UPDATE ON code_bodies BEGIN
                INSERT INTO code_fts(code_fts, rowid, body) VALUES('delete', old.id, old.body);
                INSERT INTO code_fts(rowid, body) VALUES (new.id, new.body);
            END;
        """)
    except sqlite3.OperationalError as e:
        logger.warning("code_fts unavailable: %s", e)

    upsert_node(conn, "Project", workspace_name, {
        "initialized_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": _SCHEMA_VERSION,
        "workspace": True,
    }, source_type="system")
    conn.commit()
    conn.close()
    print(f"Initialized rubick.db: {db_path}")


# ============================================================================
# Node & Edge CRUD
# ============================================================================

def upsert_node(conn: sqlite3.Connection, ntype: str, name: str,
                data: Optional[dict] = None,
                source_type: str = "manual", source_id: str = "",
                confidence: float = 1.0,
                _batch: bool = False) -> int:
    """Insert or update a node. Returns node ID.

    Args:
        _batch: If True, skip auto-commit. Caller must commit.
    """
    data_json = json.dumps(data or {})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO nodes(type, name, data, source_type, source_id, ingested_at, confidence, updated_at)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(type, name) DO UPDATE SET
             data=excluded.data,
             source_type=excluded.source_type,
             source_id=CASE WHEN excluded.source_id != '' THEN excluded.source_id ELSE nodes.source_id END,
             ingested_at=excluded.ingested_at,
             confidence=CASE WHEN excluded.confidence > nodes.confidence THEN excluded.confidence ELSE nodes.confidence END,
             updated_at=excluded.ingested_at""",
        (ntype, name, data_json, source_type, source_id, now, confidence, now)
    )
    if not _batch:
        conn.commit()
    row = conn.execute("SELECT id FROM nodes WHERE type=? AND name=?", (ntype, name)).fetchone()
    return row["id"] if row else -1


def upsert_edge(conn: sqlite3.Connection, from_type: str, from_name: str,
                to_type: str, to_name: str, edge_type: str,
                data: Optional[dict] = None,
                _batch: bool = False) -> None:
    """Insert or update an edge. Auto-creates missing endpoint nodes.

    Args:
        _batch: If True, skip auto-commit. Caller must commit.
    """
    from_id = conn.execute("SELECT id FROM nodes WHERE type=? AND name=?", (from_type, from_name)).fetchone()
    to_id = conn.execute("SELECT id FROM nodes WHERE type=? AND name=?", (to_type, to_name)).fetchone()
    fn = upsert_node(conn, from_type, from_name, _batch=True) if not from_id else from_id["id"]
    tn = upsert_node(conn, to_type, to_name, _batch=True) if not to_id else to_id["id"]
    data_json = json.dumps(data or {})
    conn.execute(
        """INSERT INTO edges(from_node_id, to_node_id, edge_type, data)
           VALUES(?, ?, ?, ?)
           ON CONFLICT(from_node_id, to_node_id, edge_type) DO UPDATE SET data=excluded.data""",
        (fn, tn, edge_type, data_json)
    )
    if not _batch:
        conn.commit()


def delete_node(conn: sqlite3.Connection, ntype: str, name: str) -> dict:
    """Delete a node and all its edges."""
    row = conn.execute("SELECT id FROM nodes WHERE type=? AND name=?", (ntype, name)).fetchone()
    if not row:
        return {"error": f"not found: {ntype}:{name}"}
    nid = row["id"]
    edge_count = conn.execute(
        "SELECT COUNT(*) AS c FROM edges WHERE from_node_id=? OR to_node_id=?", (nid, nid)
    ).fetchone()["c"]
    conn.execute("DELETE FROM edges WHERE from_node_id=? OR to_node_id=?", (nid, nid))
    conn.execute("DELETE FROM nodes WHERE id=?", (nid,))
    conn.commit()
    return {"deleted": f"{ntype}:{name}", "edges_removed": edge_count}


def query_nodes(conn: sqlite3.Connection, ntype: Optional[str] = None,
                limit: int = 50, where_clause: Optional[str] = None,
                project_slug: Optional[str] = None) -> list:
    """Query nodes with optional type, where clause, and project filter."""
    sql = "SELECT id, type, name, data, source_type, source_id, confidence, updated_at FROM nodes"
    params: list = []
    conditions: list[str] = []
    if ntype:
        conditions.append("type = ?")
        params.append(ntype)
    if where_clause:
        conditions.append(where_clause)
    if project_slug:
        conditions.append("json_extract(data, '$.project_slug') = ?")
        params.append(project_slug)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += f" ORDER BY updated_at DESC LIMIT {limit}"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def search_text(conn: sqlite3.Connection, text: str, limit: int = 50,
                ntype: Optional[str] = None) -> list:
    """Full-text search via FTS5, falling back to LIKE."""
    try:
        sql = (
            "SELECT n.id, n.type, n.name, n.data, n.source_type, n.confidence "
            "FROM nodes_fts f JOIN nodes n ON f.rowid = n.id "
            "WHERE nodes_fts MATCH ?"
        )
        params: list = [text]
        if ntype:
            sql += " AND n.type = ?"
            params.append(ntype)
        sql += f" LIMIT {limit}"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        logger.debug("FTS5 unavailable, falling back to LIKE")
        sql = "SELECT id, type, name, data, source_type, confidence FROM nodes WHERE (name LIKE ? OR data LIKE ?)"
        params = [f"%{text}%", f"%{text}%"]
        if ntype:
            sql += " AND type = ?"
            params.append(ntype)
        sql += f" LIMIT {limit}"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_node(conn: sqlite3.Connection, ntype: str, name: str) -> Optional[dict]:
    """Get a single node by type and name."""
    row = conn.execute(
        "SELECT id, type, name, data, source_type, source_id, confidence, updated_at "
        "FROM nodes WHERE type=? AND name=?", (ntype, name)
    ).fetchone()
    return dict(row) if row else None


def get_neighbors(conn: sqlite3.Connection, ntype: str, name: str,
                  edge_type: Optional[str] = None,
                  direction: str = "both") -> list:
    """Get all neighbors of a node, optionally filtered by edge type and direction."""
    node = conn.execute("SELECT id FROM nodes WHERE type=? AND name=?", (ntype, name)).fetchone()
    if not node:
        return []
    nid = node["id"]
    results = []

    if direction in ("out", "both"):
        sql = ("SELECT n.id, n.type, n.name, n.data, e.edge_type, e.data AS edge_data "
               "FROM edges e JOIN nodes n ON e.to_node_id = n.id WHERE e.from_node_id = ?")
        params: list = [nid]
        if edge_type:
            sql += " AND e.edge_type = ?"
            params.append(edge_type)
        results.extend([dict(r) for r in conn.execute(sql, params).fetchall()])

    if direction in ("in", "both"):
        sql = ("SELECT n.id, n.type, n.name, n.data, e.edge_type, e.data AS edge_data "
               "FROM edges e JOIN nodes n ON e.from_node_id = n.id WHERE e.to_node_id = ?")
        params = [nid]
        if edge_type:
            sql += " AND e.edge_type = ?"
            params.append(edge_type)
        results.extend([dict(r) for r in conn.execute(sql, params).fetchall()])

    return results


# ============================================================================
# Sync State Management
# ============================================================================

def sync_get(conn: sqlite3.Connection, source_type: str, source_id: str,
             project_slug: str = "_global") -> Optional[dict]:
    """Get sync state for a source."""
    row = conn.execute(
        "SELECT * FROM sync_state WHERE source_type=? AND source_id=? AND project_slug=?",
        (source_type, source_id, project_slug)
    ).fetchone()
    return dict(row) if row else None


def sync_update(conn: sqlite3.Connection, source_type: str, source_id: str,
                project_slug: str = "_global",
                cursor: str = "", status: str = "ok",
                error_msg: str = "") -> None:
    """Update or insert sync state."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO sync_state(source_type, source_id, project_slug, last_sync_at, cursor, status, error_msg)
           VALUES(?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(source_type, source_id, project_slug) DO UPDATE SET
             last_sync_at=excluded.last_sync_at,
             cursor=excluded.cursor,
             status=excluded.status,
             error_msg=excluded.error_msg""",
        (source_type, source_id, project_slug, now, cursor, status, error_msg)
    )
    conn.commit()


def sync_list(conn: sqlite3.Connection,
              project_slug: Optional[str] = None) -> list:
    """List all sync states, optionally filtered by project."""
    if project_slug:
        rows = conn.execute(
            "SELECT * FROM sync_state WHERE project_slug=? ORDER BY last_sync_at DESC",
            (project_slug,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM sync_state ORDER BY last_sync_at DESC").fetchall()
    return [dict(r) for r in rows]


# ============================================================================
# Graph Analysis
# ============================================================================

def impact_analysis(conn: sqlite3.Connection, ntype: str, name: str,
                    depth: int = 3) -> dict:
    """BFS impact analysis from a root node."""
    node = conn.execute("SELECT id FROM nodes WHERE type=? AND name=?", (ntype, name)).fetchone()
    if not node:
        return {"error": f"not found: {ntype}:{name}"}
    visited: set[int] = set()
    result = {"root": f"{ntype}:{name}", "impacted": [], "depth": depth}

    def traverse(node_id: int, d: int) -> None:
        if d > depth or node_id in visited:
            return
        visited.add(node_id)
        edges = conn.execute(
            """SELECT e.edge_type, n.type, n.name FROM edges e
               JOIN nodes n ON e.to_node_id = n.id WHERE e.from_node_id = ?
               UNION
               SELECT e.edge_type, n.type, n.name FROM edges e
               JOIN nodes n ON e.from_node_id = n.id WHERE e.to_node_id = ?""",
            (node_id, node_id)
        ).fetchall()
        for e in edges:
            neighbor = conn.execute("SELECT id FROM nodes WHERE type=? AND name=?", (e["type"], e["name"])).fetchone()
            if neighbor and neighbor["id"] not in visited:
                result["impacted"].append({"type": e["type"], "name": e["name"], "edge": e["edge_type"], "depth": d})
                traverse(neighbor["id"], d + 1)

    traverse(node["id"], 1)
    return result


def export_subgraph(conn: sqlite3.Connection, ntype: Optional[str] = None,
                    where_clause: Optional[str] = None,
                    depth: int = 3, max_nodes: int = 200) -> dict:
    """Export a subgraph starting from seed nodes."""
    sql = "SELECT id, type, name, data FROM nodes"
    params: list = []
    conditions: list[str] = []
    if ntype:
        conditions.append("type = ?")
        params.append(ntype)
    if where_clause:
        conditions.append(where_clause)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += f" LIMIT {max_nodes}"
    seed_nodes = conn.execute(sql, params).fetchall()
    node_ids = {r["id"] for r in seed_nodes}
    nodes_out = [dict(r) for r in seed_nodes]

    for _ in range(depth):
        if len(node_ids) >= max_nodes:
            break
        placeholders = ",".join("?" * len(node_ids))
        new_edges = conn.execute(
            f"""SELECT DISTINCT e.*, n1.type as ft, n1.name as fn, n2.type as tt, n2.name as tn
                FROM edges e
                JOIN nodes n1 ON e.from_node_id = n1.id
                JOIN nodes n2 ON e.to_node_id = n2.id
                WHERE e.from_node_id IN ({placeholders}) OR e.to_node_id IN ({placeholders})""",
            list(node_ids) + list(node_ids)
        ).fetchall()
        for e in new_edges:
            for nid_key in ["from_node_id", "to_node_id"]:
                nid = e[nid_key]
                if nid not in node_ids and len(node_ids) < max_nodes:
                    node_ids.add(nid)
                    row = conn.execute("SELECT id, type, name, data FROM nodes WHERE id=?", (nid,)).fetchone()
                    if row:
                        nodes_out.append(dict(row))

    placeholders = ",".join("?" * len(node_ids))
    edges_out = conn.execute(
        f"SELECT from_node_id, to_node_id, edge_type, data FROM edges "
        f"WHERE from_node_id IN ({placeholders}) AND to_node_id IN ({placeholders})",
        list(node_ids) + list(node_ids)
    ).fetchall()
    return {
        "nodes": nodes_out, "edges": [dict(e) for e in edges_out],
        "total_nodes": len(nodes_out), "total_edges": len(edges_out),
    }


def find_hotspots(conn: sqlite3.Connection, threshold: int = 5) -> list:
    rows = conn.execute(
        """SELECT n.type, n.name, n.data, COUNT(e.id) as edge_count
           FROM nodes n JOIN edges e ON e.to_node_id = n.id
           GROUP BY n.id HAVING edge_count >= ? ORDER BY edge_count DESC""",
        (threshold,)
    ).fetchall()
    return [dict(r) for r in rows]


def find_orphans(conn: sqlite3.Connection) -> list:
    rows = conn.execute(
        """SELECT n.type, n.name, n.data FROM nodes n
           WHERE n.id NOT IN (SELECT from_node_id FROM edges)
           AND n.id NOT IN (SELECT to_node_id FROM edges)
           AND n.type NOT IN ('Project', 'Event', 'SlackChannel')"""
    ).fetchall()
    return [dict(r) for r in rows]


def find_cycles(conn: sqlite3.Connection) -> list:
    graph: dict[int, list[int]] = {}
    edges = conn.execute(
        "SELECT from_node_id, to_node_id FROM edges WHERE edge_type IN ('CALLS', 'IMPORTS', 'DEPENDS_ON')"
    ).fetchall()
    for e in edges:
        graph.setdefault(e["from_node_id"], []).append(e["to_node_id"])

    cycles: list[list[str]] = []
    visited: set[int] = set()
    rec_stack: list[int] = []
    rec_set: set[int] = set()

    def dfs(node: int) -> None:
        visited.add(node)
        rec_stack.append(node)
        rec_set.add(node)
        for succ in graph.get(node, []):
            if succ not in visited:
                dfs(succ)
            elif succ in rec_set:
                start = rec_stack.index(succ)
                cycle_ids = rec_stack[start:]
                names = []
                for cid in cycle_ids:
                    r = conn.execute("SELECT type, name FROM nodes WHERE id=?", (cid,)).fetchone()
                    if r:
                        names.append(f"{r['type']}:{r['name']}")
                cycles.append(names)
                if len(cycles) >= 20:
                    return
        rec_stack.pop()
        rec_set.discard(node)

    for nid in graph:
        if nid not in visited:
            dfs(nid)
            if len(cycles) >= 20:
                break
    return cycles


def find_untested(conn: sqlite3.Connection) -> list:
    rows = conn.execute(
        """SELECT n.type, n.name, n.data FROM nodes n
           WHERE n.type = 'Function'
           AND n.id NOT IN (SELECT e.from_node_id FROM edges e WHERE e.edge_type = 'TESTS')
           AND n.id NOT IN (SELECT e.to_node_id FROM edges e WHERE e.edge_type = 'TESTS')"""
    ).fetchall()
    return [dict(r) for r in rows]


def find_unauthed(conn: sqlite3.Connection) -> list:
    rows = conn.execute(
        """SELECT n.type, n.name, n.data FROM nodes n
           WHERE n.type = 'Endpoint'
           AND n.id NOT IN (SELECT e.from_node_id FROM edges e WHERE e.edge_type = 'GATES')
           AND n.id NOT IN (SELECT e.to_node_id FROM edges e WHERE e.edge_type = 'GATES')"""
    ).fetchall()
    return [dict(r) for r in rows]


def find_high_complexity(conn: sqlite3.Connection, threshold: float = 0.7) -> list:
    rows = conn.execute("SELECT type, name, data FROM nodes WHERE type = 'Function'").fetchall()
    result = []
    for r in rows:
        try:
            d = json.loads(r["data"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if d.get("complexity", 0) > threshold:
            result.append(dict(r))
    return sorted(result, key=lambda x: json.loads(x.get("data") or "{}").get("complexity", 0), reverse=True)


def find_stale_signals(conn: sqlite3.Connection, days: int = 7) -> list:
    """Find unprocessed signals older than N days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        """SELECT id, type, name, data, updated_at FROM nodes
           WHERE type = 'Signal' AND updated_at < ?
           AND (json_extract(data, '$.processed') IS NULL OR json_extract(data, '$.processed') = 0)
           ORDER BY updated_at ASC LIMIT 100""",
        (cutoff,)
    ).fetchall()
    return [dict(r) for r in rows]


# ============================================================================
# Cross-Project Intelligence
# ============================================================================

def find_cross_refs(conn: sqlite3.Connection, text: str,
                    exclude_project: Optional[str] = None,
                    min_similarity: float = 0.3,
                    limit: int = 20) -> list:
    """Find cross-project references via FTS5 text similarity.

    Searches across all projects for nodes whose name or data
    matches the query text, excluding nodes from the source project.
    """
    hits = search_text(conn, text, limit=limit * 2)
    results = []
    for h in hits:
        try:
            d = json.loads(h.get("data") or "{}")
        except (json.JSONDecodeError, TypeError):
            d = {}
        proj = d.get("project_slug", "")
        if exclude_project and proj == exclude_project:
            continue
        results.append({
            "type": h["type"],
            "name": h["name"],
            "project_slug": proj,
            "source_type": h.get("source_type", ""),
            "confidence": h.get("confidence", 1.0),
        })
        if len(results) >= limit:
            break
    return results


def seed_projects(conn: sqlite3.Connection,
                  projects: Optional[list[dict]] = None) -> dict:
    """Seed the graph with known project nodes from config."""
    projects = projects or (cfg.SEED_PROJECTS if cfg else [])
    created = 0
    for p in projects:
        existing = get_node(conn, "Project", p["slug"])
        if not existing:
            upsert_node(conn, "Project", p["slug"], {
                "name": p["slug"],
                "url": p.get("url", ""),
                "role": p.get("role", "ecosystem"),
                "schema_version": _SCHEMA_VERSION,
            }, source_type="seed")
            created += 1
    return {"seeded": created, "total": len(projects)}


def seed_channels(conn: sqlite3.Connection,
                  channels: Optional[list[str]] = None) -> dict:
    """Seed Slack channels from config."""
    channels = channels or (cfg.SEED_CHANNELS if cfg else [])
    created = 0
    for ch in channels:
        existing = get_node(conn, "SlackChannel", ch)
        if not existing:
            upsert_node(conn, "SlackChannel", ch, {"name": ch}, source_type="seed")
            created += 1
    return {"seeded": created, "total": len(channels)}


# ============================================================================
# Datetime Helpers
# ============================================================================

def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        v = value.replace("Z", "+00:00")
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def _now(now: Union[None, str, datetime] = None) -> datetime:
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    if isinstance(now, str):
        parsed = _parse_iso(now)
        if parsed:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _node_data(row: dict) -> dict:
    try:
        return json.loads(row.get("data") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower())
    return s.strip("-")


# ============================================================================
# Planner Engine — DAG, Topological Sort, Critical Path, Priority Scoring
# ============================================================================

def _filter_tasks_by_scope(tasks: list, scope: str, ref_now: datetime) -> list:
    if scope not in _SCOPE_HOURS:
        return tasks
    horizon = ref_now + timedelta(hours=_SCOPE_HOURS[scope])
    out = []
    for t in tasks:
        d = _node_data(t)
        due = _parse_iso(d.get("due_date"))
        if due is not None and due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        if due is None or due <= horizon:
            out.append(t)
    return out


def _filter_tasks_by_assignee(tasks: list, assignee: Optional[str]) -> list:
    if not assignee:
        return tasks
    return [t for t in tasks if _node_data(t).get("assignee") == assignee]


def _exclude_done(tasks: list) -> list:
    return [t for t in tasks if _node_data(t).get("status") not in ("done", "completed")]


def dag_build(conn: sqlite3.Connection, scope: str = "today",
              assignee: Optional[str] = None,
              ref_now: Union[None, str, datetime] = None) -> dict:
    """Build planning DAG from Task nodes + BLOCKS/DUE_BEFORE edges."""
    ref_now_dt = _now(ref_now)
    tasks = query_nodes(conn, ntype="Task", limit=_PLAN_TASK_LIMIT)
    tasks = _exclude_done(tasks)
    tasks = _filter_tasks_by_scope(tasks, scope, ref_now_dt)
    tasks = _filter_tasks_by_assignee(tasks, assignee)

    task_names = {t["name"] for t in tasks}
    edge_rows = conn.execute(
        """SELECT n1.name AS from_name, n2.name AS to_name, e.edge_type
           FROM edges e
           JOIN nodes n1 ON e.from_node_id = n1.id
           JOIN nodes n2 ON e.to_node_id = n2.id
           WHERE e.edge_type IN ('BLOCKS', 'DUE_BEFORE')
             AND n1.type = 'Task' AND n2.type = 'Task'"""
    ).fetchall()

    edges = []
    adjacency: dict[str, list[str]] = {name: [] for name in task_names}
    in_degree: dict[str, int] = {name: 0 for name in task_names}

    for e in edge_rows:
        if e["from_name"] not in task_names or e["to_name"] not in task_names:
            continue
        edges.append({"from": e["from_name"], "to": e["to_name"], "type": e["edge_type"]})
        adjacency[e["from_name"]].append(e["to_name"])
        in_degree[e["to_name"]] += 1

    roots = sorted(n for n, d in in_degree.items() if d == 0)
    leaves = sorted(n for n, succs in adjacency.items() if not succs)

    return {
        "nodes": [dict(t) for t in tasks], "edges": edges,
        "adjacency": adjacency, "in_degree": in_degree,
        "root_tasks": roots, "leaf_tasks": leaves,
        "scope": scope, "assignee": assignee,
        "generated_at": ref_now_dt.isoformat(),
    }


def _detect_task_cycles(adjacency: dict[str, list[str]]) -> list:
    cycles: list[list[str]] = []
    visited: set[str] = set()
    rec_stack: list[str] = []
    rec_set: set[str] = set()

    def dfs(node: str) -> None:
        visited.add(node)
        rec_stack.append(node)
        rec_set.add(node)
        for succ in adjacency.get(node, []):
            if succ not in visited:
                dfs(succ)
            elif succ in rec_set:
                start = rec_stack.index(succ)
                cycles.append(rec_stack[start:] + [succ])
        rec_stack.pop()
        rec_set.discard(node)

    for n in list(adjacency.keys()):
        if n not in visited:
            dfs(n)
    return cycles


def topo_sort(conn: sqlite3.Connection, scope: str = "today",
              assignee: Optional[str] = None,
              ref_now: Union[None, str, datetime] = None,
              dag: Optional[dict] = None) -> dict:
    """Kahn's algorithm topological sort."""
    if dag is None:
        dag = dag_build(conn, scope=scope, assignee=assignee, ref_now=ref_now)

    cycle_chains = _detect_task_cycles(dag["adjacency"])
    if cycle_chains:
        return {"error": "circular_dependencies", "cycles": cycle_chains, "ordered": []}

    in_degree = dict(dag["in_degree"])
    queue = deque(sorted(n for n, d in in_degree.items() if d == 0))
    ordered: list[str] = []
    while queue:
        node = queue.popleft()
        ordered.append(node)
        for succ in sorted(dag["adjacency"].get(node, [])):
            in_degree[succ] -= 1
            if in_degree[succ] == 0:
                queue.append(succ)

    if len(ordered) != len(dag["adjacency"]):
        return {"error": "incomplete_sort", "cycles": [], "ordered": ordered}
    return {"ordered": ordered, "scope": dag["scope"], "generated_at": dag["generated_at"]}


def critical_path(conn: sqlite3.Connection, scope: str = "today",
                  assignee: Optional[str] = None,
                  ref_now: Union[None, str, datetime] = None,
                  dag: Optional[dict] = None) -> dict:
    """Critical Path Method (CPM) on task DAG."""
    if dag is None:
        dag = dag_build(conn, scope=scope, assignee=assignee, ref_now=ref_now)

    topo = topo_sort(conn, dag=dag)
    if "error" in topo:
        return {"error": topo["error"], "cycles": topo.get("cycles", [])}

    name_to_data = {t["name"]: _node_data(t) for t in dag["nodes"]}
    duration = {
        n: float(name_to_data.get(n, {}).get("estimated_hours", _DEFAULT_ESTIMATED_HOURS))
        for n in dag["adjacency"]
    }

    predecessors: dict[str, list[str]] = {n: [] for n in dag["adjacency"]}
    for from_name, succs in dag["adjacency"].items():
        for s in succs:
            predecessors.setdefault(s, []).append(from_name)

    es: dict[str, float] = {}
    ef: dict[str, float] = {}
    for node in topo["ordered"]:
        preds = predecessors.get(node, [])
        es[node] = max((ef[p] for p in preds), default=0.0)
        ef[node] = es[node] + duration[node]

    project_duration = max(ef.values(), default=0.0)

    lf: dict[str, float] = {}
    ls: dict[str, float] = {}
    for node in reversed(topo["ordered"]):
        succs = dag["adjacency"].get(node, [])
        lf[node] = min((ls[s] for s in succs), default=project_duration)
        ls[node] = lf[node] - duration[node]

    task_times: dict[str, dict] = {}
    critical: list[str] = []
    for node in topo["ordered"]:
        float_hours = round(ls[node] - es[node], 4)
        task_times[node] = {
            "duration_hours": duration[node],
            "earliest_start_hours": round(es[node], 4),
            "earliest_finish_hours": round(ef[node], 4),
            "latest_start_hours": round(ls[node], 4),
            "latest_finish_hours": round(lf[node], 4),
            "float_hours": float_hours,
            "is_critical": float_hours == 0.0,
        }
        if float_hours == 0.0:
            critical.append(node)

    return {
        "critical_path": critical, "task_times": task_times,
        "project_duration_hours": round(project_duration, 4),
        "scope": dag["scope"], "generated_at": dag["generated_at"],
    }


def _time_proximity_score(due_date: Any, ref_now: datetime) -> float:
    if due_date is None:
        return 0.1
    due = _parse_iso(due_date) if isinstance(due_date, str) else due_date
    if due is None:
        return 0.1
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    hours_until = (due - ref_now).total_seconds() / 3600.0
    if hours_until < 0:
        return 1.0
    for bucket_hours, score in _TIME_BUCKETS:
        if hours_until <= bucket_hours:
            return score
    return 0.0


def _priority_label(score: float) -> str:
    if cfg:
        return cfg.priority_label(score)
    if score >= 0.8:
        return "Critical"
    if score >= 0.6:
        return "High"
    if score >= 0.4:
        return "Medium"
    return "Low"


def priority_score(conn: sqlite3.Connection,
                   weights: Optional[dict] = None,
                   ref_now: Union[None, str, datetime] = None,
                   scope: Optional[str] = None,
                   assignee: Optional[str] = None) -> list:
    """Compute deterministic priority scores for Task nodes."""
    ref_now_dt = _now(ref_now)
    weights = weights or _DEFAULT_WEIGHTS

    tasks = query_nodes(conn, ntype="Task", limit=_PLAN_TASK_LIMIT)
    tasks = _exclude_done(tasks)
    if scope:
        tasks = _filter_tasks_by_scope(tasks, scope, ref_now_dt)
    if assignee:
        tasks = _filter_tasks_by_assignee(tasks, assignee)

    scored = []
    for t in tasks:
        d = _node_data(t)
        t_factor = _time_proximity_score(d.get("due_date"), ref_now_dt)
        u_factor = float(d.get("urgency_score", _DEFAULT_URGENCY))
        a_factor = _ACTION_SCORES.get(d.get("action_type", "fyi"), 0.2)
        s_factor = float(d.get("stakeholder_score", _DEFAULT_STAKEHOLDER))

        score = (
            weights["time_proximity"] * t_factor
            + weights["urgency_signals"] * u_factor
            + weights["action_required"] * a_factor
            + weights["stakeholder"] * s_factor
        )
        score = round(score, 4)

        scored.append({
            "task_id": d.get("id") or t["name"],
            "name": t["name"],
            "score": score,
            "label": _priority_label(score),
            "factors": {
                "time": round(t_factor, 4),
                "urgency": round(u_factor, 4),
                "action": round(a_factor, 4),
                "stakeholder": round(s_factor, 4),
            },
            "estimated_hours": float(d.get("estimated_hours", _DEFAULT_ESTIMATED_HOURS)),
            "due_date": d.get("due_date"),
            "status": d.get("status", "open"),
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


# ============================================================================
# Calendar Slots, Capacity, Slot Matching
# ============================================================================

def _load_slots(slots_path: str) -> dict:
    p = Path(slots_path)
    if not p.is_file():
        raise FileNotFoundError(f"slots file not found: {slots_path}")
    with p.open() as f:
        data = json.load(f)
    if "slots" not in data:
        raise ValueError(f"slots file missing 'slots' key: {slots_path}")
    return data


def build_slots(meetings_json: list,
                working_hours_start: str = "09:00",
                working_hours_end: str = "19:00",
                timezone_str: str = "Asia/Kolkata",
                min_slot_min: int = 30,
                scope_days: int = 1,
                ref_date: Optional[str] = None) -> dict:
    """Compute free calendar slots from meetings. Pure function — no DB."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        ZoneInfo = None  # type: ignore[assignment]

    _wh_start = working_hours_start or "09:00"
    _wh_end = working_hours_end or "19:00"

    start_h, start_m = (int(x) for x in _wh_start.split(":"))
    end_h, end_m = (int(x) for x in _wh_end.split(":"))

    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(timezone_str)
        except KeyError:
            tz = timezone.utc
    else:
        tz = timezone(timedelta(hours=5, minutes=30))

    if ref_date:
        try:
            base = datetime.strptime(ref_date, "%Y-%m-%d").replace(tzinfo=tz)
        except ValueError:
            base = datetime.now(tz=tz).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        base = datetime.now(tz=tz).replace(hour=0, minute=0, second=0, microsecond=0)

    parsed_meetings = []
    for m in meetings_json:
        ms = _parse_iso(m.get("start"))
        me = _parse_iso(m.get("end"))
        if ms and me:
            if ms.tzinfo is None:
                ms = ms.replace(tzinfo=tz)
            if me.tzinfo is None:
                me = me.replace(tzinfo=tz)
            parsed_meetings.append({"start": ms, "end": me, "title": m.get("title", "")})
    parsed_meetings.sort(key=lambda x: x["start"])

    all_slots = []
    all_meetings_out = []
    total_free = 0
    total_meeting = 0

    for day_offset in range(scope_days):
        day = base + timedelta(days=day_offset)
        day_start = day.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        day_end = day.replace(hour=end_h, minute=end_m, second=0, microsecond=0)

        day_meetings = [m for m in parsed_meetings if m["end"] > day_start and m["start"] < day_end]
        clamped = []
        for m in day_meetings:
            cs = max(m["start"], day_start)
            ce = min(m["end"], day_end)
            if ce > cs:
                clamped.append({"start": cs, "end": ce, "title": m["title"]})
                dur = int((ce - cs).total_seconds() / 60)
                total_meeting += dur
                all_meetings_out.append({
                    "start": cs.isoformat(), "end": ce.isoformat(),
                    "title": m["title"], "duration_min": dur, "type": "meeting",
                })

        cursor = day_start
        for m in sorted(clamped, key=lambda x: x["start"]):
            if m["start"] > cursor:
                gap_min = int((m["start"] - cursor).total_seconds() / 60)
                if gap_min >= min_slot_min:
                    all_slots.append({
                        "start": cursor.isoformat(), "end": m["start"].isoformat(),
                        "duration_min": gap_min, "type": "free",
                    })
                    total_free += gap_min
            cursor = max(cursor, m["end"])

        if cursor < day_end:
            gap_min = int((day_end - cursor).total_seconds() / 60)
            if gap_min >= min_slot_min:
                all_slots.append({
                    "start": cursor.isoformat(), "end": day_end.isoformat(),
                    "duration_min": gap_min, "type": "free",
                })
                total_free += gap_min

    return {
        "generated_at": datetime.now(tz=tz).isoformat(),
        "working_hours": {"start": _wh_start, "end": _wh_end},
        "timezone": timezone_str, "scope_days": scope_days,
        "ref_date": base.strftime("%Y-%m-%d"),
        "slots": all_slots, "meetings": all_meetings_out,
        "total_free_minutes": total_free, "total_meeting_minutes": total_meeting,
    }


def capacity(conn: sqlite3.Connection, slots_path: str, scope: str = "today",
             assignee: Optional[str] = None,
             ref_now: Union[None, str, datetime] = None) -> dict:
    """Compare task hours vs available slot hours."""
    slots_data = _load_slots(slots_path)
    available_min = sum(
        s.get("duration_min", 0) for s in slots_data["slots"] if s.get("type", "free") == "free"
    )
    available_hours = round(available_min / 60.0, 4)

    scored = priority_score(conn, scope=scope, assignee=assignee, ref_now=ref_now)
    task_hours = round(sum(s["estimated_hours"] for s in scored), 4)

    ratio = round(task_hours / available_hours, 4) if available_hours > 0 else float("inf")
    if ratio <= _CAPACITY_HEALTHY:
        status = "healthy"
    elif ratio <= _CAPACITY_TIGHT:
        status = "tight"
    else:
        status = "overcommitted"

    return {
        "status": status,
        "ratio": ratio if ratio != float("inf") else None,
        "available_hours": available_hours,
        "task_hours": task_hours,
        "slack_hours": round(max(available_hours - task_hours, 0.0), 4),
        "scope": scope,
        "task_count": len(scored),
    }


def slot_match(conn: sqlite3.Connection, slots_path: str,
               weights: Optional[dict] = None, scope: str = "today",
               assignee: Optional[str] = None,
               ref_now: Union[None, str, datetime] = None,
               preferences: Optional[dict] = None) -> dict:
    """Greedy task-to-slot assignment."""
    slots_data = _load_slots(slots_path)
    preferences = preferences or {}
    deep_work_hours = preferences.get("deep_work_hours", "morning")
    min_focus_min = preferences.get("focus_block_minimum_min", 60)

    scored = priority_score(conn, weights=weights, scope=scope, assignee=assignee, ref_now=ref_now)

    slots = []
    for idx, s in enumerate(slots_data["slots"]):
        if s.get("type", "free") != "free":
            continue
        start = _parse_iso(s.get("start"))
        if start is None:
            continue
        slots.append({
            "idx": idx, "start": start, "end": _parse_iso(s.get("end")),
            "remaining_min": int(s.get("duration_min", 0)),
        })

    if deep_work_hours == "morning":
        slots.sort(key=lambda s: (s["start"].hour >= 12, s["start"]))
    else:
        slots.sort(key=lambda s: s["start"])

    schedule = []
    unschedulable = []

    for task in scored:
        needed_min = max(int(round(task["estimated_hours"] * 60)), 5)
        placed = False
        for slot in slots:
            if task["label"] in ("Critical", "High") and slot["remaining_min"] < max(needed_min, min_focus_min):
                continue
            if slot["remaining_min"] < needed_min:
                continue
            slot_start = slot["end"] - timedelta(minutes=slot["remaining_min"]) if slot["end"] else slot["start"]
            slot_end = slot_start + timedelta(minutes=needed_min)
            schedule.append({
                "slot_start": slot_start.isoformat(),
                "slot_end": slot_end.isoformat(),
                "task_name": task["name"],
                "task_id": task["task_id"],
                "duration_min": needed_min,
                "score": task["score"],
                "label": task["label"],
            })
            slot["remaining_min"] -= needed_min
            placed = True
            break
        if not placed:
            unschedulable.append({
                "task_name": task["name"], "task_id": task["task_id"],
                "reason": f"No free slot of {needed_min}min available",
                "estimated_hours": task["estimated_hours"],
            })

    return {
        "schedule": schedule, "unschedulable": unschedulable,
        "task_count": len(scored), "slot_count": len(slots), "scope": scope,
    }


def _detect_conflicts(slots_data: dict, capacity_info: dict, match_result: dict) -> list:
    """Detect schedule conflicts."""
    conflicts = []

    if capacity_info["status"] == "overcommitted":
        conflicts.append({
            "type": "overcommit", "severity": "warning",
            "message": f"{capacity_info['task_hours']}h vs {capacity_info['available_hours']}h available",
            "affected_tasks": [t["task_name"] for t in match_result["unschedulable"]],
        })

    for t in match_result["unschedulable"]:
        if t.get("estimated_hours", 0) >= 2:
            conflicts.append({
                "type": "deadline_crunch", "severity": "warning",
                "message": f"No slot for '{t['task_name']}' ({t['estimated_hours']}h)",
                "affected_tasks": [t["task_name"]],
            })

    no_break_min = cfg.NO_BREAK_CHAIN_MIN_MEETINGS if cfg else 3
    no_break_gap = cfg.NO_BREAK_CHAIN_MAX_GAP_MIN if cfg else 15
    meetings = sorted(
        [m for m in slots_data.get("meetings", []) if _parse_iso(m.get("start")) and _parse_iso(m.get("end"))],
        key=lambda m: _parse_iso(m["start"])
    )
    chain: list[dict] = []
    for m in meetings:
        if not chain:
            chain.append(m)
            continue
        prev_end = _parse_iso(chain[-1]["end"])
        cur_start = _parse_iso(m["start"])
        gap_min = (cur_start - prev_end).total_seconds() / 60.0
        if gap_min <= no_break_gap:
            chain.append(m)
        else:
            if len(chain) >= no_break_min:
                conflicts.append({
                    "type": "no_break_chain", "severity": "info",
                    "message": f"Back-to-back: {len(chain)} meetings {chain[0]['start']} to {chain[-1]['end']}",
                    "affected_tasks": [],
                })
            chain = [m]
    if len(chain) >= no_break_min:
        conflicts.append({
            "type": "no_break_chain", "severity": "info",
            "message": f"Back-to-back: {len(chain)} meetings {chain[0]['start']} to {chain[-1]['end']}",
            "affected_tasks": [],
        })
    return conflicts


def _make_plan_id(ref_now: datetime) -> str:
    return "plan_" + ref_now.strftime("%Y%m%d_%H%M")


def plan(conn: sqlite3.Connection, slots_path: str,
         weights: Optional[dict] = None, scope: str = "today",
         assignee: Optional[str] = None,
         ref_now: Union[None, str, datetime] = None,
         preferences: Optional[dict] = None,
         persist: bool = False) -> dict:
    """Generate a complete plan: scored tasks, schedule, capacity, conflicts."""
    ref_now_dt = _now(ref_now)
    plan_id = _make_plan_id(ref_now_dt)

    dag = dag_build(conn, scope=scope, assignee=assignee, ref_now=ref_now_dt)
    topo = topo_sort(conn, dag=dag)
    cpm = critical_path(conn, dag=dag)
    scored = priority_score(conn, weights=weights, ref_now=ref_now_dt, scope=scope, assignee=assignee)
    cap = capacity(conn, slots_path, scope=scope, assignee=assignee, ref_now=ref_now_dt)
    match = slot_match(conn, slots_path, weights=weights, scope=scope,
                       assignee=assignee, ref_now=ref_now_dt, preferences=preferences)

    slots_data = _load_slots(slots_path)
    conflicts = _detect_conflicts(slots_data, cap, match)

    critical_with_meta = []
    if "critical_path" in cpm:
        times = cpm.get("task_times", {})
        for name in cpm["critical_path"]:
            t = times.get(name, {})
            critical_with_meta.append({
                "task_name": name,
                "duration_hours": t.get("duration_hours"),
                "earliest_start_hours": t.get("earliest_start_hours"),
                "float_hours": t.get("float_hours"),
            })

    plan_doc = {
        "plan_id": plan_id, "scope": scope, "assignee": assignee,
        "generated_at": ref_now_dt.isoformat(),
        "capacity": cap, "critical_path": critical_with_meta,
        "project_duration_hours": cpm.get("project_duration_hours"),
        "prioritized_tasks": scored,
        "schedule": match["schedule"],
        "unschedulable": match["unschedulable"],
        "conflicts": conflicts,
        "circular_deps": topo.get("cycles", []) if "error" in topo else [],
        "task_count": len(scored),
        "scheduled_count": len(match["schedule"]),
        "slot_count": match["slot_count"],
    }

    if persist:
        _persist_plan(conn, plan_doc)
    return plan_doc


def _persist_plan(conn: sqlite3.Connection, plan_doc: dict) -> str:
    plan_id = plan_doc["plan_id"]
    cap = plan_doc["capacity"]

    try:
        with conn:
            prev = conn.execute(
                "SELECT name FROM nodes WHERE type='Plan' AND name != ? ORDER BY created_at DESC LIMIT 1",
                (plan_id,)
            ).fetchone()

            upsert_node(conn, "Plan", plan_id, {
                "scope": plan_doc["scope"],
                "assignee": plan_doc["assignee"],
                "generated_at": plan_doc["generated_at"],
                "capacity_status": cap["status"],
                "capacity_ratio": cap["ratio"],
                "available_hours": cap["available_hours"],
                "task_hours": cap["task_hours"],
                "schedule_json": json.dumps(plan_doc["schedule"]),
                "conflicts_json": json.dumps(plan_doc["conflicts"]),
                "circular_deps_json": json.dumps(plan_doc["circular_deps"]),
                "task_count": plan_doc["task_count"],
                "scheduled_count": plan_doc["scheduled_count"],
            }, source_type="planner")

            for item in plan_doc["schedule"]:
                upsert_edge(conn, "Task", item["task_name"], "Plan", plan_id, "PLANNED_IN", {
                    "slot_start": item["slot_start"],
                    "slot_end": item["slot_end"],
                    "score": item["score"],
                })

            if prev:
                upsert_edge(conn, "Plan", plan_id, "Plan", prev["name"], "SUPERSEDES", {
                    "superseded_at": plan_doc["generated_at"],
                    "reason": "scheduled_refresh",
                })
    except sqlite3.Error as e:
        logger.error("failed to persist plan %s: %s", plan_id, e)
        raise
    return plan_id


def export_plan(conn: sqlite3.Connection,
                plan_name: Optional[str] = None) -> dict:
    """Reconstruct plan JSON from a stored Plan node."""
    if plan_name:
        row = conn.execute(
            "SELECT id, name, data FROM nodes WHERE type='Plan' AND name=?", (plan_name,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, name, data FROM nodes WHERE type='Plan' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

    if not row:
        return {"error": "no plan found" + (f": {plan_name}" if plan_name else "")}

    data = json.loads(row["data"] or "{}")
    plan_id = row["name"]
    plan_node_id = row["id"]

    schedule = json.loads(data.get("schedule_json") or "[]")
    conflicts = json.loads(data.get("conflicts_json") or "[]")
    circular_deps = json.loads(data.get("circular_deps_json") or "[]")

    task_rows = conn.execute(
        """SELECT n.name, n.data, e.data AS edge_data FROM edges e
           JOIN nodes n ON e.from_node_id = n.id
           WHERE e.to_node_id = ? AND e.edge_type = 'PLANNED_IN' AND n.type = 'Task'""",
        (plan_node_id,)
    ).fetchall()

    prioritized_tasks = []
    for t in task_rows:
        td = json.loads(t["data"] or "{}")
        ed = json.loads(t["edge_data"] or "{}")
        prioritized_tasks.append({
            "name": t["name"], "task_id": td.get("id") or t["name"],
            "score": ed.get("score", 0.0), "label": _priority_label(ed.get("score", 0.0)),
            "estimated_hours": float(td.get("estimated_hours", _DEFAULT_ESTIMATED_HOURS)),
            "status": td.get("status", "open"), "due_date": td.get("due_date"),
        })
    prioritized_tasks.sort(key=lambda x: x["score"], reverse=True)

    return {
        "plan_id": plan_id, "scope": data.get("scope"),
        "assignee": data.get("assignee"), "generated_at": data.get("generated_at"),
        "capacity": {
            "status": data.get("capacity_status"), "ratio": data.get("capacity_ratio"),
            "available_hours": data.get("available_hours"), "task_hours": data.get("task_hours"),
        },
        "prioritized_tasks": prioritized_tasks,
        "schedule": schedule, "conflicts": conflicts,
        "circular_deps": circular_deps,
        "task_count": data.get("task_count", len(prioritized_tasks)),
        "scheduled_count": data.get("scheduled_count", len(schedule)),
    }


def plan_diff(plan_a: Union[dict, str], plan_b: Union[dict, str]) -> dict:
    """Compare two plan JSON dicts. Returns structured diff."""
    if isinstance(plan_a, str):
        with open(plan_a) as f:
            plan_a = json.load(f)
    if isinstance(plan_b, str):
        with open(plan_b) as f:
            plan_b = json.load(f)

    tasks_a = {t["name"]: t for t in plan_a.get("prioritized_tasks", [])}
    tasks_b = {t["name"]: t for t in plan_b.get("prioritized_tasks", [])}
    schedule_a = {s["task_name"]: s for s in plan_a.get("schedule", [])}
    schedule_b = {s["task_name"]: s for s in plan_b.get("schedule", [])}

    added = [n for n in tasks_b if n not in tasks_a]
    removed = [n for n in tasks_a if n not in tasks_b]

    rescheduled = []
    for name in set(schedule_a) & set(schedule_b):
        if schedule_a[name]["slot_start"] != schedule_b[name]["slot_start"]:
            rescheduled.append({
                "task_name": name,
                "old_slot": schedule_a[name]["slot_start"],
                "new_slot": schedule_b[name]["slot_start"],
            })

    priority_changes = []
    for name in set(tasks_a) & set(tasks_b):
        old_s = tasks_a[name].get("score")
        new_s = tasks_b[name].get("score")
        if old_s is not None and new_s is not None and abs(old_s - new_s) >= 0.05:
            priority_changes.append({
                "task_name": name,
                "old_score": old_s, "new_score": new_s,
                "old_label": tasks_a[name].get("label"),
                "new_label": tasks_b[name].get("label"),
            })

    cap_a = plan_a.get("capacity", {})
    cap_b = plan_b.get("capacity", {})
    return {
        "plan_a_id": plan_a.get("plan_id"), "plan_b_id": plan_b.get("plan_id"),
        "added_tasks": added, "removed_tasks": removed,
        "rescheduled": rescheduled, "priority_changes": priority_changes,
        "newly_scheduled": [n for n in schedule_b if n not in schedule_a],
        "newly_unscheduled": [n for n in schedule_a if n not in schedule_b],
        "capacity_shift": {
            "old_status": cap_a.get("status"), "new_status": cap_b.get("status"),
            "old_ratio": cap_a.get("ratio"), "new_ratio": cap_b.get("ratio"),
        },
        "no_changes": (not added and not removed and not rescheduled and not priority_changes),
    }


# ============================================================================
# Feature Lifecycle
# ============================================================================

def feature_create(conn: sqlite3.Connection, name: str,
                   owner: Optional[str] = None, priority: str = "P2",
                   description: str = "", acceptance_criteria: str = "",
                   estimated_days: Optional[float] = None,
                   status: str = "proposed",
                   project_slug: Optional[str] = None) -> dict:
    """Create or update a Feature node."""
    if not name or not name.strip():
        return {"error": "feature name required"}

    existing = conn.execute("SELECT data FROM nodes WHERE type='Feature' AND name=?", (name,)).fetchone()
    if existing:
        return {"status": "exists", "name": name, "data": json.loads(existing["data"])}

    now_iso = datetime.now(timezone.utc).isoformat()
    data = {
        "slug": _slugify(name),
        "status": status,
        "started_at": now_iso if status == "in_progress" else None,
        "shipped_at": None, "closed_at": None, "abandoned_at": None,
        "owner": owner, "priority": priority,
        "description": description,
        "acceptance_criteria": acceptance_criteria,
        "estimated_days": estimated_days,
        "project_slug": project_slug,
    }
    nid = upsert_node(conn, "Feature", name, data, source_type="manual")
    return {"status": "created", "id": nid, "name": name, "data": data}


def feature_update(conn: sqlite3.Connection, name: str,
                   status: Optional[str] = None,
                   owner: Optional[str] = None,
                   priority: Optional[str] = None,
                   description: Optional[str] = None,
                   acceptance_criteria: Optional[str] = None,
                   estimated_days: Optional[float] = None) -> dict:
    """Update a Feature node with status transition validation."""
    if not name or not name.strip():
        return {"error": "feature name required"}

    existing = conn.execute("SELECT id, data FROM nodes WHERE type='Feature' AND name=?", (name,)).fetchone()
    if not existing:
        return {"error": f"feature not found: {name}"}

    data = json.loads(existing["data"] or "{}")
    changes: dict[str, Any] = {}
    now_iso = datetime.now(timezone.utc).isoformat()

    if status is not None and status != data.get("status"):
        current = data.get("status", "proposed")
        allowed = _FEATURE_VALID_TRANSITIONS.get(current, set())
        if status not in allowed:
            return {
                "error": "invalid_transition",
                "current_status": current,
                "requested_status": status,
                "allowed": sorted(allowed),
            }
        changes["status"] = status
        data["status"] = status
        if status == "in_progress" and not data.get("started_at"):
            data["started_at"] = now_iso
            changes["started_at"] = now_iso
        elif status == "shipped":
            data["shipped_at"] = now_iso
            changes["shipped_at"] = now_iso
            linked = _feature_linked_tasks(conn, name)
            total_hours = sum(
                float(json.loads(t.get("data") or "{}").get("estimated_hours", 1.0))
                for t in linked
            )
            data["actual_days"] = round(total_hours / 8.0, 2)
            changes["actual_days"] = data["actual_days"]
        elif status == "closed":
            data["closed_at"] = now_iso
            changes["closed_at"] = now_iso
        elif status == "abandoned":
            data["abandoned_at"] = now_iso
            changes["abandoned_at"] = now_iso

    if owner is not None:
        data["owner"] = owner; changes["owner"] = owner
    if priority is not None:
        data["priority"] = priority; changes["priority"] = priority
    if description is not None:
        data["description"] = description; changes["description"] = description
    if acceptance_criteria is not None:
        data["acceptance_criteria"] = acceptance_criteria; changes["acceptance_criteria"] = acceptance_criteria
    if estimated_days is not None:
        data["estimated_days"] = estimated_days; changes["estimated_days"] = estimated_days

    if not changes:
        return {"status": "no_changes", "name": name, "data": data}

    upsert_node(conn, "Feature", name, data)
    return {"status": "updated", "name": name, "changes": changes, "data": data}


def feature_list(conn: sqlite3.Connection,
                 status: Optional[str] = None,
                 owner: Optional[str] = None,
                 project_slug: Optional[str] = None) -> list:
    rows = query_nodes(conn, ntype="Feature", limit=500, project_slug=project_slug)
    result = []
    for r in rows:
        d = _node_data(r)
        if status and d.get("status") != status:
            continue
        if owner and d.get("owner") != owner:
            continue
        result.append({
            "name": r["name"], "status": d.get("status"),
            "owner": d.get("owner"), "priority": d.get("priority"),
            "started_at": d.get("started_at"), "shipped_at": d.get("shipped_at"),
            "project_slug": d.get("project_slug"),
        })
    return result


def feature_link(conn: sqlite3.Connection, feature_name: str,
                 node_type: str, node_name: str, edge_type: str) -> dict:
    forward = {"SPAWNED", "DISCUSSED_IN", "MENTIONED_IN"}
    if edge_type in forward:
        from_t, from_n, to_t, to_n = "Feature", feature_name, node_type, node_name
    else:
        from_t, from_n, to_t, to_n = node_type, node_name, "Feature", feature_name
    upsert_edge(conn, from_t, from_n, to_t, to_n, edge_type)
    return {"status": "linked", "edge": f"{from_t}:{from_n} --{edge_type}--> {to_t}:{to_n}"}


def _feature_linked_tasks(conn: sqlite3.Connection, feature_name: str) -> list:
    rows = conn.execute(
        """SELECT DISTINCT t.id, t.name, t.data, t.updated_at FROM nodes t
           JOIN edges e ON (
             (e.from_node_id = t.id AND e.edge_type = 'IMPLEMENTS_FEATURE')
             OR (e.to_node_id = t.id AND e.edge_type = 'SPAWNED')
           )
           JOIN nodes f ON (
             (e.to_node_id = f.id AND e.edge_type = 'IMPLEMENTS_FEATURE')
             OR (e.from_node_id = f.id AND e.edge_type = 'SPAWNED')
           )
           WHERE t.type='Task' AND f.type='Feature' AND f.name = ?""",
        (feature_name,)
    ).fetchall()
    return [dict(r) for r in rows]


def _feature_linked_nodes(conn: sqlite3.Connection, feature_name: str,
                          node_type: str, edge_types: tuple) -> list:
    placeholders = ",".join("?" * len(edge_types))
    rows = conn.execute(
        f"""SELECT DISTINCT n.id, n.name, n.data, n.updated_at FROM nodes n
            JOIN edges e ON (e.from_node_id = n.id OR e.to_node_id = n.id)
            JOIN nodes f ON (
              (e.from_node_id = f.id AND e.to_node_id = n.id)
              OR (e.to_node_id = f.id AND e.from_node_id = n.id)
            )
            WHERE n.type = ? AND f.type='Feature' AND f.name = ?
              AND e.edge_type IN ({placeholders})""",
        (node_type, feature_name, *edge_types)
    ).fetchall()
    return [dict(r) for r in rows]


def feature_health(conn: sqlite3.Connection, name: str) -> dict:
    feature = conn.execute("SELECT id, name, data FROM nodes WHERE type='Feature' AND name=?", (name,)).fetchone()
    if not feature:
        return {"error": f"feature not found: {name}"}

    fdata = json.loads(feature["data"] or "{}")
    tasks = _feature_linked_tasks(conn, name)
    by_status: dict[str, int] = {"completed": 0, "in_progress": 0, "blocked": 0, "pending": 0, "open": 0}
    blockers = []
    total_hours = 0.0
    remaining_hours = 0.0
    for t in tasks:
        td = json.loads(t["data"] or "{}")
        status = td.get("status", "open")
        by_status[status] = by_status.get(status, 0) + 1
        est = float(td.get("estimated_hours", _DEFAULT_ESTIMATED_HOURS))
        total_hours += est
        if status not in ("completed", "done"):
            remaining_hours += est
        if status == "blocked":
            blockers.append({"task_name": t["name"], "due_date": td.get("due_date")})

    total = len(tasks)
    completed = by_status.get("completed", 0) + by_status.get("done", 0)
    completion_pct = round(100.0 * completed / total, 1) if total else 0.0

    signals = _feature_linked_nodes(conn, name, "Signal", _FEATURE_SIGNAL_EDGES)
    decisions = _feature_linked_nodes(conn, name, "Decision", _FEATURE_DECISION_EDGES)

    last_signal = None
    if signals:
        ranked = []
        for s in signals:
            sd = json.loads(s["data"] or "{}")
            ts = _parse_iso(sd.get("timestamp")) or _parse_iso(s.get("updated_at"))
            if ts:
                ranked.append((ts, sd))
        if ranked:
            ranked.sort(key=lambda x: x[0], reverse=True)
            top = ranked[0]
            last_signal = {
                "type": top[1].get("signal_type"),
                "summary": top[1].get("content_summary"),
                "timestamp": top[0].isoformat(),
            }

    started = _parse_iso(fdata.get("started_at"))
    days_since = None
    if started:
        days_since = round((datetime.now(timezone.utc) - started).total_seconds() / 86400.0, 1)

    return {
        "feature": name, "status": fdata.get("status"),
        "owner": fdata.get("owner"), "priority": fdata.get("priority"),
        "tasks": {
            "total": total, "completed": completed,
            "in_progress": by_status.get("in_progress", 0),
            "blocked": by_status.get("blocked", 0),
            "pending": by_status.get("pending", 0) + by_status.get("open", 0),
        },
        "completion_pct": completion_pct,
        "total_hours": round(total_hours, 2),
        "remaining_hours": round(remaining_hours, 2),
        "estimated_days_remaining": round(remaining_hours / 8.0, 1),
        "blockers": blockers,
        "last_signal": last_signal,
        "decisions_count": len(decisions),
        "signals_count": len(signals),
        "days_since_started": days_since,
    }


def feature_timeline(conn: sqlite3.Connection, name: str,
                     since: Optional[str] = None) -> dict:
    feature = conn.execute("SELECT id FROM nodes WHERE type='Feature' AND name=?", (name,)).fetchone()
    if not feature:
        return {"error": f"feature not found: {name}"}

    since_dt = _parse_iso(since) if since else None
    events = []

    for t in _feature_linked_tasks(conn, name):
        td = json.loads(t["data"] or "{}")
        ts = _parse_iso(td.get("due_date")) or _parse_iso(t.get("updated_at"))
        if ts and (since_dt is None or ts >= since_dt):
            events.append({
                "timestamp": ts.isoformat(), "kind": "task",
                "name": t["name"], "status": td.get("status"),
            })

    for s in _feature_linked_nodes(conn, name, "Signal", _FEATURE_SIGNAL_EDGES):
        sd = json.loads(s["data"] or "{}")
        ts = _parse_iso(sd.get("timestamp")) or _parse_iso(s.get("updated_at"))
        if ts and (since_dt is None or ts >= since_dt):
            events.append({
                "timestamp": ts.isoformat(), "kind": "signal",
                "signal_type": sd.get("signal_type"),
                "summary": sd.get("content_summary"),
            })

    for d in _feature_linked_nodes(conn, name, "Decision", _FEATURE_DECISION_EDGES):
        dd = json.loads(d["data"] or "{}")
        ts = _parse_iso(dd.get("decided_at")) or _parse_iso(d.get("updated_at"))
        if ts and (since_dt is None or ts >= since_dt):
            events.append({
                "timestamp": ts.isoformat(), "kind": "decision",
                "title": d["name"], "outcome": dd.get("outcome"),
            })

    events.sort(key=lambda e: e["timestamp"])
    return {"feature": name, "since": since, "event_count": len(events), "events": events}


# ============================================================================
# Archive, Migrate, Healthcheck
# ============================================================================

def archive(conn: sqlite3.Connection,
            older_than_days: Optional[int] = None,
            dry_run: bool = False, vacuum: bool = False) -> dict:
    """Strip bulky fields from old nodes per retention policy."""
    cutoff_days = older_than_days if older_than_days is not None else (cfg.RETENTION_DAYS if cfg else 180)
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=cutoff_days)
    cutoff_iso = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

    stats: dict[str, Any] = {"archived": {}, "dry_run": dry_run, "cutoff": cutoff_iso}

    try:
        with conn:
            for ntype, retention in _ARCHIVE_AFTER_DAYS.items():
                if retention < 0:
                    continue
                rows = conn.execute(
                    "SELECT id, name, data, updated_at FROM nodes WHERE type=? AND updated_at < ?",
                    (ntype, cutoff_iso)
                ).fetchall()
                count = 0
                strip_fields = _ARCHIVE_STRIP_FIELDS.get(ntype, [])
                if not strip_fields:
                    continue
                for r in rows:
                    try:
                        data = json.loads(r["data"] or "{}")
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not any(f in data for f in strip_fields):
                        continue
                    for f in strip_fields:
                        data.pop(f, None)
                    data["_archived"] = True
                    data["_archived_at"] = datetime.now(timezone.utc).isoformat()
                    if not dry_run:
                        conn.execute(
                            "UPDATE nodes SET data=?, updated_at=updated_at WHERE id=?",
                            (json.dumps(data), r["id"])
                        )
                    count += 1
                if count:
                    stats["archived"][ntype] = count
    except sqlite3.Error as e:
        stats["error"] = str(e)
        return stats

    if vacuum and not dry_run:
        try:
            conn.execute("VACUUM")
        except sqlite3.Error as e:
            logger.warning("VACUUM failed: %s", e)
    return stats


def migrate(conn: sqlite3.Connection,
            target_version: Optional[str] = None) -> dict:
    """Bump schema version. Adds new tables/columns for v3.0."""
    target = target_version or _SCHEMA_VERSION
    proj = conn.execute("SELECT id, name, data FROM nodes WHERE type='Project' ORDER BY id LIMIT 1").fetchone()
    if not proj:
        return {"error": "no Project node; run 'init' first"}
    try:
        data = json.loads(proj["data"] or "{}")
    except (json.JSONDecodeError, TypeError):
        data = {}
    old_version = data.get("schema_version", "1.0")

    # v3.0 migration: add sync_state table and provenance columns if missing
    if old_version < "3.0":
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS sync_state (
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                project_slug TEXT NOT NULL DEFAULT '_global',
                last_sync_at TEXT,
                cursor TEXT DEFAULT '',
                status TEXT DEFAULT 'ok',
                error_msg TEXT DEFAULT '',
                PRIMARY KEY(source_type, source_id, project_slug)
            )""")
        except sqlite3.OperationalError:
            pass
        for col_sql in [
            "ALTER TABLE nodes ADD COLUMN source_type TEXT DEFAULT 'manual'",
            "ALTER TABLE nodes ADD COLUMN source_id TEXT DEFAULT ''",
            "ALTER TABLE nodes ADD COLUMN ingested_at TEXT DEFAULT (datetime('now'))",
            "ALTER TABLE nodes ADD COLUMN confidence REAL DEFAULT 1.0",
        ]:
            try:
                conn.execute(col_sql)
            except sqlite3.OperationalError:
                pass

    data["schema_version"] = target
    data["migrated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        with conn:
            upsert_node(conn, "Project", proj["name"], data)
    except sqlite3.Error as e:
        return {"error": str(e)}
    return {"project": proj["name"], "old_version": old_version, "new_version": target}


def healthcheck(conn: sqlite3.Connection, db_path: str) -> dict:
    """Database health report."""
    try:
        size_mb = round(os.path.getsize(db_path) / (1024 * 1024), 2)
    except OSError:
        size_mb = None

    total_nodes = conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"]
    total_edges = conn.execute("SELECT COUNT(*) AS c FROM edges").fetchone()["c"]

    archivable_types = [t for t, days in _ARCHIVE_AFTER_DAYS.items() if days >= 0]
    if archivable_types:
        placeholders = ",".join("?" * len(archivable_types))
        oldest = conn.execute(
            f"SELECT MIN(updated_at) AS oldest FROM nodes WHERE type IN ({placeholders})"
            " AND (data NOT LIKE '%\"_archived\":true%' OR data IS NULL)",
            archivable_types
        ).fetchone()
        oldest_iso = oldest["oldest"] if oldest else None
    else:
        oldest_iso = None

    retention_days = cfg.RETENTION_DAYS if cfg else 180
    needs_archive = False
    if oldest_iso:
        try:
            o_dt = datetime.strptime(oldest_iso, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - o_dt).days > retention_days:
                needs_archive = True
        except ValueError:
            pass

    integrity_row = conn.execute("PRAGMA integrity_check").fetchone()
    integrity = integrity_row[0] if integrity_row else "unknown"

    proj = conn.execute("SELECT data FROM nodes WHERE type='Project' ORDER BY id LIMIT 1").fetchone()
    schema_version = "unknown"
    if proj:
        try:
            schema_version = json.loads(proj["data"] or "{}").get("schema_version", "1.0")
        except (json.JSONDecodeError, TypeError):
            pass

    orphan_count = conn.execute(
        """SELECT COUNT(*) AS c FROM nodes n
           WHERE n.id NOT IN (SELECT from_node_id FROM edges)
             AND n.id NOT IN (SELECT to_node_id FROM edges)
             AND n.type NOT IN ('Project', 'Event', 'Feature', 'Decision',
                                'Person', 'SlackChannel', 'ArchDecision',
                                'Requirement', 'UseCase', 'BusinessLogic',
                                'RiskItem', 'EvolutionPlan')"""
    ).fetchone()["c"]

    sync_count = conn.execute("SELECT COUNT(*) AS c FROM sync_state").fetchone()["c"]

    return {
        "db_path": db_path, "size_mb": size_mb,
        "node_count": total_nodes, "edge_count": total_edges,
        "oldest_unarchived": oldest_iso,
        "needs_archive": needs_archive,
        "retention_days": retention_days,
        "schema_version": schema_version,
        "integrity": integrity,
        "orphan_count": orphan_count,
        "sync_sources": sync_count,
    }


def smart_reset(conn: sqlite3.Connection, workspace_path: str = "workspace") -> dict:
    """Selective reset: delete ephemeral/personal data, keep service knowledge.

    KEEPS: Project, Function, Class, Test, Module, Endpoint, DataStore,
           ProjectExpert, ArchDecision, BusinessLogic, UseCase, Config nodes
           and all structural edges (CONTAINS, CALLS, IMPORTS, TESTS, etc.)
    DELETES: Feature, Signal, Email, Meeting, Person, Plan, Task, Decision,
             PR, Commit, Branch, WebPage, JiraIssue, Document, Event, SlackChannel,
             Requirement, RiskItem nodes + connected edges + feature workspace dirs
    """
    KEEP_TYPES = (
        'Project', 'Function', 'Class', 'Test', 'Module', 'Endpoint',
        'DataStore', 'ProjectExpert', 'ArchDecision', 'BusinessLogic',
        'UseCase', 'Config',
    )
    DELETE_TYPES = (
        'Feature', 'Signal', 'Email', 'Meeting', 'Person', 'Plan', 'Task',
        'Decision', 'PR', 'Commit', 'Branch', 'WebPage', 'JiraIssue',
        'Document', 'Event', 'SlackChannel', 'Requirement', 'RiskItem',
    )

    placeholders = ",".join("?" * len(DELETE_TYPES))

    pre_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    pre_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    node_ids = [r[0] for r in conn.execute(
        f"SELECT id FROM nodes WHERE type IN ({placeholders})", DELETE_TYPES
    ).fetchall()]

    edges_deleted = 0
    if node_ids:
        batch_size = 500
        for i in range(0, len(node_ids), batch_size):
            batch = node_ids[i:i + batch_size]
            ph = ",".join("?" * len(batch))
            edges_deleted += conn.execute(
                f"DELETE FROM edges WHERE from_node_id IN ({ph}) OR to_node_id IN ({ph})",
                batch + batch
            ).rowcount

    nodes_deleted = conn.execute(
        f"DELETE FROM nodes WHERE type IN ({placeholders})", DELETE_TYPES
    ).rowcount

    truncated = []
    for tbl in ('learning_ledger', 'learning_provenance', 'chat_messages', 'chat_sessions'):
        try:
            conn.execute(f"DELETE FROM {tbl}")
            truncated.append(tbl)
        except sqlite3.OperationalError:
            pass

    dirs_removed = []
    features_dir = os.path.join(workspace_path, "features")
    if os.path.isdir(features_dir):
        import shutil
        for entry in os.listdir(features_dir):
            full = os.path.join(features_dir, entry)
            if os.path.isdir(full):
                shutil.rmtree(full)
                dirs_removed.append(entry)

    try:
        conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")
    except sqlite3.OperationalError:
        pass

    conn.execute(
        "INSERT INTO reset_log (reset_type, nodes_deleted, edges_deleted, tables_truncated, dirs_removed) "
        "VALUES (?, ?, ?, ?, ?)",
        ("smart", nodes_deleted, edges_deleted,
         json.dumps(truncated), json.dumps(dirs_removed))
    )
    conn.commit()

    post_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    post_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    return {
        "nodes_before": pre_nodes, "nodes_after": post_nodes, "nodes_deleted": nodes_deleted,
        "edges_before": pre_edges, "edges_after": post_edges, "edges_deleted": edges_deleted,
        "tables_truncated": truncated, "feature_dirs_removed": dirs_removed,
        "kept_types": list(KEEP_TYPES),
    }


def pipeline_status(workspace_path: str, slug: str) -> dict:
    """Check which pipeline phases have completed artifacts."""
    feat_dir = os.path.join(workspace_path, "features", slug)
    if not os.path.isdir(feat_dir):
        return {"slug": slug, "exists": False, "ideation": False, "solutioning": False, "techspec": False, "e2e": False, "next_phase": "ideation"}

    import glob
    has_overview = bool(glob.glob(os.path.join(feat_dir, "overview*")))
    has_solution = bool(glob.glob(os.path.join(feat_dir, "solution*")))
    has_techspec = bool(glob.glob(os.path.join(feat_dir, "tech-spec*")))
    has_e2e = bool(glob.glob(os.path.join(feat_dir, "e2e-report*")))

    if not has_overview:
        next_phase = "ideation"
    elif not has_solution:
        next_phase = "solutioning"
    elif not has_techspec:
        next_phase = "techspec"
    elif not has_e2e:
        next_phase = "e2e"
    else:
        next_phase = "complete"

    return {
        "slug": slug, "exists": True,
        "ideation": has_overview, "solutioning": has_solution, "techspec": has_techspec, "e2e": has_e2e,
        "next_phase": next_phase,
    }


def audit_report(conn: sqlite3.Connection, db_path: str, workspace_path: str = "workspace") -> dict:
    """Comprehensive system audit. Each check scored 1-5."""
    checks = {}

    total_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    total_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    # 1. Node coverage per project
    projects = conn.execute(
        "SELECT DISTINCT json_extract(data,'$.project') as p FROM nodes WHERE json_extract(data,'$.project') IS NOT NULL"
    ).fetchall()
    projects_with_functions = conn.execute(
        "SELECT COUNT(DISTINCT json_extract(data,'$.project')) FROM nodes WHERE type='Function' AND json_extract(data,'$.project') IS NOT NULL"
    ).fetchone()[0]
    proj_count = len(projects)
    coverage_pct = (projects_with_functions / max(proj_count, 1)) * 100
    checks["node_coverage"] = {"score": min(5, int(coverage_pct / 20)), "detail": f"{projects_with_functions}/{proj_count} projects have Function nodes ({coverage_pct:.0f}%)"}

    # 2. Expert depth
    total_experts = conn.execute("SELECT COUNT(*) FROM nodes WHERE type='ProjectExpert'").fetchone()[0]
    experts_with_depth = conn.execute(
        "SELECT COUNT(*) FROM expert_functions"
    ).fetchone()[0] if _table_exists(conn, 'expert_functions') else 0
    depth_pct = (experts_with_depth / max(total_experts * 100, 1)) * 100
    checks["expert_depth"] = {"score": min(5, 1 + int(depth_pct / 25)), "detail": f"{experts_with_depth} function mappings across {total_experts} experts"}

    # 3. Edge connectivity
    avg_edges = total_edges / max(total_nodes, 1)
    orphans = conn.execute(
        "SELECT COUNT(*) FROM nodes n WHERE n.id NOT IN (SELECT from_node_id FROM edges) "
        "AND n.id NOT IN (SELECT to_node_id FROM edges) AND n.type NOT IN ('Project','Feature','Person')"
    ).fetchone()[0]
    orphan_pct = (orphans / max(total_nodes, 1)) * 100
    score = 5 if orphan_pct < 5 else 4 if orphan_pct < 15 else 3 if orphan_pct < 30 else 2 if orphan_pct < 50 else 1
    checks["edge_connectivity"] = {"score": score, "detail": f"Avg {avg_edges:.2f} edges/node, {orphans} orphans ({orphan_pct:.1f}%)"}

    # 4. Code body coverage
    total_functions = conn.execute("SELECT COUNT(*) FROM nodes WHERE type='Function'").fetchone()[0]
    bodies = conn.execute("SELECT COUNT(*) FROM code_bodies").fetchone()[0] if _table_exists(conn, 'code_bodies') else 0
    body_pct = (bodies / max(total_functions, 1)) * 100
    checks["code_body_coverage"] = {"score": min(5, 1 + int(body_pct / 20)), "detail": f"{bodies}/{total_functions} functions have stored bodies ({body_pct:.0f}%)"}

    # 5. FTS5 health
    try:
        fts_count = conn.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0]
    except sqlite3.OperationalError:
        fts_count = 0
    fts_match = abs(fts_count - total_nodes) < total_nodes * 0.05
    checks["fts5_health"] = {"score": 5 if fts_match else 2, "detail": f"FTS5: {fts_count} vs nodes: {total_nodes} ({'matched' if fts_match else 'MISMATCH'})"}

    # 6. Confidence distribution
    high = conn.execute("SELECT COUNT(*) FROM nodes WHERE confidence >= 0.85").fetchone()[0]
    medium = conn.execute("SELECT COUNT(*) FROM nodes WHERE confidence >= 0.5 AND confidence < 0.85").fetchone()[0]
    low = conn.execute("SELECT COUNT(*) FROM nodes WHERE confidence < 0.5 AND confidence > 0").fetchone()[0]
    high_pct = (high / max(total_nodes, 1)) * 100
    checks["confidence_dist"] = {"score": min(5, 1 + int(high_pct / 20)), "detail": f"High: {high} ({high_pct:.0f}%), Medium: {medium}, Low: {low}"}

    # 7. Cross-service edges
    depends = conn.execute("SELECT COUNT(*) FROM edges WHERE edge_type='DEPENDS_ON'").fetchone()[0]
    calls_svc = conn.execute("SELECT COUNT(*) FROM edges WHERE edge_type='CALLS_SERVICE'").fetchone()[0]
    kafka = conn.execute("SELECT COUNT(*) FROM edges WHERE edge_type='KAFKA_TOPIC'").fetchone()[0]
    cross_total = depends + calls_svc + kafka
    checks["cross_service_edges"] = {"score": min(5, 1 + int(cross_total / 100)), "detail": f"DEPENDS_ON: {depends}, CALLS_SERVICE: {calls_svc}, KAFKA_TOPIC: {kafka} = {cross_total} total"}

    # 8. DataStore schema
    total_ds = conn.execute("SELECT COUNT(*) FROM nodes WHERE type='DataStore'").fetchone()[0]
    ds_with_schema = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE type='DataStore' AND json_extract(data,'$.evidence')='schema'"
    ).fetchone()[0]
    ds_pct = (ds_with_schema / max(total_ds, 1)) * 100
    checks["datastore_schema"] = {"score": min(5, 1 + int(ds_pct / 20)), "detail": f"{ds_with_schema}/{total_ds} DataStores with column-level schema ({ds_pct:.0f}%)"}

    # 9. Pipeline readiness
    feat_dir = os.path.join(workspace_path, "features")
    has_features_dir = os.path.isdir(feat_dir)
    checks["pipeline_readiness"] = {"score": 4 if has_features_dir else 3, "detail": f"Features dir: {'exists' if has_features_dir else 'missing'}, 3-phase pipeline active"}

    # 10. DB size
    try:
        size_mb = round(os.path.getsize(db_path) / (1024 * 1024), 2)
    except OSError:
        size_mb = 0
    checks["db_size"] = {"score": 4 if size_mb < 2000 else 3 if size_mb < 5000 else 2, "detail": f"{size_mb} MB"}

    overall = sum(c["score"] for c in checks.values()) / len(checks)
    return {"checks": checks, "overall_score": round(overall, 1), "total_nodes": total_nodes, "total_edges": total_edges}


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone()
    return row[0] > 0


def export_shareable(db_path: str, output_path: str, workspace_path: str = "workspace") -> dict:
    """Export a cleaned, compressed rubick.db for sharing with colleagues."""
    import shutil
    import tempfile
    import tarfile

    tmp_dir = tempfile.mkdtemp(prefix="nemesis_export_")
    tmp_db = os.path.join(tmp_dir, "rubick-shared.db")

    conn_src = sqlite3.connect(db_path)
    conn_src.execute(f"VACUUM INTO '{tmp_db}'")
    conn_src.close()

    conn_tmp = sqlite3.connect(tmp_db)
    conn_tmp.row_factory = sqlite3.Row

    for tbl in ('chat_sessions', 'chat_messages', 'init_settings', 'learning_provenance', 'interaction_log'):
        try:
            conn_tmp.execute(f"DELETE FROM {tbl}")
        except sqlite3.OperationalError:
            pass

    DELETE_TYPES = ('Feature', 'Signal', 'Email', 'Meeting', 'Person', 'Plan', 'Task',
                    'Decision', 'PR', 'Commit', 'Branch', 'WebPage', 'JiraIssue',
                    'Document', 'Event', 'SlackChannel')
    ph = ",".join("?" * len(DELETE_TYPES))
    node_ids = [r[0] for r in conn_tmp.execute(f"SELECT id FROM nodes WHERE type IN ({ph})", DELETE_TYPES).fetchall()]
    if node_ids:
        for i in range(0, len(node_ids), 500):
            batch = node_ids[i:i+500]
            bph = ",".join("?" * len(batch))
            conn_tmp.execute(f"DELETE FROM edges WHERE from_node_id IN ({bph}) OR to_node_id IN ({bph})", batch + batch)
    conn_tmp.execute(f"DELETE FROM nodes WHERE type IN ({ph})", DELETE_TYPES)
    try:
        conn_tmp.execute("DELETE FROM learning_ledger")
    except sqlite3.OperationalError:
        pass

    nodes_after = conn_tmp.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    edges_after = conn_tmp.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    conn_tmp.commit()
    conn_tmp.close()

    conn_vac = sqlite3.connect(tmp_db, isolation_level=None)
    conn_vac.execute("VACUUM")
    conn_vac.close()

    with tarfile.open(output_path, "w:gz") as tar:
        tar.add(tmp_db, arcname="rubick-shared.db")
        qdrant_dir = os.path.join(workspace_path, "qdrant_data")
        if os.path.isdir(qdrant_dir):
            tar.add(qdrant_dir, arcname="qdrant_data")

    export_size = os.path.getsize(output_path)
    shutil.rmtree(tmp_dir)

    return {
        "output": output_path,
        "nodes": nodes_after, "edges": edges_after,
        "size_bytes": export_size,
        "size_mb": round(export_size / (1024 * 1024), 1),
    }


def import_shareable(archive_path: str, workspace_path: str = "workspace") -> dict:
    """Import a shared nemesis brain archive."""
    import tarfile

    if not os.path.isfile(archive_path):
        return {"error": f"Archive not found: {archive_path}"}

    db_path = os.path.join(workspace_path, "rubick.db")
    if os.path.isfile(db_path):
        backup = db_path + ".pre-import.bak"
        os.rename(db_path, backup)

    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(workspace_path, filter='data')

    shared_db = os.path.join(workspace_path, "rubick-shared.db")
    if os.path.isfile(shared_db):
        os.rename(shared_db, db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    for create_sql in [
        "CREATE TABLE IF NOT EXISTS chat_sessions (id TEXT PRIMARY KEY, title TEXT, message_count INTEGER DEFAULT 0, "
        "total_input_tokens INTEGER DEFAULT 0, total_output_tokens INTEGER DEFAULT 0, total_cost_usd REAL DEFAULT 0, "
        "created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')))",
        "CREATE TABLE IF NOT EXISTS chat_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
        "content TEXT, content_type TEXT DEFAULT 'text', rubick_target TEXT, elapsed REAL, input_tokens INTEGER DEFAULT 0, "
        "output_tokens INTEGER DEFAULT 0, cache_read INTEGER DEFAULT 0, cache_write INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0, "
        "created_at TEXT DEFAULT (datetime('now')))",
        "CREATE TABLE IF NOT EXISTS init_settings (key TEXT PRIMARY KEY, value TEXT)",
        "CREATE TABLE IF NOT EXISTS learning_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, interaction_id TEXT, "
        "interaction_type TEXT, source_skill TEXT, project_slug TEXT, node_type TEXT, node_name TEXT, node_data TEXT, "
        "confidence REAL DEFAULT 0.7, edges TEXT DEFAULT '[]', status TEXT DEFAULT 'staged', created_at TEXT DEFAULT (datetime('now')), "
        "flushed_at TEXT)",
        "CREATE TABLE IF NOT EXISTS learning_provenance (id INTEGER PRIMARY KEY AUTOINCREMENT, interaction_id TEXT, "
        "source_skill TEXT, node_id INTEGER, action TEXT, created_at TEXT DEFAULT (datetime('now')))",
    ]:
        try:
            conn.execute(create_sql)
        except sqlite3.OperationalError:
            pass

    try:
        conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    conn.close()

    return {"db_path": db_path, "nodes": nodes, "edges": edges, "status": "imported"}


def get_stats(conn: sqlite3.Connection) -> dict:
    type_counts = conn.execute(
        "SELECT type, COUNT(*) as count FROM nodes GROUP BY type ORDER BY count DESC"
    ).fetchall()
    edge_counts = conn.execute(
        "SELECT edge_type, COUNT(*) as count FROM edges GROUP BY edge_type ORDER BY count DESC"
    ).fetchall()
    total_nodes = conn.execute("SELECT COUNT(*) as c FROM nodes").fetchone()["c"]
    total_edges = conn.execute("SELECT COUNT(*) as c FROM edges").fetchone()["c"]
    return {
        "total_nodes": total_nodes, "total_edges": total_edges,
        "node_types": {r["type"]: r["count"] for r in type_counts},
        "edge_types": {r["edge_type"]: r["count"] for r in edge_counts},
    }


# ============================================================================
# Code Body Storage (v4.0 — anti-hallucination layer)
# ============================================================================

def chunk_body(body: str, start_line: int, target_tokens: int = 500,
               min_tokens: int = 100, max_tokens: int = 1000,
               overlap_lines: int = 2) -> list[dict]:
    """Split a function body into embedding-ready chunks."""
    lines = body.split('\n')
    if not lines:
        return []

    def est_tokens(text):
        return max(1, len(text) // 4)

    total_tokens = est_tokens(body)
    if total_tokens <= target_tokens:
        return [{
            "content": body,
            "start_line": start_line,
            "end_line": start_line + len(lines) - 1,
            "token_estimate": total_tokens,
        }]

    split_candidates = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == '' or stripped == '}' or stripped.startswith('//') or stripped.startswith('#'):
            split_candidates.append(i)

    chunks = []
    current_start = 0

    for split_point in split_candidates:
        segment = '\n'.join(lines[current_start:split_point + 1])
        seg_tokens = est_tokens(segment)
        if seg_tokens >= target_tokens:
            content = segment
            chunks.append({
                "content": content,
                "start_line": start_line + current_start,
                "end_line": start_line + split_point,
                "token_estimate": seg_tokens,
            })
            current_start = max(0, split_point + 1 - overlap_lines)

    if current_start < len(lines):
        content = '\n'.join(lines[current_start:])
        tokens = est_tokens(content)
        if chunks and tokens < min_tokens:
            last = chunks[-1]
            merged = last["content"] + '\n' + content
            last["content"] = merged
            last["end_line"] = start_line + len(lines) - 1
            last["token_estimate"] = est_tokens(merged)
        else:
            chunks.append({
                "content": content,
                "start_line": start_line + current_start,
                "end_line": start_line + len(lines) - 1,
                "token_estimate": tokens,
            })

    if not chunks:
        chunks.append({
            "content": body,
            "start_line": start_line,
            "end_line": start_line + len(lines) - 1,
            "token_estimate": total_tokens,
        })

    return chunks


def import_code_bodies(conn: sqlite3.Connection, ast_data: dict,
                       project_slug: str) -> dict:
    """Import code bodies from AST data into code_bodies and code_chunks tables."""
    stats = {"bodies_inserted": 0, "bodies_skipped": 0, "chunks_created": 0}

    items = []
    for item in ast_data.get("functions", []):
        items.append(("Function", item))
    for item in ast_data.get("classes", []):
        items.append(("Class", item))
    for item in ast_data.get("tests", []):
        items.append(("Test", item))

    for node_type, item in items:
        body = item.get("body", "")
        if not body:
            continue

        if item.get("is_test") and node_type == "Function":
            node_type = "Test"

        node = conn.execute(
            "SELECT id FROM nodes WHERE type=? AND name=?",
            (node_type, item["name"])
        ).fetchone()
        if not node:
            continue
        node_id = node["id"]

        body_hash = hashlib.sha256(body.encode()).hexdigest()

        existing = conn.execute(
            "SELECT id, body_hash FROM code_bodies WHERE node_id=?", (node_id,)
        ).fetchone()
        if existing and existing["body_hash"] == body_hash:
            stats["bodies_skipped"] += 1
            continue

        end_line = item.get("end_line", item.get("line", 0) + body.count('\n'))
        language = item.get("language", "go")

        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            INSERT INTO code_bodies(node_id, project_slug, file_path, start_line, end_line,
                                    language, body, body_hash, byte_length, extracted_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                body=excluded.body, body_hash=excluded.body_hash,
                byte_length=excluded.byte_length, end_line=excluded.end_line,
                extracted_at=excluded.extracted_at
        """, (node_id, project_slug, item.get("file", ""),
              item.get("line", 0), end_line,
              language, body, body_hash, len(body), now))

        body_row = conn.execute(
            "SELECT id FROM code_bodies WHERE node_id=?", (node_id,)
        ).fetchone()
        body_id = body_row["id"]
        stats["bodies_inserted"] += 1

        conn.execute("DELETE FROM code_chunks WHERE body_id=?", (body_id,))
        chunks = chunk_body(body, item.get("line", 0))
        for i, chunk in enumerate(chunks):
            conn.execute("""
                INSERT INTO code_chunks(body_id, node_id, chunk_index, content,
                                        start_line, end_line, token_estimate)
                VALUES(?, ?, ?, ?, ?, ?, ?)
            """, (body_id, node_id, i, chunk["content"],
                  chunk["start_line"], chunk["end_line"], chunk["token_estimate"]))
            stats["chunks_created"] += 1

    conn.commit()
    return stats


def get_code_body(conn: sqlite3.Connection, node_type: str, node_name: str) -> Optional[dict]:
    """Retrieve full code body for a Function/Class/Test node."""
    node = conn.execute(
        "SELECT id FROM nodes WHERE type=? AND name=?", (node_type, node_name)
    ).fetchone()
    if not node:
        return None
    row = conn.execute(
        "SELECT * FROM code_bodies WHERE node_id=?", (node["id"],)
    ).fetchone()
    return dict(row) if row else None


def get_code_at_lines(conn: sqlite3.Connection, file_path: str,
                      start_line: int, end_line: int,
                      project_slug: Optional[str] = None) -> list[dict]:
    """Retrieve code bodies overlapping a line range in a file."""
    sql = """SELECT cb.*, n.type as node_type, n.name as node_name
             FROM code_bodies cb
             JOIN nodes n ON cb.node_id = n.id
             WHERE cb.file_path = ? AND cb.start_line <= ? AND cb.end_line >= ?"""
    params: list = [file_path, end_line, start_line]
    if project_slug:
        sql += " AND cb.project_slug = ?"
        params.append(project_slug)
    sql += " ORDER BY cb.start_line"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def search_code(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[dict]:
    """Full-text search over code bodies via code_fts."""
    try:
        rows = conn.execute(
            "SELECT cb.id, cb.node_id, cb.project_slug, cb.file_path, cb.start_line, "
            "cb.end_line, cb.language, cb.byte_length, n.type as node_type, n.name as node_name "
            "FROM code_fts cf "
            "JOIN code_bodies cb ON cf.rowid = cb.id "
            "JOIN nodes n ON cb.node_id = n.id "
            "WHERE code_fts MATCH ? LIMIT ?",
            (query, limit)
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        rows = conn.execute(
            "SELECT cb.id, cb.node_id, cb.project_slug, cb.file_path, cb.start_line, "
            "cb.end_line, cb.language, cb.byte_length, n.type as node_type, n.name as node_name "
            "FROM code_bodies cb "
            "JOIN nodes n ON cb.node_id = n.id "
            "WHERE cb.body LIKE ? LIMIT ?",
            (f"%{query}%", limit)
        ).fetchall()
        return [dict(r) for r in rows]


# ============================================================================
# AST Import
# ============================================================================

def import_ast(conn: sqlite3.Connection, ast_path: str,
               project_slug: Optional[str] = None) -> dict:
    """Import AST analysis JSON into the graph."""
    with open(ast_path) as f:
        ast_data = json.load(f)

    stats = {"functions": 0, "classes": 0, "imports": 0, "endpoints": 0, "db_ops": 0, "edges": 0}

    for item in ast_data.get("functions", []):
        upsert_node(conn, "Function", item["name"], {
            "file": item.get("file", ""), "line": item.get("line", 0),
            "complexity": item.get("complexity", 0),
            "params": item.get("params", []), "returns": item.get("returns", ""),
            "receiver": item.get("receiver", ""), "project_slug": project_slug,
        }, source_type="ast")
        stats["functions"] += 1
        if item.get("package"):
            upsert_node(conn, "Module", item["package"], {
                "file": item.get("file", ""), "project_slug": project_slug,
            }, source_type="ast")
            upsert_edge(conn, "Module", item["package"], "Function", item["name"], "CONTAINS")
            stats["edges"] += 1

    for item in ast_data.get("classes", []):
        upsert_node(conn, "Class", item["name"], {
            "file": item.get("file", ""), "line": item.get("line", 0),
            "methods": item.get("methods", []), "project_slug": project_slug,
        }, source_type="ast")
        stats["classes"] += 1

    for item in ast_data.get("imports", []):
        upsert_node(conn, "Module", item["module"], {
            "file": item.get("file", ""), "external": item.get("external", False),
            "project_slug": project_slug,
        }, source_type="ast")
        stats["imports"] += 1
        if item.get("importer"):
            upsert_edge(conn, "Module", item["importer"], "Module", item["module"], "IMPORTS")
            stats["edges"] += 1

    for item in ast_data.get("endpoints", []):
        upsert_node(conn, "Endpoint", item["path"], {
            "method": item.get("method", ""), "handler": item.get("handler", ""),
            "file": item.get("file", ""), "project_slug": project_slug,
        }, source_type="ast")
        stats["endpoints"] += 1
        if item.get("handler"):
            upsert_edge(conn, "Endpoint", item["path"], "Function", item["handler"], "ROUTES_TO")
            stats["edges"] += 1

    for item in ast_data.get("db_operations", []):
        upsert_node(conn, "DataStore", item["table"], {
            "operation": item.get("operation", ""), "project_slug": project_slug,
        }, source_type="ast")
        stats["db_ops"] += 1

    for item in ast_data.get("calls", []):
        upsert_edge(conn, "Function", item["caller"], "Function", item["callee"], "CALLS")
        stats["edges"] += 1

    for item in ast_data.get("tests", []):
        upsert_node(conn, "Test", item["test_name"], {
            "file": item.get("file", ""), "project_slug": project_slug,
        }, source_type="ast")
        if item.get("tests_function"):
            upsert_edge(conn, "Test", item["test_name"], "Function", item["tests_function"], "TESTS")
            stats["edges"] += 1

    conn.commit()

    # Import code bodies into separate tables (v4.0)
    try:
        body_stats = import_code_bodies(conn, ast_data, project_slug or "unknown")
        stats["bodies"] = body_stats["bodies_inserted"]
        stats["bodies_skipped"] = body_stats["bodies_skipped"]
        stats["chunks"] = body_stats["chunks_created"]
    except sqlite3.OperationalError:
        pass  # code_bodies table may not exist yet (pre-v4.0 DB)

    return {"status": "imported", "stats": stats}


# ============================================================================
# Cross-Service Edge Builder
# ============================================================================

def build_cross_service_edges(conn: sqlite3.Connection, repos_path: str) -> dict:
    """Build cross-service relationship edges from go.mod deps and code patterns.

    Creates:
    - DEPENDS_ON edges between Project nodes (from go.mod)
    - CALLS_SERVICE edges between Projects (from HTTP/gRPC patterns + rpc imports)
    - IMPORTS_LIB edges from Functions to library Modules (from import paths)
    """
    from collections import defaultdict as dd

    results = {
        "depends_on_created": 0,
        "calls_service_created": 0,
        "imports_lib_created": 0,
        "cross_service_pairs": [],
    }

    now = datetime.now(timezone.utc).isoformat()

    # -- Load project nodes --
    projects = {}
    for r in conn.execute("SELECT id, name FROM nodes WHERE type='Project'").fetchall():
        projects[r["name"]] = r["id"]

    repo_dirs = set()
    if os.path.isdir(repos_path):
        repo_dirs = set(os.listdir(repos_path))

    # -- Alias map for fuzzy project resolution --
    alias_map = {}
    for pname in projects:
        alias_map[pname] = pname
        if pname.startswith("payments-"):
            alias_map[pname[len("payments-"):]] = pname
        if pname.endswith("-service"):
            alias_map[pname[:-len("-service")]] = pname

    def resolve_project_id(name):
        name = name.lower().replace("_", "-")
        if name in projects:
            return projects[name]
        if name in alias_map:
            return projects.get(alias_map[name])
        return None

    def resolve_repo(name):
        name = name.lower().replace("_", "-")
        if name in repo_dirs:
            return name
        if name + "-service" in repo_dirs:
            return name + "-service"
        if "payments-" + name in repo_dirs:
            return "payments-" + name
        return None

    # -- Get existing edges to avoid duplicates --
    existing_depends = set()
    for r in conn.execute("SELECT from_node_id, to_node_id FROM edges WHERE edge_type='DEPENDS_ON'").fetchall():
        existing_depends.add((r["from_node_id"], r["to_node_id"]))

    existing_calls = set()
    for r in conn.execute("SELECT from_node_id, to_node_id FROM edges WHERE edge_type='CALLS_SERVICE'").fetchall():
        existing_calls.add((r["from_node_id"], r["to_node_id"]))

    # ====== Phase 1: go.mod dependencies ======
    logger.info("Phase 1: Extracting go.mod dependencies...")
    razorpay_re = re.compile(r"github\.com/razorpay/([a-zA-Z0-9_-]+)")
    dep_map = {}

    for repo in sorted(repo_dirs):
        gomod = os.path.join(repos_path, repo, "go.mod")
        if not os.path.exists(gomod):
            continue
        with open(gomod, "r") as f:
            content = f.read()

        deps = set()
        for m in razorpay_re.finditer(content):
            dep = m.group(1)
            if dep != repo:
                deps.add(dep)
        if deps:
            dep_map[repo] = deps

    # Create DEPENDS_ON edges
    for service, deps in dep_map.items():
        from_id = resolve_project_id(service)
        if not from_id:
            continue
        for dep in deps:
            to_id = resolve_project_id(dep)
            if not to_id or from_id == to_id:
                continue
            if (from_id, to_id) in existing_depends:
                continue
            data_json = json.dumps({
                "source": "go.mod",
                "discovered_at": now,
                "confidence": 0.95,
                "cross_service": True,
            })
            conn.execute(
                "INSERT OR IGNORE INTO edges (from_node_id, to_node_id, edge_type, data) VALUES (?,?,?,?)",
                (from_id, to_id, "DEPENDS_ON", data_json),
            )
            existing_depends.add((from_id, to_id))
            results["depends_on_created"] += 1

    conn.commit()
    logger.info(f"Phase 1 done: {results['depends_on_created']} DEPENDS_ON edges created")

    # ====== Phase 2: Cross-service call patterns ======
    logger.info("Phase 2: Detecting cross-service HTTP/gRPC/rpc-import patterns...")
    patterns = [
        re.compile(r'(?:client|httpClient|httpclient|Client|httpSdk)\.(?:Call|Do|Get|Post|Put|Patch|Delete)\s*\(\s*(?:ctx\s*,\s*)?["\x60]([a-z][\w-]+)["\x60]'),
        re.compile(r'grpc\.Dial[A-Za-z]*\s*\(\s*["\x60]([a-z][\w-]+)[:\.]'),
        re.compile(r'New([A-Z]\w+)(?:Service)?Client\s*\('),
        re.compile(r'["\x60]/v[12]/(?:internal|admin)/([a-z][\w-]+)'),
    ]
    proto_re = re.compile(r'"github\.com/razorpay/rpc/([a-z][\w_-]+)')

    cross_calls = dd(list)

    for repo in sorted(repo_dirs):
        repo_path = os.path.join(repos_path, repo)
        if not os.path.isdir(repo_path):
            continue
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in (".git", "vendor", "node_modules", "testdata", ".idea", "mock")]
            for fname in files:
                if not fname.endswith(".go"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", errors="replace") as f:
                        lines = f.readlines()
                except Exception:
                    continue
                rel = os.path.relpath(fpath, repo_path)
                for i, line in enumerate(lines, 1):
                    for pat in patterns:
                        for m in pat.finditer(line):
                            svc = m.group(1)
                            target = resolve_repo(svc)
                            if target and target != repo:
                                cross_calls[(repo, target)].append((rel, i, line.strip()[:120]))
                    for m in proto_re.finditer(line):
                        svc = m.group(1)
                        target = resolve_repo(svc)
                        if target and target != repo:
                            cross_calls[(repo, target)].append((rel, i, f"rpc import: {svc}"))

    # Create CALLS_SERVICE edges
    for (caller, target), evidence in cross_calls.items():
        from_id = resolve_project_id(caller)
        to_id = resolve_project_id(target)
        if not from_id or not to_id or from_id == to_id:
            continue
        if (from_id, to_id) in existing_calls:
            continue

        files = set(e[0] for e in evidence)
        rpc_count = sum(1 for e in evidence if "rpc import" in e[2])
        http_count = len(evidence) - rpc_count
        data_json = json.dumps({
            "relationship": "cross_service_call",
            "evidence_files": len(files),
            "evidence_total": len(evidence),
            "rpc_refs": rpc_count,
            "http_refs": http_count,
            "source": "pattern_detection",
            "discovered_at": now,
            "confidence": 0.85,
            "cross_service": True,
            "sample_files": sorted(files)[:5],
        })
        conn.execute(
            "INSERT OR IGNORE INTO edges (from_node_id, to_node_id, edge_type, data) VALUES (?,?,?,?)",
            (from_id, to_id, "CALLS_SERVICE", data_json),
        )
        existing_calls.add((from_id, to_id))
        results["calls_service_created"] += 1
        results["cross_service_pairs"].append({
            "caller": caller, "target": target,
            "refs": len(evidence), "rpc": rpc_count, "http": http_count,
        })

        # Also ensure DEPENDS_ON exists
        if (from_id, to_id) not in existing_depends:
            dep_data = json.dumps({
                "source": "pattern_detection",
                "discovered_at": now,
                "confidence": 0.8,
                "cross_service": True,
            })
            conn.execute(
                "INSERT OR IGNORE INTO edges (from_node_id, to_node_id, edge_type, data) VALUES (?,?,?,?)",
                (from_id, to_id, "DEPENDS_ON", dep_data),
            )
            existing_depends.add((from_id, to_id))
            results["depends_on_created"] += 1

    conn.commit()
    logger.info(f"Phase 2 done: {results['calls_service_created']} CALLS_SERVICE edges created")

    # ====== Phase 3: Cross-service IMPORTS_LIB ======
    logger.info("Phase 3: Building cross-service IMPORTS_LIB edges...")
    lib_repos = {"goutils", "integrations-go", "integrations-utils", "rpc"}

    lib_modules = {}
    for r in conn.execute(
        "SELECT id, name, json_extract(data, '$.project') as project FROM nodes WHERE type='Module' "
        "AND json_extract(data, '$.project') IN ('goutils','integrations-go','integrations-utils','rpc')"
    ).fetchall():
        lib_modules[(r["project"], r["name"])] = r["id"]

    func_by_proj = dd(list)
    for r in conn.execute(
        "SELECT id, json_extract(data, '$.project') as project, json_extract(data, '$.file') as file "
        "FROM nodes WHERE type IN ('Function','Module') AND json_extract(data, '$.project') IS NOT NULL "
        "AND json_extract(data, '$.project') NOT IN ('goutils','integrations-go','integrations-utils','rpc')"
    ).fetchall():
        if r["project"] and r["file"]:
            func_by_proj[(r["project"], r["file"])].append(r["id"])

    import_re = re.compile(r'"github\.com/razorpay/(goutils|integrations-go|integrations-utils|rpc)/([\w/.-]+)"')

    for repo in sorted(repo_dirs):
        if repo in lib_repos:
            continue
        repo_path = os.path.join(repos_path, repo)
        if not os.path.isdir(repo_path):
            continue
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in (".git", "vendor", "node_modules")]
            for fname in files:
                if not fname.endswith(".go"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", errors="replace") as f:
                        content = f.read()
                except Exception:
                    continue
                rel = os.path.relpath(fpath, repo_path)
                src_ids = func_by_proj.get((repo, rel), [])
                if not src_ids:
                    continue
                for m in import_re.finditer(content):
                    lib = m.group(1)
                    pkg = m.group(2).split("/")[0]
                    target_id = lib_modules.get((lib, pkg))
                    if not target_id:
                        continue
                    src_id = src_ids[0]
                    data_json = json.dumps({
                        "source": "import_path_match",
                        "library": lib,
                        "package": pkg,
                        "file": rel,
                        "discovered_at": now,
                        "confidence": 0.9,
                        "cross_service": True,
                    })
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO edges (from_node_id, to_node_id, edge_type, data) VALUES (?,?,?,?)",
                            (src_id, target_id, "IMPORTS_LIB", data_json),
                        )
                        results["imports_lib_created"] += 1
                    except Exception:
                        pass

    conn.commit()
    logger.info(f"Phase 3 done: {results['imports_lib_created']} IMPORTS_LIB edges created")

    # -- Final counts --
    final_counts = {}
    for etype in ["DEPENDS_ON", "CALLS_SERVICE", "IMPORTS_LIB", "CROSS_REF"]:
        total = conn.execute("SELECT count(*) FROM edges WHERE edge_type=?", (etype,)).fetchone()[0]
        final_counts[etype] = total
    results["final_edge_counts"] = final_counts

    return results


# ============================================================================
# CLI
# ============================================================================

def main():
    if len(sys.argv) < 2:
        print("Usage: rubick_graph.py <command> [db_path] [args...]")
        print("\nCommands:")
        print("  init [db_path] [name]         Initialize rubick.db")
        print("  add-node <db> --type --name    Add/update a node")
        print("  add-edge <db> --from-* --to-*  Add/update an edge")
        print("  query <db> [--type] [--limit]  Query nodes")
        print("  search <db> --text             Full-text search")
        print("  stats <db>                     Node/edge counts")
        print("  healthcheck <db>               Database health")
        print("  impact <db> --type --name      Impact analysis")
        print("  hotspots <db>                  High-connectivity nodes")
        print("  orphans <db>                   Disconnected nodes")
        print("  cycles <db>                    Circular dependencies")
        print("  cross-refs <db> --text         Cross-project references")
        print("  stale-signals <db>             Unprocessed old signals")
        print("  seed <db>                      Seed projects + channels")
        print("  sync-list <db>                 List sync states")
        print("  dag-build <db>                 Build task DAG")
        print("  topo-sort <db>                 Topological sort")
        print("  critical-path <db>             Critical path analysis")
        print("  priority-score <db>            Score all tasks")
        print("  feature-create <db> --name     Create feature")
        print("  feature-update <db> --name     Update feature")
        print("  feature-list <db>              List features")
        print("  feature-link <db>              Link node to feature")
        print("  feature-health <db> --name     Feature health report")
        print("  feature-timeline <db> --name   Feature event timeline")
        print("  archive <db>                   Archive old nodes")
        print("  migrate <db>                   Migrate schema version")
        print("  import-ast <db> <ast.json>     Import AST data")
        sys.exit(1)

    cmd = sys.argv[1]

    # DB-less commands
    if cmd == "plan-diff":
        if len(sys.argv) < 4:
            print("Usage: rubick_graph.py plan-diff <a.json> <b.json>")
            sys.exit(1)
        result = plan_diff(sys.argv[2], sys.argv[3])
        print(json.dumps(result, indent=2, default=str))
        return

    if cmd == "build-slots":
        p = argparse.ArgumentParser()
        p.add_argument("--meetings", required=True)
        p.add_argument("--start-hour", default="09:00")
        p.add_argument("--end-hour", default="19:00")
        p.add_argument("--timezone", default="Asia/Kolkata")
        p.add_argument("--min-slot", type=int, default=30)
        p.add_argument("--scope-days", type=int, default=1)
        p.add_argument("--ref-date", default=None)
        args = p.parse_args(sys.argv[2:])
        with open(args.meetings) as f:
            meetings = json.load(f)
        result = build_slots(meetings, working_hours_start=args.start_hour,
                             working_hours_end=args.end_hour, timezone_str=args.timezone,
                             min_slot_min=args.min_slot, scope_days=args.scope_days,
                             ref_date=args.ref_date)
        print(json.dumps(result, indent=2, default=str))
        return

    # Commands that need a DB path
    if len(sys.argv) < 3 and cmd != "init":
        db_path = str(cfg.RUBICK_DB_PATH) if cfg else "workspace/rubick.db"
    else:
        db_path = sys.argv[2] if len(sys.argv) > 2 else str(cfg.RUBICK_DB_PATH) if cfg else "workspace/rubick.db"

    if cmd == "init":
        name = sys.argv[3] if len(sys.argv) > 3 else "omni"
        init_db(db_path, name)
        return

    conn = get_db(db_path)

    try:
        if cmd == "add-node":
            p = argparse.ArgumentParser()
            p.add_argument("--type", required=True)
            p.add_argument("--name", required=True)
            p.add_argument("--data", default="{}")
            p.add_argument("--source-type", default="manual")
            p.add_argument("--source-id", default="")
            p.add_argument("--confidence", type=float, default=1.0)
            args = p.parse_args(sys.argv[3:])
            nid = upsert_node(conn, args.type, args.name, json.loads(args.data),
                              source_type=args.source_type, source_id=args.source_id,
                              confidence=args.confidence)
            print(json.dumps({"id": nid, "type": args.type, "name": args.name, "confidence": args.confidence}))

        elif cmd == "add-edge":
            p = argparse.ArgumentParser()
            p.add_argument("--from-type", required=True)
            p.add_argument("--from-name", required=True)
            p.add_argument("--to-type", required=True)
            p.add_argument("--to-name", required=True)
            p.add_argument("--edge-type", required=True)
            p.add_argument("--data", default="{}")
            args = p.parse_args(sys.argv[3:])
            upsert_edge(conn, args.from_type, args.from_name, args.to_type, args.to_name,
                        args.edge_type, json.loads(args.data))
            print(json.dumps({"status": "ok", "edge": f"{args.from_type}:{args.from_name} --{args.edge_type}--> {args.to_type}:{args.to_name}"}))

        elif cmd == "delete-node":
            p = argparse.ArgumentParser()
            p.add_argument("--type", required=True)
            p.add_argument("--name", required=True)
            args = p.parse_args(sys.argv[3:])
            result = delete_node(conn, args.type, args.name)
            print(json.dumps(result, indent=2))

        elif cmd == "query":
            p = argparse.ArgumentParser()
            p.add_argument("--type", default=None)
            p.add_argument("--limit", type=int, default=50)
            p.add_argument("--project", default=None)
            args = p.parse_args(sys.argv[3:])
            results = query_nodes(conn, args.type, args.limit, project_slug=args.project)
            print(json.dumps(results, indent=2))

        elif cmd == "search":
            p = argparse.ArgumentParser()
            p.add_argument("--text", required=True)
            p.add_argument("--limit", type=int, default=50)
            p.add_argument("--type", default=None)
            args = p.parse_args(sys.argv[3:])
            results = search_text(conn, args.text, args.limit, ntype=args.type)
            print(json.dumps(results, indent=2))

        elif cmd == "impact":
            p = argparse.ArgumentParser()
            p.add_argument("--type", required=True)
            p.add_argument("--name", required=True)
            p.add_argument("--depth", type=int, default=3)
            args = p.parse_args(sys.argv[3:])
            result = impact_analysis(conn, args.type, args.name, args.depth)
            print(json.dumps(result, indent=2))

        elif cmd == "export":
            p = argparse.ArgumentParser()
            p.add_argument("--type", default=None)
            p.add_argument("--depth", type=int, default=3)
            p.add_argument("--max-nodes", type=int, default=200)
            args = p.parse_args(sys.argv[3:])
            result = export_subgraph(conn, args.type, depth=args.depth, max_nodes=args.max_nodes)
            print(json.dumps(result, indent=2))

        elif cmd == "hotspots":
            print(json.dumps(find_hotspots(conn), indent=2))

        elif cmd == "orphans":
            print(json.dumps(find_orphans(conn), indent=2))

        elif cmd == "cycles":
            print(json.dumps(find_cycles(conn), indent=2))

        elif cmd == "untested":
            print(json.dumps(find_untested(conn), indent=2))

        elif cmd == "unauthed":
            print(json.dumps(find_unauthed(conn), indent=2))

        elif cmd == "high-complexity":
            print(json.dumps(find_high_complexity(conn), indent=2))

        elif cmd == "stale-signals":
            p = argparse.ArgumentParser()
            p.add_argument("--days", type=int, default=7)
            args = p.parse_args(sys.argv[3:])
            result = find_stale_signals(conn, days=args.days)
            print(json.dumps(result, indent=2))

        elif cmd == "cross-refs":
            p = argparse.ArgumentParser()
            p.add_argument("--text", required=True)
            p.add_argument("--exclude-project", default=None)
            p.add_argument("--limit", type=int, default=20)
            args = p.parse_args(sys.argv[3:])
            result = find_cross_refs(conn, args.text, exclude_project=args.exclude_project, limit=args.limit)
            print(json.dumps(result, indent=2))

        elif cmd == "seed":
            p1 = seed_projects(conn)
            p2 = seed_channels(conn)
            print(json.dumps({"projects": p1, "channels": p2}, indent=2))

        elif cmd == "sync-list":
            p = argparse.ArgumentParser()
            p.add_argument("--project", default=None)
            args = p.parse_args(sys.argv[3:])
            result = sync_list(conn, project_slug=args.project)
            print(json.dumps(result, indent=2, default=str))

        elif cmd == "stats":
            print(json.dumps(get_stats(conn), indent=2))

        elif cmd == "healthcheck":
            print(json.dumps(healthcheck(conn, db_path), indent=2))

        elif cmd == "dag-build":
            p = argparse.ArgumentParser()
            p.add_argument("--scope", default="today")
            p.add_argument("--assignee", default=None)
            p.add_argument("--now", default=None)
            args = p.parse_args(sys.argv[3:])
            result = dag_build(conn, scope=args.scope, assignee=args.assignee, ref_now=args.now)
            print(json.dumps(result, indent=2, default=str))

        elif cmd == "topo-sort":
            p = argparse.ArgumentParser()
            p.add_argument("--scope", default="today")
            p.add_argument("--assignee", default=None)
            p.add_argument("--now", default=None)
            args = p.parse_args(sys.argv[3:])
            result = topo_sort(conn, scope=args.scope, assignee=args.assignee, ref_now=args.now)
            print(json.dumps(result, indent=2, default=str))

        elif cmd == "critical-path":
            p = argparse.ArgumentParser()
            p.add_argument("--scope", default="today")
            p.add_argument("--assignee", default=None)
            p.add_argument("--now", default=None)
            args = p.parse_args(sys.argv[3:])
            result = critical_path(conn, scope=args.scope, assignee=args.assignee, ref_now=args.now)
            print(json.dumps(result, indent=2, default=str))

        elif cmd == "priority-score":
            p = argparse.ArgumentParser()
            p.add_argument("--weights", default=None)
            p.add_argument("--now", default=None)
            p.add_argument("--scope", default=None)
            p.add_argument("--assignee", default=None)
            args = p.parse_args(sys.argv[3:])
            weights = None
            if args.weights:
                with open(args.weights) as f:
                    wd = json.load(f)
                weights = wd.get("weights", wd)
            result = priority_score(conn, weights=weights, ref_now=args.now,
                                    scope=args.scope, assignee=args.assignee)
            print(json.dumps(result, indent=2, default=str))

        elif cmd == "capacity":
            p = argparse.ArgumentParser()
            p.add_argument("--slots", required=True)
            p.add_argument("--scope", default="today")
            p.add_argument("--assignee", default=None)
            p.add_argument("--now", default=None)
            args = p.parse_args(sys.argv[3:])
            result = capacity(conn, args.slots, scope=args.scope,
                              assignee=args.assignee, ref_now=args.now)
            print(json.dumps(result, indent=2, default=str))

        elif cmd == "slot-match":
            p = argparse.ArgumentParser()
            p.add_argument("--slots", required=True)
            p.add_argument("--scope", default="today")
            p.add_argument("--assignee", default=None)
            p.add_argument("--now", default=None)
            args = p.parse_args(sys.argv[3:])
            result = slot_match(conn, args.slots, scope=args.scope,
                                assignee=args.assignee, ref_now=args.now)
            print(json.dumps(result, indent=2, default=str))

        elif cmd == "plan":
            p = argparse.ArgumentParser()
            p.add_argument("--slots", required=True)
            p.add_argument("--scope", default="today")
            p.add_argument("--assignee", default=None)
            p.add_argument("--now", default=None)
            p.add_argument("--persist", action="store_true")
            args = p.parse_args(sys.argv[3:])
            result = plan(conn, args.slots, scope=args.scope,
                          assignee=args.assignee, ref_now=args.now, persist=args.persist)
            print(json.dumps(result, indent=2, default=str))

        elif cmd == "export-plan":
            p = argparse.ArgumentParser()
            p.add_argument("--plan-id", default=None)
            args = p.parse_args(sys.argv[3:])
            result = export_plan(conn, plan_name=args.plan_id)
            print(json.dumps(result, indent=2, default=str))

        elif cmd == "feature-create":
            p = argparse.ArgumentParser()
            p.add_argument("--name", required=True)
            p.add_argument("--owner", default=None)
            p.add_argument("--priority", default="P2")
            p.add_argument("--status", default="proposed")
            p.add_argument("--description", default="")
            p.add_argument("--project", default=None)
            args = p.parse_args(sys.argv[3:])
            result = feature_create(conn, args.name, owner=args.owner, priority=args.priority,
                                    status=args.status, description=args.description,
                                    project_slug=args.project)
            print(json.dumps(result, indent=2, default=str))

        elif cmd == "feature-update":
            p = argparse.ArgumentParser()
            p.add_argument("--name", required=True)
            p.add_argument("--status", default=None)
            p.add_argument("--owner", default=None)
            p.add_argument("--priority", default=None)
            p.add_argument("--description", default=None)
            args = p.parse_args(sys.argv[3:])
            result = feature_update(conn, args.name, status=args.status, owner=args.owner,
                                    priority=args.priority, description=args.description)
            print(json.dumps(result, indent=2, default=str))

        elif cmd == "feature-list":
            p = argparse.ArgumentParser()
            p.add_argument("--status", default=None)
            p.add_argument("--owner", default=None)
            p.add_argument("--project", default=None)
            args = p.parse_args(sys.argv[3:])
            result = feature_list(conn, status=args.status, owner=args.owner,
                                  project_slug=args.project)
            print(json.dumps(result, indent=2, default=str))

        elif cmd == "feature-link":
            p = argparse.ArgumentParser()
            p.add_argument("--feature", required=True)
            p.add_argument("--node-type", required=True)
            p.add_argument("--node-name", required=True)
            p.add_argument("--edge-type", required=True)
            args = p.parse_args(sys.argv[3:])
            result = feature_link(conn, args.feature, args.node_type, args.node_name, args.edge_type)
            print(json.dumps(result, indent=2, default=str))

        elif cmd == "feature-health":
            p = argparse.ArgumentParser()
            p.add_argument("--name", required=True)
            args = p.parse_args(sys.argv[3:])
            result = feature_health(conn, args.name)
            print(json.dumps(result, indent=2, default=str))

        elif cmd == "feature-timeline":
            p = argparse.ArgumentParser()
            p.add_argument("--name", required=True)
            p.add_argument("--since", default=None)
            args = p.parse_args(sys.argv[3:])
            result = feature_timeline(conn, args.name, since=args.since)
            print(json.dumps(result, indent=2, default=str))

        elif cmd == "archive":
            p = argparse.ArgumentParser()
            p.add_argument("--older-than", default="180d")
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--vacuum", action="store_true")
            args = p.parse_args(sys.argv[3:])
            m = re.match(r"^(\d+)d$", args.older_than)
            if not m:
                print(json.dumps({"error": "format: Nd (e.g. 180d)"}))
                sys.exit(1)
            result = archive(conn, older_than_days=int(m.group(1)),
                             dry_run=args.dry_run, vacuum=args.vacuum)
            print(json.dumps(result, indent=2, default=str))

        elif cmd == "migrate":
            p = argparse.ArgumentParser()
            p.add_argument("--to", default=None)
            args = p.parse_args(sys.argv[3:])
            result = migrate(conn, target_version=args.to)
            print(json.dumps(result, indent=2, default=str))

        elif cmd == "import-ast":
            if len(sys.argv) < 4:
                print("Usage: rubick_graph.py import-ast <db> <ast.json> [--project slug]")
                sys.exit(1)
            ast_path = sys.argv[3]
            project = sys.argv[5] if len(sys.argv) > 5 and sys.argv[4] == "--project" else None
            result = import_ast(conn, ast_path, project_slug=project)
            print(json.dumps(result, indent=2))

        elif cmd == "build-cross-edges":
            repos_path = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspace", "repos")
            result = build_cross_service_edges(conn, repos_path)
            print(json.dumps(result, indent=2, default=str))

        elif cmd == "smart-reset":
            ws = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspace")
            result = smart_reset(conn, workspace_path=ws)
            print(json.dumps(result, indent=2, default=str))

        elif cmd == "pipeline-status":
            p = argparse.ArgumentParser()
            p.add_argument("--slug", required=True)
            args = p.parse_args(sys.argv[3:])
            ws = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspace")
            result = pipeline_status(ws, args.slug)
            print(json.dumps(result, indent=2))

        elif cmd == "audit":
            ws = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspace")
            result = audit_report(conn, db_path, workspace_path=ws)
            print(json.dumps(result, indent=2))

        elif cmd == "export-shareable":
            p = argparse.ArgumentParser()
            p.add_argument("--output", default="nemesis-brain.tar.gz")
            args = p.parse_args(sys.argv[3:])
            ws = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspace")
            conn.close()
            result = export_shareable(db_path, args.output, workspace_path=ws)
            print(json.dumps(result, indent=2))
            return

        elif cmd == "import-shareable":
            if len(sys.argv) < 4:
                print("Usage: rubick_graph.py import-shareable <db> <archive.tar.gz>")
                sys.exit(1)
            conn.close()
            ws = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspace")
            result = import_shareable(sys.argv[3], workspace_path=ws)
            print(json.dumps(result, indent=2))
            return

        else:
            print(f"Unknown command: {cmd}")
            sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
