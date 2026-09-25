"""Local Codex Manager dashboard and workspace. Python 3.11+, no third-party packages."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
import time
import tomllib
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from alerting import AlertDispatcher
from codex_conversation import read_thread_goal
from codex_rpc import _recent_local_threads, _recent_thread_updates, is_user_thread, read_account, read_recent_turns
from environment_catalog import (change_archive, chat_catalog, deduplicate_chats,
                                 duplicate_plan, inventory, transfer_chats)
from monitoring_policy import (default_preferences, quiet_now, quota_buckets,
                               quota_outlook, token_totals, validate_preferences)
from project_usage import (install_schema as install_project_usage_schema,
                           observe as observe_project_tokens,
                           read_thread_counters, summary as project_usage_summary)
from telegram_commands import TelegramCommandBot
from workspace_cli import ManagedCodex
from workspace_diagnostics import diagnose as diagnose_workspace_agent
from workspace_federation import (PeerClient, load_config as load_federation_config,
                                  poll_client as poll_federation_client, prepare_channel,
                                  start_host as start_federation_host)
from workspace_mailbox import DEFAULT_DB as WORKSPACE_MAILBOX_DB, GENERAL, Mailbox, agent_address, valid_address
from workspace_hook_receipts import reconcile as reconcile_hook_receipts


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
WINDOW_TTL = 55
STALL_AFTER = 20 * 60
UNCONFIRMED_AFTER = 3 * 60
EVENTS = {"SessionStart", "UserPromptSubmit", "PermissionRequest", "PostToolUse", "Stop", "Interrupt", "SubagentStart", "SubagentStop"}


def now() -> float:
    return time.time()


def compact(value, limit=180):
    return str(value or "").replace("\n", " ").strip()[:limit]


def normalize_path(path):
    return os.path.normcase(os.path.normpath(path)) if path else ""


def default_accounts():
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "VSCode-Codex"
    return [
        {"id": "compte-1", "name": "Compte 1", "codex_home": str(base / "Compte-1/codex")},
        {"id": "compte-2", "name": "Compte 2", "codex_home": str(base / "Compte-2/codex")},
    ]


def demo_workspace_messages():
    """Fictitious session-level MCP traffic for the isolated dashboard demonstration."""
    base = datetime.now().astimezone() - timedelta(minutes=17)
    examples = [
        ("general", "compte-1:demo-thread-1", "Je vérifie le rendu des fenêtres actives."),
        ("general", "compte-2:demo-thread-4", "La collecte des quotas fonctionne sur mon environnement."),
        ("private:demo", "compte-1:demo-thread-1", "Peux-tu contrôler le rafraîchissement des quotas ?"),
        ("private:demo", "compte-2:demo-thread-4", "Oui. Je compare les deux derniers relevés."),
        ("general", "compte-1:demo-thread-2", "Les tests API sont terminés pour cette passe."),
        ("private:demo", "compte-2:demo-thread-4", "Le relevé est stable ; aucun écart détecté."),
    ]
    return [{"id": index, "channel_id": channel, "sender_agent_id": sender, "content": content,
             "created_at": (base + timedelta(minutes=index * 3)).isoformat(timespec="seconds"),
             "reply_to_id": None, "read_at": None}
            for index, (channel, sender, content) in enumerate(examples, 1)]


def demo_workspace_overview(messages, snapshot):
    directory = []
    for account in snapshot["accounts"]:
        threads = ((account.get("metrics") or {}).get("threads") or {}).get("data") or []
        for thread in threads:
            session_id = thread["id"]
            relevant = [task for task in account.get("tasks", []) if task.get("session_id") == session_id]
            state = relevant[0].get("display_status") if relevant else ""
            status = ("working" if state == "active" else "attention" if state in
                      {"approval", "waiting", "stalled", "unconfirmed"} else
                      "error" if state == "failed" else "idle")
            directory.append({"id": agent_address(account["id"], session_id),
                           "account_id": account["id"], "session_id": session_id,
                           "name": thread.get("name") or "Conversation Codex",
                           "workspace": relevant[0].get("workspace", "") if relevant else "",
                           "status": status, "last_seen": datetime.now().astimezone().isoformat()})
    agents = workspace_active_agents(snapshot)
    member_ids = [agent["id"] for agent in agents]
    channels = [
        {"id": GENERAL, "kind": "general", "name": "Général", "members": member_ids,
         "participants": len(member_ids)},
        {"id": "private:demo", "kind": "private", "name": "Échange privé",
         "members": ["compte-1:demo-thread-1", "compte-2:demo-thread-4"], "participants": 2},
    ]
    connections = {}
    for message in messages:
        channel = next(item for item in channels if item["id"] == message["channel_id"])
        channel["messages"] = channel.get("messages", 0) + 1
        channel["last_id"] = message["id"]
        channel["last_at"] = message["created_at"]
        if channel["kind"] == "private":
            recipient = next(item for item in channel["members"] if item != message["sender_agent_id"])
            route = (message["sender_agent_id"], recipient)
            edge = connections.setdefault(route, {"channel_id": channel["id"],
                "sender": route[0], "recipient": route[1], "messages": 0, "last_id": 0})
            edge["messages"] += 1
            edge["last_id"] = message["id"]
    for channel in channels:
        channel.setdefault("messages", 0)
        channel.setdefault("last_id", None)
        channel.setdefault("last_at", None)
    general_senders = {}
    for message in messages:
        if message["channel_id"] == GENERAL:
            sender = message["sender_agent_id"]
            general_senders[sender] = general_senders.get(sender, 0) + 1
    graph_ids = {message["sender_agent_id"] for message in messages} & set(member_ids)
    graph_ids.update(edge["recipient"] for edge in connections.values() if edge["recipient"] in member_ids)
    graph_activity = {(message["channel_id"], message["sender_agent_id"]) for message in messages
                      if message["sender_agent_id"] in member_ids}
    graph_activity.update((edge["channel_id"], edge["recipient"]) for edge in connections.values()
                          if edge["recipient"] in member_ids)
    return {"agents": agents, "directory": directory, "channels": channels, "connections": list(connections.values()),
            "graph_agents": [agent for agent in agents if agent["id"] in graph_ids], "launches": [],
            "graph_activity": [{"channel_id": channel_id, "agent_id": agent_id}
                               for channel_id, agent_id in sorted(graph_activity)],
            "general_senders": [{"sender": sender, "messages": count}
                                for sender, count in general_senders.items()]}


def demo_workspace_tasks(messages):
    created = messages[0]["created_at"]
    updated = messages[-1]["created_at"]
    examples = [
        ("task-demo-1", "Suivre l’activité des fenêtres", "working",
         "compte-1:demo-thread-1", "Valider la détection des fenêtres et des chats actifs.",
         "Détection confirmée sur deux fenêtres."),
        ("task-demo-2", "Vérifier les relevés de quotas", "review",
         "compte-2:demo-thread-4", "Comparer les relevés des deux comptes.",
         "Comparaison terminée ; validation attendue."),
        ("task-demo-3", "Documenter le scénario de test", "todo", None,
         "Écrire les étapes du premier essai de collaboration.", ""),
    ]
    return [{"id": identity, "channel_id": GENERAL, "title": title,
             "description": description, "status": status, "assignee_agent_id": assignee,
             "creator_agent_id": None, "created_at": created, "updated_at": updated,
             "revision": index, "latest_note": note}
            for index, (identity, title, status, assignee, description, note)
            in enumerate(examples, 1)]


def workspace_active_agents(snapshot: dict) -> list[dict]:
    """Only chats with current work or an approval in a confirmed VS Code window."""
    stamp = snapshot["now"]
    agents = []
    for account in snapshot["accounts"]:
        window_ids = {window["id"] for window in account.get("windows", [])}
        threads = {item["id"]: item for item in
                   (((account.get("metrics") or {}).get("threads") or {}).get("data") or [])
                   if item.get("id")}
        seen = set()
        for task in account.get("tasks", []):
            session_id = task.get("session_id")
            if not session_id or session_id in seen:
                continue
            seen.add(session_id)
            state = task.get("display_status")
            age = stamp - task.get("updated_at", 0)
            if task.get("window_id") not in window_ids or state not in {"active", "approval", "waiting"}:
                continue
            if age > (900 if state in {"approval", "waiting"} else 120):
                continue
            thread = threads.get(session_id, {})
            agents.append({"id": agent_address(account["id"], session_id),
                           "account_id": account["id"], "session_id": session_id,
                           "name": compact(thread.get("name") or thread.get("preview") or task.get("title"), 160)
                                   or "Conversation Codex",
                           "workspace": task.get("workspace") or thread.get("cwd") or "",
                           "status": "working" if state == "active" else "attention",
                           "activity_at": task.get("updated_at"),
                           "window_id": task["window_id"]})
    return agents


class Store:
    def __init__(self, path: str, accounts: list[dict], notifier=None):
        self.accounts = accounts
        self.started_at = now()
        self.account_ids = {account["id"] for account in accounts}
        self.answer_goal_retries = {}
        self.notifier = notifier
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.RLock()
        self.changed = threading.Condition()
        self.version = 0
        with self.lock:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS windows (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, label TEXT NOT NULL,
                    workspace TEXT NOT NULL, roots_json TEXT NOT NULL DEFAULT '[]', last_seen REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL, title TEXT NOT NULL, workspace TEXT NOT NULL,
                    status TEXT NOT NULL, started_at REAL NOT NULL, updated_at REAL NOT NULL,
                    ended_at REAL, source TEXT NOT NULL, error_message TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_account_updated ON tasks(account_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                    task_id TEXT, type TEXT NOT NULL, at REAL NOT NULL, details TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metrics (
                    account_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS goal_states (
                    account_id TEXT NOT NULL, thread_id TEXT NOT NULL, status TEXT NOT NULL,
                    updated_at REAL NOT NULL, objective TEXT NOT NULL DEFAULT '',
                    token_budget TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(account_id, thread_id)
                );
                -- Legacy completion-only outbox, retained for a one-time migration.
                CREATE TABLE IF NOT EXISTS goal_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL, goal_updated_at REAL NOT NULL,
                    label TEXT NOT NULL, created_at REAL NOT NULL, sent_at REAL,
                    UNIQUE(account_id, thread_id, goal_updated_at)
                );
                CREATE TABLE IF NOT EXISTS goal_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL, kind TEXT NOT NULL,
                    previous_status TEXT NOT NULL, status TEXT NOT NULL,
                    label TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL, sent_at REAL, legacy_alert_id INTEGER UNIQUE
                );
                CREATE INDEX IF NOT EXISTS idx_goal_notifications_pending
                    ON goal_notifications(sent_at, id);
                CREATE TABLE IF NOT EXISTS answer_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                    label TEXT NOT NULL, window_label TEXT NOT NULL DEFAULT '',
                    completed_at REAL NOT NULL, created_at REAL NOT NULL, sent_at REAL,
                    UNIQUE(account_id, thread_id, turn_id)
                );
                CREATE INDEX IF NOT EXISTS idx_answer_notifications_pending
                    ON answer_notifications(sent_at, id);
                CREATE TABLE IF NOT EXISTS action_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL,
                    window_label TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, sent_at REAL,
                    UNIQUE(account_id,task_id,kind)
                );
                CREATE INDEX IF NOT EXISTS idx_action_notifications_pending
                    ON action_notifications(sent_at,id);
                CREATE TABLE IF NOT EXISTS preferences (
                    id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metric_alert_states (
                    account_id TEXT NOT NULL, metric TEXT NOT NULL, period_key TEXT NOT NULL,
                    active INTEGER NOT NULL, updated_at REAL NOT NULL,
                    PRIMARY KEY(account_id, metric)
                );
                CREATE TABLE IF NOT EXISTS metric_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                    metric TEXT NOT NULL, period_key TEXT NOT NULL, label TEXT NOT NULL,
                    value REAL NOT NULL, threshold REAL NOT NULL,
                    created_at REAL NOT NULL, sent_at REAL,
                    UNIQUE(account_id, metric, period_key)
                );
                CREATE TABLE IF NOT EXISTS quota_samples (
                    account_id TEXT NOT NULL, bucket TEXT NOT NULL, reset_at REAL NOT NULL,
                    observed_at REAL NOT NULL, used_percent REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_quota_samples ON quota_samples(account_id,bucket,reset_at,observed_at);
                -- Remove state left by the discontinued Telegram write mode.
                DROP TABLE IF EXISTS telegram_reply_prompts;
                DROP TABLE IF EXISTS telegram_chat_routes;
                DROP TABLE IF EXISTS telegram_origin_turns;
            """)
            install_project_usage_schema(self.db)
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(windows)")}
            if "roots_json" not in columns:
                self.db.execute("ALTER TABLE windows ADD COLUMN roots_json TEXT NOT NULL DEFAULT '[]'")
                self.db.commit()
            task_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(tasks)")}
            if "error_message" not in task_columns:
                self.db.execute("ALTER TABLE tasks ADD COLUMN error_message TEXT NOT NULL DEFAULT ''")
                self.db.commit()
            goal_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(goal_states)")}
            for column in ("objective", "token_budget", "label"):
                if column not in goal_columns:
                    self.db.execute(f"ALTER TABLE goal_states ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            self.db.execute("""INSERT OR IGNORE INTO goal_notifications
                (account_id, thread_id, kind, previous_status, status, label, created_at, sent_at, legacy_alert_id)
                SELECT account_id, thread_id, 'status', 'active', 'complete', label, created_at, sent_at, id
                FROM goal_alerts WHERE sent_at IS NULL""")
            self.db.commit()

    def _user_thread(self, account_id: str, thread_id: str) -> bool:
        """Use Codex's local index to distinguish a chat from its subagents."""
        account = next((item for item in self.accounts if item["id"] == account_id), None)
        if not account:
            return False
        home = account.get("codex_home")
        return is_user_thread(Path(home), thread_id) if home else True

    def _goal_status(self, account_id: str, thread_id: str) -> str:
        with self.lock:
            row = self.db.execute(
                "SELECT status FROM goal_states WHERE account_id=? AND thread_id=?",
                (account_id, thread_id),
            ).fetchone()
        return row["status"] if row else "none"

    def preferences(self):
        with self.lock:
            row = self.db.execute("SELECT payload FROM preferences WHERE id=1").fetchone()
        saved = json.loads(row["payload"]) if row else None
        default = default_preferences(self.accounts)
        if not isinstance(saved, dict):
            return default
        for account_id, config in default["accounts"].items():
            config.update((saved.get("accounts") or {}).get(account_id) or {})
        default["quiet_hours"].update(saved.get("quiet_hours") or {})
        return default

    def save_preferences(self, value):
        validated = validate_preferences(value, self.accounts)
        with self.lock:
            self.db.execute("INSERT INTO preferences(id,payload) VALUES (1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                            (json.dumps(validated, ensure_ascii=False),))
            self.db.commit()
        self.announce()

    def _notification_enabled(self, account_id, category):
        return self.preferences()["accounts"][account_id][f"notify_{category}"]

    def _metric_alerts(self, account_id, data):
        """Queue each threshold crossing once per period. Called under self.lock."""
        settings = self.preferences()["accounts"][account_id]
        stamp = now()
        observations = []
        for name, bucket in quota_buckets(data):
            reset = bucket.get("resetsAt")
            period = f"{bucket.get('windowDurationMins')}:{reset}"
            remaining = max(0, min(100, 100 - bucket["usedPercent"]))
            bucket_label = "principal" if name == "primary" else "secondaire"
            observations.append((f"quota_{name}", period, remaining,
                                 settings["quota_remaining_percent"], True,
                                 f"quota {bucket_label} disponible : {remaining:.0f} %"))
            if isinstance(reset, (int, float)):
                self.db.execute("INSERT INTO quota_samples VALUES (?,?,?,?,?)",
                                (account_id, name, reset, stamp, bucket["usedPercent"]))
        daily, weekly = token_totals(data)
        if daily is not None:
            observations.append(("tokens_daily", datetime.now().date().isoformat(), daily,
                                 settings["tokens_daily"], False,
                                 f"Tokens aujourd'hui : {daily:,.0f}".replace(",", " ")))
        if weekly is not None:
            week = datetime.now().date().isocalendar()
            observations.append(("tokens_weekly", f"{week.year}-W{week.week:02d}", weekly,
                                 settings["tokens_weekly"], False,
                                 f"Tokens sur 7 jours : {weekly:,.0f}".replace(",", " ")))
        for metric, period, value, threshold, below, label in observations:
            active = value <= threshold if below else value >= threshold
            previous = self.db.execute("SELECT period_key,active FROM metric_alert_states WHERE account_id=? AND metric=?",
                                       (account_id, metric)).fetchone()
            self.db.execute("""INSERT INTO metric_alert_states VALUES (?,?,?,?,?)
                ON CONFLICT(account_id,metric) DO UPDATE SET period_key=excluded.period_key,
                active=excluded.active,updated_at=excluded.updated_at""",
                (account_id, metric, period, int(active), stamp))
            crossing = active and previous and (not previous["active"] or previous["period_key"] != period)
            if not crossing:
                continue
            category = "quota" if below else "tokens"
            if not settings[f"notify_{category}"]:
                continue
            self.db.execute("""INSERT OR IGNORE INTO metric_notifications
                (account_id,metric,period_key,label,value,threshold,created_at)
                VALUES (?,?,?,?,?,?,?)""", (account_id, metric, period, label, value, threshold, stamp))
            self.db.execute("INSERT INTO events(account_id,task_id,type,at,details) VALUES (?,?,?,?,?)",
                            (account_id, None, "metric_alert", stamp,
                             json.dumps({"metric": metric, "label": label}, ensure_ascii=False)))
            if below and self.notifier:
                self.notifier.emit(account_id, f"{label} ; seuil {threshold} %")
        self.db.execute("DELETE FROM quota_samples WHERE observed_at<?", (stamp - 8 * 86400,))

    def flush_metric_alerts(self):
        if not getattr(self.notifier, "telegram_enabled", False):
            return
        if quiet_now(self.preferences()["quiet_hours"]):
            return
        names = {account["id"]: account["name"] for account in self.accounts}
        with self.lock:
            pending = [dict(row) for row in self.db.execute(
                "SELECT * FROM metric_notifications WHERE sent_at IS NULL ORDER BY id LIMIT 20")]
        for alert in pending:
            category = "quota" if alert["metric"].startswith("quota") else "tokens"
            if (not self._notification_enabled(alert["account_id"], category)
                    or not self._metric_still_active(alert)):
                delivered = True
            else:
                delivered = self.notifier.metric_warning(names.get(alert["account_id"], alert["account_id"]),
                                                         alert["metric"], alert["label"], alert["threshold"])
            if not delivered:
                break
            with self.lock:
                self.db.execute("UPDATE metric_notifications SET sent_at=? WHERE id=? AND sent_at IS NULL",
                                (now(), alert["id"]))
                self.db.commit()

    def _metric_still_active(self, alert):
        """Drop queued warnings that became obsolete during quiet hours or a reset."""
        with self.lock:
            row = self.db.execute("SELECT payload FROM metrics WHERE account_id=?",
                                  (alert["account_id"],)).fetchone()
        if not row:
            return False
        metrics = json.loads(row["payload"])
        settings = self.preferences()["accounts"][alert["account_id"]]
        metric = alert["metric"]
        if metric.startswith("quota_"):
            name = metric.removeprefix("quota_")
            bucket = dict(quota_buckets(metrics)).get(name)
            if not bucket or f"{bucket.get('windowDurationMins')}:{bucket.get('resetsAt')}" != alert["period_key"]:
                return False
            return 100 - bucket["usedPercent"] <= settings["quota_remaining_percent"]
        daily, weekly = token_totals(metrics)
        if metric == "tokens_daily":
            return (alert["period_key"] == datetime.now().date().isoformat() and
                    daily is not None and daily >= settings["tokens_daily"])
        week = datetime.now().date().isocalendar()
        return (alert["period_key"] == f"{week.year}-W{week.week:02d}" and
                weekly is not None and weekly >= settings["tokens_weekly"])

    def announce(self):
        with self.changed:
            self.version += 1
            self.changed.notify_all()

    def heartbeat(self, payload: dict):
        account = compact(payload.get("account_id"), 50)
        window_id = compact(payload.get("window_id"), 120)
        if account not in self.account_ids or not window_id:
            raise ValueError("Compte ou fenêtre inconnu")
        label = compact(payload.get("label"), 100) or "Fenêtre VS Code"
        workspace = compact(payload.get("workspace"), 500)
        folders = payload.get("workspace_folders")
        roots = [compact(folder, 500) for folder in folders[:20] if isinstance(folder, str)] if isinstance(folders, list) else []
        if not roots and workspace:
            roots = [workspace]
        with self.lock:
            self.db.execute("""INSERT INTO windows(id,account_id,label,workspace,roots_json,last_seen) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET account_id=excluded.account_id,
                label=excluded.label, workspace=excluded.workspace, roots_json=excluded.roots_json,
                last_seen=excluded.last_seen""",
                (window_id, account, label, workspace, json.dumps(roots), now()))
            self.db.commit()
        self.announce()

    def event(self, payload: dict):
        account = compact(payload.get("account_id"), 50)
        if account not in self.account_ids:
            raise ValueError("Compte inconnu")
        event_type = payload.get("hook_event_name") or payload.get("type")
        if event_type not in EVENTS and event_type != "agent-turn-complete":
            raise ValueError("Événement non reconnu")
        session_id = compact(payload.get("session_id") or payload.get("thread-id"), 130)
        turn_id = compact(payload.get("turn_id") or payload.get("turn-id"), 130)
        agent_id = compact(payload.get("agent_id"), 130)
        user_thread = self._user_thread(account, session_id)
        workspace = compact(payload.get("cwd"), 500)
        stamp = now()
        task_id = f"{account}:{session_id}:{agent_id if event_type.startswith('Subagent') else turn_id}"
        status = {
            "UserPromptSubmit": "active", "PermissionRequest": "approval",
            "Stop": "completed", "agent-turn-complete": "completed",
            "Interrupt": "interrupted", "SubagentStart": "active", "SubagentStop": "completed",
        }.get(event_type)
        title = compact(payload.get("prompt") or payload.get("last-assistant-message") or payload.get("last_assistant_message"), 120)
        if event_type.startswith("Subagent"):
            title = compact(payload.get("agent_type"), 90) or "Sous-agent"
        if not title:
            title = "Tâche Codex"
        details = json.dumps({"session_id": session_id, "turn_id": turn_id,
                              "agent_id": agent_id, "label": title}, ensure_ascii=False)
        with self.lock:
            if event_type == "PostToolUse" and session_id:
                existing = self.db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
                if existing and existing["status"] in {"active", "approval"}:
                    self.db.execute("UPDATE tasks SET status='active', updated_at=? WHERE id=?", (stamp, task_id))
                elif not existing:
                    status = "active"
            if status and session_id:
                previous = self.db.execute("SELECT title, workspace, started_at FROM tasks WHERE id=?", (task_id,)).fetchone()
                if previous:
                    title = title if event_type == "UserPromptSubmit" else previous["title"]
                    workspace = workspace or previous["workspace"]
                    started = previous["started_at"]
                else:
                    started = stamp
                ended = stamp if status in {"completed", "interrupted", "failed"} else None
                self.db.execute("""INSERT INTO tasks(id,account_id,session_id,turn_id,title,workspace,status,started_at,updated_at,ended_at,source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET title=excluded.title, workspace=excluded.workspace,
                    status=excluded.status, updated_at=excluded.updated_at, ended_at=excluded.ended_at,
                    source=excluded.source""",
                    (task_id, account, session_id, turn_id or agent_id, title, workspace,
                     status, started, stamp, ended, "hook"))
                if event_type in {"Stop", "agent-turn-complete"} and turn_id:
                    self._queue_answer(account, session_id, turn_id, workspace, stamp)
                if event_type == "PermissionRequest" and turn_id and user_thread:
                    self._queue_action(account, task_id, "approval", workspace, title)
            self.db.execute("INSERT INTO events(account_id,task_id,type,at,details) VALUES (?,?,?,?,?)",
                            (account, task_id if status else None, event_type, stamp, details))
            self.db.commit()
        self.announce()
        if (self.notifier and user_thread and
                event_type in {"PermissionRequest", "Stop", "agent-turn-complete", "Interrupt"}):
            label = {"PermissionRequest": "validation attendue", "Stop": "tâche terminée",
                     "agent-turn-complete": "tâche terminée", "Interrupt": "tâche interrompue"}[event_type]
            self.notifier.emit(account, label)

    def reconcile_turns(self, account_id: str, turns: list[dict]):
        transitions = []
        with self.lock:
            for turn in turns:
                raw_status = turn.get("status")
                if raw_status not in {"completed", "failed", "interrupted"}:
                    continue
                session_id = compact(turn.get("thread_id"), 130)
                turn_id = compact(turn.get("turn_id"), 130)
                if not session_id or not turn_id or not self._user_thread(account_id, session_id):
                    continue
                task_id = f"{account_id}:{session_id}:{turn_id}"
                previous = self.db.execute("SELECT status,title,source,updated_at FROM tasks WHERE id=?", (task_id,)).fetchone()
                started = turn.get("started_at") or turn.get("thread_updated_at") or now()
                ended = turn.get("completed_at")
                # An interrupted turn may omit completedAt. The thread's updatedAt can
                # already belong to a newer running turn, so it is not a safe end time.
                updated = ended or (previous["updated_at"] if previous else started)
                title = compact(turn.get("title"), 120) or "Conversation Codex"
                if previous and previous["source"] == "hook":
                    title = previous["title"]
                error_message = compact(turn.get("error_message"), 300)
                self.db.execute("""INSERT INTO tasks(id,account_id,session_id,turn_id,title,workspace,status,started_at,updated_at,ended_at,source,error_message)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status, updated_at=excluded.updated_at, ended_at=excluded.ended_at,
                    error_message=CASE WHEN excluded.error_message<>'' THEN excluded.error_message ELSE tasks.error_message END""",
                    (task_id, account_id, session_id, turn_id, title, compact(turn.get("cwd"), 500),
                     raw_status, started, updated, ended, "appserver", error_message))
                if previous and previous["status"] in {"active", "approval"} and raw_status == "failed":
                    transitions.append((task_id, updated))
                if (raw_status == "failed" and isinstance(ended, (int, float))
                        and ended >= self.started_at and (not previous or previous["status"] != "failed")):
                    self._queue_action(account_id, task_id, "failed", compact(turn.get("cwd"), 500), title)
                if raw_status == "completed" and isinstance(ended, (int, float)) and ended >= self.started_at:
                    self._queue_answer(account_id, session_id, turn_id, compact(turn.get("cwd"), 500), ended)
                if (raw_status in {"completed", "failed", "interrupted"}
                        and (not previous or previous["status"] != raw_status)
                        and not (raw_status == "failed" and previous and previous["status"] in {"active", "approval"})
                        and isinstance(ended, (int, float)) and ended >= self.started_at):
                    self.db.execute("INSERT INTO events(account_id,task_id,type,at,details) VALUES (?,?,?,?,?)",
                                    (account_id, task_id, raw_status, ended,
                                     json.dumps({"label": title}, ensure_ascii=False)))
            for task_id, stamp in transitions:
                self.db.execute("INSERT INTO events(account_id,task_id,type,at,details) VALUES (?,?,?,?,?)",
                                (account_id, task_id, "failed", stamp, "{}"))
            self.db.commit()
        if transitions:
            self.announce()
            if self.notifier:
                self.notifier.emit(account_id, "erreur d'agent")

    def set_metrics(self, account_id: str, data: dict):
        with self.lock:
            previous = self.db.execute("SELECT payload FROM metrics WHERE account_id=?", (account_id,)).fetchone()
            previous_data = json.loads(previous["payload"]) if previous else {}
            if data.get("collector_error"):
                data = {**previous_data, "collector_error": data["collector_error"]}
            else:
                data = {**data, "last_success_at": now()}
            if not data.get("collector_error"):
                self._observe_goals(account_id, data, previous_data.get("last_success_at"))
                self._metric_alerts(account_id, data)
            self.db.execute("""INSERT INTO metrics VALUES (?,?,?) ON CONFLICT(account_id)
                DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at""",
                (account_id, json.dumps(data, ensure_ascii=False), now()))
            self.db.commit()
        self.announce()

    def _observe_goals(self, account_id: str, data: dict, previous_poll_at):
        """Queue meaningful goal changes; the first account poll is a quiet baseline."""
        threads = (((data.get("threads") or {}).get("data") or [])
                   + (data.get("tracked_goals") or []))
        for thread in threads:
            thread_id = compact(thread.get("id"), 130)
            if not thread_id or "goal" not in thread:
                continue
            goal = thread["goal"]
            status = goal.get("status") if isinstance(goal, dict) else "none"
            if not status:
                continue
            previous = self.db.execute(
                "SELECT status, objective, token_budget, label FROM goal_states WHERE account_id=? AND thread_id=?",
                (account_id, thread_id),
            ).fetchone()
            observed_at = now()
            goal_updated_at = goal.get("updatedAt") if isinstance(goal, dict) else None
            objective = compact(goal.get("objective"), 4000) if isinstance(goal, dict) else ""
            token_budget = json.dumps(goal.get("tokenBudget")) if isinstance(goal, dict) else "null"
            label = (compact(thread.get("name") or thread.get("preview"), 180)
                     or (previous["label"] if previous else "")
                     or compact(objective, 180) or "Objectif Codex")
            kind = None
            previous_status = previous["status"] if previous else "none"
            if previous:
                if previous_status != status:
                    kind = "status"
                elif previous["objective"] and objective and previous["objective"] != objective:
                    kind = "objective"
                elif previous["token_budget"] and previous["token_budget"] != token_budget:
                    kind = "budget"
            elif (status != "none" and isinstance(goal_updated_at, (int, float))
                  and isinstance(previous_poll_at, (int, float)) and goal_updated_at > previous_poll_at):
                kind = "status"
            if kind:
                detail = ""
                if kind == "budget":
                    budget = goal.get("tokenBudget")
                    detail = f"Nouveau budget : {budget:,} tokens".replace(",", " ") if isinstance(budget, int) else "Budget sans limite"
                elif kind == "objective":
                    detail = f"Nouvel objectif : {compact(objective, 140)}"
                self.db.execute("INSERT INTO events(account_id,task_id,type,at,details) VALUES (?,?,?,?,?)",
                                (account_id, None, "goal_change", observed_at,
                                 json.dumps({"kind": kind, "status": status, "label": label}, ensure_ascii=False)))
                if getattr(self.notifier, "telegram_enabled", False) and self._notification_enabled(account_id, "goals"):
                    self.db.execute("""INSERT INTO goal_notifications
                        (account_id, thread_id, kind, previous_status, status, label, detail, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (account_id, thread_id, kind, previous_status, status, label, detail, observed_at))
            self.db.execute("""INSERT INTO goal_states
                (account_id, thread_id, status, updated_at, objective, token_budget, label)
                VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(account_id, thread_id)
                DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at,
                    objective=excluded.objective, token_budget=excluded.token_budget,
                    label=excluded.label""",
                (account_id, thread_id, status, observed_at, objective, token_budget, label))

    def flush_goal_alerts(self):
        """Retry pending Telegram deliveries on subsequent account polls."""
        if not getattr(self.notifier, "telegram_enabled", False):
            return
        if quiet_now(self.preferences()["quiet_hours"]):
            return
        names = {account["id"]: account["name"] for account in self.accounts}
        with self.lock:
            pending = [dict(row) for row in self.db.execute(
                "SELECT * FROM goal_notifications WHERE sent_at IS NULL ORDER BY id LIMIT 20")]
        for alert in pending:
            if (self._notification_enabled(alert["account_id"], "goals") and
                    not self.notifier.goal_changed(names.get(alert["account_id"], alert["account_id"]),
                                                   alert["label"], alert["kind"],
                                                   alert["previous_status"], alert["status"], alert["detail"])):
                break
            with self.lock:
                self.db.execute("UPDATE goal_notifications SET sent_at=? WHERE id=? AND sent_at IS NULL",
                                (now(), alert["id"]))
                self.db.commit()

    def _queue_answer(self, account_id: str, thread_id: str, turn_id: str,
                      workspace: str, completed_at: float):
        """Called under self.lock; the unique turn key deduplicates hooks and polling."""
        if (not getattr(self.notifier, "telegram_enabled", False)
                or not self._notification_enabled(account_id, "answers")
                or not self._user_thread(account_id, thread_id)
                or self._goal_status(account_id, thread_id) != "none"):
            return
        metrics_row = self.db.execute("SELECT payload FROM metrics WHERE account_id=?", (account_id,)).fetchone()
        metrics = json.loads(metrics_row["payload"]) if metrics_row else {}
        threads = ((metrics.get("threads") or {}).get("data") or [])
        thread = next((item for item in threads if item.get("id") == thread_id), {})
        label = compact(thread.get("name"), 180) or compact(Path(workspace).name, 180) or "Conversation Codex"
        window_rows = self.db.execute(
            "SELECT label, roots_json FROM windows WHERE account_id=? AND last_seen>?",
            (account_id, now() - WINDOW_TTL),
        )
        path = normalize_path(workspace)
        matches = [row["label"] for row in window_rows if path and any(
            path == normalize_path(root) or path.startswith(normalize_path(root) + os.sep)
            for root in json.loads(row["roots_json"]))]
        window_label = matches[0] if len(matches) == 1 else ""
        self.db.execute("""INSERT OR IGNORE INTO answer_notifications
            (account_id, thread_id, turn_id, label, window_label, completed_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (account_id, thread_id, turn_id, label, window_label, completed_at, now()))

    def _queue_action(self, account_id, task_id, kind, workspace, label):
        """Queue approvals and failures once per turn, preserving the account boundary."""
        if not getattr(self.notifier, "telegram_enabled", False) or not self._notification_enabled(account_id, "actions"):
            return
        path = normalize_path(workspace)
        rows = self.db.execute("SELECT label,roots_json FROM windows WHERE account_id=? AND last_seen>?",
                               (account_id, now() - WINDOW_TTL))
        matches = [row["label"] for row in rows if path and any(
            path == normalize_path(root) or path.startswith(normalize_path(root) + os.sep)
            for root in json.loads(row["roots_json"]))]
        window_label = matches[0] if len(matches) == 1 else ""
        self.db.execute("""INSERT OR IGNORE INTO action_notifications
            (account_id,task_id,kind,label,window_label,created_at) VALUES (?,?,?,?,?,?)""",
            (account_id, task_id, kind, compact(label, 180) or "Conversation Codex", window_label, now()))

    def flush_action_alerts(self):
        if not getattr(self.notifier, "telegram_enabled", False):
            return
        names = {account["id"]: account["name"] for account in self.accounts}
        with self.lock:
            pending = [dict(row) for row in self.db.execute(
                "SELECT * FROM action_notifications WHERE sent_at IS NULL ORDER BY id LIMIT 20")]
        for alert in pending:
            if (self._notification_enabled(alert["account_id"], "actions") and
                    not self.notifier.action_needed(names.get(alert["account_id"], alert["account_id"]),
                                                    alert["kind"], alert["label"], alert["window_label"])):
                break
            with self.lock:
                self.db.execute("UPDATE action_notifications SET sent_at=? WHERE id=? AND sent_at IS NULL",
                                (now(), alert["id"]))
                self.db.commit()

    def flush_answer_alerts(self):
        """Retry delivery until Telegram confirms it; never duplicate a turn."""
        if not getattr(self.notifier, "telegram_enabled", False):
            return
        if quiet_now(self.preferences()["quiet_hours"]):
            return
        names = {account["id"]: account["name"] for account in self.accounts}
        with self.lock:
            pending = [dict(row) for row in self.db.execute(
                "SELECT * FROM answer_notifications WHERE sent_at IS NULL ORDER BY id LIMIT 20")]
        for alert in pending:
            account_id, thread_id = alert["account_id"], alert["thread_id"]
            send = (self._user_thread(account_id, thread_id)
                    and self._notification_enabled(account_id, "answers")
                    and self._goal_status(account_id, thread_id) == "none")
            if send:
                account = next((item for item in self.accounts if item["id"] == account_id), {})
                home = account.get("codex_home")
                if home:
                    if now() < self.answer_goal_retries.get(alert["id"], 0):
                        continue
                    try:
                        # The goal may have started since the last account poll.
                        goal = read_thread_goal(home, thread_id)
                    except (OSError, RuntimeError, TimeoutError, ValueError):
                        self.answer_goal_retries[alert["id"]] = now() + 30
                        continue
                    send = goal is None or (isinstance(goal, dict) and goal.get("status") == "none")
            if send and not self.notifier.answer_ready(names.get(account_id, account_id),
                                                       alert["label"], alert["window_label"],
                                                       account_id, thread_id):
                break
            with self.lock:
                self.db.execute("UPDATE answer_notifications SET sent_at=? WHERE id=? AND sent_at IS NULL",
                                (now(), alert["id"]))
                self.db.commit()
            self.answer_goal_retries.pop(alert["id"], None)

    def tracked_goal_threads(self, account_id: str) -> tuple[str, ...]:
        """Keep watching nonterminal goals even after they leave the recent list."""
        with self.lock:
            return tuple(row["thread_id"] for row in self.db.execute(
                """SELECT thread_id FROM goal_states WHERE account_id=?
                    AND status NOT IN ('none', 'complete') ORDER BY updated_at DESC LIMIT 24""",
                (account_id,)))

    def observe_project_usage(self, account_id: str, counters: list[dict], observed_at: float | None = None):
        if account_id not in self.account_ids:
            raise ValueError("Compte inconnu")
        with self.lock:
            delta = observe_project_tokens(self.db, account_id, counters, observed_at)
        if delta:
            self.announce()
        return delta

    def recent_thread_metadata(self, account_id: str) -> list[dict]:
        with self.lock:
            row = self.db.execute("SELECT payload FROM metrics WHERE account_id=?", (account_id,)).fetchone()
        metrics = json.loads(row["payload"]) if row else {}
        return ((metrics.get("threads") or {}).get("data") or [])[:16]

    def snapshot(self):
        stamp = now()
        with self.lock:
            windows = [dict(row) for row in self.db.execute("SELECT * FROM windows WHERE last_seen>? ORDER BY account_id,label", (stamp - WINDOW_TTL,))]
            tasks = [dict(row) for row in self.db.execute("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT 100")]
            metrics = {row["account_id"]: {**json.loads(row["payload"]), "updated_at": row["updated_at"]}
                       for row in self.db.execute("SELECT * FROM metrics")}
            events = [dict(row) for row in self.db.execute(
                """SELECT * FROM events WHERE type IN
                    ('UserPromptSubmit','PermissionRequest','Stop','Interrupt','failed','completed',
                     'interrupted','goal_change','metric_alert') ORDER BY id DESC LIMIT 40""")]
            last_hooks = {row["account_id"]: row["last_at"] for row in self.db.execute(
                "SELECT account_id,MAX(at) AS last_at FROM events WHERE type IN "
                "('UserPromptSubmit','PostToolUse','Stop','PermissionRequest','Interrupt') GROUP BY account_id")}
            projections = {}
            outlooks = {}
            projects = {account["id"]: project_usage_summary(self.db, account["id"], stamp)
                        for account in self.accounts}
            for account in self.accounts:
                account_id = account["id"]
                account_metrics = metrics.get(account_id) or {}
                projections[account_id] = {}
                outlooks[account_id] = {}
                for name, bucket in quota_buckets(account_metrics):
                    reset = bucket.get("resetsAt")
                    if not isinstance(reset, (int, float)):
                        continue
                    samples = [(row["observed_at"], row["used_percent"]) for row in self.db.execute(
                        """SELECT observed_at,used_percent FROM quota_samples
                            WHERE account_id=? AND bucket=? AND reset_at=?
                            ORDER BY observed_at DESC LIMIT 30""", (account_id, name, reset))]
                    observations = list(reversed(samples))
                    outlook = quota_outlook(observations, reset, stamp)
                    outlooks[account_id][name] = outlook
                    projections[account_id][name] = outlook.get("at") if outlook["status"] == "risk" else None
        inferred = []
        for account in self.accounts:
            account_id = account["id"]
            account_metrics = metrics.get(account_id) or {}
            writes = account_metrics.get("session_writes") or {}
            threads = ((account_metrics.get("threads") or {}).get("data") or [])
            local_updates = {**(account_metrics.get("thread_updates") or {}),
                             **_recent_thread_updates(Path(account.get("codex_home", "")),
                                                      {thread["id"] for thread in threads if thread.get("id")})}
            recent_turns = {turn.get("thread_id"): turn for turn in account_metrics.get("recent_turns") or []}
            for thread in threads:
                thread_id = thread.get("id")
                updated = max(thread.get("updatedAt") or 0, local_updates.get(thread_id) or 0)
                if not thread_id or not isinstance(updated, (int, float)):
                    continue
                latest_terminal = max((task["updated_at"] for task in tasks
                                       if task["account_id"] == account_id and task["session_id"] == thread_id
                                       and task["status"] in {"completed", "failed", "interrupted"}), default=0)
                uncertain_interruption = any(
                    task["account_id"] == account_id and task["session_id"] == thread_id
                    and task["status"] == "interrupted" and task["ended_at"] is None
                    and task["updated_at"] == latest_terminal for task in tasks)
                runtime = thread.get("status") or {}
                turn = recent_turns.get(thread_id) or {}
                confirmed = runtime.get("type") == "active" or turn.get("status") == "inProgress"
                write_at = writes.get(thread_id, 0)
                activity_threshold = latest_terminal + (1 if uncertain_interruption else 8)
                observed = (stamp - updated < 120 and updated > activity_threshold
                            and (stamp - write_at < 120 or
                                 local_updates.get(thread_id, 0) > activity_threshold))
                if not confirmed and not observed:
                    continue
                if any(task["account_id"] == account_id and task["session_id"] == thread_id
                       and task["status"] in {"active", "approval", "waiting"}
                       and task["updated_at"] > latest_terminal
                       and task["updated_at"] >= updated - 8 for task in tasks):
                    continue
                flags = runtime.get("activeFlags") or []
                status = "approval" if "waitingOnApproval" in flags else "active"
                inferred.append({"id": f"observed:{account_id}:{thread_id}", "account_id": account_id,
                                 "session_id": thread_id, "turn_id": "", "title": compact(thread.get("name") or thread.get("preview"), 120) or "Conversation Codex",
                                 "workspace": compact(thread.get("cwd"), 500), "status": status,
                                 "started_at": updated, "updated_at": updated, "ended_at": None,
                                 "source": "appserver_status" if confirmed else "recent_activity",
                                 "error_message": ""})
        tasks = inferred + tasks
        for task in tasks:
            age = stamp - task["updated_at"]
            if task["status"] == "active" and task["source"] not in {"recent_activity", "appserver_status"}:
                task["display_status"] = "stalled" if age > STALL_AFTER else "unconfirmed" if age > UNCONFIRMED_AFTER else "active"
            else:
                task["display_status"] = task["status"]
            task_path = normalize_path(task["workspace"])
            matches = [window for window in windows if window["account_id"] == task["account_id"]
                       and task_path and any(task_path == normalize_path(root) or
                       task_path.startswith(normalize_path(root) + os.sep)
                       for root in json.loads(window["roots_json"]))]
            task["window_id"] = matches[0]["id"] if len(matches) == 1 else None
            task["window_match"] = "unique_workspace" if len(matches) == 1 else "ambiguous" if len(matches) > 1 else "unmatched"
            task["signal"] = ({"hook": "hook", "appserver": "tour Codex", "appserver_status": "état Codex",
                               "recent_activity": "activité locale estimée", "demo": "démo"}).get(task["source"], task["source"])
            task["signal_at"] = task["updated_at"]
        diagnostics = {}
        for account in self.accounts:
            account_id = account["id"]
            account_metrics = metrics.get(account_id) or {}
            account_windows = [w for w in windows if w["account_id"] == account_id]
            diagnostics[account_id] = {
                "collector_at": account_metrics.get("last_success_at"),
                "hook_at": last_hooks.get(account_id),
                "window_at": max((w["last_seen"] for w in account_windows), default=None),
                "collector_stale": not account_metrics.get("last_success_at") or
                                   stamp - account_metrics["last_success_at"] > 90,
                "ambiguous_chats": sum(t["window_match"] == "ambiguous" and
                                       t["display_status"] in {"active", "approval", "waiting", "stalled"}
                                       for t in tasks if t["account_id"] == account_id),
            }
        return {"now": stamp, "accounts": [{"id": a["id"], "name": a["name"], "metrics": metrics.get(a["id"]),
                                               "windows": [w for w in windows if w["account_id"] == a["id"]],
                                               "tasks": [t for t in tasks if t["account_id"] == a["id"]][:30],
                                               "diagnostics": diagnostics[a["id"]],
                                               "quota_projection": projections[a["id"]],
                                               "quota_outlook": outlooks[a["id"]]} for a in self.accounts],
                "events": events, "window_ttl": WINDOW_TTL,
                "project_usage": projects,
                "preferences": self.preferences()}


def seed_demo(store: Store):
    projects = [
        ("compte-1", "Projet Atlas", "C:/projets/atlas", "active", "Refonte de la page d'accueil"),
        ("compte-1", "API clients", "C:/projets/api-clients", "completed", "Tests de l'API terminés"),
        ("compte-1", "Application mobile", "C:/projets/mobile", "approval", "Valider la modification des permissions"),
        ("compte-2", "Site vitrine", "C:/projets/site-vitrine", "active", "Corriger le parcours de paiement"),
        ("compte-2", "Outil interne", "C:/projets/outil-interne", "failed", "Échec du démarrage des tests"),
        ("compte-2", "Documentation", "C:/projets/docs", "completed", "Guide de déploiement rédigé"),
    ]
    stamp = now()
    with store.lock:
        for index, (account, label, workspace, status, title) in enumerate(projects):
            window_id = f"demo-window-{index+1}"
            task_id = f"demo-task-{index+1}"
            started = stamp - [470, 2100, 180, 980, 650, 3600][index]
            ended = stamp - 90 if status in {"completed", "failed"} else None
            store.db.execute("INSERT OR REPLACE INTO windows(id,account_id,label,workspace,roots_json,last_seen) VALUES (?,?,?,?,?,?)",
                             (window_id, account, label, workspace, json.dumps([workspace]), stamp))
            store.db.execute("INSERT OR REPLACE INTO tasks(id,account_id,session_id,turn_id,title,workspace,status,started_at,updated_at,ended_at,source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (task_id, account, f"demo-thread-{index+1}", f"demo-turn-{index+1}", title,
                              workspace, status, started, stamp, ended, "demo"))
            store.db.execute("INSERT INTO events(account_id,task_id,type,at,details) VALUES (?,?,?,?,?)",
                             (account, task_id, status, stamp - index * 180, "{}"))
        store.db.commit()
    day = datetime.now().date()
    usage_1 = [{"startDate": (day - timedelta(days=offset)).isoformat(), "tokens": tokens}
               for offset, tokens in enumerate((72400, 68100, 76500, 53200, 70400, 61200, 80500, 42100))]
    usage_2 = [{"startDate": (day - timedelta(days=offset)).isoformat(), "tokens": tokens}
               for offset, tokens in enumerate((31800, 36200, 29400, 41500, 33700, 24800, 38900, 30500))]
    store.set_metrics("compte-1", {"identity": {"account": {"type": "chatgpt", "email": "compte1@example.com", "planType": "pro"}},
                                  "limits": {"rateLimits": {"primary": {"usedPercent": 62, "windowDurationMins": 300, "resetsAt": stamp + 7200}}},
                                  "usage": {"summary": {"lifetimeTokens": 1820000}, "dailyUsageBuckets": usage_1},
                                  "threads": {"data": [
                                      {"id": "demo-thread-1", "name": "Refonte de la page d'accueil", "updatedAt": stamp - 180,
                                       "goal": {"status": "active", "objective": "Refondre la page d'accueil"}},
                                      {"id": "demo-thread-2", "name": "Tests de l'API clients", "updatedAt": stamp - 900,
                                       "goal": {"status": "active", "objective": "Étendre les tests de l'API"}},
                                      {"id": "demo-thread-3", "name": "Permissions de l'application mobile", "updatedAt": stamp - 1600,
                                       "goal": {"status": "paused", "objective": "Revoir les permissions mobiles"}}]}})
    store.set_metrics("compte-2", {"identity": {"account": {"type": "chatgpt", "email": "compte2@example.com", "planType": "plus"}},
                                  "limits": {"rateLimits": {"primary": {"usedPercent": 29, "windowDurationMins": 300, "resetsAt": stamp + 9900}}},
                                  "usage": {"summary": {"lifetimeTokens": 912000}, "dailyUsageBuckets": usage_2},
                                  "threads": {"data": [
                                      {"id": "demo-thread-4", "name": "Parcours de paiement", "updatedAt": stamp - 120,
                                       "goal": None},
                                      {"id": "demo-thread-5", "name": "Tests de l'outil interne", "updatedAt": stamp - 700,
                                       "goal": {"status": "blocked", "objective": "Fiabiliser les tests"}},
                                      {"id": "demo-thread-6", "name": "Guide de déploiement", "updatedAt": stamp - 1900,
                                       "goal": {"status": "complete", "objective": "Rédiger le guide"}}]}})
    with store.lock:
        store.db.executemany("INSERT INTO quota_samples VALUES (?,?,?,?,?)", [
            ("compte-1", "primary", stamp + 7200, stamp - 1200, 32),
            ("compte-2", "primary", stamp + 9900, stamp - 1200, 28),
        ])
        for index, (account_id, label, workspace, _, _) in enumerate(projects):
            key = normalize_path(workspace)
            store.db.execute("INSERT OR IGNORE INTO project_usage_state VALUES (?,?,?)",
                             (account_id, stamp - 7 * 86400, stamp))
            store.db.execute("INSERT INTO project_thread_counters VALUES (?,?,?,?,?,?,?)",
                             (account_id, f"demo-thread-{index + 1}", 90000, key, workspace,
                              stamp - 7 * 86400, stamp))
            for offset in range(7):
                tokens = [36000, 17000, 21000, 23000, 14000, 8500][index] * (7 - offset) // 7
                store.db.execute("INSERT INTO project_usage_daily VALUES (?,?,?,?,?)",
                                 (account_id, (day - timedelta(days=offset)).isoformat(), key, workspace, tokens))
        store.db.commit()


def keep_demo_windows_alive(store: Store):
    while True:
        time.sleep(15)
        with store.lock:
            stamp = now()
            store.db.execute("UPDATE windows SET last_seen=? WHERE id LIKE 'demo-window-%'", (stamp,))
            store.db.execute("""UPDATE tasks SET updated_at=? WHERE id LIKE 'demo-task-%'
                AND status IN ('active','approval','waiting')""", (stamp,))
            store.db.commit()
        store.announce()


def poll_accounts(store: Store, interval: int):
    while True:
        for account in store.accounts:
            try:
                data = read_account(account["codex_home"],
                                    tracked_thread_ids=store.tracked_goal_threads(account["id"]))
                data["collector_error"] = None
            except Exception as exc:
                data = {"collector_error": compact(exc, 250)}
            store.set_metrics(account["id"], data)
            try:
                store.observe_project_usage(account["id"], read_thread_counters(account["codex_home"]))
            except (OSError, sqlite3.Error, ValueError):
                pass  # The last valid project sample remains visible with its timestamp.
            store.reconcile_turns(account["id"], data.get("recent_turns") or [])
        time.sleep(interval)


def flush_answer_notifications(store: Store):
    while True:
        store.flush_action_alerts()
        store.flush_answer_alerts()
        store.flush_goal_alerts()
        store.flush_metric_alerts()
        time.sleep(3)


def poll_recent_turns_once(store: Store, last_checked: dict):
    for account in store.accounts:
        local_threads = _recent_local_threads(Path(account["codex_home"]))
        known = {thread["id"]: thread for thread in store.recent_thread_metadata(account["id"])
                 if thread.get("id")}
        updates = {thread["id"]: thread["updated_at"] for thread in local_threads}
        changed = [{**thread, **{key: value for key, value in known.get(thread["id"], {}).items()
                                if key in {"name", "cwd"} and value}}
                   for thread in local_threads if thread["updated_at"] >
                   last_checked.get((account["id"], thread["id"]), 0)]
        if not changed:
            continue
        try:
            turns = read_recent_turns(account["codex_home"], changed[:12])
            store.reconcile_turns(account["id"], turns)
        except Exception:
            continue
        terminal = {turn.get("thread_id") for turn in turns
                    if turn.get("status") in {"completed", "failed"}
                    or (turn.get("status") == "interrupted" and turn.get("completed_at"))}
        for thread in changed[:12]:
            if thread["id"] in terminal:
                last_checked[(account["id"], thread["id"])] = updates[thread["id"]]


def poll_recent_turns(store: Store, interval: int = 5):
    """Follow local activity changes so reply alerts do not wait for quota polls."""
    last_checked = {}
    while True:
        poll_recent_turns_once(store, last_checked)
        time.sleep(interval)


def sync_workspace_presence(store: Store):
    active = workspace_active_agents(store.snapshot())
    mailbox = Mailbox(WORKSPACE_MAILBOX_DB)
    try:
        active.extend(mailbox.managed_agents())
        mailbox.set_presence(active)
        return {**mailbox.overview(), "tasks": mailbox.work_tasks(),
                "accounts": [{"id": account["id"], "name": account["name"]}
                             for account in store.accounts]}
    finally:
        mailbox.close()


def merge_federated_overview(overview: dict, config: dict | None,
                             host_peer_online: bool = False) -> dict:
    if not config:
        return overview
    mailbox = Mailbox(WORKSPACE_MAILBOX_DB)
    try:
        queue_count = mailbox.db.execute("SELECT COUNT(*) FROM federation_outbox WHERE channel_id=? "
                                         "AND sent_at IS NULL", (config["channel_id"],)).fetchone()[0]
        queue_error = mailbox.db.execute("SELECT error FROM federation_outbox WHERE channel_id=? "
                                         "AND sent_at IS NULL AND error<>'' ORDER BY created_at DESC LIMIT 1",
                                         (config["channel_id"],)).fetchone()
        pending_outbox = [dict(row) for row in mailbox.db.execute("""SELECT client_key,
            sender_agent_id,content,created_at,error FROM federation_outbox
            WHERE channel_id=? AND sent_at IS NULL ORDER BY created_at LIMIT 20""",
            (config["channel_id"],))]
    finally:
        mailbox.close()
    overview["federation"] = {"role": config["role"], "channel_id": config["channel_id"],
                              "peer_id": config["peer_id"],
                              "connected": host_peer_online if config["role"] == "host" else False}
    overview["federation"]["queue_count"] = queue_count
    overview["federation"]["queue_error"] = queue_error[0] if queue_error else ""
    overview["federation"]["pending_outbox"] = pending_outbox
    if config["role"] != "client":
        remote_prefix = config["peer_id"] + ":"
        for key in ("agents", "directory", "graph_agents"):
            overview[key] = [{**agent, "remote": True, "workspace_id": config["peer_id"]}
                             if agent["id"].startswith(remote_prefix) else agent
                             for agent in overview[key]]
        return overview
    try:
        remote = PeerClient(config).overview()
    except (OSError, ValueError, TimeoutError) as exc:
        overview["federation"]["error"] = compact(exc, 150)
        return overview
    overview["federation"]["connected"] = True
    channel_id = config["channel_id"]
    overview["channels"] = [item for item in overview["channels"] if item["id"] != channel_id] + [remote["channel"]]
    for key in ("agents", "directory", "graph_agents"):
        local = {item["id"]: item for item in overview[key]}
        local.update({item["id"]: item for item in remote[key] if item.get("remote")})
        overview[key] = list(local.values())
    for identity, summary in remote["agent_summaries"].items():
        if identity in overview["agent_summaries"]:
            current = overview["agent_summaries"][identity]
            overview["agent_summaries"][identity] = {key: current.get(key, 0) + summary.get(key, 0)
                                                     for key in ("sent", "received", "pending")}
        else:
            overview["agent_summaries"][identity] = summary
    overview["connections"] = [item for item in overview["connections"]
                               if item["channel_id"] != channel_id] + remote["connections"]
    overview["graph_activity"] = [item for item in overview["graph_activity"]
                                  if item["channel_id"] != channel_id] + remote["graph_activity"]
    return overview


def poll_workspace_presence(store: Store, interval: int = 8):
    while True:
        try:
            sync_workspace_presence(store)
            mailbox = Mailbox(WORKSPACE_MAILBOX_DB)
            try:
                reconcile_hook_receipts(mailbox, store.accounts)
            finally:
                mailbox.close()
        except (OSError, sqlite3.Error, ValueError, KeyError, json.JSONDecodeError):
            pass
        time.sleep(interval)


class ExclusiveServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexManager/0.1"

    def log_message(self, format, *args):
        if not self.path.startswith(("/api/stream", "/api/health")):
            super().log_message(format, *args)

    def send_bytes(self, code, body: bytes, content_type="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, value):
        self.send_bytes(code, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def local_request(self):
        host = self.headers.get("Host", "")
        return host in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def catalog_account(self, account_id):
        account = next((item for item in self.server.store.accounts if item["id"] == account_id), None)
        if account is None:
            raise ValueError("Compte inconnu")
        return account

    def demo_catalog(self, account_id):
        number = 1 if account_id == "compte-1" else 2
        total = 24 if number == 1 else 17
        alpha = 12 if number == 1 else 9
        beta = 7 if number == 1 else 5
        return {"total": total, "offset": 0, "limit": 50,
                "by_source": {"vscode": total},
                "by_workspace": [{"workspace": "Projet Alpha", "count": alpha},
                                 {"workspace": "Projet Beta", "count": beta},
                                 {"workspace": "Autres projets", "count": total - alpha - beta}],
                "chats": [{"id": f"demo-{number}-{index}", "title": title,
                           "workspace": "Projet Alpha" if index < alpha else "Projet Beta" if index < alpha + beta else "Autres projets",
                           "source": "vscode", "updated_at": now() - index * 3600,
                           "tokens": 12000 * (index + 1), "pinned": index == 0,
                           "activity_status": "active" if index == 0 else "completed",
                           "goal_status": "active" if index == 0 else None}
                          for index, title in enumerate(["Conception de l'interface", "Tests API",
                                                          "Migration des données", "Documentation"] +
                                                         [f"Conversation démo {index + 1}" for index in range(4, total)])]}

    def chat_active_ids(self, account_id):
        state = next((item for item in self.server.store.snapshot()["accounts"]
                      if item["id"] == account_id), {})
        return {item["session_id"] for item in state.get("tasks", [])
                if item.get("session_id") and item.get("display_status") in
                {"active", "approval", "waiting", "stalled"}}

    def chat_data(self, account_id, limit=50, offset=0, search="", **filters):
        account = self.catalog_account(account_id)
        if self.server.demo:
            data = self.demo_catalog(account_id)
            filtered = [item for item in data["chats"]
                        if search.casefold() in item["title"].casefold()
                        and not filters.get("archived")
                        and (filters.get("min_tokens") is None or item["tokens"] >= filters["min_tokens"])
                        and (filters.get("max_tokens") is None or item["tokens"] <= filters["max_tokens"])
                        and (filters.get("date_from") is None or item["updated_at"] >= filters["date_from"])
                        and (filters.get("date_to") is None or item["updated_at"] < filters["date_to"])]
            data["total"] = len(filtered)
            data["offset"] = offset
            data["limit"] = limit
            data["chats"] = filtered[offset:offset + limit]
            return data
        data = chat_catalog(Path(account["codex_home"]), limit, offset, search, **filters)
        snapshot = self.server.store.snapshot()
        state = next((item for item in snapshot["accounts"] if item["id"] == account_id), {})
        tasks = {}
        for task in state.get("tasks", []):
            tid = task.get("session_id")
            if tid and (tid not in tasks or task.get("updated_at", 0) > tasks[tid].get("updated_at", 0)):
                tasks[tid] = task
        thread_data = ((state.get("metrics") or {}).get("threads") or {}).get("data") or []
        goals = {thread["id"]: (thread.get("goal") or {}).get("status")
                 for thread in thread_data
                 if thread.get("id") and "goal" in thread}
        for chat in data["chats"]:
            task = tasks.get(chat["id"], {})
            chat["activity_status"] = task.get("display_status")
            chat["goal_status"] = goals.get(chat["id"])
        return data

    def do_GET(self):
        if not self.local_request():
            return self.send_json(403, {"error": "Hôte interdit"})
        path = urlparse(self.path).path
        if path == "/api/health":
            return self.send_json(200, {"ok": True, "demo": self.server.demo,
                                        "telegram_goal_alerts": bool(getattr(self.server.store.notifier,
                                                                             "telegram_enabled", False)),
                                        "telegram_answer_alerts": bool(getattr(self.server.store.notifier,
                                                                               "telegram_enabled", False)),
                                        "telegram_action_alerts": bool(getattr(self.server.store.notifier,
                                                                               "telegram_enabled", False)),
                                        "telegram_metric_alerts": bool(getattr(self.server.store.notifier,
                                                                               "telegram_enabled", False))})
        if path == "/api/workspace-mcp":
            try:
                if self.server.demo:
                    overview = demo_workspace_overview(self.server.demo_workspace_messages,
                                                       self.server.store.snapshot())
                    overview["tasks"] = demo_workspace_tasks(self.server.demo_workspace_messages)
                    overview["accounts"] = [{"id": account["id"], "name": account["name"]}
                                            for account in self.server.store.accounts]
                else:
                    peer_server = getattr(self.server, "peer_server", None)
                    overview = merge_federated_overview(sync_workspace_presence(self.server.store),
                        self.server.federation_config,
                        bool(peer_server and time.time() - peer_server.last_contact < 35))
                    if self.server.federation_error:
                        overview["federation"] = {"connected": False,
                                                  "error": self.server.federation_error}
                return self.send_json(200, {**overview, "demo": self.server.demo})
            except (OSError, sqlite3.Error, ValueError) as exc:
                return self.send_json(503, {"error": compact(exc)})
        if path == "/api/workspace-mcp/task":
            try:
                task_id = valid_address(parse_qs(urlparse(self.path).query).get("task_id", [""])[0])
                if self.server.demo:
                    task = next((item for item in demo_workspace_tasks(self.server.demo_workspace_messages)
                                 if item["id"] == task_id), None)
                    if not task:
                        raise ValueError("Tâche introuvable")
                    return self.send_json(200, {"task": {**task, "events": [{"id": task["revision"],
                        "actor_agent_id": task["assignee_agent_id"], "kind": "updated",
                        "status": task["status"], "note": task["latest_note"] or "",
                        "created_at": task["updated_at"]}]}})
                mailbox = Mailbox(WORKSPACE_MAILBOX_DB)
                try:
                    return self.send_json(200, {"task": mailbox.work_task(task_id)})
                finally:
                    mailbox.close()
            except ValueError as exc:
                return self.send_json(400, {"error": compact(exc)})
            except (OSError, sqlite3.Error) as exc:
                return self.send_json(503, {"error": compact(exc)})
        if path == "/api/workspace-mcp/channel":
            query = parse_qs(urlparse(self.path).query)
            try:
                channel_id = valid_address(query.get("channel_id", [""])[0])
                before_id = int(query.get("before_id", ["0"])[0])
                limit = int(query.get("limit", ["50"])[0])
                sender = query.get("sender", [""])[0]
                recipient = query.get("recipient", [""])[0]
                pair = (valid_address(sender), valid_address(recipient)) if sender and recipient else None
                if self.server.demo:
                    if before_id < 0 or not 1 <= limit <= 100:
                        raise ValueError("Pagination invalide")
                    messages = [message for message in reversed(self.server.demo_workspace_messages)
                                if message["channel_id"] == channel_id
                                and (not before_id or message["id"] < before_id)]
                    if pair:
                        messages = [item for item in messages if item["sender_agent_id"] in pair]
                    result = {"messages": list(reversed(messages[:limit])), "has_more": len(messages) > limit}
                else:
                    federation = self.server.federation_config
                    if federation and federation["role"] == "client" and channel_id == federation["channel_id"]:
                        result = PeerClient(federation).messages(before_id, limit, pair=pair)
                    else:
                        mailbox = Mailbox(WORKSPACE_MAILBOX_DB)
                        try:
                            result = mailbox.messages(channel_id, before_id, limit, pair=pair)
                        finally:
                            mailbox.close()
                return self.send_json(200, result)
            except (OSError, sqlite3.Error, ValueError) as exc:
                return self.send_json(400, {"error": compact(exc)})
        if path == "/api/workspace-mcp/diagnostics":
            if self.server.demo:
                return self.send_json(400, {"error": "Diagnostic indisponible dans la démonstration"})
            agent_id = parse_qs(urlparse(self.path).query).get("agent_id", [""])[0]
            config = self.server.federation_config
            if config and config["role"] == "client" and agent_id.startswith(config["peer_id"] + ":"):
                try:
                    return self.send_json(200, PeerClient(config).diagnostics(agent_id))
                except (OSError, ValueError) as exc:
                    return self.send_json(503, {"error": compact(exc)})
            mailbox = Mailbox(WORKSPACE_MAILBOX_DB)
            try:
                if config and config["role"] == "host" and agent_id.startswith(config["peer_id"] + ":"):
                    return self.send_json(200, mailbox.remote_diagnostic(agent_id))
                return self.send_json(200, diagnose_workspace_agent(self.server.store, agent_id, mailbox))
            except (OSError, sqlite3.Error, ValueError) as exc:
                return self.send_json(400, {"error": compact(exc)})
            finally:
                mailbox.close()
        if path == "/api/workspace-mcp/agent":
            agent_id = parse_qs(urlparse(self.path).query).get("agent_id", [""])[0]
            try:
                agent_id = valid_address(agent_id)
                if self.server.demo:
                    return self.send_json(200, {"messages": []})
                mailbox = Mailbox(WORKSPACE_MAILBOX_DB)
                try:
                    messages = mailbox.agent_history(agent_id)
                    config = self.server.federation_config
                    if config and config["role"] == "client":
                        members = set(mailbox.shared_members(config["channel_id"]))
                        if agent_id in members or agent_id.startswith(config["peer_id"] + ":"):
                            try:
                                messages.extend(PeerClient(config).agent_history(agent_id))
                            except (OSError, ValueError):
                                pass
                    return self.send_json(200, {"messages": sorted(messages,
                        key=lambda item: item["created_at"], reverse=True)[:16]})
                finally:
                    mailbox.close()
            except (OSError, sqlite3.Error, ValueError) as exc:
                return self.send_json(400, {"error": compact(exc)})
        if path == "/api/snapshot":
            return self.send_json(200, self.server.store.snapshot())
        if path == "/api/preferences":
            return self.send_json(200, self.server.store.preferences())
        if path == "/api/environment-catalog":
            try:
                accounts = []
                for account in self.server.store.accounts:
                    if self.server.demo:
                        details = {"codex_version": "codex-cli 0.156.0 · démo",
                                   "plugins": [{"name": "Figma", "version": "13.0.0", "source": "Catalogue"}],
                                   "skills": [{"name": "openai-docs", "version": None, "source": "Compte", "enabled": True}],
                                   "mcp": [{"name": "openaiDeveloperDocs", "version": None,
                                            "transport": "HTTP", "enabled": True}]}
                        chats = self.demo_catalog(account["id"])
                    else:
                        details = inventory(Path(account["codex_home"]))
                        chats = chat_catalog(Path(account["codex_home"]), limit=1)
                    accounts.append({"id": account["id"], "name": account["name"],
                                     "inventory": details, "chats": {key: chats[key] for key in ("total", "by_source", "by_workspace")}})
                return self.send_json(200, {"accounts": accounts, "demo": self.server.demo})
            except (OSError, sqlite3.Error, ValueError) as exc:
                return self.send_json(503, {"error": compact(exc)})
        if path == "/api/chats":
            query = parse_qs(urlparse(self.path).query)
            try:
                account_id = query.get("account_id", [""])[0]
                def numeric(name):
                    raw = query.get(name, [""])[0]
                    if not raw:
                        return None
                    if not raw.isdecimal():
                        raise ValueError("Filtre numérique invalide")
                    return int(raw)

                def day(name, end=False):
                    raw = query.get(name, [""])[0]
                    if not raw:
                        return None
                    moment = datetime.strptime(raw, "%Y-%m-%d")
                    return int((moment + timedelta(days=1 if end else 0)).timestamp())

                return self.send_json(200, self.chat_data(account_id,
                    int(query.get("limit", ["50"])[0]), int(query.get("offset", ["0"])[0]),
                    query.get("search", [""])[0], min_tokens=numeric("min_tokens"),
                    max_tokens=numeric("max_tokens"), date_from=day("date_from"),
                    date_to=day("date_to", True), archived=query.get("view", [""])[0] == "archived"))
            except (OSError, sqlite3.Error, ValueError) as exc:
                return self.send_json(400, {"error": compact(exc)})
        if path == "/api/chats/duplicates":
            try:
                account_id = parse_qs(urlparse(self.path).query).get("account_id", [""])[0]
                account = self.catalog_account(account_id)
                if self.server.demo:
                    return self.send_json(200, {"groups": [], "archive_count": 0, "skipped_groups": 0})
                return self.send_json(200, duplicate_plan(Path(account["codex_home"]),
                                                           self.chat_active_ids(account_id)))
            except (OSError, sqlite3.Error, ValueError) as exc:
                return self.send_json(400, {"error": compact(exc)})
        if path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            version = -1
            try:
                while True:
                    with self.server.store.changed:
                        if self.server.store.version == version:
                            self.server.store.changed.wait(timeout=12)
                        version = self.server.store.version
                    message = "event: update\ndata: {}\n\n" if version >= 0 else ": connected\n\n"
                    self.wfile.write(message.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
        files = {"/": ("index.html", "text/html; charset=utf-8"),
                 "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                 "/workspace.js": ("workspace.js", "text/javascript; charset=utf-8"),
                 "/workspace-graph.js": ("workspace-graph.js", "text/javascript; charset=utf-8"),
                 "/vendor/cytoscape.min.js": ("vendor/cytoscape.min.js", "text/javascript; charset=utf-8"),
                 "/catalog.js": ("catalog.js", "text/javascript; charset=utf-8"),
                 "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                 "/telegram.svg": ("telegram.svg", "image/svg+xml"),
                 "/discord.svg": ("discord.svg", "image/svg+xml"),
                 "/whatsapp.svg": ("whatsapp.svg", "image/svg+xml"),
                 "/codex-manager-mark.svg": ("codex-manager-mark.svg", "image/svg+xml")}
        if path in files:
            filename, content_type = files[path]
            return self.send_bytes(200, (STATIC / filename).read_bytes(), content_type)
        self.send_json(404, {"error": "Introuvable"})

    def do_POST(self):
        if not self.local_request():
            return self.send_json(403, {"error": "Hôte interdit"})
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
            return self.send_json(403, {"error": "Origine interdite"})
        path = urlparse(self.path).path
        if path not in {"/api/windows/heartbeat", "/api/events", "/api/preferences", "/api/chats/transfer",
                        "/api/chats/archive", "/api/chats/unarchive", "/api/chats/deduplicate",
                        "/api/workspace-mcp/spawn", "/api/workspace-mcp/send", "/api/workspace-mcp/stop",
                        "/api/workspace-mcp/join", "/api/workspace-mcp/task"}:
            return self.send_json(404, {"error": "Introuvable"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send_json(415, {"error": "JSON requis"})
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 65536:
            return self.send_json(413, {"error": "Taille invalide"})
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Objet JSON requis")
            if path == "/api/preferences":
                self.server.store.save_preferences(payload)
            elif path.startswith("/api/workspace-mcp/"):
                if self.server.demo:
                    raise ValueError("Agents CLI indisponibles dans la démonstration")
                if path == "/api/workspace-mcp/task":
                    mailbox = Mailbox(WORKSPACE_MAILBOX_DB)
                    try:
                        action = payload.get("action")
                        if action == "create":
                            task = mailbox.create_work_task(payload.get("title"),
                                payload.get("description", ""), payload.get("channel_id", GENERAL),
                                payload.get("assignee_agent_id") or None)
                        elif action == "assign":
                            task = mailbox.assign_work_task(payload.get("task_id"),
                                payload.get("assignee_agent_id") or None)
                        elif action == "update":
                            task = mailbox.update_work_task(payload.get("task_id"),
                                payload.get("status"), payload.get("note", ""))
                        else:
                            raise ValueError("Action de tâche inconnue")
                    finally:
                        mailbox.close()
                    return self.send_json(200, {"task": task})
                if path == "/api/workspace-mcp/join":
                    channel_id = valid_address(payload.get("channel_id", ""))
                    agent_id = valid_address(payload.get("agent_id", ""))
                    if not self.server.federation_config or channel_id != self.server.federation_config["channel_id"]:
                        raise ValueError("Passerelle partagée indisponible")
                    if agent_id not in {item["id"] for item in workspace_active_agents(self.server.store.snapshot())}:
                        raise ValueError("Agent local indisponible")
                    mailbox = Mailbox(WORKSPACE_MAILBOX_DB)
                    try:
                        if channel_id not in {item["id"] for item in mailbox.channels()
                                              if item["kind"] == "shared"}:
                            raise ValueError("Channel partagé inconnu")
                        mailbox.join_channel(agent_id, channel_id)
                    finally:
                        mailbox.close()
                    return self.send_json(200, {"ok": True})
                if path == "/api/workspace-mcp/spawn":
                    if str(payload.get("channel_id", "")).startswith("shared:") and (
                            not self.server.federation_config or self.server.federation_config["role"] == "client"):
                        raise ValueError("Lancement CLI indisponible dans ce channel partagé")
                    launch = self.server.cli_manager.spawn(payload.get("account_id", ""),
                                                           payload.get("channel_id", ""), payload.get("name", ""))
                    return self.send_json(202, {"launch": launch})
                if path == "/api/workspace-mcp/send":
                    message = self.server.cli_manager.send(payload.get("launch_id", ""),
                                                           payload.get("channel_id", ""), payload.get("content", ""))
                    return self.send_json(200, {"message": message})
                self.server.cli_manager.stop(payload.get("launch_id", ""))
                return self.send_json(200, {"ok": True})
            elif path == "/api/chats/transfer":
                if self.server.demo:
                    raise ValueError("Transfert indisponible dans la démonstration")
                source = self.catalog_account(payload.get("source"))
                target = self.catalog_account(payload.get("target"))
                snapshot = self.server.store.snapshot()
                source_state = next((item for item in snapshot["accounts"] if item["id"] == source["id"]), {})
                active = {item["session_id"] for item in source_state.get("tasks", [])
                          if item.get("display_status") in {"active", "approval", "waiting", "stalled"}}
                results = transfer_chats(Path(source["codex_home"]), Path(target["codex_home"]),
                                         payload.get("thread_ids"), active)
                if any(item["status"] == "copied" for item in results):
                    try:
                        self.server.store.observe_project_usage(target["id"],
                            read_thread_counters(target["codex_home"]))
                    except (OSError, sqlite3.Error):
                        pass
                return self.send_json(200, {"results": results})
            elif path in {"/api/chats/archive", "/api/chats/unarchive", "/api/chats/deduplicate"}:
                if self.server.demo:
                    raise ValueError("Archivage indisponible dans la démonstration")
                account = self.catalog_account(payload.get("account_id"))
                home = Path(account["codex_home"])
                active = self.chat_active_ids(account["id"])
                if path == "/api/chats/deduplicate":
                    return self.send_json(200, deduplicate_chats(home, active))
                return self.send_json(200, {"results": change_archive(
                    home, payload.get("thread_ids"), path.endswith("/archive"), active)})
            elif path == "/api/windows/heartbeat":
                self.server.store.heartbeat(payload)
            else:
                self.server.store.event(payload)
            self.send_json(200, {"ok": True})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": compact(exc)})
        except (OSError, sqlite3.Error, RuntimeError, TimeoutError) as exc:
            self.send_json(503, {"error": compact(exc)})


def main():
    parser = argparse.ArgumentParser(description="Codex Manager : gestion locale des agents Codex")
    parser.add_argument("--demo", action="store_true", help="Affiche six fenêtres et des métriques fictives")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=str(ROOT / "data" / "monitor.sqlite"))
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--alerts-config", default=str(ROOT / "alerts.json"))
    parser.add_argument("--poll-seconds", type=int, default=30)
    args = parser.parse_args()
    config_file = Path(args.config)
    accounts = json.loads(config_file.read_text(encoding="utf-8"))["accounts"] if config_file.exists() else default_accounts()
    if args.demo:
        database = ":memory:"
    else:
        database = args.db
        Path(database).parent.mkdir(parents=True, exist_ok=True)
    notifier = None if args.demo else AlertDispatcher(Path(args.alerts_config))
    store = Store(database, accounts, notifier)
    if args.demo:
        seed_demo(store)
        threading.Thread(target=keep_demo_windows_alive, args=(store,), daemon=True).start()
    else:
        threading.Thread(target=poll_accounts, args=(store, args.poll_seconds), daemon=True).start()
        threading.Thread(target=poll_workspace_presence, args=(store,), daemon=True).start()
        if notifier.telegram_enabled:
            threading.Thread(target=poll_recent_turns, args=(store,), daemon=True).start()
            threading.Thread(target=flush_answer_notifications, args=(store,), daemon=True).start()
            command_bot = TelegramCommandBot(notifier.telegram_token, notifier.telegram_chat_id,
                                             store.snapshot, accounts)
            threading.Thread(target=command_bot.run, daemon=True).start()
    server = ExclusiveServer(("127.0.0.1", args.port), Handler)
    server.store = store
    server.demo = args.demo
    server.cli_manager = None if args.demo else ManagedCodex(accounts, WORKSPACE_MAILBOX_DB)
    server.federation_config = None
    server.federation_error = ""
    if not args.demo:
        try:
            server.federation_config = load_federation_config()
            if server.federation_config:
                mailbox = Mailbox(WORKSPACE_MAILBOX_DB)
                try:
                    prepare_channel(server.federation_config, mailbox)
                finally:
                    mailbox.close()
                if server.federation_config["role"] == "host":
                    peer_server = start_federation_host(server.federation_config, WORKSPACE_MAILBOX_DB)
                    threading.Thread(target=peer_server.serve_forever, daemon=True).start()
                    server.peer_server = peer_server
                else:
                    threading.Thread(target=poll_federation_client,
                                     args=(server.federation_config, WORKSPACE_MAILBOX_DB, 8, store), daemon=True).start()
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            server.federation_config = None
            server.federation_error = compact(exc, 150)
            print(f"Workspace fédéré indisponible : {compact(exc)}", flush=True)
    server.demo_workspace_messages = demo_workspace_messages() if args.demo else []
    print(f"Codex Manager : http://127.0.0.1:{args.port}/" + (" (démo)" if args.demo else ""), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if getattr(server, "peer_server", None):
            server.peer_server.server_close()


if __name__ == "__main__":
    main()
