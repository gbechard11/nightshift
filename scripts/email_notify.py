#!/usr/bin/env python3
"""Check for new work emails to greg@nightshiftent.ca and push Telegram summaries.
Runs every 5 minutes via cron. Tracks notified IDs in /data/greg/email_notified.json.
"""
from __future__ import annotations

import email as email_lib
import email.policy
import imaplib
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.parse
from pathlib import Path


SEEN_FILE = Path("/data/greg/email_notified.json")
WORK_ADDRESS = "greg@nightshiftent.ca"

NOISE_SENDERS = [
    "uber", "hyatt", "hotels.com", "godaddy", "printful", "constantcontact",
    "renegade", "hollywood improv", "aspen", "liberty entertainment",
    "american express", "ted fass", "challenge family", "movati",
    "all access", "world of hyatt", "coinbase", "openai", "betopper",
    "pollstar", "teamsnap", "amazon", "looker", "bootleg blondie",
    "aliexpress", "linkedin", "noreply@email", "newsletter", "unsubscribe",
    "no-reply@", "donotreply", "notification", "louisvuitton", "freshbooks",
    "docusign", "squareup", "7shifts", "patronscan", "twilio",
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
    ids = list(seen)[-500:]
    SEEN_FILE.write_text(json.dumps(ids))


def is_noise(sender: str, subject: str) -> bool:
    s = sender.lower()
    subj = subject.lower()
    if any(n in s for n in NOISE_SENDERS):
        return True
    if any(n in subj for n in NOISE_SUBJECTS):
        return True
    if re.search(r"automatic.?reply|out of office|auto.?reply", subj):
        return True
    return False


def is_work_email(parsed) -> bool:
    headers = " ".join([
        parsed.get("To", ""),
        parsed.get("Cc", ""),
        parsed.get("Delivered-To", ""),
        parsed.get("X-Original-To", ""),
    ]).lower()
    return WORK_ADDRESS in headers


def decode_header_str(s: str) -> str:
    if not s:
        return ""
    try:
        from email.header import decode_header, make_header
        return str(make_header(decode_header(s)))
    except Exception:
        return s


def extract_body(parsed) -> str:
    body = ""
    if parsed.is_multipart():
        for part in parsed.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                try:
                    body = part.get_content()
                    break
                except Exception:
                    pass
    else:
        try:
            body = parsed.get_content()
        except Exception:
            pass
    body = re.sub(r"\r?\n{3,}", "\n\n", body.strip())
    return body[:800]


def summarize_email(sender: str, subject: str, body: str) -> str:
    prompt = (
        "Summarize this email in 1-2 sentences. Be direct and factual. "
        "Note any action required.\n\n"
        f"From: {sender}\nSubject: {subject}\n\nBody:\n{body[:600]}"
    )
    try:
        result = subprocess.run(
            ["/usr/bin/claude", "--permission-mode", "bypassPermissions", "-p", prompt],
            capture_output=True, text=True, timeout=60, cwd="/data/greg"
        )
        return result.stdout.strip() or "(no summary)"
    except Exception as e:
        return f"(summary unavailable: {e})"


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
                sender = decode_header_str(parsed.get("From", ""))
                subject = decode_header_str(parsed.get("Subject", ""))

                if not is_work_email(parsed):
                    seen.add(mid)
                    continue
                if is_noise(sender, subject):
                    seen.add(mid)
                    continue

                body = extract_body(parsed)
                new_msgs.append((mid, sender, subject, body))
                seen.add(mid)
    except Exception as e:
        print(f"IMAP error: {e}", file=sys.stderr)
        sys.exit(1)

    save_seen(seen)

    for _, sender, subject, body in new_msgs:
        name = re.sub(r"\s*<.*?>", "", sender).strip() or sender
        summary = summarize_email(sender, subject, body)
        text = (
            f"📬 <b>New work email</b>\n"
            f"<b>From:</b> {name}\n"
            f"<b>Subject:</b> {subject}\n\n"
            f"{summary}"
        )
        try:
            send_telegram(token, chat_id, text)
        except Exception as e:
            print(f"Telegram error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
