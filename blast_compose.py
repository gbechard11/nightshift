#!/usr/bin/env python3
"""Server-side blast composer used by the NS Team Bot's draft_blast MCP tool.

The locked-down employee agent CANNOT run shell/blast.py. It calls the
draft_blast MCP tool, which delegates here. This module (trusted server code)
does the privileged work, but ONLY up to a queued draft + a preview to Greg —
it NEVER sends to a real list. The real send stays an owner-only action
(blast_queue.py send <id> --yes, run by Greg via Pedro).
"""
from __future__ import annotations

import csv
import os
import re
import secrets
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON = "/home/gregnightshift/nightshift/.venv/bin/python"
SCRIPTS = "/home/gregnightshift/nightshift/scripts"
BLAST = os.path.join(SCRIPTS, "blast.py")
UPLOAD = os.path.join(SCRIPTS, "blast_upload.py")
QUEUE = os.path.join(SCRIPTS, "blast_queue.py")
CONTACTS = "/data/greg/contacts/ticketweb_customers.csv"
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "gbechard11@gmail.com")
FROM_ADDR = "info@nightshiftent.ca"

# Per Greg's standing rule: blasts that ANDREW initiates default to Pawn Shop
# Live's address, not the Nightshift address. He will say if a given blast is
# NOT from Pawnshop (handled case-by-case); the default lives here.
ANDREW_UID = "8621126122"
PAWNSHOP_FROM = "gm@pawnshop-live.ca"
SENDERS_PATH = "/home/gregnightshift/nightshift/blast-senders.json"


def _sender_for(rid: str) -> str:
    """Default From address for whoever is drafting the blast."""
    return PAWNSHOP_FROM if str(rid).strip() == ANDREW_UID else FROM_ADDR


def _sender_ready(addr: str) -> bool:
    """True if blast.py can legitimately send from addr (has an SMTP profile)."""
    import json
    if addr.lower().endswith("@nightshiftent.ca"):
        return True  # served by the default SES profile (same-domain rule)
    try:
        cfg = json.load(open(SENDERS_PATH))
    except Exception:
        return False
    dom = addr.rsplit("@", 1)[-1].lower()
    return bool(cfg.get(addr) or cfg.get(dom))


def _self_approvers() -> set:
    """Telegram uids allowed to approve + send their OWN drafted blasts
    (Seba + Andrew by default; override with BLAST_SELF_APPROVE in .env)."""
    raw = os.environ.get("BLAST_SELF_APPROVE", "8722742818,8621126122")
    return {x.strip() for x in raw.replace(";", ",").split(",") if x.strip()}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:24] or "blast"


def _run(cmd: list[str], timeout: int = 180) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    out = (r.stdout + r.stderr).decode("utf-8", errors="replace").strip()
    return r.returncode, out


def _segment_count(city: str) -> int:
    try:
        rows = list(csv.DictReader(open(CONTACTS, encoding="utf-8-sig")))
    except Exception:
        return -1
    cl = city.strip().lower()
    return sum(1 for r in rows
               if (r.get("City") or "").strip().lower() == cl
               and (r.get("Email") or "").strip())


def draft(rid, requester, city, subject, html, image_drive_ids, gdrive, dl_dir) -> str:
    city = (city or "").strip()
    subject = (subject or "").strip()
    if not city or not subject or not html.strip():
        return "I need a city, a subject, and the HTML body to draft a blast."
    if _segment_count(city) <= 0:
        return (f"I couldn't find any contacts for city '{city}'. "
                f"Check the spelling (e.g. Edmonton, Winnipeg, Calgary).")

    from_addr = _sender_for(rid)
    if not _sender_ready(from_addr):
        return (
            f"This blast would go out from {from_addr} (Pawn Shop Live), but that "
            f"sender is not connected yet. In the NS Team Bot run /setupemail for "
            f"{from_addr} (have its SMTP host, login, and an app password ready). "
            f"Once it is connected the blast system picks it up automatically -- "
            f"then re-run this."
        )

    bid = f"emp-{_slug(city)}-{secrets.token_hex(3)}"
    os.makedirs(dl_dir, exist_ok=True)

    # 1) resolve images: Drive id -> S3 URL -> swap cid:NAME in the HTML
    pairs = [p for p in re.split(r"[,\n]", image_drive_ids or "") if "=" in p]
    for pair in pairs:
        nm, _, fid = pair.partition("=")
        nm, fid = nm.strip(), fid.strip()
        if not (nm and fid):
            continue
        local = os.path.join(dl_dir, f"img_{secrets.token_hex(6)}")
        msg = gdrive(["download", "--file-id", fid, "--out", local])
        if not os.path.exists(local):
            return f"Couldn't download image '{nm}' (Drive id {fid}): {msg}"
        # give it a sane extension from the download name if present
        m = re.search(r"name=(.+)$", msg)
        ext = os.path.splitext(m.group(1).strip())[1] if m else ".png"
        img = local + (ext or ".png")
        os.rename(local, img)
        rc, out = _run([PYTHON, UPLOAD, "--prefix", bid, img])
        url = out.strip().splitlines()[-1] if rc == 0 else ""
        if not url.startswith("http"):
            return f"Couldn't host image '{nm}' on S3: {out}"
        html = html.replace(f"cid:{nm}", url)

    # 2) save the rendered HTML
    html_path = os.path.join(dl_dir, f"{bid}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    # 3) queue it (owner-only send later)
    rc, out = _run([PYTHON, QUEUE, "add", "--id", bid, "--city", city,
                    "--subject", subject, "--html-file", html_path,
                    "--list", CONTACTS, "--from", from_addr,
                    "--created-by", requester, "--created-by-uid", str(rid)])
    if rc != 0:
        return f"Couldn't queue the blast: {out}"

    # 4) count the segment once, then route the draft to whoever approves it
    count = _segment_count(city)
    self_approve = str(rid).strip() in _self_approvers()
    import employee_notify

    if self_approve:
        # The requesting employee reviews their OWN draft and approves it;
        # Greg is out of the loop for Seba's / Andrew's blasts (his standing rule).
        try:
            import employee_email
            sender = employee_email.sender_for(rid) or {}
            reviewer_email = sender.get("from") or OWNER_EMAIL
        except Exception:
            reviewer_email = OWNER_EMAIL
        first = requester.split()[0] if requester else "there"
        prev_csv = os.path.join(dl_dir, f"{bid}_preview.csv")
        with open(prev_csv, "w", encoding="utf-8") as f:
            f.write("first,email\n%s,%s\n" % (first, reviewer_email))
        _run([PYTHON, BLAST, "--list", prev_csv, "--channel", "email",
              "--from", from_addr, "--subject", subject, "--html-file", html_path,
              "--campaign", f"{bid}-preview", "--yes"])
        try:
            employee_notify.notify_blast_approval(
                rid, bid,
                f"\U0001F4E3 Your email blast is ready to review:\n"
                f"\u2022 {subject}\n\u2022 From: {from_addr}\n"
                f"\u2022 Audience: {city} (~{count:,} contacts)\n"
                f"\u2022 A preview was just emailed to {reviewer_email}.\n\n"
                f"Open the preview, then tap Approve & send to blast it to all "
                f"~{count:,} {city} contacts. Nothing goes out until you tap Approve."
            )
        except Exception:
            pass
        return (
            f"\u2705 Drafted '{subject}' for {city} (~{count:,} contacts) and emailed "
            f"you a preview at {reviewer_email}. I also sent Approve / Cancel buttons here "
            f"-- review the preview, then tap Approve & send to blast it. Queue id: {bid}."
        )

    # 5) otherwise the owner (Greg) reviews + approves -- original behavior
    prev_csv = os.path.join(dl_dir, f"{bid}_preview.csv")
    with open(prev_csv, "w", encoding="utf-8") as f:
        f.write("first,email\nGreg,%s\n" % OWNER_EMAIL)
    _run([PYTHON, BLAST, "--list", prev_csv, "--channel", "email",
          "--from", from_addr, "--subject", subject, "--html-file", html_path,
          "--campaign", f"{bid}-preview", "--yes"])
    try:
        employee_notify.notify_owner(
            f"\U0001F4E3 {requester} drafted an email blast for your approval:\n"
            f"\u2022 {subject}\n\u2022 From: {from_addr}\n"
            f"\u2022 Audience: {city} (~{count:,} contacts)\n"
            f"\u2022 Preview just sent to your inbox.\n\n"
            f"To send it, tell Pedro: send blast {bid}  "
            f"(runs `blast_queue.py send {bid} --yes`). Nothing goes out until you do."
        )
    except Exception:
        pass
    return (
        f"\u2705 Drafted and queued '{subject}' for {city} (~{count:,} contacts). "
        f"Greg has a live preview in his inbox and a note to approve it -- "
        f"nothing sends until he gives the go. Queue id: {bid}."
    )


if __name__ == "__main__":  # quick manual test: blast_compose.py <city> <subject> <htmlfile> [name=fileid]
    def _g(args, timeout=120):
        env = {**os.environ, "GCAL_TOKEN": os.environ.get("EMPLOYEE_GDRIVE_TOKEN", "/data/employees/token.json")}
        r = subprocess.run([sys.executable, os.path.join(HERE, "gdrive.py"), *args],
                           cwd=HERE, env=env, capture_output=True, timeout=timeout)
        return (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    _city, _subj, _htmlf = sys.argv[1], sys.argv[2], sys.argv[3]
    _imgs = sys.argv[4] if len(sys.argv) > 4 else ""
    print(draft(rid="0", requester="TEST", city=_city, subject=_subj,
                html=open(_htmlf).read(), image_drive_ids=_imgs, gdrive=_g,
                dl_dir="/data/employees/dl"))
