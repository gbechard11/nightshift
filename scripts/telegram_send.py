#!/usr/bin/env python3
"""Send a Telegram message via the NS bot. Usage: telegram_send.py --to <name|chat_id> --msg <text>"""
import argparse, os, sys, requests
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/nightshift/.env"))

# (chat_id, use_employee_bot)
CONTACTS = {
    "greg":   (6575459992, False),
    "seba":   (8722742818, True),
    "andrew": (8621126122, True),
}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", required=True, help="Name (greg/seba/andrew) or numeric chat ID")
    parser.add_argument("--msg", required=True, help="Message text")
    args = parser.parse_args()

    main_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    employee_token = os.environ.get("EMPLOYEE_BOT_TOKEN", main_token)
    if not main_token:
        sys.exit("TELEGRAM_BOT_TOKEN not set")

    use_employee = False
    try:
        chat_id = int(args.to)
    except ValueError:
        key = args.to.lower()
        if key not in CONTACTS:
            sys.exit(f"Unknown contact '{args.to}'. Known: {', '.join(CONTACTS)}")
        chat_id, use_employee = CONTACTS[key]

    token = employee_token if use_employee else main_token

    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": args.msg},
        timeout=10,
    )
    data = r.json()
    if not data.get("ok"):
        sys.exit(f"Telegram error: {data}")
    print(f"Sent to chat_id {chat_id}")

if __name__ == "__main__":
    main()
