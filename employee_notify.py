"""Best-effort owner notifications.

Pings Greg in his Pedro chat whenever an employee performs a noteworthy action
through the connector (employee_mcp) or the employee bot (employee_bot). Delivery
uses the Pedro bot token (TELEGRAM_BOT_TOKEN) -> OWNER_TELEGRAM_ID, so the alert
always lands in Greg's existing Pedro conversation no matter which process sent
it. Failures are swallowed: a notification must NEVER break or delay the action.
"""
import logging
import os
import urllib.parse
import urllib.request

log = logging.getLogger("nightshift.employee_notify")

_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_OWNER = os.environ.get("OWNER_TELEGRAM_ID", "6575459992")

# Friendly labels for known Telegram user ids.
_NAMES = {
    6575459992: "Greg",
    8722742818: "Seba",
    8621126122: "Andrew",
}


def who(uid) -> str:
    """Map a Telegram user id to a friendly name (falls back to 'uid N')."""
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return f"uid {uid}"
    return _NAMES.get(uid, f"uid {uid}")


def notify_owner(text: str) -> None:
    """Send a one-way Telegram message to the owner. Best-effort, never raises."""
    if not _TOKEN or not _OWNER:
        return
    try:
        data = urllib.parse.urlencode(
            {"chat_id": _OWNER, "text": text, "disable_web_page_preview": "true"}
        ).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{_TOKEN}/sendMessage", data=data
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001 - notifications must not break actions
        log.warning("owner notify failed: %s", exc)


def notify_owner_request(rec) -> None:
    """Ping owner about an employee feature request with Approve/Reject buttons."""
    if not _TOKEN or not _OWNER:
        return
    import json as _json
    rid = rec["id"]
    text = (
        f"📝 Feature request from {rec.get('requester_name', 'employee')}:\n\n"
        f"{rec.get('text', '')}\n\n"
        "Approve = tell them yes (Greg builds it). Reject = decline."
    )
    markup = {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"req:approve:{rid}"},
            {"text": "❌ Reject", "callback_data": f"req:reject:{rid}"},
        ]]
    }
    try:
        data = urllib.parse.urlencode(
            {"chat_id": _OWNER, "text": text, "reply_markup": _json.dumps(markup)}
        ).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{_TOKEN}/sendMessage", data=data
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001
        log.warning("owner request notify failed: %s", exc)
