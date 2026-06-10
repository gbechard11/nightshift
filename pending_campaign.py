"""Confirm-first staging for employee Meta ad-campaign launches.

The employee chat agent can DRAFT a campaign (built PAUSED in Meta, no spend),
but it must NEVER start spend on its own judgment. The draft_ad_campaign MCP tool
builds the PAUSED campaign, stages the launch here, and DMs the employee in
Telegram with the campaign summary + a Launch / Keep-paused button. Spend starts
ONLY when the human taps Launch (handled in employee_bot.on_campaign_button),
which flips the existing PAUSED campaign ACTIVE on the campaign's own ad account.
The agent can at most surface a paused draft to approve; it can never start spend.

Disk-backed (one JSON per token) so it works across the bot process and the
per-message MCP subprocesses, which don't share memory. Mirrors pending_email.py.
"""
import json
import os
import secrets
import time
import urllib.parse
import urllib.request

PENDING_DIR = os.environ.get("PENDING_CAMPAIGN_DIR", "/data/employees/pending_campaign")


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


def stage(uid, campaign_id, name, daily_cad, summary, acct_key="nightshift") -> str:
    """Persist a pending (already-built, PAUSED) campaign launch; return its token.
    acct_key records which ad-account profile the campaign lives on so the Launch
    button activates with the right token."""
    os.makedirs(PENDING_DIR, exist_ok=True)
    token = secrets.token_urlsafe(8)
    rec = {
        "token": token, "uid": int(uid), "campaign_id": campaign_id,
        "name": name, "daily_cad": float(daily_cad), "summary": summary,
        "acct_key": acct_key, "ts": int(time.time()),
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
    """Delete a pending launch record. Best-effort. The campaign itself is left
    untouched (still PAUSED in Meta unless it was launched)."""
    rec = load(token)
    try:
        os.remove(_path(token))
    except OSError:
        pass
    return rec


def send_confirm_prompt(rec) -> bool:
    """DM the employee the campaign summary + Launch/Keep-paused buttons via the
    employee bot. The callback_data uses the existing `camp:` prefix so the bot's
    on_campaign_button handler is the single spend gate. Returns True on success."""
    if not rec:
        return False
    token = _env("EMPLOYEE_BOT_TOKEN")
    if not token:
        return False
    markup = {"inline_keyboard": [[
        {"text": "\U0001F680 Launch (start spend)", "callback_data": f"camp:go:{rec['token']}"},
        {"text": "✖️ Keep paused", "callback_data": f"camp:hold:{rec['token']}"},
    ]]}
    text = (rec.get("summary", "") or "Campaign drafted (PAUSED).") + \
        "\n\nLaunching starts real spend on this ad account. Launch now?"
    try:
        data = urllib.parse.urlencode({
            "chat_id": rec["uid"],
            "text": text,
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
