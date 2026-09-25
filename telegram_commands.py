"""Private Telegram monitoring menu and read-only answer viewer."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from codex_conversation import indexed_chat, read_latest_answer


COMMANDS = [
    ("menu", "Menu et résumé"),
    ("status", "Activité en direct"),
    ("stats", "Quotas et tokens"),
    ("projets", "Tokens par projet"),
    ("fenetres", "Détail des fenêtres"),
    ("goals", "État des goals"),
    ("alertes", "Seuils et notifications"),
    ("historique", "Changements récents"),
]
ALIASES = {"comptes": "stats", "conso": "stats", "aide": "menu"}
BUTTONS = [
    [("🟢 En direct", "status"), ("📊 Stats", "stats")],
    [("📁 Projets", "projets"), ("🪟 Fenêtres", "fenetres")],
    [("🎯 Goals", "goals"), ("🔔 Alertes", "alertes")],
    [("🕘 Historique", "historique")],
]
GOAL_LABELS = {
    "active": "en cours", "paused": "en pause", "blocked": "bloqué",
    "usageLimited": "limité par le quota", "budgetLimited": "limité par le budget",
    "complete": "terminé",
}
TASK_LABELS = {
    "active": "travaille", "approval": "validation attendue", "waiting": "réponse attendue",
    "stalled": "à vérifier", "unconfirmed": "activité non confirmée",
    "failed": "erreur", "completed": "tâche terminée",
    "interrupted": "tâche arrêtée",
}
TASK_PRIORITY = {"failed": 0, "approval": 1, "waiting": 2, "stalled": 3, "unconfirmed": 3,
                 "active": 4, "interrupted": 6, "completed": 6}
GOAL_PRIORITY = {"blocked": 0, "usageLimited": 1, "budgetLimited": 2,
                 "paused": 3, "active": 4, "complete": 5}
TASK_MARKERS = {"active": "🟢", "approval": "🟠", "waiting": "🟠", "stalled": "🟠", "unconfirmed": "🟠",
                "failed": "🔴", "completed": "⚪", "interrupted": "⚪"}
GOAL_MARKERS = {"active": "🟢", "paused": "🟠", "blocked": "🔴",
                "usageLimited": "🟠", "budgetLimited": "🟠", "complete": "✅"}
PLAN_LABELS = {"pro": "Pro", "prolite": "Pro Lite", "plus": "Plus",
               "free": "Free", "go": "Go"}


def short(value, limit=75):
    return " ".join(str(value or "").split())[:limit]


def project_name(path):
    return short(str(path or "Projet non attribué").replace("\\", "/").rstrip("/").split("/")[-1], 42)


def telegram_chunks(value: str, limit: int = 3500) -> list[str]:
    """Split by Telegram's UTF-16 length limit without cutting a code point."""
    chunks, current, units = [], [], 0
    for character in value:
        size = len(character.encode("utf-16-le")) // 2
        if units + size > limit and current:
            chunks.append("".join(current))
            current, units = [], 0
        current.append(character)
        units += size
    if current:
        chunks.append("".join(current))
    return chunks or [""]


def number(value):
    return f"{value:,}".replace(",", " ") if isinstance(value, (int, float)) else "—"


def counted(value, singular, plural):
    return f"{value} {singular if value == 1 else plural}"


def time_label(value):
    return datetime.fromtimestamp(value).strftime("%d/%m %H:%M") if isinstance(value, (int, float)) else "—"


def quota(metrics):
    rates = ((metrics or {}).get("limits") or {}).get("rateLimits") or {}
    bucket = rates.get("primary") or rates.get("secondary") or {}
    used = bucket.get("usedPercent")
    return (round(max(0, min(100, 100 - used))), bucket.get("resetsAt")) if isinstance(used, (int, float)) else (None, None)


def quota_period(metrics):
    rates = ((metrics or {}).get("limits") or {}).get("rateLimits") or {}
    bucket = rates.get("primary") or rates.get("secondary") or {}
    return period_minutes(bucket.get("windowDurationMins"))


def period_minutes(minutes):
    if not isinstance(minutes, (int, float)):
        return "période inconnue"
    if minutes >= 1440 and minutes % 1440 == 0:
        return f"{int(minutes / 1440)} j"
    if minutes >= 60 and minutes % 60 == 0:
        return f"{int(minutes / 60)} h"
    return f"{int(minutes)} min"


def today_tokens(metrics):
    today = date.today().isoformat()
    for bucket in ((metrics or {}).get("usage") or {}).get("dailyUsageBuckets") or []:
        if bucket.get("startDate") == today:
            return bucket.get("tokens")
    return None


def week_tokens(metrics):
    start = (date.today() - timedelta(days=6)).isoformat()
    buckets = ((metrics or {}).get("usage") or {}).get("dailyUsageBuckets") or []
    values = [item.get("tokens") for item in buckets if isinstance(item.get("startDate"), str)
              and item["startDate"] >= start
              and isinstance(item.get("tokens"), (int, float))]
    return sum(values) if values else None


def account_goals(account):
    metrics = account.get("metrics") or {}
    threads = ((metrics.get("threads") or {}).get("data") or []) + (metrics.get("tracked_goals") or [])
    goals = {}
    for thread in threads:
        goal = thread.get("goal")
        if not thread.get("id") or not isinstance(goal, dict) or not goal.get("status"):
            continue
        if thread["id"] not in goals or (thread.get("updatedAt") or 0) > (goals[thread["id"]].get("updatedAt") or 0):
            goals[thread["id"]] = thread
    return list(goals.values())


def confirmed_active_goals(account):
    window_ids = {window["id"] for window in account.get("windows", [])}
    sessions = {task.get("session_id") for task in account.get("tasks", [])
                if task.get("window_id") in window_ids}
    return sum(thread["id"] in sessions and thread["goal"]["status"] == "active"
               for thread in account_goals(account))


def latest_session_tasks(tasks):
    latest = {}
    for task in tasks:
        key = task.get("session_id") or task.get("id")
        if key not in latest or task.get("updated_at", 0) > latest[key].get("updated_at", 0):
            latest[key] = task
    return list(latest.values())


def confirmed_tasks(account):
    window_ids = {window["id"] for window in account.get("windows", [])}
    return [task for task in latest_session_tasks(account.get("tasks", []))
            if task.get("window_id") in window_ids]


def account_counts(account, stamp):
    tasks = confirmed_tasks(account)
    active = sum(task.get("display_status") == "active" for task in tasks)
    action = sum(task.get("display_status") in {"approval", "waiting", "stalled"} for task in tasks)
    uncertain = sum(task.get("display_status") == "unconfirmed" for task in tasks)
    errors = sum(task.get("display_status") == "failed" and task.get("updated_at", 0) > stamp - 86400
                 for task in tasks)
    open_goals = sum(thread["goal"]["status"] == "active" and
                     any(task.get("session_id") == thread["id"] for task in tasks)
                     for thread in account_goals(account))
    working_goals = sum(thread["goal"]["status"] == "active" and
                        any(task.get("session_id") == thread["id"] and task.get("display_status") == "active"
                            for task in tasks) for thread in account_goals(account))
    uncertain_goals = sum(thread["goal"]["status"] == "active" and
                          any(task.get("session_id") == thread["id"] and task.get("display_status") == "unconfirmed"
                              for task in tasks) for thread in account_goals(account))
    return {"active": active, "action": action, "errors": errors,
            "uncertain": uncertain, "goals_uncertain": uncertain_goals,
            "goals_working": working_goals, "goals_waiting": open_goals - working_goals - uncertain_goals}


def goal_display(goal, task):
    status = goal.get("status") if isinstance(goal, dict) else None
    if status == "active":
        if task and task.get("display_status") == "active":
            return "🟢", "en cours"
        if task and task.get("display_status") == "unconfirmed":
            return "🟠", "activité à confirmer"
        if task and task.get("display_status") == "interrupted":
            return "🟠", "à reprendre"
        return "🟠", "en attente"
    return GOAL_MARKERS.get(status, "⚪"), GOAL_LABELS.get(status, status or "inconnu")


def visible_window_tasks(account, window, now):
    tasks = latest_session_tasks([task for task in account.get("tasks", []) if task.get("window_id") == window["id"]])
    tasks.sort(key=lambda task: (TASK_PRIORITY.get(task.get("display_status"), 9)
                                 if task.get("display_status") != "failed" or task.get("updated_at", 0) > now - 86400 else 6,
                                 -task.get("updated_at", 0)))
    current = [task for task in tasks if task.get("display_status") in
               {"approval", "waiting", "stalled", "unconfirmed", "active"} or
               (task.get("display_status") == "failed" and task.get("updated_at", 0) > now - 86400)]
    return current[:3] if current else tasks[:1]


def window_goal(account, task):
    threads = ((account.get("metrics") or {}).get("threads") or {}).get("data") or []
    thread = next((item for item in threads if item.get("id") == task.get("session_id")
                   and "goal" in item), None)
    return (thread is not None, thread.get("goal") if thread else None)


def fresh_note(snapshot):
    stale = [account["name"] for account in snapshot.get("accounts", [])
             if not (account.get("metrics") or {}).get("last_success_at")
             or snapshot.get("now", time.time()) - account["metrics"]["last_success_at"] > 90]
    return "\n⚠ Relevé ancien ou indisponible : " + ", ".join(stale) if stale else ""


def render(command, snapshot):
    command = ALIASES.get(command, command)
    accounts = snapshot.get("accounts") or []
    stamp = datetime.fromtimestamp(snapshot.get("now", time.time())).strftime("%H:%M")
    moment = snapshot.get("now", time.time())
    counts = {account["id"]: account_counts(account, moment) for account in accounts}
    totals = {key: sum(value[key] for value in counts.values())
              for key in ("active", "action", "errors", "uncertain", "goals_working", "goals_waiting", "goals_uncertain")}
    lines = []
    if command == "menu":
        lines = [f"🧭 CODEX MANAGER · {stamp}", "",
                 f"🟢 {counted(totals['active'], 'chat', 'chats')} au travail"]
        if totals["action"] or totals["errors"]:
            lines.append(f"🟠 {counted(totals['action'], 'action', 'actions')} · "
                         f"🔴 {counted(totals['errors'], 'erreur', 'erreurs')}")
        else:
            lines.append("✅ Rien à traiter")
        if totals["uncertain"]:
            lines.append(f"🟠 {counted(totals['uncertain'], 'activité non confirmée', 'activités non confirmées')}")
        goals_line = (f"🎯 {counted(totals['goals_working'], 'goal', 'goals')} en cours · "
                      f"{counted(totals['goals_waiting'], 'goal', 'goals')} en attente")
        if totals["goals_uncertain"]:
            goals_line += f" · {counted(totals['goals_uncertain'], 'goal', 'goals')} à confirmer"
        lines += [goals_line, ""]
        for account in accounts:
            remaining, _ = quota(account.get("metrics"))
            lines.append(f"{short(account['name'], 24)} · quota {remaining if remaining is not None else '—'} %"
                         f" · {counted(counts[account['id']]['active'], 'chat actif', 'chats actifs')}")
        lines.extend(["", "Choisissez une vue avec les boutons ci-dessous."])
    elif command == "status":
        lines = [f"🟢 EN DIRECT · {stamp}",
                 f"{counted(totals['active'], 'chat', 'chats')} au travail · "
                 f"{counted(totals['action'], 'action', 'actions')} · "
                 f"{counted(totals['errors'], 'erreur', 'erreurs')}",
                 f"🎯 {counted(totals['goals_working'], 'goal', 'goals')} en cours · "
                 f"{counted(totals['goals_waiting'], 'goal', 'goals')} en attente"]
        if totals["goals_uncertain"]:
            lines[-1] += f" · {counted(totals['goals_uncertain'], 'goal', 'goals')} à confirmer"
        if totals["uncertain"]:
            lines.append(f"🟠 {counted(totals['uncertain'], 'activité non confirmée', 'activités non confirmées')}")
        for account in accounts:
            remaining, _ = quota(account.get("metrics"))
            lines.extend(["", f"{short(account['name'], 28).upper()} · quota {remaining if remaining is not None else '—'} %"])
            windows = account.get("windows") or []
            if not windows:
                lines.append("⚪ Aucune fenêtre confirmée")
                continue
            ordered = sorted(windows, key=lambda window: TASK_PRIORITY.get(
                (visible_window_tasks(account, window, moment) or [{}])[0].get("display_status"), 9))
            for window in ordered:
                tasks = visible_window_tasks(account, window, moment)
                if not tasks:
                    lines.append(f"⚪ {short(window.get('label'), 40)} · aucun chat attribué")
                    continue
                for task in tasks:
                    status = task.get("display_status")
                    marker = TASK_MARKERS.get(status, "⚪")
                    title = short(task.get("title"), 54)
                    name = short(window.get("label"), 35)
                    detail = f" · {title}" if title and title.casefold() != name.casefold() else ""
                    goal_known, goal = window_goal(account, task)
                    goal_note = ""
                    if isinstance(goal, dict):
                        _, goal_label = goal_display(goal, task)
                        goal_note = f" · goal {goal_label}"
                    elif goal_known:
                        goal_note = " · sans goal"
                    lines.append(f"{marker} {name}{detail} — {TASK_LABELS.get(status, 'état inconnu')}{goal_note}")
            ambiguous = latest_session_tasks([task for task in account.get("tasks", [])
                                              if task.get("window_match") == "ambiguous" and
                                              task.get("display_status") in {"active", "approval", "waiting", "stalled", "unconfirmed"}])
            for task in ambiguous[:3]:
                lines.append(f"🟠 Fenêtre incertaine · {short(task.get('title'), 55)}"
                             f" — {TASK_LABELS.get(task.get('display_status'), 'état inconnu')}")
    elif command == "stats":
        lines = [f"📊 QUOTAS & TOKENS · {stamp}"]
        for account in accounts:
            metrics = account.get("metrics") or {}
            remaining, reset = quota(metrics)
            plan = (((metrics.get("identity") or {}).get("account") or {}).get("planType") or "inconnu")
            credits = (((metrics.get("limits") or {}).get("rateLimits") or {}).get("credits") or {})
            quota_marker = "⚪" if remaining is None else "🔴" if remaining < 20 else "🟠" if remaining < 40 else "🟢"
            lines += ["", f"{short(account['name'], 40).upper()} · {PLAN_LABELS.get(plan, short(plan, 25))}",
                      f"{quota_marker} Quota disponible : {remaining if remaining is not None else '—'} %"
                      f" ({quota_period(metrics)})",
                      f"↻ Réinitialisation : {time_label(reset)}",
                      f"Tokens aujourd'hui : {number(today_tokens(metrics))}",
                      f"Tokens sur 7 jours : {number(week_tokens(metrics))}",
                      f"Fenêtres : {len(account.get('windows', []))} · "
                      f"{counted(counts[account['id']]['active'], 'chat actif', 'chats actifs')}"]
            secondary = (((metrics.get("limits") or {}).get("rateLimits") or {}).get("secondary") or {})
            if isinstance(secondary.get("usedPercent"), (int, float)):
                remaining_secondary = round(max(0, min(100, 100 - secondary["usedPercent"])))
                lines.append(f"Autre quota : {remaining_secondary} % ({period_minutes(secondary.get('windowDurationMins'))})")
            if credits.get("hasCredits") and credits.get("balance") is not None:
                lines.append(f"Crédits additionnels : {credits['balance']}")
            projection = (account.get("quota_projection") or {}).get("primary")
            if projection:
                lines.append(f"⚠ Au rythme observé : quota atteint vers {time_label(projection)}")
        days = [today_tokens(account.get("metrics")) for account in accounts]
        weeks = [week_tokens(account.get("metrics")) for account in accounts]
        if accounts and all(value is not None for value in days + weeks):
            lines += ["", f"TOTAL · aujourd'hui {number(sum(days))} · 7 jours {number(sum(weeks))} tokens"]
    elif command == "projets":
        lines = [f"📁 TOKENS PAR PROJET · {stamp}", "Nouveaux tokens observés depuis l'activation du suivi."]
        for account in accounts:
            usage = (snapshot.get("project_usage") or {}).get(account["id"]) or {}
            lines += ["", short(account["name"], 40).upper()]
            if not usage.get("started_at"):
                lines.append("Premier relevé en attente")
                continue
            lines += [f"Aujourd'hui {number(usage.get('today'))} · 7 jours {number(usage.get('week'))}",
                      f"Suivi depuis le {time_label(usage['started_at'])}"]
            projects = [item for item in usage.get("projects") or [] if item.get("week", 0) > 0]
            if not projects:
                lines.append("Aucune nouvelle consommation attribuée")
            for item in projects[:5]:
                lines.append(f"• {project_name(item.get('workspace'))} : "
                             f"{number(item.get('today'))} aujourd'hui · {number(item.get('week'))} sur 7 j")
            if usage.get("last_observed_at") and moment - usage["last_observed_at"] > 120:
                lines.append("⚠ Relevé projet ancien")
    elif command == "fenetres":
        lines = [f"🪟 Fenêtres confirmées · {stamp}"]
        for account in accounts:
            lines += ["", short(account["name"], 40)]
            windows = account.get("windows") or []
            if not windows:
                lines.append("Aucune fenêtre détectée")
            for window in windows[:10]:
                tasks = visible_window_tasks(account, window, snapshot.get("now", time.time()))
                marker = TASK_MARKERS.get(tasks[0].get("display_status"), "⚪") if tasks else "⚪"
                lines.append(f"{marker} {short(window.get('label') or 'Fenêtre VS Code', 65)}")
                if not tasks:
                    lines.append("  Aucun chat attribué avec certitude")
                    continue
                for task in tasks:
                    status = TASK_LABELS.get(task.get("display_status"), "état inconnu")
                    lines.append(f"  {status} · {short(task.get('title'), 75)}")
                    goal_known, goal = window_goal(account, task)
                    if isinstance(goal, dict):
                        _, goal_label = goal_display(goal, task)
                        lines.append("  🎯 Goal " + goal_label)
                    elif goal_known:
                        lines.append("  ◦ Chat simple · sans goal")
    elif command == "goals":
        lines = [f"🎯 Goals · {stamp}"]
        found = False
        for account in accounts:
            sessions = {task.get("session_id") for task in confirmed_tasks(account)}
            goals = [thread for thread in account_goals(account) if thread["id"] in sessions]
            goals.sort(key=lambda thread: (GOAL_PRIORITY.get(thread["goal"]["status"], 9),
                                           -(thread.get("updatedAt") or 0)))
            if not goals:
                continue
            lines += ["", short(account["name"], 40)]
            for thread in goals[:12]:
                goal = thread["goal"]
                label = short(thread.get("name") or thread.get("preview") or goal.get("objective") or "Goal Codex", 85)
                task = next((task for task in latest_session_tasks(account.get("tasks", []))
                             if task.get("session_id") == thread["id"]), None)
                marker, state = goal_display(goal, task)
                lines.append(f"{marker} {state} · {label}")
                found = True
        if not found:
            lines.append("Aucun goal détecté dans les conversations suivies.")
    elif command == "alertes":
        lines = [f"🔔 ALERTES · {stamp}"]
        preferences = snapshot.get("preferences") or {}
        settings = preferences.get("accounts") or {}
        for account in accounts:
            config = settings.get(account["id"]) or {}
            if not config:
                continue
            enabled = [label for key, label in (("notify_answers", "réponses sans goal"), ("notify_actions", "actions"),
                                                ("notify_goals", "goals"),
                                                ("notify_quota", "quotas"), ("notify_tokens", "tokens"))
                       if config.get(key)]
            lines += ["", short(account["name"], 40).upper(),
                      f"Quota : ≤ {config['quota_remaining_percent']} % restants",
                      f"Tokens : ≥ {number(config['tokens_daily'])}/jour · ≥ {number(config['tokens_weekly'])}/7j",
                      "Telegram : " + (", ".join(enabled) if enabled else "désactivé")]
        quiet = preferences.get("quiet_hours") or {}
        if quiet.get("enabled"):
            lines += ["", f"🌙 Silence : {quiet.get('start')}–{quiet.get('end')}"]
        lines += ["", "Modifiez les seuils dans Alertes et seuils sur le dashboard local."]
    elif command == "historique":
        lines = [f"🕘 ACTIVITÉ RÉCENTE · {stamp}"]
        names = {account["id"]: account["name"] for account in accounts}
        labels = {"UserPromptSubmit": "Chat lancé", "PermissionRequest": "Validation demandée",
                  "Stop": "Réponse terminée", "completed": "Réponse terminée",
                  "failed": "Erreur", "Interrupt": "Tour interrompu", "interrupted": "Tour interrompu",
                  "goal_change": "Goal modifié", "metric_alert": "Seuil atteint"}
        seen = set()
        for event in snapshot.get("events") or []:
            if event.get("at", 0) < moment - 7 * 86400:
                continue
            kind = labels.get(event.get("type"))
            if not kind:
                continue
            key = (event.get("account_id"), event.get("task_id"), kind, int(event.get("at", 0) / 10))
            if key in seen:
                continue
            seen.add(key)
            try:
                detail = json.loads(event.get("details") or "{}").get("label", "")
            except (ValueError, TypeError):
                detail = ""
            lines.append(f"{time_label(event.get('at'))} · {names.get(event.get('account_id'), '?')} · {kind}"
                         + (f" · {short(detail, 50)}" if detail else ""))
            if len(seen) >= 12:
                break
        if not seen:
            lines.append("Aucun changement récent.")
    else:
        return render("menu", snapshot)
    note = fresh_note(snapshot)
    return "\n".join(lines)[:4096 - len(note)] + note


class TelegramCommandBot:
    def __init__(self, token: str, chat_id: str, snapshot, accounts=()):
        self.token = token
        self.chat_id = chat_id
        self.snapshot = snapshot
        self.accounts = {account["id"]: account for account in accounts}

    def api(self, method: str, payload: dict, timeout=15):
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.load(response)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Telegram : {method} indisponible") from exc
        if not data.get("ok"):
            raise RuntimeError(f"Telegram : {method} refusé")
        return data.get("result")

    def keyboard(self, current="menu", snapshot=None):
        rows = BUTTONS if current == "menu" else [
            [("🔄 Actualiser", current), ("☰ Menu", "menu")],
            [("🟢 En direct", "status"), ("📊 Stats", "stats")],
            [("📁 Projets", "projets"), ("🪟 Fenêtres", "fenetres")],
            [("🎯 Goals", "goals"), ("🔔 Alertes", "alertes")],
            [("🕘 Historique", "historique")],
        ]
        return {"inline_keyboard": [[{"text": label, "callback_data": command} for label, command in row]
                                    for row in rows]}

    def _target(self, data: str):
        parts = data.split(":", 2)
        if len(parts) != 3 or parts[0] != "chat" or len(data.encode("utf-8")) > 64:
            raise ValueError("Sélection de chat invalide")
        account = self.accounts.get(parts[1])
        if not account:
            raise ValueError("Compte inconnu")
        return account, indexed_chat(account["codex_home"], parts[2])

    def _send_answer(self, account: dict, chat: dict, answer: str):
        header = f"💬 {account['name']} · {short(chat['name'], 110)}\n\n"
        if chat.get("latest_turn_status") in {"inProgress", "in_progress", "active"}:
            header += "⏳ Un tour est en cours. Dernière réponse terminée :\n\n"
        content = answer or "Aucune réponse finale disponible dans ce chat pour le moment."
        chunks = telegram_chunks(header + content)
        for chunk in chunks:
            self.api("sendMessage", {"chat_id": self.chat_id, "text": chunk})

    def _show_answer(self, data: str):
        account, chat = self._target(data)
        answer, details = read_latest_answer(account["codex_home"], chat["id"])
        chat["latest_turn_status"] = details.get("latest_turn_status")
        self._send_answer(account, chat, answer)

    def install_menu(self):
        scope = {"type": "chat", "chat_id": int(self.chat_id)}
        self.api("setMyCommands", {"scope": scope,
                                   "commands": [{"command": command, "description": description}
                                                for command, description in COMMANDS]})
        self.api("setChatMenuButton", {"chat_id": int(self.chat_id),
                                       "menu_button": {"type": "commands"}})

    def authorized(self, message):
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        return (chat.get("type") == "private" and str(chat.get("id")) == self.chat_id
                and str(sender.get("id")) == self.chat_id and not sender.get("is_bot"))

    def handle_update(self, update):
        message = update.get("message")
        callback = update.get("callback_query")
        if message:
            if not self.authorized(message):
                return False
            parts = str(message.get("text") or "").strip().split(maxsplit=1)
            if not parts:
                return False
            first = parts[0].split("@", 1)[0].lower()
            command = first.lstrip("/") if first.startswith("/") else ""
            if command == "start":
                command = "menu"
            command = ALIASES.get(command, command)
            if command not in {name for name, _ in COMMANDS}:
                return False
            snapshot = self.snapshot()
            self.api("sendMessage", {"chat_id": self.chat_id, "text": render(command, snapshot),
                                     "reply_markup": self.keyboard(command, snapshot)})
            return True
        if callback:
            original = callback.get("message") or {}
            requester = callback.get("from") or {}
            command = callback.get("data") or ""
            buttons = (original.get("reply_markup") or {}).get("inline_keyboard") or []
            answer_link = (isinstance(command, str) and command.startswith("chat:")
                           and str(original.get("text") or "").startswith("✅ Réponse prête ·")
                           and any(isinstance(button, dict) and button.get("text") == "Afficher la réponse"
                                   and button.get("callback_data") == command
                                   for row in buttons if isinstance(row, list) for button in row))
            normal_command = isinstance(command, str) and command in {name for name, _ in COMMANDS}
            if (not self.authorized({**original, "from": requester})
                    or not isinstance(original.get("message_id"), int)
                    or not callback.get("id")
                    or not (normal_command or answer_link)):
                return False
            self.api("answerCallbackQuery", {"callback_query_id": callback["id"]})
            if answer_link:
                try:
                    self._show_answer(command)
                except (RuntimeError, ValueError, TimeoutError, OSError) as exc:
                    self.api("sendMessage", {"chat_id": self.chat_id, "text": f"⚠ {short(exc, 220)}"})
                return True
            snapshot = self.snapshot()
            payload = {"chat_id": self.chat_id, "message_id": original["message_id"],
                       "text": render(command, snapshot), "reply_markup": self.keyboard(command, snapshot)}
            if buttons and buttons[0] and buttons[0][0].get("text") == "Voir dans le bot":
                self.api("sendMessage", {"chat_id": self.chat_id, "text": payload["text"],
                                         "reply_markup": payload["reply_markup"]})
                return True
            if original.get("text") == payload["text"] and original.get("reply_markup") == payload["reply_markup"]:
                return True
            try:
                self.api("editMessageText", payload)
            except RuntimeError:
                self.api("sendMessage", {"chat_id": self.chat_id, "text": payload["text"],
                                         "reply_markup": payload["reply_markup"]})
            return True
        return False

    def run(self):
        next_registration = 0
        offset = None
        while True:
            try:
                if time.monotonic() >= next_registration:
                    next_registration = time.monotonic() + 3600
                    try:
                        self.install_menu()
                    except RuntimeError as exc:
                        print(str(exc), flush=True)
                if offset is None:
                    # Pairing and commands from a previous run are not replayed.
                    baseline = self.api("getUpdates", {"offset": -1, "limit": 1, "timeout": 0}, timeout=10) or []
                    offset = max((item["update_id"] for item in baseline if "update_id" in item), default=0) + 1
                updates = self.api("getUpdates", {"offset": offset, "limit": 50, "timeout": 15,
                                                   "allowed_updates": ["message", "callback_query"]}, timeout=20) or []
                for update in updates:
                    self.handle_update(update)
                    offset = max(offset, update.get("update_id", 0) + 1)
            except RuntimeError as exc:
                print(str(exc), flush=True)
                time.sleep(5)
