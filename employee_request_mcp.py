#!/usr/bin/env python3
"""Stdio MCP server exposing the NS Team Bot agent's safe action tools.

Launched by claude (via --mcp-config employee_requests.mcp.json) for each
employee turn. Gives the locked-down chat agent (file/shell tools denied) a few
SAFE capabilities so it doesn't have to punt everything to Greg:

  - submit_request : forward a request/idea to Greg for approval
  - email_send     : send mail FROM the employee's own configured address
  - remember       : save a short note to the employee's persistent memory
  - recall         : read back what's been remembered

Requester identity arrives via env (NS_REQUESTER_ID / NS_REQUESTER_NAME), set
per-turn by employee_bot._ask.
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# Load .env so the helpers have their tokens even if env wasn't propagated.
try:
    with open(os.path.join(HERE, ".env")) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
except FileNotFoundError:
    pass

sys.path.insert(0, HERE)

import employee_email  # noqa: E402
import employee_notify  # noqa: E402
import employee_notes  # noqa: E402
import employee_requests  # noqa: E402
import mailer  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("nsrequests")


def _uid() -> str:
    return os.environ.get("NS_REQUESTER_ID", "").strip()


@mcp.tool()
def submit_request(text: str) -> str:
    """Forward a feature request, idea, or task to Greg (the owner) for approval.

    Use this only for things you genuinely CAN'T do yourself with your other
    tools -- e.g. a brand-new capability, money/wire actions, or anything that
    needs Greg's sign-off. `text` is the full request in the employee's words.
    Greg gets an Approve/Reject prompt; the employee is told the outcome.
    """
    rid = _uid()
    if not rid:
        return "I couldn't identify who's asking. Ask them to use the /request command instead."
    name = os.environ.get("NS_REQUESTER_NAME") or employee_notify.who(rid)
    rec = employee_requests.submit(int(rid), name, text)
    employee_notify.notify_owner_request(rec)
    return (
        f"Done -- sent to Greg for approval (request {rec['id']}). "
        "You'll hear back here when he decides."
    )


@mcp.tool()
def email_send(to: str, subject: str, body: str) -> str:
    """Send a plain-text email FROM the employee's own Nightshift address.

    Use this whenever the employee asks you to send/email something (including a
    test email to themselves). `to` is one or more comma-separated addresses.
    Do NOT file a request to Greg for this -- just send it. If the employee has
    no sending address configured yet, this returns guidance to run /setupemail.
    """
    rid = _uid()
    if not rid:
        return "I couldn't identify who's asking. Ask them to run /setupemail in this bot."
    sender = employee_email.sender_for(int(rid))
    if not sender:
        return (
            "You don't have a sending address set up yet. Run /setupemail here in "
            "the Telegram bot (takes ~1 min) and then I can send email for you."
        )
    recipients = [r.strip() for r in to.replace(";", ",").split(",") if r.strip()]
    if not recipients:
        return "I need at least one recipient email address."
    try:
        asyncio.run(asyncio.to_thread(mailer.send, subject, body, recipients, sender))
    except Exception as e:  # surface SMTP errors to the agent, don't crash
        return f"Couldn't send the email: {e}"
    return f"Sent '{subject}' from {sender.get('from')} to {', '.join(recipients)}."


@mcp.tool()
def remember(note: str) -> str:
    """Save a short, durable note about this employee or how they like things done
    (e.g. 'wants a daily briefing at 8am, point form'). Saved notes are shown to
    you at the start of every future conversation, so use this instead of saying
    you can't keep memory. Keep each note to one or two sentences."""
    rid = _uid()
    if not rid:
        return "I couldn't identify who's asking, so I can't save that."
    saved = employee_notes.append(int(rid), note)
    if not saved:
        return "Nothing to save."
    return f"Saved to memory: {saved}"


@mcp.tool()
def recall() -> str:
    """Return everything you've remembered about this employee so far."""
    rid = _uid()
    if not rid:
        return ""
    notes = employee_notes.read(int(rid))
    return notes or "(no notes saved yet)"


if __name__ == "__main__":
    mcp.run()
