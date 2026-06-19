#!/usr/bin/env python3
"""Scheduled Prism watcher -> the owner on Telegram.

Run by cron. Polls upcoming Prism shows (read-only), diffs them against the last
run's saved state, and pings the owner when a show is ADDED or its STATUS changes
(e.g. Hold -> Confirmed, Confirmed -> Settled). First run just seeds state quietly.

    python scripts/prism_watch.py

Config (from .env):
  TELEGRAM_BOT_TOKEN    bot token (reused from the main bot)
  REPORT_TG_CHAT_ID     who to notify; defaults to ALLOWED_USERS
  PRISM_REFRESH_TOKEN   the Prism Cognito refresh token (see prism.py)
  PRISM_WATCH_DAYS      lookahead window in days (default 180)
  PRISM_WATCH_STATE     state file (default /data/greg/.prism_watch_state.json)
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Load .env into the environment (cron runs without the bot's systemd env).
try:
    with open(os.path.join(HERE, ".env")) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
except FileNotFoundError:
    pass

sys.path.insert(0, HERE)

import httpx  # noqa: E402
import prism  # noqa: E402

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_IDS = [
    x.strip()
    for x in os.environ.get("REPORT_TG_CHAT_ID", os.environ.get("ALLOWED_USERS", "")).split(",")
    if x.strip()
]
WATCH_DAYS = int(os.environ.get("PRISM_WATCH_DAYS", "180"))
STATE_FILE = os.environ.get("PRISM_WATCH_STATE", "/data/greg/.prism_watch_state.json")
# Sentinel so we ping the owner only ONCE when the Prism login expires, not every
# 4h run. Cleared automatically on the next successful read (i.e. after re-auth).
REAUTH_SENTINEL = os.environ.get("PRISM_REAUTH_SENTINEL", "/data/greg/.prism_reauth_pinged")

REAUTH_MSG = (
    "🔑 Prism token expired — Pedro can't read the calendar right now.\n\n"
    "Quick fix (~10 sec): on a logged-in app.prism.fm tab, click your Prism-Token "
    "bookmark, then paste it to me here. Good for another ~24h.\n"
    "(If Prism shows you logged out, log in first — that part's only ~monthly.)"
)


async def _notify(client, text: str) -> None:
    for cid in CHAT_IDS:
        try:
            await client.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                data={"chat_id": cid, "text": text[:4000]},
            )
        except Exception as e:  # noqa: BLE001
            print(f"notify {cid} failed: {e}")


def _looks_like_expired_auth(err: str) -> bool:
    e = err.lower()
    return any(s in e for s in (
        "refresh token", "expired", "revoked", "notauthorized", "not authorized",
        "no prism_refresh_token", "secret", "401", "unauthorized",
    ))


def _should_reping() -> bool:
    """Ping on the first expiry, then re-ping at most once per ~20h while the
    token is STILL expired (a daily nudge), so a lapsed token can't sit dark
    unnoticed for days. The sentinel stores the last-ping time; cleared on the
    next healthy read."""
    try:
        with open(REAUTH_SENTINEL) as f:
            last = datetime.fromisoformat(f.read().strip())
        return (datetime.now() - last) >= timedelta(hours=20)
    except (FileNotFoundError, ValueError):
        return True


def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except OSError as e:
        print(f"prism_watch: could not save state {STATE_FILE}: {e}")


def _diff(prev: dict, shows: list[dict]) -> tuple[list[dict], dict]:
    """Return (changes, new_state). Each change is {kind, show, old_label}."""
    changes = []
    new_state = {}
    for s in shows:
        eid = str(s.get("event_id"))
        if not eid or eid == "None":
            continue
        new_state[eid] = {"status": s.get("status"), "title": s.get("title")}
        old = prev.get(eid)
        if old is None:
            # Only announce as "new" if we have prior state at all (first run seeds).
            if prev:
                changes.append({"kind": "new", "show": s, "old_label": None})
        elif old.get("status") != s.get("status"):
            changes.append(
                {"kind": "status", "show": s, "old_label": prism.status_label(old.get("status"))}
            )
    return changes, new_state


def _format(changes: list[dict]) -> str:
    lines = ["🎫 Prism update:\n"]
    for c in changes:
        s = c["show"]
        when = s.get("start") or "?"
        venue = f" @ {s['venue']}" if s.get("venue") else ""
        if c["kind"] == "new":
            lines.append(f"🆕 NEW: {s['title']} — {when}{venue} [{s['status_label']}] #{s['event_id']}")
        else:
            lines.append(
                f"🔄 {s['title']} — {when}{venue}: {c['old_label']} → {s['status_label']} #{s['event_id']}"
            )
    return "\n".join(lines)


async def main() -> int:
    if not (prism.configured() and TG_TOKEN and CHAT_IDS):
        print("prism_watch: missing config (PRISM_REFRESH_TOKEN / TELEGRAM_BOT_TOKEN / chat id).")
        return 1
    today = datetime.now().date()
    end = today + timedelta(days=WATCH_DAYS)
    async with httpx.AsyncClient(timeout=45.0) as client:
        try:
            shows = await prism.list_shows(client, today.isoformat(), end.isoformat())
        except prism.PrismError as e:
            print(f"prism_watch: lookup failed: {e}")
            # If the failure is an expired/rejected token, ping the owner with
            # re-auth steps: once on first expiry, then a daily nudge (~20h) while
            # still expired so a lapsed access token can't sit dark for days.
            if _looks_like_expired_auth(str(e)) and _should_reping():
                await _notify(client, REAUTH_MSG)
                try:
                    open(REAUTH_SENTINEL, "w").write(datetime.now().isoformat())
                except OSError:
                    pass
                print("prism_watch: pinged owner to re-auth.")
            return 1

        # Healthy read — clear the re-auth sentinel so a future expiry pings again.
        try:
            os.path.exists(REAUTH_SENTINEL) and os.remove(REAUTH_SENTINEL)
        except OSError:
            pass

        prev = _load_state()
        changes, new_state = _diff(prev, shows)
        _save_state(new_state)

        if not prev:
            print(f"prism_watch: seeded state with {len(new_state)} shows (no alerts on first run).")
            return 0
        if not changes:
            print("prism_watch: no changes.")
            return 0

        text = _format(changes)
        for cid in CHAT_IDS:
            try:
                resp = await client.post(
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                    data={"chat_id": cid, "text": text[:4000]},
                )
                print(f"sent to {cid}: HTTP {resp.status_code}")
            except Exception as e:  # noqa: BLE001
                print(f"send to {cid} failed: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
