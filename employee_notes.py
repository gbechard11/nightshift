"""Per-employee persistent notes for the NS Team Bot conversational agent.

The chat agent runs with file tools (Write/Edit/Bash) deliberately denied, so it
can't keep its own memory. This gives it a SAFE, append-only memory: one short
markdown file per Telegram user under the writable sandbox dir. The agent writes
via the `remember` MCP tool and reads via `recall`; employee_bot._ask also injects
the saved notes into every turn so preferences survive session resets.
"""
import os
from datetime import datetime, timezone

NOTES_DIR = os.environ.get("EMPLOYEE_NOTES_DIR", "/data/employees/notes")
MAX_BYTES = 16000  # keep the per-turn injection small; oldest notes drop first


def _path(uid) -> str:
    return os.path.join(NOTES_DIR, f"{int(uid)}.md")


def read(uid) -> str:
    try:
        with open(_path(uid), encoding="utf-8") as fh:
            return fh.read().strip()
    except (FileNotFoundError, ValueError):
        return ""


def append(uid, note: str) -> str:
    note = (note or "").strip()
    if not note:
        return ""
    os.makedirs(NOTES_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    line = f"- ({stamp}) {note}\n"
    existing = read(uid)
    body = (existing + "\n" + line) if existing else line
    # Trim from the top if it grows too large.
    if len(body.encode("utf-8")) > MAX_BYTES:
        body = body.encode("utf-8")[-MAX_BYTES:].decode("utf-8", "ignore")
        body = body[body.index("\n") + 1:] if "\n" in body else body
    path = _path(uid)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return note
