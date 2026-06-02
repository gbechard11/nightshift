"""Shared claude-invocation core for every Pedro front-end.

bot.py (the owner's Telegram + WhatsApp Pedro) and employee_bot.py (the
locked-down Nightshift-employee bot) both call `run_claude` here, so the
subprocess plumbing, session management and broken-session retry live in ONE
place. Neither this module nor its callers import each other's transports.

`run_claude` covers every access tier through these knobs:
  - session_file: None → one-shot/stateless; a path → stateful (memory carries
    across messages via that session's UUID).
  - allowed_tools: space/comma-separated ALLOWLIST passed as --tools. This is the
    only safe boundary for an UNTRUSTED caller: it sets the tools that exist at
    all, so escalation tools (Bash, Read, Agent, Monitor, CronCreate, MCP, …)
    are simply absent. Prefer this over disallowed_tools for untrusted callers.
  - disallowed_tools: space-separated DENYLIST passed as --disallowed-tools.
    Convenient for a TRUSTED caller (e.g. the owner's /safe mode) but NOT a real
    security boundary — the CLI has many command-capable tools (Monitor, Cron*,
    Agent sub-agents that don't inherit the denylist, MCP servers), so a denylist
    can be walked around. Never rely on it to contain an untrusted user.
  - strict_mcp: pass --strict-mcp-config to ignore all ambient MCP servers (drops
    e.g. the claude-mem MCP). Use together with allowed_tools for untrusted lanes.
  - lock: an asyncio.Lock serializing runs that share one session; None for
    independent runs.

Every run uses --permission-mode bypassPermissions, so allowed/disallowed tools
(NOT the permission prompt) constrain access.
"""
import asyncio
import json
import logging
import os
import uuid

log = logging.getLogger("nightshift.brain")

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/usr/bin/claude")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "300"))

# Proactively rotate the persistent session before it grows past the standard
# 200k context window (which would otherwise escalate into the paid 1M-context
# tier and make every later message fail). Measured from real token usage the
# CLI reports. A fresh session already costs ~30k tokens (system prompt +
# CLAUDE.md + BRAIN.md), so the default leaves room for the handoff turn itself.
ROTATE_AT_TOKENS = int(os.environ.get("PEDRO_ROTATE_TOKENS", "170000"))

# stderr fragments that mean "this session can't be used" — wipe and retry once.
_SESSION_BROKEN = ("already in use", "no conversation", "not found", "no such session")

# Session grew past the standard context window and would need 1M-context
# usage credits to resume — wipe and start fresh instead of erroring on every
# subsequent message.
_SESSION_TOO_BIG = ("usage credits required",)


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



def _parse_result(out: str):
    """Extract (text, context_tokens) from an --output-format json reply.
    Falls back to (raw_text, None) if the output is not the expected JSON."""
    try:
        o = json.loads(out)
    except Exception:
        return out, None
    if not isinstance(o, dict):
        return out, None
    text = o.get("result")
    if not isinstance(text, str):
        text = out
    u = o.get("usage") or {}
    try:
        ctx = (
            int(u.get("input_tokens", 0))
            + int(u.get("cache_creation_input_tokens", 0))
            + int(u.get("cache_read_input_tokens", 0))
        )
    except Exception:
        ctx = None
    return text, ctx


_HANDOFF_PROMPT = (
    "SYSTEM: your conversation memory is being rotated now to stay within context "
    "limits. Before it resets, prepend a dated entry to /data/greg/brain/BRAIN.md in "
    "the format '## YYYY-MM-DD [handoff] headline' capturing every open item, "
    "in-progress task, pending reply, and key decision from this conversation so a "
    "fresh session can continue seamlessly. Be specific: names, amounts, dates, and "
    "the exact next action for each. Save it with the Edit/Write tools, then reply 'saved'."
)


async def _rotate_session(base, sid, session_file, workdir, timeout):
    """Write a handoff to BRAIN.md on the OLD session, then drop the session pointer
    so the next run starts fresh (and reloads BRAIN.md). Best-effort: never raises."""
    try:
        args = base + ["--resume", sid, "-p", _HANDOFF_PROMPT]
        await _run_claude(args, workdir, min(timeout, 180))
    except Exception as e:  # noqa: BLE001
        log.warning("handoff before rotation failed (resetting anyway): %s", e)
    try:
        os.remove(session_file)
    except FileNotFoundError:
        pass
    log.info("rotated persistent session %s.. -> fresh on next message", sid[:8])


async def run_claude(
    prompt: str,
    *,
    workdir: str,
    session_file: str | None = None,
    allowed_tools: str | None = None,
    disallowed_tools: str | None = None,
    strict_mcp: bool = False,
    lock: asyncio.Lock | None = None,
    timeout: int = CLAUDE_TIMEOUT,
) -> str:
    """Invoke claude for `prompt` and return its reply text.

    - workdir: cwd for the process (also scopes claude's session store, so two
      callers with different workdirs get isolated session namespaces).
    - session_file: path holding a session UUID. If set, the run is stateful:
      created with --session-id on first use, resumed with --resume after. If
      None, the run is one-shot with no memory.
    - allowed_tools: space/comma-separated ALLOWLIST via --tools (the only tools
      that exist for the run). The safe boundary for untrusted callers.
    - disallowed_tools: space-separated DENYLIST via --disallowed-tools. Trusted
      callers only — not a real boundary (see module docstring).
    - strict_mcp: pass --strict-mcp-config to ignore ambient MCP servers.
    - lock: serialize runs sharing one session. Pass None for stateless or
      otherwise-independent runs (a throwaway lock is used → no serialization).

    Raises PedroError with a user-facing message on timeout / nonzero exit /
    missing binary.
    """
    lock = lock or asyncio.Lock()  # throwaway → effectively no serialization
    async with lock:
        base = [CLAUDE_BIN, "--permission-mode", "bypassPermissions",
                "--output-format", "json"]
        if strict_mcp:
            base.append("--strict-mcp-config")
        if allowed_tools:
            # --tools is variadic; the following arg (-p or a session flag) ends it.
            base += ["--tools", *allowed_tools.replace(",", " ").split()]
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
        _combined = (out + " " + err).lower()
        # Only ever wipe+retry on a FAILED run (rc != 0). On a successful
        # reply (rc == 0) the output text could legitimately contain these
        # phrases (e.g. explaining a 'usage credits required' error) and must
        # not trigger a spurious session reset.
        if session_file is not None and rc not in (0, None) and (
            any(s in _combined for s in _SESSION_BROKEN)
            or any(s in _combined for s in _SESSION_TOO_BIG)
        ):
            log.warning("session unusable, starting fresh: %s", (err or out)[:200])
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
        text, ctx_tokens = _parse_result(out)

        # Proactive rotation: if the persistent session has grown close to the
        # standard context window, hand off to BRAIN.md and reset BEFORE the next
        # message would tip us into the paid 1M-context tier.
        if (
            session_file is not None
            and ctx_tokens is not None
            and ctx_tokens >= ROTATE_AT_TOKENS
        ):
            log.info("session at %d ctx tokens (>= %d) — rotating",
                     ctx_tokens, ROTATE_AT_TOKENS)
            await _rotate_session(base, sid, session_file, workdir, timeout)

        return (text or "").strip() or "(empty response)"
