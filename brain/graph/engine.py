"""GraphEngine — SQLite typed tables CRUD + generic nodes + edges.

Single interface for all graph reads/writes. NetworkX loaded separately.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from brain.graph.schema import ensure_schema


class GraphEngine:

    def __init__(self, db_path: str):
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        ensure_schema(self._conn)

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self):
        self._conn.close()

    # --- Services ---
    def upsert_service(self, slug: str, **kw) -> None:
        cols = ["slug"] + list(kw.keys())
        vals = [slug] + list(kw.values())
        ph = ",".join(["?"] * len(vals))
        cs = ",".join(cols)
        up = ",".join(f"{k}=excluded.{k}" for k in kw) if kw else ""
        sql = f"INSERT INTO services({cs}) VALUES ({ph})"
        if up:
            sql += f" ON CONFLICT(slug) DO UPDATE SET {up}"
        else:
            sql = sql.replace("INSERT", "INSERT OR IGNORE")
        self._conn.execute(sql, vals)
        self._conn.commit()

    def get_service(self, slug: str) -> Optional[Dict]:
        r = self._conn.execute("SELECT * FROM services WHERE slug=?", (slug,)).fetchone()
        return dict(r) if r else None

    def list_services(self) -> List[Dict]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM services ORDER BY slug")]

    # --- Functions (batch) ---
    def upsert_functions_batch(self, rows: List[Dict]) -> int:
        if not rows:
            return 0
        cols = ["qname", "name", "file_path", "line_start", "line_end", "language",
                "signature", "receiver", "params", "returns", "complexity",
                "is_exported", "is_test", "project", "body_hash"]
        ph = ",".join(["?"] * len(cols))
        cs = ",".join(cols)
        up = ",".join(f"{c}=excluded.{c}" for c in cols if c != "qname")
        sql = f"INSERT INTO functions({cs}) VALUES ({ph}) ON CONFLICT(qname) DO UPDATE SET {up}"
        self._conn.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])
        self._conn.commit()
        return len(rows)

    def get_function(self, qname: str) -> Optional[Dict]:
        r = self._conn.execute("SELECT * FROM functions WHERE qname=?", (qname,)).fetchone()
        return dict(r) if r else None

    def find_functions(self, project: str = None, name_like: str = None, limit: int = 100) -> List[Dict]:
        sql, p = "SELECT * FROM functions WHERE 1=1", []
        if project:
            sql += " AND project=?"; p.append(project)
        if name_like:
            sql += " AND name LIKE ?"; p.append(f"%{name_like}%")
        return [dict(r) for r in self._conn.execute(sql + f" LIMIT {limit}", p)]

    def count_functions(self, project: str = None) -> int:
        if project:
            return self._conn.execute("SELECT COUNT(*) FROM functions WHERE project=?", (project,)).fetchone()[0]
        return self._conn.execute("SELECT COUNT(*) FROM functions").fetchone()[0]

    # --- Classes (batch) ---
    def upsert_classes_batch(self, rows: List[Dict]) -> int:
        if not rows: return 0
        cols = ["qname", "name", "file_path", "line_start", "line_end", "language", "kind", "is_exported", "project"]
        ph = ",".join(["?"] * len(cols))
        up = ",".join(f"{c}=excluded.{c}" for c in cols if c != "qname")
        self._conn.executemany(
            f"INSERT INTO classes({','.join(cols)}) VALUES ({ph}) ON CONFLICT(qname) DO UPDATE SET {up}",
            [tuple(r.get(c) for c in cols) for r in rows])
        self._conn.commit()
        return len(rows)

    # --- Tests (batch) ---
    def upsert_tests_batch(self, rows: List[Dict]) -> int:
        if not rows: return 0
        cols = ["qname", "name", "file_path", "line_start", "line_end", "kind", "project"]
        ph = ",".join(["?"] * len(cols))
        up = ",".join(f"{c}=excluded.{c}" for c in cols if c != "qname")
        self._conn.executemany(
            f"INSERT INTO tests({','.join(cols)}) VALUES ({ph}) ON CONFLICT(qname) DO UPDATE SET {up}",
            [tuple(r.get(c) for c in cols) for r in rows])
        self._conn.commit()
        return len(rows)

    # --- Files (batch) ---
    def upsert_files_batch(self, rows: List[Dict]) -> int:
        if not rows: return 0
        cols = ["path", "project", "language", "line_count", "hash"]
        ph = ",".join(["?"] * len(cols))
        up = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("path", "project"))
        self._conn.executemany(
            f"INSERT INTO files({','.join(cols)}) VALUES ({ph}) ON CONFLICT(path, project) DO UPDATE SET {up}",
            [tuple(r.get(c) for c in cols) for r in rows])
        self._conn.commit()
        return len(rows)

    # --- Endpoints (batch) ---
    def upsert_endpoints_batch(self, rows: List[Dict]) -> int:
        if not rows: return 0
        cols = ["route", "http_method", "handler", "file_path", "line", "auth_required", "project"]
        ph = ",".join(["?"] * len(cols))
        pk = ("route", "http_method", "project")
        up = ",".join(f"{c}=excluded.{c}" for c in cols if c not in pk)
        self._conn.executemany(
            f"INSERT INTO endpoints({','.join(cols)}) VALUES ({ph}) ON CONFLICT(route, http_method, project) DO UPDATE SET {up}",
            [tuple(r.get(c) for c in cols) for r in rows])
        self._conn.commit()
        return len(rows)

    # --- Datastores (batch) ---
    def upsert_datastores_batch(self, rows: List[Dict]) -> int:
        if not rows: return 0
        cols = ["name", "store_type", "engine", "project", "schema_def"]
        ph = ",".join(["?"] * len(cols))
        up = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("name", "project"))
        self._conn.executemany(
            f"INSERT INTO datastores({','.join(cols)}) VALUES ({ph}) ON CONFLICT(name, project) DO UPDATE SET {up}",
            [tuple(r.get(c) for c in cols) for r in rows])
        self._conn.commit()
        return len(rows)

    # --- Generic Nodes ---
    def upsert_node(self, ntype: str, name: str, data: Dict = None,
                    project_slug: str = None, source_type: str = None,
                    source_id: str = None, confidence: float = 0.7,
                    retention_days: int = -1) -> int:
        self._conn.execute(
            """INSERT INTO nodes(type,name,data,project_slug,source_type,source_id,confidence,retention_days)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(type,name) DO UPDATE SET
                 data=excluded.data, project_slug=excluded.project_slug,
                 source_type=excluded.source_type, source_id=excluded.source_id,
                 confidence=MAX(nodes.confidence, excluded.confidence),
                 updated_at=datetime('now')""",
            (ntype, name, json.dumps(data or {}), project_slug, source_type, source_id, confidence, retention_days))
        self._conn.commit()
        r = self._conn.execute("SELECT id FROM nodes WHERE type=? AND name=?", (ntype, name)).fetchone()
        return r[0] if r else 0

    def get_node(self, ntype: str, name: str) -> Optional[Dict]:
        r = self._conn.execute("SELECT * FROM nodes WHERE type=? AND name=?", (ntype, name)).fetchone()
        if not r: return None
        d = dict(r); d["data"] = json.loads(d.get("data") or "{}"); return d

    def find_nodes(self, ntype: str = None, project: str = None, limit: int = 100) -> List[Dict]:
        sql, p = "SELECT * FROM nodes WHERE 1=1", []
        if ntype: sql += " AND type=?"; p.append(ntype)
        if project: sql += " AND project_slug=?"; p.append(project)
        sql += f" ORDER BY updated_at DESC LIMIT {limit}"
        out = []
        for r in self._conn.execute(sql, p):
            d = dict(r); d["data"] = json.loads(d.get("data") or "{}"); out.append(d)
        return out

    def count_nodes(self, ntype: str = None) -> int:
        if ntype:
            return self._conn.execute("SELECT COUNT(*) FROM nodes WHERE type=?", (ntype,)).fetchone()[0]
        return self._conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]

    def delete_node(self, ntype: str, name: str) -> bool:
        c = self._conn.execute("DELETE FROM nodes WHERE type=? AND name=?", (ntype, name))
        self._conn.commit()
        return c.rowcount > 0

    # --- Edges ---
    def add_edge(self, from_type: str, from_name: str, to_type: str, to_name: str,
                 edge_type: str, data: Dict = None) -> None:
        self._conn.execute(
            """INSERT INTO edges(from_type,from_name,to_type,to_name,edge_type,data)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(from_type,from_name,to_type,to_name,edge_type)
               DO UPDATE SET data=excluded.data""",
            (from_type, from_name, to_type, to_name, edge_type, json.dumps(data or {})))
        self._conn.commit()

    def add_edges_batch(self, edges: List[Dict]) -> int:
        if not edges: return 0
        sql = """INSERT INTO edges(from_type,from_name,to_type,to_name,edge_type,data)
                 VALUES (?,?,?,?,?,?)
                 ON CONFLICT(from_type,from_name,to_type,to_name,edge_type)
                 DO UPDATE SET data=excluded.data"""
        self._conn.executemany(sql, [
            (e["from_type"], e["from_name"], e["to_type"], e["to_name"],
             e["edge_type"], json.dumps(e.get("data") or {})) for e in edges])
        self._conn.commit()
        return len(edges)

    def get_edges_from(self, from_type: str, from_name: str, edge_type: str = None) -> List[Dict]:
        sql = "SELECT * FROM edges WHERE from_type=? AND from_name=?"
        p = [from_type, from_name]
        if edge_type: sql += " AND edge_type=?"; p.append(edge_type)
        return [dict(r) for r in self._conn.execute(sql, p)]

    def get_edges_to(self, to_type: str, to_name: str, edge_type: str = None) -> List[Dict]:
        sql = "SELECT * FROM edges WHERE to_type=? AND to_name=?"
        p = [to_type, to_name]
        if edge_type: sql += " AND edge_type=?"; p.append(edge_type)
        return [dict(r) for r in self._conn.execute(sql, p)]

    def count_edges(self, edge_type: str = None) -> int:
        if edge_type:
            return self._conn.execute("SELECT COUNT(*) FROM edges WHERE edge_type=?", (edge_type,)).fetchone()[0]
        return self._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    def load_edges(self, edge_types: List[str] = None) -> List[Tuple[str, str, str]]:
        if edge_types:
            ph = ",".join(["?"] * len(edge_types))
            rows = self._conn.execute(
                f"SELECT from_name, to_name, edge_type FROM edges WHERE edge_type IN ({ph})",
                edge_types).fetchall()
        else:
            rows = self._conn.execute("SELECT from_name, to_name, edge_type FROM edges").fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    # --- Code Bodies ---
    def upsert_code_body(self, node_id: str, body: str, body_hash: str,
                         project: str = None, file_path: str = None,
                         start_line: int = None, end_line: int = None,
                         language: str = None) -> int:
        self._conn.execute(
            """INSERT INTO code_bodies(node_id,project,file_path,start_line,end_line,language,body,body_hash,byte_length)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(node_id) DO UPDATE SET body=excluded.body,body_hash=excluded.body_hash,byte_length=excluded.byte_length""",
            (node_id, project, file_path, start_line, end_line, language, body, body_hash, len(body.encode())))
        self._conn.commit()
        r = self._conn.execute("SELECT id FROM code_bodies WHERE node_id=?", (node_id,)).fetchone()
        return r[0] if r else 0

    def get_code_body(self, node_id: str) -> Optional[str]:
        r = self._conn.execute("SELECT body FROM code_bodies WHERE node_id=?", (node_id,)).fetchone()
        return r[0] if r else None

    # --- FTS5 Search ---
    @staticmethod
    def _fts5_safe(query: str) -> str:
        q = query.strip()
        if not q:
            return '""'
        if '"' not in q and not any(op in q for op in (' AND ', ' OR ', ' NOT ', ' NEAR')):
            return '"' + q.replace('"', '') + '"'
        return q

    def search_nodes_fts(self, query: str, ntype: str = None, limit: int = 20) -> List[Dict]:
        q = self._fts5_safe(query)
        if ntype:
            rows = self._conn.execute(
                "SELECT n.* FROM nodes_fts f JOIN nodes n ON n.id=f.rowid WHERE nodes_fts MATCH ? AND n.type=? ORDER BY rank LIMIT ?",
                (q, ntype, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT n.* FROM nodes_fts f JOIN nodes n ON n.id=f.rowid WHERE nodes_fts MATCH ? ORDER BY rank LIMIT ?",
                (q, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r); d["data"] = json.loads(d.get("data") or "{}"); out.append(d)
        return out

    def search_code_fts(self, query: str, project: str = None, limit: int = 20) -> List[Dict]:
        q = self._fts5_safe(query)
        if project:
            rows = self._conn.execute(
                "SELECT cb.* FROM code_fts f JOIN code_bodies cb ON cb.id=f.rowid WHERE code_fts MATCH ? AND cb.project=? ORDER BY rank LIMIT ?",
                (q, project, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT cb.* FROM code_fts f JOIN code_bodies cb ON cb.id=f.rowid WHERE code_fts MATCH ? ORDER BY rank LIMIT ?",
                (q, limit)).fetchall()
        return [dict(r) for r in rows]

    # --- Stats ---
    def stats(self) -> Dict[str, Any]:
        s = {}
        for t in ["services", "files", "functions", "classes", "modules",
                   "endpoints", "datastores", "tests", "kafka_topics"]:
            s[t] = self._conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        s["nodes"] = self._conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        s["edges"] = self._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        s["code_bodies"] = self._conn.execute("SELECT COUNT(*) FROM code_bodies").fetchone()[0]
        s["triplets"] = self._conn.execute("SELECT COUNT(*) FROM triplets").fetchone()[0]
        s["node_types"] = {r[0]: r[1] for r in self._conn.execute(
            "SELECT type, COUNT(*) FROM nodes GROUP BY type ORDER BY COUNT(*) DESC")}
        s["edge_types"] = {r[0]: r[1] for r in self._conn.execute(
            "SELECT edge_type, COUNT(*) FROM edges GROUP BY edge_type ORDER BY COUNT(*) DESC")}
        return s
