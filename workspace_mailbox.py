"""SQLite mailbox: Codex chat sessions are agents; messages belong to channels.

The earlier account-level messages table is left untouched during migration.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent / "data" / "workspace-mailbox.sqlite"
ADDRESS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")
GENERAL = "general"
TASK_STATUSES = {"todo", "working", "review", "blocked", "done"}


def valid_address(value: str) -> str:
    if not isinstance(value, str) or not ADDRESS.fullmatch(value):
        raise ValueError("Identifiant invalide")
    return value


def agent_address(account_id: str, session_id: str) -> str:
    return valid_address(f"{valid_address(account_id)}:{valid_address(session_id)}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Mailbox:
    def __init__(self, path: str | Path = DEFAULT_DB):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS agents(
              id TEXT PRIMARY KEY, account_id TEXT NOT NULL, session_id TEXT NOT NULL,
              name TEXT NOT NULL, workspace TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'idle', last_seen TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS channels(
              id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('general','private','shared')),
              name TEXT NOT NULL, private_key TEXT UNIQUE, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS channel_members(
              channel_id TEXT NOT NULL REFERENCES channels(id),
              agent_id TEXT NOT NULL REFERENCES agents(id),
              joined_at TEXT NOT NULL, PRIMARY KEY(channel_id,agent_id));
            CREATE TABLE IF NOT EXISTS channel_messages(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              channel_id TEXT NOT NULL REFERENCES channels(id),
              sender_agent_id TEXT NOT NULL REFERENCES agents(id),
              content TEXT NOT NULL, created_at TEXT NOT NULL,
              reply_to_id INTEGER, read_at TEXT);
            CREATE INDEX IF NOT EXISTS idx_channel_messages ON channel_messages(channel_id,id);
            CREATE INDEX IF NOT EXISTS idx_channel_members_agent ON channel_members(agent_id,channel_id);
            CREATE TABLE IF NOT EXISTS managed_cli(
              launch_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
              session_id TEXT, channel_id TEXT NOT NULL,
              name TEXT NOT NULL, state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS message_deliveries(
              message_id INTEGER NOT NULL, agent_id TEXT NOT NULL,
              delivered_at TEXT, method TEXT,
              PRIMARY KEY(message_id,agent_id));
            CREATE TABLE IF NOT EXISTS hook_emissions(
              nonce TEXT PRIMARY KEY, agent_id TEXT NOT NULL, event TEXT NOT NULL,
              local_ids_json TEXT NOT NULL, remote_ids_json TEXT NOT NULL,
              prepared_at TEXT NOT NULL, verified_at TEXT, proof TEXT,
              remote_ack_at TEXT, scan_path TEXT, scan_offset INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS idx_message_deliveries_agent
              ON message_deliveries(agent_id,delivered_at,message_id);
            CREATE TABLE IF NOT EXISTS inbox_checks(
              agent_id TEXT PRIMARY KEY, last_checked_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS federation_outbox(
              client_key TEXT PRIMARY KEY,channel_id TEXT NOT NULL,sender_agent_id TEXT NOT NULL,
              content TEXT NOT NULL,target_agent_id TEXT,reply_to_id INTEGER,
              created_at TEXT NOT NULL,sent_at TEXT,remote_id INTEGER,error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS agent_checks(
              agent_id TEXT PRIMARY KEY,github_status TEXT NOT NULL,github_detail TEXT NOT NULL,
              repository TEXT,checked_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS hook_verifications(
              account_id TEXT NOT NULL,event TEXT NOT NULL,client TEXT NOT NULL,
              session_id TEXT NOT NULL,verified_at TEXT NOT NULL,
              PRIMARY KEY(account_id,event,client));
            CREATE TABLE IF NOT EXISTS remote_diagnostics(
              agent_id TEXT PRIMARY KEY,reported_at TEXT NOT NULL,payload_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS work_tasks(
              id TEXT PRIMARY KEY,channel_id TEXT NOT NULL,title TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'todo',
              assignee_agent_id TEXT,creator_agent_id TEXT,
              created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_work_tasks_channel ON work_tasks(channel_id,updated_at);
            CREATE TABLE IF NOT EXISTS work_task_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL,
              actor_agent_id TEXT,kind TEXT NOT NULL,previous_status TEXT,
              status TEXT,note TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_work_task_events_task ON work_task_events(task_id,id);
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(agents)")}
        channel_sql = self.db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='channels'").fetchone()[0]
        if "'shared'" not in channel_sql:
            self.db.execute("BEGIN IMMEDIATE")
            with self.db:
                self.db.execute("""CREATE TABLE channels_new(id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('general','private','shared')),
                    name TEXT NOT NULL,private_key TEXT UNIQUE,created_at TEXT NOT NULL)""")
                self.db.execute("INSERT INTO channels_new SELECT * FROM channels")
                self.db.execute("DROP TABLE channels")
                self.db.execute("ALTER TABLE channels_new RENAME TO channels")
        if "present_until" not in columns:
            self.db.execute("ALTER TABLE agents ADD COLUMN present_until REAL NOT NULL DEFAULT 0")
        if "mcp_last_seen" not in columns:
            self.db.execute("ALTER TABLE agents ADD COLUMN mcp_last_seen TEXT")
        if "activity_at" not in columns:
            self.db.execute("ALTER TABLE agents ADD COLUMN activity_at REAL")
        message_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(channel_messages)")}
        if "target_agent_id" not in message_columns:
            self.db.execute("ALTER TABLE channel_messages ADD COLUMN target_agent_id TEXT")
        if "client_key" not in message_columns:
            self.db.execute("ALTER TABLE channel_messages ADD COLUMN client_key TEXT")
        if "global_id" not in message_columns:
            self.db.execute("ALTER TABLE channel_messages ADD COLUMN global_id TEXT")
        if "references_json" not in message_columns:
            self.db.execute("ALTER TABLE channel_messages ADD COLUMN references_json TEXT NOT NULL DEFAULT '[]'")
        delivery_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(message_deliveries)")}
        if "attempted_at" not in delivery_columns:
            self.db.execute("ALTER TABLE message_deliveries ADD COLUMN attempted_at TEXT")
            self.db.execute("ALTER TABLE message_deliveries ADD COLUMN proof TEXT")
            # Older hook receipts only prove that the script wrote to stdout.
            # They must be offered again until Codex records the context.
            self.db.execute("UPDATE message_deliveries SET attempted_at=delivered_at,"
                            "delivered_at=NULL,method=NULL WHERE method='hook'")
        emission_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(hook_emissions)")}
        if "scan_path" not in emission_columns:
            self.db.execute("ALTER TABLE hook_emissions ADD COLUMN scan_path TEXT")
            self.db.execute("ALTER TABLE hook_emissions ADD COLUMN scan_offset INTEGER NOT NULL DEFAULT 0")
        outbox_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(federation_outbox)")}
        if "references_json" not in outbox_columns:
            self.db.execute("ALTER TABLE federation_outbox ADD COLUMN references_json TEXT NOT NULL DEFAULT '[]'")
        check_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(agent_checks)")}
        if "github_checks_json" not in check_columns:
            self.db.execute("ALTER TABLE agent_checks ADD COLUMN github_checks_json TEXT NOT NULL DEFAULT '{}'")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_message_client_key "
                        "ON channel_messages(sender_agent_id,client_key) WHERE client_key IS NOT NULL")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_message_global_id "
                        "ON channel_messages(global_id) WHERE global_id IS NOT NULL")
        launch_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(managed_cli)")}
        if "last_processed_id" not in launch_columns:
            self.db.execute("ALTER TABLE managed_cli ADD COLUMN last_processed_id INTEGER NOT NULL DEFAULT 0")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO channels(id,kind,name,created_at) VALUES (?,?,?,?)",
                            (GENERAL, "general", "Général", utc_now()))

    def close(self):
        self.db.close()

    def _task_assignee(self, channel_id: str, assignee: str | None):
        if assignee is None:
            return
        assignee = valid_address(assignee)
        if assignee not in {item["id"] for item in self.active_agents()}:
            raise ValueError("Agent destinataire indisponible")
        if self.agent(assignee)["workspace"] == "Workspace distant":
            raise ValueError("Attribution distante indisponible avant synchronisation du Kanban")
        if channel_id != GENERAL:
            self._check_member(assignee, channel_id)

    def work_tasks(self, agent_id: str | None = None, channel_id: str | None = None,
                   mine_only: bool = False) -> list[dict]:
        if agent_id:
            agent_id = valid_address(agent_id)
        if channel_id:
            channel_id = valid_address(channel_id)
        if mine_only and not agent_id:
            raise ValueError("Agent requis")
        query = ("SELECT t.*, (SELECT MAX(e.id) FROM work_task_events e WHERE e.task_id=t.id) AS revision, "
                 "(SELECT e.note FROM work_task_events e WHERE e.task_id=t.id "
                 "AND e.kind='updated' AND e.note<>'' ORDER BY e.id DESC LIMIT 1) "
                 "AS latest_note FROM work_tasks t WHERE 1=1")
        params = []
        if agent_id:
            query += (" AND EXISTS (SELECT 1 FROM channel_members m WHERE m.channel_id=t.channel_id "
                      "AND m.agent_id=?)")
            params.append(agent_id)
        if channel_id:
            query += " AND t.channel_id=?"
            params.append(channel_id)
        if mine_only:
            query += " AND t.assignee_agent_id=?"
            params.append(agent_id)
        query += " ORDER BY t.updated_at DESC,t.id DESC LIMIT 500"
        return [dict(row) for row in self.db.execute(query, params)]

    def work_task(self, task_id: str, agent_id: str | None = None) -> dict:
        row = self.db.execute("SELECT * FROM work_tasks WHERE id=?", (valid_address(task_id),)).fetchone()
        if row is None:
            raise ValueError("Tâche introuvable")
        result = dict(row)
        if agent_id:
            self._check_member(valid_address(agent_id), result["channel_id"])
        result["events"] = [dict(item) for item in self.db.execute(
            "SELECT * FROM work_task_events WHERE task_id=? ORDER BY id DESC LIMIT 50", (task_id,))]
        return result

    def create_work_task(self, title: str, description: str = "", channel_id: str = GENERAL,
                         assignee: str | None = None, actor: str | None = None) -> dict:
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 180:
            raise ValueError("Titre de tâche invalide")
        if not isinstance(description, str) or len(description) > 4000:
            raise ValueError("Description trop longue")
        channel_id = valid_address(channel_id)
        channel = self.db.execute("SELECT kind FROM channels WHERE id=?", (channel_id,)).fetchone()
        if not channel:
            raise ValueError("Channel inconnu")
        if channel["kind"] == "shared":
            raise ValueError("Le Kanban du channel distant n'est pas encore synchronisé")
        if actor:
            actor = valid_address(actor)
            self._check_member(actor, channel_id)
        self._task_assignee(channel_id, assignee)
        task_id, stamp = "task-" + uuid.uuid4().hex, utc_now()
        with self.db:
            self.db.execute("INSERT INTO work_tasks VALUES (?,?,?,?,?,?,?,?,?)",
                (task_id, channel_id, title.strip(), description.strip(), "todo", assignee,
                 actor, stamp, stamp))
            self.db.execute("INSERT INTO work_task_events(task_id,actor_agent_id,kind,status,created_at) "
                            "VALUES (?,?,?,?,?)", (task_id, actor, "created", "todo", stamp))
        if assignee and assignee != actor:
            self._notify_work_assignment(task_id, title.strip(), assignee, actor)
        return self.work_task(task_id)

    def _notify_work_assignment(self, task_id: str, title: str,
                                assignee: str, actor: str | None):
        sender = actor or self.register_agent("workspace", "kanban", "Kanban Workspace")["id"]
        self.send(sender, f"Tâche Kanban attribuée : {title}\nID : {task_id}\n"
                  "Consulte list_tasks pour le contexte et update_task pour signaler l'avancement.",
                  recipient_agent=assignee)

    def assign_work_task(self, task_id: str, assignee: str | None,
                         actor: str | None = None, claim: bool = False) -> dict:
        task = self.work_task(task_id, actor)
        if claim:
            if not actor or assignee != actor or task["assignee_agent_id"] is not None:
                raise ValueError("Tâche déjà attribuée ou agent invalide")
        elif actor and actor not in {task["assignee_agent_id"], task["creator_agent_id"]}:
            raise ValueError("Seul le créateur ou le responsable peut réattribuer cette tâche")
        if assignee is not None:
            assignee = valid_address(assignee)
        self._task_assignee(task["channel_id"], assignee)
        stamp = utc_now()
        with self.db:
            if claim:
                changed = self.db.execute("UPDATE work_tasks SET assignee_agent_id=?,updated_at=? "
                    "WHERE id=? AND assignee_agent_id IS NULL", (assignee, stamp, task_id)).rowcount
                if not changed:
                    raise ValueError("Tâche déjà prise")
            else:
                self.db.execute("UPDATE work_tasks SET assignee_agent_id=?,updated_at=? WHERE id=?",
                                (assignee, stamp, task_id))
            self.db.execute("INSERT INTO work_task_events(task_id,actor_agent_id,kind,note,created_at) "
                            "VALUES (?,?,?,?,?)", (task_id, actor, "assigned", assignee or "", stamp))
        if assignee and assignee != actor and assignee != task["assignee_agent_id"]:
            self._notify_work_assignment(task_id, task["title"], assignee, actor)
        return self.work_task(task_id)

    def update_work_task(self, task_id: str, status: str | None = None,
                         note: str = "", actor: str | None = None) -> dict:
        task = self.work_task(task_id, actor)
        if actor and actor not in {task["assignee_agent_id"], task["creator_agent_id"]}:
            raise ValueError("Cette tâche ne vous est pas attribuée")
        if status is not None and status not in TASK_STATUSES:
            raise ValueError("Statut de tâche invalide")
        if not isinstance(note, str) or len(note) > 2000:
            raise ValueError("Note trop longue")
        if status is None and not note.strip():
            raise ValueError("Statut ou note requis")
        stamp = utc_now()
        with self.db:
            self.db.execute("UPDATE work_tasks SET status=?,updated_at=? WHERE id=?",
                            (status or task["status"], stamp, task_id))
            self.db.execute("INSERT INTO work_task_events "
                "(task_id,actor_agent_id,kind,previous_status,status,note,created_at) "
                "VALUES (?,?,?,?,?,?,?)", (task_id, actor, "updated", task["status"],
                                            status or task["status"], note.strip(), stamp))
        return self.work_task(task_id)

    def register_agent(self, account_id: str, session_id: str, name: str = "",
                       workspace: str = "", status: str = "idle",
                       activity_at: float | None = None) -> dict:
        identifier = agent_address(account_id, session_id)
        name = (name or f"Chat {session_id[:8]}").strip()[:160]
        workspace = (workspace or "")[:500]
        if status not in {"working", "attention", "error", "idle"}:
            status = "idle"
        stamp = utc_now()
        with self.db:
            self.db.execute("""INSERT INTO agents(id,account_id,session_id,name,workspace,status,last_seen,activity_at)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                name=CASE WHEN excluded.name LIKE 'Chat %' AND agents.name NOT LIKE 'Chat %'
                  THEN agents.name ELSE excluded.name END,
                workspace=CASE WHEN excluded.workspace='' THEN agents.workspace ELSE excluded.workspace END,
                status=excluded.status,last_seen=excluded.last_seen,
                activity_at=COALESCE(excluded.activity_at,agents.activity_at)""",
                (identifier, account_id, session_id, name, workspace, status, stamp, activity_at))
            self.db.execute("INSERT OR IGNORE INTO channel_members VALUES (?,?,?)",
                            (GENERAL, identifier, stamp))
        return self.agent(identifier)

    def register_remote_agent(self, workspace_id: str, local_agent_id: str,
                              channel_id: str, name: str = "", status: str = "idle") -> dict:
        identifier = valid_address(f"{valid_address(workspace_id)}:{valid_address(local_agent_id)}")
        if not self.db.execute("SELECT 1 FROM channels WHERE id=? AND kind='shared'",
                               (valid_address(channel_id),)).fetchone():
            raise ValueError("Channel partagé inconnu")
        account_id, session_id = identifier.rsplit(":", 1)
        stamp = utc_now()
        with self.db:
            self.db.execute("""INSERT INTO agents(id,account_id,session_id,name,workspace,status,last_seen,activity_at)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,status=excluded.status,last_seen=excluded.last_seen,
                activity_at=excluded.activity_at""",
                (identifier, account_id, session_id, (name or local_agent_id)[:160],
                 "Workspace distant", status if status in {"working","attention","error","idle"} else "idle",
                 stamp, time.time()))
            self.db.execute("INSERT OR IGNORE INTO channel_members VALUES (?,?,?)",
                            (channel_id, identifier, stamp))
            self.db.execute("UPDATE agents SET present_until=? WHERE id=?", (time.time() + 35, identifier))
        return self.agent(identifier)

    def create_shared_channel(self, channel_id: str, name: str):
        if not valid_address(channel_id).startswith("shared:"):
            raise ValueError("Identifiant de channel partagé invalide")
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError("Nom de channel invalide")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO channels(id,kind,name,created_at) VALUES (?,?,?,?)",
                            (channel_id, "shared", name.strip(), utc_now()))

    def shared_members(self, channel_id: str) -> list[str]:
        return [row[0] for row in self.db.execute("SELECT agent_id FROM channel_members WHERE channel_id=?",
                                                   (valid_address(channel_id),))]

    def queue_federated(self, sender: str, channel_id: str, content: str,
                        target_agent: str | None = None, reply_to_id: int | None = None,
                        client_key: str | None = None, references: list[str] | None = None) -> dict:
        self._check_member(sender, channel_id)
        if not self.db.execute("SELECT 1 FROM channels WHERE id=? AND kind='shared'", (channel_id,)).fetchone():
            raise ValueError("Channel partagé inconnu")
        if not isinstance(content, str) or not content.strip() or len(content) > 10000:
            raise ValueError("Le message doit contenir entre 1 et 10 000 caractères")
        if target_agent:
            valid_address(target_agent)
        if reply_to_id is not None and (type(reply_to_id) is not int or reply_to_id < 1):
            raise ValueError("Réponse invalide")
        references = self._references(references)
        key = valid_address(client_key) if client_key else uuid.uuid4().hex
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO federation_outbox
                (client_key,channel_id,sender_agent_id,content,target_agent_id,reply_to_id,created_at,references_json)
                VALUES (?,?,?,?,?,?,?,?)""",
                (key, channel_id, sender, content.strip(), target_agent, reply_to_id, utc_now(),
                 json.dumps(references, ensure_ascii=False)))
        item = dict(self.db.execute("SELECT * FROM federation_outbox WHERE client_key=?", (key,)).fetchone())
        if (item["sender_agent_id"] != sender or item["channel_id"] != channel_id or
            item["content"] != content.strip() or item["target_agent_id"] != target_agent or
            item["reply_to_id"] != reply_to_id or json.loads(item["references_json"]) != references):
            raise ValueError("Identifiant de message déjà utilisé pour un autre contenu")
        return item

    def queued_federated(self, channel_id: str, limit: int = 25) -> list[dict]:
        return [dict(row) for row in self.db.execute("""SELECT * FROM federation_outbox
            WHERE channel_id=? AND sent_at IS NULL ORDER BY created_at,client_key LIMIT ?""",
            (valid_address(channel_id), limit))]

    def mark_federated_sent(self, client_key: str, remote_id: int):
        with self.db:
            self.db.execute("UPDATE federation_outbox SET sent_at=?,remote_id=?,error='' WHERE client_key=?",
                            (utc_now(), remote_id, valid_address(client_key)))

    def mark_federated_error(self, client_key: str, error: str):
        with self.db:
            self.db.execute("UPDATE federation_outbox SET error=? WHERE client_key=?",
                            (str(error)[:180], valid_address(client_key)))

    def agent(self, identifier: str) -> dict:
        row = self.db.execute("SELECT * FROM agents WHERE id=?", (valid_address(identifier),)).fetchone()
        if row is None:
            raise ValueError("Agent inconnu")
        return dict(row)

    def save_agent_check(self, agent_id: str, result: dict):
        if result.get("status") not in {"ok", "incomplete", "error", "unknown"}:
            raise ValueError("Résultat de contrôle invalide")
        with self.db:
            self.db.execute("""INSERT INTO agent_checks
                (agent_id,github_status,github_detail,repository,checked_at,github_checks_json)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(agent_id) DO UPDATE SET github_status=excluded.github_status,
                github_detail=excluded.github_detail,repository=excluded.repository,
                checked_at=excluded.checked_at,github_checks_json=excluded.github_checks_json""",
                (valid_address(agent_id), result["status"], str(result.get("detail", ""))[:250],
                 str(result.get("repository", ""))[:200] or None, utc_now(),
                 json.dumps(result.get("checks", {}), ensure_ascii=False)))

    def agent_check(self, agent_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM agent_checks WHERE agent_id=?",
                              (valid_address(agent_id),)).fetchone()
        return {**dict(row), "checks": json.loads(row["github_checks_json"])} if row else None

    def record_hook_verification(self, account_id: str, event: str,
                                 client: str, session_id: str):
        if event not in {"UserPromptSubmit", "PostToolUse"} or client not in {"cli", "vscode"}:
            raise ValueError("Vérification de hook invalide")
        with self.db:
            self.db.execute("""INSERT INTO hook_verifications VALUES (?,?,?,?,?)
                ON CONFLICT(account_id,event,client) DO UPDATE SET
                session_id=excluded.session_id,verified_at=excluded.verified_at""",
                (valid_address(account_id), event, client, valid_address(session_id), utc_now()))

    def hook_verifications(self, account_id: str) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT event,client,session_id,verified_at FROM hook_verifications WHERE account_id=? "
            "ORDER BY client,event", (valid_address(account_id),))]

    def save_remote_diagnostics(self, agent_id: str, reported: dict):
        agent_id = valid_address(agent_id)
        if not isinstance(reported, dict):
            raise ValueError("Diagnostic distant invalide")
        clean = {"checked_at": str(reported.get("checked_at", ""))[:60]}
        for key in ("github", "github_mcp", "mcp", "hooks", "delivery"):
            item = reported.get(key)
            if not isinstance(item, dict) or item.get("status") not in {
                    "ok", "incomplete", "error", "unknown"}:
                raise ValueError("Statut de diagnostic distant invalide")
            clean[key] = {"status": item["status"],
                          "detail": str(item.get("detail", ""))[:180],
                          "at": str(item.get("at", ""))[:60] or None}
        with self.db:
            self.db.execute("""INSERT INTO remote_diagnostics VALUES (?,?,?)
                ON CONFLICT(agent_id) DO UPDATE SET reported_at=excluded.reported_at,
                payload_json=excluded.payload_json""",
                (agent_id, utc_now(), json.dumps(clean, ensure_ascii=False)))

    def remote_diagnostic(self, agent_id: str) -> dict:
        agent_id = valid_address(agent_id)
        if agent_id not in {item["id"] for item in self.active_agents()}:
            raise ValueError("Agent distant indisponible")
        row = self.db.execute("SELECT reported_at,payload_json FROM remote_diagnostics "
                              "WHERE agent_id=?", (agent_id,)).fetchone()
        if not row:
            unknown = {"status": "unknown", "detail": "Aucun contrôle rapporté par le pair", "at": None}
            return {"agent_id": agent_id, "origin": "workspace_distant",
                    "origin_workspace": agent_id.split(":", 1)[0], "reported_at": None,
                    "checked_at": None, **{key: unknown.copy() for key in
                    ("github", "github_mcp", "mcp", "hooks", "delivery")}}
        return {"agent_id": agent_id, "origin": "workspace_distant",
                "origin_workspace": agent_id.split(":", 1)[0],
                "reported_at": row["reported_at"], **json.loads(row["payload_json"])}

    def agents(self) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM agents ORDER BY CASE status WHEN 'working' THEN 0 "
            "WHEN 'attention' THEN 1 WHEN 'error' THEN 2 ELSE 3 END,last_seen DESC LIMIT 120")]

    def active_agents(self) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM agents WHERE present_until>? "
            "ORDER BY CASE status WHEN 'working' THEN 0 WHEN 'attention' THEN 1 ELSE 2 END,last_seen DESC",
            (time.time(),))]

    def touch_presence(self, agent_id: str, ttl: int = 18):
        with self.db:
            self.db.execute("UPDATE agents SET present_until=?,mcp_last_seen=? WHERE id=?",
                            (time.time() + ttl, utc_now(), valid_address(agent_id)))

    def set_presence(self, agents: list[dict], ttl: int = 25):
        """Replace the active set atomically; metadata and messages remain archived."""
        present_until = time.time() + ttl
        for agent in agents:
            self.register_agent(agent["account_id"], agent["session_id"], agent.get("name", ""),
                                agent.get("workspace", ""), agent.get("status", "idle"),
                                agent.get("activity_at"))
        with self.db:
            self.db.execute("UPDATE agents SET present_until=0 WHERE present_until>0 AND account_id NOT LIKE '%:%'")
            self.db.executemany("UPDATE agents SET present_until=? WHERE id=?",
                                [(present_until, agent["id"]) for agent in agents])

    def managed_agents(self) -> list[dict]:
        rows = self.db.execute("""SELECT * FROM managed_cli WHERE session_id IS NOT NULL
            AND state IN ('ready','working') ORDER BY created_at""").fetchall()
        return [{"id": agent_address(row["account_id"], row["session_id"]),
                 "account_id": row["account_id"], "session_id": row["session_id"],
                 "name": row["name"], "status": "working" if row["state"] == "working" else "idle",
                 "workspace": "Codex CLI", "managed": True} for row in rows]

    def managed_launches(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("""SELECT * FROM managed_cli
            WHERE state<>'stopped' ORDER BY created_at DESC LIMIT 30""")]

    def create_managed_launch(self, launch_id: str, account_id: str, channel_id: str, name: str):
        valid_address(launch_id)
        valid_address(account_id)
        valid_address(channel_id)
        if not self.db.execute("SELECT 1 FROM channels WHERE id=?", (channel_id,)).fetchone():
            raise ValueError("Channel inconnu")
        stamp = utc_now()
        latest = self.db.execute("SELECT COALESCE(MAX(id),0) FROM channel_messages WHERE channel_id=?",
                                 (channel_id,)).fetchone()[0]
        with self.db:
            self.db.execute("""INSERT INTO managed_cli(launch_id,account_id,session_id,channel_id,
                name,state,error,created_at,updated_at,last_processed_id)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (launch_id, account_id, None, channel_id, name[:160], "starting", "", stamp, stamp, latest))

    def update_managed_launch(self, launch_id: str, state: str, session_id: str | None = None,
                              error: str = ""):
        if state not in {"starting", "working", "ready", "error", "stopped"}:
            raise ValueError("État invalide")
        with self.db:
            self.db.execute("""UPDATE managed_cli SET state=?,session_id=COALESCE(?,session_id),
                error=?,updated_at=? WHERE launch_id=?""",
                (state, session_id, error[:500], utc_now(), valid_address(launch_id)))

    def managed_launch(self, launch_id: str) -> dict:
        row = self.db.execute("SELECT * FROM managed_cli WHERE launch_id=?", (valid_address(launch_id),)).fetchone()
        if row is None:
            raise ValueError("Agent CLI inconnu")
        return dict(row)

    def next_targeted_message(self, launch_id: str) -> int | None:
        launch = self.managed_launch(launch_id)
        if not launch["session_id"]:
            return None
        target = agent_address(launch["account_id"], launch["session_id"])
        row = self.db.execute("""SELECT id FROM channel_messages WHERE channel_id=?
            AND target_agent_id=? AND id>? ORDER BY id LIMIT 1""",
            (launch["channel_id"], target, launch["last_processed_id"])).fetchone()
        return row[0] if row else None

    def mark_processed(self, launch_id: str, message_id: int):
        with self.db:
            self.db.execute("UPDATE managed_cli SET last_processed_id=MAX(last_processed_id,?) WHERE launch_id=?",
                            (message_id, valid_address(launch_id)))

    def join_channel(self, agent_id: str, channel_id: str):
        self.agent(agent_id)
        if not self.db.execute("SELECT 1 FROM channels WHERE id=?", (valid_address(channel_id),)).fetchone():
            raise ValueError("Channel inconnu")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO channel_members VALUES (?,?,?)",
                            (channel_id, agent_id, utc_now()))

    def _private_channel(self, first: str, second: str) -> str:
        first, second = valid_address(first), valid_address(second)
        if first == second:
            raise ValueError("Choisissez un autre agent")
        self.agent(first)
        self.agent(second)
        key = "\0".join(sorted((first, second)))
        channel_id = "private:" + hashlib.sha256(key.encode()).hexdigest()[:20]
        stamp = utc_now()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO channels VALUES (?,?,?,?,?)",
                            (channel_id, "private", "Échange privé", key, stamp))
            self.db.executemany("INSERT OR IGNORE INTO channel_members VALUES (?,?,?)",
                                [(channel_id, first, stamp), (channel_id, second, stamp)])
        return channel_id

    def _check_member(self, agent_id: str, channel_id: str):
        if not self.db.execute("SELECT 1 FROM channel_members WHERE agent_id=? AND channel_id=?",
                               (valid_address(agent_id), valid_address(channel_id))).fetchone():
            raise ValueError("Agent non membre de ce channel")

    @staticmethod
    def _references(references: list[str] | None) -> list[str]:
        if references is None:
            return []
        if not isinstance(references, list) or len(references) > 8 or any(
                not isinstance(item, str) or not item.strip() or len(item) > 500 for item in references):
            raise ValueError("Références invalides")
        return [item.strip() for item in references]

    def send(self, sender: str, content: str, channel_id: str = GENERAL,
             recipient_agent: str | None = None, reply_to_id: int | None = None,
             target_agent: str | None = None, client_key: str | None = None,
             references: list[str] | None = None) -> dict:
        sender = valid_address(sender)
        self.agent(sender)
        references = self._references(references)
        if client_key is not None:
            client_key = valid_address(client_key)
            duplicate = self.db.execute("SELECT * FROM channel_messages WHERE sender_agent_id=? AND client_key=?",
                                        (sender, client_key)).fetchone()
            if duplicate:
                if recipient_agent:
                    key = "\0".join(sorted((sender, valid_address(recipient_agent))))
                    expected_channel = "private:" + hashlib.sha256(key.encode()).hexdigest()[:20]
                else:
                    expected_channel = channel_id
                if (duplicate["content"] != str(content).strip() or
                    duplicate["channel_id"] != expected_channel or
                    duplicate["target_agent_id"] != target_agent or
                    duplicate["reply_to_id"] != reply_to_id or
                    json.loads(duplicate["references_json"]) != references):
                    raise ValueError("Identifiant de message déjà utilisé pour un autre contenu")
                return {**dict(duplicate), "references": references}
        if not isinstance(content, str) or not content.strip() or len(content) > 10000:
            raise ValueError("Le message doit contenir entre 1 et 10 000 caractères")
        if recipient_agent:
            if target_agent:
                raise ValueError("Choisissez un destinataire privé ou une cible du channel")
            if channel_id != GENERAL:
                raise ValueError("Choisissez un channel ou un destinataire")
            channel_id = self._private_channel(sender, recipient_agent)
        self._check_member(sender, channel_id)
        if target_agent:
            self.agent(target_agent)
            self._check_member(target_agent, channel_id)
        if reply_to_id is not None:
            if type(reply_to_id) is not int or reply_to_id < 1 or not self.db.execute(
                    "SELECT 1 FROM channel_messages WHERE id=? AND channel_id=?",
                    (reply_to_id, channel_id)).fetchone():
                raise ValueError("Message de réponse introuvable dans ce channel")
        stamp = utc_now()
        if target_agent:
            recipients = [target_agent] if target_agent != sender else []
        elif channel_id == GENERAL:
            recipients = [agent["id"] for agent in self.active_agents() if agent["id"] != sender]
        else:
            recipients = [row[0] for row in self.db.execute(
                "SELECT agent_id FROM channel_members WHERE channel_id=? AND agent_id<>?",
                (channel_id, sender))]
        with self.db:
            cursor = self.db.execute(
                """INSERT INTO channel_messages(channel_id,sender_agent_id,content,created_at,
                    reply_to_id,target_agent_id,client_key,global_id,references_json) VALUES (?,?,?,?,?,?,?,?,?)""",
                (channel_id, sender, content.strip(), stamp, reply_to_id, target_agent, client_key,
                 uuid.uuid4().hex, json.dumps(references, ensure_ascii=False)))
            self.db.executemany("INSERT OR IGNORE INTO message_deliveries(message_id,agent_id) VALUES (?,?)",
                                [(cursor.lastrowid, agent_id) for agent_id in recipients])
        return {"id": cursor.lastrowid, "channel_id": channel_id,
                "sender_agent_id": sender, "content": content.strip(), "created_at": stamp,
                "reply_to_id": reply_to_id, "target_agent_id": target_agent,
                "references": references}

    def pending(self, agent_id: str, limit: int = 12, throttle_seconds: int = 0,
                after_id: int = 0) -> list[dict]:
        agent_id = valid_address(agent_id)
        if not 1 <= limit <= 50 or type(after_id) is not int or after_id < 0:
            raise ValueError("Pagination invalide")
        if throttle_seconds:
            stamp = time.time()
            with self.db:
                row = self.db.execute("SELECT last_checked_at FROM inbox_checks WHERE agent_id=?",
                                      (agent_id,)).fetchone()
                if row and stamp - row[0] < throttle_seconds:
                    return []
                self.db.execute("INSERT INTO inbox_checks VALUES (?,?) ON CONFLICT(agent_id) "
                                "DO UPDATE SET last_checked_at=excluded.last_checked_at", (agent_id, stamp))
        return [{**dict(row), "references": json.loads(row["references_json"])} for row in self.db.execute("""SELECT m.id,m.channel_id,m.sender_agent_id,
            m.content,m.created_at,m.reply_to_id,m.target_agent_id,m.references_json
            FROM message_deliveries d JOIN channel_messages m ON m.id=d.message_id
            WHERE d.agent_id=? AND d.delivered_at IS NULL AND m.id>? ORDER BY m.id LIMIT ?""",
            (agent_id, after_id, limit))]

    def pending_count(self, agent_id: str) -> int:
        return self.db.execute("""SELECT COUNT(*) FROM message_deliveries
            WHERE agent_id=? AND delivered_at IS NULL""", (valid_address(agent_id),)).fetchone()[0]

    def poll_allowed(self, key: str, seconds: int) -> bool:
        stamp = time.time()
        with self.db:
            row = self.db.execute("SELECT last_checked_at FROM inbox_checks WHERE agent_id=?",
                                  (valid_address(key),)).fetchone()
            if row and stamp - row[0] < seconds:
                return False
            self.db.execute("INSERT INTO inbox_checks VALUES (?,?) ON CONFLICT(agent_id) "
                            "DO UPDATE SET last_checked_at=excluded.last_checked_at", (key, stamp))
        return True

    def prepare_hook_emission(self, agent_id: str, event: str,
                              local_ids: list[int], remote_ids: list[int],
                              scan_path: str | None = None, scan_offset: int = 0) -> str:
        agent_id = valid_address(agent_id)
        if event not in {"UserPromptSubmit", "PostToolUse"}:
            raise ValueError("Événement inconnu")
        nonce = uuid.uuid4().hex
        stamp = utc_now()
        with self.db:
            self.db.execute("""INSERT INTO hook_emissions
                (nonce,agent_id,event,local_ids_json,remote_ids_json,prepared_at,scan_path,scan_offset)
                VALUES (?,?,?,?,?,?,?,?)""",
                (nonce, agent_id, event, json.dumps(local_ids), json.dumps(remote_ids),
                 stamp, scan_path, max(0, scan_offset)))
            self.db.executemany("""UPDATE message_deliveries SET attempted_at=?
                WHERE agent_id=? AND message_id=? AND delivered_at IS NULL""",
                [(stamp, agent_id, mid) for mid in local_ids])
        return nonce

    def pending_hook_emissions(self, limit: int = 100) -> list[dict]:
        return [dict(row) for row in self.db.execute("""SELECT * FROM hook_emissions
            WHERE verified_at IS NULL ORDER BY prepared_at DESC LIMIT ?""", (limit,))]

    def pending_remote_hook_acks(self, limit: int = 100) -> list[dict]:
        return [dict(row) for row in self.db.execute("""SELECT * FROM hook_emissions
            WHERE verified_at IS NOT NULL AND remote_ids_json!='[]' AND remote_ack_at IS NULL
            ORDER BY verified_at DESC LIMIT ?""", (limit,))]

    def verify_hook_emission(self, nonce: str, proof: str):
        with self.db:
            row = self.db.execute("SELECT * FROM hook_emissions WHERE nonce=?", (nonce,)).fetchone()
            if not row or row["verified_at"]:
                return
            stamp = utc_now()
            self.db.execute("UPDATE hook_emissions SET verified_at=?,proof=? WHERE nonce=?",
                            (stamp, proof[:500], nonce))
            ids = json.loads(row["local_ids_json"])
            self.db.executemany("""UPDATE message_deliveries
                SET delivered_at=?,method='hook',proof=?
                WHERE agent_id=? AND message_id=? AND delivered_at IS NULL""",
                [(stamp, proof[:500], row["agent_id"], mid) for mid in ids])

    def record_hook_scan(self, nonce: str, path: str, offset: int):
        with self.db:
            self.db.execute("""UPDATE hook_emissions SET scan_path=?,scan_offset=?
                WHERE nonce=? AND verified_at IS NULL""", (path, offset, nonce))

    def mark_remote_hook_ack(self, nonce: str):
        with self.db:
            self.db.execute("UPDATE hook_emissions SET remote_ack_at=? WHERE nonce=? AND verified_at IS NOT NULL",
                            (utc_now(), nonce))

    def mark_delivered(self, agent_id: str, message_ids: list[int], method: str):
        if method not in {"hook", "mcp"}:
            raise ValueError("Méthode inconnue")
        if not message_ids:
            return
        with self.db:
            self.db.executemany("UPDATE message_deliveries SET delivered_at=?,method=? "
                                "WHERE agent_id=? AND message_id=? AND delivered_at IS NULL",
                                [(utc_now(), method, valid_address(agent_id), message_id)
                                 for message_id in message_ids])

    def message(self, message_id: int, agent_id: str | None = None) -> dict:
        if type(message_id) is not int or message_id < 1:
            raise ValueError("Identifiant invalide")
        row = self.db.execute("SELECT * FROM channel_messages WHERE id=?", (message_id,)).fetchone()
        if row is None:
            raise ValueError("Message introuvable")
        if agent_id:
            self._check_member(agent_id, row["channel_id"])
        return {**dict(row), "references": json.loads(row["references_json"])}

    def agent_history(self, agent_id: str, limit: int = 12) -> list[dict]:
        agent_id = valid_address(agent_id)
        if not 1 <= limit <= 50:
            raise ValueError("Limite invalide")
        return [{**dict(row), "references": json.loads(row["references_json"])} for row in self.db.execute("""SELECT m.id,m.channel_id,m.sender_agent_id,
            m.content,m.created_at,m.reply_to_id,m.target_agent_id,m.references_json,
            CASE WHEN m.sender_agent_id=? THEN 'sent' ELSE 'received' END AS direction
            FROM channel_messages m WHERE m.sender_agent_id=? OR EXISTS (
              SELECT 1 FROM message_deliveries d WHERE d.message_id=m.id AND d.agent_id=?)
            ORDER BY m.id DESC LIMIT ?""", (agent_id, agent_id, agent_id, limit))]

    def messages(self, channel_id: str, before_id: int = 0, limit: int = 50,
                 agent_id: str | None = None, pair: tuple[str, str] | None = None) -> dict:
        channel_id = valid_address(channel_id)
        if agent_id:
            self._check_member(agent_id, channel_id)
        if type(before_id) is not int or before_id < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Pagination invalide")
        if not self.db.execute("SELECT 1 FROM channels WHERE id=?", (channel_id,)).fetchone():
            raise ValueError("Channel inconnu")
        pair_clause = ""
        params: list = [channel_id, before_id, before_id]
        if pair:
            first, second = valid_address(pair[0]), valid_address(pair[1])
            if first == second:
                raise ValueError("Choisissez deux agents distincts")
            pair_clause = """ AND ((m.sender_agent_id=? AND m.target_agent_id=?)
                OR (m.sender_agent_id=? AND m.target_agent_id=?)
                OR (m.sender_agent_id=? AND EXISTS (SELECT 1 FROM channel_messages parent
                    WHERE parent.id=m.reply_to_id AND parent.sender_agent_id=?))
                OR (m.sender_agent_id=? AND EXISTS (SELECT 1 FROM channel_messages parent
                    WHERE parent.id=m.reply_to_id AND parent.sender_agent_id=?))
                OR (m.sender_agent_id IN (?,?) AND m.reply_to_id IS NULL AND
                    m.target_agent_id IS NULL AND EXISTS (SELECT 1 FROM channels c
                    WHERE c.id=m.channel_id AND c.kind='private') AND
                    (SELECT COUNT(*) FROM channel_members cm WHERE cm.channel_id=m.channel_id)=2))"""
            params.extend((first, second, second, first, first, second, second, first, first, second))
        rows = self.db.execute("""SELECT m.id,m.channel_id,m.sender_agent_id,m.content,m.created_at,
            m.reply_to_id,m.target_agent_id,m.read_at,m.references_json,m.global_id,
            (SELECT COUNT(*) FROM message_deliveries d WHERE d.message_id=m.id) AS recipient_count,
            (SELECT COUNT(*) FROM message_deliveries d WHERE d.message_id=m.id AND d.delivered_at IS NOT NULL) AS delivered_count
            FROM channel_messages m WHERE m.channel_id=? AND (?=0 OR m.id<?)""" + pair_clause +
            " ORDER BY m.id DESC LIMIT ?", (*params, limit + 1)).fetchall()
        messages = [{**dict(row), "references": json.loads(row["references_json"]),
                     "deliveries": []} for row in reversed(rows[:limit])]
        if messages:
            by_id = {item["id"]: item for item in messages}
            placeholders = ",".join("?" for _ in messages)
            for delivery in self.db.execute("SELECT message_id,agent_id,delivered_at,method,attempted_at,proof "
                    f"FROM message_deliveries WHERE message_id IN ({placeholders}) "
                    "ORDER BY agent_id", tuple(by_id)):
                by_id[delivery["message_id"]]["deliveries"].append(dict(delivery))
        return {"messages": messages,
                "has_more": len(rows) > limit}

    def channels(self, agent_id: str | None = None) -> list[dict]:
        if agent_id:
            self.agent(agent_id)
        rows = self.db.execute("""SELECT c.id,c.kind,c.name,c.created_at,
            COUNT(DISTINCT cm.agent_id) AS participants,
            COUNT(DISTINCT m.id) AS messages,MAX(m.id) AS last_id,MAX(m.created_at) AS last_at
            FROM channels c LEFT JOIN channel_members cm ON cm.channel_id=c.id
            LEFT JOIN channel_messages m ON m.channel_id=c.id
            WHERE (? IS NULL OR EXISTS (SELECT 1 FROM channel_members own
                WHERE own.channel_id=c.id AND own.agent_id=?))
            GROUP BY c.id ORDER BY CASE WHEN c.kind='general' THEN 0 ELSE 1 END,
                last_id DESC,c.created_at DESC""", (agent_id, agent_id)).fetchall()
        result = []
        active_ids = {agent["id"] for agent in self.active_agents()}
        for row in rows:
            item = dict(row)
            item["delivery_revision"] = self.db.execute("""SELECT COUNT(*) FROM message_deliveries d
                JOIN channel_messages m ON m.id=d.message_id
                WHERE m.channel_id=? AND d.delivered_at IS NOT NULL""", (item["id"],)).fetchone()[0]
            item["members"] = [r[0] for r in self.db.execute(
                "SELECT agent_id FROM channel_members WHERE channel_id=? ORDER BY agent_id", (item["id"],))]
            if item["kind"] in {"general", "shared"}:
                item["members"] = [member for member in item["members"] if member in active_ids]
                item["participants"] = len(item["members"])
            result.append(item)
        return result

    def overview(self) -> dict:
        edges = [dict(row) for row in self.db.execute("""SELECT m.channel_id,m.sender_agent_id AS sender,
            cm.agent_id AS recipient,COUNT(*) AS messages,MAX(m.id) AS last_id
            FROM channel_messages m JOIN channels c ON c.id=m.channel_id AND c.kind='private'
            JOIN channel_members cm ON cm.channel_id=m.channel_id AND cm.agent_id<>m.sender_agent_id
            WHERE m.reply_to_id IS NULL AND m.target_agent_id IS NULL
                AND (SELECT COUNT(*) FROM channel_members pair
                WHERE pair.channel_id=m.channel_id)=2
            GROUP BY m.channel_id,m.sender_agent_id,cm.agent_id""")]
        edges.extend(dict(row) for row in self.db.execute("""SELECT m.channel_id,
            m.sender_agent_id AS sender,parent.sender_agent_id AS recipient,
            COUNT(*) AS messages,MAX(m.id) AS last_id
            FROM channel_messages m JOIN channel_messages parent ON parent.id=m.reply_to_id
                AND parent.channel_id=m.channel_id
            WHERE m.sender_agent_id<>parent.sender_agent_id
            GROUP BY m.channel_id,m.sender_agent_id,parent.sender_agent_id"""))
        edges.extend(dict(row) for row in self.db.execute("""SELECT m.channel_id,
            m.sender_agent_id AS sender,m.target_agent_id AS recipient,
            COUNT(*) AS messages,MAX(m.id) AS last_id
            FROM channel_messages m WHERE m.target_agent_id IS NOT NULL
            AND m.target_agent_id<>m.sender_agent_id AND m.reply_to_id IS NULL
            GROUP BY m.channel_id,m.sender_agent_id,m.target_agent_id"""))
        merged_edges = {}
        for edge in edges:
            key = (edge["channel_id"], edge["sender"], edge["recipient"])
            current = merged_edges.setdefault(key, {**edge, "messages": 0})
            current["messages"] += edge["messages"]
            current["last_id"] = max(current["last_id"], edge["last_id"])
        edges = list(merged_edges.values())
        general_senders = [dict(row) for row in self.db.execute(
            "SELECT sender_agent_id AS sender,COUNT(*) AS messages FROM channel_messages "
            "WHERE channel_id=? GROUP BY sender_agent_id", (GENERAL,))]
        active = self.active_agents()
        managed = {agent_address(row["account_id"], row["session_id"]): row
                   for row in self.managed_launches() if row["session_id"] and
                   row["state"] in {"ready", "working"}}
        for agent in active:
            launch = managed.get(agent["id"])
            if launch:
                agent["managed"] = True
                agent["launch_id"] = launch["launch_id"]
        active_ids = {agent["id"] for agent in active}
        summaries = {}
        for agent in active:
            identity = agent["id"]
            sent_count = self.db.execute("SELECT COUNT(*) FROM channel_messages WHERE sender_agent_id=?",
                                         (identity,)).fetchone()[0]
            received_count, pending_count = self.db.execute("""SELECT COUNT(*),
                SUM(CASE WHEN delivered_at IS NULL THEN 1 ELSE 0 END)
                FROM message_deliveries WHERE agent_id=?""", (identity,)).fetchone()
            summaries[identity] = {"sent": sent_count, "received": received_count,
                                   "pending": pending_count or 0}
        graph_activity = {(row[0], row[1]) for row in self.db.execute(
            "SELECT DISTINCT channel_id,sender_agent_id FROM channel_messages") if row[1] in active_ids}
        graph_activity.update((edge["channel_id"], edge["recipient"]) for edge in edges
                              if edge["recipient"] in active_ids)
        graph_ids = {agent_id for _, agent_id in graph_activity}
        graph_edges = [edge for edge in edges if edge["sender"] in active_ids and
                       edge["recipient"] in active_ids]
        channels = self.channels()
        for channel in channels:
            if channel["kind"] in {"general", "shared"}:
                channel["members"] = [member for member in channel["members"] if member in active_ids]
                channel["participants"] = len(channel["members"])
            else:
                channel["active_participants"] = sum(member in active_ids for member in channel["members"])
        history_ids = {member for channel in channels if channel["kind"] == "private" and channel["messages"]
                       for member in channel["members"]}
        history_ids.update(row[0] for row in self.db.execute("""SELECT DISTINCT m.sender_agent_id
            FROM channel_messages m JOIN channels c ON c.id=m.channel_id WHERE c.kind='shared'"""))
        history_ids.update(item["sender"] for item in general_senders)
        directory = [agent for agent in self.agents() if agent["id"] in active_ids | history_ids]
        return {"agents": active, "directory": directory, "channels": channels,
                "agent_summaries": summaries,
                "connections": graph_edges, "graph_agents": [agent for agent in active if agent["id"] in graph_ids],
                "graph_activity": [{"channel_id": channel_id, "agent_id": agent_id}
                                   for channel_id, agent_id in sorted(graph_activity)],
                "general_senders": general_senders, "launches": self.managed_launches()}
