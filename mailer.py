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
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

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


def send(subject: str, body: str, recipients: list[str], sender: dict | None = None) -> None:
    """Send a plain-text email. Blocking - call via asyncio.to_thread() from async code.

    If `sender` is given (an employee's own SMTP identity) it overrides the shared env
    config, so each employee sends from their own address. Raises MailError on
    misconfiguration or any SMTP failure.
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
