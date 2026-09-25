"""Verify that a real Codex CLI session can call check_environment itself."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from environment_catalog import CodexThreadClient
from workspace_cli import codex_command
from workspace_mailbox import DEFAULT_DB, Mailbox, agent_address


def main() -> int:
    parser = argparse.ArgumentParser(description="Teste le diagnostic MCP depuis un agent Codex CLI")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--workspace", type=Path,
                        help="Dépôt à tester depuis le contexte de l'agent")
    args = parser.parse_args()
    if not args.codex_home.is_dir():
        parser.error("Profil Codex introuvable")
    if args.workspace and not args.workspace.is_dir():
        parser.error("Workspace introuvable")
    env = os.environ.copy()
    env["CODEX_HOME"] = str(args.codex_home.resolve())
    for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_CI",
                "CODEX_INTERNAL_ORIGINATOR_OVERRIDE"):
        env.pop(key, None)
    context = (contextlib.nullcontext(str(args.workspace.resolve())) if args.workspace
               else tempfile.TemporaryDirectory(prefix="workspace-mcp-probe-"))
    with context as directory:
        command = codex_command() + ["exec", "--json", "--skip-git-repo-check",
            "--sandbox", "read-only", "-C", directory,
            "-c", 'model_reasoning_effort="low"', "-"]
        process = subprocess.Popen(command, cwd=directory, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        timer = threading.Timer(150, lambda: process.kill() if process.poll() is None else None)
        timer.daemon = True
        timer.start()
        session_id, tool_seen, errors = None, False, []
        try:
            assert process.stdin and process.stdout
            process.stdin.write("Appelle maintenant l'outil check_environment du serveur MCP "
                "codex_workspace, puis réponds seulement TERMINE. Ne lance aucune commande "
                "et ne lis aucun fichier.\n")
            process.stdin.close()
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    if line.strip():
                        errors.append(line.strip()[:180])
                    continue
                if event.get("type") == "thread.started":
                    session_id = event["thread_id"]
                elif event.get("type") in {"item.started", "item.completed"}:
                    item = event.get("item") or {}
                    if item.get("type") == "mcp_tool_call" and item.get("tool") == "check_environment":
                        tool_seen = True
                elif event.get("type") in {"turn.failed", "error"}:
                    errors.append(str(event.get("error") or event.get("message"))[:180])
            exit_code = process.wait()
        finally:
            timer.cancel()
    check = None
    if session_id:
        mailbox = Mailbox(args.db)
        try:
            check = mailbox.agent_check(agent_address(args.account_id, session_id))
        finally:
            mailbox.close()
    passed = exit_code == 0 and bool(check)
    archived = False
    if passed:
        try:
            client = CodexThreadClient(args.codex_home)
            try:
                client.request("thread/archive", {"threadId": session_id})
                archived = True
            finally:
                client.close()
        except (OSError, RuntimeError, TimeoutError):
            pass
    print(json.dumps({"account": args.account_id, "session_id": session_id,
        "mcp_tool_seen": tool_seen, "check_recorded": bool(check),
        "github_status": check["github_status"] if check else None,
        "chat_archived": archived, "exit_code": exit_code,
        "error": "; ".join(errors[-2:]) if not passed else ""}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
