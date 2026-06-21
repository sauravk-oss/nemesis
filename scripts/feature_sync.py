#!/usr/bin/env python3
"""Feature Sync — Drive-backed PUSH/PULL for a Nemesis feature folder.

A feature folder (`workspace/features/<slug>/`) is made shareable by mirroring
its text artifacts to Google Drive under `nemesis/features/<slug>/`. Anyone can
then run `/nemesis new <name> <drive-link>` (or `/nemesis pull <link>`) to pull
those artifacts onto a fresh machine and rebuild brain + feature state.

Two-phase, exactly like Franco — this script NEVER calls an MCP:
  * Python (here) decides *what* to push/pull and records the results.
  * The LLM (skill layer) performs the Drive MCP I/O in between.

brain.db is NEVER shipped. PULL recreates artifacts on disk; the brain is rebuilt
afterwards by re-running the learning pipeline (Franco ingest + learn-flush).

Subcommands
-----------
  manifest    --feature <slug>
  status      --feature <slug>
  push-plan   --feature <slug>
  record-push --feature <slug> --results <file-or-json> [--folder-id ID] [--share-url URL]
  pull-plan   --link <drive-link> [--feature <slug>]
  record-pull --feature <slug> --files <file-or-json> [--folder-id ID]

Per-feature manifest: workspace/features/<slug>/.drive.json (gitignored).

Flatten rule: a file in a subdir (e.g. `implementation/x.md`) is stored on Drive
with `/` replaced by `__` (`implementation__x.md`) — Drive folder nesting is
costly, so subpaths are flattened + prefixed. This is reversible on pull as long
as real filenames contain no `__`.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make the repo root importable so `brain.config` resolves when this file is run
# directly (`python3 scripts/feature_sync.py ...`), not just as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain.config import (BrainConfig, DRIVE_STORAGE_FOLDER_ID,  # noqa: E402
                          SYNC_ALLOWLIST_EXT, SYNC_MAX_FILE_BYTES,
                          SYNC_SKIP_DIR_SUFFIXES)

WORKSPACE = Path(BrainConfig().workspace)
FEATURES = WORKSPACE / "features"
MANIFEST_NAME = ".drive.json"


# ----------------------------------------------------------------------------
# Paths & manifest
# ----------------------------------------------------------------------------
def _feature_dir(slug: str) -> Path:
    return FEATURES / slug


def _manifest_path(slug: str) -> Path:
    return _feature_dir(slug) / MANIFEST_NAME


def _load_manifest(slug: str) -> dict:
    mp = _manifest_path(slug)
    if mp.exists():
        try:
            return json.loads(mp.read_text())
        except json.JSONDecodeError:
            pass
    return {"slug": slug, "folder_id": None, "share_url": None,
            "root_folder_id": DRIVE_STORAGE_FOLDER_ID,
            "files": {}, "last_push": None, "last_pull": None}


def _save_manifest(slug: str, m: dict) -> None:
    mp = _manifest_path(slug)
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(m, indent=2))


# ----------------------------------------------------------------------------
# Flatten / unflatten subpaths for the (flat) Drive folder
# ----------------------------------------------------------------------------
def _to_drive_name(rel_posix: str) -> str:
    """`implementation/x.md` -> `implementation__x.md` (flatten subdirs)."""
    return rel_posix.replace("/", "__")


def _from_drive_name(drive_name: str) -> str:
    """`implementation__x.md` -> `implementation/x.md` (reverse the flatten)."""
    return drive_name.replace("__", "/")


# ----------------------------------------------------------------------------
# File hashing / metadata
# ----------------------------------------------------------------------------
def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def _mtime_iso(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_in_skipped_dir(rel: Path) -> bool:
    return any(part.endswith(suf)
               for suf in SYNC_SKIP_DIR_SUFFIXES
               for part in rel.parts[:-1])


def _scan_local(slug: str):
    """Yield (path, rel_posix, drive_name) for every push-eligible artifact.

    Allowlist by extension, skip the manifest, skip `*-logs/` dirs and files
    over the size cap.
    """
    fdir = _feature_dir(slug)
    if not fdir.exists():
        return
    for p in sorted(fdir.rglob("*")):
        if not p.is_file() or p.name == MANIFEST_NAME:
            continue
        rel = p.relative_to(fdir)
        if _is_in_skipped_dir(rel):
            continue
        if p.suffix.lower() not in SYNC_ALLOWLIST_EXT:
            continue
        if p.stat().st_size > SYNC_MAX_FILE_BYTES:
            continue
        rel_posix = rel.as_posix()
        yield p, rel_posix, _to_drive_name(rel_posix)


# ----------------------------------------------------------------------------
# Drive link parsing (folder id)
# ----------------------------------------------------------------------------
def _extract_folder_id(link: str) -> str:
    """Pull a Drive folder/file id out of a share link (or accept a bare id)."""
    m = re.search(r"(?:folders/|file/d/|open\?id=|[?&]id=)([\w-]+)", link)
    if m:
        return m.group(1)
    # bare id (no slashes / query) — accept as-is
    if re.fullmatch(r"[\w-]+", link.strip()):
        return link.strip()
    return ""


# ----------------------------------------------------------------------------
# JSON arg (file path or inline JSON string)
# ----------------------------------------------------------------------------
def _read_json_arg(val):
    if val is None:
        return None
    p = Path(val)
    if p.exists():
        return json.loads(p.read_text())
    return json.loads(val)


# ----------------------------------------------------------------------------
# Subcommands
# ----------------------------------------------------------------------------
def cmd_manifest(args):
    m = _load_manifest(args.feature)
    _save_manifest(args.feature, m)  # create on first read
    print(json.dumps(m, indent=2, default=str))


def _diff(slug: str):
    """Compare local artifacts vs the manifest. Returns (changed, unchanged, deleted)."""
    m = _load_manifest(slug)
    recorded = m.get("files", {})
    seen = set()
    changed, unchanged = [], []
    for p, rel_posix, drive_name in _scan_local(slug):
        seen.add(drive_name)
        digest = _sha256(p)
        prev = recorded.get(drive_name)
        entry = {"local_path": str(p), "rel": rel_posix, "drive_name": drive_name,
                 "size": p.stat().st_size, "sha256": digest,
                 "mtime": _mtime_iso(p),
                 "existing_file_id": (prev or {}).get("file_id")}
        if prev and prev.get("sha256") == digest:
            unchanged.append(entry)
        else:
            entry["action"] = "update" if (prev and prev.get("file_id")) else "create"
            changed.append(entry)
    deleted = [name for name in recorded if name not in seen]
    return m, changed, unchanged, deleted


def cmd_status(args):
    m, changed, unchanged, deleted = _diff(args.feature)
    print(json.dumps({
        "slug": args.feature,
        "feature_dir": str(_feature_dir(args.feature)),
        "folder_id": m.get("folder_id"),
        "changed": [{"drive_name": c["drive_name"], "action": c["action"],
                     "size": c["size"]} for c in changed],
        "changed_count": len(changed),
        "unchanged_count": len(unchanged),
        "deleted_on_local": deleted,
        "needs_push": bool(changed),
    }, indent=2, default=str))


def cmd_push_plan(args):
    m, changed, unchanged, deleted = _diff(args.feature)
    too_large = non_allow = 0
    fdir = _feature_dir(args.feature)
    if fdir.exists():
        for p in fdir.rglob("*"):
            if not p.is_file() or p.name == MANIFEST_NAME:
                continue
            if _is_in_skipped_dir(p.relative_to(fdir)):
                continue
            if p.suffix.lower() not in SYNC_ALLOWLIST_EXT:
                non_allow += 1
            elif p.stat().st_size > SYNC_MAX_FILE_BYTES:
                too_large += 1

    folder_id = m.get("folder_id")
    plan = {
        "slug": args.feature,
        "drive": {
            "root_folder_id": m.get("root_folder_id", DRIVE_STORAGE_FOLDER_ID),
            "feature_folder_id": folder_id,
            "feature_folder_name": args.feature,
            "resolve_steps": [] if folder_id else [
                f"search_files(name='{args.feature}', "
                f"mimeType='application/vnd.google-apps.folder', "
                f"parent='{m.get('root_folder_id', DRIVE_STORAGE_FOLDER_ID)}')",
                "if not found: create_file(folder) with that name + parent",
                "thread the resolved id back via: record-push --folder-id <id>",
            ],
        },
        "uploads": [
            {"local_path": c["local_path"], "drive_name": c["drive_name"],
             "action": c["action"], "existing_file_id": c["existing_file_id"],
             "size": c["size"], "sha256": c["sha256"]}
            for c in changed
        ],
        "skipped": {"unchanged": len(unchanged), "too_large": too_large,
                    "non_allowlisted": non_allow},
        "count": len(changed),
        "record_format": {
            "folder_id": "<resolved folder id>",
            "share_url": "<optional folder share url>",
            "files": {"<drive_name>": {"file_id": "<uploaded file id>"}},
        },
    }
    print(json.dumps(plan, indent=2, default=str))


def cmd_record_push(args):
    m = _load_manifest(args.feature)
    results = _read_json_arg(args.results) or {}
    if args.folder_id:
        m["folder_id"] = args.folder_id
    elif results.get("folder_id"):
        m["folder_id"] = results["folder_id"]
    if args.share_url:
        m["share_url"] = args.share_url
    elif results.get("share_url"):
        m["share_url"] = results["share_url"]
    if not m.get("share_url") and m.get("folder_id"):
        m["share_url"] = f"https://drive.google.com/drive/folders/{m['folder_id']}"

    fdir = _feature_dir(args.feature)
    files = results.get("files", {})
    recorded = m.setdefault("files", {})
    written = []
    for drive_name, info in files.items():
        local = fdir / _from_drive_name(drive_name)
        if not local.exists():
            continue
        recorded[drive_name] = {
            "file_id": (info or {}).get("file_id"),
            "sha256": _sha256(local), "size": local.stat().st_size,
            "mtime": _mtime_iso(local), "pushed_at": _now_iso(),
        }
        written.append(drive_name)
    m["last_push"] = _now_iso()
    _save_manifest(args.feature, m)
    print(json.dumps({"slug": args.feature, "folder_id": m.get("folder_id"),
                      "share_url": m.get("share_url"), "recorded": written,
                      "recorded_count": len(written)}, indent=2, default=str))


def cmd_pull_plan(args):
    folder_id = _extract_folder_id(args.link)
    out = {
        "link": args.link,
        "folder_id": folder_id or None,
        "feature": args.feature,
        "feature_dir": str(_feature_dir(args.feature)) if args.feature else None,
        "steps": [
            f"search_files(parent='{folder_id}') — list every file in the folder "
            f"(recurse into any subfolders)",
            "download_file_content(file_id) for each file; capture text content",
            "hand the downloads back via: record-pull --feature <slug> "
            "--files <json> --folder-id " + (folder_id or "<id>"),
        ],
        "record_format": {
            "folder_id": folder_id or "<id>",
            "files": [
                {"drive_name": "overview.md", "file_id": "<id>",
                 "content": "<utf-8 text>"},
                {"drive_name": "implementation__x.md", "file_id": "<id>",
                 "content_b64": "<base64 if not utf-8 text>"},
            ],
        },
        "note": ("brain.db is NOT shipped — after record-pull, rebuild brain via "
                 "feature-create + Franco ingest of each pulled artifact + learn-flush."),
    }
    if not folder_id:
        out["error"] = "could not extract a Drive folder id from the link"
    print(json.dumps(out, indent=2, default=str))


def cmd_record_pull(args):
    m = _load_manifest(args.feature)
    payload = _read_json_arg(args.files) or {}
    if args.folder_id:
        m["folder_id"] = args.folder_id
    elif payload.get("folder_id"):
        m["folder_id"] = payload["folder_id"]
    if m.get("folder_id") and not m.get("share_url"):
        m["share_url"] = f"https://drive.google.com/drive/folders/{m['folder_id']}"

    fdir = _feature_dir(args.feature)
    fdir.mkdir(parents=True, exist_ok=True)
    recorded = m.setdefault("files", {})
    written, total_bytes = [], 0
    for f in payload.get("files", []):
        drive_name = f.get("drive_name")
        if not drive_name or drive_name == MANIFEST_NAME:
            continue
        rel = _from_drive_name(drive_name)
        dest = fdir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if f.get("content_b64") is not None:
            data = base64.b64decode(f["content_b64"])
            dest.write_bytes(data)
        else:
            dest.write_text(f.get("content", ""))
        recorded[drive_name] = {
            "file_id": f.get("file_id"), "sha256": _sha256(dest),
            "size": dest.stat().st_size, "mtime": _mtime_iso(dest),
            "pulled_at": _now_iso(),
        }
        total_bytes += dest.stat().st_size
        written.append(rel)
    m["last_pull"] = _now_iso()
    _save_manifest(args.feature, m)
    print(json.dumps({"slug": args.feature, "feature_dir": str(fdir),
                      "folder_id": m.get("folder_id"), "written": written,
                      "written_count": len(written), "bytes": total_bytes,
                      "last_pull": m["last_pull"]}, indent=2, default=str))


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="feature_sync",
                                description="Drive-backed PUSH/PULL for a feature folder")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("manifest", help="read/create the .drive.json manifest")
    m.add_argument("--feature", required=True)
    m.set_defaults(func=cmd_manifest)

    s = sub.add_parser("status", help="diff local artifacts vs manifest")
    s.add_argument("--feature", required=True)
    s.set_defaults(func=cmd_status)

    pp = sub.add_parser("push-plan", help="emit the upload plan for the LLM")
    pp.add_argument("--feature", required=True)
    pp.set_defaults(func=cmd_push_plan)

    rp = sub.add_parser("record-push", help="write back upload results into the manifest")
    rp.add_argument("--feature", required=True)
    rp.add_argument("--results", required=True, help="JSON file path or inline JSON")
    rp.add_argument("--folder-id", dest="folder_id")
    rp.add_argument("--share-url", dest="share_url")
    rp.set_defaults(func=cmd_record_push)

    pl = sub.add_parser("pull-plan", help="parse a Drive link, emit the download plan")
    pl.add_argument("--link", required=True)
    pl.add_argument("--feature")
    pl.set_defaults(func=cmd_pull_plan)

    rl = sub.add_parser("record-pull", help="write downloaded files to disk + manifest")
    rl.add_argument("--feature", required=True)
    rl.add_argument("--files", required=True, help="JSON file path or inline JSON")
    rl.add_argument("--folder-id", dest="folder_id")
    rl.set_defaults(func=cmd_record_pull)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
