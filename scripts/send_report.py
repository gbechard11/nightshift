#!/usr/bin/env python3
"""Scheduled Meta Ads digest -> the owner on Telegram.

Run by cron every few days. It loads .env itself (cron has no systemd
EnvironmentFile), pulls last-7-days account totals from Meta (read-only), and
sends one Telegram message to the owner, who can then forward/direct it to Seba.

    python scripts/send_report.py

Config (from .env):
  TELEGRAM_BOT_TOKEN   the bot token (reused from the main bot)
  REPORT_TG_CHAT_ID    who to send to; defaults to ALLOWED_USERS
  META_ACCESS_TOKEN / META_AD_ACCOUNT_ID   the Meta CAD account
"""
import asyncio
import os
import sys

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
import meta_ads  # noqa: E402

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_IDS = [
    x.strip()
    for x in os.environ.get("REPORT_TG_CHAT_ID", os.environ.get("ALLOWED_USERS", "")).split(",")
    if x.strip()
]


def _format(rows: list[dict], obj: str) -> str:
    if not rows:
        return (
            "📊 Meta Ads digest — last 7 days\n"
            f"Account: {obj}\n"
            "No delivery or data in this window."
        )
    r = rows[0]
    return (
        "📊 Meta Ads digest — last 7 days\n"
        f"Account: {obj}\n\n"
        f"Spend: ${r.get('spend', '?')}\n"
        f"Reach: {r.get('reach', '?')}\n"
        f"Impressions: {r.get('impressions', '?')}\n"
        f"Clicks: {r.get('clicks', '?')}\n"
        f"CTR: {r.get('ctr', '?')}%\n\n"
        "Want this sent to Seba? Just reply and tell me."
    )


async def main() -> int:
    if not (meta_ads.configured() and TG_TOKEN and CHAT_IDS):
        print("send_report: missing config (META token / TELEGRAM_BOT_TOKEN / chat id).")
        return 1
    obj = meta_ads.AD_ACCOUNT_ID
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            rows = await meta_ads.get_insights(
                client, obj, fields="spend,reach,impressions,clicks,ctr"
            )
            text = _format(rows, obj)
        except meta_ads.MetaError as e:
            text = f"⚠️ Meta Ads digest failed: {e}"
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
