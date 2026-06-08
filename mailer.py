"""Tiny SMTP email sender for Pedro — used to deliver Meta Ads reports to the
report recipients (Seba) so he's copied on reports/optimization updates.

Design mirrors whatsapp.py / meta_ads.py: env config up top, a `configured()`
gate, and it stays completely dormant until SMTP creds are set — so the bot is
unaffected if email isn't configured yet.

Set in .env to enable:
    SMTP_HOST        e.g. smtp.gmail.com   (Gmail/Workspace) or your mail host
    SMTP_PORT        587 (STARTTLS, default) or 465 (SSL)
    SMTP_USER        the login / sending address
    SMTP_PASSWORD    app password (Gmail/Workspace require an App Password, not the
                     normal account password)
    MAIL_FROM        optional "From" address; defaults to SMTP_USER

Recipients come from META_REPORT_RECIPIENTS (defaults to seba@nightshiftent.ca),
shared with meta_ads so there's one source of truth.

smtplib is blocking, so callers should invoke send() via asyncio.to_thread().
"""
import imaplib
import logging
import mimetypes
import os
import re
import smtplib
import ssl
import time
from email.generator import BytesGenerator
from email.message import EmailMessage
from io import BytesIO

log = logging.getLogger("nightshift.mailer")

SMTP_HOST = os.environ.get("REPORT_SMTP_HOST") or os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("REPORT_SMTP_PORT") or os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("REPORT_SMTP_USER") or os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("REPORT_SMTP_PASSWORD") or os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "") or SMTP_USER


class MailError(Exception):
    """User-facing failure from an email send."""


def configured() -> bool:
    """True only if we have enough to actually send. Keeps email dormant otherwise."""
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def _resolve(sender: dict | None):
    """Pick SMTP creds: an employee's own identity dict, else the shared env config."""
    if sender:
        host = sender.get("smtp_host") or SMTP_HOST
        port = int(sender.get("smtp_port") or SMTP_PORT)
        user = sender.get("smtp_user") or SMTP_USER
        password = sender.get("smtp_pass") or SMTP_PASSWORD
        from_addr = sender.get("from") or user
    else:
        host, port, user, password, from_addr = (
            SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_FROM
        )
    return host, port, user, password, from_addr


# SMTP host -> (IMAP host, fallback Sent-folder name) for the providers our
# employees actually use. We still prefer the server's own \Sent-flagged folder
# when we can list it (handles cPanel "INBOX.Sent" automatically); these are
# only the fallback when the LIST lookup turns up nothing.
_SENT_DEFAULTS = {
    "smtp.gmail.com": ("imap.gmail.com", "[Gmail]/Sent Mail"),
    "smtp.googlemail.com": ("imap.gmail.com", "[Gmail]/Sent Mail"),
    "smtp.office365.com": ("outlook.office365.com", "Sent Items"),
}


def _imap_for(host: str) -> tuple[str, str]:
    """Best-guess (imap_host, fallback Sent folder) from an SMTP host."""
    h = (host or "").lower()
    if h in _SENT_DEFAULTS:
        return _SENT_DEFAULTS[h]
    # cPanel/GreenGeeks (and most cPanel-style hosts) use the SAME hostname for
    # IMAP and SMTP, with a nested INBOX.Sent folder.
    if h.startswith("mail."):
        return host, "INBOX.Sent"
    # Generic fallback: swap smtp->imap if present, else reuse the host.
    return (h.replace("smtp", "imap", 1) if "smtp" in h else host), "Sent"


def _save_to_sent(msg: EmailMessage, host: str, user: str, password: str,
                  folder_override: str | None = None) -> None:
    """Best-effort: APPEND a copy of the just-sent message to the sender's Sent
    folder so it shows up in their Outlook/webmail. NEVER raises — the mail has
    already gone out, so a Sent-copy failure must not surface as a send failure.

    The same login/password used for SMTP also authenticates IMAP on Gmail,
    Office365 and cPanel/GreenGeeks, so no extra creds are needed.
    """
    try:
        imap_host, sent_folder = _imap_for(host)
        if folder_override:
            sent_folder = folder_override
        with imaplib.IMAP4_SSL(imap_host, 993) as imap:
            imap.login(user, password)
            # Prefer the mailbox's own \Sent special-use folder when we can find
            # it — more reliable than guessing the name per provider. LIST lines
            # look like:  (\HasNoChildren \Sent) "." INBOX.Sent
            #         or  (\HasNoChildren \Sent) "/" "[Gmail]/Sent Mail"
            try:
                typ, boxes = imap.list()
                if typ == "OK":
                    for raw in boxes or []:
                        line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
                        m = re.match(r'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|\S+)\s+(?P<name>.+?)\s*$', line)
                        if m and "\\Sent" in m.group("flags"):
                            name = m.group("name").strip()
                            if name.startswith('"') and name.endswith('"'):
                                name = name[1:-1]
                            if name:
                                sent_folder = name
                            break
            except Exception:  # noqa: BLE001 — keep the fallback folder name
                pass
            buf = BytesIO()
            BytesGenerator(buf, mangle_from_=False).flatten(msg)
            raw_bytes = buf.getvalue().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            box = '"%s"' % sent_folder if " " in sent_folder else sent_folder
            imap.append(box, "\\Seen", imaplib.Time2Internaldate(time.time()), raw_bytes)
    except Exception as e:  # noqa: BLE001
        log.warning("sent-folder save failed (mail still sent): %s", e)


def send(subject: str, body: str, recipients: list[str], sender: dict | None = None,
         attachments: list[str] | None = None) -> None:
    """Send a plain-text email. Blocking - call via asyncio.to_thread() from async code.

    If `sender` is given (an employee's own SMTP identity) it overrides the shared env
    config, so each employee sends from their own address. Raises MailError on
    misconfiguration or any SMTP failure.

    After a successful send, a copy is APPENDed to the sender's IMAP Sent folder
    (best-effort) so it shows up in their Outlook/webmail. Pass sender["sent_folder"]
    to override the auto-detected folder name.
    """
    host, port, user, password, from_addr = _resolve(sender)
    if not (host and user and password):
        raise MailError(
            "Email not set up - run /setupemail to add your sending address."
        )
    recipients = [r.strip() for r in recipients if r and r.strip()]
    if not recipients:
        raise MailError("No recipients to send to.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    for _path in (attachments or []):
        with open(_path, "rb") as _fh:
            _data = _fh.read()
        _ctype = mimetypes.guess_type(_path)[0] or "application/octet-stream"
        _main, _, _sub = _ctype.partition("/")
        msg.add_attachment(_data, maintype=_main, subtype=_sub or "octet-stream",
                           filename=os.path.basename(_path))

    ctx = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30, context=ctx) as srv:
                srv.login(user, password)
                srv.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as srv:
                srv.starttls(context=ctx)
                srv.login(user, password)
                srv.send_message(msg)
    except MailError:
        raise
    except Exception as e:  # noqa: BLE001 - surface a clean message to the caller
        raise MailError(f"{type(e).__name__}: {e}") from e
    log.info("emailed '%s' to %s (from %s)", subject, ", ".join(recipients), from_addr)

    # Mail is out — now best-effort drop a copy in the sender's Sent folder so it
    # syncs into their Outlook. Failure here is logged, never raised.
    _save_to_sent(msg, host, user, password, (sender or {}).get("sent_folder"))
