"""Read-only diagnostics for a Codex Workspace agent."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

from workspace_mailbox import Mailbox, valid_address


def _check(status: str, detail: str, at: str | None = None) -> dict:
    return {"status": status, "detail": detail, "at": at}


def _run(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                              timeout=5, check=False,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired):
        return None


def probe_github(workspace: str) -> dict:
    """Run from the caller's process environment; no output or secrets are returned."""
    cwd = workspace if workspace and Path(workspace).is_dir() else None
    result = {**_check("unknown", "Authentification GitHub non vérifiée"),
              "checks": {"cli": _check("unknown", "GitHub CLI non vérifié"),
                         "repository": _check("unknown", "Accès API au dépôt non vérifié"),
                         "git": _check("unknown", "Transport Git non vérifié")}}
    gh_available = bool(shutil.which("gh"))
    if gh_available:
        auth = _run("gh", "auth", "status", "--active", "--hostname", "github.com", cwd=cwd)
        if auth is None:
            result["checks"]["cli"] = _check("unknown", "Contrôle GitHub CLI expiré ou indisponible")
        elif auth.returncode:
            result["checks"]["cli"] = _check("incomplete", "GitHub CLI non authentifié")
        else:
            result["checks"]["cli"] = _check("ok", "Authentification GitHub CLI vérifiée")
            result["status"] = "ok"
            result["detail"] = "GitHub CLI authentifié dans l'environnement de cet agent"
            if cwd:
                repo = _run("gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner", cwd=cwd)
                if repo and repo.returncode == 0:
                    result["repository"] = repo.stdout.strip()[:200]
                    result["checks"]["repository"] = _check("ok", "Accès API au dépôt vérifié")
    else:
        result["checks"]["cli"] = _check("incomplete", "GitHub CLI non installé")
    if cwd and shutil.which("git"):
        remote = _run("git", "remote", "get-url", "origin", cwd=cwd)
        if remote and remote.returncode == 0:
            transport = _run("git", "ls-remote", "origin", "HEAD", cwd=cwd)
            if transport and transport.returncode == 0:
                result["checks"]["git"] = _check("ok", "Lecture du remote Git réussie")
                if result["status"] != "ok":
                    result["status"] = "ok"
                    result["detail"] = "Accès Git au dépôt vérifié ; état GitHub CLI séparé"
            else:
                result["checks"]["git"] = _check("unknown", "Lecture du remote Git non confirmée")
        elif remote is None:
            result["checks"]["git"] = _check("unknown", "Contrôle Git expiré ou indisponible")
        else:
            result["checks"]["git"] = _check("unknown", "Aucun remote origin dans ce dossier")
    elif cwd:
        result["checks"]["git"] = _check("incomplete", "Git non installé")
    if result["status"] != "ok" and result["checks"]["cli"]["status"] == "incomplete":
        result["status"] = "incomplete"
        result["detail"] = "Aucune authentification GitHub vérifiée dans cet environnement"
    return result


def diagnose(store, agent_id: str, mailbox: Mailbox, probe_host: bool = True) -> dict:
    """Report observable facts. Never return config content or process output."""
    agent_id = valid_address(agent_id)
    active = {agent["id"]: agent for agent in mailbox.active_agents()}
    if agent_id not in active:
        raise ValueError("Agent indisponible")
    agent = mailbox.agent(agent_id)
    account = next((item for item in store.accounts if item["id"] == agent["account_id"]), None)
    if not account:
        raise ValueError("Compte inconnu")
    home = Path(account["codex_home"])
    config = {}
    try:
        config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        pass

    mcp_configured = bool((config.get("mcp_servers") or {}).get("codex_workspace"))
    last_mcp = agent.get("mcp_last_seen")
    if not mcp_configured:
        mcp = _check("incomplete", "Serveur codex_workspace absent de ce profil")
    elif last_mcp:
        mcp = _check("ok", "Appel MCP observé pour ce chat", last_mcp)
    else:
        mcp = _check("unknown", "Configuré ; aucun appel observé pour ce chat")

    hook_file = home / "hooks.json"
    try:
        hooks = json.loads(hook_file.read_text(encoding="utf-8-sig")).get("hooks", {})
    except (OSError, ValueError):
        hooks = {}
    def installed(event: str) -> bool:
        return any("hook_sender.py" in str(item.get("command", ""))
                   for group in hooks.get(event, []) for item in group.get("hooks", []))
    hook_configured = installed("UserPromptSubmit") and installed("PostToolUse")
    with store.lock:
        row = store.db.execute("""SELECT MAX(at) FROM events
            WHERE account_id=? AND type IN ('UserPromptSubmit','PostToolUse')
            AND json_extract(details,'$.session_id')=?""",
            (agent["account_id"], agent["session_id"])).fetchone()
    last_hook = row[0] if row and row[0] else None
    if not hook_configured:
        hook = _check("incomplete", "Hooks de réception absents de ce profil")
    elif last_hook:
        hook = _check("ok", "Exécution observée pour ce chat", time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(last_hook)))
    else:
        hook = _check("unknown", "Configurés ; approbation et exécution non vérifiées pour ce chat")
    current_definition_executed = bool(last_hook and hook_file.is_file() and
                                       last_hook >= hook_file.stat().st_mtime)
    hook["checks"] = {
        "installation": _check("ok" if hook_configured else "incomplete",
                               "UserPromptSubmit et PostToolUse présents" if hook_configured
                               else "Un ou plusieurs hooks de réception sont absents"),
        "execution": _check("ok" if last_hook else "unknown",
                            "Événement observé pour ce chat" if last_hook
                            else "Aucune exécution observée pour ce chat", hook.get("at")),
        "approval": _check("ok" if current_definition_executed else "unknown",
                           "Configuration actuelle exécutée par Codex" if current_definition_executed
                           else "Confiance explicite non exposée ; vérifier /hooks si nécessaire"),
    }
    verifications = mailbox.hook_verifications(agent["account_id"])
    for event, label in (("UserPromptSubmit", "prompt"), ("PostToolUse", "post_tool")):
        verified = next((item for item in verifications
                         if item["client"] == "cli" and item["event"] == event), None)
        hook["checks"][label] = (_check("ok", "Contexte reconnu par un modèle Codex CLI de ce profil",
                                         verified["verified_at"]) if verified else
                                 _check("unknown", "Réception par le modèle CLI non testée"))
    vscode_proof = next((item for item in verifications
                         if item["client"] == "vscode" and item["session_id"] == agent["session_id"]), None)
    vscode_receipts = {}
    for row in mailbox.db.execute("""SELECT event,verified_at
        FROM hook_emissions WHERE agent_id=? AND verified_at IS NOT NULL
        ORDER BY verified_at DESC""", (agent_id,)):
        vscode_receipts.setdefault(row["event"], row)
    for event, key in (("UserPromptSubmit", "vscode_prompt"), ("PostToolUse", "vscode_post_tool")):
        receipt = vscode_receipts.get(event)
        hook["checks"][key] = (_check("ok", "Contexte enregistré par Codex dans ce chat VS Code",
                                      receipt["verified_at"]) if receipt else
                                _check("unknown", "Injection dans ce chat VS Code non confirmée"))
    latest_receipt = max(vscode_receipts.values(), key=lambda row: row["verified_at"], default=None)
    hook["checks"]["vscode"] = (_check("ok",
        f"Contexte {vscode_proof['event']} observé par le modèle dans ce chat",
        vscode_proof["verified_at"]) if vscode_proof else
        _check("ok", "Contexte enregistré par Codex dans cette session ; interprétation par le modèle non vérifiée",
               latest_receipt["verified_at"]) if latest_receipt else
        _check("unknown", "Aucune injection vérifiée dans cette session VS Code"))

    latest_attempt = mailbox.db.execute("""SELECT prepared_at,verified_at,proof FROM hook_emissions
        WHERE agent_id=? ORDER BY prepared_at DESC,rowid DESC LIMIT 1""", (agent_id,)).fetchone()
    if latest_attempt and latest_attempt["verified_at"]:
        delivery = _check("ok", "Contexte enregistré par Codex dans cette session ; "
                          + (latest_attempt["proof"] or "preuve de session conservée"),
                          latest_attempt["verified_at"])
    elif latest_attempt:
        delivery = _check("unknown", "Sortie du hook préparée ; réception par Codex non confirmée. "
                          "Le message reste en attente.", latest_attempt["prepared_at"])
    else:
        delivery = _check("unknown", "Aucune transmission automatique vérifiée pour ce chat")

    recorded = mailbox.agent_check(agent_id)
    if recorded:
        github = _check(recorded["github_status"], recorded["github_detail"], recorded["checked_at"])
        if recorded["repository"]:
            github["repository"] = recorded["repository"]
        github["checks"] = recorded["checks"]
    else:
        if probe_host:
            host_probe = probe_github(active[agent_id].get("workspace") or "")
            github = _check("unknown", "Contrôle dans le chat non observé. Hôte : " +
                            host_probe["checks"]["cli"]["detail"])
        else:
            github = _check("unknown", "Contrôle GitHub non encore effectué dans ce chat")
    github_mcp_configured = any("github" in name.lower() for name in
                                (config.get("mcp_servers") or {}))
    github_mcp = _check("unknown", "Configuré ; aucun appel vérifiable par ce moniteur" if github_mcp_configured
                        else "Non configuré dans ce profil (optionnel)")
    return {"agent_id": agent_id, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "github": github, "github_mcp": github_mcp,
            "mcp": mcp, "hooks": hook, "delivery": delivery}
