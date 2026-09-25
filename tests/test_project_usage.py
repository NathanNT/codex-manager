import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path

from project_usage import baseline_copied_thread, install_schema, observe, read_thread_counters, summary


class ProjectUsageTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        install_schema(self.db)
        self.start = datetime(2026, 9, 24, 10, 0).timestamp()

    def counter(self, thread_id="chat-a", tokens=100, project="C:/Projects/Atlas", created=None, updated=None):
        return {"id": thread_id, "tokens": tokens, "cwd": project,
                "created_at": self.start - 86400 if created is None else created,
                "updated_at": self.start if updated is None else updated}

    def test_first_poll_baselines_old_tokens_and_counts_only_increases(self):
        self.assertEqual(observe(self.db, "compte-1", [self.counter()], self.start), 0)
        self.assertEqual(observe(self.db, "compte-1", [self.counter(tokens=150)], self.start + 30), 50)
        self.assertEqual(observe(self.db, "compte-1", [self.counter(tokens=150)], self.start + 60), 0)
        result = summary(self.db, "compte-1", self.start + 30)
        self.assertEqual(result["today"], 50)
        self.assertEqual(result["projects"][0]["chats"], 1)

    def test_new_chat_first_turn_is_counted_but_copied_chat_is_baselined(self):
        observe(self.db, "compte-1", [], self.start)
        observe(self.db, "compte-2", [self.counter(thread_id="copied")], self.start)
        fresh = self.counter(thread_id="new", tokens=70, created=self.start + 10, updated=self.start + 15)
        clone = self.counter(thread_id="copied", tokens=900, created=self.start + 10, updated=self.start + 15)
        self.assertEqual(observe(self.db, "compte-1", [fresh, clone], self.start + 30), 70)

    def test_counter_reset_and_project_change_do_not_misattribute(self):
        observe(self.db, "compte-1", [self.counter()], self.start)
        observe(self.db, "compte-1", [self.counter(tokens=90)], self.start + 30)
        observe(self.db, "compte-1", [self.counter(tokens=110, project="C:/Projects/Beta")], self.start + 60)
        result = summary(self.db, "compte-1", self.start + 60)
        unassigned = next(item for item in result["projects"] if item["workspace"] == "Projet non attribué")
        self.assertEqual(unassigned["today"], 20)
        self.assertEqual(result["today"], 20)

    def test_new_telegram_fork_baselines_copied_history_before_new_tokens(self):
        observe(self.db, "compte-1", [self.counter(tokens=1000)], self.start)
        baseline_copied_thread(self.db, "compte-1", "fork")
        fork = self.counter(thread_id="fork", tokens=1000, created=self.start + 10)
        self.assertEqual(observe(self.db, "compte-1", [fork], self.start + 15), 0)
        self.assertEqual(observe(self.db, "compte-1", [self.counter(
            thread_id="fork", tokens=1040, created=self.start + 10)], self.start + 30), 40)
        self.assertEqual(summary(self.db, "compte-1", self.start + 30)["today"], 40)

    def test_daily_rollover_uses_thread_update_day(self):
        start = datetime(2026, 9, 23, 23, 50).timestamp()
        old = self.counter(tokens=100, updated=start)
        observe(self.db, "compte-1", [old], start)
        next_day = start + 20 * 60
        observe(self.db, "compte-1", [self.counter(tokens=130, updated=next_day)], next_day)
        self.assertEqual(summary(self.db, "compte-1", next_day)["today"], 30)

    def test_reader_ignores_internal_threads(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state_5.sqlite"
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE threads (id TEXT,cwd TEXT,tokens_used INTEGER,created_at INTEGER,updated_at INTEGER,source TEXT)")
                db.execute("INSERT INTO threads VALUES ('chat','C:/Atlas',42,1,2,'vscode')")
                db.execute("INSERT INTO threads VALUES ('guardian','C:/Atlas',999,1,2,'subAgentOther')")
                db.commit()
            self.assertEqual([item["id"] for item in read_thread_counters(root)], ["chat"])


if __name__ == "__main__":
    unittest.main()
