#!/usr/bin/env python3
"""Franco — Universal Data Collector for Rubick.

Franco pulls data from everywhere and ingests it into Rubick's knowledge graph.
Auto-detects source type, fetches via the right MCP/CLI/internal function,
normalizes to FrancoDocument, deduplicates, and ingests to rubick.db via
the learning pipeline.

Usage:
    rubick_franco.py collect <source> [--feature F] [--force]
    rubick_franco.py batch <sources_json> [--feature F]
    rubick_franco.py status [--feature F]
    rubick_franco.py refetch <feature>
    rubick_franco.py docs <path> [--feature F] [--project P]
"""

import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import brain_config as cfg
from rubick_ingest import detect_source, _extract_source_id, extract_entities_structural
from rubick_learn import record, flush, status as learn_status
from rubick_graph import get_db, get_node, upsert_node, upsert_edge


# ---------------------------------------------------------------------------
# FrancoDocument schema
# ---------------------------------------------------------------------------

def _empty_doc() -> dict:
    return {
        "source_type": "",
        "source_id": "",
        "title": "",
        "body": "",
        "author": "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "url": "",
        "metadata": {},
        "feature": "_global",
        "confidence": cfg.LEARNING_DEFAULT_CONFIDENCE,
        "node_type": "Signal",
        "edges": [],
    }


# ---------------------------------------------------------------------------
# Source → Node type mapping
# ---------------------------------------------------------------------------

_SOURCE_TO_NODE = {
    "slack_thread": "Signal",
    "slack_channel": "Signal",
    "slack_search": "Signal",
    "drive_doc": "Document",
    "drive_file": "Document",
    "drive_folder": "Document",
    "gmail_thread": "Email",
    "gmail_search": "Email",
    "github_pr": "PR",
    "github_issue": "JiraIssue",
    "github_commit": "Signal",
    "github_search": "Signal",
    "devrev_task": "JiraIssue",
    "devrev_id": "JiraIssue",
    "jira_issue": "JiraIssue",
    "jira_id": "JiraIssue",
    "figma": "Document",
    "gsheet": "Document",
    "slides": "Document",
    "calendar": "Meeting",
    "web_url": "WebPage",
    "local_file": "Document",
    "local_dir": "Document",
    "razorpay_docs": "Document",
    "repo_skill": "Document",
}

_SOURCE_TO_EDGE = {
    "slack_thread": "SIGNAL_FOR",
    "slack_channel": "SIGNAL_FOR",
    "slack_search": "SIGNAL_FOR",
    "drive_doc": "RELATES_TO",
    "drive_file": "RELATES_TO",
    "drive_folder": "RELATES_TO",
    "gmail_thread": "MENTIONED_IN",
    "gmail_search": "MENTIONED_IN",
    "github_pr": "IMPLEMENTS",
    "github_issue": "TRACKS",
    "github_commit": "RELATES_TO",
    "github_search": "RELATES_TO",
    "devrev_task": "TRACKS",
    "devrev_id": "TRACKS",
    "jira_issue": "TRACKS",
    "jira_id": "TRACKS",
    "figma": "RELATES_TO",
    "gsheet": "RELATES_TO",
    "slides": "RELATES_TO",
    "calendar": "RELATES_TO",
    "web_url": "RELATES_TO",
    "local_file": "RELATES_TO",
    "local_dir": "RELATES_TO",
    "razorpay_docs": "RELATES_TO",
    "repo_skill": "RELATES_TO",
}

# Internal source types that don't create new nodes
_INLINE_SOURCES = {"rubick_context", "expert_knowledge", "code_body"}


# ---------------------------------------------------------------------------
# Core: detect + normalize + ingest
# ---------------------------------------------------------------------------

def franco_collect(source: str, feature: str = "_global",
                   options: dict = None, force: bool = False) -> dict:
    """Auto-detect source, normalize, and ingest to rubick.db.

    Returns: {source_type, source_id, node_type, node_name, status, detail}

    Note: For MCP-backed sources (Slack, Gmail, Drive, Figma, Calendar),
    this function prepares the fetch instructions. The actual MCP call is
    made by the LLM caller (commands/franco.md skill). For CLI-backed
    sources (GitHub, DevRev) and local sources, this function fetches directly.
    """
    options = options or {}
    detected = detect_source(source)
    source_type = detected["source_type"]
    source_id = detected["source_id"]

    if source_type in _INLINE_SOURCES:
        return _handle_inline(source_type, source, options)

    prefix_handlers = {
        "expert": ("expert_knowledge", "query_expert"),
        "code": ("code_body", "get_code_body"),
        "context": ("rubick_context", "context_for_v2"),
    }
    first_word = source.strip().split()[0].lower() if source.strip() else ""
    if source_type == "unknown" and first_word in prefix_handlers:
        _, fn = prefix_handlers[first_word]
        return _handle_inline(fn, source, options)

    if source_type == "unknown":
        return _handle_search_command(source, feature, options)

    conn = get_db(str(cfg.RUBICK_DB_PATH))

    if not force and _is_cached(conn, source_type, source_id):
        conn.close()
        return {
            "source_type": source_type,
            "source_id": source_id,
            "status": "duplicate",
            "detail": f"Already ingested: {source_type}:{source_id}",
        }

    doc = _empty_doc()
    doc["source_type"] = source_type
    doc["source_id"] = source_id
    doc["feature"] = feature
    doc["url"] = detected.get("url", "")
    doc["node_type"] = _SOURCE_TO_NODE.get(source_type, "Signal")

    fetch_map = getattr(cfg, "FRANCO_FETCH_MAP", {})
    route = fetch_map.get(source_type, {})
    method = route.get("method", "")

    if method == "cli":
        doc = _fetch_cli(doc, detected, route, options)
    elif method == "read":
        doc = _fetch_local(doc, detected, options)
    elif method == "glob_read":
        return _fetch_local_dir(detected.get("path", source), feature, options)
    elif method == "mcp":
        doc["metadata"]["mcp_tool"] = f"mcp__{route.get('mcp', '')}__" + route.get("tool", "")
        doc["metadata"]["mcp_params"] = _build_mcp_params(source_type, detected, options)
        doc["metadata"]["fetch_pending"] = True
    elif method == "internal":
        return _handle_inline(route.get("fn", ""), source, options)
    else:
        doc["title"] = f"{source_type}:{source_id}"
        doc["body"] = source
        doc["metadata"]["raw_input"] = source

    doc["title"] = doc["title"] or f"{source_type}:{source_id}"
    node_name = f"{source_type}:{source_id}"

    edges = []
    edge_type = _SOURCE_TO_EDGE.get(source_type, "RELATES_TO")
    if feature and feature != "_global":
        edges.append({"to_type": "Feature", "to_name": feature, "edge_type": edge_type})

    if source_type == "github_pr":
        m = re.search(r"github\.com/([\w.-]+)/([\w.-]+)/pull/", source)
        if m:
            edges.append({"to_type": "Project", "to_name": m.group(2), "edge_type": "OPENS_PR"})

    result = record(
        interaction_type="franco_collect",
        source_skill="franco",
        items=[{
            "type": doc["node_type"],
            "name": node_name,
            "data": {
                "source_type": doc["source_type"],
                "source_id": doc["source_id"],
                "title": doc["title"],
                "body": doc["body"][:4000],
                "author": doc["author"],
                "timestamp": doc["timestamp"],
                "url": doc["url"],
                "metadata": doc["metadata"],
                "feature": doc["feature"],
            },
            "confidence": doc["confidence"],
            "edges": edges,
        }],
        project=feature,
    )

    flush_result = flush(interaction_id=result.get("interaction_id"))

    conn.close()
    return {
        "source_type": source_type,
        "source_id": source_id,
        "node_type": doc["node_type"],
        "node_name": node_name,
        "status": "ingested",
        "staged": result.get("staged", 0),
        "flushed": flush_result,
        "fetch_pending": doc.get("metadata", {}).get("fetch_pending", False),
        "mcp_tool": doc.get("metadata", {}).get("mcp_tool", ""),
    }


def franco_batch(sources: list[str], feature: str = "_global",
                 options: dict = None) -> list[dict]:
    """Collect multiple sources. Dedup + sequential."""
    results = []
    for src in sources:
        r = franco_collect(src, feature=feature, options=options)
        results.append(r)
    return results


def franco_ingest_mcp_response(source_type: str, source_id: str,
                               mcp_response: dict, feature: str = "_global") -> dict:
    """Ingest an MCP response that was fetched by the LLM caller.

    Called after the skill .md orchestrates the actual MCP tool call.
    """
    doc = _normalize_mcp_response(mcp_response, source_type)
    doc["source_type"] = source_type
    doc["source_id"] = source_id
    doc["feature"] = feature
    doc["node_type"] = _SOURCE_TO_NODE.get(source_type, "Signal")

    node_name = f"{source_type}:{source_id}"
    edges = []
    edge_type = _SOURCE_TO_EDGE.get(source_type, "RELATES_TO")
    if feature and feature != "_global":
        edges.append({"to_type": "Feature", "to_name": feature, "edge_type": edge_type})

    result = record(
        interaction_type="franco_ingest",
        source_skill="franco",
        items=[{
            "type": doc["node_type"],
            "name": node_name,
            "data": {
                "source_type": doc["source_type"],
                "source_id": doc["source_id"],
                "title": doc["title"],
                "body": doc["body"][:4000],
                "author": doc["author"],
                "timestamp": doc["timestamp"],
                "url": doc["url"],
                "metadata": doc["metadata"],
                "feature": doc["feature"],
            },
            "confidence": doc["confidence"],
            "edges": edges,
        }],
        project=feature,
    )

    flush_result = flush(interaction_id=result.get("interaction_id"))
    return {
        "source_type": source_type,
        "source_id": source_id,
        "node_name": node_name,
        "status": "ingested",
        "flushed": flush_result,
    }


# ---------------------------------------------------------------------------
# Dedup check
# ---------------------------------------------------------------------------

def _is_cached(conn, source_type: str, source_id: str) -> bool:
    """Check if a (source_type, source_id) pair already exists in the graph."""
    name = f"{source_type}:{source_id}"
    node_type = _SOURCE_TO_NODE.get(source_type, "Signal")
    existing = get_node(conn, node_type, name)
    return existing is not None


# ---------------------------------------------------------------------------
# CLI fetchers (GitHub, DevRev — can run directly)
# ---------------------------------------------------------------------------

def _fetch_cli(doc: dict, detected: dict, route: dict, options: dict) -> dict:
    """Fetch data via CLI command (gh, etc.)."""
    cmd_template = route.get("cmd", "")
    if not cmd_template:
        return doc

    url = detected.get("raw_input", "")
    params = _extract_cli_params(detected["source_type"], url)
    try:
        cmd = cmd_template.format(**params)
    except KeyError:
        doc["metadata"]["fetch_error"] = f"Missing params for: {cmd_template}"
        return doc

    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                doc = _normalize_cli_response(doc, detected["source_type"], data)
            except json.JSONDecodeError:
                doc["body"] = result.stdout[:4000]
                doc["title"] = doc["title"] or f"{detected['source_type']}:{detected['source_id']}"
        else:
            doc["metadata"]["fetch_error"] = result.stderr[:500]
    except subprocess.TimeoutExpired:
        doc["metadata"]["fetch_error"] = "CLI timeout (30s)"
    except Exception as e:
        doc["metadata"]["fetch_error"] = str(e)[:500]

    return doc


def _extract_cli_params(source_type: str, url: str) -> dict:
    """Extract CLI template parameters from URL."""
    params = {}
    if source_type == "github_pr":
        m = re.search(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)", url)
        if m:
            params = {"owner": m.group(1), "repo": m.group(2), "number": m.group(3)}
    elif source_type == "github_issue":
        m = re.search(r"github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)", url)
        if m:
            params = {"owner": m.group(1), "repo": m.group(2), "number": m.group(3)}
    elif source_type == "github_commit":
        m = re.search(r"github\.com/([\w.-]+)/([\w.-]+)/commit/([\da-f]+)", url)
        if m:
            params = {"owner": m.group(1), "repo": m.group(2), "sha": m.group(3)}
    elif source_type == "github_search":
        params = {"query": url, "repo": ""}
    elif source_type in ("devrev_task", "devrev_id"):
        m = re.search(r"((?:ISS|TKT|TASK)-\d+|[\w-]+$)", url)
        if m:
            params = {"id": m.group(1)}
    return params


def _normalize_cli_response(doc: dict, source_type: str, data: dict) -> dict:
    """Normalize a CLI JSON response into FrancoDocument fields."""
    if source_type == "github_pr":
        doc["title"] = data.get("title", "")
        body_parts = [data.get("body", "")]
        for comment in data.get("comments", [])[:10]:
            body_parts.append(f"--- {comment.get('author', {}).get('login', '?')}:\n{comment.get('body', '')}")
        doc["body"] = "\n\n".join(body_parts)[:4000]
        doc["author"] = data.get("author", {}).get("login", "")
        doc["metadata"]["files"] = [f.get("path", "") for f in data.get("files", [])[:50]]
        doc["metadata"]["state"] = data.get("state", "")
        doc["metadata"]["reviews"] = len(data.get("reviews", []))
    elif source_type == "github_issue":
        doc["title"] = data.get("title", "")
        body_parts = [data.get("body", "")]
        for comment in data.get("comments", [])[:10]:
            body_parts.append(f"--- {comment.get('author', {}).get('login', '?')}:\n{comment.get('body', '')}")
        doc["body"] = "\n\n".join(body_parts)[:4000]
        doc["author"] = data.get("author", {}).get("login", "")
        doc["metadata"]["labels"] = [lb.get("name", "") for lb in data.get("labels", [])]
        doc["metadata"]["state"] = data.get("state", "")
    return doc


# ---------------------------------------------------------------------------
# Local file fetchers
# ---------------------------------------------------------------------------

def _fetch_local(doc: dict, detected: dict, options: dict) -> dict:
    """Read a local file."""
    path = detected.get("path", detected.get("raw_input", ""))
    if not os.path.isfile(path):
        doc["metadata"]["fetch_error"] = f"File not found: {path}"
        return doc

    try:
        with open(path, "r", errors="replace") as f:
            content = f.read()
        doc["title"] = os.path.basename(path)
        doc["body"] = content[:8000]
        doc["metadata"]["file_path"] = path
        doc["metadata"]["file_size"] = os.path.getsize(path)

        entities = extract_entities_structural(content, "local_file")
        if entities.get("github_refs"):
            doc["metadata"]["github_refs"] = entities["github_refs"]
        if entities.get("jira_refs"):
            doc["metadata"]["jira_refs"] = entities["jira_refs"]
    except Exception as e:
        doc["metadata"]["fetch_error"] = str(e)[:500]

    return doc


def _fetch_local_dir(path: str, feature: str = "_global",
                     options: dict = None) -> dict:
    """Ingest all markdown/text files in a directory."""
    options = options or {}
    project = options.get("project", "_global")

    if not os.path.isdir(path):
        return {"status": "error", "detail": f"Not a directory: {path}"}

    patterns = ["**/*.md", "**/*.txt", "**/*.rst"]
    files = []
    for pattern in patterns:
        files.extend(glob.glob(os.path.join(path, pattern), recursive=True))

    files = sorted(set(files))
    if not files:
        return {"status": "empty", "detail": f"No markdown/text files in: {path}"}

    results = []
    for fpath in files:
        rel = os.path.relpath(fpath, path)
        try:
            with open(fpath, "r", errors="replace") as f:
                content = f.read()
        except Exception:
            continue

        source_id = hashlib.sha256(fpath.encode()).hexdigest()[:16]
        node_name = f"local_file:{source_id}"

        edges = []
        if feature and feature != "_global":
            edges.append({"to_type": "Feature", "to_name": feature, "edge_type": "RELATES_TO"})

        record(
            interaction_type="franco_docs",
            source_skill="franco",
            items=[{
                "type": "Document",
                "name": node_name,
                "data": {
                    "source_type": "local_file",
                    "source_id": source_id,
                    "title": rel,
                    "body": content[:4000],
                    "file_path": fpath,
                    "feature": feature,
                    "project_slug": project,
                },
                "confidence": 0.9,
                "edges": edges,
            }],
            project=project,
        )
        results.append({"file": rel, "source_id": source_id})

    flush_result = flush()
    return {
        "status": "ingested",
        "source_type": "local_dir",
        "files_ingested": len(results),
        "files": results[:20],
        "flushed": flush_result,
    }


# ---------------------------------------------------------------------------
# MCP param builders
# ---------------------------------------------------------------------------

def _build_mcp_params(source_type: str, detected: dict, options: dict) -> dict:
    """Build MCP tool parameters from detected source."""
    url = detected.get("raw_input", "")
    sid = detected.get("source_id", "")
    params = {}

    if source_type == "slack_thread":
        m = re.search(r"/archives/(\w+)/p(\d+)", url)
        if m:
            channel = m.group(1)
            ts = m.group(2)
            ts_formatted = ts[:10] + "." + ts[10:] if len(ts) > 10 else ts
            params = {"channel": channel, "thread_ts": ts_formatted}
    elif source_type == "slack_channel":
        m = re.search(r"/(archives|channels?)/(\w+)", url)
        if m:
            params = {"channel": m.group(2), "limit": options.get("limit", 50)}
    elif source_type == "drive_doc":
        m = re.search(r"/document/d/([\w-]+)", url)
        if m:
            params = {"document_id": m.group(1)}
    elif source_type == "drive_file":
        m = re.search(r"(?:file/d|open\?id=)([\w-]+)", url)
        if m:
            params = {"file_id": m.group(1)}
    elif source_type == "gmail_thread":
        m = re.search(r"#(?:inbox|all|sent)/([\w]+)", url)
        if m:
            params = {"thread_id": m.group(1)}
    elif source_type == "figma":
        m = re.search(r"figma\.com/(?:file|design)/([\w-]+)", url)
        if m:
            params = {"file_key": m.group(1)}
    elif source_type == "gsheet":
        m = re.search(r"/spreadsheets/d/([\w-]+)", url)
        if m:
            params = {"spreadsheet_id": m.group(1)}
            if options.get("range"):
                params["range"] = options["range"]
    elif source_type == "slides":
        m = re.search(r"/presentation/d/([\w-]+)", url)
        if m:
            params = {"presentation_id": m.group(1)}
    elif source_type == "calendar":
        params = {"event_id": sid}

    return params


# ---------------------------------------------------------------------------
# MCP response normalizer
# ---------------------------------------------------------------------------

def _normalize_mcp_response(response: dict, source_type: str) -> dict:
    """Normalize raw MCP response into FrancoDocument fields."""
    doc = _empty_doc()

    if source_type in ("slack_thread", "slack_channel"):
        messages = response.get("messages", response.get("replies", []))
        if isinstance(messages, list):
            parts = []
            for msg in messages[:50]:
                user = msg.get("user", msg.get("username", "?"))
                text = msg.get("text", "")
                parts.append(f"[{user}]: {text}")
            doc["body"] = "\n".join(parts)[:4000]
            doc["title"] = f"Slack {source_type.split('_')[1]} ({len(messages)} messages)"
            if messages:
                doc["author"] = messages[0].get("user", "")
        doc["confidence"] = 0.8

    elif source_type in ("drive_doc",):
        doc["title"] = response.get("title", response.get("name", ""))
        doc["body"] = response.get("body", response.get("content", ""))[:4000]
        doc["author"] = response.get("lastModifyingUser", {}).get("displayName", "")
        doc["confidence"] = 0.85

    elif source_type in ("drive_file",):
        doc["title"] = response.get("name", "")
        doc["body"] = response.get("content", response.get("text", ""))[:4000]
        doc["confidence"] = 0.85

    elif source_type in ("gmail_thread", "gmail_search"):
        messages = response.get("messages", [])
        if isinstance(messages, list):
            parts = []
            for msg in messages[:20]:
                frm = msg.get("from", msg.get("sender", "?"))
                subj = msg.get("subject", "")
                body = msg.get("body", msg.get("snippet", ""))
                parts.append(f"From: {frm}\nSubject: {subj}\n{body}")
            doc["body"] = "\n---\n".join(parts)[:4000]
            doc["title"] = messages[0].get("subject", "") if messages else ""
            doc["author"] = messages[0].get("from", "") if messages else ""
        doc["confidence"] = 0.8

    elif source_type == "figma":
        doc["title"] = response.get("name", response.get("fileName", ""))
        doc["body"] = json.dumps(response.get("components", response), indent=2)[:4000]
        doc["metadata"]["figma_data"] = {
            k: v for k, v in response.items()
            if k in ("name", "lastModified", "version", "components")
        }
        doc["confidence"] = 0.85

    elif source_type in ("gsheet", "slides"):
        doc["title"] = response.get("title", response.get("name", ""))
        doc["body"] = json.dumps(response.get("values", response.get("slides", response)), indent=2)[:4000]
        doc["confidence"] = 0.85

    elif source_type == "calendar":
        doc["title"] = response.get("summary", response.get("title", ""))
        doc["body"] = response.get("description", "")
        attendees = response.get("attendees", [])
        doc["metadata"]["attendees"] = [a.get("email", "") for a in attendees[:20]]
        doc["metadata"]["start"] = response.get("start", {}).get("dateTime", "")
        doc["metadata"]["end"] = response.get("end", {}).get("dateTime", "")
        doc["confidence"] = 0.9

    else:
        doc["title"] = response.get("title", response.get("name", str(source_type)))
        doc["body"] = json.dumps(response, indent=2)[:4000] if isinstance(response, dict) else str(response)[:4000]

    return doc


# ---------------------------------------------------------------------------
# Inline / internal handlers
# ---------------------------------------------------------------------------

def _handle_inline(fn_name: str, source: str, options: dict) -> dict:
    """Handle internal source types (rubick context, hero, code body)."""
    conn = get_db(str(cfg.RUBICK_DB_PATH))

    if fn_name in ("rubick_context", "context_for_v2"):
        try:
            from rubick_context import context_for_v2
            budget = options.get("budget", 4000)
            target = source.replace("rubick://", "").strip()
            ctx = context_for_v2(conn, target, budget=budget)
            conn.close()
            return {"source_type": "rubick_context", "status": "inline", "context": ctx}
        except ImportError:
            from rubick_context import context_for
            target = source.replace("rubick://", "").strip()
            ctx = context_for(conn, target, budget=options.get("budget", 4000))
            conn.close()
            return {"source_type": "rubick_context", "status": "inline", "context": ctx}

    elif fn_name in ("expert_knowledge", "query_expert"):
        project = source.replace("hero:", "").replace("hero ", "").strip()
        row = conn.execute(
            "SELECT id, name, data FROM nodes WHERE type = 'ProjectExpert' AND name LIKE ?",
            (f"%{project}%",)
        ).fetchone()
        conn.close()
        if row:
            return {
                "source_type": "expert_knowledge",
                "status": "inline",
                "expert": {"id": row["id"], "name": row["name"], "data": json.loads(row["data"])},
            }
        return {"source_type": "expert_knowledge", "status": "not_found", "project": project}

    elif fn_name in ("code_body", "get_code_body"):
        parts = source.replace("code:", "").replace("code ", "").strip().split(":", 1)
        node_type = parts[0] if len(parts) > 1 else "Function"
        node_name = parts[-1]
        from rubick_graph import get_code_body
        result = get_code_body(conn, node_type, node_name)
        conn.close()
        if result:
            return {"source_type": "code_body", "status": "inline", "code": result}
        return {"source_type": "code_body", "status": "not_found", "query": source}

    conn.close()
    return {"source_type": fn_name, "status": "unknown_internal"}


# ---------------------------------------------------------------------------
# Search command handler
# ---------------------------------------------------------------------------

def _handle_search_command(source: str, feature: str, options: dict) -> dict:
    """Handle 'search slack/gmail/github <query>' patterns."""
    parts = source.strip().split(None, 2)
    if len(parts) < 2 or parts[0].lower() != "search":
        return {
            "source_type": "unknown",
            "status": "unrecognized",
            "detail": f"Cannot detect source type for: {source}",
            "hint": "Try a URL, or 'search slack/gmail/github <query>'",
        }

    platform = parts[1].lower()
    query = parts[2] if len(parts) > 2 else ""

    fetch_map = getattr(cfg, "FRANCO_FETCH_MAP", {})

    if platform == "slack":
        route = fetch_map.get("slack_search", {})
        return {
            "source_type": "slack_search",
            "status": "search_pending",
            "query": query,
            "mcp_tool": f"mcp__{route.get('mcp', 'plugin_compass_slack-mcp')}__" + route.get("tool", "slack_search_messages"),
            "mcp_params": {"query": query},
        }
    elif platform == "gmail":
        route = fetch_map.get("gmail_search", {})
        return {
            "source_type": "gmail_search",
            "status": "search_pending",
            "query": query,
            "mcp_tool": f"mcp__{route.get('mcp', 'f22d0c2f')}__" + route.get("tool", "search_threads"),
            "mcp_params": {"query": query},
        }
    elif platform == "github":
        route = fetch_map.get("github_search", {})
        return {
            "source_type": "github_search",
            "status": "search_pending",
            "query": query,
            "cli_cmd": f"gh search code '{query}' --repo razorpay/ --json path,textMatches --limit 20",
        }
    else:
        return {
            "source_type": "unknown",
            "status": "unsupported_platform",
            "detail": f"Search not supported for: {platform}",
            "supported": ["slack", "gmail", "github"],
        }


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def franco_status(feature: str = None) -> dict:
    """Show what Franco has collected, optionally filtered by feature."""
    conn = get_db(str(cfg.RUBICK_DB_PATH))

    query = """
        SELECT ll.source_skill, ll.interaction_type, ll.node_type, ll.node_name,
               ll.status, ll.project_slug, ll.created_at
        FROM learning_ledger ll
        WHERE ll.source_skill = 'franco'
    """
    params = []
    if feature:
        query += " AND ll.project_slug = ?"
        params.append(feature)
    query += " ORDER BY ll.created_at DESC LIMIT 100"

    rows = conn.execute(query, params).fetchall()

    by_type = {}
    for row in rows:
        nt = row["node_type"]
        by_type[nt] = by_type.get(nt, 0) + 1

    by_status = {}
    for row in rows:
        st = row["status"]
        by_status[st] = by_status.get(st, 0) + 1

    conn.close()
    return {
        "total_interactions": len(rows),
        "by_node_type": by_type,
        "by_status": by_status,
        "feature_filter": feature,
        "recent": [
            {
                "node_type": r["node_type"],
                "node_name": r["node_name"],
                "status": r["status"],
                "created_at": r["created_at"],
            }
            for r in rows[:10]
        ],
    }


def franco_refetch(feature: str) -> dict:
    """Mark all Franco items for a feature as needing re-collection."""
    conn = get_db(str(cfg.RUBICK_DB_PATH))
    conn.execute(
        """UPDATE learning_ledger SET status = 'staged'
           WHERE source_skill = 'franco' AND project_slug = ? AND status = 'flushed'""",
        (feature,)
    )
    count = conn.execute("SELECT changes()").fetchone()[0]
    conn.commit()
    conn.close()
    flush_result = flush()
    return {"feature": feature, "restaged": count, "flushed": flush_result}


# ---------------------------------------------------------------------------
# Scheduled Pulls
# ---------------------------------------------------------------------------

def franco_scheduled_pull(sources_config: Optional[list] = None, dry_run: bool = False) -> dict:
    """Pull from configured sources if stale. Each source: {source_type, source_id, interval_hours}.

    Checks sync_state to decide whether to fetch. If dry_run=True, returns stale list without fetching.
    """
    if sources_config is None:
        sources_config = getattr(cfg, "FRANCO_SCHEDULED_SOURCES", [])

    if not sources_config:
        return {"skipped": True, "reason": "no sources configured"}

    conn = get_db(str(cfg.RUBICK_DB_PATH))
    now = datetime.now(timezone.utc)
    results = {"fetched": 0, "skipped": 0, "failed": 0, "stale": [], "details": []}

    for src in sources_config:
        source_type = src.get("source_type", "")
        source_id = src.get("source_id", "")
        interval_hours = src.get("interval_hours", 6)

        last_sync = conn.execute(
            "SELECT last_sync_at FROM sync_state WHERE source_type = ? AND source_id = ?",
            (source_type, source_id)
        ).fetchone()

        if last_sync and last_sync["last_sync_at"]:
            try:
                last_dt = datetime.fromisoformat(last_sync["last_sync_at"].replace("Z", "+00:00"))
                hours_since = (now - last_dt).total_seconds() / 3600
                if hours_since < interval_hours:
                    results["skipped"] += 1
                    results["details"].append({
                        "source": f"{source_type}:{source_id}",
                        "status": "fresh",
                        "hours_since": round(hours_since, 1),
                    })
                    continue
            except (ValueError, TypeError):
                pass

        results["stale"].append(f"{source_type}:{source_id}")
        if dry_run:
            results["details"].append({"source": f"{source_type}:{source_id}", "status": "stale"})
            continue

        try:
            r = franco_collect(source_id, feature="_global", force=False)
            results["fetched"] += 1
            results["details"].append({
                "source": f"{source_type}:{source_id}",
                "status": "fetched",
                "result": r.get("status", "ok"),
            })
        except Exception as e:
            results["failed"] += 1
            results["details"].append({
                "source": f"{source_type}:{source_id}",
                "status": "error",
                "error": str(e)[:200],
            })

    conn.close()
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Franco — Universal Data Collector")
    sub = parser.add_subparsers(dest="command")

    collect_p = sub.add_parser("collect", help="Collect from a single source")
    collect_p.add_argument("source", help="URL, ID, path, or query")
    collect_p.add_argument("--feature", default="_global")
    collect_p.add_argument("--force", action="store_true", help="Skip dedup check")

    batch_p = sub.add_parser("batch", help="Collect from multiple sources")
    batch_p.add_argument("sources_json", help="JSON file with list of source strings")
    batch_p.add_argument("--feature", default="_global")

    status_p = sub.add_parser("status", help="Show collection status")
    status_p.add_argument("--feature", default=None)

    refetch_p = sub.add_parser("refetch", help="Re-collect all sources for a feature")
    refetch_p.add_argument("feature")

    docs_p = sub.add_parser("docs", help="Ingest a directory of docs")
    docs_p.add_argument("path", help="Directory path")
    docs_p.add_argument("--feature", default="_global")
    docs_p.add_argument("--project", default="_global")

    sched_p = sub.add_parser("scheduled-pull", help="Run scheduled pull for configured sources")

    args = parser.parse_args()

    if args.command == "collect":
        result = franco_collect(args.source, feature=args.feature, force=args.force)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "batch":
        with open(args.sources_json) as f:
            sources = json.load(f)
        results = franco_batch(sources, feature=args.feature)
        print(json.dumps(results, indent=2, default=str))

    elif args.command == "status":
        result = franco_status(feature=args.feature)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "refetch":
        result = franco_refetch(args.feature)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "docs":
        result = _fetch_local_dir(args.path, feature=args.feature,
                                  options={"project": args.project})
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "scheduled-pull":
        result = franco_scheduled_pull()
        print(json.dumps(result, indent=2, default=str))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
