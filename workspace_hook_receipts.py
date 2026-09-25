"""Confirm hook delivery only when Codex records the context in its rollout.

The hook's stdout is merely a proposal. A Codex developer-role rollout entry
containing the one-time receipt proves that the runtime accepted that context.
It does not prove the model understood or acted on the message.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from workspace_federation import PeerClient, load_config as load_federation_config
from workspace_mailbox import Mailbox


def _rollout_paths(home: Path, session_id: str):
    for root in (home / "sessions", home / "archived_sessions"):
        if root.is_dir():
            for path in root.rglob(f"*{session_id}*.jsonl"):
                try:
                    with path.open("rb") as stream:
                        header = json.loads(stream.readline())
                    if (header.get("payload") or {}).get("id") == session_id:
                        yield path
                except (OSError, ValueError, json.JSONDecodeError):
                    continue


def _visible_receipts(path: Path, nonces: set[str], offset: int = 0) -> tuple[dict[str, str], int]:
    found = {}
    scanned_to = offset
    try:
        with path.open("rb") as rollout:
            rollout.seek(offset)
            while True:
                raw = rollout.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    break
                scanned_to = rollout.tell()
                if b"WMCP-HOOK-" not in raw:
                    continue
                line = raw.decode("utf-8", errors="replace")
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = record.get("payload") or {}
                if (record.get("type") != "response_item" or payload.get("type") != "message"
                        or payload.get("role") != "developer"):
                    continue
                content = payload.get("content") or []
                text = "\n".join(item.get("text", "") for item in content if isinstance(item, dict))
                for nonce in nonces - found.keys():
                    if f"WMCP-HOOK-{nonce}" in text:
                        found[nonce] = f"Codex rollout : {path.name}, octet {scanned_to}"
                if len(found) == len(nonces):
                    break
    except OSError:
        pass
    return found, scanned_to


def reconcile(mailbox: Mailbox, accounts: list[dict]) -> int:
    """Resolve locally observed receipts; retry peer acknowledgements separately."""
    unresolved = mailbox.pending_hook_emissions()
    profiles = {item["id"]: Path(item["codex_home"]) for item in accounts}
    grouped = defaultdict(dict)
    for attempt in unresolved:
        if attempt["verified_at"]:
            continue
        match = next((account for account in profiles
                      if attempt["agent_id"].startswith(account + ":")), None)
        if match:
            grouped[(match, attempt["agent_id"][len(match) + 1:])][attempt["nonce"]] = attempt
    confirmed = 0
    for (account, session_id), attempts in grouped.items():
        remaining = set(attempts)
        prepared_at = min(datetime.fromisoformat(item["prepared_at"]).timestamp()
                          for item in attempts.values())
        paths = sorted(_rollout_paths(profiles[account], session_id),
                       key=lambda path: path.stat().st_mtime, reverse=True)
        for path in paths:
            # An older rollout segment cannot contain a hook emitted later.
            if path.stat().st_mtime + 3 < prepared_at:
                continue
            scan_from = min((attempt["scan_offset"] if attempt["scan_path"] == str(path) else 0)
                            for attempt in attempts.values() if attempt["nonce"] in remaining)
            found, scanned_to = _visible_receipts(path, remaining, scan_from)
            for nonce, proof in found.items():
                mailbox.verify_hook_emission(nonce, proof)
                remaining.discard(nonce)
                confirmed += 1
            for nonce in remaining:
                mailbox.record_hook_scan(nonce, str(path), scanned_to)
            if not remaining:
                break
    config = load_federation_config()
    if config and config["role"] == "client":
        for attempt in mailbox.pending_remote_hook_acks():
            remote_ids = json.loads(attempt["remote_ids_json"])
            if not remote_ids:
                continue
            try:
                PeerClient(config, timeout=0.6).mark_delivered(
                    attempt["agent_id"], remote_ids, "hook")
                mailbox.mark_remote_hook_ack(attempt["nonce"])
            except (OSError, ValueError, KeyError):
                pass
    return confirmed
