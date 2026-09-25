"""End-to-end hook test through the VS Code extension's bundled app server.

This exercises the same binary and turn/start protocol used by the extension.
It does not automate the extension's visible chat UI.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workspace_hook_receipts import reconcile
from workspace_mailbox import DEFAULT_DB, Mailbox, agent_address


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--codex-executable", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    marker = "WMCP-" + uuid.uuid4().hex[:16].upper()
    env = {**os.environ, "CODEX_HOME": str(args.codex_home.resolve())}
    output: queue.Queue = queue.Queue()
    process = subprocess.Popen([str(args.codex_executable), "app-server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def reader():
        for line in process.stdout:
            try:
                output.put(json.loads(line))
            except json.JSONDecodeError:
                pass
        output.put(None)

    threading.Thread(target=reader, daemon=True).start()
    sequence = 0

    def send(method: str, params: dict):
        nonlocal sequence
        sequence += 1
        process.stdin.write(json.dumps({"id": sequence, "method": method,
                                        "params": params}, ensure_ascii=False) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            item = output.get(timeout=max(0.1, deadline - time.monotonic()))
            if item is None:
                raise RuntimeError("App server terminé")
            if item.get("id") == sequence:
                if "error" in item:
                    raise RuntimeError(str(item["error"])[:200])
                return item.get("result") or {}
        raise TimeoutError(method)

    def turn(thread_id: str, prompt: str) -> str:
        send("turn/start", {"threadId": thread_id,
                            "input": [{"type": "text", "text": prompt}]})
        deadline = time.monotonic() + 120
        answer = ""
        while time.monotonic() < deadline:
            item = output.get(timeout=max(0.1, deadline - time.monotonic()))
            if item is None:
                raise RuntimeError("App server terminé pendant le tour")
            params = item.get("params") or {}
            if item.get("method") == "item/completed":
                completed = params.get("item") or {}
                if completed.get("type") == "agentMessage":
                    answer = completed.get("text") or answer
            if item.get("method") == "turn/completed":
                return answer
        raise TimeoutError("Tour Codex expiré")

    session_id = ""
    try:
        send("initialize", {"clientInfo": {"name": "workspace_hook_probe",
                                       "title": "Workspace hook probe", "version": "1.0"},
                            "capabilities": {"experimentalApi": True}})
        process.stdin.write('{"method":"initialized","params":{}}\n')
        process.stdin.flush()
        with tempfile.TemporaryDirectory(prefix="workspace-app-hook-") as directory:
            started = send("thread/start", {"cwd": directory, "approvalPolicy": "never",
                                            "sandbox": "read-only"})
            session_id = started["thread"]["id"]
            turn(session_id, "Réponds uniquement PRET. Aucun outil nécessaire.")
            mailbox = Mailbox(args.db)
            try:
                sender = mailbox.register_agent("workspace", "hook-probe", "Diagnostic Workspace MCP")["id"]
                target = mailbox.register_agent(args.account_id, session_id, "Test app server")["id"]
                message = mailbox.send(sender, "Code de validation : " + marker,
                                       recipient_agent=target)
            finally:
                mailbox.close()
            answer = turn(session_id, "Recopie uniquement le code WMCP reçu du Workspace MCP "
                          "avec ce tour. Si aucun message n'arrive, réponds ABSENT. Aucun outil.")
            mailbox = Mailbox(args.db)
            try:
                reconcile(mailbox, [{"id": args.account_id,
                                     "codex_home": str(args.codex_home)}])
                receipt = mailbox.db.execute("""SELECT delivered_at,proof FROM message_deliveries
                    WHERE message_id=? AND agent_id=?""",
                    (message["id"], agent_address(args.account_id, session_id))).fetchone()
            finally:
                mailbox.close()
            success = marker in answer and bool(receipt and receipt["proof"])
            if success:
                send("thread/archive", {"threadId": session_id})
            print(json.dumps({"account": args.account_id, "session_id": session_id,
                              "model_saw_context": marker in answer,
                              "context_recorded_by_codex": bool(receipt and receipt["proof"]),
                              "test_chat_archived": success}, ensure_ascii=False))
            return 0 if success else 1
    finally:
        try:
            process.stdin.close()
            process.terminate()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
