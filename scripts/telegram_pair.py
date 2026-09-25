"""Pair one private Telegram chat using a short-lived, local challenge."""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request


class PairingError(Exception):
    pass


def bot_api(token: str, method: str, payload: dict, timeout: int = 15):
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except (OSError, ValueError) as exc:
        raise PairingError(f"Telegram ne répond pas à {method}.") from exc
    if not result.get("ok"):
        raise PairingError(f"Telegram a refusé {method}.")
    return result.get("result")


def pairing_chat(update: dict, challenge: str) -> str | None:
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id")
    if (chat.get("type") != "private" or not isinstance(chat_id, int) or chat_id <= 0
            or sender.get("id") != chat_id or sender.get("is_bot")
            or message.get("text") != f"/pair {challenge}"):
        return None
    return str(chat_id)


def wait_for_pairing(token: str, challenge: str, baseline: list[dict], timeout_seconds: int = 180) -> str:
    offset = max((item["update_id"] for item in baseline if isinstance(item.get("update_id"), int)),
                 default=None)
    offset = offset + 1 if offset is not None else None
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        payload = {"limit": 100, "timeout": min(10, max(0, int(deadline - time.monotonic()))),
                   "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        updates = bot_api(token, "getUpdates", payload, timeout=15) or []
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                offset = max(offset or 0, update_id + 1)
            chat_id = pairing_chat(update, challenge)
            if chat_id:
                return chat_id
    raise PairingError("Délai d'appairage dépassé. Relancez le script pour obtenir un nouveau code.")


def save_pairing(token: str, chat_id: str):
    if os.name != "nt":
        raise PairingError("L'enregistrement des variables utilisateur nécessite Windows.")
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "TELEGRAM_API_KEY", 0, winreg.REG_SZ, token)
        winreg.SetValueEx(key, "TELEGRAM_CHAT_ID", 0, winreg.REG_SZ, chat_id)
        # Written last: the notifier only accepts a chat that completed the challenge.
        winreg.SetValueEx(key, "TELEGRAM_PAIRED_CHAT_ID", 0, winreg.REG_SZ, chat_id)


def main() -> int:
    token = (os.environ.get("TELEGRAM_API_KEY") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise PairingError("TELEGRAM_API_KEY est absent.")
    bot = bot_api(token, "getMe", {})
    webhook = bot_api(token, "getWebhookInfo", {})
    if webhook.get("url"):
        raise PairingError("Un webhook Telegram est actif : l'appairage par getUpdates est indisponible.")
    # A negative offset discards older pending messages; only messages sent after the
    # challenge is shown can complete this pairing.
    baseline = bot_api(token, "getUpdates", {"offset": -1, "limit": 1, "timeout": 0,
                                              "allowed_updates": ["message"]}) or []
    challenge = secrets.token_urlsafe(18)
    print(f"Envoyez dans le chat privé de @{bot['username']} : /pair {challenge}", flush=True)
    print("Ce code expire dans 3 minutes. /start seul ne suffit pas.", flush=True)
    chat_id = wait_for_pairing(token, challenge, baseline)
    bot_api(token, "sendMessage", {"chat_id": chat_id,
                                   "text": "Codex Manager : appairage confirmé pour ce chat privé."})
    save_pairing(token, chat_id)
    print("Chat privé appairé et enregistré. Identifiant masqué.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PairingError as error:
        print(f"Appairage impossible : {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Appairage annulé.", file=sys.stderr)
        sys.exit(1)
