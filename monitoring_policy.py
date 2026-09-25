"""Validated local alert preferences and calculations for Codex account metrics."""

from __future__ import annotations

from datetime import date, datetime, timedelta


DEFAULT_ACCOUNT = {
    "quota_remaining_percent": 20,
    "tokens_daily": 100000,
    "tokens_weekly": 500000,
    "notify_answers": True,
    "notify_actions": True,
    "notify_goals": True,
    "notify_quota": True,
    "notify_tokens": True,
}


def default_preferences(accounts):
    return {"accounts": {account["id"]: DEFAULT_ACCOUNT.copy() for account in accounts},
            "quiet_hours": {"enabled": False, "start": "22:00", "end": "08:00"}}


def validate_preferences(value, accounts):
    """Reject partial/unknown settings so clients cannot accidentally disable alerts."""
    expected = default_preferences(accounts)
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError("Paramètres d'alerte invalides")
    if not isinstance(value["accounts"], dict) or set(value["accounts"]) != set(expected["accounts"]):
        raise ValueError("Comptes d'alerte invalides")
    for account_id, config in value["accounts"].items():
        if not isinstance(config, dict) or set(config) != set(DEFAULT_ACCOUNT):
            raise ValueError(f"Seuils invalides pour {account_id}")
        for key, default in DEFAULT_ACCOUNT.items():
            field = config[key]
            if isinstance(default, bool):
                if type(field) is not bool:
                    raise ValueError(f"Option invalide : {key}")
            elif type(field) is not int or field < 1 or field > (100 if key == "quota_remaining_percent" else 1000000000):
                raise ValueError(f"Seuil invalide : {key}")
    quiet = value["quiet_hours"]
    if not isinstance(quiet, dict) or set(quiet) != {"enabled", "start", "end"} or type(quiet["enabled"]) is not bool:
        raise ValueError("Plage silencieuse invalide")
    for key in ("start", "end"):
        try:
            datetime.strptime(quiet[key], "%H:%M")
        except (TypeError, ValueError) as exc:
            raise ValueError("Heure invalide ; format HH:MM requis") from exc
    return value


def quiet_now(quiet, moment=None):
    if not quiet["enabled"]:
        return False
    current = (moment or datetime.now()).strftime("%H:%M")
    start, end = quiet["start"], quiet["end"]
    if start < end:
        return start <= current < end
    if start > end:
        return current >= start or current < end
    return False


def quota_buckets(metrics):
    limits = ((metrics.get("limits") or {}).get("rateLimits") or {})
    for name in ("primary", "secondary"):
        bucket = limits.get(name) or {}
        used = bucket.get("usedPercent")
        if isinstance(used, (int, float)) and not isinstance(used, bool):
            yield name, bucket


def token_totals(metrics, today=None):
    """Calendar day and rolling seven days, using Codex's published daily buckets."""
    today = today or date.today()
    buckets = (metrics.get("usage") or {}).get("dailyUsageBuckets") or []
    values = {item.get("startDate"): item.get("tokens") for item in buckets
              if isinstance(item, dict) and isinstance(item.get("tokens"), (int, float))}
    daily = values.get(today.isoformat())
    dates = [(today - timedelta(days=offset)).isoformat() for offset in range(7)]
    weekly = sum(values.get(day, 0) for day in dates) if any(day in values for day in dates) else None
    return daily, weekly


def quota_projection(samples, reset_at, current_time):
    """Return predicted exhaustion only with two spaced observations in one reset window."""
    outlook = quota_outlook(samples, reset_at, current_time)
    return outlook.get("at") if outlook["status"] == "risk" else None


def quota_outlook(samples, reset_at, current_time):
    """Classify the observed pace without treating missing samples as a safe quota."""
    if not isinstance(reset_at, (int, float)) or reset_at <= current_time:
        return {"status": "unavailable"}
    if samples and samples[-1][1] >= 100:
        return {"status": "exhausted"}
    if len(samples) < 2:
        return {"status": "insufficient"}
    first, last = samples[0], samples[-1]
    span = last[0] - first[0]
    consumed = last[1] - first[1]
    if span < 600 or consumed < 0 or 0 < consumed < 1:
        return {"status": "insufficient"}
    if consumed == 0:
        return {"status": "safe", "reset_at": reset_at}
    rate = consumed / span
    estimated_at = last[0] + max(0, 100 - last[1]) / rate
    if estimated_at <= current_time:
        return {"status": "insufficient"}
    if estimated_at < reset_at:
        return {"status": "risk", "at": round(estimated_at), "reset_at": reset_at}
    return {"status": "safe", "reset_at": reset_at}
