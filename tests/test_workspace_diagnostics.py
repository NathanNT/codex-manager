import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from workspace_diagnostics import diagnose, probe_github
from workspace_mailbox import Mailbox


class WorkspaceDiagnosticsTests(unittest.TestCase):
    def test_github_checks_keep_cli_api_and_git_transport_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            successful = SimpleNamespace(returncode=0, stdout="example/project\n")
            with patch("workspace_diagnostics.shutil.which", side_effect=lambda name: name), \
                 patch("workspace_diagnostics._run", side_effect=[successful] * 4) as run:
                result = probe_github(directory)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["checks"]["cli"]["status"], "ok")
            self.assertEqual(result["checks"]["repository"]["status"], "ok")
            self.assertEqual(result["checks"]["git"]["status"], "ok")
            self.assertEqual(run.call_count, 4)
            with patch("workspace_diagnostics.shutil.which", side_effect=lambda name: name), \
                 patch("workspace_diagnostics._run", side_effect=[successful, None,
                    SimpleNamespace(returncode=1, stdout="")]):
                limited = probe_github(directory)
            self.assertEqual(limited["checks"]["cli"]["status"], "ok")
            self.assertEqual(limited["checks"]["repository"]["status"], "unknown")
            self.assertEqual(limited["checks"]["git"]["status"], "unknown")
            with patch("workspace_diagnostics.shutil.which",
                       side_effect=lambda name: None if name == "gh" else name), \
                 patch("workspace_diagnostics._run", side_effect=[successful, successful]):
                git_only = probe_github(directory)
            self.assertEqual(git_only["checks"]["cli"]["status"], "incomplete")
            self.assertEqual(git_only["checks"]["git"]["status"], "ok")
            self.assertEqual(git_only["status"], "ok")

    def test_config_files_do_not_count_as_observed_mcp_or_hook(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "codex"
            home.mkdir()
            (home / "config.toml").write_text('[mcp_servers.codex_workspace]\ncommand = "python"\n')
            (home / "hooks.json").write_text(json.dumps({"hooks": {
                "UserPromptSubmit": [{"hooks": [{"command": "python hook_sender.py"}]}],
                "PostToolUse": [{"hooks": [{"command": "python hook_sender.py"}]}]}}))
            events = sqlite3.connect(base / "monitor.sqlite", check_same_thread=False)
            events.execute("CREATE TABLE events(account_id TEXT,type TEXT,at REAL,details TEXT)")
            store = SimpleNamespace(accounts=[{"id": "compte-1", "codex_home": str(home)}],
                                    lock=threading.RLock(), db=events)
            mailbox = Mailbox(base / "mailbox.sqlite")
            try:
                agent = mailbox.register_agent("compte-1", "chat-a")
                mailbox.set_presence([agent])
                with patch("workspace_diagnostics._run", return_value=None):
                    result = diagnose(store, agent["id"], mailbox)
                self.assertEqual(result["mcp"]["status"], "unknown")
                self.assertEqual(result["hooks"]["status"], "unknown")
                self.assertEqual(result["delivery"]["status"], "unknown")
                self.assertEqual(result["github"]["status"], "unknown")
                mailbox.save_agent_check(agent["id"], {"status": "ok", "detail": "Dépôt accessible",
                                                         "repository": "example/project"})
                mailbox.record_hook_verification("compte-1", "UserPromptSubmit", "cli", "chat-a")
                mailbox.touch_presence(agent["id"])
                events.execute("INSERT INTO events VALUES (?,?,?,?)",
                               ("compte-1", "UserPromptSubmit", 1000.0, '{"session_id":"chat-a"}'))
                events.commit()
                with patch("workspace_diagnostics._run", return_value=None):
                    result = diagnose(store, agent["id"], mailbox)
                self.assertEqual(result["mcp"]["status"], "ok")
                self.assertEqual(result["hooks"]["status"], "ok")
                self.assertEqual(result["github"]["status"], "ok")
                self.assertEqual(result["github"]["repository"], "example/project")
                self.assertEqual(result["hooks"]["checks"]["prompt"]["status"], "ok")
                self.assertEqual(result["hooks"]["checks"]["vscode"]["status"], "unknown")
                mailbox.record_hook_verification("compte-1", "PostToolUse", "vscode", "chat-a")
                sender = mailbox.register_agent("workspace", "hook-probe")["id"]
                message = mailbox.send(sender, "Test", target_agent=agent["id"])
                nonce = mailbox.prepare_hook_emission(agent["id"], "PostToolUse", [message["id"]], [])
                with patch("workspace_diagnostics._run", return_value=None):
                    unconfirmed = diagnose(store, agent["id"], mailbox)
                self.assertEqual(unconfirmed["delivery"]["status"], "unknown")
                mailbox.verify_hook_emission(nonce, "Codex rollout : test, ligne 1")
                with patch("workspace_diagnostics._run", return_value=None):
                    verified = diagnose(store, agent["id"], mailbox)
                self.assertEqual(verified["hooks"]["checks"]["vscode"]["status"], "ok")
                self.assertEqual(verified["delivery"]["status"], "ok")
                mailbox.prepare_hook_emission(agent["id"], "UserPromptSubmit", [], [])
                with patch("workspace_diagnostics._run", return_value=None):
                    newer = diagnose(store, agent["id"], mailbox)
                self.assertEqual(newer["delivery"]["status"], "unknown")
            finally:
                mailbox.close()
                events.close()


if __name__ == "__main__":
    unittest.main()
