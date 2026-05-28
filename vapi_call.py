#!/usr/bin/env python3
"""Place an outbound phone call via Vapi and wait for the result.

Used two ways:
  - imported by bot.py for the confirm-first /call Telegram flow
  - run directly for testing:
        python vapi_call.py --to +17805551234 --objective "Ask if Saturday works"

Required env: VAPI_API_KEY, VAPI_PHONE_NUMBER_ID
Optional env: VAPI_OWNER_NAME, VAPI_OWNER_COMPANY, VAPI_CALLBACK_NUMBER,
              VAPI_MODEL_PROVIDER, VAPI_MODEL, VAPI_POLL_INTERVAL, VAPI_CALL_TIMEOUT
"""
import argparse
import asyncio
import os
import re

import httpx

VAPI_BASE = "https://api.vapi.ai"

OWNER_NAME = os.environ.get("VAPI_OWNER_NAME", "Greg")
OWNER_COMPANY = os.environ.get("VAPI_OWNER_COMPANY", "Nightshift Entertainment")
CALLBACK_NUMBER = os.environ.get("VAPI_CALLBACK_NUMBER", "")
MODEL_PROVIDER = os.environ.get("VAPI_MODEL_PROVIDER", "openai")
MODEL_NAME = os.environ.get("VAPI_MODEL", "gpt-4o")

POLL_INTERVAL_S = float(os.environ.get("VAPI_POLL_INTERVAL", "5"))
CALL_TIMEOUT_S = float(os.environ.get("VAPI_CALL_TIMEOUT", "600"))

# E.164: a leading + and 10-15 digits, e.g. +17805551234
E164 = re.compile(r"^\+\d{10,15}$")

# Vapi call status values that mean the call is over.
TERMINAL_STATUSES = {"ended"}


def system_prompt(objective: str) -> str:
    callback = (
        f" If they ask for a callback number, give: {CALLBACK_NUMBER}."
        if CALLBACK_NUMBER
        else ""
    )
    return (
        f"You are a polite, concise voice assistant placing a phone call on behalf of "
        f"{OWNER_NAME} at {OWNER_COMPANY}. You are {OWNER_NAME}'s assistant — never claim to be a "
        f"human or to be {OWNER_NAME}. If asked who you are, say you are {OWNER_NAME}'s assistant.\n\n"
        f"Your objective for THIS call:\n{objective}\n\n"
        "Guidelines:\n"
        "- Speak naturally and briefly; let the other person talk.\n"
        "- Stay on objective and gather the specific information or outcome requested.\n"
        "- If you reach voicemail, leave a short message: who you're calling for, the "
        f"purpose, and a callback request.{callback}\n"
        "- Do NOT make commitments, payments, bookings, or legal agreements on "
        f"{OWNER_NAME}'s behalf — only gather information and relay messages.\n"
        "- When the objective is met or the conversation is clearly done, thank them and "
        "end the call."
    )


def first_message(purpose: str | None = None) -> str:
    intro = (
        f"Hi, this is {OWNER_NAME}'s assistant calling from {OWNER_COMPANY}."
    )
    if purpose:
        intro += f" {purpose}"
    return intro


def build_assistant(objective: str, purpose: str | None = None) -> dict:
    # Minimal, well-formed transient assistant. Voice/transcriber are intentionally
    # omitted so Vapi applies account defaults (no extra provider keys needed to get a
    # first call working). Structured-outcome extraction can be layered on later once
    # we've confirmed the live end-of-call response shape.
    return {
        "firstMessage": first_message(purpose),
        "model": {
            "provider": MODEL_PROVIDER,
            "model": MODEL_NAME,
            "messages": [{"role": "system", "content": system_prompt(objective)}],
        },
    }


async def place_call(
    to_number: str,
    objective: str,
    *,
    purpose: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """POST an outbound call. Returns the created call object (has 'id', 'status')."""
    if not E164.match(to_number):
        raise ValueError(f"Number must be E.164 like +17805551234, got: {to_number!r}")
    api_key = os.environ["VAPI_API_KEY"]
    phone_number_id = os.environ["VAPI_PHONE_NUMBER_ID"]
    payload = {
        "phoneNumberId": phone_number_id,
        "customer": {"number": to_number},
        "assistant": build_assistant(objective, purpose),
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    try:
        r = await client.post(f"{VAPI_BASE}/call", headers=headers, json=payload)
        r.raise_for_status()
        return r.json()
    finally:
        if own:
            await client.aclose()


async def get_call(call_id: str, *, client: httpx.AsyncClient | None = None) -> dict:
    api_key = os.environ["VAPI_API_KEY"]
    headers = {"Authorization": f"Bearer {api_key}"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    try:
        r = await client.get(f"{VAPI_BASE}/call/{call_id}", headers=headers)
        r.raise_for_status()
        return r.json()
    finally:
        if own:
            await client.aclose()


async def wait_for_call(
    call_id: str,
    *,
    on_tick=None,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Poll until the call ends or CALL_TIMEOUT_S elapses. Returns the last call object.

    on_tick(status) is awaited each poll so callers can show progress.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + CALL_TIMEOUT_S
    call: dict = {}
    while True:
        call = await get_call(call_id, client=client)
        status = call.get("status")
        if status in TERMINAL_STATUSES:
            return call
        if loop.time() > deadline:
            return call
        if on_tick is not None:
            await on_tick(status)
        await asyncio.sleep(POLL_INTERVAL_S)


def format_result(call: dict) -> str:
    status = call.get("status", "unknown")
    ended = call.get("endedReason", "")
    analysis = call.get("analysis") or {}
    summary = (analysis.get("summary") or "").strip()
    transcript = (call.get("transcript") or "").strip()
    artifact = call.get("artifact") or {}
    recording = call.get("recordingUrl") or artifact.get("recordingUrl") or ""

    header = f"Call {status}" + (f" — {ended}" if ended else "")
    lines = [header]
    if summary:
        lines += ["", "Summary:", summary]
    elif transcript:
        lines += ["", "Transcript:", transcript[:1500]]
    if recording:
        lines += ["", f"Recording: {recording}"]
    return "\n".join(lines)


async def _amain() -> None:
    ap = argparse.ArgumentParser(description="Place an outbound Vapi call.")
    ap.add_argument("--to", required=True, help="E.164 number, e.g. +17805551234")
    ap.add_argument("--objective", required=True, help="What the call should accomplish")
    ap.add_argument("--purpose", default=None, help="Optional one-line purpose for the opener")
    ap.add_argument("--no-wait", action="store_true", help="Dispatch and exit without polling")
    args = ap.parse_args()

    async with httpx.AsyncClient(timeout=30.0) as client:
        call = await place_call(args.to, args.objective, purpose=args.purpose, client=client)
        call_id = call.get("id")
        print(f"call_id={call_id} status={call.get('status')}")
        if args.no_wait or not call_id:
            return

        async def tick(status):
            print(f"  ...{status}")

        result = await wait_for_call(call_id, on_tick=tick, client=client)
        print(format_result(result))


if __name__ == "__main__":
    asyncio.run(_amain())
