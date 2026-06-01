"""WhatsApp transport for Pedro, via Twilio.

Mirrors the Telegram path: inbound messages arrive on a Twilio webhook, get
routed through the shared `run_pedro` brain, and the reply is sent back out
through Twilio's REST API.

Two design points worth knowing:
- Twilio's webhook times out in ~15s, but a claude turn can take up to
  CLAUDE_TIMEOUT (300s). So we ACK the webhook immediately with empty TwiML and
  send the actual reply asynchronously via the REST API once claude finishes.
- This module never imports bot.py. The brain (`run_pedro`) is injected into
  build_webhook_app(), so there's no circular import.
"""
import asyncio
import base64
import hashlib
import hmac
import logging
import os

import httpx
from aiohttp import web

log = logging.getLogger("nightshift.whatsapp")

ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
# Sandbox sender is whatsapp:+14155238886; a registered sender is your own number.
WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")
# Comma-separated allowlist of senders, e.g. "whatsapp:+17805551234".
ALLOWED_WHATSAPP = {
    x.strip() for x in os.environ.get("ALLOWED_WHATSAPP", "").split(",") if x.strip()
}
# Owner number(s) get the full Pedro: shell/file tools and the shared, persistent
# conversation memory. Every OTHER allowlisted sender is a "guest" and runs in a
# restricted lane (see GUEST_ALLOWED_TOOLS): one-shot, no memory, no shell, no
# file access. If OWNER_WHATSAPP is empty, all allowed senders are treated as the
# owner (backwards-compatible) — so if you allowlist guests, SET THIS.
OWNER_WHATSAPP = {
    x.strip() for x in os.environ.get("OWNER_WHATSAPP", "").split(",") if x.strip()
}
# Tools ALLOWED for guests — an allowlist is the real boundary. A denylist does
# NOT contain an untrusted guest: sub-agents (Agent) ignore --disallowed-tools,
# and the CLI also has Monitor/CronCreate/MCP tools that run commands. Guests get
# only these tools (web research); nothing else exists for the run.
GUEST_ALLOWED_TOOLS = os.environ.get(
    "PEDRO_GUEST_ALLOWED_TOOLS",
    "WebSearch WebFetch",
)
WEBHOOK_PORT = int(os.environ.get("WHATSAPP_WEBHOOK_PORT", "8770"))
WEBHOOK_PATH = os.environ.get("WHATSAPP_WEBHOOK_PATH", "/whatsapp")
# The public https URL the tunnel exposes, e.g. https://abc.trycloudflare.com .
# Required to validate Twilio's request signature. If unset, signature checking
# is skipped and we fall back to the sender allowlist alone (logged loudly).
PUBLIC_BASE_URL = os.environ.get("WHATSAPP_PUBLIC_URL", "").rstrip("/")

# Voice notes: inbound audio is fetched from Twilio and transcribed with Groq
# Whisper (same service the Telegram path uses). If GROQ_API_KEY is unset, voice
# notes get a polite "couldn't transcribe" reply instead.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_TRANSCRIBE_MODEL = os.environ.get("GROQ_TRANSCRIBE_MODEL", "whisper-large-v3-turbo")

TWILIO_MAX_MSG = 1500  # WhatsApp hard limit is ~1600; leave headroom.
_API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def configured() -> bool:
    """True only if we have enough to both receive and send."""
    return bool(ACCOUNT_SID and AUTH_TOKEN and WHATSAPP_FROM)


def _twiml(body: str = "") -> web.Response:
    return web.Response(text=EMPTY_TWIML, content_type="application/xml")


def valid_signature(url: str, params: dict, signature: str) -> bool:
    """Validate Twilio's X-Twilio-Signature.

    Twilio signs: the full request URL, followed by each POST param appended as
    key+value in alphabetical key order; HMAC-SHA1 with the auth token; base64.
    """
    if not (AUTH_TOKEN and signature):
        return False
    data = url
    for key in sorted(params.keys()):
        data += key + params[key]
    digest = hmac.new(
        AUTH_TOKEN.encode("utf-8"), data.encode("utf-8"), hashlib.sha1
    ).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature)


async def send_whatsapp(to: str, body: str, client: httpx.AsyncClient | None = None) -> None:
    """Send a WhatsApp message (chunked) from WHATSAPP_FROM to `to`."""
    if not configured():
        log.error("send_whatsapp called but Twilio is not configured")
        return
    body = body.strip() or "(empty response)"
    chunks = [body[i:i + TWILIO_MAX_MSG] for i in range(0, len(body), TWILIO_MAX_MSG)]
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=30.0)
    try:
        for chunk in chunks:
            resp = await client.post(
                _API.format(sid=ACCOUNT_SID),
                auth=(ACCOUNT_SID, AUTH_TOKEN),
                data={"From": WHATSAPP_FROM, "To": to, "Body": chunk},
            )
            if resp.status_code >= 300:
                log.error("twilio send failed %s: %s", resp.status_code, resp.text[:300])
                return
    finally:
        if own:
            await client.aclose()


async def transcribe_media(media_url: str, content_type: str) -> str | None:
    """Download a Twilio media URL and transcribe it with Groq Whisper.

    Twilio media URLs require Twilio basic-auth and 302-redirect to a temporary
    CDN URL, so we follow redirects (httpx drops the auth header on the cross-host
    hop, which is what we want). Returns the transcript, or None on any failure.
    """
    if not GROQ_API_KEY:
        log.warning("voice note received but GROQ_API_KEY unset; cannot transcribe")
        return None
    # Derive a sensible filename extension from the content type (e.g. audio/ogg).
    ext = (content_type.split("/")[-1].split(";")[0].strip() or "ogg")
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            media = await client.get(media_url, auth=(ACCOUNT_SID, AUTH_TOKEN))
            media.raise_for_status()
            audio_bytes = media.content
            resp = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": (f"voice.{ext}", audio_bytes, content_type or "audio/ogg")},
                data={"model": GROQ_TRANSCRIBE_MODEL},
            )
            resp.raise_for_status()
            return (resp.json().get("text") or "").strip()
    except Exception:  # noqa: BLE001 - any failure → None, caller replies gracefully
        log.exception("whatsapp voice transcription failed")
        return None


def build_webhook_app(run_pedro) -> web.Application:
    """Build the aiohttp app. `run_pedro(prompt, restricted=, disallowed_tools=)
    -> str` is the shared brain. Owners get the full call; guests get the
    restricted lane with GUEST_ALLOWED_TOOLS."""

    async def _process_and_reply(
        to: str, body: str, media_url: str, media_type: str, is_guest: bool
    ) -> None:
        try:
            prompt = body
            if media_url:
                transcript = await transcribe_media(media_url, media_type)
                if not transcript:
                    await send_whatsapp(
                        to,
                        "Sorry — I couldn't transcribe that voice note. "
                        "Try again, or send it as text.",
                    )
                    return
                # Echo what was heard (mirrors the Telegram voice path) so the
                # sender can catch transcription errors.
                await send_whatsapp(to, f"🎙 Heard: {transcript}")
                prompt = f"{body}\n\n{transcript}" if body else transcript
            if not prompt.strip():
                return
            if is_guest:
                out = await run_pedro(
                    prompt, restricted=True,
                    allowed_tools=GUEST_ALLOWED_TOOLS, strict_mcp=True,
                )
            else:
                out = await run_pedro(prompt)
        except Exception as e:  # noqa: BLE001 - surface any failure to the user
            log.exception("whatsapp run_pedro failed")
            out = f"Error: {e}"
        await send_whatsapp(to, out)

    async def handle(request: web.Request) -> web.Response:
        form = await request.post()
        params = {k: str(v) for k, v in form.items()}

        if PUBLIC_BASE_URL:
            sig = request.headers.get("X-Twilio-Signature", "")
            url = PUBLIC_BASE_URL + WEBHOOK_PATH
            if not valid_signature(url, params, sig):
                log.warning("rejected: bad Twilio signature from %s", request.remote)
                return web.Response(status=403, text="bad signature")
        else:
            log.warning(
                "WHATSAPP_PUBLIC_URL unset - signature validation OFF, "
                "relying on sender allowlist only"
            )

        sender = params.get("From", "")
        if ALLOWED_WHATSAPP and sender not in ALLOWED_WHATSAPP:
            log.warning("ignored unauthorized whatsapp sender: %s", sender)
            return _twiml()

        # Owner gets full Pedro; any other allowlisted sender is a restricted
        # guest. If OWNER_WHATSAPP is unset, treat everyone as owner (back-compat).
        is_guest = bool(OWNER_WHATSAPP) and sender not in OWNER_WHATSAPP

        # Voice notes (and other media) arrive as NumMedia + MediaUrl{i}; grab the
        # first audio attachment for transcription.
        try:
            num_media = int(params.get("NumMedia", "0") or "0")
        except ValueError:
            num_media = 0
        media_url = ""
        media_type = ""
        for i in range(num_media):
            ctype = params.get(f"MediaContentType{i}", "")
            if ctype.startswith("audio/"):
                media_url = params.get(f"MediaUrl{i}", "")
                media_type = ctype
                break

        body = (params.get("Body") or "").strip()
        if not body and not media_url:
            return _twiml()

        log.info(
            "whatsapp from %s%s: %s",
            sender,
            " [guest]" if is_guest else "",
            body[:200] if body else f"<audio {media_type}>",
        )
        # Ack now; reply asynchronously so we don't hit Twilio's ~15s timeout.
        asyncio.create_task(_process_and_reply(sender, body, media_url, media_type, is_guest))
        return _twiml()

    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, handle)
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    return app
