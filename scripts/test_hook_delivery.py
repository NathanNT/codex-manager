"""Exercise a real Codex CLI hook round trip without modifying a project.

The random marker exists only in the mailbox. Seeing it in the model's final
answer establishes that Codex accepted the hook's additionalContext.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workspace_cli import codex_command
from workspace_mailbox import DEFAULT_DB, Mailbox, agent_address
from workspace_hook_receipts import reconcile


def main() -> int:
    parser = argparse.ArgumentParser(description="Teste la réception réelle d'un message par Codex CLI")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--codex-executable", type=Path,
                        help="Tester le binaire Codex embarqué dans l'extension VS Code")
    parser.add_argument("--event", choices=("prompt", "post-tool"), default="prompt")
    parser.add_argument("--sandbox", choices=("read-only", "workspace-write", "danger-full-access"),
                        default="read-only")
    parser.add_argument("--keep-chat", action="store_true", help="Conserver le chat de test dans l'historique")
    args = parser.parse_args()
    if not args.codex_home.is_dir():
        parser.error("Profil Codex introuvable")
    if args.codex_executable and not args.codex_executable.is_file():
        parser.error("Binaire Codex introuvable")
    marker = "WMCP-" + uuid.uuid4().hex[:16].upper()
    sender = agent_address("workspace", "hook-probe")
    env = os.environ.copy()
    env["CODEX_HOME"] = str(args.codex_home.resolve())
    for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_CI",
                "CODEX_INTERNAL_ORIGINATOR_OVERRIDE"):
        env.pop(key, None)
    with tempfile.TemporaryDirectory(prefix="workspace-hook-probe-") as directory:
        command = ([str(args.codex_executable)] if args.codex_executable else codex_command()) + [
            "exec", "--json", "--skip-git-repo-check",
            "--sandbox", args.sandbox, "-C", directory,
            "-c", 'model_reasoning_effort="low"']

        def invoke(suffix: list[str], prompt: str, on_command=None):
            process = subprocess.Popen(command + suffix + ["-"], cwd=directory, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            timer = threading.Timer(150, lambda: process.kill() if process.poll() is None else None)
            timer.daemon = True
            timer.start()
            session, final, errors = None, "", []
            try:
                assert process.stdin and process.stdout
                process.stdin.write(prompt)
                process.stdin.close()
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        if line.strip():
                            errors.append(line.strip()[:180])
                        continue
                    if event.get("type") == "thread.started":
                        session = event["thread_id"]
                    elif event.get("type") == "item.started" and on_command:
                        item = event.get("item") or {}
                        if item.get("type") == "command_execution":
                            on_command()
                    elif event.get("type") == "item.completed":
                        item = event.get("item") or {}
                        if item.get("type") == "agent_message":
                            final = item.get("text", final)
                    elif event.get("type") in {"turn.failed", "error"}:
                        errors.append(str(event.get("error") or event.get("message"))[:180])
                return process.wait(), session, final, errors
            finally:
                timer.cancel()

        first_code, session_id, first_final, first_errors = invoke(
            [], "Réponds uniquement PRET. Ne lance aucun outil et ne lis aucun fichier.")
        if first_code or not session_id:
            print(f"ÉCHEC : session Codex non créée. {'; '.join(first_errors[-2:])}")
            return 1
        message_id = None

        def send_marker():
            nonlocal message_id
            if message_id:
                return
            mailbox = Mailbox(args.db)
            try:
                mailbox.register_agent("workspace", "hook-probe", "Test de réception")
                target = mailbox.register_agent(args.account_id, session_id, "Test CLI Codex")["id"]
                message = mailbox.send(sender, f"Code de validation : {marker}",
                                       recipient_agent=target)
                message_id = message["id"]
            finally:
                mailbox.close()

        if args.event == "prompt":
            send_marker()
            second_prompt = ("Si un nouveau message Workspace MCP t'est transmis avec ce tour, "
                "recopie uniquement son code WMCP dans ta réponse finale ; sinon réponds ABSENT. "
                "Ne lance aucun outil et ne lis aucun fichier.")
        else:
            second_prompt = ("Exécute une seule commande PowerShell : "
                "Start-Sleep -Seconds 3; Write-Output PRET. Après le résultat de l'outil, "
                "si un nouveau message Workspace MCP t'est transmis, recopie uniquement "
                "son code WMCP dans ta réponse finale ; sinon réponds ABSENT. "
                "Ne lis aucun fichier et ne modifie rien.")
        code, resumed_id, final, errors = invoke(["resume", session_id], second_prompt,
            on_command=send_marker if args.event == "post-tool" else None)
    row = None
    if message_id:
        mailbox = Mailbox(args.db)
        try:
            reconcile(mailbox, [{"id": args.account_id, "codex_home": str(args.codex_home)}])
            row = mailbox.db.execute("SELECT delivered_at,method,attempted_at,proof FROM message_deliveries "
                "WHERE message_id=? AND agent_id=?",
                (message_id, agent_address(args.account_id, session_id))).fetchone()
        finally:
            mailbox.close()
    accepted = code == 0 and marker in final
    if accepted:
        mailbox = Mailbox(args.db)
        try:
            mailbox.record_hook_verification(args.account_id,
                "UserPromptSubmit" if args.event == "prompt" else "PostToolUse",
                "cli", session_id)
        finally:
            mailbox.close()
    archived = False
    if accepted and not args.keep_chat:
        try:
            from environment_catalog import CodexThreadClient
            client = CodexThreadClient(args.codex_home)
            try:
                client.request("thread/archive", {"threadId": session_id})
                archived = True
            finally:
                client.close()
        except (OSError, RuntimeError, TimeoutError):
            pass
    print(json.dumps({"account": args.account_id, "event": args.event,
                      "session_id": session_id, "message_created": bool(message_id),
                      "hook_output_recorded": bool(row and row["attempted_at"]),
                      "context_recorded_by_codex": bool(row and row["proof"]),
                      "delivery_method": row["method"] if row else None,
                      "model_saw_context": accepted, "codex_exit_code": code,
                      "test_chat_archived": archived,
                      "first_answer": first_final[:120], "resumed_session_id": resumed_id,
                      "final_answer": final[:300],
                      "error": "; ".join(errors[-2:]) if errors and not accepted else ""},
                     ensure_ascii=False))
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
