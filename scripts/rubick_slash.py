#!/usr/bin/env python3
"""Omni Slash — @slash bot interaction manager for Rubick.

Manages the send-wait-store cycle for querying @slash (Razorpay's internal
knowledge bot) via Slack channel C0B3U3Z2JG1. The actual Slack I/O is done
by Claude Code using MCP tools; this script handles persistence in rubick.db.

Usage (called by Claude Code / arch skill):
    rubick_slash.py format-question  --question "..." [--feature F] [--context C]
    rubick_slash.py store            --question "..." --response "..." [--feature F] [--thread-ts T]
    rubick_slash.py recall           [--feature F] [--query Q] [--limit N]
    rubick_slash.py pending          [--feature F]
    rubick_slash.py list             [--feature F] [--limit N]
    rubick_slash.py questions        --feature F [--scope discovery|deep|impact]
"""

import sys
import json
import sqlite3
import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import brain_config as cfg
except ImportError:
    cfg = None

DB_PATH = (cfg.RUBICK_DB_PATH if cfg else
           Path.home() / "Projects" / "Agents" / "nemesis_v2" / "workspace" / "rubick.db")

SLASH_CHANNEL = "C0B3U3Z2JG1"
SLASH_BOT_USER_ID = "U0AK4Q67HEY"
SLASH_MENTION = f"<@{SLASH_BOT_USER_ID}>"


def get_db(path=None):
    p = str(path or DB_PATH)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_slash_table(conn)
    return conn


def _ensure_slash_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS slash_interactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            question    TEXT NOT NULL,
            response    TEXT,
            feature     TEXT,
            scope       TEXT DEFAULT 'general',
            thread_ts   TEXT,
            channel_id  TEXT DEFAULT 'C0B3U3Z2JG1',
            status      TEXT DEFAULT 'pending',  -- pending | answered | stale
            asked_at    TEXT NOT NULL,
            answered_at TEXT,
            data        TEXT DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_slash_feature
        ON slash_interactions(feature)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_slash_status
        ON slash_interactions(status)
    """)
    conn.commit()


def format_question(question: str, feature: str = None, context: str = None) -> dict:
    """Format a question for @slash with proper mention and context."""
    parts = [SLASH_MENTION]

    if feature:
        parts.append(f"[Feature: {feature}]")

    parts.append(question)

    if context:
        parts.append(f"\n\nContext: {context}")

    message = " ".join(parts)

    return {
        "channel": SLASH_CHANNEL,
        "message": message,
        "slash_mention": SLASH_MENTION,
        "bot_user_id": SLASH_BOT_USER_ID,
    }


def store_question(conn, question: str, feature: str = None, thread_ts: str = None,
                   scope: str = "general") -> int:
    """Record that a question was sent (before response arrives)."""
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute("""
        INSERT INTO slash_interactions (question, feature, scope, thread_ts, channel_id, status, asked_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?)
    """, (question, feature, scope, thread_ts, SLASH_CHANNEL, now))
    conn.commit()
    return cur.lastrowid


def store_response(conn, question: str, response: str, feature: str = None,
                   thread_ts: str = None, scope: str = "general",
                   data: dict = None) -> int:
    """Store a complete Q&A interaction."""
    now = datetime.now(timezone.utc).isoformat()

    existing = conn.execute("""
        SELECT id FROM slash_interactions
        WHERE question = ? AND feature IS ? AND status = 'pending'
        ORDER BY asked_at DESC LIMIT 1
    """, (question, feature)).fetchone()

    if existing:
        conn.execute("""
            UPDATE slash_interactions
            SET response = ?, status = 'answered', answered_at = ?,
                thread_ts = COALESCE(?, thread_ts), data = ?
            WHERE id = ?
        """, (response, now, thread_ts, json.dumps(data or {}), existing["id"]))
        conn.commit()
        return existing["id"]

    cur = conn.execute("""
        INSERT INTO slash_interactions
            (question, response, feature, scope, thread_ts, channel_id, status, asked_at, answered_at, data)
        VALUES (?, ?, ?, ?, ?, ?, 'answered', ?, ?, ?)
    """, (question, response, feature, scope, thread_ts, SLASH_CHANNEL, now, now,
          json.dumps(data or {})))
    conn.commit()

    _persist_to_graph(conn, question, response, feature, thread_ts)

    return cur.lastrowid


def _persist_to_graph(conn, question: str, response: str, feature: str,
                      thread_ts: str = None):
    """Also store as a Signal node in the main graph for context retrieval."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        node_name = f"slash:{question[:80]}"
        summary = response[:500] if response else ""

        conn.execute("""
            INSERT INTO nodes (type, name, summary, source_type, source_id, ingested_at, confidence, data)
            VALUES ('Signal', ?, ?, 'slash_bot', ?, ?, 0.85, ?)
            ON CONFLICT(type, name) DO UPDATE SET
                summary = excluded.summary,
                data = excluded.data,
                ingested_at = excluded.ingested_at
        """, (node_name, summary, thread_ts or "", now,
              json.dumps({
                  "signal_type": "slash_response",
                  "question": question,
                  "feature": feature,
                  "channel": SLASH_CHANNEL,
                  "full_response_length": len(response) if response else 0,
              })))

        if feature:
            feat_row = conn.execute(
                "SELECT id FROM nodes WHERE type='Feature' AND name=?", (feature,)
            ).fetchone()
            node_row = conn.execute(
                "SELECT id FROM nodes WHERE type='Signal' AND name=?", (node_name,)
            ).fetchone()
            if feat_row and node_row:
                conn.execute("""
                    INSERT INTO edges (from_id, to_id, edge_type, data)
                    VALUES (?, ?, 'SIGNAL_FOR', '{}')
                    ON CONFLICT(from_id, to_id, edge_type) DO NOTHING
                """, (node_row["id"], feat_row["id"]))

        conn.commit()
    except Exception:
        pass


def recall(conn, feature: str = None, query: str = None, limit: int = 10) -> list[dict]:
    """Retrieve stored Slash responses, optionally filtered by feature/query."""
    conditions = ["status = 'answered'"]
    params = []

    if feature:
        conditions.append("feature = ?")
        params.append(feature)

    if query:
        conditions.append("(question LIKE ? OR response LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])

    where = " AND ".join(conditions)
    rows = conn.execute(f"""
        SELECT id, question, response, feature, scope, thread_ts, asked_at, answered_at
        FROM slash_interactions
        WHERE {where}
        ORDER BY answered_at DESC
        LIMIT ?
    """, params + [limit]).fetchall()

    return [dict(r) for r in rows]


def get_pending(conn, feature: str = None) -> list[dict]:
    """List questions still awaiting Slash response."""
    if feature:
        rows = conn.execute("""
            SELECT id, question, feature, scope, thread_ts, asked_at
            FROM slash_interactions
            WHERE status = 'pending' AND feature = ?
            ORDER BY asked_at DESC
        """, (feature,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT id, question, feature, scope, thread_ts, asked_at
            FROM slash_interactions
            WHERE status = 'pending'
            ORDER BY asked_at DESC
        """).fetchall()
    return [dict(r) for r in rows]


def list_interactions(conn, feature: str = None, limit: int = 20) -> list[dict]:
    """List all Slash interactions."""
    if feature:
        rows = conn.execute("""
            SELECT id, question, feature, scope, status, asked_at, answered_at,
                   CASE WHEN response IS NOT NULL THEN length(response) ELSE 0 END as response_len
            FROM slash_interactions
            WHERE feature = ?
            ORDER BY asked_at DESC LIMIT ?
        """, (feature, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT id, question, feature, scope, status, asked_at, answered_at,
                   CASE WHEN response IS NOT NULL THEN length(response) ELSE 0 END as response_len
            FROM slash_interactions
            ORDER BY asked_at DESC LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def generate_questions(feature: str, scope: str = "discovery") -> list[dict]:
    """Generate structured questions for @slash based on feature and scope.

    Scopes:
        discovery  — initial broad scan (repos, services, flows, owners)
        deep       — detailed code-level analysis (functions, patterns, edge cases)
        impact     — cross-project impact (dependencies, shared resources, risks)
    """
    templates = {
        "discovery": [
            {
                "id": "repos",
                "question": f"Which Razorpay repos and services are involved in the {feature} feature? "
                           f"List each repo with its role (primary/ecosystem) and the key files/modules.",
                "purpose": "Map the service landscape",
            },
            {
                "id": "flow",
                "question": f"Describe the end-to-end flow for {feature}. "
                           f"Include the API endpoints hit, services called, database tables touched, "
                           f"and any async jobs or event flows.",
                "purpose": "Understand the data/control flow",
            },
            {
                "id": "owners",
                "question": f"Who are the code owners and domain experts for the {feature} feature? "
                           f"Which teams own the relevant repos?",
                "purpose": "Identify stakeholders",
            },
            {
                "id": "config",
                "question": f"What feature flags, DCS configs, or experiments control the {feature} feature? "
                           f"List each flag with its current state and which service reads it.",
                "purpose": "Map feature toggles",
            },
        ],
        "deep": [
            {
                "id": "code_paths",
                "question": f"For the {feature} feature, show me the critical code paths — "
                           f"key functions, their file locations, and what each one does. "
                           f"Include error handling and edge cases.",
                "purpose": "Deep code understanding",
            },
            {
                "id": "data_model",
                "question": f"What database tables and fields are used by {feature}? "
                           f"Include table schemas, indexes, and any migrations related to this feature.",
                "purpose": "Data layer analysis",
            },
            {
                "id": "contracts",
                "question": f"What are the API contracts (request/response schemas) for {feature}? "
                           f"Include internal RPC protos and external merchant-facing APIs.",
                "purpose": "Contract analysis",
            },
        ],
        "impact": [
            {
                "id": "dependencies",
                "question": f"What are the upstream and downstream dependencies of {feature}? "
                           f"Which other features or services would break if {feature} changes?",
                "purpose": "Dependency mapping",
            },
            {
                "id": "recent_changes",
                "question": f"What recent PRs, commits, or incidents have touched the {feature} feature "
                           f"in the last 30 days?",
                "purpose": "Recent activity",
            },
            {
                "id": "risks",
                "question": f"What are the known risks, edge cases, or past incidents related to {feature}? "
                           f"Include any oncall alerts or monitoring dashboards.",
                "purpose": "Risk intelligence",
            },
        ],
    }

    return templates.get(scope, templates["discovery"])


def main():
    if len(sys.argv) < 2:
        print("Usage: rubick_slash.py <command> [args]")
        print("\nCommands:")
        print("  format-question  --question Q [--feature F] [--context C]")
        print("  store            --question Q --response R [--feature F] [--thread-ts T]")
        print("  recall           [--feature F] [--query Q] [--limit N]")
        print("  pending          [--feature F]")
        print("  list             [--feature F] [--limit N]")
        print("  questions        --feature F [--scope discovery|deep|impact]")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "format-question":
        p = argparse.ArgumentParser()
        p.add_argument("--question", required=True)
        p.add_argument("--feature", default=None)
        p.add_argument("--context", default=None)
        args = p.parse_args(sys.argv[2:])
        result = format_question(args.question, args.feature, args.context)
        print(json.dumps(result, indent=2))
        return

    if cmd == "questions":
        p = argparse.ArgumentParser()
        p.add_argument("--feature", required=True)
        p.add_argument("--scope", default="discovery",
                       choices=["discovery", "deep", "impact"])
        args = p.parse_args(sys.argv[2:])
        result = generate_questions(args.feature, args.scope)
        print(json.dumps(result, indent=2))
        return

    conn = get_db()

    try:
        if cmd == "store":
            p = argparse.ArgumentParser()
            p.add_argument("--question", required=True)
            p.add_argument("--response", required=True)
            p.add_argument("--feature", default=None)
            p.add_argument("--thread-ts", default=None)
            p.add_argument("--scope", default="general")
            p.add_argument("--data", default="{}")
            args = p.parse_args(sys.argv[2:])
            rid = store_response(conn, args.question, args.response, args.feature,
                                 args.thread_ts, args.scope, json.loads(args.data))
            print(json.dumps({"ok": True, "id": rid, "status": "answered"}))

        elif cmd == "record-pending":
            p = argparse.ArgumentParser()
            p.add_argument("--question", required=True)
            p.add_argument("--feature", default=None)
            p.add_argument("--thread-ts", default=None)
            p.add_argument("--scope", default="general")
            args = p.parse_args(sys.argv[2:])
            rid = store_question(conn, args.question, args.feature, args.thread_ts, args.scope)
            print(json.dumps({"ok": True, "id": rid, "status": "pending"}))

        elif cmd == "recall":
            p = argparse.ArgumentParser()
            p.add_argument("--feature", default=None)
            p.add_argument("--query", default=None)
            p.add_argument("--limit", type=int, default=10)
            args = p.parse_args(sys.argv[2:])
            results = recall(conn, args.feature, args.query, args.limit)
            print(json.dumps({"count": len(results), "results": results}, indent=2))

        elif cmd == "pending":
            p = argparse.ArgumentParser()
            p.add_argument("--feature", default=None)
            args = p.parse_args(sys.argv[2:])
            results = get_pending(conn, args.feature)
            print(json.dumps({"count": len(results), "results": results}, indent=2))

        elif cmd == "list":
            p = argparse.ArgumentParser()
            p.add_argument("--feature", default=None)
            p.add_argument("--limit", type=int, default=20)
            args = p.parse_args(sys.argv[2:])
            results = list_interactions(conn, args.feature, args.limit)
            print(json.dumps({"count": len(results), "results": results}, indent=2))

        else:
            print(json.dumps({"error": f"unknown command: {cmd}"}))
            sys.exit(1)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
