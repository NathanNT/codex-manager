import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from environment_catalog import (change_archive, chat_catalog, deduplicate_chats,
                                 duplicate_plan, inventory, transfer_chats)


CHAT_ID = "019c5780-9bb0-7ed2-bb45-768d14cd2575"


class EnvironmentCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / "source"
        self.target = root / "target"
        for home in (self.source, self.target):
            home.mkdir()
            with closing(sqlite3.connect(home / "state_5.sqlite")) as db:
                db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, updated_at INTEGER, "
                           "source TEXT, archived INTEGER, name TEXT, title TEXT, cwd TEXT, "
                           "tokens_used INTEGER, is_pinned INTEGER)")
                db.commit()
        self.rollout = self.source / "sessions" / "2026" / "09" / "23" / f"rollout-{CHAT_ID}.jsonl"
        self.rollout.parent.mkdir(parents=True)
        self.rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
        os.utime(self.rollout, (1, 1))
        with closing(sqlite3.connect(self.source / "state_5.sqlite")) as db:
            db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (CHAT_ID, str(self.rollout), 1, "vscode", 0, "Chat de test", "", "C:/project", 123, 0))
            db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?)",
                       ("guardian", "", 1, '{"subagent":{}}', 0, "Interne", "", "", 5, 0))
            db.commit()

    def test_catalog_excludes_internal_threads(self):
        result = chat_catalog(self.source)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["chats"][0]["tokens"], 123)
        self.assertEqual(result["by_workspace"][0]["count"], 1)

    def test_catalog_filters_tokens_dates_and_archive_view(self):
        newer = "019c5780-9bb0-7ed2-bb45-768d14cd2576"
        archived = "019c5780-9bb0-7ed2-bb45-768d14cd2577"
        with closing(sqlite3.connect(self.source / "state_5.sqlite")) as db:
            db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (newer, "", 200, "vscode", 0, "Autre", "", "C:/project", 800, 0))
            db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (archived, "", 300, "vscode", 1, "Archive", "", "C:/project", 900, 0))
            db.commit()
        filtered = chat_catalog(self.source, min_tokens=500, max_tokens=850, date_from=100, date_to=250)
        self.assertEqual([item["id"] for item in filtered["chats"]], [newer])
        self.assertEqual(chat_catalog(self.source, archived=True)["chats"][0]["id"], archived)

    def test_duplicate_plan_keeps_newest_within_each_workspace(self):
        older = "019c5780-9bb0-7ed2-bb45-768d14cd2576"
        newer = "019c5780-9bb0-7ed2-bb45-768d14cd2577"
        elsewhere = "019c5780-9bb0-7ed2-bb45-768d14cd2578"
        with closing(sqlite3.connect(self.source / "state_5.sqlite")) as db:
            for thread_id, title, workspace, updated in (
                (older, "Chat de test (1)", "C:/project", 2),
                (newer, "Chat de test (2)", "C:/project", 3),
                (elsewhere, "Chat de test (3)", "C:/elsewhere", 4)):
                db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (thread_id, "", updated, "vscode", 0, title, "", workspace, 20, 0))
            db.commit()
        plan = duplicate_plan(self.source)
        self.assertEqual(plan["archive_count"], 2)
        self.assertEqual(plan["groups"][0]["keep"], newer)
        self.assertNotIn(elsewhere, plan["groups"][0]["archive"])
        self.assertEqual(duplicate_plan(self.source, {older})["skipped_groups"], 1)

    def test_archive_and_deduplicate_use_codex_app_server_methods(self):
        older = "019c5780-9bb0-7ed2-bb45-768d14cd2576"
        with closing(sqlite3.connect(self.source / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET name='Chat de test (2)',updated_at=3 WHERE id=?", (CHAT_ID,))
            db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (older, "", 2, "vscode", 0, "Chat de test (1)", "", "C:/project", 20, 0))
            db.commit()
        calls = []
        class FakeClient:
            def __init__(self, home): pass
            def request(self, method, params): calls.append((method, params))
            def close(self): pass
        result = deduplicate_chats(self.source, client_factory=FakeClient)
        self.assertEqual(result["results"][0]["archived"], 1)
        self.assertEqual(calls[0], ("thread/name/set", {"threadId": CHAT_ID, "name": "Chat de test"}))
        self.assertEqual(calls[1], ("thread/archive", {"threadId": older}))
        self.assertEqual(change_archive(self.source, [older], True, {older}, FakeClient)[0]["status"], "error")

    def test_transfer_keeps_source_and_copies_selected_history(self):
        result = transfer_chats(self.source, self.target, [CHAT_ID], verify=lambda *_: True)
        self.assertEqual(result[0]["status"], "copied")
        self.assertTrue(self.rollout.exists())
        self.assertEqual((self.target / "sessions" / self.rollout.relative_to(self.source / "sessions")).read_bytes(),
                         self.rollout.read_bytes())

    def test_transfer_does_not_replace_existing_chat(self):
        with closing(sqlite3.connect(self.target / "state_5.sqlite")) as db:
            db.execute("INSERT INTO threads(id) VALUES (?)", (CHAT_ID,))
            db.commit()
        result = transfer_chats(self.source, self.target, [CHAT_ID], verify=lambda *_: True)
        self.assertEqual(result[0]["status"], "exists")

    def test_transfer_rejects_active_and_rolls_back_failed_verification(self):
        blocked = transfer_chats(self.source, self.target, [CHAT_ID], {CHAT_ID}, verify=lambda *_: True)
        self.assertEqual(blocked[0]["status"], "error")
        failed = transfer_chats(self.source, self.target, [CHAT_ID], verify=lambda *_: False)
        self.assertEqual(failed[0]["status"], "error")
        self.assertFalse((self.target / "sessions" / self.rollout.relative_to(self.source / "sessions")).exists())

    def test_transfer_rejects_writer_lock(self):
        locks = self.source / "thread-writer-locks"
        locks.mkdir()
        (locks / f"{CHAT_ID}.lock").touch()
        result = transfer_chats(self.source, self.target, [CHAT_ID], verify=lambda *_: True)
        self.assertEqual(result[0]["status"], "error")

    def test_inventory_never_returns_mcp_url_or_token(self):
        (self.source / "config.toml").write_text(
            '[mcp_servers.private]\nurl="https://secret.example/?token=secret-token"\n', encoding="utf-8")
        result = inventory(self.source)
        self.assertEqual(result["mcp"][0]["name"], "private")
        self.assertNotIn("secret-token", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
