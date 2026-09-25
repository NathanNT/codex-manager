"""Small, read-only Codex App Server client used by the account poller."""

from __future__ import annotations

import json
import os
import queue
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path


def _reader(stream, output: queue.Queue):
    try:
        for line in stream:
            try:
                output.put(json.loads(line))
            except json.JSONDecodeError:
                continue
    finally:
        output.put(None)


def _recent_session_writes(home: Path, thread_ids: set[str], max_age: float = 180.0) -> dict[str, float]:
    """Corroborate recent thread updates without reading unstable rollout contents."""
    session_dir = home / "sessions"
    if not session_dir.is_dir() or not thread_ids:
        return {}
    cutoff = time.time() - max_age
    writes: dict[str, float] = {}
    try:
        for path in session_dir.rglob("*.jsonl"):
            try:
                modified = path.stat().st_mtime
            except OSError:
                continue
            if modified < cutoff:
                continue
            for thread_id in thread_ids:
                # Child rollouts include the parent thread id in their filename.
                if thread_id in path.stem:
                    writes[thread_id] = max(writes.get(thread_id, 0), modified)
    except OSError:
        return writes
    return writes


def _recent_thread_updates(home: Path, thread_ids: set[str], max_age: float = 180.0) -> dict[str, float]:
    """Read only thread IDs and update times from Codex's local state index."""
    database = home / "state_5.sqlite"
    if not database.is_file() or not thread_ids:
        return {}
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=1)
        try:
            placeholders = ",".join("?" for _ in thread_ids)
            rows = connection.execute(
                f"SELECT id, updated_at FROM threads WHERE id IN ({placeholders}) AND updated_at >= ?",
                (*thread_ids, int(time.time() - max_age)),
            )
            return {thread_id: updated for thread_id, updated in rows}
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return {}


def _recent_local_threads(home: Path, max_age: float = 180.0, limit: int = 16) -> list[dict]:
    """Discover user chats only; subagent threads have their own completed turns."""
    database = home / "state_5.sqlite"
    if not database.is_file():
        return []
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=1)
        try:
            rows = connection.execute(
                "SELECT id, name, cwd, updated_at FROM threads "
                "WHERE source IN ('vscode','cli') AND updated_at >= ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (int(time.time() - max_age), limit),
            )
            return [{"id": thread_id, "name": name or "", "cwd": cwd or "",
                     "updated_at": updated_at} for thread_id, name, cwd, updated_at in rows]
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return []


def is_user_thread(home: Path, thread_id: str) -> bool:
    """Accept notification targets only when Codex indexes a top-level chat."""
    database = home / "state_5.sqlite"
    if not database.is_file() or not thread_id:
        return False
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=1)
        try:
            return connection.execute(
                "SELECT 1 FROM threads WHERE id=? AND source IN ('vscode','cli') LIMIT 1",
                (thread_id,),
            ).fetchone() is not None
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return False


def read_account(codex_home: str, timeout: float = 35.0,
                 tracked_thread_ids: tuple[str, ...] = ()) -> dict:
    """Query one account without handling or returning its credentials."""
    home = Path(codex_home)
    if not home.is_dir():
        raise RuntimeError(f"Dossier Codex introuvable : {home}")

    executable = shutil.which("codex.cmd" if os.name == "nt" else "codex")
    if not executable:
        raise RuntimeError("La commande codex est introuvable dans PATH")

    env = os.environ.copy()
    env["CODEX_HOME"] = str(home)
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        [executable, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
        creationflags=creationflags,
    )
    messages: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(process.stdout, messages), daemon=True).start()
    deadline = time.monotonic() + timeout

    def send(message: dict):
        process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        process.stdin.flush()

    def receive(expected: set[int]) -> dict:
        results = {}
        while expected:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex App Server ne répond pas")
            try:
                message = messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("Codex App Server ne répond pas") from exc
            if message is None:
                raise RuntimeError("Codex App Server s'est arrêté")
            message_id = message.get("id")
            if message_id in expected:
                results[message_id] = message
                expected.remove(message_id)
        return results

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "codex_supervision", "title": "Codex Manager", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        }})
        handshake = receive({1})[1]
        if "error" in handshake:
            raise RuntimeError(str(handshake["error"].get("message", "initialisation refusée")))
        send({"method": "initialized", "params": {}})
        requests = {
            2: ("account/read", {"refreshToken": False}),
            3: ("account/rateLimits/read", {}),
            4: ("account/usage/read", {}),
            5: ("thread/list", {"limit": 40, "sortKey": "updated_at", "sortDirection": "desc", "sourceKinds": ["vscode", "cli"]}),
            6: ("hooks/list", {"cwds": [str(Path.cwd())]}),
        }
        for request_id, (method, params) in requests.items():
            send({"id": request_id, "method": method, "params": params})
        replies = receive(set(requests))
        result = {}
        for request_id, (method, _) in requests.items():
            key = {2: "identity", 3: "limits", 4: "usage", 5: "threads", 6: "hooks"}[request_id]
            reply = replies[request_id]
            value = reply.get("result")
            if key == "hooks" and isinstance(value, dict):
                entries = value.get("data") or []
                hooks = [hook for entry in entries for hook in entry.get("hooks", [])]
                value = {"total": len(hooks), "untrusted": sum(hook.get("trustStatus") != "trusted" and hook.get("trustStatus") != "managed" for hook in hooks),
                         "errors": sum(len(entry.get("errors") or []) for entry in entries)}
            result[key] = value
            if "error" in reply:
                result[key + "Error"] = reply["error"].get("message", "Erreur inconnue")
        recent_threads = (result.get("threads") or {}).get("data") or []
        unique_threads = {}
        for thread in recent_threads:
            thread_id = thread.get("id")
            if thread_id and (thread_id not in unique_threads or
                              max(thread.get("updatedAt") or 0, thread.get("recencyAt") or 0) >
                              max(unique_threads[thread_id].get("updatedAt") or 0,
                                  unique_threads[thread_id].get("recencyAt") or 0)):
                unique_threads[thread_id] = thread
        recent_threads = sorted(unique_threads.values(), key=lambda thread:
                                max(thread.get("updatedAt") or 0, thread.get("recencyAt") or 0),
                                reverse=True)[:16]
        if isinstance(result.get("threads"), dict):
            result["threads"]["data"] = recent_threads
        result["session_writes"] = _recent_session_writes(home, {thread["id"] for thread in recent_threads})
        result["thread_updates"] = _recent_thread_updates(home, {thread["id"] for thread in recent_threads})
        turn_requests = {}
        goal_requests = {}
        for index, thread in enumerate(recent_threads[:12]):
            request_id = 100 + index
            turn_requests[request_id] = thread
            send({"id": request_id, "method": "thread/turns/list", "params": {
                "threadId": thread["id"], "limit": 1, "itemsView": "notLoaded", "sortDirection": "desc"
            }})
        recent_ids = {thread["id"] for thread in recent_threads}
        result["tracked_goals"] = [{"id": thread_id} for thread_id in tracked_thread_ids
                                   if thread_id not in recent_ids][:24]
        for index, thread in enumerate(recent_threads + result["tracked_goals"]):
            request_id = 200 + index
            goal_requests[request_id] = thread
            send({"id": request_id, "method": "thread/goal/get", "params": {"threadId": thread["id"]}})
        detail_replies = receive(set(goal_requests) | set(turn_requests)) if goal_requests else {}
        if goal_requests:
            for request_id, thread in goal_requests.items():
                reply = detail_replies[request_id]
                if "error" in reply:
                    thread["goalError"] = reply["error"].get("message", "Goal indisponible")
                else:
                    thread["goal"] = (reply.get("result") or {}).get("goal")
        result["recent_turns"] = []
        if turn_requests:
            for request_id, thread in turn_requests.items():
                turns = (detail_replies[request_id].get("result") or {}).get("data") or []
                if not turns:
                    continue
                turn = turns[0]
                error = turn.get("error") or {}
                result["recent_turns"].append({
                    "thread_id": thread["id"], "turn_id": turn.get("id"),
                    "title": thread.get("name") or thread.get("preview") or "Conversation Codex",
                    "cwd": thread.get("cwd") or "", "status": turn.get("status"),
                    "started_at": turn.get("startedAt"), "completed_at": turn.get("completedAt"),
                    "thread_updated_at": thread.get("updatedAt"),
                    "error_message": error.get("message") if isinstance(error, dict) else None,
                })
        return result
    finally:
        try:
            process.stdin.close()
        except (OSError, AttributeError):
            pass
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()


def read_recent_turns(codex_home: str, threads: list[dict], timeout: float = 12.0) -> list[dict]:
    """Check known active chats without reloading quotas, usage, hooks or goals."""
    if not threads:
        return []
    executable = shutil.which("codex.cmd" if os.name == "nt" else "codex")
    if not executable:
        raise RuntimeError("La commande codex est introuvable dans PATH")
    env = os.environ.copy()
    env["CODEX_HOME"] = codex_home
    process = subprocess.Popen(
        [executable, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
        env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    messages: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(process.stdout, messages), daemon=True).start()
    deadline = time.monotonic() + timeout

    def send(message: dict):
        process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        process.stdin.flush()

    def receive(expected: set[int]) -> dict:
        result = {}
        while expected:
            try:
                message = messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise TimeoutError("Codex App Server ne répond pas") from exc
            if message is None:
                raise RuntimeError("Codex App Server s'est arrêté")
            if message.get("id") in expected:
                result[message["id"]] = message
                expected.remove(message["id"])
        return result

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "codex_supervision", "title": "Codex Manager", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        }})
        handshake = receive({1})[1]
        if "error" in handshake:
            raise RuntimeError(str(handshake["error"].get("message", "initialisation refusée")))
        send({"method": "initialized", "params": {}})
        for index, thread in enumerate(threads):
            send({"id": index + 2, "method": "thread/turns/list", "params": {
                "threadId": thread["id"], "limit": 1, "itemsView": "notLoaded", "sortDirection": "desc",
            }})
        replies = receive(set(range(2, len(threads) + 2)))
        result = []
        for index, thread in enumerate(threads):
            turns = (replies[index + 2].get("result") or {}).get("data") or []
            if not turns:
                continue
            turn = turns[0]
            error = turn.get("error") or {}
            result.append({
                "thread_id": thread["id"], "turn_id": turn.get("id"),
                "title": thread.get("name") or "Conversation Codex", "cwd": thread.get("cwd") or "",
                "status": turn.get("status"), "started_at": turn.get("startedAt"),
                "completed_at": turn.get("completedAt"),
                "error_message": error.get("message") if isinstance(error, dict) else None,
            })
        return result
    finally:
        try:
            process.stdin.close()
        except (OSError, AttributeError):
            pass
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
