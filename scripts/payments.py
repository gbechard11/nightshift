#!/usr/bin/env python3
"""Payment ledger for Greg — tracks money OWED (outgoing) and money EXPECTED
(incoming). It records and reminds. It NEVER moves money.

Ledger: /data/greg/payments.json

Usage:
  payments.py add --direction outgoing --who "DJ Mina" --amount 500 \
      --reason "June 12 Pawn Shop Live show" --due 2026-06-12
  payments.py add --direction incoming --who "Showpass" --amount 3200 \
      --reason "June 12 ticket settlement" --due 2026-06-20
  payments.py list [--status pending|paid] [--direction outgoing|incoming]
  payments.py pay <id>            # mark an entry paid/received
  payments.py rm <id>             # delete an entry
  payments.py remind [--window 7] [--notify]   # digest of due/overdue/owed
                                               # --notify sends to Telegram
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date

LEDGER = os.environ.get("PEDRO_LEDGER", "/data/greg/payments.json")


def load_env(path: str = "~/nightshift/.env") -> None:
    p = os.path.expanduser(path)
    if not os.path.exists(p):
        return
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            os.environ.setdefault(k.strip(), v)


def load() -> dict:
    if os.path.exists(LEDGER):
        with open(LEDGER) as f:
            return json.load(f)
    return {"next_id": 1, "payments": []}


def save(data: dict) -> None:
    os.makedirs(os.path.dirname(LEDGER) or ".", exist_ok=True)
    tmp = LEDGER + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, LEDGER)


def _money(p: dict) -> str:
    return f"{p['amount']:,.2f} {p.get('currency', 'CAD')}"


def cmd_add(a) -> None:
    data = load()
    pid = data["next_id"]
    data["next_id"] += 1
    entry = {
        "id": pid,
        "direction": a.direction,
        "counterparty": a.who,
        "amount": float(a.amount),
        "currency": a.currency,
        "reason": a.reason,
        "due_date": a.due,
        "status": "pending",
        "created": date.today().isoformat(),
        "paid_date": None,
        "notes": a.notes or "",
    }
    data["payments"].append(entry)
    save(data)
    print(json.dumps({"ok": True, "added": entry}, indent=2))


def cmd_list(a) -> None:
    data = load()
    items = data["payments"]
    if a.status:
        items = [p for p in items if p["status"] == a.status]
    if a.direction:
        items = [p for p in items if p["direction"] == a.direction]
    print(json.dumps(items, indent=2))


def cmd_pay(a) -> None:
    data = load()
    for p in data["payments"]:
        if p["id"] == a.id:
            p["status"] = "paid"
            p["paid_date"] = date.today().isoformat()
            save(data)
            print(json.dumps({"ok": True, "paid": p}, indent=2))
            return
    print(json.dumps({"ok": False, "error": f"no payment with id {a.id}"}))
    sys.exit(1)


def cmd_rm(a) -> None:
    data = load()
    before = len(data["payments"])
    data["payments"] = [p for p in data["payments"] if p["id"] != a.id]
    if len(data["payments"]) == before:
        print(json.dumps({"ok": False, "error": f"no payment with id {a.id}"}))
        sys.exit(1)
    save(data)
    print(json.dumps({"ok": True, "removed_id": a.id}))


def _digest(window: int) -> str:
    data = load()
    today = date.today()
    overdue, due_soon, incoming = [], [], []
    for p in data["payments"]:
        if p["status"] != "pending":
            continue
        due = date.fromisoformat(p["due_date"]) if p.get("due_date") else None
        if p["direction"] == "outgoing":
            if due and due < today:
                overdue.append((p, due))
            elif due and (due - today).days <= window:
                due_soon.append((p, due))
            elif not due:
                due_soon.append((p, None))
        else:
            incoming.append((p, due))

    if not (overdue or due_soon or incoming):
        return "Payments: nothing due, overdue, or outstanding. You're square."

    lines = ["*Payment status*"]
    if overdue:
        lines.append("\nOVERDUE (you owe):")
        for p, due in overdue:
            days = (today - due).days
            lines.append(f"  #{p['id']} {p['counterparty']} — {_money(p)} — {p['reason']} (due {p['due_date']}, {days}d late)")
    if due_soon:
        lines.append(f"\nDUE SOON (you owe, next {window}d):")
        for p, due in due_soon:
            d = p["due_date"] or "no date"
            lines.append(f"  #{p['id']} {p['counterparty']} — {_money(p)} — {p['reason']} (due {d})")
    if incoming:
        lines.append("\nOWED TO YOU (expected):")
        for p, due in incoming:
            d = p["due_date"] or "no date"
            lines.append(f"  #{p['id']} {p['counterparty']} — {_money(p)} — {p['reason']} (expected {d})")
    lines.append("\nReply e.g. \"mark payment 3 paid\" to update. (Pedro never moves money — you send/receive yourself.)")
    return "\n".join(lines)


def _send_telegram(text: str) -> None:
    load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = (os.environ.get("ALLOWED_USERS", "").split(",") or [""])[0].strip()
    if not token or not chat:
        print("cannot notify: TELEGRAM_BOT_TOKEN/ALLOWED_USERS missing", file=sys.stderr)
        return
    payload = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "Markdown"}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with urllib.request.urlopen(urllib.request.Request(url, data=payload), timeout=30) as r:
        r.read()


def cmd_remind(a) -> None:
    text = _digest(a.window)
    if a.notify:
        _send_telegram(text)
    else:
        print(text)


def main() -> None:
    p = argparse.ArgumentParser(description="Greg's payment ledger (track only, never moves money)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("add")
    pa.add_argument("--direction", required=True, choices=["outgoing", "incoming"])
    pa.add_argument("--who", required=True)
    pa.add_argument("--amount", required=True)
    pa.add_argument("--reason", required=True)
    pa.add_argument("--due", default=None, help="YYYY-MM-DD")
    pa.add_argument("--currency", default="CAD")
    pa.add_argument("--notes", default="")
    pa.set_defaults(func=cmd_add)

    pl = sub.add_parser("list")
    pl.add_argument("--status", choices=["pending", "paid"])
    pl.add_argument("--direction", choices=["outgoing", "incoming"])
    pl.set_defaults(func=cmd_list)

    pp = sub.add_parser("pay")
    pp.add_argument("id", type=int)
    pp.set_defaults(func=cmd_pay)

    pr = sub.add_parser("rm")
    pr.add_argument("id", type=int)
    pr.set_defaults(func=cmd_rm)

    prem = sub.add_parser("remind")
    prem.add_argument("--window", type=int, default=7, help="days ahead to flag as due soon")
    prem.add_argument("--notify", action="store_true", help="send digest to Telegram")
    prem.set_defaults(func=cmd_remind)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
