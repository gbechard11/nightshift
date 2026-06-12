#!/home/gregnightshift/nightshift/.venv/bin/python
"""
Daily 8am briefing for Seba — delivered via Telegram chat (employee bot).
Cron: 0 14 * * *  (8am MDT = 14:00 UTC)

Parts:
  1. Open items / to-do from brain log
  2. Seba's unread / recent emails (requires SEBA_IMAP_USER + SEBA_IMAP_PASS in .env)
"""
from __future__ import annotations
import imaplib
import email as email_lib
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from pathlib import Path

ENV_FILE = Path('/home/gregnightshift/nightshift/.env')
BRAIN_FILE = Path('/data/greg/brain/BRAIN.md')
TELEGRAM_SEND = Path('/home/gregnightshift/nightshift/scripts/telegram_send.py')


def load_env():
    if not ENV_FILE.exists():
        return
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            os.environ.setdefault(k.strip(), v.strip())


def extract_open_items() -> list[str]:
    """Pull numbered open items from the latest brain log handoff entry."""
    text = BRAIN_FILE.read_text()
    items = []
    collecting = False
    seen_first_entry = False

    for line in text.split('\n'):
        if line.startswith('## '):
            if seen_first_entry:
                break  # stop at second top-level entry
            seen_first_entry = True
            collecting = True
            continue

        if not collecting:
            continue

        stripped = line.strip()
        if not stripped:
            continue

        # Match numbered items: "1. **headline** — ..."
        m = re.match(r'^(\d+)\.\s+\*\*(.+?)\*\*', stripped)
        if m:
            items.append(f"{m.group(1)}. {m.group(2)}")

    return items


def fetch_seba_inbox() -> list[dict] | str:
    """Fetch unread + recent emails from Seba's inbox via IMAP."""
    host = os.environ.get('SEBA_IMAP_HOST', 'imap.gmail.com')
    user = os.environ.get('SEBA_IMAP_USER', '')
    password = os.environ.get('SEBA_IMAP_PASS', '')

    if not user or not password:
        return 'NOT_CONFIGURED'

    messages = []
    try:
        mail = imaplib.IMAP4_SSL(host, 993)
        mail.login(user, password)
        mail.select('INBOX')

        _, unseen_data = mail.search(None, 'UNSEEN')
        unseen_ids = set(unseen_data[0].split()) if unseen_data[0] else set()

        since = (datetime.now() - timedelta(days=7)).strftime('%d-%b-%Y')
        _, recent_data = mail.search(None, f'SINCE {since}')
        recent_ids = set(recent_data[0].split()) if recent_data[0] else set()

        all_ids = list(unseen_ids | recent_ids)[-25:]

        for mid in all_ids:
            _, hdr = mail.fetch(mid, '(BODY[HEADER.FIELDS (FROM SUBJECT DATE)])')
            raw = hdr[0][1] if hdr and hdr[0] else b''
            msg = email_lib.message_from_bytes(raw)
            frm = str(make_header(decode_header(msg.get('From', '(unknown)'))))
            subj = str(make_header(decode_header(msg.get('Subject', '(no subject)'))))
            date_str = msg.get('Date', '')
            messages.append({
                'from': frm,
                'subject': subj,
                'date': date_str,
                'unread': mid in unseen_ids,
            })

        mail.logout()
        messages.sort(key=lambda m: (0 if m['unread'] else 1))

    except Exception as e:
        return f'ERROR: {e}'

    return messages


def build_body(items: list[str], inbox) -> str:
    today = datetime.now().strftime('%A, %B %-d, %Y')
    sep = '=' * 52

    parts = [
        f'Good morning Seba! Daily briefing for {today}.',
        '',
        sep,
        'OPEN ITEMS / TO-DO  (from brain log)',
        sep,
    ]

    if items:
        for item in items:
            parts.append(f'  • {item}')
    else:
        parts.append('  (no open items found in brain log)')

    parts += ['', sep, 'YOUR INBOX — UNREAD / LAST 7 DAYS', sep]

    if inbox == 'NOT_CONFIGURED':
        parts.append('  Inbox reading not yet configured.')
        parts.append('  Add SEBA_IMAP_USER and SEBA_IMAP_PASS to ~/nightshift/.env to enable.')
    elif isinstance(inbox, str) and inbox.startswith('ERROR:'):
        parts.append(f'  Could not read inbox: {inbox}')
    elif not inbox:
        parts.append('  Inbox clear — no unread or recent messages.')
    else:
        for e in inbox:
            flag = '[UNREAD] ' if e.get('unread') else '         '
            parts.append(f"  {flag}From:    {e['from']}")
            parts.append(f"           Subject: {e['subject']}")
            parts.append(f"           Date:    {e['date']}")
            parts.append('')

    return '\n'.join(parts)


def main():
    load_env()
    items = extract_open_items()
    inbox = fetch_seba_inbox()
    body = build_body(items, inbox)

    result = subprocess.run(
        [sys.executable, str(TELEGRAM_SEND),
         '--to', 'seba',
         '--msg', body[:4000]],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        print(f'send failed: {result.stderr}', file=sys.stderr)
        sys.exit(result.returncode)

    inbox_count = len(inbox) if isinstance(inbox, list) else 'n/a'
    print(f'Sent to chat. Items: {len(items)}, Inbox entries: {inbox_count}')


if __name__ == '__main__':
    main()
