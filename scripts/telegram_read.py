#!/usr/bin/env python3
"""Read recent messages received by the NS bots.

Usage: telegram_read.py [--bot main|employee] [--count N] [--incoming-only]

The employee bot is a long-running long-poll process, so calling getUpdates
from here would steal/clash with its own polling. Instead, employee_bot.py
appends every inbound/outbound message to a JSONL log; for --bot employee we
read that log. The main owner bot has no such log, so --bot main still uses
getUpdates (only reliable while the owner bot is stopped).
"""
import argparse, json, os, sys, requests
from dotenv import load_dotenv
from datetime import datetime

load_dotenv(os.path.expanduser("~/nightshift/.env"))

EMPLOYEE_CHAT_LOG = "/data/employees/employee_chat.jsonl"

CONTACTS_BY_ID = {
    6575459992: "greg",
    8722742818: "seba",
    8621126122: "andrew",
}


def read_employee_log(count, incoming_only):
    if not os.path.exists(EMPLOYEE_CHAT_LOG):
        print(f"No chat log yet at {EMPLOYEE_CHAT_LOG}")
        return
    rows = []
    with open(EMPLOYEE_CHAT_LOG, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if incoming_only:
        rows = [r for r in rows if r.get("direction") == "in"]
    if not rows:
        print("No messages found.")
        return
    for r in rows[-count:]:
        ts = datetime.fromtimestamp(r.get("ts", 0)).strftime("%Y-%m-%d %H:%M:%S")
        uid = r.get("uid")
        name = CONTACTS_BY_ID.get(uid) or str(uid)
        arrow = "<-" if r.get("direction") == "in" else "->"
        text = r.get("text", "")
        print(f"[{ts}] {arrow} {name}: {text}")


def fetch_updates(token, count):
    r = requests.get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        params={"limit": count, "allowed_updates": ["message"]},
        timeout=10,
    )
    data = r.json()
    if not data.get("ok"):
        sys.exit(f"Telegram error: {data}")
    return data["result"]


def format_updates(updates):
    if not updates:
        print("No messages found.")
        return
    for u in updates:
        msg = u.get("message", {})
        if not msg:
            continue
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        name = CONTACTS_BY_ID.get(chat_id)
        sender = name or chat.get("username") or chat.get("first_name") or str(chat_id)
        ts = datetime.fromtimestamp(msg.get("date", 0)).strftime("%Y-%m-%d %H:%M:%S")
        text = msg.get("text", "[non-text message]")
        print(f"[{ts}] {sender}: {text}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", choices=["main", "employee"], default="employee",
                        help="Which bot to read from (default: employee)")
    parser.add_argument("--count", type=int, default=20, help="Number of recent messages to show")
    parser.add_argument("--incoming-only", action="store_true",
                        help="Employee log only: show inbound (employee->bot) messages")
    args = parser.parse_args()

    if args.bot == "employee":
        read_employee_log(args.count, args.incoming_only)
        return

    main_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not main_token:
        sys.exit("TELEGRAM_BOT_TOKEN not set")
    updates = fetch_updates(main_token, args.count)
    format_updates(updates)


if __name__ == "__main__":
    main()
