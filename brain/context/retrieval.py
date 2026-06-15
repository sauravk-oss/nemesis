"""Hybrid context retrieval — graph walk + FTS5 + vector search.

Three-channel retrieval with consumer-specific weights.
Replaces rubick_context.py's BFS-only approach.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from brain.config import EDGE_WEIGHTS, HYBRID_WEIGHTS, TOKENS_PER_NODE
from brain.graph.engine import GraphEngine
from brain.graph.networkx_cache import NetworkXCache
from brain.types import ContextResult


class ContextRetriever:

    def __init__(self, engine: GraphEngine, nxc: NetworkXCache,
                 vector_search_fn=None):
        self._engine = engine
        self._nxc = nxc
        self._vector_search = vector_search_fn

    def context_for(self, target: str, budget: int = 4000,
                    consumer: str = "default", depth: int = 3,
                    project: str = None) -> ContextResult:
        result = ContextResult(target=target, budget=budget)
        weights = HYBRID_WEIGHTS.get(consumer, HYBRID_WEIGHTS["default"])

        graph_budget = int(budget * weights["graph"])
        fts_budget = int(budget * weights["fts5"])
        vector_budget = int(budget * weights["vector"])

        scored: List[Dict[str, Any]] = []

        # Channel 1: Graph walk (NetworkX BFS)
        graph_nodes = self._graph_walk(target, depth, project)
        result.graph_nodes = len(graph_nodes)
        for node in graph_nodes:
            scored.append({**node, "channel": "graph"})

        # Channel 2: FTS5 keyword search
        fts_nodes = self._fts_search(target, project)
        result.fts_hits = len(fts_nodes)
        for node in fts_nodes:
            if not any(s["name"] == node["name"] for s in scored):
                scored.append({**node, "channel": "fts5"})

        # Channel 3: Vector search (lazy, only if available)
        if self._vector_search and vector_budget > 0:
            vec_nodes = self._vector_search(target, limit=10, project=project)
            result.vector_hits = len(vec_nodes)
            for node in vec_nodes:
                if not any(s["name"] == node["name"] for s in scored):
                    scored.append({**node, "channel": "vector"})

        scored.sort(key=lambda x: x.get("score", 0), reverse=True)

        # Serialize within budget
        lines = []
        tokens_used = 0
        for node in scored:
            text = self._serialize_node(node)
            est_tokens = len(text.split()) + 10
            if tokens_used + est_tokens > budget:
                break
            lines.append(text)
            tokens_used += est_tokens
            result.sources.append(f"{node.get('type', '?')}:{node['name']}")

        result.text = "\n\n".join(lines)
        result.tokens_used = tokens_used
        return result

    def _graph_walk(self, target: str, depth: int, project: str = None) -> List[Dict]:
        seed = self._resolve_target(target, project)
        if not seed:
            return []

        bfs_nodes = self._nxc.bfs_tree(seed, depth=depth)
        results = []
        for qname in bfs_nodes[:100]:
            fn = self._engine.get_function(qname)
            if fn:
                score = self._score_node(fn, seed)
                results.append({"name": qname, "type": "Function", "score": score, **fn})
                continue
            node = self._find_generic_node(qname)
            if node:
                score = self._score_node(node, seed)
                results.append({"name": qname, "type": node.get("type", "?"), "score": score, **node})

        results.sort(key=lambda x: x["score"], reverse=True)
        return results

    def _fts_search(self, query: str, project: str = None) -> List[Dict]:
        results = []
        fts_query = query.replace(".", " ").replace("::", " ")

        node_hits = self._engine.search_nodes_fts(fts_query, limit=10)
        for n in node_hits:
            results.append({"name": n["name"], "type": n["type"], "score": 0.5, **n})

        code_hits = self._engine.search_code_fts(fts_query, project=project, limit=10)
        for c in code_hits:
            results.append({"name": c["node_id"], "type": "CodeBody", "score": 0.4,
                            "body_preview": c.get("body", "")[:200]})
        return results

    def _resolve_target(self, target: str, project: str = None) -> Optional[str]:
        fn = self._engine.get_function(target)
        if fn:
            return target

        funcs = self._engine.find_functions(name_like=target, limit=1)
        if funcs:
            return funcs[0]["qname"]

        svc = self._engine.get_service(target)
        if svc:
            return target

        node_hits = self._engine.search_nodes_fts(target, limit=1)
        if node_hits:
            return node_hits[0]["name"]

        return target

    def _find_generic_node(self, name: str) -> Optional[Dict]:
        r = self._engine.conn.execute(
            "SELECT * FROM nodes WHERE name=? LIMIT 1", (name,)).fetchone()
        if r:
            d = dict(r)
            d["data"] = json.loads(d.get("data") or "{}")
            return d
        return None

    def _score_node(self, node: Dict, seed: str) -> float:
        score = 0.5
        confidence = node.get("confidence", 0.7)
        score *= (0.5 + confidence * 0.5)

        path = self._nxc.shortest_path(seed, node.get("qname", node.get("name", "")))
        if path:
            distance = len(path) - 1
            score *= max(0.2, 1.0 - distance * 0.15)

        pr = self._nxc.pagerank()
        node_name = node.get("qname", node.get("name", ""))
        if node_name in pr:
            score += pr[node_name] * 1000

        return round(score, 4)

    def _serialize_node(self, node: Dict) -> str:
        ntype = node.get("type", "?")
        name = node.get("name", "?")
        parts = [f"## [{ntype}] {name}"]

        if ntype == "Function":
            if node.get("signature"):
                parts.append(f"Signature: {node['signature']}")
            if node.get("file_path"):
                parts.append(f"File: {node['file_path']}:{node.get('line_start', '?')}")
            if node.get("project"):
                parts.append(f"Project: {node['project']}")
            body = self._engine.get_code_body(name)
            if body:
                parts.append(f"```\n{body[:500]}\n```")
        elif ntype == "CodeBody":
            if node.get("body_preview"):
                parts.append(f"```\n{node['body_preview']}\n```")
        else:
            data = node.get("data", {})
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except (json.JSONDecodeError, TypeError):
                    data = {}
            for k, v in data.items():
                if v and k not in ("raw_metadata", "raw_content"):
                    parts.append(f"{k}: {v}")

        conf = node.get("confidence")
        if conf:
            parts.append(f"Confidence: {conf}")

        return "\n".join(parts)
