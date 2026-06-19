"""Agent-to-agent message relay between an external partner bot (e.g. Seba's
@TARSMEDIAFINA_BOT) and the Nightshift Team Bot.

Telegram bots cannot message each other directly, so the channel is a small
per-identity JSONL mailbox on disk:

  /data/employees/relay/<uid>.jsonl   one thread per connected identity
  /data/employees/relay/<uid>.read    read-cursor (count of to_partner msgs served)

Each line: {"ts","from","dir","text"} where dir is:
  "to_ns"      -> message the partner agent sent INTO the Nightshift Team Bot
  "to_partner" -> reply the Nightshift side sent back OUT to the partner agent

The partner agent writes with message_ns_team and polls with read_ns_team_messages
(both in employee_mcp); the Nightshift owner replies with /reply (employee_bot).
Best-effort and dependency-free so either process can import it.
"""
import json
import os
import threading
from datetime import datetime

RELAY_DIR = os.environ.get("AGENT_RELAY_DIR", "/data/employees/relay")
_lock = threading.Lock()


def _path(uid) -> str:
    return os.path.join(RELAY_DIR, f"{int(uid)}.jsonl")


def _cursor(uid) -> str:
    return os.path.join(RELAY_DIR, f"{int(uid)}.read")


def append(uid, frm: str, direction: str, text: str) -> dict:
    """Append one message to the thread for `uid`. direction in {to_ns,to_partner}."""
    if direction not in ("to_ns", "to_partner"):
        raise ValueError("direction must be to_ns or to_partner")
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "from": frm,
        "dir": direction,
        "text": (text or "").strip(),
    }
    with _lock:
        os.makedirs(RELAY_DIR, exist_ok=True)
        with open(_path(uid), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _read_all(uid) -> list:
    p = _path(uid)
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def thread(uid, limit: int = 50) -> list:
    """Return the last `limit` messages of the thread (both directions)."""
    return _read_all(uid)[-limit:]


def unread_to_partner(uid, advance: bool = True) -> list:
    """Return to_partner messages not yet served to the partner agent.
    Advances the read cursor unless advance=False (peek)."""
    msgs = [m for m in _read_all(uid) if m.get("dir") == "to_partner"]
    seen = 0
    cp = _cursor(uid)
    if os.path.exists(cp):
        try:
            seen = int(open(cp, encoding="utf-8").read().strip() or "0")
        except (ValueError, OSError):
            seen = 0
    new = msgs[seen:]
    if advance and new:
        with _lock:
            os.makedirs(RELAY_DIR, exist_ok=True)
            tmp = cp + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(str(len(msgs)))
            os.replace(tmp, cp)
    return new
