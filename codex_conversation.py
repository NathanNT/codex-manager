"""Read the latest Codex answer through a restricted local App Server client."""

from __future__ import annotations

import json
import os
import queue
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path

from codex_rpc import _reader


def indexed_chat(home: str | Path, thread_id: str) -> dict:
    """Resolve a user chat inside one account, never trusting Telegram metadata."""
    try:
        if str(uuid.UUID(thread_id)) != thread_id:
            raise ValueError("Identifiant de chat invalide")
    except (ValueError, AttributeError) as exc:
        raise ValueError("Identifiant de chat invalide") from exc
    path = Path(home) / "state_5.sqlite"
    try:
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)) as db:
            row = db.execute("SELECT name,cwd,source FROM threads WHERE id=?", (thread_id,)).fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError("Index des chats Codex indisponible") from exc
    if not row or row[2] not in {"vscode", "cli"}:
        raise ValueError("Chat absent de ce compte")
    return {"id": thread_id, "name": row[0] or "Conversation Codex", "cwd": row[1] or ""}


def final_answer(turns: list[dict]) -> tuple[str, dict | None]:
    """Return the newest completed user-facing answer, excluding tool output."""
    for turn in turns:
        if turn.get("status") != "completed":
            continue
        messages = [item for item in turn.get("items") or []
                    if item.get("type") == "agentMessage" and isinstance(item.get("text"), str)
                    and item["text"].strip()]
        finals = [item for item in messages if item.get("phase") == "final_answer"]
        if finals:
            return finals[-1]["text"].strip(), turn
        if messages:
            return messages[-1]["text"].strip(), turn
    return "", None


class AppServerClient:
    def __init__(self, home: str | Path, timeout: float = 25.0):
        self.home = str(home)
        self.timeout = timeout
        self.process = None
        self.messages = queue.Queue()
        self.next_id = 1

    def __enter__(self):
        executable = shutil.which("codex.cmd" if os.name == "nt" else "codex")
        if not executable:
            raise RuntimeError("La commande Codex est introuvable")
        env = os.environ.copy()
        env["CODEX_HOME"] = self.home
        self.process = subprocess.Popen(
            [executable, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        threading.Thread(target=_reader, args=(self.process.stdout, self.messages), daemon=True).start()
        try:
            self._request("initialize", {"clientInfo": {"name": "codex_supervision",
                         "title": "Codex Manager Telegram", "version": "0.1.0"},
                         "capabilities": {"experimentalApi": True}}, timeout=self.timeout)
            self._send({"method": "initialized", "params": {}})
        except Exception:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_):
        if self.process:
            try:
                self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.terminate()
            except OSError:
                pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def _send(self, message: dict):
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _request(self, method: str, params: dict, timeout: float = 25.0) -> dict:
        if method not in {"initialize", "thread/turns/list", "thread/goal/get"}:
            raise ValueError("Méthode Codex interdite en lecture seule")
        request_id = self.next_id
        self.next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex App Server ne répond pas")
            try:
                message = self.messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("Codex App Server ne répond pas") from exc
            if message is None:
                raise RuntimeError("Codex App Server s'est arrêté")
            if "method" in message and "id" in message:
                self._send({"id": message["id"], "error": {"code": -32601,
                            "message": "Client en lecture seule"}})
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(str(message["error"].get("message") or "Codex a refusé la requête")[:250])
                return message.get("result") or {}

    def turns(self, thread_id: str, limit: int = 8, items_view: str = "full") -> list[dict]:
        return self._request("thread/turns/list", {"threadId": thread_id, "limit": limit,
                            "itemsView": items_view, "sortDirection": "desc"}).get("data") or []

    def goal(self, thread_id: str) -> dict | None:
        """Read the current official goal state without changing the chat."""
        return self._request("thread/goal/get", {"threadId": thread_id}, timeout=self.timeout).get("goal")


def read_latest_answer(home: str | Path, thread_id: str) -> tuple[str, dict]:
    chat = indexed_chat(home, thread_id)
    with AppServerClient(home) as client:
        turns = client.turns(thread_id)
        answer, _ = final_answer(turns)
    chat["latest_turn_status"] = turns[0].get("status") if turns else None
    return answer, chat


def read_thread_goal(home: str | Path, thread_id: str) -> dict | None:
    indexed_chat(home, thread_id)
    with AppServerClient(home, timeout=8) as client:
        return client.goal(thread_id)
