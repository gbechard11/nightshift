#!/usr/bin/env python3
"""Read recent messages from Gmail via IMAP.

Usage:
    email_read.py                                   # last 10 unread in INBOX
    email_read.py --count 25 --all                  # last 25 messages (read + unread)
    email_read.py --search 'FROM "venue"'           # raw IMAP search
    email_read.py --mailbox "[Gmail]/All Mail" --count 5
    email_read.py --save-attachments /tmp/att       # save attachments to dir, report paths in JSON

Prints one JSON object per message to stdout, one per line — easy for Claude
to parse without overflowing context (we cap the body preview).
"""
from __future__ import annotations

import argparse
import email
import email.policy
import imaplib
import json
import os
import sys
from pathlib import Path
from email.header import decode_header, make_header


def _load_env(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _decode(s):
    if not s:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return s


def _attachments(msg: email.message.EmailMessage, save_dir: str | None = None) -> list[dict]:
    results = []
    for part in msg.walk():
        disp = part.get("Content-Disposition", "") or ""
        filename = part.get_filename()
        if not filename and not disp.startswith("attachment"):
            continue
        if not filename:
            filename = f"attachment.{part.get_content_subtype() or 'bin'}"
        filename = str(make_header(decode_header(filename)))
        entry: dict = {"filename": filename, "content_type": part.get_content_type()}
        if save_dir:
            save_path = Path(save_dir) / filename
            save_path.parent.mkdir(parents=True, exist_ok=True)
            payload = part.get_payload(decode=True)
            if payload:
                save_path.write_bytes(payload)
                entry["saved_path"] = str(save_path)
        results.append(entry)
    return results


def _body_preview(msg: email.message.EmailMessage, limit: int = 1500) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get("Content-Disposition", "").startswith("attachment"):
                try:
                    body = part.get_content()
                except Exception:
                    body = part.get_payload(decode=True).decode(errors="replace")
                break
    else:
        try:
            body = msg.get_content()
        except Exception:
            payload = msg.get_payload(decode=True)
            body = payload.decode(errors="replace") if payload else ""
    body = (body or "").strip()
    if len(body) > limit:
        body = body[:limit] + f"\n…[truncated, full length {len(body)} chars]"
    return body


def main() -> None:
    p = argparse.ArgumentParser(description="Read Gmail via IMAP")
    p.add_argument("--count", type=int, default=10, help="how many messages to fetch (default 10)")
    p.add_argument("--all", action="store_true", help="include read messages (default: unread only)")
    p.add_argument("--search", default=None, help="raw IMAP search criteria (overrides --all)")
    p.add_argument("--mailbox", default="INBOX")
    p.add_argument("--body-limit", type=int, default=1500, help="max chars of body preview")
    p.add_argument("--save-attachments", metavar="DIR", default=None, help="save attachments to this directory")
    args = p.parse_args()

    _load_env(os.path.expanduser("~/nightshift/.env"))
    host = os.environ.get("IMAP_HOST", "imap.gmail.com")
    port = int(os.environ.get("IMAP_PORT", "993"))
    user = os.environ.get("IMAP_USER") or os.environ.get("EMAIL_USER")
    pwd = os.environ.get("IMAP_PASS") or os.environ.get("EMAIL_PASS")
    if not user or not pwd:
        print(json.dumps({"ok": False, "error": "IMAP_USER/IMAP_PASS not set"}), file=sys.stderr)
        sys.exit(1)

    try:
        m = imaplib.IMAP4_SSL(host, port)
        m.login(user, pwd)
        m.select(args.mailbox, readonly=True)
        criteria = args.search if args.search else ("ALL" if args.all else "UNSEEN")
        typ, data = m.search(None, criteria)
        if typ != "OK":
            raise RuntimeError(f"search failed: {typ}")
        ids = data[0].split()[-args.count:]
        for msg_id in reversed(ids):
            typ, msg_data = m.fetch(msg_id, "(RFC822)")
            if typ != "OK":
                continue
            raw = msg_data[0][1]
            parsed = email.message_from_bytes(raw, policy=email.policy.default)
            atts = _attachments(parsed, args.save_attachments)
            entry = {
                "id": msg_id.decode(),
                "message_id": parsed.get("Message-ID", ""),
                "from": _decode(parsed.get("From")),
                "to": _decode(parsed.get("To")),
                "cc": _decode(parsed.get("Cc")) if parsed.get("Cc") else "",
                "reply_to": _decode(parsed.get("Reply-To")) if parsed.get("Reply-To") else "",
                "subject": _decode(parsed.get("Subject")),
                "date": parsed.get("Date"),
                "body": _body_preview(parsed, args.body_limit),
                "attachments": atts,
            }
            print(json.dumps(entry, ensure_ascii=False))
        m.close()
        m.logout()
    except imaplib.IMAP4.error as e:
        print(json.dumps({"ok": False, "error": f"IMAP error: {e}"}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
