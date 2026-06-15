#!/usr/bin/env python3
"""Convert a feature tech-spec markdown file to .docx via rubick_doc.py.

Usage:
    python3 md_to_docx.py <input.md> <output.docx> [--title T] [--author A] [--team T]

Maps the markdown's top-level `## N. Title` sections to rubick_doc.py template
sections and writes a properly formatted Razorpay Tech Spec document.
"""

import sys, os, re, subprocess, tempfile, json, shutil
from pathlib import Path

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
RUBICK_DOC  = os.path.join(SCRIPTS_DIR, "rubick_doc.py")

def run(cmd: list[str]) -> dict:
    result = subprocess.run(
        [sys.executable, RUBICK_DOC] + cmd,
        capture_output=True, text=True
    )
    try:
        return json.loads(result.stdout)
    except Exception:
        return {"ok": False, "stdout": result.stdout, "stderr": result.stderr}


def extract_sections(md: str) -> list[dict]:
    """Split markdown on top-level ## N. headings, return [{num, title, content}]."""
    # Match lines like: ## 1. Problem Statement  or  ## 15. Appendix
    pattern = re.compile(r"^## (\d+)\.\s+(.+)$", re.MULTILINE)
    matches = list(pattern.finditer(md))
    sections = []
    for i, m in enumerate(matches):
        start = m.end()
        end   = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        content = md[start:end].strip()
        sections.append({
            "num":     int(m.group(1)),
            "title":   m.group(2).strip(),
            "content": content,
        })
    return sections


def extract_meta(md: str) -> dict:
    """Pull Version, Date, Author, Team, Status from the frontmatter block."""
    meta = {"title": "Tech Spec", "author": "Unknown", "team": "Payments Platform"}
    h1 = re.search(r"^#\s+(.+)$", md, re.MULTILINE)
    if h1:
        meta["title"] = h1.group(1).strip()
    for key, pattern in [
        ("author", r"\*\*Author\*\*:\s*(.+?)[\s|]"),
        ("team",   r"\*\*Team\*\*:\s*(.+?)(?:\n|$)"),
        ("version",r"\*\*Version\*\*:\s*(.+?)[\s|]"),
        ("status", r"\*\*Status\*\*:\s*(.+?)[\s|]"),
    ]:
        m = re.search(pattern, md)
        if m:
            meta[key] = m.group(1).strip().rstrip("|").strip()
    return meta


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        print("Usage: md_to_docx.py <input.md> <output.docx> [--title T] [--author A] [--team T]")
        sys.exit(1)

    input_md  = args[0]
    output    = args[1]

    # Parse optional overrides
    overrides = {}
    i = 2
    while i < len(args):
        if args[i] in ("--title",) and i + 1 < len(args):
            overrides["title"] = args[i + 1]; i += 2
        elif args[i] in ("--author",) and i + 1 < len(args):
            overrides["author"] = args[i + 1]; i += 2
        elif args[i] in ("--team",) and i + 1 < len(args):
            overrides["team"] = args[i + 1]; i += 2
        else:
            i += 1

    with open(input_md, "r") as f:
        md = f.read()

    meta     = extract_meta(md)
    meta.update(overrides)
    sections = extract_sections(md)

    # Map markdown section numbers to rubick template section numbers.
    # The v2 spec is numbered 1-15; the template has 1-16.
    # We do a best-effort 1:1 pass-through for 1-15.
    SEC_MAP = {
        1: 1,    # Problem Statement
        2: 2,    # Introduction & Scope
        3: 9,    # NFRs → Section 9 (NFRs in template)
        4: 4,    # Impact Assessment → Section 4 (Futuristic Scope) — close enough
        5: 5,    # Assumptions → Section 5
        6: 6,    # Current Architecture → Section 6
        7: 8,    # Final Approach → Section 8 (Final Approach in template)
        8: 10,   # DB Touchpoints → Section 10
        9: 11,   # Testing Strategy → Section 11
        10: 12,  # Observability → Section 12
        11: 13,  # Rollout Plan → Section 13
        12: 14,  # Rollback Plan → Section 14
        13: 15,  # Open Questions → Section 15
        14: 16,  # Risk Register → Section 16 (Appendix — risk register appended)
        15: 16,  # Appendix → Section 16 (merged)
    }

    # ── Step 1: Create the document ──────────────────────────────────────────
    print(f"Creating document: {output}")
    res = run([
        "create",
        "--title",  meta["title"],
        "--author", meta.get("author", "Saurav K"),
        "--team",   meta.get("team", "Payments Platform — Emandate / Offers Pod"),
        "--template", "tech-spec",
        "--output", output,
    ])
    print("  create:", res)
    if not res.get("ok") and not os.path.exists(output):
        print("ERROR: could not create document")
        sys.exit(1)

    # ── Step 2: Add sections ─────────────────────────────────────────────────
    tmpdir = tempfile.mkdtemp()
    try:
        for sec in sections:
            template_num = SEC_MAP.get(sec["num"])
            if template_num is None:
                continue

            if not sec["content"].strip():
                continue

            # Write content to temp file
            tf = os.path.join(tmpdir, f"sec_{template_num}.md")
            with open(tf, "w") as f:
                f.write(sec["content"])

            print(f"  Section {sec['num']} → template {template_num}: {sec['title'][:50]}")
            res = run([
                "add-section",
                "--doc",          output,
                "--number",       str(template_num),
                "--content-file", tf,
            ])
            if not res.get("ok"):
                print(f"    WARN: {res}")

        # ── Step 3: Finalize ──────────────────────────────────────────────────
        print("Finalizing…")
        res = run(["finalize", "--doc", output])
        print("  finalize:", res)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    size_kb = os.path.getsize(output) / 1024 if os.path.exists(output) else 0
    print(f"\n✓ Done → {output} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
