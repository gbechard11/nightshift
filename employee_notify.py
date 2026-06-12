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
    7682958654: "Gabe",
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


def notify_blast_approval(uid, bid, text) -> None:
    """DM the employee (via the NS Team Bot) their drafted blast with Approve/Cancel
    buttons. The Approve tap is what authorizes the real send (handled in employee_bot).
    Best-effort, never raises."""
    token = os.environ.get("EMPLOYEE_BOT_TOKEN") or _TOKEN
    if not token or not uid:
        return
    import json as _json
    markup = {"inline_keyboard": [[
        {"text": "\u2705 Approve & send", "callback_data": f"blastsend:{bid}"},
        {"text": "\u274C Cancel", "callback_data": f"blastcancel:{bid}"},
    ]]}
    try:
        data = urllib.parse.urlencode(
            {"chat_id": uid, "text": text, "reply_markup": _json.dumps(markup),
             "disable_web_page_preview": "true"}
        ).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001
        log.warning("blast approval notify failed: %s", exc)


def notify_blast_scheduled(uid, bid, text) -> None:
    """DM the employee their scheduled blast with Approve & Cancel buttons. It will
    NOT fire on its timer until the employee taps Approve (which arms it) \u2014 a
    scheduled mass-send still needs an explicit human go. Best-effort."""
    token = os.environ.get("EMPLOYEE_BOT_TOKEN") or _TOKEN
    if not token or not uid:
        return
    import json as _json
    markup = {"inline_keyboard": [[
        {"text": "\u2705 Approve & schedule", "callback_data": f"blastarm:{bid}"},
        {"text": "\u274C Cancel", "callback_data": f"blastcancel:{bid}"},
    ]]}
    try:
        data = urllib.parse.urlencode(
            {"chat_id": uid, "text": text, "reply_markup": _json.dumps(markup),
             "disable_web_page_preview": "true"}
        ).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001
        log.warning("blast scheduled notify failed: %s", exc)


def send_plain(uid, text, *, use_employee_bot: bool = True) -> None:
    """Send a plain text message to a Telegram user. Best-effort."""
    token = (os.environ.get("EMPLOYEE_BOT_TOKEN") if use_employee_bot else None) or _TOKEN
    if not token or not uid:
        return
    try:
        data = urllib.parse.urlencode(
            {"chat_id": uid, "text": text, "disable_web_page_preview": "true"}
        ).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001
        log.warning("send_plain notify failed: %s", exc)
