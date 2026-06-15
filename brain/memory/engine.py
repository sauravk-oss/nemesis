"""Memory engine — learning pipeline, sync state, interactions.

Handles knowledge staging (record) → flush to graph → provenance tracking.
Confidence evolution: 0.7 (LLM) → 0.85 (multi-source) → 1.0 (user-confirmed).
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from brain.config import (LEARNING_DECAY_DAYS, LEARNING_DECAY_FACTOR,
                          LEARNING_DEFAULT_CONFIDENCE, LEARNING_MULTI_SOURCE_CONFIDENCE)
from brain.graph.engine import GraphEngine
from brain.types import LearningItem


class MemoryEngine:

    def __init__(self, engine: GraphEngine):
        self._engine = engine

    # --- Learning Pipeline ---
    def record(self, source_skill: str, items: List[LearningItem],
               interaction_type: str = "analysis") -> int:
        conn = self._engine.conn
        cur = conn.execute(
            "INSERT INTO interactions(session_id, question, query_type) VALUES (?, ?, ?)",
            (f"{source_skill}_{int(time.time())}", f"auto:{source_skill}", interaction_type))
        interaction_id = cur.lastrowid

        for item in items:
            conn.execute(
                """INSERT INTO learning_ledger(interaction_id, type, source_skill,
                   node_type, node_name, node_data, confidence, edges, status)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (interaction_id, interaction_type, source_skill,
                 item.node_type, item.node_name, json.dumps(item.node_data),
                 item.confidence, json.dumps(item.edges), "staged"))
        conn.commit()
        return interaction_id

    def flush(self, interaction_id: int = None, dry_run: bool = False) -> Dict[str, int]:
        conn = self._engine.conn
        sql = "SELECT * FROM learning_ledger WHERE status='staged'"
        params = []
        if interaction_id:
            sql += " AND interaction_id=?"
            params.append(interaction_id)

        rows = conn.execute(sql, params).fetchall()
        stats = {"created": 0, "merged": 0, "skipped": 0, "edges": 0}

        for row in rows:
            row = dict(row)
            ntype = row["node_type"]
            nname = row["node_name"]
            ndata = json.loads(row.get("node_data") or "{}")
            confidence = row.get("confidence", LEARNING_DEFAULT_CONFIDENCE)
            edges = json.loads(row.get("edges") or "[]")

            existing = self._engine.get_node(ntype, nname)

            if dry_run:
                stats["created" if not existing else "merged"] += 1
                stats["edges"] += len(edges)
                continue

            if existing:
                new_conf = max(existing.get("confidence", 0), confidence)
                if confidence >= LEARNING_MULTI_SOURCE_CONFIDENCE:
                    new_conf = max(new_conf, LEARNING_MULTI_SOURCE_CONFIDENCE)
                merged_data = {**existing.get("data", {}), **ndata}
                self._engine.upsert_node(
                    ntype, nname, data=merged_data,
                    project_slug=ndata.get("project") or existing.get("project_slug"),
                    source_type=row.get("source_skill"),
                    confidence=new_conf)
                stats["merged"] += 1
            else:
                self._engine.upsert_node(
                    ntype, nname, data=ndata,
                    project_slug=ndata.get("project"),
                    source_type=row.get("source_skill"),
                    confidence=confidence)
                stats["created"] += 1

            for edge in edges:
                self._engine.add_edge(
                    edge.get("from_type", ntype), edge.get("from_name", nname),
                    edge["to_type"], edge["to_name"], edge["edge_type"])
                stats["edges"] += 1

            conn.execute(
                "UPDATE learning_ledger SET status='flushed' WHERE id=?",
                (row["id"],))

        conn.commit()
        return stats

    def status(self) -> Dict[str, Any]:
        conn = self._engine.conn
        counts = {}
        for status in ["staged", "flushed", "skipped"]:
            counts[status] = conn.execute(
                "SELECT COUNT(*) FROM learning_ledger WHERE status=?",
                (status,)).fetchone()[0]

        recent = conn.execute(
            """SELECT interaction_id, source_skill, type, COUNT(*) as items,
                      MAX(created_at) as last_at
               FROM learning_ledger
               GROUP BY interaction_id
               ORDER BY last_at DESC LIMIT 10""").fetchall()

        return {"counts": counts, "recent": [dict(r) for r in recent]}

    def decay_report(self, days: int = None) -> List[Dict]:
        days = days or LEARNING_DECAY_DAYS
        conn = self._engine.conn
        rows = conn.execute(
            """SELECT * FROM nodes
               WHERE confidence < 0.85
                 AND updated_at < datetime('now', ? || ' days')
               ORDER BY updated_at ASC LIMIT 50""",
            (f"-{days}",)).fetchall()
        return [dict(r) for r in rows]

    # --- Sync State ---
    def get_sync_cursor(self, source_type: str, source_id: str) -> Optional[str]:
        r = self._engine.conn.execute(
            "SELECT last_cursor FROM sync_state WHERE source_type=? AND source_id=?",
            (source_type, source_id)).fetchone()
        return r[0] if r else None

    def update_sync_cursor(self, source_type: str, source_id: str,
                           cursor: str, project: str = None) -> None:
        self._engine.conn.execute(
            """INSERT INTO sync_state(source_type, source_id, project_slug, last_cursor, last_sync)
               VALUES (?,?,?,?,datetime('now'))
               ON CONFLICT(source_type, source_id) DO UPDATE SET
                 last_cursor=excluded.last_cursor, last_sync=datetime('now')""",
            (source_type, source_id, project, cursor))
        self._engine.conn.commit()

    # --- Slash Interactions ---
    def store_slash(self, question: str, response: str = None,
                    feature: str = None, thread_ts: str = None) -> int:
        cur = self._engine.conn.execute(
            """INSERT INTO slash_interactions(question, response, feature, thread_ts, channel_id, status)
               VALUES (?,?,?,?,?,?)""",
            (question, response, feature, thread_ts, "C0B3U3Z2JG1",
             "answered" if response else "pending"))
        self._engine.conn.commit()
        return cur.lastrowid

    def recall_slash(self, query: str, limit: int = 5) -> List[Dict]:
        rows = self._engine.conn.execute(
            """SELECT * FROM slash_interactions
               WHERE question LIKE ? OR response LIKE ?
               ORDER BY created_at DESC LIMIT ?""",
            (f"%{query}%", f"%{query}%", limit)).fetchall()
        return [dict(r) for r in rows]
