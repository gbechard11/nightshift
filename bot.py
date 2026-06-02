import asyncio
import logging
import os
import platform
import re
import secrets
import shutil
import subprocess
from datetime import datetime, timedelta

import httpx
from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import mailer
import meta_ads
import prism
import vapi_call
import whatsapp
import wire
from pedro_brain import PedroError, run_claude

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USERS = {
    int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip()
}
# Owner-only commands (e.g. /wire, which displays banking details) gate on this,
# NOT on ALLOWED_USERS. Defaults to Greg's Telegram id; override via env.
OWNER_ID = int(os.environ.get("OWNER_TELEGRAM_ID", "6575459992"))
CLAUDE_WORKDIR = os.environ.get("CLAUDE_WORKDIR", "/data/greg")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_TRANSCRIBE_MODEL = os.environ.get("GROQ_TRANSCRIBE_MODEL", "whisper-large-v3-turbo")
INBOX_DIR = os.environ.get("PEDRO_INBOX", "/data/greg/inbox")
SESSION_FILE = os.environ.get("PEDRO_SESSION_FILE", "/data/greg/.pedro_session_id")
SAFE_DISALLOWED_TOOLS = os.environ.get(
    "PEDRO_SAFE_DISALLOWED_TOOLS", "Bash Edit Write NotebookEdit"
)
VAPI_API_KEY = os.environ.get("VAPI_API_KEY", "")
VAPI_PHONE_NUMBER_ID = os.environ.get("VAPI_PHONE_NUMBER_ID", "")
TELEGRAM_MAX_MSG = 4000

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("nightshift")

# Confirm-first calling: drafts awaiting a button tap, keyed by a short token.
PENDING_CALLS: dict[str, dict] = {}

# Confirm-first ad launches: PAUSED campaigns awaiting a Launch tap, keyed by token.
# The campaign already exists (PAUSED, no spend); the button only flips it ACTIVE.
PENDING_CAMPAIGNS: dict[str, dict] = {}
PENDING_WIRES: dict[str, dict] = {}

META_NOT_CONFIGURED = (
    "📣 Meta Ads isn't configured yet. Set META_ACCESS_TOKEN (a System User token "
    "with ads_management) and META_AD_ACCOUNT_ID in .env on the VPS, then restart."
)

PRISM_NOT_CONFIGURED = (
    "🎫 Prism isn't connected yet. From a logged-in app.prism.fm browser, copy the "
    "localStorage 'refreshToken', set PRISM_REFRESH_TOKEN in .env on the VPS, then "
    "restart."
)


def authorized(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True
    return bool(update.effective_user and update.effective_user.id in ALLOWED_USERS)


def _is_owner(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == OWNER_ID)


# Serialize the owner's claude runs — there is one shared, persistent session,
# so only one stateful run may proceed at a time. Restricted runs are one-shot
# and stateless, so they stay concurrent (no lock passed).
_claude_lock = asyncio.Lock()


async def run_pedro(
    prompt: str,
    restricted: bool = False,
    disallowed_tools: str | None = None,
    allowed_tools: str | None = None,
    strict_mcp: bool = False,
) -> str:
    """Owner brain shared by Telegram and WhatsApp, over pedro_brain.run_claude.

    Full Pedro runs against one shared, persistent session (serialized by
    _claude_lock) with every tool available. `restricted=True` is the one-shot,
    no-memory lane used by /safe and WhatsApp guests: no session, dangerous tools
    blocked (SAFE_DISALLOWED_TOOLS unless `disallowed_tools` overrides it, e.g.
    the tighter guest set that also blocks Read/Glob/Grep). Raises PedroError on
    timeout / nonzero exit / missing binary.
    """
    if restricted:
        # Untrusted restricted callers (WhatsApp guests) pass an allowlist —
        # the real boundary. The trusted owner /safe convenience may use the
        # denylist (not a boundary, but fine for the owner).
        if allowed_tools is not None:
            return await run_claude(
                prompt, workdir=CLAUDE_WORKDIR,
                allowed_tools=allowed_tools, strict_mcp=strict_mcp,
            )
        blocked = disallowed_tools if disallowed_tools is not None else SAFE_DISALLOWED_TOOLS
        return await run_claude(
            prompt, workdir=CLAUDE_WORKDIR, disallowed_tools=blocked
        )
    return await run_claude(
        prompt, workdir=CLAUDE_WORKDIR, session_file=SESSION_FILE, lock=_claude_lock
    )


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    await update.message.reply_text(
        "Hi, I'm agentpedro — your nightshift assistant.\n\n"
        "Just talk to me normally, or use:\n"
        "/ask <prompt>  - same as plain text\n"
        "/safe <prompt> - read-only, no memory, no shell/file writes\n"
        "/call <number> <objective> - I call someone on your behalf (you approve first)\n"
        "/research <artist> [genre=hip_hop] [similar=A,B] [label=Label] - smart Meta targeting research\n"
        "/draft <name> | <ids> | <$CAD/day> | [objective] | [ticket_link] | [caption] | [flyer.jpg] - build a PAUSED campaign + ad\n"
        "/media - list images available for ads (drop files in /data/greg/ads/)\n"
        "/pause <campaign_id> - stop a campaign's spend\n"
        "/report [id]   - last-7-day ad insights (defaults to the account)\n"
        "/shows [days|all] - upcoming Prism shows (default: next 60 days)\n"
        "/show <event_id>  - details for one Prism show\n"
        "/settlement <event_id> - ticket revenue, taxes, expenses for a show\n"
        "/new           - clear conversation memory, start fresh\n"
        "/status        - VPS health\n"
        "/whoami        - your Telegram user ID\n\n"
        "Voice notes work. PDFs/photos/documents also work — attach with caption."
    )
    if _is_owner(update):
        await update.message.reply_text(
            "Owner tools:\n"
            "/wire <recipient> <amount_usd> - prep an Agility Forex wire "
            "(info only, never sends money)\n"
            "/wire list - known wire recipients"
        )


async def _call_claude(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    prompt: str,
    restricted: bool = False,
) -> None:
    # Fast "busy" feedback for stateful runs (restricted is stateless, never blocks).
    if not restricted and _claude_lock.locked():
        await update.message.reply_text(
            "⏳ Pedro is still working on your previous request. Try again in a moment, "
            "or use /safe <prompt> for an independent one-shot."
        )
        return

    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    log.info(
        "claude%s from %s: %s",
        " [safe]" if restricted else "",
        update.effective_user.id,
        prompt[:200],
    )
    try:
        out = await run_pedro(prompt, restricted=restricted)
    except PedroError as e:
        await update.message.reply_text(str(e))
        return

    for i in range(0, len(out), TELEGRAM_MAX_MSG):
        await update.message.reply_text(out[i:i + TELEGRAM_MAX_MSG])


async def cmd_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ALLOWED_USERS:
        await update.message.reply_text(
            "/ask is disabled until ALLOWED_USERS is configured on the server."
        )
        return
    if not authorized(update):
        return
    prompt = " ".join(ctx.args).strip() if ctx.args else ""
    if not prompt:
        await update.message.reply_text("Usage: /ask <your prompt>")
        return
    await _call_claude(update, ctx, prompt)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ALLOWED_USERS or not authorized(update):
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    await _call_claude(update, ctx, text)


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    try:
        os.remove(SESSION_FILE)
        await update.message.reply_text("🧹 Fresh conversation. Previous context cleared.")
    except FileNotFoundError:
        await update.message.reply_text("Already a fresh conversation.")


async def cmd_safe(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ALLOWED_USERS:
        await update.message.reply_text(
            "/safe is disabled until ALLOWED_USERS is configured on the server."
        )
        return
    if not authorized(update):
        return
    prompt = " ".join(ctx.args).strip() if ctx.args else ""
    if not prompt:
        await update.message.reply_text(
            "Usage: /safe <prompt>\n"
            "Runs claude with Bash/Edit/Write/NotebookEdit blocked. One-shot, no memory."
        )
        return
    await _call_claude(update, ctx, prompt, restricted=True)


async def cmd_call(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    if not (VAPI_API_KEY and VAPI_PHONE_NUMBER_ID):
        await update.message.reply_text(
            "📞 Calling isn't configured. Set VAPI_API_KEY and VAPI_PHONE_NUMBER_ID in .env."
        )
        return
    args = ctx.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /call <number> <objective>\n"
            "Example: /call +17805551234 Ask if they can deliver the PA system Saturday "
            "and get a quote."
        )
        return
    number = args[0]
    objective = " ".join(args[1:]).strip()
    if not vapi_call.E164.match(number):
        await update.message.reply_text(
            f"That number isn't E.164 format. Use e.g. +17805551234 (got: {number})."
        )
        return

    token = secrets.token_urlsafe(8)
    PENDING_CALLS[token] = {"to": number, "objective": objective}
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("📞 Call now", callback_data=f"call:go:{token}"),
            InlineKeyboardButton("✏️ Edit", callback_data=f"call:edit:{token}"),
            InlineKeyboardButton("✖️ Cancel", callback_data=f"call:cancel:{token}"),
        ]]
    )
    await update.message.reply_text(
        "📋 Ready to call:\n\n"
        f"To: {number}\n"
        f"Objective: {objective}\n\n"
        "Pedro will open with:\n"
        f"“{vapi_call.first_message()}”\n\n"
        "Place the call?",
        reply_markup=keyboard,
    )


async def on_call_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not authorized(update):
        return
    try:
        _, action, token = query.data.split(":", 2)
    except ValueError:
        return

    draft = PENDING_CALLS.get(token)
    if not draft:
        await query.edit_message_text("This call request expired. Send /call again.")
        return

    if action == "cancel":
        PENDING_CALLS.pop(token, None)
        await query.edit_message_text("✖️ Call cancelled.")
        return

    if action == "edit":
        PENDING_CALLS.pop(token, None)
        await query.edit_message_text(
            "✏️ Re-send with your changes:\n"
            f"/call {draft['to']} {draft['objective']}"
        )
        return

    if action != "go":
        return

    PENDING_CALLS.pop(token, None)
    await query.edit_message_text(f"📞 Dialing {draft['to']}…")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            call = await vapi_call.place_call(
                draft["to"], draft["objective"], client=client
            )
            call_id = call.get("id")
            if not call_id:
                await query.message.reply_text(f"Vapi returned no call id:\n{call}")
                return

            last = {"status": None}

            async def tick(status):
                if status != last["status"]:
                    last["status"] = status
                    await ctx.bot.send_chat_action(
                        query.message.chat_id, ChatAction.TYPING
                    )

            result = await vapi_call.wait_for_call(call_id, on_tick=tick, client=client)
        await query.message.reply_text("📞 " + vapi_call.format_result(result))
    except httpx.HTTPStatusError as e:
        await query.message.reply_text(
            f"Call failed ({e.response.status_code}): {e.response.text[:500]}"
        )
    except Exception as e:  # noqa: BLE001 - surface any failure to the user
        log.exception("call failed")
        await query.message.reply_text(f"Call error: {e}")


def _format_reach(est: dict | None) -> str:
    """Best-effort one-liner from a reachestimate response (shape varies by API
    version). Returns '' if there's nothing usable."""
    if not isinstance(est, dict):
        return ""
    users = est.get("users") or est.get("estimate_mau") or est.get("estimate_dau")
    if isinstance(users, (int, float)):
        return f"Est. audience: ~{int(users):,}"
    return ""


async def cmd_research(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Read-only: search Meta targeting interests for an artist/genre/topic."""
    if not authorized(update):
        return
    if not meta_ads.configured():
        await update.message.reply_text(META_NOT_CONFIGURED)
        return
    query = " ".join(ctx.args).strip() if ctx.args else ""
    if not query:
        await update.message.reply_text(
            "Usage: /research <artist> [genre=<genre>] [similar=artist1,artist2] [label=<label>]\n"
            "Example: /research Drake genre=hip_hop similar=Future,Travis_Scott label=OVO_Sound\n"
            "Simple: /research Drake\n"
            "(use _ for spaces inside a value, e.g. genre=hip_hop)"
        )
        return

    # Parse optional keyword args: genre=..., similar=..., label=... (underscores -> spaces).
    artist_tokens = []
    genre = None
    similar_artists = []
    label = None
    for tok in query.split():
        if tok.startswith("genre="):
            genre = tok[len("genre="):].replace("_", " ")
        elif tok.startswith("similar="):
            similar_artists = [
                s.strip().replace("_", " ")
                for s in tok[len("similar="):].split(",")
                if s.strip()
            ]
        elif tok.startswith("label="):
            label = tok[len("label="):].replace("_", " ")
        else:
            artist_tokens.append(tok)
    artist_name = " ".join(artist_tokens).strip() or query

    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            result = await meta_ads.research_artist_targeting(
                client,
                artist_name=artist_name,
                genre=genre,
                similar_artists=similar_artists or None,
                label=label,
            )
    except meta_ads.MetaError as e:
        await update.message.reply_text(f"Research failed: {e}")
        return

    all_ids = result["all_ids"]
    if not all_ids:
        await update.message.reply_text(
            f'No targeting interests found for "{artist_name}".\n'
            "Try adding genre= or similar= to broaden the search."
        )
        return

    summary = result["summary"]
    if len(summary) > 3800:  # Telegram hard-caps at 4096
        summary = summary[:3800] + "\n…(truncated)"
    ids_csv = ",".join(all_ids)
    summary += (
        f"\n\nTo draft a campaign with all {len(all_ids)} interests:\n"
        f"/draft {artist_name} fans | {ids_csv} | 20"
    )
    await update.message.reply_text(summary)


async def cmd_draft(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a PAUSED campaign + ad set (no spend) and offer a Launch button.

    The campaign is built PAUSED in meta_ads; only the inline Launch button flips
    it ACTIVE (via on_campaign_button → activate_campaign), so spend always needs
    an explicit per-campaign tap.
    """
    if not authorized(update):
        return
    if not meta_ads.configured():
        await update.message.reply_text(META_NOT_CONFIGURED)
        return
    raw = (update.message.text or "").partition(" ")[2].strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 3 or not parts[0]:
        await update.message.reply_text(
            "Usage: /draft <name> | <interest_ids csv> | <daily $CAD> | [objective] | [ticket_link] | [caption] | [image_url]\n"
            "Example: /draft Drake fans YEG | 6003123456789 | 20 | OUTCOME_TRAFFIC | https://showpass.com/event | Get tickets before they sell out!\n"
            "Get interest ids from /research. Objective defaults to OUTCOME_TRAFFIC.\n"
            "ticket_link, caption, image_url are optional — include them to create the full ad creative."
        )
        return
    name = parts[0]
    interest_ids = [x.strip() for x in parts[1].split(",") if x.strip()]
    try:
        daily_cad = float(parts[2])
    except ValueError:
        await update.message.reply_text(f"Daily budget must be a number in CAD. Got: {parts[2]}")
        return
    if daily_cad <= 0:
        await update.message.reply_text("Daily budget must be greater than 0.")
        return
    objective = parts[3] if len(parts) > 3 and parts[3] else "OUTCOME_TRAFFIC"
    ticket_link = parts[4] if len(parts) > 4 and parts[4] else None
    caption = parts[5] if len(parts) > 5 and parts[5] else None
    image_url = parts[6] if len(parts) > 6 and parts[6] else None
    daily_cents = int(round(daily_cad * 100))
    targeting = meta_ads.build_targeting(interest_ids)

    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    creative_id = None
    ad_id = None
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            camp = await meta_ads.create_campaign(client, name, objective=objective)
            campaign_id = camp.get("id")
            if not campaign_id:
                await update.message.reply_text(f"Meta returned no campaign id:\n{camp}")
                return
            adset = await meta_ads.create_adset(
                client, campaign_id, f"{name} — ad set", daily_cents, targeting
            )
            adset_id = adset.get("id")
            creative_error = None
            image_hash = None
            image_label = None
            if ticket_link and caption and adset_id:
                try:
                    # If image_url looks like a filename (not http), treat it as a
                    # local media file — resolve it, upload to Meta, use the hash.
                    if image_url and not image_url.startswith("http"):
                        file_path = meta_ads.resolve_media_path(image_url)
                        image_hash = await meta_ads.upload_ad_image(client, file_path)
                        image_label = image_url  # show the original filename in confirmation
                        image_url = None  # clear so create_adcreative uses hash path
                    creative = await meta_ads.create_adcreative(
                        client, f"{name} — creative", ticket_link, caption,
                        image_hash=image_hash, image_url=image_url,
                    )
                    creative_id = creative.get("id")
                    if creative_id:
                        ad = await meta_ads.create_ad(
                            client, adset_id, f"{name} — ad", creative_id
                        )
                        ad_id = ad.get("id")
                except meta_ads.MetaError as ce:
                    creative_error = str(ce)
            try:
                est = await meta_ads.reach_estimate(client, targeting)
            except meta_ads.MetaError:
                est = None
    except meta_ads.MetaError as e:
        await update.message.reply_text(f"Draft failed (nothing was launched): {e}")
        return

    token = secrets.token_urlsafe(8)
    PENDING_CAMPAIGNS[token] = {
        "campaign_id": campaign_id,
        "name": name,
        "daily_cad": daily_cad,
    }
    reach_line = _format_reach(est)
    creative_lines = ""
    if creative_id:
        creative_lines = (
            f"Creative id: {creative_id}\n"
            f"Ad id: {ad_id or 'n/a'}\n"
            f"Ticket link: {ticket_link}\n"
            f"Caption: {caption}\n"
            + (f"Image: {image_label or image_url} {'(uploaded)' if image_hash else ''}\n" if (image_label or image_url) else "Image: (OG preview from link)\n")
        )
    elif creative_error:
        creative_lines = f"Creative: FAILED — {creative_error}\nCampaign + ad set still created. Fix and re-add creative manually.\n"
    else:
        creative_lines = "Creative: none (add ticket_link + caption to /draft to create one)\n"
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🚀 Launch (start spend)", callback_data=f"camp:go:{token}"),
            InlineKeyboardButton("✖️ Keep paused", callback_data=f"camp:hold:{token}"),
        ]]
    )
    await update.message.reply_text(
        "📋 Campaign drafted — PAUSED, not spending:\n\n"
        f"Name: {name}\n"
        f"Campaign id: {campaign_id}\n"
        f"Objective: {objective}\n"
        f"Daily budget: ${daily_cad:.2f} CAD\n"
        f"Interests: {', '.join(interest_ids) or '(none — broad)'}\n"
        f"Geo: Canada\n"
        f"{creative_lines}"
        + (f"{reach_line}\n" if reach_line else "")
        + "\nLaunching starts real spend on the Nightshift CAD account. Launch now?",
        reply_markup=keyboard,
    )


async def on_campaign_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """The spend gate. 'go' flips a PAUSED campaign ACTIVE; 'hold' leaves it paused."""
    query = update.callback_query
    await query.answer()
    if not authorized(update):
        return
    try:
        _, action, token = query.data.split(":", 2)
    except ValueError:
        return

    draft = PENDING_CAMPAIGNS.get(token)
    if not draft:
        await query.edit_message_text(
            "This draft expired. The campaign is still PAUSED and safe. Re-draft with /draft."
        )
        return

    if action == "hold":
        PENDING_CAMPAIGNS.pop(token, None)
        await query.edit_message_text(
            f"✋ Kept PAUSED. Campaign {draft['campaign_id']} is not spending."
        )
        return

    if action != "go":
        return

    PENDING_CAMPAIGNS.pop(token, None)
    await query.edit_message_text(f"🚀 Launching campaign {draft['campaign_id']}…")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await meta_ads.activate_campaign(client, draft["campaign_id"])
    except meta_ads.MetaError as e:
        await query.message.reply_text(f"Launch failed (campaign stays paused): {e}")
        return
    except Exception as e:  # noqa: BLE001 - surface any failure to the user
        log.exception("campaign activate failed")
        await query.message.reply_text(f"Launch error (campaign stays paused): {e}")
        return
    await query.message.reply_text(
        f"✅ Campaign {draft['campaign_id']} is ACTIVE, spending up to "
        f"${draft['daily_cad']:.2f} CAD/day.\n"
        f"Pause anytime with /pause {draft['campaign_id']}."
    )


async def cmd_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """List media files available in the local ads folder for use in /draft."""
    if not authorized(update):
        return
    files = meta_ads.list_media()
    if not files:
        await update.message.reply_text(
            f"No media files found in {meta_ads.MEDIA_DIR}.\n"
            f"Drop image files there (SCP/SFTP), then reference them by filename in /draft:\n"
            f"/draft ... | ticket_link | caption | flyer.jpg"
        )
        return
    lines = "\n".join(f"  • {f}" for f in files)
    await update.message.reply_text(
        f"Media files in {meta_ads.MEDIA_DIR}:\n{lines}\n\n"
        f"Use a filename as the last arg in /draft:\n"
        f"/draft ... | https://nightshiftent.ca | Caption here | {files[0]}"
    )


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Pause a campaign — always safe, stops spend immediately."""
    if not authorized(update):
        return
    if not meta_ads.configured():
        await update.message.reply_text(META_NOT_CONFIGURED)
        return
    cid = (ctx.args[0] if ctx.args else "").strip()
    if not cid:
        await update.message.reply_text("Usage: /pause <campaign_id>")
        return
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await meta_ads.pause_campaign(client, cid)
    except meta_ads.MetaError as e:
        await update.message.reply_text(f"Pause failed: {e}")
        return
    await update.message.reply_text(f"⏸ Campaign {cid} paused — spend stopped.")


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Read-only insights for a campaign/adset/ad/account (last 7 days)."""
    if not authorized(update):
        return
    if not meta_ads.configured():
        await update.message.reply_text(META_NOT_CONFIGURED)
        return
    obj = (ctx.args[0] if ctx.args else meta_ads.AD_ACCOUNT_ID).strip()
    if not obj:
        await update.message.reply_text(
            "Usage: /report <campaign_id|account_id>\n"
            "(defaults to META_AD_ACCOUNT_ID once that's set)"
        )
        return
    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            rows = await meta_ads.get_insights(client, obj)
    except meta_ads.MetaError as e:
        await update.message.reply_text(f"Report failed: {e}")
        return
    if not rows:
        await update.message.reply_text(f"No insights for {obj} (last 7 days).")
        return
    lines = [f"📊 Insights for {obj} (last 7 days):\n"]
    for r in rows:
        lines.append(
            f"• {r.get('campaign_name', obj)}: "
            f"reach {r.get('reach', '?')}, impr {r.get('impressions', '?')}, "
            f"clicks {r.get('clicks', '?')}, CTR {r.get('ctr', '?')}, "
            f"spend ${r.get('spend', '?')}"
        )
    report_text = "\n".join(lines)
    await update.message.reply_text(report_text)

    # Email a copy to the report recipients (Seba) so he's always looped in.
    recipients = meta_ads.REPORT_RECIPIENTS
    if not recipients:
        return
    if not mailer.configured():
        await update.message.reply_text(
            "📧 (Email not set up yet — set SMTP_HOST/SMTP_USER/SMTP_PASSWORD in .env "
            f"to auto-send these to {', '.join(recipients)}.)"
        )
        return
    try:
        await asyncio.to_thread(
            mailer.send,
            f"Nightshift Ads report — {obj} (last 7 days)",
            report_text,
            recipients,
        )
        await update.message.reply_text(f"📧 Report emailed to {', '.join(recipients)}.")
    except mailer.MailError as e:
        await update.message.reply_text(f"⚠️ Report shown above but email failed: {e}")


async def cmd_shows(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Read-only: list upcoming Prism shows. /shows [days|all]."""
    if not authorized(update):
        return
    if not prism.configured():
        await update.message.reply_text(PRISM_NOT_CONFIGURED)
        return
    arg = (ctx.args[0].lower() if ctx.args else "").strip()
    today = datetime.now().date()
    if arg == "all":
        start, end = today, today + timedelta(days=365 * 2)
    else:
        try:
            days = int(arg) if arg else 60
        except ValueError:
            await update.message.reply_text("Usage: /shows [days|all]  e.g. /shows 30")
            return
        start, end = today, today + timedelta(days=max(1, days))

    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            shows = await prism.list_shows(client, start.isoformat(), end.isoformat())
    except prism.PrismError as e:
        await update.message.reply_text(f"Prism lookup failed: {e}")
        return

    header = f"🎫 Shows {start.isoformat()} → {end.isoformat()} ({len(shows)} found):\n\n"
    body = prism.format_shows(shows)
    text = header + body
    for i in range(0, len(text), TELEGRAM_MAX_MSG):
        await update.message.reply_text(text[i:i + TELEGRAM_MAX_MSG])


async def cmd_show(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Read-only: details for a single Prism show by event id."""
    if not authorized(update):
        return
    if not prism.configured():
        await update.message.reply_text(PRISM_NOT_CONFIGURED)
        return
    eid = (ctx.args[0] if ctx.args else "").strip()
    if not eid:
        await update.message.reply_text("Usage: /show <event_id>  (get ids from /shows)")
        return
    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            s = await prism.get_show(client, eid)
    except prism.PrismError as e:
        await update.message.reply_text(f"Prism lookup failed: {e}")
        return
    if not s:
        await update.message.reply_text(f"No Prism show found with id {eid}.")
        return

    when = s["start"] or "?"
    if s.get("end") and s["end"] != s["start"]:
        when += f" → {s['end']}"
    times = ""
    if s.get("start_time") and not s.get("all_day"):
        times = f"\nTime: {s['start_time']}" + (f"–{s['end_time']}" if s.get("end_time") else "")
    genres = ", ".join(s["genres"]) if s.get("genres") else "—"
    await update.message.reply_text(
        f"🎫 {s['title']}  (#{s['event_id']})\n"
        f"Status: {s['status_label']}\n"
        f"Date: {when}{times}\n"
        f"Venue: {s.get('venue') or '—'}\n"
        f"Stage: {s.get('stage') or '—'}\n"
        f"Genres: {genres}\n"
        f"{'Matinee' if s.get('is_matinee') else ''}\n"
        f"\nOpen in Prism: https://app.prism.fm/event/{s['event_id']}/dashboard"
    )


async def cmd_settlement(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Read-only: ticket revenue, taxes, net gross and expense lines for a show."""
    if not authorized(update):
        return
    if not prism.configured():
        await update.message.reply_text(PRISM_NOT_CONFIGURED)
        return
    eid = (ctx.args[0] if ctx.args else "").strip()
    if not eid:
        await update.message.reply_text(
            "Usage: /settlement <event_id>  (get ids from /shows)"
        )
        return
    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            s = await prism.get_settlement(client, eid)
    except prism.PrismError as e:
        await update.message.reply_text(f"Settlement lookup failed: {e}")
        return
    text = prism.format_settlement(s, eid)
    for i in range(0, len(text), TELEGRAM_MAX_MSG):
        await update.message.reply_text(text[i:i + TELEGRAM_MAX_MSG])


WIRE_NOT_CONFIGURED = (
    "💸 No wire recipients are set up yet. "
    "Add them to wire_recipients.json on the VPS."
)


async def cmd_wire(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # Owner-only: this surfaces banking details. Never moves money.
    if not _is_owner(update):
        return
    args = ctx.args or []
    if not args or args[0].lower() == "list":
        names = wire.list_recipients()
        if not names:
            await update.message.reply_text(WIRE_NOT_CONFIGURED)
            return
        await update.message.reply_text(
            "💸 Known wire recipients:\n  "
            + "\n  ".join(names)
            + "\n\nUsage: /wire <recipient> <amount_usd>"
        )
        return
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /wire <recipient> <amount_usd>\n"
            "Example: /wire dgi 5000\n"
            "Or: /wire list"
        )
        return
    raw_amount = args[-1].replace("$", "").replace(",", "")
    recipient_query = " ".join(args[:-1]).strip()
    try:
        amount_usd = float(raw_amount)
        if amount_usd <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            f"Couldn't read an amount from \u201c{args[-1]}\u201d. "
            "Put the USD amount last, e.g. /wire dgi 5000."
        )
        return
    rec = wire.find_recipient(recipient_query)
    if not rec:
        known = ", ".join(wire.list_recipients()) or "(none)"
        await update.message.reply_text(
            f"No recipient matches \u201c{recipient_query}\u201d.\nKnown: {known}"
        )
        return
    token = secrets.token_urlsafe(8)
    PENDING_WIRES[token] = {"recipient": recipient_query, "amount_usd": amount_usd}
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "✅ Show banking details", callback_data=f"wire:show:{token}"
            ),
            InlineKeyboardButton(
                "✖️ Cancel", callback_data=f"wire:cancel:{token}"
            ),
        ]]
    )
    await update.message.reply_text(
        "💸 Prepare wire \u2014 reveal banking details?\n\n"
        f"Recipient: {rec['name']}\n"
        f"Amount: ${amount_usd:,.2f} USD\n\n"
        "This will display full bank / account / SWIFT details so you can enter "
        "the wire in Agility Forex. Pedro never moves money \u2014 you book it "
        "yourself.",
        reply_markup=keyboard,
    )


async def on_wire_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_owner(update):
        return
    try:
        _, action, token = query.data.split(":", 2)
    except ValueError:
        return
    pending = PENDING_WIRES.get(token)
    if not pending:
        await query.edit_message_text("This wire prep expired. Send /wire again.")
        return
    if action == "cancel":
        PENDING_WIRES.pop(token, None)
        await query.edit_message_text("✖️ Wire prep cancelled.")
        return
    if action != "show":
        return
    PENDING_WIRES.pop(token, None)
    await query.edit_message_text("💸 Preparing wire details\u2026")
    try:
        result = await asyncio.to_thread(
            wire.prep_wire, pending["recipient"], pending["amount_usd"]
        )
    except Exception as e:  # noqa: BLE001 - surface any prep failure to the owner
        log.exception("wire prep failed")
        await query.message.reply_text(f"Wire prep failed: {e}")
        return
    summary = result["summary"]
    for i in range(0, len(summary), TELEGRAM_MAX_MSG):
        chunk = summary[i:i + TELEGRAM_MAX_MSG]
        try:
            await query.message.reply_text(chunk, parse_mode="Markdown")
        except Exception:  # noqa: BLE001 - fall back to plain text on markdown errors
            await query.message.reply_text(chunk)


async def on_attachment(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ALLOWED_USERS or not authorized(update):
        return
    msg = update.message
    file_id = None
    filename = None
    if msg.document:
        file_id = msg.document.file_id
        filename = msg.document.file_name or f"{file_id}.bin"
    elif msg.photo:
        largest = msg.photo[-1]
        file_id = largest.file_id
        filename = f"photo-{file_id}.jpg"
    if not file_id:
        return

    await ctx.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
    os.makedirs(INBOX_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = re.sub(r"[^\w.\-]", "_", filename)
    path = os.path.join(INBOX_DIR, f"{stamp}-{safe_name}")

    tg_file = await ctx.bot.get_file(file_id)
    await tg_file.download_to_drive(path)
    log.info("attachment from %s saved to %s", update.effective_user.id, path)

    caption = (msg.caption or "").strip()
    prompt = f"I just sent you a file. It is on disk at `{path}`. "
    if caption:
        prompt += f'My caption: "{caption}". '
    prompt += "Please open it (Read tool handles PDFs and images natively) and respond."
    await _call_claude(update, ctx, prompt)


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ALLOWED_USERS or not authorized(update):
        return
    if not GROQ_API_KEY:
        await update.message.reply_text(
            "Voice transcription disabled — GROQ_API_KEY not set in .env."
        )
        return

    voice = update.message.voice or update.message.audio
    if not voice:
        return

    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    tg_file = await ctx.bot.get_file(voice.file_id)
    audio_bytes = bytes(await tg_file.download_as_bytearray())

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": ("voice.ogg", audio_bytes, "audio/ogg")},
                data={"model": GROQ_TRANSCRIBE_MODEL},
            )
            resp.raise_for_status()
            text = (resp.json().get("text") or "").strip()
    except httpx.HTTPStatusError as e:
        await update.message.reply_text(
            f"Groq transcription failed ({e.response.status_code}): "
            f"{e.response.text[:300]}"
        )
        return
    except Exception as e:
        log.exception("voice transcription failed")
        await update.message.reply_text(f"Transcription error: {e}")
        return

    if not text:
        await update.message.reply_text("(empty transcription)")
        return

    log.info("voice from %s: %s", update.effective_user.id, text[:200])
    await update.message.reply_text(f"🎙 Heard: {text}")
    await _call_claude(update, ctx, text)


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    await update.message.reply_text(
        f"User ID: {u.id}\nUsername: @{u.username}\nName: {u.full_name}"
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    uptime = subprocess.check_output(["uptime", "-p"], text=True).strip()
    disk = shutil.disk_usage("/")
    disk_pct = disk.used / disk.total * 100

    meminfo: dict[str, int] = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, rest = line.partition(":")
            meminfo[k.strip()] = int(rest.split()[0]) * 1024
    mem_total = meminfo["MemTotal"]
    mem_avail = meminfo.get("MemAvailable", meminfo["MemFree"])
    mem_used_pct = (mem_total - mem_avail) / mem_total * 100

    wa = "on" if whatsapp.configured() else "off"
    await update.message.reply_text(
        f"Host: {platform.node()}\n"
        f"Uptime: {uptime}\n"
        f"Disk /: {disk_pct:.1f}% used "
        f"({disk.free / 1e9:.1f} GB free of {disk.total / 1e9:.1f} GB)\n"
        f"Memory: {mem_used_pct:.1f}% used "
        f"({(mem_total - mem_avail) / 1e9:.2f} / {mem_total / 1e9:.2f} GB)\n"
        f"WhatsApp: {wa}\n"
        f"Now: {datetime.now().isoformat(timespec='seconds')}"
    )


async def _post_init(application: Application) -> None:
    """Start the Twilio WhatsApp webhook server on the bot's event loop,
    alongside Telegram long-polling. No-op if Twilio isn't configured."""
    if not whatsapp.configured():
        log.info("WhatsApp not configured (Twilio env vars unset) — webhook skipped")
        return
    runner = web.AppRunner(whatsapp.build_webhook_app(run_pedro))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", whatsapp.WEBHOOK_PORT)
    await site.start()
    application.bot_data["_wh_runner"] = runner
    log.info(
        "WhatsApp webhook listening on 127.0.0.1:%s%s",
        whatsapp.WEBHOOK_PORT,
        whatsapp.WEBHOOK_PATH,
    )


async def _post_shutdown(application: Application) -> None:
    runner = application.bot_data.get("_wh_runner")
    if runner is not None:
        await runner.cleanup()


def main() -> None:
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("safe", cmd_safe))
    app.add_handler(CommandHandler("call", cmd_call))
    app.add_handler(CommandHandler("research", cmd_research))
    app.add_handler(CommandHandler("draft", cmd_draft))
    app.add_handler(CommandHandler("media", cmd_media))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("shows", cmd_shows))
    app.add_handler(CommandHandler("show", cmd_show))
    app.add_handler(CommandHandler("settlement", cmd_settlement))
    app.add_handler(CommandHandler("wire", cmd_wire))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CallbackQueryHandler(on_call_button, pattern=r"^call:"))
    app.add_handler(CallbackQueryHandler(on_campaign_button, pattern=r"^camp:"))
    app.add_handler(CallbackQueryHandler(on_wire_button, pattern=r"^wire:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_attachment))
    log.info("Starting agentpedro bot")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
