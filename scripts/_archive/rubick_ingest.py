#!/usr/bin/env python3
"""Rubick Ingestion Engine — universal source detection, entity extraction, graph linking.

Single entry point: `brain ingest <url_or_id>` auto-detects source type and ingests.
Supports: Slack threads/channels, Gmail threads, Google Drive docs, GitHub PRs/issues/commits,
Jira issues, web URLs, local files.

Architecture:
  1. Source Detection — regex patterns match URL/ID to platform
  2. Fetch — MCP tool calls retrieve raw content (handled by LLM caller, not this script)
  3. Entity Extraction — structural parsing + LLM classification stub
  4. Graph Linking — upsert nodes/edges into rubick.db
  5. Sync State — update incremental sync cursors

This script provides the deterministic infrastructure. LLM-dependent steps
(content summarization, urgency scoring, entity extraction) are stubbed
with reasonable defaults — the brain SKILL.md orchestrates the full pipeline.
"""

import json
import re
import sys
import os
import argparse
import hashlib
from datetime import datetime, timezone
from typing import Optional

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import brain_config as cfg
    from rubick_graph import (
        get_db, upsert_node, upsert_edge, get_node, sync_update,
        _parse_iso,
    )
except ImportError as e:
    print(f"Import error: {e}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Source Detection
# ---------------------------------------------------------------------------

_SOURCE_PATTERNS = cfg.SOURCE_PATTERNS if cfg else {}
_URGENCY_KEYWORDS = cfg.URGENCY_KEYWORDS if cfg else [
    "urgent", "blocker", "blocked", "P0", "hotfix", "ASAP",
    "critical", "incident", "outage", "production",
]


def detect_source(input_str: str) -> dict:
    """Detect source type from a URL, ID, or path.

    Returns: {source_type, source_id, platform, metadata}
    """
    input_str = input_str.strip()

    for stype, pattern in _SOURCE_PATTERNS.items():
        if re.match(pattern, input_str):
            source_id = _extract_source_id(stype, input_str)
            platform = stype.split("_")[0]
            return {
                "source_type": stype,
                "source_id": source_id,
                "platform": platform,
                "url": input_str if input_str.startswith("http") else None,
                "raw_input": input_str,
            }

    if os.path.exists(input_str):
        return {
            "source_type": "local_file",
            "source_id": hashlib.sha256(input_str.encode()).hexdigest()[:16],
            "platform": "local",
            "path": input_str,
            "raw_input": input_str,
        }

    if input_str.startswith("http"):
        return {
            "source_type": "web_url",
            "source_id": hashlib.sha256(input_str.encode()).hexdigest()[:16],
            "platform": "web",
            "url": input_str,
            "raw_input": input_str,
        }

    return {
        "source_type": "unknown",
        "source_id": input_str,
        "platform": "unknown",
        "raw_input": input_str,
    }


def _extract_source_id(source_type: str, url: str) -> str:
    """Extract a stable ID from a URL based on source type."""
    if source_type == "slack_thread":
        m = re.search(r"/archives/(\w+)/p(\d+)", url)
        if m:
            return f"{m.group(1)}:{m.group(2)}"
    elif source_type == "slack_channel":
        m = re.search(r"/(archives|channels?)/(\w+)", url)
        if m:
            return m.group(2)
    elif source_type == "drive_doc":
        m = re.search(r"/document/d/([\w-]+)", url)
        if m:
            return m.group(1)
    elif source_type in ("drive_file", "drive_folder"):
        m = re.search(r"(?:file/d|open\?id=|folders/)([\w-]+)", url)
        if m:
            return m.group(1)
    elif source_type == "gmail_thread":
        m = re.search(r"#(?:inbox|all|sent)/([\w]+)", url)
        if m:
            return m.group(1)
    elif source_type == "github_pr":
        m = re.search(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)", url)
        if m:
            return f"{m.group(1)}/{m.group(2)}#{m.group(3)}"
    elif source_type == "github_issue":
        m = re.search(r"github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)", url)
        if m:
            return f"{m.group(1)}/{m.group(2)}#{m.group(3)}"
    elif source_type == "github_commit":
        m = re.search(r"github\.com/([\w.-]+)/([\w.-]+)/commit/([\da-f]+)", url)
        if m:
            return f"{m.group(1)}/{m.group(2)}@{m.group(3)[:7]}"
    elif source_type in ("jira_issue", "jira_id"):
        m = re.search(r"([A-Z][A-Z0-9]+-\d+)", url)
        if m:
            return m.group(1)
    elif source_type == "devrev_task":
        m = re.search(r"(?:works|tasks)/([\w-]+)", url)
        if m:
            return m.group(1)
    elif source_type == "devrev_id":
        m = re.search(r"((?:ISS|TKT|TASK)-\d+)", url)
        if m:
            return m.group(1)
    return hashlib.sha256(url.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Urgency Detection
# ---------------------------------------------------------------------------

def detect_urgency(text: str) -> dict:
    """Scan text for urgency signals. Returns score and matched keywords."""
    text_lower = text.lower()
    matched = [kw for kw in _URGENCY_KEYWORDS if kw.lower() in text_lower]

    if not matched:
        return {"urgency_score": 0.2, "matched_keywords": [], "action_required": False}

    score = min(0.3 + 0.15 * len(matched), 1.0)
    action_required = any(kw in matched for kw in ["blocker", "blocked", "ASAP", "urgent", "P0", "hotfix"])

    return {
        "urgency_score": round(score, 2),
        "matched_keywords": matched,
        "action_required": action_required,
    }


# ---------------------------------------------------------------------------
# Entity Extraction (Structural Pass)
# ---------------------------------------------------------------------------

def extract_entities_structural(text: str, source_type: str) -> dict:
    """Extract entities from text using regex/heuristic patterns.

    This is the fast structural pass. LLM pass adds semantic extraction.
    Returns: {people, tasks, decisions, jira_refs, github_refs, urls}
    """
    entities: dict[str, list] = {
        "people": [],
        "tasks": [],
        "decisions": [],
        "jira_refs": [],
        "github_refs": [],
        "urls": [],
    }

    emails = set(re.findall(r'[\w.+-]+@[\w.-]+\.\w+', text))
    for email in emails:
        entities["people"].append({"email": email, "source": source_type})

    slack_mentions = set(re.findall(r'<@(\w+)>', text))
    for sid in slack_mentions:
        entities["people"].append({"slack_id": sid, "source": source_type})

    jira_ids = set(re.findall(r'\b([A-Z][A-Z0-9]+-\d+)\b', text))
    for jid in jira_ids:
        entities["jira_refs"].append({"key": jid})

    pr_refs = set(re.findall(r'(?:https?://)?github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)', text))
    for repo, num in pr_refs:
        entities["github_refs"].append({"repo": repo, "type": "pr", "number": int(num)})

    issue_refs = set(re.findall(r'(?:https?://)?github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)', text))
    for repo, num in issue_refs:
        entities["github_refs"].append({"repo": repo, "type": "issue", "number": int(num)})

    commit_refs = set(re.findall(r'(?:https?://)?github\.com/([\w.-]+/[\w.-]+)/commit/([\da-f]{7,40})', text))
    for repo, sha in commit_refs:
        entities["github_refs"].append({"repo": repo, "type": "commit", "sha": sha[:7]})

    urls = set(re.findall(r'https?://[^\s<>"\']+', text))
    entities["urls"] = [{"url": u} for u in urls]

    action_patterns = [
        r'(?:TODO|ACTION|TASK)[\s:]+(.+?)(?:\n|$)',
        r'(?:@\w+)\s+(?:please|can you|will you)\s+(.+?)(?:\n|$)',
    ]
    for pattern in action_patterns:
        for match in re.findall(pattern, text, re.IGNORECASE):
            entities["tasks"].append({"title": match.strip()[:200], "source": source_type})

    decision_patterns = [
        r'(?:DECISION|DECIDED|AGREED)[\s:]+(.+?)(?:\n|$)',
        r'(?:we\'ll go with|let\'s go with|decision is)\s+(.+?)(?:\n|$)',
    ]
    for pattern in decision_patterns:
        for match in re.findall(pattern, text, re.IGNORECASE):
            entities["decisions"].append({"title": match.strip()[:200], "source": source_type})

    return entities


# ---------------------------------------------------------------------------
# Graph Linking
# ---------------------------------------------------------------------------

def ingest_signal(conn, source_type: str, source_id: str,
                  content_summary: str = "",
                  raw_metadata: Optional[dict] = None,
                  urgency_score: float = 0.2,
                  action_required: bool = False,
                  timestamp: Optional[str] = None,
                  project_slug: Optional[str] = None,
                  signal_type: Optional[str] = None) -> dict:
    """Ingest a signal into the graph. Deduplicates on (source_type, source_id)."""
    name = f"{source_type}:{source_id}"

    existing = get_node(conn, "Signal", name)
    if existing:
        return {"status": "duplicate", "name": name}

    now_iso = timestamp or datetime.now(timezone.utc).isoformat()
    data = {
        "signal_type": signal_type or source_type,
        "source_id": source_id,
        "content_summary": content_summary[:500] if content_summary else "",
        "raw_metadata": json.dumps(raw_metadata or {}),
        "timestamp": now_iso,
        "urgency_score": urgency_score,
        "action_required": action_required,
        "processed": False,
        "project_slug": project_slug,
    }
    nid = upsert_node(conn, "Signal", name, data,
                      source_type=source_type, source_id=source_id,
                      confidence=0.8)

    if project_slug:
        upsert_edge(conn, "Signal", name, "Project", project_slug, "PART_OF")

    return {"status": "ingested", "id": nid, "name": name, "data": data}


def ingest_person(conn, name: str, email: str = "",
                  slack_id: str = "", role: str = "") -> dict:
    """Ingest or update a Person node. Deduplicates on name."""
    existing = get_node(conn, "Person", name)
    data = {
        "email": email, "slack_id": slack_id, "role": role,
        "first_seen": datetime.now(timezone.utc).isoformat(),
    }
    if existing:
        old_data = json.loads(existing.get("data") or "{}")
        if email and not old_data.get("email"):
            old_data["email"] = email
        if slack_id and not old_data.get("slack_id"):
            old_data["slack_id"] = slack_id
        if role and not old_data.get("role"):
            old_data["role"] = role
        data = old_data

    nid = upsert_node(conn, "Person", name, data, source_type="ingest")
    return {"status": "upserted", "id": nid, "name": name}


def ingest_decision(conn, title: str, context: str = "",
                    outcome: str = "", decided_by: str = "",
                    source: str = "", project_slug: Optional[str] = None) -> dict:
    """Ingest a Decision node."""
    data = {
        "context": context, "outcome": outcome,
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "decided_by": decided_by, "source": source,
        "reversible": True, "project_slug": project_slug,
    }
    nid = upsert_node(conn, "Decision", title, data, source_type="ingest")
    if project_slug:
        upsert_edge(conn, "Decision", title, "Project", project_slug, "PART_OF")
    return {"status": "created", "id": nid, "name": title}


def ingest_task_from_signal(conn, title: str, source: str = "",
                            urgency_score: float = 0.4,
                            action_type: str = "needs_response",
                            project_slug: Optional[str] = None) -> dict:
    """Create a Task node from an extracted action item."""
    existing = get_node(conn, "Task", title)
    if existing:
        return {"status": "exists", "name": title}

    data = {
        "status": "open", "source": source,
        "urgency_score": urgency_score,
        "stakeholder_score": 0.5,
        "estimated_hours": 1.0,
        "action_type": action_type,
        "project_slug": project_slug,
    }
    nid = upsert_node(conn, "Task", title, data, source_type="ingest")
    if project_slug:
        upsert_edge(conn, "Task", title, "Project", project_slug, "PART_OF")
    return {"status": "created", "id": nid, "name": title}


def link_entities_to_signal(conn, signal_name: str,
                            entities: dict, project_slug: Optional[str] = None) -> dict:
    """Link extracted entities to a Signal node in the graph."""
    links = {"people": 0, "tasks": 0, "decisions": 0, "jira_refs": 0, "github_refs": 0}

    for p in entities.get("people", []):
        person_name = p.get("email") or p.get("slack_id") or "unknown"
        ingest_person(conn, person_name, email=p.get("email", ""), slack_id=p.get("slack_id", ""))
        upsert_edge(conn, "Signal", signal_name, "Person", person_name, "MENTIONED_IN")
        links["people"] += 1

    for t in entities.get("tasks", []):
        result = ingest_task_from_signal(conn, t["title"], source=t.get("source", ""),
                                         project_slug=project_slug)
        if result["status"] in ("created", "exists"):
            upsert_edge(conn, "Signal", signal_name, "Task", t["title"], "SIGNAL_FOR")
            links["tasks"] += 1

    for d in entities.get("decisions", []):
        ingest_decision(conn, d["title"], source=d.get("source", ""), project_slug=project_slug)
        upsert_edge(conn, "Decision", d["title"], "Signal", signal_name, "DECIDED_BY")
        links["decisions"] += 1

    for j in entities.get("jira_refs", []):
        existing = get_node(conn, "JiraIssue", j["key"])
        if not existing:
            upsert_node(conn, "JiraIssue", j["key"], {
                "key": j["key"], "project_slug": project_slug,
            }, source_type="ingest")
        upsert_edge(conn, "Signal", signal_name, "JiraIssue", j["key"], "MENTIONED_IN")
        links["jira_refs"] += 1

    for g in entities.get("github_refs", []):
        if g["type"] == "pr":
            name = f"{g['repo']}#{g['number']}"
            existing = get_node(conn, "PR", name)
            if not existing:
                upsert_node(conn, "PR", name, {
                    "number": g["number"], "project_slug": project_slug,
                }, source_type="ingest")
            upsert_edge(conn, "Signal", signal_name, "PR", name, "MENTIONED_IN")
        elif g["type"] == "issue":
            name = f"{g['repo']}#{g['number']}"
            upsert_edge(conn, "Signal", signal_name, "JiraIssue", name, "MENTIONED_IN")
        elif g["type"] == "commit":
            name = f"{g['repo']}@{g['sha']}"
            existing = get_node(conn, "Commit", name)
            if not existing:
                upsert_node(conn, "Commit", name, {
                    "short_hash": g["sha"], "project_slug": project_slug,
                }, source_type="ingest")
            upsert_edge(conn, "Signal", signal_name, "Commit", name, "MENTIONED_IN")
        links["github_refs"] += 1

    return {"signal": signal_name, "links": links}


# ---------------------------------------------------------------------------
# Full Ingestion Pipeline
# ---------------------------------------------------------------------------

def ingest_text(conn, text: str, source_type: str, source_id: str,
                content_summary: str = "",
                project_slug: Optional[str] = None,
                timestamp: Optional[str] = None) -> dict:
    """Full ingestion pipeline for a text blob.

    1. Detect urgency
    2. Extract entities (structural)
    3. Create Signal node
    4. Link entities
    5. Update sync state
    """
    urgency = detect_urgency(text)

    entities = extract_entities_structural(text, source_type)

    signal_result = ingest_signal(
        conn, source_type=source_type, source_id=source_id,
        content_summary=content_summary or text[:200],
        raw_metadata={"text_length": len(text), "entities_found": {k: len(v) for k, v in entities.items()}},
        urgency_score=urgency["urgency_score"],
        action_required=urgency["action_required"],
        timestamp=timestamp,
        project_slug=project_slug,
        signal_type=source_type,
    )

    if signal_result["status"] == "duplicate":
        return signal_result

    link_result = link_entities_to_signal(
        conn, signal_result["name"], entities, project_slug=project_slug
    )

    sync_update(conn, source_type, source_id, project_slug=project_slug or "_global")

    return {
        "status": "ingested",
        "signal": signal_result,
        "urgency": urgency,
        "entities": {k: len(v) for k, v in entities.items()},
        "links": link_result["links"],
    }


def ingest_email(conn, thread_id: str, subject: str, body: str,
                 sender: str = "", date: str = "",
                 project_slug: Optional[str] = None) -> dict:
    """Ingest a Gmail thread as Email + Signal nodes."""
    email_data = {
        "thread_id": thread_id, "subject": subject,
        "date": date or datetime.now(timezone.utc).isoformat(),
        "has_decisions": False, "has_action_items": False,
        "body": body[:5000],
    }
    upsert_node(conn, "Email", thread_id, email_data,
                source_type="gmail", source_id=thread_id)

    if sender:
        ingest_person(conn, sender, email=sender)
        upsert_edge(conn, "Person", sender, "Email", thread_id, "AUTHORED_BY")

    result = ingest_text(conn, body, source_type="email", source_id=thread_id,
                         content_summary=subject, project_slug=project_slug,
                         timestamp=date)

    upsert_edge(conn, "Signal", f"email:{thread_id}", "Email", thread_id, "DISCUSSED_IN")
    return result


def ingest_slack_thread(conn, channel_id: str, thread_ts: str,
                        messages: list[dict],
                        project_slug: Optional[str] = None) -> dict:
    """Ingest a Slack thread as a Signal with extracted entities."""
    full_text = "\n".join(
        f"{m.get('user', '?')}: {m.get('text', '')}" for m in messages
    )
    source_id = f"{channel_id}:{thread_ts}"

    result = ingest_text(conn, full_text, source_type="slack_thread",
                         source_id=source_id,
                         content_summary=messages[0].get("text", "")[:200] if messages else "",
                         project_slug=project_slug)

    channel_node = get_node(conn, "SlackChannel", channel_id)
    if channel_node:
        signal_name = f"slack_thread:{source_id}"
        upsert_edge(conn, "Signal", signal_name, "SlackChannel", channel_id, "SYNCED_FROM")

    return result


def ingest_meeting(conn, event_id: str, title: str,
                   start: str, end: str,
                   participants: Optional[list[str]] = None,
                   notes: str = "",
                   project_slug: Optional[str] = None) -> dict:
    """Ingest a calendar event as a Meeting node."""
    start_dt = _parse_iso(start)
    end_dt = _parse_iso(end)
    duration = int((end_dt - start_dt).total_seconds() / 60) if start_dt and end_dt else 0

    meeting_type = "ad-hoc"
    title_lower = title.lower()
    if any(kw in title_lower for kw in ["standup", "daily", "scrum"]):
        meeting_type = "standup"
    elif any(kw in title_lower for kw in ["sprint", "planning", "backlog"]):
        meeting_type = "planning"
    elif any(kw in title_lower for kw in ["review", "retro", "demo"]):
        meeting_type = "review"

    data = {
        "title": title, "date": start,
        "duration_minutes": duration,
        "type": meeting_type,
        "participants": json.dumps(participants or []),
    }
    upsert_node(conn, "Meeting", event_id, data,
                source_type="calendar", source_id=event_id)

    if participants:
        for p in participants:
            ingest_person(conn, p, email=p if "@" in p else "")
            upsert_edge(conn, "Person", p, "Meeting", event_id, "ATTENDED")

    if project_slug:
        upsert_edge(conn, "Meeting", event_id, "Project", project_slug, "PART_OF")

    if notes:
        ingest_text(conn, notes, source_type="meeting_notes",
                    source_id=event_id, content_summary=title,
                    project_slug=project_slug, timestamp=start)

    return {"status": "ingested", "meeting_id": event_id, "type": meeting_type, "duration": duration}


def ingest_commit(conn, hash_str: str, subject: str,
                  author: str = "", date: str = "",
                  files_changed: int = 0, insertions: int = 0,
                  deletions: int = 0, pr_ref: str = "",
                  project_slug: Optional[str] = None) -> dict:
    """Ingest a git commit."""
    data = {
        "hash": hash_str, "short_hash": hash_str[:7],
        "subject": subject, "date": date or datetime.now(timezone.utc).isoformat(),
        "files_changed": files_changed,
        "insertions": insertions, "deletions": deletions,
        "pr_ref": pr_ref, "project_slug": project_slug,
    }
    nid = upsert_node(conn, "Commit", hash_str[:7], data,
                      source_type="github", source_id=hash_str)

    if author:
        ingest_person(conn, author, email=author if "@" in author else "")
        upsert_edge(conn, "Commit", hash_str[:7], "Person", author, "AUTHORED_BY")

    if project_slug:
        upsert_edge(conn, "Commit", hash_str[:7], "Project", project_slug, "PART_OF")

    return {"status": "ingested", "id": nid, "hash": hash_str[:7]}


def ingest_document(conn, title: str, source_url: str = "",
                    source_type_doc: str = "google_doc",
                    content: str = "", owner: str = "",
                    project_slug: Optional[str] = None) -> dict:
    """Ingest a document (Google Doc, PDF, etc.)."""
    content_hash = hashlib.sha256(content.encode()).hexdigest() if content else ""
    data = {
        "hash": content_hash, "title": title,
        "source_url": source_url,
        "source_type": source_type_doc,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "owner": owner,
    }
    nid = upsert_node(conn, "Document", title, data,
                      source_type="drive", source_id=source_url)

    if owner:
        ingest_person(conn, owner, email=owner if "@" in owner else "")

    if project_slug:
        upsert_edge(conn, "Document", title, "Project", project_slug, "PART_OF")

    if content:
        entities = extract_entities_structural(content, "document")
        for j in entities.get("jira_refs", []):
            upsert_edge(conn, "Document", title, "JiraIssue", j["key"], "REFERENCES")

    return {"status": "ingested", "id": nid, "title": title}


def ingest_web_page(conn, url: str, title: str = "",
                    content: str = "",
                    project_slug: Optional[str] = None) -> dict:
    """Ingest a web page."""
    data = {
        "url": url, "title": title or url,
        "content_summary": content[:500] if content else "",
        "raw_content": content[:10000] if content else "",
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }
    nid = upsert_node(conn, "WebPage", url, data,
                      source_type="web", source_id=url)

    if project_slug:
        upsert_edge(conn, "WebPage", url, "Project", project_slug, "PART_OF")

    return {"status": "ingested", "id": nid, "url": url}


# ---------------------------------------------------------------------------
# Batch Ingestion
# ---------------------------------------------------------------------------

def ingest_batch(conn, items: list[dict],
                 project_slug: Optional[str] = None) -> dict:
    """Ingest a batch of items. Each item needs: source_type, source_id, text/content."""
    results = {"ingested": 0, "duplicates": 0, "errors": 0}
    max_batch = cfg.INGEST_MAX_BATCH if cfg else 50

    for item in items[:max_batch]:
        try:
            stype = item.get("source_type", "unknown")
            sid = item.get("source_id", "")
            text = item.get("text") or item.get("content", "")

            r = ingest_text(conn, text, stype, sid,
                            content_summary=item.get("summary", ""),
                            project_slug=project_slug or item.get("project_slug"),
                            timestamp=item.get("timestamp"))

            if r.get("status") == "duplicate":
                results["duplicates"] += 1
            else:
                results["ingested"] += 1
        except Exception as e:
            results["errors"] += 1
            logger_msg = f"batch ingest error for {item.get('source_id', '?')}: {e}"
            print(logger_msg, file=sys.stderr)

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Rubick Ingestion Engine")
    sub = parser.add_subparsers(dest="command")

    det = sub.add_parser("detect", help="Detect source type from URL/ID")
    det.add_argument("input", help="URL, ID, or file path")

    ing = sub.add_parser("ingest-text", help="Ingest raw text")
    ing.add_argument("--db", default=None)
    ing.add_argument("--source-type", required=True)
    ing.add_argument("--source-id", required=True)
    ing.add_argument("--text", default=None, help="Text content (or read from stdin)")
    ing.add_argument("--summary", default="")
    ing.add_argument("--project", default=None)

    urg = sub.add_parser("urgency", help="Detect urgency in text")
    urg.add_argument("text", help="Text to analyze")

    ext = sub.add_parser("extract", help="Extract entities from text")
    ext.add_argument("--source-type", default="unknown")
    ext.add_argument("text", help="Text to analyze")

    batch = sub.add_parser("ingest-batch", help="Ingest batch from JSON file")
    batch.add_argument("--db", default=None)
    batch.add_argument("--file", required=True, help="Path to JSON array of items")
    batch.add_argument("--project", default=None)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "detect":
        result = detect_source(args.input)
        print(json.dumps(result, indent=2))

    elif args.command == "urgency":
        result = detect_urgency(args.text)
        print(json.dumps(result, indent=2))

    elif args.command == "extract":
        result = extract_entities_structural(args.text, args.source_type)
        print(json.dumps(result, indent=2, default=list))

    elif args.command == "ingest-text":
        db_path = args.db or (str(cfg.RUBICK_DB_PATH) if cfg else "workspace/rubick.db")
        conn = get_db(db_path)
        text = args.text or sys.stdin.read()
        result = ingest_text(conn, text, args.source_type, args.source_id,
                             content_summary=args.summary,
                             project_slug=args.project)
        print(json.dumps(result, indent=2, default=str))
        conn.close()

    elif args.command == "ingest-batch":
        db_path = args.db or (str(cfg.RUBICK_DB_PATH) if cfg else "workspace/rubick.db")
        conn = get_db(db_path)
        with open(args.file) as f:
            items = json.load(f)
        result = ingest_batch(conn, items, project_slug=args.project)
        print(json.dumps(result, indent=2))
        conn.close()


if __name__ == "__main__":
    main()
