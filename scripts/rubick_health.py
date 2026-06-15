#!/usr/bin/env python3
"""Rubick Health Engine — validation, repair, auto-linking, pruning.

Diagnoses and fixes graph quality issues:
- Ghost nodes (orphaned Project nodes from bulk GitHub imports)
- Orphaned architecture nodes (ArchDecisions, Requirements, etc. without edges)
- Duplicate nodes (same concept, different names)
- Dangling edges (pointing to deleted nodes)
- Missing edges (references in data JSON not materialized as edges)
- Confidence decay for stale unvalidated nodes

Usage:
    rubick_health.py diagnose [db_path]           Full health report
    rubick_health.py repair [db_path] [--dry-run]  Fix all detected issues
    rubick_health.py prune-ghosts [db_path]        Remove orphan github Project nodes
    rubick_health.py auto-link [db_path]           Create missing edges from data refs
    rubick_health.py dedup [db_path]               Merge duplicate nodes
    rubick_health.py integrity [db_path]           Check referential integrity
    rubick_health.py reindex-fts [db_path]         Rebuild FTS5 index
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import os
import argparse
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import brain_config as cfg
    from rubick_graph import get_db, upsert_node, upsert_edge, VALID_NODE_TYPES, VALID_EDGE_TYPES
except ImportError as e:
    print(f"Import error: {e}", file=sys.stderr)
    sys.exit(1)

DB_PATH = str(cfg.RUBICK_DB_PATH) if cfg else "workspace/rubick.db"

SEED_PROJECT_NAMES = {
    "omni", "emandate-service", "offers-engine", "rpc", "payments-mandate",
    "api", "pg-router", "checkout-service", "batch", "mock-gateway",
}

ARCH_TYPES = {"ArchDecision", "Requirement", "RiskItem", "BusinessLogic", "UseCase"}
FEATURE_LINK_EDGES = {
    "ArchDecision": "DECIDED_BY",
    "Requirement": "HAS_REQUIREMENT",
    "RiskItem": "HAS_RISK",
    "BusinessLogic": "ENCODES",
    "UseCase": "HAS_USE_CASE",
}


# ============================================================================
# Diagnose — comprehensive health report
# ============================================================================

def diagnose(db_path: str = DB_PATH) -> dict:
    """Run all health checks and return a structured report."""
    conn = get_db(db_path)
    report: dict = {"timestamp": datetime.now(timezone.utc).isoformat(), "issues": [], "stats": {}}

    total_nodes = conn.execute("SELECT COUNT(*) as c FROM nodes").fetchone()["c"]
    total_edges = conn.execute("SELECT COUNT(*) as c FROM edges").fetchone()["c"]
    report["stats"]["total_nodes"] = total_nodes
    report["stats"]["total_edges"] = total_edges
    report["stats"]["edge_density"] = round(total_edges / max(total_nodes, 1), 3)

    # 1. Ghost Project nodes
    ghosts = _find_ghost_projects(conn)
    if ghosts:
        report["issues"].append({
            "type": "ghost_projects",
            "severity": "critical",
            "count": len(ghosts),
            "description": f"{len(ghosts)} Project nodes from GitHub with zero edges — dead weight polluting the graph",
            "fix": "prune-ghosts",
        })

    # 2. Orphaned architecture nodes
    orphaned_arch = _find_orphaned_arch_nodes(conn)
    if orphaned_arch:
        report["issues"].append({
            "type": "orphaned_arch_nodes",
            "severity": "critical",
            "count": len(orphaned_arch),
            "description": f"{len(orphaned_arch)} architecture nodes (ArchDecision/Requirement/RiskItem/etc.) with zero edges — BFS can never reach them",
            "fix": "auto-link",
            "details": [{"type": n["type"], "name": n["name"]} for n in orphaned_arch],
        })

    # 3. Duplicate nodes
    dupes = _find_duplicates(conn)
    if dupes:
        report["issues"].append({
            "type": "duplicate_nodes",
            "severity": "medium",
            "count": len(dupes),
            "description": f"{len(dupes)} duplicate node groups detected",
            "fix": "dedup",
            "details": dupes,
        })

    # 4. Dangling edges
    dangling = _find_dangling_edges(conn)
    if dangling:
        report["issues"].append({
            "type": "dangling_edges",
            "severity": "high",
            "count": len(dangling),
            "description": f"{len(dangling)} edges pointing to non-existent nodes",
            "fix": "integrity",
        })

    # 5. Orphan non-arch nodes
    all_orphans = _find_all_orphans(conn)
    non_arch_orphans = [o for o in all_orphans if o["type"] not in ARCH_TYPES and o["type"] != "Project"]
    if non_arch_orphans:
        by_type = defaultdict(int)
        for o in non_arch_orphans:
            by_type[o["type"]] += 1
        report["issues"].append({
            "type": "orphaned_nodes",
            "severity": "low",
            "count": len(non_arch_orphans),
            "description": f"{len(non_arch_orphans)} non-architecture nodes with zero edges",
            "by_type": dict(by_type),
        })

    # 6. Unlinked Signals (signals without feature/project edges)
    unlinked_signals = _find_unlinked_signals(conn)
    if unlinked_signals:
        report["issues"].append({
            "type": "unlinked_signals",
            "severity": "medium",
            "count": len(unlinked_signals),
            "description": f"{len(unlinked_signals)} Signal nodes not linked to any feature or project",
            "fix": "auto-link",
        })

    # 7. FTS sync check
    fts_synced = _check_fts_sync(conn)
    if not fts_synced["ok"]:
        report["issues"].append({
            "type": "fts_desync",
            "severity": "high",
            "count": fts_synced["difference"],
            "description": f"FTS5 index has {fts_synced['fts_count']} rows but nodes table has {fts_synced['node_count']}",
            "fix": "reindex-fts",
        })

    # 8. Confidence distribution
    conf_dist = _confidence_distribution(conn)
    report["stats"]["confidence"] = conf_dist
    stale_unvalidated = conf_dist.get("extracted", 0)
    if stale_unvalidated > total_nodes * 0.3:
        report["issues"].append({
            "type": "high_unvalidated_ratio",
            "severity": "low",
            "count": stale_unvalidated,
            "description": f"{stale_unvalidated} nodes at extraction confidence (0.7) — consider running /arch validate",
        })

    # Score
    critical = sum(1 for i in report["issues"] if i["severity"] == "critical")
    high = sum(1 for i in report["issues"] if i["severity"] == "high")
    medium = sum(1 for i in report["issues"] if i["severity"] == "medium")

    if critical > 0:
        report["health_score"] = max(0, 30 - critical * 15)
        report["health_grade"] = "F"
    elif high > 0:
        report["health_score"] = max(30, 60 - high * 10)
        report["health_grade"] = "D"
    elif medium > 0:
        report["health_score"] = max(60, 80 - medium * 5)
        report["health_grade"] = "C"
    else:
        report["health_score"] = 95
        report["health_grade"] = "A"

    conn.close()
    return report


# ============================================================================
# Repair — fix all detected issues
# ============================================================================

def repair(db_path: str = DB_PATH, dry_run: bool = False) -> dict:
    """Run all repairs in sequence. Returns summary of actions taken."""
    results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "actions": [],
    }

    r = prune_ghosts(db_path, dry_run=dry_run)
    results["actions"].append({"action": "prune_ghosts", **r})

    r = auto_link(db_path, dry_run=dry_run)
    results["actions"].append({"action": "auto_link", **r})

    r = dedup(db_path, dry_run=dry_run)
    results["actions"].append({"action": "dedup", **r})

    r = fix_integrity(db_path, dry_run=dry_run)
    results["actions"].append({"action": "integrity", **r})

    r = reindex_fts(db_path, dry_run=dry_run)
    results["actions"].append({"action": "reindex_fts", **r})

    total_fixed = sum(a.get("fixed", 0) + a.get("pruned", 0) + a.get("linked", 0) + a.get("merged", 0) for a in results["actions"])
    results["total_fixed"] = total_fixed

    return results


# ============================================================================
# Prune ghost Project nodes
# ============================================================================

def prune_ghosts(db_path: str = DB_PATH, dry_run: bool = False) -> dict:
    """Remove Project nodes from github source that have zero edges."""
    conn = get_db(db_path)
    ghosts = _find_ghost_projects(conn)

    if not ghosts:
        conn.close()
        return {"pruned": 0, "message": "No ghost projects found"}

    if not dry_run:
        ghost_ids = [g["id"] for g in ghosts]
        batch_size = 500
        for i in range(0, len(ghost_ids), batch_size):
            batch = ghost_ids[i:i + batch_size]
            placeholders = ",".join("?" * len(batch))
            conn.execute(f"DELETE FROM nodes WHERE id IN ({placeholders})", batch)
        conn.commit()

    conn.close()
    return {"pruned": len(ghosts), "dry_run": dry_run}


# ============================================================================
# Auto-link orphaned nodes
# ============================================================================

def auto_link(db_path: str = DB_PATH, dry_run: bool = False) -> dict:
    """Create missing edges by parsing data JSON fields for references."""
    conn = get_db(db_path)
    linked = 0
    details = []

    # 1. Link architecture nodes to their features
    linked += _link_arch_to_features(conn, dry_run, details)

    # 2. Link Signals to features/projects they mention
    linked += _link_signals_to_targets(conn, dry_run, details)

    # 3. Link Emails/Meetings to projects they discuss
    linked += _link_comms_to_projects(conn, dry_run, details)

    if not dry_run:
        conn.commit()
    conn.close()
    return {"linked": linked, "dry_run": dry_run, "details": details}


def _link_arch_to_features(conn: sqlite3.Connection, dry_run: bool, details: list) -> int:
    """Link ArchDecision/Requirement/RiskItem/BusinessLogic/UseCase to Features."""
    linked = 0
    features = conn.execute("SELECT id, name FROM nodes WHERE type = 'Feature'").fetchall()
    feature_map = {f["name"].lower(): f for f in features}
    feature_names = list(feature_map.keys())

    for arch_type in ARCH_TYPES:
        rows = conn.execute(
            "SELECT id, name, data FROM nodes WHERE type = ?", (arch_type,)
        ).fetchall()

        for row in rows:
            has_edges = conn.execute(
                "SELECT COUNT(*) as c FROM edges WHERE from_node_id = ? OR to_node_id = ?",
                (row["id"], row["id"])
            ).fetchone()["c"]

            data = _safe_json(row["data"])
            feature_ref = (
                data.get("feature") or data.get("feature_slug") or
                data.get("feature_name") or data.get("source_feature") or ""
            ).lower()

            matched_feature = None
            if feature_ref:
                for fname in feature_names:
                    if fname in feature_ref or feature_ref in fname:
                        matched_feature = feature_map[fname]
                        break

            if not matched_feature and feature_ref:
                for fname in feature_names:
                    if _fuzzy_match(feature_ref, fname):
                        matched_feature = feature_map[fname]
                        break

            if matched_feature and has_edges == 0:
                edge_type = FEATURE_LINK_EDGES.get(arch_type, "RELATES_TO")
                if not dry_run:
                    try:
                        conn.execute(
                            """INSERT INTO edges (from_node_id, to_node_id, edge_type, data)
                               VALUES (?, ?, ?, '{}')
                               ON CONFLICT(from_node_id, to_node_id, edge_type) DO NOTHING""",
                            (matched_feature["id"], row["id"], edge_type)
                        )
                        linked += 1
                    except sqlite3.IntegrityError:
                        pass
                else:
                    linked += 1
                details.append({
                    "from": f"Feature:{matched_feature['name']}",
                    "to": f"{arch_type}:{row['name']}",
                    "edge": edge_type,
                })

            # Also link to Document if source_doc is set
            source_doc = data.get("source_doc", "")
            if source_doc and not dry_run:
                doc_row = conn.execute(
                    "SELECT id FROM nodes WHERE type = 'Document' AND name = ?",
                    (source_doc,)
                ).fetchone()
                if doc_row:
                    try:
                        conn.execute(
                            """INSERT INTO edges (from_node_id, to_node_id, edge_type, data)
                               VALUES (?, ?, 'EXTRACTED_FROM', '{}')
                               ON CONFLICT DO NOTHING""",
                            (row["id"], doc_row["id"])
                        )
                        linked += 1
                        details.append({
                            "from": f"{arch_type}:{row['name']}",
                            "to": f"Document:{source_doc}",
                            "edge": "EXTRACTED_FROM",
                        })
                    except sqlite3.IntegrityError:
                        pass

    return linked


def _link_signals_to_targets(conn: sqlite3.Connection, dry_run: bool, details: list) -> int:
    """Link Signal nodes to features/projects they reference in data."""
    linked = 0
    signals = conn.execute(
        """SELECT s.id, s.name, s.data FROM nodes s
           WHERE s.type = 'Signal'
           AND s.id NOT IN (
               SELECT from_node_id FROM edges WHERE edge_type IN ('SIGNAL_FOR', 'RELATES_TO', 'IMPLEMENTS_FEATURE')
           )"""
    ).fetchall()

    features = conn.execute("SELECT id, name FROM nodes WHERE type = 'Feature'").fetchall()
    feature_map = {f["name"].lower(): f for f in features}

    for sig in signals:
        data = _safe_json(sig["data"])
        text = f"{sig['name']} {data.get('content_summary', '')} {data.get('title', '')}".lower()

        for fname, fnode in feature_map.items():
            if fname in text or _slug_match(fname, text):
                if not dry_run:
                    try:
                        conn.execute(
                            """INSERT INTO edges (from_node_id, to_node_id, edge_type, data)
                               VALUES (?, ?, 'SIGNAL_FOR', '{"auto_linked": true}')
                               ON CONFLICT DO NOTHING""",
                            (sig["id"], fnode["id"])
                        )
                        linked += 1
                    except sqlite3.IntegrityError:
                        pass
                else:
                    linked += 1
                details.append({
                    "from": f"Signal:{sig['name'][:50]}",
                    "to": f"Feature:{fnode['name']}",
                    "edge": "SIGNAL_FOR",
                })
                break

    return linked


def _link_comms_to_projects(conn: sqlite3.Connection, dry_run: bool, details: list) -> int:
    """Link orphaned Email/Meeting nodes to projects they mention."""
    linked = 0
    seed_projects = conn.execute(
        "SELECT id, name FROM nodes WHERE type = 'Project' AND source_type = 'seed'"
    ).fetchall()

    for comm_type in ("Email", "Meeting"):
        orphans = conn.execute(
            f"""SELECT n.id, n.name, n.data FROM nodes n
                WHERE n.type = ?
                AND n.id NOT IN (SELECT from_node_id FROM edges)
                AND n.id NOT IN (SELECT to_node_id FROM edges)""",
            (comm_type,)
        ).fetchall()

        for node in orphans:
            data = _safe_json(node["data"])
            text = f"{node['name']} {data.get('subject', '')} {data.get('title', '')} {data.get('content_summary', '')}".lower()

            for proj in seed_projects:
                if proj["name"].lower() in text:
                    if not dry_run:
                        try:
                            conn.execute(
                                """INSERT INTO edges (from_node_id, to_node_id, edge_type, data)
                                   VALUES (?, ?, 'DISCUSSED_IN', '{"auto_linked": true}')
                                   ON CONFLICT DO NOTHING""",
                                (node["id"], proj["id"])
                            )
                            linked += 1
                        except sqlite3.IntegrityError:
                            pass
                    else:
                        linked += 1
                    details.append({
                        "from": f"{comm_type}:{node['name'][:50]}",
                        "to": f"Project:{proj['name']}",
                        "edge": "DISCUSSED_IN",
                    })
                    break

    return linked


# ============================================================================
# Deduplication
# ============================================================================

def dedup(db_path: str = DB_PATH, dry_run: bool = False) -> dict:
    """Merge duplicate nodes (same type, similar names)."""
    conn = get_db(db_path)
    dupes = _find_duplicates(conn)
    merged = 0

    for group in dupes:
        if dry_run:
            merged += len(group["duplicates"])
            continue

        keep_id = group["keep_id"]
        keep_name = group["keep_name"]

        for dup in group["duplicates"]:
            dup_id = dup["id"]
            conn.execute(
                "UPDATE edges SET from_node_id = ? WHERE from_node_id = ?",
                (keep_id, dup_id)
            )
            conn.execute(
                "UPDATE edges SET to_node_id = ? WHERE to_node_id = ?",
                (keep_id, dup_id)
            )
            # Remove duplicate edges created by re-pointing
            conn.execute("""
                DELETE FROM edges WHERE id NOT IN (
                    SELECT MIN(id) FROM edges GROUP BY from_node_id, to_node_id, edge_type
                )
            """)
            conn.execute("DELETE FROM nodes WHERE id = ?", (dup_id,))
            merged += 1

    if not dry_run:
        conn.commit()
    conn.close()
    return {"merged": merged, "groups": len(dupes), "dry_run": dry_run}


# ============================================================================
# Integrity checks
# ============================================================================

def fix_integrity(db_path: str = DB_PATH, dry_run: bool = False) -> dict:
    """Fix dangling edges and invalid references."""
    conn = get_db(db_path)
    fixed = 0

    # Remove edges pointing to non-existent nodes
    dangling = conn.execute("""
        SELECT e.id FROM edges e
        LEFT JOIN nodes n1 ON e.from_node_id = n1.id
        LEFT JOIN nodes n2 ON e.to_node_id = n2.id
        WHERE n1.id IS NULL OR n2.id IS NULL
    """).fetchall()

    if dangling and not dry_run:
        ids = [d["id"] for d in dangling]
        batch_size = 500
        for i in range(0, len(ids), batch_size):
            batch = ids[i:i + batch_size]
            placeholders = ",".join("?" * len(batch))
            conn.execute(f"DELETE FROM edges WHERE id IN ({placeholders})", batch)
        fixed += len(dangling)

    # Remove self-referential edges
    self_refs = conn.execute(
        "SELECT id FROM edges WHERE from_node_id = to_node_id"
    ).fetchall()
    if self_refs and not dry_run:
        for sr in self_refs:
            conn.execute("DELETE FROM edges WHERE id = ?", (sr["id"],))
        fixed += len(self_refs)

    if not dry_run:
        conn.commit()
    conn.close()
    return {
        "fixed": fixed,
        "dangling_removed": len(dangling),
        "self_refs_removed": len(self_refs),
        "dry_run": dry_run,
    }


# ============================================================================
# FTS reindex
# ============================================================================

def reindex_fts(db_path: str = DB_PATH, dry_run: bool = False) -> dict:
    """Rebuild FTS5 index from scratch."""
    conn = get_db(db_path)
    node_count = conn.execute("SELECT COUNT(*) as c FROM nodes").fetchone()["c"]

    if dry_run:
        fts_count = conn.execute("SELECT COUNT(*) as c FROM nodes_fts").fetchone()["c"]
        conn.close()
        return {"reindexed": 0, "node_count": node_count, "fts_count": fts_count, "dry_run": True}

    conn.execute("DELETE FROM nodes_fts")
    conn.execute("INSERT INTO nodes_fts(rowid, name, data) SELECT id, name, data FROM nodes")
    conn.commit()

    fts_count = conn.execute("SELECT COUNT(*) as c FROM nodes_fts").fetchone()["c"]
    conn.close()
    return {"reindexed": fts_count, "node_count": node_count, "dry_run": False}


# ============================================================================
# Add missing indexes for performance
# ============================================================================

def add_indexes(db_path: str = DB_PATH) -> dict:
    """Add performance indexes that don't exist yet."""
    conn = get_db(db_path)
    added = []

    indexes = [
        ("idx_nodes_confidence", "CREATE INDEX IF NOT EXISTS idx_nodes_confidence ON nodes(confidence)"),
        ("idx_nodes_type_confidence", "CREATE INDEX IF NOT EXISTS idx_nodes_type_confidence ON nodes(type, confidence DESC)"),
        ("idx_edges_from_type", "CREATE INDEX IF NOT EXISTS idx_edges_from_type ON edges(from_node_id, edge_type)"),
        ("idx_edges_to_type", "CREATE INDEX IF NOT EXISTS idx_edges_to_type ON edges(to_node_id, edge_type)"),
        ("idx_nodes_source_type_name", "CREATE INDEX IF NOT EXISTS idx_nodes_source_type_name ON nodes(source_type)"),
    ]

    for name, sql in indexes:
        try:
            conn.execute(sql)
            added.append(name)
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()
    return {"added": added}


# ============================================================================
# Internal helpers
# ============================================================================

def _find_ghost_projects(conn: sqlite3.Connection) -> list:
    """Find Project nodes from github source with zero edges."""
    rows = conn.execute("""
        SELECT n.id, n.name FROM nodes n
        WHERE n.type = 'Project' AND n.source_type = 'github'
        AND n.id NOT IN (SELECT from_node_id FROM edges)
        AND n.id NOT IN (SELECT to_node_id FROM edges)
    """).fetchall()
    return [dict(r) for r in rows]


def _find_orphaned_arch_nodes(conn: sqlite3.Connection) -> list:
    """Find architecture nodes with zero edges."""
    placeholders = ",".join("?" * len(ARCH_TYPES))
    rows = conn.execute(f"""
        SELECT n.id, n.type, n.name, n.data FROM nodes n
        WHERE n.type IN ({placeholders})
        AND n.id NOT IN (SELECT from_node_id FROM edges)
        AND n.id NOT IN (SELECT to_node_id FROM edges)
    """, list(ARCH_TYPES)).fetchall()
    return [dict(r) for r in rows]


def _find_all_orphans(conn: sqlite3.Connection) -> list:
    """Find all nodes with zero edges."""
    rows = conn.execute("""
        SELECT n.id, n.type, n.name FROM nodes n
        WHERE n.id NOT IN (SELECT from_node_id FROM edges)
        AND n.id NOT IN (SELECT to_node_id FROM edges)
        AND n.type NOT IN ('Project', 'SlackChannel')
    """).fetchall()
    return [dict(r) for r in rows]


def _find_duplicates(conn: sqlite3.Connection) -> list:
    """Find duplicate nodes by normalized name similarity within same type."""
    groups = []

    for ntype in ("Decision", "ArchDecision", "Requirement", "Feature"):
        rows = conn.execute(
            "SELECT id, name, data, confidence FROM nodes WHERE type = ? ORDER BY confidence DESC, id ASC",
            (ntype,)
        ).fetchall()

        seen = []
        for row in rows:
            norm = _normalize_name(row["name"])
            matched = False
            for group in seen:
                if _similar_names(norm, group["norm"]):
                    group["duplicates"].append(dict(row))
                    matched = True
                    break
            if not matched:
                seen.append({
                    "norm": norm,
                    "keep_id": row["id"],
                    "keep_name": row["name"],
                    "keep_type": ntype,
                    "duplicates": [],
                })

        for group in seen:
            if group["duplicates"]:
                groups.append(group)

    return groups


def _find_dangling_edges(conn: sqlite3.Connection) -> list:
    """Find edges pointing to non-existent nodes."""
    rows = conn.execute("""
        SELECT e.id, e.from_node_id, e.to_node_id, e.edge_type FROM edges e
        LEFT JOIN nodes n1 ON e.from_node_id = n1.id
        LEFT JOIN nodes n2 ON e.to_node_id = n2.id
        WHERE n1.id IS NULL OR n2.id IS NULL
    """).fetchall()
    return [dict(r) for r in rows]


def _find_unlinked_signals(conn: sqlite3.Connection) -> list:
    """Find signals not linked to any feature or project."""
    rows = conn.execute("""
        SELECT n.id, n.name FROM nodes n
        WHERE n.type = 'Signal'
        AND n.id NOT IN (
            SELECT from_node_id FROM edges
            WHERE edge_type IN ('SIGNAL_FOR', 'RELATES_TO', 'IMPLEMENTS_FEATURE', 'MENTIONED_IN')
        )
        AND n.id NOT IN (
            SELECT to_node_id FROM edges
            WHERE edge_type IN ('SIGNAL_FOR', 'RELATES_TO', 'IMPLEMENTS_FEATURE', 'MENTIONED_IN')
        )
    """).fetchall()
    return [dict(r) for r in rows]


def _check_fts_sync(conn: sqlite3.Connection) -> dict:
    """Check if FTS5 index is in sync with nodes table."""
    try:
        fts_count = conn.execute("SELECT COUNT(*) as c FROM nodes_fts").fetchone()["c"]
    except sqlite3.OperationalError:
        return {"ok": False, "fts_count": 0, "node_count": 0, "difference": -1}
    node_count = conn.execute("SELECT COUNT(*) as c FROM nodes").fetchone()["c"]
    diff = abs(fts_count - node_count)
    return {"ok": diff == 0, "fts_count": fts_count, "node_count": node_count, "difference": diff}


def _confidence_distribution(conn: sqlite3.Connection) -> dict:
    """Get confidence level distribution."""
    rows = conn.execute("""
        SELECT
            CASE
                WHEN confidence >= 1.0 THEN 'confirmed'
                WHEN confidence >= 0.85 THEN 'reviewed'
                WHEN confidence >= 0.7 THEN 'extracted'
                WHEN confidence >= 0.5 THEN 'disputed'
                ELSE 'rejected'
            END as level,
            COUNT(*) as cnt
        FROM nodes GROUP BY level
    """).fetchall()
    return {r["level"]: r["cnt"] for r in rows}


def _safe_json(data: str) -> dict:
    try:
        return json.loads(data or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _normalize_name(name: str) -> str:
    """Normalize a node name for dedup comparison."""
    name = name.lower().strip()
    name = re.sub(r'\[memory\]\s*', '', name)
    name = re.sub(r'deadline:\s*', '', name)
    name = re.sub(r'[^a-z0-9\s]', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def _similar_names(a: str, b: str) -> bool:
    """Check if two normalized names are similar enough to be duplicates."""
    if a == b:
        return True
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a or not words_b:
        return False
    overlap = words_a & words_b
    smaller = min(len(words_a), len(words_b))
    return len(overlap) / smaller >= 0.7


def _fuzzy_match(ref: str, name: str) -> bool:
    """Fuzzy match a feature reference to a feature name."""
    ref_words = set(re.sub(r'[^a-z0-9]', ' ', ref.lower()).split())
    name_words = set(re.sub(r'[^a-z0-9]', ' ', name.lower()).split())
    if not ref_words or not name_words:
        return False
    overlap = ref_words & name_words
    return len(overlap) >= 2 and len(overlap) / len(ref_words) >= 0.5


def _slug_match(slug: str, text: str) -> bool:
    """Check if a slug (e.g. 'dfb-instant-discount') appears in text."""
    slug_words = slug.replace("-", " ").replace("_", " ").split()
    if len(slug_words) < 2:
        return False
    return all(w in text for w in slug_words)


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Rubick Health Engine")
    parser.add_argument("command", choices=[
        "diagnose", "repair", "prune-ghosts", "auto-link",
        "dedup", "integrity", "reindex-fts", "add-indexes",
    ])
    parser.add_argument("db_path", nargs="?", default=DB_PATH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.command == "diagnose":
        result = diagnose(args.db_path)
    elif args.command == "repair":
        result = repair(args.db_path, dry_run=args.dry_run)
    elif args.command == "prune-ghosts":
        result = prune_ghosts(args.db_path, dry_run=args.dry_run)
    elif args.command == "auto-link":
        result = auto_link(args.db_path, dry_run=args.dry_run)
    elif args.command == "dedup":
        result = dedup(args.db_path, dry_run=args.dry_run)
    elif args.command == "integrity":
        result = fix_integrity(args.db_path, dry_run=args.dry_run)
    elif args.command == "reindex-fts":
        result = reindex_fts(args.db_path, dry_run=args.dry_run)
    elif args.command == "add-indexes":
        result = add_indexes(args.db_path)
    else:
        result = {"error": f"unknown command: {args.command}"}

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
