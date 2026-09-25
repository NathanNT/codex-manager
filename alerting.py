"""Optional outbound alerts. Telegram credentials are read from environment only."""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path


class AlertDispatcher:
    def __init__(self, config_path: Path):
        self.telegram_token = (os.environ.get("TELEGRAM_API_KEY") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        self.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        paired_chat_id = os.environ.get("TELEGRAM_PAIRED_CHAT_ID", "").strip()
        self.telegram_enabled = bool(self.telegram_token and re.fullmatch(r"[1-9][0-9]*", self.telegram_chat_id)
                                     and paired_chat_id == self.telegram_chat_id)
        self.discord_enabled = False

    def emit(self, account: str, message: str):
        """Legacy hook retained for callers; external alerts use paired Telegram only."""
        return None

    def goal_changed(self, account: str, label: str, kind: str, previous: str, status: str,
                     detail: str = "") -> bool:
        """Return True only after Telegram confirms delivery."""
        if not self.telegram_enabled:
            return False
        names = {
            "paused": "🟠 Goal en pause", "blocked": "🔴 Goal bloqué",
            "usageLimited": "🟠 Goal limité par le quota", "budgetLimited": "🟠 Goal limité par le budget",
            "complete": "✅ Goal terminé", "none": "⚪ Goal terminé ou supprimé",
        }
        if kind == "objective":
            heading = "🎯 Objectif du goal modifié"
        elif kind == "budget":
            heading = "📊 Budget du goal modifié"
        elif status == "active":
            heading = "🟢 Goal démarré" if previous == "none" else "🟢 Goal repris"
        else:
            heading = names.get(status, f"Goal : {status[:40]}")
        message = f"{heading} · {account}\n{label.strip()[:180] or 'Objectif Codex'}"
        if detail:
            message += f"\n{detail[:100]}"
        return self._telegram_message(message, "goals")

    def answer_ready(self, account: str, label: str, window: str = "",
                     account_id: str = "", thread_id: str = "") -> bool:
        """Notify once a top-level Codex reply is complete, with no reply content."""
        message = f"✅ Réponse prête · {account}\n{label.strip()[:180] or 'Conversation Codex'}"
        if window:
            message += f"\n🪟 {window.strip()[:100]}"
        target = f"chat:{account_id}:{thread_id}" if account_id and thread_id else "status"
        return self._telegram_message(message, target,
                                      "Afficher la réponse" if target != "status" else "Voir dans le bot")

    def metric_warning(self, account: str, metric: str, label: str, threshold: float) -> bool:
        """Send an actionable threshold alert to the paired private chat only."""
        if metric.startswith("quota"):
            message = f"🔴 Alerte quota · {account}\n{label}\nSeuil : {threshold:g} % restants"
        else:
            period = "aujourd'hui" if metric == "tokens_daily" else "sur 7 jours"
            message = f"🟠 Alerte tokens · {account}\n{label}\nSeuil {period} : {threshold:,.0f} tokens".replace(",", " ")
        return self._telegram_message(message, "stats")

    def action_needed(self, account: str, kind: str, label: str, window: str = "") -> bool:
        heading = "🟠 Validation requise" if kind == "approval" else "🔴 Erreur d'agent"
        message = f"{heading} · {account}\n{label.strip()[:180] or 'Conversation Codex'}"
        if window:
            message += f"\n🪟 {window.strip()[:100]}"
        return self._telegram_message(message, "status")

    def _telegram_message(self, message: str, view: str = "status", button: str = "Voir dans le bot") -> bool:
        if not self.telegram_enabled:
            return False
        payload = {"chat_id": self.telegram_chat_id, "text": message,
                   "reply_markup": {"inline_keyboard": [[{"text": button, "callback_data": view}]]}}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
            data=data, headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                return json.load(response).get("ok") is True
        except (OSError, ValueError):
            return False
