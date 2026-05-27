#!/usr/bin/env python3
"""Phase 3 one-shot: bulk-archive all Gmail Promotions + Updates to AutoTrash.

Uses Gmail IMAP's X-GM-RAW extension to search by category, then batched UID
STORE to add the AutoTrash label and remove the \\Inbox label (= archive)
in chunks of 1000 messages per IMAP roundtrip. Two orders of magnitude
faster than the per-message loop in email_clean.py.

Not a deletion — every message is still in Gmail under the AutoTrash label,
recoverable by clicking "Move to Inbox" on any of them. Greg can purge the
label later from Gmail UI when comfortable.
"""
from __future__ import annotations

import imaplib
import json
import os
import sys
from datetime import datetime

AUTOTRASH_LABEL = os.environ.get("PEDRO_AUTOTRASH_LABEL", "AutoTrash")
BATCH = int(os.environ.get("PEDRO_BATCH_SIZE", "1000"))


def emit(**kwargs) -> None:
    print(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), **kwargs}), flush=True)


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
            os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    load_env()
    user = os.environ.get("IMAP_USER")
    pwd = os.environ.get("IMAP_PASS")
    if not user or not pwd:
        emit(event="error", error="IMAP creds missing")
        return 1

    m = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=120)
    m.login(user, pwd)
    m.select('"INBOX"')
    m.create(f'"{AUTOTRASH_LABEL}"')  # noop if exists

    total = 0
    for category in ["promotions", "updates"]:
        emit(event="search_start", category=category)
        typ, data = m.uid("SEARCH", "X-GM-RAW", f'"category:{category}"')
        if typ != "OK":
            emit(event="search_error", category=category, raw=str(data))
            continue
        uids = data[0].split()
        emit(event="search_done", category=category, count=len(uids))

        for i in range(0, len(uids), BATCH):
            chunk = uids[i:i + BATCH]
            seq = b",".join(chunk).decode()
            try:
                m.uid("STORE", seq, "+X-GM-LABELS", AUTOTRASH_LABEL)
                # IMAP requires the system label wrapped in parens for the STORE list form;
                # passing just "\\Inbox" gets quoted as a literal string and silently no-ops.
                m.uid("STORE", seq, "-X-GM-LABELS", "(\\Inbox)")
                total += len(chunk)
            except Exception as e:
                emit(event="store_error", category=category, chunk_start=i, error=str(e))
                continue
            emit(
                event="progress",
                category=category,
                archived_this_category=min(i + BATCH, len(uids)),
                of=len(uids),
                running_total=total,
            )

        emit(event="category_done", category=category, archived=len(uids))

    m.close()
    m.logout()
    emit(event="complete", total_archived=total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
