import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

from scripts.install_workspace_mcp import install
from server import workspace_active_agents
from workspace_mailbox import Mailbox, agent_address
from workspace_hook_receipts import reconcile

ROOT = Path(__file__).resolve().parents[1]


class WorkspaceMcpTests(unittest.TestCase):
    def test_kanban_task_can_be_created_through_dashboard_api(self):
        from server import ExclusiveServer, Handler
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "mailbox.sqlite"
            http = ExclusiveServer(("127.0.0.1", 0), Handler)
            http.demo = False
            worker = threading.Thread(target=http.serve_forever, daemon=True)
            worker.start()
            try:
                with patch("server.WORKSPACE_MAILBOX_DB", db):
                    address = f"http://127.0.0.1:{http.server_port}/api/workspace-mcp/task"
                    payload = json.dumps({"action": "create", "title": "Tâche depuis Kanban",
                                          "channel_id": "general"}).encode("utf-8")
                    request = Request(address, data=payload,
                                      headers={"Content-Type": "application/json"})
                    with urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        task = json.load(response)["task"]
                    self.assertEqual(task["title"], "Tâche depuis Kanban")
                    with urlopen(address + "?task_id=" + task["id"], timeout=5) as response:
                        self.assertEqual(json.load(response)["task"]["status"], "todo")
            finally:
                http.shutdown()
                worker.join(timeout=5)
                http.server_close()

    def test_kanban_assignment_and_progress_are_shared_with_mcp_agents(self):
        from workspace_cli import mcp_call
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            db = base / "mailbox.sqlite"
            accounts = []
            for number, session in ((1, "chat-a"), (2, "chat-b")):
                home = base / f"Compte-{number}" / "codex"
                home.mkdir(parents=True)
                with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
                    connection.execute("CREATE TABLE threads(id TEXT,name TEXT,title TEXT,cwd TEXT,"
                                       "source TEXT,archived INTEGER)")
                    connection.execute("INSERT INTO threads VALUES (?,?,?,?,?,0)",
                                       (session, f"Agent {number}", "", str(base), "vscode"))
                    connection.commit()
                accounts.append({"id": f"compte-{number}", "codex_home": str(home)})
            a, b = agent_address("compte-1", "chat-a"), agent_address("compte-2", "chat-b")
            self.assertEqual(mcp_call(accounts[0], db, "chat-a", "list_agents", {})["self"], a)
            self.assertEqual(mcp_call(accounts[1], db, "chat-b", "list_agents", {})["self"], b)
            mailbox = Mailbox(db)
            try:
                task = mailbox.create_work_task("Vérifier la stratégie", "Comparer deux approches",
                                                assignee=b)
                self.assertEqual(task["status"], "todo")
                self.assertEqual(task["assignee_agent_id"], b)
                self.assertIn(task["id"], mailbox.pending(b)[0]["content"])
            finally:
                mailbox.close()
            mine = mcp_call(accounts[1], db, "chat-b", "list_tasks", {"mine_only": True})
            self.assertEqual([item["id"] for item in mine["tasks"]], [task["id"]])
            updated = mcp_call(accounts[1], db, "chat-b", "update_task",
                               {"task_id": task["id"], "status": "working", "note": "Analyse lancée"})
            self.assertEqual(updated["status"], "working")
            self.assertEqual(updated["events"][0]["note"], "Analyse lancée")
            mailbox = Mailbox(db)
            try:
                with self.assertRaises(ValueError):
                    mailbox.update_work_task(task["id"], "done", actor=a)
                mailbox.assign_work_task(task["id"], a)
                self.assertEqual(mailbox.work_task(task["id"])["assignee_agent_id"], a)
                self.assertIn(task["id"], mailbox.pending(a)[0]["content"])
                free = mailbox.create_work_task("Tester un combat")
            finally:
                mailbox.close()
            claimed = mcp_call(accounts[0], db, "chat-a", "claim_task", {"task_id": free["id"]})
            self.assertEqual(claimed["assignee_agent_id"], a)
            mailbox = Mailbox(db)
            try:
                with self.assertRaises(ValueError):
                    mailbox.assign_work_task(free["id"], b, actor=b, claim=True)
            finally:
                mailbox.close()

    def test_only_recent_work_in_confirmed_windows_becomes_active_agent(self):
        now = 1000
        snapshot = {"now": now, "accounts": [{"id": "compte-1",
            "windows": [{"id": "window-1"}],
            "metrics": {"threads": {"data": [
                {"id": "open-work", "name": "Travail"},
                {"id": "old-chat", "name": "Historique"},
                {"id": "closed-window", "name": "Fenêtre fermée"}]}},
            "tasks": [
                {"session_id": "open-work", "window_id": "window-1", "display_status": "active",
                 "updated_at": 995, "title": "Travail", "workspace": "C:/project"},
                {"session_id": "old-chat", "window_id": "window-1", "display_status": "completed",
                 "updated_at": 999, "title": "Historique", "workspace": "C:/project"},
                {"session_id": "closed-window", "window_id": None, "display_status": "active",
                 "updated_at": 999, "title": "Fenêtre fermée", "workspace": "C:/other"}]}]}
        self.assertEqual([agent["session_id"] for agent in workspace_active_agents(snapshot)], ["open-work"])
        snapshot["accounts"][0]["tasks"][0]["updated_at"] = 800
        self.assertEqual(workspace_active_agents(snapshot), [])

    def test_sessions_have_distinct_identities_and_channel_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            db = base / "mailbox.sqlite"
            homes = [base / "Compte-1" / "codex", base / "Compte-2" / "codex"]
            for home, sessions in zip(homes, (("chat-a", "chat-b"), ("chat-c",))):
                home.mkdir(parents=True)
                with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
                    connection.execute("CREATE TABLE threads(id TEXT,name TEXT,title TEXT,cwd TEXT,source TEXT,archived INTEGER,updated_at INTEGER)")
                    connection.executemany("INSERT INTO threads VALUES (?,?,?,?,?,0,100)",
                        [(sid, sid.upper(), "", "C:/project", "exec" if sid == "chat-c" else "vscode")
                         for sid in sessions])
                    connection.commit()

            def run(account, home, thread, tool, arguments=None):
                request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments or {},
                               "_meta": {"threadId": thread}}}
                process = subprocess.run([sys.executable, str(ROOT / "workspace_mcp.py"),
                    "--account-id", account, "--codex-home", str(home), "--db", str(db)],
                    input=json.dumps(request) + "\n", text=True, capture_output=True, timeout=10)
                self.assertEqual(process.returncode, 0, process.stderr)
                return json.loads(process.stdout)["result"]

            a = agent_address("compte-1", "chat-a")
            b = agent_address("compte-1", "chat-b")
            c = agent_address("compte-2", "chat-c")
            discovered = run("compte-1", homes[0], "chat-a", "list_agents")["structuredContent"]["agents"]
            self.assertEqual({agent["id"] for agent in discovered}, {agent_address("compte-1", "chat-a")})
            run("compte-1", homes[0], "chat-b", "list_agents")
            run("compte-2", homes[1], "chat-c", "list_agents")
            discovered = run("compte-1", homes[0], "chat-a", "list_agents")["structuredContent"]["agents"]
            self.assertEqual({agent["id"] for agent in discovered}, {
                agent_address("compte-1", "chat-a"), agent_address("compte-1", "chat-b"),
                agent_address("compte-2", "chat-c")})
            sent = run("compte-1", homes[0], "chat-a", "send_message",
                       {"recipient_agent": c, "content": "Bonjour C"})
            self.assertFalse(sent["isError"])
            private_id = sent["structuredContent"]["channel_id"]
            self.assertEqual(sent["structuredContent"]["sender_agent_id"], a)
            from workspace_mcp import respond
            pending_mailbox = Mailbox(db)
            try:
                staged = respond({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "read_inbox", "arguments": {},
                               "_meta": {"threadId": "chat-c"}}},
                    pending_mailbox, "compte-2", homes[1])
                self.assertIn("_delivery_commit", staged["result"])
                self.assertEqual(len(pending_mailbox.pending(c)), 1)
            finally:
                pending_mailbox.close()
            self.assertEqual([item["content"] for item in run("compte-2", homes[1], "chat-c",
                "read_messages", {"channel_id": private_id})["structuredContent"]["messages"]], ["Bonjour C"])
            self.assertTrue(run("compte-1", homes[0], "chat-b", "read_messages",
                {"channel_id": private_id})["isError"])
            self.assertTrue(run("compte-1", homes[0], "chat-c", "list_agents")["isError"])
            general = run("compte-1", homes[0], "chat-b", "send_message",
                          {"content": "Bonjour tout le monde"})["structuredContent"]
            self.assertEqual(general["channel_id"], "general")
            mailbox = Mailbox(db)
            try:
                overview = mailbox.overview()
                self.assertNotIn(sent["structuredContent"]["id"],
                                 [item["id"] for item in mailbox.pending(c)])
                self.assertEqual({item["id"] for item in overview["agents"]}, {a, b, c})
                self.assertEqual(len(overview["connections"]), 1)
                self.assertEqual(len(overview["channels"]), 2)
                self.assertEqual(overview["general_senders"], [{"sender": b, "messages": 1}])
                self.assertEqual(mailbox.messages("general")["messages"][0]["sender_agent_id"], b)
            finally:
                mailbox.close()

    def test_missing_metadata_and_legacy_data_are_not_misattributed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            db = base / "mailbox.sqlite"
            with closing(sqlite3.connect(db)) as connection:
                connection.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,sender TEXT,recipient TEXT,content TEXT)")
                connection.execute("INSERT INTO messages VALUES (1,'compte-1','compte-2','Legacy')")
                connection.commit()
            mailbox = Mailbox(db)
            try:
                self.assertEqual(mailbox.overview()["agents"], [])
                self.assertEqual(mailbox.channels()[0]["id"], "general")
                self.assertEqual(mailbox.db.execute("SELECT content FROM messages").fetchone()[0], "Legacy")
                from workspace_mcp import respond
                result = respond({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "list_agents", "arguments": {}}}, mailbox, "compte-1", base)
                self.assertTrue(result["result"]["isError"])
            finally:
                mailbox.close()

    def test_history_pagination_and_installer_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            mailbox = Mailbox(base / "mailbox.sqlite")
            try:
                a = mailbox.register_agent("compte-1", "chat-a")["id"]
                b = mailbox.register_agent("compte-2", "chat-b")["id"]
                ids = [mailbox.send(a, f"Message {i}", recipient_agent=b)["id"] for i in range(4)]
                channel = mailbox.channels(a)[1]["id"]
                latest = mailbox.messages(channel, limit=2)
                self.assertTrue(latest["has_more"])
                self.assertEqual([m["id"] for m in latest["messages"]], ids[2:])
                older = mailbox.messages(channel, before_id=ids[2], limit=2)
                self.assertFalse(older["has_more"])
                self.assertEqual([m["id"] for m in older["messages"]], ids[:2])
                mailbox.set_presence([{"id": a, "account_id": "compte-1", "session_id": "chat-a",
                                       "status": "working"}])
                self.assertEqual([item["id"] for item in mailbox.overview()["agents"]], [a])
                self.assertEqual(mailbox.channels()[0]["members"], [a])
                mailbox.set_presence([])
                self.assertEqual(mailbox.overview()["agents"], [])
                self.assertEqual(len(mailbox.messages(channel)["messages"]), 4)
                with self.assertRaises(ValueError):
                    mailbox.send(a, " ", recipient_agent=b)
            finally:
                mailbox.close()
            config = base / "config.toml"
            config.write_text('model = "gpt-6-sol"\n', encoding="utf-8")
            self.assertIn("installé", install(config, "compte-1"))
            self.assertEqual(install(config, "compte-1"), "déjà configuré")
            self.assertIn("--codex-home", config.read_text(encoding="utf-8"))
            old = base / "legacy.toml"
            old_args = [str(ROOT / "workspace_mcp.py"), "--agent-id", "compte-1",
                        "--db", str(ROOT / "data" / "workspace-mailbox.sqlite")]
            old.write_text('model = "gpt-6-sol"\n\n[mcp_servers.codex_workspace]\n'
                           f'command = {json.dumps(sys.executable)}\n'
                           f'args = {json.dumps(old_args, ensure_ascii=False)}\n',
                           encoding="utf-8")
            self.assertIn("mis à jour", install(old, "compte-1"))
            self.assertNotIn("--agent-id", old.read_text(encoding="utf-8"))
            self.assertEqual(len(list(base.glob("legacy.toml.bak-workspace-mcp-*"))), 1)
            moved = base / "moved.toml"
            moved_args = [str(base / "old-folder" / "workspace_mcp.py"), "--account-id", "compte-1",
                          "--codex-home", str(base), "--db", str(base / "old-folder" / "data" /
                                                               "workspace-mailbox.sqlite")]
            moved.write_text('model = "gpt-6-sol"\n\n[mcp_servers.codex_workspace]\n'
                             f'command = {json.dumps(sys.executable)}\n'
                             f'args = {json.dumps(moved_args, ensure_ascii=False)}\n', encoding="utf-8")
            self.assertIn("mis à jour", install(moved, "compte-1"))
            self.assertEqual(tomllib.loads(moved.read_text(encoding="utf-8"))
                             ["mcp_servers"]["codex_workspace"]["args"][0],
                             str(ROOT / "workspace_mcp.py"))

    def test_graph_uses_only_observed_messages_and_keeps_available_agents_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            mailbox = Mailbox(Path(directory) / "mailbox.sqlite")
            try:
                a = mailbox.register_agent("compte-1", "chat-a", "Alice")["id"]
                b = mailbox.register_agent("compte-2", "chat-b", "Bob")["id"]
                c = mailbox.register_agent("compte-2", "chat-c", "Charlie")["id"]
                presence = [{"id": identity, "account_id": identity.split(":")[0],
                             "session_id": identity.split(":")[1], "status": "idle"}
                            for identity in (a, b, c)]
                mailbox.set_presence(presence)
                self.assertEqual(len(mailbox.overview()["agents"]), 3)
                self.assertEqual(mailbox.overview()["graph_agents"], [])
                first = mailbox.send(a, "Bonjour Bob", target_agent=b)
                overview = mailbox.overview()
                self.assertEqual({item["id"] for item in overview["graph_agents"]}, {a, b})
                self.assertEqual([(item["sender"], item["recipient"]) for item in overview["connections"]], [(a, b)])
                self.assertEqual({item["agent_id"] for item in overview["graph_activity"]
                                  if item["channel_id"] == "general"}, {a, b})
                reply = mailbox.send(b, "Bonjour Alice", reply_to_id=first["id"])
                final = mailbox.send(a, "Bien reçu", reply_to_id=reply["id"])
                overview = mailbox.overview()
                self.assertEqual({(item["sender"], item["recipient"]) for item in overview["connections"]},
                                 {(a, b), (b, a)})
                self.assertEqual(next(item["messages"] for item in overview["connections"]
                                      if item["sender"] == a and item["recipient"] == b), 2)
                self.assertEqual(mailbox.messages("general")["messages"][1]["reply_to_id"], first["id"])
                self.assertEqual(reply["reply_to_id"], first["id"])
                pair_messages = mailbox.messages("general", pair=(a, b))["messages"]
                self.assertEqual([item["id"] for item in pair_messages],
                                 [first["id"], reply["id"], final["id"]])
                mailbox.create_managed_launch("cli-test", "compte-2", "general", "Agent CLI")
                mailbox.update_managed_launch("cli-test", "ready", session_id="chat-b")
                self.assertEqual(mailbox.next_targeted_message("cli-test"), None)
                directed = mailbox.send(a, "Encore un message", target_agent=b)
                self.assertEqual(mailbox.next_targeted_message("cli-test"), directed["id"])
                mailbox.mark_processed("cli-test", directed["id"])
                self.assertEqual(mailbox.next_targeted_message("cli-test"), None)
                with self.assertRaises(ValueError):
                    mailbox.send(a, "Mauvaise réponse", reply_to_id=9999)
                mailbox.set_presence(presence[1:])
                self.assertEqual({item["id"] for item in mailbox.overview()["graph_agents"]}, {b})
                self.assertEqual(mailbox.overview()["connections"], [])
            finally:
                mailbox.close()

    def test_mcp_stdio_preserves_utf8_text_for_cli_sessions(self):
        from workspace_cli import mcp_call
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "codex"
            home.mkdir()
            with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
                connection.execute("""CREATE TABLE threads(id TEXT,name TEXT,title TEXT,cwd TEXT,
                    source TEXT,archived INTEGER,updated_at INTEGER)""")
                connection.execute("INSERT INTO threads VALUES (?,?,?,?,?,0,100)",
                                   ("cli-chat", "Agent CLI", "", str(base), "exec"))
                connection.commit()
            db = base / "mailbox.sqlite"
            account = {"id": "compte-2", "codex_home": str(home)}
            message = mcp_call(account, db, "cli-chat", "send_message",
                               {"channel_id": "general", "content": "Échange à vérifier — reçu ✓"})
            self.assertEqual(message["content"], "Échange à vérifier — reçu ✓")
            read = mcp_call(account, db, "cli-chat", "read_messages", {"channel_id": "general"})
            self.assertEqual(read["messages"][0]["content"], message["content"])
            mailbox = Mailbox(db)
            try:
                for number in range(120):
                    mailbox.send(agent_address("compte-2", "cli-chat"), f"Suite {number}")
            finally:
                mailbox.close()
            recent = mcp_call(account, db, "cli-chat", "read_messages",
                              {"channel_id": "general", "limit": 100})
            self.assertTrue(recent["has_more"])
            self.assertNotIn(message["id"], [item["id"] for item in recent["messages"]])
            exact = mcp_call(account, db, "cli-chat", "get_message",
                             {"channel_id": "general", "message_id": message["id"]})
            self.assertEqual(exact["content"], message["content"])

    def test_delivery_is_per_recipient_and_retries_do_not_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            mailbox = Mailbox(Path(directory) / "mailbox.sqlite")
            try:
                a = mailbox.register_agent("compte-1", "chat-a")["id"]
                b = mailbox.register_agent("compte-2", "chat-b")["id"]
                c = mailbox.register_agent("compte-2", "chat-c")["id"]
                mailbox.set_presence([mailbox.agent(agent) for agent in (a, b, c)])
                first = mailbox.send(a, "Information sans réponse requise", client_key="stable-1",
                                     references=["PR #42", "docs/contrat.md"])
                retry = mailbox.send(a, "Information sans réponse requise", client_key="stable-1",
                                     references=["PR #42", "docs/contrat.md"])
                self.assertEqual(first["id"], retry["id"])
                with self.assertRaises(ValueError):
                    mailbox.send(a, "Autre contenu", client_key="stable-1")
                self.assertEqual(mailbox.message(first["id"])["references"], ["PR #42", "docs/contrat.md"])
                self.assertEqual([item["id"] for item in mailbox.pending(b)], [first["id"]])
                self.assertEqual([item["id"] for item in mailbox.pending(c)], [first["id"]])
                self.assertEqual(mailbox.pending(a), [])
                self.assertEqual(mailbox.messages("general")["messages"][0]["delivered_count"], 0)
                mailbox.mark_delivered(b, [first["id"]], "hook")
                self.assertEqual(mailbox.pending(b), [])
                self.assertEqual([item["id"] for item in mailbox.pending(c)], [first["id"]])
                self.assertEqual(mailbox.messages("general")["messages"][0]["delivered_count"], 1)
                deliveries = mailbox.messages("general")["messages"][0]["deliveries"]
                self.assertEqual({item["agent_id"]: item["method"] for item in deliveries},
                                 {b: "hook", c: None})
                mailbox.mark_delivered(c, [first["id"]], "mcp")
                self.assertEqual(mailbox.messages("general")["messages"][0]["delivered_count"], 2)
                private = mailbox.send(a, "Question", recipient_agent=b)
                self.assertEqual([item["id"] for item in mailbox.pending(b)], [private["id"]])
                self.assertEqual(mailbox.pending(c), [])
                second_private = mailbox.send(a, "Précision", recipient_agent=b)
                self.assertEqual([item["id"] for item in mailbox.pending(b, after_id=private["id"])],
                                 [second_private["id"]])
                self.assertEqual(mailbox.message(private["id"], b)["content"], "Question")
                with self.assertRaises(ValueError):
                    mailbox.message(private["id"], c)
            finally:
                mailbox.close()

    def test_prompt_hook_injects_only_recipient_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "mailbox.sqlite"
            mailbox = Mailbox(db)
            try:
                sender = mailbox.register_agent("compte-1", "chat-a")["id"]
                target = mailbox.register_agent("compte-2", "chat-b")["id"]
                other = mailbox.register_agent("compte-2", "chat-c")["id"]
                message = mailbox.send(sender, "Contrat API prêt", recipient_agent=target)
                mailbox.send(sender, "Secret pour C", recipient_agent=other)
                environment = {**os.environ, "CODEX_WORKSPACE_MAILBOX_DB": str(db)}
                event = {"session_id": "chat-b", "hook_event_name": "UserPromptSubmit"}
                process = subprocess.run([sys.executable, str(ROOT / "hook_sender.py"),
                    "--account", "compte-2", "--url", "http://127.0.0.1:1/api/events"],
                    input=json.dumps(event), text=True, capture_output=True, timeout=10, env=environment)
                self.assertEqual(process.returncode, 0, process.stderr)
                output = json.loads(process.stdout)
                context = output["hookSpecificOutput"]["additionalContext"]
                self.assertIn("Contrat API prêt", context)
                self.assertNotIn("Secret pour C", context)
                self.assertEqual([item["id"] for item in mailbox.pending(target)], [message["id"]])
                self.assertEqual(len(mailbox.pending(other)), 1)
                self.assertEqual(mailbox.messages(message["channel_id"])["messages"][0]["delivered_count"], 0)
                emission = mailbox.pending_hook_emissions()[0]
                self.assertIn("WMCP-HOOK-" + emission["nonce"], context)
                home = Path(directory) / "codex"
                rollout = home / "sessions" / "rollout-chat-b.jsonl"
                rollout.parent.mkdir(parents=True)
                # A user or tool echo is insufficient to claim delivery.
                header = json.dumps({"type": "session_meta", "payload": {"id": "chat-b"}}) + "\n"
                rollout.write_text(header + json.dumps({"type": "response_item", "payload": {
                    "type": "message", "role": "user", "content": [
                        {"type": "input_text", "text": context}]}}) + "\n", encoding="utf-8")
                accounts = [{"id": "compte-2", "codex_home": str(home)}]
                self.assertEqual(reconcile(mailbox, accounts), 0)
                self.assertEqual(len(mailbox.pending(target)), 1)
                self.assertGreater(mailbox.pending_hook_emissions()[0]["scan_offset"], 0)
                with rollout.open("a", encoding="utf-8") as output:
                    output.write(json.dumps({"type": "response_item", "payload": {
                        "type": "message", "role": "developer", "content": [
                            {"type": "input_text", "text": context}]}}) + "\n")
                self.assertEqual(reconcile(mailbox, accounts), 1)
                self.assertEqual(mailbox.pending(target), [])
                delivery = mailbox.messages(message["channel_id"])["messages"][0]["deliveries"][0]
                self.assertEqual(delivery["method"], "hook")
                self.assertIn("rollout-chat-b.jsonl", delivery["proof"])
                self.assertEqual(reconcile(mailbox, accounts), 0)
            finally:
                mailbox.close()

    def test_interrupted_cli_turn_is_not_replayed_after_restart(self):
        from workspace_cli import ManagedCodex
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "mailbox.sqlite"
            mailbox = Mailbox(db)
            try:
                mailbox.create_managed_launch("cli-interrupted", "compte-2", "general", "Revue")
                mailbox.update_managed_launch("cli-interrupted", "working", session_id="chat-cli")
            finally:
                mailbox.close()
            ManagedCodex([{"id": "compte-2", "codex_home": directory}], db)
            mailbox = Mailbox(db)
            try:
                launch = mailbox.managed_launch("cli-interrupted")
                self.assertEqual(launch["state"], "error")
                self.assertIn("résultat incertain", launch["error"])
            finally:
                mailbox.close()
