import imaplib
import smtplib
import email
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from datetime import datetime, timedelta
import os

# Generalized mailbox helpers: every function takes an explicit `creds` dict so
# the SAME code serves any employee's mailbox. creds keys: imap_host, imap_port,
# smtp_host, smtp_port, email, password (smtp_* only needed by send_reply).
# Employees self-enroll their IMAP creds through the Team Bot (/setupinbox),
# stored per-uid by employee_email (employee-inboxes.json).


def _decode(value):
    parts = decode_header(value)
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(part)
    return " ".join(result)


def check_imap(creds):
    """Log in and select INBOX to validate creds. Raises on failure."""
    conn = imaplib.IMAP4_SSL(creds["imap_host"], int(creds.get("imap_port", 993)))
    try:
        conn.login(creds["email"], creds["password"])
        conn.select("INBOX")
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


def get_unread_emails(creds, since_hours=12):
    conn = imaplib.IMAP4_SSL(creds["imap_host"], int(creds.get("imap_port", 993)))
    conn.login(creds["email"], creds["password"])
    conn.select("INBOX")
    since = (datetime.now() - timedelta(hours=since_hours)).strftime("%d-%b-%Y")
    _, ids = conn.search(None, f'(UNSEEN SINCE "{since}")')
    emails = []
    try:
        for mid in ids[0].split():
            # BODY.PEEK[] so reading does NOT mark the mail as read.
            _, data = conn.fetch(mid, "(BODY.PEEK[])")
            # A FETCH can return a bare flags line (not a (header, bytes) tuple);
            # skip anything that isn't the literal we asked for.
            if not data or not isinstance(data[0], tuple) or len(data[0]) < 2:
                continue
            msg = email.message_from_bytes(data[0][1])
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        # get_payload(decode=True) is None for an empty/bad part.
                        body = (part.get_payload(decode=True) or b"").decode(
                            "utf-8", errors="replace")
                        break
            else:
                body = (msg.get_payload(decode=True) or b"").decode(
                    "utf-8", errors="replace")
            emails.append({
                "id": mid.decode(),
                "subject": _decode(msg.get("Subject", "(no subject)")),
                "from": _decode(msg.get("From", "")),
                "date": msg.get("Date", ""),
                "body": body[:400],
            })
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return emails


def search_emails(creds, query="", since_days=30, max_results=20):
    """Search all mail (read + unread) by subject/sender/body text.

    Uses BODY.PEEK so nothing is marked as read. Returns newest-first, capped
    at max_results. `query` is matched via IMAP TEXT (subject + headers + body).
    """
    conn = imaplib.IMAP4_SSL(creds["imap_host"], int(creds.get("imap_port", 993)))
    conn.login(creds["email"], creds["password"])
    conn.select("INBOX")
    since = (datetime.now() - timedelta(days=since_days)).strftime("%d-%b-%Y")
    criteria = ['SINCE "%s"' % since]
    if query.strip():
        safe_q = query.strip().replace('"', "")
        criteria.append('TEXT "%s"' % safe_q)
    _, ids = conn.search(None, "(%s)" % " ".join(criteria))
    all_ids = ids[0].split()
    # Newest first, capped
    all_ids = all_ids[-max_results:][::-1]
    emails = []
    try:
        for mid in all_ids:
            _, data = conn.fetch(mid, "(BODY.PEEK[])")
            if not data or not isinstance(data[0], tuple) or len(data[0]) < 2:
                continue
            msg = email.message_from_bytes(data[0][1])
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = (part.get_payload(decode=True) or b"").decode(
                            "utf-8", errors="replace")
                        break
            else:
                body = (msg.get_payload(decode=True) or b"").decode(
                    "utf-8", errors="replace")
            emails.append({
                "id": mid.decode(),
                "subject": _decode(msg.get("Subject", "(no subject)")),
                "from": _decode(msg.get("From", "")),
                "date": msg.get("Date", ""),
                "body": body[:600],
            })
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return emails


def send_reply(creds, to, subject, body, reply_to_msg_id=None):
    msg = MIMEMultipart()
    msg["From"] = creds["email"]
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to_msg_id:
        msg["In-Reply-To"] = reply_to_msg_id
        msg["References"] = reply_to_msg_id
    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP_SSL(creds["smtp_host"], int(creds.get("smtp_port", 465))) as server:
        server.login(creds["email"], creds["password"])
        server.sendmail(creds["email"], to, msg.as_string())
