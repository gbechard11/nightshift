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
    }
    with open(_spec_path(a.id), "w") as f:
        json.dump(spec, f, indent=2)
    print(json.dumps({"ok": True, "queued": a.id, "city": a.city,
                      "segment_count": count, "subject": a.subject}))


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

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
