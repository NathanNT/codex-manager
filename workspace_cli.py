"""Resumable Codex CLI chats bridged to the shared MCP mailbox over stdio."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from workspace_mailbox import DEFAULT_DB, Mailbox, agent_address, valid_address

ROOT = Path(__file__).resolve().parent
WORKDIR = ROOT / "data" / "cli-workspace"
OPERATOR = agent_address("workspace", "operator")
TURN_TIMEOUT = 180


def codex_command() -> list[str]:
    entry = shutil.which("codex.cmd" if os.name == "nt" else "codex")
    if not entry:
        raise ValueError("Codex CLI est introuvable sur ce PC")
    if Path(entry).suffix.lower() in {".cmd", ".ps1"}:
        script = Path(entry).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        node = shutil.which("node")
        if not node or not script.is_file():
            raise ValueError("Installation Codex CLI incomplète")
        return [node, str(script)]
    return [entry]


def mcp_call(account: dict, db: Path, thread_id: str, name: str, arguments: dict) -> dict:
    """Make a real MCP JSON-RPC tool call with the CLI session's verified identity."""
    command = [sys.executable, str(ROOT / "workspace_mcp.py"), "--account-id", account["id"],
               "--codex-home", account["codex_home"], "--db", str(db)]
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": name, "arguments": arguments, "_meta": {"threadId": thread_id}}},
    ]
    payload = "\n".join(json.dumps(item, ensure_ascii=False) for item in requests) + "\n"
    for attempt in range(5):
        process = subprocess.run(command, input=payload, text=True, capture_output=True,
                                 encoding="utf-8", errors="replace", timeout=20,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if process.returncode:
            raise ValueError(f"Serveur MCP indisponible : {process.stderr[:250]}")
        try:
            result = next(item["result"] for item in (json.loads(line) for line in process.stdout.splitlines())
                          if item.get("id") == 2)
        except (StopIteration, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("Réponse MCP invalide") from exc
        if not result.get("isError"):
            return result["structuredContent"]
        error = result.get("content", [{}])[0].get("text", "Erreur MCP")
        if "inconnu dans ce profil" in error and attempt < 4:
            time.sleep(.25)
            continue
        raise ValueError(error)
    raise ValueError("Session Codex non reconnue par le MCP")


class ManagedCodex:
    def __init__(self, accounts: list[dict], db: Path = DEFAULT_DB):
        self.accounts = {item["id"]: item for item in accounts}
        self.db = Path(db)
        self.processes: dict[str, subprocess.Popen] = {}
        self.lock = threading.RLock()
        WORKDIR.mkdir(parents=True, exist_ok=True)
        mailbox = Mailbox(self.db)
        try:
            for launch in mailbox.managed_launches():
                if launch["session_id"] and launch["state"] == "ready":
                    mailbox.update_managed_launch(launch["launch_id"], "ready")
                    self._start_controller(launch["launch_id"], initial=False)
                elif launch["state"] == "working":
                    mailbox.update_managed_launch(launch["launch_id"], "error",
                        error="Tour interrompu par le redémarrage ; résultat incertain. "
                              "Aucune relance automatique pour éviter de répéter le travail")
                elif launch["state"] == "starting":
                    mailbox.update_managed_launch(launch["launch_id"], "error",
                                                  error="Lancement interrompu par le redémarrage du serveur")
        finally:
            mailbox.close()

    def _start_controller(self, launch_id: str, initial: bool):
        threading.Thread(target=self._controller, args=(launch_id, initial), daemon=True).start()

    def spawn(self, account_id: str, channel_id: str, name: str = "") -> dict:
        if account_id not in self.accounts:
            raise ValueError("Compte Codex inconnu")
        codex_command()
        name = (name or "Agent Codex CLI").strip()[:80]
        if not name:
            raise ValueError("Nom d'agent vide")
        launch_id = "cli-" + uuid.uuid4().hex
        mailbox = Mailbox(self.db)
        try:
            mailbox.create_managed_launch(launch_id, account_id, valid_address(channel_id), name)
            result = mailbox.managed_launch(launch_id)
        finally:
            mailbox.close()
        self._start_controller(launch_id, initial=True)
        return result

    def send(self, launch_id: str, channel_id: str, content: str) -> dict:
        mailbox = Mailbox(self.db)
        try:
            launch = mailbox.managed_launch(launch_id)
            if launch["state"] not in {"ready", "working"} or not launch["session_id"]:
                raise ValueError("Cet agent CLI n'est pas disponible")
            if channel_id != launch["channel_id"]:
                raise ValueError("Cet agent n'est pas lié au channel sélectionné")
            target = agent_address(launch["account_id"], launch["session_id"])
            mailbox.register_agent("workspace", "operator", "Vous", "Dashboard", "idle")
            mailbox.join_channel(OPERATOR, channel_id)
            return mailbox.send(OPERATOR, content, channel_id, target_agent=target)
        finally:
            mailbox.close()

    def stop(self, launch_id: str):
        mailbox = Mailbox(self.db)
        try:
            mailbox.managed_launch(launch_id)
            mailbox.update_managed_launch(launch_id, "stopped")
        finally:
            mailbox.close()
        with self.lock:
            process = self.processes.get(launch_id)
        if process and process.poll() is None:
            process.kill()

    def _controller(self, launch_id: str, initial: bool):
        if initial:
            self._run_turn(launch_id, None)
        while True:
            mailbox = Mailbox(self.db)
            try:
                launch = mailbox.managed_launch(launch_id)
                if launch["state"] in {"error", "stopped"}:
                    return
                next_id = mailbox.next_targeted_message(launch_id) if launch["state"] == "ready" else None
            finally:
                mailbox.close()
            if next_id:
                self._run_turn(launch_id, next_id)
            else:
                time.sleep(1)

    def _run_turn(self, launch_id: str, message_id: int | None):
        mailbox = Mailbox(self.db)
        try:
            launch = mailbox.managed_launch(launch_id)
            if launch["state"] == "stopped":
                return
            account = self.accounts[launch["account_id"]]
            channel_id = launch["channel_id"]
            session_id = launch["session_id"]
            mailbox.update_managed_launch(launch_id, "working")
        finally:
            mailbox.close()
        if session_id:
            try:
                incoming = mcp_call(account, self.db, session_id, "get_message",
                                    {"channel_id": channel_id, "message_id": message_id})
                if incoming["channel_id"] != channel_id:
                    raise ValueError("Message hors du channel de cet agent")
            except (ValueError, KeyError) as exc:
                self._fail(launch_id, f"Lecture du channel via MCP : {exc}")
                return
            prompt = (f"Tu es {launch['name']}. Réponds brièvement au message suivant reçu dans "
                      f"le channel {channel_id}. Le texte final sera envoyé par la passerelle MCP sous ton identité. "
                      "N'utilise aucun outil et ne modifie aucun fichier.\n\n"
                      f"Message #{message_id} : {incoming['content']}")
        else:
            prompt = (f"Tu es {launch['name']}, un agent de messagerie du Workspace MCP. "
                      f"Présente-toi en une phrase courte dans le channel {channel_id}. "
                      "Ton texte final sera envoyé par la passerelle MCP sous ton identité. "
                      "N'utilise aucun outil et ne modifie aucun fichier.")
        command = codex_command() + ["exec", "--json", "--ignore-user-config",
            "--skip-git-repo-check", "--sandbox", "read-only", "-C", str(WORKDIR),
            "-c", "model_reasoning_effort=\"low\""]
        command += ["resume", session_id, "-"] if session_id else ["-"]
        env = os.environ.copy()
        env["CODEX_HOME"] = account["codex_home"]
        for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_CI", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE"):
            env.pop(key, None)
        try:
            process = subprocess.Popen(command, cwd=WORKDIR, env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8", errors="replace", bufsize=1,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, ValueError) as exc:
            self._fail(launch_id, str(exc))
            return
        with self.lock:
            self.processes[launch_id] = process
        timer = threading.Timer(TURN_TIMEOUT, lambda: process.kill() if process.poll() is None else None)
        timer.daemon = True
        timer.start()
        errors, final_text = [], ""
        try:
            assert process.stdin and process.stdout
            process.stdin.write(prompt)
            process.stdin.close()
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    if line.strip(): errors.append(line.strip()[:250])
                    continue
                if event.get("type") == "thread.started" and not session_id:
                    session_id = valid_address(event["thread_id"])
                    mailbox = Mailbox(self.db)
                    try:
                        mailbox.register_agent(account["id"], session_id, launch["name"], "Codex CLI", "working")
                        mailbox.join_channel(agent_address(account["id"], session_id), channel_id)
                        mailbox.update_managed_launch(launch_id, "working", session_id=session_id)
                    finally:
                        mailbox.close()
                elif event.get("type") in {"item.completed", "item_completed"}:
                    item = event.get("item") or {}
                    if item.get("type") in {"agent_message", "AgentMessage"}:
                        final_text = item.get("text") or "".join(part.get("text", "")
                            for part in item.get("content", []) if part.get("type") in {"Text", "text"})
                elif event.get("type") in {"turn.failed", "error"}:
                    errors.append(str(event.get("error") or event.get("message") or event)[:300])
            returncode = process.wait()
        finally:
            timer.cancel()
            with self.lock:
                self.processes.pop(launch_id, None)
        mailbox = Mailbox(self.db)
        try:
            if mailbox.managed_launch(launch_id)["state"] == "stopped":
                return
        finally:
            mailbox.close()
        if returncode or not session_id or not final_text.strip():
            self._fail(launch_id, " ; ".join(errors[-2:]) or "Aucune réponse Codex CLI exploitable")
            return
        try:
            mcp_call(account, self.db, session_id, "send_message",
                     {"channel_id": channel_id, "content": final_text.strip()[:10000],
                      "client_key": f"{launch_id}:{message_id or 'initial'}",
                      **({"reply_to_id": message_id} if message_id else {})})
        except ValueError as exc:
            self._fail(launch_id, f"Envoi via MCP : {exc}")
            return
        mailbox = Mailbox(self.db)
        try:
            if message_id:
                mailbox.mark_delivered(agent_address(account["id"], session_id), [message_id], "mcp")
                mailbox.mark_processed(launch_id, message_id)
            mailbox.update_managed_launch(launch_id, "ready")
        finally:
            mailbox.close()

    def _fail(self, launch_id: str, error: str):
        mailbox = Mailbox(self.db)
        try:
            mailbox.update_managed_launch(launch_id, "error", error=error)
        finally:
            mailbox.close()
