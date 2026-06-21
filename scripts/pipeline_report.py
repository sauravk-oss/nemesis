#!/usr/bin/env python3
"""Pipeline Report — render a single self-contained HTML showing a feature's
*entire* AI pipeline as an Argo-Workflows-style interactive node graph.

What you get (one file, opens in any browser, no network needed for structure):

  * A light-theme DAG canvas. A vertical spine runs top-to-bottom:
        root  →  Ideation  →  Solutioning  →  Tech Spec  →  Implementation  →  E2E
    Each phase fans its children out to the right:
        skill invocations (circles, tier-coloured) · iterations (blue) ·
        documents (white "artifact" cards) · an Open-Questions node (amber if any open).
    The root fans out to a Brain-Knowledge node (+ an Archive node if archives exist).
  * Status-coloured circular nodes (green = succeeded, blue = running, grey = pending,
    dashed = deferred/skipped) exactly like the Argo UI.
  * A sub-toolbar: zoom −/+/fit, a node search box, and filter chips
    (All / Phases / Skills / Iterations / Docs / Questions).
  * Click ANY node → a right sidebar slides in with SUMMARY and DETAILS tabs.
    - SUMMARY = the key facts for that node.
    - DETAILS = the deep content: for a document node the doc is embedded in full
      (Markdown rendered inline; .html artifacts via <iframe srcdoc>); for a phase it's
      the skills/iterations/docs/open-questions tables; for the root it's the brain panel.

Pure Python, stdlib only. It NEVER calls an MCP. It MAY read brain.db through
`brain.api.BrainAPI` (that is a local DB read, not an MCP call) and degrades silently
to a files-only report if brain isn't importable.

Subcommands
-----------
  build --feature <slug> [--manifest <pipeline.json>] [--out <path>]
        [--drive-url <url>] [--title <str>] [--no-brain]
        -> writes workspace/features/<slug>/pipeline-report.html, prints summary JSON

Pipeline manifest (optional; assembled by the LLM to narrate skills + iterations):
{
  "feature": "gpay-bifrost-account-matching",
  "title":   "GPay Bifrost Account Matching for UPI TPV",
  "devrev":  "ENH-18653",
  "brain_feature": "GPay Bifrost Account Matching for UPI TPV",  # name in brain.db
  "drive_url": "https://drive.google.com/drive/folders/...",
  "service": "payments-upi (Go)", "branch": "feat/...", "commit": "240de646",
  "anchor_merchant": "WintWealth",
  "phases": [
    {
      "name": "Ideation", "status": "complete",
      "summary": "framed the SEBI TPV problem",
      "docs": ["overview.md", "overview.html"],          # optional; auto-discovered if omitted
      "skills": [
        {"skill":"product-management:brainstorm","tier":"skill",
         "input":"2 Slack threads + PRD","output":"problem statement + 5 user stories",
         "summary":"framed the SEBI TPV problem"}
      ],
      "iterations": [
        {"n":1,"input":"raw sources","output":"overview-v2.md","note":"first As-Is/To-Be"}
      ],
      "open_questions": [
        {"q":"Google API credentials?","status":"open","resolution":"RZP enrollment in progress"}
      ]
    }
  ]
}

If no manifest is given, the report still renders from whatever is on disk
(docs + archive + embedded test/change reports + brain panel).
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from brain.config import BrainConfig

    _WORKSPACE = Path(BrainConfig().workspace)
except Exception:  # pragma: no cover
    _WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"

FEATURES = _WORKSPACE / "features"

# How top-level docs map onto pipeline phases (for auto-discovery).
PHASE_ORDER = ["Ideation", "Solutioning", "Tech Spec", "Implementation", "Dev Testing", "E2E"]
_DOC_PHASE = [
    ("overview", "Ideation"),
    ("blocker", "Solutioning"),
    ("open-question", "Solutioning"),
    ("solution", "Solutioning"),
    ("minimal-change", "Tech Spec"),
    ("db-migration", "Tech Spec"),
    ("tech-spec", "Tech Spec"),
    ("techspec", "Tech Spec"),
    ("hld", "Tech Spec"),
    ("change-report", "Implementation"),
    ("test-report", "Implementation"),
    ("implementation", "Implementation"),
    ("deploy", "Implementation"),
    ("dev-test", "Dev Testing"),
    ("devtest", "Dev Testing"),
    ("e2e", "E2E"),
    ("scenario", "E2E"),
]

# Argo-style status map: manifest status -> (label, css color class, glyph)
_STATUS = {
    "complete":    ("Succeeded", "c-ok",   "✓"),
    "succeeded":   ("Succeeded", "c-ok",   "✓"),
    "done":        ("Succeeded", "c-ok",   "✓"),
    "in_progress": ("Running",   "c-run",  "▶"),
    "running":     ("Running",   "c-run",  "▶"),
    "active":      ("Running",   "c-run",  "▶"),
    "pending":     ("Pending",   "c-pend", ""),
    "proposed":    ("Pending",   "c-pend", ""),
    "deferred":    ("Deferred",  "c-skip", ""),
    "skipped":     ("Skipped",   "c-skip", ""),
}

# Skill fallback tier -> (css color class, glyph)
_TIER = {
    "skill": ("c-ok",    "✓"),
    "brain": ("c-info",  "◆"),
    "slash": ("c-warn",  "◆"),
    "none":  ("c-error", "!"),
}

# node kind -> (half-width, half-height) in px
_SIZE = {
    "root":      (18, 18),
    "brain":     (13, 13),
    "phase":     (15, 15),
    "subpipe":   (14, 14),
    "source":    (11, 13),
    "skill":     (11, 11),
    "iter":      (10, 10),
    "questions": (12, 12),
    "doc":       (12, 15),
    "archive":   (12, 12),
}

# Layout geometry (top-to-bottom spine; children fan right; grandchildren fan
# further right under sub-pipelines). Generous row height keeps the tree tall
# and readable even with archived-version nodes hanging off every phase.
COL_SPINE = 120
COL_CHILD = 470
COL_GRAND = 900
ROW_H = 74
BAND_GAP = 48
TOP = 72
CANVAS_W = 1580


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _phase_for(filename: str) -> str:
    low = filename.lower()
    for key, phase in _DOC_PHASE:
        if key in low:
            return phase
    return "Ideation"


# ----------------------------------------------------------------------------
# Compact, safe Markdown -> HTML (escape first, then a small grammar).
# Handles: ATX headings, fenced code, pipe tables, bullet/numbered lists,
# blockquotes, **bold**, `inline code`, and paragraphs. Good enough to make
# the embedded docs genuinely readable without any third-party dependency.
# ----------------------------------------------------------------------------
def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)",
                  r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)
    return text


def md_to_html(md: str) -> str:
    lines = md.replace("\r\n", "\n").split("\n")
    out: list = []
    i = 0
    n = len(lines)
    in_code = False
    code_buf: list = []

    def flush_list(buf, ordered):
        if not buf:
            return
        tag = "ol" if ordered else "ul"
        out.append(f"<{tag}>")
        out.extend(f"<li>{_inline(x)}</li>" for x in buf)
        out.append(f"</{tag}>")

    list_buf: list = []
    list_ordered = False

    while i < n:
        ln = lines[i]

        # fenced code
        if ln.strip().startswith("```"):
            if in_code:
                out.append("<pre class='code'>" + html.escape("\n".join(code_buf)) + "</pre>")
                code_buf = []
                in_code = False
            else:
                flush_list(list_buf, list_ordered); list_buf = []
                in_code = True
            i += 1
            continue
        if in_code:
            code_buf.append(ln)
            i += 1
            continue

        # table: a header row followed by a |---| separator
        if "|" in ln and i + 1 < n and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]) and "-" in lines[i + 1]:
            flush_list(list_buf, list_ordered); list_buf = []
            header = [c.strip() for c in ln.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(h)}</th>" for h in header) +
                       "</tr></thead><tbody>")
            for r in rows:
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
            out.append("</tbody></table>")
            continue

        # headings
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            flush_list(list_buf, list_ordered); list_buf = []
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(m.group(2).strip())}</h{lvl}>")
            i += 1
            continue

        # blockquote
        if ln.strip().startswith(">"):
            flush_list(list_buf, list_ordered); list_buf = []
            out.append(f"<blockquote>{_inline(ln.strip()[1:].strip())}</blockquote>")
            i += 1
            continue

        # lists
        mb = re.match(r"^\s*[-*+]\s+(.*)$", ln)
        mo = re.match(r"^\s*\d+\.\s+(.*)$", ln)
        if mb or mo:
            ordered = bool(mo)
            if list_buf and ordered != list_ordered:
                flush_list(list_buf, list_ordered); list_buf = []
            list_ordered = ordered
            list_buf.append((mo or mb).group(1))
            i += 1
            continue

        # blank line / paragraph
        if not ln.strip():
            flush_list(list_buf, list_ordered); list_buf = []
            i += 1
            continue

        flush_list(list_buf, list_ordered); list_buf = []
        out.append(f"<p>{_inline(ln.strip())}</p>")
        i += 1

    if in_code:
        out.append("<pre class='code'>" + html.escape("\n".join(code_buf)) + "</pre>")
    flush_list(list_buf, list_ordered)
    return "\n".join(out)


# ----------------------------------------------------------------------------
# Small render helpers
# ----------------------------------------------------------------------------
_BADGE_KIND = {"ok": "c-ok", "warn": "c-warn", "crit": "c-error",
               "info": "c-info", "run": "c-run", "skip": "c-skip"}


def _badge(text: str, kind: str = "") -> str:
    cls = _BADGE_KIND.get(kind, "")
    return f'<span class="badge {cls}">{html.escape(str(text))}</span>'


def _embed_doc(path: Path) -> str:
    """Embed a doc in full. .md is rendered; .html goes in an iframe srcdoc;
    anything else lands in a <pre>."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # pragma: no cover
        return f"<p class='muted'>could not read {html.escape(path.name)}: {html.escape(str(e))}</p>"
    suffix = path.suffix.lower()
    size = f"{len(raw)//1024} KB" if len(raw) >= 1024 else f"{len(raw)} B"
    head = f"<p class='muted'>{html.escape(str(path))} &middot; {size}</p>"
    if suffix in (".md", ".markdown", ".txt"):
        return head + "<div class='doc'>" + md_to_html(raw) + "</div>"
    if suffix in (".html", ".htm"):
        srcdoc = html.escape(raw, quote=True)
        return (head + f'<iframe class="htmlframe" srcdoc="{srcdoc}" '
                'sandbox="allow-same-origin" loading="lazy"></iframe>')
    if suffix == ".json":
        return head + "<pre class='code'>" + html.escape(raw) + "</pre>"
    return head + "<pre class='code'>" + html.escape(raw[:20000]) + "</pre>"


def _oq_node(oq: list) -> str:
    rows = ["<table><thead><tr><th>#</th><th>Question</th><th>Status</th>"
            "<th>Resolution / assumption</th></tr></thead><tbody>"]
    for i, q in enumerate(oq, 1):
        st = (q.get("status") or "open").lower()
        kind = "ok" if st in ("resolved", "closed") else "crit"
        rows.append(f"<tr><td>{i}</td><td>{_inline(q.get('q',''))}</td>"
                    f"<td>{_badge(st, kind)}</td>"
                    f"<td>{_inline(q.get('resolution') or q.get('assumption') or '-')}</td></tr>")
    rows.append("</tbody></table>")
    return "".join(rows)


def _skills_table(skills: list) -> str:
    r = ["<table><thead><tr><th>Skill</th><th>Tier</th><th>Input</th><th>Output</th>"
         "</tr></thead><tbody>"]
    for s in skills:
        r.append(f"<tr><td>{_inline(s.get('skill','?'))}</td>"
                 f"<td>{_badge(s.get('tier','skill'))}</td>"
                 f"<td>{_inline(str(s.get('input','-')))}</td>"
                 f"<td>{_inline(str(s.get('output','-')))}</td></tr>")
    r.append("</tbody></table>")
    return "".join(r)


def _iters_table(iters: list) -> str:
    r = ["<table><thead><tr><th>#</th><th>Note</th><th>Input</th><th>Output</th>"
         "</tr></thead><tbody>"]
    for it in iters:
        r.append(f"<tr><td>{_inline(str(it.get('n','?')))}</td>"
                 f"<td>{_inline(str(it.get('note','-')))}</td>"
                 f"<td>{_inline(str(it.get('input','-')))}</td>"
                 f"<td>{_inline(str(it.get('output','-')))}</td></tr>")
    r.append("</tbody></table>")
    return "".join(r)


# ----------------------------------------------------------------------------
# Brain enrichment (local DB read; degrades to {} on any failure).
# ----------------------------------------------------------------------------
def brain_enrich(brain_feature: str) -> dict:
    try:
        from brain.api import BrainAPI
    except Exception:
        return {}
    data: dict = {}
    try:
        b = BrainAPI()
        try:
            data["stats"] = b.stats()
        except Exception:
            pass
        if brain_feature:
            try:
                data["feature_health"] = b.feature_health(brain_feature)
            except Exception:
                pass
            for ntype in ("Requirement", "RiskItem", "ArchDecision", "Signal"):
                try:
                    hits = b.search(brain_feature, ntype=ntype, limit=50)
                    data.setdefault("nodes", {})[ntype] = len(hits or [])
                except Exception:
                    pass
        b.close()
    except Exception:
        return data
    return data


def _brain_panel(brain: dict) -> str:
    if not brain:
        return ("<p class='muted'>brain.db not queried (run with brain importable to "
                "power this panel — every code/file context lives there).</p>")
    parts = []
    fh = brain.get("feature_health") or {}
    if fh:
        parts.append("<table><tbody>")
        for k in ("name", "status", "owner", "requirements", "risks", "tasks", "confidence"):
            if k in fh:
                parts.append(f"<tr><th>{k}</th><td>{_inline(str(fh[k]))}</td></tr>")
        parts.append("</tbody></table>")
    nodes = brain.get("nodes") or {}
    if nodes:
        parts.append("<p><strong>Feature-linked nodes:</strong> " +
                     " &middot; ".join(f"{k} {v}" for k, v in nodes.items()) + "</p>")
    st = brain.get("stats") or {}
    g = st.get("graph") if isinstance(st, dict) else None
    if g:
        parts.append(f"<p class='muted'>Brain totals: {g.get('nodes','?')} nodes &middot; "
                     f"{g.get('edges','?')} edges &middot; {g.get('services','?')} services &middot; "
                     f"{g.get('code_bodies','?')} code bodies</p>")
    return "\n".join(parts) or "<p class='muted'>no brain data</p>"


# ----------------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------------
_SKIP_DIRS = {"archive", "_archive"}


def _is_report_file(name: str) -> bool:
    """Never let the report embed a prior copy of itself."""
    return name.lower().startswith("pipeline-report")


def discover_docs(fdir: Path) -> dict:
    """Return {phase: [Path,...]} for top-level docs, sorted sensibly."""
    by_phase: dict = {p: [] for p in PHASE_ORDER}
    if not fdir.exists():
        return by_phase
    for p in sorted(fdir.iterdir()):
        if p.is_dir():
            continue
        if p.suffix.lower() not in (".md", ".html", ".htm", ".json", ".txt"):
            continue
        if p.name.startswith(".") or _is_report_file(p.name):
            continue
        by_phase[_phase_for(p.name)].append(p)
    # implementation/ subdir docs
    impl = fdir / "implementation"
    if impl.exists():
        for p in sorted(impl.rglob("*")):
            if p.is_file() and p.suffix.lower() in (".md", ".html", ".json") and not _is_report_file(p.name):
                by_phase["Implementation"].append(p)
    return by_phase


def discover_archive(fdir: Path) -> list:
    items = []
    for d in _SKIP_DIRS:
        ad = fdir / d
        if ad.exists():
            for p in sorted(ad.iterdir()):
                if p.is_file() and not _is_report_file(p.name):
                    items.append(p)
    return items


# ----------------------------------------------------------------------------
# Assembly — build the node graph (nodes + edges + per-node panels), lay it out
# top-to-bottom (spine) with children fanning right, and render the Argo page.
# ----------------------------------------------------------------------------
def build_report(slug: str, manifest: dict, drive_url: str = None, title: str = None,
                 use_brain: bool = True, out_path: Path = None) -> dict:
    fdir = FEATURES / slug
    manifest = manifest or {}
    title = title or manifest.get("title") or slug
    devrev = manifest.get("devrev", "")
    drive_url = drive_url or manifest.get("drive_url", "")
    brain_feature = manifest.get("brain_feature") or title
    service = manifest.get("service", "")
    branch = manifest.get("branch", "")
    commit = manifest.get("commit", "")
    anchor = manifest.get("anchor_merchant", "")

    brain = brain_enrich(brain_feature) if use_brain else {}

    man_phases = {p.get("name", ""): p for p in (manifest.get("phases") or [])}
    docs_by_phase = discover_docs(fdir)
    phase_names = list(PHASE_ORDER) + [p for p in man_phases if p not in PHASE_ORDER]

    panels: dict = {}
    counts = {"docs": 0, "skills": 0, "iterations": 0, "open_questions": 0,
              "subpipelines": 0, "archived": 0}

    def mk(nid, kind, ntitle, nsub, glyph, cls, su, de, search=None):
        panels[nid] = {"title": ntitle, "sub": nsub, "su": su, "de": de}
        return {"id": nid, "kind": kind, "title": ntitle, "sub": nsub,
                "glyph": glyph, "cls": cls,
                "search": (search if search is not None else f"{ntitle} {nsub}").lower()}

    def kv(k, v):
        return f"<tr><th>{html.escape(k)}</th><td>{_inline(str(v))}</td></tr>" if v else ""

    # ---- shared sources (Slack threads / PRD / DevRev) fan off the root next to Brain ----
    _SRC_GLYPH = {"slack": "S", "chat": "S", "doc": "D", "drive": "D", "prd": "D",
                  "github": "G", "pr": "G", "devrev": "T", "ticket": "T", "gmail": "M"}
    source_nodes: list = []
    for si, src in enumerate(manifest.get("sources") or []):
        skind = (src.get("kind") or "doc").lower()
        glyph = _SRC_GLYPH.get(skind, "•")
        stitle = src.get("title") or src.get("ref") or f"source {si + 1}"
        ref = src.get("ref") or ""
        ssu = "<table><tbody>" + kv("Kind", skind) + kv("Source", stitle)
        if ref and str(ref).startswith("http"):
            ssu += (f'<tr><th>Ref</th><td><a href="{html.escape(str(ref), quote=True)}" '
                    f'target="_blank" rel="noopener">{html.escape(str(ref))}</a></td></tr>')
        elif ref:
            ssu += kv("Ref", ref)
        ssu += "</tbody></table>"
        if src.get("summary"):
            ssu += f"<p>{_inline(str(src['summary']))}</p>"
        sde = f"<p>{_inline(str(src.get('summary') or 'Ingested into brain.db as a source node.'))}</p>"
        if ref and str(ref).startswith("http"):
            sde += (f'<p><a href="{html.escape(str(ref), quote=True)}" target="_blank" '
                    f'rel="noopener">Open source &#8599;</a></p>')
        source_nodes.append(mk(f"src-{si}", "source", stitle, skind, glyph, "c-src",
                               ssu, sde, search=f"source {skind} {stitle} {ref}"))

    # ---- archived files: routed to their phase as superseded doc-nodes ----
    archive_files = discover_archive(fdir)

    # ---- phases -> spine bands ----
    phase_bands: list = []  # [(phase_node, [child_node,...]), ...]
    any_running = False
    for idx, name in enumerate(phase_names):
        mp = man_phases.get(name, {})

        docs: list = []
        seen = set()
        for d in (mp.get("docs") or []):
            cand = fdir / d
            if cand.exists() and cand.name not in seen and not _is_report_file(cand.name):
                docs.append(cand); seen.add(cand.name)
        for p in docs_by_phase.get(name, []):
            if p.name not in seen:
                docs.append(p); seen.add(p.name)

        skills = mp.get("skills") or []
        iters = mp.get("iterations") or []
        oqs = mp.get("open_questions") or []
        subs = mp.get("subpipelines") or []
        arch_docs = [p for p in archive_files if _phase_for(p.name) == name]
        status = (mp.get("status") or ("complete" if docs else "pending")).lower()

        # Render a band when it has content OR is an explicitly declared phase, so
        # pending Dev Testing / E2E phases still appear ("keep them open").
        if not (docs or skills or iters or oqs or subs or arch_docs) and name not in man_phases:
            continue

        counts["docs"] += len(docs)
        counts["skills"] += len(skills)
        counts["iterations"] += len(iters)
        counts["open_questions"] += len(oqs)
        counts["subpipelines"] += len(subs)
        counts["archived"] += len(arch_docs)
        if status in ("in_progress", "running", "active"):
            any_running = True

        n_open = sum(1 for q in oqs if (q.get("status") or "open").lower() not in ("resolved", "closed"))

        children: list = []

        for k, s in enumerate(skills):
            tier = (s.get("tier") or "skill").lower()
            tcls, tg = _TIER.get(tier, ("c-ok", "✓"))
            su = ("<table><tbody>"
                  + kv("Skill", s.get("skill", "?")) + kv("Tier", tier) + kv("Phase", name)
                  + "</tbody></table>")
            if s.get("summary"):
                su += f"<p>{_inline(str(s['summary']))}</p>"
            de = ""
            if s.get("input"):
                de += f"<p><strong>Input</strong><br>{_inline(str(s['input']))}</p>"
            if s.get("output"):
                de += f"<p><strong>Output</strong><br>{_inline(str(s['output']))}</p>"
            de = de or "<p class='muted'>no input/output recorded</p>"
            children.append(mk(f"sk-{idx}-{k}", "skill", s.get("skill", "?"), tier, tg, tcls,
                               su, de, search=f"{s.get('skill','')} {tier} {s.get('summary','')}"))

        for k, it in enumerate(iters):
            nn = it.get("n", k + 1)
            su = "<table><tbody>" + kv("Iteration", nn) + kv("Phase", name) + "</tbody></table>"
            if it.get("note"):
                su += f"<p>{_inline(str(it['note']))}</p>"
            de = ""
            if it.get("input"):
                de += f"<p><strong>Input</strong><br>{_inline(str(it['input']))}</p>"
            if it.get("output"):
                de += f"<p><strong>Output</strong><br>{_inline(str(it['output']))}</p>"
            de = de or "<p class='muted'>no input/output recorded</p>"
            children.append(mk(f"it-{idx}-{k}", "iter", f"Iteration {nn}",
                               str(it.get("note", ""))[:48], str(nn), "c-run",
                               su, de, search=f"iteration {nn} {it.get('note','')}"))

        for k, p in enumerate(docs):
            try:
                sz = p.stat().st_size
            except Exception:
                sz = 0
            size = f"{sz//1024} KB" if sz >= 1024 else f"{sz} B"
            typ = p.suffix.lstrip(".")
            su = ("<table><tbody>"
                  + kv("File", p.name) + kv("Type", typ) + kv("Size", size) + kv("Phase", name)
                  + "</tbody></table>")
            children.append(mk(f"dc-{idx}-{k}", "doc", p.name, f"{typ} &middot; {size}",
                               "", "c-doc", su, _embed_doc(p), search=f"{p.name} {typ}"))

        # sub-pipelines: payments-upi + mozart run in sync under Implementation.
        # Each is a child node whose own docs + skills hang off it as grandchildren.
        for si2, sp in enumerate(subs):
            sp_name = sp.get("name") or sp.get("repo") or f"sub {si2 + 1}"
            sp_status = (sp.get("status") or "pending").lower()
            sl, scl, sgl = _STATUS.get(sp_status, ("Pending", "c-pend", ""))
            psu = ("<table><tbody>"
                   + kv("Sub-pipeline", sp_name) + kv("Repo", sp.get("repo"))
                   + kv("Status", sl) + kv("Branch", sp.get("branch")) + kv("Commit", sp.get("commit")))
            pr = sp.get("pr")
            if pr and str(pr).startswith("http"):
                psu += (f'<tr><th>PR</th><td><a href="{html.escape(str(pr), quote=True)}" '
                        f'target="_blank" rel="noopener">{html.escape(str(pr))}</a></td></tr>')
            elif pr:
                psu += kv("PR", pr)
            psu += "</tbody></table>"
            if sp.get("summary"):
                psu += f"<p>{_inline(str(sp['summary']))}</p>"

            grand: list = []
            sp_skills = sp.get("skills") or []
            for gk, s in enumerate(sp_skills):
                tier = (s.get("tier") or "skill").lower()
                tcls, tg = _TIER.get(tier, ("c-ok", "✓"))
                gsu = ("<table><tbody>" + kv("Skill", s.get("skill", "?")) + kv("Tier", tier)
                       + kv("Sub-pipeline", sp_name) + "</tbody></table>")
                if s.get("summary"):
                    gsu += f"<p>{_inline(str(s['summary']))}</p>"
                gde = ""
                if s.get("input"):
                    gde += f"<p><strong>Input</strong><br>{_inline(str(s['input']))}</p>"
                if s.get("output"):
                    gde += f"<p><strong>Output</strong><br>{_inline(str(s['output']))}</p>"
                grand.append(mk(f"sp-{idx}-{si2}-sk{gk}", "skill", s.get("skill", "?"), tier,
                                tg, tcls, gsu, gde or "<p class='muted'>—</p>",
                                search=f"{sp_name} {s.get('skill', '')} {tier}"))
            for gk, d in enumerate(sp.get("docs") or []):
                cand = (fdir / d) if isinstance(d, str) else None
                if cand and cand.exists() and not _is_report_file(cand.name):
                    try:
                        sz = cand.stat().st_size
                    except Exception:
                        sz = 0
                    size = f"{sz // 1024} KB" if sz >= 1024 else f"{sz} B"
                    typ = cand.suffix.lstrip(".")
                    gsu = ("<table><tbody>" + kv("File", cand.name) + kv("Type", typ)
                           + kv("Size", size) + kv("Sub-pipeline", sp_name) + "</tbody></table>")
                    grand.append(mk(f"sp-{idx}-{si2}-dc{gk}", "doc", cand.name,
                                    f"{typ} &middot; {size}", "", "c-doc", gsu, _embed_doc(cand),
                                    search=f"{sp_name} {cand.name}"))
                else:
                    label = d if isinstance(d, str) else (d.get("name") if isinstance(d, dict) else str(d))
                    note = d.get("note") if isinstance(d, dict) else ""
                    gsu = ("<table><tbody>" + kv("Artifact", label) + kv("Sub-pipeline", sp_name)
                           + "</tbody></table>")
                    grand.append(mk(f"sp-{idx}-{si2}-dc{gk}", "doc", str(label), "artifact",
                                    "", "c-doc", gsu,
                                    f"<p>{_inline(str(note) or 'Artifact tracked in the sub-pipeline (not embedded).')}</p>",
                                    search=f"{sp_name} {label}"))
            pde = []
            if sp_skills:
                pde.append("<h4>Skills</h4>" + _skills_table(sp_skills))
            if sp.get("changes"):
                pde.append("<h4>Changes</h4><ul>"
                           + "".join(f"<li>{_inline(str(c))}</li>" for c in sp["changes"]) + "</ul>")
            sp_nd = mk(f"sp-{idx}-{si2}", "subpipe", sp_name, sl, sgl or "", scl,
                       psu, "\n".join(pde) or "<p class='muted'>see grandchild nodes</p>",
                       search=f"subpipeline {sp_name} {sp.get('repo', '')} {sl}")
            sp_nd["_grand"] = grand
            children.append(sp_nd)

        # archived (superseded) iterations: dimmed doc-nodes under their phase.
        for ak, p in enumerate(arch_docs):
            parent = p.parent.name
            try:
                sz = p.stat().st_size
            except Exception:
                sz = 0
            size = f"{sz // 1024} KB" if sz >= 1024 else f"{sz} B"
            typ = p.suffix.lstrip(".")
            asu = ("<table><tbody>" + kv("File", f"{parent}/{p.name}") + kv("Type", typ)
                   + kv("Size", size) + kv("Phase", name)
                   + "<tr><th>State</th><td>superseded (archived)</td></tr></tbody></table>")
            children.append(mk(f"ar-{idx}-{ak}", "doc", p.name, f"archived &middot; {typ}",
                               "", "c-doc arch", asu, _embed_doc(p),
                               search=f"archive {parent} {p.name} {typ} superseded"))

        if oqs:
            qcls = "c-warn" if n_open else "c-ok"
            su = (f"<p>{_badge(str(n_open) + ' open', 'warn' if n_open else 'ok')}"
                  f"{_badge(str(len(oqs)) + ' total')}</p>")
            children.append(mk(f"oq-{idx}", "questions", "Open Questions",
                               f"{n_open} open / {len(oqs)}", "?", qcls, su, _oq_node(oqs),
                               search="open questions " + " ".join(q.get('q', '') for q in oqs)))

        slabel, scls, sg = _STATUS.get(status, ("Pending", "c-pend", ""))
        psu = (f"<p>{_badge(slabel, {'c-ok':'ok','c-run':'run','c-pend':'','c-skip':'skip'}.get(scls,''))}</p>"
               f"<p class='muted'>{len(docs)} docs &middot; {len(skills)} skills &middot; "
               f"{len(iters)} iterations &middot; {n_open} open questions</p>")
        if mp.get("summary"):
            psu += f"<p>{_inline(str(mp['summary']))}</p>"
        if mp.get("notes"):
            psu += f"<p>{_inline(str(mp['notes']))}</p>"
        pde = []
        if skills:
            pde.append("<h4>Skills</h4>" + _skills_table(skills))
        if iters:
            pde.append("<h4>Iterations</h4>" + _iters_table(iters))
        if docs:
            pde.append("<h4>Documents</h4><ul>" + "".join(f"<li>{_inline(p.name)}</li>" for p in docs) + "</ul>")
        if oqs:
            pde.append("<h4>Open Questions</h4>" + _oq_node(oqs))
        phase_nd = mk(f"ph-{idx}", "phase", name, slabel, sg or "", scls,
                      psu, "\n".join(pde) or "<p class='muted'>no details</p>",
                      search=f"{name} {slabel}")
        phase_bands.append((phase_nd, children))

    # ---- root + brain + archive ----
    overall = ("Running", "c-run") if any_running else ("Succeeded", "c-ok")
    rsu = ("<table><tbody>"
           + kv("Feature", title) + kv("Slug", slug) + kv("DevRev", devrev)
           + kv("Service", service) + kv("Branch", branch) + kv("Commit", commit)
           + kv("Anchor merchant", anchor) + kv("Generated", _now_iso())
           + "</tbody></table>"
           + f"<p class='muted'>{len(phase_bands)} phases &middot; {len(source_nodes)} sources &middot; "
           f"{counts['docs']} docs &middot; {counts['subpipelines']} sub-pipelines &middot; "
           f"{counts['skills']} skill invocations &middot; {counts['iterations']} iterations &middot; "
           f"{counts['archived']} archived &middot; {counts['open_questions']} open questions</p>")
    root_nd = mk("root", "root", title, (service or devrev or "AI pipeline"),
                 "★", "c-root", rsu, _brain_panel(brain), search=f"{title} {slug} {devrev}")

    fh = (brain or {}).get("feature_health") or {}
    if brain:
        bsu = "<p>brain.db was queried for this feature (local DB read — no MCP).</p>"
        if fh:
            bsu += "<table><tbody>" + "".join(
                kv(k, fh[k]) for k in ("name", "status", "confidence", "requirements", "risks") if k in fh
            ) + "</tbody></table>"
    else:
        bsu = ("<p class='muted'>brain.db not queried. Run with the brain package importable "
               "to power this panel.</p>")
    brain_nd = mk("brain", "brain", "Brain Knowledge", "brain.db", "◆", "c-info",
                  bsu, _brain_panel(brain), search="brain knowledge brain.db context")

    # ---- layout (spine top→down; children fan right; grandchildren fan further) ----
    band0_children = [brain_nd] + source_nodes
    bands = [(root_nd, band0_children)] + phase_bands

    def _slot_rows(ch):
        return max(len(ch.get("_grand") or []), 1)

    pos: dict = {}
    y = TOP
    for spine_nd, children in bands:
        band_top = y
        total_rows = sum(_slot_rows(c) for c in children) if children else 1
        row_cursor = band_top
        for ch in children:
            rows = _slot_rows(ch)
            slot_top = row_cursor
            pos[ch["id"]] = (COL_CHILD, slot_top + (rows * ROW_H) / 2)
            for gj, g in enumerate(ch.get("_grand") or []):
                pos[g["id"]] = (COL_GRAND, slot_top + gj * ROW_H + ROW_H / 2)
            row_cursor += rows * ROW_H
        pos[spine_nd["id"]] = (COL_SPINE, band_top + (total_rows * ROW_H) / 2)
        y = band_top + total_rows * ROW_H + BAND_GAP
    W = CANVAS_W
    H = int(y + 20)

    ordered = [root_nd, brain_nd] + source_nodes
    for spine_nd, children in phase_bands:
        ordered.append(spine_nd)
        for ch in children:
            ordered.append(ch)
            ordered.extend(ch.get("_grand") or [])

    # ---- edges ----
    spine_ids = [b[0]["id"] for b in bands]
    edge_paths = []
    for a, b in zip(spine_ids, spine_ids[1:]):
        x1, y1 = pos[a]; x2, y2 = pos[b]
        edge_paths.append(f'<path class="edge spine" d="M {x1:.1f} {y1:.1f} L {x2:.1f} {y2:.1f}"/>')
    for spine_nd, children in bands:
        sx, sy = pos[spine_nd["id"]]
        for ch in children:
            cx, cy = pos[ch["id"]]
            mx = (sx + cx) / 2
            edge_paths.append(f'<path class="edge" d="M {sx:.1f} {sy:.1f} '
                              f'C {mx:.1f} {sy:.1f} {mx:.1f} {cy:.1f} {cx:.1f} {cy:.1f}"/>')
            for g in ch.get("_grand") or []:
                gx, gy = pos[g["id"]]
                gmx = (cx + gx) / 2
                edge_paths.append(f'<path class="edge" d="M {cx:.1f} {cy:.1f} '
                                  f'C {gmx:.1f} {cy:.1f} {gmx:.1f} {gy:.1f} {gx:.1f} {gy:.1f}"/>')

    # ---- nodes ----
    node_html = []
    for nd in ordered:
        x, y = pos[nd["id"]]
        hw, hh = _SIZE.get(nd["kind"], (12, 12))
        archived = "arch" in nd["cls"]
        if nd["kind"] == "doc":
            shape = f'<span class="shape doc{" arch" if archived else ""}"><i></i><i></i><i></i></span>'
        else:
            g = html.escape(nd["glyph"]) if nd["glyph"] else ""
            shape = f'<span class="shape {nd["cls"]}">{g}</span>'
        lab = (f'<div class="lab"><div class="t">{html.escape(nd["title"])}</div>'
               f'<div class="s">{nd["sub"]}</div></div>')
        ncls = f'node k-{nd["kind"]}' + (" archived" if archived else "")
        node_html.append(
            f'<div class="{ncls}" id="node-{nd["id"]}" '
            f'style="left:{x - hw:.1f}px;top:{y - hh:.1f}px;width:{2 * hw}px;height:{2 * hh}px" '
            f'data-kind="{nd["kind"]}" data-search="{html.escape(nd["search"], quote=True)}" '
            f"onclick=\"event.stopPropagation();openNode('{nd['id']}')\">{shape}{lab}</div>")

    # ---- panels ----
    panel_html = []
    for pid, p in panels.items():
        panel_html.append(
            f'<div class="panel" id="panel-{pid}" data-title="{html.escape(p["title"], quote=True)}" '
            f'data-sub="{html.escape(p["sub"], quote=True)}">'
            f'<div class="tab tab-su">{p["su"]}</div>'
            f'<div class="tab tab-de">{p["de"]}</div></div>')

    # ---- toolbar bits ----
    subtitle = " &middot; ".join(x for x in [
        f"DevRev {html.escape(devrev)}" if devrev else "",
        f"slug {html.escape(slug)}",
        f"service {html.escape(service)}" if service else "",
        f"branch {html.escape(branch)}" if branch else "",
        f"commit {html.escape(commit)}" if commit else "",
        f"generated {_now_iso()}"] if x)
    drive_btn = (f'<a class="hbtn" href="{html.escape(drive_url, quote=True)}" target="_blank" '
                 'rel="noopener">&#128193; Drive &#8599;</a>') if drive_url else ""
    devrev_chip = f'<span class="chip">{html.escape(devrev)}</span>' if devrev else ""
    legend = (
        '<span class="lg"><span class="dot c-ok"></span>Succeeded</span>'
        '<span class="lg"><span class="dot c-run"></span>Running</span>'
        '<span class="lg"><span class="dot c-pend"></span>Pending</span>'
        '<span class="lg"><span class="dot c-warn"></span>Open&nbsp;Q</span>'
        '<span class="lg"><span class="dot c-info"></span>Brain</span>'
        '<span class="lg"><span class="dot c-src"></span>Source</span>'
        '<span class="lg"><span class="docmini"></span>Artifact</span>'
    )

    page = (_PAGE
            .replace("__TITLE__", html.escape(title))
            .replace("__SUBTITLE__", subtitle)
            .replace("__PILLCLS__", overall[1])
            .replace("__STATUS__", overall[0])
            .replace("__DRIVE_BTN__", drive_btn)
            .replace("__DEVREV__", devrev_chip)
            .replace("__LEGEND__", legend)
            .replace("__W__", str(W))
            .replace("__H__", str(H))
            .replace("__EDGES__", "\n".join(edge_paths))
            .replace("__NODES__", "\n".join(node_html))
            .replace("__PANELS__", "\n".join(panel_html)))

    if out_path is None:
        out_path = fdir / "pipeline-report.html"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")

    return {
        "feature": slug,
        "out": str(out_path),
        "bytes": len(page),
        "phases": len(phase_bands),
        "sources": len(source_nodes),
        "docs": counts["docs"],
        "subpipelines": counts["subpipelines"],
        "skills": counts["skills"],
        "iterations": counts["iterations"],
        "open_questions": counts["open_questions"],
        "archive_files": len(archive_files),
        "brain_used": bool(brain),
        "drive_url": drive_url or None,
        "nodes": len(ordered),
        "edges": len(edge_paths),
    }


# ----------------------------------------------------------------------------
# Page template (light Argo-Workflows theme). Placeholders are __TOKENS__ filled
# with str.replace() — NOT str.format() — so the CSS/JS braces stay literal.
# ----------------------------------------------------------------------------
_PAGE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__ — Pipeline</title>
<style>
  :root{
    --bg:#fafbfc; --panel:#ffffff; --line:#e3e7ec; --line2:#eef1f4;
    --fg:#1f2933; --muted:#6b7280; --edge:#c2c8d0; --edge2:#aab1bb;
    --ok:#18be94; --run:#1f9bcf; --pend:#cdd3dc; --info:#4c6ef5;
    --warn:#e0a800; --error:#e35d4f; --root:#11a37f; --src:#8b5cf6;
  }
  *{box-sizing:border-box;}
  html,body{height:100%;margin:0;}
  body{display:flex;flex-direction:column;background:var(--bg);color:var(--fg);
       font:13px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;}

  /* ---- header / toolbars ---- */
  header{flex:0 0 auto;background:var(--panel);border-bottom:1px solid var(--line);
         padding:10px 18px 0;position:relative;z-index:10;}
  .hbar{display:flex;align-items:center;justify-content:space-between;gap:12px;}
  .htitle{display:flex;align-items:center;gap:9px;font-size:16px;font-weight:700;}
  .htitle .logo{color:var(--root);font-size:15px;}
  .hactions{display:flex;align-items:center;gap:8px;}
  .pill{font-size:11px;font-weight:700;padding:2px 10px;border-radius:11px;color:#fff;letter-spacing:.02em;}
  .pill.c-ok{background:var(--ok);} .pill.c-run{background:var(--run);}
  .hbtn{font-size:12px;font-weight:600;text-decoration:none;color:var(--root);
        border:1px solid var(--line);border-radius:6px;padding:4px 10px;background:#fff;}
  .hbtn:hover{border-color:var(--root);}
  .chip{font-size:11px;font-weight:600;color:var(--muted);border:1px solid var(--line);
        border-radius:11px;padding:2px 9px;background:#f4f6f8;}
  .hsub{color:var(--muted);font-size:11px;margin:5px 0 9px;}
  .subtoolbar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
              border-top:1px solid var(--line2);padding:7px 0;}
  .zoom{display:inline-flex;align-items:center;gap:4px;}
  .zoom button{width:24px;height:24px;border:1px solid var(--line);background:#fff;border-radius:5px;
               cursor:pointer;font-size:14px;line-height:1;color:var(--fg);}
  .zoom button:hover{border-color:var(--root);}
  .zoom #zlbl{font-size:11px;color:var(--muted);min-width:38px;text-align:center;}
  #search{border:1px solid var(--line);border-radius:6px;padding:4px 9px;font-size:12px;
          width:200px;outline:none;}
  #search:focus{border-color:var(--info);}
  .filters{display:inline-flex;gap:5px;flex-wrap:wrap;}
  .fbtn{font-size:11px;border:1px solid var(--line);background:#fff;color:var(--muted);
        border-radius:11px;padding:3px 10px;cursor:pointer;}
  .fbtn.on{background:var(--root);border-color:var(--root);color:#fff;}
  .legend{margin-left:auto;display:inline-flex;flex-wrap:wrap;align-items:center;}
  .lg{font-size:10.5px;color:var(--muted);margin-left:12px;display:inline-flex;align-items:center;gap:4px;}
  .dot{width:9px;height:9px;border-radius:50%;display:inline-block;}
  .dot.c-ok{background:var(--ok);} .dot.c-run{background:var(--run);}
  .dot.c-pend{background:var(--pend);} .dot.c-warn{background:var(--warn);}
  .dot.c-info{background:var(--info);} .dot.c-src{background:var(--src);}
  .docmini{width:9px;height:11px;background:#fff;border:1px solid var(--edge);border-radius:2px;display:inline-block;}

  /* ---- canvas ---- */
  #canvas{flex:1 1 auto;position:relative;overflow:auto;background:
          radial-gradient(circle, #eef1f4 1px, transparent 1px) 0 0/22px 22px var(--bg);}
  #canvas-inner{position:relative;transform-origin:top left;}
  svg.edges{position:absolute;top:0;left:0;pointer-events:none;}
  .edge{fill:none;stroke:var(--edge);stroke-width:1.5;}
  .edge.spine{stroke:var(--edge2);stroke-width:2;}

  .node{position:absolute;cursor:pointer;}
  .node .shape{display:flex;align-items:center;justify-content:center;width:100%;height:100%;
               border-radius:50%;color:#fff;font-size:11px;font-weight:700;
               border:2px solid #fff;box-shadow:0 1px 3px rgba(16,24,40,.16);transition:transform .1s;}
  .node:hover .shape{transform:scale(1.12);}
  .shape.c-ok{background:var(--ok);} .shape.c-run{background:var(--run);}
  .shape.c-pend{background:var(--pend);color:#6b7280;}
  .shape.c-skip{background:#fff;border:1.5px dashed #b6bcc6;color:#9aa1ab;}
  .shape.c-info{background:var(--info);} .shape.c-warn{background:var(--warn);}
  .shape.c-error{background:var(--error);} .shape.c-root{background:var(--root);font-size:14px;}
  .shape.c-src{background:var(--src);}
  /* sub-pipeline nodes are rounded squares so they read as nested pipelines */
  .node.k-subpipe .shape{border-radius:7px;}
  .shape.doc{background:#fff;border:1px solid var(--edge);border-radius:3px;
             flex-direction:column;align-items:flex-start;justify-content:center;gap:2.5px;padding:4px;}
  .shape.doc i{display:block;height:1.6px;background:#b6bcc6;border-radius:1px;}
  .shape.doc i:nth-child(1){width:65%;} .shape.doc i:nth-child(2){width:90%;}
  .shape.doc i:nth-child(3){width:55%;}
  .shape.doc.arch{border-style:dashed;border-color:#c2c8d0;background:#f7f8fa;}
  .node .lab{position:absolute;left:100%;margin-left:10px;top:50%;transform:translateY(-50%);
             white-space:nowrap;pointer-events:none;}
  .node .lab .t{font-size:12px;font-weight:600;color:var(--fg);line-height:1.2;}
  .node .lab .s{font-size:10.5px;color:var(--muted);line-height:1.2;}
  .node.dim{opacity:.18;}
  .node.hide{display:none;}
  .node.archived{opacity:.62;}
  .node.archived .lab .t{font-style:italic;color:var(--muted);font-weight:500;}
  .node.sel .shape{outline:3px solid rgba(76,110,245,.35);outline-offset:1px;}

  /* ---- sidebar ---- */
  #sidebar{position:fixed;top:0;right:0;height:100%;width:460px;max-width:92vw;background:var(--panel);
           border-left:1px solid var(--line);box-shadow:-6px 0 22px rgba(16,24,40,.10);
           transform:translateX(100%);transition:transform .18s ease;z-index:40;
           display:flex;flex-direction:column;}
  #sidebar.open{transform:translateX(0);}
  .sb-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;
           padding:16px 18px 11px;border-bottom:1px solid var(--line2);}
  #sb-title{font-size:15px;font-weight:700;color:var(--fg);}
  #sb-sub{font-size:11px;margin-top:2px;}
  .sb-x{border:none;background:none;font-size:17px;cursor:pointer;color:var(--muted);line-height:1;}
  .sb-x:hover{color:var(--fg);}
  .sb-tabs{display:flex;gap:2px;padding:8px 14px 0;border-bottom:1px solid var(--line2);}
  .sb-tabs button{border:none;background:none;padding:7px 12px;font-size:11px;font-weight:700;
                  letter-spacing:.05em;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;}
  .sb-tabs button.on{color:var(--root);border-bottom-color:var(--root);}
  #sb-body{padding:14px 18px 40px;overflow:auto;flex:1 1 auto;}
  #sb-body .tab{display:none;}
  #sb-body.show-su .tab-su{display:block;}
  #sb-body.show-de .tab-de{display:block;}

  /* ---- shared content styling (panels + embedded docs) ---- */
  .badge{display:inline-block;font-size:10.5px;padding:1px 8px;border-radius:10px;margin:0 3px 0 0;
         background:#f0f2f5;color:var(--muted);border:1px solid var(--line);}
  .badge.c-ok{color:#0c7a5b;border-color:var(--ok);background:#e7f8f2;}
  .badge.c-run{color:#14688c;border-color:var(--run);background:#e6f4fb;}
  .badge.c-warn{color:#8a6d00;border-color:var(--warn);background:#fdf5dd;}
  .badge.c-error{color:#a8392e;border-color:var(--error);background:#fcebe9;}
  .badge.c-info{color:#2f44b0;border-color:var(--info);background:#eef0fd;}
  .badge.c-skip{color:#6b7280;border-style:dashed;}
  .muted{color:var(--muted);}
  #sb-body h4{margin:16px 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);}
  table{border-collapse:collapse;width:100%;margin:8px 0;font-size:12px;}
  th,td{border:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top;}
  th{background:#f4f6f8;font-weight:600;}
  pre.code{background:#f6f8fa;border:1px solid var(--line);border-radius:6px;padding:10px;overflow:auto;
           font-size:11.5px;}
  code{background:#f0f2f5;padding:1px 4px;border-radius:4px;font-size:11.5px;}
  .doc h1,.doc h2,.doc h3{border-bottom:1px solid var(--line2);padding-bottom:3px;}
  .doc h1{font-size:17px;} .doc h2{font-size:15px;} .doc h3{font-size:13px;}
  blockquote{border-left:3px solid var(--info);margin:6px 0;padding:2px 12px;color:var(--muted);}
  .htmlframe{width:100%;height:500px;border:1px solid var(--line);border-radius:6px;background:#fff;}
  a{color:var(--info);}
  #panels{display:none;}
</style></head>
<body>
<header>
  <div class="hbar">
    <div class="htitle"><span class="logo">◆</span> __TITLE__
      <span class="pill __PILLCLS__">__STATUS__</span></div>
    <div class="hactions">__DRIVE_BTN__ __DEVREV__</div>
  </div>
  <div class="hsub">__SUBTITLE__</div>
  <div class="subtoolbar">
    <div class="zoom">
      <button onclick="zoomOut()" title="zoom out">&minus;</button>
      <span id="zlbl">100%</span>
      <button onclick="zoomIn()" title="zoom in">+</button>
      <button onclick="zoomReset()" title="reset" style="width:auto;padding:0 7px;font-size:11px;">fit</button>
    </div>
    <input id="search" type="text" placeholder="search nodes…" oninput="doSearch(this.value)">
    <div class="filters">
      <button class="fbtn on" data-k="all" onclick="doFilter('all',this)">All</button>
      <button class="fbtn" data-k="phase" onclick="doFilter('phase',this)">Phases</button>
      <button class="fbtn" data-k="subpipe" onclick="doFilter('subpipe',this)">Sub-pipes</button>
      <button class="fbtn" data-k="source" onclick="doFilter('source',this)">Sources</button>
      <button class="fbtn" data-k="skill" onclick="doFilter('skill',this)">Skills</button>
      <button class="fbtn" data-k="iter" onclick="doFilter('iter',this)">Iterations</button>
      <button class="fbtn" data-k="doc" onclick="doFilter('doc',this)">Docs</button>
      <button class="fbtn" data-k="questions" onclick="doFilter('questions',this)">Questions</button>
    </div>
    <div class="legend">__LEGEND__</div>
  </div>
</header>

<div id="canvas" onclick="closeSidebar()">
  <div id="canvas-inner" style="width:__W__px;height:__H__px">
    <svg class="edges" width="__W__" height="__H__">__EDGES__</svg>
    __NODES__
  </div>
</div>

<aside id="sidebar">
  <div class="sb-head">
    <div><div id="sb-title">—</div><div id="sb-sub" class="muted"></div></div>
    <button class="sb-x" onclick="closeSidebar()">&#10005;</button>
  </div>
  <div class="sb-tabs">
    <button id="tab-su" class="on" onclick="showTab('su')">SUMMARY</button>
    <button id="tab-de" onclick="showTab('de')">DETAILS</button>
  </div>
  <div id="sb-body" class="show-su"></div>
</aside>

<div id="panels" hidden>__PANELS__</div>

<script>
function openNode(id){
  var p=document.getElementById('panel-'+id);
  if(!p)return;
  document.getElementById('sb-title').innerHTML=p.getAttribute('data-title')||'';
  document.getElementById('sb-sub').innerHTML=p.getAttribute('data-sub')||'';
  document.getElementById('sb-body').innerHTML=p.innerHTML;
  showTab('su');
  document.getElementById('sidebar').classList.add('open');
  var prev=document.querySelector('.node.sel'); if(prev)prev.classList.remove('sel');
  var nn=document.getElementById('node-'+id); if(nn)nn.classList.add('sel');
}
function closeSidebar(){
  document.getElementById('sidebar').classList.remove('open');
  var prev=document.querySelector('.node.sel'); if(prev)prev.classList.remove('sel');
}
function showTab(t){
  document.getElementById('tab-su').classList.toggle('on',t==='su');
  document.getElementById('tab-de').classList.toggle('on',t==='de');
  var b=document.getElementById('sb-body');
  b.classList.toggle('show-su',t==='su');
  b.classList.toggle('show-de',t==='de');
}
var _z=1;
function applyZoom(){
  document.getElementById('canvas-inner').style.transform='scale('+_z+')';
  document.getElementById('zlbl').textContent=Math.round(_z*100)+'%';
}
function zoomIn(){_z=Math.min(2,Math.round((_z+0.1)*10)/10);applyZoom();}
function zoomOut(){_z=Math.max(0.4,Math.round((_z-0.1)*10)/10);applyZoom();}
function zoomReset(){_z=1;applyZoom();}
function doSearch(v){
  v=(v||'').trim().toLowerCase();
  document.querySelectorAll('.node').forEach(function(n){
    if(!v){n.classList.remove('dim');return;}
    var s=n.getAttribute('data-search')||'';
    n.classList.toggle('dim', s.indexOf(v)<0);
  });
}
function doFilter(k,btn){
  document.querySelectorAll('.filters .fbtn').forEach(function(b){b.classList.remove('on');});
  btn.classList.add('on');
  document.querySelectorAll('.node').forEach(function(n){
    var kind=n.getAttribute('data-kind');
    if(k==='all'||kind==='root'||kind==='brain'){n.classList.remove('hide');return;}
    n.classList.toggle('hide', kind!==k);
  });
}
document.addEventListener('keydown',function(e){if(e.key==='Escape')closeSidebar();});
applyZoom();
</script>
</body></html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_json_arg(val: str):
    """Accept either a path to a JSON file or a raw JSON string."""
    p = Path(val)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return json.loads(val)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pipeline_report.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser(
        "build",
        help="render pipeline-report.html (Argo-style interactive node graph + sidebar)",
    )
    p.add_argument("--feature", required=True,
                   help="feature slug (workspace/features/<slug>/)")
    p.add_argument("--manifest", default=None,
                   help="path to a manifest JSON file, or a raw JSON string")
    p.add_argument("--drive-url", default=None,
                   help="Drive folder share URL for the header button")
    p.add_argument("--title", default=None,
                   help="report title (overrides manifest 'title')")
    p.add_argument("--no-brain", action="store_true",
                   help="skip brain.db enrichment (offline / portable mode)")
    p.add_argument("--out", default=None,
                   help="output path (default workspace/features/<slug>/pipeline-report.html)")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "build":
        manifest = _load_json_arg(args.manifest) if args.manifest else {}
        out_path = Path(args.out) if args.out else None
        summary = build_report(
            args.feature, manifest,
            drive_url=args.drive_url,
            title=args.title,
            use_brain=not args.no_brain,
            out_path=out_path,
        )
        print(json.dumps(summary, indent=2, default=str))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
