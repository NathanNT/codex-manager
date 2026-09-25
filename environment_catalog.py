"""Local inventory and carefully verified Codex conversation copies.

Only names, versions and aggregate chat metadata leave this module through HTTP.
Credentials, MCP URLs, conversation bodies and rollout paths stay on disk.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import tomllib
from contextlib import closing
from pathlib import Path

from codex_rpc import _reader


THREAD_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{27,36}$")
NUMBERED_TITLE = re.compile(r"^(?P<base>.+?)\s*\((?P<number>\d+)\)$")


def _database(home: Path):
    path = home / "state_5.sqlite"
    return sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True, timeout=2)


def _config(home: Path) -> dict:
    try:
        return tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _skill_name(path: Path) -> str:
    try:
        header = path.read_text(encoding="utf-8")[:1500]
        match = re.search(r"(?m)^name:\s*['\"]?([^'\"\r\n]+)", header)
        return match.group(1).strip() if match else path.parent.name
    except OSError:
        return path.parent.name


def inventory(home: Path) -> dict:
    """Inventory locally installed files and configured MCP servers without secrets."""
    config = _config(home)
    disabled = {str(Path(item["path"]).resolve()).casefold()
                for item in (config.get("skills") or {}).get("config", [])
                if isinstance(item, dict) and item.get("enabled") is False and item.get("path")}
    plugins = []
    plugin_skills = []
    cache = home / "plugins" / "cache"
    if cache.is_dir():
        for manifest in cache.glob("*/*/*/.codex-plugin/plugin.json"):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                name = str(data.get("name") or manifest.parents[2].name)
                version = str(data.get("version") or manifest.parents[1].name)
            except (OSError, ValueError):
                continue
            plugins.append({"name": name, "version": version, "source": manifest.parents[3].name})
            plugin_root = manifest.parent.parent
            for skill in plugin_root.glob("skills/*/SKILL.md"):
                plugin_skills.append({"name": _skill_name(skill), "version": version,
                                      "source": name, "enabled": True})
    plugins.sort(key=lambda item: item["name"].casefold())

    skills = []
    roots = [(home / "skills", "Compte"), (Path.home() / ".agents" / "skills", "Global")]
    for root, source in roots:
        if not root.is_dir():
            continue
        for skill in list(root.glob("*/SKILL.md")) + list(root.glob(".system/*/SKILL.md")):
            skills.append({"name": _skill_name(skill), "version": None, "source": source,
                           "enabled": str(skill.resolve()).casefold() not in disabled})
    skills.extend(plugin_skills)
    skills.sort(key=lambda item: (item["name"].casefold(), item["source"]))

    mcp = []
    for name, item in (config.get("mcp_servers") or {}).items():
        if not isinstance(item, dict):
            continue
        mcp.append({"name": name, "version": item.get("version") if isinstance(item.get("version"), str) else None,
                    "transport": "HTTP" if "url" in item else "stdio",
                    "enabled": item.get("enabled") is not False})
    mcp.sort(key=lambda item: item["name"].casefold())
    executable = shutil.which("codex.cmd" if os.name == "nt" else "codex")
    try:
        version = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                 timeout=4, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0).stdout.strip() if executable else None
    except (OSError, subprocess.TimeoutExpired):
        version = None
    return {"plugins": plugins, "skills": skills, "mcp": mcp, "codex_version": version}


def chat_catalog(home: Path, limit: int = 50, offset: int = 0, search: str = "",
                 min_tokens: int | None = None, max_tokens: int | None = None,
                 date_from: int | None = None, date_to: int | None = None,
                 archived: bool = False) -> dict:
    """User conversations only. Internal guardian and subagent threads are excluded."""
    limit = min(max(int(limit), 1), 100)
    offset = min(max(int(offset), 0), 100000)
    search = search.strip()[:100]
    with closing(_database(home)) as db:
        db.row_factory = sqlite3.Row
        where = "source IN ('vscode','cli') AND archived=?"
        args: list = [int(archived)]
        if search:
            where += " AND (name LIKE ? OR title LIKE ? OR cwd LIKE ?)"
            args.extend([f"%{search}%"] * 3)
        for field, operator, value in (("tokens_used", ">=", min_tokens),
                                       ("tokens_used", "<=", max_tokens),
                                       ("updated_at", ">=", date_from),
                                       ("updated_at", "<", date_to)):
            if value is not None:
                if not isinstance(value, int) or value < 0:
                    raise ValueError("Filtre numérique invalide")
                where += f" AND COALESCE({field},0) {operator} ?"
                args.append(value)
        count = db.execute(f"SELECT COUNT(*) FROM threads WHERE {where}", args).fetchone()[0]
        rows = db.execute(
            "SELECT id, COALESCE(NULLIF(name,''),NULLIF(title,''),'Conversation Codex') AS label, "
            "cwd, source, updated_at, tokens_used, is_pinned FROM threads WHERE " + where +
            " ORDER BY updated_at DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        distribution = db.execute(
            "SELECT cwd,COUNT(*) AS total FROM threads WHERE source IN ('vscode','cli') "
            "AND archived=? GROUP BY cwd ORDER BY total DESC LIMIT 8", (int(archived),)).fetchall()
        totals = db.execute(
            "SELECT source,COUNT(*) FROM threads WHERE source IN ('vscode','cli') "
            "AND archived=? GROUP BY source", (int(archived),)).fetchall()
    return {
        "total": count, "offset": offset, "limit": limit, "archived": archived,
        "by_source": dict(totals),
        "by_workspace": [{"workspace": row[0] or "Sans projet", "count": row[1]} for row in distribution],
        "chats": [{"id": row["id"], "title": row["label"][:160],
                   "workspace": row["cwd"] or "", "source": row["source"],
                   "updated_at": row["updated_at"], "tokens": row["tokens_used"],
                   "pinned": bool(row["is_pinned"])} for row in rows],
    }


def duplicate_plan(home: Path, active_ids: set[str] | None = None) -> dict:
    """Find numbered title families inside the same Codex account and workspace."""
    active_ids = active_ids or set()
    with closing(_database(home)) as db:
        rows = db.execute("SELECT id,COALESCE(NULLIF(name,''),NULLIF(title,''),'') AS label,"
                          "cwd,updated_at,is_pinned FROM threads WHERE source IN ('vscode','cli') "
                          "AND archived=0").fetchall()
    families: dict[tuple[str, str], list[dict]] = {}
    for thread_id, label, cwd, updated_at, pinned in rows:
        label = label.strip()
        match = NUMBERED_TITLE.fullmatch(label)
        base = match.group("base").strip() if match else label
        if not base:
            continue
        families.setdefault((base.casefold(), (cwd or "").casefold()), []).append({
            "id": thread_id, "label": label, "base": base, "updated_at": updated_at or 0,
            "number": int(match.group("number")) if match else -1, "pinned": bool(pinned)})
    groups = []
    skipped = 0
    for family in families.values():
        if len(family) < 2 or not any(item["number"] >= 0 for item in family):
            continue
        latest = max(family, key=lambda item: (item["updated_at"], item["number"], item["id"]))
        older = [item for item in family if item["id"] != latest["id"]]
        if any(item["id"] in active_ids or item["pinned"] or
               (home / "thread-writer-locks" / f"{item['id']}.lock").exists()
               for item in family):
            skipped += 1
            continue
        groups.append({"title": latest["base"], "keep": latest["id"],
                       "current_title": latest["label"],
                       "archive": [item["id"] for item in older],
                       "archive_count": len(older)})
    groups.sort(key=lambda item: item["title"].casefold())
    return {"groups": groups, "archive_count": sum(item["archive_count"] for item in groups),
            "skipped_groups": skipped}


class CodexThreadClient:
    """Short-lived official app-server connection for thread metadata operations."""

    def __init__(self, home: Path):
        executable = shutil.which("codex.cmd" if os.name == "nt" else "codex")
        if not executable:
            raise RuntimeError("CLI Codex introuvable")
        env = os.environ.copy()
        env["CODEX_HOME"] = str(home)
        self.process = subprocess.Popen(
            [executable, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.messages: queue.Queue = queue.Queue()
        self.sequence = 0
        threading.Thread(target=_reader, args=(self.process.stdout, self.messages), daemon=True).start()
        self.request("initialize", {"clientInfo": {"name": "codex_supervision", "version": "0.1.0"},
                                    "capabilities": {"experimentalApi": True}})
        self.process.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict):
        self.sequence += 1
        request_id = self.sequence
        self.process.stdin.write(json.dumps({"id": request_id, "method": method,
                                             "params": params}, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                message = self.messages.get(timeout=max(.1, deadline - time.monotonic()))
            except queue.Empty:
                break
            if message is None:
                break
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(str(message["error"].get("message", "Erreur Codex"))[:180])
                return message.get("result") or {}
        raise TimeoutError(f"Réponse Codex expirée : {method}")

    def close(self):
        try:
            self.process.stdin.close()
            self.process.terminate()
            self.process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()


def change_archive(home: Path, thread_ids: list[str], archive: bool,
                   active_ids: set[str] | None = None, client_factory=CodexThreadClient) -> list[dict]:
    """Archive or restore selected native threads without deleting their histories."""
    if not isinstance(thread_ids, list) or not 1 <= len(thread_ids) <= 100 or len(set(thread_ids)) != len(thread_ids):
        raise ValueError("Sélectionnez entre 1 et 100 chats distincts")
    if any(not isinstance(item, str) or not THREAD_ID.fullmatch(item) for item in thread_ids):
        raise ValueError("Identifiant de chat invalide")
    active_ids = active_ids or set()
    results = []
    with _TRANSFER_LOCK:
        with closing(_database(home)) as db:
            known = {row[0] for row in db.execute(
                "SELECT id FROM threads WHERE source IN ('vscode','cli') AND archived=?",
                (int(not archive),)).fetchall()}
        client = client_factory(home)
        try:
            for thread_id in thread_ids:
                if thread_id not in known:
                    results.append({"id": thread_id, "status": "error", "message": "Chat introuvable dans cette vue"})
                    continue
                if archive and (thread_id in active_ids or
                                (home / "thread-writer-locks" / f"{thread_id}.lock").exists()):
                    results.append({"id": thread_id, "status": "error", "message": "Chat encore actif dans Codex"})
                    continue
                try:
                    client.request("thread/archive" if archive else "thread/unarchive", {"threadId": thread_id})
                    results.append({"id": thread_id, "status": "archived" if archive else "restored"})
                except (OSError, RuntimeError, TimeoutError) as exc:
                    results.append({"id": thread_id, "status": "error", "message": str(exc)[:180]})
        finally:
            client.close()
    return results


def deduplicate_chats(home: Path, active_ids: set[str] | None = None,
                      client_factory=CodexThreadClient) -> dict:
    """Rename each newest numbered chat and archive its older siblings via Codex."""
    results = []
    with _TRANSFER_LOCK:
        plan = duplicate_plan(home, active_ids)
        if not plan["groups"]:
            return {"results": [], "skipped_groups": plan["skipped_groups"]}
        client = client_factory(home)
        try:
            for group in plan["groups"]:
                entry = {"title": group["title"], "kept": group["keep"], "archived": 0}
                try:
                    if group["current_title"] != group["title"]:
                        client.request("thread/name/set", {"threadId": group["keep"], "name": group["title"]})
                    for thread_id in group["archive"]:
                        try:
                            client.request("thread/archive", {"threadId": thread_id})
                            entry["archived"] += 1
                        except (OSError, RuntimeError, TimeoutError) as exc:
                            entry.setdefault("errors", []).append({"id": thread_id, "message": str(exc)[:180]})
                except (OSError, RuntimeError, TimeoutError) as exc:
                    entry.setdefault("errors", []).append({"id": group["keep"], "message": str(exc)[:180]})
                results.append(entry)
        finally:
            client.close()
    return {"results": results, "skipped_groups": plan["skipped_groups"]}


def _safe_rollout(home: Path, stored_path: str) -> tuple[Path, Path]:
    # Old indexes sometimes use Windows extended paths (\\?\C:\...).
    value = stored_path.removeprefix("\\\\?\\")
    source = Path(value).resolve(strict=True)
    sessions = (home / "sessions").resolve(strict=True)
    if not source.is_relative_to(sessions) or source.suffix != ".jsonl":
        raise ValueError("Historique hors du dossier sessions")
    return source, source.relative_to(sessions)


def _read_thread(home: Path, thread_id: str, timeout: float = 20.0) -> bool:
    executable = shutil.which("codex.cmd" if os.name == "nt" else "codex")
    if not executable:
        raise RuntimeError("CLI Codex introuvable")
    env = os.environ.copy()
    env["CODEX_HOME"] = str(home)
    process = subprocess.Popen(
        [executable, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
        env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    messages: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(process.stdout, messages), daemon=True).start()
    deadline = time.monotonic() + timeout

    def send(message):
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def receive(request_id):
        while time.monotonic() < deadline:
            try:
                message = messages.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                break
            if message is None:
                break
            if message.get("id") == request_id:
                return message
        raise TimeoutError("Vérification Codex expirée")

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "codex_supervision", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True}}})
        if "error" in receive(1):
            return False
        send({"method": "initialized", "params": {}})
        # Codex indexes copied rollouts on startup, but thread/read returns no
        # turns until the history has been loaded by thread/resume.
        send({"id": 2, "method": "thread/resume", "params": {"threadId": thread_id}})
        if "error" in receive(2):
            return False
        send({"id": 3, "method": "thread/read", "params": {"threadId": thread_id, "includeTurns": True}})
        reply = receive(3)
        thread = (reply.get("result") or {}).get("thread") or {}
        return thread.get("id") == thread_id and bool(thread.get("turns"))
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()


_TRANSFER_LOCK = threading.Lock()


def transfer_chats(source_home: Path, target_home: Path, thread_ids: list[str], active_ids: set[str] | None = None,
                   verify=_read_thread) -> list[dict]:
    """Copy selected native rollouts, preserving the source; verify with the target App Server."""
    if source_home.resolve() == target_home.resolve():
        raise ValueError("Choisissez deux comptes différents")
    if not isinstance(thread_ids, list) or not 1 <= len(thread_ids) <= 10 or len(set(thread_ids)) != len(thread_ids):
        raise ValueError("Sélectionnez entre 1 et 10 chats distincts")
    if any(not isinstance(item, str) or not THREAD_ID.fullmatch(item) for item in thread_ids):
        raise ValueError("Identifiant de chat invalide")
    active_ids = active_ids or set()
    results = []
    with _TRANSFER_LOCK:
        with closing(_database(source_home)) as src_db, closing(_database(target_home)) as dst_db:
            for thread_id in thread_ids:
                created = False
                row = src_db.execute(
                    "SELECT rollout_path,updated_at FROM threads WHERE id=? "
                    "AND source IN ('vscode','cli') AND archived=0", (thread_id,)).fetchone()
                if not row or not row[0]:
                    results.append({"id": thread_id, "status": "error", "message": "Chat source introuvable"})
                    continue
                if thread_id in active_ids:
                    results.append({"id": thread_id, "status": "error", "message": "Chat encore actif : attendez la fin du tour"})
                    continue
                if (source_home / "thread-writer-locks" / f"{thread_id}.lock").exists():
                    results.append({"id": thread_id, "status": "error", "message": "Chat ouvert en écriture dans Codex"})
                    continue
                if dst_db.execute("SELECT 1 FROM threads WHERE id=?", (thread_id,)).fetchone():
                    results.append({"id": thread_id, "status": "exists", "message": "Déjà présent dans le compte cible"})
                    continue
                try:
                    source, relative = _safe_rollout(source_home, row[0])
                    if source.stat().st_mtime > time.time() - 20:
                        raise ValueError("Historique encore en cours d'écriture")
                    target_root = (target_home / "sessions").resolve()
                    target = target_root / relative
                    if target.exists():
                        raise ValueError("Un fichier du même nom existe déjà")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.parent.resolve().is_relative_to(target_root):
                        raise ValueError("Destination hors du dossier sessions")
                    before = source.stat()
                    # Exclusive creation prevents replacing any destination history.
                    with source.open("rb") as stream, target.open("xb") as output:
                        created = True
                        shutil.copyfileobj(stream, output)
                    after = source.stat()
                    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                        raise ValueError("Le chat a changé pendant la copie")
                    if not verify(target_home, thread_id):
                        raise ValueError("Codex ne retrouve pas le chat dans la destination")
                    results.append({"id": thread_id, "status": "copied", "message": "Historique copié et lu par Codex"})
                except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                    message = str(exc)[:150]
                    if created:
                        # App Server may have indexed the new file before a later
                        # verification error. Retain it if so: deleting it would
                        # leave a broken index entry in the destination account.
                        try:
                            indexed = dst_db.execute("SELECT 1 FROM threads WHERE id=?", (thread_id,)).fetchone()
                        except sqlite3.Error:
                            indexed = True
                        if indexed:
                            message += " · copie non vérifiée conservée dans le compte cible"
                        else:
                            try:
                                target.unlink(missing_ok=True)
                            except OSError:
                                message += " · nettoyage manuel requis dans le compte cible"
                    results.append({"id": thread_id, "status": "error", "message": message})
                finally:
                    if "target" in locals():
                        del target
    return results
