import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from codex_conversation import AppServerClient, final_answer, indexed_chat
from server import Store
from telegram_commands import TelegramCommandBot, telegram_chunks


THREAD_ID = "123e4567-e89b-42d3-a456-426614174000"


def create_home(path):
    home = Path(path)
    with closing(sqlite3.connect(home / "state_5.sqlite")) as db:
        db.execute("CREATE TABLE threads (id TEXT, name TEXT, cwd TEXT, source TEXT)")
        db.execute("INSERT INTO threads VALUES (?,?,?,?)",
                   (THREAD_ID, "Projet test", str(home), "vscode"))
        db.commit()
    return home


class FakeBot(TelegramCommandBot):
    def __init__(self, home):
        super().__init__("test-token", "42", lambda: {"accounts": []},
                         [{"id": "compte-1", "name": "Compte 1", "codex_home": str(home)}])
        self.calls = []

    def api(self, method, payload, timeout=15):
        self.calls.append((method, payload))
        return True


def answer_callback(sender=42, data=None, button="Afficher la réponse"):
    data = data or f"chat:compte-1:{THREAD_ID}"
    return {"callback_query": {"id": "cb", "data": data, "from": {"id": sender},
        "message": {"chat": {"id": 42, "type": "private"}, "message_id": 12,
            "text": "✅ Réponse prête · Compte 1\nProjet test",
            "reply_markup": {"inline_keyboard": [[{"text": button, "callback_data": data}]]}}}}


class ConversationTests(unittest.TestCase):
    def test_final_answer_ignores_interrupted_turn_and_tool_output(self):
        text, turn = final_answer([
            {"status": "interrupted", "items": [{"type": "agentMessage", "text": "inachevé"}]},
            {"status": "completed", "items": [{"type": "mcpToolCall", "text": "secret"},
                {"type": "agentMessage", "phase": "commentary", "text": "travail"},
                {"type": "agentMessage", "phase": "final_answer", "text": "Réponse finale"}]},
        ])
        self.assertEqual(text, "Réponse finale")
        self.assertEqual(turn["status"], "completed")

    def test_reader_rejects_other_account_and_writer_methods(self):
        with tempfile.TemporaryDirectory() as directory:
            home = create_home(directory)
            self.assertEqual(indexed_chat(home, THREAD_ID)["name"], "Projet test")
            with self.assertRaisesRegex(ValueError, "absent de ce compte"):
                indexed_chat(home, "123e4567-e89b-42d3-a456-426614174001")
        with self.assertRaisesRegex(ValueError, "lecture seule"):
            AppServerClient("unused")._request("turn/start", {})
        with self.assertRaisesRegex(ValueError, "lecture seule"):
            AppServerClient("unused")._request("thread/fork", {})

    def test_notification_button_reads_one_answer_without_navigation(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = FakeBot(create_home(directory))
            with patch("telegram_commands.read_latest_answer", return_value=("Réponse privée", {})):
                self.assertTrue(bot.handle_update(answer_callback()))
            self.assertEqual([method for method, _ in bot.calls], ["answerCallbackQuery", "sendMessage"])
            self.assertIn("Réponse privée", bot.calls[-1][1]["text"])
            self.assertNotIn("reply_markup", bot.calls[-1][1])
            self.assertFalse(any("reponses" in str(payload) or "compose:" in str(payload)
                                 for _, payload in bot.calls))

    def test_old_reply_and_chat_browsing_buttons_are_inert(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = FakeBot(create_home(directory))
            self.assertFalse(bot.handle_update(answer_callback(sender=99)))
            self.assertFalse(bot.handle_update(answer_callback(button="🔄 Actualiser")))
            self.assertFalse(bot.handle_update(answer_callback(data=f"compose:compte-1:{THREAD_ID}")))
            self.assertFalse(bot.handle_update(answer_callback(data="reponses")))
            self.assertFalse(bot.handle_update({"message": {"chat": {"id": 42, "type": "private"},
                "from": {"id": 42}, "reply_to_message": {"message_id": 99}, "text": "Lance ce travail"}}))
            self.assertEqual(bot.calls, [])

    def test_telegram_message_chunks_respect_utf16_limit(self):
        self.assertEqual("".join(telegram_chunks("🙂" * 3000)), "🙂" * 3000)
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 3500
                            for chunk in telegram_chunks("🙂" * 3000)))

    def test_old_telegram_write_state_is_removed_on_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.sqlite"
            with closing(sqlite3.connect(path)) as db:
                for name in ("telegram_reply_prompts", "telegram_chat_routes", "telegram_origin_turns"):
                    db.execute(f"CREATE TABLE {name} (legacy TEXT)")
                    db.execute(f"INSERT INTO {name} VALUES ('old')")
                db.commit()
            store = Store(str(path), [{"id": "compte-1", "name": "Compte 1"}])
            remaining = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"telegram_reply_prompts", "telegram_chat_routes", "telegram_origin_turns"}.isdisjoint(remaining))
            store.db.close()


if __name__ == "__main__":
    unittest.main()
