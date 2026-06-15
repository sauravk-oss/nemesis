"""UnifiedBrainAPI — single entry point for all brain operations.

Wires GraphEngine + NetworkXCache + ContextRetriever + MemoryEngine.
LanceDB vector search is lazy-loaded on first semantic query.

Usage:
    from brain.api import BrainAPI
    brain = BrainAPI()
    ctx = brain.context_for("CreateMandate", budget=4000, consumer="arch")
    impact = brain.impact(["svc.CreateMandate"])
    brain.learn(skill="nemesis", items=[...])
    brain.flush()
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from brain.config import BrainConfig, SEED_PROJECTS, SERVICE_DEPS
from brain.graph.engine import GraphEngine
from brain.graph.networkx_cache import NetworkXCache
from brain.graph.algorithms import (dead_code_candidates, impact_analysis,
                                     service_health, test_gaps)
from brain.context.retrieval import ContextRetriever
from brain.memory.engine import MemoryEngine
from brain.types import (ContextResult, HealthReport, ImpactResult,
                         LearningItem)


class BrainAPI:

    def __init__(self, config: BrainConfig = None):
        self._config = config or BrainConfig()
        Path(self._config.workspace).mkdir(parents=True, exist_ok=True)
        self._engine = GraphEngine(self._config.db_path)
        self._nxc = NetworkXCache(
            self._engine, edge_types=self._config.networkx_edge_types)
        self._memory = MemoryEngine(self._engine)
        self._retriever = ContextRetriever(self._engine, self._nxc)
        self._lance = None  # lazy

    @property
    def engine(self) -> GraphEngine:
        return self._engine

    @property
    def nxc(self) -> NetworkXCache:
        return self._nxc

    @property
    def memory(self) -> MemoryEngine:
        return self._memory

    # ------------------------------------------------------------------
    # Context Retrieval
    # ------------------------------------------------------------------
    def context_for(self, target: str, budget: int = 4000,
                    consumer: str = "default", depth: int = 3,
                    project: str = None) -> ContextResult:
        return self._retriever.context_for(
            target, budget=budget, consumer=consumer,
            depth=depth, project=project)

    # ------------------------------------------------------------------
    # Graph Queries
    # ------------------------------------------------------------------
    def who_calls(self, function: str, depth: int = 5) -> List[str]:
        return self._nxc.callers(function, depth=depth)

    def what_calls(self, function: str, depth: int = 5) -> List[str]:
        return self._nxc.callees(function, depth=depth)

    def path(self, source: str, target: str) -> List[str]:
        return self._nxc.shortest_path(source, target)

    def search(self, query: str, ntype: str = None, limit: int = 20) -> List[Dict]:
        return self._engine.search_nodes_fts(query, ntype=ntype, limit=limit)

    def search_code(self, query: str, project: str = None, limit: int = 20) -> List[Dict]:
        return self._engine.search_code_fts(query, project=project, limit=limit)

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------
    def impact(self, functions: List[str], max_depth: int = 5) -> ImpactResult:
        return impact_analysis(self._nxc, self._engine, functions, max_depth)

    def health(self, project: str) -> HealthReport:
        return service_health(self._engine, self._nxc, project)

    def dead_code(self, project: str) -> List[Dict]:
        return dead_code_candidates(self._engine, self._nxc, project)

    def test_gap(self, project: str) -> List[Dict]:
        return test_gaps(self._engine, self._nxc, project)

    # ------------------------------------------------------------------
    # Features
    # ------------------------------------------------------------------
    def feature_create(self, name: str, owner: str = None,
                       project: str = None) -> int:
        return self._engine.upsert_node(
            "Feature", name,
            data={"owner": owner, "status": "proposed"},
            project_slug=project)

    def feature_update(self, name: str, status: str = None,
                       data: Dict = None) -> bool:
        existing = self._engine.get_node("Feature", name)
        if not existing:
            return False
        merged = {**existing.get("data", {})}
        if status:
            merged["status"] = status
        if data:
            merged.update(data)
        self._engine.upsert_node("Feature", name, data=merged,
                                 project_slug=existing.get("project_slug"))
        return True

    def feature_list(self, status: str = None) -> List[Dict]:
        features = self._engine.find_nodes(ntype="Feature", limit=200)
        if status:
            features = [f for f in features
                        if f.get("data", {}).get("status") == status]
        return features

    def feature_health(self, name: str) -> Dict[str, Any]:
        node = self._engine.get_node("Feature", name)
        if not node:
            return {"error": f"Feature '{name}' not found"}

        reqs = self._engine.get_edges_from("Feature", name, "HAS_REQUIREMENT")
        risks = self._engine.get_edges_from("Feature", name, "HAS_RISK")
        tasks = self._engine.get_edges_from("Feature", name, "IMPLEMENTS_FEATURE")

        return {
            "name": name, "status": node.get("data", {}).get("status", "?"),
            "owner": node.get("data", {}).get("owner", "?"),
            "requirements": len(reqs), "risks": len(risks), "tasks": len(tasks),
            "confidence": node.get("confidence", 0),
        }

    # ------------------------------------------------------------------
    # Learning Pipeline
    # ------------------------------------------------------------------
    def learn(self, skill: str, items: List[LearningItem],
              interaction_type: str = "analysis") -> int:
        return self._memory.record(skill, items, interaction_type)

    def flush(self, interaction_id: int = None, dry_run: bool = False) -> Dict:
        result = self._memory.flush(interaction_id, dry_run)
        if not dry_run and (result.get("created", 0) + result.get("merged", 0)) > 0:
            self._nxc.refresh()
        return result

    def learn_status(self) -> Dict:
        return self._memory.status()

    # ------------------------------------------------------------------
    # Node/Edge CRUD (for direct manipulation)
    # ------------------------------------------------------------------
    def add_node(self, ntype: str, name: str, data: Dict = None,
                 project: str = None, confidence: float = 0.7) -> int:
        return self._engine.upsert_node(
            ntype, name, data=data, project_slug=project, confidence=confidence)

    def get_node(self, ntype: str, name: str) -> Optional[Dict]:
        return self._engine.get_node(ntype, name)

    def add_edge(self, from_type: str, from_name: str,
                 to_type: str, to_name: str, edge_type: str) -> None:
        self._engine.add_edge(from_type, from_name, to_type, to_name, edge_type)

    def delete_node(self, ntype: str, name: str) -> bool:
        return self._engine.delete_node(ntype, name)

    # ------------------------------------------------------------------
    # Ingestion (seed services + dependencies)
    # ------------------------------------------------------------------
    def seed_services(self) -> int:
        count = 0
        for p in SEED_PROJECTS:
            self._engine.upsert_service(p["slug"], role=p.get("role"),
                                         language=p.get("lang"))
            count += 1

        for src, deps in SERVICE_DEPS.items():
            for dep in deps:
                self._engine.add_edge("Service", src, "Service", dep, "DEPENDS_ON")
        return count

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------
    def get_sync_cursor(self, source_type: str, source_id: str) -> Optional[str]:
        return self._memory.get_sync_cursor(source_type, source_id)

    def update_sync_cursor(self, source_type: str, source_id: str,
                           cursor: str, project: str = None) -> None:
        self._memory.update_sync_cursor(source_type, source_id, cursor, project)

    # ------------------------------------------------------------------
    # Slash
    # ------------------------------------------------------------------
    def slash_store(self, question: str, response: str = None,
                    feature: str = None, thread_ts: str = None) -> int:
        return self._memory.store_slash(question, response, feature, thread_ts)

    def slash_recall(self, query: str, limit: int = 5) -> List[Dict]:
        return self._memory.recall_slash(query, limit)

    # ------------------------------------------------------------------
    # Stats & Lifecycle
    # ------------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        gs = self._engine.stats()
        ns = self._nxc.stats()
        return {
            "graph": gs,
            "networkx": ns,
            "db_path": self._config.db_path,
            "schema_version": self._engine.conn.execute(
                "SELECT value FROM brain_meta WHERE key='schema_version'"
            ).fetchone()[0],
        }

    def close(self):
        self._engine.close()

    def refresh_graph(self) -> float:
        return self._nxc.refresh()
