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
