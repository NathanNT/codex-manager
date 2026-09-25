# Setup and local operation

## Prerequisites

Codex Manager currently targets Windows, Python 3.11+, Codex CLI, and VS Code profiles containing the Codex extension. Node.js and npm are required to package the presence extension. The Python backend itself has no third-party runtime dependencies.

Run `python server.py` from the repository root and open `http://127.0.0.1:8765/`. `scripts/start.ps1` starts the live and demo servers in the background; `scripts/stop.ps1` stops them. The demo can also be started directly with `python server.py --demo --port 8768`; its data is synthetic and held in memory.

## Profiles and configuration

The default Codex homes are `%LOCALAPPDATA%\VSCode-Codex\Compte-1\codex` and `Compte-2\codex`. If yours differ, create an ignored `config.json` in the repository root:

```json
{
  "accounts": [
    {"id": "compte-1", "name": "Account 1", "codex_home": "C:/path/to/account-1/codex"},
    {"id": "compte-2", "name": "Account 2", "codex_home": "C:/path/to/account-2/codex"}
  ]
}
```

Do not commit this file. The extension normally derives the account ID from `CODEX_HOME`. If that variable is not available in the VS Code process, set `codexSupervision.accountId` to `compte-1` or `compte-2` in that profile's settings.

## Hooks, presence, and MCP

```powershell
python install_hooks.py
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_extension.ps1
python scripts/install_workspace_mcp.py
```

The hook installer backs up an existing `hooks.json` before changing it. In the Codex CLI for **each account**, open `/hooks`, review, and approve the eight personal monitoring hooks. Codex will not run them before that review. If `/hooks` is unavailable in the VS Code chat, start the CLI for each profile in separate terminals, for example:

```powershell
$env:CODEX_HOME = "$env:LOCALAPPDATA\VSCode-Codex\Compte-1\codex"
codex
```

Repeat with `Compte-2`. The presence extension sends a temporary window ID, account ID, and workspace every 15 seconds. Reload existing VS Code windows after installation. The MCP installer backs up each `config.toml`; restart existing Codex sessions to load the tools. Add or merge the repository [agent instructions](../AGENTS.md) in projects whose agents should collaborate.

## Windows startup

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_autostart.ps1
```

The per-user startup shortcut starts the live server and tray icon at Windows sign-in. A green dot means the server responds; orange means starting; red means unavailable. Left-click opens the dashboard. The right-click menu can restart or stop the server. The icon checks health every ten seconds and retries after failure. No administrator rights are needed.

To disable future automatic starts, run `scripts/uninstall_autostart.ps1`. Quit the existing tray icon separately. Reinstall the shortcut if you move the project directory.

## Verification and screenshots

```powershell
python -m unittest discover -s tests -v
python -m py_compile server.py workspace_mailbox.py workspace_mcp.py environment_catalog.py project_usage.py codex_conversation.py telegram_commands.py alerting.py monitoring_policy.py codex_rpc.py hook_sender.py install_hooks.py
node --check extension/extension.js
node --check static/app.js
node --check static/catalog.js
```

With the demo server running, `node scripts/capture_demo.mjs` captures desktop and mobile views and checks 390 px and 320 px layouts. Browser tooling is optional for normal operation.
