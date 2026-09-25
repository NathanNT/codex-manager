# Codex Manager Presence

This local VS Code extension reports a temporary window ID, account ID, and workspace to `127.0.0.1:8765` every 15 seconds. It does not transmit source code, prompts, tokens, or secrets.

The extension infers `compte-1` or `compte-2` from `CODEX_HOME`. If that variable is unavailable in VS Code, set `codexSupervision.accountId` in the corresponding VS Code profile.

Install it in both profiles with `scripts/install_extension.ps1`, then reload any VS Code windows that were already open. See the [setup guide](https://github.com/NathanNT/codex-manager/blob/main/docs/setup.md) in the project repository.
