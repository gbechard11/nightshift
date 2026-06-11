#!/usr/bin/env python3
"""Owner-side blast queue: prepared blasts wait here for Greg's go.

The employee NS Team Bot can PREPARE + PREVIEW + QUEUE a blast (via the
draft_blast MCP tool) but can NEVER send to a real list. Queued blasts land
here; only Greg (through Pedro, which has blast.py) fires them. Keeps the
irreversible mass-send a human, owner-only action.

Queue dir: /data/greg/blast_queue/ (outside the git repo, survives auto-deploy).

Commands:
  blast_queue.py add --id <slug> --city <C> --subject <S> --html-file <path>
                     --list <csv> [--from <addr>] [--created-by <name>]
  blast_queue.py list
  blast_queue.py show <id>
  blast_queue.py send <id> [--yes]     # preview unless --yes; --yes does the real send
  blast_queue.py cancel <id>
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

QUEUE_DIR = "/data/greg/blast_queue"
BLAST = "/home/gregnightshift/nightshift/scripts/blast.py"
PYTHON = "/home/gregnightshift/nightshift/.venv/bin/python"

# Telegram uids allowed to self-approve (and therefore have their scheduled blasts
# auto-fired by the cron). Must match BLAST_SELF_APPROVE in .env.
_SELF_APPROVERS = {"8722742818", "8621126122"}


def _spec_path(qid: str) -> str:
    return os.path.join(QUEUE_DIR, f"{qid}.json")


def _count_segment(list_path: str, city: str) -> int:
    try:
        with open(list_path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return -1
    if not city:
        return sum(1 for r in rows if (r.get("email") or r.get("Email") or "").strip())
    cl = city.strip().lower()
    n = 0
    for r in rows:
        rc = (r.get("city") or r.get("City") or "").strip().lower()
        if rc == cl and (r.get("email") or r.get("Email") or "").strip():
            n += 1
    return n


def cmd_add(a: argparse.Namespace) -> None:
    os.makedirs(QUEUE_DIR, exist_ok=True)
    src = Path(a.html_file)
    if not src.exists():
        sys.exit(f"html file not found: {a.html_file}")
    if not Path(a.list).exists():
        sys.exit(f"list not found: {a.list}")
    # copy the HTML into the queue so it's stable even if /tmp is cleared
    html_dest = os.path.join(QUEUE_DIR, f"{a.id}.html")
    shutil.copyfile(src, html_dest)
    count = _count_segment(a.list, a.city)
    spec = {
        "id": a.id,
        "city": a.city,
        "subject": a.subject,
        "html_file": html_dest,
        "list": a.list,
        "from": a.from_addr,
        "segment_count": count,
        "created_by": a.created_by or "owner",
        "created_by_uid": a.created_by_uid or "",
        "status": "queued",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "send_at": a.send_at or None,
    }
    with open(_spec_path(a.id), "w") as f:
        json.dump(spec, f, indent=2)
    print(json.dumps({"ok": True, "queued": a.id, "city": a.city,
                      "segment_count": count, "subject": a.subject,
                      "send_at": a.send_at or None}))


def cmd_list(_a: argparse.Namespace) -> None:
    if not os.path.isdir(QUEUE_DIR):
        print("(queue empty)")
        return
    specs = sorted(Path(QUEUE_DIR).glob("*.json"))
    if not specs:
        print("(queue empty)")
        return
    for p in specs:
        try:
            s = json.load(open(p))
        except Exception:
            continue
        print(f"[{s.get('status','?'):8}] {s['id']:24} {s.get('city',''):14} "
              f"~{s.get('segment_count','?')} → {s.get('subject','')}  (by {s.get('created_by','?')})")


def cmd_show(a: argparse.Namespace) -> None:
    p = _spec_path(a.id)
    if not os.path.exists(p):
        sys.exit(f"no queued blast '{a.id}'")
    print(open(p).read())


def cmd_send(a: argparse.Namespace) -> None:
    p = _spec_path(a.id)
    if not os.path.exists(p):
        sys.exit(f"no queued blast '{a.id}'")
    s = json.load(open(p))
    if s.get("status") == "sent" and not a.force:
        sys.exit(f"'{a.id}' already sent ({s.get('sent_at')}). Use --force to resend.")
    cmd = [PYTHON, BLAST, "--list", s["list"], "--channel", "email",
           "--from", s["from"], "--subject", s["subject"],
           "--html-file", s["html_file"], "--campaign", s["id"]]
    if s.get("city"):
        cmd += ["--city", s["city"]]
    if a.yes:
        cmd.append("--yes")
    print(f"$ {' '.join(cmd)}\n")
    r = subprocess.run(cmd)
    if a.yes and r.returncode == 0:
        s["status"] = "sent"
        s["sent_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        json.dump(s, open(p, "w"), indent=2)
        print(f"\n[queue] marked '{a.id}' as sent.")


def cmd_cancel(a: argparse.Namespace) -> None:
    p = _spec_path(a.id)
    if not os.path.exists(p):
        sys.exit(f"no queued blast '{a.id}'")
    s = json.load(open(p))
    s["status"] = "cancelled"
    json.dump(s, open(p, "w"), indent=2)
    print(f"cancelled '{a.id}'")


def _tg_notify(uid: str, text: str) -> None:
    """Send a plain Telegram message via the Employee Bot. Best-effort."""
    import os as _os, urllib.request as _ur, urllib.parse as _up
    token = _os.environ.get("EMPLOYEE_BOT_TOKEN", "")
    if not token or not uid:
        return
    try:
        data = _up.urlencode({"chat_id": uid, "text": text,
                               "disable_web_page_preview": "true"}).encode()
        _ur.urlopen(_ur.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                data=data), timeout=10).read()
    except Exception:
        pass


def cmd_fire_scheduled(_a: argparse.Namespace) -> None:
    """Fire all queued blasts whose send_at <= now (called by cron every minute).
    Only auto-fires blasts created by self-approvers (Seba/Andrew); others still
    require explicit owner approval."""
    now_str = time.strftime("%Y-%m-%dT%H:%M:%S")
    if not os.path.isdir(QUEUE_DIR):
        return

    # Load SELF_APPROVERS from env if set, fall back to hardcoded default.
    raw = os.environ.get("BLAST_SELF_APPROVE", ",".join(_SELF_APPROVERS))
    self_approvers = {x.strip() for x in raw.replace(";", ",").split(",") if x.strip()}

    fired = 0
    for p in sorted(Path(QUEUE_DIR).glob("*.json")):
        try:
            s = json.load(open(p))
        except Exception:
            continue
        if s.get("status") != "queued":
            continue
        send_at = s.get("send_at")
        if not send_at:
            continue
        if str(s.get("created_by_uid", "")) not in self_approvers:
            continue
        if send_at > now_str:
            continue  # Not yet due

        qid = s["id"]
        print(f"[{now_str}] Firing scheduled blast: {qid} (due {send_at})")
        cmd = [PYTHON, BLAST, "--list", s["list"], "--channel", "email",
               "--from", s["from"], "--subject", s["subject"],
               "--html-file", s["html_file"], "--campaign", qid]
        if s.get("city"):
            cmd += ["--city", s["city"]]
        cmd.append("--yes")
        r = subprocess.run(cmd, capture_output=True, text=True)
        success = r.returncode == 0
        s["status"] = "sent" if success else "failed"
        s["sent_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        s["sent_method"] = "scheduled"
        json.dump(s, open(p, "w"), indent=2)

        uid = s.get("created_by_uid", "")
        city = s.get("city", "")
        count = s.get("segment_count", "?")
        subj = s.get("subject", "")
        if success:
            print(f"[{now_str}] Sent: {qid}")
            _tg_notify(uid, f"✅ Scheduled blast sent: \"{subj}\" → "
                            f"{city} (~{count} contacts). Queue id: {qid}.")
        else:
            err = ((r.stdout or "") + (r.stderr or ""))[-300:]
            print(f"[{now_str}] FAILED: {qid}: {err}")
            _tg_notify(uid, f"⚠️ Scheduled blast FAILED: \"{subj}\" "
                            f"(queue id {qid}). Error: {err[:200]}")
        fired += 1

    if fired:
        print(f"[{now_str}] fire-scheduled: {fired} blast(s) processed")


def main() -> None:
    p = argparse.ArgumentParser(description="Owner-side blast queue")
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("add")
    pa.add_argument("--id", required=True)
    pa.add_argument("--city", default="")
    pa.add_argument("--subject", required=True)
    pa.add_argument("--html-file", required=True)
    pa.add_argument("--list", required=True)
    pa.add_argument("--from", dest="from_addr", default="info@nightshiftent.ca")
    pa.add_argument("--created-by", default="")
    pa.add_argument("--created-by-uid", dest="created_by_uid", default="")
    pa.add_argument("--send-at", dest="send_at", default="",
                    help="ISO datetime (YYYY-MM-DDTHH:MM:SS) to auto-fire this blast")
    pa.set_defaults(func=cmd_add)

    sub.add_parser("list").set_defaults(func=cmd_list)

    ps = sub.add_parser("show")
    ps.add_argument("id")
    ps.set_defaults(func=cmd_show)

    pse = sub.add_parser("send")
    pse.add_argument("id")
    pse.add_argument("--yes", action="store_true", help="actually send (omit to preview)")
    pse.add_argument("--force", action="store_true", help="resend even if already sent")
    pse.set_defaults(func=cmd_send)

    pc = sub.add_parser("cancel")
    pc.add_argument("id")
    pc.set_defaults(func=cmd_cancel)

    sub.add_parser("fire-scheduled",
                   help="Fire all due scheduled blasts (run by cron every minute)"
                   ).set_defaults(func=cmd_fire_scheduled)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
