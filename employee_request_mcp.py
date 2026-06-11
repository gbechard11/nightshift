#!/usr/bin/env python3
"""Stdio MCP server exposing the NS Team Bot agent's safe action tools.

Launched by claude (via --mcp-config employee_requests.mcp.json) for each
employee turn. Gives the locked-down chat agent (file/shell tools denied) a few
SAFE capabilities so it doesn't have to punt everything to Greg:

  - submit_request         : forward a request/idea to Greg for approval
  - email_send             : send mail (with optional Drive attachments) FROM
                             the employee's own configured address
  - remember / recall      : persistent per-employee memory notes
  - drive_list / drive_find / drive_read_text : browse + read Greg's Drive
  - drive_make_folder / drive_create_text_file : create NEW Drive items

Drive access mirrors the remote connector's tiers: read everything, create-only
(never overwrite or delete). Requester identity arrives via env
(NS_REQUESTER_ID / NS_REQUESTER_NAME), set per-turn by employee_bot._ask.
"""
import os
import re
import secrets
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    with open(os.path.join(HERE, ".env")) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
                # The Prism browser token is refreshed in .env out-of-band (via
                # Pedro's owner-only /prismtoken). The inherited service env can
                # hold a STALE one, and setdefault won't replace it, so OVERRIDE
                # just these PRISM keys from the freshest .env on every spawn.
                # SCOPED to PRISM_* only — a blanket override would re-introduce
                # greg's IMAP/SMTP creds that the service deliberately strips.
                if k.strip() in ("PRISM_ACCESS_TOKEN", "PRISM_REFRESH_TOKEN", "PRISM_APP_VERSION"):
                    os.environ[k.strip()] = v.strip()
except FileNotFoundError:
    pass

sys.path.insert(0, HERE)

import employee_email  # noqa: E402
import imap_email  # noqa: E402
import employee_notify  # noqa: E402
import employee_notes  # noqa: E402
import employee_requests  # noqa: E402
import mailer  # noqa: E402
import pending_email  # noqa: E402
from mcp.server.fastmcp import FastMCP, Image  # noqa: E402

mcp = FastMCP("nsrequests")

GDRIVE_BIN = os.path.join(HERE, "gdrive.py")
DRIVE_TOKEN = os.environ.get("EMPLOYEE_GDRIVE_TOKEN", "/data/employees/token.json")
DL_DIR = os.environ.get("EMPLOYEE_DL_DIR", "/data/employees/dl")
# Where employee_bot saves images/screenshots dropped into the chat. view_attachment
# is hard-scoped to this dir so the sandboxed agent can SEE what the employee sent
# without gaining read access to anything else on disk.
INBOX_DIR = os.environ.get("EMPLOYEE_INBOX_DIR", "/data/employees/inbox")
_ALLOWED = {"list", "find", "download", "mkdir", "upload"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_MAX_VIEW_BYTES = 12 * 1024 * 1024  # 12 MB ceiling, well above any Telegram photo


def _uid() -> str:
    return os.environ.get("NS_REQUESTER_ID", "").strip()


def _gdrive(args, timeout=120):
    """Run gdrive.py with the sandbox token. Create-only: never overwrite."""
    if not args or args[0] not in _ALLOWED:
        raise ValueError("drive subcommand not allowed: %s" % args[:1])
    if args[0] == "upload" and "--file-id" in args:  # the only overwrite path
        raise ValueError("overwriting existing Drive files is not allowed here")
    env = {**os.environ, "GCAL_TOKEN": DRIVE_TOKEN}
    try:
        r = subprocess.run(
            [sys.executable, GDRIVE_BIN, *args], cwd=HERE, env=env,
            capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "Drive command timed out."
    out = (r.stdout + r.stderr).decode("utf-8", errors="replace").strip()
    return out or "(no output)"


@mcp.tool()
def submit_request(text: str) -> str:
    """Forward a feature request, idea, or task to Greg (the owner) for approval.

    Use this only for things you genuinely CANNOT do yourself with your other
    tools -- a brand-new capability, money/wire actions, or anything that needs
    Greg's sign-off. `text` is the full request in the employee's words.
    """
    rid = _uid()
    if not rid:
        return "I couldn't identify who's asking. Ask them to use the /request command instead."
    name = os.environ.get("NS_REQUESTER_NAME") or employee_notify.who(rid)
    rec = employee_requests.submit(int(rid), name, text)
    employee_notify.notify_owner_request(rec)
    return (
        "Done -- sent to Greg for approval (request %s). "
        "You'll hear back here when he decides." % rec["id"]
    )


@mcp.tool()
def email_send(to: str, subject: str, body: str, attach_file_ids: str = "") -> str:
    """Send a plain-text email FROM the employee's own Nightshift address.

    `to` is one or more comma-separated addresses. `attach_file_ids` is an
    OPTIONAL comma-separated list of Google Drive file ids (from drive_find /
    drive_list) to download and attach -- use this to email graphics/PDFs etc.
    Just send it; do NOT file a request to Greg. If the employee has no sending
    address yet, this returns guidance to run /setupemail.
    """
    rid = _uid()
    if not rid:
        return "I couldn't identify who's asking. Ask them to run /setupemail in this bot."
    sender = employee_email.sender_for(int(rid))
    if not sender:
        return (
            "You don't have a sending address set up yet. Run /setupemail here in "
            "the Telegram bot (takes ~1 min) and then I can send email for you."
        )
    recipients = [r.strip() for r in to.replace(";", ",").split(",") if r.strip()]
    if not recipients:
        return "I need at least one recipient email address."

    ids = [f.strip() for f in attach_file_ids.replace(";", ",").split(",") if f.strip()]
    workdirs = []
    paths = []
    try:
        for fid in ids:
            work = os.path.join(DL_DIR, "att_" + secrets.token_hex(6))
            os.makedirs(work, exist_ok=True)
            workdirs.append(work)
            raw = os.path.join(work, "raw")
            msg = _gdrive(["download", "--file-id", fid, "--out", raw])
            if not os.path.exists(raw):
                return "Couldn't download attachment %s: %s" % (fid, msg)
            m = re.search(r"name=(.+)$", msg)
            real = (m.group(1).strip() if m else "") or "attachment"
            final = os.path.join(work, os.path.basename(real))
            os.rename(raw, final)
            paths.append(final)
        token = pending_email.stage(int(rid), sender.get("from"), recipients, [],
                                    subject, body, attachments=paths)
    except Exception as e:
        return "Couldn't prepare the email: %s" % e
    finally:
        for w in workdirs:
            shutil.rmtree(w, ignore_errors=True)
    ok = pending_email.send_confirm_prompt(pending_email.load(token))
    if not ok:
        pending_email.discard(token)
        return ("I prepared the email but couldn't reach you on Telegram to confirm it. "
                "Open the NS Team Bot in Telegram (send /start) and try again.")
    extra = (" with %d attachment(s)" % len(paths)) if paths else ""
    return ("Staged for your confirmation%s -- NOT sent. I've sent the exact draft to your "
            "Telegram with a Send / Cancel button; tap Send there to send it. Nothing goes "
            "out until you tap Send. Do not tell the user it was already sent." % extra)


@mcp.tool()
def email_unread(since_hours: int = 24) -> str:
    """Scan the employee's OWN inbox and summarize UNREAD mail from the last
    since_hours hours (default 24, max 168). Read-only: uses BODY.PEEK so nothing
    is marked read. Use this whenever the employee asks you to check, scan, read,
    or go through their email. If their inbox isn't connected, returns guidance to
    run /setupinbox."""
    rid = _uid()
    if not rid:
        return "I couldn't identify who's asking. Ask them to run /setupinbox in this bot."
    creds = employee_email.inbox_for(int(rid))
    if not creds:
        return (
            "Your inbox isn't connected yet. Run /setupinbox here in the Telegram "
            "bot (~1 min, it auto-detects your GreenGeeks server) and then I can "
            "scan your unread mail."
        )
    try:
        hrs = max(1, min(int(since_hours), 168))
    except (TypeError, ValueError):
        hrs = 24
    try:
        emails = imap_email.get_unread_emails(creds, hrs)
    except Exception as e:  # noqa: BLE001
        return "Couldn't read your inbox: %s" % e
    if not emails:
        return "No unread mail in the last %dh." % hrs
    lines = ["%d unread in the last %dh:" % (len(emails), hrs), ""]
    for e in emails:
        lines.append("From: %s\nSubject: %s\nPreview: %s" % (
            e.get('from', ''), e.get('subject', ''), (e.get('body') or '')[:200]))
        lines.append("--")
    return "\n".join(lines)


@mcp.tool()
def remember(note: str) -> str:
    """Save a short, durable note about this employee or how they like things done
    (e.g. 'wants a daily briefing at 8am, point form'). Saved notes are shown to
    you at the start of every future conversation, so use this instead of saying
    you can't keep memory. Keep each note to one or two sentences."""
    rid = _uid()
    if not rid:
        return "I couldn't identify who's asking, so I can't save that."
    saved = employee_notes.append(int(rid), note)
    if not saved:
        return "Nothing to save."
    return "Saved to memory: %s" % saved


@mcp.tool()
def recall() -> str:
    """Return everything you've remembered about this employee so far."""
    rid = _uid()
    if not rid:
        return ""
    notes = employee_notes.read(int(rid))
    return notes or "(no notes saved yet)"


@mcp.tool()
def drive_list(folder_id: str = "") -> str:
    """List Google Drive items. No folder_id (or 'root') lists the top level
    (My Drive root, which includes folders shared in); otherwise pass a folder id
    from a previous listing to look inside it."""
    arg = folder_id.strip()
    folder = "root" if (not arg or arg.lower() in ("root", "shared")) else arg
    return _gdrive(["list", "--folder", folder, "--max", "50"])


@mcp.tool()
def drive_find(query: str) -> str:
    """Search across all of Greg's Drive for files/folders whose name matches."""
    if not query.strip():
        raise ValueError("Provide a name to search for.")
    return _gdrive(["find", "--name", query.strip(), "--max", "50"])


@mcp.tool()
def drive_read_text(file_id: str) -> str:
    """Download a Drive file by id and return its TEXT contents. Binary files
    (images, PDFs) are reported, not returned -- attach them with email_send's
    attach_file_ids instead, or use the Telegram /get command."""
    fid = file_id.strip()
    if not fid:
        raise ValueError("Provide a file id (from drive_list / drive_find).")
    os.makedirs(DL_DIR, exist_ok=True)
    out_path = os.path.join(DL_DIR, "rd_" + secrets.token_hex(8))
    msg = _gdrive(["download", "--file-id", fid, "--out", out_path])
    if not os.path.exists(out_path):
        return msg or "Download failed."
    try:
        data = open(out_path, "rb").read()
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass
    try:
        return data.decode("utf-8")[:100_000]
    except UnicodeDecodeError:
        return ("That file is binary (%d bytes) -- attach it with "
                "email_send(attach_file_ids=...) or use /get %s in Telegram."
                % (len(data), fid))


@mcp.tool()
def drive_make_folder(name: str, parent_id: str = "") -> str:
    """Create a NEW folder in Drive. parent_id optional (omit for My Drive root)."""
    if not name.strip():
        raise ValueError("Give the new folder a name.")
    args = ["mkdir", "--name", name.strip()]
    if parent_id.strip():
        args += ["--parent", parent_id.strip()]
    return _gdrive(args)


@mcp.tool()
def drive_create_text_file(name: str, content: str, parent_id: str = "") -> str:
    """Create a NEW text file in Drive with the given contents (create-only,
    never overwrites)."""
    if not name.strip():
        raise ValueError("Give the file a name.")
    os.makedirs(DL_DIR, exist_ok=True)
    local = os.path.join(DL_DIR, "mk_" + secrets.token_hex(8))
    with open(local, "w", encoding="utf-8") as fh:
        fh.write(content)
    try:
        args = ["upload", "--file", local, "--name", name.strip()]
        if parent_id.strip():
            args += ["--parent", parent_id.strip()]
        out = _gdrive(args)
    finally:
        try:
            os.remove(local)
        except OSError:
            pass
    return "Created new file:\n%s" % out


@mcp.tool()
def draft_blast(city: str, subject: str, html: str, image_drive_ids: str = "") -> str:
    """Prepare a marketing EMAIL BLAST to a city segment and QUEUE it for Greg's
    approval. Use this whenever an employee asks you to create or send an email
    blast / eblast to customers (e.g. "blast Edmonton about DJ Mina this weekend").

    YOU write the full HTML email body and pass it as `html` -- follow the
    Nightshift style: a bold headline, a short hype intro that greets {first}, the
    event details, and a clear GET TICKETS button linking to the ticket URL.
    Reference each image as src="cid:NAME".

    Args:
      city: segment to target, e.g. "Edmonton" (matches the contact list's City).
      subject: subject line (you may personalize with {first}).
      html: the complete HTML email body you wrote.
      image_drive_ids: OPTIONAL comma-separated NAME=DRIVE_FILE_ID pairs for images
        referenced as cid:NAME (e.g. "hero=1F332...,ev1=1Ygh..."). Each is uploaded
        to S3 and the cid:NAME link is swapped for the hosted URL.

    Does NOT send to the list -- it uploads images, renders the email, QUEUES it,
    emails Greg a PREVIEW, and pings him to approve. Only Greg fires the real send."""
    rid = _uid()
    if not rid:
        return "I couldn't identify who's asking, so I can't draft that."
    name = os.environ.get("NS_REQUESTER_NAME") or employee_notify.who(rid)
    try:
        import blast_compose
        return blast_compose.draft(rid=rid, requester=name, city=city,
                                   subject=subject, html=html,
                                   image_drive_ids=image_drive_ids,
                                   gdrive=_gdrive, dl_dir=DL_DIR)
    except Exception as e:
        return "Couldn't draft the blast: %s" % e


@mcp.tool()
def view_attachment(path: str) -> Image:
    """Look at an image or screenshot the employee just sent you in the chat.

    When an employee drops a photo/screenshot, the chat tells you the exact disk
    `path` it was saved to. Pass that path here to actually SEE the image, then
    respond — diagnose the problem they're showing you, read text off the screen,
    describe what's in the picture, etc. This is how you look at images, exactly
    like Greg's own assistant does. Only works on files the employee just sent
    (the inbox dir); it cannot open anything else on disk.
    """
    real = os.path.realpath(path)
    root = os.path.realpath(INBOX_DIR)
    if real != root and not real.startswith(root + os.sep):
        raise ValueError(
            "I can only view an image the employee just sent me in this chat."
        )
    if not os.path.isfile(real):
        raise ValueError("That image is no longer on disk — ask them to resend it.")
    ext = os.path.splitext(real)[1].lower()
    if ext not in _IMAGE_EXTS:
        raise ValueError(
            "That attachment isn't an image I can view (%s). I can only look at "
            "photos/screenshots." % (ext or "no extension")
        )
    if os.path.getsize(real) > _MAX_VIEW_BYTES:
        raise ValueError("That image is too large for me to open.")
    return Image(path=real)


@mcp.tool()
def blast_stats(query: str = "") -> str:
    """Report how an email blast is performing: delivered, total clicks, unique
    clickers, click-through rate, and a per-link breakdown. Use this whenever an
    employee asks how a blast/eblast is doing (e.g. "how's the DJ Mina blast?").

    `query` matches a campaign by its id, city, or event/subject words (e.g.
    "mina", "edmonton mina"). Leave blank to list the available blasts.
    """
    import glob
    import json
    click_dir = os.path.join(HERE, "blast-clicks")
    ledger_dir = os.path.join(HERE, "blast-ledger")
    queue_dir = "/data/greg/blast_queue"

    def _load(path):
        out = []
        if os.path.exists(path):
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
        return out

    # Real campaigns = ledger files, minus the throwaway preview campaigns.
    camps = set()
    for p in glob.glob(os.path.join(ledger_dir, "*.jsonl")):
        cid = os.path.basename(p)[:-6]
        if any(x in cid.lower() for x in ("preview", "test", "sandbox", "firetest")):
            continue
        camps.add(cid)
    labels = {}
    for p in glob.glob(os.path.join(queue_dir, "*.json")):
        try:
            s = json.load(open(p))
        except Exception:
            continue
        cid = s.get("id") or os.path.basename(p)[:-5]
        labels[cid] = (str(s.get("subject", "")) + " " + str(s.get("city", ""))).lower()

    if not camps:
        return "No blasts have gone out yet."

    q = query.strip().lower()
    if not q:
        return "Which blast? Here are the campaigns:\n" + "\n".join(
            "• %s%s" % (c, (" — " + labels[c]) if labels.get(c) else "") for c in sorted(camps))

    matches = [c for c in camps if q in c.lower() or q in labels.get(c, "")]
    if not matches:
        toks = q.split()
        matches = [c for c in camps if all(t in (c.lower() + " " + labels.get(c, "")) for t in toks)]
    if not matches:
        return "I couldn't find a blast matching '%s'. Campaigns:\n%s" % (
            query, "\n".join("• " + c for c in sorted(camps)))
    if len(matches) > 1:
        return "Several blasts match '%s' — which one?\n%s" % (
            query, "\n".join("• " + c for c in sorted(matches)))

    cid = matches[0]
    clicks = _load(os.path.join(click_dir, cid + ".jsonl"))
    uniq = {c.get("email", "").lower() for c in clicks if c.get("email")}
    delivered = {r["recipient"].lower() for r in _load(os.path.join(ledger_dir, cid + ".jsonl"))
                 if r.get("ok") and r.get("channel") == "email" and r.get("recipient")}
    d = len(delivered)
    by_url = {}
    for c in clicks:
        by_url[c.get("url", "?")] = by_url.get(c.get("url", "?"), 0) + 1

    out = ["\U0001F4CA Blast: %s" % cid, "Delivered: %s" % f"{d:,}",
           "Total clicks: %s" % f"{len(clicks):,}",
           "Unique clickers: %s%s" % (f"{len(uniq):,}",
                                      f" ({100 * len(uniq) / d:.1f}% click-through)" if d else "")]
    if by_url:
        out.append("By link:")
        for url, n in sorted(by_url.items(), key=lambda x: -x[1])[:5]:
            out.append("  %s → %s" % (f"{n:,}", url))
    if not clicks:
        out.append("(No clicks recorded — either nobody's clicked yet, or this blast "
                   "went out before click tracking was turned on.)")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Prism (READ-ONLY) — let the chat agent answer venue availability / show
# questions instead of punting to Greg. No writes: never creates/changes a
# booking. Token cache is redirected to the employee-writable dir because
# prism.py's default (/data/greg/.prism_token.json) isn't writable under this
# service's sandbox (ProtectSystem=strict; only /data/employees is RW here).
# Tools are async def so FastMCP awaits them in its own loop (no asyncio.run).
# ---------------------------------------------------------------------------
os.environ.setdefault("PRISM_TOKEN_CACHE", "/data/employees/.prism_token.json")
import datetime as _dt  # noqa: E402
import httpx  # noqa: E402
import prism  # noqa: E402

_WEEKDAY_ALIASES = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "weds": 2,
    "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4,
    "friday": 4, "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}
_WD_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _ord(n) -> str:
    """1 -> '1st', 2 -> '2nd', 11 -> '11th', etc. (for hold positions)."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return "%d%s" % (n, suf)


@mcp.tool()
async def prism_list_shows(start: str, end: str) -> str:
    """List Prism shows and holds between two dates (YYYY-MM-DD inclusive) — i.e.
    what's booked across the venues. READ-ONLY. Use when an employee asks what's
    on, what's booked, or wants to see the calendar for a stretch of dates."""
    if not prism.configured():
        return "Prism isn't connected yet (no refresh token on the server) — tell Greg."
    try:
        _dt.date.fromisoformat(start)
        _dt.date.fromisoformat(end)
    except ValueError:
        return "Dates must be YYYY-MM-DD, e.g. 2026-11-01."
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            shows = await prism.list_shows(client, start, end)
    except Exception as e:  # noqa: BLE001
        return "Prism lookup failed: %s" % e
    return prism.format_shows(shows, limit=60)


@mcp.tool()
async def prism_check_availability(start: str, end: str, weekdays: str = "", venue: str = "") -> str:
    """Check which dates are OPEN, HELD, or BOOKED in Prism between start and end
    (YYYY-MM-DD inclusive). READ-ONLY. Use for 'what dates are available/open' and
    hold-position questions. Factors in BOTH confirmed shows AND holds — a date
    with no confirmed show can still carry a 1st/2nd/… hold, so OPEN here means no
    confirmed show AND no live hold. For HELD dates it shows the hold positions and
    the next available position.

    `weekdays`: optional comma list to narrow days, e.g. 'Fri,Sat' for
    Friday/Saturday avails. `venue`: optional venue-name filter (substring, e.g.
    'Union Hall' or 'Pawn Shop') — pass it whenever the employee asks about a
    specific room, because a date can be open at one venue and held/booked at
    another."""
    if not prism.configured():
        return "Prism isn't connected yet (no refresh token on the server) — tell Greg."
    try:
        d0 = _dt.date.fromisoformat(start)
        d1 = _dt.date.fromisoformat(end)
    except ValueError:
        return "Dates must be YYYY-MM-DD, e.g. 2026-11-01 and 2026-11-30."
    if d1 < d0:
        return "End date is before start date."
    if (d1 - d0).days > 366:
        return "Range too wide — keep it within ~12 months."

    wd_filter = set()
    for w in str(weekdays).replace(";", ",").split(","):
        w = w.strip().lower()
        if not w:
            continue
        if w not in _WEEKDAY_ALIASES:
            return "Unknown weekday %r — use names like Fri, Sat, Monday." % w
        wd_filter.add(_WEEKDAY_ALIASES[w])
    ven = venue.strip().lower()

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            shows = await prism.list_shows(client, start, end)
            holds = await prism.list_holds(client, start, end)
    except Exception as e:  # noqa: BLE001
        return "Prism lookup failed: %s" % e

    by_date: dict[str, list] = {}
    holds_by_date: dict[str, list] = {}
    venues_seen = set()
    for s in shows:
        day = s.get("start")
        if not day:
            continue
        day = str(day)[:10]
        vname = s.get("venue") or ""
        if vname:
            venues_seen.add(vname)
        if ven and ven not in vname.lower():
            continue
        by_date.setdefault(day, []).append(s)
    for h in holds:
        if h.get("cleared"):
            continue
        day = h.get("date")
        if not day:
            continue
        vname = h.get("venue") or ""
        if vname:
            venues_seen.add(vname)
        if ven and ven not in vname.lower():
            continue
        holds_by_date.setdefault(day, []).append(h)

    header = "Prism availability %s → %s" % (start, end)
    if venue:
        header += " | venue~'%s'" % venue
    if wd_filter:
        header += " | " + ",".join(_WD_NAMES[i] for i in sorted(wd_filter))
    lines = [header, ""]

    cur, one, shown, capped = d0, _dt.timedelta(days=1), 0, False
    while cur <= d1:
        if not wd_filter or cur.weekday() in wd_filter:
            if shown >= 80:
                capped = True
                break
            iso = cur.isoformat()
            label = "%s %s" % (_WD_NAMES[cur.weekday()], iso)
            booked = by_date.get(iso, [])
            held = sorted(holds_by_date.get(iso, []), key=lambda h: h.get("level") or 0)
            if booked:
                tags = ", ".join(
                    "%s%s [%s]" % (
                        b["title"],
                        (" @ %s" % b["venue"]) if b.get("venue") else "",
                        b["status_label"],
                    )
                    for b in booked
                )
                extra = "  (+%d hold%s)" % (len(held), "" if len(held) == 1 else "s") if held else ""
                lines.append("• %s — BOOKED: %s%s" % (label, tags, extra))
            elif held:
                nextpos = max((h.get("level") or 0) for h in held) + 1
                hs = ", ".join(
                    "%s%s (%s)" % (
                        h["artist"],
                        (" @ %s" % h["venue"]) if h.get("venue") else "",
                        _ord(h.get("level")),
                    )
                    for h in held
                )
                lines.append("• %s — HELD: %s → next open position %s" % (label, hs, _ord(nextpos)))
            else:
                lines.append("• %s — OPEN" % label)
            shown += 1
        cur += one

    if capped:
        lines.append("…(stopped at 80 dates — narrow the range or use weekdays=)")
    if not venue and venues_seen:
        lines.append("")
        lines.append("Note: no venue filter — OPEN means nothing confirmed or held at ANY venue. "
                     "Venues with activity in range: %s. Re-run with venue= for a specific room."
                     % ", ".join(sorted(venues_seen)))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Meta ads — let the chat agent draft a campaign conversationally (no slash
# commands), on ANY configured ad account (@nightshift, @pawnshop, ...). Spend
# is gated by a Telegram Launch button (pending_campaign -> on_campaign_button).
# Mirrors the email_send stage->confirm pattern: the agent can surface a paused
# draft to approve, but can never start spend itself.
# ---------------------------------------------------------------------------
import asyncio  # noqa: E402
import httpx  # noqa: E402
import meta_ads  # noqa: E402
import pending_campaign  # noqa: E402

_AD_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _ad_account(account: str):
    """Resolve an account selector ('@pawnshop', 'pawnshop', '' -> default) to an
    AdProfile. Raises meta_ads.MetaError (with the known-accounts list) if unknown."""
    return meta_ads.get_profile(account or None)


def _is_video_file(path: str) -> bool:
    """Detect MP4/M4V/QuickTime by the ISO base media file format ftyp box."""
    try:
        with open(path, "rb") as f:
            header = f.read(12)
        return len(header) >= 8 and header[4:8] == b"ftyp"
    except OSError:
        return False


async def _resolve_creative_image(client, image: str, acct):
    """Return (image_hash, image_url, video_id, label) for the ad creative media.

    Accepts http URL, ad-media filename, chat-inbox image, or Google Drive file id
    (including mp4 video files). (None, None, None, None) when no image is given.
    """
    image = (image or "").strip()
    if not image:
        return None, None, None, None
    if image.startswith("http"):
        return None, image, None, image
    _, ext = os.path.splitext(image)
    if ext.lower() in _AD_IMG_EXTS:
        path = None
        try:
            path = meta_ads.resolve_media_path(image)
        except Exception:
            cand = os.path.join(INBOX_DIR, os.path.basename(image))
            if os.path.exists(cand):
                path = cand
        if not path:
            raise ValueError("image file not found in ad media or inbox: %s" % image)
        h = await meta_ads.upload_ad_image(client, path, acct=acct)
        return h, None, None, os.path.basename(path)
    work = os.path.join(DL_DIR, "adimg_" + secrets.token_hex(6))
    os.makedirs(work, exist_ok=True)
    try:
        raw = os.path.join(work, "raw")
        msg = _gdrive(["download", "--file-id", image, "--out", raw])
        if not os.path.exists(raw):
            raise ValueError("couldn't download image from Drive id %s: %s" % (image, msg))
        if _is_video_file(raw):
            vid = await meta_ads.upload_ad_video(client, raw, acct=acct)
            return None, None, vid, "Drive:" + image
        h = await meta_ads.upload_ad_image(client, raw, acct=acct)
        return h, None, None, "Drive:" + image
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def _build_campaign(acct, name, daily_cad, interest_ids, objective, ticket_link, caption, image):
    daily_cents = int(round(daily_cad * 100))
    targeting = meta_ads.build_targeting(interest_ids, countries=acct.default_countries)
    async with httpx.AsyncClient(timeout=300.0) as client:
        camp = await meta_ads.create_campaign(client, name, objective=objective, acct=acct)
        campaign_id = camp.get("id")
        if not campaign_id:
            raise ValueError("Meta returned no campaign id: %s" % camp)
        adset = await meta_ads.create_adset(
            client, campaign_id, f"{name} — ad set", daily_cents, targeting, acct=acct
        )
        adset_id = adset.get("id")
        creative_id = ad_id = image_label = creative_error = None
        if ticket_link and caption and adset_id:
            try:
                image_hash, image_url, video_id, image_label = await _resolve_creative_image(client, image, acct)
                if video_id:
                    creative = await meta_ads.create_adcreative_video(
                        client, f"{name} — creative", ticket_link, caption,
                        video_id=video_id, acct=acct,
                    )
                else:
                    creative = await meta_ads.create_adcreative(
                        client, f"{name} — creative", ticket_link, caption,
                        image_hash=image_hash, image_url=image_url, acct=acct,
                    )
                creative_id = creative.get("id")
                if creative_id:
                    ad = await meta_ads.create_ad(client, adset_id, f"{name} — ad", creative_id, acct=acct)
                    ad_id = ad.get("id")
            except Exception as ce:  # noqa: BLE001
                creative_error = str(ce)
        try:
            est = await meta_ads.reach_estimate(client, targeting, acct=acct)
        except Exception:  # noqa: BLE001
            est = None
    return {
        "campaign_id": campaign_id, "objective": objective, "creative_id": creative_id,
        "ad_id": ad_id, "image_label": image_label, "creative_error": creative_error, "est": est,
    }


def _reach_line(est) -> str:
    if not isinstance(est, dict):
        return ""
    users = est.get("users") or est.get("estimate_mau") or est.get("estimate_dau")
    if isinstance(users, (int, float)):
        return "Est. audience: ~{:,}".format(int(users))
    return ""


@mcp.tool()
def list_ad_accounts() -> str:
    """List the Meta ad accounts you can build campaigns on, with readiness. Pass
    an account's @key (e.g. @nightshift, @pawnshop) as the `account` argument to
    research_audience / draft_ad_campaign. Call this when an employee asks which
    accounts exist, which to use, or names a brand/venue you should match."""
    profs = meta_ads.list_profiles()
    if not profs:
        return "No Meta ad accounts are configured. Tell Greg."
    lines = ["Ad accounts you can use (pass the @key as `account`):"]
    for p in profs:
        lines.append("• " + p.status_line())
    lines.append("")
    lines.append("Default is @%s. Only ✅ ready accounts can build campaigns." % meta_ads.DEFAULT_PROFILE_KEY)
    return "\n".join(lines)


@mcp.tool()
def list_ad_media() -> str:
    """List image filenames available in the server's ad-media folder, for use as
    the `image` argument to draft_ad_campaign."""
    files = meta_ads.list_media()
    if not files:
        return ("No ad-media files on the server. Use an http image URL, a Google Drive "
                "file id, or have the employee drop the image in this chat instead.")
    return "Ad media available:\n" + "\n".join("• " + f for f in files)


@mcp.tool()
async def research_audience(artist: str, genre: str = "", similar: str = "", account: str = "") -> str:
    """Find Meta targeting interest ids for an artist (plus optional genre and
    comma-separated similar artists). Read-only. The returned interest ids feed
    straight into draft_ad_campaign's `interest_ids`. `account` picks which ad
    account context to use (default @nightshift; see list_ad_accounts)."""
    try:
        acct = _ad_account(account)
    except meta_ads.MetaError as e:
        return str(e)
    if not meta_ads.configured(acct):
        return "Ad account @%s isn't configured yet (no token) — tell Greg." % acct.key
    sims = [s.strip() for s in str(similar).replace(";", ",").split(",") if s.strip()]

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            res = await meta_ads.research_artist_targeting(
                client, artist, genre=genre or None, similar_artists=sims or None, acct=acct
            )
    except Exception as e:  # noqa: BLE001
        return "Research failed: %s" % e
    ids = res.get("all_ids", [])
    summary = res.get("summary", "") or ""
    return "%s\n\nInterest ids (pass to draft_ad_campaign): %s" % (summary, ",".join(ids))


@mcp.tool()
async def draft_ad_campaign(name: str, daily_cad: float, interest_ids: str = "",
                            objective: str = "OUTCOME_TRAFFIC", ticket_link: str = "",
                            caption: str = "", image: str = "", account: str = "") -> str:
    """Build a Meta (Facebook/Instagram) ad campaign — created PAUSED (no spend).

    Use this WHENEVER an employee asks you to make / build / run / launch / boost a
    Facebook or Instagram ad or campaign. You can do this yourself — do NOT say you
    have no Meta access and do NOT punt to Greg. The campaign is always created
    PAUSED; the employee then gets a Launch button in Telegram and spend starts
    ONLY when they tap it, so building a draft is always safe.

    Args:
      - name: short campaign name, e.g. "Ownboss YEG".
      - daily_cad: DAILY budget in the account's currency (dollars, e.g. 7). If the
        employee gives a TOTAL over N days/weeks, divide (total / days) and confirm
        the daily number + run length before calling.
      - interest_ids: OPTIONAL comma-separated Meta interest ids from
        research_audience. Empty = broad targeting in the account's countries.
      - objective: OUTCOME_TRAFFIC (default; ticket link clicks) or OUTCOME_AWARENESS.
      - ticket_link + caption: include BOTH to attach a clickable ad creative.
      - image: OPTIONAL creative image — http URL, Google Drive file id (drive_find),
        an ad-media filename (list_ad_media), or an image the employee dropped here.
      - account: which ad account to use — '@nightshift' (default), '@pawnshop', etc.
        Call list_ad_accounts to see options + readiness. If the employee names a
        venue/brand (e.g. "Pawn Shop"), pick the matching account.
    """
    rid = _uid()
    if not rid:
        return "I couldn't identify who's asking. Ask them to message me in the NS Team Bot."
    try:
        acct = _ad_account(account)
    except meta_ads.MetaError as e:
        return str(e)
    if not (acct.token and acct.ad_account_id):
        return ("Ad account @%s (%s) isn't ready yet — %s. Pick a ready account (call "
                "list_ad_accounts), or tell Greg to finish setting it up."
                % (acct.key, acct.label, acct.status_line()))
    try:
        daily = float(daily_cad)
    except (TypeError, ValueError):
        return "daily_cad must be a number of %s dollars (e.g. 7)." % acct.currency
    if daily <= 0:
        return "daily_cad must be greater than 0."
    if not str(name).strip():
        return "Give the campaign a short name."
    ids = [x.strip() for x in str(interest_ids).replace(";", ",").split(",") if x.strip()]
    try:
        res = await _build_campaign(
            acct, str(name).strip(), daily, ids, (objective or "OUTCOME_TRAFFIC").strip(),
            str(ticket_link).strip(), str(caption).strip(), str(image).strip(),
        )
    except Exception as e:  # noqa: BLE001
        return "Draft failed (nothing was launched, no spend started): %s" % e

    lines = [
        "\U0001F4CB Campaign drafted — PAUSED, not spending:",
        "",
        "Account: %s (@%s)" % (acct.label, acct.key),
        "Name: %s" % str(name).strip(),
        "Campaign id: %s" % res["campaign_id"],
        "Objective: %s" % res["objective"],
        "Daily budget: $%.2f %s" % (daily, acct.currency),
        "Interests: %s" % (", ".join(ids) or "(none — broad)"),
        "Geo: %s" % ", ".join(acct.default_countries),
    ]
    if res["creative_id"]:
        lines.append("Creative id: %s" % res["creative_id"])
        lines.append("Ticket link: %s" % str(ticket_link).strip())
        if res["image_label"]:
            lines.append("Image: %s" % res["image_label"])
    elif res["creative_error"]:
        lines.append("Creative: FAILED — %s (campaign + ad set still created)" % res["creative_error"])
    elif not (str(ticket_link).strip() and str(caption).strip()):
        lines.append("Creative: none (give a ticket link + caption to attach a clickable ad)")
    rl = _reach_line(res["est"])
    if rl:
        lines.append(rl)
    summary = "\n".join(lines)

    token = pending_campaign.stage(
        int(rid), res["campaign_id"], str(name).strip(), daily, summary, acct_key=acct.key
    )
    ok = pending_campaign.send_confirm_prompt(pending_campaign.load(token))
    if not ok:
        return ("Campaign %s is built and PAUSED on @%s, but I couldn't reach you on Telegram with "
                "the Launch button. Open the NS Team Bot (send /start) and ask me to try again, or "
                "launch it from Ads Manager." % (res["campaign_id"], acct.key))
    extra = ""
    if res.get("creative_error"):
        extra = (" The campaign + ad set were created, but the ad creative failed (%s) — "
                 "mention that to the employee." % res["creative_error"])
    return ("Built and staged on @%s — the campaign is PAUSED (no spend). I've sent the summary to "
            "your Telegram with a Launch button; spend only starts when you tap Launch. Do NOT tell "
            "the user it is already live." % acct.key + extra)



# ---------------------------------------------------------------------------
# Showpass (ticketing) — read-only lookups against the public Discovery API.
# Both brands (Nightshift + Pawn Shop) are one Showpass org (ID 41). These use
# no credentials, so they are safe for employees; anything write-shaped
# (discounts, tracking links) stays Greg/Pedro-side in showpass.py.
# ---------------------------------------------------------------------------
import showpass  # noqa: E402


@mcp.tool()
async def showpass_events(query: str = "", days: int = 0) -> str:
    """List our upcoming Showpass events (Nightshift + Pawn Shop ticketing) with
    on-sale links. READ-ONLY. Use when an employee asks what's on sale, wants a
    ticket link, or needs an event's Showpass slug. `query` filters by event or
    venue name; `days` (optional) caps how far ahead to look."""
    try:
        evs = await asyncio.to_thread(
            showpass.list_events, query, days if days > 0 else None
        )
    except Exception as e:  # noqa: BLE001
        return "Showpass lookup failed: %s" % e
    return showpass.format_events(evs)


@mcp.tool()
async def showpass_event(slug: str) -> str:
    """Full public detail for one Showpass event: dates, venue, ticket types,
    prices, sold-out status. READ-ONLY. `slug` is the part after showpass.com/
    in the event URL (get it from showpass_events)."""
    try:
        ev = await asyncio.to_thread(showpass.get_event, slug)
    except Exception as e:  # noqa: BLE001
        return "Showpass lookup failed: %s" % e
    return showpass.format_event_detail(ev)


# Server entry point — MUST stay at end of file so every @mcp.tool() above is
# registered before the server starts (mcp.run() blocks). Do not move it up.
if __name__ == "__main__":
    mcp.run()
