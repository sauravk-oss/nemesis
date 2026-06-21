#!/usr/bin/env python3
"""Pipeline Report — render a single self-contained HTML showing a feature's
*entire* AI pipeline as a collapsible tree.

What you get (one file, opens in any browser, no network needed for structure):

  Feature (root)
   ├─ Brain Knowledge .................. powered by brain.db (feature_health + stats)
   ├─ Pipeline
   │   ├─ Phase: Ideation .............. docs · skills used (input→output) · iterations
   │   ├─ Phase: Solutioning
   │   ├─ Phase: Tech Spec
   │   ├─ Phase: Implementation ........ change-report.md + test-report.md embedded
   │   └─ Phase: E2E
   ├─ Archive ......................... every archived doc version, collapsible
   └─ More details → Drive URL ........ redirect for the full feature folder

Every node is a native <details>/<summary> — collapsible with zero JS. Each doc is
embedded in full (Markdown rendered inline; .html artifacts via <iframe srcdoc>), so
the "test added report everything" is visible right inside the tree.

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
  "phases": [
    {
      "name": "Ideation", "status": "complete",
      "docs": ["overview.md", "overview.html"],          # optional; auto-discovered if omitted
      "skills": [
        {"skill":"product-management:brainstorm","tier":"skill",
         "input":"2 Slack threads + PRD","output":"problem statement + 5 user stories",
         "summary":"framed the SEBI TPV problem"}
      ],
      "iterations": [
        {"n":1,"input":"raw sources","output":"overview-v2.md","note":"first As-Is/To-Be"},
        {"n":2,"input":"open questions resolved","output":"overview-v4.md"}
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
PHASE_ORDER = ["Ideation", "Solutioning", "Tech Spec", "Implementation", "E2E"]
_DOC_PHASE = [
    ("overview", "Ideation"),
    ("solution", "Solutioning"),
    ("tech-spec", "Tech Spec"),
    ("techspec", "Tech Spec"),
    ("hld", "Tech Spec"),
    ("change-report", "Implementation"),
    ("test-report", "Implementation"),
    ("implementation", "Implementation"),
    ("deploy", "Implementation"),
    ("e2e", "E2E"),
    ("devtest", "E2E"),
    ("scenario", "E2E"),
]


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
# HTML node helpers (native <details> = collapsible, zero JS).
# ----------------------------------------------------------------------------
def _details(summary_html: str, body_html: str, open_=False, cls="node") -> str:
    op = " open" if open_ else ""
    return (f'<details class="{cls}"{op}><summary>{summary_html}</summary>'
            f'<div class="body">{body_html}</div></details>')


def _badge(text: str, kind: str = "") -> str:
    return f'<span class="badge {kind}">{html.escape(str(text))}</span>'


def _embed_doc(path: Path) -> str:
    """Embed a doc in full. .md is rendered; .html goes in an iframe srcdoc;
    anything else lands in a <pre>."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # pragma: no cover
        return f"<p class='muted'>could not read {html.escape(path.name)}: {html.escape(str(e))}</p>"
    suffix = path.suffix.lower()
    size = f"{len(raw)//1024} KB" if len(raw) >= 1024 else f"{len(raw)} B"
    head = f"<p class='muted'>{html.escape(str(path))} · {size}</p>"
    if suffix in (".md", ".markdown", ".txt"):
        return head + "<div class='doc'>" + md_to_html(raw) + "</div>"
    if suffix in (".html", ".htm"):
        srcdoc = html.escape(raw, quote=True)
        return (head + f'<iframe class="htmlframe" srcdoc="{srcdoc}" '
                'sandbox="allow-same-origin" loading="lazy"></iframe>')
    if suffix == ".json":
        return head + "<pre class='code'>" + html.escape(raw) + "</pre>"
    return head + "<pre class='code'>" + html.escape(raw[:20000]) + "</pre>"


def _skill_node(s: dict) -> str:
    skill = html.escape(s.get("skill", "?"))
    tier = s.get("tier", "skill")
    tier_kind = {"skill": "ok", "brain": "info", "slash": "warn", "none": "crit"}.get(tier, "")
    summ = html.escape(s.get("summary", "")) or skill
    rows = []
    if s.get("input"):
        rows.append(f"<p><strong>Input</strong> → {_inline(str(s['input']))}</p>")
    if s.get("output"):
        rows.append(f"<p><strong>Output</strong> → {_inline(str(s['output']))}</p>")
    if not rows:
        rows.append("<p class='muted'>no input/output recorded</p>")
    label = f"&#129520; {skill} {_badge(tier, tier_kind)} <span class='muted'>{summ}</span>"
    return _details(label, "\n".join(rows), cls="leaf")


def _iter_node(it: dict) -> str:
    n = it.get("n", "?")
    label = (f"&#128260; Iteration {html.escape(str(n))} "
             f"<span class='muted'>{html.escape(it.get('note',''))}</span>")
    body = []
    if it.get("input"):
        body.append(f"<p><strong>Input</strong> → {_inline(str(it['input']))}</p>")
    if it.get("output"):
        body.append(f"<p><strong>Output</strong> → {_inline(str(it['output']))}</p>")
    if not body:
        body.append("<p class='muted'>no input/output recorded</p>")
    return _details(label, "\n".join(body), cls="leaf")


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
                     " · ".join(f"{k} {v}" for k, v in nodes.items()) + "</p>")
    st = brain.get("stats") or {}
    g = st.get("graph") if isinstance(st, dict) else None
    if g:
        parts.append(f"<p class='muted'>Brain totals: {g.get('nodes','?')} nodes · "
                     f"{g.get('edges','?')} edges · {g.get('services','?')} services · "
                     f"{g.get('code_bodies','?')} code bodies</p>")
    return "\n".join(parts) or "<p class='muted'>no brain data</p>"


# ----------------------------------------------------------------------------
# Discovery + assembly
# ----------------------------------------------------------------------------
_SKIP_DIRS = {"archive", "_archive"}


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
        if p.name.startswith("."):
            continue
        by_phase[_phase_for(p.name)].append(p)
    # implementation/ subdir docs
    impl = fdir / "implementation"
    if impl.exists():
        for p in sorted(impl.rglob("*")):
            if p.is_file() and p.suffix.lower() in (".md", ".html", ".json"):
                by_phase["Implementation"].append(p)
    return by_phase


def discover_archive(fdir: Path) -> list:
    items = []
    for d in _SKIP_DIRS:
        ad = fdir / d
        if ad.exists():
            for p in sorted(ad.iterdir()):
                if p.is_file():
                    items.append(p)
    return items


def build_report(slug: str, manifest: dict, drive_url: str = None, title: str = None,
                 use_brain: bool = True, out_path: Path = None) -> dict:
    fdir = FEATURES / slug
    manifest = manifest or {}
    title = title or manifest.get("title") or slug
    devrev = manifest.get("devrev", "")
    drive_url = drive_url or manifest.get("drive_url", "")
    brain_feature = manifest.get("brain_feature") or title

    brain = brain_enrich(brain_feature) if use_brain else {}

    # Map manifest phases by name for quick lookup; auto-discover docs.
    man_phases = {p.get("name", ""): p for p in (manifest.get("phases") or [])}
    docs_by_phase = discover_docs(fdir)

    # Union of phases we know about, in canonical order, then any extras.
    phase_names = list(PHASE_ORDER) + [p for p in man_phases if p not in PHASE_ORDER]

    phase_nodes = []
    counts = {"docs": 0, "skills": 0, "iterations": 0, "open_questions": 0}
    for name in phase_names:
        mp = man_phases.get(name, {})
        # docs: manifest list (resolved against fdir) ∪ auto-discovered
        docs: list = []
        seen = set()
        for d in mp.get("docs", []) or []:
            cand = (fdir / d)
            if cand.exists() and cand.name not in seen:
                docs.append(cand); seen.add(cand.name)
        for p in docs_by_phase.get(name, []):
            if p.name not in seen:
                docs.append(p); seen.add(p.name)

        skills = mp.get("skills", []) or []
        iters = mp.get("iterations", []) or []
        oqs = mp.get("open_questions", []) or []
        status = mp.get("status") or ("complete" if docs else "pending")

        if not (docs or skills or iters or oqs):
            continue  # nothing to show for this phase

        counts["docs"] += len(docs)
        counts["skills"] += len(skills)
        counts["iterations"] += len(iters)
        counts["open_questions"] += len(oqs)

        body = []
        if docs:
            doc_children = "".join(
                _details("&#128196; " + html.escape(p.name) +
                         (f" {_badge(p.suffix.lstrip('.'))}"), _embed_doc(p), cls="leaf")
                for p in docs)
            body.append(_details(f"&#128193; Documents {_badge(len(docs))}", doc_children, open_=True))
        if skills:
            body.append(_details(f"&#129520; Skills Used {_badge(len(skills))}",
                                 "".join(_skill_node(s) for s in skills), open_=True))
        if iters:
            body.append(_details(f"&#128260; Iterations {_badge(len(iters))}",
                                 "".join(_iter_node(it) for it in iters)))
        if oqs:
            n_open = sum(1 for q in oqs if (q.get("status") or "open").lower() not in ("resolved", "closed"))
            body.append(_details(f"&#10067; Open Questions {_badge(str(n_open)+' open', 'crit' if n_open else 'ok')}",
                                 _oq_node(oqs)))
        if mp.get("notes"):
            body.append(f"<p>{_inline(str(mp['notes']))}</p>")

        status_kind = {"complete": "ok", "in_progress": "warn", "pending": "muted"}.get(status, "")
        label = f"<strong>{html.escape(name)}</strong> {_badge(status, status_kind)}"
        phase_nodes.append(_details(label, "\n".join(body), open_=(status != "pending")))

    # Archive node
    archive = discover_archive(fdir)
    archive_html = ""
    if archive:
        children = "".join(
            _details("&#128196; " + html.escape(p.name), _embed_doc(p), cls="leaf")
            for p in archive)
        archive_html = _details(f"&#128230; Archive {_badge(len(archive))} "
                                "<span class='muted'>every iteration kept</span>", children)

    # Assemble page
    subtitle = " · ".join(x for x in [f"DevRev {devrev}" if devrev else "",
                                      f"slug {slug}", f"generated {_now_iso()}"] if x)
    brain_node = _details("&#129504; Brain Knowledge "
                          "<span class='muted'>powered by brain.db — all file/code context</span>",
                          _brain_panel(brain), open_=True)
    pipeline_node = _details(f"&#128640; Pipeline {_badge(len(phase_nodes))} phases",
                             "\n".join(phase_nodes) or "<p class='muted'>no phases yet</p>", open_=True)

    drive_html = ""
    if drive_url:
        drive_html = (f'<div class="drive"><a href="{html.escape(drive_url, quote=True)}" '
                      'target="_blank" rel="noopener">&#128193; Open the full feature folder on '
                      'Google Drive for more details &rarr;</a></div>')

    root_children = brain_node + pipeline_node + archive_html
    body_html = _details(f"&#11088; <strong>{html.escape(title)}</strong>", root_children, open_=True)

    page = _PAGE.format(
        title=html.escape(title),
        subtitle=html.escape(subtitle),
        drive=drive_html,
        tree=body_html,
    )

    if out_path is None:
        out_path = fdir / "pipeline-report.html"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")

    return {
        "feature": slug,
        "out": str(out_path),
        "bytes": len(page),
        "phases": len(phase_nodes),
        "docs": counts["docs"],
        "skills": counts["skills"],
        "iterations": counts["iterations"],
        "open_questions": counts["open_questions"],
        "archive_files": len(archive),
        "brain_used": bool(brain),
        "drive_url": drive_url or None,
    }


_PAGE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — Pipeline Report</title>
<style>
  :root {{ --bg:#0d1117; --card:#161b22; --line:#30363d; --fg:#e6edf3; --muted:#8b949e;
           --ok:#3fb950; --warn:#d29922; --crit:#f85149; --info:#58a6ff; --accent:#bc8cff; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
          font:14px/1.55 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }}
  header {{ padding:20px 24px; border-bottom:1px solid var(--line); position:sticky; top:0;
            background:linear-gradient(180deg,#161b22,#0d1117); z-index:5; }}
  header h1 {{ margin:0 0 4px; font-size:20px; }}
  header .sub {{ color:var(--muted); font-size:12px; }}
  .toolbar {{ margin-top:10px; }}
  .toolbar button {{ background:var(--card); color:var(--fg); border:1px solid var(--line);
                     border-radius:6px; padding:5px 10px; cursor:pointer; font-size:12px; margin-right:8px; }}
  .toolbar button:hover {{ border-color:var(--accent); }}
  main {{ padding:18px 24px 60px; max-width:1100px; }}
  details {{ border:1px solid var(--line); border-radius:8px; margin:6px 0; background:var(--card); }}
  details.leaf {{ background:#0f141a; }}
  summary {{ cursor:pointer; padding:8px 12px; list-style:none; user-select:none; }}
  summary::-webkit-details-marker {{ display:none; }}
  summary::before {{ content:"\\25B8"; color:var(--muted); display:inline-block;
                     width:1em; transition:transform .15s; }}
  details[open] > summary::before {{ transform:rotate(90deg); }}
  .body {{ padding:4px 14px 12px 26px; border-top:1px solid var(--line); }}
  .badge {{ display:inline-block; font-size:11px; padding:1px 7px; border-radius:10px;
            background:#21262d; color:var(--muted); border:1px solid var(--line); margin:0 3px; }}
  .badge.ok {{ color:var(--ok); border-color:var(--ok); }}
  .badge.warn {{ color:var(--warn); border-color:var(--warn); }}
  .badge.crit {{ color:var(--crit); border-color:var(--crit); }}
  .badge.info {{ color:var(--info); border-color:var(--info); }}
  .muted {{ color:var(--muted); }}
  table {{ border-collapse:collapse; width:100%; margin:8px 0; font-size:13px; }}
  th,td {{ border:1px solid var(--line); padding:5px 8px; text-align:left; vertical-align:top; }}
  th {{ background:#21262d; }}
  pre.code {{ background:#0b0f14; border:1px solid var(--line); border-radius:6px;
              padding:10px; overflow:auto; font-size:12px; }}
  code {{ background:#0b0f14; padding:1px 4px; border-radius:4px; font-size:12px; }}
  .doc h1,.doc h2,.doc h3 {{ border-bottom:1px solid var(--line); padding-bottom:3px; }}
  blockquote {{ border-left:3px solid var(--accent); margin:6px 0; padding:2px 12px; color:var(--muted); }}
  .htmlframe {{ width:100%; height:520px; border:1px solid var(--line); border-radius:6px; background:#fff; }}
  .drive {{ margin:12px 0; }}
  .drive a {{ color:var(--info); text-decoration:none; font-weight:600; }}
  a {{ color:var(--info); }}
</style></head>
<body>
<header>
  <h1>&#11088; {title}</h1>
  <div class="sub">{subtitle}</div>
  <div class="toolbar">
    <button onclick="document.querySelectorAll('details').forEach(d=>d.open=true)">Expand all</button>
    <button onclick="document.querySelectorAll('details').forEach(d=>d.open=false)">Collapse all</button>
  </div>
</header>
<main>
{drive}
{tree}
</main>
</body></html>
"""


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def _load_json_arg(val: str):
    p = Path(val)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return json.loads(val)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pipeline_report.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("build", help="render pipeline-report.html (collapsible tree)")
    p.add_argument("--feature", required=True, help="feature slug (dir under workspace/features/)")
    p.add_argument("--manifest", help="pipeline manifest file path or inline JSON (optional)")
    p.add_argument("--drive-url", help="Drive folder URL for the 'more details' redirect")
    p.add_argument("--title", help="human title (default: manifest.title or slug)")
    p.add_argument("--no-brain", action="store_true", help="skip the brain.db query")
    p.add_argument("--out", help="output path (default workspace/features/<slug>/pipeline-report.html)")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "build":
        manifest = _load_json_arg(args.manifest) if args.manifest else {}
        out_path = Path(args.out) if args.out else None
        summary = build_report(args.feature, manifest, drive_url=args.drive_url,
                               title=args.title, use_brain=not args.no_brain, out_path=out_path)
        print(json.dumps(summary, indent=2, default=str))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
