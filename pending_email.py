"""Confirm-first staging for employee email sends.

The employee chat agent must NEVER send email on its own judgment. Instead of
sending, email_send STAGES the fully-rendered draft here and pings the employee
in Telegram with the exact draft + a Send / Cancel button. The email goes out
ONLY when the human taps Send (handled in employee_bot.on_email_confirm). This
makes the human tap -- not the agent's interpretation of an instruction -- the
thing that authorizes a send. An ambiguous "push it please" can at most surface
a draft for the user to reject; it can never put mail in anyone's outbox.

Disk-backed (one JSON per token) so it works across the bot process and the
per-message MCP subprocesses, which don't share memory.
"""
import json
import os
import secrets
import shutil
import time
import urllib.parse
import urllib.request

PENDING_DIR = os.environ.get("PENDING_EMAIL_DIR", "/data/employees/pending_email")


def _env(key: str, default: str = "") -> str:
    """Read an env var, falling back to ~/nightshift/.env so stdio-MCP contexts
    that didn't inherit the var still find the bot token."""
    v = os.environ.get(key)
    if v:
        return v
    try:
        with open(os.path.expanduser("~/nightshift/.env"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, val = line.split("=", 1)
                    if k.strip() == key:
                        return val.strip()
    except FileNotFoundError:
        pass
    return default


def _path(token: str) -> str:
    return os.path.join(PENDING_DIR, f"{token}.json")


def _att_dir(token: str) -> str:
    return os.path.join(PENDING_DIR, f"{token}_att")


def stage(uid, sender, to, cc, subject, body, attachments=None, bcc=None) -> str:
    """Persist a pending send and return its token. Attachment file paths are
    COPIED into a per-token dir so they survive until the user confirms (the
    caller is free to clean up its own temp copies)."""
    os.makedirs(PENDING_DIR, exist_ok=True)
    token = secrets.token_hex(8)
    to = to if isinstance(to, list) else [a.strip() for a in str(to).split(",") if a.strip()]
    cc = cc if isinstance(cc, list) else [a.strip() for a in str(cc or "").split(",") if a.strip()]
    bcc = bcc if isinstance(bcc, list) else [a.strip() for a in str(bcc or "").split(",") if a.strip()]
    saved = []
    if attachments:
        os.makedirs(_att_dir(token), exist_ok=True)
        for p in attachments:
            try:
                dst = os.path.join(_att_dir(token), os.path.basename(p))
                shutil.copy2(p, dst)
                saved.append(dst)
            except OSError:
                pass
    rec = {
        "token": token, "uid": int(uid), "sender": sender,
        "to": to, "cc": cc, "bcc": bcc, "subject": subject, "body": body,
        "attachments": saved, "ts": int(time.time()),
    }
    tmp = _path(token) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    os.replace(tmp, _path(token))
    os.chmod(_path(token), 0o600)
    return token


def load(token: str):
    try:
        with open(_path(token), encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return None


def discard(token: str):
    """Delete a pending send + its copied attachments. Best-effort."""
    rec = load(token)
    try:
        os.remove(_path(token))
    except OSError:
        pass
    shutil.rmtree(_att_dir(token), ignore_errors=True)
    return rec


def _fmt(v) -> str:
    if not v:
        return ""
    return ", ".join(v) if isinstance(v, list) else str(v)


def preview_text(rec) -> str:
    lines = [
        "\U0001F4E7 Confirm send — nothing goes out until you tap Send.",
        "",
        f"From: {rec.get('sender', '')}",
        f"To: {_fmt(rec.get('to'))}",
    ]
    if rec.get("cc"):
        lines.append(f"Cc: {_fmt(rec.get('cc'))}")
    if rec.get("bcc"):
        lines.append(f"Bcc: {_fmt(rec.get('bcc'))}")
    lines.append(f"Subject: {rec.get('subject', '')}")
    if rec.get("attachments"):
        lines.append("Attachments: " + ", ".join(os.path.basename(a) for a in rec["attachments"]))
    lines.append("")
    body = rec.get("body", "") or ""
    lines.append(body if len(body) <= 1500 else body[:1500] + " …(truncated)")
    return "\n".join(lines)


def send_confirm_prompt(rec) -> bool:
    """DM the employee the exact draft + Send/Cancel buttons via the employee bot.
    Returns True if Telegram accepted the message."""
    if not rec:
        return False
    token = _env("EMPLOYEE_BOT_TOKEN")
    if not token:
        return False
    markup = {"inline_keyboard": [[
        {"text": "✅ Send now", "callback_data": f"emailsend:confirm:{rec['token']}"},
        {"text": "❌ Cancel", "callback_data": f"emailsend:cancel:{rec['token']}"},
    ]]}
    try:
        data = urllib.parse.urlencode({
            "chat_id": rec["uid"],
            "text": preview_text(rec),
            "reply_markup": json.dumps(markup),
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception:  # noqa: BLE001 - delivery failure is reported to the caller
        return False
