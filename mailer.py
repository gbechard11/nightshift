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

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "") or SMTP_USER


class MailError(Exception):
    """User-facing failure from an email send."""


def configured() -> bool:
    """True only if we have enough to actually send. Keeps email dormant otherwise."""
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def send(subject: str, body: str, recipients: list[str]) -> None:
    """Send a plain-text email. Blocking — call via asyncio.to_thread() from async code.

    Raises MailError on misconfiguration or any SMTP failure.
    """
    if not configured():
        raise MailError(
            "SMTP not configured — set SMTP_HOST, SMTP_USER and SMTP_PASSWORD in .env."
        )
    recipients = [r.strip() for r in recipients if r and r.strip()]
    if not recipients:
        raise MailError("No recipients to send to.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    ctx = ssl.create_default_context()
    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30, context=ctx) as s:
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
                s.starttls(context=ctx)
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)
    except MailError:
        raise
    except Exception as e:  # noqa: BLE001 - surface a clean message to the caller
        raise MailError(f"{type(e).__name__}: {e}") from e
    log.info("emailed report '%s' to %s", subject, ", ".join(recipients))
