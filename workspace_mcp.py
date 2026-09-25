"""Dependency-free Codex Workspace MCP stdio server."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from workspace_mailbox import DEFAULT_DB, GENERAL, Mailbox, agent_address, valid_address
from workspace_federation import PeerClient, load_config as load_federation_config
from workspace_diagnostics import probe_github

TOOLS = [
    {"name": "list_agents", "description": "Liste les chats Codex connus et leur identifiant de session.",
     "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "list_channels", "description": "Liste les channels auxquels participe ce chat.",
     "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "check_environment", "description": "Vérifie GitHub CLI et le dépôt depuis l'environnement MCP de ce chat.",
     "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "join_channel", "description": "Ajoute ce chat à un channel partagé choisi.",
     "inputSchema": {"type": "object", "properties": {"channel_id": {"type": "string"}},
                     "required": ["channel_id"], "additionalProperties": False}},
    {"name": "send_message", "description": "Écrit dans general par défaut. target_agent cible un agent dans le channel ; recipient_agent crée un channel privé.",
     "inputSchema": {"type": "object", "properties": {
         "content": {"type": "string"}, "channel_id": {"type": "string"},
         "recipient_agent": {"type": "string"},
         "target_agent": {"type": "string"},
         "reply_to_id": {"type": "integer", "minimum": 1},
         "references": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
         "client_key": {"type": "string", "description": "Identifiant stable pour éviter un doublon lors d'une relance"}},
      "required": ["content"], "additionalProperties": False}},
    {"name": "read_inbox", "description": "Lit les nouveaux messages destinés à ce chat, sans exiger de réponse.",
     "inputSchema": {"type": "object", "properties": {
         "limit": {"type": "integer", "minimum": 1, "maximum": 50},
         "after_id": {"type": "integer", "minimum": 0},
         "remote_after_id": {"type": "integer", "minimum": 0}}, "additionalProperties": False}},
    {"name": "get_message", "description": "Retrouve un message exact par identifiant, y compris ancien.",
     "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {
         "message_id": {"type": "integer", "minimum": 1}, "channel_id": {"type": "string"}},
         "required": ["message_id"], "additionalProperties": False}},
    {"name": "read_messages", "description": "Lit un channel dont ce chat est membre, avec pagination.",
     "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {
         "channel_id": {"type": "string"}, "before_id": {"type": "integer", "minimum": 0},
         "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
         "additionalProperties": False}},
    {"name": "list_tasks", "description": "Liste les tâches du Kanban visibles par ce chat. mine_only limite aux tâches attribuées.",
     "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {
         "channel_id": {"type": "string"}, "mine_only": {"type": "boolean"}},
         "additionalProperties": False}},
    {"name": "create_task", "description": "Crée une tâche Kanban dans un channel. L'agent destinataire doit être disponible.",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string"}, "description": {"type": "string"},
         "channel_id": {"type": "string"}, "assignee_agent_id": {"type": "string"}},
         "required": ["title"], "additionalProperties": False}},
    {"name": "claim_task", "description": "Prend une tâche Kanban encore non attribuée.",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}},
         "required": ["task_id"], "additionalProperties": False}},
    {"name": "assign_task", "description": "Attribue une tâche créée par ce chat ou dont il est responsable à un agent disponible.",
     "inputSchema": {"type": "object", "properties": {
         "task_id": {"type": "string"}, "assignee_agent_id": {"type": "string"}},
         "required": ["task_id", "assignee_agent_id"], "additionalProperties": False}},
    {"name": "update_task", "description": "Met à jour l'état et/ou ajoute une note de progression à une tâche attribuée.",
     "inputSchema": {"type": "object", "properties": {
         "task_id": {"type": "string"},
         "status": {"type": "string", "enum": ["todo", "working", "review", "blocked", "done"]},
         "note": {"type": "string"}},
         "required": ["task_id"], "additionalProperties": False}},
]


def origin_agent(request: dict, mailbox: Mailbox, account_id: str, codex_home: Path) -> str:
    # Codex attaches threadId to MCP request _meta:
    # https://github.com/openai/codex/pull/45409
    metadata = ((request.get("params") or {}).get("_meta") or {})
    thread_id = valid_address(metadata.get("threadId"))
    database = codex_home / "state_5.sqlite"
    if not database.is_file():
        raise ValueError("Index local des chats Codex indisponible")
    try:
        with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=2)) as connection:
            row = connection.execute(
                "SELECT COALESCE(NULLIF(name,''),NULLIF(title,''),'') AS name,cwd "
                "FROM threads WHERE id=? AND source IN ('vscode','cli','exec') AND archived=0 LIMIT 1",
                (thread_id,)).fetchone()
    except sqlite3.Error as exc:
        raise ValueError("Impossible de vérifier le chat Codex") from exc
    if row is None:
        raise ValueError("Chat Codex inconnu dans ce profil")
    agent_id = agent_address(account_id, thread_id)
    try:
        status = mailbox.agent(agent_id)["status"]
    except ValueError:
        status = "idle"
    mailbox.register_agent(account_id, thread_id, row[0] or "", row[1] or "", status)
    mailbox.touch_presence(agent_id)
    return agent_id


def respond(request: dict, mailbox: Mailbox, account_id: str, codex_home: Path) -> dict | None:
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                "error": {"code": -32600, "message": "Invalid Request"}}
    method, request_id = request.get("method"), request.get("id")
    if request_id is None:
        return None
    try:
        if method == "initialize":
            offered = (request.get("params") or {}).get("protocolVersion")
            version = offered if offered in ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05") else "2025-11-25"
            result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "codex-workspace-mailbox", "version": "0.2.0"},
                      "instructions": "Chaque chat Codex est un agent distinct. Utilisez list_tasks pour voir le Kanban, claim_task pour prendre une tâche libre et update_task pour signaler votre avancement. Utilisez list_agents et send_message pour collaborer. Les tâches et messages restent sur ce PC."}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = request.get("params") or {}
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise ValueError("Arguments invalides")
            agent_id = origin_agent(request, mailbox, account_id, codex_home)
            federation_error = None
            try:
                federation = load_federation_config()
            except (ValueError, OSError, KeyError) as exc:
                federation = None
                federation_error = str(exc)
            client = PeerClient(federation) if federation and federation["role"] == "client" else None
            shared = federation["channel_id"] if client else None
            name = params.get("name")
            delivery_commit = None
            if name == "list_agents":
                if arguments:
                    raise ValueError("Arguments inconnus")
                value = {"self": agent_id, "pending_local_messages": mailbox.pending_count(agent_id),
                         "agents": mailbox.active_agents()}
                if client:
                    try:
                        remote_overview = client.overview()
                        value["agents"].extend(agent for agent in remote_overview["agents"]
                                               if agent.get("remote"))
                        value["pending_shared_messages"] = (remote_overview["agent_summaries"]
                            .get(agent_id, {}).get("pending", 0))
                    except (OSError, ValueError):
                        value["pending_shared_messages"] = None
            elif name == "list_channels":
                if arguments:
                    raise ValueError("Arguments inconnus")
                value = {"self": agent_id, "pending_local_messages": mailbox.pending_count(agent_id),
                         "channels": mailbox.channels(agent_id)}
                if client and any(item["id"] == shared for item in value["channels"]):
                    try:
                        remote_overview = client.overview()
                        remote = remote_overview["channel"]
                        value["channels"] = [remote if item["id"] == shared else item for item in value["channels"]]
                        value["pending_shared_messages"] = (remote_overview["agent_summaries"]
                            .get(agent_id, {}).get("pending", 0))
                    except (OSError, ValueError):
                        value["pending_shared_messages"] = None
            elif name == "check_environment":
                if arguments:
                    raise ValueError("Arguments inconnus")
                value = {"github": probe_github(mailbox.agent(agent_id)["workspace"]),
                         "pending_local_messages": mailbox.pending_count(agent_id)}
                mailbox.save_agent_check(agent_id, value["github"])
            elif name == "join_channel":
                if set(arguments) != {"channel_id"} or not str(arguments["channel_id"]).startswith("shared:"):
                    raise ValueError("Channel partagé requis")
                if not federation or arguments["channel_id"] != federation["channel_id"]:
                    raise ValueError(f"Passerelle partagée indisponible : {federation_error or 'configuration absente'}")
                mailbox.join_channel(agent_id, arguments["channel_id"])
                value = {"self": agent_id, "channel_id": arguments["channel_id"], "joined": True}
            elif name == "send_message":
                if set(arguments) - {"content", "channel_id", "recipient_agent", "target_agent", "reply_to_id", "client_key", "references"} or "content" not in arguments:
                    raise ValueError("content est requis")
                channel_id = arguments.get("channel_id", GENERAL)
                if str(channel_id).startswith("shared:") and (not federation or channel_id != federation["channel_id"]):
                    raise ValueError(f"Passerelle partagée indisponible : {federation_error or 'configuration absente'}")
                if client and channel_id == shared:
                    if arguments.get("recipient_agent"):
                        raise ValueError("Utilisez target_agent dans le channel partagé")
                    queued = mailbox.queue_federated(agent_id, shared, arguments["content"],
                        arguments.get("target_agent"), arguments.get("reply_to_id"),
                        arguments.get("client_key"), arguments.get("references"))
                    try:
                        client.announce([mailbox.agent(agent_id)])
                        client.flush(mailbox)
                    except (OSError, ValueError):
                        pass
                    queued = dict(mailbox.db.execute("SELECT * FROM federation_outbox WHERE client_key=?",
                        (queued["client_key"],)).fetchone())
                    value = {"channel_id": shared, "client_key": queued["client_key"],
                             "queued": not bool(queued["sent_at"]), "id": queued["remote_id"],
                             "sender_agent_id": agent_id, "content": queued["content"]}
                else:
                    value = mailbox.send(agent_id, arguments["content"], channel_id,
                                         arguments.get("recipient_agent"), arguments.get("reply_to_id"),
                                         arguments.get("target_agent"), arguments.get("client_key"),
                                         arguments.get("references"))
            elif name == "read_inbox":
                if set(arguments) - {"limit", "after_id", "remote_after_id"}:
                    raise ValueError("Arguments inconnus")
                after_id = arguments.get("after_id", 0)
                limit = arguments.get("limit", 12)
                pending = mailbox.pending(agent_id, limit, after_id=after_id)
                value = {"self": agent_id, "messages": pending,
                         "next_after_id": pending[-1]["id"] if pending else after_id}
                delivery_commit = {"agent_id": agent_id,
                                   "local_ids": [item["id"] for item in pending]}
                if client and agent_id in mailbox.shared_members(shared):
                    try:
                        remote_after_id = arguments.get("remote_after_id", 0)
                        remote = client.inbox(agent_id, after_id=remote_after_id, limit=limit)
                        value["messages"].extend(remote)
                        value["next_remote_after_id"] = (remote[-1]["id"] if remote else remote_after_id)
                        delivery_commit["remote_ids"] = [item["id"] for item in remote]
                        delivery_commit["federation"] = federation
                    except (OSError, ValueError):
                        pass
            elif name == "get_message":
                if set(arguments) - {"message_id", "channel_id"} or "message_id" not in arguments:
                    raise ValueError("message_id est requis")
                if str(arguments.get("channel_id", "")).startswith("shared:") and not federation:
                    raise ValueError("Passerelle partagée indisponible")
                value = (client.message(agent_id, arguments["message_id"])
                         if client and arguments.get("channel_id") == shared else
                         mailbox.message(arguments["message_id"], agent_id))
            elif name == "read_messages":
                if set(arguments) - {"channel_id", "before_id", "limit"}:
                    raise ValueError("Arguments inconnus")
                channel_id = arguments.get("channel_id", GENERAL)
                if str(channel_id).startswith("shared:") and not federation:
                    raise ValueError("Passerelle partagée indisponible")
                if client and channel_id == shared:
                    mailbox._check_member(agent_id, shared)
                    value = {"channel_id": channel_id, **client.messages(
                        arguments.get("before_id", 0), arguments.get("limit", 50), agent_id)}
                    delivery_commit = {"agent_id": agent_id,
                        "remote_ids": [item["id"] for item in value["messages"]],
                        "federation": federation}
                else:
                    value = {"channel_id": channel_id, **mailbox.messages(
                        channel_id, arguments.get("before_id", 0), arguments.get("limit", 50), agent_id)}
                    delivery_commit = {"agent_id": agent_id,
                        "local_ids": [item["id"] for item in value["messages"]]}
            elif name == "list_tasks":
                if set(arguments) - {"channel_id", "mine_only"} or type(arguments.get("mine_only", False)) is not bool:
                    raise ValueError("Arguments inconnus")
                value = {"self": agent_id, "tasks": mailbox.work_tasks(agent_id,
                    arguments.get("channel_id"), arguments.get("mine_only", False))}
            elif name == "create_task":
                if set(arguments) - {"title", "description", "channel_id", "assignee_agent_id"} or "title" not in arguments:
                    raise ValueError("title est requis")
                value = mailbox.create_work_task(arguments["title"],
                    arguments.get("description", ""), arguments.get("channel_id", GENERAL),
                    arguments.get("assignee_agent_id"), agent_id)
            elif name in {"claim_task", "assign_task"}:
                expected = {"task_id"} if name == "claim_task" else {"task_id", "assignee_agent_id"}
                if set(arguments) != expected:
                    raise ValueError("Arguments de tâche invalides")
                value = mailbox.assign_work_task(arguments["task_id"],
                    agent_id if name == "claim_task" else arguments["assignee_agent_id"],
                    agent_id, claim=name == "claim_task")
            elif name == "update_task":
                if set(arguments) - {"task_id", "status", "note"} or "task_id" not in arguments:
                    raise ValueError("task_id est requis")
                value = mailbox.update_work_task(arguments["task_id"], arguments.get("status"),
                                                 arguments.get("note", ""), agent_id)
            else:
                return {"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32602, "message": "Outil inconnu"}}
            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                      "structuredContent": value, "isError": False}
            if delivery_commit:
                result["_delivery_commit"] = delivery_commit
        else:
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32601, "message": "Method not found"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except (ValueError, TypeError) as exc:
        if method == "tools/call":
            return {"jsonrpc": "2.0", "id": request_id,
                    "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True}}
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32602, "message": str(exc)}}


def commit_delivery(mailbox: Mailbox, commit: dict):
    if commit.get("local_ids"):
        mailbox.mark_delivered(commit["agent_id"], commit["local_ids"], "mcp")
    if commit.get("remote_ids"):
        PeerClient(commit["federation"]).mark_delivered(commit["agent_id"],
            commit["remote_ids"], "mcp")


def main():
    # MCP stdio uses UTF-8 on every platform, including Windows pipe transports.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Serveur MCP local Codex Workspace")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--codex-home", required=True)
    parser.add_argument("--db", default=os.environ.get("CODEX_WORKSPACE_MAILBOX_DB", str(DEFAULT_DB)))
    args = parser.parse_args()
    mailbox = Mailbox(args.db)
    try:
        for line in sys.stdin:
            try:
                response = respond(json.loads(line), mailbox, valid_address(args.account_id), Path(args.codex_home))
            except json.JSONDecodeError:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            if response is not None:
                commit = (response.get("result") or {}).pop("_delivery_commit", None)
                sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                sys.stdout.flush()
                if commit:
                    try:
                        commit_delivery(mailbox, commit)
                    except (OSError, ValueError):
                        # The client received the content. If the receipt fails,
                        # the next inbox read may repeat the same message.
                        pass
    finally:
        mailbox.close()


if __name__ == "__main__":
    main()
