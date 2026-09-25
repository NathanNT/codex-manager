import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from alerting import AlertDispatcher
from codex_rpc import _recent_local_threads
from server import Store, poll_recent_turns_once


class TelegramGoalTests(unittest.TestCase):
    def test_goal_change_is_baselined_deduplicated_and_retried(self):
        class Notifier:
            telegram_enabled = True

            def __init__(self):
                self.deliver = False
                self.attempts = []

            def emit(self, account, message):
                pass

            def goal_changed(self, account, label, kind, previous, status, detail):
                self.attempts.append((account, label, kind, previous, status))
                return self.deliver

        notifier = Notifier()
        accounts = [{"id": "compte-1", "name": "Compte 1"}]

        def metrics(thread_id, status, updated):
            return {"threads": {"data": [{"id": thread_id, "name": "Projet Atlas",
                                           "goal": {"status": status, "updatedAt": updated,
                                                    "objective": "Long objectif privé"}}]}}

        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "monitor.sqlite")
            store = Store(database, accounts, notifier)
            store.set_metrics("compte-1", metrics("old", "complete", 1))
            store.set_metrics("compte-1", metrics("current", "active", 2))
            store.set_metrics("compte-1", metrics("current", "complete", 3))
            store.flush_goal_alerts()
            self.assertEqual(notifier.attempts, [("Compte 1", "Projet Atlas", "status", "active", "complete")])
            self.assertIsNone(store.db.execute("SELECT sent_at FROM goal_notifications").fetchone()[0])

            notifier.deliver = True
            store.flush_goal_alerts()
            self.assertEqual(len(notifier.attempts), 2)
            self.assertIsNotNone(store.db.execute("SELECT sent_at FROM goal_notifications").fetchone()[0])
            store.db.close()

            reopened = Store(database, accounts, notifier)
            reopened.set_metrics("compte-1", metrics("current", "complete", 3))
            reopened.flush_goal_alerts()
            self.assertEqual(len(notifier.attempts), 2)
            reopened.set_metrics("compte-1", metrics("current", "active", 4))
            reopened.set_metrics("compte-1", metrics("current", "complete", 5))
            reopened.flush_goal_alerts()
            self.assertEqual(len(notifier.attempts), 4)
            self.assertEqual(reopened.db.execute("SELECT count(*) FROM goal_notifications").fetchone()[0], 3)
            reopened.db.close()

    def test_telegram_only_receives_goal_changes_from_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "TELEGRAM_API_KEY": "test-token", "TELEGRAM_BOT_TOKEN": "old-token",
            "TELEGRAM_CHAT_ID": "12345", "TELEGRAM_PAIRED_CHAT_ID": "12345"
        }):
            dispatcher = AlertDispatcher(Path(directory) / "missing.json")
            self.assertTrue(dispatcher.telegram_enabled)
            self.assertEqual(dispatcher.telegram_token, "test-token")
            dispatcher.emit("Compte 1", "tâche terminée")
            self.assertFalse(dispatcher.discord_enabled)
            with patch("alerting.urllib.request.urlopen", side_effect=lambda *args, **kwargs: io.BytesIO(b'{"ok":true}')) as urlopen:
                self.assertTrue(dispatcher.goal_changed("Compte 1", "Projet Atlas", "status", "active", "complete"))
                self.assertTrue(dispatcher.goal_changed("Compte 1", "Projet Atlas", "status", "active", "blocked"))
                self.assertTrue(dispatcher.goal_changed("Compte 1", "Projet Atlas", "status", "blocked", "usageLimited"))
                self.assertTrue(dispatcher.goal_changed("Compte 1", "Projet Atlas", "status", "active", "none"))
                requests = [call.args[0] for call in urlopen.call_args_list]
            request = requests[0]
            payload = json.loads(request.data)
            self.assertEqual(payload["chat_id"], "12345")
            self.assertEqual(payload["text"], "✅ Goal terminé · Compte 1\nProjet Atlas")
            self.assertEqual(payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "goals")
            self.assertEqual(json.loads(requests[1].data)["text"], "🔴 Goal bloqué · Compte 1\nProjet Atlas")
            self.assertEqual(json.loads(requests[2].data)["text"], "🟠 Goal limité par le quota · Compte 1\nProjet Atlas")
            self.assertEqual(json.loads(requests[3].data)["text"], "⚪ Goal terminé ou supprimé · Compte 1\nProjet Atlas")
            self.assertTrue(request.full_url.endswith("/sendMessage"))

    def test_answer_notification_contains_context_but_not_reply_text(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "TELEGRAM_API_KEY": "test-token", "TELEGRAM_CHAT_ID": "12345",
            "TELEGRAM_PAIRED_CHAT_ID": "12345",
        }):
            dispatcher = AlertDispatcher(Path(directory) / "missing.json")
            with patch("alerting.urllib.request.urlopen", return_value=io.BytesIO(b'{"ok":true}')) as urlopen:
                self.assertTrue(dispatcher.answer_ready("Compte 2", "Codex Supervision", "Projet A",
                                                        "compte-2", "123e4567-e89b-42d3-a456-426614174000"))
            payload = json.loads(urlopen.call_args.args[0].data)
            self.assertEqual(payload["text"], "✅ Réponse prête · Compte 2\nCodex Supervision\n🪟 Projet A")
            self.assertEqual(payload["chat_id"], "12345")
            self.assertEqual(payload["reply_markup"]["inline_keyboard"][0][0], {
                "text": "Afficher la réponse",
                "callback_data": "chat:compte-2:123e4567-e89b-42d3-a456-426614174000"})

    def test_metric_warning_uses_paired_private_chat(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "TELEGRAM_API_KEY": "test-token", "TELEGRAM_CHAT_ID": "12345",
            "TELEGRAM_PAIRED_CHAT_ID": "12345",
        }):
            dispatcher = AlertDispatcher(Path(directory) / "missing.json")
            with patch("alerting.urllib.request.urlopen", side_effect=lambda *args, **kwargs: io.BytesIO(b'{"ok":true}')) as urlopen:
                self.assertTrue(dispatcher.metric_warning("Compte 1", "quota_primary", "quota principal disponible : 15 %", 20))
                self.assertTrue(dispatcher.metric_warning("Compte 2", "tokens_daily", "Tokens aujourd'hui : 120 000", 100000))
                self.assertTrue(dispatcher.action_needed("Compte 1", "approval", "Projet A", "Fenêtre A"))
            payloads = [json.loads(call.args[0].data) for call in urlopen.call_args_list]
            self.assertEqual([payload["chat_id"] for payload in payloads], ["12345"] * 3)
            self.assertIn("Seuil : 20 % restants", payloads[0]["text"])
            self.assertIn("100 000 tokens", payloads[1]["text"])
            self.assertIn("Validation requise", payloads[2]["text"])

    def test_answer_hook_and_completed_poll_notify_once_with_retry(self):
        class Notifier:
            telegram_enabled = True

            def __init__(self):
                self.deliver = False
                self.calls = []

            def emit(self, account, message):
                pass

            def answer_ready(self, account, label, window, account_id, thread_id):
                self.calls.append((account, label, window, account_id, thread_id))
                return self.deliver

        notifier = Notifier()
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / "monitor.sqlite"),
                          [{"id": "compte-1", "name": "Compte 1"}], notifier)
            store.heartbeat({"account_id": "compte-1", "window_id": "w1",
                             "label": "Projet Atlas", "workspace": "C:/Projet-A"})
            store.set_metrics("compte-1", {"threads": {"data": [{"id": "chat-simple",
                "name": "Discussion Atlas", "goal": None}]}})
            store.event({"account_id": "compte-1", "hook_event_name": "Stop",
                         "session_id": "chat-simple", "turn_id": "t1", "cwd": "C:/Projet-A",
                         "last_assistant_message": "Contenu privé de la réponse"})
            store.reconcile_turns("compte-1", [{"thread_id": "chat-simple", "turn_id": "t1",
                                                "status": "completed", "cwd": "C:/Projet-A",
                                                "started_at": store.started_at,
                                                "completed_at": store.started_at + 1}])
            self.assertEqual(store.db.execute("SELECT count(*) FROM answer_notifications").fetchone()[0], 1)
            store.flush_answer_alerts()
            self.assertEqual(notifier.calls, [("Compte 1", "Discussion Atlas", "Projet Atlas", "compte-1", "chat-simple")])
            self.assertIsNone(store.db.execute("SELECT sent_at FROM answer_notifications").fetchone()[0])
            notifier.deliver = True
            store.flush_answer_alerts()
            self.assertIsNotNone(store.db.execute("SELECT sent_at FROM answer_notifications").fetchone()[0])
            store.flush_answer_alerts()
            self.assertEqual(len(notifier.calls), 2)
            store.db.close()

    def test_completed_poll_notifies_only_new_turns_without_hook(self):
        class Notifier:
            telegram_enabled = True

            def answer_ready(self, account, label, window, account_id, thread_id):
                return True

        store = Store(":memory:", [{"id": "compte-1", "name": "Compte 1"}], Notifier())
        store.reconcile_turns("compte-1", [{"thread_id": "old", "turn_id": "t1", "status": "completed",
                                            "completed_at": store.started_at - 100}])
        store.reconcile_turns("compte-1", [{"thread_id": "new", "turn_id": "t2", "status": "completed",
                                            "completed_at": store.started_at + 1}])
        self.assertEqual(store.db.execute("SELECT thread_id FROM answer_notifications").fetchone()[0], "new")

    def test_goal_replies_wait_for_goal_changes_while_simple_chat_still_notifies(self):
        class Notifier:
            telegram_enabled = True

            def __init__(self):
                self.answers = []
                self.goals = []

            def emit(self, account, message):
                pass

            def answer_ready(self, account, label, window, account_id, thread_id):
                self.answers.append(thread_id)
                return True

            def goal_changed(self, account, label, kind, previous, status, detail):
                self.goals.append((previous, status))
                return True

        notifier = Notifier()
        store = Store(":memory:", [{"id": "compte-1", "name": "Compte 1"}], notifier)

        def metrics(status):
            return {"threads": {"data": [{"id": "goal-chat", "name": "Projet Atlas",
                                            "goal": {"status": status, "updatedAt": time.time(),
                                                     "objective": "Travail long"}},
                                           {"id": "simple-chat", "name": "Chat simple", "goal": None}]}}

        store.set_metrics("compte-1", metrics("active"))
        for turn in ("step-1", "step-2"):
            store.event({"account_id": "compte-1", "hook_event_name": "Stop",
                         "session_id": "goal-chat", "turn_id": turn})
        store.flush_answer_alerts()
        self.assertEqual(notifier.answers, [])
        store.event({"account_id": "compte-1", "hook_event_name": "Stop",
                     "session_id": "simple-chat", "turn_id": "simple-1"})
        store.flush_answer_alerts()
        self.assertEqual(notifier.answers, ["simple-chat"])
        store.event({"account_id": "compte-1", "hook_event_name": "Stop",
                     "session_id": "goal-chat", "turn_id": "last-step"})
        store.set_metrics("compte-1", metrics("complete"))
        store.flush_answer_alerts()
        store.flush_goal_alerts()
        self.assertEqual(notifier.answers, ["simple-chat"])
        self.assertEqual(notifier.goals, [("active", "complete")])
        store.db.close()

    def test_goal_starting_between_polls_suppresses_queued_answer(self):
        class Notifier:
            telegram_enabled = True

            def __init__(self):
                self.answers = []

            def emit(self, account, message):
                pass

            def answer_ready(self, account, label, window, account_id, thread_id):
                self.answers.append(thread_id)
                return True

        with tempfile.TemporaryDirectory() as directory:
            with closing(sqlite3.connect(Path(directory) / "state_5.sqlite")) as db:
                db.execute("CREATE TABLE threads (id TEXT, source TEXT)")
                db.execute("INSERT INTO threads VALUES ('chat', 'vscode')")
                db.commit()
            notifier = Notifier()
            store = Store(":memory:", [{"id": "compte-1", "name": "Compte 1",
                                         "codex_home": directory}], notifier)
            store.event({"account_id": "compte-1", "hook_event_name": "Stop",
                         "session_id": "chat", "turn_id": "first"})
            with patch("server.read_thread_goal", return_value={"status": "active"}):
                store.flush_answer_alerts()
            self.assertEqual(notifier.answers, [])
            self.assertEqual(store.db.execute(
                "SELECT count(*) FROM answer_notifications WHERE sent_at IS NULL").fetchone()[0], 0)
            store.event({"account_id": "compte-1", "hook_event_name": "Stop",
                         "session_id": "chat", "turn_id": "second"})
            with patch("server.read_thread_goal", return_value=None):
                store.flush_answer_alerts()
            self.assertEqual(notifier.answers, ["chat"])
            store.db.close()

    def test_subagent_completion_never_sends_answer_alert(self):
        class Notifier:
            telegram_enabled = True

            def __init__(self):
                self.answers = []
                self.events = []

            def emit(self, account, message):
                self.events.append((account, message))

            def answer_ready(self, account, label, window, account_id, thread_id):
                self.answers.append(thread_id)
                return True

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with closing(sqlite3.connect(home / "state_5.sqlite")) as db:
                db.execute("CREATE TABLE threads (id TEXT, source TEXT, name TEXT, cwd TEXT, updated_at INTEGER)")
                db.executemany("INSERT INTO threads VALUES (?,?,?,?,?)", [
                    ("parent", "vscode", "Parent", "C:/Projet-A", int(time.time())),
                    ("child", '{"subagent":{"thread_spawn":{"parent_thread_id":"parent"}}}',
                     "Child", "C:/Projet-A", int(time.time())),
                ])
                db.commit()
            self.assertEqual([thread["id"] for thread in _recent_local_threads(home)], ["parent"])
            notifier = Notifier()
            store = Store(":memory:", [{"id": "compte-1", "name": "Compte 1",
                                         "codex_home": directory}], notifier)
            store.event({"account_id": "compte-1", "hook_event_name": "Stop",
                         "session_id": "child", "turn_id": "child-turn"})
            store.reconcile_turns("compte-1", [{"thread_id": "child", "turn_id": "child-turn",
                                                "status": "completed", "completed_at": store.started_at + 1}])
            self.assertEqual(store.db.execute("SELECT count(*) FROM answer_notifications").fetchone()[0], 0)
            self.assertEqual(notifier.events, [])
            store.event({"account_id": "compte-1", "hook_event_name": "Stop",
                         "session_id": "parent", "turn_id": "parent-turn"})
            # Previously queued alerts are also filtered before delivery.
            store.db.execute("""INSERT INTO answer_notifications
                (account_id,thread_id,turn_id,label,completed_at,created_at)
                VALUES (?,?,?,?,?,?)""",
                ("compte-1", "child", "old-child-turn", "Child", time.time(), time.time()))
            store.db.commit()
            with patch("server.read_thread_goal", return_value=None):
                store.flush_answer_alerts()
            self.assertEqual(notifier.answers, ["parent"])
            self.assertEqual(store.db.execute(
                "SELECT count(*) FROM answer_notifications WHERE sent_at IS NULL").fetchone()[0], 0)
            self.assertEqual(notifier.events, [("compte-1", "tâche terminée")])
            store.db.close()

    def test_quick_poll_discovers_new_chat_and_checks_each_local_update_once(self):
        class Notifier:
            telegram_enabled = True

        with tempfile.TemporaryDirectory() as directory:
            with closing(sqlite3.connect(Path(directory) / "state_5.sqlite")) as db:
                db.execute("CREATE TABLE threads (id TEXT, source TEXT, name TEXT, cwd TEXT, updated_at INTEGER)")
                db.execute("INSERT INTO threads VALUES (?,?,?,?,?)",
                           ("new-chat", "vscode", "", "C:/Projet-A", int(time.time())))
                db.commit()
            store = Store(":memory:", [{"id": "compte-1", "name": "Compte 1",
                                         "codex_home": directory}], Notifier())
            checked = {}
            local = [{"id": "new-chat", "name": "", "cwd": "C:/Projet-A",
                      "updated_at": store.started_at + 1}]
            turns = [{"thread_id": "new-chat", "turn_id": "t1", "status": "completed",
                      "cwd": "C:/Projet-A", "completed_at": store.started_at + 1}]
            with patch("server._recent_local_threads", return_value=local), \
                 patch("server.read_recent_turns", return_value=turns) as read:
                poll_recent_turns_once(store, checked)
                poll_recent_turns_once(store, checked)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(store.db.execute("SELECT count(*) FROM answer_notifications").fetchone()[0], 1)

    def test_quick_poll_retries_provisional_turn_until_reply_is_complete(self):
        class Notifier:
            telegram_enabled = True

        with tempfile.TemporaryDirectory() as directory:
            with closing(sqlite3.connect(Path(directory) / "state_5.sqlite")) as db:
                db.execute("CREATE TABLE threads (id TEXT, source TEXT, name TEXT, cwd TEXT, updated_at INTEGER)")
                db.execute("INSERT INTO threads VALUES (?,?,?,?,?)",
                           ("chat", "vscode", "Chat simple", "C:/Projet-A", int(time.time())))
                db.commit()
            store = Store(":memory:", [{"id": "compte-1", "name": "Compte 1",
                                         "codex_home": directory}], Notifier())
            checked = {}
            local = [{"id": "chat", "name": "Chat simple", "cwd": "C:/Projet-A",
                      "updated_at": store.started_at + 1}]
            provisional = {"thread_id": "chat", "turn_id": "t1", "status": "interrupted",
                           "started_at": store.started_at, "completed_at": None, "cwd": "C:/Projet-A"}
            complete = {**provisional, "status": "completed", "completed_at": store.started_at + 2}
            with patch("server._recent_local_threads", return_value=local), \
                 patch("server.read_recent_turns", side_effect=[[provisional], [complete]]) as read:
                poll_recent_turns_once(store, checked)
                poll_recent_turns_once(store, checked)
            self.assertEqual(read.call_count, 2)
            self.assertEqual(store.db.execute("SELECT count(*) FROM answer_notifications").fetchone()[0], 1)

    def test_telegram_refuses_unpaired_or_group_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"
            for chat_id, paired in (("12345", ""), ("12345", "99999"),
                                    ("-100123", "-100123"), ("not-an-id", "not-an-id")):
                with self.subTest(chat_id=chat_id, paired=paired), patch.dict(os.environ, {
                    "TELEGRAM_API_KEY": "test-token", "TELEGRAM_CHAT_ID": chat_id,
                    "TELEGRAM_PAIRED_CHAT_ID": paired,
                }):
                    self.assertFalse(AlertDispatcher(path).telegram_enabled)

    def test_new_goal_completed_between_polls_is_not_missed(self):
        class Notifier:
            telegram_enabled = True

            def emit(self, account, message):
                pass

            def goal_changed(self, account, label, kind, previous, status, detail):
                return True

        store = Store(":memory:", [{"id": "compte-1", "name": "Compte 1"}], Notifier())
        store.set_metrics("compte-1", {"threads": {"data": []}})
        previous_poll = store.db.execute("SELECT payload FROM metrics").fetchone()[0]
        previous_poll_at = json.loads(previous_poll)["last_success_at"]
        store.set_metrics("compte-1", {"threads": {"data": [{"id": "new-thread", "name": "Nouvelle tâche",
            "goal": {"status": "complete", "updatedAt": previous_poll_at + 0.1}}]}})
        self.assertEqual(store.db.execute("SELECT count(*) FROM goal_notifications").fetchone()[0], 1)
        store.flush_goal_alerts()
        self.assertIsNotNone(store.db.execute("SELECT sent_at FROM goal_notifications").fetchone()[0])

    def test_every_meaningful_goal_change_is_queued(self):
        class Notifier:
            telegram_enabled = True

            def emit(self, account, message):
                pass

        store = Store(":memory:", [{"id": "compte-1", "name": "Compte 1"}], Notifier())

        def observe(status, objective="Objectif A", budget=None, tokens=0):
            goal = None if status == "none" else {"status": status, "objective": objective,
                                                   "tokenBudget": budget, "tokensUsed": tokens}
            store.set_metrics("compte-1", {"threads": {"data": [{"id": "thread", "name": "Projet Atlas",
                                                              "goal": goal}]}})

        observe("active")  # Existing work is a quiet baseline.
        observe("active", tokens=1000)  # Usage updates do not change the goal lifecycle.
        for status in ("paused", "blocked", "usageLimited", "budgetLimited", "active"):
            observe(status)
        observe("active", objective="Objectif B")
        observe("active", objective="Objectif B", budget=5000)
        observe("complete", objective="Objectif B", budget=5000)
        observe("none")
        observe("none")

        rows = store.db.execute("SELECT kind, previous_status, status, detail FROM goal_notifications ORDER BY id").fetchall()
        self.assertEqual([(row["kind"], row["status"]) for row in rows], [
            ("status", "paused"), ("status", "blocked"), ("status", "usageLimited"),
            ("status", "budgetLimited"), ("status", "active"), ("objective", "active"),
            ("budget", "active"), ("status", "complete"), ("status", "none"),
        ])
        self.assertEqual(rows[6]["detail"], "Nouveau budget : 5 000 tokens")
        self.assertEqual(rows[5]["detail"], "Nouvel objectif : Objectif B")

    def test_tracked_goal_outside_recent_list_and_legacy_pending_alert(self):
        class Notifier:
            telegram_enabled = True

            def __init__(self):
                self.messages = []

            def emit(self, account, message):
                pass

            def goal_changed(self, account, label, kind, previous, status, detail):
                self.messages.append((kind, previous, status, label))
                return True

        notifier = Notifier()
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "monitor.sqlite")
            old = sqlite3.connect(database)
            old.execute("""CREATE TABLE goal_states (
                account_id TEXT NOT NULL, thread_id TEXT NOT NULL, status TEXT NOT NULL,
                updated_at REAL NOT NULL, PRIMARY KEY(account_id, thread_id))""")
            old.execute("INSERT INTO goal_states VALUES ('compte-1', 'older-thread', 'active', 10)")
            old.execute("""CREATE TABLE goal_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                thread_id TEXT NOT NULL, goal_updated_at REAL NOT NULL,
                label TEXT NOT NULL, created_at REAL NOT NULL, sent_at REAL,
                UNIQUE(account_id, thread_id, goal_updated_at))""")
            old.execute("INSERT INTO goal_alerts(account_id,thread_id,goal_updated_at,label,created_at) VALUES (?,?,?,?,?)",
                        ("compte-1", "legacy", 11, "Ancien goal", 11))
            old.commit()
            old.close()

            store = Store(database, [{"id": "compte-1", "name": "Compte 1"}], notifier)
            self.assertEqual(store.tracked_goal_threads("compte-1"), ("older-thread",))
            store.set_metrics("compte-1", {"threads": {"data": []}, "tracked_goals": [
                {"id": "older-thread", "goal": {"status": "blocked", "objective": "Objectif ancien"}}]})
            store.flush_goal_alerts()
            self.assertEqual(notifier.messages, [
                ("status", "active", "complete", "Ancien goal"),
                ("status", "active", "blocked", "Objectif ancien"),
            ])
            store.db.close()


if __name__ == "__main__":
    unittest.main()
