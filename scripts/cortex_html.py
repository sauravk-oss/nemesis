#!/usr/bin/env python3
"""cortex_html.py — Jarvis UI generator for Nexus pipeline.

Generates:
  - nexus.html: 3D interactive graph dashboard (Three.js + Obsidian-style)
  - Per-phase standalone HTMLs (overview.html, solution.html, etc.)
  - nexus-data.json: Data manifest consumed by the dashboard

Commands:
  generate-nexus <feature>           → nexus.html + nexus-data.json
  generate-phase <phase> <feature>   → {phase}.html
  update-data <feature> <phase> <json> → updates nexus-data.json
  persist-chat <feature> <phase> <msg> → saves chat message
  add-variation <feature> <phase>    → creates variation snapshot
  export <feature> <format>          → md/docx/html/zip
  demo                               → generates demo nexus.html with sample data
"""

import argparse
import datetime
import html as html_mod
import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

WORKSPACE = Path("workspace")
FEATURES_DIR = WORKSPACE / "features"
DB_PATH = WORKSPACE / "rubick.db"

PHASES = [
    {"id": "lens",     "name": "Lens",     "role": "Feature Understanding",  "icon": "\U0001f50d", "artifact": "overview.md"},
    {"id": "forge",    "name": "Forge",    "role": "Solution Design",        "icon": "\U0001f528", "artifact": "solution.md"},
    {"id": "sentinel", "name": "Sentinel", "role": "Risk Analysis",          "icon": "\U0001f6e1️", "artifact": "risk-analysis.md"},
    {"id": "scribe",   "name": "Scribe",   "role": "Tech Spec Generation",   "icon": "\U0001f4dc", "artifact": "tech-spec.md"},
    {"id": "welder",   "name": "Welder",   "role": "Implementation",         "icon": "⚡", "artifact": "implementation/"},
    {"id": "launch",   "name": "Launch",   "role": "Run & Deploy",           "icon": "\U0001f680", "artifact": "launch/"},
]

PHASE_EDGES = [
    ("lens", "forge"), ("forge", "sentinel"), ("sentinel", "scribe"),
    ("scribe", "welder"), ("welder", "launch"),
]

PHASE_DESCRIPTIONS = {
    "lens":     "Ingests Slack threads, docs, PRDs. Maps As-Is and To-Be flows. Identifies services, blockers, requirements.",
    "forge":    "Designs exact code changes per service. Traces code paths. Consults project experts. Produces solution.md.",
    "sentinel": "Validates solution against ecosystem. Finds gaps, risks, blast radius. Produces amendments.",
    "scribe":   "Generates 16-section Razorpay Tech Spec. Renders diagrams. Creates Google Doc.",
    "welder":   "Implements code changes. Writes tests. Creates branches. Self-reviews via engineering skills.",
    "launch":   "Runs tests. Verifies behavior. Generates deploy checklist. Go/no-go decision.",
}


# ═══════════════════════════════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════════════════════════════

def _load_feature_data(slug, db_path=None):
    db = db_path or DB_PATH
    if not Path(db).exists():
        return None
    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM nodes WHERE type='Feature' AND name=? LIMIT 1", (slug,)
        ).fetchone()
        conn.close()
        if row:
            data = json.loads(row["data"]) if row["data"] else {}
            return {
                "slug": slug,
                "name": data.get("display_name", slug.replace("-", " ").title()),
                "status": data.get("status", "proposed"),
                "phase": data.get("phase", "lens"),
                "created_at": row["created_at"] if "created_at" in row.keys() else "",
                "owner": data.get("owner", ""),
                "data": data,
            }
    except Exception:
        pass
    return None


def _load_experts(slug, db_path=None):
    db = db_path or DB_PATH
    if not Path(db).exists():
        return []
    experts = []
    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM nodes WHERE type='ProjectExpert' ORDER BY CAST(json_extract(data, '$.xp') AS INTEGER) DESC LIMIT 20"
        ).fetchall()
        for row in rows:
            data = json.loads(row["data"]) if row["data"] else {}
            experts.append({
                "codename": data.get("role", row["name"]),
                "domain": data.get("domain", ""),
                "level": data.get("level", 1),
                "xp": data.get("xp", 0),
                "projects": data.get("projects", []),
                "phases": [],
            })
        conn.close()
    except Exception:
        pass
    return experts


def _load_cortex_stats(db_path=None):
    db = db_path or DB_PATH
    if not Path(db).exists():
        return _default_stats()
    try:
        conn = sqlite3.connect(str(db))
        total_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        total_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        type_counts = {}
        for row in conn.execute("SELECT type, COUNT(*) as c FROM nodes GROUP BY type"):
            type_counts[row[0]] = row[1]
        conn.close()
        return {
            "total_nodes": total_nodes, "total_edges": total_edges,
            "functions": type_counts.get("Function", 0),
            "tests": type_counts.get("Test", 0),
            "endpoints": type_counts.get("Endpoint", 0),
            "experts_total": type_counts.get("ProjectExpert", 0),
            "experts_involved": 0,
            "decisions": type_counts.get("ArchDecision", 0),
            "risks": type_counts.get("RiskItem", 0),
            "classes": type_counts.get("Class", 0),
            "modules": type_counts.get("Module", 0),
            "datastores": type_counts.get("DataStore", 0),
        }
    except Exception:
        return _default_stats()


def _default_stats():
    return {
        "total_nodes": 0, "total_edges": 0, "functions": 0, "tests": 0,
        "endpoints": 0, "experts_total": 0, "experts_involved": 0,
        "decisions": 0, "risks": 0, "classes": 0, "modules": 0, "datastores": 0,
    }


def _detect_phase_status(slug):
    base = FEATURES_DIR / slug
    statuses = {}
    artifact_map = {
        "lens": ["overview.md", "overview_v5.md"],
        "forge": ["solution.md", "solution_v2.md"],
        "sentinel": ["risk_analysis/risk-analysis.md", "risk_analysis"],
        "scribe": ["tech-spec-*.md", "tech-spec-*.docx"],
        "welder": ["implementation/"],
        "launch": ["launch/"],
    }
    found_active = False
    for phase in PHASES:
        pid = phase["id"]
        patterns = artifact_map.get(pid, [])
        exists = False
        for pat in patterns:
            if "*" in pat:
                import glob
                exists = bool(glob.glob(str(base / pat)))
            else:
                exists = (base / pat).exists()
            if exists:
                break
        if exists:
            statuses[pid] = "done"
        elif not found_active:
            statuses[pid] = "active"
            found_active = True
        else:
            statuses[pid] = "pending"
    if not found_active:
        for pid in statuses:
            if statuses[pid] == "pending":
                statuses[pid] = "active"
                break
    return statuses


def _load_nexus_data_json(slug):
    path = FEATURES_DIR / slug / "nexus-data.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def build_nexus_data(slug, db_path=None):
    feature = _load_feature_data(slug, db_path)
    if not feature:
        feature = {
            "slug": slug, "name": slug.replace("-", " ").title(),
            "status": "proposed", "phase": "lens",
            "created_at": datetime.datetime.now().isoformat(),
        }
    experts = _load_experts(slug, db_path)
    stats = _load_cortex_stats(db_path)
    statuses = _detect_phase_status(slug)
    current_phase = "lens"
    for p in PHASES:
        if statuses.get(p["id"]) == "active":
            current_phase = p["id"]
            break
    existing = _load_nexus_data_json(slug)
    phases_data = {}
    for p in PHASES:
        pid = p["id"]
        phase_info = {
            "status": statuses.get(pid, "pending"), "name": p["name"],
            "role": p["role"], "icon": p["icon"],
            "description": PHASE_DESCRIPTIONS.get(pid, ""),
            "artifacts": [], "summary": "", "content_html": "",
            "mermaid_diagrams": [], "experts_involved": [],
            "cortex_nodes_created": 0,
            "variations": {"v1": {"selected": True, "summary": "Default"}},
            "chat_history": [],
        }
        if existing and pid in existing.get("phases", {}):
            old = existing["phases"][pid]
            phase_info["chat_history"] = old.get("chat_history", [])
            phase_info["variations"] = old.get("variations", phase_info["variations"])
            phase_info["content_html"] = old.get("content_html", "")
        artifact_file = FEATURES_DIR / slug / p["artifact"]
        if artifact_file.exists() and artifact_file.is_file():
            phase_info["artifacts"].append(p["artifact"])
            try:
                content = artifact_file.read_text(errors="replace")[:8000]
                phase_info["content_html"] = _md_to_html(content)
                phase_info["summary"] = content[:200].replace("\n", " ").strip()
            except Exception:
                pass
        phases_data[pid] = phase_info
    risks = []
    try:
        db = db_path or DB_PATH
        if Path(db).exists():
            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            for row in conn.execute(
                "SELECT * FROM nodes WHERE type='RiskItem' AND name LIKE ? LIMIT 20",
                (f"%{slug}%",)
            ).fetchall():
                data = json.loads(row["data"]) if row["data"] else {}
                risks.append({
                    "id": row["name"], "severity": data.get("severity", "medium"),
                    "phase": "sentinel",
                    "summary": data.get("description", row["name"])[:120],
                })
            conn.close()
    except Exception:
        pass
    return {
        "feature": {
            "slug": slug, "name": feature.get("name", slug),
            "created_at": feature.get("created_at", ""),
            "current_phase": current_phase, "variation": "v1",
        },
        "phases": phases_data, "experts": experts[:12],
        "cortex_stats": stats, "risks": risks,
        "generated_at": datetime.datetime.now().isoformat(),
    }


def _md_to_html(md_text):
    lines = md_text.split("\n")
    out = []
    in_code = False
    in_table = False
    for line in lines:
        if line.startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                lang = line[3:].strip()
                out.append(f'<pre><code class="lang-{lang}">')
                in_code = True
            continue
        if in_code:
            out.append(html_mod.escape(line))
            continue
        if line.startswith("|") and "|" in line[1:]:
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if all(set(c) <= set("- :") for c in cells):
                continue
            if not in_table:
                out.append("<table>")
                in_table = True
                out.append("<tr>" + "".join(f"<th>{html_mod.escape(c)}</th>" for c in cells) + "</tr>")
            else:
                out.append("<tr>" + "".join(f"<td>{html_mod.escape(c)}</td>" for c in cells) + "</tr>")
            continue
        if in_table and not line.startswith("|"):
            out.append("</table>")
            in_table = False
        if line.startswith("#### "):
            out.append(f"<h4>{html_mod.escape(line[5:])}</h4>")
        elif line.startswith("### "):
            out.append(f"<h3>{html_mod.escape(line[4:])}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{html_mod.escape(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{html_mod.escape(line[2:])}</h1>")
        elif line.startswith("- "):
            out.append(f"<li>{html_mod.escape(line[2:])}</li>")
        elif line.startswith("> "):
            out.append(f"<blockquote>{html_mod.escape(line[2:])}</blockquote>")
        elif line.strip() == "":
            out.append("<br>")
        else:
            text = html_mod.escape(line)
            text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
            text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
            out.append(f"<p>{text}</p>")
    if in_table:
        out.append("</table>")
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


# ═══════════════════════════════════════════════════════════════════════════════
# NEXUS.HTML TEMPLATE — Three.js 3D Obsidian-style Dashboard
# ═══════════════════════════════════════════════════════════════════════════════

NEXUS_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NEXUS — {{FEATURE_NAME}}</title>
<script type="importmap">
{"imports":{
  "three":"https://unpkg.com/three@0.164.1/build/three.module.js",
  "three/addons/":"https://unpkg.com/three@0.164.1/examples/jsm/"
}}
</script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#050810;--panel:rgba(8,14,28,0.94);--cyan:#00d4ff;--green:#00ff88;
  --red:#ff4444;--amber:#ffaa00;--purple:#a78bfa;--pink:#ff6b9d;
  --text:#c8d6e5;--dim:#4a5a6a;--bright:#eef4ff;
  --border:rgba(0,212,255,0.10);--glass:rgba(0,212,255,0.05);
  --hud:'JetBrains Mono',monospace;--body:'Inter',-apple-system,sans-serif;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{font-family:var(--body);background:var(--bg);color:var(--text)}
canvas{display:block}

/* ── Header ── */
.hdr{position:fixed;top:0;left:0;right:0;height:52px;background:var(--panel);
  backdrop-filter:blur(20px);border-bottom:1px solid var(--border);
  display:flex;align-items:center;padding:0 20px;z-index:100;gap:14px}
.hdr-logo{font-family:var(--hud);font-size:16px;font-weight:600;
  letter-spacing:6px;color:var(--cyan);text-transform:uppercase}
.hdr-feat{font-size:13px;color:var(--dim);flex:1}
.hdr-feat strong{color:var(--text)}
.phase-hud{display:flex;gap:3px}
.ph-badge{font-family:var(--hud);font-size:9px;padding:3px 8px;border-radius:3px;
  cursor:pointer;border:1px solid transparent;text-transform:uppercase;letter-spacing:1px;
  transition:all .3s}
.ph-badge.done{background:rgba(0,255,136,.08);color:var(--green);border-color:rgba(0,255,136,.3)}
.ph-badge.active{background:rgba(0,212,255,.08);color:var(--cyan);border-color:var(--cyan);
  animation:bpulse 2s infinite}
.ph-badge.pending{background:rgba(255,255,255,.02);color:var(--dim)}
@keyframes bpulse{0%,100%{box-shadow:0 0 4px rgba(0,212,255,.1)}50%{box-shadow:0 0 12px rgba(0,212,255,.2)}}
.export-grp{display:flex;gap:3px}
.exp-btn{font-family:var(--hud);font-size:9px;padding:3px 7px;background:var(--glass);
  border:1px solid var(--border);color:var(--dim);border-radius:3px;cursor:pointer;transition:all .2s}
.exp-btn:hover{border-color:var(--cyan);color:var(--cyan)}

/* ── Search ── */
.search-box{position:fixed;top:62px;left:50%;transform:translateX(-50%);z-index:90;
  width:340px}
.search-input{width:100%;background:var(--panel);backdrop-filter:blur(16px);
  border:1px solid var(--border);border-radius:20px;padding:8px 16px 8px 36px;
  color:var(--text);font-family:var(--body);font-size:12px;outline:none;transition:border-color .2s}
.search-input:focus{border-color:var(--cyan)}
.search-input::placeholder{color:var(--dim)}
.search-icon{position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--dim);font-size:13px}

/* ── Filters ── */
.filters{position:fixed;top:62px;left:16px;z-index:90;display:flex;gap:4px}
.flt{font-family:var(--hud);font-size:9px;padding:3px 8px;background:var(--panel);
  border:1px solid var(--border);color:var(--dim);border-radius:12px;cursor:pointer;
  backdrop-filter:blur(12px);transition:all .2s}
.flt.on{border-color:var(--cyan);color:var(--cyan);background:rgba(0,212,255,.06)}

/* ── Stats sidebar ── */
.stats{position:fixed;top:52px;right:0;width:180px;height:calc(100vh - 94px);z-index:10;
  display:flex;flex-direction:column;justify-content:center;padding:12px;gap:6px;
  pointer-events:none}
.stat-card{pointer-events:all;padding:6px 10px;background:var(--panel);
  border:1px solid var(--border);border-radius:6px;backdrop-filter:blur(12px)}
.stat-label{font-family:var(--hud);font-size:8px;color:var(--dim);
  text-transform:uppercase;letter-spacing:1px;margin-bottom:2px}
.stat-bar{height:3px;background:rgba(255,255,255,.04);border-radius:2px;overflow:hidden;margin-bottom:1px}
.stat-fill{height:100%;border-radius:2px;transition:width 1s}
.stat-val{font-family:var(--hud);font-size:10px;color:var(--text);text-align:right}

/* ── Status bar ── */
.sbar{position:fixed;bottom:0;left:0;right:0;height:42px;background:var(--panel);
  backdrop-filter:blur(20px);border-top:1px solid var(--border);
  display:flex;align-items:center;padding:0 20px;z-index:100;gap:16px}
.sbar .s{font-family:var(--hud);font-size:10px;color:var(--dim)}
.sbar .s .n{color:var(--cyan);font-weight:600}
.sbar .sep{width:1px;height:16px;background:var(--border)}
.sbar .ver{margin-left:auto;font-family:var(--hud);font-size:9px;color:var(--dim);letter-spacing:1px}

/* ── Detail panel ── */
.dpanel{position:fixed;bottom:42px;left:0;right:0;height:0;background:var(--panel);
  backdrop-filter:blur(24px);border-top:1px solid rgba(0,212,255,.2);z-index:50;
  transition:height .4s cubic-bezier(.16,1,.3,1);overflow:hidden;display:flex;flex-direction:column}
.dpanel.open{height:55vh}
.dp-head{display:flex;align-items:center;padding:10px 20px;border-bottom:1px solid var(--border);gap:10px;flex-shrink:0}
.dp-icon{font-size:18px}
.dp-name{font-family:var(--hud);font-size:14px;font-weight:600;color:var(--cyan);
  text-transform:uppercase;letter-spacing:2px}
.dp-role{font-size:12px;color:var(--dim);flex:1}
.dp-close{width:28px;height:28px;border-radius:6px;border:1px solid var(--border);
  background:var(--glass);color:var(--dim);cursor:pointer;font-size:14px;
  display:flex;align-items:center;justify-content:center;transition:all .2s}
.dp-close:hover{border-color:var(--red);color:var(--red)}
.dp-body{display:flex;flex:1;overflow:hidden}
.dp-content{flex:0 0 60%;padding:12px 20px;overflow-y:auto;border-right:1px solid var(--border)}
.dp-content h1,.dp-content h2,.dp-content h3{color:var(--cyan);margin:12px 0 6px}
.dp-content h1{font-size:16px}.dp-content h2{font-size:14px}
.dp-content h3{font-size:12px;color:var(--green)}
.dp-content p{font-size:12px;line-height:1.7;margin:3px 0}
.dp-content pre{background:var(--bg);border:1px solid var(--border);border-radius:6px;
  padding:10px;font-family:var(--hud);font-size:11px;overflow-x:auto;margin:6px 0}
.dp-content code{font-family:var(--hud);font-size:11px;color:var(--green)}
.dp-content table{width:100%;border-collapse:collapse;font-size:11px;margin:6px 0}
.dp-content th,.dp-content td{padding:4px 8px;border-bottom:1px solid var(--border);text-align:left}
.dp-content th{color:var(--cyan);font-weight:500}
.dp-content strong{color:var(--bright)}
.dp-content blockquote{border-left:2px solid var(--cyan);padding-left:10px;color:var(--dim);font-style:italic}
.dp-content li{font-size:12px;margin:2px 0;padding-left:6px}
.dp-chat{flex:0 0 40%;display:flex;flex-direction:column;background:rgba(0,0,0,.12)}
.chat-hdr{font-family:var(--hud);font-size:10px;padding:8px 14px;color:var(--dim);
  text-transform:uppercase;letter-spacing:1px;border-bottom:1px solid var(--border)}
.chat-msgs{flex:1;overflow-y:auto;padding:10px 14px}
.chat-m{margin:6px 0;padding:6px 10px;border-radius:6px;font-size:12px;line-height:1.5;max-width:88%}
.chat-m.u{background:rgba(0,212,255,.06);border:1px solid rgba(0,212,255,.15);margin-left:auto;text-align:right}
.chat-m.n{background:var(--glass);border:1px solid var(--border)}
.chat-m .ts{font-family:var(--hud);font-size:8px;color:var(--dim);margin-top:2px}
.chat-in{padding:6px 10px;border-top:1px solid var(--border);display:flex;gap:4px;align-items:center}
.chat-inp{flex:1;background:var(--bg);border:1px solid var(--border);border-radius:6px;
  padding:6px 10px;color:var(--text);font-family:var(--body);font-size:12px;outline:none}
.chat-inp:focus{border-color:var(--cyan)}
.chat-inp::placeholder{color:var(--dim)}
.chat-send{width:30px;height:30px;border-radius:6px;border:1px solid var(--cyan);
  background:rgba(0,212,255,.06);color:var(--cyan);cursor:pointer;font-size:12px;
  display:flex;align-items:center;justify-content:center;transition:all .2s}
.chat-send:hover{background:var(--cyan);color:var(--bg)}
.var-bar{display:flex;gap:4px;padding:6px 20px;border-top:1px solid var(--border);
  background:rgba(0,0,0,.15);flex-shrink:0}
.var-t{font-family:var(--hud);font-size:9px;padding:3px 10px;border-radius:3px;cursor:pointer;
  border:1px solid var(--border);color:var(--dim);background:transparent;transition:all .2s}
.var-t.sel{border-color:var(--green);color:var(--green);background:rgba(0,255,136,.06)}
.var-t.add{border-style:dashed}.var-t.add:hover{border-color:var(--cyan);color:var(--cyan)}
.conn-bar{padding:6px 20px;border-top:1px solid var(--border);font-size:10px;
  color:var(--dim);display:flex;gap:16px;flex-shrink:0;background:rgba(0,0,0,.15)}
.conn-bar span{color:var(--text)}
.conn-bar .etag{background:var(--purple);color:var(--bg);padding:1px 5px;border-radius:2px;
  font-family:var(--hud);font-size:8px;font-weight:600}

/* ── Node label (HTML overlay) ── */
.node-label{position:absolute;font-family:var(--hud);font-size:10px;font-weight:600;
  text-align:center;pointer-events:none;text-shadow:0 0 8px rgba(0,0,0,.9);white-space:nowrap;
  transform:translate(-50%,-50%)}

/* ── Scrollbar ── */
::-webkit-scrollbar{width:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
</style>
</head>
<body>

<!-- Header -->
<div class="hdr">
  <div class="hdr-logo">Nexus</div>
  <div class="hdr-feat"><strong id="feat-name">{{FEATURE_NAME}}</strong></div>
  <div class="phase-hud" id="phud"></div>
  <div class="export-grp">
    <button class="exp-btn" onclick="xport('md')">MD</button>
    <button class="exp-btn" onclick="xport('docx')">DOCX</button>
    <button class="exp-btn" onclick="xport('html')">HTML</button>
    <button class="exp-btn" onclick="xport('zip')">ZIP</button>
  </div>
</div>

<!-- Search -->
<div class="search-box">
  <span class="search-icon">&#x1F50D;</span>
  <input class="search-input" id="search" placeholder="Search phases, experts, risks..." oninput="onSearch(this.value)">
</div>

<!-- Filters -->
<div class="filters">
  <button class="flt on" data-f="all" onclick="setFilter(this)">All</button>
  <button class="flt" data-f="experts" onclick="setFilter(this)">Experts</button>
  <button class="flt" data-f="risks" onclick="setFilter(this)">Risks</button>
</div>

<!-- Stats -->
<div class="stats" id="stats-panel"></div>

<!-- Detail Panel -->
<div class="dpanel" id="dpanel">
  <div class="dp-head">
    <span class="dp-icon" id="dp-icon"></span>
    <span class="dp-name" id="dp-name"></span>
    <span class="dp-role" id="dp-role"></span>
    <button class="dp-close" onclick="closePanel()">&#x2715;</button>
  </div>
  <div class="dp-body">
    <div class="dp-content" id="dp-content"></div>
    <div class="dp-chat">
      <div class="chat-hdr">Nexus Chat</div>
      <div class="chat-msgs" id="dp-msgs"></div>
      <div class="chat-in">
        <input class="chat-inp" id="chat-inp" placeholder="Ask about this phase..." onkeydown="if(event.key==='Enter')sendMsg()">
        <button class="chat-send" onclick="sendMsg()">&#x25B6;</button>
      </div>
    </div>
  </div>
  <div class="conn-bar" id="dp-conn"></div>
  <div class="var-bar" id="dp-vars"></div>
</div>

<!-- Status Bar -->
<div class="sbar" id="sbar"></div>

<!-- ═══════ Three.js 3D Scene ═══════ -->
<script type="module">
import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
import {EffectComposer} from 'three/addons/postprocessing/EffectComposer.js';
import {RenderPass} from 'three/addons/postprocessing/RenderPass.js';
import {UnrealBloomPass} from 'three/addons/postprocessing/UnrealBloomPass.js';

const D = {{NEXUS_DATA_JSON}};

const PO = ['lens','forge','sentinel','scribe','welder','launch'];
const PM = {
  lens:{name:'Lens',color:0x00d4ff},forge:{name:'Forge',color:0xffaa00},
  sentinel:{name:'Sentinel',color:0xff4444},scribe:{name:'Scribe',color:0x00ff88},
  welder:{name:'Welder',color:0xa78bfa},launch:{name:'Launch',color:0xff6b9d},
};
const STATUS_EMISSIVE = {done:0x00ff88,active:0x00d4ff,pending:0x1a1a2e,blocked:0xff4444};

let scene, camera, renderer, composer, controls;
let phaseNodes = {}, expertNodes = [], riskNodes = [], edgeLines = [];
let particleSystems = [];
let raycaster, mouse, hoveredObj = null, curPhase = null;
let labelEls = {};
let clock = new THREE.Clock();

// ── Init ──
init();
animate();

function init(){
  // Scene
  scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x050810, 0.04);

  // Camera
  camera = new THREE.PerspectiveCamera(50, window.innerWidth/window.innerHeight, 0.1, 200);
  camera.position.set(0, 3, 12);

  // Renderer
  renderer = new THREE.WebGLRenderer({antialias:true, alpha:false});
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setClearColor(0x050810);
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.2;
  document.body.prepend(renderer.domElement);

  // Post-processing (bloom)
  composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(
    new THREE.Vector2(window.innerWidth, window.innerHeight), 0.8, 0.4, 0.2
  );
  composer.addPass(bloom);

  // Controls
  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.05;
  controls.maxDistance = 30;
  controls.minDistance = 4;
  controls.autoRotate = true;
  controls.autoRotateSpeed = 0.3;

  // Lights
  scene.add(new THREE.AmbientLight(0x222244, 0.6));
  const dirLight = new THREE.DirectionalLight(0x4488ff, 0.4);
  dirLight.position.set(5, 8, 5);
  scene.add(dirLight);

  // Background particles
  createStarField();

  // Phase nodes — arranged in 3D spiral
  const positions = [
    [-3.5, 0.8, 0],  [-1.2, 1.8, 1.5],  [1.2, 0.6, -0.8],
    [3.5, 1.6, 0.8],  [1.8, -0.8, 2.0],  [4.0, -0.2, -0.5],
  ];
  PO.forEach((pid, i) => {
    const pos = positions[i];
    const phase = D.phases[pid] || {};
    const meta = PM[pid];
    const status = phase.status || 'pending';
    const emissive = STATUS_EMISSIVE[status] || 0x1a1a2e;

    const geo = new THREE.IcosahedronGeometry(0.55, 2);
    const mat = new THREE.MeshStandardMaterial({
      color: meta.color, emissive: emissive, emissiveIntensity: status==='pending' ? 0.1 : 0.5,
      metalness: 0.3, roughness: 0.4, transparent: true, opacity: status==='pending' ? 0.4 : 0.9,
    });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.position.set(...pos);
    mesh.userData = {type:'phase', id:pid, status:status};
    scene.add(mesh);
    phaseNodes[pid] = mesh;

    // Glow ring for active/done
    if(status !== 'pending'){
      const ringGeo = new THREE.RingGeometry(0.7, 0.78, 32);
      const ringMat = new THREE.MeshBasicMaterial({
        color: emissive, transparent:true, opacity:0.3, side:THREE.DoubleSide
      });
      const ring = new THREE.Mesh(ringGeo, ringMat);
      ring.position.copy(mesh.position);
      ring.lookAt(camera.position);
      ring.userData = {ring:true, parent:pid};
      scene.add(ring);
    }

    // HTML label
    createLabel(pid, meta.name.toUpperCase(), meta.color, mesh.position);
  });

  // Edges between phases
  const edgePairs = [[0,1],[1,2],[2,3],[3,4],[4,5]];
  edgePairs.forEach(([a,b]) => {
    const pA = phaseNodes[PO[a]].position;
    const pB = phaseNodes[PO[b]].position;
    const pts = [];
    for(let t=0;t<=1;t+=0.02) pts.push(new THREE.Vector3().lerpVectors(pA,pB,t));
    const lineGeo = new THREE.BufferGeometry().setFromPoints(pts);
    const lineMat = new THREE.LineBasicMaterial({color:0x00d4ff, transparent:true, opacity:0.2});
    const line = new THREE.Line(lineGeo, lineMat);
    scene.add(line);
    edgeLines.push(line);

    // Particles along edge
    createEdgeParticles(pA, pB);
  });

  // Expert nodes
  (D.experts||[]).forEach((exp, i) => {
    const targetPhase = (exp.phases && exp.phases[0]) || PO[Math.min(i, 5)];
    const parent = phaseNodes[targetPhase] || phaseNodes.lens;
    const angle = (i / Math.max(D.experts.length, 1)) * Math.PI * 2;
    const r = 1.4 + Math.random()*0.4;

    const geo = new THREE.SphereGeometry(0.12, 12, 12);
    const mat = new THREE.MeshStandardMaterial({
      color:0xa78bfa, emissive:0xa78bfa, emissiveIntensity:0.4,
      metalness:0.2, roughness:0.5,
    });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.position.set(
      parent.position.x + Math.cos(angle)*r,
      parent.position.y + Math.sin(angle)*0.6,
      parent.position.z + Math.sin(angle)*r
    );
    mesh.userData = {type:'expert', data:exp, idx:i};
    scene.add(mesh);
    expertNodes.push(mesh);

    // Expert-to-phase line
    const lGeo = new THREE.BufferGeometry().setFromPoints([mesh.position.clone(), parent.position.clone()]);
    const lMat = new THREE.LineBasicMaterial({color:0xa78bfa, transparent:true, opacity:0.1});
    scene.add(new THREE.Line(lGeo, lMat));

    createLabel('exp-'+i, exp.codename, 0xa78bfa, mesh.position, 8);
  });

  // Risk nodes
  (D.risks||[]).forEach((risk, i) => {
    const parent = phaseNodes[risk.phase] || phaseNodes.sentinel;
    const angle = (i / Math.max(D.risks.length, 1)) * Math.PI * 2 + 1;
    const geo = new THREE.OctahedronGeometry(0.14, 0);
    const mat = new THREE.MeshStandardMaterial({
      color:0xff4444, emissive:0xff4444, emissiveIntensity:0.6,
      metalness:0.2, roughness:0.5,
    });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.position.set(
      parent.position.x + Math.cos(angle)*1.2,
      parent.position.y + 0.8,
      parent.position.z + Math.sin(angle)*1.2
    );
    mesh.userData = {type:'risk', data:risk, idx:i};
    scene.add(mesh);
    riskNodes.push(mesh);

    const lGeo = new THREE.BufferGeometry().setFromPoints([mesh.position.clone(), parent.position.clone()]);
    const lMat = new THREE.LineBasicMaterial({color:0xff4444, transparent:true, opacity:0.15});
    scene.add(new THREE.Line(lGeo, lMat));

    createLabel('risk-'+i, risk.id, 0xff4444, mesh.position, 8);
  });

  // Raycaster
  raycaster = new THREE.Raycaster();
  mouse = new THREE.Vector2();

  // Events
  renderer.domElement.addEventListener('mousemove', onMouseMove);
  renderer.domElement.addEventListener('click', onClick);
  window.addEventListener('resize', onResize);

  // Build UI
  buildHUD();
  buildStats();
  buildStatusBar();
}

function createStarField(){
  const geo = new THREE.BufferGeometry();
  const n = 2000;
  const pos = new Float32Array(n*3);
  for(let i=0;i<n*3;i++) pos[i] = (Math.random()-0.5)*80;
  geo.setAttribute('position', new THREE.BufferAttribute(pos,3));
  const mat = new THREE.PointsMaterial({color:0x334466, size:0.03, sizeAttenuation:true});
  scene.add(new THREE.Points(geo, mat));
}

function createEdgeParticles(pA, pB){
  const count = 12;
  const geo = new THREE.BufferGeometry();
  const pos = new Float32Array(count*3);
  const offsets = new Float32Array(count);
  for(let i=0;i<count;i++){
    offsets[i] = Math.random();
    const t = offsets[i];
    pos[i*3] = pA.x + (pB.x-pA.x)*t;
    pos[i*3+1] = pA.y + (pB.y-pA.y)*t;
    pos[i*3+2] = pA.z + (pB.z-pA.z)*t;
  }
  geo.setAttribute('position', new THREE.BufferAttribute(pos,3));
  const mat = new THREE.PointsMaterial({color:0x00d4ff, size:0.04, sizeAttenuation:true, transparent:true, opacity:0.6});
  const pts = new THREE.Points(geo, mat);
  pts.userData = {pA:pA.clone(), pB:pB.clone(), offsets:offsets};
  scene.add(pts);
  particleSystems.push(pts);
}

function createLabel(id, text, color, pos3d, size){
  const el = document.createElement('div');
  el.className = 'node-label';
  el.textContent = text;
  el.style.color = '#'+new THREE.Color(color).getHexString();
  el.style.fontSize = (size||10)+'px';
  document.body.appendChild(el);
  labelEls[id] = {el:el, pos:pos3d.clone()};
}

// ── Animation loop ──
function animate(){
  requestAnimationFrame(animate);
  const t = clock.getElapsedTime();

  // Rotate phase nodes
  Object.values(phaseNodes).forEach(m => { m.rotation.y = t*0.3; m.rotation.x = Math.sin(t*0.5)*0.1; });

  // Animate edge particles
  particleSystems.forEach(ps => {
    const {pA,pB,offsets} = ps.userData;
    const pos = ps.geometry.attributes.position.array;
    for(let i=0;i<offsets.length;i++){
      offsets[i] = (offsets[i] + 0.003) % 1;
      const tt = offsets[i];
      pos[i*3] = pA.x + (pB.x-pA.x)*tt;
      pos[i*3+1] = pA.y + (pB.y-pA.y)*tt;
      pos[i*3+2] = pA.z + (pB.z-pA.z)*tt;
    }
    ps.geometry.attributes.position.needsUpdate = true;
  });

  // Bob expert nodes
  expertNodes.forEach((m,i) => { m.position.y += Math.sin(t*1.5+i)*0.0008; });

  // Spin risk nodes
  riskNodes.forEach(m => { m.rotation.y = t*1.5; m.rotation.x = t; });

  // Update rings to face camera
  scene.children.forEach(c => {
    if(c.userData && c.userData.ring) c.lookAt(camera.position);
  });

  controls.update();
  composer.render();
  updateLabels();
}

function updateLabels(){
  Object.values(labelEls).forEach(({el, pos}) => {
    const v = pos.clone().project(camera);
    if(v.z > 1){ el.style.display='none'; return; }
    el.style.display = 'block';
    el.style.left = ((v.x+1)/2*window.innerWidth) + 'px';
    el.style.top = ((-v.y+1)/2*window.innerHeight) + 'px';
  });
}

// ── Interaction ──
function onMouseMove(e){
  mouse.x = (e.clientX/window.innerWidth)*2-1;
  mouse.y = -(e.clientY/window.innerHeight)*2+1;
  raycaster.setFromCamera(mouse, camera);

  const allMeshes = [...Object.values(phaseNodes), ...expertNodes, ...riskNodes];
  const hits = raycaster.intersectObjects(allMeshes);
  renderer.domElement.style.cursor = hits.length > 0 ? 'pointer' : 'default';

  // Hover glow
  if(hoveredObj){
    hoveredObj.material.emissiveIntensity = hoveredObj.userData._origEmissive || 0.5;
    hoveredObj = null;
  }
  if(hits.length > 0){
    hoveredObj = hits[0].object;
    hoveredObj.userData._origEmissive = hoveredObj.material.emissiveIntensity;
    hoveredObj.material.emissiveIntensity = 1.2;
  }
}

function onClick(e){
  raycaster.setFromCamera(mouse, camera);
  const allMeshes = [...Object.values(phaseNodes), ...expertNodes, ...riskNodes];
  const hits = raycaster.intersectObjects(allMeshes);
  if(hits.length === 0){ closePanel(); return; }
  const obj = hits[0].object;
  if(obj.userData.type === 'phase') openPanel(obj.userData.id);
}

function onResize(){
  camera.aspect = window.innerWidth/window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
  composer.setSize(window.innerWidth, window.innerHeight);
}

// ── HUD ──
function buildHUD(){
  const hud = document.getElementById('phud');
  PO.forEach(pid => {
    const ph = D.phases[pid]||{};
    const b = document.createElement('div');
    b.className = 'ph-badge '+(ph.status||'pending');
    b.textContent = PM[pid].name;
    b.onclick = () => { openPanel(pid); focusNode(pid); };
    hud.appendChild(b);
  });
}

function buildStats(){
  const panel = document.getElementById('stats-panel');
  const s = D.cortex_stats;
  const mx = Math.max(s.functions,1);
  [{l:'Functions',v:s.functions,m:mx,c:'var(--cyan)'},{l:'Tests',v:s.tests,m:mx,c:'var(--green)'},
   {l:'Endpoints',v:s.endpoints,m:2000,c:'var(--amber)'},{l:'Experts',v:s.experts_total,m:50,c:'var(--purple)'},
   {l:'Decisions',v:s.decisions,m:100,c:'var(--green)'},{l:'Risks',v:s.risks,m:Math.max(s.risks,10),c:'var(--red)'}
  ].forEach(item => {
    const pct = Math.min(100,(item.v/item.m)*100);
    const d = document.createElement('div');
    d.className='stat-card';
    d.innerHTML=`<div class="stat-label">${item.l}</div>
      <div class="stat-bar"><div class="stat-fill" style="width:${pct}%;background:${item.c}"></div></div>
      <div class="stat-val">${item.v>=1000?(item.v/1000).toFixed(1)+'K':item.v}</div>`;
    panel.appendChild(d);
  });
}

function buildStatusBar(){
  const bar = document.getElementById('sbar');
  const s = D.cortex_stats;
  const f = D.feature;
  const ai = PO.indexOf(f.current_phase)+1;
  const fmt = n => n>=1000?(n/1000).toFixed(1)+'K':n;
  bar.innerHTML=`
    <div class="s"><span class="n">${fmt(s.total_nodes)}</span> nodes</div><div class="sep"></div>
    <div class="s"><span class="n">${fmt(s.total_edges)}</span> edges</div><div class="sep"></div>
    <div class="s"><span class="n">${s.experts_total}</span> experts</div><div class="sep"></div>
    <div class="s">Phase <span class="n">${ai}</span>/6</div><div class="sep"></div>
    <div class="s"><span class="n" style="color:${s.risks>0?'var(--red)':'var(--green)'}">${s.risks}</span> risks</div>
    <div class="ver">NEXUS v3.0</div>`;
}

// ── Detail panel ──
function openPanel(pid){
  curPhase = pid;
  const ph = D.phases[pid]||{};
  const meta = PM[pid];
  document.getElementById('dp-icon').textContent = ph.icon||'';
  document.getElementById('dp-name').textContent = meta.name;
  document.getElementById('dp-role').textContent = '\\u2014 '+(ph.role||'');
  const content = document.getElementById('dp-content');
  content.innerHTML = (ph.content_html && ph.content_html.trim())
    ? ph.content_html
    : '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:var(--dim)"><div style="font-size:24px;opacity:.5">'+
      (ph.icon||'')+'</div><div>Phase not yet executed.</div></div>';
  // Chat
  const msgs = document.getElementById('dp-msgs');
  msgs.innerHTML='';
  (ph.chat_history||[]).forEach(m => {
    const d=document.createElement('div');
    d.className='chat-m '+(m.role==='user'?'u':'n');
    d.innerHTML=m.content+'<div class="ts">'+(m.ts||'')+'</div>';
    msgs.appendChild(d);
  });
  msgs.scrollTop=msgs.scrollHeight;
  // Connections
  const ci = PO.indexOf(pid);
  const prev = ci>0?PM[PO[ci-1]].name:'Sources';
  const next = ci<5?PM[PO[ci+1]].name:'Deploy';
  const exps = (ph.experts_involved||[]).map(e=>'<span class="etag">'+e+'</span>').join(' ');
  document.getElementById('dp-conn').innerHTML=
    '<div>\\u2190 Input: <span>'+prev+'</span></div><div>\\u2192 Output: <span>'+next+'</span></div>'+
    '<div>Experts: '+(exps||'<span style="color:var(--dim)">none</span>')+'</div>';
  // Variations
  const vb = document.getElementById('dp-vars');
  vb.innerHTML='';
  const vars = ph.variations||{v1:{selected:true}};
  Object.entries(vars).forEach(([k,v])=>{
    const t=document.createElement('button');
    t.className='var-t'+(v.selected?' sel':'');
    t.textContent=k;
    t.onclick=()=>selVar(pid,k);
    vb.appendChild(t);
  });
  const ab=document.createElement('button');
  ab.className='var-t add';ab.textContent='+ New';
  ab.onclick=()=>addVar(pid);
  vb.appendChild(ab);
  document.getElementById('dpanel').classList.add('open');
  controls.autoRotate = false;
}

window.closePanel = function(){
  document.getElementById('dpanel').classList.remove('open');
  curPhase=null;
  controls.autoRotate = true;
};
window.sendMsg = function(){
  if(!curPhase)return;
  const inp=document.getElementById('chat-inp');
  const txt=inp.value.trim();if(!txt)return;
  const ph=D.phases[curPhase];
  if(!ph.chat_history)ph.chat_history=[];
  const ts=new Date().toLocaleTimeString();
  ph.chat_history.push({role:'user',content:txt,ts:ts});
  const msgs=document.getElementById('dp-msgs');
  const d=document.createElement('div');d.className='chat-m u';
  d.innerHTML=txt+'<div class="ts">'+ts+'</div>';msgs.appendChild(d);
  setTimeout(()=>{
    const rts=new Date().toLocaleTimeString();
    ph.chat_history.push({role:'nexus',content:"Noted \\u2014 I'll incorporate this in the next run.",ts:rts});
    const rd=document.createElement('div');rd.className='chat-m n';
    rd.innerHTML="Noted \\u2014 I'll incorporate this in the next run.<div class='ts'>"+rts+"</div>";
    msgs.appendChild(rd);msgs.scrollTop=msgs.scrollHeight;
  },400);
  inp.value='';msgs.scrollTop=msgs.scrollHeight;
};
window.xport = function(fmt){
  alert('Export as '+fmt.toUpperCase()+' \\u2014 run: python3 scripts/cortex_html.py export '+D.feature.slug+' '+fmt);
};
function selVar(pid,k){
  const ph=D.phases[pid];if(!ph.variations)return;
  Object.keys(ph.variations).forEach(vk=>{ph.variations[vk].selected=(vk===k)});
  openPanel(pid);
}
function addVar(pid){
  const ph=D.phases[pid];if(!ph.variations)ph.variations={};
  const n=Object.keys(ph.variations).length+1;
  ph.variations['v'+n]={selected:false,summary:'Variation '+n};
  openPanel(pid);
}
function focusNode(pid){
  const node=phaseNodes[pid];if(!node)return;
  const p=node.position;
  controls.target.set(p.x,p.y,p.z);
}
window.setFilter = function(btn){
  document.querySelectorAll('.flt').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
  const f=btn.dataset.f;
  expertNodes.forEach(m=>{m.visible=(f==='all'||f==='experts')});
  riskNodes.forEach(m=>{m.visible=(f==='all'||f==='risks')});
  // Toggle labels
  Object.entries(labelEls).forEach(([id,{el}])=>{
    if(id.startsWith('exp-')) el.style.display=(f==='all'||f==='experts')?'':'none';
    if(id.startsWith('risk-')) el.style.display=(f==='all'||f==='risks')?'':'none';
  });
};
window.onSearch = function(q){
  q = q.toLowerCase();
  if(!q){ Object.values(labelEls).forEach(({el})=>el.style.opacity='1'); return; }
  Object.entries(labelEls).forEach(([id,{el}])=>{
    el.style.opacity = el.textContent.toLowerCase().includes(q)?'1':'0.15';
  });
};
</script>
</body>
</html>'''


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE STANDALONE TEMPLATE
# ═══════════════════════════════════════════════════════════════════════════════

PHASE_TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{PHASE_NAME}} — {{FEATURE_NAME}}</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg-deep: #0a0e17; --bg-panel: rgba(13,20,35,0.92);
  --cyan: #00d4ff; --green: #00ff88; --red: #ff4444; --amber: #ffaa00;
  --text: #c8d6e5; --text-dim: #5a6a7a; --border: rgba(0,212,255,0.12);
  --glass: rgba(0,212,255,0.06);
  --font-hud: 'JetBrains Mono', monospace; --font-body: 'Inter', sans-serif;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: var(--font-body); background: var(--bg-deep); color: var(--text);
  padding: 40px; max-width: 1200px; margin: 0 auto; }
body::after { content: ''; position: fixed; inset: 0;
  background: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,212,255,0.012) 2px, rgba(0,212,255,0.012) 4px);
  pointer-events: none; z-index: 10000; }
.header-bar { display: flex; align-items: center; gap: 16px; padding: 16px 24px;
  background: var(--bg-panel); border: 1px solid var(--border); border-radius: 12px; margin-bottom: 24px; }
.header-bar .icon { font-size: 24px; }
.header-bar .phase-name { font-family: var(--font-hud); font-size: 18px; font-weight: 600;
  color: var(--cyan); text-transform: uppercase; letter-spacing: 2px; }
.header-bar .feature-name { color: var(--text-dim); font-size: 14px; flex: 1; }
.header-bar .back-link { font-family: var(--font-hud); font-size: 11px; color: var(--cyan);
  text-decoration: none; padding: 4px 12px; border: 1px solid var(--border); border-radius: 6px; }
.content { background: var(--bg-panel); border: 1px solid var(--border); border-radius: 12px; padding: 32px; }
.content h1 { color: var(--cyan); font-size: 20px; margin: 24px 0 12px; }
.content h2 { color: var(--cyan); font-size: 16px; margin: 20px 0 10px; }
.content h3 { color: var(--green); font-size: 14px; margin: 16px 0 8px; }
.content p { font-size: 14px; line-height: 1.7; margin: 6px 0; }
.content pre { background: var(--bg-deep); border: 1px solid var(--border); border-radius: 8px;
  padding: 16px; font-family: var(--font-hud); font-size: 13px; overflow-x: auto; margin: 12px 0; }
.content code { font-family: var(--font-hud); font-size: 13px; color: var(--green); }
.content table { width: 100%; border-collapse: collapse; margin: 12px 0; }
.content th, .content td { padding: 8px 12px; border-bottom: 1px solid var(--border); text-align: left; font-size: 13px; }
.content th { color: var(--cyan); font-weight: 500; }
.content strong { color: #e8f0fe; }
.content blockquote { border-left: 3px solid var(--cyan); padding-left: 16px; color: var(--text-dim); font-style: italic; }
.content li { font-size: 14px; margin: 4px 0; padding-left: 8px; }
.mermaid { margin: 16px 0; }
</style>
</head>
<body>
<div class="header-bar">
  <span class="icon">{{PHASE_ICON}}</span>
  <span class="phase-name">{{PHASE_NAME}}</span>
  <span class="feature-name">{{FEATURE_NAME}}</span>
  <a class="back-link" href="nexus.html">&#x2190; Nexus Dashboard</a>
</div>
<div class="content">
{{CONTENT_HTML}}
</div>
<script>mermaid.initialize({ startOnLoad: true, theme: 'dark' });</script>
</body>
</html>'''


# ═══════════════════════════════════════════════════════════════════════════════
# GENERATION FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def generate_nexus(slug, db_path=None):
    out_dir = FEATURES_DIR / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    data = build_nexus_data(slug, db_path)
    json_path = out_dir / "nexus-data.json"
    with open(json_path, "w") as f:
        json.dump(data, f, indent=2)
    html = NEXUS_TEMPLATE
    html = html.replace("{{FEATURE_NAME}}", data["feature"]["name"])
    html = html.replace("{{NEXUS_DATA_JSON}}", json.dumps(data, indent=2))
    html_path = out_dir / "nexus.html"
    with open(html_path, "w") as f:
        f.write(html)
    print(json.dumps({
        "status": "ok", "nexus_html": str(html_path), "nexus_data": str(json_path),
        "feature": data["feature"]["name"],
        "phases": {k: v["status"] for k, v in data["phases"].items()},
        "experts": len(data["experts"]), "risks": len(data["risks"]),
    }, indent=2))
    return str(html_path)


def generate_phase(phase_id, slug, db_path=None):
    out_dir = FEATURES_DIR / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    data = build_nexus_data(slug, db_path)
    phase = data["phases"].get(phase_id, {})
    meta = None
    for p in PHASES:
        if p["id"] == phase_id:
            meta = p
            break
    if not meta:
        print(json.dumps({"error": f"Unknown phase: {phase_id}"}))
        return
    content_html = phase.get("content_html", "<p>No content generated yet.</p>")
    html = PHASE_TEMPLATE
    html = html.replace("{{PHASE_NAME}}", meta["name"])
    html = html.replace("{{PHASE_ICON}}", meta["icon"])
    html = html.replace("{{FEATURE_NAME}}", data["feature"]["name"])
    html = html.replace("{{CONTENT_HTML}}", content_html)
    filename_map = {
        "lens": "overview.html", "forge": "solution.html",
        "sentinel": "risk-analysis.html", "scribe": "spec.html",
        "welder": "implementation.html", "launch": "launch.html",
    }
    html_path = out_dir / filename_map.get(phase_id, f"{phase_id}.html")
    with open(html_path, "w") as f:
        f.write(html)
    print(json.dumps({"status": "ok", "path": str(html_path), "phase": phase_id}))
    return str(html_path)


def update_data(slug, phase_id, updates_json):
    json_path = FEATURES_DIR / slug / "nexus-data.json"
    if not json_path.exists():
        print(json.dumps({"error": "nexus-data.json not found. Run generate-nexus first."}))
        return
    with open(json_path) as f:
        data = json.load(f)
    updates = json.loads(updates_json) if isinstance(updates_json, str) else updates_json
    if phase_id in data["phases"]:
        data["phases"][phase_id].update(updates)
    else:
        data["phases"][phase_id] = updates
    with open(json_path, "w") as f:
        json.dump(data, f, indent=2)
    print(json.dumps({"status": "ok", "updated": phase_id}))


def persist_chat(slug, phase_id, message, role="user"):
    json_path = FEATURES_DIR / slug / "nexus-data.json"
    if not json_path.exists():
        print(json.dumps({"error": "nexus-data.json not found"}))
        return
    with open(json_path) as f:
        data = json.load(f)
    if phase_id not in data["phases"]:
        data["phases"][phase_id] = {}
    phase = data["phases"][phase_id]
    if "chat_history" not in phase:
        phase["chat_history"] = []
    phase["chat_history"].append({
        "role": role, "content": message,
        "ts": datetime.datetime.now().strftime("%H:%M:%S"),
    })
    with open(json_path, "w") as f:
        json.dump(data, f, indent=2)
    print(json.dumps({"status": "ok", "phase": phase_id, "messages": len(phase["chat_history"])}))


def add_variation(slug, phase_id):
    json_path = FEATURES_DIR / slug / "nexus-data.json"
    if not json_path.exists():
        print(json.dumps({"error": "nexus-data.json not found"}))
        return
    with open(json_path) as f:
        data = json.load(f)
    phase = data["phases"].get(phase_id, {})
    variations = phase.get("variations", {})
    next_num = len(variations) + 1
    key = f"v{next_num}"
    variations[key] = {"selected": False, "summary": f"Variation {next_num}"}
    phase["variations"] = variations
    data["phases"][phase_id] = phase
    var_dir = FEATURES_DIR / slug / "variations"
    var_dir.mkdir(parents=True, exist_ok=True)
    var_file = var_dir / f"{phase_id}-{key}.json"
    with open(var_file, "w") as f:
        json.dump(phase, f, indent=2)
    with open(json_path, "w") as f:
        json.dump(data, f, indent=2)
    print(json.dumps({"status": "ok", "variation": key, "path": str(var_file)}))


def generate_demo():
    slug = "_nexus-demo"
    out_dir = FEATURES_DIR / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    demo_data = {
        "feature": {
            "slug": slug, "name": "Instant Offer Discount",
            "created_at": datetime.datetime.now().isoformat(),
            "current_phase": "forge", "variation": "v1",
        },
        "phases": {
            "lens": {
                "status": "done", "name": "Lens", "role": "Feature Understanding", "icon": "\U0001f50d",
                "description": PHASE_DESCRIPTIONS["lens"],
                "artifacts": ["overview.md"],
                "summary": "5 services, 2 payment paths, 3 blockers identified",
                "content_html": "<h2>As-Is Flow</h2><p>The current payment flow processes offers through <code>pg-router → payments-card → api</code>. When both CFB and DFB discounts are applied simultaneously, the fee calculation uses the pre-discount amount instead of the post-discount amount.</p><h3>Blockers</h3><table><tr><th>Service</th><th>Issue</th><th>Severity</th></tr><tr><td>pg-router</td><td>Amount mismatch on discount+fee</td><td>BLOCKER</td></tr><tr><td>offers-engine</td><td>Missing instant discount type</td><td>HIGH</td></tr><tr><td>checkout-service</td><td>UI doesn't show combined discount</td><td>MEDIUM</td></tr></table><h2>To-Be Flow</h2><p>The corrected flow ensures <strong>payment_amount − fee + offer_discount = order_amount</strong> holds at every service boundary.</p>",
                "mermaid_diagrams": [],
                "experts_involved": ["Switchboard", "Precision"],
                "cortex_nodes_created": 24,
                "variations": {"v1": {"selected": True, "summary": "Original analysis"}, "v2": {"summary": "Added UPI path"}},
                "chat_history": [
                    {"role": "user", "content": "What about the UPI flow?", "ts": "14:23:05"},
                    {"role": "nexus", "content": "Good catch — UPI has a separate path through payments-upi that also needs the fee correction.", "ts": "14:23:08"},
                ],
            },
            "forge": {
                "status": "active", "name": "Forge", "role": "Solution Design", "icon": "\U0001f528",
                "description": PHASE_DESCRIPTIONS["forge"], "artifacts": [],
                "summary": "Designing code changes for 3 services...",
                "content_html": "<h2>Solution In Progress</h2><p>Tracing code paths through pg-router, offers-engine, and checkout-service...</p><h3>Changes Identified</h3><p><strong>C1</strong>: <code>pg-router/internal/handler/payment.go:142</code> — Fix fee calculation</p><p><strong>C2</strong>: <code>offers-engine/src/evaluator/instant.go</code> — Add instant discount handler</p>",
                "mermaid_diagrams": [], "experts_involved": ["Switchboard", "Precision", "Transmuter"],
                "cortex_nodes_created": 8,
                "variations": {"v1": {"selected": True, "summary": "Default"}}, "chat_history": [],
            },
            "sentinel": {"status":"pending","name":"Sentinel","role":"Risk Analysis","icon":"\U0001f6e1️","description":PHASE_DESCRIPTIONS["sentinel"],"artifacts":[],"summary":"","content_html":"","mermaid_diagrams":[],"experts_involved":[],"cortex_nodes_created":0,"variations":{"v1":{"selected":True,"summary":"Default"}},"chat_history":[]},
            "scribe": {"status":"pending","name":"Scribe","role":"Tech Spec Generation","icon":"\U0001f4dc","description":PHASE_DESCRIPTIONS["scribe"],"artifacts":[],"summary":"","content_html":"","mermaid_diagrams":[],"experts_involved":[],"cortex_nodes_created":0,"variations":{"v1":{"selected":True,"summary":"Default"}},"chat_history":[]},
            "welder": {"status":"pending","name":"Welder","role":"Implementation","icon":"⚡","description":PHASE_DESCRIPTIONS["welder"],"artifacts":[],"summary":"","content_html":"","mermaid_diagrams":[],"experts_involved":[],"cortex_nodes_created":0,"variations":{"v1":{"selected":True,"summary":"Default"}},"chat_history":[]},
            "launch": {"status":"pending","name":"Launch","role":"Run & Deploy","icon":"\U0001f680","description":PHASE_DESCRIPTIONS["launch"],"artifacts":[],"summary":"","content_html":"","mermaid_diagrams":[],"experts_involved":[],"cortex_nodes_created":0,"variations":{"v1":{"selected":True,"summary":"Default"}},"chat_history":[]},
        },
        "experts": [
            {"codename":"Switchboard","domain":"Gateway/routing","level":3,"xp":1800,"projects":["pg-router","edge"],"phases":["lens","forge"]},
            {"codename":"Precision","domain":"Payment core","level":2,"xp":700,"projects":["payments-card","payments-upi"],"phases":["forge"]},
            {"codename":"Transmuter","domain":"Offers/pricing","level":2,"xp":650,"projects":["offers-engine","optimizer-core"],"phases":["forge"]},
            {"codename":"Mirage","domain":"Frontend","level":2,"xp":520,"projects":["checkout","dashboard"],"phases":["lens"]},
            {"codename":"Monolith","domain":"PHP monolith","level":4,"xp":3200,"projects":["api"],"phases":["forge","sentinel"]},
        ],
        "cortex_stats": {
            "total_nodes":715424,"total_edges":732823,"functions":512000,"tests":112000,
            "endpoints":1400,"experts_total":46,"experts_involved":5,"decisions":47,"risks":3,
            "classes":36000,"modules":32000,"datastores":2600,
        },
        "risks": [
            {"id":"R1","severity":"blocker","phase":"sentinel","summary":"pg-router amount mismatch on discount+fee"},
            {"id":"R2","severity":"high","phase":"sentinel","summary":"offers-engine missing instant discount validation"},
            {"id":"R3","severity":"medium","phase":"sentinel","summary":"checkout UI doesn't show combined discount breakdown"},
        ],
        "generated_at": datetime.datetime.now().isoformat(),
    }
    json_path = out_dir / "nexus-data.json"
    with open(json_path, "w") as f:
        json.dump(demo_data, f, indent=2)
    html = NEXUS_TEMPLATE
    html = html.replace("{{FEATURE_NAME}}", demo_data["feature"]["name"])
    html = html.replace("{{NEXUS_DATA_JSON}}", json.dumps(demo_data, indent=2))
    html_path = out_dir / "nexus.html"
    with open(html_path, "w") as f:
        f.write(html)
    print(json.dumps({
        "status": "ok", "mode": "demo",
        "nexus_html": str(html_path), "nexus_data": str(json_path),
        "open_with": f"open {html_path}",
    }, indent=2))
    return str(html_path)


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Nexus Jarvis UI generator")
    sub = parser.add_subparsers(dest="command")
    p1 = sub.add_parser("generate-nexus", help="Generate nexus.html dashboard")
    p1.add_argument("feature", help="Feature slug")
    p1.add_argument("--db", default=str(DB_PATH), help="Database path")
    p2 = sub.add_parser("generate-phase", help="Generate standalone phase HTML")
    p2.add_argument("phase", choices=[p["id"] for p in PHASES], help="Phase ID")
    p2.add_argument("feature", help="Feature slug")
    p2.add_argument("--db", default=str(DB_PATH), help="Database path")
    p3 = sub.add_parser("update-data", help="Update nexus-data.json")
    p3.add_argument("feature", help="Feature slug")
    p3.add_argument("phase", help="Phase ID")
    p3.add_argument("json_data", help="JSON updates")
    p4 = sub.add_parser("persist-chat", help="Add chat message")
    p4.add_argument("feature", help="Feature slug")
    p4.add_argument("phase", help="Phase ID")
    p4.add_argument("message", help="Chat message")
    p4.add_argument("--role", default="user", help="user or nexus")
    p5 = sub.add_parser("add-variation", help="Create new variation")
    p5.add_argument("feature", help="Feature slug")
    p5.add_argument("phase", help="Phase ID")
    p6 = sub.add_parser("demo", help="Generate demo dashboard with sample data")
    args = parser.parse_args()
    if args.command == "generate-nexus":
        generate_nexus(args.feature, args.db)
    elif args.command == "generate-phase":
        generate_phase(args.phase, args.feature, args.db)
    elif args.command == "update-data":
        update_data(args.feature, args.phase, args.json_data)
    elif args.command == "persist-chat":
        persist_chat(args.feature, args.phase, args.message, args.role)
    elif args.command == "add-variation":
        add_variation(args.feature, args.phase)
    elif args.command == "demo":
        generate_demo()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
