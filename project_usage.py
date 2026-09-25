"""Forward-only project token accounting from Codex's local thread counters.

The account usage API provides authoritative account totals but no project split.
Stored thread totals may include copied histories, so we never import old totals.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path


UNASSIGNED = "__unassigned__"


def project_identity(cwd: str | None) -> tuple[str, str]:
    if not cwd:
        return UNASSIGNED, "Projet non attribué"
    label = os.path.normpath(cwd.removeprefix("\\\\?\\"))
    return os.path.normcase(label), label


def read_thread_counters(codex_home: str | Path) -> list[dict]:
    """Read only user-facing Codex threads; exclude guardian and subagent logs."""
    path = Path(codex_home) / "state_5.sqlite"
    with closing(sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True, timeout=2)) as db:
        rows = db.execute(
            "SELECT id,cwd,tokens_used,created_at,updated_at FROM threads "
            "WHERE source IN ('vscode','cli') AND tokens_used IS NOT NULL").fetchall()
    return [{"id": thread_id, "cwd": cwd or "", "tokens": int(tokens),
             "created_at": created_at or 0, "updated_at": updated_at or 0}
            for thread_id, cwd, tokens, created_at, updated_at in rows
            if isinstance(tokens, int) and tokens >= 0]


def install_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS project_usage_state (
            account_id TEXT PRIMARY KEY, started_at REAL NOT NULL, last_observed_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_thread_counters (
            account_id TEXT NOT NULL, thread_id TEXT NOT NULL, tokens INTEGER NOT NULL,
            project_key TEXT NOT NULL, project_label TEXT NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY(account_id,thread_id)
        );
        CREATE TABLE IF NOT EXISTS project_usage_daily (
            account_id TEXT NOT NULL, day TEXT NOT NULL, project_key TEXT NOT NULL,
            project_label TEXT NOT NULL, tokens INTEGER NOT NULL,
            PRIMARY KEY(account_id,day,project_key)
        );
        CREATE INDEX IF NOT EXISTS idx_project_usage_day ON project_usage_daily(day,account_id);
        CREATE TABLE IF NOT EXISTS project_baseline_pending (
            account_id TEXT NOT NULL, thread_id TEXT NOT NULL,
            PRIMARY KEY(account_id,thread_id)
        );
    """)


def baseline_copied_thread(db: sqlite3.Connection, account_id: str, thread_id: str) -> None:
    """Skip copied history the first time a fork appears in the local index."""
    with db:
        db.execute("INSERT OR IGNORE INTO project_baseline_pending VALUES (?,?)",
                   (account_id, thread_id))


def observe(db: sqlite3.Connection, account_id: str, counters: list[dict], observed_at: float | None = None) -> int:
    """Record only positive changes, baseline existing threads, and count new chats.

    Caller serializes access to the monitor database. Returns attributed tokens.
    """
    stamp = time.time() if observed_at is None else observed_at
    state = db.execute("SELECT started_at FROM project_usage_state WHERE account_id=?", (account_id,)).fetchone()
    first = state is None
    started_at = stamp if first else state[0]
    previous = {row[0]: row[1:] for row in db.execute(
        "SELECT thread_id,tokens,project_key,updated_at FROM project_thread_counters WHERE account_id=?",
        (account_id,))}
    other_ids = {row[0] for row in db.execute(
        "SELECT thread_id FROM project_thread_counters WHERE account_id<>?", (account_id,))}
    pending = {row[0] for row in db.execute(
        "SELECT thread_id FROM project_baseline_pending WHERE account_id=?", (account_id,))}
    daily = defaultdict(int)
    upserts = []
    for counter in counters:
        thread_id = counter["id"]
        tokens = counter["tokens"]
        key, label = project_identity(counter.get("cwd"))
        old = previous.get(thread_id)
        delta = 0
        if old is not None and tokens > old[0]:
            delta = tokens - old[0]
            if key != old[1]:
                key, label = UNASSIGNED, "Projet non attribué"
        elif old is None and not first and thread_id not in pending and counter["created_at"] >= started_at and thread_id not in other_ids:
            # A brand-new chat may finish its first turn before our next poll.
            delta = tokens
        if delta:
            updated = counter.get("updated_at") or stamp
            if old is not None and updated <= old[2]:
                updated = stamp  # Counter changed without a fresh thread timestamp.
            day = datetime.fromtimestamp(max(started_at, min(updated, stamp))).date().isoformat()
            daily[(day, key, label)] += delta
        current_key, current_label = project_identity(counter.get("cwd"))
        upserts.append((account_id, thread_id, tokens, current_key, current_label,
                        counter.get("created_at") or 0, counter.get("updated_at") or 0))
    with db:
        db.execute("INSERT INTO project_usage_state VALUES (?,?,?) "
                   "ON CONFLICT(account_id) DO UPDATE SET last_observed_at=excluded.last_observed_at",
                   (account_id, started_at, stamp))
        db.executemany("""INSERT INTO project_thread_counters VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(account_id,thread_id) DO UPDATE SET tokens=excluded.tokens,
            project_key=excluded.project_key,project_label=excluded.project_label,
            created_at=excluded.created_at,updated_at=excluded.updated_at""", upserts)
        db.executemany("""INSERT INTO project_usage_daily VALUES (?,?,?,?,?)
            ON CONFLICT(account_id,day,project_key) DO UPDATE SET
            tokens=project_usage_daily.tokens+excluded.tokens,
            project_label=excluded.project_label""",
            [(account_id, day, key, label, tokens) for (day, key, label), tokens in daily.items()])
        db.executemany("DELETE FROM project_baseline_pending WHERE account_id=? AND thread_id=?",
                       [(account_id, item[1]) for item in upserts if item[1] in pending])
    return sum(daily.values())


def summary(db: sqlite3.Connection, account_id: str, stamp: float | None = None) -> dict:
    stamp = time.time() if stamp is None else stamp
    today = datetime.fromtimestamp(stamp).date()
    week_start = (today - timedelta(days=6)).isoformat()
    state = db.execute("SELECT started_at,last_observed_at FROM project_usage_state WHERE account_id=?",
                       (account_id,)).fetchone()
    counts = {row[0]: {"workspace": row[1], "chats": row[2], "today": 0, "week": 0, "since_start": 0}
              for row in db.execute("SELECT project_key,MAX(project_label),COUNT(*) FROM project_thread_counters "
                                    "WHERE account_id=? GROUP BY project_key", (account_id,))}
    for key, label, day, tokens in db.execute(
            "SELECT project_key,project_label,day,tokens FROM project_usage_daily WHERE account_id=?",
            (account_id,)):
        item = counts.setdefault(key, {"workspace": label, "chats": 0, "today": 0, "week": 0, "since_start": 0})
        item["since_start"] += tokens
        if day >= week_start:
            item["week"] += tokens
        if day == today.isoformat():
            item["today"] += tokens
    projects = sorted(counts.values(), key=lambda item: (-item["week"], -item["today"], -item["chats"], item["workspace"]))
    return {"started_at": state[0] if state else None, "last_observed_at": state[1] if state else None,
            "today": sum(item["today"] for item in projects),
            "week": sum(item["week"] for item in projects),
            "since_start": sum(item["since_start"] for item in projects),
            "projects": projects}
