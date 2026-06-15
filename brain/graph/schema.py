"""SQLite DDL for brain.db — all tables, indexes, FTS5, triggers.

Typed tables for code (715K nodes), generic table for workflow (~500 nodes).
Single edges table. WAL mode. FTS5 for text search.
"""
from __future__ import annotations

import sqlite3

SCHEMA_VERSION = "2.0.0"

_TYPED_CODE_TABLES = """
CREATE TABLE IF NOT EXISTS services (
    slug TEXT PRIMARY KEY, name TEXT, role TEXT, language TEXT,
    url TEXT, team TEXT, description TEXT
);
CREATE TABLE IF NOT EXISTS files (
    path TEXT NOT NULL, project TEXT NOT NULL, language TEXT,
    line_count INTEGER, hash TEXT,
    PRIMARY KEY (path, project)
);
CREATE TABLE IF NOT EXISTS functions (
    qname TEXT PRIMARY KEY, name TEXT NOT NULL, file_path TEXT,
    line_start INTEGER, line_end INTEGER, language TEXT,
    signature TEXT, receiver TEXT, params TEXT, returns TEXT,
    complexity REAL DEFAULT 0.0, is_exported BOOLEAN DEFAULT 0,
    is_test BOOLEAN DEFAULT 0, project TEXT, body_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_func_project ON functions(project);
CREATE INDEX IF NOT EXISTS idx_func_file ON functions(file_path);
CREATE INDEX IF NOT EXISTS idx_func_name ON functions(name);

CREATE TABLE IF NOT EXISTS classes (
    qname TEXT PRIMARY KEY, name TEXT NOT NULL, file_path TEXT,
    line_start INTEGER, line_end INTEGER, language TEXT,
    kind TEXT DEFAULT 'class', is_exported BOOLEAN DEFAULT 0, project TEXT
);
CREATE INDEX IF NOT EXISTS idx_class_project ON classes(project);

CREATE TABLE IF NOT EXISTS modules (
    path TEXT NOT NULL, project TEXT NOT NULL, is_external BOOLEAN DEFAULT 0,
    PRIMARY KEY (path, project)
);
CREATE TABLE IF NOT EXISTS endpoints (
    route TEXT NOT NULL, http_method TEXT NOT NULL, handler TEXT,
    file_path TEXT, line INTEGER, auth_required BOOLEAN DEFAULT 0, project TEXT,
    PRIMARY KEY (route, http_method, project)
);
CREATE INDEX IF NOT EXISTS idx_ep_project ON endpoints(project);
CREATE INDEX IF NOT EXISTS idx_ep_handler ON endpoints(handler);

CREATE TABLE IF NOT EXISTS datastores (
    name TEXT NOT NULL, store_type TEXT, engine TEXT, project TEXT, schema_def TEXT,
    PRIMARY KEY (name, project)
);
CREATE TABLE IF NOT EXISTS tests (
    qname TEXT PRIMARY KEY, name TEXT NOT NULL, file_path TEXT,
    line_start INTEGER, line_end INTEGER, kind TEXT DEFAULT 'unit', project TEXT
);
CREATE INDEX IF NOT EXISTS idx_test_project ON tests(project);

CREATE TABLE IF NOT EXISTS kafka_topics (name TEXT PRIMARY KEY, project TEXT);
"""

_GENERIC_NODES = """
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL, name TEXT NOT NULL,
    data TEXT DEFAULT '{}',
    project_slug TEXT, source_type TEXT, source_id TEXT,
    confidence REAL DEFAULT 0.7,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    retention_days INTEGER DEFAULT -1,
    UNIQUE(type, name)
);
CREATE INDEX IF NOT EXISTS idx_node_type ON nodes(type);
CREATE INDEX IF NOT EXISTS idx_node_project ON nodes(project_slug);
CREATE INDEX IF NOT EXISTS idx_node_source ON nodes(source_type, source_id);
"""

_EDGES = """
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_type TEXT NOT NULL, from_name TEXT NOT NULL,
    to_type TEXT NOT NULL, to_name TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    data TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(from_type, from_name, to_type, to_name, edge_type)
);
CREATE INDEX IF NOT EXISTS idx_edge_from ON edges(from_type, from_name, edge_type);
CREATE INDEX IF NOT EXISTS idx_edge_to ON edges(to_type, to_name, edge_type);
CREATE INDEX IF NOT EXISTS idx_edge_type ON edges(edge_type);
"""

_CODE_BODIES = """
CREATE TABLE IF NOT EXISTS code_bodies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL UNIQUE,
    project TEXT, file_path TEXT,
    start_line INTEGER, end_line INTEGER, language TEXT,
    body TEXT NOT NULL, body_hash TEXT NOT NULL,
    byte_length INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cb_project ON code_bodies(project);
CREATE INDEX IF NOT EXISTS idx_cb_hash ON code_bodies(body_hash);

CREATE TABLE IF NOT EXISTS code_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    body_id INTEGER NOT NULL REFERENCES code_bodies(id),
    node_id TEXT NOT NULL, chunk_index INTEGER DEFAULT 0,
    content TEXT NOT NULL,
    start_line INTEGER, end_line INTEGER,
    token_estimate INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chunk_node ON code_chunks(node_id);
"""

_KNOWLEDGE = """
CREATE TABLE IF NOT EXISTS triplets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL, predicate TEXT NOT NULL, object TEXT NOT NULL,
    source TEXT, confidence REAL DEFAULT 0.7, project TEXT, category TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(subject, predicate, object)
);
CREATE INDEX IF NOT EXISTS idx_trip_subject ON triplets(subject);

CREATE TABLE IF NOT EXISTS arch_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL, decision TEXT, rationale TEXT, alternatives TEXT,
    status TEXT DEFAULT 'proposed', project TEXT, confidence REAL DEFAULT 0.7,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(title, project)
);

CREATE TABLE IF NOT EXISTS business_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, description TEXT, domain TEXT,
    conditions TEXT, actions TEXT, exceptions TEXT, source_functions TEXT,
    confidence REAL DEFAULT 0.7, project TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(name, project)
);
"""

_MEMORY = """
CREATE TABLE IF NOT EXISTS interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT, question TEXT, answer TEXT,
    query_type TEXT, brains_used TEXT,
    tokens_used INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS learning_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interaction_id INTEGER, type TEXT, source_skill TEXT,
    node_type TEXT, node_name TEXT, node_data TEXT DEFAULT '{}',
    confidence REAL DEFAULT 0.7, edges TEXT DEFAULT '[]',
    status TEXT DEFAULT 'staged',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ledger_status ON learning_ledger(status);

CREATE TABLE IF NOT EXISTS sync_state (
    source_type TEXT NOT NULL, source_id TEXT NOT NULL,
    project_slug TEXT, last_cursor TEXT,
    last_sync TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (source_type, source_id)
);

CREATE TABLE IF NOT EXISTS slash_interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT, response TEXT, feature TEXT, scope TEXT,
    thread_ts TEXT, channel_id TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now'))
);
"""

_FTS5 = """
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    name, data, type, content='nodes', content_rowid='id'
);
CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(
    body, node_id, project, content='code_bodies', content_rowid='id'
);
CREATE VIRTUAL TABLE IF NOT EXISTS triplets_fts USING fts5(
    subject, predicate, object, content='triplets', content_rowid='id'
);
"""

_FTS5_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS nodes_ai AFTER INSERT ON nodes BEGIN
    INSERT INTO nodes_fts(rowid, name, data, type) VALUES (new.id, new.name, new.data, new.type);
END;
CREATE TRIGGER IF NOT EXISTS nodes_ad AFTER DELETE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, name, data, type) VALUES ('delete', old.id, old.name, old.data, old.type);
END;
CREATE TRIGGER IF NOT EXISTS nodes_au AFTER UPDATE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, name, data, type) VALUES ('delete', old.id, old.name, old.data, old.type);
    INSERT INTO nodes_fts(rowid, name, data, type) VALUES (new.id, new.name, new.data, new.type);
END;
CREATE TRIGGER IF NOT EXISTS cb_ai AFTER INSERT ON code_bodies BEGIN
    INSERT INTO code_fts(rowid, body, node_id, project) VALUES (new.id, new.body, new.node_id, new.project);
END;
CREATE TRIGGER IF NOT EXISTS cb_ad AFTER DELETE ON code_bodies BEGIN
    INSERT INTO code_fts(code_fts, rowid, body, node_id, project) VALUES ('delete', old.id, old.body, old.node_id, old.project);
END;
CREATE TRIGGER IF NOT EXISTS trip_ai AFTER INSERT ON triplets BEGIN
    INSERT INTO triplets_fts(rowid, subject, predicate, object) VALUES (new.id, new.subject, new.predicate, new.object);
END;
CREATE TRIGGER IF NOT EXISTS trip_ad AFTER DELETE ON triplets BEGIN
    INSERT INTO triplets_fts(triplets_fts, rowid, subject, predicate, object) VALUES ('delete', old.id, old.subject, old.predicate, old.object);
END;
"""

_META = """
CREATE TABLE IF NOT EXISTS brain_meta (key TEXT PRIMARY KEY, value TEXT);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-20000")

    for ddl in [_TYPED_CODE_TABLES, _GENERIC_NODES, _EDGES, _CODE_BODIES,
                _KNOWLEDGE, _MEMORY, _FTS5, _FTS5_TRIGGERS, _META]:
        conn.executescript(ddl)

    conn.execute(
        "INSERT OR REPLACE INTO brain_meta(key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()
