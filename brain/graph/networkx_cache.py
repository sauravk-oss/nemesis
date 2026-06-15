"""In-memory NetworkX DiGraph loaded from SQLite edges table.

Loads at startup (~2s for 733K edges, ~100MB RAM).
Provides fast graph algorithms: BFS, PageRank, ancestors, descendants.
Refresh after bulk ingestion.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Set, Tuple

import networkx as nx

from brain.graph.engine import GraphEngine


class NetworkXCache:

    def __init__(self, engine: GraphEngine, edge_types: List[str] = None):
        self._engine = engine
        self._edge_types = edge_types
        self._graph: nx.DiGraph = nx.DiGraph()
        self._pagerank: Dict[str, float] = {}
        self._load_time: float = 0.0
        self.refresh()

    @property
    def graph(self) -> nx.DiGraph:
        return self._graph

    @property
    def node_count(self) -> int:
        return self._graph.number_of_nodes()

    @property
    def edge_count(self) -> int:
        return self._graph.number_of_edges()

    def refresh(self) -> float:
        start = time.monotonic()
        raw = self._engine.load_edges(self._edge_types)
        g = nx.DiGraph()
        for from_name, to_name, etype in raw:
            g.add_edge(from_name, to_name, edge_type=etype)
        self._graph = g
        self._pagerank = {}
        self._load_time = time.monotonic() - start
        return self._load_time

    # --- Traversal ---
    def bfs_tree(self, source: str, depth: int = 5) -> List[str]:
        if source not in self._graph:
            return []
        try:
            tree = nx.bfs_tree(self._graph, source, depth_limit=depth)
            return list(tree.nodes())
        except nx.NetworkXError:
            return []

    def callers(self, target: str, depth: int = 5) -> List[str]:
        if target not in self._graph:
            return []
        try:
            tree = nx.bfs_tree(self._graph.reverse(copy=False), target, depth_limit=depth)
            nodes = list(tree.nodes())
            nodes.remove(target)
            return nodes
        except nx.NetworkXError:
            return []

    def callees(self, source: str, depth: int = 5) -> List[str]:
        if source not in self._graph:
            return []
        try:
            tree = nx.bfs_tree(self._graph, source, depth_limit=depth)
            nodes = list(tree.nodes())
            nodes.remove(source)
            return nodes
        except nx.NetworkXError:
            return []

    def shortest_path(self, source: str, target: str) -> List[str]:
        try:
            return nx.shortest_path(self._graph, source, target)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return []

    def neighbors(self, node: str, direction: str = "both") -> Set[str]:
        if node not in self._graph:
            return set()
        out = set()
        if direction in ("out", "both"):
            out.update(self._graph.successors(node))
        if direction in ("in", "both"):
            out.update(self._graph.predecessors(node))
        return out

    # --- Algorithms ---
    def pagerank(self, force: bool = False) -> Dict[str, float]:
        if not self._pagerank or force:
            if self._graph.number_of_nodes() == 0:
                return {}
            self._pagerank = nx.pagerank(self._graph, max_iter=100)
        return self._pagerank

    def top_pagerank(self, n: int = 20) -> List[Tuple[str, float]]:
        pr = self.pagerank()
        return sorted(pr.items(), key=lambda x: x[1], reverse=True)[:n]

    def connected_components(self) -> List[Set[str]]:
        undirected = self._graph.to_undirected()
        return [c for c in nx.connected_components(undirected) if len(c) > 1]

    def impact_set(self, sources: List[str], max_depth: int = 5) -> Set[str]:
        impacted = set()
        for src in sources:
            impacted.update(self.callees(src, depth=max_depth))
        return impacted

    def blast_radius(self, sources: List[str], max_depth: int = 5) -> Dict[str, int]:
        impacted = self.impact_set(sources, max_depth)
        by_depth: Dict[str, int] = {}
        for src in sources:
            if src not in self._graph:
                continue
            for node in impacted:
                try:
                    d = nx.shortest_path_length(self._graph, src, node)
                    by_depth[node] = min(by_depth.get(node, 999), d)
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    pass
        return by_depth

    def stats(self) -> Dict:
        return {
            "nodes": self.node_count,
            "edges": self.edge_count,
            "load_time_sec": round(self._load_time, 2),
            "weakly_connected_components": nx.number_weakly_connected_components(self._graph) if self.node_count > 0 else 0,
        }
