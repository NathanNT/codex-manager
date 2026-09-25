import unittest
import time
import sqlite3
import tempfile
from pathlib import Path

from server import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:", [{"id": "compte-1", "name": "Compte 1"}])

    def test_hook_lifecycle_and_unique_window(self):
        self.store.heartbeat({"account_id": "compte-1", "window_id": "w1", "label": "Projet A", "workspace": "C:/Projet-A"})
        self.store.event({"account_id": "compte-1", "hook_event_name": "UserPromptSubmit", "session_id": "s1", "turn_id": "t1", "cwd": "C:/Projet-A", "prompt": "Corriger un test"})
        task = self.store.snapshot()["accounts"][0]["tasks"][0]
        self.assertEqual(task["status"], "active")
        self.assertEqual(task["window_id"], "w1")
        self.store.event({"account_id": "compte-1", "hook_event_name": "PermissionRequest", "session_id": "s1", "turn_id": "t1", "cwd": "C:/Projet-A"})
        self.assertEqual(self.store.snapshot()["accounts"][0]["tasks"][0]["status"], "approval")
        self.store.event({"account_id": "compte-1", "hook_event_name": "PostToolUse", "session_id": "s1", "turn_id": "t1", "cwd": "C:/Projet-A"})
        self.assertEqual(self.store.snapshot()["accounts"][0]["tasks"][0]["status"], "active")
        self.store.event({"account_id": "compte-1", "hook_event_name": "Stop", "session_id": "s1", "turn_id": "t1", "cwd": "C:/Projet-A"})
        self.assertEqual(self.store.snapshot()["accounts"][0]["tasks"][0]["status"], "completed")

    def test_tool_event_can_discover_turn_after_hooks_are_approved(self):
        self.store.event({"account_id": "compte-1", "hook_event_name": "PostToolUse",
                          "session_id": "s1", "turn_id": "t1", "cwd": "C:/Projet-A"})
        task = self.store.snapshot()["accounts"][0]["tasks"][0]
        self.assertEqual(task["status"], "active")
        self.assertEqual(task["source"], "hook")

    def test_old_working_signal_becomes_unconfirmed_before_stalled(self):
        self.store.heartbeat({"account_id": "compte-1", "window_id": "w1", "workspace": "C:/Projet-A"})
        self.store.event({"account_id": "compte-1", "hook_event_name": "UserPromptSubmit",
                          "session_id": "s1", "turn_id": "t1", "cwd": "C:/Projet-A"})
        with self.store.lock:
            self.store.db.execute("UPDATE tasks SET updated_at=? WHERE session_id='s1'", (time.time() - 181,))
            self.store.db.commit()
        self.assertEqual(self.store.snapshot()["accounts"][0]["tasks"][0]["display_status"], "unconfirmed")
        with self.store.lock:
            self.store.db.execute("UPDATE tasks SET updated_at=? WHERE session_id='s1'", (time.time() - 1201,))
            self.store.db.commit()
        self.assertEqual(self.store.snapshot()["accounts"][0]["tasks"][0]["display_status"], "stalled")

    def test_project_usage_is_available_in_dashboard_snapshot(self):
        created = time.time() - 3600
        counter = {"id": "chat-project", "cwd": "C:/Projets/Atlas", "tokens": 120,
                   "created_at": created, "updated_at": time.time()}
        self.assertEqual(self.store.observe_project_usage("compte-1", [counter]), 0)
        self.assertEqual(self.store.observe_project_usage("compte-1", [{**counter, "tokens": 180}]), 60)
        usage = self.store.snapshot()["project_usage"]["compte-1"]
        self.assertEqual(usage["today"], 60)
        self.assertEqual(usage["projects"][0]["chats"], 1)

    def test_two_windows_same_workspace_are_ambiguous(self):
        for window_id in ("w1", "w2"):
            self.store.heartbeat({"account_id": "compte-1", "window_id": window_id, "workspace": "C:/Projet-A"})
        self.store.event({"account_id": "compte-1", "hook_event_name": "UserPromptSubmit", "session_id": "s1", "turn_id": "t1", "cwd": "C:/Projet-A"})
        task = self.store.snapshot()["accounts"][0]["tasks"][0]
        self.assertIsNone(task["window_id"])
        self.assertEqual(task["window_match"], "ambiguous")

    def test_multi_root_window_matches_subfolder(self):
        self.store.heartbeat({"account_id": "compte-1", "window_id": "w1", "workspace": "C:/Projet-A",
                              "workspace_folders": ["C:/Projet-A", "C:/Projet-B"]})
        self.store.event({"account_id": "compte-1", "hook_event_name": "UserPromptSubmit", "session_id": "s1", "turn_id": "t1", "cwd": "C:/Projet-B/service"})
        self.assertEqual(self.store.snapshot()["accounts"][0]["tasks"][0]["window_id"], "w1")

    def test_unknown_account_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.heartbeat({"account_id": "other", "window_id": "w1"})

    def test_failed_poll_retains_last_good_metrics(self):
        self.store.set_metrics("compte-1", {"usage": {"summary": {"lifetimeTokens": 1200}}})
        self.store.set_metrics("compte-1", {"collector_error": "Réseau indisponible"})
        metrics = self.store.snapshot()["accounts"][0]["metrics"]
        self.assertEqual(metrics["usage"]["summary"]["lifetimeTokens"], 1200)
        self.assertEqual(metrics["collector_error"], "Réseau indisponible")

    def test_alerts_are_sent_for_approval_and_quota_crossing_once(self):
        class Notifier:
            def __init__(self):
                self.messages = []

            def emit(self, account, message):
                self.messages.append((account, message))

        notifier = Notifier()
        store = Store(":memory:", [{"id": "compte-1", "name": "Compte 1"}], notifier)
        store.set_metrics("compte-1", {"limits": {"rateLimits": {"primary": {"usedPercent": 79}}}})
        store.set_metrics("compte-1", {"limits": {"rateLimits": {"primary": {"usedPercent": 85}}}})
        store.set_metrics("compte-1", {"limits": {"rateLimits": {"primary": {"usedPercent": 86}}}})
        store.event({"account_id": "compte-1", "hook_event_name": "PermissionRequest", "session_id": "s1", "turn_id": "t1"})
        self.assertEqual(len(notifier.messages), 2)
        self.assertIn("quota", notifier.messages[0][1])
        self.assertIn("validation", notifier.messages[1][1])

    def test_official_turn_history_closes_task_and_reports_failure(self):
        self.store.event({"account_id": "compte-1", "hook_event_name": "UserPromptSubmit",
                          "session_id": "s1", "turn_id": "t1", "cwd": "C:/Projet-A", "prompt": "Corriger le build"})
        self.store.reconcile_turns("compte-1", [{"thread_id": "s1", "turn_id": "t1", "status": "failed",
                                               "title": "Ancien titre", "cwd": "C:/Projet-A",
                                               "started_at": 100, "completed_at": 140,
                                               "error_message": "Erreur réseau"}])
        task = self.store.snapshot()["accounts"][0]["tasks"][0]
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["title"], "Corriger le build")
        self.assertEqual(task["error_message"], "Erreur réseau")
        self.assertEqual(sum(event["type"] == "failed" for event in self.store.snapshot()["events"]), 1)
        self.store.reconcile_turns("compte-1", [{"thread_id": "s1", "turn_id": "t1", "status": "failed", "completed_at": 140}])
        self.assertEqual(sum(event["type"] == "failed" for event in self.store.snapshot()["events"]), 1)

    def test_interrupted_turn_without_end_time_has_stable_history(self):
        turn = {"thread_id": "s1", "turn_id": "t1", "status": "interrupted",
                "started_at": 100, "completed_at": None, "thread_updated_at": 145}
        self.store.reconcile_turns("compte-1", [turn])
        first = self.store.snapshot()["accounts"][0]["tasks"][0]
        self.assertIsNone(first["ended_at"])
        self.assertEqual(first["updated_at"], 100)
        self.store.reconcile_turns("compte-1", [turn])
        second = self.store.snapshot()["accounts"][0]["tasks"][0]
        self.assertEqual(second["updated_at"], 100)

    def test_recent_chat_without_goal_is_visible_until_its_turn_finishes(self):
        stamp = time.time()
        self.store.heartbeat({"account_id": "compte-1", "window_id": "w1", "workspace": "C:/Projet-A"})
        self.store.set_metrics("compte-1", {
            "threads": {"data": [{"id": "chat-simple", "name": "Chat simple", "cwd": "C:/Projet-A",
                                  "updatedAt": stamp, "status": {"type": "notLoaded"}, "goal": None}]},
            "session_writes": {"chat-simple": stamp},
        })
        task = self.store.snapshot()["accounts"][0]["tasks"][0]
        self.assertEqual((task["display_status"], task["window_id"], task["source"]),
                         ("active", "w1", "recent_activity"))
        self.store.reconcile_turns("compte-1", [{"thread_id": "chat-simple", "turn_id": "t1",
                                                 "status": "completed", "cwd": "C:/Projet-A",
                                                 "started_at": stamp - 5, "completed_at": stamp}])
        tasks = self.store.snapshot()["accounts"][0]["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["display_status"], "completed")

    def test_new_activity_supersedes_an_older_interrupted_turn(self):
        stamp = time.time()
        self.store.heartbeat({"account_id": "compte-1", "window_id": "w1", "workspace": "C:/Projet-A"})
        self.store.reconcile_turns("compte-1", [{"thread_id": "chat-simple", "turn_id": "t1",
                                                 "status": "interrupted", "cwd": "C:/Projet-A",
                                                 "started_at": stamp - 350,
                                                 "thread_updated_at": stamp - 300}])
        self.store.set_metrics("compte-1", {
            "threads": {"data": [{"id": "chat-simple", "name": "Chat simple", "cwd": "C:/Projet-A",
                                  "updatedAt": stamp, "goal": None}]},
            "session_writes": {"chat-simple": stamp},
        })
        tasks = self.store.snapshot()["accounts"][0]["tasks"]
        self.assertEqual(tasks[0]["display_status"], "active")
        self.assertEqual(tasks[0]["source"], "recent_activity")

    def test_new_activity_supersedes_a_stale_active_hook(self):
        stamp = time.time()
        self.store.heartbeat({"account_id": "compte-1", "window_id": "w1", "workspace": "C:/Projet-A"})
        self.store.event({"account_id": "compte-1", "hook_event_name": "UserPromptSubmit",
                          "session_id": "chat-simple", "turn_id": "t1", "cwd": "C:/Projet-A"})
        with self.store.lock:
            self.store.db.execute("UPDATE tasks SET updated_at=? WHERE session_id=?",
                                  (stamp - 400, "chat-simple"))
            self.store.db.commit()
        self.store.set_metrics("compte-1", {
            "threads": {"data": [{"id": "chat-simple", "name": "Chat simple", "cwd": "C:/Projet-A",
                                  "updatedAt": stamp, "goal": None}]},
            "session_writes": {"chat-simple": stamp},
        })
        tasks = self.store.snapshot()["accounts"][0]["tasks"]
        self.assertEqual(tasks[0]["source"], "recent_activity")
        self.assertEqual(tasks[0]["display_status"], "active")

    def test_local_state_detects_running_chat_without_goal_when_app_server_is_stale(self):
        stamp = time.time()
        self.store.heartbeat({"account_id": "compte-1", "window_id": "w1", "workspace": "C:/Projet-A"})
        self.store.reconcile_turns("compte-1", [{"thread_id": "chat-simple", "turn_id": "t1",
                                                 "status": "interrupted", "cwd": "C:/Projet-A",
                                                 "started_at": stamp - 60, "completed_at": None}])
        self.store.set_metrics("compte-1", {
            "threads": {"data": [{"id": "chat-simple", "name": "Chat simple", "cwd": "C:/Projet-A",
                                  "updatedAt": stamp - 3600, "status": {"type": "notLoaded"},
                                  "goal": None}]},
            "thread_updates": {"chat-simple": stamp},
        })
        tasks = self.store.snapshot()["accounts"][0]["tasks"]
        self.assertEqual(tasks[0]["source"], "recent_activity")
        self.assertEqual((tasks[0]["display_status"], tasks[0]["window_id"]), ("active", "w1"))

    def test_snapshot_reads_new_local_activity_without_waiting_for_account_poll(self):
        stamp = int(time.time())
        with tempfile.TemporaryDirectory() as home:
            self.store.accounts[0]["codex_home"] = home
            connection = sqlite3.connect(Path(home) / "state_5.sqlite")
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, updated_at INTEGER, source TEXT)")
            connection.execute("INSERT INTO threads VALUES (?, ?, ?)", ("chat-simple", stamp - 60, "vscode"))
            connection.commit()
            connection.close()
            self.store.heartbeat({"account_id": "compte-1", "window_id": "w1", "workspace": "C:/Projet-A"})
            self.store.reconcile_turns("compte-1", [{"thread_id": "chat-simple", "turn_id": "t1",
                                                     "status": "interrupted", "cwd": "C:/Projet-A",
                                                     "started_at": stamp - 10, "completed_at": None}])
            self.store.set_metrics("compte-1", {
                "threads": {"data": [{"id": "chat-simple", "name": "Chat simple", "cwd": "C:/Projet-A",
                                      "updatedAt": stamp - 3600, "goal": None}]},
            })
            self.assertEqual(self.store.snapshot()["accounts"][0]["tasks"][0]["display_status"], "interrupted")
            connection = sqlite3.connect(Path(home) / "state_5.sqlite")
            try:
                connection.execute("UPDATE threads SET updated_at=? WHERE id=?", (stamp - 8, "chat-simple"))
                connection.commit()
            finally:
                connection.close()
            task = self.store.snapshot()["accounts"][0]["tasks"][0]
            self.assertEqual((task["display_status"], task["source"]), ("active", "recent_activity"))


if __name__ == "__main__":
    unittest.main()
