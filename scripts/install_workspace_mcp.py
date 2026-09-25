"""Register the shared local mailbox MCP server in both Codex profiles."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tomllib
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "workspace_mcp.py"
DATABASE = ROOT / "data" / "workspace-mailbox.sqlite"


def install(config_path: Path, account_id: str) -> str:
    original = config_path.read_text(encoding="utf-8")
    config = tomllib.loads(original)
    existing = config.get("mcp_servers", {}).get("codex_workspace")
    args = [str(SERVER), "--account-id", account_id, "--codex-home", str(config_path.parent),
            "--db", str(DATABASE)]
    block = "\n[mcp_servers.codex_workspace]\n" + \
        f"command = {json.dumps(sys.executable)}\n" + \
        f"args = {json.dumps(args, ensure_ascii=False)}\n"
    if existing:
        if existing.get("command") == sys.executable and existing.get("args") == args:
            return "déjà configuré"
        old_args = existing.get("args")
        old_layout = (isinstance(old_args, list) and len(old_args) == 5
                      and old_args[1:3] == ["--agent-id", account_id]
                      and old_args[3] == "--db")
        current_layout = (isinstance(old_args, list) and len(old_args) == 7
                          and old_args[1:3] == ["--account-id", account_id]
                          and old_args[3:5] == ["--codex-home", str(config_path.parent)]
                          and old_args[5] == "--db")
        managed_path = (isinstance(old_args, list) and old_args
                        and Path(old_args[0]).name == "workspace_mcp.py"
                        and Path(old_args[-1]).name == DATABASE.name)
        if existing.get("command") != sys.executable or not managed_path or not (old_layout or current_layout):
            raise ValueError(f"Configuration codex_workspace existante à examiner : {config_path}")
        old_block = "\n[mcp_servers.codex_workspace]\n" + \
            f"command = {json.dumps(existing['command'])}\n" + \
            f"args = {json.dumps(old_args, ensure_ascii=False)}\n"
        if original.count(old_block) != 1:
            raise ValueError(f"Bloc codex_workspace à examiner : {config_path}")
        updated = original.replace(old_block, block)
    else:
        updated = original.rstrip() + "\n" + block
    tomllib.loads(updated)
    backup = config_path.with_name(config_path.name + ".bak-workspace-mcp-" +
                                   datetime.now().strftime("%Y%m%d-%H%M%S%f"))
    shutil.copy2(config_path, backup)
    temporary = config_path.with_name(config_path.name + ".workspace-mcp.tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.replace(temporary, config_path)
    return f"{'mis à jour' if existing else 'installé'} (sauvegarde : {backup.name})"


def main():
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "VSCode-Codex"
    for number in (1, 2):
        profile = base / f"Compte-{number}" / "codex" / "config.toml"
        print(f"Compte {number} : {install(profile, f'compte-{number}')}")
    print("Redémarrez les sessions Codex déjà ouvertes pour voir les nouveaux outils MCP.")


if __name__ == "__main__":
    main()
