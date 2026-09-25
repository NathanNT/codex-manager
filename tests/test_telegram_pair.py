import unittest
from unittest.mock import patch

from scripts.telegram_pair import pairing_chat, wait_for_pairing


class TelegramPairTests(unittest.TestCase):
    def test_only_fresh_code_from_same_private_user_pairs(self):
        def update(text, chat_id=42, chat_type="private", sender_id=42, sender_bot=False):
            return {"message": {"text": text, "chat": {"id": chat_id, "type": chat_type},
                                "from": {"id": sender_id, "is_bot": sender_bot}}}

        self.assertEqual(pairing_chat(update("/pair secret"), "secret"), "42")
        for item in (update("/start"), update("/pair wrong"),
                     update("/pair secret", chat_id=-42, chat_type="group", sender_id=42),
                     update("/pair secret", sender_id=99),
                     update("/pair secret", sender_bot=True)):
            self.assertIsNone(pairing_chat(item, "secret"))

    def test_old_updates_cannot_complete_pairing(self):
        responses = [
            [{"update_id": 11, "message": {"text": "/start",
              "chat": {"id": 42, "type": "private"}, "from": {"id": 42}}}],
            [{"update_id": 12, "message": {"text": "/pair secret",
              "chat": {"id": 42, "type": "private"}, "from": {"id": 42}}}],
        ]
        with patch("scripts.telegram_pair.bot_api", side_effect=responses) as api:
            chat_id = wait_for_pairing("token", "secret", [{"update_id": 10}], timeout_seconds=3)
        self.assertEqual(chat_id, "42")
        self.assertEqual(api.call_args_list[0].args[2]["offset"], 11)
        self.assertEqual(api.call_args_list[1].args[2]["offset"], 12)


if __name__ == "__main__":
    unittest.main()
