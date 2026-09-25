import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from monitoring_policy import default_preferences, quiet_now, quota_outlook, quota_projection
from server import Store
from telegram_commands import render


ACCOUNTS = [{"id": "compte-1", "name": "Compte 1"}, {"id": "compte-2", "name": "Compte 2"}]


class Notifier:
    telegram_enabled = True

    def __init__(self):
        self.messages = []
        self.actions = []
        self.fail = False

    def emit(self, account, message):
        pass

    def metric_warning(self, account, metric, label, threshold):
        if self.fail:
            return False
        self.messages.append((account, metric, threshold))
        return True

    def action_needed(self, account, kind, label, window):
        self.actions.append((account, kind, label, window))
        return True


def metrics(used, daily, old=10000, secondary=None):
    today = date.today()
    rates = {"primary": {"usedPercent": used, "windowDurationMins": 300,
                         "resetsAt": 2000000000}}
    if secondary is not None:
        rates["secondary"] = {"usedPercent": secondary, "windowDurationMins": 10080,
                              "resetsAt": 2000000001}
    return {"limits": {"rateLimits": rates}, "usage": {"dailyUsageBuckets": [
        {"startDate": today.isoformat(), "tokens": daily},
        {"startDate": (today - timedelta(days=1)).isoformat(), "tokens": old}]},
        "threads": {"data": []}}


class MonitoringPreferencesTests(unittest.TestCase):
    def test_thresholds_per_account_deduplicate_and_retry(self):
        notifier = Notifier()
        store = Store(":memory:", ACCOUNTS, notifier)
        settings = default_preferences(ACCOUNTS)
        settings["accounts"]["compte-1"].update({"quota_remaining_percent": 25,
                                                 "tokens_daily": 20000,
                                                 "tokens_weekly": 30000})
        settings["accounts"]["compte-2"]["notify_tokens"] = False
        store.save_preferences(settings)
        for account in ACCOUNTS:
            store.set_metrics(account["id"], metrics(70, 18000, secondary=70))
        store.set_metrics("compte-1", metrics(80, 22000, secondary=80))
        store.set_metrics("compte-1", metrics(82, 25000, secondary=82))
        store.set_metrics("compte-2", metrics(82, 25000, secondary=82))
        pending = store.db.execute("SELECT account_id,metric FROM metric_notifications ORDER BY id").fetchall()
        self.assertEqual([(row[0], row[1]) for row in pending], [
            ("compte-1", "quota_primary"), ("compte-1", "quota_secondary"),
            ("compte-1", "tokens_daily"), ("compte-1", "tokens_weekly"),
            ("compte-2", "quota_primary"), ("compte-2", "quota_secondary")])
        notifier.fail = True
        store.flush_metric_alerts()
        self.assertEqual(notifier.messages, [])
        notifier.fail = False
        store.flush_metric_alerts()
        store.flush_metric_alerts()
        self.assertEqual(len(notifier.messages), 6)

    def test_preferences_persist_and_validation_rejects_partial_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "monitor.sqlite")
            store = Store(path, ACCOUNTS)
            settings = store.preferences()
            settings["accounts"]["compte-2"]["tokens_daily"] = 12345
            store.save_preferences(settings)
            with self.assertRaises(ValueError):
                store.save_preferences({"accounts": {}})
            settings["accounts"]["compte-2"]["tokens_daily"] = True
            with self.assertRaises(ValueError):
                store.save_preferences(settings)
            store.db.close()
            reopened = Store(path, ACCOUNTS)
            self.assertEqual(reopened.preferences()["accounts"]["compte-2"]["tokens_daily"], 12345)
            reopened.db.close()

    def test_quiet_hours_defer_delivery(self):
        notifier = Notifier()
        store = Store(":memory:", ACCOUNTS, notifier)
        store.set_metrics("compte-1", metrics(70, 18000))
        store.set_metrics("compte-1", metrics(85, 18000))
        with patch("server.quiet_now", return_value=True):
            store.flush_metric_alerts()
        self.assertEqual(notifier.messages, [])
        store.flush_metric_alerts()
        self.assertEqual(len(notifier.messages), 1)
        self.assertTrue(quiet_now({"enabled": True, "start": "22:00", "end": "08:00"},
                                  datetime(2026, 9, 23, 23, 0)))

    def test_obsolete_quota_alert_is_not_sent_after_reset_or_setting_change(self):
        notifier = Notifier()
        store = Store(":memory:", ACCOUNTS, notifier)
        store.set_metrics("compte-1", metrics(70, 18000))
        store.set_metrics("compte-1", metrics(85, 18000))
        settings = store.preferences()
        settings["accounts"]["compte-1"]["quota_remaining_percent"] = 10
        store.save_preferences(settings)
        store.flush_metric_alerts()
        self.assertEqual(notifier.messages, [])
        self.assertIsNotNone(store.db.execute("SELECT sent_at FROM metric_notifications").fetchone()[0])

    def test_approval_and_failure_are_immediate_and_deduplicated(self):
        notifier = Notifier()
        store = Store(":memory:", ACCOUNTS, notifier)
        store.heartbeat({"account_id": "compte-1", "window_id": "w1", "label": "Projet A",
                         "workspace": "C:/project"})
        payload = {"account_id": "compte-1", "hook_event_name": "PermissionRequest",
                   "session_id": "s1", "turn_id": "t1", "cwd": "C:/project"}
        store.event(payload)
        store.event(payload)
        with patch("server.quiet_now", return_value=True):
            store.flush_action_alerts()
            store.flush_action_alerts()
        self.assertEqual(notifier.actions, [("Compte 1", "approval", "Tâche Codex", "Projet A")])
        store.reconcile_turns("compte-1", [{"thread_id": "s1", "turn_id": "t1",
                                            "status": "failed", "cwd": "C:/project",
                                            "completed_at": time.time(), "title": "Projet A"}])
        store.flush_action_alerts()
        self.assertEqual([item[1] for item in notifier.actions], ["approval", "failed"])

    def test_projection_needs_enough_signal_and_finishes_before_reset(self):
        self.assertIsNone(quota_projection([(0, 20), (300, 30)], 5000, 600))
        self.assertEqual(quota_projection([(100, 20), (1000, 50)], 3000, 1000), 2500)
        self.assertIsNone(quota_projection([(100, 20), (1000, 50)], 2000, 1000))
        self.assertEqual(quota_outlook([(0, 20), (300, 30)], 5000, 600)["status"], "insufficient")
        self.assertEqual(quota_outlook([(100, 20), (1000, 50)], 3000, 1000),
                         {"status": "risk", "at": 2500, "reset_at": 3000})
        self.assertEqual(quota_outlook([(100, 20), (1000, 50)], 2000, 1000),
                         {"status": "safe", "reset_at": 2000})
        self.assertEqual(quota_outlook([(100, 29), (1000, 29)], 2000, 1000),
                         {"status": "safe", "reset_at": 2000})
        self.assertEqual(quota_outlook([(100, 29), (1000, 29.5)], 2000, 1000)["status"],
                         "insufficient")
        self.assertEqual(quota_outlook([(1000, 100)], 2000, 1000)["status"], "exhausted")

    def test_snapshot_explains_ambiguous_window_and_telegram_shows_settings(self):
        store = Store(":memory:", ACCOUNTS)
        for window_id in ("w1", "w2"):
            store.heartbeat({"account_id": "compte-1", "window_id": window_id,
                             "workspace": "C:/project"})
        store.event({"account_id": "compte-1", "hook_event_name": "UserPromptSubmit",
                     "session_id": "s1", "turn_id": "t1", "cwd": "C:/project"})
        snapshot = store.snapshot()
        self.assertEqual(snapshot["accounts"][0]["diagnostics"]["ambiguous_chats"], 1)
        self.assertEqual(snapshot["accounts"][0]["tasks"][0]["window_match"], "ambiguous")
        self.assertEqual(snapshot["accounts"][0]["tasks"][0]["signal"], "hook")
        self.assertIn("20 %", render("alertes", snapshot))
        self.assertIn("Chat lancé", render("historique", snapshot))


if __name__ == "__main__":
    unittest.main()
