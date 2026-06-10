#!/usr/bin/env python3
"""Mass broadcaster for Nightshift — email + WhatsApp from one CSV list.

Sends a personalized message to every row of a CSV over email (own SMTP,
free) and/or WhatsApp (Twilio). Built for promo blasts to an existing
opted-in contact list — NOT cold outreach.

Key properties:
  * One recipient per email (never exposes the list in To/Cc).
  * Rate-limited so you don't trip spam filters / Twilio limits.
  * Opt-out aware (skips anyone in the opt-out files; adds unsubscribe
    header + footer to email).
  * Resumable: every successful send is logged to a per-campaign ledger,
    so re-running after a crash never double-sends.
  * Confirm-first: previews and refuses to send unless you pass --yes.

Email is hard-pinned to send as greg@nightshiftent.ca (same as email_send.py).

Usage:
  # Preview (no send) — always do this first:
  blast.py --list fans.csv --channel email \
      --subject "AOKI is coming" --body-file body.txt --campaign aoki

  # Actually send:
  blast.py --list fans.csv --channel email \
      --subject "AOKI is coming" --body-file body.txt --campaign aoki --yes

  # WhatsApp blast (only reaches numbers that joined your Twilio sandbox):
  blast.py --list fans.csv --channel whatsapp \
      --wa-body-file wa.txt --campaign aoki --yes

  # Both channels at once:
  blast.py --list fans.csv --channel both \
      --subject S --body-file body.txt --wa-body-file wa.txt --campaign aoki --yes

CSV format: a header row. Recognized columns: `email`, `whatsapp` (or `phone`),
`name`. Any column can be used in templates as {column}. Missing values render
as blank. WhatsApp numbers must be E.164 (e.g. +12045551234).

Templates: plain text files. Use {name}, {email}, or any CSV column as a
placeholder. Example body.txt:
    Hey {name}, tickets for AOKI just dropped — grab yours: https://...
"""
from __future__ import annotations

import argparse
import base64
import csv
import html as _htmlmod
import imaplib
import json
import mimetypes
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

ENV_PATH = os.path.expanduser("~/nightshift/.env")
LEDGER_DIR = os.path.expanduser("~/nightshift/blast-ledger")
OPTOUT_EMAIL = os.path.expanduser("~/nightshift/blast-optout-email.txt")
OPTOUT_WA = os.path.expanduser("~/nightshift/blast-optout-wa.txt")
SENDERS_PATH = os.path.expanduser("~/nightshift/blast-senders.json")
DEFAULT_FROM = "greg@nightshiftent.ca"

# Shared signed-token + opt-out helpers (used for per-recipient unsubscribe
# links). Lives in ~/nightshift; degrade gracefully if it can't be imported.
sys.path.insert(0, os.path.expanduser("~/nightshift"))
try:
    import unsub_common as _unsub
except Exception:
    _unsub = None


def _load_env(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _die(msg: str, **extra) -> None:
    print(json.dumps({"ok": False, "error": msg, **extra}), file=sys.stderr)
    sys.exit(1)


class _Safe(dict):
    """format_map helper: unknown {placeholders} render as empty string."""

    def __missing__(self, key):  # noqa: D401
        return ""


def _render(template: str, row: dict) -> str:
    """Brace-safe personalization: replace only {known_column} tokens, leaving any
    other braces (e.g. CSS in an HTML template) untouched. Used for HTML bodies
    where format_map would choke on `{` in stylesheets."""
    out = template
    for k, v in row.items():
        out = out.replace("{" + k + "}", v or "")
    return out


def _html_to_text(html: str) -> str:
    """Crude HTML -> plain-text fallback for the multipart text/plain part."""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
    text = re.sub(r"(?i)<\s*br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</\s*(p|div|tr|h[1-6])\s*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = _htmlmod.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _load_optout(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    out = set()
    with open(path) as f:
        for line in f:
            v = line.strip().lower()
            if v and not v.startswith("#"):
                out.add(v)
    return out


def _load_ledger(campaign: str) -> set[str]:
    """Return set of '<channel>:<recipient>' already sent for this campaign."""
    path = os.path.join(LEDGER_DIR, f"{campaign}.jsonl")
    done: set[str] = set()
    if not os.path.exists(path):
        return done
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("ok"):
                done.add(f"{rec['channel']}:{rec['recipient'].lower()}")
    return done


def _ledger_append(campaign: str, rec: dict) -> None:
    os.makedirs(LEDGER_DIR, exist_ok=True)
    path = os.path.join(LEDGER_DIR, f"{campaign}.jsonl")
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _read_rows(list_path: str) -> list[dict]:
    with open(list_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = []
        for raw in reader:
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            # normalize phone -> whatsapp
            if "whatsapp" not in row and "phone" in row:
                row["whatsapp"] = row["phone"]
            rows.append(row)
    return rows


def _norm_wa(num: str) -> str | None:
    n = num.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not n:
        return None
    if not n.startswith("+"):
        return None  # require E.164 — we can't guess the country code
    return n


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _resolve_sender(from_addr: str) -> dict:
    """Map a From address to the SMTP account that can legitimately send it.

    Order: (1) an explicit per-address/per-domain profile in blast-senders.json,
    (2) the default SMTP_* account in .env — but ONLY for addresses on that
    account's own domain (same-domain so SPF/DKIM/DMARC stay aligned). Any other
    domain is refused, because sending it through the wrong server fails DMARC
    and gets quarantined. Drop that domain's real SMTP creds into
    blast-senders.json to enable it.
    """
    from_addr = from_addr.strip()
    domain = from_addr.rsplit("@", 1)[-1].lower()

    if os.path.exists(SENDERS_PATH):
        try:
            cfg = json.load(open(SENDERS_PATH))
        except Exception as e:
            _die(f"blast-senders.json is invalid JSON: {e}")
        prof = cfg.get(from_addr) or cfg.get(domain)
        if prof:
            return {
                "host": prof["smtp_host"],
                "port": int(prof.get("smtp_port", 465)),
                "user": prof["smtp_user"],
                "password": prof["smtp_pass"],
                "from_addr": from_addr,
                "from_name": prof.get("from_name"),
                "save_sent": False,
            }

    default_user = os.environ.get("SMTP_USER") or os.environ.get("EMAIL_USER") or ""
    default_domain = default_user.rsplit("@", 1)[-1].lower()
    if default_user and domain == default_domain:
        return {
            "host": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
            "port": int(os.environ.get("SMTP_PORT", "465")),
            "user": default_user,
            "password": os.environ.get("SMTP_PASS") or os.environ.get("EMAIL_PASS"),
            "from_addr": from_addr,
            "from_name": None,
            # only the default mailbox is IMAP-backed (Greg's Gmail Sent)
            "save_sent": from_addr.lower() == default_user.lower(),
        }

    _die(
        f"No SMTP credentials for '{from_addr}'. Sending it through the "
        f"{default_domain or 'default'} server would fail SPF/DKIM/DMARC and be "
        f"quarantined as spam. Add a profile for '{domain}' to {SENDERS_PATH} "
        f"with that domain's own smtp_host/smtp_user/smtp_pass."
    )


def _smtp_connect(prof: dict):
    if not prof["user"] or not prof["password"]:
        _die(f"SMTP user/pass missing for sender {prof['from_addr']}")
    ctx = ssl.create_default_context()
    if prof["port"] == 465:
        s = smtplib.SMTP_SSL(prof["host"], prof["port"], context=ctx, timeout=30)
    else:
        s = smtplib.SMTP(prof["host"], prof["port"], timeout=30)
        s.starttls(context=ctx)
    s.login(prof["user"], prof["password"])
    return s


def _inject_html_footer(html: str, unsub_line: str) -> str:
    footer = (
        '<div style="margin-top:28px;padding-top:16px;border-top:1px solid #e2e2e2;'
        'font-size:12px;line-height:1.5;color:#999;font-family:Arial,Helvetica,sans-serif;'
        f'text-align:center;">{unsub_line}</div>'
    )
    low = html.lower()
    if "</body>" in low:
        idx = low.rfind("</body>")
        return html[:idx] + footer + html[idx:]
    return html + footer


def _wrap_tracking_links(html: str, email: str, campaign: str) -> str:
    """Rewrite content links to pass through the /c click-tracking redirect.
    Skips mailto/tel/anchor links and our own unsubscribe/click endpoints."""
    if _unsub is None:
        return html
    base = getattr(_unsub, "BASE_URL", "")
    def _repl(m):
        q, url = m.group(1), m.group(2)
        if not url.lower().startswith(("http://", "https://")):
            return m.group(0)
        if base and url.startswith(base):  # don't double-wrap our own links
            return m.group(0)
        try:
            turl = _unsub.click_url(email, campaign, url)
        except Exception:
            return m.group(0)
        return f"href={q}{turl}{q}"
    return re.sub(r'href=(["\'])(.*?)\1', _repl, html, flags=re.IGNORECASE)


def _build_email(to_addr: str, subject: str, body: str, from_addr: str,
                 from_name: str, footer: bool, reply_to: str = "",
                 html: str = "", inline_images: list | None = None,
                 campaign: str = "", track: bool = False) -> EmailMessage:
    domain = from_addr.rsplit("@", 1)[-1]
    msg = EmailMessage()
    msg["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    msg["To"] = to_addr
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=domain)

    # Per-recipient one-click unsubscribe: a signed link to the /u endpoint that
    # adds this address to blast-optout-email.txt. Falls back to a mailto if the
    # helper is unavailable. The HTTPS link is what makes List-Unsubscribe-Post
    # (RFC 8058) valid, so Gmail/Yahoo's native Unsubscribe button works.
    unsub_link = ""
    if _unsub is not None:
        try:
            unsub_link = _unsub.unsub_url(to_addr)
        except Exception:
            unsub_link = ""
    mailto = f"mailto:{reply_to or from_addr}?subject=unsubscribe"
    if unsub_link:
        msg["List-Unsubscribe"] = f"<{unsub_link}>, <{mailto}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    else:
        msg["List-Unsubscribe"] = f"<{mailto}>"

    intro = "You're receiving this because you signed up with Nightshift Entertainment."
    if unsub_link:
        text_unsub_line = f"{intro}\nUnsubscribe: {unsub_link}"
        html_unsub_line = (
            f"{intro}<br>"
            f'<a href="{unsub_link}" style="color:#999;text-decoration:underline;">'
            "Unsubscribe</a>"
        )
    else:
        text_unsub_line = f"{intro}\nTo stop, just reply to this email and we'll remove you."
        html_unsub_line = f"{intro}<br>To stop, just reply to this email and we'll remove you."

    if footer:
        body = body.rstrip() + f"\n\n—\n{text_unsub_line}"
    msg.set_content(body)  # text/plain part (also the fallback for HTML mail)

    if html:
        if track and campaign and _unsub is not None:
            html = _wrap_tracking_links(html, to_addr, campaign)
        if footer:
            html = _inject_html_footer(html, html_unsub_line)
        msg.add_alternative(html, subtype="html")
        if inline_images:
            html_part = msg.get_payload()[-1]  # the text/html alternative
            for cid, maintype, subtype, data in inline_images:
                html_part.add_related(data, maintype=maintype, subtype=subtype,
                                      cid=f"<{cid}>")
    return msg


def _save_to_sent(msg: EmailMessage) -> None:
    imap_host = os.environ.get("IMAP_HOST", "imap.gmail.com")
    imap_port = int(os.environ.get("IMAP_PORT", "993"))
    imap_user = os.environ.get("IMAP_USER")
    imap_pass = os.environ.get("IMAP_PASS")
    if not (imap_user and imap_pass):
        return
    try:
        from email.generator import BytesGenerator
        from io import BytesIO
        with imaplib.IMAP4_SSL(imap_host, imap_port) as imap:
            imap.login(imap_user, imap_pass)
            buf = BytesIO()
            BytesGenerator(buf, mangle_from_=False).flatten(msg)
            raw = buf.getvalue().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            imap.append('"[Gmail]/Sent Mail"', "\\Seen", imaplib.Time2Internaldate(time.time()), raw)
    except Exception:
        pass  # non-fatal


# ---------------------------------------------------------------------------
# WhatsApp (Twilio REST, stdlib only)
# ---------------------------------------------------------------------------

def _wa_send(to_e164: str, body: str) -> tuple[bool, str]:
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    wa_from = os.environ.get("TWILIO_WHATSAPP_FROM", "")
    if not (sid and token and wa_from):
        return False, "TWILIO_ACCOUNT_SID/AUTH_TOKEN/WHATSAPP_FROM not set"
    if not wa_from.startswith("whatsapp:"):
        wa_from = "whatsapp:" + wa_from
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urllib.parse.urlencode({
        "From": wa_from,
        "To": f"whatsapp:{to_e164}",
        "Body": body,
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req.add_header("Authorization", "Basic " + auth)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
            return True, payload.get("sid", "")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
            return False, f"{err.get('code')}: {err.get('message')}"
        except Exception:
            return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Mass email + WhatsApp broadcaster")
    p.add_argument("--list", required=True, help="CSV contact list")
    p.add_argument("--channel", required=True, choices=["email", "whatsapp", "both"])
    p.add_argument("--campaign", required=True, help="campaign id (for resumable ledger)")
    p.add_argument("--subject", help="email subject (templated)")
    p.add_argument("--body-file", help="plain-text email body template file")
    p.add_argument("--html-file", help="HTML email body template (multipart HTML+text). "
                                       "Personalize with {name}/{col}; reference inline images as src=\"cid:NAME\"")
    p.add_argument("--inline-image", action="append", default=[], metavar="NAME=PATH",
                   help="embed an image by Content-ID for HTML mail; reference as cid:NAME. "
                        "Repeatable. 'NAME=path' or just 'path' (NAME defaults to the filename stem).")
    p.add_argument("--wa-body-file", help="WhatsApp body template file")
    p.add_argument("--city", default="", help="segment filter: only send to rows whose 'city' column matches (case-insensitive)")
    p.add_argument("--from", dest="from_addr", default=DEFAULT_FROM,
                   help=f"email From address (default {DEFAULT_FROM}); must have SMTP creds "
                        f"(same domain as the default account, or a profile in blast-senders.json)")
    p.add_argument("--from-name", default=None,
                   help="display name for From (defaults to sender profile / $EMAIL_FROM_NAME / 'Nightshift Entertainment')")
    p.add_argument("--reply-to", default="",
                   help="Reply-To address (route replies to a real mailbox when From is a no-reply/bulk address)")
    p.add_argument("--rate", type=float, default=30.0, help="max messages/min per channel (default 30)")
    p.add_argument("--limit", type=int, default=0, help="cap number of recipients (0 = all)")
    p.add_argument("--no-footer", action="store_true", help="omit unsubscribe footer on email")
    p.add_argument("--no-track-clicks", action="store_true",
                   help="don't wrap email links with /c click tracking")
    p.add_argument("--no-sent-copy", action="store_true", help="don't save each email to Sent")
    p.add_argument("--yes", action="store_true", help="actually send (omit to preview only)")
    args = p.parse_args()

    _load_env(ENV_PATH)

    want_email = args.channel in ("email", "both")
    want_wa = args.channel in ("whatsapp", "both")

    if want_email and not args.subject:
        _die("email channel needs --subject")
    if want_email and not (args.body_file or args.html_file):
        _die("email channel needs --body-file and/or --html-file")
    if want_wa and not args.wa_body_file:
        _die("whatsapp channel needs --wa-body-file")

    sender = _resolve_sender(args.from_addr) if want_email else None
    if sender:
        from_addr = sender["from_addr"]
        from_name = (args.from_name or sender.get("from_name")
                     or os.environ.get("EMAIL_FROM_NAME") or "Nightshift Entertainment")

    html_tmpl = Path(args.html_file).read_text() if (want_email and args.html_file) else ""
    if want_email and args.body_file:
        email_tmpl = Path(args.body_file).read_text()
    elif html_tmpl:
        email_tmpl = _html_to_text(html_tmpl)  # auto text fallback from HTML
    else:
        email_tmpl = ""
    wa_tmpl = Path(args.wa_body_file).read_text() if want_wa else ""

    # Pre-load inline images once (shared across all recipients)
    inline_images = []  # (cid, maintype, subtype, data)
    for spec in args.inline_image:
        if "=" in spec:
            cid, _, path = spec.partition("=")
        else:
            path, cid = spec, Path(spec).stem
        cid = cid.strip()
        p = Path(path.strip())
        if not p.exists():
            _die(f"Inline image not found: {path}")
        ctype, _ = mimetypes.guess_type(str(p))
        maintype, subtype = (ctype or "image/png").split("/", 1)
        inline_images.append((cid, maintype, subtype, p.read_bytes()))

    rows = _read_rows(args.list)
    if args.city:
        rows = [r for r in rows if r.get("city", "").strip().lower() == args.city.strip().lower()]
    if args.limit:
        rows = rows[: args.limit]

    optout_email = _load_optout(OPTOUT_EMAIL)
    optout_wa = _load_optout(OPTOUT_WA)
    done = _load_ledger(args.campaign)

    delay = 60.0 / args.rate if args.rate > 0 else 0.0

    # Build the work plan
    plan = []  # (channel, recipient, subject, body, row)
    skipped = {"no_addr": 0, "optout": 0, "already_sent": 0, "bad_number": 0}
    for row in rows:
        if want_email:
            addr = row.get("email", "").lower()
            if not addr:
                skipped["no_addr"] += 1
            elif addr in optout_email:
                skipped["optout"] += 1
            elif f"email:{addr}" in done:
                skipped["already_sent"] += 1
            else:
                subj = args.subject.format_map(_Safe(row))
                body = email_tmpl.format_map(_Safe(row)) if email_tmpl else ""
                html_body = _render(html_tmpl, row) if html_tmpl else ""
                plan.append(("email", row["email"], subj, body, row, html_body))
        if want_wa:
            num = _norm_wa(row.get("whatsapp", ""))
            if not row.get("whatsapp"):
                skipped["no_addr"] += 1
            elif num is None:
                skipped["bad_number"] += 1
            elif num.lower() in optout_wa:
                skipped["optout"] += 1
            elif f"whatsapp:{num.lower()}" in done:
                skipped["already_sent"] += 1
            else:
                body = wa_tmpl.format_map(_Safe(row))
                plan.append(("whatsapp", num, None, body, row, ""))

    n_email = sum(1 for x in plan if x[0] == "email")
    n_wa = sum(1 for x in plan if x[0] == "whatsapp")

    # ----- Preview -----
    if not args.yes:
        print("=== DRY RUN (no messages sent — add --yes to send) ===\n")
        print(f"Campaign:   {args.campaign}")
        if want_email:
            print(f"Email from: {from_name} <{from_addr}>  (SMTP {sender['host']})")
            fmt = "HTML+text" if html_tmpl else "plain text"
            extras = []
            if html_tmpl:
                extras.append(f"format={fmt}")
            if inline_images:
                extras.append(f"{len(inline_images)} inline image(s): " +
                              ", ".join(c for c, *_ in inline_images))
            if args.city:
                extras.append(f"city={args.city}")
            if extras:
                print("Email:      " + " | ".join(extras))
        print(f"List:       {args.list}  ({len(rows)} rows)")
        print(f"To send:    {n_email} emails, {n_wa} WhatsApp")
        print(f"Skipped:    {skipped}")
        print(f"Rate:       {args.rate}/min  (~{delay:.1f}s between sends)")
        eta = (len(plan) * delay) / 60.0
        print(f"Est. time:  ~{eta:.1f} min\n")
        for ch, rcpt, subj, body, _, html_body in plan[:3]:
            print(f"--- sample {ch} -> {rcpt} ---")
            if subj:
                print(f"Subject: {subj}")
            shown = body or (_html_to_text(html_body) if html_body else "")
            preview = shown if len(shown) < 400 else shown[:400] + "…"
            if ch == "email" and html_body:
                preview += "\n[HTML body — text shown above]"
            if not args.no_footer and ch == "email":
                preview += "\n[+ unsubscribe footer]"
            print(preview + "\n")
        if len(plan) > 3:
            print(f"... and {len(plan) - 3} more.")
        return

    # ----- Real send -----
    sent = {"email": 0, "whatsapp": 0}
    failed = {"email": 0, "whatsapp": 0}
    smtp = None
    last = 0.0
    for ch, rcpt, subj, body, row, html_body in plan:
        if delay:
            wait = delay - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
        last = time.time()
        if ch == "email":
            try:
                if smtp is None:
                    smtp = _smtp_connect(sender)
                msg = _build_email(rcpt, subj, body, from_addr, from_name,
                                   not args.no_footer, args.reply_to,
                                   html=html_body, inline_images=inline_images,
                                   campaign=args.campaign, track=not args.no_track_clicks)
                smtp.send_message(msg, from_addr=from_addr, to_addrs=[rcpt])
                if not args.no_sent_copy and sender["save_sent"]:
                    _save_to_sent(msg)
                sent["email"] += 1
                _ledger_append(args.campaign, {"ok": True, "channel": "email", "recipient": rcpt})
            except Exception as e:
                failed["email"] += 1
                _ledger_append(args.campaign, {"ok": False, "channel": "email", "recipient": rcpt, "error": str(e)})
                try:
                    if smtp:
                        smtp.quit()
                except Exception:
                    pass
                smtp = None  # force reconnect next time
        else:
            ok, info = _wa_send(rcpt, body)
            if ok:
                sent["whatsapp"] += 1
                _ledger_append(args.campaign, {"ok": True, "channel": "whatsapp", "recipient": rcpt, "sid": info})
            else:
                failed["whatsapp"] += 1
                _ledger_append(args.campaign, {"ok": False, "channel": "whatsapp", "recipient": rcpt, "error": info})

    if smtp:
        try:
            smtp.quit()
        except Exception:
            pass

    print(json.dumps({
        "ok": True,
        "campaign": args.campaign,
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
    }))


if __name__ == "__main__":
    main()
