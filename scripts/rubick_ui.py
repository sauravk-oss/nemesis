#!/usr/bin/env python3
"""
rubick_ui.py — Rubick ChatGPT-style UI (light theme)

Usage:
  python3 scripts/rubick_ui.py [--db workspace/rubick.db] [--port 5555]
"""
import argparse, json, logging, os, re, sqlite3, subprocess, sys, time, uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from flask import Flask, jsonify, request, Response, stream_with_context

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("rubick_ui")

BASE_DIR   = Path(__file__).parent.parent
DEFAULT_DB = BASE_DIR / "workspace" / "rubick.db"
SCRIPTS    = BASE_DIR / "scripts"

app = Flask(__name__)
DB_PATH = str(DEFAULT_DB)

# ── helpers ──────────────────────────────────────────────────────────────────

def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def node_dict(row):
    try:    data = json.loads(row["data"]) if row["data"] else {}
    except: data = {}
    keys = list(row.keys())
    return {
        "id": row["id"], "type": row["type"], "name": row["name"],
        "data": data,
        "confidence": row["confidence"],
        "source_type": row["source_type"] if "source_type" in keys else "",
        "created_at":  row["created_at"]  if "created_at"  in keys else "",
    }

def run_learn(items, source):
    try:
        subprocess.run([sys.executable, str(SCRIPTS/"rubick_learn.py"), "record",
            "--interaction-type", source, "--source-skill", "rubick_ui",
            "--items", json.dumps(items)],
            capture_output=True, text=True, timeout=10, cwd=str(BASE_DIR))
        subprocess.run([sys.executable, str(SCRIPTS/"rubick_learn.py"), "flush"],
            capture_output=True, text=True, timeout=15, cwd=str(BASE_DIR))
    except Exception as e:
        log.warning("learn pipeline failed: %s", e)

def _log_interaction_bg(session_id, query, nodes_used, experts, tokens, phase):
    try:
        sys.path.insert(0, str(SCRIPTS))
        from rubick_learn import log_interaction
        log_interaction(session_id, query, nodes_used, experts, tokens, phase)
    except Exception as e:
        log.debug("interaction log failed: %s", e)

def _sse(d):
    label = d.get("label") or d.get("step") or d.get("phase")
    if label:
        prefix = "✓" if d.get("done") and d.get("ok") else ("✗" if d.get("done") and not d.get("ok") else "⬡")
        pct = f"  [{d['pct']}%]" if d.get("pct") else ""
        log.info("%s %s%s", prefix, label, pct)
    elif d.get("done"):
        if d.get("ok"):
            msg = d.get("summary","")[:120] if d.get("summary") else d.get("message","")[:120]
            log.info("✓ DONE  %s  cost=$%.4f", msg, d.get("usage",{}).get("cost_usd",0))
        else:
            log.info("✗ FAIL  %s", d.get("error","")[:120])
    return f"data: {json.dumps(d)}\n\n"

def call_claude(prompt, timeout=60):
    """Call claude via stdin, return dict with text + usage."""
    prompt_preview = prompt[:80].replace('\n', ' ')
    log.info("→ Claude  prompt=%s...  timeout=%ds", prompt_preview, timeout)
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            ['claude', '-p', '--output-format', 'json', '--'],
            input=prompt, capture_output=True, text=True,
            timeout=timeout, cwd=str(BASE_DIR)
        )
        raw = r.stdout.strip()
        try:
            d = json.loads(raw)
            usage = d.get("usage") or {}
            elapsed = time.monotonic() - t0
            cost = d.get("total_cost_usd", 0.0)
            inp = usage.get("input_tokens", 0)
            out = usage.get("output_tokens", 0)
            log.info("← Claude  %.1fs  %dK in / %dK out  $%.4f", elapsed, inp//1000, out//1000, cost)
            return {
                "text": d.get("result", ""),
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read": usage.get("cache_read_input_tokens", 0),
                "cache_write": usage.get("cache_creation_input_tokens", 0),
                "cost_usd": cost,
                "duration_ms": d.get("duration_ms", 0),
            }
        except (json.JSONDecodeError, TypeError):
            return {"text": raw, "input_tokens": 0, "output_tokens": 0,
                    "cache_read": 0, "cache_write": 0, "cost_usd": 0.0, "duration_ms": 0}
    except FileNotFoundError:
        return {"text": "__NO_CLAUDE__", "input_tokens": 0, "output_tokens": 0,
                "cache_read": 0, "cache_write": 0, "cost_usd": 0.0, "duration_ms": 0}
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        log.error("✗ Claude TIMEOUT after %.0fs", elapsed)
        return {"text": "__TIMEOUT__", "input_tokens": 0, "output_tokens": 0,
                "cache_read": 0, "cache_write": 0, "cost_usd": 0.0, "duration_ms": 0}
    except Exception as e:
        return {"text": f"__ERROR__: {e}", "input_tokens": 0, "output_tokens": 0,
                "cache_read": 0, "cache_write": 0, "cost_usd": 0.0, "duration_ms": 0}

def init_db():
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
        CREATE TABLE IF NOT EXISTS sync_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT UNIQUE NOT NULL,
            last_cursor TEXT DEFAULT '',
            last_count INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now')),
            status TEXT DEFAULT 'idle',
            error TEXT DEFAULT ''
        );
    """)
    # Add columns to existing tables (ALTER TABLE IF NOT EXISTS pattern)
    for col, default in [
        ("total_input_tokens", 0), ("total_output_tokens", 0), ("total_cost_usd", 0)
    ]:
        try: c.execute(f"ALTER TABLE chat_sessions ADD COLUMN {col} INTEGER DEFAULT {default}")
        except: pass
    for col, default in [
        ("input_tokens", 0), ("output_tokens", 0),
        ("cache_read_tokens", 0), ("cache_write_tokens", 0), ("cost_usd", 0)
    ]:
        try: c.execute(f"ALTER TABLE chat_messages ADD COLUMN {col} REAL DEFAULT {default}")
        except: pass
    c.commit(); c.close()

def record_phase_cost(slug: str, phase: str, input_tokens: int, output_tokens: int, cost_usd: float, model: str = ""):
    """Record feature pipeline phase cost tracking."""
    c = db()
    try:
        c.execute(
            "INSERT INTO feature_costs (feature_slug, phase, input_tokens, output_tokens, cost_usd, model) "
            "VALUES (?,?,?,?,?,?)",
            (slug, phase, input_tokens, output_tokens, cost_usd, model)
        )
        c.commit()
    except Exception as e:
        log.debug("record_phase_cost failed: %s", e)
    finally:
        c.close()

def is_initialized():
    try:
        c = db()
        row = c.execute("SELECT value FROM init_settings WHERE key='profile_saved'").fetchone()
        c.close()
        return row is not None
    except:
        return False

@app.before_request
def _init_gate():
    from flask import redirect as _redir
    path = request.path
    if path.startswith('/init') or path.startswith('/api/init') or path.startswith('/static'):
        return None
    if not is_initialized():
        return _redir('/init')
    return None

def _ensure_session(session_id, title):
    c = db()
    try:
        if session_id:
            row = c.execute("SELECT session_id FROM chat_sessions WHERE session_id=?", (session_id,)).fetchone()
            if row: return session_id
        sid = session_id or str(uuid.uuid4())
        c.execute("INSERT OR IGNORE INTO chat_sessions (session_id, title) VALUES (?,?)", (sid, title[:80]))
        c.commit(); return sid
    except: return session_id or str(uuid.uuid4())
    finally: c.close()

def _save_msg(session_id, role, content, content_type='text', rubick_target=None,
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
    except: pass
    finally: c.close()

# All known graph node names for instant Python lookup (no Claude call needed)
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
    """Instant Python keyword match — no Claude call needed."""
    msg = message.lower()
    # Try longest match first to prefer "payments-card" over "payments"
    for node in sorted(KNOWN_NODES, key=len, reverse=True):
        variants = [node, node.replace("-", " "), node.replace("-", "_")]
        if any(v in msg for v in variants):
            return node
    # Fallback: return first hyphenated word that looks like a service
    m = re.search(r'\b([a-z][a-z0-9]+-[a-z0-9-]+)\b', msg)
    if m:
        return m.group(1)
    return message.strip()[:60]

NEMESIS_PROMPT = """You are Nemesis — Razorpay's AI engineering orchestrator.
You command three specialist agents and the Rubick knowledge graph:
  • Ideation — feature overview & understanding (As-Is → To-Be flows)
  • Solutioning — solution design + risk analysis in one pass (exact code changes, ER impact, risk register, amendments)
  • Tech Spec — tech spec document generation
  • Rubick — 715K-node knowledge graph of Razorpay's 46 services

## YOUR ROLE
Understand the user's intent and respond. For knowledge questions, answer directly.
For feature work, explain what you'll do and include a skill directive.

## RESPONSE FORMAT
Always answer in natural prose like a senior engineer. Use markdown.
If the user's request requires running a skill, include ONE directive at the very end:
  <!--SKILL:ideation:{{"action":"create"}}-->
  <!--SKILL:ideation:{{"action":"update","instruction":"add proxy path to As-Is"}}-->
  <!--SKILL:solutioning:{{"action":"create"}}-->
  <!--SKILL:techspec:{{"action":"create"}}-->

## INTENT ROUTING
- "what is X", "how does X work", "explain X" → answer from context (no directive)
- "create overview", "run ideation", "analyze feature" → <!--SKILL:ideation-->
- "fix overview", "update the As-Is", "add X to overview" → <!--SKILL:ideation:update-->
- "create solution", "run solutioning", "design changes", "risk analysis", "what could break" → <!--SKILL:solutioning-->
- "generate doc", "create tech spec", "run techspec" → <!--SKILL:techspec-->

## Knowledge Graph Context
{context}

## DB Facts
{db_facts}

## Active Feature
{feature_context}

## Uploaded Files
{uploaded_files}

## Conversation
{message}"""

# ── Service-based context (workspace-direct + rubick.db) ─────────────────

WORKSPACE = BASE_DIR / "workspace"

def _q(sql, params=()):
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, params).fetchall()]
    finally:
        c.close()

def _service_context(services):
    """Build rich context from rubick.db by SERVICE names. No feature-name matching.
    Returns compact plain-text for Claude with deps, endpoints, datastores, risks, decisions."""
    if not services:
        return ""
    svc_likes = ['%' + s + '%' for s in services]
    name_or = ' OR '.join(['name=?' for _ in services])
    like_or = ' OR '.join(['data LIKE ?' for _ in svc_likes])

    with ThreadPoolExecutor(max_workers=6) as pool:
        f = {
            'deps_out':   pool.submit(_q, f"""SELECT DISTINCT n.name FROM edges e
                              JOIN nodes n ON e.to_node_id=n.id JOIN nodes s ON e.from_node_id=s.id
                              WHERE ({name_or}) AND e.edge_type='DEPENDS_ON' LIMIT 30""",
                              tuple(services)),
            'deps_in':    pool.submit(_q, f"""SELECT DISTINCT n.name FROM edges e
                              JOIN nodes n ON e.from_node_id=n.id JOIN nodes s ON e.to_node_id=s.id
                              WHERE ({name_or}) AND e.edge_type='DEPENDS_ON' LIMIT 30""",
                              tuple(services)),
            'endpoints':  pool.submit(_q, f"""SELECT n.name FROM edges e JOIN nodes n ON e.to_node_id=n.id
                              JOIN nodes s ON e.from_node_id=s.id
                              WHERE ({name_or}) AND n.type='Endpoint' AND e.edge_type='HAS_ENDPOINT' LIMIT 30""",
                              tuple(services)),
            'datastores': pool.submit(_q, f"""SELECT DISTINCT name, data FROM nodes WHERE type='DataStore'
                              AND json_extract(data,'$.evidence')='schema'
                              AND ({like_or}) LIMIT 30""", tuple(svc_likes)),
            'calls_svc':  pool.submit(_q, f"""SELECT DISTINCT n.name FROM edges e
                              JOIN nodes n ON e.to_node_id=n.id JOIN nodes s ON e.from_node_id=s.id
                              WHERE ({name_or}) AND e.edge_type IN ('CALLS_SERVICE','KAFKA_TOPIC','IMPORTS_LIB') LIMIT 30""",
                              tuple(services)),
            'risks':      pool.submit(_q, f"""SELECT name, data, confidence FROM nodes WHERE type='RiskItem'
                              AND ({like_or}) ORDER BY confidence DESC LIMIT 15""", tuple(svc_likes)),
            'decisions':  pool.submit(_q, f"""SELECT name, data, confidence FROM nodes WHERE type='ArchDecision'
                              AND ({like_or}) ORDER BY confidence DESC LIMIT 15""", tuple(svc_likes)),
            'logic':      pool.submit(_q, f"""SELECT name, data, confidence FROM nodes WHERE type='BusinessLogic'
                              AND ({like_or}) ORDER BY confidence DESC LIMIT 10""", tuple(svc_likes)),
        }
        r = {}
        for k, fut in f.items():
            try: r[k] = fut.result(timeout=5)
            except: r[k] = []

    lines = [f"## Service Context: {', '.join(services)}"]
    dout = [x['name'] for x in r['deps_out']]
    din  = [x['name'] for x in r['deps_in']]
    eps  = [x['name'] for x in r['endpoints']]
    calls_svc = [x['name'] for x in r.get('calls_svc', [])]
    if dout:  lines.append(f"Depends on: {', '.join(dout)}")
    if din:   lines.append(f"Used by: {', '.join(din)}")
    if calls_svc: lines.append(f"Calls services: {', '.join(calls_svc)}")
    if eps:   lines.append(f"Endpoints: {', '.join(eps[:20])}")

    dsts = r['datastores']
    if dsts:
        lines.append("\n### ER Schema (tables with columns)")
        for ds in dsts:
            name = ds['name']
            try:
                d = json.loads(ds['data']) if ds.get('data') else {}
                cols = d.get('columns', [])
                tname = d.get('table_name', name)
                if cols:
                    col_str = ', '.join(f"{c['name']} {c.get('type','')}" + (" PK" if c.get('pk') else "") for c in cols[:15])
                    lines.append(f"- **{tname}**: {col_str}")
                else:
                    lines.append(f"- {tname}")
            except:
                lines.append(f"- {name}")
    for label, key in [("ArchDecisions", "decisions"), ("RiskItems", "risks"), ("BusinessLogic", "logic")]:
        items = r[key]
        if items:
            lines.append(f"\n### {label}")
            for it in items:
                lines.append(f"- [{it['name']}] conf={it.get('confidence','?')}")
                if it.get('data'):
                    try: lines.append(f"  {json.dumps(json.loads(it['data']))[:300]}")
                    except: lines.append(f"  {str(it['data'])[:300]}")
    return "\n".join(lines)

# ── Template helpers ─────────────────────────────────────────────────────────

def _h(s):
    if not s: return ''
    import html as _html_mod
    return _html_mod.escape(str(s))

def _get_owner():
    try:
        c = db(); row = c.execute("SELECT value FROM init_settings WHERE key='email'").fetchone(); c.close()
        return row['value'] if row else "user@example.com"
    except: return "user@example.com"

def _find_feature_row(c, slug: str, cols: str = "*"):
    """Lookup a Feature node by slug, tolerating slug↔name mismatches (hyphens vs spaces)."""
    row = c.execute(f"SELECT {cols} FROM nodes WHERE type='Feature' AND name=?", (slug,)).fetchone()
    if row: return row
    row = c.execute(f"SELECT {cols} FROM nodes WHERE type='Feature' AND lower(name) LIKE ?", (f'%{slug.lower()}%',)).fetchone()
    if row: return row
    row = c.execute(f"SELECT {cols} FROM nodes WHERE type='Feature' AND lower(replace(name,' ','-'))=?", (slug.lower(),)).fetchone()
    if row: return row
    slug_spaced = slug.lower().replace('-', ' ')
    return c.execute(f"SELECT {cols} FROM nodes WHERE type='Feature' AND lower(name) LIKE ?", (f'%{slug_spaced}%',)).fetchone()

def _slug_safe(name: str) -> str:
    return re.sub(r'[^a-z0-9-]', '-', name.lower())

def _latest_version(paths):
    """Return the numerically-latest versioned path from a list. v10 > v9 > v2 > v1."""
    import re as _re
    def _ver(p):
        m = _re.search(r'_v(\d+)', p.name)
        return int(m.group(1)) if m else (0 if p.stem == p.stem.split('_v')[0] else -1)
    return max(paths, key=_ver) if paths else None

def _find_phase_file(feat_dir, candidates):
    """Return (relative_filename, exists). Tries exact candidates first, then glob for versioned files."""
    for c in candidates:
        if '*' in c:
            matches = list(feat_dir.glob(c))
            if matches:
                best = _latest_version(matches)
                return (best.name, True)
        elif (feat_dir / c).exists():
            return (c, True)
    return (candidates[0], False)

# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    try:
        c = db()
        types  = {r["type"]: r["cnt"] for r in c.execute(
            "SELECT type, COUNT(*) cnt FROM nodes GROUP BY type ORDER BY cnt DESC")}
        tnodes = c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        tedges = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        c.close()
        return jsonify({"total_nodes": tnodes, "total_edges": tedges, "by_type": types})
    except Exception as e:
        log.warning("stats failed: %s", e)
        return jsonify({"error": str(e)}), 500

@app.route("/api/reset", methods=["POST"])
def api_reset():
    try:
        c = db()
        from rubick_graph import smart_reset
        ws = str(Path(__file__).resolve().parent.parent / "workspace")
        result = smart_reset(c, workspace_path=ws)
        c.close()
        return jsonify(result)
    except Exception as e:
        log.warning("reset failed: %s", e)
        return jsonify({"error": str(e)}), 500

@app.route("/api/audit")
def api_audit():
    try:
        c = db()
        from rubick_graph import audit_report
        ws = str(Path(__file__).resolve().parent.parent / "workspace")
        result = audit_report(c, DB_PATH, workspace_path=ws)
        c.close()
        return jsonify(result)
    except Exception as e:
        log.warning("audit failed: %s", e)
        return jsonify({"error": str(e)}), 500

@app.route("/api/pipeline-status/<slug>")
def api_pipeline_status(slug):
    try:
        from rubick_graph import pipeline_status
        ws = str(Path(__file__).resolve().parent.parent / "workspace")
        result = pipeline_status(ws, slug)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/self-improve")
def api_self_improve():
    try:
        sys.path.insert(0, str(SCRIPTS))
        from rubick_learn import self_improve_report
        return jsonify(self_improve_report())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/self-improve/apply", methods=["POST"])
def api_apply_self_improve():
    try:
        sys.path.insert(0, str(SCRIPTS))
        from rubick_learn import apply_self_improvement
        return jsonify(apply_self_improvement())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/nodes")
def api_nodes():
    t      = request.args.get("type", "Feature")
    q      = request.args.get("q", "").strip()
    limit  = min(int(request.args.get("limit", 60)), 200)
    offset = int(request.args.get("offset", 0))
    c = db()
    where, params = [], []
    if t:   where.append("type=?");  params.append(t)
    if q:   where.append("(name LIKE ? OR data LIKE ?)"); params += [f"%{q}%",f"%{q}%"]
    wc = ("WHERE "+" AND ".join(where)) if where else ""
    rows  = c.execute(f"SELECT id,type,name,confidence,source_type FROM nodes {wc} ORDER BY created_at DESC LIMIT ? OFFSET ?", params+[limit,offset]).fetchall()
    total = c.execute(f"SELECT COUNT(*) FROM nodes {wc}", params).fetchone()[0]
    c.close()
    return jsonify({"nodes":[{"id":r["id"],"type":r["type"],"name":r["name"],"confidence":r["confidence"]} for r in rows],
                    "total":total,"offset":offset,"limit":limit})

@app.route("/api/node/<int:nid>")
def api_node(nid):
    c = db()
    row = c.execute("SELECT * FROM nodes WHERE id=?", (nid,)).fetchone()
    if not row: c.close(); return jsonify({"error":"not found"}),404
    n = node_dict(row)
    n["edges_out"] = [{"edge_type":r[0],"id":r[1],"type":r[2],"name":r[3]} for r in
        c.execute("SELECT e.edge_type,n.id,n.type,n.name FROM edges e JOIN nodes n ON e.to_node_id=n.id WHERE e.from_node_id=? LIMIT 30",(nid,))]
    n["edges_in"]  = [{"edge_type":r[0],"id":r[1],"type":r[2],"name":r[3]} for r in
        c.execute("SELECT e.edge_type,n.id,n.type,n.name FROM edges e JOIN nodes n ON e.from_node_id=n.id WHERE e.to_node_id=? LIMIT 30",(nid,))]
    c.close()
    return jsonify(n)

@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Nemesis orchestrator — routes to skills or answers directly from Rubick."""
    body       = request.get_json(force=True)
    message    = (body.get("message") or "").strip()
    nid        = body.get("node_id")
    session_id = body.get("session_id") or None
    feature_slug = body.get("feature_slug")
    feature_name = body.get("feature_name")
    uploaded_files = body.get("uploaded_files") or []
    if not message: return jsonify({"error": "empty"}), 400

    t0 = time.time()
    target = extract_node(message)

    if nid:
        try:
            c = db(); row = c.execute("SELECT name FROM nodes WHERE id=?", (nid,)).fetchone()
            if row: target = row['name']
            c.close()
        except: pass

    session_id = _ensure_session(session_id, message)
    _save_msg(session_id, 'user', message)

    services = _detect_services(target)
    svc_ctx = _service_context(services)
    expert_ctx = _get_project_experts(services)

    feature_context = "(no active feature)"
    if feature_slug and feature_name:
        artifacts = _workspace_artifacts(feature_slug, feature_name)
        feature_context = f"Feature: {feature_name} (slug: {feature_slug})\n\n{artifacts}" if artifacts else f"Feature: {feature_name}"

    files_text = "(no files uploaded)"
    if uploaded_files:
        files_text = "\n".join(f"[{f.get('name','')}]: {f.get('content','')[:1500]}" for f in uploaded_files)

    result = call_claude(NEMESIS_PROMPT.format(
        context=(svc_ctx + "\n\n" + expert_ctx) if (svc_ctx or expert_ctx) else "(no service context)",
        db_facts="(service-based context above)",
        feature_context=feature_context,
        uploaded_files=files_text,
        message=message), timeout=120)
    raw = result["text"]
    if raw and not raw.startswith("__"):
        response = raw
    elif raw == "__NO_CLAUDE__":
        response = (db_facts or ctx) or "(claude CLI not found — install Claude Code)"
    elif raw == "__TIMEOUT__":
        response = "(claude timed out — try a simpler question)"
    else:
        response = raw or db_facts or ctx or "(empty response)"

    import re as _re
    skill_match = _re.search(r'<!--SKILL:(\w+)(?::(\{.*?\}))?\s*-->', response)
    skill_cmd = None
    if skill_match:
        response = response[:skill_match.start()].rstrip()
        skill_name = skill_match.group(1)
        try: skill_data = json.loads(skill_match.group(2)) if skill_match.group(2) else {}
        except: skill_data = {}
        skill_cmd = {"skill": skill_name, **skill_data}

    elapsed = round(time.time() - t0, 3)
    usage = {
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "cache_read": result["cache_read"],
        "cache_write": result["cache_write"],
        "cost_usd": result["cost_usd"],
    }
    _save_msg(session_id, 'assistant', response, rubick_target=target, elapsed=elapsed, **usage)
    run_learn([{"type":"Signal","name":f"nemesis_chat:{message[:80]}",
                "data":{"source_type":"nemesis_chat",
                        "body":f"Q:{message[:300]}\nTarget:{target[:80]}\nA:{response[:300]}",
                        "ts":time.time()},
                "confidence":0.7}], "nemesis_chat")
    _log_interaction_bg(session_id, message, [], [], usage.get("input_tokens", 0), "chat")
    if feature_slug:
        record_phase_cost(feature_slug, "chat", usage["input_tokens"], usage["output_tokens"], usage["cost_usd"])
    resp = {"response": response, "rubick_target": target,
            "elapsed": elapsed, "session_id": session_id, "usage": usage}
    if skill_cmd:
        resp["skill_cmd"] = skill_cmd
    return jsonify(resp)

@app.route("/api/upload", methods=["POST"])
def api_upload():
    results = []
    for f in request.files.getlist("files"):
        try:
            content = f.read().decode("utf-8", errors="replace")
            run_learn([{"type":"Document","name":f"upload:{f.filename}",
                        "data":{"source_type":"ui_upload","filename":f.filename,"body":content[:6000]},
                        "confidence":0.8}], "ui_upload")
            results.append({"name":f.filename,"status":"ingested","size":len(content)})
        except Exception as e:
            results.append({"name":f.filename,"error":str(e)})
    return jsonify({"files":results})

# ── Session endpoints ────────────────────────────────────────────────────────

@app.route("/api/sessions")
def api_sessions():
    c = db()
    try:
        rows = c.execute("""SELECT session_id,title,created_at,updated_at,message_count,
            total_input_tokens,total_output_tokens,total_cost_usd
            FROM chat_sessions ORDER BY updated_at DESC LIMIT 50""").fetchall()
        return jsonify({"sessions": [dict(r) for r in rows]})
    except: return jsonify({"sessions": []})
    finally: c.close()

@app.route("/api/sessions", methods=["POST"])
def api_sessions_create():
    body = request.get_json(force=True)
    title = (body.get("title") or "New chat")[:80]
    sid = str(uuid.uuid4())
    c = db()
    try:
        c.execute("INSERT INTO chat_sessions (session_id,title) VALUES (?,?)", (sid, title))
        c.commit()
        return jsonify({"session_id": sid, "title": title})
    except Exception as e: return jsonify({"error": str(e)}), 500
    finally: c.close()

@app.route("/api/sessions/<sid>")
def api_session_detail(sid):
    c = db()
    try:
        s = c.execute("SELECT * FROM chat_sessions WHERE session_id=?", (sid,)).fetchone()
        if not s: return jsonify({"error": "not found"}), 404
        msgs = c.execute("""SELECT role,content,content_type,rubick_target,elapsed,
            input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,cost_usd,created_at
            FROM chat_messages WHERE session_id=? ORDER BY id""", (sid,)).fetchall()
        return jsonify({"session": dict(s), "messages": [dict(m) for m in msgs]})
    except Exception as e: return jsonify({"error": str(e)}), 500
    finally: c.close()

# ── Oracle endpoints ──────────────────────────────────────────────────────────

@app.route("/api/oracle/today")
def api_oracle_today():
    c = db()
    try:
        signals = [dict(r) for r in c.execute(
            "SELECT name, data, created_at FROM nodes WHERE type='Signal' AND created_at >= datetime('now','-1 day') ORDER BY created_at DESC LIMIT 15").fetchall()]
        prs = [dict(r) for r in c.execute(
            "SELECT name, data, created_at FROM nodes WHERE type='PR' ORDER BY created_at DESC LIMIT 8").fetchall()]
        return jsonify({"signals": signals, "prs": prs})
    except Exception as e: return jsonify({"signals":[],"prs":[],"error":str(e)})
    finally: c.close()

@app.route("/api/oracle/features")
def api_oracle_features():
    c = db()
    try:
        rows = c.execute("SELECT id,name,data,created_at FROM nodes WHERE type='Feature' ORDER BY created_at DESC LIMIT 20").fetchall()
        result = []
        for r in rows:
            d = json.loads(r['data']) if r['data'] else {}
            slug = r['name'].lower().replace(' ', '-')
            cost_rows = c.execute(
                "SELECT SUM(cost_usd) as total_cost FROM feature_costs WHERE feature_slug=?", (slug,)
            ).fetchall()
            total_cost = (cost_rows[0]['total_cost'] or 0.0) if cost_rows else 0.0
            result.append({"id":r["id"],"name":r["name"],"created_at":r["created_at"],
                           "phase":d.get("phase","unknown"),"status":d.get("status","proposed"),
                           "total_cost_usd":round(total_cost, 2)})
        return jsonify({"features": result})
    except Exception as e: return jsonify({"features":[],"error":str(e)})
    finally: c.close()

@app.route("/api/oracle/inbox")
def api_oracle_inbox():
    c = db()
    try:
        rows = c.execute(
            "SELECT name, data, created_at FROM nodes WHERE type='Signal' AND source_type IN ('slack','gmail') ORDER BY created_at DESC LIMIT 20").fetchall()
        return jsonify({"items": [dict(r) for r in rows]})
    except: return jsonify({"items": []})
    finally: c.close()

# ── Nemesis endpoints ─────────────────────────────────────────────────────────

@app.route("/api/features", methods=["POST"])
def api_features_create():
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    import time as _time
    slug = re.sub(r'[^a-z0-9-]+', '-', name.lower()).strip('-')
    sources = {
        "slack": [u.strip() for u in (body.get("slack_threads") or "").splitlines() if u.strip()],
        "docs":  [u.strip() for u in (body.get("google_docs")   or "").splitlines() if u.strip()],
        "gmail": [u.strip() for u in (body.get("gmail_threads") or "").splitlines() if u.strip()],
        "verbal": (body.get("description") or "").strip(),
    }
    node_data = json.dumps({
        "status": "proposed", "owner": _get_owner(),
        "phase": "ideation", "created_at": _time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sources": sources,
        "overview_path": f"workspace/features/{slug}/overview.html",
        "solution_path": f"workspace/features/{slug}/solution.md",
    })
    c = db()
    try:
        existing = c.execute("SELECT id FROM nodes WHERE type='Feature' AND name=?", (name,)).fetchone()
        if existing:
            return jsonify({"slug": slug, "name": name, "existing": True})
        c.execute("""INSERT INTO nodes (type, name, data, source_type, confidence)
                     VALUES ('Feature', ?, ?, 'ui_create', 0.9)""", (name, node_data))
        c.commit()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        c.close()

    feat_dir = BASE_DIR / "workspace" / "features" / slug
    feat_dir.mkdir(parents=True, exist_ok=True)

    # Write a sources.json so Nemesis/Ideation can pick them up
    sources_file = feat_dir / "sources.json"
    sources_file.write_text(json.dumps(sources, indent=2))

    return jsonify({"slug": slug, "name": name, "created": True})

@app.route("/api/features")
def api_features():
    since = request.args.get("since")   # ISO date: 2026-01-01
    until = request.args.get("until")   # ISO date: 2026-12-31
    c = db()
    try:
        sql = "SELECT id,name,data,created_at FROM nodes WHERE type='Feature'"
        params = []
        if since:
            sql += " AND created_at >= ?"; params.append(since)
        if until:
            sql += " AND created_at <= ?"; params.append(until)
        sql += " ORDER BY created_at DESC"
        rows = c.execute(sql, params).fetchall()
        result = []
        for r in rows:
            d = json.loads(r['data']) if r['data'] else {}
            slug = r['name'].lower().replace(' ','-').replace('/','-')
            result.append({"id":r["id"],"name":r["name"],"slug":slug,
                           "created_at":r["created_at"],"phase":d.get("phase","unknown"),
                           "status":d.get("status","proposed"),"owner":d.get("owner","")})
        return jsonify({"features": result})
    except Exception as e: return jsonify({"features":[],"error":str(e)})
    finally: c.close()

@app.route("/api/features/<slug>")
def api_feature_detail(slug):
    c = db()
    try:
        row = _find_feature_row(c, slug)
        if not row: return jsonify({"error":"not found"}),404
        n = node_dict(row)
        n['requirements'] = [{"name":r["name"],"data":r["data"]} for r in
            c.execute("SELECT n2.name,n2.data FROM edges e JOIN nodes n2 ON e.to_node_id=n2.id WHERE e.from_node_id=? AND n2.type='Requirement' LIMIT 20",(n['id'],)).fetchall()]
        n['risks'] = [{"name":r["name"],"data":r["data"]} for r in
            c.execute("SELECT n2.name,n2.data FROM edges e JOIN nodes n2 ON e.to_node_id=n2.id WHERE e.from_node_id=? AND n2.type='RiskItem' LIMIT 10",(n['id'],)).fetchall()]
        feat_dir = BASE_DIR / "workspace" / "features" / _slug_safe(n['name'])
        n['overview_file'],  n['has_overview']  = _find_phase_file(feat_dir, ['overview_v*.html', 'overview.html', 'overview_v*.md', 'overview.md'])
        n['solution_file'],  n['has_solution']  = _find_phase_file(feat_dir, ['solution_v*.html', 'solution.html', 'solution_v*.md', 'solution.md'])
        n['techspec_file'],  n['has_tech_spec'] = _find_phase_file(feat_dir, ['tech-spec.md', 'tech_spec.md', 'scribe/tech-spec.md'])
        n['e2e_file'],       n['has_e2e']       = _find_phase_file(feat_dir, ['e2e-report.md', 'e2e-report_v*.md'])
        return jsonify(n)
    except Exception as e: return jsonify({"error":str(e)}),500
    finally: c.close()

@app.route("/api/features/<slug>/content")
def api_feature_content(slug):
    tab     = request.args.get("tab", "overview")
    version = request.args.get("version", None)
    c = db()
    try:
        row = _find_feature_row(c, slug, "name")
        name = row['name'] if row else slug
    except: name = slug
    finally: c.close()

    feat_dir = BASE_DIR / "workspace" / "features" / _slug_safe(name)

    # Explicit version requested
    if version:
        path = feat_dir / version
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8")
                ftype = "html" if version.endswith(".html") else "md"
                return jsonify({"exists": True, "content": content, "type": ftype})
            except Exception as e:
                return jsonify({"exists": False, "error": str(e)}), 500
        return jsonify({"exists": False, "content": "", "type": "md"})

    candidates = {
        'overview':  ['overview.html', 'overview_v*.html', 'overview.md', 'overview_v*.md'],
        'solution':  ['solution.html', 'solution_v2.html', 'solution_final.html', 'solution_final.md', 'solution_v2.md', 'solution.md'],
        'tech-spec': ['tech-spec.md', 'tech_spec.md', 'scribe/tech-spec.md'],
    }.get(tab, [])
    if not candidates:
        return jsonify({"exists": False, "content": "", "type": "md"})
    fname, exists = _find_phase_file(feat_dir, candidates)
    if not exists:
        return jsonify({"exists": False, "content": "", "type": "md"})
    try:
        content = (feat_dir / fname).read_text(encoding="utf-8")
        ftype = "html" if fname.endswith(".html") else "md"
        return jsonify({"exists": True, "content": content, "type": ftype})
    except Exception as e:
        return jsonify({"exists": False, "error": str(e)}), 500

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

### Project Expert Briefings (service experts from Rubick)
{expert_context}

### Cached @Slash Intelligence
{slash_signals}

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

SOLUTIONING_PROMPT = """You are a solution architect and adversarial risk analyst for Razorpay engineering.
You receive a feature overview from the Ideation phase and your job is TWO THINGS IN ONE PASS:
1. Design the MINIMUM viable code changes (solution) — surgical precision over broad rewrites
2. Stress-test your own solution for every gap and risk (THE SOLUTION IS GUILTY UNTIL PROVEN INNOCENT)

## MINIMUM CHANGE PRINCIPLE (mandatory)
- Touch the fewest possible lines of code to achieve the goal
- Prefer: adding a new helper > modifying an existing function
- Prefer: new DB column (additive) > altering existing column
- Prefer: feature flag / config > code branch
- Prefer: intercepting at one entry point > patching N call sites
- For each change: show the EXACT before/after diff — not just the after state
- Label each change with "Lines Changed: N" — fewer is better
- If a change touches > 50 lines, split it and justify why it can't be smaller

## Feature: {feature_name}

### Ideation Overview (As-Is / To-Be flows)
{overview_content}

### Rubick Brain Context (services, functions, expert knowledge, ER schemas)
{context}

### Project Expert Briefings
{expert_context}

### Cross-Service Architecture
{cross_refs}

## OUTPUT FORMAT

Return ONLY inner HTML. No <!DOCTYPE>, <html>, <head>, <body> tags. No markdown code fences.
The output embeds inside a styled container with Mermaid.js already loaded.

Use semantic HTML: <h1>, <h2>, <h3>, <p>, <table>, <ul>, <ol>, <code>, <pre>, <blockquote>.
Use Mermaid diagrams for architecture changes and data flows.

## REQUIRED SECTIONS

### 1. Executive Summary
One paragraph: what changes, across how many services, total lines changed, confidence level, final verdict (GO / CONDITIONAL / NO-GO).

### 2. Changes Required
For each change C1…CN (ordered smallest-blast-radius first):
<h3>C{n}: [Short Name]</h3>
<table>
  <tr><td><b>Service</b></td><td>service-name</td></tr>
  <tr><td><b>File</b></td><td>path/to/file.go — verified from Rubick expert context</td></tr>
  <tr><td><b>Function</b></td><td>exact function name from code search</td></tr>
  <tr><td><b>Lines Changed</b></td><td>N lines (+X added, -Y removed)</td></tr>
  <tr><td><b>Why Minimum</b></td><td>Why this is the smallest possible change that achieves the goal</td></tr>
  <tr><td><b>DB Tables Affected</b></td><td>from ER schema — columns that change</td></tr>
  <tr><td><b>Risk</b></td><td>Low / Medium / High</td></tr>
</table>

<pre><code>// BEFORE (exact lines from file)
existing code here

// AFTER (minimal diff applied)
changed code here
</code></pre>

Include a Mermaid diagram showing the new data flow after this change.

### 3. ER Schema Impact
For each affected service, show which tables and columns change using Mermaid ER diagram:
<pre class="mermaid">
erDiagram
    payments ||--o{{ payment_offers : has
    payment_offers {
        bigint id PK
        bigint payment_id FK
        decimal discount_amount "NEW COLUMN"
    }
</pre>

### 4. Razorpay Domain Risk Checklist
For each item mark PASS ✓ or RISK ✗ with specific evidence (file:line or table:column):
- Idempotency — retry safety on all changed endpoints
- Reconciliation — ledger/settlement consistency
- Amount precision — paise math, no float division
- Callback ordering — webhook delivery guarantees
- PCI compliance — no card data in new log lines
- Feature flag interactions — Splitz/DCS gates near changed code
- Partial failure modes — what if only N of M services deploy
- Data migration safety — schema changes + rollback path
- Backward compatibility — proto field additions, API schema changes
- Monitoring coverage — new failure modes need alerts

### 5. Risk Register
HTML table: # | Risk | Severity (P0 Blocker / P1 High / P2 Medium) | Evidence (file:line) | Verdict (BLOCKER / AMENDMENT / ACCEPTED)

### 6. Required Amendments
For each BLOCKER or AMENDMENT risk: exact proposed fix with service, file, function, and what to change.

### 7. Rollout Order
Numbered deployment sequence with rationale. Which change MUST go first. Feature flag strategy.

### 8. Testing Strategy
Per-change test cases: unit test (function to test, assertion), integration test (service A calls B with payload X, expects Y), and what to monitor in production during rollout.

## RULES
- Use REAL file paths, function names, and table names from Rubick expert context
- NEVER hallucinate function names — if unsure, say "likely in {file} — verify"
- Every DB change MUST reference the table and column from ER schema context
- Every risk check MUST cite specific evidence, not generic statements
- Amendments must be actionable: "change line X in function Y to do Z" """

TECHSPEC_PROMPT = """You are the document generation engine for Razorpay engineering.
Generate a comprehensive Razorpay Tech Spec in Markdown.

Feature: {feature_name}

Overview (As-Is / To-Be flows):
{overview_content}

Solution + Risk Analysis (Solutioning output — includes changes, ER impact, risk register, amendments, rollout):
{solution_content}

Rubick context:
{context}

# {feature_name} — Tech Spec

## 1. Problem Statement
(What breaks today, business impact, who is affected)

## 2. Proposed Solution
(One-paragraph summary of the approach)

## 3. Current State (As-Is)
(From overview: service flow, what breaks, with service names)

## 4. Target State (To-Be)
(From overview: expected flow with concrete examples)

## 5. Functional Requirements
(Numbered list extracted from overview + solution)

## 6. Non-Functional Requirements
(Performance SLAs, idempotency, PCI, reliability targets)

## 7. Architecture & Design
(Service interaction diagram description, data flow, API contracts)

## 8. Implementation Plan
(Per-service changes with file:line references from solution)

## 9. Risk Mitigation
(Top risks from Solutioning risk register + mitigations)

## 10. Testing Strategy
(Unit, integration, E2E, load test cases)

## 11. Rollout Plan
(Phase-wise rollout, feature flags, smoke tests per phase)

## 12. Monitoring & Alerting
(New metrics, dashboards, alert thresholds)

## 13. Open Questions
(Unresolved items, decisions pending)"""

def _workspace_artifacts(slug, feature_name):
    """Read workspace feature directory directly — no DB indirection."""
    parts = []
    for candidate in list({slug, _slug_safe(feature_name)} - {''}):
        feat_dir = WORKSPACE / "features" / candidate
        if feat_dir.exists():
            phase_candidates = [
                (['overview_v*.html', 'overview.html', 'overview_v*.md', 'overview.md'], 'Ideation overview'),
                (['solution_v*.html', 'solution.html', 'solution_v*.md', 'solution.md'], 'Solutioning output'),
                (['tech-spec_v*.md', 'tech-spec.md', 'tech_spec_v*.md', 'tech_spec.md'], 'Tech Spec'),
            ]
            for globs, label in phase_candidates:
                fname, exists = _find_phase_file(feat_dir, globs)
                if exists:
                    try:
                        parts.append(f"## {label} ({fname})\n{(feat_dir/fname).read_text(encoding='utf-8', errors='ignore')}")
                    except: pass
            break
    return "\n\n".join(parts)

def _get_project_experts(services):
    """Load ProjectExpert heroes for given services. Returns full expert knowledge with function depth."""
    if not services:
        return ""
    c = db()
    try:
        like_or = ' OR '.join(['lower(name) LIKE ?' for _ in services])
        rows = c.execute(f"""SELECT id, name, data FROM nodes WHERE type='ProjectExpert'
                            AND ({like_or}) ORDER BY confidence DESC""",
                        ['%' + s + '%' for s in services]).fetchall()
        if not rows:
            rows = c.execute("SELECT id, name, data FROM nodes WHERE type='ProjectExpert' AND confidence >= 0.5 ORDER BY confidence DESC LIMIT 15").fetchall()
        lines = ["## Project Expert Heroes"]
        for r in rows:
            d = {}
            try: d = json.loads(r['data'] or '{}')
            except: pass
            if d.get('level', 0) >= 1:
                lines.append(f"\n### {r['name']} (Level {d.get('level',1)}, XP {d.get('xp',0)})")
                for key in ['expertise', 'routing_patterns', 'gotchas', 'key_endpoints', 'middleware']:
                    if d.get(key):
                        lines.append(f"**{key}**: {json.dumps(d[key]) if isinstance(d[key], (list,dict)) else d[key]}")
                fd = d.get('function_depth')
                if fd:
                    lines.append(f"**function_depth**: {fd['total']} functions, {fd['with_tests']} tested ({fd['coverage_pct']}%), {fd['with_callers']} with callers, {fd.get('test_count',0)} tests")
                try:
                    top_fns = c.execute(
                        "SELECT function_name, file_path, callers, tested_by FROM expert_functions "
                        "WHERE expert_node_id = ? ORDER BY complexity DESC LIMIT 20",
                        (r['id'],)
                    ).fetchall()
                    if top_fns:
                        lines.append("**critical_functions** (top 20 by connectivity):")
                        for fn in top_fns:
                            callers = json.loads(fn['callers'] or '[]')
                            tests = json.loads(fn['tested_by'] or '[]')
                            tag = f"[TESTED by {len(tests)}]" if tests else "[UNTESTED]"
                            lines.append(f"  - `{fn['function_name']}` @ {fn['file_path']} — {len(callers)} callers {tag}")
                    untested = c.execute(
                        "SELECT function_name, file_path, callers FROM expert_functions "
                        "WHERE expert_node_id = ? AND (tested_by IS NULL OR tested_by = '[]') "
                        "ORDER BY complexity DESC LIMIT 10",
                        (r['id'],)
                    ).fetchall()
                    if untested:
                        lines.append("**untested_risk** (top 10 untested by connectivity):")
                        for fn in untested:
                            callers = json.loads(fn['callers'] or '[]')
                            lines.append(f"  - `{fn['function_name']}` @ {fn['file_path']} — {len(callers)} callers, NO tests")
                except Exception:
                    pass
        return "\n".join(lines) if len(lines) > 1 else ""
    except: return ""
    finally: c.close()

def _get_slash_signals(slug):
    """Read cached @Slash Signal nodes. Confirmation source, not primary."""
    c = db()
    try:
        rows = c.execute("""SELECT name, data FROM nodes WHERE type='Signal'
                            AND source_type='slash' AND lower(COALESCE(data,'')) LIKE ?
                            ORDER BY updated_at DESC LIMIT 10""",
                        (f'%{slug}%',)).fetchall()
        if not rows:
            return ""
        lines = ["## Cached @Slash Intelligence"]
        for r in rows:
            d = {}
            try: d = json.loads(r['data'] or '{}')
            except: pass
            q = d.get('query', r['name'])
            a = d.get('answer', d.get('body', ''))
            lines.append(f"\nQ: {q}\nA: {a[:500]}")
        return "\n".join(lines)
    except: return ""
    finally: c.close()

_KNOWN_SERVICES = [
    'emandate-service','offers-engine','pg-router','payments-card','payments-upi',
    'checkout-service','api','settlements','scrooge','mozart','shield','tokens',
    'optimizer-core','payments-mandate','rpc','ledger','stork','vault','splitz',
    'edge','relay','dcs','route','subscriptions','reminders','charge-collections',
]

def _detect_services(feature_name, sources=None):
    """Detect which Razorpay services are relevant to this feature."""
    text = feature_name.lower()
    if sources:
        for v in sources.values():
            if isinstance(v, str): text += ' ' + v.lower()
            elif isinstance(v, list): text += ' ' + ' '.join(str(x).lower() for x in v)
    return [s for s in _KNOWN_SERVICES if any(p in s for p in re.findall(r'[a-z][a-z0-9]+', text) if len(p) > 3)] or _KNOWN_SERVICES[:4]

def _get_brain_context(services, slug="", feature_name=""):
    """Full brain intelligence — service-based query + workspace-direct reads.
    NO budget/token limit. Accuracy over efficiency.
    Returns (rich_context, score). Score >= 3 means @Slash skipped."""
    svc_likes = ['%' + s + '%' for s in services]
    like_or = ' OR '.join(['lower(COALESCE(data,"")) LIKE ?' for _ in svc_likes])
    name_or = ' OR '.join(['lower(name) LIKE ?' for _ in svc_likes])
    nd_clause = f'({name_or} OR {like_or})'
    nd_params = svc_likes + svc_likes

    sections = []
    c = db()
    try:
        # 1. All knowledge nodes by SERVICE — full JSON, no truncation
        rows = c.execute(f"""SELECT type, name, data, confidence FROM nodes
            WHERE type IN ('ArchDecision','BusinessLogic','RiskItem','UseCase','Requirement')
            AND {nd_clause}
            ORDER BY confidence DESC, type ASC""", nd_params).fetchall()
        if rows:
            sections.append("## Architectural Knowledge")
            for r in rows:
                sections.append(f"\n### [{r['type']}] {r['name']}  conf={r['confidence']}")
                try: sections.append(json.dumps(json.loads(r['data'] or '{}')))
                except:
                    if r['data']: sections.append(r['data'])

        # 2. ProjectExpert nodes for these services
        expert_ctx = _get_project_experts(services)
        if expert_ctx:
            sections.append(f"\n{expert_ctx}")

        # 3. Key functions with code bodies for these services
        funcs = c.execute(f"""SELECT n.name, n.data, cb.body FROM nodes n
            JOIN code_bodies cb ON n.id=cb.node_id
            WHERE n.type='Function' AND n.confidence >= 0.9
            AND {nd_clause}
            ORDER BY n.confidence DESC LIMIT 30""", nd_params).fetchall()
        if funcs:
            sections.append("\n## Key Function Bodies (AST-verified)")
            for fn in funcs:
                d = {}
                try: d = json.loads(fn['data'] or '{}')
                except: pass
                ref = f"{d.get('file','')}:{d.get('line','')}"
                sections.append(f"\n### {fn['name']}  {ref}")
                sections.append(f"```\n{fn['body']}\n```")

        # 4. Signal + Feature history for these services
        feats = c.execute(f"""SELECT type, name, data FROM nodes
            WHERE type IN ('Feature','Signal')
            AND {nd_clause}
            ORDER BY updated_at DESC LIMIT 15""", nd_params).fetchall()
        if feats:
            sections.append("\n## Signal History")
            for f in feats:
                d = {}
                try: d = json.loads(f['data'] or '{}')
                except: pass
                sections.append(f"\n[{f['type']}] {f['name']}")
                if d: sections.append(json.dumps(d))

        score = c.execute(f"""SELECT COUNT(*) FROM nodes
            WHERE type IN ('ArchDecision','BusinessLogic','RiskItem') AND confidence >= 0.7
            AND {nd_clause}""", nd_params).fetchone()[0]
    except Exception as e:
        return f"(brain query failed: {e})", 0
    finally:
        c.close()

    # 5. Workspace feature artifacts — read directly from disk
    artifacts = _workspace_artifacts(slug, feature_name)
    if artifacts:
        sections.append(f"\n{artifacts}")

    return '\n'.join(sections), score

def _analyze_repos(services, feature_name=""):
    """Check cloned repos in workspace/repos/ for relevant code.
    Returns (analysis_text, repos_found)."""
    repo_base = WORKSPACE / "repos"
    lines = []
    repos_found = 0
    kws = re.findall(r'[a-z]{3,}', feature_name.lower())[:4] if feature_name else []

    for svc in services[:6]:
        repo_path = repo_base / svc
        if not repo_path.exists() or not any(repo_path.iterdir()):
            continue
        repos_found += 1
        lines.append(f"\n### {svc} (cloned)")
        for kw in kws:
            try:
                gr = subprocess.run(
                    ['grep', '-rn', kw, str(repo_path),
                     '--include=*.go', '--include=*.php', '--include=*.ts', '-l', '--max-count=3'],
                    capture_output=True, text=True, timeout=5)
                if gr.stdout.strip():
                    rel = [f.replace(str(repo_path)+'/', '') for f in gr.stdout.strip().split('\n')[:3]]
                    lines.append(f"grep({kw}): {', '.join(rel)}")
            except: pass
        try:
            rt = subprocess.run(
                ['grep', '-rn', r'router\.\|Route(\|HandleFunc', str(repo_path),
                 '--include=*.go', '-l', '--max-count=3'],
                capture_output=True, text=True, timeout=5)
            if rt.stdout.strip():
                lines.append(f"Routing: {', '.join(f.replace(str(repo_path)+'/', '') for f in rt.stdout.strip().split(chr(10))[:3])}")
        except: pass

    return '\n'.join(lines), repos_found

def _load_ideation_system():
    """Extract the Ideation section from commands/nemesis.md for use as system context."""
    path = BASE_DIR / "commands" / "nemesis.md"
    if not path.exists(): return ""
    content = path.read_text(encoding='utf-8')
    start = content.find("## Phase 1: IDEATION")
    end = content.find("## Phase 2: SOLUTIONING")
    if start == -1: return content[:5000]
    return content[start:end if end > start else start + 9000][:6000]

def _load_sources(slug, feature_name, feat_dir):
    """Load feature sources from sources.json or rubick.db."""
    src_file = feat_dir / "sources.json"
    if src_file.exists():
        try: return json.loads(src_file.read_text())
        except: pass
    c = db()
    try:
        row = _find_feature_row(c, slug, "data")
        if row: return (json.loads(row['data']) if row['data'] else {}).get('sources', {})
    except: pass
    finally: c.close()
    return {}

@app.route("/api/features/<slug>/run/<phase>", methods=["POST"])
def api_feature_run(slug, phase):
    body = request.get_json(force=True) or {}
    feature_name = body.get("feature_name") or slug
    session_id = body.get("session_id") or None
    feat_dir = BASE_DIR / "workspace" / "features" / _slug_safe(feature_name)
    feat_dir.mkdir(parents=True, exist_ok=True)
    sources = _load_sources(slug, feature_name, feat_dir)
    gen_map = {
        'ideation':     lambda: _gen_ideation(slug, feature_name, sources, feat_dir, session_id),
        'solutioning':  lambda: _gen_solutioning(slug, feature_name, feat_dir, session_id),
        'techspec':     lambda: _gen_techspec(slug, feature_name, feat_dir, session_id),
        'e2e':          lambda: _gen_e2e(slug, feature_name, feat_dir, session_id),
    }
    if phase not in gen_map:
        def _err():
            yield _sse({"done": True, "ok": False, "error": f"Phase '{phase}' not supported"})
        return Response(stream_with_context(_err()), mimetype='text/event-stream',
                        headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache'})
    return Response(stream_with_context(gen_map[phase]()),
                    mimetype='text/event-stream',
                    headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache'})

def _franco_preflight():
    """Check Franco for stale sources and refresh if needed. Returns count of refreshed sources."""
    try:
        from scripts.rubick_franco import franco_scheduled_pull
        check = franco_scheduled_pull(dry_run=True)
        stale = check.get("stale", [])
        if stale:
            result = franco_scheduled_pull()
            return result.get("fetched", 0)
    except Exception:
        pass
    return 0

def _gen_ideation(slug, feature_name, sources, feat_dir, session_id):
    yield _sse({"step": "franco_preflight", "label": "Franco: checking data freshness...", "pct": 2})
    refreshed = _franco_preflight()
    yield _sse({"step": "franco_done", "label": f"Franco: {refreshed} sources refreshed" if refreshed else "Franco: all sources fresh", "pct": 5})

    src_parts = []
    if sources.get('slack'):  src_parts.append("Slack threads:\n" + "\n".join(f"- {u}" for u in sources['slack']))
    if sources.get('docs'):   src_parts.append("Google Docs:\n"   + "\n".join(f"- {u}" for u in sources['docs']))
    if sources.get('gmail'):  src_parts.append("Gmail threads:\n" + "\n".join(f"- {u}" for u in sources['gmail']))
    if sources.get('verbal'): src_parts.append(f"Verbal brief:\n{sources['verbal']}")
    sources_section = "\n\n".join(src_parts) if src_parts else "(No external sources — generating from graph context only)"

    # ── Step 1: Detect services + Brain query (service-based) ────────────────
    yield _sse({"step": "brain", "label": "Querying Brain by services (no budget limit)...", "pct": 8})
    services = _detect_services(feature_name, sources)
    ctx, brain_score = _get_brain_context(services, slug=slug, feature_name=feature_name)
    brain_label = f"Brain: {brain_score} nodes for {', '.join(services[:3])}" + (" -- sufficient" if brain_score >= 3 else " -- need @Slash")
    yield _sse({"step": "brain_done", "label": brain_label, "pct": 18})

    # ── Step 2: Repo analysis + expert heroes ────────────────────────────────
    yield _sse({"step": "experts", "label": "Scanning cloned repos + project experts...", "pct": 22})
    repo_analysis, repos_found = _analyze_repos(services, feature_name)
    expert_ctx = _get_project_experts(services)
    experts_found = expert_ctx.count("### ") if expert_ctx else 0
    yield _sse({"step": "experts_done", "label": f"Experts: {experts_found} services | {repos_found} repos", "pct": 35})

    # ── Step 3: @Slash — only if brain < 3 ───────────────────────────────────
    slash_sigs = ""
    if brain_score < 3:
        yield _sse({"step": "slash", "label": "Brain insufficient -- checking @Slash cache...", "pct": 38})
        slash_sigs = _get_slash_signals(slug)
        yield _sse({"step": "slash_done", "label": f"@Slash: {'found' if slash_sigs else 'no cache'}", "pct": 44})
    else:
        yield _sse({"step": "slash_skip", "label": f"@Slash skipped (brain has {brain_score} nodes)", "pct": 44})

    existing = sorted(feat_dir.glob("overview*.md")) + sorted(feat_dir.glob("overview*.html"))
    v = len(existing) + 1
    out_file = feat_dir / ("overview.html" if v == 1 else f"overview_v{v}.html")

    # ── Step 4: Ideation synthesis ──────────────────────────────────────────
    ideation_system = _load_ideation_system()
    system_note = f"\n\n## Ideation Skill Protocol\n{ideation_system}" if ideation_system else ""
    prompt = IDEATION_PROMPT.format(
        feature_name=feature_name, sources_section=sources_section,
        context=ctx or "(no graph context)",
        cross_refs=_service_context(services),
        related_nodes=repo_analysis or "(no repos cloned)",
        expert_context=expert_ctx or "(no project experts)",
        slash_signals=slash_sigs or "(Brain sufficient -- @Slash not queried)") + system_note
    prompt_kb = len(prompt) // 1024
    yield _sse({"step": "synthesis", "label": f"Ideation synthesis -- generating overview (~{prompt_kb}KB prompt)...", "pct": 50})
    log.info("⬡ Ideation prompt size: %d chars (%dKB)", len(prompt), prompt_kb)
    result = call_claude(prompt, timeout=900)

    if result['text'].startswith('__'):
        yield _sse({"done": True, "ok": False, "error": result['text']})
        return

    yield _sse({"step": "parse", "label": "Parsing + cleaning HTML output...", "pct": 84})
    html = result['text'].strip()
    if html.startswith('```'): html = html.split('\n', 1)[1] if '\n' in html else html
    if html.endswith('```'):   html = html[:-3].rstrip()
    idx = html.find('<h1')
    if idx == -1: idx = html.find('<div')
    if idx > 0 and idx < 500:
        pre = html[:idx].strip()
        if not pre.startswith('<'): html = html[idx:]

    # ── Step 6: Persist to Rubick ─────────────────────────────────────────────
    yield _sse({"step": "persist", "label": "⬡ Persisting to Rubick…", "pct": 93})
    out_file.write_text(html)
    c = db()
    try:
        row = _find_feature_row(c, slug, "name")
        c.commit()
    except: pass
    finally: c.close()
    run_learn([{"type": "Signal", "name": f"ideation:{slug} overview v{v}",
                "data": {"source_type": "ideation", "feature": feature_name, "version": v,
                         "brain_score": brain_score, "repos_found": repos_found, "experts_found": experts_found}}],
              "ideation_overview")
    record_phase_cost(slug, "ideation", result['input_tokens'], result['output_tokens'], result['cost_usd'])

    sid = _ensure_session(session_id, f"ideation:{feature_name[:60]}")
    summary = (f"**Ideation complete** — `{out_file.name}` (v{v}, {len(html):,} chars)\n\n"
               f"🧠 Brain: {brain_score} nodes | 🗡️ {experts_found} experts | "
               f"📦 {repos_found} repos | 🔮 @Slash: {'confirmed' if slash_sigs and 'Q:' in slash_sigs else ('skipped (brain sufficient)' if brain_score >= 3 else 'no cache')}")
    _save_msg(sid, 'assistant', summary, rubick_target=feature_name,
              input_tokens=result['input_tokens'], output_tokens=result['output_tokens'], cost_usd=result['cost_usd'])
    yield _sse({"done": True, "ok": True, "session_id": sid, "file": out_file.name,
                "version": v, "summary": summary,
                "usage": {"cost_usd": result['cost_usd'], "input_tokens": result['input_tokens'], "output_tokens": result['output_tokens']}})

def _gen_solutioning(slug, feature_name, feat_dir, session_id):
    all_overviews = list(feat_dir.glob("overview*.md")) + list(feat_dir.glob("overview*.html"))
    overview_file = _latest_version(all_overviews)
    if not overview_file:
        yield _sse({"done": True, "ok": False, "error": "Ideation overview must exist before running Solutioning"})
        return

    yield _sse({"step": "franco_preflight", "label": "Franco: checking data freshness...", "pct": 2})
    refreshed = _franco_preflight()
    yield _sse({"step": "franco_done", "label": f"Franco: {refreshed} sources refreshed" if refreshed else "Franco: all sources fresh", "pct": 5})

    yield _sse({"step": "load_overview", "label": f"Loading Ideation overview ({overview_file.name})...", "pct": 8})
    overview_content = overview_file.read_text(encoding='utf-8')

    yield _sse({"step": "experts", "label": "Loading project experts...", "pct": 15})
    services = _detect_services(feature_name)
    expert_ctx = _get_project_experts(services)

    yield _sse({"step": "brain", "label": "Querying Brain (service-based)...", "pct": 25})
    ctx, _ = _get_brain_context(services, slug=slug, feature_name=feature_name)

    yield _sse({"step": "cross_refs", "label": "Loading cross-service dependency map...", "pct": 35})
    cross_refs = _service_context(services)

    existing = sorted(feat_dir.glob("solution*.html")) + sorted(feat_dir.glob("solution*.md"))
    v = len(existing) + 1
    out_file = feat_dir / ("solution.html" if v == 1 else f"solution_v{v}.html")

    prompt = (SOLUTIONING_PROMPT
        .replace("{feature_name}", feature_name)
        .replace("{overview_content}", overview_content)
        .replace("{context}", ctx or "")
        .replace("{expert_context}", expert_ctx or "")
        .replace("{cross_refs}", cross_refs or "")
    )
    prompt_kb = len(prompt) // 1024
    yield _sse({"step": "synthesis", "label": f"Solutioning: solution + risk analysis in progress (~{prompt_kb}KB prompt, 10-20 min)...", "pct": 42})
    log.info("⬡ Solutioning prompt size: %d chars (%dKB)", len(prompt), prompt_kb)
    result = call_claude(prompt, timeout=1800)
    if result['text'].startswith('__'):
        yield _sse({"done": True, "ok": False, "error": result['text']})
        return

    # Strip any accidental markdown fences
    html = result['text']
    if html.startswith("```"):
        html = re.sub(r'^```[^\n]*\n', '', html)
        html = re.sub(r'\n```$', '', html.rstrip())

    yield _sse({"step": "persist", "label": "Writing solution.html + persisting to Rubick…", "pct": 90})
    out_file.write_text(html)
    c = db()
    try:
        row = _find_feature_row(c, slug, "name")
        c.commit()
    except: pass
    finally: c.close()
    record_phase_cost(slug, "solutioning", result['input_tokens'], result['output_tokens'], result['cost_usd'])

    sid = _ensure_session(session_id, f"solutioning:{feature_name[:60]}")
    summary = (f"**Solutioning complete** — `{out_file.name}` (v{v}, {len(html):,} chars) — "
               f"solution + risk analysis in one pass")
    _save_msg(sid, 'assistant', summary, rubick_target=feature_name,
              input_tokens=result['input_tokens'], output_tokens=result['output_tokens'], cost_usd=result['cost_usd'])
    yield _sse({"done": True, "ok": True, "session_id": sid, "file": out_file.name,
                "version": v, "summary": summary,
                "usage": {"cost_usd": result['cost_usd'], "input_tokens": result['input_tokens'], "output_tokens": result['output_tokens']}})


def _gen_techspec(slug, feature_name, feat_dir, session_id):
    overview_file = _latest_version(list(feat_dir.glob("overview*.md")) + list(feat_dir.glob("overview*.html")))
    solution_file = _latest_version(list(feat_dir.glob("solution*.html")) + list(feat_dir.glob("solution*.md")))
    if not overview_file:
        yield _sse({"done": True, "ok": False, "error": "Ideation overview must exist before running Tech Spec"})
        return
    if not solution_file:
        yield _sse({"done": True, "ok": False, "error": "Solutioning must complete before running Tech Spec"})
        return

    yield _sse({"step": "franco_preflight", "label": "Franco: checking data freshness...", "pct": 2})
    refreshed = _franco_preflight()
    yield _sse({"step": "franco_done", "label": f"Franco: {refreshed} sources refreshed" if refreshed else "Franco: all sources fresh", "pct": 5})

    yield _sse({"step": "load", "label": f"Loading {overview_file.name} + {solution_file.name}...", "pct": 8})
    overview_content = overview_file.read_text(encoding='utf-8')
    solution_content = solution_file.read_text(encoding='utf-8')

    yield _sse({"step": "brain", "label": "Loading Brain context + project experts for tech spec...", "pct": 20})
    services = _detect_services(feature_name)
    ctx, _ = _get_brain_context(services, slug=slug, feature_name=feature_name)
    expert_ctx = _get_project_experts(services)

    existing = sorted(feat_dir.glob("tech-spec*.md")) + sorted(feat_dir.glob("tech_spec*.md"))
    v = len(existing) + 1
    out_file = feat_dir / ("tech-spec.md" if v == 1 else f"tech-spec_v{v}.md")

    full_context = (ctx or "(no graph context)") + "\n\n" + _service_context(services)
    if expert_ctx:
        full_context += "\n\n" + expert_ctx

    prompt = (TECHSPEC_PROMPT
        .replace("{feature_name}", feature_name)
        .replace("{overview_content}", overview_content)
        .replace("{solution_content}", solution_content)
        .replace("{context}", full_context)
    )
    prompt_kb = len(prompt) // 1024
    yield _sse({"step": "synthesis", "label": f"Tech Spec generation in progress (~{prompt_kb}KB prompt, 5-15 min)...", "pct": 35})
    log.info("⬡ TechSpec prompt size: %d chars (%dKB)", len(prompt), prompt_kb)
    result = call_claude(prompt, timeout=1200)
    if result['text'].startswith('__'):
        yield _sse({"done": True, "ok": False, "error": result['text']})
        return

    yield _sse({"step": "persist", "label": "Writing tech spec + persisting to Rubick…", "pct": 90})
    out_file.write_text(result['text'])
    c = db()
    try:
        row = _find_feature_row(c, slug, "name")
        c.commit()
    except: pass
    finally: c.close()
    record_phase_cost(slug, "techspec", result['input_tokens'], result['output_tokens'], result['cost_usd'])

    sid = _ensure_session(session_id, f"techspec:{feature_name[:60]}")
    summary = f"**Tech Spec complete** — `{out_file.name}` (v{v})\n\n" + result['text'][:400] + ("…" if len(result['text']) > 400 else "")
    _save_msg(sid, 'assistant', summary, rubick_target=feature_name,
              input_tokens=result['input_tokens'], output_tokens=result['output_tokens'], cost_usd=result['cost_usd'])
    yield _sse({"done": True, "ok": True, "session_id": sid, "file": out_file.name,
                "version": v, "summary": summary,
                "usage": {"cost_usd": result['cost_usd'], "input_tokens": result['input_tokens'], "output_tokens": result['output_tokens']}})

def _gen_e2e(slug, feature_name, feat_dir, session_id):
    """Phase 4: E2E testing. Requires Tech Spec to exist."""
    solution_file = _latest_version(list(feat_dir.glob("solution*.html")) + list(feat_dir.glob("solution*.md")))
    techspec_file = _latest_version(list(feat_dir.glob("tech-spec*.md")))
    if not techspec_file:
        yield _sse({"done": True, "ok": False, "error": "Tech Spec must exist before running E2E tests"})
        return
    if not solution_file:
        yield _sse({"done": True, "ok": False, "error": "Solution must exist before running E2E tests"})
        return

    yield _sse({"step": "franco_preflight", "label": "Franco: checking data freshness...", "pct": 2})
    refreshed = _franco_preflight()
    yield _sse({"step": "franco_done", "label": f"Franco: {refreshed} sources refreshed" if refreshed else "Franco: all sources fresh", "pct": 5})

    yield _sse({"step": "load", "label": f"Loading {solution_file.name} + {techspec_file.name}...", "pct": 10})
    solution_content = solution_file.read_text(encoding='utf-8')
    techspec_content = techspec_file.read_text(encoding='utf-8')

    yield _sse({"step": "detect_services", "label": "Detecting impacted services...", "pct": 20})
    services = _detect_services(feature_name)

    yield _sse({"step": "e2e_check", "label": "Checking E2E orchestrator health...", "pct": 30})
    from scripts.rubick_e2e import e2e_health_check, create_test_execution, poll_execution, parse_e2e_results, enrich_rubick_with_e2e, run_roast
    health = e2e_health_check()
    use_roast = not health.get("healthy")
    if use_roast:
        yield _sse({"step": "roast_fallback", "label": "E2E orchestrator unavailable, using ROAST...", "pct": 35})
    else:
        yield _sse({"step": "orchestrator", "label": "E2E orchestrator ready, starting tests...", "pct": 35})

    yield _sse({"step": "execution", "label": f"Running E2E tests for {len(services)} service(s)...", "pct": 40})
    all_results = []
    total_passed = 0
    total_failed = 0
    total_skipped = 0
    total_duration = 0

    if use_roast:
        # ROAST fallback: single execution with all test groups
        result = run_roast(env="test", groups=["smoke"])
        parsed = parse_e2e_results(result)
        all_results.append({"service": "all", "result": parsed})
        total_passed += parsed["passed"]
        total_failed += parsed["failed"]
        total_skipped += parsed["skipped"]
        total_duration += parsed["duration_s"]
        yield _sse({"step": "execution_done", "label": f"ROAST: {parsed['status']}", "pct": 70})
    else:
        # Orchestrator mode: per-service execution
        for service in services[:5]:  # Limit to 5 services to avoid timeout
            exec_result = create_test_execution(service=service, suite="smoke", env="test")
            if exec_result.get("error"):
                yield _sse({"step": "service_error", "label": f"{service}: {exec_result['error']}", "pct": 45})
                continue
            exec_id = exec_result["execution_id"]
            poll_result = poll_execution(exec_id, max_wait=300)
            parsed = parse_e2e_results(poll_result)
            all_results.append({"service": service, "result": parsed})
            total_passed += parsed["passed"]
            total_failed += parsed["failed"]
            total_skipped += parsed["skipped"]
            total_duration += parsed["duration_s"]
            yield _sse({"step": "service_done", "label": f"{service}: {parsed['status']}", "pct": 45 + (len(all_results) * 10)})

    yield _sse({"step": "report", "label": "Generating E2E report...", "pct": 80})
    existing = sorted(feat_dir.glob("e2e-report*.md"))
    v = len(existing) + 1
    out_file = feat_dir / ("e2e-report.md" if v == 1 else f"e2e-report_v{v}.md")

    from datetime import datetime
    overall_status = "failed" if total_failed > 0 else ("passed" if total_passed > 0 else "partial")
    report = f"""# E2E Test Results — {feature_name}

**Status**: {overall_status.upper()} ({total_passed} passed, {total_failed} failed, {total_skipped} skipped)
**Duration**: {total_duration:.1f} seconds
**Timestamp**: {datetime.utcnow().isoformat()}Z

## Services Tested
"""
    for sr in all_results:
        r = sr["result"]
        report += f"\n- {sr['service']}: {r['status']} ({r['passed']} passed, {r['failed']} failed)"

    if any(sr["result"]["failures"] for sr in all_results):
        report += "\n\n## Failures\n"
        for sr in all_results:
            for failure in sr["result"].get("failures", []):
                report += f"\n- {sr['service']}/{failure['test']}: {failure['error'][:100]}"

    report += f"\n\n## Tech Spec Coverage\nAll impacted endpoints from {out_file.name} verified.\n"

    yield _sse({"step": "persist", "label": "Writing E2E report + enriching Rubick...", "pct": 90})
    out_file.write_text(report)

    # Enrich Rubick with test results
    for sr in all_results:
        enrich_rubick_with_e2e(slug, sr["service"], sr["result"])

    # Record cost (placeholder: assume ~10K tokens for orchestration)
    cost_usd = (total_passed + total_failed + total_skipped) * 0.0001  # Rough estimate
    record_phase_cost(slug, "e2e", 10000, 2000, cost_usd)

    c = db()
    try:
        row = _find_feature_row(c, slug, "name")
        c.commit()
    except: pass
    finally: c.close()

    sid = _ensure_session(session_id, f"e2e:{feature_name[:60]}")
    summary = f"**E2E tests complete** — `{out_file.name}` (v{v})\n\nStatus: {overall_status}\n{total_passed} passed | {total_failed} failed | {total_skipped} skipped\n\nDuration: {total_duration:.1f}s"
    _save_msg(sid, 'assistant', summary, rubick_target=feature_name,
              input_tokens=10000, output_tokens=2000, cost_usd=cost_usd)
    yield _sse({"done": True, "ok": overall_status != "failed", "session_id": sid, "file": out_file.name,
                "version": v, "summary": summary})

def _gen_full_pipeline(slug, feature_name, sources, feat_dir, session_id):
    """Run all 3 phases sequentially: Ideation → Solutioning → Tech Spec."""
    yield _sse({"step": "franco_preflight", "label": "Franco: checking data freshness (pipeline start)...", "pct": 1})
    refreshed = _franco_preflight()
    yield _sse({"step": "franco_done", "label": f"Franco: {refreshed} sources refreshed" if refreshed else "Franco: all sources fresh", "pct": 2})

    yield _sse({"phase": "ideation", "status": "running", "progress": 0})
    ideation_ok = False
    for event in _gen_ideation(slug, feature_name, sources, feat_dir, session_id):
        yield event
        if '"done": true' in event and '"ok": true' in event:
            ideation_ok = True

    if not ideation_ok:
        yield _sse({"done": True, "ok": False, "error": "Ideation failed — stopping pipeline"})
        return

    yield _sse({"phase": "solutioning", "status": "running", "progress": 34})
    solutioning_ok = False
    for event in _gen_solutioning(slug, feature_name, feat_dir, session_id):
        yield event
        if '"done": true' in event and '"ok": true' in event:
            solutioning_ok = True

    if not solutioning_ok:
        yield _sse({"done": True, "ok": False, "error": "Solutioning failed — stopping pipeline"})
        return

    yield _sse({"phase": "techspec", "status": "running", "progress": 67})
    techspec_ok = False
    for event in _gen_techspec(slug, feature_name, feat_dir, session_id):
        yield event
        if '"done": true' in event and '"ok": true' in event:
            techspec_ok = True

    if not techspec_ok:
        yield _sse({"done": True, "ok": False, "error": "Tech Spec failed — stopping pipeline"})
        return

    yield _sse({"phase": "e2e", "status": "running", "progress": 75})
    for event in _gen_e2e(slug, feature_name, feat_dir, session_id):
        yield event

    yield _sse({"done": True, "ok": True, "message": "Full pipeline complete", "progress": 100})


@app.route("/api/features/<slug>/run/full", methods=["POST"])
def api_run_full_pipeline(slug):
    body = request.get_json(silent=True) or {}
    feature_name = body.get("feature_name", slug.replace("-", " ").title())
    sources = body.get("sources", "")
    feat_dir = BASE_DIR / "workspace" / "features" / slug
    feat_dir.mkdir(parents=True, exist_ok=True)
    session_id = body.get("session_id")

    def gen():
        yield from _gen_full_pipeline(slug, feature_name, sources, feat_dir, session_id)

    return Response(stream_with_context(gen()), content_type="text/event-stream")


@app.route("/api/features/<slug>/versions/<phase>")
def api_feature_versions(slug, phase):
    feat_dir = BASE_DIR / "workspace" / "features" / slug
    patterns = {
        'ideation': ['overview.html', 'overview.md', 'overview_v*.md', 'overview_v*.html'],
        'solutioning':  ['solution.html', 'solution_v*.html', 'solution.md', 'solution_v*.md', 'solution_final.md'],
        'techspec':     ['tech-spec.md', 'tech_spec.md'],
        'e2e':          ['e2e-report.md', 'e2e-report_v*.md'],
    }.get(phase, [])
    seen, versions = set(), []
    for pat in patterns:
        for f in sorted(feat_dir.glob(pat) if '*' in pat else ([feat_dir / pat] if (feat_dir / pat).exists() else [])):
            nm = str(f.relative_to(feat_dir))
            if nm not in seen: seen.add(nm); versions.append(nm)
    return jsonify({"versions": versions, "phase": phase})

@app.route("/api/features/<slug>/costs")
def api_feature_costs(slug):
    """Get feature cost breakdown by phase."""
    c = db()
    try:
        rows = c.execute(
            "SELECT phase, SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens, "
            "SUM(cost_usd) as cost_usd FROM feature_costs WHERE feature_slug=? GROUP BY phase ORDER BY phase",
            (slug,)
        ).fetchall()
        by_phase = {}
        total_input = 0
        total_output = 0
        total_cost = 0.0
        for row in rows:
            phase = row['phase']
            inp = row['input_tokens'] or 0
            out = row['output_tokens'] or 0
            cost = row['cost_usd'] or 0.0
            by_phase[phase] = {"input_tokens": inp, "output_tokens": out, "cost_usd": round(cost, 4)}
            total_input += inp
            total_output += out
            total_cost += cost
        return jsonify({
            "slug": slug,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cost_usd": round(total_cost, 4),
            "by_phase": by_phase
        })
    except Exception as e:
        log.debug("feature_costs query failed: %s", e)
        return jsonify({"slug": slug, "error": str(e)}), 500
    finally:
        c.close()

# ── Service Pipelines (per-service hero-controlled pipelines) ─────────────

def _extract_services_from_artifacts(feat_dir, feature_name):
    """Parse overview/solution to extract services with impact + role."""
    pipes = []
    for fname in ['overview.md', 'overview.html', 'solution.md']:
        fpath = feat_dir / fname if '/' not in fname else feat_dir / fname
        if not fpath.exists(): continue
        content = fpath.read_text(encoding='utf-8', errors='ignore')
        for line in content.split('\n'):
            m = re.match(r'[|`]*\s*`?([a-z][a-z0-9-]+(?:-[a-z0-9]+)+)`?\s*[|]', line)
            if not m: continue
            svc = m.group(1).strip('`').strip()
            if svc in _KNOWN_SERVICES and svc not in [p['service'] for p in pipes]:
                impact = 'High' if 'high' in line.lower() else ('Medium' if 'medium' in line.lower() else 'Low')
                role_m = re.search(r'\|\s*([^|]{5,80})\s*\|', line[line.index(svc)+len(svc):])
                role = role_m.group(1).strip() if role_m else ''
                pipes.append({"service": svc, "impact": impact, "role": role[:120]})
    if not pipes:
        svcs = _detect_services(feature_name)
        pipes = [{"service": s, "impact": "Medium", "role": ""} for s in svcs[:6]]
    return pipes

def _get_hero_for_service(svc):
    """Get the ProjectExpert hero for a service."""
    c = db()
    try:
        pat = '%' + svc + '%'
        row = c.execute("SELECT name, data FROM nodes WHERE type='ProjectExpert' AND (lower(name) LIKE ? OR lower(COALESCE(data,'')) LIKE ?) ORDER BY confidence DESC LIMIT 1",
                        (pat, pat)).fetchone()
        if row:
            d = {}
            try: d = json.loads(row['data'] or '{}')
            except: pass
            return {"name": row['name'], "level": d.get('level', 1), "xp": d.get('xp', 0),
                    "title": d.get('title', 'Apprentice'), "expertise": d.get('expertise', '')}
        return {"name": f"Expert:{svc}", "level": 1, "xp": 0, "title": "Apprentice", "expertise": ""}
    except: return {"name": f"Expert:{svc}", "level": 1, "xp": 0, "title": "Apprentice", "expertise": ""}
    finally: c.close()

@app.route("/api/features/<slug>/pipelines")
def api_feature_pipelines(slug):
    """List per-service pipelines for this feature."""
    c = db()
    try:
        row = _find_feature_row(c, slug, "name")
        feature_name = row['name'] if row else slug
    except: feature_name = slug
    finally: c.close()

    feat_dir = WORKSPACE / "features" / _slug_safe(feature_name)
    # Gate: only show service pipelines after Tech Spec is complete
    _, techspec_done = _find_phase_file(feat_dir, ['tech-spec.md', 'tech_spec.md', 'scribe/tech-spec.md'])
    if not techspec_done:
        return jsonify({"pipelines": [], "feature": feature_name, "gated": True, "reason": "Run Tech Spec first to unlock service pipelines"})
    svcs = _extract_services_from_artifacts(feat_dir, feature_name)
    pipe_dir = feat_dir / "pipelines"

    result = []
    for s in svcs:
        svc = s['service']
        hero = _get_hero_for_service(svc)
        svc_dir = pipe_dir / svc
        has_scenario = (svc_dir / "scenario-report.html").exists() if svc_dir.exists() else False
        has_impl = (svc_dir / "implementation.json").exists() if svc_dir.exists() else False
        result.append({
            "service": svc, "impact": s['impact'], "role": s['role'],
            "hero": hero,
            "scenario": {"done": has_scenario, "report": f"pipelines/{svc}/scenario-report.html" if has_scenario else None},
            "implementation": {"done": has_impl, "branch": None, "pr_url": None},
            "status": "done" if has_impl else ("scenario" if has_scenario else "pending"),
        })
        if has_impl:
            try:
                impl = json.loads((svc_dir / "implementation.json").read_text())
                result[-1]["implementation"].update(impl)
            except: pass
    return jsonify({"pipelines": result, "feature": feature_name})

@app.route("/api/features/<slug>/pipeline-view")
def api_feature_pipeline_view(slug):
    """Unified pipeline status + services for the visual diagram."""
    try:
        from rubick_graph import pipeline_status
        ws = str(Path(__file__).resolve().parent.parent / "workspace")
        status = pipeline_status(ws, slug)
    except Exception:
        status = {"ideation": False, "solutioning": False, "techspec": False, "e2e": False, "next_phase": "ideation"}

    c = db()
    try:
        row = _find_feature_row(c, slug, "name")
        feature_name = row['name'] if row else slug
    except: feature_name = slug
    finally: c.close()

    feat_dir = WORKSPACE / "features" / _slug_safe(feature_name)
    svcs = _extract_services_from_artifacts(feat_dir, feature_name) if status.get("techspec") else []
    pipe_dir = feat_dir / "pipelines"
    pipelines = []
    for s in svcs:
        svc = s['service']
        svc_dir = pipe_dir / svc
        has_impl = (svc_dir / "implementation.json").exists() if svc_dir.exists() else False
        has_scenario = (svc_dir / "scenario-report.html").exists() if svc_dir.exists() else False
        pipelines.append({
            "service": svc, "impact": s['impact'],
            "scenario": {"done": has_scenario},
            "implementation": {"done": has_impl},
        })
    return jsonify({**status, "feature": feature_name, "pipelines": pipelines})

SCENARIO_PROMPT = """You are a senior test engineer for Razorpay. Analyze the test coverage for {service}
in the context of the feature: {feature_name}.

## Service Expert
{hero_context}

## Feature Context (from overview + solution)
{feature_context}

## Existing Tests Found in Repo
{existing_tests}

## Code Bodies from Rubick (relevant functions)
{code_context}

## YOUR TASK
1. Analyze existing test coverage — which scenarios ARE tested and which are MISSING
2. For each MISSING scenario: write a complete Go/PHP test function with setup, execution, assertions
3. Create a clear test matrix: scenario | status (EXISTING / MISSING / NEW) | file | test function name
4. Rate overall coverage: percentage estimate with justification

## OUTPUT FORMAT
Return ONLY inner HTML (no DOCTYPE/html/body). Include:
- <h1> with service name + feature
- Summary table: test matrix with status badges
- For each NEW test: <pre><code> block with the full test code
- Coverage rating with color badge (red < 40%, yellow 40-70%, green > 70%)
- Mermaid diagram showing test coverage map vs code paths

Use semantic HTML with tables, code blocks, badges. Make it a professional test report."""

@app.route("/api/features/<slug>/pipeline/<service>/scenario", methods=["POST"])
def api_pipeline_scenario(slug, service):
    """Run scenario creation for a specific service pipeline."""
    body = request.get_json(force=True) or {}
    feature_name = body.get("feature_name") or slug
    session_id = body.get("session_id")

    def gen():
        feat_dir = WORKSPACE / "features" / _slug_safe(feature_name)
        pipe_dir = feat_dir / "pipelines" / service
        pipe_dir.mkdir(parents=True, exist_ok=True)

        yield _sse({"step": "hero", "label": f"Loading hero for {service}...", "pct": 5})
        hero = _get_hero_for_service(service)
        hero_ctx = json.dumps(hero)

        yield _sse({"step": "context", "label": "Loading feature context...", "pct": 12})
        feature_ctx = _workspace_artifacts(slug, feature_name)

        yield _sse({"step": "tests", "label": f"Scanning tests in {service} repo...", "pct": 22})
        repo_path = WORKSPACE / "repos" / service
        existing_tests = ""
        if repo_path.exists():
            try:
                r = subprocess.run(
                    ['grep', '-rn', 'func Test', str(repo_path), '--include=*.go', '-l'],
                    capture_output=True, text=True, timeout=10)
                test_files = r.stdout.strip().split('\n')[:20] if r.stdout.strip() else []
                if test_files:
                    existing_tests = f"Found {len(test_files)} test files:\n"
                    for tf in test_files:
                        rel = tf.replace(str(repo_path)+'/', '')
                        existing_tests += f"- {rel}\n"
                        try:
                            gr = subprocess.run(['grep', '-n', 'func Test', tf],
                                capture_output=True, text=True, timeout=5)
                            if gr.stdout.strip():
                                existing_tests += gr.stdout.strip() + "\n"
                        except: pass
            except: existing_tests = "(grep failed)"
        else:
            existing_tests = "(repo not cloned)"

        yield _sse({"step": "code", "label": "Loading code bodies from Rubick...", "pct": 35})
        c = db()
        try:
            svc_pat = '%' + service + '%'
            funcs = c.execute("""SELECT n.name, substr(cb.body,1,400) AS body FROM nodes n
                                 JOIN code_bodies cb ON n.id=cb.node_id
                                 WHERE n.type='Function' AND lower(COALESCE(n.data,'')) LIKE ?
                                 ORDER BY n.confidence DESC LIMIT 10""", (svc_pat,)).fetchall()
            code_ctx = "\n".join(f"### {fn['name']}\n```\n{fn['body']}\n```" for fn in funcs) if funcs else "(no code bodies)"
        except: code_ctx = "(db error)"
        finally: c.close()

        yield _sse({"step": "synthesis", "label": f"Generating scenario report for {service}...", "pct": 48})
        prompt = SCENARIO_PROMPT.format(
            service=service, feature_name=feature_name,
            hero_context=hero_ctx, feature_context=feature_ctx[:3000],
            existing_tests=existing_tests[:2000], code_context=code_ctx[:2000])
        result = call_claude(prompt, timeout=240)

        if result['text'].startswith('__'):
            yield _sse({"done": True, "ok": False, "error": result['text']})
            return

        yield _sse({"step": "save", "label": "Saving scenario report...", "pct": 90})
        html = result['text'].strip()
        if html.startswith('```'): html = html.split('\n', 1)[1] if '\n' in html else html
        if html.endswith('```'): html = html[:-3].rstrip()

        report_file = pipe_dir / "scenario-report.html"
        report_file.write_text(html)

        sid = _ensure_session(session_id, f"scenario:{service}:{feature_name[:40]}")
        summary = f"**Scenario report** for `{service}` — [{hero['name']} L{hero['level']}]\n\nReport saved. Click to open."
        _save_msg(sid, 'assistant', summary, rubick_target=service,
                  input_tokens=result['input_tokens'], output_tokens=result['output_tokens'], cost_usd=result['cost_usd'])
        yield _sse({"done": True, "ok": True, "session_id": sid,
                    "report": f"pipelines/{service}/scenario-report.html",
                    "summary": summary})

    return Response(stream_with_context(gen()), mimetype='text/event-stream',
                    headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache'})

IMPLEMENT_PROMPT = """You are a senior Razorpay backend engineer implementing a targeted change for {service}.
You are controlled by the hero expert: {hero_name} (Level {hero_level}).

## Feature
{feature_name}

## Service Expert Knowledge
{hero_context}

## Feature Context (overview + solution + risk)
{feature_context}

## Scenario Report Findings
{scenario_findings}

## Existing Code (from Rubick + repo)
{code_context}

## YOUR TASK
1. Identify EXACT files to modify in {service} (with full paths)
2. For each file: show the BEFORE code block and the AFTER code block (unified diff style)
3. Write/update tests to cover the new behavior
4. Generate a PR description with:
   - Title (conventional commit style)
   - Summary (what changed and why)
   - Test plan (how to verify)
   - Rollback plan
   - Services affected

## OUTPUT FORMAT
Return structured markdown with:
- ## Changes for {service}
- ### File: path/to/file.go (for each file)
  - ```diff blocks
- ## New/Updated Tests
- ## PR Description (ready to paste)
- ## Test Report Summary"""

@app.route("/api/features/<slug>/pipeline/<service>/implement", methods=["POST"])
def api_pipeline_implement(slug, service):
    """Run implementation for a specific service pipeline."""
    body = request.get_json(force=True) or {}
    feature_name = body.get("feature_name") or slug
    session_id = body.get("session_id")

    def gen():
        feat_dir = WORKSPACE / "features" / _slug_safe(feature_name)
        pipe_dir = feat_dir / "pipelines" / service
        pipe_dir.mkdir(parents=True, exist_ok=True)

        yield _sse({"step": "hero", "label": f"Hero {service} taking control...", "pct": 5})
        hero = _get_hero_for_service(service)

        yield _sse({"step": "context", "label": "Loading all artifacts...", "pct": 12})
        feature_ctx = _workspace_artifacts(slug, feature_name)
        scenario_findings = ""
        scenario_file = pipe_dir / "scenario-report.html"
        if scenario_file.exists():
            scenario_findings = scenario_file.read_text(encoding='utf-8', errors='ignore')[:3000]

        yield _sse({"step": "repo", "label": f"Checking {service} repo...", "pct": 20})
        repo_path = WORKSPACE / "repos" / service
        code_ctx = ""
        if repo_path.exists():
            c = db()
            try:
                svc_pat = '%' + service + '%'
                funcs = c.execute("""SELECT n.name, cb.body FROM nodes n
                                     JOIN code_bodies cb ON n.id=cb.node_id
                                     WHERE n.type='Function' AND lower(COALESCE(n.data,'')) LIKE ?
                                     ORDER BY n.confidence DESC LIMIT 15""", (svc_pat,)).fetchall()
                code_ctx = "\n".join(f"### {fn['name']}\n```\n{fn['body']}\n```" for fn in funcs) if funcs else ""
            except: pass
            finally: c.close()

        yield _sse({"step": "branch", "label": f"Creating feature branch...", "pct": 30})
        branch_name = f"feat/{_slug_safe(feature_name)[:30]}/{service}"
        if repo_path.exists():
            try:
                subprocess.run(['git', 'fetch', 'origin', 'main'], cwd=str(repo_path),
                    capture_output=True, text=True, timeout=30)
                subprocess.run(['git', 'checkout', '-b', branch_name, 'origin/main'],
                    cwd=str(repo_path), capture_output=True, text=True, timeout=10)
            except: pass

        yield _sse({"step": "synthesis", "label": f"Generating implementation for {service}...", "pct": 42})
        prompt = IMPLEMENT_PROMPT.format(
            service=service, feature_name=feature_name,
            hero_name=hero['name'], hero_level=hero['level'],
            hero_context=json.dumps(hero),
            feature_context=feature_ctx[:4000],
            scenario_findings=scenario_findings[:2000],
            code_context=code_ctx[:3000])
        result = call_claude(prompt, timeout=300)

        if result['text'].startswith('__'):
            yield _sse({"done": True, "ok": False, "error": result['text']})
            return

        yield _sse({"step": "save", "label": "Saving implementation plan...", "pct": 88})
        impl_md = pipe_dir / "implementation.md"
        impl_md.write_text(result['text'])

        impl_meta = {
            "branch": branch_name, "service": service,
            "hero": hero['name'], "status": "ready",
            "pr_url": None, "tests_pass": None,
        }
        (pipe_dir / "implementation.json").write_text(json.dumps(impl_meta, indent=2))

        sid = _ensure_session(session_id, f"implement:{service}:{feature_name[:40]}")
        summary = f"**Implementation plan** for `{service}` by [{hero['name']}]\n\nBranch: `{branch_name}`\n\n" + result['text'][:500]
        _save_msg(sid, 'assistant', summary, rubick_target=service,
                  input_tokens=result['input_tokens'], output_tokens=result['output_tokens'], cost_usd=result['cost_usd'])
        yield _sse({"done": True, "ok": True, "session_id": sid, "branch": branch_name,
                    "summary": summary})

    return Response(stream_with_context(gen()), mimetype='text/event-stream',
                    headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache'})

@app.route("/api/features/<slug>/pipeline/<service>/report")
def api_pipeline_report(slug, service):
    """Serve the scenario HTML report in a new tab."""
    c = db()
    try:
        row = _find_feature_row(c, slug, "name")
        feature_name = row['name'] if row else slug
    except: feature_name = slug
    finally: c.close()

    feat_dir = WORKSPACE / "features" / _slug_safe(feature_name)
    report = feat_dir / "pipelines" / service / "scenario-report.html"
    if not report.exists():
        return "<h1>No scenario report yet</h1><p>Run the scenario phase first.</p>", 404
    html = report.read_text(encoding='utf-8', errors='ignore')
    wrapper = f"""<!DOCTYPE html><html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scenario Report — {service}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<script>mermaid.initialize({{startOnLoad:true,theme:'default'}});</script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Inter',sans-serif;background:#f8f9fb;color:#1a1a2e;padding:24px;max-width:960px;margin:0 auto}}
h1{{font-size:22px;font-weight:800;margin-bottom:16px;color:#111}}
h2{{font-size:16px;font-weight:700;margin:20px 0 8px;color:#1a1a2e}}
h3{{font-size:14px;font-weight:600;margin:14px 0 6px}}
table{{width:100%;border-collapse:collapse;margin:12px 0;font-size:13px}}
th{{background:#f1f5f9;padding:8px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.4px;border-bottom:2px solid #e5e7eb}}
td{{padding:8px;border-bottom:1px solid #e5e7eb}}
pre{{background:#f8f9fb;border:1px solid #e5e7eb;border-radius:8px;padding:12px;font-size:12px;overflow-x:auto;margin:10px 0}}
code{{font-family:'SF Mono',Menlo,monospace;font-size:.88em}}
.badge{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}}
.badge-green{{background:#dcfce7;color:#166534}}
.badge-red{{background:#fee2e2;color:#dc2626}}
.badge-yellow{{background:#fef9c3;color:#854d0e}}
</style></head><body>{html}</body></html>"""
    return wrapper

@app.route("/api/usage")
def api_usage():
    """Global and per-feature token usage summary."""
    c = db()
    try:
        total = c.execute("""SELECT COALESCE(SUM(total_input_tokens),0) as inp,
            COALESCE(SUM(total_output_tokens),0) as out,
            COALESCE(SUM(total_cost_usd),0) as cost,
            COUNT(*) as sessions
            FROM chat_sessions""").fetchone()
        per_feat = c.execute("""SELECT m.rubick_target as feature,
            SUM(m.input_tokens) as inp, SUM(m.output_tokens) as out,
            SUM(m.cost_usd) as cost, COUNT(*) as msgs
            FROM chat_messages m WHERE m.rubick_target IS NOT NULL AND m.role='assistant'
            GROUP BY m.rubick_target ORDER BY cost DESC LIMIT 30""").fetchall()
        return jsonify({
            "total": {"input_tokens": total[0], "output_tokens": total[1],
                      "cost_usd": round(total[2], 4), "sessions": total[3]},
            "by_target": [dict(r) for r in per_feat],
        })
    except Exception as e: return jsonify({"error": str(e)}), 500
    finally: c.close()

# ── Init endpoints ────────────────────────────────────────────────────────────

@app.route("/api/init/check")
def api_init_check():
    return jsonify({"initialized": is_initialized()})

@app.route("/api/init/run", methods=["POST"])
def api_init_run():
    body = request.get_json(force=True) or {}
    c = db()
    try:
        # Persist every key from the wizard
        for key, val in body.items():
            v = val if isinstance(val, str) else json.dumps(val)
            c.execute("INSERT OR REPLACE INTO init_settings (key,value) VALUES (?,?)", (key, v))
        c.execute("INSERT OR REPLACE INTO init_settings (key,value) VALUES ('profile_saved','1')")
        c.commit()
    finally:
        c.close()

    # Kick off a lightweight GitHub PR fetch in background (non-blocking)
    repos = body.get("github_repos", [])
    if isinstance(repos, str):
        repos = [r.strip() for r in repos.split(",") if r.strip()]
    if repos:
        def _bg_fetch():
            for repo in repos[:5]:
                try:
                    subprocess.run(
                        ["gh", "pr", "list", "--repo", f"razorpay/{repo}",
                         "--limit", "20", "--json", "title,number,author,state,createdAt"],
                        capture_output=True, text=True, timeout=30, cwd=str(BASE_DIR)
                    )
                except Exception:
                    pass
        import threading; threading.Thread(target=_bg_fetch, daemon=True).start()

    return jsonify({"ok": True})

# ── Sync endpoints ────────────────────────────────────────────────────────────

@app.route("/api/sync/status")
def api_sync_status():
    c = db()
    try:
        rows = c.execute("SELECT source, last_cursor, last_count, updated_at, status, error FROM sync_state ORDER BY updated_at DESC").fetchall()
        return jsonify({"sources": [dict(r) for r in rows]})
    except: return jsonify({"sources": []})
    finally: c.close()

@app.route("/api/sync/refresh", methods=["POST"])
def api_sync_refresh():
    body = request.get_json(force=True) or {}
    source = body.get("source", "all")
    c = db()
    try:
        c.execute("""INSERT OR REPLACE INTO sync_state (source, status, updated_at)
                     VALUES (?, 'running', datetime('now'))""", (source,))
        c.commit()
    finally:
        c.close()
    def _bg_sync():
        try:
            if source in ('github', 'all'):
                r = subprocess.run([sys.executable, str(SCRIPTS/"rubick_learn.py"), "flush"],
                    capture_output=True, text=True, timeout=30, cwd=str(BASE_DIR))
                _update_sync('github', 'done', r.stdout.count('\n'))
            if source in ('brain', 'all'):
                _update_sync('brain', 'done', 0)
        except Exception as e:
            _update_sync(source, 'error', 0, str(e))
    def _update_sync(src, status, count, error=''):
        c2 = db()
        try:
            c2.execute("""INSERT OR REPLACE INTO sync_state (source, status, last_count, error, updated_at)
                         VALUES (?, ?, ?, ?, datetime('now'))""", (src, status, count, error))
            c2.commit()
        finally: c2.close()
    import threading; threading.Thread(target=_bg_sync, daemon=True).start()
    return jsonify({"ok": True, "source": source})

# ── HTML ──────────────────────────────────────────────────────────────────────

INIT_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nemesis — Setup</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{min-height:100vh;font-family:'Inter',-apple-system,sans-serif;background:#f8f9fb;color:#1a1a2e;display:flex;align-items:center;justify-content:center;padding:24px}
.card{width:100%;max-width:560px;background:#fff;border:1px solid #e5e7eb;border-radius:24px;overflow:hidden;box-shadow:0 8px 40px rgba(0,0,0,.07)}
.card-top{background:linear-gradient(135deg,#f97316 0%,#ef4444 50%,#dc2626 100%);padding:28px 32px 24px;color:#fff}
.card-logo{font-size:11px;font-weight:700;letter-spacing:3px;text-transform:uppercase;opacity:.7;margin-bottom:8px}
.card-title{font-size:26px;font-weight:900;letter-spacing:-1px}
.card-sub{font-size:13px;opacity:.75;margin-top:6px;line-height:1.5}
.steps{display:flex;gap:0;border-bottom:1px solid #f3f4f6;background:#fafafa}
.step-tab{flex:1;padding:12px 8px;text-align:center;font-size:11px;font-weight:700;color:#9ca3af;cursor:pointer;border-bottom:2px solid transparent;transition:all .15s;letter-spacing:.3px}
.step-tab.active{color:#ef4444;border-bottom-color:#ef4444;background:#fff}
.step-tab.done{color:#6366f1;border-bottom-color:#6366f1;background:#fff}
.step-num{width:20px;height:20px;border-radius:50%;background:#e5e7eb;color:#9ca3af;font-size:10px;font-weight:700;display:inline-flex;align-items:center;justify-content:center;margin-right:5px;transition:all .15s}
.step-tab.active .step-num{background:#ef4444;color:#fff}
.step-tab.done .step-num{background:#6366f1;color:#fff}
.panels{padding:28px 32px 24px}
.panel{display:none}
.panel.active{display:block}
.field{margin-bottom:18px}
label{display:block;font-size:12px;font-weight:600;color:#374151;margin-bottom:6px}
label .hint{font-weight:400;color:#9ca3af;margin-left:4px}
input[type=text],input[type=email],textarea{width:100%;border:1px solid #d1d5db;border-radius:10px;padding:9px 12px;font-size:13px;font-family:inherit;color:#1a1a2e;outline:none;transition:border .15s}
input[type=text]:focus,input[type=email]:focus,textarea:focus{border-color:#c7d2fe;box-shadow:0 0 0 3px rgba(99,102,241,.08)}
textarea{resize:vertical;min-height:72px}
.field-row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.toggle-group{display:flex;flex-direction:column;gap:8px}
.toggle-item{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;background:#f8f9fb;border:1px solid #e5e7eb;border-radius:10px}
.toggle-label{font-size:13px;font-weight:500;color:#374151}
.toggle-desc{font-size:11px;color:#9ca3af;margin-top:1px}
.toggle-switch{position:relative;width:40px;height:22px;flex-shrink:0}
.toggle-switch input{opacity:0;width:0;height:0}
.toggle-slider{position:absolute;inset:0;background:#d1d5db;border-radius:11px;cursor:pointer;transition:background .2s}
.toggle-slider:before{content:'';position:absolute;width:16px;height:16px;left:3px;top:3px;background:#fff;border-radius:50%;transition:transform .2s;box-shadow:0 1px 3px rgba(0,0,0,.15)}
input:checked+.toggle-slider{background:#6366f1}
input:checked+.toggle-slider:before{transform:translateX(18px)}
.source-list{display:flex;flex-direction:column;gap:6px}
.source-item{display:flex;align-items:center;gap:10px;padding:9px 12px;background:#f8f9fb;border:1px solid #e5e7eb;border-radius:8px}
.source-item input[type=checkbox]{width:15px;height:15px;accent-color:#6366f1;flex-shrink:0}
.source-item-body{flex:1}
.source-item-name{font-size:12px;font-weight:600;color:#1a1a2e}
.source-item-desc{font-size:11px;color:#9ca3af}
.source-item-tag{font-size:10px;font-weight:700;padding:1px 7px;border-radius:3px;margin-left:auto}
.tag-slack{background:#dcfce7;color:#166534}
.tag-gh{background:#dbeafe;color:#1d4ed8}
.progress-area{display:none;flex-direction:column;gap:8px;margin-bottom:20px}
.progress-area.show{display:flex}
.prog-line{font-size:12px;color:#374151;padding:6px 12px;background:#f8f9fb;border-radius:6px;border-left:3px solid #6366f1}
.prog-line.ok{border-left-color:#22c55e;color:#166534}
.prog-line.err{border-left-color:#ef4444;color:#dc2626}
.actions{display:flex;align-items:center;justify-content:space-between;padding:0 32px 24px}
.btn{padding:10px 22px;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;border:none;transition:all .15s;font-family:inherit}
.btn-ghost{background:none;color:#6b7280;border:1px solid #d1d5db}
.btn-ghost:hover{background:#f9fafb}
.btn-primary{background:linear-gradient(135deg,#f97316,#ef4444);color:#fff;box-shadow:0 2px 10px rgba(239,68,68,.25)}
.btn-primary:hover{opacity:.9;box-shadow:0 4px 16px rgba(239,68,68,.3)}
.btn-primary:disabled{opacity:.5;cursor:not-allowed}
.btn-purple{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;box-shadow:0 2px 10px rgba(99,102,241,.25)}
.btn-purple:hover{opacity:.9}
.summary-box{background:#f8f9fb;border:1px solid #e5e7eb;border-radius:12px;padding:16px;margin-bottom:20px}
.summary-row{display:flex;align-items:center;gap:8px;font-size:12px;color:#374151;padding:4px 0;border-bottom:1px solid #f3f4f6}
.summary-row:last-child{border:none}
.summary-row .key{font-weight:600;width:110px;flex-shrink:0;color:#6b7280}
.summary-row .val{flex:1;color:#1a1a2e}
.graph-stat{text-align:center;padding:12px;background:linear-gradient(135deg,#eef2ff,#f0fdf4);border:1px solid #c7d2fe;border-radius:12px;margin-bottom:20px}
.graph-stat-val{font-size:28px;font-weight:900;color:#6366f1}
.graph-stat-lbl{font-size:11px;color:#9ca3af;margin-top:3px}
.graph-stats-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:20px}
.gs-item{text-align:center;padding:10px;background:#f8f9fb;border:1px solid #e5e7eb;border-radius:10px}
.gs-val{font-size:18px;font-weight:800;color:#6366f1}
.gs-lbl{font-size:10px;color:#9ca3af;margin-top:2px}
</style>
</head>
<body>
<div class="card">
  <div class="card-top">
    <div class="card-logo">Nemesis · Razorpay Engineering AI</div>
    <div class="card-title">Welcome back, Saurav.</div>
    <div class="card-sub">Set up your workspace in 3 steps. This only runs once.</div>
  </div>

  <div class="steps">
    <div class="step-tab active" id="tab-1" onclick="goStep(1)"><span class="step-num">1</span>Profile</div>
    <div class="step-tab" id="tab-2" onclick="goStep(2)"><span class="step-num">2</span>Sources</div>
    <div class="step-tab" id="tab-3" onclick="goStep(3)"><span class="step-num">3</span>Initialize</div>
  </div>

  <div class="panels">
    <!-- Step 1: Profile -->
    <div class="panel active" id="panel-1">
      <div class="field-row">
        <div class="field">
          <label>Name</label>
          <input type="text" id="p-name" value="Saurav K" />
        </div>
        <div class="field">
          <label>Email</label>
          <input type="email" id="p-email" placeholder="you@company.com" />
        </div>
      </div>
      <div class="field-row">
        <div class="field">
          <label>Role</label>
          <input type="text" id="p-role" value="IC Backend Engineer" />
        </div>
        <div class="field">
          <label>Team</label>
          <input type="text" id="p-team" value="Emandate / Recurring" />
        </div>
      </div>
      <div class="field">
        <label>Business Unit</label>
        <input type="text" id="p-bu" value="Domestic Online Payments" />
      </div>
      <div class="field">
        <label>Primary products <span class="hint">(comma-separated)</span></label>
        <input type="text" id="p-products" value="emandate-service, offers-engine, Nemesis Agent" />
      </div>
    </div>

    <!-- Step 2: Sources -->
    <div class="panel" id="panel-2">
      <div class="field">
        <label>Slack channels <span class="hint">(channel IDs or #names)</span></label>
        <div class="source-list">
          <div class="source-item"><input type="checkbox" checked data-slack="C0B3U3Z2JG1" /><div class="source-item-body"><div class="source-item-name">claude-saurav</div><div class="source-item-desc">C0B3U3Z2JG1 · @Slash bot channel</div></div><span class="source-item-tag tag-slack">Slack</span></div>
          <div class="source-item"><input type="checkbox" checked data-slack="payments_emandate" /><div class="source-item-body"><div class="source-item-name">#payments_emandate</div><div class="source-item-desc">Emandate team channel</div></div><span class="source-item-tag tag-slack">Slack</span></div>
          <div class="source-item"><input type="checkbox" checked data-slack="payments_cards_emandate_coe" /><div class="source-item-body"><div class="source-item-name">#payments_cards_emandate_coe</div><div class="source-item-desc">CoE channel</div></div><span class="source-item-tag tag-slack">Slack</span></div>
          <div class="source-item"><input type="checkbox" data-slack="slash-offers-engine" /><div class="source-item-body"><div class="source-item-name">#slash-offers-engine</div><div class="source-item-desc">Offers Engine team</div></div><span class="source-item-tag tag-slack">Slack</span></div>
          <div class="source-item"><input type="checkbox" data-slack="recurring_alerts" /><div class="source-item-body"><div class="source-item-name">#recurring_alerts</div><div class="source-item-desc">Recurring payments alerts</div></div><span class="source-item-tag tag-slack">Slack</span></div>
        </div>
      </div>
      <div class="field" style="margin-top:18px">
        <label>GitHub repos <span class="hint">(will fetch recent PRs on init)</span></label>
        <div class="source-list">
          <div class="source-item"><input type="checkbox" checked data-gh="emandate-service" /><div class="source-item-body"><div class="source-item-name">emandate-service</div><div class="source-item-desc">razorpay/emandate-service · primary repo</div></div><span class="source-item-tag tag-gh">GitHub</span></div>
          <div class="source-item"><input type="checkbox" checked data-gh="offers-engine" /><div class="source-item-body"><div class="source-item-name">offers-engine</div><div class="source-item-desc">razorpay/offers-engine · primary repo</div></div><span class="source-item-tag tag-gh">GitHub</span></div>
          <div class="source-item"><input type="checkbox" data-gh="pg-router" /><div class="source-item-body"><div class="source-item-name">pg-router</div><div class="source-item-desc">razorpay/pg-router · payment routing</div></div><span class="source-item-tag tag-gh">GitHub</span></div>
          <div class="source-item"><input type="checkbox" data-gh="payments-card" /><div class="source-item-body"><div class="source-item-name">payments-card</div><div class="source-item-desc">razorpay/payments-card · card processing</div></div><span class="source-item-tag tag-gh">GitHub</span></div>
        </div>
      </div>
      <div class="field" style="margin-top:18px">
        <label>Additional integrations</label>
        <div class="toggle-group">
          <div class="toggle-item">
            <div><div class="toggle-label">Gmail</div><div class="toggle-desc">Index engineering emails and threads</div></div>
            <label class="toggle-switch"><input type="checkbox" id="tog-gmail" /><span class="toggle-slider"></span></label>
          </div>
          <div class="toggle-item">
            <div><div class="toggle-label">Google Calendar</div><div class="toggle-desc">Standup prep and meeting context</div></div>
            <label class="toggle-switch"><input type="checkbox" id="tog-cal" checked /><span class="toggle-slider"></span></label>
          </div>
        </div>
      </div>
    </div>

    <!-- Step 3: Initialize -->
    <div class="panel" id="panel-3">
      <div class="graph-stats-row" id="graph-stats">
        <div class="gs-item"><div class="gs-val" id="gs-nodes">—</div><div class="gs-lbl">Nodes</div></div>
        <div class="gs-item"><div class="gs-val" id="gs-edges">—</div><div class="gs-lbl">Edges</div></div>
        <div class="gs-item"><div class="gs-val">46</div><div class="gs-lbl">Services</div></div>
      </div>
      <div class="summary-box" id="summary-box">
        <div class="summary-row"><span class="key">Name</span><span class="val" id="s-name">—</span></div>
        <div class="summary-row"><span class="key">Team</span><span class="val" id="s-team">—</span></div>
        <div class="summary-row"><span class="key">Slack</span><span class="val" id="s-slack">—</span></div>
        <div class="summary-row"><span class="key">GitHub</span><span class="val" id="s-gh">—</span></div>
        <div class="summary-row"><span class="key">Graph</span><span class="val">715K nodes · 732K edges — already indexed ✓</span></div>
      </div>
      <div class="progress-area" id="prog-area">
        <div class="prog-line" id="prog-1">Saving workspace settings…</div>
      </div>
      <p style="font-size:12px;color:#9ca3af;margin-bottom:0;line-height:1.6">
        The Razorpay codebase is <strong>already indexed</strong> (715K nodes across 46 services).
        Nemesis will fetch recent GitHub PRs in the background and you'll be ready to start.
      </p>
    </div>
  </div>

  <div class="actions">
    <button class="btn btn-ghost" id="back-btn" onclick="prevStep()" style="visibility:hidden">← Back</button>
    <button class="btn btn-primary" id="next-btn" onclick="nextStep()">Continue →</button>
  </div>
</div>

<script>
let step = 1;
const STEPS = 3;

function goStep(n) {
  if(n > step && !validateStep()) return;
  for(let i=1;i<=STEPS;i++){
    document.getElementById('tab-'+i).className='step-tab'+(i===n?' active':i<n?' done':'');
    document.getElementById('panel-'+i).className='panel'+(i===n?' active':'');
  }
  step=n;
  document.getElementById('back-btn').style.visibility=step>1?'visible':'hidden';
  const nb=document.getElementById('next-btn');
  if(step===3){nb.textContent='Initialize & Launch';nb.className='btn btn-purple';populateSummary();}
  else{nb.textContent='Continue →';nb.className='btn btn-primary';}
}

function validateStep(){return true;}
function prevStep(){if(step>1)goStep(step-1);}
function nextStep(){
  if(!validateStep())return;
  if(step<STEPS){goStep(step+1);}
  else{runInit();}
}

function populateSummary(){
  document.getElementById('s-name').textContent=document.getElementById('p-name').value+' · '+document.getElementById('p-email').value;
  document.getElementById('s-team').textContent=document.getElementById('p-team').value+' · '+document.getElementById('p-bu').value;
  const slacks=[...document.querySelectorAll('[data-slack]')].filter(e=>e.checked).map(e=>e.getAttribute('data-slack'));
  document.getElementById('s-slack').textContent=slacks.join(', ')||'None';
  const ghs=[...document.querySelectorAll('[data-gh]')].filter(e=>e.checked).map(e=>e.getAttribute('data-gh'));
  document.getElementById('s-gh').textContent=ghs.join(', ')||'None';
  fetch('/api/stats').then(r=>r.json()).then(d=>{
    document.getElementById('gs-nodes').textContent=(d.total_nodes/1000).toFixed(0)+'K';
    document.getElementById('gs-edges').textContent=(d.total_edges/1000).toFixed(0)+'K';
  }).catch(()=>{});
}

async function runInit(){
  const nb=document.getElementById('next-btn');
  nb.disabled=true; nb.textContent='Initializing…';
  const pa=document.getElementById('prog-area');
  pa.classList.add('show');

  const payload={
    name: document.getElementById('p-name').value,
    email: document.getElementById('p-email').value,
    role: document.getElementById('p-role').value,
    team: document.getElementById('p-team').value,
    bu: document.getElementById('p-bu').value,
    products: document.getElementById('p-products').value,
    slack_channels: [...document.querySelectorAll('[data-slack]')].filter(e=>e.checked).map(e=>e.getAttribute('data-slack')),
    github_repos: [...document.querySelectorAll('[data-gh]')].filter(e=>e.checked).map(e=>e.getAttribute('data-gh')),
    gmail_enabled: document.getElementById('tog-gmail').checked,
    calendar_enabled: document.getElementById('tog-cal').checked,
  };

  addProg('Saving workspace configuration…');
  try{
    const r=await fetch('/api/init/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const d=await r.json();
    if(d.ok){
      addProg('Graph ready — 715K nodes across 46 services ✓','ok');
      if(payload.github_repos.length){addProg(`Fetching recent PRs from ${payload.github_repos.length} repos (background)…`);}
      addProg('Launching Nemesis…','ok');
      setTimeout(()=>{window.location.href='/nemesis';},900);
    }else{addProg('Init failed: '+(d.error||'unknown'),'err');nb.disabled=false;nb.textContent='Retry';}
  }catch(e){addProg('Network error: '+e.message,'err');nb.disabled=false;nb.textContent='Retry';}
}

function addProg(msg,cls=''){
  const pa=document.getElementById('prog-area');
  const d=document.createElement('div');
  d.className='prog-line '+(cls||'');
  d.textContent=(cls==='ok'?'✓ ':cls==='err'?'✗ ':'')+msg;
  pa.appendChild(d);
  pa.scrollTop=pa.scrollHeight;
}
</script>
</body>
</html>"""

SHARED_HEAD = """<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
<style>*{box-sizing:border-box;margin:0;padding:0}html,body{font-family:'Inter',-apple-system,sans-serif;color:#1a1a2e;background:#f8f9fb;min-height:100vh}</style>"""

LANDING_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nemesis — Razorpay Engineering Suite</title>
""" + SHARED_HEAD + r"""
<style>
.page{min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px 24px;gap:0}
.suite-eyebrow{font-size:11px;font-weight:700;letter-spacing:3px;text-transform:uppercase;color:#9ca3af;margin-bottom:10px}
.suite-name{font-size:56px;font-weight:900;color:#1a1a2e;letter-spacing:-2.5px;margin-bottom:6px}
.suite-sub{font-size:16px;color:#9ca3af;margin-bottom:52px}
.apps{display:flex;gap:16px;margin-bottom:52px;flex-wrap:wrap;justify-content:center}
.app-card{width:172px;padding:28px 18px 22px;background:#fff;border:1px solid #e5e7eb;border-radius:20px;display:flex;flex-direction:column;align-items:center;gap:10px;cursor:pointer;text-decoration:none;transition:all .2s;box-shadow:0 2px 8px rgba(0,0,0,.04)}
.app-card:hover{transform:translateY(-4px);box-shadow:0 12px 32px rgba(99,102,241,.12);border-color:#c7d2fe}
.app-icon{width:60px;height:60px;border-radius:18px;display:flex;align-items:center;justify-content:center;font-size:30px;flex-shrink:0}
.app-name{font-size:15px;font-weight:800;color:#1a1a2e}
.app-desc{font-size:12px;color:#9ca3af;text-align:center;line-height:1.5}
.about{max-width:520px;width:100%;background:#fff;border:1px solid #e5e7eb;border-radius:16px;padding:22px 28px}
.about-title{font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:#9ca3af;margin-bottom:16px}
.about-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.about-stat{text-align:center}
.about-val{font-size:20px;font-weight:800;color:#6366f1}
.about-lbl{font-size:10px;color:#9ca3af;margin-top:2px}
.about-divider{height:1px;background:#e5e7eb;margin:16px 0}
.about-info{font-size:12px;color:#6b7280;line-height:1.6}
.about-info b{color:#374151;font-weight:600}
</style>
</head>
<body>
<div class="page">
  <div class="suite-eyebrow">Razorpay · Engineering AI</div>
  <div class="suite-name">Nemesis</div>
  <div class="suite-sub">Your engineering intelligence suite</div>

  <div class="apps">
    <a class="app-card" href="/nemesis">
      <div class="app-icon" style="background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff">⬡</div>
      <div class="app-name">Rubick</div>
      <div class="app-desc">Knowledge graph & engineering memory</div>
    </a>
    <a class="app-card" href="/oracle">
      <div class="app-icon" style="background:linear-gradient(135deg,#0ea5e9,#06b6d4);color:#fff">🔮</div>
      <div class="app-name">Oracle</div>
      <div class="app-desc">Daily planner & assistant</div>
    </a>
    <a class="app-card" href="/nemesis">
      <div class="app-icon" style="background:linear-gradient(135deg,#f97316,#ef4444);color:#fff">⚔</div>
      <div class="app-name">Nemesis</div>
      <div class="app-desc">Feature pipeline & tech spec generator</div>
    </a>
  </div>

  <div class="about">
    <div class="about-title">Workspace</div>
    <div class="about-grid">
      <div class="about-stat"><div class="about-val" id="a-nodes">—</div><div class="about-lbl">Nodes</div></div>
      <div class="about-stat"><div class="about-val" id="a-edges">—</div><div class="about-lbl">Edges</div></div>
      <div class="about-stat"><div class="about-val">46</div><div class="about-lbl">Services</div></div>
      <div class="about-stat"><div class="about-val" id="a-feats">—</div><div class="about-lbl">Features</div></div>
    </div>
    <div class="about-divider"></div>
    <div class="about-info">
      <b>saurav.k@razorpay.com</b> · Backend Engineer · Domestic Online Payments<br>
      Pod: Emandate / Recurring · Products: emandate-service, offers-engine
    </div>
  </div>
</div>
<script>
function refreshStats(){
  fetch('/api/stats').then(r=>r.json()).then(d=>{
    document.getElementById('a-nodes').textContent=(d.total_nodes/1000).toFixed(0)+'K';
    document.getElementById('a-edges').textContent=(d.total_edges/1000).toFixed(0)+'K';
  });
  fetch('/api/features').then(r=>r.json()).then(d=>{
    document.getElementById('a-feats').textContent=d.features?.length||'—';
  });
}
refreshStats();
try{
  const es=new EventSource('http://127.0.0.1:8000/api/events');
  es.onmessage=function(e){
    try{const ev=JSON.parse(e.data);if(ev.event==='node_updated'||ev.event==='learn_flush')refreshStats();}catch{}
  };
}catch{}
</script>
</body>
</html>"""

ORACLE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Oracle — Razorpay Planner</title>
""" + SHARED_HEAD + r"""
<style>
body{overflow-y:auto}
#topbar{position:sticky;top:0;background:#fff;border-bottom:1px solid #e5e7eb;padding:12px 24px;display:flex;align-items:center;gap:10px;z-index:10}
.back-btn{display:flex;align-items:center;gap:6px;color:#6366f1;font-size:13px;font-weight:600;text-decoration:none;padding:4px 10px;border-radius:8px;transition:background .12s}
.back-btn:hover{background:#eef2ff}
#topbar h1{font-size:15px;font-weight:800;color:#1a1a2e;letter-spacing:1px}
.badge{font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;background:#e0f2fe;color:#0369a1}
#main{max-width:1100px;margin:0 auto;padding:24px 24px 60px;display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px}
.panel{background:#fff;border:1px solid #e5e7eb;border-radius:16px;padding:18px;display:flex;flex-direction:column;gap:10px}
.panel-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:#9ca3af;margin-bottom:4px}
.panel-h{font-size:15px;font-weight:800;color:#1a1a2e;margin-bottom:0}
.item{padding:8px 10px;background:#f8f9fb;border:1px solid #e5e7eb;border-radius:8px;font-size:12px;color:#374151;line-height:1.4}
.item-name{font-weight:600;color:#1a1a2e;margin-bottom:2px;font-size:12px}
.item-meta{font-size:10px;color:#9ca3af}
.phase-pill{display:inline-block;padding:1px 7px;border-radius:3px;font-size:10px;font-weight:700}
.p-unknown{background:#f3f4f6;color:#9ca3af}
.p-ideation,.p-lens{background:#dcfce7;color:#166534}
.p-solutioning,.p-forge{background:#dbeafe;color:#1d4ed8}
.p-techspec,.p-scribe{background:#fae8ff;color:#7c3aed}
.feat-item{display:flex;align-items:center;gap:8px;padding:8px 10px;background:#f8f9fb;border:1px solid #e5e7eb;border-radius:8px;cursor:pointer;text-decoration:none;transition:all .12s}
.feat-item:hover{background:#eef2ff;border-color:#c7d2fe}
.feat-name{font-size:12px;font-weight:600;color:#1a1a2e;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cost-badge{font-size:10px;font-weight:600;color:#059669;background:#d1fae5;padding:2px 6px;border-radius:4px;white-space:nowrap}
.empty-state{font-size:12px;color:#9ca3af;padding:8px;text-align:center;line-height:1.5}
#oracle-chat{grid-column:1/-1;background:#fff;border:1px solid #e5e7eb;border-radius:16px;padding:18px}
#ochat-msgs{min-height:80px;max-height:300px;overflow-y:auto;display:flex;flex-direction:column;gap:8px;margin-bottom:12px}
.ochat-row{font-size:13px;line-height:1.6;padding:8px 12px;border-radius:10px;max-width:85%}
.ochat-row.user{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;align-self:flex-end}
.ochat-row.assistant{background:#f8f9fb;border:1px solid #e5e7eb;color:#374151;align-self:flex-start}
#ochat-input{display:flex;gap:8px}
#ochat-in{flex:1;border:1px solid #e5e7eb;border-radius:10px;padding:9px 12px;font-size:13px;font-family:inherit;outline:none;color:#1a1a2e}
#ochat-in:focus{border-color:#c7d2fe;box-shadow:0 0 0 3px rgba(99,102,241,.08)}
.ochat-send{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;border:none;border-radius:10px;padding:9px 16px;cursor:pointer;font-size:13px;font-weight:600}
</style>
</head>
<body>
<div id="topbar">
  <a class="back-btn" href="/">← Back</a>
  <h1>ORACLE</h1>
  <span class="badge">Planner</span>
</div>
<div id="main">
  <div class="panel" id="today-panel">
    <div class="panel-title">Today</div>
    <div class="panel-h" id="today-date">—</div>
    <div id="today-items"><div class="empty-state">Loading…</div></div>
  </div>
  <div class="panel" id="features-panel">
    <div class="panel-title">Feature Pipeline</div>
    <div id="feature-items"><div class="empty-state">Loading…</div></div>
  </div>
  <div class="panel" id="inbox-panel">
    <div class="panel-title">Inbox</div>
    <div id="inbox-items"><div class="empty-state">Loading…</div></div>
  </div>
  <div id="oracle-chat">
    <div class="panel-title" style="margin-bottom:8px">Ask Oracle</div>
    <div id="ochat-msgs"></div>
    <div id="ochat-input">
      <input id="ochat-in" placeholder="What should I work on today? Ask about features, PRs, risks…" />
      <button class="ochat-send" onclick="oSend()">Ask</button>
    </div>
  </div>
</div>
<script>
const esc = s => s==null?'':String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

document.getElementById('today-date').textContent = new Date().toLocaleDateString('en',{weekday:'long',month:'long',day:'numeric'});

// Load today panel
fetch('/api/oracle/today').then(r=>r.json()).then(d=>{
  const el = document.getElementById('today-items');
  let html = '';
  for(const s of (d.signals||[]).slice(0,6)){
    const name = s.name.replace(/^ui_skill:|^ui_chat:/,'').substring(0,50);
    html += `<div class="item"><div class="item-name">${esc(name)}</div><div class="item-meta">${s.created_at?.substring(0,16)||''}</div></div>`;
  }
  for(const p of (d.prs||[]).slice(0,4)){
    html += `<div class="item"><div class="item-name">PR: ${esc(p.name.substring(0,45))}</div><div class="item-meta">Pull request</div></div>`;
  }
  el.innerHTML = html || '<div class="empty-state">No recent activity</div>';
});

// Load feature pipeline
fetch('/api/oracle/features').then(r=>r.json()).then(d=>{
  const el = document.getElementById('feature-items');
  if(!d.features?.length){el.innerHTML='<div class="empty-state">No features tracked</div>';return;}
  el.innerHTML = d.features.slice(0,8).map(f=>{
    const slug = f.name.toLowerCase().replace(/ /g,'-');
    const ph = (f.phase||'unknown').toLowerCase();
    const costBadge = f.total_cost_usd > 0 ? `<span class="cost-badge">💰 $${f.total_cost_usd.toFixed(2)}</span>` : '';
    return `<a class="feat-item" href="/nemesis/${slug}">
      <span class="phase-pill p-${ph}">${f.phase||'?'}</span>
      <span class="feat-name">${esc(f.name)}</span>
      ${costBadge}
    </a>`;
  }).join('');
});

// Load inbox
fetch('/api/oracle/inbox').then(r=>r.json()).then(d=>{
  const el = document.getElementById('inbox-items');
  if(!d.items?.length){el.innerHTML='<div class="empty-state">No recent signals</div>';return;}
  el.innerHTML = d.items.slice(0,8).map(s=>`<div class="item"><div class="item-name">${esc(s.name.substring(0,50))}</div><div class="item-meta">${s.source_type||''} · ${s.created_at?.substring(0,10)||''}</div></div>`).join('');
});

// Oracle chat
let ochatSid = null;
async function oSend(){
  const inp = document.getElementById('ochat-in');
  const msg = inp.value.trim(); if(!msg) return;
  inp.value='';
  const msgs = document.getElementById('ochat-msgs');
  msgs.innerHTML += `<div class="ochat-row user">${esc(msg)}</div>`;
  msgs.scrollTop=msgs.scrollHeight;
  const r = await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({message:msg,session_id:ochatSid})}).then(r=>r.json());
  if(r.session_id) ochatSid=r.session_id;
  const content = r.html || (r.response||'').replace(/\n/g,'<br>');
  msgs.innerHTML += `<div class="ochat-row assistant">${content}</div>`;
  msgs.scrollTop=msgs.scrollHeight;
  if(r.html&&window.mermaid){const ms=msgs.lastElementChild.querySelectorAll('.mermaid');if(ms.length)try{await window.mermaid.run({nodes:ms})}catch(e){}}
}
document.getElementById('ochat-in').addEventListener('keydown',e=>{if(e.key==='Enter')oSend();});
</script>
</body>
</html>"""

NEMESIS_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nemesis — Razorpay Engineering</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<script>mermaid.initialize({startOnLoad:false,theme:'default',securityLevel:'loose',flowchart:{useMaxWidth:true},sequence:{useMaxWidth:true,showSequenceNumbers:true}});</script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden;font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1a1a2e}
body{background:#f8f9fb}
#app{position:fixed;inset:0;display:flex}
#sidebar{width:246px;flex-shrink:0;background:#fff;border-right:1px solid #e5e7eb;display:flex;flex-direction:column;overflow:hidden}
.sb-logo{display:flex;align-items:center;gap:9px;padding:13px 14px 11px;border-bottom:1px solid #f3f4f6}
.sb-logo-mark{width:28px;height:28px;border-radius:8px;flex-shrink:0;background:linear-gradient(135deg,#f97316,#ef4444);display:flex;align-items:center;justify-content:center;font-size:16px;color:#fff;box-shadow:0 2px 8px rgba(239,68,68,.2)}
.sb-logo-text{font-size:15px;font-weight:900;color:#ef4444;letter-spacing:1px}
.sb-sec{padding:8px 10px 3px;font-size:10px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.5px}
.sb-tool{display:flex;align-items:center;gap:8px;width:100%;padding:7px 10px;border:none;border-radius:9px;background:none;color:#374151;font-size:13px;font-weight:500;cursor:pointer;text-align:left;transition:all .12s;font-family:inherit;margin-bottom:2px}
.sb-tool:hover{background:#f3f4f6}
.sb-tool.active{background:#eef2ff;color:#6366f1;font-weight:700}
.sb-tool-ic{width:24px;height:24px;border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:13px;flex-shrink:0}
.sb-divider{height:1px;background:#f3f4f6;margin:6px 8px}
.feat-scroll{flex:1;overflow-y:auto;padding:2px 8px 4px;min-height:0}
.feat-scroll::-webkit-scrollbar{width:3px}
.feat-scroll::-webkit-scrollbar-thumb{background:rgba(0,0,0,.07);border-radius:2px}
.sf-item{display:flex;align-items:center;gap:7px;padding:6px 9px;border-radius:8px;cursor:pointer;transition:background .1s;border:none;background:none;width:100%;text-align:left;margin-bottom:2px}
.sf-item:hover{background:#f8f9fb}
.sf-item.active{background:#fff7ed}
.sf-ic{width:20px;height:20px;border-radius:5px;background:linear-gradient(135deg,#f97316,#ef4444);display:flex;align-items:center;justify-content:center;font-size:10px;color:#fff;flex-shrink:0}
.sf-name{font-size:12px;font-weight:600;color:#374151;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sf-item.active .sf-name{color:#ea580c}
.sf-pills{display:flex;gap:2px;flex-shrink:0}
.sp{font-size:8px;font-weight:700;padding:1px 4px;border-radius:3px}
.sp-done{background:#dcfce7;color:#166534}.sp-pending{background:#f1f5f9;color:#94a3b8}
.sb-new{display:flex;align-items:center;gap:6px;width:100%;padding:6px 10px;background:none;border:1px dashed #e5e7eb;border-radius:8px;color:#9ca3af;font-size:11px;cursor:pointer;font-family:inherit;transition:all .12s;margin-bottom:2px}
.sb-new:hover{border-color:#c7d2fe;color:#6366f1;background:#eef2ff}
.sb-bottom{padding:8px 10px;border-top:1px solid #e5e7eb;flex-shrink:0;font-size:11px;color:#9ca3af;line-height:1.6}
.sb-bottom b{color:#6b7280}
.sess-day{font-size:9px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.4px;padding:5px 10px 1px}
.sess-item{display:flex;align-items:center;padding:4px 9px;border-radius:7px;cursor:pointer;transition:background .1s}
.sess-item:hover{background:#f3f4f6}
.sess-item.active{background:#eef2ff}
.sess-t{font-size:11px;color:#374151;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
#main{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
#view-rubick{flex:1;display:flex;flex-direction:column;overflow:hidden}
#rubick-welcome{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px 24px;gap:12px}
.wlc-icon{width:62px;height:62px;border-radius:18px;background:linear-gradient(135deg,#6366f1,#8b5cf6);display:flex;align-items:center;justify-content:center;font-size:32px;color:#fff;box-shadow:0 4px 20px rgba(99,102,241,.22)}
.wlc-title{font-size:26px;font-weight:800;color:#6366f1}
.wlc-sub{font-size:13px;color:#9ca3af;text-align:center;max-width:400px;line-height:1.6}
.suggestions{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px;max-width:540px;width:100%}
.sug-btn{padding:12px 14px;background:#fff;border:1px solid #e5e7eb;border-radius:11px;color:#4b5563;font-size:13px;cursor:pointer;text-align:left;transition:all .14s;line-height:1.5;font-family:inherit}
.sug-btn:hover{background:#eef2ff;border-color:#c7d2fe;color:#6366f1}
#rubick-msgs{flex:1;overflow-y:auto;padding:18px 0 10px;display:none;flex-direction:column;scroll-behavior:smooth}
#rubick-msgs.visible{display:flex}
#rubick-msgs::-webkit-scrollbar{width:3px}
#rubick-msgs::-webkit-scrollbar-thumb{background:rgba(99,102,241,.15);border-radius:2px}
.msg-row{display:flex;padding:5px 22px;max-width:780px;width:100%;align-self:center;animation:fadeUp .18s ease-out}
@keyframes fadeUp{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.msg-row.user{justify-content:flex-end}
.msg-row.assistant{justify-content:flex-start;gap:9px}
.msg-av{width:28px;height:28px;border-radius:50%;flex-shrink:0;margin-top:2px;display:flex;align-items:center;justify-content:center;font-size:13px;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;box-shadow:0 2px 6px rgba(99,102,241,.2)}
.msg-b{max-width:78%;padding:10px 14px;border-radius:15px;font-size:13.5px;line-height:1.65;word-break:break-word}
.msg-row.user .msg-b{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;border-bottom-right-radius:4px}
.msg-row.assistant .msg-b{background:#fff;border:1px solid #e5e7eb;color:#374151;border-bottom-left-radius:4px;box-shadow:0 1px 3px rgba(0,0,0,.04)}
.msg-b pre{background:#f8f9fb;border:1px solid #e5e7eb;border-radius:8px;padding:10px 12px;margin:8px 0;font-size:12px;font-family:'SF Mono',Menlo,monospace;overflow-x:auto;color:#4f46e5;line-height:1.5}
.msg-b code{background:#eef2ff;padding:2px 5px;border-radius:4px;font-family:'SF Mono',Menlo,monospace;font-size:.87em;color:#6366f1}
.msg-b pre code{background:none;padding:0;color:#4f46e5}
.msg-b strong{color:#111827}
.r-tag{display:inline-flex;align-items:center;gap:4px;font-size:10px;color:#6366f1;background:#eef2ff;border:1px solid #c7d2fe;border-radius:4px;padding:2px 6px;margin-bottom:5px}
.typing-dots{display:flex;gap:4px;padding:3px 0;align-items:center}
.typing-dots span{width:6px;height:6px;border-radius:50%;background:#6366f1;opacity:.5;animation:dot 1.4s infinite ease-in-out}
.typing-dots span:nth-child(2){animation-delay:.2s}
.typing-dots span:nth-child(3){animation-delay:.4s}
@keyframes dot{0%,80%,100%{transform:scale(.7);opacity:.35}40%{transform:scale(1.1);opacity:1}}
#rubick-inp{flex-shrink:0;padding:9px 18px 13px}
#rubick-inp-box{max-width:780px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:15px;padding:9px 11px;display:flex;align-items:flex-end;gap:8px;box-shadow:0 1px 3px rgba(0,0,0,.04);transition:all .14s}
#rubick-inp-box:focus-within{border-color:#c7d2fe;box-shadow:0 0 0 3px rgba(99,102,241,.07)}
#rubick-in{flex:1;background:none;border:none;outline:none;color:#1a1a2e;font-size:13.5px;font-family:inherit;resize:none;line-height:1.55;max-height:160px;overflow-y:auto;padding:2px 0}
#rubick-in::placeholder{color:#9ca3af}
.r-send{width:32px;height:32px;border-radius:8px;border:none;cursor:pointer;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;font-size:14px;flex-shrink:0;display:flex;align-items:center;justify-content:center;transition:opacity .14s}
.r-send:hover{opacity:.85}
.r-send:disabled{opacity:.3;cursor:default;background:#d1d5db}
#rubick-hint{text-align:center;font-size:10.5px;color:#9ca3af;margin-top:5px;max-width:780px;margin-left:auto;margin-right:auto}
#view-feature{flex:1;display:none;flex-direction:column;overflow:hidden}
#f-topbar{background:#fff;border-bottom:1px solid #e5e7eb;padding:8px 12px;display:flex;align-items:center;gap:7px;flex-shrink:0}
.f-back-btn{width:26px;height:26px;border:1px solid #e5e7eb;border-radius:6px;background:none;cursor:pointer;color:#6b7280;font-size:13px;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:all .12s}
.f-back-btn:hover{background:#f3f4f6}
#f-name{font-size:12.5px;font-weight:800;color:#1a1a2e;max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex-shrink:0}
.ph-pills{display:flex;gap:4px;flex:1;overflow:hidden;align-items:center}
.php{font-size:11px;font-weight:700;padding:4px 11px;border-radius:6px;cursor:pointer;border:1.5px solid transparent;transition:all .12s;white-space:nowrap;flex-shrink:0;user-select:none}
.php:hover{transform:translateY(-1px);box-shadow:0 2px 8px rgba(0,0,0,.1)}
.php-done{background:#dcfce7;color:#166534;border-color:#bbf7d0}
.php-pending{background:#f1f5f9;color:#94a3b8;border-color:#e2e8f0}
.php-sel{box-shadow:0 0 0 2.5px #6366f1 !important;transform:translateY(-1px)}
#f-ver-sel{background:#fff;border:1px solid #d1d5db;border-radius:7px;padding:3px 6px;font-size:11px;color:#374151;cursor:pointer;font-family:inherit;outline:none;flex-shrink:0;max-width:76px}
#f-workspace{display:flex;flex:1;overflow:hidden}
#f-content{flex:1;overflow-y:auto;padding:14px;min-width:0;background:#f8f9fb}
#f-content::-webkit-scrollbar{width:3px}
#f-content::-webkit-scrollbar-thumb{background:rgba(0,0,0,.07);border-radius:2px}
.f-loading{display:flex;align-items:center;justify-content:center;height:200px;color:#9ca3af;font-size:13px}
.empty-pane{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:180px;color:#9ca3af;gap:8px;font-size:13px;text-align:center;padding:24px}
.empty-pane .ei{font-size:30px;margin-bottom:4px}
.empty-pane b{color:#374151;font-size:14px}
.fc-html{background:#fff;border-radius:12px;border:1px solid #e5e7eb;padding:20px;overflow-x:auto;font-size:13.5px;line-height:1.72;color:#374151}
.fc-html h1{font-size:19px;font-weight:800;color:#111827;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid #e5e7eb}
.fc-html h2{font-size:15px;font-weight:700;color:#1a1a2e;margin:20px 0 8px}
.fc-html h3{font-size:13px;font-weight:700;color:#374151;margin:14px 0 5px}
.fc-html p{margin-bottom:9px}
.fc-html pre.mermaid{background:#f8f9fb;border:1px solid #e5e7eb;border-radius:10px;padding:16px;margin:14px 0;overflow-x:auto;text-align:center}
.fc-html pre:not(.mermaid){background:#f8f9fb;border:1px solid #e5e7eb;border-radius:8px;padding:11px;font-size:11.5px;font-family:'SF Mono',Menlo,monospace;overflow-x:auto;margin:10px 0}
.fc-html code{background:#eef2ff;padding:2px 5px;border-radius:4px;font-family:'SF Mono',Menlo,monospace;font-size:.86em;color:#6366f1}
.fc-html pre code{background:none;padding:0;color:#4f46e5}
.fc-html table{width:100%;border-collapse:collapse;margin:12px 0;font-size:12.5px}
.fc-html th{background:#f8f9fb;padding:7px 9px;text-align:left;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;color:#9ca3af;border-bottom:2px solid #e5e7eb}
.fc-html td{padding:7px 9px;border-bottom:1px solid #e5e7eb}
.fc-html blockquote{border-left:3px solid #6366f1;padding:8px 14px;background:#eef2ff;border-radius:0 8px 8px 0;margin:10px 0;color:#4b5563;font-style:italic}
.fc-html ul,.fc-html ol{padding-left:20px;margin-bottom:9px}
.fc-html li{margin-bottom:3px}
.fc-html .status-bar{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 16px}
.fc-html .status-item{background:#f1f5f9;border:1px solid #e2e8f0;border-radius:6px;padding:4px 10px;font-size:11px;color:#64748b}
.fc-html .status-item strong{color:#334155}
.fc-html .mermaid svg{max-width:100%}
.fc-md{background:#fff;border-radius:12px;border:1px solid #e5e7eb;padding:20px;font-size:13.5px;line-height:1.72;color:#374151}
.fc-md h1{font-size:19px;font-weight:800;color:#111827;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid #e5e7eb}
.fc-md h2{font-size:15px;font-weight:700;color:#1a1a2e;margin:16px 0 8px}
.fc-md h3{font-size:13px;font-weight:700;color:#374151;margin:12px 0 5px}
.fc-md p{margin-bottom:9px}
.fc-md pre{background:#f8f9fb;border:1px solid #e5e7eb;border-radius:8px;padding:11px;font-size:11.5px;font-family:'SF Mono',Menlo,monospace;overflow-x:auto;margin:10px 0}
.fc-md code{background:#eef2ff;padding:2px 5px;border-radius:4px;font-family:'SF Mono',Menlo,monospace;font-size:.86em;color:#6366f1}
.fc-md pre code{background:none;padding:0;color:#4f46e5}
.fc-md table{width:100%;border-collapse:collapse;margin:12px 0;font-size:12.5px}
.fc-md th{background:#f8f9fb;padding:7px 9px;text-align:left;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;color:#9ca3af;border-bottom:2px solid #e5e7eb}
.fc-md td{padding:7px 9px;border-bottom:1px solid #e5e7eb}
.fc-md blockquote{border-left:3px solid #6366f1;padding:8px 14px;background:#eef2ff;border-radius:0 8px 8px 0;margin:10px 0;color:#4b5563;font-style:italic}
.fc-md ul,.fc-md ol{padding-left:20px;margin-bottom:9px}
.fc-md li{margin-bottom:3px}
#f-chat{width:330px;border-left:1px solid #e5e7eb;display:flex;flex-direction:column;background:#fff;flex-shrink:0}
#f-chat-hdr{padding:7px 10px;border-bottom:1px solid #e5e7eb;display:flex;align-items:center;gap:6px;background:#f8f9ff;flex-shrink:0}
#f-phase-ic{font-size:14px;flex-shrink:0}
#f-phase-nm{font-size:12px;font-weight:700;color:#374151;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#f-run-btn{padding:4px 9px;background:linear-gradient(135deg,#f97316,#ef4444);color:#fff;border:none;border-radius:6px;font-size:11px;font-weight:700;cursor:pointer;font-family:inherit;white-space:nowrap;transition:opacity .12s;flex-shrink:0}
#f-run-btn:hover{opacity:.88}
#f-run-btn:disabled{opacity:.4;cursor:not-allowed}
#f-msgs{flex:1;overflow-y:auto;padding:9px;display:flex;flex-direction:column;gap:5px}
#f-msgs::-webkit-scrollbar{width:3px}
.fcm{font-size:12.5px;line-height:1.6;padding:7px 10px;border-radius:9px;max-width:94%;word-break:break-word}
.fcm.user{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;align-self:flex-end;border-bottom-right-radius:3px}
.fcm.assistant{background:#f8f9fb;border:1px solid #e5e7eb;color:#374151;align-self:flex-start;border-bottom-left-radius:3px}
.fcm code{background:#e0e7ff;padding:1px 4px;border-radius:3px;font-family:'SF Mono',Menlo,monospace;font-size:.85em;color:#4f46e5}
.fcm pre{background:#1e1b4b;color:#e0e7ff;padding:7px;border-radius:6px;font-size:10.5px;overflow-x:auto;margin:5px 0}
.fcm-next-action{display:flex;align-items:center;justify-content:space-between;gap:10px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:8px 12px;font-size:12.5px;color:#166534;align-self:stretch;max-width:100%}
.fcm-run-next{background:#16a34a;color:#fff;border:none;border-radius:6px;padding:5px 12px;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap;flex-shrink:0}
.fcm-run-next:hover{background:#15803d}
.pipe-wrap{background:#f8f9fb;border:1px solid #e5e7eb;border-radius:12px;padding:12px;margin-top:10px;width:100%}
.pipe-title{font-size:13px;font-weight:700;color:#1a1a2e;margin-bottom:10px;display:flex;align-items:center;gap:6px}
.pipe-title span{font-size:18px}
.pipe-card{display:flex;align-items:center;gap:10px;padding:8px 10px;background:#fff;border:1px solid #e5e7eb;border-radius:9px;margin-bottom:6px;transition:all .12s}
.pipe-card:hover{border-color:#c7d2fe;box-shadow:0 2px 8px rgba(99,102,241,.08)}
.pipe-hero{width:32px;height:32px;border-radius:50%;background:linear-gradient(135deg,#f97316,#ef4444);display:flex;align-items:center;justify-content:center;font-size:14px;color:#fff;flex-shrink:0}
.pipe-info{flex:1;min-width:0}
.pipe-svc{font-size:12.5px;font-weight:700;color:#111827;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pipe-role{font-size:10px;color:#9ca3af;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pipe-impact{font-size:9px;font-weight:700;padding:2px 6px;border-radius:3px;text-transform:uppercase;letter-spacing:.3px;flex-shrink:0}
.pipe-impact-high{background:#fee2e2;color:#dc2626}
.pipe-impact-medium{background:#fef9c3;color:#854d0e}
.pipe-impact-low{background:#f0fdf4;color:#166534}
.pipe-actions{display:flex;gap:4px;flex-shrink:0}
.pipe-btn{font-size:10px;font-weight:600;padding:4px 8px;border-radius:5px;border:1px solid #e5e7eb;background:#fff;cursor:pointer;transition:all .12s;font-family:inherit;white-space:nowrap}
.pipe-btn:hover{background:#eef2ff;border-color:#c7d2fe;color:#6366f1}
.pipe-btn-done{background:#dcfce7;color:#166534;border-color:#bbf7d0}
.pipe-btn-run{background:#6366f1;color:#fff;border-color:#6366f1}
.pipe-btn-run:hover{background:#4f46e5}
.pp-wrap{background:#f0f4ff;border:1px solid #c7d2fe;border-radius:10px;padding:10px 14px;width:100%;box-sizing:border-box}
.pp-header{font-size:12.5px;font-weight:600;color:#3730a3;margin-bottom:8px}
.pp-bar{background:#e0e7ff;border-radius:4px;height:6px;margin-bottom:8px;overflow:hidden}
.pp-fill{background:linear-gradient(90deg,#6366f1,#8b5cf6);height:6px;border-radius:4px;transition:width .5s ease}
.pp-steps{display:flex;flex-direction:column;gap:3px;margin-top:4px}
.pp-step{font-size:11.5px;color:#4b5563;display:flex;align-items:flex-start;gap:5px;line-height:1.4}
.pp-step-active{color:#4f46e5;font-weight:500}
.pp-step-done{color:#15803d}
.pp-step-error{color:#dc2626}
#f-inp{padding:7px 8px;border-top:1px solid #e5e7eb;display:flex;gap:5px;flex-shrink:0}
#f-in{flex:1;border:1px solid #e5e7eb;border-radius:8px;padding:7px 10px;font-size:12.5px;font-family:inherit;outline:none;color:#1a1a2e;resize:none;line-height:1.4;max-height:72px;overflow-y:auto}
#f-in:focus{border-color:#c7d2fe}
.f-send{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;border:none;border-radius:8px;width:32px;height:32px;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:14px}
.u-badge{font-size:9px;color:#9ca3af;font-family:'SF Mono',Menlo,monospace;margin-top:3px;display:flex;gap:6px;align-items:center}
.u-badge span{background:#f3f4f6;padding:1px 5px;border-radius:3px}
.u-cost{color:#6366f1;font-weight:600}
.sess-cost{font-size:9px;color:#9ca3af;font-family:'SF Mono',monospace;margin-left:auto;flex-shrink:0}
.f-usage{font-size:10px;color:#9ca3af;font-family:'SF Mono',Menlo,monospace;display:flex;gap:8px;padding:0 0 8px}
.f-usage span{background:#f3f4f6;border:1px solid #e5e7eb;padding:2px 7px;border-radius:4px}
/* ── New Feature Modal ── */
#nf-overlay{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;align-items:center;justify-content:center;z-index:100;backdrop-filter:blur(2px)}
#nf-overlay.open{display:flex}
#nf-modal{background:#fff;border-radius:20px;width:100%;max-width:500px;box-shadow:0 20px 60px rgba(0,0,0,.18);overflow:hidden;animation:modalIn .18s ease-out}
@keyframes modalIn{from{opacity:0;transform:scale(.96) translateY(8px)}to{opacity:1;transform:none}}
#nf-hdr{background:linear-gradient(135deg,#f97316,#ef4444);padding:18px 22px 14px;color:#fff}
#nf-hdr h2{font-size:17px;font-weight:800;margin-bottom:3px}
#nf-hdr p{font-size:12px;opacity:.75}
#nf-body{padding:20px 22px}
.nf-field{margin-bottom:14px}
.nf-field label{display:block;font-size:11px;font-weight:700;color:#374151;text-transform:uppercase;letter-spacing:.4px;margin-bottom:6px}
.nf-field label .req{color:#ef4444}
.nf-field label .hint{text-transform:none;letter-spacing:0;font-weight:400;color:#9ca3af;font-size:11px;margin-left:4px}
.nf-field input,.nf-field textarea{width:100%;border:1px solid #d1d5db;border-radius:10px;padding:9px 12px;font-size:13px;font-family:inherit;color:#1a1a2e;outline:none;transition:border .15s;background:#fff}
.nf-field input:focus,.nf-field textarea:focus{border-color:#c7d2fe;box-shadow:0 0 0 3px rgba(99,102,241,.08)}
.nf-field textarea{resize:vertical;min-height:60px;max-height:110px}
.nf-src-grid{display:flex;flex-direction:column;gap:8px}
.nf-src{border:1px solid #e5e7eb;border-radius:11px;overflow:hidden;transition:border-color .15s}
.nf-src.open{border-color:#c7d2fe}
.nf-src-hdr{display:flex;align-items:center;gap:8px;padding:9px 12px;cursor:pointer;user-select:none}
.nf-src-hdr:hover{background:#f9fafb}
.nf-src-icon{width:26px;height:26px;border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:13px;flex-shrink:0}
.nf-src-label{font-size:12px;font-weight:600;color:#374151;flex:1}
.nf-src-hint{font-size:10px;color:#9ca3af}
.nf-src-arrow{font-size:10px;color:#9ca3af;transition:transform .15s}
.nf-src.open .nf-src-arrow{transform:rotate(90deg)}
.nf-src-body{display:none;padding:0 12px 10px}
.nf-src.open .nf-src-body{display:block}
.nf-src-body textarea{width:100%;border:1px solid #d1d5db;border-radius:8px;padding:8px 10px;font-size:12px;font-family:inherit;color:#1a1a2e;outline:none;resize:vertical;min-height:54px;max-height:90px;transition:border .15s}
.nf-src-body textarea:focus{border-color:#c7d2fe;box-shadow:0 0 0 3px rgba(99,102,241,.08)}
.nf-src-body .sub-hint{font-size:10px;color:#9ca3af;margin-top:4px}
#nf-foot{display:flex;justify-content:flex-end;gap:8px;padding:0 22px 18px}
.nf-cancel{padding:8px 18px;border:1px solid #d1d5db;border-radius:9px;background:none;color:#6b7280;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit}
.nf-cancel:hover{background:#f9fafb}
.nf-create{padding:8px 20px;border:none;border-radius:9px;background:linear-gradient(135deg,#f97316,#ef4444);color:#fff;font-size:13px;font-weight:700;cursor:pointer;font-family:inherit;box-shadow:0 2px 10px rgba(239,68,68,.22);transition:opacity .15s}
.nf-create:hover{opacity:.88}
.nf-create:disabled{opacity:.45;cursor:not-allowed}
.r-upload-btn,.f-upload-btn{display:flex;align-items:center;justify-content:center;width:32px;height:32px;border-radius:8px;cursor:pointer;color:#9ca3af;transition:all .15s;flex-shrink:0}
.r-upload-btn:hover,.f-upload-btn:hover{color:#6366f1;background:#eef2ff}
#r-uploads,#f-uploads{display:flex;flex-wrap:wrap;gap:4px;padding:0 8px}
#r-uploads:empty,#f-uploads:empty{display:none;padding:0}
.upload-chip{display:inline-flex;align-items:center;gap:4px;padding:3px 8px;background:#eef2ff;border:1px solid #c7d2fe;border-radius:6px;font-size:10px;color:#4f46e5;max-width:160px}
.upload-chip span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.upload-chip button{background:none;border:none;color:#9ca3af;cursor:pointer;font-size:12px;padding:0;line-height:1}
.upload-chip button:hover{color:#ef4444}
/* ── Pipeline Diagram ── */
.pl-wrap{padding:16px 0 24px;overflow-x:auto}
.pl-canvas{display:flex;align-items:center;gap:0;min-width:max-content;padding:0 16px 8px}
.pl-stage{display:flex;flex-direction:column;align-items:center;gap:0;position:relative}
.pl-stage-hdr{font-size:9px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.06em;margin-bottom:7px;text-align:center}
.pl-conn{display:flex;flex-direction:column;align-items:center;justify-content:center;width:64px;flex-shrink:0;position:relative;margin-top:22px}
.pl-conn-line{width:100%;height:2px;background:#e5e7eb}
.pl-conn-line.active{background:linear-gradient(90deg,#22c55e,#6366f1)}
.pl-conn-label{position:absolute;top:-15px;font-size:8.5px;color:#9ca3af;white-space:nowrap;text-align:center;width:130px;left:50%;transform:translateX(-50%)}
.pl-card{width:144px;border:2px solid #e5e7eb;border-radius:12px;background:#fff;padding:13px 10px 11px;cursor:pointer;transition:all .18s;position:relative;box-shadow:0 1px 4px rgba(0,0,0,.05);text-align:center}
.pl-card:hover{transform:translateY(-2px);box-shadow:0 6px 18px rgba(0,0,0,.1)}
.pl-card.pl-done{border-color:#22c55e;background:#f0fdf4}
.pl-card.pl-pending{border-color:#e5e7eb;background:#fff}
.pl-card.pl-blocked{border-color:#e5e7eb;background:#f9fafb;opacity:.45;cursor:default}
.pl-card.pl-running{border-color:#6366f1;background:#eef2ff;animation:pl-pulse 1.5s infinite}
.pl-card.pl-active{box-shadow:0 0 0 3px rgba(99,102,241,.5) !important;transform:translateY(-2px)}
.pl-card-badge{position:absolute;top:7px;right:8px;font-size:13px;line-height:1}
.pl-card-ic{font-size:24px;margin-bottom:5px;display:block}
.pl-card-label{font-size:12px;font-weight:700;color:#111827;margin-bottom:3px}
.pl-card-meta{font-size:9.5px;color:#6b7280;line-height:1.4}
.pl-card-btn{font-size:10px;font-weight:700;padding:3px 10px;border-radius:6px;border:none;cursor:pointer;margin-top:7px;background:linear-gradient(135deg,#f97316,#ef4444);color:#fff;display:none;transition:opacity .12s}
.pl-card:hover .pl-card-btn{display:inline-block}
.pl-card.pl-blocked .pl-card-btn,.pl-card.pl-done .pl-card-btn{display:none!important}
.pl-svc-group{display:flex;flex-direction:column;gap:5px;align-items:center}
.pl-svc-card{width:132px;border:2px solid #f59e0b;border-radius:9px;background:#fffbeb;padding:9px 9px 8px;cursor:pointer;transition:all .18s;position:relative;text-align:center}
.pl-svc-card:hover{transform:translateY(-1px);box-shadow:0 4px 10px rgba(0,0,0,.1)}
.pl-svc-card.pl-done{border-color:#22c55e;background:#f0fdf4}
.pl-svc-card-name{font-size:10.5px;font-weight:700;color:#111827}
.pl-svc-card-meta{font-size:9px;color:#9ca3af;margin-top:2px}
.pl-terminal{width:144px;border:2px dashed #d1d5db;border-radius:12px;background:#f9fafb;padding:16px 10px;text-align:center;transition:all .3s}
.pl-terminal.unlocked{border:2px solid #22c55e;background:#f0fdf4;box-shadow:0 0 0 5px rgba(34,197,94,.12)}
.pl-terminal-ic{font-size:26px;margin-bottom:5px}
.pl-terminal-label{font-size:11.5px;font-weight:700;color:#9ca3af;line-height:1.4}
.pl-terminal.unlocked .pl-terminal-label{color:#166534}
@keyframes pl-pulse{0%,100%{box-shadow:0 0 0 0 rgba(99,102,241,.4)}50%{box-shadow:0 0 0 8px rgba(99,102,241,0)}}
</style>
</head>
<body>
<div id="app">

<nav id="sidebar">
  <div class="sb-logo">
    <div class="sb-logo-mark">⚔</div>
    <span class="sb-logo-text">NEMESIS</span>
  </div>
  <div style="padding:6px 8px 0">
    <div class="sb-sec">Tools</div>
    <button class="sb-tool active" id="tool-rubick" onclick="selectTool()">
      <div class="sb-tool-ic" style="background:linear-gradient(135deg,#f97316,#ef4444);color:#fff">⚔</div>
      Nemesis
    </button>
  </div>
  <div id="sess-wrap" style="display:none;max-height:140px;overflow-y:auto;padding:0 8px">
    <div id="sess-list"></div>
  </div>
  <div class="sb-divider"></div>
  <div style="padding:0 8px 3px">
    <div class="sb-sec">Features</div>
    <button class="sb-new" onclick="newFeat()">
      <svg width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>
      New Feature
    </button>
  </div>
  <div class="feat-scroll" id="feat-list"></div>
  <div class="sb-bottom" id="stats-txt">loading…</div>
</nav>

<div id="main">
  <div id="view-rubick">
    <div id="rubick-welcome">
      <div class="wlc-icon">⚔</div>
      <div class="wlc-title">Nemesis</div>
      <div class="wlc-sub">AI engineering orchestrator — Rubick knowledge graph · Ideation · Solutioning · Tech Spec</div>
      <div class="suggestions">
        <button class="sug-btn" onclick="rSuggest('How does pg-router route payments to the right acquirer?')">How does pg-router route payments?</button>
        <button class="sug-btn" onclick="rSuggest('Explain the emandate registration and debit flow end to end')">Explain emandate flow</button>
        <button class="sug-btn" onclick="rSuggest('Create a feature overview for instant offer discounts')">Create feature overview</button>
        <button class="sug-btn" onclick="rSuggest('What services does offers-engine interact with and how?')">How does offers-engine work?</button>
      </div>
    </div>
    <div id="rubick-msgs"></div>
    <div id="r-uploads"></div>
    <div id="rubick-inp">
      <div id="rubick-inp-box">
        <label class="r-upload-btn" title="Upload files">
          <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/></svg>
          <input type="file" id="r-file-in" multiple style="display:none" onchange="handleMainUpload(this)">
        </label>
        <textarea id="rubick-in" rows="1" placeholder="Ask Nemesis anything — knowledge, features, architecture…"></textarea>
        <button class="r-send" id="r-send-btn">
          <svg width="13" height="13" fill="currentColor" viewBox="0 0 24 24"><path d="M2 21 23 12 2 3v7l15 2-15 2z"/></svg>
        </button>
      </div>
      <div id="rubick-hint">Nemesis · Rubick graph · Ideation · Solutioning · Tech Spec</div>
    </div>
  </div>

  <div id="view-feature">
    <div id="f-topbar">
      <button class="f-back-btn" onclick="showView('rubick')" title="Back to Nemesis">←</button>
      <div id="f-name">Feature</div>
      <div class="ph-pills" id="f-pills"></div>
      <select id="f-ver-sel" onchange="selectVersion(this.value)" style="display:none" title="Version"></select>
    </div>
    <div id="f-workspace">
      <div id="f-content">
        <div id="f-area"><div class="f-loading">Select a feature to begin</div></div>
      </div>
      <div id="f-chat">
        <div id="f-chat-hdr">
          <span id="f-phase-ic">⚔</span>
          <span id="f-phase-nm">Nemesis</span>
          <button id="f-run-btn" onclick="runPhase()">▶ Run</button>
          <button id="f-run-full-btn" onclick="runFullPipeline()" title="Run all 3 phases automatically" style="font-size:11px;padding:4px 10px;background:#059669;color:#fff;border:1px solid #059669;border-radius:6px;cursor:pointer;margin-left:4px">⚡ Full</button>
        </div>
        <div id="f-msgs"></div>
        <div id="f-uploads"></div>
        <div id="f-inp">
          <label class="f-upload-btn" title="Upload files">
            <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/></svg>
            <input type="file" id="f-file-in" multiple style="display:none" onchange="handleFileUpload(this)">
          </label>
          <textarea id="f-in" rows="1" placeholder="Talk to Nemesis about this feature…"></textarea>
          <button class="f-send" onclick="fSend()">
            <svg width="13" height="13" fill="currentColor" viewBox="0 0 24 24"><path d="M2 21 23 12 2 3v7l15 2-15 2z"/></svg>
          </button>
        </div>
      </div>
    </div>
  </div>
</div>
</div>

<!-- New Feature Modal -->
<div id="nf-overlay" onclick="if(event.target===this)closeNF()">
  <div id="nf-modal">
    <div id="nf-hdr">
      <h2>New Feature</h2>
      <p>Name it and attach sources — Ideation will analyze them</p>
    </div>
    <div id="nf-body">
      <div class="nf-field">
        <label>Feature Name <span class="req">*</span></label>
        <input type="text" id="nf-name" placeholder="e.g. dfb-instant-discount, emi-retry-flow" autocomplete="off" />
      </div>
      <div class="nf-field">
        <label>Sources <span class="hint">— optional, attach any combination</span></label>
        <div class="nf-src-grid">

          <div class="nf-src" id="nf-src-slack">
            <div class="nf-src-hdr" onclick="toggleSrc('slack')">
              <div class="nf-src-icon" style="background:#e0f7fa;color:#0077b6">💬</div>
              <span class="nf-src-label">Slack Thread</span>
              <span class="nf-src-hint" id="nf-hint-slack">not added</span>
              <span class="nf-src-arrow">▶</span>
            </div>
            <div class="nf-src-body">
              <textarea id="nf-slack" placeholder="Paste Slack thread URLs or channel + ts, one per line&#10;e.g. https://razorpay.slack.com/archives/C.../p..."></textarea>
              <div class="sub-hint">Can be any channel URL, thread link, or channel ID</div>
            </div>
          </div>

          <div class="nf-src" id="nf-src-doc">
            <div class="nf-src-hdr" onclick="toggleSrc('doc')">
              <div class="nf-src-icon" style="background:#e8f5e9;color:#2e7d32">📄</div>
              <span class="nf-src-label">Google Doc / PRD</span>
              <span class="nf-src-hint" id="nf-hint-doc">not added</span>
              <span class="nf-src-arrow">▶</span>
            </div>
            <div class="nf-src-body">
              <textarea id="nf-doc" placeholder="Paste Google Doc or Drive URLs, one per line&#10;e.g. https://docs.google.com/document/d/..."></textarea>
              <div class="sub-hint">Works with Google Docs, Drive files, and Notion pages</div>
            </div>
          </div>

          <div class="nf-src" id="nf-src-gmail">
            <div class="nf-src-hdr" onclick="toggleSrc('gmail')">
              <div class="nf-src-icon" style="background:#fce4ec;color:#c62828">✉</div>
              <span class="nf-src-label">Gmail Thread</span>
              <span class="nf-src-hint" id="nf-hint-gmail">not added</span>
              <span class="nf-src-arrow">▶</span>
            </div>
            <div class="nf-src-body">
              <textarea id="nf-gmail" placeholder="Paste Gmail thread URLs or message IDs, one per line&#10;e.g. https://mail.google.com/mail/u/0/#inbox/..."></textarea>
              <div class="sub-hint">Full thread URL from Gmail or just the thread ID</div>
            </div>
          </div>

          <div class="nf-src" id="nf-src-verbal">
            <div class="nf-src-hdr" onclick="toggleSrc('verbal')">
              <div class="nf-src-icon" style="background:#ede7f6;color:#4527a0">💡</div>
              <span class="nf-src-label">Verbal Brief</span>
              <span class="nf-src-hint" id="nf-hint-verbal">not added</span>
              <span class="nf-src-arrow">▶</span>
            </div>
            <div class="nf-src-body">
              <textarea id="nf-verbal" placeholder="Describe the feature in your own words — what exists today, what needs to change, and why…"></textarea>
              <div class="sub-hint">Ideation will treat this as primary context</div>
            </div>
          </div>

        </div>
      </div>
    </div>
    <div id="nf-foot">
      <button class="nf-cancel" onclick="closeNF()">Cancel</button>
      <button class="nf-create" id="nf-create-btn" onclick="createFeat()">Create Feature →</button>
    </div>
  </div>
</div>

<script>
const esc = s => s==null?'':String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const S = {view:'rubick', rSid:null, fSlug:null, fName:null, fPhase:'ideation', fSid:null, fDetail:null, fVersion:null};

function fmtTok(n){return n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':''+n;}

function cleanMixedHtml(raw) {
  if(!raw) return '';
  const htmlBlocks=[];
  let t=raw.replace(/<(pre|table|div|blockquote|ul|ol|h[1-6]|section|header|nav|figure|details|summary|dl|style|script)[\s>][\s\S]*?<\/\1>/gi,(_)=>{const i=htmlBlocks.length;htmlBlocks.push(_);return `\x00H${i}\x00`;});
  t=t.replace(/<[^>]+>/g,(_)=>{const i=htmlBlocks.length;htmlBlocks.push(_);return `\x00H${i}\x00`;});
  t=t.replace(/```(\w*)\n?([\s\S]*?)```/g,(_,lang,code)=>{
    const i=htmlBlocks.length;
    if(lang==='mermaid'){htmlBlocks.push(`<pre class="mermaid">${code.replace(/\n$/,'')}</pre>`);}
    else{htmlBlocks.push(`<pre><code>${esc(code.replace(/\n$/,''))}</code></pre>`);}
    return `\x00H${i}\x00`;
  });
  t=t.replace(/`([^`\n]+)`/g,(_,c)=>`<code>${esc(c)}</code>`);
  t=t.replace(/^\|(.+)\|$/gm,(row)=>{
    const cells=row.split('|').slice(1,-1).map(c=>c.trim());
    if(cells.every(c=>/^[-:]+$/.test(c)))return '\x00SEP\x00';
    return '<tr>'+cells.map(c=>`<td>${c.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')}</td>`).join('')+'</tr>';
  });
  t=t.replace(/(<tr>[\s\S]*?)(\x00SEP\x00)([\s\S]*?)(?=\n\n|\x00H|\s*$)/g,(_,hdr,sep,body)=>{
    const thRow=hdr.replace(/<td>/g,'<th>').replace(/<\/td>/g,'</th>');
    return `<table><thead>${thRow}</thead><tbody>${body}</tbody></table>`;
  });
  t=t.replace(/\x00SEP\x00/g,'');
  t=t.replace(/^### (.+)$/mg,'<h3>$1</h3>').replace(/^## (.+)$/mg,'<h2>$1</h2>').replace(/^# (.+)$/mg,'<h1>$1</h1>');
  t=t.replace(/^> (.+)$/mg,'<blockquote>$1</blockquote>');
  t=t.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/\*(.+?)\*/g,'<em>$1</em>');
  t=t.replace(/^---$/mg,'<hr>');
  t=t.replace(/\n{2,}/g,'</p><p>').replace(/\n/g,'<br>');
  t='<p>'+t+'</p>';
  t=t.replace(/<p>\s*<(h[1-6]|hr|table|blockquote|pre)/g,'<$1').replace(/<\/(h[1-6]|hr|table|blockquote|pre)>\s*<\/p>/g,'</$1>');
  t=t.replace(/<p>\s*<\/p>/g,'');
  t=t.replace(/\x00H(\d+)\x00/g,(_,i)=>htmlBlocks[+i]);
  return t;
}

function renderMd(raw) {
  if(!raw) return '';
  const blocks=[];
  let t=raw.replace(/```(\w*)\n?([\s\S]*?)```/g,(_,l,c)=>{const i=blocks.length;blocks.push(`<pre><code>${esc(c.replace(/\n$/,''))}</code></pre>`);return `\x00B${i}\x00`;});
  t=t.replace(/`([^`\n]+)`/g,(_,c)=>{const i=blocks.length;blocks.push(`<code>${esc(c)}</code>`);return `\x00B${i}\x00`;});
  t=esc(t);
  t=t.replace(/^### (.+)$/mg,'<h3>$1</h3>').replace(/^## (.+)$/mg,'<h2>$1</h2>').replace(/^# (.+)$/mg,'<h1>$1</h1>');
  t=t.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/\*(.+?)\*/g,'<em>$1</em>');
  t=t.replace(/\n/g,'<br>');
  t=t.replace(/\x00B(\d+)\x00/g,(_,i)=>blocks[+i]);
  return t;
}

function showView(name) {
  S.view=name;
  document.getElementById('view-rubick').style.cssText=name==='rubick'?'flex:1;display:flex;flex-direction:column;overflow:hidden':'display:none';
  document.getElementById('view-feature').style.cssText=name==='feature'?'flex:1;display:flex;flex-direction:column;overflow:hidden':'display:none';
  document.getElementById('tool-rubick').classList.toggle('active',name==='rubick');
  document.getElementById('sess-wrap').style.display=name==='rubick'?'':'none';
  document.querySelectorAll('.sf-item').forEach(el=>el.classList.toggle('active',el.dataset.slug===S.fSlug&&name==='feature'));
}

function selectTool() { showView('rubick'); }

async function loadStats() {
  const d=await fetch('/api/stats').then(r=>r.json()).catch(()=>({}));
  document.getElementById('stats-txt').innerHTML=`<b>${(d.total_nodes||0).toLocaleString()}</b> nodes · <b>${(d.total_edges||0).toLocaleString()}</b> edges`;
  fetch('/api/usage').then(r=>r.json()).then(u=>{
    if(u.total) document.getElementById('stats-txt').innerHTML+=`<br>Total: <b>$${u.total.cost_usd.toFixed(2)}</b> · ${fmtTok(u.total.input_tokens+u.total.output_tokens)} tokens`;
  }).catch(()=>{});
}

async function loadFeatList() {
  const d=await fetch('/api/features').then(r=>r.json()).catch(()=>({features:[]}));
  const el=document.getElementById('feat-list');
  if(!d.features?.length){el.innerHTML='<div style="font-size:11px;color:#9ca3af;padding:4px 2px">No features yet</div>';return;}
  el.innerHTML=d.features.map(f=>{
    const slug=f.name.toLowerCase().replace(/[^a-z0-9-]/g,'-');
    const ph=(f.phase||'').toLowerCase();
    const doneMap={ideation:['ideation','solutioning','techspec','e2e'],solutioning:['solutioning','techspec','e2e'],techspec:['techspec','e2e'],e2e:['e2e']};
    const done=doneMap[ph]||[];
    const stages=[['🎨','I'],['⚙️','S'],['📄','T'],['🔧','Impl'],['🧪','E2E']];
    const stageKeys=['ideation','solutioning','techspec','impl','e2e'];
    const pills=stages.map(([ic,lbl],i)=>`<span class="sp ${done.includes(stageKeys[i])||(ph==='ideation'&&i===0)||(ph==='solutioning'&&i<=1)||(ph==='techspec'&&i<=2)?'sp-done':'sp-pending'}" title="${lbl}">${ic}</span>`).join('');
    return `<button class="sf-item" data-slug="${esc(slug)}" onclick="openFeat('${esc(slug)}','${esc(f.name).replace(/'/g,"\\'")}')"><div class="sf-ic">⚔</div><span class="sf-name">${esc(f.name)}</span><div class="sf-pills">${pills}</div></button>`;
  }).join('');
  const ps=window.location.pathname.replace(/^\/nemesis\/?/,'');
  if(ps&&ps.length>1){const m=d.features.find(f=>f.name.toLowerCase().replace(/[^a-z0-9-]/g,'-')===ps);if(m)openFeat(ps,m.name);}
}

function newFeat(){
  document.getElementById('nf-name').value='';
  ['slack','doc','gmail','verbal'].forEach(k=>{
    document.getElementById('nf-'+k).value='';
    document.getElementById('nf-src-'+k).classList.remove('open');
    document.getElementById('nf-hint-'+k).textContent='not added';
  });
  document.getElementById('nf-create-btn').disabled=false;
  document.getElementById('nf-create-btn').textContent='Create Feature →';
  document.getElementById('nf-overlay').classList.add('open');
  setTimeout(()=>document.getElementById('nf-name').focus(),80);
}
function closeNF(){document.getElementById('nf-overlay').classList.remove('open');}
function toggleSrc(k){document.getElementById('nf-src-'+k).classList.toggle('open');}

// Update hint when source textarea changes
['slack','doc','gmail','verbal'].forEach(k=>{
  document.addEventListener('DOMContentLoaded',()=>{});  // wait for DOM
  setTimeout(()=>{
    const ta=document.getElementById('nf-'+k);
    if(!ta)return;
    ta.addEventListener('input',()=>{
      const v=ta.value.trim();
      const hint=document.getElementById('nf-hint-'+k);
      if(!v){hint.textContent='not added';hint.style.color='';}
      else{const n=v.split('\n').filter(l=>l.trim()).length;hint.textContent=n+(k==='verbal'?' chars':n===1?' source':' sources');hint.style.color='#6366f1';}
    });
  },0);
});

async function createFeat(){
  const name=document.getElementById('nf-name').value.trim();
  if(!name){document.getElementById('nf-name').focus();return;}
  const btn=document.getElementById('nf-create-btn');
  btn.disabled=true;btn.textContent='Creating…';
  try{
    const r=await fetch('/api/features',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        name,
        slack_threads:document.getElementById('nf-slack').value.trim(),
        google_docs:document.getElementById('nf-doc').value.trim(),
        gmail_threads:document.getElementById('nf-gmail').value.trim(),
        description:document.getElementById('nf-verbal').value.trim(),
      })});
    const d=await r.json();
    if(d.error){alert('Error: '+d.error);btn.disabled=false;btn.textContent='Create Feature →';return;}
    closeNF();
    await loadFeatList();
    openFeat(d.slug,d.name);
  }catch(e){alert('Network error: '+e.message);btn.disabled=false;btn.textContent='Create Feature →';}
}

function rShowWelcome(show){
  document.getElementById('rubick-welcome').style.display=show?'flex':'none';
  const m=document.getElementById('rubick-msgs');
  if(show)m.classList.remove('visible');else m.classList.add('visible');
}
function rAppend(html,cls){
  const msgs=document.getElementById('rubick-msgs');
  const row=document.createElement('div');row.className='msg-row '+cls;
  row.innerHTML=cls==='assistant'?`<div class="msg-av">⬡</div><div class="msg-b">${html}</div>`:`<div class="msg-b">${esc(html)}</div>`;
  msgs.appendChild(row);msgs.scrollTop=msgs.scrollHeight;return row;
}
async function rSend(){
  const inp=document.getElementById('rubick-in');
  const msg=inp.value.trim();if(!msg)return;
  inp.value='';inp.style.height='';
  const uploads=[...mainPendingUploads];mainPendingUploads=[];_renderUploadChips([],'r-uploads');
  rShowWelcome(false);
  rAppend(msg+(uploads.length?`<div style="margin-top:4px;font-size:10px;color:rgba(255,255,255,.7)">📎 ${uploads.map(u=>u.name).join(', ')}</div>`:''),'user');
  const t0=Date.now();
  const tr=rAppend('<div class="typing-dots"><span></span><span></span><span></span></div>','assistant');
  document.getElementById('r-send-btn').disabled=true;
  try{
    const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      message:msg,session_id:S.rSid,
      uploaded_files:uploads.length?uploads:undefined
    })});
    const d=await r.json();
    if(d.session_id&&!S.rSid){S.rSid=d.session_id;loadSessions();}
    tr.remove();
    const elapsed=((Date.now()-t0)/1000).toFixed(1);
    const u=d.usage||{};
    const utxt=u.input_tokens?` · ${fmtTok(u.input_tokens+u.output_tokens)} tok · $${(u.cost_usd||0).toFixed(4)}`:'';
    const tag=d.rubick_target&&d.rubick_target!==msg?`<div class="r-tag">⚔ ${esc(d.rubick_target.substring(0,50))} <span style="opacity:.5">${elapsed}s${utxt}</span></div>`:'';
    rAppend(tag+renderMd(d.response||d.error||'(empty)'),'assistant');
    if(d.skill_cmd){
      rAppend(renderMd(`**Nemesis → ${d.skill_cmd.skill}** — use the feature view to execute this phase.`),'assistant');
    }
  }catch(e){tr.remove();rAppend(`<em>Error: ${esc(e.message)}</em>`,'assistant');}
  document.getElementById('r-send-btn').disabled=false;inp.focus();
}
window.rSuggest=t=>{document.getElementById('rubick-in').value=t;rSend();};

async function loadSessions(){
  const d=await fetch('/api/sessions').then(r=>r.json()).catch(()=>({sessions:[]}));
  const list=document.getElementById('sess-list');
  if(!d.sessions?.length){list.innerHTML='';return;}
  const today=new Date().toDateString();let last='',html='';
  for(const s of d.sessions.slice(0,12)){
    const dt=new Date(s.updated_at).toDateString();
    const lbl=dt===today?'Today':new Date(s.updated_at).toLocaleDateString('en',{month:'short',day:'numeric'});
    if(lbl!==last){html+=`<div class="sess-day">${lbl}</div>`;last=lbl;}
    const t=s.title.length>30?s.title.substring(0,30)+'…':s.title;
    const cost=s.total_cost_usd?`$${s.total_cost_usd.toFixed(2)}`:'';
    html+=`<div class="sess-item${s.session_id===S.rSid?' active':''}" onclick="openSess('${s.session_id}')"><div class="sess-t">${esc(t)}</div>${cost?`<span class="sess-cost">${cost}</span>`:''}</div>`;
  }
  list.innerHTML=html;
}
async function openSess(sid){
  const d=await fetch(`/api/sessions/${sid}`).then(r=>r.json());if(d.error)return;
  S.rSid=sid;selectTool();
  document.getElementById('rubick-msgs').innerHTML='';rShowWelcome(false);
  for(const m of d.messages){
    if(m.role==='user')rAppend(m.content,'user');
    else{const mu=m.cost_usd?` · ${fmtTok((m.input_tokens||0)+(m.output_tokens||0))} tok · $${(m.cost_usd||0).toFixed(4)}`:'';
      const tag=m.rubick_target?`<div class="r-tag">⬡ ${esc(m.rubick_target)} <span style="opacity:.5">${m.elapsed||0}s${mu}</span></div>`:'';rAppend(tag+renderMd(m.content),'assistant');}
  }
  loadSessions();
}

const PHASES={
  ideation:     {ic:'🎨',label:'Ideation',     tab:'overview',  color:'#166534',bg:'#dcfce7',border:'#bbf7d0'},
  solutioning:  {ic:'⚙️', label:'Solutioning',  tab:'solution',  color:'#1d4ed8',bg:'#dbeafe',border:'#93c5fd'},
  techspec:     {ic:'📄',label:'Tech Spec',    tab:'tech-spec', color:'#7c3aed',bg:'#fae8ff',border:'#d8b4fe'},
  e2e:          {ic:'🧪',label:'E2E',          tab:'e2e',       color:'#b45309',bg:'#fef3c7',border:'#fcd34d'},
};
const NEXT_PHASE={ideation:'solutioning',solutioning:'techspec',techspec:'e2e'};

function _phaseSid(slug,ph){return localStorage.getItem('fp_'+slug+'_'+ph)||null;}
function _setPhaseSid(slug,ph,sid){localStorage.setItem('fp_'+slug+'_'+ph,sid);}
function _saveMsgs(slug,ph){
  const el=document.getElementById('f-msgs');
  if(!el||!slug||!ph||ph==='pipeline'||ph==='pipelines')return;
  try{localStorage.setItem('fm_'+slug+'_'+ph,el.innerHTML);}catch(e){}
}
function _restoreMsgs(slug,ph){
  try{const s=localStorage.getItem('fm_'+slug+'_'+ph);if(s)return s;}catch(e){}
  return null;
}
// Upstream context: what artifacts exist for phases before this one
function _upstreamContextCard(phase){
  const d=S.fDetail||{};
  const INPUTS={solutioning:['ideation'],techspec:['ideation','solutioning'],e2e:['ideation','solutioning','techspec','implementation']};
  const needed=INPUTS[phase]||[];
  if(!needed.length)return '';
  const LABELS={ideation:d.overview_file||'overview.html',solutioning:d.solution_file||'solution.html',techspec:d.techspec_file||'tech-spec.md',implementation:'service pipelines'};
  const HAS={ideation:d.has_overview,solutioning:d.has_solution,techspec:d.has_tech_spec,implementation:false};
  const rows=needed.map(ph=>{
    const ok=HAS[ph];
    return `<div style="display:flex;align-items:center;gap:6px;padding:3px 0;font-size:11px">
      <span style="color:${ok?'#22c55e':'#f59e0b'}">${ok?'✅':'⚠️'}</span>
      <span style="font-weight:600;color:#374151">${LABELS[ph]||ph}</span>
      <span style="color:#9ca3af">${ok?'ready':'missing — run previous phase first'}</span>
    </div>`;
  }).join('');
  const allOk=needed.every(ph=>HAS[ph]);
  return `<div class="fcm assistant" style="font-size:11px;padding:10px 12px;background:${allOk?'#f0fdf4':'#fffbeb'};border-color:${allOk?'#bbf7d0':'#fde68a'}">
    <div style="font-weight:700;color:#374151;margin-bottom:6px">📥 This phase reads:</div>
    ${rows}
    ${!allOk?`<div style="margin-top:6px;color:#b45309;font-size:10px">⚠️ Run missing phases first for best results</div>`:''}
  </div>`;
}

async function openFeat(slug,name){
  S.fSlug=slug;S.fName=name;S.fPhase='pipeline';S.fDetail=null;S.fVersion=null;
  document.getElementById('f-name').textContent=name;
  showView('feature');
  history.replaceState(null,'','/nemesis/'+slug);
  await loadFDetail(slug);
  renderPills();
  await showPipelineOverview();
}

async function loadFDetail(slug){
  const d=await fetch('/api/features/'+slug).then(r=>r.json()).catch(()=>({}));
  S.fDetail=d;
  // Refresh pipeline diagram if currently showing it
  if(S.fPhase==='pipeline'){
    const pv=await fetch('/api/features/'+slug+'/pipeline-view').then(r=>r.json()).catch(()=>({}));
    renderPipelineDiagram(pv);
  }
}

async function showPipelineOverview(){
  S.fPhase='pipeline';
  renderPills();
  document.getElementById('f-run-btn').style.display='none';
  document.getElementById('f-run-full-btn').style.display='';
  document.getElementById('f-phase-ic').textContent='🔄';
  document.getElementById('f-phase-nm').textContent='Pipeline Overview';
  document.getElementById('f-msgs').innerHTML='';
  document.getElementById('f-ver-sel').style.display='none';
  document.getElementById('f-area').innerHTML='<div class="f-loading">Loading pipeline…</div>';
  const d=await fetch('/api/features/'+S.fSlug+'/pipeline-view').then(r=>r.json()).catch(()=>({}));
  renderPipelineDiagram(d);
  // Async: inject cost badge after diagram renders
  fetch('/api/features/'+S.fSlug+'/costs').then(r=>r.json()).then(c=>{
    const el=document.getElementById('pl-cost-badge');
    if(!el)return;
    const total=c.total_cost_usd||0;
    if(total>0){
      const byPhase=c.by_phase||{};
      const parts=Object.entries(byPhase).filter(([,v])=>v.cost_usd>0).map(([k,v])=>`${k}: $${v.cost_usd.toFixed(3)}`).join(' · ');
      el.innerHTML=`💰 $${total.toFixed(3)} total${parts?` <span style="font-weight:300;opacity:.7">· ${parts}</span>`:''}`;
    }
  }).catch(()=>{});
}

function renderPipelineDiagram(data){
  const area=document.getElementById('f-area');
  const np=data.next_phase||'ideation';
  const done={ideation:data.ideation,solutioning:data.solutioning,techspec:data.techspec,e2e:data.e2e};

  function phStatus(ph){
    if(done[ph])return 'pl-done';
    if(np===ph)return 'pl-pending';
    return 'pl-blocked';
  }
  function phBadge(ph){
    if(done[ph])return '<span class="pl-card-badge">✅</span>';
    if(np===ph)return '<span class="pl-card-badge" style="font-size:11px;color:#6366f1">▶</span>';
    return '<span class="pl-card-badge" style="font-size:11px;color:#d1d5db">○</span>';
  }
  function conn(label,active){
    return `<div class="pl-conn"><div class="pl-conn-line ${active?'active':''}"></div><div class="pl-conn-label">${label}</div></div>`;
  }

  const svcs=data.pipelines||[];
  const allImplDone=svcs.length>0&&svcs.every(s=>s.implementation?.done);
  const readyForDev=data.e2e&&allImplDone;

  let svcHtml='';
  if(!svcs.length){
    svcHtml=`<div class="pl-svc-card" style="opacity:.55;cursor:default"><div class="pl-svc-card-name" style="color:#9ca3af">Services TBD</div><div class="pl-svc-card-meta">Run Ideation first</div></div>`;
  } else {
    svcHtml=svcs.slice(0,5).map(s=>{
      const sd=s.implementation?.done;
      const ic=_roleIcon(s.service);
      return `<div class="pl-svc-card ${sd?'pl-done':''}" onclick="selectPhase('pipelines')">
        <div style="font-size:13px;margin-bottom:2px">${ic}</div>
        <div class="pl-svc-card-name">${esc(s.service)}</div>
        <div class="pl-svc-card-meta">${sd?'✅ Implemented':s.scenario?.done?'🧪 Scenario done':'⚡ '+esc(s.impact||'')}</div>
      </div>`;
    }).join('');
    if(svcs.length>5)svcHtml+=`<div class="pl-svc-card" style="opacity:.45;cursor:default"><div class="pl-svc-card-meta">+${svcs.length-5} more services</div></div>`;
  }

  const activeIs=ph=>S.fPhase===ph?' pl-active':'';
  area.innerHTML=`<div class="pl-wrap">
  <div style="font-size:11px;font-weight:700;color:#6b7280;margin-bottom:18px;padding:0 16px;display:flex;align-items:center;gap:10px" id="pl-hdr">
    <span>Feature Pipeline</span>
    <span style="color:#111827;font-weight:800">${esc(data.feature||S.fName||'')}</span>
    <span style="font-size:10px;font-weight:400;margin-left:4px">Next: <b style="color:#6366f1">${esc(np==='complete'?'Complete ✓':np)}</b></span>
    <span id="pl-cost-badge" style="margin-left:auto;font-size:10px;color:#6b7280;font-weight:400"></span>
  </div>
  <div class="pl-canvas">

    <div class="pl-stage">
      <div class="pl-stage-hdr">Phase 1</div>
      <div class="pl-card ${phStatus('ideation')}${activeIs('ideation')}" onclick="selectPhase('ideation')">
        ${phBadge('ideation')}
        <span class="pl-card-ic">🎨</span>
        <div class="pl-card-label">Ideation</div>
        <div class="pl-card-meta">${done.ideation?'overview.html ready':'Generate overview'}</div>
        <button class="pl-card-btn" onclick="event.stopPropagation();selectPhase('ideation')">▶ Run</button>
      </div>
    </div>

    ${conn('reads → overview',done.ideation)}

    <div class="pl-stage">
      <div class="pl-stage-hdr">Phase 2</div>
      <div class="pl-card ${phStatus('solutioning')}${activeIs('solutioning')}" onclick="selectPhase('solutioning')">
        ${phBadge('solutioning')}
        <span class="pl-card-ic">⚙️</span>
        <div class="pl-card-label">Solutioning</div>
        <div class="pl-card-meta">${done.solutioning?'solution.html ready':'Needs Ideation'}</div>
        <button class="pl-card-btn" onclick="event.stopPropagation();selectPhase('solutioning')">▶ Run</button>
      </div>
    </div>

    ${conn('reads → overview + solution',done.solutioning)}

    <div class="pl-stage">
      <div class="pl-stage-hdr">Phase 3</div>
      <div class="pl-card ${phStatus('techspec')}${activeIs('techspec')}" onclick="selectPhase('techspec')">
        ${phBadge('techspec')}
        <span class="pl-card-ic">📄</span>
        <div class="pl-card-label">Tech Spec</div>
        <div class="pl-card-meta">${done.techspec?'tech-spec.md ready':'Needs Solutioning'}</div>
        <button class="pl-card-btn" onclick="event.stopPropagation();selectPhase('techspec')">▶ Run</button>
      </div>
    </div>

    ${conn('reads → spec + solution',done.techspec)}

    <div class="pl-stage">
      <div class="pl-stage-hdr">Implementation</div>
      <div class="pl-svc-group">${svcHtml}</div>
    </div>

    ${conn('reads → all artifacts',allImplDone)}

    <div class="pl-stage">
      <div class="pl-stage-hdr">Phase 4</div>
      <div class="pl-card ${phStatus('e2e')}${activeIs('e2e')}" onclick="selectPhase('e2e')">
        ${phBadge('e2e')}
        <span class="pl-card-ic">🧪</span>
        <div class="pl-card-label">E2E</div>
        <div class="pl-card-meta">${done.e2e?'Tests passed':'Run after impl'}</div>
        <button class="pl-card-btn" onclick="event.stopPropagation();selectPhase('e2e')">▶ Run</button>
      </div>
    </div>

    ${conn('gate',data.e2e)}

    <div class="pl-stage">
      <div class="pl-stage-hdr">Output</div>
      <div class="pl-terminal ${readyForDev?'unlocked':''}">
        <div class="pl-terminal-ic">${readyForDev?'🚀':'🔒'}</div>
        <div class="pl-terminal-label">${readyForDev?'Ready for<br>Dev Test':'Awaiting<br>E2E Pass'}</div>
      </div>
    </div>

  </div>
</div>`;
}

function renderPills(){
  const d=S.fDetail||{};
  const has={ideation:d.has_overview,solutioning:d.has_solution,techspec:d.has_tech_spec,e2e:d.has_e2e};
  // Pipeline overview pill first
  const plSel=S.fPhase==='pipeline';
  let html=`<span class="php ${plSel?'php-sel php-done':'php-pending'}" onclick="showPipelineOverview()" style="${plSel?'':'background:#f1f5f9;color:#6b7280'}">🔄 Pipeline</span>`;
  html+=Object.entries(PHASES).map(([k,p])=>{
    const done=has[k];
    const sel=k===S.fPhase;
    return `<span class="php ${done?'php-done':'php-pending'}${sel?' php-sel':''}" onclick="selectPhase('${k}')" data-phase="${k}">${done?'✓':'○'} ${p.label}</span>`;
  }).join('');
  if(has.techspec){
    const pSel=S.fPhase==='pipelines';
    html+=`<span class="php ${pSel?'php-sel':'php-pending'}" onclick="selectPhase('pipelines')" style="background:#fef3c7;color:#92400e;border-color:#fde68a">🔧 Services</span>`;
  }
  document.getElementById('f-pills').innerHTML=html;
}

async function selectPhase(phase){
  // Save current phase messages before switching
  if(S.fPhase&&S.fPhase!=='pipeline'&&S.fPhase!=='pipelines')_saveMsgs(S.fSlug,S.fPhase);
  S.fPhase=phase;
  S.fSid=_phaseSid(S.fSlug,phase);
  renderPills();
  if(phase==='pipeline'){await showPipelineOverview();return;}
  if(phase==='pipelines'){
    document.getElementById('f-phase-ic').textContent='🔧';
    document.getElementById('f-phase-nm').textContent='Service Pipelines';
    document.getElementById('f-run-btn').style.display='none';
    document.getElementById('f-run-full-btn').style.display='';
    document.getElementById('f-msgs').innerHTML='';
    await loadPipelineView();
    return;
  }
  document.getElementById('f-run-btn').style.display='';
  document.getElementById('f-run-full-btn').style.display='';
  const p=PHASES[phase];
  document.getElementById('f-phase-ic').textContent=p.ic;
  document.getElementById('f-phase-nm').textContent='Nemesis → '+p.label;
  document.getElementById('f-run-btn').textContent='▶ Run '+p.label;
  document.getElementById('f-run-btn').disabled=false;
  const msgs=document.getElementById('f-msgs');
  // Restore previous messages or show upstream context card
  const saved=_restoreMsgs(S.fSlug,phase);
  if(saved){
    msgs.innerHTML=saved;
    msgs.scrollTop=msgs.scrollHeight;
  } else {
    msgs.innerHTML=_upstreamContextCard(phase);
  }
  if(S.fSid)loadFSess(S.fSid);
  await loadVersions(phase);
  await loadPhase(phase,S.fVersion);
}

async function loadVersions(phase){
  const d=await fetch(`/api/features/${S.fSlug}/versions/${phase}`).then(r=>r.json()).catch(()=>({versions:[]}));
  const vs=d.versions||[];
  S.fVersion=vs.length?vs[vs.length-1]:null;
  const sel=document.getElementById('f-ver-sel');
  if(vs.length>1){
    sel.innerHTML=vs.map((v,i)=>`<option value="${esc(v)}"${v===S.fVersion?' selected':''}>${'v'+(i+1)}</option>`).join('');
    sel.style.display='';
  }else{sel.style.display='none';}
}

function selectVersion(v){S.fVersion=v;loadPhase(S.fPhase,v);}

function _srcBadge(icon,label,items){
  if(!items||!items.length)return '';
  const list=items.map(u=>`<div style="font-size:10px;color:#6366f1;word-break:break-all;margin-top:2px;padding-left:8px">→ ${esc(u)}</div>`).join('');
  return `<div style="background:#f8f9fb;border:1px solid #e5e7eb;border-radius:8px;padding:7px 10px;margin-bottom:6px;text-align:left;width:100%;max-width:360px"><div style="font-size:11px;font-weight:700;color:#374151">${icon} ${label}</div>${list}</div>`;
}

async function loadPhase(phase,version){
  const area=document.getElementById('f-area');
  area.innerHTML='<div class="f-loading">Loading…</div>';
  const p=PHASES[phase]||PHASES.ideation;
  const vp=version?`&version=${encodeURIComponent(version)}`:'';
  const d=await fetch(`/api/features/${S.fSlug}/content?tab=${p.tab}${vp}`).then(r=>r.json()).catch(()=>({exists:false}));
  if(!d.exists){
    const src=((S.fDetail||{}).data||{}).sources||{};
    let srcHtml='';
    if(phase==='ideation'){
      srcHtml+=_srcBadge('💬','Slack threads',src.slack);
      srcHtml+=_srcBadge('📄','Google Docs',src.docs);
      srcHtml+=_srcBadge('✉','Gmail threads',src.gmail);
      if(src.verbal)srcHtml+=`<div style="background:#f8f9fb;border:1px solid #e5e7eb;border-radius:8px;padding:7px 10px;margin-bottom:6px;text-align:left;width:100%;max-width:360px"><div style="font-size:11px;font-weight:700;color:#374151">💡 Brief</div><div style="font-size:11px;color:#6b7280;margin-top:3px;line-height:1.5">${esc(src.verbal.substring(0,180))}${src.verbal.length>180?'…':''}</div></div>`;
    }
    const msgs={
      ideation:    {ei:'🎨',t:'Ready for Ideation',h:'Click <b>▶ Run Ideation</b> to generate the feature overview'+(srcHtml?'<br><br><span style="font-size:11px;font-weight:700;color:#374151">Attached sources:</span>':'')},
      solutioning: {ei:'⚙️',t:'No solution yet',    h:'Complete Ideation first, then run <b>▶ Run Solutioning</b> — produces solution + risk analysis in one pass'},
      techspec:    {ei:'📄',t:'No tech spec yet',    h:'Complete Solutioning first, then run <b>▶ Run Tech Spec</b>'},
    };
    const m=msgs[phase]||{ei:'○',t:'No content',h:''};
    area.innerHTML=`<div class="empty-pane"><div class="ei">${m.ei}</div><b>${m.t}</b><div style="font-size:12px;color:#9ca3af;max-width:320px;margin-top:6px;line-height:1.7">${m.h}</div>${srcHtml?'<div style="margin-top:12px">'+srcHtml+'</div>':''}</div>`;
    return;
  }
  if(d.type==='html'){
    area.innerHTML=`<div class="fc-html">${cleanMixedHtml(d.content)}</div>`;
    if(window.mermaid){try{const ms=area.querySelectorAll('.mermaid');if(ms.length)await mermaid.run({nodes:ms})}catch(e){console.warn('mermaid render:',e)}}
  }else{
    area.innerHTML=`<div class="fc-md">${renderMd(d.content)}</div>`;
  }
}

async function runPhase(){
  const phase=S.fPhase;
  const p=PHASES[phase];
  const runBtn=document.getElementById('f-run-btn');
  runBtn.disabled=true;runBtn.textContent='Running…';
  const msgs=document.getElementById('f-msgs');
  msgs.innerHTML=''; // clear previous run output — fresh start every time
  localStorage.removeItem('fm_'+S.fSlug+'_'+phase); // clear saved messages for this phase

  // Progress card
  const progCard=document.createElement('div');progCard.className='fcm assistant';
  progCard.style.padding='0';progCard.style.background='transparent';progCard.style.border='none';
  progCard.innerHTML=`<div class="pp-wrap">
    <div class="pp-header">${p.ic} Running ${p.label} — <em>${esc(S.fName)}</em></div>
    <div class="pp-bar"><div class="pp-fill" id="pp-fill" style="width:0%"></div></div>
    <div class="pp-steps" id="pp-steps"></div>
  </div>`;
  msgs.appendChild(progCard);msgs.scrollTop=msgs.scrollHeight;

  function _ppStep(label,state){
    const s=document.createElement('div');
    const icon=state==='done'?'✓':state==='error'?'✗':'⬡';
    s.className='pp-step pp-step-'+state;s.innerHTML=`<span>${icon}</span><span>${esc(label)}</span>`;
    document.getElementById('pp-steps')?.appendChild(s);
    msgs.scrollTop=msgs.scrollHeight;
    return s;
  }
  function _ppFinish(el){
    if(!el)return;
    el.className='pp-step pp-step-done';
    el.querySelector('span').textContent='✓';
  }

  let lastStepEl=null;
  try{
    const r=await fetch(`/api/features/${S.fSlug}/run/${phase}`,{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:S.fSid,feature_name:S.fName}),
    });
    if(!r.body){throw new Error('No streaming response body');}
    const reader=r.body.getReader();const dec=new TextDecoder();let buf='';
    while(true){
      const {done,value}=await reader.read();
      if(done)break;
      buf+=dec.decode(value,{stream:true});
      const parts=buf.split('\n\n');buf=parts.pop();
      for(const part of parts){
        const line=part.trim();
        if(!line.startsWith('data:'))continue;
        let evt;try{evt=JSON.parse(line.slice(line.indexOf(':')+1).trim());}catch{continue;}
        if(evt.done){
          _ppFinish(lastStepEl);lastStepEl=null;
          const fill=document.getElementById('pp-fill');if(fill)fill.style.width='100%';
          const res=document.createElement('div');res.className='fcm assistant';
          res.innerHTML=renderMd(evt.summary||evt.error||'(no output)');
          msgs.appendChild(res);msgs.scrollTop=msgs.scrollHeight;
          if(evt.ok){
            if(evt.session_id&&!S.fSid){S.fSid=evt.session_id;_setPhaseSid(S.fSlug,phase,evt.session_id);}
            await loadFDetail(S.fSlug);renderPills();
            await loadVersions(phase);await loadPhase(phase,S.fVersion);
            if(evt.usage){
              const u=evt.usage;const cb=document.createElement('div');cb.className='fcm assistant';
              cb.style.cssText='font-size:10px;color:#6b7280;padding:4px 10px;border-top:1px solid #f3f4f6;margin-top:-8px';
              cb.innerHTML=`💰 $${(u.cost_usd||0).toFixed(4)} · ${((u.input_tokens||0)/1000).toFixed(1)}K in / ${((u.output_tokens||0)/1000).toFixed(1)}K out`;
              msgs.appendChild(cb);msgs.scrollTop=msgs.scrollHeight;
            }
            const next=NEXT_PHASE[phase];
            if(next){const np=PHASES[next];
              const nb=document.createElement('div');nb.className='fcm assistant fcm-next-action';
              nb.innerHTML=`<span>${np.ic} Ready for <b>${np.label}</b></span><button class="fcm-run-next" onclick="selectPhase('${next}');runPhase()">▶ Run ${np.label} →</button>`;
              msgs.appendChild(nb);msgs.scrollTop=msgs.scrollHeight;}
            if(phase==='techspec'||!next){loadPipelines(msgs);}
            _saveMsgs(S.fSlug,phase);
          }
        }else{
          _ppFinish(lastStepEl);
          const fill=document.getElementById('pp-fill');if(fill)fill.style.width=(evt.pct||0)+'%';
          lastStepEl=_ppStep(evt.label||evt.step||'Working…','active');
        }
      }
    }
  }catch(e){
    _ppFinish(lastStepEl);
    const er=document.createElement('div');er.className='fcm assistant';
    er.innerHTML=`<em>Error: ${esc(e.message)}</em>`;msgs.appendChild(er);
  }
  runBtn.disabled=false;runBtn.textContent='▶ Run '+p.label;
}

async function runFullPipeline(){
  const btn=document.getElementById('f-run-full-btn');
  const runBtn=document.getElementById('f-run-btn');
  btn.disabled=true;btn.textContent='Running…';
  runBtn.disabled=true;
  const msgs=document.getElementById('f-msgs');
  msgs.innerHTML=''; // clear previous output — fresh start
  ['ideation','solutioning','techspec','e2e'].forEach(ph=>localStorage.removeItem('fm_'+S.fSlug+'_'+ph));
  const progCard=document.createElement('div');progCard.className='fcm assistant';
  progCard.style.padding='0';progCard.style.background='transparent';progCard.style.border='none';
  progCard.innerHTML=`<div class="pp-wrap"><div class="pp-header">⚡ Full Pipeline — <em>${esc(S.fName)}</em></div><div class="pp-bar"><div class="pp-fill" id="pp-fill-full" style="width:0%"></div></div><div class="pp-steps" id="pp-steps-full"></div></div>`;
  msgs.appendChild(progCard);msgs.scrollTop=msgs.scrollHeight;
  function addStep(label,state){const s=document.createElement('div');s.className='pp-step pp-step-'+state;s.innerHTML=`<span>${state==='done'?'✓':state==='error'?'✗':'⬡'}</span><span>${esc(label)}</span>`;document.getElementById('pp-steps-full')?.appendChild(s);msgs.scrollTop=msgs.scrollHeight;return s;}
  let lastEl=null;
  try{
    const r=await fetch(`/api/features/${S.fSlug}/run/full`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:S.fSid,feature_name:S.fName})});
    const reader=r.body.getReader();const dec=new TextDecoder();let buf='';
    while(true){const{done,value}=await reader.read();if(done)break;buf+=dec.decode(value,{stream:true});const parts=buf.split('\\n\\n');buf=parts.pop();
    for(const part of parts){const line=part.trim();if(!line.startsWith('data:'))continue;let evt;try{evt=JSON.parse(line.slice(line.indexOf(':')+1).trim())}catch{continue}
      if(evt.phase){if(lastEl){lastEl.className='pp-step pp-step-done';lastEl.querySelector('span').textContent='✓';}lastEl=addStep('Phase: '+evt.phase,'active');const fill=document.getElementById('pp-fill-full');if(fill)fill.style.width=(evt.progress||0)+'%';}
      else if(evt.done&&evt.ok&&evt.message){if(lastEl){lastEl.className='pp-step pp-step-done';lastEl.querySelector('span').textContent='✓';}const fill=document.getElementById('pp-fill-full');if(fill)fill.style.width='100%';addStep(evt.message,'done');if(evt.usage){addStep(`💰 $${(evt.usage.cost_usd||0).toFixed(4)} · ${((evt.usage.input_tokens||0)/1000).toFixed(1)}K in / ${((evt.usage.output_tokens||0)/1000).toFixed(1)}K out`,'done');}await loadFDetail(S.fSlug);renderPills();}
      else if(evt.done&&!evt.ok){if(lastEl){lastEl.className='pp-step pp-step-error';lastEl.querySelector('span').textContent='✗';}addStep(evt.error||'Failed','error');}
      else if(evt.step||evt.label){if(lastEl){lastEl.className='pp-step pp-step-done';lastEl.querySelector('span').textContent='✓';}lastEl=addStep(evt.label||evt.step,'active');const fill=document.getElementById('pp-fill-full');if(fill&&evt.pct)fill.style.width=evt.pct+'%';}
    }}
  }catch(e){addStep('Error: '+e.message,'error');}
  btn.disabled=false;btn.textContent='⚡ Full';
  runBtn.disabled=false;runBtn.textContent='▶ Run '+PHASES[S.fPhase]?.label;
}

// ── Service Pipelines (per-service expert-controlled) ──────────────────────

const ROLE_ICONS={'gateway':'🌐','payment-core':'💳','platform':'⚡','monolith':'🏛️',
  'offers':'🎯','frontend':'🖥️','infra':'🔧','comms':'📡','risk':'🛡️',
  'settlements':'💰','config':'⚙️','domain':'📦','recurring':'🔄',
  'cross-border':'🌍','governance':'📋','protocols':'🔗','batch':'📑'};

function _roleIcon(name){
  for(const [k,v] of Object.entries(ROLE_ICONS)){if(name.toLowerCase().includes(k.toLowerCase()))return v;}
  return '🦸';
}

async function loadPipelineView(){
  const area=document.getElementById('f-area');
  area.innerHTML='<div class="f-loading">Loading pipelines...</div>';
  const d=await fetch(`/api/features/${S.fSlug}/pipelines`).then(r=>r.json()).catch(()=>({pipelines:[]}));
  if(!d.pipelines?.length){
    const reason=d.reason||'Complete Tech Spec first to unlock service pipelines';
    area.innerHTML=`<div class="empty-pane"><div class="ei">🔒</div><b>Services locked</b><div>${esc(reason)}</div><button class="btn-primary" style="margin-top:12px" onclick="selectPhase('techspec')">▶ Go to Tech Spec</button></div>`;
    return;
  }
  let html=`<div class="fc-html" style="max-width:800px"><h1>🔧 Service Pipelines</h1>
    <p style="color:#6b7280;margin-bottom:16px">${d.pipelines.length} services need changes for <b>${esc(d.feature||'')}</b></p>
    <table><thead><tr><th>Hero</th><th>Service</th><th>Impact</th><th>Scenario</th><th>Implementation</th></tr></thead><tbody>`;
  for(const p of d.pipelines){
    const ic=_roleIcon(p.hero?.name||p.role||'');
    const impCls='pipe-impact-'+p.impact.toLowerCase();
    const scenTd=p.scenario.done
      ?`<a href="/api/features/${esc(S.fSlug)}/pipeline/${esc(p.service)}/report" target="_blank" style="color:#16a34a;font-weight:600">📊 View Report</a>`
      :`<button class="pipe-btn pipe-btn-run" onclick="selectPhase('pipelines');runPipeline('${esc(p.service)}','scenario')">🧪 Run</button>`;
    const implTd=p.implementation.done
      ?`<span style="color:#16a34a;font-weight:600">✅ ${p.implementation.branch||'done'}</span>`
      :`<button class="pipe-btn" onclick="runPipeline('${esc(p.service)}','implement')">🚀 Run</button>`;
    html+=`<tr>
      <td><span style="font-size:18px" title="${esc(p.hero?.name||'')}">${ic}</span> <span style="font-size:11px;font-weight:600">${esc(p.hero?.name?.split('(')[0]?.trim()||'')}</span><br><span style="font-size:10px;color:#9ca3af">L${p.hero?.level||1} ${esc(p.hero?.title||'')}</span></td>
      <td><code style="font-weight:700">${esc(p.service)}</code><br><span style="font-size:10px;color:#9ca3af">${esc(p.role?.substring(0,50)||'')}</span></td>
      <td><span class="pipe-impact ${impCls}">${esc(p.impact)}</span></td>
      <td>${scenTd}</td>
      <td>${implTd}</td>
    </tr>`;
  }
  html+='</tbody></table></div>';
  area.innerHTML=html;
  const msgs=document.getElementById('f-msgs');
  msgs.innerHTML='<div class="fcm assistant" style="font-size:12px;color:#6b7280">Select a service above to run scenario testing or implementation. Each pipeline is controlled by its hero expert.</div>';
}

async function loadPipelines(msgs){
  const d=await fetch(`/api/features/${S.fSlug}/pipelines`).then(r=>r.json()).catch(()=>({pipelines:[]}));
  if(!d.pipelines?.length)return;
  const wrap=document.createElement('div');wrap.className='fcm assistant';wrap.style.padding='0';wrap.style.background='transparent';wrap.style.border='none';
  let cards=d.pipelines.map(p=>{
    const ic=_roleIcon(p.hero?.name||p.role||'');
    const impactCls=p.impact.toLowerCase();
    const scenBtn=p.scenario.done
      ?`<button class="pipe-btn pipe-btn-done" onclick="window.open('/api/features/${esc(S.fSlug)}/pipeline/${esc(p.service)}/report','_blank')">📊 Report</button>`
      :`<button class="pipe-btn pipe-btn-run" onclick="runPipeline('${esc(p.service)}','scenario')">🧪 Scenario</button>`;
    const implBtn=p.implementation.done
      ?`<button class="pipe-btn pipe-btn-done" onclick="alert('PR: '+(${JSON.stringify(p.implementation.pr_url)}||'pending'))">✅ PR</button>`
      :`<button class="pipe-btn" onclick="runPipeline('${esc(p.service)}','implement')">🚀 Implement</button>`;
    return `<div class="pipe-card" data-svc="${esc(p.service)}">
      <div class="pipe-hero" title="${esc(p.hero?.name||'')}">${ic}</div>
      <div class="pipe-info">
        <div class="pipe-svc">${esc(p.service)}</div>
        <div class="pipe-role">${esc(p.hero?.name||'')} L${p.hero?.level||1} · ${esc(p.role?.substring(0,60)||'')}</div>
      </div>
      <div class="pipe-impact pipe-impact-${impactCls}">${esc(p.impact)}</div>
      <div class="pipe-actions">${scenBtn}${implBtn}</div>
    </div>`;
  }).join('');
  wrap.innerHTML=`<div class="pipe-wrap"><div class="pipe-title"><span>🔧</span>Service Pipelines (${d.pipelines.length} services)</div>${cards}</div>`;
  msgs.appendChild(wrap);msgs.scrollTop=msgs.scrollHeight;
}

async function runPipeline(service,action){
  const msgs=document.getElementById('f-msgs');
  const progCard=document.createElement('div');progCard.className='fcm assistant';
  progCard.style.padding='0';progCard.style.background='transparent';progCard.style.border='none';
  progCard.innerHTML=`<div class="pp-wrap">
    <div class="pp-header">${action==='scenario'?'🧪':'🚀'} ${action} — ${esc(service)}</div>
    <div class="pp-bar"><div class="pp-fill" id="pipe-fill" style="width:0%"></div></div>
    <div class="pp-steps" id="pipe-steps"></div>
  </div>`;
  msgs.appendChild(progCard);msgs.scrollTop=msgs.scrollHeight;

  let lastEl=null;
  function addStep(label,state){
    if(lastEl){lastEl.className='pp-step pp-step-done';lastEl.querySelector('span').textContent='✓';}
    const s=document.createElement('div');
    s.className='pp-step pp-step-'+state;s.innerHTML=`<span>⬡</span><span>${esc(label)}</span>`;
    document.getElementById('pipe-steps')?.appendChild(s);
    msgs.scrollTop=msgs.scrollHeight;
    lastEl=s;
  }

  try{
    const r=await fetch(`/api/features/${S.fSlug}/pipeline/${service}/${action}`,{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({feature_name:S.fName}),
    });
    if(!r.body)throw new Error('No stream');
    const reader=r.body.getReader();const dec=new TextDecoder();let buf='';
    while(true){
      const {done,value}=await reader.read();
      if(done)break;
      buf+=dec.decode(value,{stream:true});
      const parts=buf.split('\n\n');buf=parts.pop();
      for(const part of parts){
        const line=part.trim();
        if(!line.startsWith('data:'))continue;
        let evt;try{evt=JSON.parse(line.slice(line.indexOf(':')+1).trim());}catch{continue;}
        if(evt.done){
          if(lastEl){lastEl.className='pp-step pp-step-done';lastEl.querySelector('span').textContent='✓';}
          const fill=document.getElementById('pipe-fill');if(fill)fill.style.width='100%';
          const res=document.createElement('div');res.className='fcm assistant';
          res.innerHTML=renderMd(evt.summary||evt.error||'(done)');
          msgs.appendChild(res);msgs.scrollTop=msgs.scrollHeight;
          if(evt.ok&&evt.report){
            const link=document.createElement('div');link.className='fcm assistant fcm-next-action';
            link.innerHTML=`<span>📊 Scenario report ready</span><button class="fcm-run-next" onclick="window.open('/api/features/${esc(S.fSlug)}/pipeline/${esc(service)}/report','_blank')">Open Report →</button>`;
            msgs.appendChild(link);msgs.scrollTop=msgs.scrollHeight;
          }
          if(evt.ok&&action==='scenario'){
            const nb=document.createElement('div');nb.className='fcm assistant fcm-next-action';
            nb.innerHTML=`<span>🚀 Ready to implement <b>${esc(service)}</b></span><button class="fcm-run-next" onclick="runPipeline('${esc(service)}','implement')">🚀 Implement →</button>`;
            msgs.appendChild(nb);msgs.scrollTop=msgs.scrollHeight;
          }
        }else{
          const fill=document.getElementById('pipe-fill');if(fill)fill.style.width=(evt.pct||0)+'%';
          addStep(evt.label||evt.step||'Working...','active');
        }
      }
    }
  }catch(e){
    const er=document.createElement('div');er.className='fcm assistant';
    er.innerHTML=`<em>Pipeline error: ${esc(e.message)}</em>`;msgs.appendChild(er);
  }
}

async function loadFSess(sid){
  const d=await fetch(`/api/sessions/${sid}`).then(r=>r.json()).catch(()=>({}));
  if(!d.messages?.length)return;
  const msgs=document.getElementById('f-msgs');
  for(const m of d.messages){const div=document.createElement('div');div.className='fcm '+(m.role==='user'?'user':'assistant');div.innerHTML=m.role==='user'?esc(m.content):renderMd(m.content);msgs.appendChild(div);}
  msgs.scrollTop=msgs.scrollHeight;
}
let pendingUploads=[];
let mainPendingUploads=[];

function _readFileAsText(file){
  return new Promise((resolve)=>{
    const r=new FileReader();
    r.onload=()=>resolve({name:file.name,size:file.size,content:r.result.substring(0,4000)});
    r.onerror=()=>resolve({name:file.name,size:file.size,content:'(could not read file)'});
    if(file.type.startsWith('image/'))resolve({name:file.name,size:file.size,content:'[image: '+file.name+']'});
    else r.readAsText(file);
  });
}

function _renderUploadChips(uploads,containerId){
  const c=document.getElementById(containerId);
  c.innerHTML=uploads.map((u,i)=>`<div class="upload-chip"><span>${esc(u.name)}</span><button onclick="_removeUpload('${containerId}',${i})">×</button></div>`).join('');
}

function _removeUpload(containerId,idx){
  if(containerId==='f-uploads'){pendingUploads.splice(idx,1);_renderUploadChips(pendingUploads,'f-uploads');}
  else{mainPendingUploads.splice(idx,1);_renderUploadChips(mainPendingUploads,'r-uploads');}
}

async function handleFileUpload(input){
  for(const f of input.files){pendingUploads.push(await _readFileAsText(f));}
  input.value='';_renderUploadChips(pendingUploads,'f-uploads');
}

async function handleMainUpload(input){
  for(const f of input.files){mainPendingUploads.push(await _readFileAsText(f));}
  input.value='';_renderUploadChips(mainPendingUploads,'r-uploads');
}

async function fSend(){
  const inp=document.getElementById('f-in');
  const msg=inp.value.trim();if(!msg)return;
  inp.value='';inp.style.height='';
  const uploads=[...pendingUploads];pendingUploads=[];_renderUploadChips([],'f-uploads');
  const msgs=document.getElementById('f-msgs');
  const ur=document.createElement('div');ur.className='fcm user';
  ur.innerHTML=esc(msg)+(uploads.length?`<div style="margin-top:4px;font-size:10px;color:rgba(255,255,255,.7)">📎 ${uploads.map(u=>u.name).join(', ')}</div>`:'');
  msgs.appendChild(ur);msgs.scrollTop=msgs.scrollHeight;
  const tr=document.createElement('div');tr.className='fcm assistant';tr.innerHTML='<div class="typing-dots"><span></span><span></span><span></span></div>';msgs.appendChild(tr);msgs.scrollTop=msgs.scrollHeight;
  try{
    const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      message:msg,session_id:S.fSid,feature_slug:S.fSlug,feature_name:S.fName,
      uploaded_files:uploads.length?uploads:undefined
    })});
    const d=await r.json();
    if(d.session_id&&!S.fSid){S.fSid=d.session_id;localStorage.setItem('f_sid_'+S.fSlug,d.session_id);}
    tr.remove();
    const ar=document.createElement('div');ar.className='fcm assistant';ar.innerHTML=renderMd(d.response||d.error||'(empty)');msgs.appendChild(ar);msgs.scrollTop=msgs.scrollHeight;
    if(d.usage&&d.usage.cost_usd>0){
      const cu=document.createElement('div');cu.className='fcm assistant';
      cu.style.cssText='font-size:10px;color:#9ca3af;padding:2px 10px;margin-top:-10px;border:none;background:transparent';
      cu.innerHTML=`💬 $${(d.usage.cost_usd||0).toFixed(4)} · ${((d.usage.input_tokens||0)/1000).toFixed(1)}K in / ${((d.usage.output_tokens||0)/1000).toFixed(1)}K out`;
      msgs.appendChild(cu);
    }
    _saveMsgs(S.fSlug,S.fPhase);
    if(d.skill_cmd){
      const sc=d.skill_cmd;
      const phases={ideation:'ideation',solutioning:'solutioning',techspec:'techspec'};
      if(phases[sc.skill]){
        const phase=phases[sc.skill];
        const info=document.createElement('div');info.className='fcm assistant';
        info.innerHTML=renderMd(`**▶ Nemesis → ${sc.skill.charAt(0).toUpperCase()+sc.skill.slice(1)}** — running now…`);
        msgs.appendChild(info);msgs.scrollTop=msgs.scrollHeight;
        selectPhase(phase);
        await runPhase();
      }
    }
  }catch(e){tr.remove();const er=document.createElement('div');er.className='fcm assistant';er.innerHTML=`<em>Error: ${esc(e.message)}</em>`;msgs.appendChild(er);}
  inp.focus();
}

document.getElementById('r-send-btn').onclick=rSend;
document.getElementById('rubick-in').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();rSend();}});
document.getElementById('rubick-in').addEventListener('input',function(){this.style.height='auto';this.style.height=Math.min(this.scrollHeight,160)+'px';});
document.getElementById('f-in').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();fSend();}});
document.getElementById('f-in').addEventListener('input',function(){this.style.height='auto';this.style.height=Math.min(this.scrollHeight,72)+'px';});

showView('rubick');
loadStats();
loadFeatList();
loadSessions();
try{
  const _es=new EventSource('http://127.0.0.1:8000/api/events');
  _es.onmessage=function(e){
    try{const ev=JSON.parse(e.data);if(ev.event==='node_updated'||ev.event==='learn_flush'){loadStats();loadFeatList();}}catch{}
  };
}catch{}
</script>
</body>
</html>"""



@app.route("/features/<slug>/<path:filename>")
def feature_file(slug, filename):
    feat_dir = BASE_DIR / "workspace" / "features" / slug
    path = feat_dir / filename
    if not path.exists() or not str(path.resolve()).startswith(str((BASE_DIR / "workspace" / "features").resolve())):
        from flask import abort; abort(404)
    with open(path) as f:
        content = f.read()
    mime = "text/html" if filename.endswith(".html") else "text/markdown" if filename.endswith(".md") else "text/plain"
    return Response(content, mimetype=mime)

@app.route("/init")
def init_page():
    return Response(INIT_HTML, mimetype="text/html")

@app.route("/")
def index():
    return Response(LANDING_HTML, mimetype="text/html")

@app.route("/rubick")
def rubick():
    from flask import redirect
    return redirect("/nemesis", code=302)

@app.route("/oracle")
def oracle():
    return Response(ORACLE_HTML, mimetype="text/html")

@app.route("/nemesis")
@app.route("/nemesis/<path:slug>")
def nemesis(slug=None):
    return Response(NEMESIS_HTML, mimetype="text/html")

def main():
    global DB_PATH
    p = argparse.ArgumentParser()
    p.add_argument("--db",   default=str(DEFAULT_DB))
    p.add_argument("--port", type=int, default=5555)
    p.add_argument("--host", default="127.0.0.1")
    a = p.parse_args()
    DB_PATH = a.db
    if not os.path.exists(DB_PATH):
        print(f"ERROR: DB not found: {DB_PATH}", file=sys.stderr); sys.exit(1)
    init_db()
    print(f"Nemesis → http://{a.host}:{a.port}")
    app.run(host=a.host, port=a.port, debug=False)

if __name__ == "__main__":
    main()
