#!/usr/bin/env python3
"""Send an email via Gmail SMTP using credentials from ~/nightshift/.env.

Usage:
    email_send.py --to alice@example.com --subject "Hello" --body "Body text"
    email_send.py --to "a@x.com,b@y.com" --cc "c@z.com" --subject S --body-file /tmp/msg.txt
    email_send.py --to alice@example.com --subject "Hi" --body-stdin   # read body from stdin

Designed to be called by Claude on the VPS (or from cron) — quiet on success,
prints a JSON error blob to stderr on failure.
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path


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


def _die(msg: str, **extra) -> None:
    print(json.dumps({"ok": False, "error": msg, **extra}), file=sys.stderr)
    sys.exit(1)


def main() -> None:
    p = argparse.ArgumentParser(description="Send email via Gmail SMTP")
    p.add_argument("--to", required=True, help="comma-separated recipient(s)")
    p.add_argument("--cc", default="", help="comma-separated cc(s)")
    p.add_argument("--bcc", default="", help="comma-separated bcc(s)")
    p.add_argument("--subject", required=True)
    body_group = p.add_mutually_exclusive_group(required=True)
    body_group.add_argument("--body", help="message body")
    body_group.add_argument("--body-file", help="path to file containing body")
    body_group.add_argument("--body-stdin", action="store_true", help="read body from stdin")
    p.add_argument(
        "--from-name",
        default=os.environ.get("EMAIL_FROM_NAME", ""),
        help="display name for the From header (defaults to $EMAIL_FROM_NAME)",
    )
    args = p.parse_args()

    _load_env(os.path.expanduser("~/nightshift/.env"))

    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER") or os.environ.get("EMAIL_USER")
    smtp_pass = os.environ.get("SMTP_PASS") or os.environ.get("EMAIL_PASS")
    from_addr = os.environ.get("EMAIL_FROM", smtp_user)
    if not smtp_user or not smtp_pass:
        _die("SMTP_USER and SMTP_PASS must be set in env")

    if args.body_stdin:
        body = sys.stdin.read()
    elif args.body_file:
        body = Path(args.body_file).read_text()
    else:
        body = args.body

    msg = EmailMessage()
    msg["From"] = f"{args.from_name} <{from_addr}>" if args.from_name else from_addr
    msg["To"] = args.to
    if args.cc:
        msg["Cc"] = args.cc
    msg["Subject"] = args.subject
    msg.set_content(body)

    rcpts = [a.strip() for a in (args.to + "," + args.cc + "," + args.bcc).split(",") if a.strip()]

    try:
        ctx = ssl.create_default_context()
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=30) as s:
                s.login(smtp_user, smtp_pass)
                s.send_message(msg, from_addr=from_addr, to_addrs=rcpts)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as s:
                s.starttls(context=ctx)
                s.login(smtp_user, smtp_pass)
                s.send_message(msg, from_addr=from_addr, to_addrs=rcpts)
    except smtplib.SMTPAuthenticationError as e:
        _die("SMTP auth failed — App Password rejected", code=e.smtp_code, response=str(e.smtp_error))
    except Exception as e:
        _die(f"SMTP send failed: {type(e).__name__}: {e}")

    print(json.dumps({"ok": True, "to": args.to, "subject": args.subject}))


if __name__ == "__main__":
    main()
