"""Wire prep helper for Pedro.

Looks up recipient banking details from wire_recipients.json, fetches the
live CAD/USD rate, and returns a formatted Telegram message Greg can use to
enter the wire in Agility Forex in one shot.

This module NEVER moves money. It only prepares information.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path

log = logging.getLogger("nightshift.wire")

RECIPIENTS_FILE = Path(__file__).parent / "wire_recipients.json"
RATE_API = "https://open.er-api.com/v6/latest/USD"


def _load_recipients() -> dict:
    if RECIPIENTS_FILE.exists():
        return json.loads(RECIPIENTS_FILE.read_text())
    return {}


def find_recipient(query: str) -> dict | None:
    """Find a recipient by name or alias (case-insensitive)."""
    q = query.lower().strip()
    for key, rec in _load_recipients().items():
        if q == key or q in [a.lower() for a in rec.get("alias", [])]:
            return rec
        if q in rec.get("name", "").lower():
            return rec
    return None


def list_recipients() -> list[str]:
    return [f"{k} ({v['name']})" for k, v in _load_recipients().items()]


def get_cad_usd_rate() -> float | None:
    """Fetch live USD→CAD rate from open.er-api.com (free, no key needed)."""
    try:
        with urllib.request.urlopen(RATE_API, timeout=8) as r:
            data = json.loads(r.read())
        return float(data["rates"]["CAD"])
    except Exception as e:
        log.warning("Rate fetch failed: %s", e)
        return None


def prep_wire(recipient_query: str, amount_usd: float) -> dict:
    """Prepare a wire summary.

    Returns a dict with:
      - found: bool
      - recipient: dict or None
      - amount_usd: float
      - rate: float or None (CAD per 1 USD)
      - amount_cad: float or None
      - summary: human-readable Telegram message
    """
    rec = find_recipient(recipient_query)
    rate = get_cad_usd_rate()
    amount_cad = round(amount_usd * rate, 2) if rate else None

    if not rec:
        known = ", ".join(list_recipients())
        return {
            "found": False,
            "summary": (
                f"Recipient '{recipient_query}' not found.\n"
                f"Known recipients: {known}\n"
                f"Add new ones via /wire_add or edit wire_recipients.json on the VPS."
            ),
        }

    lines = [
        "💸 *Wire Prep — Ready to Enter in Agility Forex*",
        "",
        f"*Recipient:* {rec['name']}",
        f"*Amount:* ${amount_usd:,.2f} USD",
    ]
    if rate and amount_cad:
        lines.append(f"*Rate:* {rate:.5f} CAD/USD  →  ~${amount_cad:,.2f} CAD from your TD account")
    else:
        lines.append("*Rate:* unavailable — check Agility for live rate")

    lines += [
        "",
        "*Banking Details:*",
        f"  Bank: {rec.get('bank_name', '—')}",
        f"  Bank Address: {rec.get('bank_address', '—')}",
        f"  Account Name: {rec.get('account_name', '—')}",
        f"  Account #: {rec.get('account_number', '—')}",
    ]
    if rec.get("routing"):
        lines.append(f"  Routing #: {rec['routing']}")
    if rec.get("swift"):
        lines.append(f"  SWIFT: {rec['swift']}")
    if rec.get("notes"):
        lines.append(f"\n⚠️ Note: {rec['notes']}")

    lines += [
        "",
        "Log into agilityforex.com, book a USD buy, enter the above details.",
        "Reply 'done' or tell me the deal ID when it's sent.",
    ]

    return {
        "found": True,
        "recipient": rec,
        "amount_usd": amount_usd,
        "rate": rate,
        "amount_cad": amount_cad,
        "summary": "\n".join(lines),
    }
