import unittest
from datetime import date

from telegram_commands import TelegramCommandBot, render


def sample_snapshot():
    return {"now": 1_800_000_000, "accounts": [{
        "id": "compte-1", "name": "Compte 1",
        "windows": [{"id": "window-1", "label": "Projet Atlas"}],
        "tasks": [
            {"window_id": "window-1", "display_status": "active", "title": "Compiler Atlas",
             "session_id": "thread-1", "updated_at": 1_800_000_000},
            {"window_id": None, "display_status": "active", "title": "Session non confirmée",
             "session_id": "thread-2", "updated_at": 1_800_000_000},
        ],
        "metrics": {"last_success_at": 1_800_000_000,
                    "identity": {"account": {"planType": "pro"}},
                    "limits": {"rateLimits": {"primary": {"usedPercent": 35, "resetsAt": 1_800_000_600}}},
                    "usage": {"dailyUsageBuckets": [{"startDate": date.today().isoformat(),
                                                       "tokens": 1234}]},
                    "threads": {"data": [{"id": "thread-1", "name": "Goal Atlas",
                                           "goal": {"status": "blocked", "objective": "Compiler Atlas"}}]}}
    }]}


class FakeBot(TelegramCommandBot):
    def __init__(self):
        super().__init__("secret-test", "42", sample_snapshot)
        self.calls = []

    def api(self, method, payload, timeout=15):
        self.calls.append((method, payload))
        return True


class TelegramCommandTests(unittest.TestCase):
    def test_menu_and_views_match_confirmed_dashboard_data(self):
        snapshot = sample_snapshot()
        self.assertIn("🟢 1 chat au travail", render("menu", snapshot))
        self.assertIn("1 chat au travail", render("status", snapshot))
        self.assertIn("🟢 Projet Atlas · Compiler Atlas — travaille · goal bloqué", render("status", snapshot))
        self.assertIn("Projet Atlas", render("fenetres", snapshot))
        self.assertNotIn("Session non confirmée", render("fenetres", snapshot))
        self.assertIn("🔴 bloqué · Goal Atlas", render("goals", snapshot))
        self.assertIn("Tokens aujourd'hui : 1 234", render("stats", snapshot))
        self.assertIn("Quota disponible : 65 %", render("stats", snapshot))
        self.assertEqual(render("comptes", snapshot), render("stats", snapshot))
        self.assertEqual(render("conso", snapshot), render("stats", snapshot))

    def test_recent_error_is_visible_ahead_of_work_in_same_window(self):
        snapshot = sample_snapshot()
        snapshot["accounts"][0]["tasks"].append({
            "window_id": "window-1", "display_status": "failed",
            "title": "Échec des tests", "updated_at": snapshot["now"] - 30,
        })
        self.assertIn("🔴 Projet Atlas · Échec des tests — erreur", render("status", snapshot))
        self.assertIn("🟢 Projet Atlas · Compiler Atlas — travaille", render("status", snapshot))

    def test_active_chat_without_goal_is_still_reported(self):
        snapshot = sample_snapshot()
        snapshot["accounts"][0]["metrics"]["threads"]["data"][0]["goal"] = None
        self.assertIn("1 chat au travail", render("status", snapshot))
        self.assertIn("🟢 Projet Atlas · Compiler Atlas — travaille", render("status", snapshot))
        self.assertIn("sans goal", render("status", snapshot))
        self.assertIn("Chat simple · sans goal", render("fenetres", snapshot))

    def test_open_goal_and_inactive_chat_are_distinct(self):
        snapshot = sample_snapshot()
        snapshot["accounts"][0]["tasks"][0]["display_status"] = "interrupted"
        snapshot["accounts"][0]["metrics"]["threads"]["data"][0]["goal"]["status"] = "active"
        status = render("status", snapshot)
        self.assertIn("1 goal en attente", status)
        self.assertIn("⚪ Projet Atlas · Compiler Atlas — tâche arrêtée · goal à reprendre", status)

    def test_unconfirmed_work_is_not_reported_as_active(self):
        snapshot = sample_snapshot()
        snapshot["accounts"][0]["tasks"][0]["display_status"] = "unconfirmed"
        snapshot["accounts"][0]["metrics"]["threads"]["data"][0]["goal"]["status"] = "active"
        status = render("status", snapshot)
        self.assertIn("1 activité non confirmée", status)
        self.assertIn("activité non confirmée · goal activité à confirmer", status)
        self.assertNotIn("1 chat au travail", status)

    def test_project_usage_is_available_in_private_bot_menu(self):
        snapshot = sample_snapshot()
        snapshot["project_usage"] = {"compte-1": {"started_at": snapshot["now"] - 3600,
            "last_observed_at": snapshot["now"], "today": 1500, "week": 3000,
            "projects": [{"workspace": "C:/Projects/Atlas", "today": 1500, "week": 3000}]}}
        message = render("projets", snapshot)
        self.assertIn("TOKENS PAR PROJET", message)
        self.assertIn("Atlas : 1 500 aujourd'hui · 3 000 sur 7 j", message)
        self.assertTrue(any(button["callback_data"] == "projets" for row in FakeBot().keyboard("menu")["inline_keyboard"] for button in row))

    def test_stats_separate_accounts_and_goals_require_confirmed_window(self):
        snapshot = sample_snapshot()
        snapshot["accounts"].append({
            "id": "compte-2", "name": "Compte 2", "windows": [], "tasks": [],
            "metrics": {"last_success_at": snapshot["now"],
                        "identity": {"account": {"planType": "prolite"}},
                        "limits": {"rateLimits": {"primary": {"usedPercent": 29,
                             "windowDurationMins": 10080, "resetsAt": snapshot["now"] + 600}}},
                        "usage": {"dailyUsageBuckets": [{"startDate": date.today().isoformat(),
                                                           "tokens": 2000}]},
                        "threads": {"data": [{"id": "hidden", "name": "Goal sans fenêtre",
                                               "goal": {"status": "active"}}]}},
        })
        stats = render("stats", snapshot)
        self.assertIn("COMPTE 1 · Pro", stats)
        self.assertIn("COMPTE 2 · Pro Lite", stats)
        self.assertIn("Quota disponible : 71 % (7 j)", stats)
        self.assertIn("TOTAL · aujourd'hui 3 234", stats)
        self.assertNotIn("Goal sans fenêtre", render("goals", snapshot))

    def test_unauthorized_message_and_button_get_no_dashboard_data(self):
        bot = FakeBot()
        for message in (
            {"chat": {"id": 99, "type": "private"}, "from": {"id": 99}, "text": "/status"},
            {"chat": {"id": 42, "type": "private"}, "from": {"id": 99}, "text": "/status"},
            {"chat": {"id": -42, "type": "group"}, "from": {"id": 42}, "text": "/status"},
            {"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": "bonjour"},
        ):
            self.assertFalse(bot.handle_update({"message": message}))
        self.assertFalse(bot.handle_update({"callback_query": {"id": "cb", "data": "status",
            "from": {"id": 99}, "message": {"chat": {"id": 42, "type": "private"}, "message_id": 1}}}))
        self.assertEqual(bot.calls, [])

    def test_owner_commands_buttons_and_scoped_menu(self):
        bot = FakeBot()
        bot.install_menu()
        self.assertEqual(bot.calls[0][0], "setMyCommands")
        self.assertEqual(bot.calls[0][1]["scope"], {"type": "chat", "chat_id": 42})
        self.assertEqual(bot.calls[1][0], "setChatMenuButton")
        bot.calls.clear()
        self.assertTrue(bot.handle_update({"message": {
            "chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": "/status"}}))
        self.assertEqual(bot.calls[0][0], "sendMessage")
        self.assertIn("EN DIRECT", bot.calls[0][1]["text"])
        self.assertEqual(bot.calls[0][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "status")
        bot.calls.clear()
        self.assertTrue(bot.handle_update({"callback_query": {"id": "cb", "data": "goals",
            "from": {"id": 42}, "message": {"chat": {"id": 42, "type": "private"}, "message_id": 1}}}))
        self.assertEqual([call[0] for call in bot.calls], ["answerCallbackQuery", "editMessageText"])
        self.assertIn("Goal Atlas", bot.calls[1][1]["text"])

    def test_stats_menu_has_refresh_and_callbacks_do_not_duplicate_unchanged_messages(self):
        bot = FakeBot()
        bot.install_menu()
        commands = [item["command"] for item in bot.calls[0][1]["commands"]]
        self.assertEqual(commands, ["menu", "status", "stats", "projets", "fenetres", "goals", "alertes", "historique"])
        bot.calls.clear()
        text = render("stats", sample_snapshot())
        markup = bot.keyboard("stats")
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "stats")
        self.assertTrue(bot.handle_update({"callback_query": {"id": "cb", "data": "stats",
            "from": {"id": 42}, "message": {"chat": {"id": 42, "type": "private"},
                                         "message_id": 1, "text": text, "reply_markup": markup}}}))
        self.assertEqual([call[0] for call in bot.calls], ["answerCallbackQuery"])

    def test_alert_button_opens_view_without_replacing_original_alert(self):
        bot = FakeBot()
        self.assertTrue(bot.handle_update({"callback_query": {"id": "cb", "data": "status",
            "from": {"id": 42}, "message": {"chat": {"id": 42, "type": "private"},
                "message_id": 3, "text": "🟠 Validation requise",
                "reply_markup": {"inline_keyboard": [[{"text": "Voir dans le bot",
                                                       "callback_data": "status"}]]}}}}))
        self.assertEqual([call[0] for call in bot.calls], ["answerCallbackQuery", "sendMessage"])


if __name__ == "__main__":
    unittest.main()
