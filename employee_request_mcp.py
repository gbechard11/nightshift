#!/usr/bin/env python3
"""Stdio MCP server exposing ONE tool to the NS Team Bot agent: submit_request.

Launched by claude (via --mcp-config employee_requests.mcp.json) for each
employee turn. Lets the agent forward a request/idea to Greg for approval
WITHOUT the employee needing the /request slash command. Requester identity
arrives via env (NS_REQUESTER_ID / NS_REQUESTER_NAME), set per-turn by
employee_bot._ask.
"""
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

import employee_notify  # noqa: E402
import employee_requests  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("nsrequests")


@mcp.tool()
def submit_request(text: str) -> str:
    """Forward a feature request, idea, or task to Greg (the owner) for approval.

    Use whenever the employee asks to send/forward a request, idea, feature, or
    task to Greg or to Pedro. `text` is the full request in the employee's words.
    Greg gets an Approve/Reject prompt; the employee is told the outcome.
    """
    rid = os.environ.get("NS_REQUESTER_ID", "").strip()
    if not rid:
        return "I couldn't identify who's asking. Ask them to use the /request command instead."
    name = os.environ.get("NS_REQUESTER_NAME") or employee_notify.who(rid)
    rec = employee_requests.submit(int(rid), name, text)
    employee_notify.notify_owner_request(rec)
    return (
        f"Done — sent to Greg for approval (request {rec['id']}). "
        "You'll hear back here when he decides."
    )


if __name__ == "__main__":
    mcp.run()
