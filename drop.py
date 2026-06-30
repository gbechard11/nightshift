#!/usr/bin/env python3
"""Nightshift Drops CLI — create Laylo-style drop pages and grow an owned list.

A *drop* is a one-tap hosted landing page for a show or release. Fans leave
email + mobile; those opt-ins land in the master list that scripts/blast.py
sends from. This is the in-house replacement for Laylo's "drop page".

Examples:
  # Create a teaser drop (no tickets yet — pure "notify me"):
  drop.py create --id loud-sessions-wpg \
      --title "Loud Sessions" --subtitle "Winnipeg" \
      --venue "Park Theatre — Sun Jul 12" --city Winnipeg \
      --brand "Nightshift Entertainment" \
      --art https://.../flyer.jpg

  # Flip it live once tickets exist (adds a Get Tickets button):
  drop.py update --id loud-sessions-wpg --status live \
      --buy https://www.showpass.com/loud-sessions-wpg/

  drop.py list
  drop.py url loud-sessions-wpg
  drop.py signups loud-sessions-wpg --export /tmp/wpg.csv
  drop.py notify loud-sessions-wpg        # prints the ready-to-run blast command
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drop_common as dc  # noqa: E402

NIGHTSHIFT = dc.NIGHTSHIFT


def _apply_fields(drop: dict, args) -> dict:
    field_map = {
        "title": "title", "subtitle": "subtitle", "venue": "venue_line",
        "art": "art_url", "buy": "buy_url", "city": "city", "brand": "brand",
        "blurb": "blurb", "kicker": "kicker", "cta": "cta", "status": "status",
    }
    for argname, key in field_map.items():
        val = getattr(args, argname, None)
        if val is not None:
            drop[key] = val
    return drop


def cmd_create(args):
    drop_id = dc.slugify(args.id or args.title or "")
    if not drop_id:
        print(json.dumps({"ok": False, "error": "need --id or --title"})); return 1
    if dc.load_drop(drop_id) and not args.force:
        print(json.dumps({"ok": False, "error": f"drop '{drop_id}' exists (use update, or --force)"}))
        return 1
    drop = {
        "id": drop_id,
        "title": args.title or drop_id,
        "subtitle": "", "venue_line": "", "art_url": "", "buy_url": "",
        "city": "", "brand": "Nightshift Entertainment", "blurb": "",
        "kicker": "", "cta": "", "status": args.status or "teaser",
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _apply_fields(drop, args)
    dc.save_drop(drop)
    print(json.dumps({"ok": True, "id": drop_id, "url": dc.drop_url(drop_id),
                      "status": drop["status"]}, indent=2))
    return 0


def cmd_update(args):
    drop = dc.load_drop(args.id)
    if not drop:
        print(json.dumps({"ok": False, "error": f"no drop '{args.id}'"})); return 1
    _apply_fields(drop, args)
    dc.save_drop(drop)
    print(json.dumps({"ok": True, "id": drop["id"], "url": dc.drop_url(drop["id"]),
                      "status": drop.get("status")}, indent=2))
    return 0


def cmd_list(args):
    drops = dc.list_drops()
    if args.json:
        print(json.dumps(drops, indent=2)); return 0
    if not drops:
        print("No drops yet. Create one with: drop.py create --id <id> --title ..."); return 0
    for d in drops:
        n = dc.signup_count(d["id"])
        print(f"{d['id']:<28} {d.get('status','?'):<7} {n:>5} signups   {d.get('title','')}")
    return 0


def cmd_show(args):
    drop = dc.load_drop(args.id)
    if not drop:
        print(json.dumps({"ok": False, "error": f"no drop '{args.id}'"})); return 1
    drop = dict(drop)
    drop["_url"] = dc.drop_url(drop["id"])
    drop["_signups"] = dc.signup_count(drop["id"])
    print(json.dumps(drop, indent=2)); return 0


def cmd_url(args):
    if not dc.load_drop(args.id):
        print(json.dumps({"ok": False, "error": f"no drop '{args.id}'"})); return 1
    print(dc.drop_url(args.id)); return 0


def cmd_render(args):
    drop = dc.load_drop(args.id)
    if not drop:
        print(json.dumps({"ok": False, "error": f"no drop '{args.id}'"})); return 1
    html = dc.render_page(drop)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(html)
        print(json.dumps({"ok": True, "wrote": args.out})); return 0
    sys.stdout.write(html); return 0


def cmd_signups(args):
    per = dc._signups_path(args.id)
    if not os.path.exists(per):
        print(json.dumps({"ok": True, "id": args.id, "count": 0, "rows": []})); return 0
    if args.export:
        shutil.copyfile(per, args.export)
        print(json.dumps({"ok": True, "exported": args.export, "count": dc.signup_count(args.id)}))
        return 0
    with open(per, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(json.dumps({"ok": True, "id": args.id, "count": len(rows),
                      "rows": rows if args.full else rows[-10:]}, indent=2))
    return 0


def cmd_notify(args):
    """Prepare a blast to everyone who signed up for this drop.

    Confirm-first by design: we export the segment and PRINT the exact blast.py
    command rather than firing a mass send automatically."""
    per = dc._signups_path(args.id)
    n = dc.signup_count(args.id)
    if n == 0:
        print(json.dumps({"ok": False, "error": "no signups yet for this drop"})); return 1
    seg = os.path.join("/tmp", f"drop-{dc.slugify(args.id)}.csv")
    shutil.copyfile(per, seg)
    blast = os.path.join(NIGHTSHIFT, "scripts", "blast.py")
    drop = dc.load_drop(args.id) or {}
    subj = drop.get("title", args.id)
    cmd = (f"python3 {blast} --list {seg} --channel {args.channel} "
           f"--campaign drop-{dc.slugify(args.id)} "
           f"--subject {json.dumps(subj)} --body-file <BODY.txt> "
           f"--sms-body-file <SMS.txt>   # add --yes to send")
    print(json.dumps({
        "ok": True, "id": args.id, "signups": n, "segment_csv": seg,
        "next": "review then run the blast command below",
        "blast_command": cmd,
    }, indent=2))
    return 0


def main():
    p = argparse.ArgumentParser(description="Nightshift Drops — drop pages + owned-list capture")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_fields(sp):
        sp.add_argument("--title"); sp.add_argument("--subtitle")
        sp.add_argument("--venue", help="venue/date line")
        sp.add_argument("--art", help="hero image URL (use a real flyer/photo)")
        sp.add_argument("--buy", help="ticket URL (set + --status live to show Get Tickets)")
        sp.add_argument("--city"); sp.add_argument("--brand")
        sp.add_argument("--blurb"); sp.add_argument("--kicker")
        sp.add_argument("--cta", help="button label in notify mode (default 'Notify Me')")
        sp.add_argument("--status", choices=["teaser", "live", "closed"])

    sc = sub.add_parser("create"); sc.add_argument("--id"); sc.add_argument("--force", action="store_true")
    add_fields(sc); sc.set_defaults(func=cmd_create)

    su = sub.add_parser("update"); su.add_argument("--id", required=True)
    add_fields(su); su.set_defaults(func=cmd_update)

    sl = sub.add_parser("list"); sl.add_argument("--json", action="store_true"); sl.set_defaults(func=cmd_list)
    ss = sub.add_parser("show"); ss.add_argument("id"); ss.set_defaults(func=cmd_show)
    sr = sub.add_parser("url"); sr.add_argument("id"); sr.set_defaults(func=cmd_url)
    srn = sub.add_parser("render"); srn.add_argument("id"); srn.add_argument("--out"); srn.set_defaults(func=cmd_render)

    sg = sub.add_parser("signups"); sg.add_argument("id")
    sg.add_argument("--export"); sg.add_argument("--full", action="store_true")
    sg.set_defaults(func=cmd_signups)

    sn = sub.add_parser("notify"); sn.add_argument("id")
    sn.add_argument("--channel", default="all", choices=["email", "sms", "whatsapp", "both", "all"])
    sn.set_defaults(func=cmd_notify)

    args = p.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
