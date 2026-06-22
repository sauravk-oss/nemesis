"""Brain CLI — command-line interface for all brain operations.

Usage: python3 -m brain <command> [args]

Commands:
    init                           First-time setup (dirs + schema + seed + skills)
    skills                         List the 16 Razorpay skills + phase bindings
    stats                          Graph statistics
    health <project>               Service health report (A-F grade)
    search <query> [--type T]      FTS5 text search
    search-code <query> [-p P]     Search code bodies
    context <target> [-b N] [-c C] Context retrieval with budget
    who-calls <function> [-d N]    All callers (up to N hops)
    what-calls <function> [-d N]   All callees (up to N hops)
    path <source> <target>         Shortest path between nodes
    impact <func1,func2> [-d N]    Impact/blast radius analysis
    dead-code <project>            Dead code candidates
    test-gaps <project>            Untested high-PageRank functions

    add-node <type> <name> [-d JSON] [-p P] [-c F]
    get-node <type> <name>
    delete-node <type> <name>
    add-edge <from_type> <from> <to_type> <to> <edge_type>

    feature-create <name> [--owner O]
    feature-update <name> --status S
    feature-list [--status S]
    feature-health <name>

    learn-status                   Learning pipeline state
    learn-flush [--dry-run]        Flush staged items to graph

    ingest <source> [--feature F] [--project P] [--max-chars N]
                                   Franco phase-1: ingest a local file directly;
                                   remote/MCP sources print a needs_fetch plan
    ingest-mcp <type> <id> --payload FILE [--feature F] [--project P]
                                   Franco phase-2: ingest an LLM-fetched payload
                                   (JSON file) → learn → flush (dedup on type+id)

    seed                           Seed all 45 projects + deps
    refresh                        Reload NetworkX from edges

    register-sources               Register data sources from config/sources.json
                                   (DataSource nodes + RELATES_TO edges; no MCP)
    init-experts [--level N]       Seed ProjectExpert nodes for every project to
                                   level N (default 1). Idempotent; never downgrades.
    doctor                         Health check: deps, brain.db, sources, experts,
                                   gh auth, skills, required MCPs (green/amber/red)

    migrate-rubick <path>          Migrate from rubick.db
"""
from __future__ import annotations

import json
import sys
from typing import List

from brain.api import BrainAPI
from brain.config import BrainConfig


def main(argv: List[str] = None):
    args = argv or sys.argv[1:]
    if not args:
        print(__doc__)
        return

    cmd = args[0]
    rest = args[1:]
    brain = BrainAPI()

    try:
        if cmd == "init":
            _init(brain)
        elif cmd == "skills":
            _skills(brain)
        elif cmd == "stats":
            _stats(brain)
        elif cmd == "health":
            _health(brain, rest)
        elif cmd == "search":
            _search(brain, rest)
        elif cmd == "search-code":
            _search_code(brain, rest)
        elif cmd == "context":
            _context(brain, rest)
        elif cmd == "who-calls":
            _who_calls(brain, rest)
        elif cmd == "what-calls":
            _what_calls(brain, rest)
        elif cmd == "path":
            _path(brain, rest)
        elif cmd == "impact":
            _impact(brain, rest)
        elif cmd == "dead-code":
            _dead_code(brain, rest)
        elif cmd == "test-gaps":
            _test_gaps(brain, rest)
        elif cmd == "add-node":
            _add_node(brain, rest)
        elif cmd == "get-node":
            _get_node(brain, rest)
        elif cmd == "delete-node":
            _delete_node(brain, rest)
        elif cmd == "add-edge":
            _add_edge(brain, rest)
        elif cmd == "feature-create":
            _feature_create(brain, rest)
        elif cmd == "feature-update":
            _feature_update(brain, rest)
        elif cmd == "feature-list":
            _feature_list(brain, rest)
        elif cmd == "feature-health":
            _feature_health(brain, rest)
        elif cmd == "learn-status":
            _learn_status(brain)
        elif cmd == "learn-flush":
            _learn_flush(brain, rest)
        elif cmd == "ingest":
            _ingest(brain, rest)
        elif cmd == "ingest-mcp":
            _ingest_mcp(brain, rest)
        elif cmd == "seed":
            _seed(brain)
        elif cmd == "refresh":
            _refresh(brain)
        elif cmd == "register-sources":
            _register_sources(brain)
        elif cmd == "init-experts":
            _init_experts(brain, rest)
        elif cmd == "doctor":
            _doctor(brain)
        elif cmd == "migrate-rubick":
            _migrate(brain, rest)
        else:
            print(f"Unknown command: {cmd}")
            print(__doc__)
    finally:
        brain.close()


def _flag(args, flag, default=None):
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return default


def _has_flag(args, flag):
    return flag in args


def _init(brain: BrainAPI):
    from pathlib import Path
    ws = Path(brain._config.workspace)
    dirs = [ws, ws / "features", ws / "repos", ws / "lance"]
    created = []
    for d in dirs:
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(str(d.relative_to(ws.parent)))

    count = brain.seed_services()
    stats = brain.stats()
    skills_loaded = _load_skill_registry(brain)

    print("Nemesis Brain — initialized")
    print(f"  Workspace:  {ws}")
    print(f"  Database:   {brain._config.db_path}")
    print(f"  Dirs created: {len(created)} ({', '.join(created) if created else 'all existed'})")
    print(f"  Services:   {count} seeded")
    print(f"  Edges:      {stats['graph']['edges']}")
    print(f"  Skills:     {skills_loaded} registered (run 'python3 -m brain skills' to list)")
    print()
    print("Next steps:")
    print("  python3 -m brain stats              # verify graph")
    print("  python3 -m brain skills             # list the 16 Razorpay skills + phase bindings")
    print("  python3 -m brain migrate-rubick workspace/rubick.db  # import old data")
    print("  Open Claude Code and run /nemesis    # start building features")


def _load_skill_registry(brain: BrainAPI) -> int:
    """Register the Razorpay skill registry at init.

    Skills resolve dynamically through the Skill tool, so "loading" means
    recording availability in Brain so /nemesis Step 0 and every phase can
    honor the fallback chain. Non-blocking — Brain failures never abort init.
    """
    from datetime import datetime, timezone

    from brain.config import SKILL_REGISTRY

    try:
        brain.add_node(
            "Signal",
            "skill-registry:loaded",
            data={
                "skills": len(SKILL_REGISTRY),
                "names": [s["skill"] for s in SKILL_REGISTRY],
                "loaded_at": datetime.now(timezone.utc).isoformat(),
                "status": "verified",
            },
            confidence=0.85,
        )
    except Exception:
        pass  # registry availability never blocks init
    return len(SKILL_REGISTRY)


def _skills(brain: BrainAPI):
    from brain.config import SKILL_REGISTRY

    print(f"Razorpay Skill Registry — {len(SKILL_REGISTRY)} skills loaded")
    print(f"{'SKILL':<34} {'PHASES':<28} FALLBACK")
    print("-" * 92)
    for s in SKILL_REGISTRY:
        print(f"{s['skill']:<34} {s['phases']:<28} {s['fallback']}")
    print()
    print("Fallback chain (every Skill() call): Razorpay skill > Brain context > @Slash > proceed.")


def _stats(brain: BrainAPI):
    s = brain.stats()
    print(json.dumps(s, indent=2, default=str))


def _health(brain: BrainAPI, args):
    if not args:
        print("Usage: python3 -m brain health <project>"); return
    r = brain.health(args[0])
    print(json.dumps({"project": r.project, "grade": r.grade, "score": r.score,
                       "metrics": r.metrics, "recommendations": r.recommendations}, indent=2))


def _search(brain: BrainAPI, args):
    if not args:
        print("Usage: python3 -m brain search <query> [--type T]"); return
    query = args[0]
    ntype = _flag(args, "--type")
    results = brain.search(query, ntype=ntype)
    print(json.dumps(results, indent=2, default=str))


def _search_code(brain: BrainAPI, args):
    if not args:
        print("Usage: python3 -m brain search-code <query> [-p project]"); return
    results = brain.search_code(args[0], project=_flag(args, "-p"))
    for r in results:
        r.pop("body", None)
    print(json.dumps(results, indent=2, default=str))


def _context(brain: BrainAPI, args):
    if not args:
        print("Usage: python3 -m brain context <target> [-b budget] [-c consumer]"); return
    budget = int(_flag(args, "-b", "4000"))
    consumer = _flag(args, "-c", "default")
    result = brain.context_for(args[0], budget=budget, consumer=consumer)
    print(json.dumps({"target": result.target, "tokens_used": result.tokens_used,
                       "budget": result.budget, "graph_nodes": result.graph_nodes,
                       "fts_hits": result.fts_hits, "vector_hits": result.vector_hits,
                       "sources": result.sources[:10]}, indent=2))
    print("\n--- CONTEXT ---\n")
    print(result.text)


def _who_calls(brain: BrainAPI, args):
    if not args:
        print("Usage: python3 -m brain who-calls <function> [-d depth]"); return
    depth = int(_flag(args, "-d", "5"))
    callers = brain.who_calls(args[0], depth=depth)
    print(json.dumps({"function": args[0], "callers": callers, "count": len(callers)}, indent=2))


def _what_calls(brain: BrainAPI, args):
    if not args:
        print("Usage: python3 -m brain what-calls <function> [-d depth]"); return
    depth = int(_flag(args, "-d", "5"))
    callees = brain.what_calls(args[0], depth=depth)
    print(json.dumps({"function": args[0], "callees": callees, "count": len(callees)}, indent=2))


def _path(brain: BrainAPI, args):
    if len(args) < 2:
        print("Usage: python3 -m brain path <source> <target>"); return
    p = brain.path(args[0], args[1])
    print(json.dumps({"source": args[0], "target": args[1], "path": p, "hops": len(p) - 1 if p else -1}, indent=2))


def _impact(brain: BrainAPI, args):
    if not args:
        print("Usage: python3 -m brain impact <func1,func2,...> [-d depth]"); return
    funcs = args[0].split(",")
    depth = int(_flag(args, "-d", "5"))
    r = brain.impact(funcs, max_depth=depth)
    print(json.dumps({"functions": r.changed_functions, "direct_callers": r.direct_callers,
                       "total_impacted": r.total_impacted, "services": r.impacted_services,
                       "risk_scores": r.risk_scores, "test_gaps_count": len(r.test_gaps),
                       "overall_risk": r.overall_risk}, indent=2))


def _dead_code(brain: BrainAPI, args):
    if not args:
        print("Usage: python3 -m brain dead-code <project>"); return
    results = brain.dead_code(args[0])
    print(json.dumps({"project": args[0], "count": len(results), "candidates": results[:30]}, indent=2))


def _test_gaps(brain: BrainAPI, args):
    if not args:
        print("Usage: python3 -m brain test-gaps <project>"); return
    results = brain.test_gap(args[0])
    print(json.dumps({"project": args[0], "count": len(results), "gaps": results[:20]}, indent=2))


def _add_node(brain: BrainAPI, args):
    if len(args) < 2:
        print("Usage: python3 -m brain add-node <type> <name> [-d JSON] [-p project] [-c confidence]"); return
    data = json.loads(_flag(args, "-d", "{}"))
    project = _flag(args, "-p")
    confidence = float(_flag(args, "-c", "0.7"))
    nid = brain.add_node(args[0], args[1], data=data, project=project, confidence=confidence)
    print(json.dumps({"id": nid, "type": args[0], "name": args[1]}))


def _get_node(brain: BrainAPI, args):
    if len(args) < 2:
        print("Usage: python3 -m brain get-node <type> <name>"); return
    node = brain.get_node(args[0], args[1])
    print(json.dumps(node or {"error": "not found"}, indent=2, default=str))


def _delete_node(brain: BrainAPI, args):
    if len(args) < 2:
        print("Usage: python3 -m brain delete-node <type> <name>"); return
    ok = brain.delete_node(args[0], args[1])
    print(json.dumps({"deleted": ok, "type": args[0], "name": args[1]}))


def _add_edge(brain: BrainAPI, args):
    if len(args) < 5:
        print("Usage: python3 -m brain add-edge <from_type> <from> <to_type> <to> <edge_type>"); return
    brain.add_edge(args[0], args[1], args[2], args[3], args[4])
    print(json.dumps({"ok": True, "edge": f"{args[0]}:{args[1]} --{args[4]}--> {args[2]}:{args[3]}"}))


def _feature_create(brain: BrainAPI, args):
    if not args:
        print("Usage: python3 -m brain feature-create <name> [--owner O]"); return
    owner = _flag(args, "--owner")
    fid = brain.feature_create(args[0], owner=owner)
    print(json.dumps({"id": fid, "name": args[0], "status": "proposed"}))


def _feature_update(brain: BrainAPI, args):
    if not args:
        print("Usage: python3 -m brain feature-update <name> --status S"); return
    status = _flag(args, "--status")
    ok = brain.feature_update(args[0], status=status)
    print(json.dumps({"updated": ok, "name": args[0], "status": status}))


def _feature_list(brain: BrainAPI, args):
    status = _flag(args, "--status")
    features = brain.feature_list(status=status)
    print(json.dumps([{"name": f["name"], "status": f.get("data", {}).get("status"),
                        "confidence": f.get("confidence")} for f in features], indent=2))


def _feature_health(brain: BrainAPI, args):
    if not args:
        print("Usage: python3 -m brain feature-health <name>"); return
    r = brain.feature_health(args[0])
    print(json.dumps(r, indent=2))


def _learn_status(brain: BrainAPI):
    print(json.dumps(brain.learn_status(), indent=2, default=str))


def _learn_flush(brain: BrainAPI, args):
    dry = _has_flag(args, "--dry-run")
    result = brain.flush(dry_run=dry)
    print(json.dumps({"dry_run": dry, **result}, indent=2))


def _ingest(brain: BrainAPI, args):
    if not args:
        print("Usage: python3 -m brain ingest <source> [--feature F] [--project P] [--max-chars N]")
        return
    source = args[0]
    feature = _flag(args, "--feature")
    project = _flag(args, "--project")
    max_chars = int(_flag(args, "--max-chars", "8000"))

    # Directory → bulk-ingest every text artifact recursively (the `docs` flow).
    from pathlib import Path
    p = Path(source)
    if p.is_dir():
        exts = {".md", ".txt", ".rst", ".html"}
        files = sorted(f for f in p.rglob("*")
                       if f.is_file() and f.suffix.lower() in exts)
        summary = {"directory": str(p), "files": len(files),
                   "ingested": 0, "unchanged": 0, "error": 0}
        for f in files:
            r = brain.ingest(str(f), feature=feature, project=project,
                             max_chars=max_chars)
            summary[r.get("status", "error")] = summary.get(r.get("status", "error"), 0) + 1
        print(json.dumps(summary, indent=2, default=str))
        return

    result = brain.ingest(source, feature=feature, project=project,
                          max_chars=max_chars)
    print(json.dumps(result, indent=2, default=str))
    if result.get("status") == "needs_fetch":
        det = result.get("detection", {})
        print(f"\n[needs_fetch] {result['source_type']}:{result['source_id']} is "
              f"MCP/CLI-backed. The skill layer (Franco) must fetch it, then call:\n"
              f"  python3 -m brain ingest-mcp {result['source_type']} "
              f"'{result['source_id']}' --payload <file.json>"
              + (f" --feature {feature}" if feature else "")
              + (f" --project {project}" if project else ""))


def _ingest_mcp(brain: BrainAPI, args):
    if len(args) < 2:
        print("Usage: python3 -m brain ingest-mcp <source_type> <source_id> "
              "--payload FILE [--feature F] [--project P] [--max-chars N]")
        return
    source_type, source_id = args[0], args[1]
    payload_file = _flag(args, "--payload")
    if not payload_file:
        print("Error: --payload <json-file> is required (the LLM-fetched response)")
        return
    from pathlib import Path
    raw = Path(payload_file).read_text()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = raw  # treat as plain text
    result = brain.ingest_mcp_response(
        source_type, source_id, payload,
        feature=_flag(args, "--feature"), project=_flag(args, "--project"),
        max_chars=int(_flag(args, "--max-chars", "4000")))
    print(json.dumps(result, indent=2, default=str))


def _seed(brain: BrainAPI):
    count = brain.seed_services()
    print(json.dumps({"services_seeded": count}))


def _refresh(brain: BrainAPI):
    t = brain.refresh_graph()
    print(json.dumps({"refreshed": True, "load_time_sec": round(t, 2),
                       "nodes": brain.nxc.node_count, "edges": brain.nxc.edge_count}))


_EXPERT_LEVEL_NAMES = {1: "Apprentice", 2: "Journeyman", 3: "Adept",
                       4: "Master", 5: "Grand Master"}


def _register_sources(brain: BrainAPI):
    """Register every entry in config/sources.json as a DataSource node.

    Upserts one DataSource node per source (name = "<category>:<key>") plus a
    RELATES_TO edge to each listed project Service node. Pure DB writes — no MCP
    calls (the bounded live L1 ingest happens later, LLM-driven, via Franco).
    Idempotent: re-running refreshes node data and re-asserts edges.
    """
    from brain.config import load_sources

    sources = load_sources()
    categories = ("slack", "drive", "github", "devrev")
    ref_field = {"slack": "id", "drive": "id", "github": "repo", "devrev": "query"}

    summary = {"nodes": 0, "edges": 0, "missing_id": 0, "by_category": {}}
    for cat in categories:
        entries = sources.get(cat) or []
        summary["by_category"][cat] = 0
        for e in entries:
            key = e.get("key")
            if not key:
                continue
            ref = e.get(ref_field[cat], "")
            relates = e.get("relates_to") or []
            nname = f"{cat}:{key}"
            data = {
                "category": cat, "key": key, "ref": ref,
                "name": e.get("name", key), "ds_type": e.get("type", cat),
                "role": e.get("role"), "relates_to": relates,
                "ingest": e.get("ingest", {}),
            }
            brain.add_node("DataSource", nname, data=data, confidence=0.85)
            summary["nodes"] += 1
            summary["by_category"][cat] += 1
            for slug in relates:
                brain.add_edge("DataSource", nname, "Service", slug, "RELATES_TO")
                summary["edges"] += 1
            if not ref:
                summary["missing_id"] += 1

    print(json.dumps(summary, indent=2, default=str))
    if summary["missing_id"]:
        print(f"\nNote: {summary['missing_id']} source(s) have an empty id/ref in "
              f"config/sources.json — registered, but not ingestable until you fill "
              f"the channel ID (Decision #29: channel ID over name).")


def _init_experts(brain: BrainAPI, args):
    """Seed a ProjectExpert node for every SEED_PROJECT to the requested level.

    Idempotent and non-destructive: an existing expert is left untouched when its
    level is already >= the requested level (NEVER downgrades a leveled-up
    expert). `brain init` seeds services + skills but NOT experts — this command
    gives a fresh brain.db its expert baseline (run by /nemesis init at L1).
    """
    from brain.config import EXPERT_XP_THRESHOLDS, SEED_PROJECTS

    level = int(_flag(args, "--level", "1"))
    if level not in EXPERT_XP_THRESHOLDS:
        print(f"Error: --level must be one of {sorted(EXPERT_XP_THRESHOLDS)}")
        return
    level_name = _EXPERT_LEVEL_NAMES[level]
    xp = EXPERT_XP_THRESHOLDS[level]

    summary = {"level": level, "level_name": level_name, "created": 0,
               "leveled_up": 0, "skipped_higher": 0, "total": len(SEED_PROJECTS)}
    for proj in SEED_PROJECTS:
        slug = proj["slug"]
        existing = brain.get_node("ProjectExpert", slug)
        existing_data = (existing.get("data") or {}) if existing else {}
        cur_level = existing_data.get("level", 0) or 0
        if existing and cur_level >= level:
            summary["skipped_higher"] += 1
            continue
        data = dict(existing_data)
        data.update({"project": slug, "level": level, "level_name": level_name,
                     "xp": max(xp, existing_data.get("xp", 0) or 0),
                     "title": level_name})
        data.setdefault("role", proj.get("role"))
        data.setdefault("expertise", "")
        data.setdefault("function_depth", 0)
        data.setdefault("deep_read_at", None)
        brain.add_node("ProjectExpert", slug, data=data, project=slug,
                       confidence=0.8)
        summary["leveled_up" if existing else "created"] += 1

    print(json.dumps(summary, indent=2, default=str))
    if summary["skipped_higher"]:
        print(f"\n{summary['skipped_higher']} expert(s) already at level "
              f">= {level} — left untouched (never downgraded).")


def _doctor(brain: BrainAPI):
    """Health check for a Nemesis brain install.

    Prints a green/amber/red table across: Python deps, brain.db reachability +
    stats, registered data sources (vs config/sources.json), ProjectExpert
    coverage, gh auth, the skill registry, and required (OAuth) MCPs. MCPs are
    validate-only — this never stores tokens or makes MCP calls.
    """
    import importlib
    import subprocess

    from brain.config import (REQUIRED_MCP, SEED_PROJECTS, SKILL_REGISTRY,
                              load_sources)

    checks = []  # (name, status, detail, hint); status in {OK, WARN, FAIL, INFO}

    # 1. Python deps
    missing = [m for m in ("networkx",) if not _importable(importlib, m)]
    optional_missing = [m for m in ("lancedb", "sentence_transformers")
                        if not _importable(importlib, m)]
    if missing:
        checks.append(("Python deps", "FAIL", f"missing: {', '.join(missing)}",
                       "pip install -r requirements.txt"))
    elif optional_missing:
        checks.append(("Python deps", "WARN",
                       f"core OK; optional missing: {', '.join(optional_missing)}",
                       "vector search disabled until: pip install -r requirements.txt"))
    else:
        checks.append(("Python deps", "OK", "core + optional present", ""))

    # 2. brain.db reachable + stats
    nt = {}
    try:
        g = brain.stats()["graph"]
        nodes, edges, svcs = g.get("nodes", 0), g.get("edges", 0), g.get("services", 0)
        nt = g.get("node_types", {})
        if edges > 0:
            checks.append(("brain.db", "OK",
                           f"{nodes:,} nodes / {edges:,} edges / {svcs} services", ""))
        else:
            checks.append(("brain.db", "WARN", "reachable but empty",
                           "run: python3 -m brain init"))
    except Exception as exc:
        checks.append(("brain.db", "FAIL", f"unreachable: {exc}",
                       f"check {brain._config.db_path}; run: python3 -m brain init"))

    # 3. Data sources registered vs config
    try:
        src = load_sources()
        configured = sum(len(src.get(c) or [])
                         for c in ("slack", "drive", "github", "devrev"))
        registered = nt.get("DataSource", 0)
        if registered < configured:
            checks.append(("Data sources", "WARN",
                           f"{registered} registered / {configured} configured",
                           "run: python3 -m brain register-sources"))
        else:
            checks.append(("Data sources", "OK",
                           f"{registered} registered / {configured} configured", ""))
    except Exception as exc:
        checks.append(("Data sources", "WARN", f"config unreadable: {exc}",
                       "check config/sources.json"))

    # 4. ProjectExpert coverage
    experts, seed_n = nt.get("ProjectExpert", 0), len(SEED_PROJECTS)
    if experts < seed_n:
        checks.append(("Experts", "WARN", f"{experts} / {seed_n} projects",
                       "run: python3 -m brain init-experts --level 1"))
    else:
        checks.append(("Experts", "OK", f"{experts} / {seed_n} projects", ""))

    # 5. gh auth (best-effort subprocess; never fails the doctor)
    try:
        r = subprocess.run(["gh", "auth", "status"], capture_output=True,
                           text=True, timeout=10)
        if r.returncode == 0:
            checks.append(("GitHub CLI", "OK", "gh authenticated", ""))
        else:
            checks.append(("GitHub CLI", "WARN", "gh not authenticated",
                           "run: gh auth login"))
    except FileNotFoundError:
        checks.append(("GitHub CLI", "WARN", "gh not installed",
                       "install GitHub CLI: https://cli.github.com"))
    except Exception as exc:
        checks.append(("GitHub CLI", "WARN", f"probe failed: {exc}",
                       "run: gh auth status"))

    # 6. Skill registry
    checks.append(("Skills", "OK", f"{len(SKILL_REGISTRY)} registered", ""))

    # 7. Required MCPs (validate-only). Reading local Claude config JSON is NOT an
    # MCP call — we only detect which OAuth connectors/servers are wired up.
    present, missing = _probe_mcps()
    total = len(REQUIRED_MCP)
    if missing:
        checks.append(("MCPs", "WARN",
                       f"{present}/{total} connected; missing: {', '.join(missing)}",
                       "connect missing OAuth MCPs inside Claude Code (tokens never stored here)"))
    else:
        checks.append(("MCPs", "OK",
                       f"{present}/{total} connected (OAuth connectors)", ""))

    # render
    rank = {"FAIL": 0, "WARN": 1, "OK": 2, "INFO": 2}
    worst = min((rank[c[1]] for c in checks), default=2)
    verdict = {0: "RED", 1: "AMBER", 2: "GREEN"}[worst]
    mark = {"OK": "[OK]  ", "WARN": "[WARN]", "FAIL": "[FAIL]", "INFO": "[INFO]"}

    print(f"Nemesis Doctor — {verdict}")
    print(f"  db: {brain._config.db_path}")
    print("-" * 78)
    print(f"  {'CHECK':<14} {'STATUS':<6} DETAIL")
    print("-" * 78)
    for name, status, detail, hint in checks:
        print(f"  {name:<14} {mark[status]} {detail}")
        if hint and status != "OK":
            print(f"  {'':<14}        -> {hint}")
    print("-" * 78)
    print(f"  Verdict: {verdict}  "
          f"({sum(1 for c in checks if c[1] == 'OK')} ok, "
          f"{sum(1 for c in checks if c[1] == 'WARN')} warn, "
          f"{sum(1 for c in checks if c[1] == 'FAIL')} fail, "
          f"{sum(1 for c in checks if c[1] == 'INFO')} info)")


def _importable(importlib, mod: str) -> bool:
    try:
        importlib.import_module(mod)
        return True
    except Exception:
        return False


def _probe_mcps():
    """Detect which REQUIRED_MCP entries are wired up (local server or hosted connector).

    Reads only local Claude config JSON — never makes an MCP call. Returns
    (present_count, [missing_keys]). An MCP counts as present if its server name
    or any alias matches a local mcpServers key OR a claude.ai connector name.
    """
    import json as _json
    import os as _os
    from brain.config import REQUIRED_MCP

    pool = set()
    for p in (_os.path.expanduser("~/.claude/settings.json"),
              _os.path.join(_os.getcwd(), ".claude", "settings.json")):
        try:
            with open(p) as fh:
                for k in (_json.load(fh).get("mcpServers") or {}):
                    pool.add(k.lower())
        except Exception:
            continue
    try:
        with open(_os.path.expanduser("~/.claude.json")) as fh:
            for name in (_json.load(fh).get("claudeAiMcpEverConnected") or []):
                pool.add(str(name).lower())
    except Exception:
        pass

    present, missing = 0, []
    for mcp in REQUIRED_MCP:
        tokens = [str(mcp["server"]), *[str(a) for a in mcp.get("aliases", [])]]
        hit = any(any(t.lower() in c or c in t.lower() for c in pool) for t in tokens)
        if hit:
            present += 1
        else:
            missing.append(str(mcp["key"]))
    return present, missing


def _migrate(brain: BrainAPI, args):
    if not args:
        print("Usage: python3 -m brain migrate-rubick <rubick_db_path>"); return
    from brain.migration.migrate import migrate_rubick
    report = migrate_rubick(args[0], brain)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
