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


if __name__ == "__main__":
    mcp.run()
