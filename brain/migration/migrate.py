"""Migrate rubick.db → brain.db.

Reads old rubick.db (read-only), writes to new brain.db via BrainAPI.
rubick.db is never modified — it stays as backup.

rubick.db schema: nodes(id, type, name, data JSON, source_type, source_id,
                        ingested_at, confidence, created_at, updated_at)
                  edges(id, from_node_id, to_node_id, edge_type, data, created_at)
                  code_bodies(id, node_id, project_slug, file_path, start_line,
                              end_line, language, body, body_hash, byte_length)

Phases:
1. Services (from Project nodes)
2. Code nodes (Function, Class, Test → typed tables)
3. Other code nodes (Module, Endpoint, DataStore → typed tables)
4. Workflow nodes (Feature, Signal, Expert, etc.) → generic nodes table
5. Edges → resolve node IDs via join, insert as (type, name) pairs
6. Code bodies → code_bodies table
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict

from brain.api import BrainAPI


def migrate_rubick(rubick_path: str, brain: BrainAPI) -> Dict[str, Any]:
    start = time.monotonic()
    report = {"source": rubick_path, "phases": {}, "errors": []}

    try:
        old = sqlite3.connect(f"file:{rubick_path}?mode=ro", uri=True)
        old.row_factory = sqlite3.Row
    except Exception as e:
        report["errors"].append(f"Cannot open rubick.db: {e}")
        return report

    eng = brain.engine

    # Build node ID → (type, name) lookup
    print("Building node ID index...")
    id_map: Dict[int, tuple] = {}
    for row in old.execute("SELECT id, type, name FROM nodes"):
        id_map[row["id"]] = (row["type"], row["name"])
    print(f"  {len(id_map):,} nodes indexed")

    # Phase 1: Services
    print("Phase 1: Services...")
    p1 = 0
    try:
        for row in old.execute("SELECT * FROM nodes WHERE type='Project'"):
            d = json.loads(row["data"] or "{}")
            eng.upsert_service(
                row["name"], role=d.get("role", ""), language=d.get("language", ""),
                url=d.get("url", ""), description=d.get("description", ""))
            p1 += 1
    except Exception as e:
        report["errors"].append(f"Phase 1 (services): {e}")
    report["phases"]["1_services"] = p1
    print(f"  {p1} services")

    # Phase 2: Core code nodes (Function, Class, Test)
    print("Phase 2: Core code nodes...")
    p2 = 0
    for ntype in ("Function", "Class", "Test"):
        try:
            batch = []
            cursor = old.execute("SELECT * FROM nodes WHERE type=?", (ntype,))
            while True:
                rows = cursor.fetchmany(1000)
                if not rows:
                    break
                for row in rows:
                    d = json.loads(row["data"] or "{}")
                    project = d.get("project") or d.get("project_slug") or ""
                    if ntype == "Function":
                        params = d.get("params")
                        if isinstance(params, (list, dict)):
                            params = json.dumps(params)
                        returns = d.get("returns")
                        if isinstance(returns, (list, dict)):
                            returns = json.dumps(returns)
                        batch.append({
                            "qname": row["name"],
                            "name": d.get("short_name", row["name"].rsplit(".", 1)[-1]),
                            "file_path": d.get("file_path") or d.get("file"),
                            "line_start": d.get("line_start") or d.get("line"),
                            "line_end": d.get("line_end"),
                            "language": d.get("language"),
                            "signature": d.get("signature"),
                            "receiver": d.get("receiver") or "",
                            "params": params or "",
                            "returns": returns or "",
                            "complexity": d.get("complexity", 0),
                            "is_exported": d.get("is_exported", False),
                            "is_test": d.get("is_test", False),
                            "project": project,
                            "body_hash": d.get("body_hash"),
                        })
                    elif ntype == "Class":
                        batch.append({
                            "qname": row["name"],
                            "name": d.get("short_name", row["name"].rsplit(".", 1)[-1]),
                            "file_path": d.get("file_path"),
                            "line_start": d.get("line_start"),
                            "line_end": d.get("line_end"),
                            "language": d.get("language"),
                            "kind": d.get("kind", "class"),
                            "is_exported": d.get("is_exported", False),
                            "project": project,
                        })
                    elif ntype == "Test":
                        batch.append({
                            "qname": row["name"],
                            "name": d.get("short_name", row["name"].rsplit(".", 1)[-1]),
                            "file_path": d.get("file_path"),
                            "line_start": d.get("line_start"),
                            "line_end": d.get("line_end"),
                            "kind": d.get("kind", "unit"),
                            "project": project,
                        })

                    if len(batch) >= 500:
                        _flush_code_batch(eng, ntype, batch)
                        p2 += len(batch)
                        batch = []

            if batch:
                _flush_code_batch(eng, ntype, batch)
                p2 += len(batch)

            print(f"  {ntype}: done")
        except Exception as e:
            report["errors"].append(f"Phase 2 ({ntype}): {e}")
            print(f"  {ntype}: ERROR — {e}")

    report["phases"]["2_code_nodes"] = p2

    # Phase 3: Other code nodes (Module, Endpoint, DataStore → generic nodes)
    print("Phase 3: Other code nodes...")
    p3 = 0
    for ntype in ("Module", "Endpoint", "DataStore"):
        try:
            cursor = old.execute("SELECT * FROM nodes WHERE type=?", (ntype,))
            while True:
                rows = cursor.fetchmany(1000)
                if not rows:
                    break
                for row in rows:
                    d = json.loads(row["data"] or "{}")
                    eng.upsert_node(
                        ntype, row["name"], data=d,
                        project_slug=d.get("project") or d.get("project_slug"),
                        source_type=row["source_type"],
                        source_id=row["source_id"],
                        confidence=row["confidence"] or 0.7)
                    p3 += 1
        except Exception as e:
            report["errors"].append(f"Phase 3 ({ntype}): {e}")
            print(f"  {ntype}: ERROR — {e}")
    report["phases"]["3_other_code"] = p3
    print(f"  {p3} nodes (Module, Endpoint, DataStore)")

    # Phase 4: Workflow nodes → generic table
    print("Phase 4: Workflow nodes...")
    p4 = 0
    workflow_types = [
        "Feature", "Requirement", "RiskItem", "ArchDecision", "UseCase",
        "BusinessLogic", "Signal", "ProjectExpert", "Document", "Task",
        "Person", "Email", "Commit", "Meeting", "Plan", "Branch", "PR",
        "WebPage", "JiraIssue", "ReviewResult", "SlackChannel",
    ]
    for ntype in workflow_types:
        try:
            rows = old.execute("SELECT * FROM nodes WHERE type=?", (ntype,)).fetchall()
            for row in rows:
                d = json.loads(row["data"] or "{}")
                eng.upsert_node(
                    ntype, row["name"], data=d,
                    project_slug=d.get("project") or d.get("project_slug"),
                    source_type=row["source_type"],
                    source_id=row["source_id"],
                    confidence=row["confidence"] or 0.7,
                    retention_days=-1)
                p4 += 1
        except Exception as e:
            report["errors"].append(f"Phase 4 ({ntype}): {e}")
    report["phases"]["4_workflow"] = p4
    print(f"  {p4} workflow nodes")

    # Phase 5: Edges (resolve IDs via id_map)
    print("Phase 5: Edges...")
    p5 = 0
    edge_errors = 0
    try:
        batch = []
        cursor = old.execute("SELECT * FROM edges")
        while True:
            rows = cursor.fetchmany(2000)
            if not rows:
                break
            for row in rows:
                from_id = row["from_node_id"]
                to_id = row["to_node_id"]
                if from_id not in id_map or to_id not in id_map:
                    edge_errors += 1
                    continue
                ft, fn = id_map[from_id]
                tt, tn = id_map[to_id]
                batch.append({
                    "from_type": ft, "from_name": fn,
                    "to_type": tt, "to_name": tn,
                    "edge_type": row["edge_type"],
                    "data": json.loads(row["data"] or "{}") if row["data"] else {},
                })
                if len(batch) >= 500:
                    eng.add_edges_batch(batch)
                    p5 += len(batch)
                    batch = []

        if batch:
            eng.add_edges_batch(batch)
            p5 += len(batch)
    except Exception as e:
        report["errors"].append(f"Phase 5 (edges): {e}")
        print(f"  Edges: ERROR — {e}")
    report["phases"]["5_edges"] = p5
    if edge_errors:
        report["phases"]["5_edge_orphans"] = edge_errors
    print(f"  {p5:,} edges ({edge_errors} orphans skipped)")

    # Phase 6: Code bodies
    print("Phase 6: Code bodies...")
    p6 = 0
    try:
        cursor = old.execute("SELECT * FROM code_bodies")
        while True:
            rows = cursor.fetchmany(1000)
            if not rows:
                break
            for row in rows:
                node_key = id_map.get(row["node_id"])
                qname = node_key[1] if node_key else str(row["node_id"])
                eng.upsert_code_body(
                    qname, row["body"], row["body_hash"],
                    project=row["project_slug"],
                    file_path=row["file_path"],
                    start_line=row["start_line"],
                    end_line=row["end_line"],
                    language=row["language"])
                p6 += 1
                if p6 % 50000 == 0:
                    print(f"  {p6:,} bodies...")
    except Exception as e:
        report["errors"].append(f"Phase 6 (bodies): {e}")
        print(f"  Bodies: ERROR — {e}")
    report["phases"]["6_bodies"] = p6
    print(f"  {p6:,} code bodies")

    old.close()

    # Refresh NetworkX
    print("Refreshing NetworkX graph...")
    brain.refresh_graph()

    duration = round(time.monotonic() - start, 1)
    report["duration_sec"] = duration
    report["totals"] = {
        "nodes": p1 + p2 + p3 + p4,
        "edges": p5,
        "bodies": p6,
    }
    print(f"\nMigration complete in {duration}s")
    print(f"  Nodes: {p1 + p2 + p3 + p4:,}")
    print(f"  Edges: {p5:,}")
    print(f"  Bodies: {p6:,}")
    print(f"  Errors: {len(report['errors'])}")
    return report


def _flush_code_batch(eng, ntype: str, batch: list):
    if ntype == "Function":
        eng.upsert_functions_batch(batch)
    elif ntype == "Class":
        eng.upsert_classes_batch(batch)
    elif ntype == "Test":
        eng.upsert_tests_batch(batch)
