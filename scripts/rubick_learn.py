#!/usr/bin/env python3
"""Omni Learn — continuous learning pipeline for Nemesis v2.

Every skill interaction becomes a learning opportunity. Extracted knowledge is
staged in `learning_ledger`, then flushed to rubick.db as nodes + edges.

Usage:
    rubick_learn.py record --interaction-type T --source-skill S --items JSON [--project P]
    rubick_learn.py flush [--interaction-id ID] [--dry-run]
    rubick_learn.py status
    rubick_learn.py history [--node-type T] [--node-name N] [--limit N]
    rubick_learn.py stats
    rubick_learn.py decay-report [--days 90]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import brain_config as cfg

DB_PATH = str(cfg.RUBICK_DB_PATH)


def get_db(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ---------------------------------------------------------------------------
# record — stage knowledge items in the learning ledger
# ---------------------------------------------------------------------------

def record(interaction_type: str, source_skill: str, items: list[dict],
           project: str = "_global") -> dict:
    interaction_id = f"learn-{uuid.uuid4().hex[:12]}"
    conn = get_db()

    staged = 0
    for item in items[:cfg.LEARNING_MAX_ITEMS_PER_INTERACTION]:
        node_type = item.get("type", "Signal")
        node_name = item.get("name", "")
        if not node_name:
            continue
        node_data = json.dumps(item.get("data", {}))
        confidence = item.get("confidence", cfg.LEARNING_DEFAULT_CONFIDENCE)
        edges = json.dumps(item.get("edges", []))

        conn.execute(
            """INSERT INTO learning_ledger
               (interaction_id, interaction_type, source_skill, project_slug,
                node_type, node_name, node_data, confidence, edges)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (interaction_id, interaction_type, source_skill, project,
             node_type, node_name, node_data, confidence, edges)
        )
        staged += 1

    conn.commit()
    conn.close()
    return {"interaction_id": interaction_id, "staged": staged}


# ---------------------------------------------------------------------------
# flush — promote staged items to nodes + edges in rubick.db
# ---------------------------------------------------------------------------

def flush(interaction_id: str = None, dry_run: bool = False) -> dict:
    conn = get_db()

    where = "WHERE status = 'staged'"
    params = []
    if interaction_id:
        where += " AND interaction_id = ?"
        params.append(interaction_id)

    rows = conn.execute(
        f"SELECT * FROM learning_ledger {where} ORDER BY created_at", params
    ).fetchall()

    created = 0
    merged = 0
    edges_created = 0
    skipped = 0

    for row in rows:
        node_type = row["node_type"]
        node_name = row["node_name"]
        node_data = json.loads(row["node_data"])
        confidence = row["confidence"]
        edge_defs = json.loads(row["edges"])

        existing = conn.execute(
            "SELECT id, confidence, data FROM nodes WHERE type = ? AND name = ?",
            (node_type, node_name)
        ).fetchone()

        action = "created"
        if existing:
            existing_data = json.loads(existing["data"])
            if existing["confidence"] >= confidence and not _has_new_info(existing_data, node_data):
                if not dry_run:
                    conn.execute(
                        "UPDATE learning_ledger SET status = 'skipped', flushed_at = ? WHERE id = ?",
                        (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), row["id"])
                    )
                skipped += 1
                continue

            merged_data = {**existing_data, **node_data}
            new_confidence = max(existing["confidence"], confidence)
            if _is_multi_source(conn, node_type, node_name, row["source_skill"]):
                new_confidence = max(new_confidence, cfg.LEARNING_MULTI_SOURCE_CONFIDENCE)

            if not dry_run:
                conn.execute(
                    """UPDATE nodes SET data = ?, confidence = ?,
                       updated_at = datetime('now'), source_type = ?
                       WHERE id = ?""",
                    (json.dumps(merged_data), new_confidence, row["source_skill"], existing["id"])
                )
                _update_fts(conn, existing["id"], node_name, merged_data)
            action = "merged"
            merged += 1
        else:
            if not dry_run:
                now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    """INSERT INTO nodes (type, name, data, source_type, source_id,
                       ingested_at, confidence, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (node_type, node_name, json.dumps(node_data),
                     row["source_skill"], row["interaction_id"], now, confidence, now)
                )
            created += 1

        if not dry_run:
            node_id_row = conn.execute(
                "SELECT id FROM nodes WHERE type = ? AND name = ?",
                (node_type, node_name)
            ).fetchone()
            node_id = node_id_row["id"] if node_id_row else -1

            conn.execute(
                """INSERT INTO learning_provenance
                   (interaction_id, source_skill, node_id, action)
                   VALUES (?, ?, ?, ?)""",
                (row["interaction_id"], row["source_skill"], node_id, action)
            )

            for edge_def in edge_defs:
                _create_edge(conn, node_type, node_name, edge_def)
                edges_created += 1

            conn.execute(
                "UPDATE learning_ledger SET status = 'flushed', flushed_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), row["id"])
            )

    if not dry_run:
        _create_cross_links(conn, rows)
        conn.commit()
    conn.close()

    return {
        "processed": len(rows),
        "created": created,
        "merged": merged,
        "edges": edges_created,
        "skipped": skipped,
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# status — show learning ledger state
# ---------------------------------------------------------------------------

def status() -> dict:
    conn = get_db()
    counts = {}
    for row in conn.execute(
        "SELECT status, COUNT(*) as cnt FROM learning_ledger GROUP BY status"
    ):
        counts[row["status"]] = row["cnt"]

    recent = conn.execute(
        """SELECT interaction_id, source_skill, interaction_type, COUNT(*) as items,
                  MIN(created_at) as ts
           FROM learning_ledger
           GROUP BY interaction_id
           ORDER BY ts DESC LIMIT 5"""
    ).fetchall()

    conn.close()
    return {
        "ledger_counts": counts,
        "recent_interactions": [dict(r) for r in recent],
    }


# ---------------------------------------------------------------------------
# history — track what produced a given node
# ---------------------------------------------------------------------------

def history(node_type: str = None, node_name: str = None, limit: int = 20) -> list:
    conn = get_db()
    query = """
        SELECT lp.interaction_id, lp.source_skill, lp.action, lp.created_at,
               n.type as node_type, n.name as node_name, n.confidence
        FROM learning_provenance lp
        JOIN nodes n ON n.id = lp.node_id
    """
    params = []
    conditions = []
    if node_type:
        conditions.append("n.type = ?")
        params.append(node_type)
    if node_name:
        conditions.append("n.name LIKE ?")
        params.append(f"%{node_name}%")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += f" ORDER BY lp.created_at DESC LIMIT {limit}"

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# stats — aggregate learning metrics
# ---------------------------------------------------------------------------

def stats() -> dict:
    conn = get_db()

    total_learned = conn.execute(
        "SELECT COUNT(*) FROM learning_provenance"
    ).fetchone()[0]

    by_skill = conn.execute(
        """SELECT source_skill, COUNT(*) as cnt
           FROM learning_provenance GROUP BY source_skill ORDER BY cnt DESC"""
    ).fetchall()

    by_type = conn.execute(
        """SELECT n.type, COUNT(*) as cnt, AVG(n.confidence) as avg_conf
           FROM learning_provenance lp JOIN nodes n ON n.id = lp.node_id
           GROUP BY n.type ORDER BY cnt DESC"""
    ).fetchall()

    confidence_dist = conn.execute(
        """SELECT
             SUM(CASE WHEN confidence >= 0.85 THEN 1 ELSE 0 END) as high,
             SUM(CASE WHEN confidence >= 0.5 AND confidence < 0.85 THEN 1 ELSE 0 END) as medium,
             SUM(CASE WHEN confidence < 0.5 THEN 1 ELSE 0 END) as low
           FROM nodes WHERE type IN ('Requirement', 'RiskItem', 'ArchDecision',
                                     'BusinessLogic', 'UseCase')"""
    ).fetchone()

    conn.close()
    return {
        "total_learned": total_learned,
        "by_skill": [dict(r) for r in by_skill],
        "by_type": [dict(r) for r in by_type],
        "confidence_distribution": dict(confidence_dist) if confidence_dist else {},
    }


# ---------------------------------------------------------------------------
# decay-report — find nodes needing revalidation
# ---------------------------------------------------------------------------

def decay_report(days: int = None) -> list:
    days = days or cfg.LEARNING_CONFIDENCE_DECAY_DAYS
    conn = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    rows = conn.execute(
        """SELECT id, type, name, confidence, updated_at,
                  json_extract(data, '$.confirmed_at') as confirmed_at
           FROM nodes
           WHERE type IN ('Requirement', 'RiskItem', 'ArchDecision', 'BusinessLogic', 'UseCase')
             AND confidence < 0.85
             AND updated_at < ?
           ORDER BY confidence ASC, updated_at ASC""",
        (cutoff,)
    ).fetchall()

    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_new_info(existing: dict, incoming: dict) -> bool:
    for k, v in incoming.items():
        if k not in existing or existing[k] != v:
            return True
    return False


def _is_multi_source(conn: sqlite3.Connection, node_type: str, node_name: str,
                     current_skill: str) -> bool:
    row = conn.execute(
        """SELECT COUNT(DISTINCT source_skill) as cnt
           FROM learning_provenance lp
           JOIN nodes n ON n.id = lp.node_id
           WHERE n.type = ? AND n.name = ? AND lp.source_skill != ?""",
        (node_type, node_name, current_skill)
    ).fetchone()
    return row and row["cnt"] > 0


def _create_edge(conn: sqlite3.Connection, node_type: str, node_name: str,
                 edge_def: dict):
    target_type = edge_def.get("target_type", "")
    target_name = edge_def.get("target_name", "")
    edge_type = edge_def.get("edge_type", "RELATES_TO")
    if not target_type or not target_name:
        return

    from_id = conn.execute(
        "SELECT id FROM nodes WHERE type = ? AND name = ?",
        (node_type, node_name)
    ).fetchone()
    to_id = conn.execute(
        "SELECT id FROM nodes WHERE type = ? AND name = ?",
        (target_type, target_name)
    ).fetchone()

    if not from_id or not to_id:
        return

    edge_data = json.dumps(edge_def.get("data", {}))
    conn.execute(
        """INSERT INTO edges (from_node_id, to_node_id, edge_type, data)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(from_node_id, to_node_id, edge_type) DO UPDATE SET data = excluded.data""",
        (from_id["id"], to_id["id"], edge_type, edge_data)
    )


def _create_cross_links(conn: sqlite3.Connection, rows):
    for row in rows:
        if row["status"] != "staged":
            continue
        try:
            results = conn.execute(
                """SELECT id, type, name FROM nodes
                   WHERE type = ? AND name != ? AND name LIKE ?""",
                (row["node_type"], row["node_name"], f"%{row['node_name'].split()[-1]}%")
            ).fetchall()
            for match in results[:3]:
                from_id = conn.execute(
                    "SELECT id FROM nodes WHERE type = ? AND name = ?",
                    (row["node_type"], row["node_name"])
                ).fetchone()
                if from_id:
                    conn.execute(
                        """INSERT OR IGNORE INTO edges (from_node_id, to_node_id, edge_type, data)
                           VALUES (?, ?, 'CROSS_REF', '{"auto": true}')""",
                        (from_id["id"], match["id"])
                    )
        except Exception:
            pass


def _update_fts(conn: sqlite3.Connection, node_id: int, name: str, data: dict):
    try:
        conn.execute(
            "INSERT INTO nodes_fts(nodes_fts, rowid, name, data) VALUES('delete', ?, ?, ?)",
            (node_id, name, json.dumps(data))
        )
        conn.execute(
            "INSERT INTO nodes_fts(rowid, name, data) VALUES(?, ?, ?)",
            (node_id, name, json.dumps(data))
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: rubick_learn.py {record|flush|status|history|stats|decay-report|recover}")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "record":
        p = argparse.ArgumentParser()
        p.add_argument("--interaction-type", required=True)
        p.add_argument("--source-skill", required=True)
        p.add_argument("--items", required=True, help="JSON array of items")
        p.add_argument("--project", default="_global")
        args = p.parse_args(sys.argv[2:])
        result = record(args.interaction_type, args.source_skill,
                       json.loads(args.items), args.project)
        print(json.dumps(result))

    elif cmd == "flush":
        p = argparse.ArgumentParser()
        p.add_argument("--interaction-id", default=None)
        p.add_argument("--dry-run", action="store_true")
        args = p.parse_args(sys.argv[2:])
        result = flush(args.interaction_id, args.dry_run)
        print(json.dumps(result))

    elif cmd == "status":
        print(json.dumps(status(), indent=2))

    elif cmd == "history":
        p = argparse.ArgumentParser()
        p.add_argument("--node-type", default=None)
        p.add_argument("--node-name", default=None)
        p.add_argument("--limit", type=int, default=20)
        args = p.parse_args(sys.argv[2:])
        print(json.dumps(history(args.node_type, args.node_name, args.limit), indent=2))

    elif cmd == "stats":
        print(json.dumps(stats(), indent=2))

    elif cmd == "decay-report":
        p = argparse.ArgumentParser()
        p.add_argument("--days", type=int, default=cfg.LEARNING_CONFIDENCE_DECAY_DAYS)
        args = p.parse_args(sys.argv[2:])
        print(json.dumps(decay_report(args.days), indent=2))

    elif cmd == "recover":
        # Flush orphaned staged items older than --minutes (default 10)
        p = argparse.ArgumentParser()
        p.add_argument("--minutes", type=int, default=10)
        p.add_argument("--dry-run", action="store_true")
        args = p.parse_args(sys.argv[2:])

        conn = get_db()
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=args.minutes)).strftime("%Y-%m-%d %H:%M:%S")
        orphans = conn.execute(
            "SELECT DISTINCT interaction_id, COUNT(*) as cnt FROM learning_ledger "
            "WHERE status = 'staged' AND created_at < ? GROUP BY interaction_id",
            (cutoff,)
        ).fetchall()

        total_flushed = 0
        for row in orphans:
            iid = row["interaction_id"]
            if args.dry_run:
                print(json.dumps({"dry_run": True, "interaction_id": iid, "items": row["cnt"]}))
            else:
                result = flush(interaction_id=iid)
                total_flushed += result.get("created", 0) + result.get("merged", 0)
                print(json.dumps({"recovered": iid, **result}))

        print(json.dumps({
            "orphaned_interactions": len(orphans),
            "total_flushed": total_flushed,
            "cutoff": cutoff,
            "dry_run": args.dry_run,
        }))

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Interaction Logging & Self-Improvement
# ---------------------------------------------------------------------------

def log_interaction(session_id: str, query: str, nodes_used: list,
                    experts_consulted: list, tokens_used: int = 0,
                    phase: str = "chat") -> int:
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO interaction_log
           (session_id, query, nodes_used, experts_consulted, tokens_used, phase)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (session_id, query[:500],
         json.dumps(nodes_used[:100]),
         json.dumps(experts_consulted[:20]),
         tokens_used, phase)
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def mark_interaction_quality(interaction_id: int, quality: str) -> bool:
    if quality not in ("useful", "partial", "irrelevant"):
        return False
    conn = get_db()
    conn.execute(
        "UPDATE interaction_log SET response_quality = ? WHERE id = ?",
        (quality, interaction_id)
    )
    conn.commit()
    conn.close()
    return True


def self_improve_report() -> dict:
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM interaction_log").fetchone()[0]
    by_quality = conn.execute(
        "SELECT response_quality, COUNT(*) c FROM interaction_log GROUP BY response_quality"
    ).fetchall()

    useful_nodes: dict[str, int] = {}
    useful_experts: dict[str, int] = {}
    rows = conn.execute(
        "SELECT nodes_used, experts_consulted FROM interaction_log WHERE response_quality = 'useful'"
    ).fetchall()
    for r in rows:
        for nid in json.loads(r["nodes_used"] or "[]"):
            useful_nodes[str(nid)] = useful_nodes.get(str(nid), 0) + 1
        for eid in json.loads(r["experts_consulted"] or "[]"):
            useful_experts[str(eid)] = useful_experts.get(str(eid), 0) + 1

    all_used_nodes: set[str] = set()
    all_rows = conn.execute("SELECT nodes_used FROM interaction_log").fetchall()
    for r in all_rows:
        for nid in json.loads(r["nodes_used"] or "[]"):
            all_used_nodes.add(str(nid))

    never_useful = all_used_nodes - set(useful_nodes.keys())

    conn.close()
    return {
        "total_interactions": total,
        "quality_distribution": {r["response_quality"]: r["c"] for r in by_quality},
        "top_useful_nodes": sorted(useful_nodes.items(), key=lambda x: -x[1])[:20],
        "top_useful_experts": sorted(useful_experts.items(), key=lambda x: -x[1])[:10],
        "never_useful_count": len(never_useful),
    }


def apply_self_improvement() -> dict:
    conn = get_db()
    report = self_improve_report()

    boosted = 0
    for nid_str, count in report["top_useful_nodes"]:
        if count >= 3:
            conn.execute(
                "UPDATE nodes SET confidence = MIN(confidence + 0.05, 1.0) WHERE id = ?",
                (int(nid_str),)
            )
            boosted += 1

    decayed = 0
    rows = conn.execute("SELECT nodes_used FROM interaction_log WHERE response_quality = 'irrelevant'").fetchall()
    irrelevant_nodes: dict[str, int] = {}
    for r in rows:
        for nid in json.loads(r["nodes_used"] or "[]"):
            irrelevant_nodes[str(nid)] = irrelevant_nodes.get(str(nid), 0) + 1
    for nid_str, count in irrelevant_nodes.items():
        if count >= 3:
            conn.execute(
                "UPDATE nodes SET confidence = MAX(confidence - 0.03, 0.1) WHERE id = ?",
                (int(nid_str),)
            )
            decayed += 1

    conn.commit()
    conn.close()
    return {"boosted": boosted, "decayed": decayed, "report": report}


if __name__ == "__main__":
    main()
