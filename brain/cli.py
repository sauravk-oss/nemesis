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

    seed                           Seed all 45 projects + deps
    refresh                        Reload NetworkX from edges

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
        elif cmd == "seed":
            _seed(brain)
        elif cmd == "refresh":
            _refresh(brain)
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
        print("Usage: python -m brain health <project>"); return
    r = brain.health(args[0])
    print(json.dumps({"project": r.project, "grade": r.grade, "score": r.score,
                       "metrics": r.metrics, "recommendations": r.recommendations}, indent=2))


def _search(brain: BrainAPI, args):
    if not args:
        print("Usage: python -m brain search <query> [--type T]"); return
    query = args[0]
    ntype = _flag(args, "--type")
    results = brain.search(query, ntype=ntype)
    print(json.dumps(results, indent=2, default=str))


def _search_code(brain: BrainAPI, args):
    if not args:
        print("Usage: python -m brain search-code <query> [-p project]"); return
    results = brain.search_code(args[0], project=_flag(args, "-p"))
    for r in results:
        r.pop("body", None)
    print(json.dumps(results, indent=2, default=str))


def _context(brain: BrainAPI, args):
    if not args:
        print("Usage: python -m brain context <target> [-b budget] [-c consumer]"); return
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
        print("Usage: python -m brain who-calls <function> [-d depth]"); return
    depth = int(_flag(args, "-d", "5"))
    callers = brain.who_calls(args[0], depth=depth)
    print(json.dumps({"function": args[0], "callers": callers, "count": len(callers)}, indent=2))


def _what_calls(brain: BrainAPI, args):
    if not args:
        print("Usage: python -m brain what-calls <function> [-d depth]"); return
    depth = int(_flag(args, "-d", "5"))
    callees = brain.what_calls(args[0], depth=depth)
    print(json.dumps({"function": args[0], "callees": callees, "count": len(callees)}, indent=2))


def _path(brain: BrainAPI, args):
    if len(args) < 2:
        print("Usage: python -m brain path <source> <target>"); return
    p = brain.path(args[0], args[1])
    print(json.dumps({"source": args[0], "target": args[1], "path": p, "hops": len(p) - 1 if p else -1}, indent=2))


def _impact(brain: BrainAPI, args):
    if not args:
        print("Usage: python -m brain impact <func1,func2,...> [-d depth]"); return
    funcs = args[0].split(",")
    depth = int(_flag(args, "-d", "5"))
    r = brain.impact(funcs, max_depth=depth)
    print(json.dumps({"functions": r.changed_functions, "direct_callers": r.direct_callers,
                       "total_impacted": r.total_impacted, "services": r.impacted_services,
                       "risk_scores": r.risk_scores, "test_gaps_count": len(r.test_gaps),
                       "overall_risk": r.overall_risk}, indent=2))


def _dead_code(brain: BrainAPI, args):
    if not args:
        print("Usage: python -m brain dead-code <project>"); return
    results = brain.dead_code(args[0])
    print(json.dumps({"project": args[0], "count": len(results), "candidates": results[:30]}, indent=2))


def _test_gaps(brain: BrainAPI, args):
    if not args:
        print("Usage: python -m brain test-gaps <project>"); return
    results = brain.test_gap(args[0])
    print(json.dumps({"project": args[0], "count": len(results), "gaps": results[:20]}, indent=2))


def _add_node(brain: BrainAPI, args):
    if len(args) < 2:
        print("Usage: python -m brain add-node <type> <name> [-d JSON] [-p project] [-c confidence]"); return
    data = json.loads(_flag(args, "-d", "{}"))
    project = _flag(args, "-p")
    confidence = float(_flag(args, "-c", "0.7"))
    nid = brain.add_node(args[0], args[1], data=data, project=project, confidence=confidence)
    print(json.dumps({"id": nid, "type": args[0], "name": args[1]}))


def _get_node(brain: BrainAPI, args):
    if len(args) < 2:
        print("Usage: python -m brain get-node <type> <name>"); return
    node = brain.get_node(args[0], args[1])
    print(json.dumps(node or {"error": "not found"}, indent=2, default=str))


def _delete_node(brain: BrainAPI, args):
    if len(args) < 2:
        print("Usage: python -m brain delete-node <type> <name>"); return
    ok = brain.delete_node(args[0], args[1])
    print(json.dumps({"deleted": ok, "type": args[0], "name": args[1]}))


def _add_edge(brain: BrainAPI, args):
    if len(args) < 5:
        print("Usage: python -m brain add-edge <from_type> <from> <to_type> <to> <edge_type>"); return
    brain.add_edge(args[0], args[1], args[2], args[3], args[4])
    print(json.dumps({"ok": True, "edge": f"{args[0]}:{args[1]} --{args[4]}--> {args[2]}:{args[3]}"}))


def _feature_create(brain: BrainAPI, args):
    if not args:
        print("Usage: python -m brain feature-create <name> [--owner O]"); return
    owner = _flag(args, "--owner")
    fid = brain.feature_create(args[0], owner=owner)
    print(json.dumps({"id": fid, "name": args[0], "status": "proposed"}))


def _feature_update(brain: BrainAPI, args):
    if not args:
        print("Usage: python -m brain feature-update <name> --status S"); return
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
        print("Usage: python -m brain feature-health <name>"); return
    r = brain.feature_health(args[0])
    print(json.dumps(r, indent=2))


def _learn_status(brain: BrainAPI):
    print(json.dumps(brain.learn_status(), indent=2, default=str))


def _learn_flush(brain: BrainAPI, args):
    dry = _has_flag(args, "--dry-run")
    result = brain.flush(dry_run=dry)
    print(json.dumps({"dry_run": dry, **result}, indent=2))


def _seed(brain: BrainAPI):
    count = brain.seed_services()
    print(json.dumps({"services_seeded": count}))


def _refresh(brain: BrainAPI):
    t = brain.refresh_graph()
    print(json.dumps({"refreshed": True, "load_time_sec": round(t, 2),
                       "nodes": brain.nxc.node_count, "edges": brain.nxc.edge_count}))


def _migrate(brain: BrainAPI, args):
    if not args:
        print("Usage: python -m brain migrate-rubick <rubick_db_path>"); return
    from brain.migration.migrate import migrate_rubick
    report = migrate_rubick(args[0], brain)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
