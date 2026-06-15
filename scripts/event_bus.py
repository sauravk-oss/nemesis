"""SSE Event Bus for Nemesis v2.

Polls rubick.db for changes and broadcasts events to connected SSE clients.
Event types: node_updated, feature_phase_complete, learn_flush, health_update, sync_complete.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from typing import AsyncGenerator

log = logging.getLogger("event_bus")

_subscribers: list[asyncio.Queue] = []
_last_node_ts: str = ""
_last_ledger_ts: str = ""


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    try:
        _subscribers.remove(q)
    except ValueError:
        pass


def broadcast(event: str, data: dict | None = None) -> None:
    payload = json.dumps({"event": event, "data": data or {}, "ts": time.time()})
    dead = []
    for q in _subscribers:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _subscribers.remove(q)


async def sse_stream(q: asyncio.Queue) -> AsyncGenerator[str, None]:
    try:
        while True:
            try:
                payload = await asyncio.wait_for(q.get(), timeout=30.0)
                yield f"data: {payload}\n\n"
            except asyncio.TimeoutError:
                yield f"data: {json.dumps({'event': 'heartbeat', 'ts': time.time()})}\n\n"
    except asyncio.CancelledError:
        return


async def poll_changes(db_path: str, interval: float = 2.0) -> None:
    global _last_node_ts, _last_ledger_ts

    while True:
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row

            row = conn.execute(
                "SELECT MAX(updated_at) as ts FROM nodes"
            ).fetchone()
            new_node_ts = row["ts"] or "" if row else ""

            if new_node_ts and new_node_ts != _last_node_ts and _last_node_ts:
                changed = conn.execute(
                    "SELECT type, name, id FROM nodes WHERE updated_at > ? LIMIT 10",
                    (_last_node_ts,)
                ).fetchall()
                for r in changed:
                    broadcast("node_updated", {
                        "type": r["type"], "name": r["name"], "id": r["id"]
                    })
                    if r["type"] == "Feature":
                        broadcast("feature_phase_complete", {
                            "name": r["name"], "id": r["id"]
                        })
            _last_node_ts = new_node_ts

            try:
                row = conn.execute(
                    "SELECT MAX(created_at) as ts FROM learning_ledger"
                ).fetchone()
                new_ledger_ts = row["ts"] or "" if row else ""
                if new_ledger_ts and new_ledger_ts != _last_ledger_ts and _last_ledger_ts:
                    count = conn.execute(
                        "SELECT COUNT(*) as cnt FROM learning_ledger WHERE created_at > ?",
                        (_last_ledger_ts,)
                    ).fetchone()
                    broadcast("learn_flush", {"count": count["cnt"] if count else 0})
                _last_ledger_ts = new_ledger_ts
            except sqlite3.OperationalError:
                pass

            conn.close()
        except Exception as e:
            log.warning("poll_changes error: %s", e)

        await asyncio.sleep(interval)
