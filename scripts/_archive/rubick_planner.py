#!/usr/bin/env python3
"""Omni Planner — Interactive daily planner powered by the Rubick knowledge graph.

Queries rubick.db for tasks, signals, features, meetings, emails, PRs and produces
structured JSON that Claude renders as a beautiful interactive dashboard.

All operations are read-only against external systems. Writes only go to rubick.db
(task nodes, plan nodes, daily log entries).

Integrates with rubick_graph.py planner engine (DAG, topo sort, CPM, priority scoring,
calendar slot building, capacity analysis, slot matching, conflict detection) and
rubick_context.py (BFS context retrieval, recall).
"""

import sys
import os
import json
import argparse
import tempfile
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import brain_config as cfg
from rubick_graph import (
    get_db, upsert_node, upsert_edge, get_node, get_neighbors,
    search_text, dag_build, topo_sort, critical_path, priority_score,
    build_slots, capacity, plan as graph_plan,
    feature_health, feature_timeline, get_stats,
    _parse_iso, _node_data,
)
from rubick_context import context_for, recall

DB_PATH = str(cfg.RUBICK_DB_PATH)
USER_EMAIL = "saurav.k@razorpay.com"
IST = timezone(timedelta(hours=5, minutes=30))

# Priority → scoring field derivation tables
# Maps human-friendly priority to the fields the priority_score() engine actually reads
_PRIORITY_TO_URGENCY: dict[str, float] = {
    "critical": 0.95, "high": 0.75, "medium": 0.45, "low": 0.2,
}
_PRIORITY_TO_ACTION: dict[str, str] = {
    "critical": "blocks_others", "high": "needs_response", "medium": "fyi", "low": "fyi",
}
_PRIORITY_TO_STAKEHOLDER: dict[str, float] = {
    "critical": 0.9, "high": 0.7, "medium": 0.5, "low": 0.3,
}

# Keywords that boost scoring fields (scanned in task titles)
_BLOCKER_KEYWORDS = {"block", "deploy", "hotfix", "prod", "incident", "outage", "P0", "ASAP"}
_RESPONSE_KEYWORDS = {"review", "reply", "respond", "update", "feedback", "approve"}
_HIGH_STAKEHOLDER_KEYWORDS = {"manager", "lead", "director", "PM", "CTO", "VP", "cross-team"}


def _derive_scoring_fields(title: str, priority: str, feature: str = "") -> dict:
    """Derive urgency_score, action_type, stakeholder_score from task metadata.

    The priority_score() engine in rubick_graph.py reads these three fields.
    We derive them from the human-friendly priority level, then refine
    using keyword detection in the title.
    """
    title_lower = title.lower()
    words = set(title_lower.split())

    # Base urgency from priority level
    urgency = _PRIORITY_TO_URGENCY.get(priority, 0.45)

    # Boost urgency if title contains urgent keywords
    for kw in cfg.URGENCY_KEYWORDS:
        if kw.lower() in title_lower:
            urgency = min(urgency + 0.2, 1.0)
            break

    # Action type from priority, refined by keywords
    action = _PRIORITY_TO_ACTION.get(priority, "fyi")
    if words & _BLOCKER_KEYWORDS:
        action = "blocks_others"
    elif words & _RESPONSE_KEYWORDS:
        action = "needs_response"

    # Stakeholder score from priority, boosted if has feature or stakeholder keywords
    stakeholder = _PRIORITY_TO_STAKEHOLDER.get(priority, 0.5)
    if feature:
        stakeholder = max(stakeholder, 0.65)  # Part of a tracked effort
    if words & _HIGH_STAKEHOLDER_KEYWORDS:
        stakeholder = min(stakeholder + 0.2, 1.0)

    return {
        "urgency_score": round(urgency, 2),
        "action_type": action,
        "stakeholder_score": round(stakeholder, 2),
    }


# ============================================================================
# DB helpers
# ============================================================================

def _db():
    return get_db(DB_PATH)

def _data(row):
    try:
        raw = row["data"]
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError, KeyError):
        return {}

def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def _today():
    return datetime.now(IST).strftime("%Y-%m-%d")

def _ist_now():
    return datetime.now(IST)


def _parse_time_display(raw_time):
    """Parse an ISO timestamp or HH:MM string into HH:MM display format.
    Returns (display_time, original_iso) tuple."""
    if not raw_time:
        return ("", "")
    if "T" in raw_time:
        parsed = _parse_iso(raw_time)
        if parsed:
            local = parsed.astimezone(IST)
            return (local.strftime("%H:%M"), raw_time)
        return (raw_time, raw_time)
    if len(raw_time) == 5 and ":" in raw_time:
        return (raw_time, "")
    return (raw_time, "")


# ============================================================================
# Slots bridge — temp file for planner engine
# ============================================================================

def _build_slots_file(conn, scope_days=1):
    """Build a slots JSON file from meeting nodes. Returns temp file path."""
    today = _today()
    meetings = _get_meetings(conn, today)

    meetings_json = []
    for m in meetings:
        start_iso = m.get("start_time_iso", "")
        end_iso = m.get("end_time_iso", "")
        if start_iso and end_iso:
            meetings_json.append({
                "start": start_iso,
                "end": end_iso,
                "title": m.get("title", ""),
            })

    slots_data = build_slots(
        meetings_json,
        working_hours_start=cfg.WORKING_HOURS_START,
        working_hours_end=cfg.WORKING_HOURS_END,
        timezone_str=cfg.DEFAULT_TIMEZONE,
        min_slot_min=cfg.SLOT_MIN_DURATION_MIN,
        scope_days=scope_days,
        ref_date=today,
    )

    tmp = tempfile.NamedTemporaryFile(
        mode='w', suffix='.json', prefix='slots_',
        dir=str(cfg.TMP_DIR), delete=False
    )
    json.dump(slots_data, tmp)
    tmp.close()
    return tmp.name


# ============================================================================
# 1. DASHBOARD — The main view
# ============================================================================

def dashboard():
    """Generate the full daily dashboard: calendar, tasks, missed comms, features, alerts, capacity."""
    conn = _db()
    today = _today()
    ist = _ist_now()
    hour = ist.hour

    _auto_spawn_recurring(conn, today, ist)

    result = {
        "date": today,
        "time": ist.strftime("%H:%M IST"),
        "greeting": _greeting(hour),
        "sections": {}
    }

    # --- Calendar / Meetings ---
    meetings = _get_meetings(conn, today)
    result["sections"]["meetings"] = {
        "title": "Today's Calendar",
        "count": len(meetings),
        "items": meetings,
        "next_meeting": _next_meeting(meetings, ist),
    }

    # --- Active Tasks (with live priority scoring) ---
    tasks = _get_tasks(conn, scope="today")
    result["sections"]["tasks"] = {
        "title": "Today's Tasks",
        "count": len(tasks),
        "items": tasks,
        "completed": sum(1 for t in tasks if t.get("status") == "done"),
        "blocked": [t for t in tasks if t.get("status") == "blocked"],
    }

    # --- Missed Communications ---
    missed = _get_missed_comms(conn)
    result["sections"]["missed"] = {
        "title": "Needs Your Attention",
        "count": missed["total"],
        "unreplied_slack": missed["slack"],
        "unreplied_email": missed["email"],
        "pending_reviews": missed["pr_reviews"],
    }

    # --- Active Features ---
    features = _get_active_features(conn)
    result["sections"]["features"] = {
        "title": "Active Features",
        "count": len(features),
        "items": features,
    }

    # --- Capacity Analysis ---
    try:
        slots_path = _build_slots_file(conn)
        try:
            cap = capacity(conn, slots_path, scope="today")
            result["sections"]["capacity"] = {
                "title": "Capacity",
                "status": cap["status"],
                "ratio": cap["ratio"],
                "available_hours": cap["available_hours"],
                "task_hours": cap["task_hours"],
                "slack_hours": cap.get("slack_hours", 0),
                "task_count": cap.get("task_count", 0),
            }
        finally:
            os.unlink(slots_path)
    except Exception:
        result["sections"]["capacity"] = {"title": "Capacity", "status": "unavailable"}

    # --- Alerts & Blockers ---
    alert_list = _generate_alerts(conn, tasks, result["sections"]["meetings"], missed, features)
    result["sections"]["alerts"] = {
        "title": "Alerts",
        "count": len(alert_list),
        "items": alert_list,
    }

    # --- Quick Stats ---
    result["sections"]["stats"] = _graph_stats(conn)

    conn.close()
    print(json.dumps(result, indent=2, default=str))


def _greeting(hour):
    if hour < 12:
        return "Good morning"
    elif hour < 17:
        return "Good afternoon"
    else:
        return "Good evening"


# ============================================================================
# 2. MEETINGS — Calendar view
# ============================================================================

def _get_meetings(conn, today):
    rows = conn.execute(
        "SELECT id, name, data, updated_at FROM nodes WHERE type='Meeting' ORDER BY updated_at DESC LIMIT 50"
    ).fetchall()
    meetings = []
    for r in rows:
        d = _data(r)
        # Meeting data may store times under different field names depending on source:
        #   Calendar MCP ingestion: "start", "end"
        #   Manual/other:           "start_time", "end_time", "time"
        raw_start = d.get("start_time") or d.get("start") or d.get("time") or ""
        raw_end = d.get("end_time") or d.get("end") or ""

        # Derive the meeting date from the start timestamp
        meeting_date = d.get("date", "")
        if not meeting_date and raw_start and "T" in str(raw_start):
            # Extract date from ISO timestamp (e.g. "2026-05-13T10:00:00+05:30" → "2026-05-13")
            parsed_start = _parse_iso(str(raw_start))
            if parsed_start:
                meeting_date = parsed_start.astimezone(IST).strftime("%Y-%m-%d")
            else:
                # Try fixing common format issues (missing zero-pad: 04-9 → 04-09)
                try:
                    parts = str(raw_start).split("T")[0].split("-")
                    if len(parts) == 3:
                        meeting_date = f"{parts[0]}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
                except (ValueError, IndexError):
                    pass

        # Only include if date matches today. Skip meetings with no parseable date
        # rather than showing every unparseable meeting on every day.
        if meeting_date != today:
            continue

        start_display, start_iso = _parse_time_display(str(raw_start))
        end_display, end_iso = _parse_time_display(str(raw_end))
        title = d.get("summary") or r["name"]
        meetings.append({
            "id": r["id"],
            "title": title,
            "time": start_display,
            "end_time": end_display,
            "start_time_iso": start_iso or str(raw_start),
            "end_time_iso": end_iso or str(raw_end),
            "attendees": d.get("attendees", d.get("participants", [])),
            "location": d.get("location", ""),
            "status": d.get("status", "confirmed"),
        })
    meetings.sort(key=lambda x: x.get("time", ""))
    return meetings


def _next_meeting(meetings, now_ist):
    now_hm = now_ist.strftime("%H:%M")
    for m in meetings:
        t = m.get("time", "")
        if t and t > now_hm:
            return m
    return None


# ============================================================================
# 3. TASKS — Daily task management with live priority scoring
# ============================================================================

def _get_tasks(conn, scope="today"):
    """Get tasks with live priority scoring from the planner engine."""
    if scope in ("today", "week", "sprint"):
        try:
            scored = priority_score(conn, scope=scope)
        except Exception:
            scored = []

        if scored:
            tasks = []
            for s in scored:
                node = get_node(conn, "Task", s["name"])
                d = _data(node) if node else {}
                deps_out = get_neighbors(conn, "Task", s["name"], edge_type="BLOCKS", direction="out")
                deps_in = get_neighbors(conn, "Task", s["name"], edge_type="BLOCKS", direction="in")
                tasks.append({
                    "id": node["id"] if node else None,
                    "title": s["name"],
                    "status": s.get("status", d.get("status", "open")),
                    "priority": s.get("label", "Medium").lower(),
                    "priority_score": s.get("score", 0.5),
                    "due_date": s.get("due_date", d.get("due_date", "")),
                    "estimated_hours": s.get("estimated_hours", 1.0),
                    "feature": d.get("feature", ""),
                    "source": node.get("source_type", "") if node else "",
                    "blocked_by": [n["name"] for n in deps_in],
                    "blocks": [n["name"] for n in deps_out],
                    "tags": d.get("tags", []),
                    "recurrence": d.get("recurrence"),
                    "scoring_factors": s.get("factors", {}),
                    "created": d.get("created_at", ""),
                })
            return tasks
        # Fall through to raw SQL if priority_score returns empty

    rows = conn.execute(
        "SELECT id, name, data, source_type, source_id, confidence, updated_at "
        "FROM nodes WHERE type='Task' ORDER BY updated_at DESC LIMIT 100"
    ).fetchall()
    tasks = []
    today = _today()
    for r in rows:
        d = _data(r)
        deps_out = get_neighbors(conn, "Task", r["name"], edge_type="BLOCKS", direction="out")
        deps_in = get_neighbors(conn, "Task", r["name"], edge_type="BLOCKS", direction="in")
        task = {
            "id": r["id"],
            "title": r["name"],
            "status": d.get("status", "open"),
            "priority": d.get("priority", "medium"),
            "priority_score": d.get("priority_score", 0.5),
            "due_date": d.get("due_date", ""),
            "estimated_hours": d.get("estimated_hours", 1.0),
            "feature": d.get("feature", ""),
            "source": r["source_type"],
            "blocked_by": [n["name"] for n in deps_in],
            "blocks": [n["name"] for n in deps_out],
            "tags": d.get("tags", []),
            "recurrence": d.get("recurrence"),
            "created": d.get("created_at", r["updated_at"]),
        }
        if scope == "today":
            due = task["due_date"]
            if due and due[:10] <= today:
                tasks.append(task)
            elif not due and task["status"] in ("open", "in_progress", "blocked"):
                tasks.append(task)
        else:
            tasks.append(task)
    tasks.sort(key=lambda t: t["priority_score"], reverse=True)
    return tasks


def add_task(title, priority="medium", feature="", due_date="",
             estimated_hours=1.0, tags=None, recurrence="", blocks="",
             urgency=None, action_type=None, stakeholder=None):
    """Add a new task to the brain. Uses upsert_node to handle duplicates gracefully.

    Writes urgency_score, action_type, stakeholder_score fields that the
    priority_score() engine reads — derived from priority + title keywords,
    or overridden via explicit flags.
    """
    # --- Input validation ---
    title = title.strip() if title else ""
    if not title:
        print(json.dumps({"error": "task title cannot be empty"}))
        return
    if len(title) > 200:
        print(json.dumps({"error": "task title too long (max 200 chars)"}))
        return
    if estimated_hours <= 0:
        print(json.dumps({"error": "estimated hours must be > 0"}))
        return
    valid_priorities = ("critical", "high", "medium", "low")
    if priority not in valid_priorities:
        print(json.dumps({"error": f"invalid priority: {priority}. Must be one of {valid_priorities}"}))
        return
    valid_recurrences = ("", "daily", "weekdays", "weekly", "monthly")
    if recurrence not in valid_recurrences:
        print(json.dumps({"error": f"invalid recurrence: {recurrence}. Must be one of {valid_recurrences}"}))
        return

    conn = _db()
    now = _now()
    today = _today()

    # Derive scoring fields the priority_score() engine reads
    scoring = _derive_scoring_fields(title, priority, feature)
    if urgency is not None:
        scoring["urgency_score"] = max(0.0, min(1.0, urgency))
    if action_type is not None:
        scoring["action_type"] = action_type
    if stakeholder is not None:
        scoring["stakeholder_score"] = max(0.0, min(1.0, stakeholder))

    data = {
        "status": "open",
        "priority": priority,
        "due_date": due_date or today,
        "estimated_hours": estimated_hours,
        "feature": feature,
        "tags": tags or [],
        "created_at": now,
        "content_summary": title,
        # Scoring fields for priority_score() engine
        "urgency_score": scoring["urgency_score"],
        "action_type": scoring["action_type"],
        "stakeholder_score": scoring["stakeholder_score"],
    }
    if recurrence:
        data["recurrence"] = recurrence

    node_id = upsert_node(conn, "Task", title, data,
                          source_type="planner",
                          source_id=f"task:{today}:{title[:30]}")

    if feature:
        upsert_edge(conn, "Task", title, "Feature", feature, "IMPLEMENTS_FEATURE")

    if blocks:
        upsert_edge(conn, "Task", title, "Task", blocks, "BLOCKS")

    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "task": title, "id": node_id,
                       "priority": priority, "due": due_date or today,
                       "recurrence": recurrence or None}))


def complete_task(task_name):
    """Mark a task as done."""
    conn = _db()
    now = _now()
    row = conn.execute("SELECT id, name, data FROM nodes WHERE type='Task' AND name=?", (task_name,)).fetchone()
    if not row:
        rows = conn.execute("SELECT id, name, data FROM nodes WHERE type='Task' AND name LIKE ?",
                            (f"%{task_name}%",)).fetchall()
        if len(rows) == 1:
            row = rows[0]
        elif len(rows) > 1:
            print(json.dumps({"error": "ambiguous", "matches": [r["name"] for r in rows]}))
            conn.close()
            return
        else:
            print(json.dumps({"error": f"task not found: {task_name}"}))
            conn.close()
            return

    d = _data(row)
    d["status"] = "done"
    d["completed_at"] = now
    conn.execute("UPDATE nodes SET data=?, updated_at=? WHERE id=?", (json.dumps(d), now, row["id"]))
    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "completed": row["name"]}))


def update_task(task_name, **kwargs):
    """Update task fields (status, priority, due_date, feature, etc.)."""
    conn = _db()
    now = _now()
    row = conn.execute("SELECT id, data FROM nodes WHERE type='Task' AND name LIKE ?",
                       (f"%{task_name}%",)).fetchone()
    if not row:
        print(json.dumps({"error": f"task not found: {task_name}"}))
        conn.close()
        return

    d = _data(row)
    for k, v in kwargs.items():
        if v is not None:
            d[k] = v
    if "priority" in kwargs:
        # Re-derive scoring fields when priority changes
        scoring = _derive_scoring_fields(
            d.get("content_summary", task_name), kwargs["priority"], d.get("feature", "")
        )
        d["urgency_score"] = scoring["urgency_score"]
        d["action_type"] = scoring["action_type"]
        d["stakeholder_score"] = scoring["stakeholder_score"]

    conn.execute("UPDATE nodes SET data=?, updated_at=? WHERE id=?", (json.dumps(d), now, row["id"]))
    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "updated": task_name, "changes": kwargs}))


# ============================================================================
# 4. MISSED COMMUNICATIONS — Unreplied signals
# ============================================================================

def _get_missed_comms(conn):
    ist = _ist_now()
    cutoff_7d = (ist - timedelta(days=7)).strftime("%Y-%m-%d")

    slack_signals = conn.execute("""
        SELECT n.id, n.name, n.data, n.updated_at FROM nodes n
        WHERE n.type = 'Signal' AND n.source_type = 'slack'
        AND n.updated_at >= ?
        ORDER BY n.updated_at DESC LIMIT 50
    """, (cutoff_7d,)).fetchall()

    unreplied_slack = []
    for s in slack_signals:
        d = _data(s)
        author = d.get("author", "")
        urgency = d.get("urgency", 0.3)
        text = d.get("content_summary", s["name"])
        if author and author.lower() != "saurav kumar" and urgency >= 0.5:
            unreplied_slack.append({
                "id": s["id"],
                "channel": d.get("channel", ""),
                "author": author,
                "summary": text[:120],
                "urgency": urgency,
                "date": d.get("date", s["updated_at"][:10]),
                "needs_reply": urgency >= 0.5,
            })

    emails = conn.execute("""
        SELECT n.id, n.name, n.data, n.updated_at FROM nodes n
        WHERE n.type = 'Email'
        AND n.updated_at >= ?
        ORDER BY n.updated_at DESC LIMIT 30
    """, (cutoff_7d,)).fetchall()

    unreplied_email = []
    for e in emails:
        d = _data(e)
        if d.get("needs_reply") or d.get("action_required"):
            unreplied_email.append({
                "id": e["id"],
                "subject": e["name"],
                "from": d.get("from", d.get("sender", "")),
                "date": d.get("date", e["updated_at"][:10]),
                "urgency": d.get("urgency", 0.4),
                "thread_id": d.get("thread_id", ""),
            })

    prs = conn.execute("""
        SELECT n.id, n.name, n.data FROM nodes n
        WHERE n.type = 'PR'
        AND json_extract(n.data, '$.state') = 'OPEN'
        ORDER BY n.updated_at DESC LIMIT 20
    """).fetchall()

    pending_reviews = []
    for p in prs:
        d = _data(p)
        if d.get("review_requested") or d.get("author", "").lower() != "saurav":
            pending_reviews.append({
                "id": p["id"],
                "title": p["name"],
                "repo": d.get("repo", ""),
                "author": d.get("author", ""),
                "url": d.get("url", ""),
                "created": d.get("created_at", ""),
            })

    return {
        "slack": unreplied_slack[:10],
        "email": unreplied_email[:10],
        "pr_reviews": pending_reviews[:10],
        "total": len(unreplied_slack[:10]) + len(unreplied_email[:10]) + len(pending_reviews[:10]),
    }


def missed():
    """Show all missed communications."""
    conn = _db()
    result = _get_missed_comms(conn)
    conn.close()
    print(json.dumps(result, indent=2, default=str))


# ============================================================================
# 5. FEATURES — Feature context and health
# ============================================================================

def _get_active_features(conn):
    rows = conn.execute(
        "SELECT id, name, data, updated_at FROM nodes WHERE type='Feature' "
        "AND json_extract(data, '$.status') IN ('proposed', 'in_progress', 'blocked') "
        "ORDER BY updated_at DESC LIMIT 20"
    ).fetchall()
    features = []
    for r in rows:
        d = _data(r)
        task_count = conn.execute(
            "SELECT COUNT(*) as c FROM edges e JOIN nodes n ON e.from_node_id = n.id "
            "WHERE e.to_node_id = ? AND n.type = 'Task'", (r["id"],)
        ).fetchone()["c"]
        signal_count = conn.execute(
            "SELECT COUNT(*) as c FROM edges e JOIN nodes n ON e.from_node_id = n.id "
            "WHERE e.to_node_id = ? AND n.type = 'Signal'", (r["id"],)
        ).fetchone()["c"]

        features.append({
            "id": r["id"],
            "name": r["name"],
            "status": d.get("status", "proposed"),
            "owner": d.get("owner", ""),
            "tasks": task_count,
            "signals": signal_count,
            "last_updated": r["updated_at"],
            "health": d.get("health_score", "unknown"),
        })
    return features


def feature_context(feature_name):
    """Get full context for a specific feature using rubick_graph health + rubick_context."""
    conn = _db()

    node = get_node(conn, "Feature", feature_name)
    if not node:
        row = conn.execute(
            "SELECT name FROM nodes WHERE type='Feature' AND name LIKE ?",
            (f"%{feature_name}%",)
        ).fetchone()
        if row:
            node = get_node(conn, "Feature", row["name"])

    if not node:
        print(json.dumps({"error": f"feature not found: {feature_name}"}))
        conn.close()
        return

    name = node["name"]
    d = _node_data(node)

    try:
        health = feature_health(conn, name)
    except Exception:
        health = {}

    try:
        tl = feature_timeline(conn, name,
                              since=(_ist_now() - timedelta(days=30)).isoformat())
    except Exception:
        tl = {"events": []}

    try:
        ctx = context_for(conn, name, consumer="planner")
    except Exception:
        ctx = {"body": ""}

    neighbors = get_neighbors(conn, "Feature", name)
    tasks = [n for n in neighbors if n.get("type") == "Task"]
    signals = [n for n in neighbors if n.get("type") == "Signal"]
    decisions = [n for n in neighbors if n.get("type") in ("Decision", "ArchDecision")]
    prs = [n for n in neighbors if n.get("type") == "PR"]

    result = {
        "feature": {
            "name": name,
            "status": d.get("status", "proposed"),
            "owner": d.get("owner", ""),
            "description": d.get("description", d.get("content_summary", "")),
            "created": d.get("created_at", node.get("updated_at", "")),
            "last_updated": node.get("updated_at", ""),
        },
        "health": health,
        "tasks": [{"title": t["name"], "status": _node_data(t).get("status", "open"),
                    "priority": _node_data(t).get("priority", "medium")} for t in tasks],
        "signals": [{"summary": _node_data(s).get("content_summary", s["name"])[:100],
                      "channel": _node_data(s).get("channel", ""),
                      "date": _node_data(s).get("date", "")} for s in signals[:15]],
        "decisions": [{"title": dc["name"],
                        "summary": _node_data(dc).get("content_summary", "")} for dc in decisions],
        "pull_requests": [{"title": p["name"], "repo": _node_data(p).get("repo", ""),
                            "state": _node_data(p).get("state", ""),
                            "url": _node_data(p).get("url", "")} for p in prs],
        "timeline": tl.get("events", [])[:10],
        "context_summary": ctx.get("body", "")[:500],
    }
    conn.close()
    print(json.dumps(result, indent=2, default=str))


# ============================================================================
# 6. DAILY LOG — Record what happened today
# ============================================================================

def log_entry(text, category="note"):
    """Add a daily log entry (note, decision, blocker, win)."""
    conn = _db()
    now = _now()
    today = _today()
    name = f"[{today}] {category}: {text[:60]}"
    data = {
        "date": today,
        "category": category,
        "content_summary": text,
        "created_at": now,
    }
    node_id = upsert_node(conn, "Signal", name, data,
                          source_type="planner",
                          source_id=f"log:{today}:{category}")
    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "logged": name, "id": node_id}))


# ============================================================================
# 7. WEEKLY REVIEW — Summary of the past week
# ============================================================================

def weekly_review():
    """Generate a weekly review summary."""
    conn = _db()
    ist = _ist_now()
    week_ago = (ist - timedelta(days=7)).strftime("%Y-%m-%d")

    completed = conn.execute("""
        SELECT name, data FROM nodes WHERE type='Task'
        AND json_extract(data, '$.status') = 'done'
        AND json_extract(data, '$.completed_at') >= ?
        ORDER BY json_extract(data, '$.completed_at') DESC
    """, (week_ago,)).fetchall()

    still_open = conn.execute("""
        SELECT name, data FROM nodes WHERE type='Task'
        AND json_extract(data, '$.status') IN ('open', 'in_progress', 'blocked')
        ORDER BY json_extract(data, '$.priority_score') DESC LIMIT 20
    """).fetchall()

    signal_count = conn.execute(
        "SELECT COUNT(*) as c FROM nodes WHERE type='Signal' AND updated_at >= ?", (week_ago,)
    ).fetchone()["c"]

    features_touched = conn.execute("""
        SELECT DISTINCT name, data FROM nodes WHERE type='Feature'
        AND updated_at >= ?
    """, (week_ago,)).fetchall()

    prs = conn.execute("""
        SELECT name, data FROM nodes WHERE type='PR'
        AND updated_at >= ?
    """, (week_ago,)).fetchall()

    result = {
        "period": f"{week_ago} to {_today()}",
        "completed_tasks": [{"title": r["name"], "feature": _data(r).get("feature", "")} for r in completed],
        "carried_over": [{"title": r["name"], "priority": _data(r).get("priority", "medium"),
                          "status": _data(r).get("status", "open")} for r in still_open],
        "signals_ingested": signal_count,
        "features_touched": [{"name": r["name"], "status": _data(r).get("status", "")} for r in features_touched],
        "pull_requests": [{"title": r["name"], "state": _data(r).get("state", "")} for r in prs],
        "stats": {
            "tasks_done": len(completed),
            "tasks_pending": len(still_open),
            "completion_rate": round(len(completed) / max(len(completed) + len(still_open), 1) * 100, 1),
        }
    }
    conn.close()
    print(json.dumps(result, indent=2, default=str))


# ============================================================================
# 8. FOCUS MODE — Deep work session planner (uses priority_score engine)
# ============================================================================

def focus(hours=2.0):
    """Plan a focused work block using live priority scoring from the planner engine."""
    conn = _db()

    try:
        scored = priority_score(conn, scope="today")
    except Exception:
        scored = []

    if not scored:
        tasks = _get_tasks(conn, scope="today")
        open_tasks = [t for t in tasks if t["status"] in ("open", "in_progress")]
    else:
        open_tasks = [t for t in scored if t.get("status", "open") in ("open", "in_progress")]
        open_tasks = [{
            "title": t["name"],
            "status": t.get("status", "open"),
            "priority_score": t.get("score", 0.5),
            "priority": t.get("label", "Medium").lower(),
            "estimated_hours": t.get("estimated_hours", 1.0),
            "due_date": t.get("due_date", ""),
            "scoring_factors": t.get("factors", {}),
        } for t in open_tasks]

    selected = []
    remaining = hours
    for t in open_tasks:
        est = t.get("estimated_hours", 1.0)
        if est <= remaining:
            selected.append(t)
            remaining -= est
        if remaining <= 0:
            break

    result = {
        "focus_hours": hours,
        "tasks": selected,
        "time_allocated": round(hours - remaining, 2),
        "time_remaining": round(remaining, 2),
        "scoring_method": "priority_score_v3" if scored else "static_fallback",
    }
    conn.close()
    print(json.dumps(result, indent=2, default=str))


# ============================================================================
# 9. SEARCH — Query the brain (delegates to rubick_graph.search_text)
# ============================================================================

def search(query, ntype=None):
    """Search the knowledge graph using FTS5 via rubick_graph."""
    conn = _db()
    try:
        results_raw = search_text(conn, query, limit=20, ntype=ntype)
        results = []
        for r in results_raw:
            d = r.get("data", {})
            if isinstance(d, str):
                try:
                    d = json.loads(d)
                except (json.JSONDecodeError, TypeError):
                    d = {}
            results.append({
                "id": r.get("id"),
                "type": r.get("type", ""),
                "name": r.get("name", ""),
                "summary": d.get("content_summary", d.get("description", ""))[:120] if isinstance(d, dict) else "",
                "source": r.get("source_type", ""),
                "updated": r.get("updated_at", ""),
            })
    except Exception:
        # Fallback to raw SQL
        sql = "SELECT id, type, name, data, source_type, updated_at FROM nodes WHERE name LIKE ? OR data LIKE ? LIMIT 20"
        rows = conn.execute(sql, (f"%{query}%", f"%{query}%")).fetchall()
        results = []
        for r in rows:
            d = _data(r)
            results.append({
                "id": r["id"], "type": r["type"], "name": r["name"],
                "summary": d.get("content_summary", d.get("description", ""))[:120],
                "source": r["source_type"], "updated": r["updated_at"],
            })

    conn.close()
    print(json.dumps({"query": query, "count": len(results), "results": results}, indent=2, default=str))


# ============================================================================
# 10. GRAPH STATS — Quick overview (delegates to rubick_graph.get_stats)
# ============================================================================

def _graph_stats(conn):
    try:
        s = get_stats(conn)
        return {
            "total_nodes": s["total_nodes"],
            "total_edges": s["total_edges"],
            "by_type": s.get("node_types", {}),
            "edge_types": s.get("edge_types", {}),
        }
    except Exception:
        total = conn.execute("SELECT COUNT(*) as c FROM nodes").fetchone()["c"]
        edges = conn.execute("SELECT COUNT(*) as c FROM edges").fetchone()["c"]
        types = conn.execute("SELECT type, COUNT(*) as c FROM nodes GROUP BY type ORDER BY c DESC").fetchall()
        return {
            "total_nodes": total,
            "total_edges": edges,
            "by_type": {r["type"]: r["c"] for r in types},
        }


def stats():
    conn = _db()
    result = _graph_stats(conn)
    conn.close()
    print(json.dumps(result, indent=2))


# ============================================================================
# 11. ALERTS — Smart notifications
# ============================================================================

def _generate_alerts(conn, tasks, meetings, missed, features):
    alerts_list = []
    ist = _ist_now()

    for t in tasks:
        if t.get("due_date") and t["due_date"] < _today() and t["status"] not in ("done", "closed"):
            alerts_list.append({
                "type": "overdue",
                "severity": "warning",
                "title": f"Overdue: {t['title']}",
                "detail": f"Due {t['due_date']}, status: {t['status']}",
            })

    for t in tasks:
        if t.get("status") == "blocked":
            blockers = t.get("blocked_by", [])
            alerts_list.append({
                "type": "blocked",
                "severity": "critical",
                "title": f"Blocked: {t['title']}",
                "detail": f"Blocked by: {', '.join(blockers) if blockers else 'unknown'}",
            })

    for s in missed.get("slack", []):
        if s.get("urgency", 0) >= 0.7:
            alerts_list.append({
                "type": "urgent_slack",
                "severity": "warning",
                "title": f"Urgent from {s['author']}",
                "detail": s["summary"][:80],
            })

    next_mtg = meetings.get("next_meeting") if isinstance(meetings, dict) else None
    if next_mtg:
        mtg_time = next_mtg.get("time", "")
        if mtg_time:
            try:
                mtg_h, mtg_m = int(mtg_time.split(":")[0]), int(mtg_time.split(":")[1])
                now_h, now_m = ist.hour, ist.minute
                diff_min = (mtg_h * 60 + mtg_m) - (now_h * 60 + now_m)
                if 0 < diff_min <= 15:
                    alerts_list.append({
                        "type": "upcoming_meeting",
                        "severity": "info",
                        "title": f"Meeting in {diff_min}min: {next_mtg['title']}",
                        "detail": f"At {mtg_time}",
                    })
                elif diff_min > 15:
                    alerts_list.append({
                        "type": "next_meeting",
                        "severity": "info",
                        "title": f"Next: {next_mtg['title']}",
                        "detail": f"At {mtg_time}",
                    })
            except (ValueError, IndexError):
                alerts_list.append({
                    "type": "next_meeting",
                    "severity": "info",
                    "title": f"Next: {next_mtg['title']}",
                    "detail": f"At {mtg_time}",
                })

    for f in features:
        if f.get("last_updated"):
            try:
                last = datetime.fromisoformat(f["last_updated"].replace("Z", "+00:00"))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                days_stale = (datetime.now(timezone.utc) - last).days
                if days_stale > 7:
                    alerts_list.append({
                        "type": "stale_feature",
                        "severity": "info",
                        "title": f"Stale: {f['name']}",
                        "detail": f"No update in {days_stale} days",
                    })
            except (ValueError, TypeError):
                pass

    alerts_list.sort(key=lambda a: {"critical": 0, "warning": 1, "info": 2}.get(a["severity"], 3))
    return alerts_list


def alerts():
    """Show current alerts (fixed: builds proper meetings structure)."""
    conn = _db()
    tasks = _get_tasks(conn, scope="today")
    meetings_raw = _get_meetings(conn, _today())
    ist = _ist_now()
    meetings_section = {
        "title": "Today's Calendar",
        "count": len(meetings_raw),
        "items": meetings_raw,
        "next_meeting": _next_meeting(meetings_raw, ist),
    }
    missed_data = _get_missed_comms(conn)
    features = _get_active_features(conn)
    result = _generate_alerts(conn, tasks, meetings_section, missed_data, features)
    conn.close()
    print(json.dumps(result, indent=2, default=str))


# ============================================================================
# 12. THREAD TRACKER — Auto-close stale threads
# ============================================================================

def _query_stale_signals(conn, days=7, limit=100):
    """Shared query for stale, low-urgency, unresolved signals."""
    cutoff = (_ist_now() - timedelta(days=days)).strftime("%Y-%m-%d")
    return conn.execute("""
        SELECT id, name, data, updated_at FROM nodes
        WHERE type = 'Signal' AND updated_at < ?
        AND json_extract(data, '$.urgency') < 0.5
        AND (json_extract(data, '$.status') IS NULL OR json_extract(data, '$.status') != 'resolved')
        ORDER BY updated_at ASC LIMIT ?
    """, (cutoff, limit)).fetchall()


def _age_days(updated_at):
    """Compute how many days old a timestamp is."""
    if not updated_at:
        return 999
    try:
        ts = updated_at.replace("Z", "+00:00") if "Z" in updated_at else updated_at
        last = datetime.fromisoformat(ts)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).days
    except (ValueError, TypeError):
        return 999


def stale_threads(days=7):
    """Find stale signal threads that can be auto-closed."""
    conn = _db()
    rows = _query_stale_signals(conn, days=days, limit=30)
    stale = []
    for r in rows:
        d = _data(r)
        stale.append({
            "id": r["id"],
            "title": r["name"][:80],
            "channel": d.get("channel", ""),
            "age_days": _age_days(r["updated_at"]),
            "urgency": d.get("urgency", 0),
        })
    conn.close()
    print(json.dumps({"stale_count": len(stale), "items": stale, "action": "archive_candidates"}, indent=2, default=str))


def close_thread(signal_id):
    """Archive/close a stale thread by marking it resolved."""
    conn = _db()
    now = _now()
    row = conn.execute("SELECT id, data FROM nodes WHERE id=? AND type='Signal'", (signal_id,)).fetchone()
    if not row:
        print(json.dumps({"error": f"signal not found: {signal_id}"}))
        conn.close()
        return

    d = _data(row)
    d["status"] = "resolved"
    d["closed_at"] = now
    d["closed_by"] = "planner"
    conn.execute("UPDATE nodes SET data=?, updated_at=? WHERE id=?", (json.dumps(d), now, row["id"]))
    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "closed": signal_id}))


# ============================================================================
# 13. REMEMBER — Store context for future recall
# ============================================================================

def remember(text, category="context", target=""):
    """Store a memory/context note in the brain for future recall."""
    conn = _db()
    now = _now()
    today = _today()
    name = f"[Memory] {category}: {text[:50]}"
    data = {
        "date": today,
        "category": category,
        "content_summary": text,
        "target": target,
        "created_at": now,
    }
    node_id = upsert_node(conn, "Decision", name, data,
                          source_type="planner",
                          source_id=f"memory:{today}:{category}")
    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "remembered": text[:80], "id": node_id}))


# ============================================================================
# 14. SMART PLAN — Full planner engine pipeline
# ============================================================================

def smart_plan(scope="today", persist=False):
    """Generate a full plan using the rubick_graph planner engine.

    Chains: DAG build → topo sort → CPM → priority scoring →
    calendar slot building → capacity → slot matching → conflict detection.
    """
    conn = _db()
    scope_days = 1 if scope == "today" else (7 if scope == "week" else 14)
    slots_path = _build_slots_file(conn, scope_days=scope_days)
    try:
        result = graph_plan(
            conn, slots_path,
            scope=scope,
            persist=persist,
        )
    finally:
        try:
            os.unlink(slots_path)
        except OSError:
            pass
    conn.close()
    print(json.dumps(result, indent=2, default=str))


# ============================================================================
# 15. TASK DEPENDENCIES — DAG-based dependency management
# ============================================================================

def add_dependency(blocker, blocked):
    """Create a BLOCKS edge between two tasks."""
    conn = _db()

    b1 = get_node(conn, "Task", blocker)
    if not b1:
        row = conn.execute("SELECT name FROM nodes WHERE type='Task' AND name LIKE ?",
                           (f"%{blocker}%",)).fetchone()
        if row:
            blocker = row["name"]
            b1 = get_node(conn, "Task", blocker)

    b2 = get_node(conn, "Task", blocked)
    if not b2:
        row = conn.execute("SELECT name FROM nodes WHERE type='Task' AND name LIKE ?",
                           (f"%{blocked}%",)).fetchone()
        if row:
            blocked = row["name"]
            b2 = get_node(conn, "Task", blocked)

    if not b1:
        print(json.dumps({"error": f"blocker task not found: {blocker}"}))
        conn.close()
        return
    if not b2:
        print(json.dumps({"error": f"blocked task not found: {blocked}"}))
        conn.close()
        return

    upsert_edge(conn, "Task", blocker, "Task", blocked, "BLOCKS")
    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "blocker": blocker, "blocked": blocked, "edge": "BLOCKS"}))


def remove_dependency(blocker, blocked):
    """Remove a BLOCKS edge between two tasks."""
    conn = _db()
    row = conn.execute("""
        SELECT e.from_node_id, e.to_node_id FROM edges e
        JOIN nodes n1 ON e.from_node_id = n1.id
        JOIN nodes n2 ON e.to_node_id = n2.id
        WHERE n1.type = 'Task' AND n1.name LIKE ?
        AND n2.type = 'Task' AND n2.name LIKE ?
        AND e.edge_type = 'BLOCKS'
    """, (f"%{blocker}%", f"%{blocked}%")).fetchone()

    if not row:
        print(json.dumps({"error": f"dependency not found: {blocker} -> {blocked}"}))
        conn.close()
        return

    conn.execute("DELETE FROM edges WHERE from_node_id=? AND to_node_id=? AND edge_type='BLOCKS'",
                 (row["from_node_id"], row["to_node_id"]))
    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "removed": f"{blocker} -/-> {blocked}"}))


def show_deps(task_name):
    """Show dependencies for a task."""
    conn = _db()
    node = get_node(conn, "Task", task_name)
    if not node:
        row = conn.execute("SELECT name FROM nodes WHERE type='Task' AND name LIKE ?",
                           (f"%{task_name}%",)).fetchone()
        if row:
            task_name = row["name"]
            node = get_node(conn, "Task", task_name)

    if not node:
        print(json.dumps({"error": f"task not found: {task_name}"}))
        conn.close()
        return

    blocks = get_neighbors(conn, "Task", task_name, edge_type="BLOCKS", direction="out")
    blocked_by = get_neighbors(conn, "Task", task_name, edge_type="BLOCKS", direction="in")
    due_before = get_neighbors(conn, "Task", task_name, edge_type="DUE_BEFORE", direction="out")

    result = {
        "task": task_name,
        "blocks": [{"name": n["name"], "status": _node_data(n).get("status", "open")} for n in blocks],
        "blocked_by": [{"name": n["name"], "status": _node_data(n).get("status", "open")} for n in blocked_by],
        "due_before": [{"name": n["name"]} for n in due_before],
    }
    conn.close()
    print(json.dumps(result, indent=2, default=str))


def show_dag(scope="today"):
    """Show task DAG with topological order and critical path."""
    conn = _db()
    dag = dag_build(conn, scope=scope)
    topo = topo_sort(conn, dag=dag)
    cpm = critical_path(conn, dag=dag)

    result = {
        "scope": scope,
        "task_count": len(dag.get("nodes", {})),
        "edge_count": len(dag.get("edges", [])),
        "root_tasks": dag.get("root_tasks", []),
        "leaf_tasks": dag.get("leaf_tasks", []),
        "topological_order": topo.get("ordered", []),
        "circular_deps": topo.get("cycles", []),
        "has_cycles": bool(topo.get("error")),
        "critical_path": cpm.get("critical_path", []),
        "project_duration_hours": cpm.get("project_duration_hours"),
        "task_times": cpm.get("task_times", {}),
    }
    conn.close()
    print(json.dumps(result, indent=2, default=str))


# ============================================================================
# 16. RECURRING TASKS — Auto-spawn recurring task instances
# ============================================================================

def _auto_spawn_recurring(conn, today, ist):
    """Idempotently spawn today's recurring task instances. Called at dashboard start."""
    day_name = ist.strftime("%A").lower()
    rows = conn.execute(
        "SELECT id, name, data FROM nodes WHERE type='Task'"
    ).fetchall()

    for r in rows:
        d = _data(r)
        recur = d.get("recurrence")
        if not recur:
            continue
        if d.get("status") in ("done", "closed"):
            continue
        if d.get("recurrence_parent"):
            continue

        instance_name = f"{r['name']} [{today}]"
        existing = get_node(conn, "Task", instance_name)
        if existing:
            continue

        should_spawn = False
        if recur == "daily":
            should_spawn = True
        elif recur == "weekdays" and day_name not in ("saturday", "sunday"):
            should_spawn = True
        elif recur == "weekly" and day_name == d.get("recurrence_day", "monday"):
            should_spawn = True
        elif recur == "monthly" and ist.day == int(d.get("recurrence_day_of_month", 1)):
            should_spawn = True

        if should_spawn:
            instance_data = {
                "status": "open",
                "priority": d.get("priority", "medium"),
                "priority_score": d.get("priority_score", 0.5),
                "due_date": today,
                "estimated_hours": d.get("estimated_hours", 1.0),
                "feature": d.get("feature", ""),
                "tags": list(set(d.get("tags", []) + ["recurring"])),
                "recurrence_parent": r["name"],
                "content_summary": r["name"],
                "created_at": _now(),
            }
            upsert_node(conn, "Task", instance_name, instance_data,
                       source_type="planner",
                       source_id=f"recur:{today}:{r['name'][:30]}")

    conn.commit()


def spawn_recurring():
    """Manually trigger recurring task spawning."""
    conn = _db()
    today = _today()
    ist = _ist_now()

    before = conn.execute("SELECT COUNT(*) as c FROM nodes WHERE type='Task'").fetchone()["c"]
    _auto_spawn_recurring(conn, today, ist)
    after = conn.execute("SELECT COUNT(*) as c FROM nodes WHERE type='Task'").fetchone()["c"]

    spawned = after - before
    conn.close()
    print(json.dumps({"ok": True, "spawned": spawned, "date": today}))


# ============================================================================
# 17. BATCH OPERATIONS — Bulk actions
# ============================================================================

def bulk_close_stale(days=7):
    """Batch-close stale signal threads."""
    conn = _db()
    rows = _query_stale_signals(conn, days=days, limit=100)
    now = _now()
    closed = []
    for r in rows:
        d = _data(r)
        d["status"] = "resolved"
        d["closed_at"] = now
        d["closed_by"] = "bulk_close"
        conn.execute("UPDATE nodes SET data=?, updated_at=? WHERE id=?",
                     (json.dumps(d), now, r["id"]))
        closed.append({"id": r["id"], "name": r["name"][:80]})

    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "closed": len(closed), "items": closed}, indent=2, default=str))


def backfill_scoring():
    """Backfill urgency_score/action_type/stakeholder_score on existing tasks.

    Existing tasks only have 'priority' and 'priority_score' fields.
    This derives the engine-compatible scoring fields from those.
    """
    conn = _db()
    rows = conn.execute("SELECT id, name, data FROM nodes WHERE type='Task'").fetchall()
    now = _now()
    updated = []
    for r in rows:
        d = _data(r)
        # Skip if already has scoring fields
        if "urgency_score" in d and "action_type" in d and "stakeholder_score" in d:
            continue
        priority = d.get("priority", "medium")
        title = d.get("content_summary", r["name"])
        feature = d.get("feature", "")
        scoring = _derive_scoring_fields(title, priority, feature)
        d["urgency_score"] = scoring["urgency_score"]
        d["action_type"] = scoring["action_type"]
        d["stakeholder_score"] = scoring["stakeholder_score"]
        conn.execute("UPDATE nodes SET data=?, updated_at=? WHERE id=?",
                     (json.dumps(d), now, r["id"]))
        updated.append({"name": r["name"], **scoring})
    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "backfilled": len(updated), "tasks": updated}, indent=2, default=str))


def bulk_update_priority(scope="today"):
    """Recalculate priority scores for all tasks using the planner engine."""
    conn = _db()
    try:
        scored = priority_score(conn, scope=scope)
    except Exception as e:
        print(json.dumps({"error": f"priority scoring failed: {e}"}))
        conn.close()
        return

    now = _now()
    updated = []
    for s in scored:
        node = get_node(conn, "Task", s["name"])
        if node:
            d = _data(node)
            d["priority_score"] = s["score"]
            d["priority"] = s["label"].lower()
            d["scoring_factors"] = s["factors"]
            conn.execute("UPDATE nodes SET data=?, updated_at=? WHERE id=?",
                         (json.dumps(d), now, node["id"]))
            updated.append({"name": s["name"], "score": s["score"], "label": s["label"]})

    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "updated": len(updated), "tasks": updated}, indent=2, default=str))


# ============================================================================
# 18. RECALL — Context retrieval from brain (delegates to rubick_context)
# ============================================================================

def brain_recall(query, budget=0, ntype=None):
    """What does the brain know about X? Delegates to rubick_context.recall()."""
    conn = _db()
    try:
        result = recall(conn, query, budget=budget, ntype=ntype)
    except Exception as e:
        result = {"error": str(e)}
    conn.close()
    print(json.dumps(result, indent=2, default=str))


# ============================================================================
# CLI Router
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Omni Planner — Interactive daily planner")
    sub = parser.add_subparsers(dest="command")

    # --- Existing commands ---
    sub.add_parser("dashboard", help="Full daily dashboard")
    sub.add_parser("stats", help="Graph stats")
    sub.add_parser("missed", help="Missed communications")
    sub.add_parser("alerts", help="Current alerts")
    sub.add_parser("weekly-review", help="Weekly summary")

    p_tasks = sub.add_parser("tasks", help="List tasks")
    p_tasks.add_argument("--scope", default="today", choices=["today", "week", "sprint", "all"])

    p_add = sub.add_parser("add-task", help="Add a task")
    p_add.add_argument("title", nargs="+")
    p_add.add_argument("--priority", default="medium", choices=["critical", "high", "medium", "low"])
    p_add.add_argument("--feature", default="")
    p_add.add_argument("--due", default="")
    p_add.add_argument("--hours", type=float, default=1.0)
    p_add.add_argument("--tags", default="")
    p_add.add_argument("--recur", default="", choices=["", "daily", "weekdays", "weekly", "monthly"])
    p_add.add_argument("--blocks", default="")
    p_add.add_argument("--urgency", type=float, default=None, help="Override urgency_score (0-1)")
    p_add.add_argument("--action-type", default=None, choices=["blocks_others", "needs_response", "fyi"])
    p_add.add_argument("--stakeholder", type=float, default=None, help="Override stakeholder_score (0-1)")

    p_done = sub.add_parser("complete", help="Complete a task")
    p_done.add_argument("task", nargs="+")

    p_update = sub.add_parser("update-task", help="Update a task")
    p_update.add_argument("task", nargs="+")
    p_update.add_argument("--status", choices=["open", "in_progress", "blocked", "done"])
    p_update.add_argument("--priority", choices=["critical", "high", "medium", "low"])
    p_update.add_argument("--due", default=None)
    p_update.add_argument("--feature", default=None)

    p_feat = sub.add_parser("feature", help="Feature context")
    p_feat.add_argument("name", nargs="+")

    p_focus = sub.add_parser("focus", help="Focus mode planner")
    p_focus.add_argument("--hours", type=float, default=2.0)

    p_search = sub.add_parser("search", help="Search the brain")
    p_search.add_argument("query", nargs="+")
    p_search.add_argument("--type", default=None)

    p_log = sub.add_parser("log", help="Add a daily log entry")
    p_log.add_argument("text", nargs="+")
    p_log.add_argument("--category", default="note", choices=["note", "decision", "blocker", "win", "idea"])

    p_stale = sub.add_parser("stale-threads", help="Find stale threads")
    p_stale.add_argument("--days", type=int, default=7)

    p_close = sub.add_parser("close-thread", help="Close a stale thread")
    p_close.add_argument("signal_id", type=int)

    p_remember = sub.add_parser("remember", help="Store a memory note")
    p_remember.add_argument("text", nargs="+")
    p_remember.add_argument("--category", default="context")
    p_remember.add_argument("--target", default="")

    # --- New commands ---
    p_smartplan = sub.add_parser("smart-plan", help="Full plan with DAG+CPM+slots+capacity")
    p_smartplan.add_argument("--scope", default="today", choices=["today", "week", "sprint"])
    p_smartplan.add_argument("--persist", action="store_true")

    p_dep = sub.add_parser("add-dep", help="Add task dependency (blocker blocks blocked)")
    p_dep.add_argument("blocker", nargs="+")
    p_dep.add_argument("--blocks", required=True, nargs="+")

    p_rdep = sub.add_parser("remove-dep", help="Remove task dependency")
    p_rdep.add_argument("blocker", nargs="+")
    p_rdep.add_argument("--blocks", required=True, nargs="+")

    p_deps = sub.add_parser("deps", help="Show task dependencies")
    p_deps.add_argument("task", nargs="+")

    p_dag = sub.add_parser("dag", help="Task DAG with critical path")
    p_dag.add_argument("--scope", default="today", choices=["today", "week", "sprint", "all"])

    sub.add_parser("spawn-recurring", help="Spawn today's recurring task instances")

    p_bulk_close = sub.add_parser("bulk-close-stale", help="Batch close stale threads")
    p_bulk_close.add_argument("--days", type=int, default=7)

    p_bulk_priority = sub.add_parser("bulk-update-priority", help="Recalculate all priorities")
    p_bulk_priority.add_argument("--scope", default="today", choices=["today", "week", "sprint", "all"])

    sub.add_parser("backfill-scoring", help="Backfill scoring fields on existing tasks")

    p_recall = sub.add_parser("recall", help="What does the brain know about X?")
    p_recall.add_argument("query", nargs="+")
    p_recall.add_argument("--budget", type=int, default=0)
    p_recall.add_argument("--type", default=None)

    args = parser.parse_args()

    if args.command == "dashboard":
        dashboard()
    elif args.command == "stats":
        stats()
    elif args.command == "missed":
        missed()
    elif args.command == "alerts":
        alerts()
    elif args.command == "weekly-review":
        weekly_review()
    elif args.command == "tasks":
        conn = _db()
        tasks = _get_tasks(conn, scope=args.scope)
        conn.close()
        print(json.dumps({"scope": args.scope, "count": len(tasks), "tasks": tasks}, indent=2, default=str))
    elif args.command == "add-task":
        title = " ".join(args.title)
        tags = [t.strip() for t in args.tags.split(",")] if args.tags else []
        add_task(title, priority=args.priority, feature=args.feature,
                 due_date=args.due, estimated_hours=args.hours, tags=tags,
                 recurrence=args.recur, blocks=args.blocks,
                 urgency=args.urgency, action_type=getattr(args, 'action_type', None),
                 stakeholder=args.stakeholder)
    elif args.command == "complete":
        complete_task(" ".join(args.task))
    elif args.command == "update-task":
        kwargs = {}
        if args.status: kwargs["status"] = args.status
        if args.priority: kwargs["priority"] = args.priority
        if args.due: kwargs["due_date"] = args.due
        if args.feature: kwargs["feature"] = args.feature
        update_task(" ".join(args.task), **kwargs)
    elif args.command == "feature":
        feature_context(" ".join(args.name))
    elif args.command == "focus":
        focus(hours=args.hours)
    elif args.command == "search":
        search(" ".join(args.query), ntype=args.type)
    elif args.command == "log":
        log_entry(" ".join(args.text), category=args.category)
    elif args.command == "stale-threads":
        stale_threads(days=args.days)
    elif args.command == "close-thread":
        close_thread(args.signal_id)
    elif args.command == "remember":
        remember(" ".join(args.text), category=args.category, target=args.target)
    elif args.command == "smart-plan":
        smart_plan(scope=args.scope, persist=args.persist)
    elif args.command == "add-dep":
        add_dependency(" ".join(args.blocker), " ".join(args.blocks))
    elif args.command == "remove-dep":
        remove_dependency(" ".join(args.blocker), " ".join(args.blocks))
    elif args.command == "deps":
        show_deps(" ".join(args.task))
    elif args.command == "dag":
        show_dag(scope=args.scope)
    elif args.command == "spawn-recurring":
        spawn_recurring()
    elif args.command == "bulk-close-stale":
        bulk_close_stale(days=args.days)
    elif args.command == "bulk-update-priority":
        bulk_update_priority(scope=args.scope)
    elif args.command == "backfill-scoring":
        backfill_scoring()
    elif args.command == "recall":
        brain_recall(" ".join(args.query), budget=args.budget, ntype=args.type)
    else:
        dashboard()


if __name__ == "__main__":
    main()
