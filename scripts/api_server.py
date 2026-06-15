#!/usr/bin/env python3
"""Nemesis v2 FastAPI Event Bus — shared API for Next.js + Flask UIs.

Imports all Python engines directly (no subprocess overhead for graph/context/learn).
Claude CLI calls still use subprocess (claude -p).

Usage:
    uvicorn scripts.api_server:app --port 8000 --reload
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

# ── Path setup ───────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.parent
SCRIPTS = BASE_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS))

import brain_config as cfg
from rubick_graph import get_db as _graph_db, query_nodes, search_text, get_stats
from rubick_context import context_for, context_for_v2
from rubick_learn import record as learn_record, flush as learn_flush
from rubick_learn import status as learn_status, get_db as _learn_db

try:
    from rubick_planner import dashboard as planner_dashboard
except ImportError:
    planner_dashboard = None

try:
    from rubick_health import diagnose as health_diagnose
except ImportError:
    health_diagnose = None

from api_models import (
    ChatRequest, ChatResponse, FeatureCreateRequest, FeatureRunRequest,
    FeatureListResponse, HealthResponse, InitRunRequest, NodeListResponse,
    SessionCreateRequest, SessionListResponse, SkillRunRequest, SkillInfo,
    StatsResponse, SyncTriggerRequest, UsageResponse, UploadResult,
)
import event_bus

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("api_server")

# ── Config ───────────────────────────────────────────────────────────────────

DB_PATH = str(cfg.RUBICK_DB_PATH)
WORKSPACE = BASE_DIR / "workspace"
FEATURES_DIR = WORKSPACE / "features"
_executor = ThreadPoolExecutor(max_workers=8)


# ── Database ─────────────────────────────────────────────────────────────────

def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _q(sql, params=()):
    c = db()
    try:
        return [dict(r) for r in c.execute(sql, params).fetchall()]
    finally:
        c.close()


def init_tables():
    c = db()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS init_settings (
            id INTEGER PRIMARY KEY,
            key TEXT UNIQUE NOT NULL,
            value TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            message_count INTEGER DEFAULT 0,
            total_input_tokens INTEGER DEFAULT 0,
            total_output_tokens INTEGER DEFAULT 0,
            total_cost_usd REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            content_type TEXT DEFAULT 'text',
            rubick_target TEXT,
            elapsed REAL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_msgs_session ON chat_messages(session_id);
    """)
    for col, default in [
        ("total_input_tokens", 0), ("total_output_tokens", 0), ("total_cost_usd", 0)
    ]:
        try:
            c.execute(f"ALTER TABLE chat_sessions ADD COLUMN {col} INTEGER DEFAULT {default}")
        except:
            pass
    for col, default in [
        ("input_tokens", 0), ("output_tokens", 0),
        ("cache_read_tokens", 0), ("cache_write_tokens", 0), ("cost_usd", 0)
    ]:
        try:
            c.execute(f"ALTER TABLE chat_messages ADD COLUMN {col} REAL DEFAULT {default}")
        except:
            pass
    c.commit()
    c.close()


# ── Claude CLI ───────────────────────────────────────────────────────────────

def call_claude(prompt: str, timeout: int = 90) -> dict:
    try:
        r = subprocess.run(
            ["claude", "-p", "--output-format", "json", "--"],
            input=prompt, capture_output=True, text=True,
            timeout=timeout, cwd=str(BASE_DIR)
        )
        raw = r.stdout.strip()
        try:
            d = json.loads(raw)
            usage = d.get("usage") or {}
            return {
                "text": d.get("result", ""),
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_read": usage.get("cache_read_input_tokens", 0),
                "cache_write": usage.get("cache_creation_input_tokens", 0),
                "cost_usd": d.get("total_cost_usd", 0.0),
                "duration_ms": d.get("duration_ms", 0),
            }
        except (json.JSONDecodeError, TypeError):
            return {"text": raw, "input_tokens": 0, "output_tokens": 0,
                    "cache_read": 0, "cache_write": 0, "cost_usd": 0.0, "duration_ms": 0}
    except FileNotFoundError:
        return {"text": "__NO_CLAUDE__", "input_tokens": 0, "output_tokens": 0,
                "cache_read": 0, "cache_write": 0, "cost_usd": 0.0, "duration_ms": 0}
    except subprocess.TimeoutExpired:
        return {"text": "__TIMEOUT__", "input_tokens": 0, "output_tokens": 0,
                "cache_read": 0, "cache_write": 0, "cost_usd": 0.0, "duration_ms": 0}
    except Exception as e:
        return {"text": f"__ERROR__: {e}", "input_tokens": 0, "output_tokens": 0,
                "cache_read": 0, "cache_write": 0, "cost_usd": 0.0, "duration_ms": 0}


def _claude_available() -> bool:
    return shutil.which("claude") is not None


# ── Helpers ──────────────────────────────────────────────────────────────────

KNOWN_NODES = [
    "pg-router", "payments-card", "payments-upi", "emandate-service", "offers-engine",
    "checkout-service", "api", "rpc", "ledger", "splitz", "stork", "raven", "vault",
    "metro", "cps", "scrooge", "settlements", "shield", "mozart", "subscriptions",
    "reminders", "terminals", "dcs", "route", "edge", "relay", "bin-service",
    "apm-service", "optimizer-core", "magic-checkout-service", "payments-cross-border",
    "charge-collections", "tokens", "downtime-manager", "batch", "mock-gateway",
    "dashboard", "checkout", "goutils", "integrations-go", "integrations-utils",
    "payments-mandate", "payments-bank-transfer", "payment-methods", "payments-nb-wallet",
    "cfb-dfb-instant-offer-discount", "governor-executor", "cross-border",
]


def extract_node(message: str) -> str:
    msg = message.lower()
    for node in sorted(KNOWN_NODES, key=len, reverse=True):
        variants = [node, node.replace("-", " "), node.replace("-", "_")]
        if any(v in msg for v in variants):
            return node
    m = re.search(r'\b([a-z][a-z0-9]+-[a-z0-9-]+)\b', msg)
    if m:
        return m.group(1)
    return message.strip()[:60]


def _slug_safe(name: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", name.lower())


def _find_phase_file(feat_dir: Path, candidates: list[str]):
    for c in candidates:
        if (feat_dir / c).exists():
            return (c, True)
    return (candidates[0] if candidates else "", False)


def node_dict(row) -> dict:
    try:
        data = json.loads(row["data"]) if row["data"] else {}
    except:
        data = {}
    keys = list(row.keys())
    return {
        "id": row["id"], "type": row["type"], "name": row["name"],
        "data": data, "confidence": row["confidence"],
        "source_type": row["source_type"] if "source_type" in keys else "",
        "created_at": row["created_at"] if "created_at" in keys else "",
    }


def build_db_context(target: str) -> str:
    with ThreadPoolExecutor(max_workers=6) as pool:
        f = {
            "fn_count": pool.submit(_q, "SELECT COUNT(*) cnt FROM nodes WHERE type='Function' AND data LIKE ?", (f"%{target}%",)),
            "test_count": pool.submit(_q, "SELECT COUNT(*) cnt FROM nodes WHERE type='Test' AND data LIKE ?", (f"%{target}%",)),
            "cls_count": pool.submit(_q, "SELECT COUNT(*) cnt FROM nodes WHERE type='Class' AND data LIKE ?", (f"%{target}%",)),
            "deps_out": pool.submit(_q, """SELECT DISTINCT n.name FROM edges e
                JOIN nodes n ON e.to_node_id=n.id JOIN nodes s ON e.from_node_id=s.id
                WHERE s.name=? AND e.edge_type='DEPENDS_ON' LIMIT 20""", (target,)),
            "deps_in": pool.submit(_q, """SELECT DISTINCT n.name FROM edges e
                JOIN nodes n ON e.from_node_id=n.id JOIN nodes s ON e.to_node_id=s.id
                WHERE s.name=? AND e.edge_type='DEPENDS_ON' LIMIT 20""", (target,)),
            "endpoints": pool.submit(_q, """SELECT n.name FROM edges e JOIN nodes n ON e.to_node_id=n.id
                JOIN nodes s ON e.from_node_id=s.id
                WHERE s.name=? AND n.type='Endpoint' AND e.edge_type='HAS_ENDPOINT' LIMIT 15""", (target,)),
            "datastores": pool.submit(_q, "SELECT name FROM nodes WHERE type='DataStore' AND data LIKE ? LIMIT 10", (f'%"{target}"%',)),
            "risks": pool.submit(_q, "SELECT name FROM nodes WHERE type='RiskItem' AND data LIKE ? LIMIT 6", (f"%{target}%",)),
            "decisions": pool.submit(_q, "SELECT name FROM nodes WHERE type='ArchDecision' AND data LIKE ? LIMIT 6", (f"%{target}%",)),
        }
        r = {}
        for k, fut in f.items():
            try:
                r[k] = fut.result(timeout=5)
            except:
                r[k] = []

    fns = r["fn_count"][0]["cnt"] if r["fn_count"] else 0
    tests = r["test_count"][0]["cnt"] if r["test_count"] else 0
    cls = r["cls_count"][0]["cnt"] if r["cls_count"] else 0
    dout = [x["name"] for x in r["deps_out"]]
    din = [x["name"] for x in r["deps_in"]]
    eps = [x["name"] for x in r["endpoints"]]
    dsts = [x["name"] for x in r["datastores"]]
    risks = [x["name"] for x in r["risks"]]
    decs = [x["name"] for x in r["decisions"]]

    if not any([fns, tests, cls, dout, din, eps]):
        return ""

    lines = [f"[{target}]"]
    stats = []
    if fns:
        stats.append(f"Functions: {fns:,}")
    if tests:
        stats.append(f"Tests: {tests:,}")
    if cls:
        stats.append(f"Classes: {cls:,}")
    if eps:
        stats.append(f"Endpoints: {len(eps)}")
    if dsts:
        stats.append(f"DataStores: {len(dsts)}")
    if stats:
        lines.append(" | ".join(stats))
    if dout:
        lines.append(f"Depends on: {', '.join(dout)}")
    if din:
        lines.append(f"Used by: {', '.join(din)}")
    if eps:
        lines.append(f"Endpoints: {', '.join(eps)}")
    if dsts:
        lines.append(f"DataStores: {', '.join(dsts)}")
    if risks:
        lines.append(f"Risks: {', '.join(risks)}")
    if decs:
        lines.append(f"Arch decisions: {', '.join(decs)}")
    return "\n".join(lines)


def _ensure_session(session_id: str | None, title: str) -> str:
    c = db()
    try:
        if session_id:
            row = c.execute("SELECT session_id FROM chat_sessions WHERE session_id=?", (session_id,)).fetchone()
            if row:
                return session_id
        sid = session_id or str(uuid.uuid4())
        c.execute("INSERT OR IGNORE INTO chat_sessions (session_id, title) VALUES (?,?)", (sid, title[:80]))
        c.commit()
        return sid
    except:
        return session_id or str(uuid.uuid4())
    finally:
        c.close()


def _save_msg(session_id, role, content, content_type="text", rubick_target=None,
              elapsed=None, input_tokens=0, output_tokens=0, cache_read=0, cache_write=0, cost_usd=0.0):
    c = db()
    try:
        c.execute("""INSERT INTO chat_messages
            (session_id,role,content,content_type,rubick_target,elapsed,
             input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,cost_usd)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, role, content, content_type, rubick_target, elapsed,
             input_tokens, output_tokens, cache_read, cache_write, cost_usd))
        c.execute("""UPDATE chat_sessions SET message_count=message_count+1,
            total_input_tokens=total_input_tokens+?,
            total_output_tokens=total_output_tokens+?,
            total_cost_usd=total_cost_usd+?,
            updated_at=datetime('now') WHERE session_id=?""",
            (input_tokens, output_tokens, cost_usd, session_id))
        c.commit()
    except:
        pass
    finally:
        c.close()


def _get_rubick_context(target: str, budget: int = 3000) -> str:
    try:
        result = context_for_v2(target, consumer="arch", budget=budget)
        if isinstance(result, dict):
            return result.get("body") or ""
        return str(result) if result else ""
    except Exception:
        try:
            result = context_for(target, budget=budget)
            if isinstance(result, dict):
                return result.get("body") or ""
            return str(result) if result else ""
        except Exception:
            return ""


NEMESIS_PROMPT = """You are Nemesis — Razorpay's AI engineering orchestrator.
You command four specialist agents and the Rubick knowledge graph:
  - Ideation — feature overview & understanding (As-Is/To-Be flows)
  - Solutioning — solution design (exact code changes)
  - Tech Spec — tech spec document generation
  - Rubick — 715K-node knowledge graph of Razorpay's 46 services

Answer in natural prose like a senior engineer. Use markdown.
If a skill needs to run, include ONE directive at the end:
  <!--SKILL:ideation:{{"action":"create"}}-->
  <!--SKILL:solutioning:{{"action":"create"}}-->

Graph context (semantic):
{context}

Graph facts (DB):
{db_facts}

Question: {message}"""

IDEATION_PROMPT = """You are an Expert Systems Architect and Lead Business Analyst
for Razorpay engineering. You cut through noisy Slack threads, fragmented docs, and half-formed
requirements to produce a crystal-clear picture of what exists and what needs to be built.

## Your Analysis Protocol

1. IDENTIFY THE WHY — what business/technical problem triggers this feature
2. MAP THE AS-IS STATE — current flow through services, endpoints, data tables. What breaks.
3. DEFINE THE TO-BE STATE — expected end-to-end flow with concrete numeric examples
4. IDENTIFY MULTI-PATH ARCHITECTURE — Razorpay services often have Splitz gates (native Go vs proxy PHP)
5. MAP CROSS-PROJECT DEPENDENCIES — which services talk to which, via what protocol

## Feature: {feature_name}

### Sources
{sources_section}

### Rubick Knowledge Graph Context (services, functions, endpoints, patterns)
{context}

### Cross-Project References
{cross_refs}

### Related Knowledge (existing Rubick nodes)
{related_nodes}

## OUTPUT FORMAT

Return ONLY inner HTML. No <!DOCTYPE>, <html>, <head>, <body> tags. No markdown code fences.
The output embeds inside a styled container with Mermaid.js already loaded.

Use semantic HTML: <h1>, <h2>, <h3>, <p>, <table>, <ul>, <ol>, <code>, <pre>, <blockquote>.

DIAGRAMS — include Mermaid sequence/flow diagrams for every service interaction:
  <pre class="mermaid">
  sequenceDiagram
      participant A as service-name
      participant B as other-service
      A->>B: POST /v1/endpoint
      Note over B: What happens here
  </pre>

Use sequenceDiagram for request flows. Use flowchart TB for service dependency maps.
Use REAL service names from the context (pg-router, checkout-service, api, etc.).

## REQUIRED SECTIONS

1. **Title + Status Bar** — <h1> with feature name, <div> with service count + complexity

2. **TL;DR** — 2-3 sentences: what it does, what's broken, what the fix is

3. **As-Is: Current State** — How the system works today. What breaks. MUST include:
   - A Mermaid sequenceDiagram showing the current broken flow
   - Specific endpoint paths, function names, file references from context
   - What the user sees when it breaks (concrete example with amounts)

4. **To-Be: Expected Behavior** — What needs to change. MUST include:
   - A Mermaid sequenceDiagram showing the target flow with annotations
   - Step-by-step expected behavior with concrete numeric examples
   - As-Is vs To-Be comparison table

5. **Services Involved** — HTML table: Service | Role | Protocol | Impact

6. **Cross-Project Service Map** — Mermaid flowchart showing service dependencies:
   <pre class="mermaid">
   flowchart TB
       subgraph core["Core Services"]
           A[pg-router] -->|gRPC| B[payments-card]
       end
   </pre>

7. **Key APIs in the Chain** — HTML table: # | API | Service | Protocol | Purpose

8. **Key Questions** — Numbered list of open questions before solutioning

## RULES
- Use REAL service names, endpoint paths, and function names from the context
- Include concrete examples with rupee amounts where relevant
- Do NOT include implementation/solution details — this is understanding only
- Every service interaction MUST have a Mermaid diagram
- Be specific about what breaks and where"""

SOLUTIONING_PROMPT = """You are a solution design engine for Razorpay engineering.

Design the exact code changes for feature "{feature_name}".

Overview (Ideation output):
{overview_content}

Graph context:
{context}

Produce a solution document:

# {feature_name} — Solution

## Summary of Changes
(What changes, where, and why — one paragraph)

## Changes Required
For each change:
### C{{n}}: [short name]
- **Service**: `service-name`
- **File**: `path/to/file.go` (or best guess from context)
- **Change**: What needs to change and why
- **Risk**: Low/Medium/High

## Testing Strategy
(How to test this end-to-end)

## Rollout Order
(Which changes to deploy first and why)"""


# ── Skill registry ───────────────────────────────────────────────────────────

SKILLS = [
    SkillInfo(id="brain", name="Brain", command="/brain", description="Memory agent — knowledge graph operations",
              mode="python", status="active", commands=["stats", "health", "search", "context-for", "ingest", "feature-list"]),
    SkillInfo(id="nemesis", name="Nemesis", command="/nemesis", description="AI orchestrator — routes to Ideation, Solutioning, Tech Spec",
              mode="claude", status="active", commands=["ideation", "solutioning", "techspec"]),
    SkillInfo(id="plan", name="Planner", command="/plan", description="Interactive daily planner with DAG scheduling",
              mode="python", status="active", commands=["dashboard", "tasks", "add", "done", "focus", "weekly"]),
    SkillInfo(id="review", name="Review", command="/review", description="Code review & audit with Razorpay domain checks",
              mode="claude", status="active", commands=["pr", "diff", "audit", "triage", "checklist", "security"]),
    SkillInfo(id="techspec", name="Tech Spec", command="/silencer", description="Tech spec document generation (Google Docs)",
              mode="claude", status="active", commands=["generate", "section", "diagram", "review", "export"]),
    SkillInfo(id="slash", name="Slash", command="/slash", description="@Slash bot tribal knowledge queries",
              mode="claude", status="active", commands=["ask", "deep", "recall", "pending"]),
    SkillInfo(id="diagram", name="Diagram", command="/diagram", description="Architecture diagram generation (Canva/Mermaid)",
              mode="claude", status="active", commands=["flow", "arch", "entity", "impact", "timeline", "class"]),
    SkillInfo(id="doc", name="Doc", command="/doc", description=".docx document creation (python-docx)",
              mode="claude", status="active", commands=["create", "from-arch", "section", "finalize"]),
    SkillInfo(id="explain", name="Explain", command="/explain", description="Payment flow explainer",
              mode="claude", status="active", commands=["ask", "step", "flow", "doc"]),
    SkillInfo(id="standup", name="Standup", command="/standup", description="Daily standup & communication aggregator",
              mode="claude", status="active", commands=["today", "weekly", "prep", "digest", "missed"]),
    SkillInfo(id="tickets", name="Tickets", command="/tickets", description="Jira/DevRev ticket management",
              mode="claude", status="active", commands=["create", "from-spec", "triage", "status", "search"]),
    SkillInfo(id="franco", name="Franco", command="/franco", description="Universal data collector (auto-detect source)",
              mode="claude", status="active", commands=["collect", "batch", "docs"]),
    SkillInfo(id="designer", name="Designer", command="/designer", description="Creative design workflow (Canva/Figma)",
              mode="claude", status="active", commands=["create", "edit", "export", "mockup"]),
    SkillInfo(id="db-validator", name="DB Validator", command="/db-validator", description="Payment state & pre-deploy validation",
              mode="claude", status="pending", commands=["validate", "offer", "payment", "logs"]),
]

SKILL_MAP = {s.id: s for s in SKILLS}


# ── App lifecycle ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_tables()
    log.info("Nemesis FastAPI event bus starting — DB: %s", DB_PATH)
    poll_task = asyncio.create_task(event_bus.poll_changes(DB_PATH))
    yield
    poll_task.cancel()
    try:
        await poll_task
    except asyncio.CancelledError:
        pass
    log.info("Event bus shut down")


app = FastAPI(title="Nemesis v2 Event Bus", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000",
                   "http://localhost:5555", "http://127.0.0.1:5555"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════

# ── Health ───────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def api_health():
    try:
        c = db()
        tnodes = c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        tedges = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        db_size = os.path.getsize(DB_PATH) / (1024 * 1024) if os.path.exists(DB_PATH) else 0
        c.close()
    except Exception:
        tnodes, tedges, db_size = 0, 0, 0

    qdrant_ok = False
    try:
        from rubick_vectors import init_qdrant
        qdrant_ok = True
    except Exception:
        pass

    return {
        "status": "healthy",
        "db": {"nodes": tnodes, "edges": tedges, "size_mb": round(db_size, 1)},
        "qdrant": {"available": qdrant_ok},
        "claude": {"available": _claude_available()},
        "skills": {"total": len(SKILLS), "active": sum(1 for s in SKILLS if s.status == "active"),
                   "pending": sum(1 for s in SKILLS if s.status == "pending")},
    }


# ── Stats ────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def api_stats():
    try:
        c = db()
        types = {r["type"]: r["cnt"] for r in c.execute(
            "SELECT type, COUNT(*) cnt FROM nodes GROUP BY type ORDER BY cnt DESC")}
        tnodes = c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        tedges = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        c.close()
        return {"total_nodes": tnodes, "total_edges": tedges, "by_type": types}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Nodes ────────────────────────────────────────────────────────────────────

@app.get("/api/nodes")
async def api_nodes(
    type: str = "Feature",
    q: str = "",
    limit: int = Query(60, le=200),
    offset: int = 0,
):
    c = db()
    where, params = [], []
    if type:
        where.append("type=?")
        params.append(type)
    if q:
        where.append("(name LIKE ? OR data LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    wc = ("WHERE " + " AND ".join(where)) if where else ""
    rows = c.execute(
        f"SELECT id,type,name,confidence,source_type FROM nodes {wc} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset]
    ).fetchall()
    total = c.execute(f"SELECT COUNT(*) FROM nodes {wc}", params).fetchone()[0]
    c.close()
    return {
        "nodes": [{"id": r["id"], "type": r["type"], "name": r["name"], "confidence": r["confidence"]} for r in rows],
        "total": total, "offset": offset, "limit": limit,
    }


@app.get("/api/node/{nid}")
async def api_node(nid: int):
    c = db()
    row = c.execute("SELECT * FROM nodes WHERE id=?", (nid,)).fetchone()
    if not row:
        c.close()
        raise HTTPException(status_code=404, detail="not found")
    n = node_dict(row)
    n["edges_out"] = [{"edge_type": r[0], "id": r[1], "type": r[2], "name": r[3]} for r in
        c.execute("SELECT e.edge_type,n.id,n.type,n.name FROM edges e JOIN nodes n ON e.to_node_id=n.id WHERE e.from_node_id=? LIMIT 30", (nid,))]
    n["edges_in"] = [{"edge_type": r[0], "id": r[1], "type": r[2], "name": r[3]} for r in
        c.execute("SELECT e.edge_type,n.id,n.type,n.name FROM edges e JOIN nodes n ON e.from_node_id=n.id WHERE e.to_node_id=? LIMIT 30", (nid,))]
    c.close()
    return n


# ── Chat (RAG) ───────────────────────────────────────────────────────────────

@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="empty message")

    t0 = time.time()
    target = extract_node(req.message)

    if req.node_id:
        try:
            c = db()
            row = c.execute("SELECT name FROM nodes WHERE id=?", (req.node_id,)).fetchone()
            if row:
                target = row["name"]
            c.close()
        except:
            pass

    session_id = _ensure_session(req.session_id, req.message)
    _save_msg(session_id, "user", req.message)

    loop = asyncio.get_event_loop()
    db_facts = await loop.run_in_executor(_executor, build_db_context, target)
    ctx = await loop.run_in_executor(_executor, _get_rubick_context, target, 3000)

    result = await loop.run_in_executor(
        _executor, call_claude,
        NEMESIS_PROMPT.format(
            context=ctx or "(no semantic context available)",
            db_facts=db_facts or "(no DB facts — target not found in graph)",
            message=req.message,
        ),
        90,
    )

    raw = result["text"]
    if raw and not raw.startswith("__"):
        response = raw
    elif raw == "__NO_CLAUDE__":
        response = (db_facts or ctx) or "(claude CLI not found — install Claude Code)"
    elif raw == "__TIMEOUT__":
        response = "(claude timed out — try a simpler question)"
    else:
        response = raw or db_facts or ctx or "(empty response)"

    elapsed = round(time.time() - t0, 3)
    usage = {
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "cache_read": result["cache_read"],
        "cache_write": result["cache_write"],
        "cost_usd": result["cost_usd"],
    }
    _save_msg(session_id, "assistant", response, rubick_target=target, elapsed=elapsed, **usage)

    def _learn():
        learn_record("ui_chat", "api_server", [{
            "type": "Signal", "name": f"ui_chat:{req.message[:80]}",
            "data": {"source_type": "ui_chat",
                     "body": f"Q:{req.message[:300]}\nTarget:{target[:80]}\nA:{response[:300]}",
                     "ts": time.time()},
            "confidence": 0.7,
        }])
        learn_flush()

    loop.run_in_executor(_executor, _learn)

    event_bus.broadcast("chat_response", {"session_id": session_id, "target": target})
    return {"response": response, "rubick_target": target, "elapsed": elapsed,
            "session_id": session_id, "usage": usage}


# ── SSE Events ───────────────────────────────────────────────────────────────

@app.get("/api/events")
async def api_events():
    q = event_bus.subscribe()
    try:
        return StreamingResponse(
            event_bus.sse_stream(q),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                     "X-Accel-Buffering": "no"},
        )
    except Exception:
        event_bus.unsubscribe(q)
        raise


# ── Features ─────────────────────────────────────────────────────────────────

def _resolve_phase(raw: str) -> str:
    r = (raw or "").lower()
    if "techspec" in r or "silencer" in r or r in ("done", "complete"):
        return "techspec"
    if "solutioning" in r or "invoker" in r or "solution" in r:
        return "solutioning"
    return "ideation"


def _detect_artifacts(slug: str) -> dict:
    d = FEATURES_DIR / slug
    if not d.is_dir():
        return {}
    try:
        files = os.listdir(d)
    except OSError:
        return {}
    return {
        "hasOverview": any(f.startswith("overview") for f in files),
        "hasSolution": any(f.startswith("solution") for f in files),
        "hasRisk": (d / "risk_analysis").is_dir(),
        "hasTechSpec": any(f.endswith(".docx") or f.startswith("tech-spec") for f in files),
    }


@app.get("/api/features")
async def api_features():
    c = db()
    try:
        rows = c.execute(
            "SELECT id,name,data,confidence,updated_at FROM nodes WHERE type='Feature' ORDER BY updated_at DESC"
        ).fetchall()
        c.close()
    except Exception as e:
        return {"features": [], "error": str(e)}

    disk_slugs = []
    try:
        disk_slugs = [d for d in os.listdir(FEATURES_DIR)
                      if not d.endswith(".md") and (FEATURES_DIR / d).is_dir()]
    except OSError:
        pass

    db_names = set()
    result = []
    for r in rows:
        d = json.loads(r["data"]) if r["data"] else {}
        slug = re.sub(r"[^a-z0-9-]+", "-", r["name"].lower()).strip("-")
        db_names.add(r["name"].lower())
        phase = _resolve_phase(d.get("phase", d.get("status", "")))
        status = "done" if d.get("status", "").lower() in ("done", "complete", "shipped") else "in_progress"
        services = d.get("services", [])
        if isinstance(services, str):
            services = [services]
        arts = _detect_artifacts(slug)
        if not arts:
            arts = _detect_artifacts(r["name"])
        result.append({
            "id": str(r["id"]), "name": r["name"], "slug": slug,
            "phase": phase, "status": status,
            "complexity": d.get("complexity", "M"),
            "services": services,
            "lastUpdated": (r["updated_at"] or "").split(" ")[0] or "-",
            "confidence": r["confidence"],
            "artifacts": arts,
            "merchant": d.get("merchant"),
            "nextPhase": d.get("next_phase"),
            "raw": d,
        })

    for s in disk_slugs:
        if s not in db_names and not any(f["slug"] == s for f in result):
            result.append({
                "id": f"disk-{s}", "name": s, "slug": s,
                "phase": "ideation", "status": "in_progress",
                "complexity": "M", "services": [],
                "lastUpdated": "-", "confidence": 0.5,
                "artifacts": _detect_artifacts(s),
                "merchant": None, "nextPhase": None, "raw": {},
            })

    return {"features": result}


@app.post("/api/features")
async def api_features_create(req: FeatureCreateRequest):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")

    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    sources = {
        "slack": [u.strip() for u in (req.slack_threads or "").splitlines() if u.strip()],
        "docs": [u.strip() for u in (req.google_docs or "").splitlines() if u.strip()],
        "gmail": [u.strip() for u in (req.gmail_threads or "").splitlines() if u.strip()],
        "verbal": (req.description or "").strip(),
    }
    node_data = json.dumps({
        "status": "proposed", "owner": "saurav.k@razorpay.com",
        "phase": "ideation", "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sources": sources,
    })
    c = db()
    try:
        existing = c.execute("SELECT id FROM nodes WHERE type='Feature' AND name=?", (name,)).fetchone()
        if existing:
            return {"slug": slug, "name": name, "existing": True}
        c.execute("INSERT INTO nodes (type, name, data, source_type, confidence) VALUES ('Feature', ?, ?, 'ui_create', 0.9)", (name, node_data))
        c.commit()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        c.close()

    feat_dir = FEATURES_DIR / slug
    feat_dir.mkdir(parents=True, exist_ok=True)
    (feat_dir / "sources.json").write_text(json.dumps(sources, indent=2))

    event_bus.broadcast("node_updated", {"type": "Feature", "name": name})
    return {"slug": slug, "name": name, "created": True}


def _glob_files(directory: Path, patterns: list[str]) -> list[dict]:
    results = []
    seen = set()
    for pattern in patterns:
        parts = pattern.split("/")
        dir_part = "/".join(parts[:-1]) if len(parts) > 1 else ""
        file_pat = parts[-1]
        scan_dir = directory / dir_part if dir_part else directory
        escaped = re.escape(file_pat).replace(r"\*", ".*")
        regex = re.compile(f"^{escaped}$", re.IGNORECASE)
        try:
            for entry in os.listdir(scan_dir):
                rel = f"{dir_part}/{entry}" if dir_part else entry
                if rel in seen:
                    continue
                fp = scan_dir / entry
                if fp.is_file() and regex.match(entry):
                    seen.add(rel)
                    st = fp.stat()
                    results.append({
                        "name": entry, "path": rel, "size": st.st_size,
                        "mtime": st.st_mtime * 1000, "ext": os.path.splitext(entry)[1].lower(),
                    })
        except OSError:
            pass
    results.sort(key=lambda f: f["mtime"], reverse=True)
    return results


def _extract_version(name: str) -> int:
    m = re.search(r"_v(\d+)\.", name)
    return int(m.group(1)) if m else 1


def _walk_dir(directory: Path, base: str = "") -> list[dict]:
    results = []
    try:
        for entry in os.listdir(directory):
            fp = directory / entry
            rel = f"{base}/{entry}" if base else entry
            if fp.is_dir():
                results.extend(_walk_dir(fp, rel))
            elif fp.is_file():
                st = fp.stat()
                results.append({
                    "name": entry, "path": rel, "size": st.st_size,
                    "mtime": st.st_mtime * 1000, "ext": os.path.splitext(entry)[1].lower(),
                })
    except OSError:
        pass
    return results


def _detect_phase(directory: Path, patterns: list[str]) -> dict:
    files = _glob_files(directory, patterns)
    if not files:
        return {"exists": False, "files": []}
    md_files = sorted([f for f in files if f["ext"] == ".md"], key=lambda f: _extract_version(f["name"]), reverse=True)
    primary_md = None
    if md_files:
        try:
            primary_md = (directory / md_files[0]["path"]).read_text(encoding="utf-8")[:4000]
        except OSError:
            pass
    versions = [_extract_version(f["name"]) for f in files]
    max_ver = max(versions)
    canonical = next((f for f in files if _extract_version(f["name"]) == max_ver), None)
    return {
        "exists": True, "files": files, "primaryMd": primary_md,
        "canonical": canonical["path"] if canonical else None, "version": max_ver,
    }


def _categorize_files(all_files: list[dict]) -> list[dict]:
    cats = [
        {"label": "Ideation — Overview", "icon": "paint", "phase": "ideation", "files": []},
        {"label": "Solutioning — Solution", "icon": "flask", "phase": "solutioning", "files": []},
        {"label": "Risk Analysis", "icon": "ghost", "phase": "risk", "files": []},
        {"label": "Tech Spec", "icon": "doc", "phase": "techspec", "files": []},
        {"label": "Implementation Docs", "icon": "wrench", "files": []},
        {"label": "Reference / Context", "icon": "book", "files": []},
        {"label": "Data", "icon": "data", "files": []},
    ]
    for f in all_files:
        lp = f["path"].lower()
        ln = f["name"].lower()
        if ln.startswith("overview") or "overview" in lp:
            cats[0]["files"].append(f)
        elif ln.startswith("solution") or "solution" in lp:
            cats[1]["files"].append(f)
        elif lp.startswith("risk_analysis") or "cross_check" in ln or "risk-analysis" in ln:
            cats[2]["files"].append(f)
        elif ln.endswith(".docx") or ln.startswith("tech-spec"):
            cats[3]["files"].append(f)
        elif ln.startswith("impl-doc") or ln.startswith("final-approach"):
            cats[4]["files"].append(f)
        elif lp.startswith("razorpay-docs"):
            cats[5]["files"].append(f)
        elif f["ext"] == ".json":
            cats[6]["files"].append(f)
        else:
            cats[5]["files"].append(f)
    return [c for c in cats if c["files"]]


@app.get("/api/features/{slug}")
async def api_feature_detail(slug: str):
    c = db()
    try:
        row = c.execute("SELECT * FROM nodes WHERE type='Feature' AND name=?", (slug,)).fetchone()
        if not row:
            all_feats = c.execute("SELECT * FROM nodes WHERE type='Feature'").fetchall()
            for f in all_feats:
                candidate_slug = re.sub(r"[^a-z0-9-]+", "-", f["name"].lower()).strip("-")
                if candidate_slug == slug:
                    row = f
                    break
        if not row:
            row = c.execute(
                "SELECT * FROM nodes WHERE type='Feature' AND json_extract(data,'$.slug')=?",
                (slug,)
            ).fetchone()
        feature_node = None
        if row:
            d = json.loads(row["data"]) if row["data"] else {}
            feature_node = {"name": row["name"], "data": d, "confidence": row["confidence"],
                            "created_at": row["created_at"],
                            "updated_at": row["updated_at"] if "updated_at" in row.keys() else ""}

        signals = c.execute("""
            SELECT name, json_extract(data,'$.source_type') AS source_type,
                json_extract(data,'$.url') AS url, json_extract(data,'$.channel') AS channel,
                json_extract(data,'$.content') AS content, created_at
            FROM nodes WHERE type='Signal'
                AND (json_extract(data,'$.feature_slug')=?
                     OR json_extract(data,'$.project_slug') IN (
                        SELECT DISTINCT json_extract(data,'$.project_slug') FROM nodes
                        WHERE type='Feature' AND name=?))
            ORDER BY created_at DESC LIMIT 60
        """, (slug, row["name"] if row else slug)).fetchall()

        decisions = c.execute("""
            SELECT name, json_extract(data,'$.description') AS description,
                json_extract(data,'$.status') AS status, confidence
            FROM nodes WHERE type='ArchDecision'
                AND (json_extract(data,'$.feature_slug')=? OR json_extract(data,'$.feature')=?)
            LIMIT 20
        """, (slug, slug)).fetchall()
        c.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    feat_dir = FEATURES_DIR / slug
    all_features = []
    try:
        all_features = sorted([d for d in os.listdir(FEATURES_DIR)
                               if (FEATURES_DIR / d).is_dir()])
    except OSError:
        pass

    phases = {
        "ideation": _detect_phase(feat_dir, ["overview*.md", "overview*.html"]),
        "solutioning": _detect_phase(feat_dir, ["solution*.md", "solution*.html"]),
        "risk": _detect_phase(feat_dir, ["risk_analysis/*.md", "risk_analysis/*.html", "CROSS_CHECK_AUDIT.md"]),
        "techspec": _detect_phase(feat_dir, ["tech-spec*.md", "tech-spec*.docx", "*.docx"]),
    }

    sol_files = _glob_files(feat_dir, ["solution*.md"])
    solution_versions = {
        "canonical": sorted(sol_files, key=lambda f: _extract_version(f["name"]), reverse=True)[0]["path"] if sol_files else None,
        "all": [{"path": f["path"], "version": _extract_version(f["name"]), "size": f["size"]} for f in sol_files],
    }

    all_files = _walk_dir(feat_dir)
    categories = _categorize_files(all_files)

    phase_order = ["ideation", "solutioning", "risk", "techspec"]
    completed = [p for p in phase_order if phases[p]["exists"]]
    current = "done"
    for p in phase_order:
        if not phases[p]["exists"]:
            current = p
            break

    return {
        "slug": slug,
        "featureNode": feature_node,
        "allFeatures": all_features,
        "signals": [dict(r) for r in signals],
        "decisions": [dict(r) for r in decisions],
        "phases": phases,
        "solutionVersions": solution_versions,
        "allFiles": all_files,
        "categories": categories,
        "nemesisCommands": {p: f"/nemesis {p} {slug}" for p in phase_order},
        "pipeline": {"completedPhases": completed, "currentPhase": current,
                     "progress": len(completed), "total": 4},
    }


@app.get("/api/features/{slug}/content")
async def api_feature_content(slug: str, tab: str = "overview", version: str = None):
    c = db()
    try:
        row = c.execute("SELECT name FROM nodes WHERE type='Feature' AND (name=? OR lower(name) LIKE ?)",
                        (slug, f"%{slug.lower()}%")).fetchone()
        name = row["name"] if row else slug
    except:
        name = slug
    finally:
        c.close()

    feat_dir = FEATURES_DIR / _slug_safe(name)
    if version:
        path = feat_dir / version
        if path.exists():
            content = path.read_text(encoding="utf-8")
            ftype = "html" if version.endswith(".html") else "md"
            return {"exists": True, "content": content, "type": ftype}
        return {"exists": False, "content": "", "type": "md"}

    candidates = {
        "overview": ["overview.html", "overview.md"],
        "solution": ["solution_final.md", "solution_v2.md", "solution.md"],
        "risk": ["risk_analysis/risk-analysis.md", "risk-analysis.md"],
        "tech-spec": ["tech-spec.md", "tech_spec.md", "scribe/tech-spec.md"],
    }.get(tab, [])
    if not candidates:
        return {"exists": False, "content": "", "type": "md"}
    fname, exists = _find_phase_file(feat_dir, candidates)
    if not exists:
        return {"exists": False, "content": "", "type": "md"}
    content = (feat_dir / fname).read_text(encoding="utf-8")
    ftype = "html" if fname.endswith(".html") else "md"
    return {"exists": True, "content": content, "type": ftype}


@app.get("/api/features/{slug}/file")
async def api_feature_file(slug: str, path: str = ""):
    feat_dir = FEATURES_DIR / slug
    if not path:
        raise HTTPException(status_code=400, detail="path required")
    fp = feat_dir / path
    if not fp.exists() or not str(fp.resolve()).startswith(str(FEATURES_DIR.resolve())):
        raise HTTPException(status_code=404, detail="file not found")
    content = fp.read_text(encoding="utf-8")
    ftype = "html" if path.endswith(".html") else "md"
    return {"content": content, "type": ftype, "path": path}


@app.post("/api/features/{slug}/run/{phase}")
async def api_feature_run(slug: str, phase: str, req: FeatureRunRequest = None):
    if req is None:
        req = FeatureRunRequest()
    feature_name = req.feature_name or slug
    feat_dir = FEATURES_DIR / _slug_safe(feature_name)
    feat_dir.mkdir(parents=True, exist_ok=True)

    sources = {}
    src_file = feat_dir / "sources.json"
    if src_file.exists():
        try:
            sources = json.loads(src_file.read_text())
        except:
            pass
    if not sources:
        c = db()
        try:
            row = c.execute("SELECT data FROM nodes WHERE type='Feature' AND (name=? OR lower(name) LIKE ?)",
                            (feature_name, f"%{slug.lower()}%")).fetchone()
            if row:
                sources = (json.loads(row["data"]) if row["data"] else {}).get("sources", {})
        except:
            pass
        finally:
            c.close()

    loop = asyncio.get_event_loop()
    if phase == "ideation":
        return await loop.run_in_executor(_executor, _run_ideation, slug, feature_name, sources, feat_dir, req.session_id)
    elif phase == "solutioning":
        return await loop.run_in_executor(_executor, _run_solutioning, slug, feature_name, feat_dir, req.session_id)
    else:
        raise HTTPException(status_code=400, detail=f"Phase '{phase}' not yet supported — use ideation or solutioning")


def _get_cross_refs(target: str) -> str:
    try:
        result = search_text(target, limit=10)
        if isinstance(result, list):
            lines = []
            for r in result[:10]:
                lines.append(f"[{r.get('type','')}] {r.get('name','')} (proj={r.get('project_slug','')})")
            return "\n".join(lines)
        return str(result)[:2000] if result else ""
    except Exception:
        return ""


def _get_related_nodes(feature_name: str, slug: str) -> str:
    c = db()
    try:
        rows = c.execute(
            """SELECT type, name, substr(data,1,300) AS data, confidence
               FROM nodes WHERE (type IN ('Requirement','ArchDecision','Signal','RiskItem','BusinessLogic'))
               AND (lower(name) LIKE ? OR lower(COALESCE(data,'')) LIKE ?)
               ORDER BY confidence DESC, updated_at DESC LIMIT 20""",
            (f"%{slug}%", f"%{slug}%")).fetchall()
        if not rows:
            return "(no related nodes found)"
        lines = []
        for r in rows:
            lines.append(f"[{r['type']}] {r['name']} (conf={r['confidence']})")
            if r["data"] and r["data"] != "{}":
                lines.append(f"  data: {r['data'][:200]}")
        return "\n".join(lines)
    except Exception:
        return ""
    finally:
        c.close()


def _run_ideation(slug, feature_name, sources, feat_dir, session_id):
    src_parts = []
    if sources.get("slack"):
        src_parts.append("Slack threads:\n" + "\n".join(f"- {u}" for u in sources["slack"]))
    if sources.get("docs"):
        src_parts.append("Google Docs:\n" + "\n".join(f"- {u}" for u in sources["docs"]))
    if sources.get("gmail"):
        src_parts.append("Gmail threads:\n" + "\n".join(f"- {u}" for u in sources["gmail"]))
    if sources.get("verbal"):
        src_parts.append(f"Verbal brief:\n{sources['verbal']}")
    sources_section = "\n\n".join(src_parts) if src_parts else "(No external sources — generating from graph context only)"

    ctx = _get_rubick_context(feature_name, budget=4000)
    cross_refs = _get_cross_refs(feature_name)
    related = _get_related_nodes(feature_name, slug)

    existing = sorted(feat_dir.glob("overview*.md")) + sorted(feat_dir.glob("overview*.html"))
    v = len(existing) + 1
    out_file = feat_dir / ("overview.html" if v == 1 else f"overview_v{v}.html")

    result = call_claude(IDEATION_PROMPT.format(
        feature_name=feature_name, sources_section=sources_section,
        context=ctx or "(no graph context available)",
        cross_refs=cross_refs or "(no cross-refs found)",
        related_nodes=related or "(no related nodes)"), timeout=300)
    if result["text"].startswith("__"):
        return {"error": result["text"], "ok": False}

    html = result["text"].strip()
    if html.startswith("```"): html = html.split("\n", 1)[1] if "\n" in html else html
    if html.endswith("```"):   html = html[:-3].rstrip()
    idx = html.find("<h1")
    if idx == -1: idx = html.find("<div")
    if idx > 0 and idx < 500:
        pre = html[:idx].strip()
        if not pre.startswith("<"):
            html = html[idx:]

    out_file.write_text(html)

    c = db()
    try:
        c.execute("UPDATE nodes SET data=json_set(COALESCE(data,'{}'),'$.phase','ideation','$.status','in-progress') WHERE type='Feature' AND (name=? OR lower(name) LIKE ?)",
                  (feature_name, f"%{slug}%"))
        c.commit()
    except:
        pass
    finally:
        c.close()

    try:
        learn_record("ideation_overview", "nemesis",
                     [{"type": "Signal", "name": f"ideation:{slug} overview v{v}",
                       "data": {"source_type": "ideation", "feature": feature_name, "version": v}}])
        learn_flush()
    except Exception:
        pass

    session_id = _ensure_session(session_id, f"ideation:{feature_name[:60]}")
    summary = f"**Ideation complete** — `{out_file.name}` (v{v}, {len(html)} chars, HTML+Mermaid)"
    _save_msg(session_id, "assistant", summary, rubick_target=feature_name,
              input_tokens=result["input_tokens"], output_tokens=result["output_tokens"], cost_usd=result["cost_usd"])
    event_bus.broadcast("feature_phase_complete", {"slug": slug, "phase": "ideation", "version": v})
    return {"ok": True, "session_id": session_id, "file": out_file.name, "version": v,
            "summary": summary, "usage": {"cost_usd": result["cost_usd"],
                                           "input_tokens": result["input_tokens"],
                                           "output_tokens": result["output_tokens"]}}


def _run_solutioning(slug, feature_name, feat_dir, session_id):
    overviews = sorted(feat_dir.glob("overview*.md")) + sorted(feat_dir.glob("overview*.html"))
    if not overviews:
        return {"error": "Ideation overview must exist before running Solutioning", "ok": False}
    overview_content = overviews[-1].read_text(encoding="utf-8")[:4000]

    ctx = _get_rubick_context(feature_name, budget=2000)
    existing = sorted(feat_dir.glob("solution*.md"))
    v = len(existing) + 1
    out_file = feat_dir / ("solution.md" if v == 1 else f"solution_v{v}.md")

    result = call_claude(SOLUTIONING_PROMPT.format(
        feature_name=feature_name, overview_content=overview_content,
        context=ctx or "(no graph context)"), timeout=180)
    if result["text"].startswith("__"):
        return {"error": result["text"], "ok": False}

    out_file.write_text(result["text"])

    c = db()
    try:
        c.execute("UPDATE nodes SET data=json_set(COALESCE(data,'{}'),'$.phase','solutioning') WHERE type='Feature' AND (name=? OR lower(name) LIKE ?)",
                  (feature_name, f"%{slug}%"))
        c.commit()
    except:
        pass
    finally:
        c.close()

    session_id = _ensure_session(session_id, f"solutioning:{feature_name[:60]}")
    summary = f"**Solutioning complete** — `{out_file.name}` (v{v})"
    _save_msg(session_id, "assistant", summary, rubick_target=feature_name,
              input_tokens=result["input_tokens"], output_tokens=result["output_tokens"], cost_usd=result["cost_usd"])
    event_bus.broadcast("feature_phase_complete", {"slug": slug, "phase": "solutioning", "version": v})
    return {"ok": True, "session_id": session_id, "file": out_file.name, "version": v,
            "summary": summary, "usage": {"cost_usd": result["cost_usd"],
                                           "input_tokens": result["input_tokens"],
                                           "output_tokens": result["output_tokens"]}}


@app.get("/api/features/{slug}/versions/{phase}")
async def api_feature_versions(slug: str, phase: str):
    feat_dir = FEATURES_DIR / slug
    patterns = {
        "ideation": ["overview.html", "overview.md", "overview_v*.md", "overview_v*.html"],
        "solutioning": ["solution.md", "solution_v*.md", "solution_final.md"],
        "risk_analysis": ["risk_analysis/risk-analysis.md", "risk-analysis.md"],
        "techspec": ["tech-spec.md", "tech_spec.md"],
    }.get(phase, [])
    seen, versions = set(), []
    for pat in patterns:
        if "*" in pat:
            files = sorted(feat_dir.glob(pat))
        else:
            fp = feat_dir / pat
            files = [fp] if fp.exists() else []
        for f in files:
            nm = str(f.relative_to(feat_dir))
            if nm not in seen:
                seen.add(nm)
                versions.append(nm)
    return {"versions": versions, "phase": phase}


# ── Sessions ─────────────────────────────────────────────────────────────────

@app.get("/api/sessions")
async def api_sessions():
    c = db()
    try:
        rows = c.execute("""SELECT session_id,title,created_at,updated_at,message_count,
            total_input_tokens,total_output_tokens,total_cost_usd
            FROM chat_sessions ORDER BY updated_at DESC LIMIT 50""").fetchall()
        return {"sessions": [dict(r) for r in rows]}
    except:
        return {"sessions": []}
    finally:
        c.close()


@app.post("/api/sessions")
async def api_sessions_create(req: SessionCreateRequest):
    sid = str(uuid.uuid4())
    c = db()
    try:
        c.execute("INSERT INTO chat_sessions (session_id,title) VALUES (?,?)", (sid, req.title[:80]))
        c.commit()
        return {"session_id": sid, "title": req.title[:80]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        c.close()


@app.get("/api/sessions/{sid}")
async def api_session_detail(sid: str):
    c = db()
    try:
        s = c.execute("SELECT * FROM chat_sessions WHERE session_id=?", (sid,)).fetchone()
        if not s:
            raise HTTPException(status_code=404, detail="not found")
        msgs = c.execute("""SELECT role,content,content_type,rubick_target,elapsed,
            input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,cost_usd,created_at
            FROM chat_messages WHERE session_id=? ORDER BY id""", (sid,)).fetchall()
        return {"session": dict(s), "messages": [dict(m) for m in msgs]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        c.close()


# ── Oracle ───────────────────────────────────────────────────────────────────

@app.get("/api/oracle/today")
async def api_oracle_today():
    c = db()
    try:
        signals = [dict(r) for r in c.execute(
            "SELECT name, data, created_at FROM nodes WHERE type='Signal' AND created_at >= datetime('now','-1 day') ORDER BY created_at DESC LIMIT 15").fetchall()]
        prs = [dict(r) for r in c.execute(
            "SELECT name, data, created_at FROM nodes WHERE type='PR' ORDER BY created_at DESC LIMIT 8").fetchall()]
        return {"signals": signals, "prs": prs}
    except Exception as e:
        return {"signals": [], "prs": [], "error": str(e)}
    finally:
        c.close()


@app.get("/api/oracle/features")
async def api_oracle_features():
    c = db()
    try:
        rows = c.execute("SELECT id,name,data,created_at FROM nodes WHERE type='Feature' ORDER BY created_at DESC LIMIT 20").fetchall()
        result = []
        for r in rows:
            d = json.loads(r["data"]) if r["data"] else {}
            result.append({"id": r["id"], "name": r["name"], "created_at": r["created_at"],
                           "phase": d.get("phase", "unknown"), "status": d.get("status", "proposed")})
        return {"features": result}
    except Exception as e:
        return {"features": [], "error": str(e)}
    finally:
        c.close()


@app.get("/api/oracle/inbox")
async def api_oracle_inbox():
    c = db()
    try:
        rows = c.execute(
            "SELECT name, data, created_at FROM nodes WHERE type='Signal' AND source_type IN ('slack','gmail') ORDER BY created_at DESC LIMIT 20").fetchall()
        return {"items": [dict(r) for r in rows]}
    except:
        return {"items": []}
    finally:
        c.close()


# ── Usage ────────────────────────────────────────────────────────────────────

def _count_files(directory: Path) -> int:
    count = 0
    try:
        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if d != ".git"]
            count += len(files)
    except OSError:
        pass
    return count


@app.get("/api/usage")
async def api_usage():
    c = db()
    try:
        stats = c.execute(
            "SELECT (SELECT COUNT(*) FROM nodes) as nodes, (SELECT COUNT(*) FROM edges) as edges"
        ).fetchone()
        ledger = c.execute("""
            SELECT project_slug, source_skill, interaction_type, COUNT(*) as cnt, MAX(created_at) as last_at
            FROM learning_ledger GROUP BY project_slug, source_skill, interaction_type
        """).fetchall()
        c.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    by_slug: dict[str, dict] = {}
    for row in ledger:
        s = row["project_slug"] or "unknown"
        if s not in by_slug:
            by_slug[s] = {"interactions": 0, "skills": set(), "lastActivity": ""}
        by_slug[s]["interactions"] += row["cnt"]
        if row["source_skill"]:
            by_slug[s]["skills"].add(row["source_skill"])
        if not by_slug[s]["lastActivity"] or (row["last_at"] or "") > by_slug[s]["lastActivity"]:
            by_slug[s]["lastActivity"] = row["last_at"] or ""

    disk_slugs = []
    try:
        disk_slugs = [d for d in os.listdir(FEATURES_DIR) if (FEATURES_DIR / d).is_dir()]
    except OSError:
        pass

    features = []
    for s in disk_slugs:
        info = by_slug.get(s)
        features.append({
            "slug": s,
            "interactions": info["interactions"] if info else 0,
            "skills": list(info["skills"]) if info else [],
            "fileCount": _count_files(FEATURES_DIR / s),
            "lastActivity": (info["lastActivity"].split(" ")[0] if info and info["lastActivity"] else None),
        })

    total_interactions = sum(v["interactions"] for v in by_slug.values())
    return {
        "brain": {"nodes": stats["nodes"], "edges": stats["edges"], "projects": 45,
                  "features": len(disk_slugs)},
        "totalInteractions": total_interactions,
        "features": features,
    }


# ── Graph (project constellation) ────────────────────────────────────────────

ROLE_COLORS = {
    "primary": "#d97706", "core": "#dc2626", "infra": "#6b7280",
    "domain": "#2563eb", "gateway": "#16a34a", "support": "#7c3aed",
    "frontend": "#0891b2", "ecosystem": "#c026d3",
}
ROLE_RING = {
    "primary": 0, "core": 0, "infra": 1, "domain": 1,
    "gateway": 2, "support": 2, "frontend": 2, "ecosystem": 2,
}


@app.get("/api/graph")
async def api_graph():
    c = db()
    try:
        projects = c.execute(
            "SELECT id, name, data FROM nodes WHERE type='Project' AND name != 'omni' ORDER BY name").fetchall()
        type_counts = c.execute("""
            SELECT json_extract(data,'$.project_slug') as ps, type, COUNT(*) as cnt
            FROM nodes
            WHERE type IN ('Function','Class','Module','Endpoint','Test','DataStore','ArchDecision')
            AND json_extract(data,'$.project_slug') IS NOT NULL
            GROUP BY ps, type
        """).fetchall()
        deps = c.execute("""
            SELECT n1.name as from_proj, n2.name as to_proj
            FROM edges e
            JOIN nodes n1 ON e.from_node_id = n1.id AND n1.type='Project'
            JOIN nodes n2 ON e.to_node_id = n2.id AND n2.type='Project'
            WHERE e.edge_type='DEPENDS_ON'
        """).fetchall()
        c.close()
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    type_map: dict[str, dict[str, int]] = {}
    for r in type_counts:
        ps = r["ps"]
        if ps not in type_map:
            type_map[ps] = {}
        type_map[ps][r["type"]] = r["cnt"]

    nodes = []
    for p in projects:
        d = json.loads(p["data"]) if p["data"] else {}
        role = d.get("role", "infra")
        types = type_map.get(p["name"], {})
        total = sum(types.values())
        nodes.append({
            "id": p["name"], "name": p["name"], "role": role,
            "color": ROLE_COLORS.get(role, "#6b7280"),
            "ring": ROLE_RING.get(role, 2),
            "totalNodes": total, "types": types,
        })

    return {"nodes": nodes, "edges": [{"from_proj": d["from_proj"], "to_proj": d["to_proj"]} for d in deps]}


# ── Expert (ProjectExpert) ───────────────────────────────────────────────────

@app.get("/api/hero/{slug}")
async def api_hero(slug: str):
    c = db()
    try:
        type_dist = c.execute("""
            SELECT type, COUNT(*) AS count FROM nodes
            WHERE json_extract(data,'$.project_slug')=?
                AND type IN ('Function','Class','Module','Endpoint','Test','DataStore','ArchDecision')
            GROUP BY type ORDER BY count DESC
        """, (slug,)).fetchall()

        ledger = c.execute("""
            SELECT source_skill, project_slug, created_at FROM learning_ledger
            WHERE project_slug=? ORDER BY created_at DESC LIMIT 40
        """, (slug,)).fetchall()

        features = c.execute("""
            SELECT name, json_extract(data,'$.description') AS description,
                json_extract(data,'$.status') AS status, created_at
            FROM nodes WHERE type='Feature'
                AND (json_extract(data,'$.project_slug')=? OR json_extract(data,'$.projects') LIKE ?)
            ORDER BY created_at DESC LIMIT 15
        """, (slug, f"%{slug}%")).fetchall()

        endpoints = c.execute("""
            SELECT name, json_extract(data,'$.http_method') AS http_method,
                json_extract(data,'$.http_path') AS http_path
            FROM nodes WHERE type='Endpoint' AND json_extract(data,'$.project_slug')=?
            ORDER BY name LIMIT 10
        """, (slug,)).fetchall()

        expert_row = c.execute("""
            SELECT data FROM nodes WHERE type='ProjectExpert' AND json_extract(data,'$.project_slug')=?
            LIMIT 1
        """, (slug,)).fetchone()
        c.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    expert = None
    if expert_row and expert_row["data"]:
        try:
            expert = json.loads(expert_row["data"])
        except (json.JSONDecodeError, TypeError):
            pass

    feature_dirs = []
    try:
        feature_dirs = [d for d in os.listdir(FEATURES_DIR) if (FEATURES_DIR / d).is_dir()]
    except OSError:
        pass

    return {
        "slug": slug,
        "typeDist": [dict(r) for r in type_dist],
        "ledger": [dict(r) for r in ledger],
        "features": [dict(r) for r in features],
        "endpoints": [dict(r) for r in endpoints],
        "expert": expert,
        "featureDirs": feature_dirs,
    }


# ── Projects search ──────────────────────────────────────────────────────────

@app.get("/api/projects/search")
async def api_projects_search(q: str = "", limit: int = 20):
    if not q:
        return {"projects": [], "total": 0}
    limit = max(1, min(limit, 50))
    role_priority = {"primary": 0, "core": 1, "infra": 2, "domain": 3, "gateway": 4, "support": 5, "frontend": 6, "ecosystem": 7}
    c = db()
    try:
        rows = c.execute(
            "SELECT name, data FROM nodes WHERE type='Project' AND name LIKE ? ORDER BY name LIMIT ?",
            (f"%{q}%", limit)
        ).fetchall()
        results = []
        for r in rows:
            d = json.loads(r["data"]) if r["data"] else {}
            results.append({"name": r["name"], "role": d.get("role", "infra")})
        results.sort(key=lambda x: role_priority.get(x["role"], 99))
        return {"projects": results, "total": len(results)}
    except Exception:
        return {"projects": [], "total": 0}
    finally:
        c.close()


# ── Init ─────────────────────────────────────────────────────────────────────

@app.get("/api/init/check")
async def api_init_check():
    try:
        c = db()
        row = c.execute("SELECT value FROM init_settings WHERE key='profile_saved'").fetchone()
        c.close()
        return {"initialized": row is not None}
    except:
        return {"initialized": False}


@app.post("/api/init/run")
async def api_init_run(request: Request):
    body = await request.json()
    c = db()
    try:
        for key, val in body.items():
            v = val if isinstance(val, str) else json.dumps(val)
            c.execute("INSERT OR REPLACE INTO init_settings (key,value) VALUES (?,?)", (key, v))
        c.execute("INSERT OR REPLACE INTO init_settings (key,value) VALUES ('profile_saved','1')")
        c.commit()
    finally:
        c.close()
    return {"ok": True}


# ── Upload ───────────────────────────────────────────────────────────────────

@app.post("/api/upload")
async def api_upload(files: list[UploadFile] = File(...)):
    results = []
    for f in files:
        try:
            content = (await f.read()).decode("utf-8", errors="replace")
            learn_record("ui_upload", "api_server", [{
                "type": "Document", "name": f"upload:{f.filename}",
                "data": {"source_type": "ui_upload", "filename": f.filename, "body": content[:6000]},
                "confidence": 0.8,
            }])
            learn_flush()
            results.append({"name": f.filename, "status": "ingested", "size": len(content)})
        except Exception as e:
            results.append({"name": f.filename, "status": "error", "error": str(e), "size": 0})
    return {"files": results}


# ── Skills ───────────────────────────────────────────────────────────────────

@app.get("/api/skills")
async def api_skills():
    return {"skills": [s.model_dump() for s in SKILLS]}


@app.post("/api/skills/{skill_id}/run")
async def api_skill_run(skill_id: str, req: SkillRunRequest):
    skill = SKILL_MAP.get(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail=f"skill '{skill_id}' not found")

    command = req.command.strip()
    args = req.args or ""

    if skill.mode == "python":
        return await _run_python_skill(skill_id, command, args, req.feature_slug)
    else:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, _run_claude_skill, skill_id, command, args, req.feature_slug)


async def _run_python_skill(skill_id: str, command: str, args: str, feature_slug: str | None) -> dict:
    if skill_id == "brain":
        if command == "stats":
            c = db()
            tnodes = c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            tedges = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            types = {r["type"]: r["cnt"] for r in c.execute(
                "SELECT type, COUNT(*) cnt FROM nodes GROUP BY type ORDER BY cnt DESC LIMIT 20")}
            c.close()
            return {"output": json.dumps({"nodes": tnodes, "edges": tedges, "types": types}, indent=2)}
        elif command == "health" and health_diagnose:
            result = health_diagnose(DB_PATH)
            return {"output": json.dumps(result, indent=2, default=str)}
        elif command == "context-for" and args:
            ctx = _get_rubick_context(args, 3000)
            return {"output": ctx or "(no context found)"}
        elif command == "search" and args:
            c = db()
            rows = c.execute(
                "SELECT id, type, name FROM nodes WHERE name LIKE ? OR data LIKE ? LIMIT 20",
                (f"%{args}%", f"%{args}%")).fetchall()
            c.close()
            return {"output": json.dumps([dict(r) for r in rows], indent=2)}
    elif skill_id == "plan":
        if command == "dashboard" and planner_dashboard:
            result = planner_dashboard()
            return {"output": json.dumps(result, indent=2, default=str)}

    return {"output": f"(python mode not implemented for {skill_id} {command})"}


def _run_claude_skill(skill_id: str, command: str, args: str, feature_slug: str | None) -> dict:
    skill_file = BASE_DIR / "commands" / f"{skill_id}.md"
    if not skill_file.exists():
        return {"error": f"skill file not found: {skill_file}"}

    prompt = f"Execute: /{skill_id} {command} {args}".strip()
    if feature_slug:
        prompt += f"\nFeature: {feature_slug}"

    result = call_claude(prompt, timeout=120)
    if result["text"].startswith("__"):
        return {"error": result["text"]}
    return {
        "output": result["text"],
        "usage": {"input_tokens": result["input_tokens"], "output_tokens": result["output_tokens"],
                  "cost_usd": result["cost_usd"]},
    }


# ── Sync ─────────────────────────────────────────────────────────────────────

@app.get("/api/sync/status")
async def api_sync_status():
    c = db()
    try:
        rows = c.execute("SELECT * FROM sync_state ORDER BY last_sync DESC").fetchall()
        return {"sources": [dict(r) for r in rows]}
    except:
        return {"sources": []}
    finally:
        c.close()


@app.post("/api/sync/trigger")
async def api_sync_trigger(req: SyncTriggerRequest):
    event_bus.broadcast("sync_trigger", {"source": req.source})
    return {"ok": True, "message": f"Sync triggered for {req.source}"}


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=True)
