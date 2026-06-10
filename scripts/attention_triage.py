#!/usr/bin/env python3
"""Attention-triage (READ-ONLY). Surface UNSEEN inbox mail that looks like a
genuine inbound from a real person that Greg has NOT yet replied to.

Deletes/moves/flags NOTHING. Writes a ranked digest to attention_digest.txt.

Logic:
  candidate  = unseen, dated within --days, no List-Unsubscribe header,
               sender not an automation address, not from Greg himself,
               subject not an auto-reply/calendar/bounce.
  needs-reply = candidate whose Gmail thread has NO sent message from Greg
               dated at/after the inbound message (i.e. he hasn't answered).
  One row per thread (the newest inbound in that thread), newest first.
"""
import argparse
import email
import email.policy
import imaplib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime

sys.path.insert(0, "/home/gregnightshift/nightshift/scripts")
from email_notify import decode_header_str, load_env

ap = argparse.ArgumentParser()
ap.add_argument("--days", type=int, default=90, help="only consider mail newer than N days")
ap.add_argument("--top", type=int, default=80, help="rows to print to stdout")
ap.add_argument("--json", action="store_true", help="also emit JSON to attention_digest.json")
ap.add_argument("--brief", action="store_true", help="print a short DM-sized summary (deal-critical only)")
args = ap.parse_args()

load_env(os.path.expanduser("~/nightshift/.env"))
ME = os.environ["IMAP_USER"].lower()  # gbechard11@gmail.com
HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")

# Senders on these domains are "internal" (Greg's own org / venues).
INTERNAL_DOMAINS = (
    "nightshiftent.ca", "pawnshop-live.ca", "pawn-shop-live.ca",
    "eecwinnipeg.com", "eecwinnipeg.ca", "unionhall.ca",
)
# Priority tiers — checked against sender+subject, P1 wins over P2.
P1_KW = [  # money / deal-critical
    "advance", "settlement", "settle", "offer", "deposit", "contract", "wire",
    "invoice", "overdue", "payment", "guarantee", "hold", "holds",
    "ticket count", "ticket counts", "counts", "quote", "payout", "balance",
    "past due", "remittance", "deal memo", "po #", "purchase order",
]
P2_KW = [  # show ops / scheduling
    "run of show", "load-in", "load in", "routing", "avails", "schedule",
    "hospitality", "rider", "stage plot", "set time", "walkout", "day sheet",
    "booking", "confirm", "announce", "on-sale", "on sale", "press",
]


def priority(sender, subject):
    blob = (sender + " " + subject).lower()
    if any(k in blob for k in P1_KW):
        return 1
    if any(k in blob for k in P2_KW):
        return 2
    return 3


def is_internal(se):
    return any(se.endswith("@" + d) or se.endswith("." + d) for d in INTERNAL_DOMAINS)

# True automation markers only — deliberately NOT info@/support@/team@,
# since real venue/booking inquiries often come from those role addresses.
AUTOMATED = [
    "no-reply", "noreply", "donotreply", "do-not-reply", "do_not_reply",
    "notification", "notifications", "newsletter", "mailer-daemon",
    "postmaster", "bounce", "mailchimp", "sendgrid", "updates@",
    "marketing@", "alerts@", "@news", "noreply@", "automated",
]
AUTO_SUBJECT = [
    "out of office", "automatic reply", "auto-reply", "autoreply",
    "undeliverable", "delivery status notification", "read:", "accepted:",
    "declined:", "tentative:", "invitation:", "canceled:", "cancelled:",
    "updated invitation",
]
# Transactional / service notices: real, but FYI — not something Greg replies to.
# Routed to a separate bucket so they don't clutter the reply list, but nothing
# money-related is hidden. (Invoices / payment-requests deliberately stay in the
# reply list since they may need a human response.)
TXN_SENDER = [
    "interac.ca", "@td.com", "tdcanadatrust", "@email.apple.com", "testflight",
    "wordpress@", "@intuit.com", "quickbooks", "amazonses", "@patronscan.com",
]
TXN_SUBJECT = [
    "interac e-transfer", "your receipt", "receipt from", "receipt for",
    "payment receipt", "password changed", "password reset", "welcome to ",
    "production test", "ses ", "account suspension", "security alert",
    "sign-in", "verification code", "***spam***",
]


def is_txn(sender, subject):
    s, sub = sender.lower(), subject.lower()
    return any(p in s for p in TXN_SENDER) or any(p in sub for p in TXN_SUBJECT)

THR = re.compile(rb"X-GM-THRID (\d+)")


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def is_automated(sender):
    s = sender.lower()
    return any(p in s for p in AUTOMATED)


def parse_when(raw):
    try:
        w = parsedate_to_datetime(raw)
    except Exception:
        return None
    if w is None:
        return None
    if w.tzinfo is None:
        w = w.replace(tzinfo=timezone.utc)
    return w


cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

m = imaplib.IMAP4_SSL(HOST, 993)
m.login(os.environ["IMAP_USER"], os.environ["IMAP_PASS"])

# ---- 1) gather unseen candidates, keep newest inbound per thread ----
m.select("INBOX", readonly=True)
_, d = m.uid("search", None, "UNSEEN")
uids = d[0].split()
hf = "(X-GM-THRID BODY.PEEK[HEADER.FIELDS (FROM DATE SUBJECT LIST-UNSUBSCRIBE)])"
cands = {}        # thrid -> newest candidate record
seen_unseen = 0
for c in chunks(uids, 400):
    _, resp = m.uid("fetch", b",".join(c).decode(), hf)
    for it in resp:
        if not isinstance(it, tuple):
            continue
        seen_unseen += 1
        mt = THR.search(it[0])
        thrid = mt.group(1).decode() if mt else None
        p = email.message_from_bytes(it[1], policy=email.policy.default)
        if p.get("List-Unsubscribe"):
            continue
        sender = decode_header_str(p.get("From", ""))
        if is_automated(sender):
            continue
        when = parse_when(p.get("Date", ""))
        if when is None or when < cutoff:
            continue
        addrs = getaddresses([sender])
        se = (addrs[0][1] if addrs and addrs[0][1] else sender).lower().strip()
        if se == ME:
            continue
        subject = decode_header_str(p.get("Subject", ""))
        if any(k in subject.lower() for k in AUTO_SUBJECT):
            continue
        thrid = thrid or f"nouid-{se}-{subject[:30]}"
        rec = {"thrid": thrid, "from": sender, "email": se,
               "subject": subject, "date": when}
        if thrid not in cands or when > cands[thrid]["date"]:
            cands[thrid] = rec

# ---- 2) latest sent date per thread (did Greg reply?) ----
m.select('"[Gmail]/Sent Mail"', readonly=True)
since = cutoff.strftime("%d-%b-%Y")
_, d = m.uid("search", None, "SINCE", since)
sent = d[0].split()
sent_latest = defaultdict(lambda: None)
for c in chunks(sent, 400):
    _, resp = m.uid("fetch", b",".join(c).decode(),
                    "(X-GM-THRID BODY.PEEK[HEADER.FIELDS (DATE)])")
    for it in resp:
        if not isinstance(it, tuple):
            continue
        mt = THR.search(it[0])
        thrid = mt.group(1).decode() if mt else None
        if not thrid:
            continue
        p = email.message_from_bytes(it[1], policy=email.policy.default)
        when = parse_when(p.get("Date", ""))
        if when is None:
            continue
        if sent_latest[thrid] is None or when > sent_latest[thrid]:
            sent_latest[thrid] = when
m.logout()

# ---- 3) keep only threads Greg hasn't answered; split reply vs FYI ----
needs, fyi = [], []
for thrid, rec in cands.items():
    sl = sent_latest.get(thrid)
    if sl is not None and sl >= rec["date"]:
        continue  # he replied after the inbound -> handled
    (fyi if is_txn(rec["from"], rec["subject"]) else needs).append(rec)
needs.sort(key=lambda r: r["date"], reverse=True)
fyi.sort(key=lambda r: r["date"], reverse=True)

now = datetime.now(timezone.utc)
PTAG = {1: "$$", 2: "show", 3: "  "}

# annotate + sort by priority (P1 first), then newest
for r in needs:
    r["prio"] = priority(r["from"], r["subject"])
ext = sorted((r for r in needs if not is_internal(r["email"])),
             key=lambda r: (r["prio"], -r["date"].timestamp()))
intl = sorted((r for r in needs if is_internal(r["email"])),
              key=lambda r: (r["prio"], -r["date"].timestamp()))

lines = []


def fmt(r):
    age = (now - r["date"]).days
    nm = re.sub(r"\s*<[^>]+>", "", r["from"]).strip().strip('"') or r["email"]
    return f"[{PTAG[r.get('prio', 3)]:>4}] {r['date']:%Y-%m-%d} ({age:>3}d)  {nm[:24]:24}  {r['subject'][:42]}"


def section(title, rows, cap):
    lines.append("")
    lines.append(f"{title} ({len(rows)})")
    lines.append("-" * 78)
    for r in rows[:cap]:
        lines.append(fmt(r))
    if len(rows) > cap:
        lines.append(f"... +{len(rows) - cap} more (raise --top to see all)")


p1 = sum(1 for r in needs if r["prio"] == 1)
lines.append(f"ATTENTION TRIAGE  (window: last {args.days} days)   priority: [$$]=money/deal  [show]=ops")
lines.append(f"unseen scanned: {seen_unseen}   NEEDS REPLY: {len(needs)} "
             f"(deal-critical: {p1})   external: {len(ext)}  internal: {len(intl)}   FYI: {len(fyi)}")
lines.append("=" * 78)
section("EXTERNAL — agents, promoters, venues, public", ext, args.top)
section("INTERNAL — Nightshift / Pawn Shop team", intl, max(20, args.top // 2))
section("FYI / TRANSACTIONAL — money & service notices, no reply needed", fyi, 20)

# top senders among needs-reply
from collections import Counter
bysender = Counter(r["email"] for r in needs)
lines.append("")
lines.append("--- top unanswered senders ---")
for e, n in bysender.most_common(15):
    lines.append(f"  {n:4d}  {e}")

report = "\n".join(lines)
# Always persist the full digest so /triage and the file stay in sync.
with open("/data/greg/inbox_cleanup/attention_digest.txt", "w") as f:
    f.write(report + "\n")
if args.json:
    with open("/data/greg/inbox_cleanup/attention_digest.json", "w") as f:
        json.dump([{**r, "date": r["date"].isoformat()} for r in needs], f, indent=2)

if args.brief:
    # Short, DM-sized: counts + deal-critical only.
    def brief_line(r):
        age = (now - r["date"]).days
        nm = re.sub(r"\s*<[^>]+>", "", r["from"]).strip().strip('"') or r["email"]
        return f"• {r['date']:%m-%d} ({age}d) {nm[:22]} — {r['subject'][:40]}"

    b = [f"🗂️ Attention triage · last {args.days}d",
         f"{len(needs)} need reply · {p1} deal-critical · {len(ext)} ext / {len(intl)} int",
         ""]
    ext_p1 = [r for r in ext if r["prio"] == 1][:12]
    int_p1 = [r for r in intl if r["prio"] == 1][:6]
    if ext_p1:
        b.append("🔴 DEAL-CRITICAL — external:")
        b += [brief_line(r) for r in ext_p1]
    if int_p1:
        b.append("")
        b.append("👥 TEAM — deal-critical:")
        b += [brief_line(r) for r in int_p1]
    b.append("")
    b.append("Full list: /triage")
    print("\n".join(b))
else:
    print(report)
    print(f"\nDigest -> /data/greg/inbox_cleanup/attention_digest.txt")
