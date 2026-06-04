import imaplib
import smtplib
import email
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from datetime import datetime, timedelta
import os

IMAP_HOST = os.getenv("SEBA_IMAP_HOST")
IMAP_PORT = int(os.getenv("SEBA_IMAP_PORT", 993))
SMTP_HOST = os.getenv("SEBA_SMTP_HOST")
SMTP_PORT = int(os.getenv("SEBA_SMTP_PORT", 465))
EMAIL = os.getenv("SEBA_EMAIL")
PASSWORD = os.getenv("SEBA_EMAIL_PASSWORD")


def _decode(value):
    parts = decode_header(value)
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(part)
    return " ".join(result)


def get_unread_emails(since_hours=12):
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    conn.login(EMAIL, PASSWORD)
    conn.select("INBOX")
    since = (datetime.now() - timedelta(hours=since_hours)).strftime("%d-%b-%Y")
    _, ids = conn.search(None, f'(UNSEEN SINCE "{since}")')
    emails = []
    for mid in ids[0].split():
        # BODY.PEEK[] so reading the briefing does NOT mark Seba's mail as read.
        _, data = conn.fetch(mid, "(BODY.PEEK[])")
        msg = email.message_from_bytes(data[0][1])
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                    break
        else:
            body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
        emails.append({
            "id": mid.decode(),
            "subject": _decode(msg.get("Subject", "(no subject)")),
            "from": _decode(msg.get("From", "")),
            "date": msg.get("Date", ""),
            "body": body[:400],
        })
    conn.logout()
    return emails


def send_reply(to, subject, body, reply_to_msg_id=None):
    msg = MIMEMultipart()
    msg["From"] = EMAIL
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to_msg_id:
        msg["In-Reply-To"] = reply_to_msg_id
        msg["References"] = reply_to_msg_id
    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(EMAIL, PASSWORD)
        server.sendmail(EMAIL, to, msg.as_string())
