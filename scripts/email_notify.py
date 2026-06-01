#!/usr/bin/env python3
"""Check for new business emails and push Telegram notifications.
Runs every 5 minutes via cron. Tracks notified IDs in /data/greg/email_notified.json.
"""
from __future__ import annotations

import email as email_lib
import email.policy
import imaplib
import json
import os
import re
import sys
import urllib.request
import urllib.parse
from pathlib import Path


SEEN_FILE = Path("/data/greg/email_notified.json")

NOISE_SENDERS = [
    "uber", "hyatt", "hotels.com", "godaddy", "printful", "constantcontact",
    "renegade", "hollywood improv", "aspen", "liberty entertainment",
    "american express", "ted fass", "challenge family", "movati",
    "all access", "world of hyatt", "coinbase", "openai", "betopper",
    "pollstar", "teamsnap", "amazon", "looker", "bootleg blondie",
    "aliexpress", "linkedin", "noreply@email", "newsletter", "unsubscribe",
    "no-reply@", "donotreply", "notification", "louisvuitton", "freshbooks",
    "docusign", "squareup", "7shifts", "patronscan", "twilio", "showpass",
    "vapi.ai", "trykeep.com", "keep team", "down by the river",
]

NOISE_SUBJECTS = [
    "daily report", "ticket counts report", "daily sales", "auto notify",
    "verification code", "account has been funded", "shipped",
    "your order", "invoice waiting", "open shifts",
]


def load_env(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set) -> None:
    # Keep last 500 IDs to avoid unbounded growth
    ids = list(seen)[-500:]
    SEEN_FILE.write_text(json.dumps(ids))


def is_noise(sender: str, subject: str) -> bool:
    s = sender.lower()
    subj = subject.lower()
    if any(n in s for n in NOISE_SENDERS):
        return True
    if any(n in subj for n in NOISE_SUBJECTS):
        return True
    # Auto-replies
    if re.search(r"automatic.?reply|out of office|auto.?reply", subj):
        return True
    return False


def decode_header(s: str) -> str:
    if not s:
        return ""
    try:
        from email.header import decode_header, make_header
        return str(make_header(decode_header(s)))
    except Exception:
        return s


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(url, data=data)
    urllib.request.urlopen(req, timeout=10)


def main() -> None:
    load_env(os.path.expanduser("~/nightshift/.env"))

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_ids = [x.strip() for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip()]
    if not token or not chat_ids:
        sys.exit(0)
    chat_id = chat_ids[0]

    imap_host = os.environ.get("IMAP_HOST", "imap.gmail.com")
    imap_port = int(os.environ.get("IMAP_PORT", "993"))
    imap_user = os.environ.get("IMAP_USER")
    imap_pass = os.environ.get("IMAP_PASS")
    if not imap_user or not imap_pass:
        sys.exit(0)

    seen = load_seen()
    new_msgs = []

    try:
        with imaplib.IMAP4_SSL(imap_host, imap_port) as m:
            m.login(imap_user, imap_pass)
            m.select("INBOX", readonly=True)
            _, data = m.search(None, "UNSEEN")
            ids = data[0].split()
            for msg_id in ids:
                mid = msg_id.decode()
                if mid in seen:
                    continue
                _, msg_data = m.fetch(msg_id, "(RFC822)")
                raw = msg_data[0][1]
                parsed = email_lib.message_from_bytes(raw, policy=email_lib.policy.default)
                sender = decode_header(parsed.get("From", ""))
                subject = decode_header(parsed.get("Subject", ""))
                if is_noise(sender, subject):
                    seen.add(mid)
                    continue
                new_msgs.append((mid, sender, subject))
                seen.add(mid)
    except Exception as e:
        print(f"IMAP error: {e}", file=sys.stderr)
        sys.exit(1)

    save_seen(seen)

    if not new_msgs:
        sys.exit(0)

    lines = [f"📬 <b>{len(new_msgs)} new email{'s' if len(new_msgs) > 1 else ''}</b>"]
    for _, sender, subject in new_msgs[:5]:
        # Trim sender to display name or short address
        name = re.sub(r"\s*<.*?>", "", sender).strip() or sender
        lines.append(f"• <b>{name}</b>: {subject}")
    if len(new_msgs) > 5:
        lines.append(f"…and {len(new_msgs) - 5} more")

    send_telegram(token, chat_id, "\n".join(lines))


if __name__ == "__main__":
    main()
