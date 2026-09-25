"""Install the monitoring hooks into each Codex home, preserving existing hooks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from server import default_accounts


EVENTS = ("SessionStart", "UserPromptSubmit", "PermissionRequest", "PostToolUse", "Stop", "Interrupt", "SubagentStart", "SubagentStop")
HOOK_PATH = str(Path(__file__).resolve().parent / "hook_sender.py").replace("\\", "/").lower()
LEGACY_MARKER = "codex-supervision/hook_sender.py"


def own_hook(command: str) -> bool:
    normalized = str(command).replace("\\", "/").lower()
    return HOOK_PATH in normalized or LEGACY_MARKER in normalized


def install(home: Path, account_id: str, endpoint: str):
    file = home / "hooks.json"
    if not home.is_dir():
        raise FileNotFoundError(home)
    original = json.loads(file.read_text(encoding="utf-8-sig")) if file.exists() else {"hooks": {}}
    if not isinstance(original.get("hooks"), dict):
        raise ValueError(f"Format de hooks.json non reconnu : {file}")

    command_parts = [sys.executable, str(Path(__file__).resolve().parent / "hook_sender.py"),
                     "--account", account_id, "--url", endpoint]
    command = subprocess.list2cmdline(command_parts) if os.name == "nt" else __import__("shlex").join(command_parts)
    for event in EVENTS:
        groups = original["hooks"].setdefault(event, [])
        groups[:] = [group for group in groups if not any(
            own_hook(handler.get("command", "")) or own_hook(handler.get("commandWindows", ""))
            for handler in group.get("hooks", []))]
        handler = {"type": "command", "command": command, "timeout": 2}
        if os.name == "nt":
            handler["commandWindows"] = command
        group = {"hooks": [handler]}
        if event == "PostToolUse":
            group["matcher"] = "Bash|apply_patch"
        groups.append(group)
    if file.exists():
        backup = file.with_suffix(f".json.bak.{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(file, backup)
    temp = file.with_suffix(".json.tmp")
    temp.write_text(json.dumps(original, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(file)
    return file


def main():
    parser = argparse.ArgumentParser(description="Installe les hooks Codex Manager")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()
    config = Path(args.config)
    accounts = json.loads(config.read_text(encoding="utf-8"))["accounts"] if config.exists() else default_accounts()
    for account in accounts:
        file = install(Path(account["codex_home"]), account["id"], f"http://127.0.0.1:{args.port}/api/events")
        print(f"{account['name']} : {file}")
    print("Ouvrez /hooks dans Codex pour examiner et approuver les nouveaux hooks de chaque compte.")


if __name__ == "__main__":
    main()
