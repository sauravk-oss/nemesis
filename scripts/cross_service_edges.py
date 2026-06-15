#!/usr/bin/env python3
"""
Cross-Service Edge Builder for rubick.db

Detects and creates cross-service relationship edges:
1. DEPENDS_ON edges between Project nodes from go.mod analysis
2. RELATES_TO edges for cross-service HTTP/gRPC call patterns
3. IMPORTS edges for cross-service library imports

Schema notes (from rubick_graph.py):
  - nodes table: id (INTEGER PK AUTO), type, name, data(JSON), source_type, confidence
  - edges table: id (INTEGER PK AUTO), from_node_id, to_node_id, edge_type, data(JSON)
  - UNIQUE(from_node_id, to_node_id, edge_type) on edges
  - UNIQUE(type, name) on nodes
"""

import sqlite3
import os
import re
import json
from datetime import datetime
from collections import defaultdict

import sys
sys.path.insert(0, "/Users/saurav.k/Projects/Agents/nemesis_v2/scripts")

DB_PATH = "/Users/saurav.k/Projects/Agents/nemesis_v2/workspace/rubick.db"
REPOS_PATH = "/Users/saurav.k/Projects/Agents/nemesis_v2/workspace/repos"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

# ── Phase 0: Reconnaissance ──

def recon():
    conn = get_db()
    print("=" * 60)
    print("PHASE 0: RECONNAISSANCE")
    print("=" * 60)

    print("\n--- Edge Type Distribution ---")
    rows = conn.execute("SELECT edge_type, count(*) as cnt FROM edges GROUP BY edge_type ORDER BY cnt DESC").fetchall()
    for r in rows:
        print(f"  {r['edge_type']:25s}  {r['cnt']:>8,}")

    print("\n--- Project Nodes ---")
    projects = conn.execute("SELECT id, name FROM nodes WHERE type='Project' ORDER BY name").fetchall()
    for p in projects:
        print(f"  {p['name']:35s}  id={p['id']}")
    print(f"  Total: {len(projects)}")

    print("\n--- Existing Project-to-Project DEPENDS_ON ---")
    deps = conn.execute("""
        SELECT n1.name as from_proj, n2.name as to_proj
        FROM edges e
        JOIN nodes n1 ON e.from_node_id = n1.id
        JOIN nodes n2 ON e.to_node_id = n2.id
        WHERE e.edge_type = 'DEPENDS_ON'
        AND n1.type = 'Project' AND n2.type = 'Project'
        ORDER BY n1.name, n2.name
    """).fetchall()
    for d in deps:
        print(f"  {d['from_proj']:30s} -> {d['to_proj']}")
    print(f"  Total: {len(deps)}")

    print("\n--- Cross-Project IMPORTS check ---")
    cross_imports = conn.execute("""
        SELECT json_extract(n1.data,'$.project') as p1,
               json_extract(n2.data,'$.project') as p2,
               count(*) as cnt
        FROM edges e
        JOIN nodes n1 ON e.from_node_id = n1.id
        JOIN nodes n2 ON e.to_node_id = n2.id
        WHERE e.edge_type = 'IMPORTS'
        AND json_extract(n1.data,'$.project') IS NOT NULL
        AND json_extract(n2.data,'$.project') IS NOT NULL
        AND json_extract(n1.data,'$.project') != json_extract(n2.data,'$.project')
        GROUP BY p1, p2
        ORDER BY cnt DESC
        LIMIT 10
    """).fetchall()
    for ci in cross_imports:
        print(f"  {ci['p1']:30s} -> {ci['p2']:30s}  ({ci['cnt']})")
    if not cross_imports:
        print("  (none found)")

    conn.close()

# ── Phase 1: go.mod dependency extraction ──

def extract_gomod_deps():
    print("\n" + "=" * 60)
    print("PHASE 1: go.mod DEPENDENCY EXTRACTION")
    print("=" * 60)

    repos = sorted(os.listdir(REPOS_PATH))
    dep_map = {}  # service -> set of dependency names (first path component)
    razorpay_re = re.compile(r'github\.com/razorpay/([a-zA-Z0-9_-]+)')

    for repo in repos:
        gomod = os.path.join(REPOS_PATH, repo, "go.mod")
        if not os.path.exists(gomod):
            continue
        with open(gomod, 'r') as f:
            content = f.read()

        deps = set()
        for m in razorpay_re.finditer(content):
            dep = m.group(1)
            if dep != repo:  # skip self
                deps.add(dep)

        if deps:
            dep_map[repo] = deps
            print(f"\n  {repo} depends on ({len(deps)}):")
            for d in sorted(deps):
                print(f"    -> {d}")

    total_pairs = sum(len(v) for v in dep_map.values())
    print(f"\n  Services with cross-deps: {len(dep_map)}")
    print(f"  Total dependency pairs: {total_pairs}")
    return dep_map

# ── Phase 2: Create DEPENDS_ON edges (Project -> Project) ──

def create_depends_on_edges(dep_map):
    print("\n" + "=" * 60)
    print("PHASE 2: CREATE DEPENDS_ON EDGES")
    print("=" * 60)

    conn = get_db()

    # Load project nodes
    projects = {}
    for r in conn.execute("SELECT id, name FROM nodes WHERE type='Project'").fetchall():
        projects[r['name']] = r['id']

    # Alias map: some go.mod deps don't exactly match project names
    # E.g., "cross-border" in repos but dep might be "payments-cross-border" or "cross-border-sdk"
    alias_map = {}
    for pname in projects:
        alias_map[pname] = pname
        # Strip common prefixes/suffixes
        if pname.startswith('payments-'):
            alias_map[pname[len('payments-'):]] = pname
        if pname.endswith('-service'):
            alias_map[pname[:-len('-service')]] = pname

    def resolve_project(dep_name):
        """Try to resolve a go.mod dependency name to a Project node id."""
        dep_name = dep_name.lower()
        # Direct match
        if dep_name in projects:
            return projects[dep_name]
        # Alias
        if dep_name in alias_map:
            return projects.get(alias_map[dep_name])
        # Replace underscores with hyphens
        alt = dep_name.replace('_', '-')
        if alt in projects:
            return projects[alt]
        if alt in alias_map:
            return projects.get(alias_map[alt])
        return None

    # Get existing deps to avoid duplicates
    existing = set()
    for r in conn.execute("""
        SELECT from_node_id, to_node_id FROM edges
        WHERE edge_type='DEPENDS_ON'
    """).fetchall():
        existing.add((r['from_node_id'], r['to_node_id']))

    now = datetime.utcnow().isoformat()
    created = 0
    skipped_exist = 0
    skipped_no_proj = 0
    unresolved = set()

    for service, deps in dep_map.items():
        from_id = resolve_project(service)
        if not from_id:
            continue

        for dep in deps:
            to_id = resolve_project(dep)
            if not to_id:
                unresolved.add(dep)
                skipped_no_proj += 1
                continue
            if from_id == to_id:
                continue
            if (from_id, to_id) in existing:
                skipped_exist += 1
                continue

            data = json.dumps({
                "source": "go.mod",
                "discovered_at": now,
                "confidence": 0.95,
                "cross_service": True
            })
            conn.execute("""
                INSERT OR IGNORE INTO edges (from_node_id, to_node_id, edge_type, data)
                VALUES (?, ?, 'DEPENDS_ON', ?)
            """, (from_id, to_id, data))
            existing.add((from_id, to_id))
            created += 1

    conn.commit()
    conn.close()

    print(f"  Created: {created} new DEPENDS_ON edges")
    print(f"  Skipped (already exist): {skipped_exist}")
    print(f"  Skipped (no project node): {skipped_no_proj}")
    if unresolved:
        print(f"  Unresolved deps ({len(unresolved)}): {sorted(unresolved)[:20]}")
    return created

# ── Phase 3: Detect cross-service HTTP/gRPC calls ──

def detect_cross_service_calls():
    print("\n" + "=" * 60)
    print("PHASE 3: DETECT CROSS-SERVICE CALLS (HTTP/gRPC)")
    print("=" * 60)

    # Known service name -> repo name mapping
    repo_dirs = set(os.listdir(REPOS_PATH))

    def resolve_service(name):
        name = name.lower().replace('_', '-')
        if name in repo_dirs:
            return name
        if name + '-service' in repo_dirs:
            return name + '-service'
        if 'payments-' + name in repo_dirs:
            return 'payments-' + name
        return None

    # Patterns to detect cross-service calls
    patterns = [
        # HTTP client calls with service name
        re.compile(r'(?:client|httpClient|httpclient|Client|httpSdk)\.(?:Call|Do|Get|Post|Put|Patch|Delete)\s*\(\s*(?:ctx\s*,\s*)?["\x60]([a-z][\w-]+)["\x60]'),
        # gRPC Dial
        re.compile(r'grpc\.Dial[A-Za-z]*\s*\(\s*["\x60]([a-z][\w-]+)[:\.]'),
        # NewXxxServiceClient
        re.compile(r'New([A-Z]\w+)(?:Service)?Client\s*\('),
        # Service URL config
        re.compile(r'(?:SERVICE_URL|BASE_URL|HOST|service_name|serviceName|ServiceName)\s*[:=]\s*["\x60](?:https?://)?([a-z][\w-]+)'),
        # Internal API paths
        re.compile(r'["\x60]/v[12]/(?:internal|admin)/([a-z][\w-]+)'),
    ]

    # RPC proto import pattern
    proto_re = re.compile(r'"github\.com/razorpay/rpc/([a-z][\w_-]+)')

    repos = sorted(os.listdir(REPOS_PATH))
    cross_calls = defaultdict(list)  # (caller, target) -> [(file, line_no, snippet)]

    for repo in repos:
        repo_path = os.path.join(REPOS_PATH, repo)
        if not os.path.isdir(repo_path):
            continue

        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in ('.git', 'vendor', 'node_modules', 'testdata', '.idea', 'mock')]
            for fname in files:
                if not fname.endswith('.go'):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, 'r', errors='replace') as f:
                        lines = f.readlines()
                except:
                    continue

                rel = os.path.relpath(fpath, repo_path)

                for i, line in enumerate(lines, 1):
                    # Check HTTP/gRPC patterns
                    for pat in patterns:
                        for m in pat.finditer(line):
                            svc = m.group(1)
                            target = resolve_service(svc)
                            if target and target != repo:
                                cross_calls[(repo, target)].append((rel, i, line.strip()[:120]))

                    # Check rpc proto imports
                    for m in proto_re.finditer(line):
                        svc = m.group(1)
                        target = resolve_service(svc)
                        if target and target != repo:
                            cross_calls[(repo, target)].append((rel, i, f"rpc import: {svc}"))

    print(f"\n  Cross-service call pairs detected: {len(cross_calls)}")
    for (caller, target), evidence in sorted(cross_calls.items()):
        print(f"\n  {caller} -> {target}  ({len(evidence)} refs)")
        for f, l, txt in evidence[:3]:
            print(f"    {f}:{l}  {txt}")
        if len(evidence) > 3:
            print(f"    ... +{len(evidence)-3} more")

    return cross_calls

# ── Phase 4: Create RELATES_TO edges for cross-service calls ──

def create_cross_call_edges(cross_calls):
    print("\n" + "=" * 60)
    print("PHASE 4: CREATE CROSS-SERVICE CALL EDGES")
    print("=" * 60)

    conn = get_db()

    # Load project nodes
    projects = {}
    for r in conn.execute("SELECT id, name FROM nodes WHERE type='Project'").fetchall():
        projects[r['name']] = r['id']

    now = datetime.utcnow().isoformat()
    created_relates = 0
    created_depends = 0

    # Get existing edges
    existing_depends = set()
    for r in conn.execute("SELECT from_node_id, to_node_id FROM edges WHERE edge_type='DEPENDS_ON'").fetchall():
        existing_depends.add((r['from_node_id'], r['to_node_id']))

    existing_relates = set()
    for r in conn.execute("SELECT from_node_id, to_node_id FROM edges WHERE edge_type='CALLS_SERVICE'").fetchall():
        existing_relates.add((r['from_node_id'], r['to_node_id']))

    for (caller, target), evidence in cross_calls.items():
        from_id = projects.get(caller)
        to_id = projects.get(target)
        if not from_id or not to_id or from_id == to_id:
            continue

        # Categorize evidence
        files = set(e[0] for e in evidence)
        rpc_count = sum(1 for e in evidence if 'rpc import' in e[2])
        http_count = len(evidence) - rpc_count

        # Create CALLS_SERVICE edge (matches existing edge type in DB)
        if (from_id, to_id) not in existing_relates:
            data = json.dumps({
                "relationship": "cross_service_call",
                "evidence_files": len(files),
                "evidence_total": len(evidence),
                "rpc_refs": rpc_count,
                "http_refs": http_count,
                "source": "pattern_detection",
                "discovered_at": now,
                "confidence": 0.85,
                "cross_service": True,
                "sample_files": sorted(files)[:5]
            })
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO edges (from_node_id, to_node_id, edge_type, data)
                    VALUES (?, ?, 'CALLS_SERVICE', ?)
                """, (from_id, to_id, data))
                existing_relates.add((from_id, to_id))
                created_relates += 1
            except Exception as e:
                print(f"  ERROR: {e}")

        # Also ensure DEPENDS_ON exists
        if (from_id, to_id) not in existing_depends:
            data = json.dumps({
                "source": "pattern_detection",
                "discovered_at": now,
                "confidence": 0.8,
                "cross_service": True
            })
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO edges (from_node_id, to_node_id, edge_type, data)
                    VALUES (?, ?, 'DEPENDS_ON', ?)
                """, (from_id, to_id, data))
                existing_depends.add((from_id, to_id))
                created_depends += 1
            except:
                pass

    conn.commit()
    conn.close()

    print(f"  Created: {created_relates} CALLS_SERVICE edges (cross-service calls)")
    print(f"  Created: {created_depends} DEPENDS_ON edges (from call patterns)")
    return created_relates, created_depends

# ── Phase 5: Create function-level cross-service IMPORTS ──

def create_cross_imports(dep_map):
    """Create IMPORTS edges from functions in service A to modules in service B."""
    print("\n" + "=" * 60)
    print("PHASE 5: CROSS-SERVICE IMPORTS (function -> module)")
    print("=" * 60)

    conn = get_db()

    # Library repos that many services import
    lib_repos = {'goutils', 'integrations-go', 'integrations-utils', 'rpc'}

    # Get Module nodes from libraries
    lib_modules = {}
    for r in conn.execute("""
        SELECT id, name, json_extract(data, '$.project') as project
        FROM nodes WHERE type='Module'
        AND json_extract(data, '$.project') IN ('goutils','integrations-go','integrations-utils','rpc')
    """).fetchall():
        lib_modules[(r['project'], r['name'])] = r['id']
    print(f"  Library module nodes: {len(lib_modules)}")

    # Get Function/Module nodes by (project, file)
    func_by_proj = defaultdict(list)
    for r in conn.execute("""
        SELECT id, type, json_extract(data, '$.project') as project,
               json_extract(data, '$.file') as file
        FROM nodes
        WHERE type IN ('Function', 'Module')
        AND json_extract(data, '$.project') IS NOT NULL
        AND json_extract(data, '$.project') NOT IN ('goutils','integrations-go','integrations-utils','rpc')
    """).fetchall():
        if r['project'] and r['file']:
            func_by_proj[(r['project'], r['file'])].append(r['id'])

    print(f"  Service function/module files: {len(func_by_proj)}")

    # For each service that depends on a library, scan its Go files for imports
    import_re = re.compile(r'"github\.com/razorpay/(goutils|integrations-go|integrations-utils|rpc)/([\w/.-]+)"')

    now = datetime.utcnow().isoformat()
    created = 0
    repos = sorted(os.listdir(REPOS_PATH))

    for repo in repos:
        if repo in lib_repos:
            continue
        repo_deps = dep_map.get(repo, set())
        if not repo_deps.intersection(lib_repos):
            continue

        repo_path = os.path.join(REPOS_PATH, repo)
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in ('.git', 'vendor', 'node_modules')]
            for fname in files:
                if not fname.endswith('.go'):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, 'r', errors='replace') as f:
                        content = f.read()
                except:
                    continue

                rel = os.path.relpath(fpath, repo_path)
                src_ids = func_by_proj.get((repo, rel), [])
                if not src_ids:
                    continue

                for m in import_re.finditer(content):
                    lib = m.group(1)
                    pkg = m.group(2).split('/')[0]  # first path component
                    target_id = lib_modules.get((lib, pkg))
                    if not target_id:
                        continue

                    # Create edge from first function in file to library module
                    src_id = src_ids[0]
                    data = json.dumps({
                        "source": "import_path_match",
                        "library": lib,
                        "package": pkg,
                        "file": rel,
                        "discovered_at": now,
                        "confidence": 0.9,
                        "cross_service": True
                    })
                    try:
                        conn.execute("""
                            INSERT OR IGNORE INTO edges (from_node_id, to_node_id, edge_type, data)
                            VALUES (?, ?, 'IMPORTS_LIB', ?)
                        """, (src_id, target_id, data))
                        created += 1
                    except:
                        pass

    conn.commit()
    conn.close()

    print(f"  Created: {created} cross-service IMPORTS edges")
    return created

# ── Phase 6: Final summary ──

def final_summary():
    print("\n" + "=" * 60)
    print("FINAL SUMMARY")
    print("=" * 60)

    conn = get_db()

    print("\n--- Edge Type Distribution (After) ---")
    for r in conn.execute("SELECT edge_type, count(*) as cnt FROM edges GROUP BY edge_type ORDER BY cnt DESC").fetchall():
        print(f"  {r['edge_type']:25s}  {r['cnt']:>8,}")

    print("\n--- Cross-Service Edges ---")
    for etype in ['DEPENDS_ON', 'CALLS_SERVICE', 'IMPORTS_LIB', 'CROSS_REF']:
        cross = conn.execute(f"""
            SELECT count(*) FROM edges
            WHERE edge_type=? AND json_extract(data, '$.cross_service') = 1
        """, (etype,)).fetchone()[0]
        total = conn.execute("SELECT count(*) FROM edges WHERE edge_type=?", (etype,)).fetchone()[0]
        print(f"  {etype}: {total} total, {cross} cross-service")

    print("\n--- All Project DEPENDS_ON pairs ---")
    rows = conn.execute("""
        SELECT n1.name as from_p, n2.name as to_p, json_extract(e.data, '$.source') as src
        FROM edges e
        JOIN nodes n1 ON e.from_node_id = n1.id
        JOIN nodes n2 ON e.to_node_id = n2.id
        WHERE e.edge_type = 'DEPENDS_ON'
        AND n1.type = 'Project' AND n2.type = 'Project'
        ORDER BY n1.name, n2.name
    """).fetchall()
    for r in rows:
        print(f"  {r['from_p']:30s} -> {r['to_p']:30s}  [{r['src'] or 'seed'}]")
    print(f"  Total project-level DEPENDS_ON: {len(rows)}")

    print("\n--- Cross-Service CALLS_SERVICE pairs ---")
    rows = conn.execute("""
        SELECT n1.name as from_p, n2.name as to_p,
               json_extract(e.data, '$.evidence_total') as refs,
               json_extract(e.data, '$.rpc_refs') as rpc,
               json_extract(e.data, '$.http_refs') as http
        FROM edges e
        JOIN nodes n1 ON e.from_node_id = n1.id
        JOIN nodes n2 ON e.to_node_id = n2.id
        WHERE e.edge_type = 'CALLS_SERVICE'
        ORDER BY CAST(COALESCE(json_extract(e.data, '$.evidence_total'), '0') AS INTEGER) DESC
    """).fetchall()
    for r in rows:
        print(f"  {r['from_p']:30s} -> {r['to_p']:30s}  (refs={r['refs']}, rpc={r['rpc']}, http={r['http']})")
    print(f"  Total CALLS_SERVICE: {len(rows)}")

    conn.close()

# ── Main ──

if __name__ == "__main__":
    recon()
    dep_map = extract_gomod_deps()
    create_depends_on_edges(dep_map)
    cross_calls = detect_cross_service_calls()
    create_cross_call_edges(cross_calls)
    create_cross_imports(dep_map)
    final_summary()
