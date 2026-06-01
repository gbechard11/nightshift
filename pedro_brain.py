"""Shared claude-invocation core for every Pedro front-end.

bot.py (the owner's Telegram + WhatsApp Pedro) and employee_bot.py (the
locked-down Nightshift-employee bot) both call `run_claude` here, so the
subprocess plumbing, session management and broken-session retry live in ONE
place. Neither this module nor its callers import each other's transports.

`run_claude` covers every access tier through three knobs:
  - session_file: None → one-shot/stateless; a path → stateful (memory carries
    across messages via that session's UUID).
  - disallowed_tools: space-separated tools blocked with --disallowed-tools
    (e.g. the restricted set that denies Bash/Read so secrets stay unreachable).
  - lock: an asyncio.Lock serializing runs that share one session; None for
    independent runs.

Every run uses --permission-mode bypassPermissions, so access is constrained by
disallowed_tools, NOT by the permission prompt. That makes the disallowed set
the real security boundary for any untrusted caller.
"""
import asyncio
import logging
import os
import uuid

log = logging.getLogger("nightshift.brain")

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/usr/bin/claude")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "300"))

# stderr fragments that mean "this session can't be used" — wipe and retry once.
_SESSION_BROKEN = ("already in use", "no conversation", "not found", "no such session")


class PedroError(Exception):
    """User-facing failure from a claude run (timeout, nonzero exit, etc.)."""


async def _run_claude(args: list[str], workdir: str, timeout: int):
    """Run claude; return (returncode, stdout_str, stderr_str).
    returncode is None on timeout or missing binary (message is in stderr_str)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
        )
    except FileNotFoundError:
        return None, "", f"claude binary not found at {CLAUDE_BIN}"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None, "", f"claude timed out after {timeout}s"
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def _get_session_id(session_file: str) -> tuple[str, bool]:
    """Return (session_id, is_new). is_new=True means we just created it and the
    first claude call should use --session-id; otherwise use --resume."""
    try:
        with open(session_file) as f:
            sid = f.read().strip()
        if sid:
            return sid, False
    except FileNotFoundError:
        pass
    sid = str(uuid.uuid4())
    os.makedirs(os.path.dirname(session_file) or ".", exist_ok=True)
    with open(session_file, "w") as f:
        f.write(sid)
    return sid, True


async def run_claude(
    prompt: str,
    *,
    workdir: str,
    session_file: str | None = None,
    disallowed_tools: str | None = None,
    lock: asyncio.Lock | None = None,
    timeout: int = CLAUDE_TIMEOUT,
) -> str:
    """Invoke claude for `prompt` and return its reply text.

    - workdir: cwd for the process (also scopes claude's session store, so two
      callers with different workdirs get isolated session namespaces).
    - session_file: path holding a session UUID. If set, the run is stateful:
      created with --session-id on first use, resumed with --resume after. If
      None, the run is one-shot with no memory.
    - disallowed_tools: space-separated tools to block via --disallowed-tools.
    - lock: serialize runs sharing one session. Pass None for stateless or
      otherwise-independent runs (a throwaway lock is used → no serialization).

    Raises PedroError with a user-facing message on timeout / nonzero exit /
    missing binary.
    """
    lock = lock or asyncio.Lock()  # throwaway → effectively no serialization
    async with lock:
        base = [CLAUDE_BIN, "--permission-mode", "bypassPermissions"]
        if disallowed_tools:
            base += ["--disallowed-tools", disallowed_tools]

        if session_file is None:
            args = base + ["-p", prompt]
        else:
            sid, is_new = _get_session_id(session_file)
            # First message of a conversation creates the session (--session-id);
            # every later message RESUMES it (--resume) so context carries over.
            session_flag = "--session-id" if is_new else "--resume"
            args = base + [session_flag, sid, "-p", prompt]

        rc, out, err = await _run_claude(args, workdir, timeout)

        # Recover from a broken/locked/missing session: wipe the pointer, start a
        # genuinely fresh session, retry once. (Stateless runs have no session to
        # recover, so this only applies when session_file is set.)
        if (
            rc not in (0, None)
            and session_file is not None
            and any(s in err.lower() for s in _SESSION_BROKEN)
        ):
            log.warning("session unusable, starting fresh: %s", err[:200])
            try:
                os.remove(session_file)
            except FileNotFoundError:
                pass
            sid, _ = _get_session_id(session_file)  # creates a new one
            args = base + ["--session-id", sid, "-p", prompt]
            rc, out, err = await _run_claude(args, workdir, timeout)

        if rc is None:
            raise PedroError(err)  # timeout / missing-binary message
        if rc != 0:
            raise PedroError(
                f"claude exited {rc}:\n{(err.strip() or '(no stderr)')[:1500]}"
            )
        return out.strip() or "(empty response)"
