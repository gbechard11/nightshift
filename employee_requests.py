"""Lightweight employee feature-request flow.

An employee runs /request <text> in the employee bot. The request is persisted
to disk and the owner (Greg) is pinged in his Pedro chat with Approve/Reject
buttons. The tap is handled by the Pedro bot (bot.py), which records the
decision and notifies the employee back via the employee bot token. State lives
on disk because employee_bot and bot run as separate processes.
"""
import json
import os
import time
import urllib.parse
import urllib.request
import uuid

REQ_DIR = os.environ.get("EMPLOYEE_REQUESTS_DIR", "/data/employees/requests")
_EMPLOYEE_TOKEN = os.environ.get("EMPLOYEE_BOT_TOKEN", "")


def _path(req_id):
    return os.path.join(REQ_DIR, f"{req_id}.json")


def submit(requester_id, requester_name, text):
    os.makedirs(REQ_DIR, exist_ok=True)
    req_id = uuid.uuid4().hex[:10]
    rec = {
        "id": req_id,
        "requester_id": int(requester_id),
        "requester_name": requester_name,
        "text": text,
        "status": "pending",
        "created": int(time.time()),
    }
    with open(_path(req_id), "w", encoding="utf-8") as f:
        json.dump(rec, f)
    return rec


def load(req_id):
    try:
        with open(_path(req_id), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def set_status(req_id, status):
    rec = load(req_id)
    if not rec:
        return None
    rec["status"] = status
    rec["decided"] = int(time.time())
    with open(_path(req_id), "w", encoding="utf-8") as f:
        json.dump(rec, f)
    return rec


def notify_employee(chat_id, text):
    """Message an employee via the employee bot token. Best-effort, never raises."""
    if not _EMPLOYEE_TOKEN:
        return
    try:
        data = urllib.parse.urlencode(
            {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
        ).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{_EMPLOYEE_TOKEN}/sendMessage", data=data
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception:  # noqa: BLE001 - notifications must not break anything
        pass
