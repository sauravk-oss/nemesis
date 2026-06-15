#!/usr/bin/env python3
"""Rubick Context Retrieval Engine.

Token-efficient graph-to-text with budget-aware traversal.
Core API: context_for(feature_slug, budget=N) returns a relevance-scored
summary within the token budget.

Design: "Math is Code, Meaning is LLM" — this module handles the deterministic
scoring, BFS traversal, and budget truncation. The LLM interprets the results.
"""

import json
import sys
import os
import argparse
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import brain_config as cfg
    from rubick_graph import (
        get_db, query_nodes, search_text, get_node, get_neighbors,
        find_cross_refs, _parse_iso, _node_data, feature_timeline,
    )
except ImportError as e:
    print(f"Import error: {e}. Run from nemesis_v2/scripts/ directory.", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_BUDGET_DEFAULT = cfg.CONTEXT_BUDGET_DEFAULT if cfg else 2000
_BUDGET_PLANNER = cfg.CONTEXT_BUDGET_PLANNER if cfg else 1500
_BUDGET_ARCH = cfg.CONTEXT_BUDGET_ARCH_INIT if cfg else 4000
_BUDGET_DEV = cfg.CONTEXT_BUDGET_DEV if cfg else 3000
_TOKENS_PER_NODE = cfg.TOKENS_PER_NODE_ESTIMATE if cfg else 80
_EDGE_RELEVANCE = cfg.EDGE_RELEVANCE if cfg else {}
_RECENCY_BOOST_DAYS = cfg.RECENCY_BOOST_DAYS if cfg else 7
_RECENCY_BOOST_SCORE = cfg.RECENCY_BOOST_SCORE if cfg else 0.2
_URGENCY_BOOST_THRESHOLD = cfg.URGENCY_BOOST_THRESHOLD if cfg else 0.7
_URGENCY_BOOST_SCORE = cfg.URGENCY_BOOST_SCORE if cfg else 0.3


# ---------------------------------------------------------------------------
# Token Estimation
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    """Rough token count: ~4 chars per token for English text."""
    return max(1, len(text) // 4)


def _node_to_text(node: dict, compact: bool = False) -> str:
    """Serialize a graph node to a compact text representation."""
    ntype = node.get("type", "?")
    name = node.get("name", "?")
    data = _node_data(node)

    if compact:
        summary = data.get("content_summary") or data.get("description") or data.get("title") or ""
        if summary:
            return f"[{ntype}] {name}: {summary[:120]}"
        return f"[{ntype}] {name}"

    parts = [f"## [{ntype}] {name}"]

    skip_keys = {"_archived", "_archived_at", "raw_metadata", "raw_content",
                 "diff", "schedule_json", "conflicts_json", "circular_deps_json",
                 "body", "diff_summary"}

    for k, v in data.items():
        if k in skip_keys or v is None or v == "" or v == []:
            continue
        if isinstance(v, str) and len(v) > 200:
            v = v[:200] + "..."
        parts.append(f"- {k}: {v}")

    if node.get("source_type"):
        parts.append(f"- _source: {node['source_type']}")
    if node.get("confidence") and node["confidence"] < 1.0:
        parts.append(f"- _confidence: {node['confidence']}")

    return "\n".join(parts)


def _edge_to_text(edge_type: str, neighbor: dict, compact: bool = False) -> str:
    """Serialize an edge + neighbor to text."""
    ntype = neighbor.get("type", "?")
    name = neighbor.get("name", "?")
    if compact:
        return f"  → {edge_type} → [{ntype}] {name}"
    data = _node_data(neighbor)
    summary = data.get("content_summary") or data.get("description") or data.get("title") or ""
    if summary:
        return f"  → {edge_type} → [{ntype}] {name}: {summary[:100]}"
    return f"  → {edge_type} → [{ntype}] {name}"


# ---------------------------------------------------------------------------
# Relevance Scoring
# ---------------------------------------------------------------------------

def _score_edge(edge_type: str) -> float:
    """Score an edge by its type relevance."""
    return _EDGE_RELEVANCE.get(edge_type, 0.3)


def _score_recency(updated_at: Optional[str]) -> float:
    """Boost score for recently modified nodes."""
    if not updated_at:
        return 0.0
    dt = _parse_iso(updated_at)
    if not dt:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    if age_days <= _RECENCY_BOOST_DAYS:
        return _RECENCY_BOOST_SCORE
    return 0.0


def _score_urgency(node: dict) -> float:
    """Boost score for urgent nodes."""
    data = _node_data(node)
    urgency = float(data.get("urgency_score", 0.0))
    if urgency >= _URGENCY_BOOST_THRESHOLD:
        return _URGENCY_BOOST_SCORE
    return 0.0


def _score_confidence(neighbor: dict) -> float:
    """Boost/penalize based on confidence and age-based decay."""
    confidence = float(neighbor.get("confidence", 1.0))
    if confidence >= 0.85:
        return 0.1
    if confidence < 0.5:
        return -0.1

    # Decay: unvalidated nodes (< 0.85) older than LEARNING_CONFIDENCE_DECAY_DAYS
    updated = neighbor.get("updated_at")
    if updated and confidence < 0.85:
        try:
            decay_days = getattr(cfg, "LEARNING_CONFIDENCE_DECAY_DAYS", 90) if cfg else 90
            decay_factor = getattr(cfg, "LEARNING_CONFIDENCE_DECAY_FACTOR", 0.1) if cfg else 0.1
            updated_dt = datetime.strptime(updated[:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            age = (datetime.now(timezone.utc) - updated_dt).days
            if age > decay_days:
                return -decay_factor
        except (ValueError, TypeError):
            pass
    return 0.0


def _relevance_score(edge_type: str, neighbor: dict) -> float:
    """Combined relevance score for a neighbor node."""
    base = _score_edge(edge_type)
    recency = _score_recency(neighbor.get("updated_at"))
    urgency = _score_urgency(neighbor)
    conf = _score_confidence(neighbor)
    return min(base + recency + urgency + conf, 1.5)


# ---------------------------------------------------------------------------
# Budget-Aware BFS Traversal
# ---------------------------------------------------------------------------

def _bfs_collect(conn, seed_type: str, seed_name: str,
                 budget: int, depth: int = 3,
                 compact: bool = False) -> tuple[list[dict], int, bool]:
    """Priority-queue BFS from seed node, collecting neighbors by relevance.

    Returns (collected, leftover_count, budget_enforced).
    """
    import heapq

    hard_cap_ratio = 1.1
    if cfg and hasattr(cfg, "BUDGET_HARD_CAP_RATIO"):
        hard_cap_ratio = cfg.BUDGET_HARD_CAP_RATIO
    hard_cap = int(budget * hard_cap_ratio)

    seed = get_node(conn, seed_type, seed_name)
    if not seed:
        return [], 0, False

    seed_text = _node_to_text(seed, compact=compact)
    seed_tokens = _estimate_tokens(seed_text)
    remaining_budget = budget - seed_tokens
    tokens_used = seed_tokens

    visited = {(seed_type, seed_name)}
    heap: list[tuple[float, int, dict, int]] = []
    _counter = 0

    neighbors = get_neighbors(conn, seed_type, seed_name)
    for n in neighbors:
        key = (n["type"], n["name"])
        if key not in visited:
            score = _relevance_score(n["edge_type"], n)
            heapq.heappush(heap, (-score, _counter, n, 1))
            _counter += 1
            visited.add(key)

    collected: list[dict] = []
    budget_enforced = False
    while heap and remaining_budget > 0:
        if tokens_used >= hard_cap:
            budget_enforced = True
            break

        neg_score, _, node, current_depth = heapq.heappop(heap)
        score = -neg_score

        text = _edge_to_text(node["edge_type"], node, compact=compact)
        tokens = _estimate_tokens(text)

        if tokens_used + tokens > hard_cap:
            budget_enforced = True
            break

        if tokens <= remaining_budget:
            collected.append({
                "type": node["type"], "name": node["name"],
                "edge_type": node["edge_type"],
                "score": round(score, 4), "text": text, "tokens": tokens,
            })
            remaining_budget -= tokens
            tokens_used += tokens

            if current_depth < depth:
                sub_neighbors = get_neighbors(conn, node["type"], node["name"])
                for sn in sub_neighbors:
                    key = (sn["type"], sn["name"])
                    if key not in visited:
                        sub_score = score * 0.7 * _score_edge(sn["edge_type"])
                        heapq.heappush(heap, (-sub_score, _counter, sn, current_depth + 1))
                        _counter += 1
                        visited.add(key)

    collected.sort(key=lambda x: x["score"], reverse=True)
    return collected, len(heap), budget_enforced


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def context_for(conn, target: str, budget: int = 0,
                consumer: str = "default",
                depth: int = 3) -> dict:
    """Retrieve token-budgeted context for a target (feature slug, task name, or search query).

    Args:
        conn: Database connection
        target: Feature name, task name, node "Type:Name", or free-text query
        budget: Token budget (0 = use consumer default)
        consumer: "planner", "arch", "dev", "user", or "default"
        depth: BFS traversal depth

    Returns:
        Dict with header, body (text), nodes_included, tokens_used, budget
    """
    if budget <= 0:
        budget = {
            "planner": _BUDGET_PLANNER,
            "arch": _BUDGET_ARCH,
            "dev": _BUDGET_DEV,
            "user": _BUDGET_DEFAULT,
        }.get(consumer, _BUDGET_DEFAULT)

    # Resolve target to a seed node
    seed_type, seed_name = _resolve_target(conn, target)
    if not seed_type:
        return {
            "error": f"target not found: {target}",
            "header": f"No context found for '{target}'",
            "body": "", "nodes_included": 0, "tokens_used": 0, "budget": budget,
        }

    seed = get_node(conn, seed_type, seed_name)
    seed_text = _node_to_text(seed, compact=False)
    seed_tokens = _estimate_tokens(seed_text)

    collected, leftover_count, budget_enforced = _bfs_collect(conn, seed_type, seed_name, budget, depth=depth)

    body_parts = [seed_text]
    if collected:
        body_parts.append("\n### Related")
        for item in collected:
            body_parts.append(item["text"])

    body = "\n".join(body_parts)
    tokens_used = seed_tokens + sum(c["tokens"] for c in collected)

    truncated = leftover_count > 0 or budget_enforced
    nodes_dropped = leftover_count if truncated else 0

    result = {
        "header": f"Context for {seed_type}:{seed_name} ({tokens_used}/{budget} tokens)",
        "body": body,
        "seed": {"type": seed_type, "name": seed_name},
        "nodes_included": 1 + len(collected),
        "tokens_used": tokens_used,
        "budget": budget,
        "consumer": consumer,
        "truncated": truncated,
        "nodes_dropped": nodes_dropped,
        "budget_enforced": budget_enforced,
    }

    if truncated:
        result["truncation_warning"] = (
            f"Budget exhausted: {nodes_dropped} candidate nodes were dropped. "
            f"Increase budget from {budget} to include more context."
        )

    return result


def context_for_v2(conn, target: str, budget: int = 0,
                    consumer: str = "default", depth: int = 3,
                    mode: str = "auto", include_code: bool = True) -> dict:
    """Hybrid retrieval: vector + BFS + FTS5 with provenance chain.

    Modes: "auto" (all sources), "graph" (BFS only), "semantic" (vector only), "keyword" (FTS5 only).
    """
    if budget <= 0:
        budget = {
            "planner": _BUDGET_PLANNER,
            "arch": _BUDGET_ARCH,
            "dev": _BUDGET_DEV,
            "user": _BUDGET_DEFAULT,
        }.get(consumer, _BUDGET_DEFAULT)

    # Consumer-specific weights
    weights = {"vector": 0.4, "bfs": 0.35, "fts5": 0.25}
    if cfg and hasattr(cfg, "HYBRID_CONSUMER_WEIGHTS"):
        weights = cfg.HYBRID_CONSUMER_WEIGHTS.get(consumer, weights)

    seed_type, seed_name = _resolve_target(conn, target)

    # Collect results from each source
    node_scores: dict[tuple[str, str], dict] = {}  # (type, name) -> {scores, metadata}
    retrieval_sources = {"vector": 0, "bfs": 0, "fts5": 0}
    provenance_chain = []

    bfs_budget_enforced = False

    # Source 1: BFS graph walk
    if mode in ("auto", "graph"):
        if seed_type:
            collected, leftover, bfs_budget_enforced = _bfs_collect(conn, seed_type, seed_name, budget, depth=depth)
            for item in collected:
                key = (item["type"], item["name"])
                if key not in node_scores:
                    node_scores[key] = {"text": item["text"], "tokens": item["tokens"],
                                        "scores": {}, "node": item}
                node_scores[key]["scores"]["bfs"] = item["score"]
                retrieval_sources["bfs"] += 1

    # Source 2: Vector search
    if mode in ("auto", "semantic"):
        try:
            from rubick_vectors import vector_search
            v_results = vector_search(target, limit=20)
            if v_results:
                max_v_score = max(r["score"] for r in v_results)
                for r in v_results:
                    key = (r.get("node_type", "Function"), r.get("node_name", ""))
                    normalized = r["score"] / max_v_score if max_v_score > 0 else 0
                    if key not in node_scores:
                        text = f"  → [VECTOR] {r.get('node_type')}:{r.get('node_name')} @ {r.get('file_path', '')}:{r.get('line_number', 0)}"
                        node_scores[key] = {"text": text, "tokens": _estimate_tokens(text),
                                            "scores": {}, "node": r}
                    node_scores[key]["scores"]["vector"] = normalized
                    retrieval_sources["vector"] += 1

                    provenance_chain.append({
                        "node": f"{r.get('node_type')}:{r.get('node_name')}",
                        "repo": f"razorpay/{r.get('project_slug', '')}",
                        "file": r.get("file_path", ""),
                        "line": r.get("line_number", 0),
                        "commit_sha": r.get("commit_sha", ""),
                        "verified": False,
                    })
        except (ImportError, Exception):
            pass  # Qdrant unavailable — graceful degradation

    # Source 3: FTS5 keyword search
    if mode in ("auto", "keyword"):
        fts_hits = search_text(conn, target, limit=20)
        if fts_hits:
            for i, h in enumerate(fts_hits):
                key = (h["type"], h["name"])
                fts_score = 1.0 - (i / len(fts_hits))  # rank-based normalization
                if key not in node_scores:
                    text = _node_to_text(h, compact=True)
                    node_scores[key] = {"text": text, "tokens": _estimate_tokens(text),
                                        "scores": {}, "node": h}
                node_scores[key]["scores"]["fts5"] = fts_score
                retrieval_sources["fts5"] += 1

    # Score fusion
    active_weights = {}
    if mode == "graph":
        active_weights = {"bfs": 1.0}
    elif mode == "semantic":
        active_weights = {"vector": 1.0}
    elif mode == "keyword":
        active_weights = {"fts5": 1.0}
    else:
        total_w = sum(weights[k] for k in weights if retrieval_sources.get(k, 0) > 0)
        if total_w > 0:
            active_weights = {k: weights[k] / total_w for k in weights if retrieval_sources.get(k, 0) > 0}
        else:
            active_weights = weights

    ranked = []
    for key, data in node_scores.items():
        fused = sum(active_weights.get(src, 0) * score for src, score in data["scores"].items())
        ranked.append({**data, "fused_score": fused, "key": key})

    ranked.sort(key=lambda x: x["fused_score"], reverse=True)

    # Budget-aware truncation + code enrichment
    hard_cap_ratio = 1.1
    if cfg and hasattr(cfg, "BUDGET_HARD_CAP_RATIO"):
        hard_cap_ratio = cfg.BUDGET_HARD_CAP_RATIO
    hard_cap = int(budget * hard_cap_ratio)

    body_parts = []
    if seed_type:
        seed = get_node(conn, seed_type, seed_name)
        if seed:
            body_parts.append(_node_to_text(seed, compact=False))

    tokens_used = sum(_estimate_tokens(p) for p in body_parts)
    included = 0
    code_snippets = 0
    v2_budget_enforced = bfs_budget_enforced

    for item in ranked:
        t = item["tokens"]
        if tokens_used + t > hard_cap:
            v2_budget_enforced = True
            break
        if tokens_used + t > budget:
            break
        body_parts.append(item["text"])
        tokens_used += t
        included += 1

        if include_code and item["key"][0] in ("Function", "Class", "Test"):
            try:
                body_row = conn.execute(
                    "SELECT body, file_path, start_line FROM code_bodies cb "
                    "JOIN nodes n ON cb.node_id = n.id "
                    "WHERE n.type = ? AND n.name = ? LIMIT 1",
                    (item["key"][0], item["key"][1])
                ).fetchone()
                if body_row:
                    code_text = body_row["body"][:2000]
                    code_tokens = _estimate_tokens(code_text)
                    if tokens_used + code_tokens + 10 <= hard_cap:
                        source_comment = f"// source: {body_row['file_path']}:{body_row['start_line']}"
                        body_parts.append(f"```\n{source_comment}\n{code_text}\n```")
                        tokens_used += code_tokens + 10
                        code_snippets += 1
            except Exception:
                pass

    body = "\n".join(body_parts)

    # Verify provenance
    stale_count = 0
    drift_count = 0
    try:
        from rubick_vectors import verify_batch
        verified = verify_batch(provenance_chain)
        for p in verified:
            if not p["verified"]:
                if any("file_missing" in w or "line_mismatch" in w for w in p.get("warnings", [])):
                    stale_count += 1
                if any("commit_drift" in w for w in p.get("warnings", [])):
                    drift_count += 1
        provenance_chain = verified
    except (ImportError, Exception):
        pass

    return {
        "header": f"Context for {target} ({tokens_used}/{budget} tokens, mode={mode})",
        "body": body,
        "seed": {"type": seed_type, "name": seed_name} if seed_type else None,
        "nodes_included": included,
        "tokens_used": tokens_used,
        "budget": budget,
        "budget_enforced": v2_budget_enforced,
        "consumer": consumer,
        "mode": mode,
        "retrieval_sources": retrieval_sources,
        "provenance_chain": provenance_chain,
        "stale_count": stale_count,
        "drift_count": drift_count,
        "code_snippets_included": code_snippets,
        "truncated": included < len(ranked),
        "nodes_dropped": len(ranked) - included,
    }


def _resolve_target(conn, target: str) -> tuple[Optional[str], Optional[str]]:
    """Resolve a target string to (node_type, node_name).

    Supports:
    - "Type:Name" explicit format
    - Feature name/slug lookup
    - Task name lookup
    - Free-text search fallback
    """
    if ":" in target and not target.startswith("http"):
        parts = target.split(":", 1)
        node = get_node(conn, parts[0], parts[1])
        if node:
            return parts[0], parts[1]

    for ntype in ("Feature", "Task", "JiraIssue", "PR", "Decision"):
        node = get_node(conn, ntype, target)
        if node:
            return ntype, target

    hits = search_text(conn, target, limit=1)
    if hits:
        return hits[0]["type"], hits[0]["name"]

    return None, None


def recall(conn, query: str, budget: int = 0,
           ntype: Optional[str] = None,
           project_slug: Optional[str] = None) -> dict:
    """Search memory for a query. Returns matching nodes within budget.

    This is the "what do I know about X?" function.
    """
    if budget <= 0:
        budget = _BUDGET_DEFAULT

    hits = search_text(conn, query, limit=50, ntype=ntype)

    if project_slug:
        hits = [h for h in hits if _node_data(h).get("project_slug") == project_slug]

    results = []
    tokens_used = 0
    for h in hits:
        text = _node_to_text(h, compact=True)
        tokens = _estimate_tokens(text)
        if tokens_used + tokens > budget:
            break
        results.append({
            "type": h["type"], "name": h["name"],
            "text": text, "tokens": tokens,
            "source_type": h.get("source_type", ""),
            "confidence": h.get("confidence", 1.0),
        })
        tokens_used += tokens

    body = "\n".join(r["text"] for r in results)
    return {
        "header": f"Recall: '{query}' ({len(results)} results, {tokens_used}/{budget} tokens)",
        "body": body,
        "results": results,
        "tokens_used": tokens_used,
        "budget": budget,
    }


def timeline(conn, target: str, days: int = 30,
             budget: int = 0) -> dict:
    """Build a chronological timeline of events related to a target.

    Covers: signals, tasks, decisions, commits, PRs linked to the target.
    """
    if budget <= 0:
        budget = _BUDGET_DEFAULT

    seed_type, seed_name = _resolve_target(conn, target)
    if not seed_type:
        return {"error": f"target not found: {target}", "events": []}

    if seed_type == "Feature":
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        ft = feature_timeline(conn, seed_name, since=since)
        if "error" in ft:
            return ft

        body_parts = [f"# Timeline: {seed_name} (last {days} days)"]
        tokens_used = _estimate_tokens(body_parts[0])

        for e in ft["events"]:
            line = f"- [{e['timestamp'][:10]}] {e['kind']}: {e.get('name') or e.get('title') or e.get('summary', '')}"
            tokens = _estimate_tokens(line)
            if tokens_used + tokens > budget:
                body_parts.append(f"... ({ft['event_count'] - len(body_parts) + 1} more events truncated)")
                break
            body_parts.append(line)
            tokens_used += tokens

        return {
            "header": f"Timeline for {seed_name} ({tokens_used}/{budget} tokens)",
            "body": "\n".join(body_parts),
            "event_count": ft["event_count"],
            "tokens_used": tokens_used,
            "budget": budget,
        }

    neighbors = get_neighbors(conn, seed_type, seed_name)
    events = []
    for n in neighbors:
        data = _node_data(n)
        ts = (data.get("timestamp") or data.get("date") or
              data.get("decided_at") or data.get("created_at") or
              n.get("updated_at"))
        if ts:
            events.append({
                "timestamp": ts,
                "type": n["type"],
                "name": n["name"],
                "edge": n["edge_type"],
                "summary": data.get("content_summary") or data.get("title") or data.get("subject") or "",
            })

    events.sort(key=lambda e: e["timestamp"])

    body_parts = [f"# Timeline: {seed_type}:{seed_name}"]
    tokens_used = _estimate_tokens(body_parts[0])
    for e in events:
        line = f"- [{e['timestamp'][:10]}] {e['type']}: {e['summary'][:80]}"
        tokens = _estimate_tokens(line)
        if tokens_used + tokens > budget:
            break
        body_parts.append(line)
        tokens_used += tokens

    return {
        "header": f"Timeline for {seed_type}:{seed_name} ({tokens_used}/{budget} tokens)",
        "body": "\n".join(body_parts),
        "event_count": len(events),
        "tokens_used": tokens_used,
        "budget": budget,
    }


def status(conn, project_slug: Optional[str] = None,
           budget: int = 0) -> dict:
    """Generate a project status summary: active features, tasks, recent signals."""
    if budget <= 0:
        budget = _BUDGET_DEFAULT

    parts = ["# Status Summary"]
    tokens_used = _estimate_tokens(parts[0])

    features = query_nodes(conn, ntype="Feature", limit=20, project_slug=project_slug)
    active_features = [f for f in features if _node_data(f).get("status") in ("in_progress", "blocked")]
    if active_features:
        parts.append(f"\n## Active Features ({len(active_features)})")
        for f in active_features:
            d = _node_data(f)
            line = f"- **{f['name']}** [{d.get('status')}] owner={d.get('owner', '?')} priority={d.get('priority', '?')}"
            tokens = _estimate_tokens(line)
            if tokens_used + tokens > budget:
                break
            parts.append(line)
            tokens_used += tokens

    tasks = query_nodes(conn, ntype="Task", limit=10, project_slug=project_slug)
    open_tasks = [t for t in tasks if _node_data(t).get("status") not in ("done", "completed")]
    if open_tasks and tokens_used < budget - 100:
        parts.append(f"\n## Open Tasks ({len(open_tasks)})")
        for t in open_tasks[:5]:
            d = _node_data(t)
            line = f"- {t['name']} [{d.get('status', 'open')}] due={d.get('due_date', '?')}"
            tokens = _estimate_tokens(line)
            if tokens_used + tokens > budget:
                break
            parts.append(line)
            tokens_used += tokens

    signals = query_nodes(conn, ntype="Signal", limit=5, project_slug=project_slug)
    if signals and tokens_used < budget - 100:
        parts.append(f"\n## Recent Signals")
        for s in signals[:3]:
            d = _node_data(s)
            line = f"- [{d.get('signal_type', '?')}] {d.get('content_summary', s['name'][:60])}"
            tokens = _estimate_tokens(line)
            if tokens_used + tokens > budget:
                break
            parts.append(line)
            tokens_used += tokens

    return {
        "header": f"Status ({tokens_used}/{budget} tokens)",
        "body": "\n".join(parts),
        "tokens_used": tokens_used,
        "budget": budget,
        "features_active": len(active_features),
        "tasks_open": len(open_tasks),
    }


def decisions(conn, target: Optional[str] = None,
              project_slug: Optional[str] = None,
              budget: int = 0) -> dict:
    """Retrieve decision history for a target or project."""
    if budget <= 0:
        budget = _BUDGET_DEFAULT

    if target:
        seed_type, seed_name = _resolve_target(conn, target)
        if seed_type:
            nodes = get_neighbors(conn, seed_type, seed_name, edge_type="DECIDED_BY")
            decision_nodes = [n for n in nodes if n["type"] == "Decision"]
        else:
            decision_nodes = search_text(conn, target, limit=20, ntype="Decision")
    else:
        decision_nodes = query_nodes(conn, ntype="Decision", limit=50, project_slug=project_slug)

    parts = [f"# Decisions{' for ' + target if target else ''}"]
    tokens_used = _estimate_tokens(parts[0])

    for d in decision_nodes:
        data = _node_data(d)
        line = (f"- **{d['name']}** [{data.get('decided_at', '?')[:10]}] "
                f"by {data.get('decided_by', '?')}: {data.get('outcome', '?')[:100]}")
        tokens = _estimate_tokens(line)
        if tokens_used + tokens > budget:
            break
        parts.append(line)
        tokens_used += tokens

    return {
        "header": f"Decisions ({len(decision_nodes)} found, {tokens_used}/{budget} tokens)",
        "body": "\n".join(parts),
        "count": len(decision_nodes),
        "tokens_used": tokens_used,
        "budget": budget,
    }


def people(conn, target: Optional[str] = None,
           project_slug: Optional[str] = None,
           budget: int = 0) -> dict:
    """List people related to a target or project."""
    if budget <= 0:
        budget = _BUDGET_DEFAULT

    if target:
        seed_type, seed_name = _resolve_target(conn, target)
        if seed_type:
            nodes = get_neighbors(conn, seed_type, seed_name)
            person_nodes = [n for n in nodes if n["type"] == "Person"]
        else:
            person_nodes = search_text(conn, target, limit=20, ntype="Person")
    else:
        person_nodes = query_nodes(conn, ntype="Person", limit=50, project_slug=project_slug)

    parts = [f"# People{' related to ' + target if target else ''}"]
    tokens_used = _estimate_tokens(parts[0])

    for p in person_nodes:
        data = _node_data(p)
        line = f"- {p['name']} ({data.get('email', '?')}) role={data.get('role', '?')}"
        tokens = _estimate_tokens(line)
        if tokens_used + tokens > budget:
            break
        parts.append(line)
        tokens_used += tokens

    return {
        "header": f"People ({len(person_nodes)} found, {tokens_used}/{budget} tokens)",
        "body": "\n".join(parts),
        "count": len(person_nodes),
        "tokens_used": tokens_used,
        "budget": budget,
    }


def cross_refs(conn, target: str, budget: int = 0) -> dict:
    """Find cross-project references for a target."""
    if budget <= 0:
        budget = _BUDGET_DEFAULT

    seed_type, seed_name = _resolve_target(conn, target)
    source_project = None
    if seed_type:
        seed = get_node(conn, seed_type, seed_name)
        if seed:
            source_project = _node_data(seed).get("project_slug")

    refs = find_cross_refs(conn, target, exclude_project=source_project)

    parts = [f"# Cross-Project References for '{target}'"]
    tokens_used = _estimate_tokens(parts[0])

    for r in refs:
        line = f"- [{r['type']}] {r['name']} (project={r['project_slug'] or 'global'}, confidence={r['confidence']})"
        tokens = _estimate_tokens(line)
        if tokens_used + tokens > budget:
            break
        parts.append(line)
        tokens_used += tokens

    return {
        "header": f"Cross-refs ({len(refs)} found, {tokens_used}/{budget} tokens)",
        "body": "\n".join(parts),
        "count": len(refs),
        "tokens_used": tokens_used,
        "budget": budget,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Rubick Context Retrieval")
    parser.add_argument("command", choices=[
        "context-for", "context-for-v2", "recall", "timeline", "status",
        "decisions", "people", "cross-refs",
    ])
    parser.add_argument("--db", default=None,
                        help="Path to rubick.db (default: from brain_config)")
    parser.add_argument("--target", default=None, help="Target feature/task/query")
    parser.add_argument("--query", default=None, help="Search query (for recall)")
    parser.add_argument("--budget", type=int, default=0, help="Token budget")
    parser.add_argument("--consumer", default="default",
                        choices=["planner", "arch", "dev", "user", "default"])
    parser.add_argument("--depth", type=int, default=3, help="BFS depth")
    parser.add_argument("--days", type=int, default=30, help="Timeline lookback days")
    parser.add_argument("--project", default=None, help="Filter by project slug")
    parser.add_argument("--type", default=None, help="Filter by node type")
    parser.add_argument("--body-only", action="store_true", help="Print body text only")
    parser.add_argument("--mode", default="auto", choices=["auto", "graph", "semantic", "keyword"],
                        help="Retrieval mode for context-for-v2")
    parser.add_argument("--include-code", action="store_true", default=True,
                        help="Include code bodies in context-for-v2")

    args = parser.parse_args()

    db_path = args.db or (str(cfg.RUBICK_DB_PATH) if cfg else "workspace/rubick.db")
    conn = get_db(db_path)

    try:
        if args.command == "context-for":
            target = args.target or args.query
            if not target:
                print(json.dumps({"error": "provide --target"}))
                sys.exit(1)
            result = context_for(conn, target, budget=args.budget,
                                 consumer=args.consumer, depth=args.depth)

        elif args.command == "context-for-v2":
            target = args.target or args.query
            if not target:
                print(json.dumps({"error": "provide --target"}))
                sys.exit(1)
            result = context_for_v2(conn, target, budget=args.budget,
                                    consumer=args.consumer, depth=args.depth,
                                    mode=args.mode, include_code=args.include_code)

        elif args.command == "recall":
            query = args.query or args.target
            if not query:
                print(json.dumps({"error": "provide --query"}))
                sys.exit(1)
            result = recall(conn, query, budget=args.budget,
                            ntype=args.type, project_slug=args.project)

        elif args.command == "timeline":
            target = args.target or args.query
            if not target:
                print(json.dumps({"error": "provide --target"}))
                sys.exit(1)
            result = timeline(conn, target, days=args.days, budget=args.budget)

        elif args.command == "status":
            result = status(conn, project_slug=args.project, budget=args.budget)

        elif args.command == "decisions":
            result = decisions(conn, target=args.target,
                               project_slug=args.project, budget=args.budget)

        elif args.command == "people":
            result = people(conn, target=args.target,
                            project_slug=args.project, budget=args.budget)

        elif args.command == "cross-refs":
            target = args.target or args.query
            if not target:
                print(json.dumps({"error": "provide --target"}))
                sys.exit(1)
            result = cross_refs(conn, target, budget=args.budget)

        else:
            result = {"error": f"unknown command: {args.command}"}

        if args.body_only and "body" in result:
            print(result["body"])
        else:
            print(json.dumps(result, indent=2, default=str))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
