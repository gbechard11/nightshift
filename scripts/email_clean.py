#!/usr/bin/env python3
"""Nightly Pedro email cleanup.

Connects to Gmail via IMAP. Walks the Inbox UNSEEN messages. For each one
whose sender matches a junk pattern AND is NOT in the allowlist, applies
the "AutoTrash" Gmail label and removes the "\Inbox" label (= archive).
Messages then live under the AutoTrash label, out of the inbox, but
findable for review.

Config: /data/greg/brain/email-filters.json (junk + allowlist patterns).
Credentials: ~/nightshift/.env (IMAP_*).

Run from cron. Logs one JSON object per event to stdout — pipe to
logger -t nightshift-email-clean for syslog capture.
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import json
import os
import re
import sys
from datetime import datetime

CONFIG_PATH = os.environ.get("PEDRO_EMAIL_FILTERS", "/data/greg/brain/email-filters.json")
AUTOTRASH_LABEL = os.environ.get("PEDRO_AUTOTRASH_LABEL", "AutoTrash")


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


def load_filters() -> dict:
    if not os.path.exists(CONFIG_PATH):
        emit(event="config_missing", path=CONFIG_PATH)
        return {}
    with open(CONFIG_PATH) as f:
        return json.load(f)


def domain_match(addr: str, domains: list[str]) -> bool:
    if "@" not in addr:
        return False
    a_domain = addr.split("@", 1)[1].lower()
    for d in domains:
        d = d.lower().lstrip("@")
        if a_domain == d or a_domain.endswith("." + d):
            return True
    return False


def main() -> int:
    load_env()
    cfg = load_filters()
    junk_addrs = {s.lower() for s in cfg.get("junk_senders", [])}
    junk_domains = [d.lstrip("@").lower() for d in cfg.get("junk_domains", [])]
    allow_addrs = {s.lower() for s in cfg.get("allowlist_senders", [])}
    allow_domains = [d.lstrip("@").lower() for d in cfg.get("allowlist_domains", [])]

    host = os.environ.get("IMAP_HOST", "imap.gmail.com")
    port = int(os.environ.get("IMAP_PORT", "993"))
    user = os.environ.get("IMAP_USER")
    pwd = os.environ.get("IMAP_PASS")
    if not user or not pwd:
        emit(event="error", error="IMAP_USER/IMAP_PASS not set")
        return 1

    m = imaplib.IMAP4_SSL(host, port, timeout=60)
    m.login(user, pwd)
    m.select('"INBOX"')

    # Ensure AutoTrash label exists (Gmail IMAP creates folder = label)
    typ, _ = m.create(f'"{AUTOTRASH_LABEL}"')
    # Gmail returns NO if label exists, that's fine — ignore

    typ, data = m.search(None, "UNSEEN")
    if typ != "OK":
        emit(event="error", error=f"IMAP search failed: {data}")
        return 1
    ids = data[0].split()
    emit(event="start", unseen_count=len(ids), config=CONFIG_PATH)

    moved = 0
    skipped_allow = 0
    examples: list[dict] = []

    for msg_id in ids:
        typ, hdr_data = m.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
        if typ != "OK":
            continue
        raw = None
        for item in hdr_data:
            if isinstance(item, tuple) and len(item) >= 2:
                raw = item[1]
                break
        if not raw:
            continue
        try:
            parsed = email.message_from_bytes(raw, policy=email.policy.compat32)
            from_field = parsed.get("From", "") or ""
            subject = parsed.get("Subject", "") or ""
        except Exception:
            continue

        addrs = re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", from_field)
        if not addrs:
            continue
        addr = addrs[0].lower()

        # Allowlist always wins
        if addr in allow_addrs or domain_match(addr, allow_domains):
            skipped_allow += 1
            continue
        if addr not in junk_addrs and not domain_match(addr, junk_domains):
            continue

        # Junk + not allowlisted: tag AutoTrash + archive from Inbox
        try:
            m.store(msg_id, "+X-GM-LABELS", AUTOTRASH_LABEL)
            m.store(msg_id, "-X-GM-LABELS", "\\Inbox")
            moved += 1
            if len(examples) < 25:
                examples.append({"from": addr, "subject": subject[:80]})
        except Exception as e:
            emit(event="store_error", error=str(e), addr=addr)

    m.close()
    m.logout()

    emit(
        event="complete",
        unseen_scanned=len(ids),
        moved_to_autotrash=moved,
        skipped_allowlist=skipped_allow,
    )
    for e in examples:
        emit(event="moved", **e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
