"""Per-employee email senders + self-service setup persistence.

Each employee sends mail from THEIR OWN address/SMTP account, never a shared
identity. Config is a JSON file keyed by Telegram user id:

    {
      "8621126122": {
        "from": "andrew@example.com",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "smtp_user": "andrew@example.com",
        "smtp_pass": "<app password>"
      }
    }

For Gmail / Google Workspace, smtp_pass must be an App Password. The file holds
secrets — it lives in the writable sandbox dir (/data/employees) so the employee
bot can create/update it under ProtectHome=read-only, and is kept chmod 600.
"""
import json
import os

PATH = os.environ.get("EMPLOYEE_EMAILS", "/data/employees/employee-emails.json")


def _load() -> dict:
    try:
        with open(PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def sender_for(uid: int) -> dict | None:
    """Return this employee's own sender config, or None if they have none."""
    s = _load().get(str(uid))
    if not s:
        return None
    if not (s.get("smtp_host") and s.get("smtp_user") and s.get("smtp_pass")):
        return None
    return s


def configured(uid: int) -> bool:
    return sender_for(uid) is not None


def infer_smtp(email: str) -> tuple[str | None, int | None]:
    """Best-guess SMTP host/port from the address domain. Returns (None, None)
    when unknown so the caller asks the user for the server."""
    domain = email.rsplit("@", 1)[-1].lower()
    # nightshiftent.ca is Google Workspace (seba@ already sends via Gmail SMTP).
    if domain in ("gmail.com", "googlemail.com", "nightshiftent.ca"):
        return "smtp.gmail.com", 587
    if domain in ("outlook.com", "hotmail.com", "live.com", "office365.com"):
        return "smtp.office365.com", 587
    return None, None


def save_sender(uid: int, sender: dict) -> None:
    """Create/update this employee's entry. Atomic write, 0600."""
    data = _load()
    data[str(uid)] = sender
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, PATH)
    os.chmod(PATH, 0o600)


# ---- Per-employee inbox (IMAP read) creds, self-enrolled via /setupinbox ----
# Separate file from senders so re-running /setupemail never clobbers inbox creds
# (and vice-versa). Same writable sandbox dir, same 0600 hygiene.
INBOX_PATH = os.environ.get("EMPLOYEE_INBOXES", "/data/employees/employee-inboxes.json")


def _load_inboxes() -> dict:
    try:
        with open(INBOX_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def inbox_for(uid: int) -> dict | None:
    """Return this employee's IMAP read creds, or None if unset/incomplete."""
    s = _load_inboxes().get(str(uid))
    if not s:
        return None
    if not (s.get("imap_host") and s.get("email") and s.get("password")):
        return None
    return s


def save_inbox(uid: int, inbox: dict) -> None:
    """Create/update this employee's inbox creds. Atomic write, 0600."""
    data = _load_inboxes()
    data[str(uid)] = inbox
    os.makedirs(os.path.dirname(INBOX_PATH), exist_ok=True)
    tmp = INBOX_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, INBOX_PATH)
    os.chmod(INBOX_PATH, 0o600)


def infer_imap(email: str) -> tuple[str | None, int | None]:
    """Best-guess IMAP host/port from the address domain."""
    domain = email.rsplit("@", 1)[-1].lower()
    if domain == "nightshiftent.ca":
        return "mail.nightshiftent.ca", 993  # GreenGeeks cPanel mail
    if domain in ("gmail.com", "googlemail.com"):
        return "imap.gmail.com", 993
    if domain in ("outlook.com", "hotmail.com", "live.com", "office365.com"):
        return "outlook.office365.com", 993
    return None, None
