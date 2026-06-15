#!/usr/bin/env python3
"""Rubick Expert Initialization Engine.

Eagerly initializes all 45 project experts to Level 2 on first brain init.
Each expert deeply reads its project's codebase and stores structured
expertise as ProjectExpert nodes in rubick.db.

Usage:
    rubick_heroes.py init <db_path> --config <config_path> --repos <repos_path>
    rubick_heroes.py status <db_path>
    rubick_heroes.py refresh <db_path> --project <slug> --repos <repos_path>
"""

import sys
import os
import json
import re
import hashlib
import argparse
import sqlite3
import subprocess
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import brain_config as cfg
except ImportError:
    cfg = None

logger = logging.getLogger("rubick_heroes")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DB_PATH = str(cfg.RUBICK_DB_PATH) if cfg else "workspace/rubick.db"
_EXPERTS_CONFIG = str(cfg.NEMESIS_ROOT / "config" / "experts.json") if cfg else "config/experts.json"
_REPOS_BASE = str(cfg.GITHUB_CLONE_BASE) if cfg and hasattr(cfg, "GITHUB_CLONE_BASE") else "workspace/repos"

_XP_INITIAL_DEEP_READ = 300
_LEVEL_THRESHOLDS = {1: 0, 2: 500, 3: 1500, 4: 3000, 5: 5000}
_LEVEL_NAMES = {1: "L1", 2: "L2", 3: "L3", 4: "L4", 5: "L5"}
_BATCH_SIZE = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _xp_to_level(xp: int) -> tuple[int, str]:
    level = 1
    for lvl, threshold in sorted(_LEVEL_THRESHOLDS.items()):
        if xp >= threshold:
            level = lvl
    return level, _LEVEL_NAMES.get(level, "Unknown")


def _load_experts_config(config_path: str) -> dict:
    with open(config_path, 'r') as f:
        return json.load(f)


def _detect_language(repo_path: str) -> str:
    if os.path.isfile(os.path.join(repo_path, "go.mod")):
        return "go"
    if os.path.isfile(os.path.join(repo_path, "composer.json")):
        return "php"
    if os.path.isfile(os.path.join(repo_path, "package.json")):
        return "ts"
    if any(f.endswith(".proto") for f in os.listdir(repo_path) if os.path.isfile(os.path.join(repo_path, f))):
        return "proto"
    return "unknown"


def _run_cmd(cmd: list[str], cwd: str, timeout: int = 30) -> str:
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _get_commit_sha(repo_path: str) -> str:
    sha = _run_cmd(["git", "rev-parse", "HEAD"], repo_path, timeout=5)
    return sha[:12] if sha else "unknown"


def _count_by_grep(repo_path: str, pattern: str, include: str = "*.go") -> int:
    result = _run_cmd(
        ["grep", "-rlc", pattern, "--include", include, "."],
        repo_path, timeout=15
    )
    if not result:
        return 0
    return sum(int(line.split(":")[-1]) for line in result.split("\n") if ":" in line)


# ---------------------------------------------------------------------------
# Level 2 Deep Read — extracts patterns from source files
# ---------------------------------------------------------------------------

def _extract_routing_pattern(repo_path: str, lang: str) -> dict:
    """Extract routing framework and patterns."""
    info = {"framework": "unknown", "patterns": [], "entry_files": []}

    if lang == "go":
        for pattern in ["server.go", "routes.go", "router.go", "handler.go"]:
            result = _run_cmd(
                ["find", ".", "-name", pattern, "-not", "-path", "*/vendor/*"],
                repo_path, timeout=10
            )
            if result:
                info["entry_files"].extend(result.split("\n")[:5])

        grep_out = _run_cmd(
            ["grep", "-rn", "chi.NewRouter\\|gin.Default\\|gin.New\\|mux.NewRouter\\|spine",
             "--include=*.go", "-l", "."],
            repo_path, timeout=10
        )
        if grep_out:
            files = grep_out.split("\n")
            if any("chi" in f or "chi.NewRouter" in open(os.path.join(repo_path, f.lstrip("./"))).read(500)
                   if os.path.isfile(os.path.join(repo_path, f.lstrip("./"))) else False for f in files):
                info["framework"] = "chi"
            elif any("gin" in f for f in files):
                info["framework"] = "gin"
            elif any("spine" in f for f in files):
                info["framework"] = "spine"

        route_grep = _run_cmd(
            ["grep", "-rhn", "r\\.Post\\|r\\.Get\\|r\\.Put\\|r\\.Delete\\|r\\.Route\\|router\\.Handle",
             "--include=*.go", "."],
            repo_path, timeout=10
        )
        if route_grep:
            info["patterns"] = list(set(
                line.strip()[:120] for line in route_grep.split("\n")[:20] if line.strip()
            ))

    elif lang == "php":
        route_files = _run_cmd(
            ["find", ".", "-path", "*/routes/*.php", "-not", "-path", "*/vendor/*"],
            repo_path, timeout=10
        )
        if route_files:
            info["entry_files"] = route_files.split("\n")[:5]
            info["framework"] = "laravel"

    elif lang == "ts":
        route_grep = _run_cmd(
            ["find", ".", "-name", "routes.ts", "-o", "-name", "router.ts",
             "-not", "-path", "*/node_modules/*"],
            repo_path, timeout=10
        )
        if route_grep:
            info["entry_files"] = route_grep.split("\n")[:5]
            info["framework"] = "express/next"

    return info


def _extract_middleware(repo_path: str, lang: str) -> list[str]:
    """Extract middleware chain."""
    if lang == "go":
        grep_out = _run_cmd(
            ["grep", "-rhn", "Use(\\|middleware\\.\\|Middleware",
             "--include=*.go", "."],
            repo_path, timeout=10
        )
        if grep_out:
            middlewares = set()
            for line in grep_out.split("\n"):
                line = line.strip()
                m = re.search(r'Use\((\w+)', line)
                if m:
                    middlewares.add(m.group(1))
                m = re.search(r'middleware\.(\w+)', line)
                if m:
                    middlewares.add(m.group(1))
            return sorted(middlewares)[:20]
    elif lang == "php":
        grep_out = _run_cmd(
            ["grep", "-rhn", "middleware\\|->middleware",
             "--include=*.php", "."],
            repo_path, timeout=10
        )
        if grep_out:
            middlewares = set()
            for line in grep_out.split("\n"):
                m = re.search(r"middleware\(['\"](\w+)", line)
                if m:
                    middlewares.add(m.group(1))
            return sorted(middlewares)[:20]
    return []


def _extract_config_mechanism(repo_path: str, lang: str) -> dict:
    """Detect config/feature flag mechanisms."""
    mechanisms = {"splitz": False, "dcs": False, "razorx": False, "env": False, "gates": []}

    if lang in ("go", "php"):
        ext = "*.go" if lang == "go" else "*.php"
        for name, pattern in [("splitz", "Splitz\\|splitz"), ("dcs", "DCS\\|dcs\\."),
                               ("razorx", "Razorx\\|razorx")]:
            count = _run_cmd(
                ["grep", "-rlc", pattern, "--include", ext, "."],
                repo_path, timeout=10
            )
            if count:
                mechanisms[name] = True

        gate_grep = _run_cmd(
            ["grep", "-rhn", "IsEnabled\\|IsExperimentOn\\|GetTreatment\\|variant",
             "--include", ext, "."],
            repo_path, timeout=10
        )
        if gate_grep:
            gates = set()
            for line in gate_grep.split("\n"):
                m = re.search(r'["\']([A-Za-z]\w{5,})["\']', line)
                if m and not m.group(1).startswith("func"):
                    gates.add(m.group(1))
            mechanisms["gates"] = sorted(gates)[:30]

    return mechanisms


def _extract_key_structs(repo_path: str, lang: str) -> dict[str, str]:
    """Extract key data structures."""
    structs = {}

    if lang == "go":
        grep_out = _run_cmd(
            ["grep", "-rn", "^type.*struct {",
             "--include=*.go", "."],
            repo_path, timeout=10
        )
        if grep_out:
            for line in grep_out.split("\n")[:50]:
                m = re.match(r'^(.*?):(\d+):type\s+(\w+)\s+struct\s*\{', line)
                if m and m.group(3)[0].isupper():
                    structs[m.group(3)] = f"{m.group(1).lstrip('./')}:{m.group(2)}"

    elif lang == "php":
        grep_out = _run_cmd(
            ["grep", "-rn", "^class ",
             "--include=*.php", "."],
            repo_path, timeout=10
        )
        if grep_out:
            for line in grep_out.split("\n")[:50]:
                m = re.match(r'^(.*?):(\d+):class\s+(\w+)', line)
                if m:
                    structs[m.group(3)] = f"{m.group(1).lstrip('./')}:{m.group(2)}"

    return structs


def _extract_entry_points(repo_path: str, lang: str) -> list[str]:
    """Extract API entry points (endpoints)."""
    endpoints = []

    if lang == "go":
        grep_out = _run_cmd(
            ["grep", "-rhn",
             'Post(\\|Get(\\|Put(\\|Delete(\\|Handle(\\|HandleFunc(',
             "--include=*.go", "."],
            repo_path, timeout=10
        )
        if grep_out:
            for line in grep_out.split("\n")[:30]:
                m = re.search(r'(?:Post|Get|Put|Delete|Handle|HandleFunc)\(\s*["\']([^"\']+)', line)
                if m:
                    endpoints.append(m.group(1))

    elif lang == "php":
        grep_out = _run_cmd(
            ["grep", "-rhn",
             "Route::post\\|Route::get\\|Route::put\\|Route::delete",
             "--include=*.php", "."],
            repo_path, timeout=10
        )
        if grep_out:
            for line in grep_out.split("\n")[:30]:
                m = re.search(r"Route::\w+\(\s*['\"]([^'\"]+)", line)
                if m:
                    endpoints.append(m.group(1))

    return sorted(set(endpoints))[:50]


def _get_rubick_stats(conn: sqlite3.Connection, project_slug: str) -> dict:
    """Get node counts from rubick.db for a project."""
    stats = {}
    for node_type in ("Function", "Class", "Test", "Endpoint", "Module", "DataStore"):
        row = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE type = ? AND "
            "json_extract(data, '$.project') = ?",
            (node_type, project_slug)
        ).fetchone()
        stats[f"{node_type.lower()}_count"] = row[0] if row else 0
    return stats


# ---------------------------------------------------------------------------
# Core: Deep Read for a Single Project
# ---------------------------------------------------------------------------

def deep_read_project(repo_path: str, project_slug: str,
                      role: str = "unknown",
                      conn: Optional[sqlite3.Connection] = None) -> dict:
    """Perform Level 2 deep read of a project and return expertise dict."""
    lang = _detect_language(repo_path)
    commit_sha = _get_commit_sha(repo_path)
    now = datetime.now(timezone.utc).isoformat()

    routing = _extract_routing_pattern(repo_path, lang)
    middleware = _extract_middleware(repo_path, lang)
    config = _extract_config_mechanism(repo_path, lang)
    structs = _extract_key_structs(repo_path, lang)
    entry_points = _extract_entry_points(repo_path, lang)

    rubick_stats = {}
    if conn:
        rubick_stats = _get_rubick_stats(conn, project_slug)

    expertise = {
        "routing_pattern": f"{routing['framework']} with {len(routing['patterns'])} routes",
        "routing_framework": routing["framework"],
        "routing_entry_files": routing["entry_files"][:5],
        "route_count": len(routing["patterns"]),
        "middleware_chain": middleware,
        "config_mechanism": {
            k: v for k, v in config.items() if k != "gates"
        },
        "splitz_gates": config.get("gates", [])[:30],
        "key_data_structures": dict(list(structs.items())[:30]),
        "entry_points": entry_points[:30],
        "language": lang,
        "commit_sha": commit_sha,
        **rubick_stats,
    }

    # Level 2 requires 500 XP; initial deep-read earns 300 + 200 bonus for eager init
    init_xp = _LEVEL_THRESHOLDS[2]

    return {
        "project": project_slug,
        "role": role,
        "level": 2,
        "level_name": "L2",
        "xp": init_xp,
        "deep_read_at": now,
        "expertise": expertise,
        "features_analyzed": [],
        "contradictions_found": 0,
        "confirmations": 0,
    }


# ---------------------------------------------------------------------------
# Store Expert in rubick.db
# ---------------------------------------------------------------------------

def store_expert(conn: sqlite3.Connection, expert_data: dict, project_slug: str) -> int:
    """Store a ProjectExpert node + EXPERT_ON edge in rubick.db.

    Returns the node ID.
    """
    now = datetime.now(timezone.utc).isoformat()
    expert_name = project_slug
    data_json = json.dumps(expert_data, ensure_ascii=False)

    row = conn.execute(
        "SELECT id FROM nodes WHERE type = 'ProjectExpert' AND name = ?",
        (expert_name,)
    ).fetchone()

    if row:
        conn.execute(
            "UPDATE nodes SET data = ?, updated_at = ? WHERE id = ?",
            (data_json, now, row[0])
        )
        node_id = row[0]
    else:
        cur = conn.execute(
            "INSERT INTO nodes (type, name, data, source_type, confidence, created_at, updated_at) "
            "VALUES (?, ?, ?, 'expert', 0.85, ?, ?)",
            ("ProjectExpert", expert_name, data_json, now, now)
        )
        node_id = cur.lastrowid

    project_row = conn.execute(
        "SELECT id FROM nodes WHERE type = 'Project' AND name = ?",
        (project_slug,)
    ).fetchone()

    if project_row:
        existing_edge = conn.execute(
            "SELECT id FROM edges WHERE from_node_id = ? AND to_node_id = ? AND edge_type = 'EXPERT_ON'",
            (node_id, project_row[0])
        ).fetchone()
        if not existing_edge:
            conn.execute(
                "INSERT INTO edges (from_node_id, to_node_id, edge_type, data, created_at) "
                "VALUES (?, ?, 'EXPERT_ON', '{}', ?)",
                (node_id, project_row[0], now)
            )

    conn.commit()
    return node_id


# ---------------------------------------------------------------------------
# Eager Expert Init — batch all projects
# ---------------------------------------------------------------------------

def eager_hero_init(db_path: str, experts_config_path: str, repos_path: str,
                    batch_size: int = _BATCH_SIZE) -> dict:
    """Initialize all project experts to Level 2.

    Reads each project's codebase, extracts Level 2 expertise (routing,
    middleware, config, structs, endpoints), and stores ProjectExpert nodes.

    Returns summary: {initialized, already_expert, skipped, failed, details}.
    """
    config = _load_experts_config(experts_config_path)
    project_to_role = config.get("project_to_role", {})

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    existing_experts = {}
    try:
        rows = conn.execute(
            "SELECT name, data FROM nodes WHERE type = 'ProjectExpert'"
        ).fetchall()
        for row in rows:
            data = json.loads(row["data"]) if row["data"] else {}
            existing_experts[row["name"]] = data.get("level", 0)
    except Exception:
        pass

    results = {
        "initialized": 0,
        "already_expert": 0,
        "skipped": 0,
        "failed": [],
        "details": [],
    }

    projects = list(project_to_role.items())
    for i in range(0, len(projects), batch_size):
        batch = projects[i:i + batch_size]

        for project_slug, role in batch:
            if project_slug in existing_experts and existing_experts[project_slug] >= 2:
                results["already_expert"] += 1
                results["details"].append({
                    "project": project_slug, "role": role,
                    "status": "already_level2+", "level": existing_experts[project_slug],
                })
                continue

            repo_path = os.path.join(repos_path, project_slug)
            if not os.path.isdir(repo_path):
                results["skipped"] += 1
                results["details"].append({
                    "project": project_slug, "role": role,
                    "status": "repo_missing",
                })
                continue

            try:
                expert_data = deep_read_project(
                    repo_path, project_slug, role=role, conn=conn
                )
                node_id = store_expert(conn, expert_data, project_slug)
                results["initialized"] += 1
                results["details"].append({
                    "project": project_slug, "role": role,
                    "status": "initialized", "level": 2, "node_id": node_id,
                    "routes": expert_data["expertise"].get("route_count", 0),
                    "structs": len(expert_data["expertise"].get("key_data_structures", {})),
                    "middleware": len(expert_data["expertise"].get("middleware_chain", [])),
                    "splitz_gates": len(expert_data["expertise"].get("splitz_gates", [])),
                })
            except Exception as e:
                results["failed"].append({
                    "project": project_slug, "role": role,
                    "error": str(e)[:200],
                })

    conn.close()

    results["total_projects"] = len(projects)
    return results


# ---------------------------------------------------------------------------
# Refresh a Single Project Expert
# ---------------------------------------------------------------------------

def refresh_expert(db_path: str, project_slug: str, repos_path: str,
                   experts_config_path: Optional[str] = None) -> dict:
    """Re-read a single project and update its expert node."""
    config_path = experts_config_path or _EXPERTS_CONFIG
    config = _load_experts_config(config_path)
    project_to_role = config.get("project_to_role", {})

    role = project_to_role.get(project_slug, "unknown")

    repo_path = os.path.join(repos_path, project_slug)
    if not os.path.isdir(repo_path):
        return {"error": f"Repo not found: {repo_path}"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    existing = conn.execute(
        "SELECT data FROM nodes WHERE type = 'ProjectExpert' AND name = ?",
        (project_slug,)
    ).fetchone()

    prior_xp = 0
    prior_features = []
    if existing and existing["data"]:
        old_data = json.loads(existing["data"])
        prior_xp = old_data.get("xp", 0)
        prior_features = old_data.get("features_analyzed", [])

    expert_data = deep_read_project(
        repo_path, project_slug, role=role, conn=conn
    )

    expert_data["xp"] = max(prior_xp, _XP_INITIAL_DEEP_READ)
    expert_data["features_analyzed"] = prior_features
    level, level_name = _xp_to_level(expert_data["xp"])
    expert_data["level"] = level
    expert_data["level_name"] = level_name

    node_id = store_expert(conn, expert_data, project_slug)
    conn.close()

    return {
        "project": project_slug,
        "role": role,
        "status": "refreshed",
        "level": level,
        "xp": expert_data["xp"],
        "node_id": node_id,
    }


# ---------------------------------------------------------------------------
# Status Report
# ---------------------------------------------------------------------------

def hero_status(db_path: str) -> dict:
    """Get status of all project experts."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    experts = []
    try:
        rows = conn.execute(
            "SELECT name, data, updated_at FROM nodes WHERE type = 'ProjectExpert' "
            "ORDER BY updated_at DESC"
        ).fetchall()
        for row in rows:
            data = json.loads(row["data"]) if row["data"] else {}
            experts.append({
                "name": row["name"],
                "project": data.get("project", "?"),
                "role": data.get("role", data.get("archetype", "?")),
                "level": data.get("level", 0),
                "level_name": data.get("level_name", "?"),
                "xp": data.get("xp", 0),
                "features_analyzed": len(data.get("features_analyzed", [])),
                "deep_read_at": data.get("deep_read_at", "?"),
                "updated_at": row["updated_at"],
            })
    except Exception:
        pass

    conn.close()

    level_dist = {}
    for e in experts:
        lvl = e.get("level_name", "?")
        level_dist[lvl] = level_dist.get(lvl, 0) + 1

    return {
        "total_experts": len(experts),
        "level_distribution": level_dist,
        "experts": experts,
    }


# ---------------------------------------------------------------------------
# Function-Level Expert Knowledge
# ---------------------------------------------------------------------------

def build_function_knowledge(conn: sqlite3.Connection,
                             project_slug: str) -> dict:
    """Populate expert_functions and expert_tests for a project's expert.

    Queries CALLS, TESTS, CONTAINS edges to map each function to its callers,
    callees, and test coverage. Returns summary stats.
    """
    expert_row = conn.execute(
        "SELECT id, data FROM nodes WHERE type = 'ProjectExpert' AND (name = ? OR name LIKE ?)",
        (project_slug, f"%:{project_slug}")
    ).fetchone()
    if not expert_row:
        return {"error": f"No expert found for {project_slug}", "functions": 0, "tests": 0}

    expert_node_id = expert_row["id"]

    slug_pattern = f'%"project_slug":"{project_slug}"%'

    func_rows = conn.execute(
        "SELECT id, name, data FROM nodes WHERE type = 'Function' AND data LIKE ?",
        (slug_pattern,)
    ).fetchall()

    test_rows = conn.execute(
        "SELECT id, name, data FROM nodes WHERE type = 'Test' AND data LIKE ?",
        (slug_pattern,)
    ).fetchall()

    func_ids = {r["id"] for r in func_rows}
    test_ids = {r["id"] for r in test_rows}
    func_id_to_name = {r["id"]: r["name"] for r in func_rows}
    test_id_to_name = {r["id"]: r["name"] for r in test_rows}

    callers_map: dict[int, list[str]] = {fid: [] for fid in func_ids}
    callees_map: dict[int, list[str]] = {fid: [] for fid in func_ids}
    tested_by_map: dict[int, list[str]] = {fid: [] for fid in func_ids}
    tests_map: dict[int, list[str]] = {tid: [] for tid in test_ids}

    call_edges = conn.execute(
        """SELECT from_node_id, to_node_id FROM edges
           WHERE edge_type = 'CALLS'
           AND (from_node_id IN ({fids}) OR to_node_id IN ({fids}))""".format(
            fids=",".join(str(f) for f in func_ids) if func_ids else "0"
        )
    ).fetchall()

    for e in call_edges:
        from_id, to_id = e["from_node_id"], e["to_node_id"]
        if to_id in callers_map and from_id in func_id_to_name:
            callers_map[to_id].append(func_id_to_name[from_id])
        if from_id in callees_map and to_id in func_id_to_name:
            callees_map[from_id].append(func_id_to_name[to_id])

    test_edges = conn.execute(
        """SELECT from_node_id, to_node_id FROM edges
           WHERE edge_type = 'TESTS'
           AND (from_node_id IN ({tids}) OR to_node_id IN ({fids}))""".format(
            tids=",".join(str(t) for t in test_ids) if test_ids else "0",
            fids=",".join(str(f) for f in func_ids) if func_ids else "0"
        )
    ).fetchall()

    for e in test_edges:
        from_id, to_id = e["from_node_id"], e["to_node_id"]
        if to_id in tested_by_map and from_id in test_id_to_name:
            tested_by_map[to_id].append(test_id_to_name[from_id])
        if from_id in tests_map and to_id in func_id_to_name:
            tests_map[from_id].append(func_id_to_name[to_id])

    fn_inserted = 0
    for frow in func_rows:
        fid = frow["id"]
        fdata = json.loads(frow["data"]) if frow["data"] else {}
        file_path = fdata.get("file_path", "")
        line_number = fdata.get("start_line", 0)

        body_row = conn.execute(
            "SELECT body_hash FROM code_bodies WHERE node_id = ? LIMIT 1", (fid,)
        ).fetchone()

        conn.execute(
            """INSERT OR REPLACE INTO expert_functions
               (expert_node_id, function_node_id, function_name, file_path,
                line_number, callers, callees, tested_by, complexity, body_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (expert_node_id, fid, frow["name"], file_path, line_number,
             json.dumps(callers_map.get(fid, [])),
             json.dumps(callees_map.get(fid, [])),
             json.dumps(tested_by_map.get(fid, [])),
             len(callers_map.get(fid, [])) + len(callees_map.get(fid, [])),
             body_row["body_hash"] if body_row else None)
        )
        fn_inserted += 1

    test_inserted = 0
    for trow in test_rows:
        tid = trow["id"]
        tdata = json.loads(trow["data"]) if trow["data"] else {}
        file_path = tdata.get("file_path", "")

        assertion_count = 0
        body_row = conn.execute(
            "SELECT body FROM code_bodies WHERE node_id = ? LIMIT 1", (tid,)
        ).fetchone()
        if body_row and body_row["body"]:
            assertion_count = len(re.findall(
                r'\b(?:assert|Assert|require|Require|expect|Expect|t\.Error|t\.Fatal|t\.Run|Should|ShouldNot)\b',
                body_row["body"]
            ))

        conn.execute(
            """INSERT OR REPLACE INTO expert_tests
               (expert_node_id, test_node_id, test_name, file_path,
                functions_tested, assertion_count, edge_cases)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (expert_node_id, tid, trow["name"], file_path,
             json.dumps(tests_map.get(tid, [])),
             assertion_count,
             None)
        )
        test_inserted += 1

    with_tests = sum(1 for fid in func_ids if tested_by_map.get(fid))
    with_callers = sum(1 for fid in func_ids if callers_map.get(fid))
    total = len(func_ids)
    coverage_pct = round(with_tests / total * 100, 1) if total > 0 else 0.0

    expert_data = json.loads(expert_row["data"]) if expert_row["data"] else {}
    expert_data["function_depth"] = {
        "total": total,
        "with_tests": with_tests,
        "with_callers": with_callers,
        "coverage_pct": coverage_pct,
        "test_count": len(test_ids),
    }
    conn.execute(
        "UPDATE nodes SET data = ?, updated_at = ? WHERE id = ?",
        (json.dumps(expert_data, ensure_ascii=False),
         datetime.now(timezone.utc).isoformat(), expert_node_id)
    )
    conn.commit()

    return {
        "project": project_slug,
        "expert_node_id": expert_node_id,
        "functions": fn_inserted,
        "tests": test_inserted,
        "with_tests": with_tests,
        "with_callers": with_callers,
        "coverage_pct": coverage_pct,
    }


def build_all_function_knowledge(db_path: str) -> dict:
    """Build function-level knowledge for ALL project experts."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    experts = conn.execute(
        "SELECT name FROM nodes WHERE type = 'ProjectExpert'"
    ).fetchall()

    results = {"total": 0, "success": 0, "failed": [], "details": []}
    for row in experts:
        project_slug = row["name"].split(":")[-1] if ":" in row["name"] else row["name"]
        results["total"] += 1
        try:
            r = build_function_knowledge(conn, project_slug)
            if "error" not in r:
                results["success"] += 1
            results["details"].append(r)
        except Exception as e:
            results["failed"].append({"project": project_slug, "error": str(e)[:200]})

    conn.close()
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Rubick Expert Initialization Engine")
    sub = parser.add_subparsers(dest="command")

    init_p = sub.add_parser("init", help="Initialize all project experts to Level 2")
    init_p.add_argument("db_path")
    init_p.add_argument("--config", default=_EXPERTS_CONFIG, help="Path to experts.json")
    init_p.add_argument("--repos", default=_REPOS_BASE, help="Path to cloned repos")
    init_p.add_argument("--batch-size", type=int, default=_BATCH_SIZE)

    status_p = sub.add_parser("status", help="Show all expert statuses")
    status_p.add_argument("db_path")

    refresh_p = sub.add_parser("refresh", help="Refresh a single project expert")
    refresh_p.add_argument("db_path")
    refresh_p.add_argument("--project", required=True)
    refresh_p.add_argument("--repos", default=_REPOS_BASE)
    refresh_p.add_argument("--config", default=_EXPERTS_CONFIG)

    build_k = sub.add_parser("build-knowledge", help="Build function-level knowledge for all experts")
    build_k.add_argument("db_path")
    build_k.add_argument("--project", help="Single project slug (default: all)")

    args = parser.parse_args()

    if args.command == "init":
        result = eager_hero_init(args.db_path, args.config, args.repos, args.batch_size)
        print(f"\nExpert Init Complete:")
        print(f"  Initialized: {result['initialized']}")
        print(f"  Already Level 2+: {result['already_expert']}")
        print(f"  Skipped (no repo): {result['skipped']}")
        print(f"  Failed: {len(result['failed'])}")
        print(f"  Total projects: {result['total_projects']}")
        if result["failed"]:
            print("\nFailures:")
            for f in result["failed"]:
                print(f"  {f['project']} ({f['role']}): {f['error']}")
        print("\nDetails:")
        for d in result["details"]:
            if d["status"] == "initialized":
                print(f"  ✓ {d['project']:30s} {d['role']:20s} "
                      f"routes={d['routes']} structs={d['structs']} "
                      f"middleware={d['middleware']} gates={d['splitz_gates']}")
            elif d["status"] == "already_level2+":
                print(f"  = {d['project']:30s} {d['role']:20s} level={d['level']}")
            else:
                print(f"  ✗ {d['project']:30s} {d['role']:20s} {d['status']}")
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "status":
        status = hero_status(args.db_path)
        print(f"\nProject Experts: {status['total_experts']}")
        print(f"Level Distribution: {json.dumps(status['level_distribution'])}")
        print()
        for e in status["experts"]:
            print(f"  {e['project']:30s} {e['role']:15s} L{e['level']} ({e['level_name']:4s}) "
                  f"XP={e['xp']:5d}  features={e['features_analyzed']}")

    elif args.command == "refresh":
        result = refresh_expert(args.db_path, args.project, args.repos, args.config)
        print(json.dumps(result, indent=2))

    elif args.command == "build-knowledge":
        if args.project:
            conn = sqlite3.connect(args.db_path)
            conn.row_factory = sqlite3.Row
            result = build_function_knowledge(conn, args.project)
            conn.close()
            print(json.dumps(result, indent=2))
        else:
            result = build_all_function_knowledge(args.db_path)
            print(f"\nFunction Knowledge Build Complete:")
            print(f"  Total experts: {result['total']}")
            print(f"  Success: {result['success']}")
            print(f"  Failed: {len(result['failed'])}")
            for d in result["details"]:
                if "error" not in d:
                    print(f"  {d['project']:30s} fns={d['functions']:5d} tests={d['tests']:4d} "
                          f"coverage={d['coverage_pct']}%")
            print(json.dumps(result, indent=2, default=str))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
