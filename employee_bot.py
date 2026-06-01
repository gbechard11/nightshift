"""Separate Telegram bot for Nightshift employees — a locked-down Pedro.

This is a deliberately restricted front-end to the same claude brain
(pedro_brain.run_claude), kept entirely separate from Greg's owner Pedro:

- Its OWN bot token (EMPLOYEE_BOT_TOKEN) and OWN allowlist (EMPLOYEE_USERS), so
  employees never see or touch the owner bot, its session, or its context.
- Every run is locked to an ALLOWLIST of tools (--tools), plus --strict-mcp-config.
  Only WebSearch/WebFetch exist for the run; there is no Bash/Read/Agent/Monitor/
  Cron/MCP, so an employee prompt cannot read .env, run commands, spawn an
  unrestricted sub-agent, or otherwise reach the VPS. An allowlist is essential
  here: a denylist is unsafe because the CLI ships many command-capable tools
  (Monitor, CronCreate, Agent sub-agents that ignore the denylist, MCP servers)
  that a blocked prompt can pivot through. Enforced by the harness, not by
  instructions to the model.
- Each employee gets their OWN persistent session keyed by Telegram id, so
  context carries across their messages but is isolated from other employees
  and from Greg. Runs in a neutral workdir (EMPLOYEE_WORKDIR), not /data/greg.
- Ad commands (/research, /draft, /pause, /report, /media) call meta_ads directly
  — plain Meta Graph API calls, NOT subject to the claude tool allowlist (which
  only governs the conversational lane). Allowlisted users get Pedro's full ad
  workflow INCLUDING the Launch button that starts real spend on the CAD account.
  Per Greg's explicit decision, Seba may self-launch his own campaigns (this is a
  deliberate exception to the "only Greg approves spend" rule). The /call (phone)
  path is deliberately omitted.

Runs as its own process / systemd service (nightshift-employees.service),
separate from nightshift.service.
"""
import asyncio
import logging
import os
import secrets

import httpx
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
from pedro_brain import PedroError, run_claude

TOKEN = os.environ["EMPLOYEE_BOT_TOKEN"]
EMPLOYEE_USERS = {
    int(x) for x in os.environ.get("EMPLOYEE_USERS", "").split(",") if x.strip()
}
# Neutral workdir — NOT /data/greg. Also scopes claude's session store, so
# employee sessions live in their own namespace away from the owner's.
WORKDIR = os.environ.get("EMPLOYEE_WORKDIR", "/data/employees")
SESSION_DIR = os.environ.get("EMPLOYEE_SESSION_DIR", "/data/employees/sessions")
# The security boundary: an ALLOWLIST of the only tools that exist for the run.
# Web research only — no shell/file/sub-agent/cron/MCP, so .env and tokens are
# unreachable. (A denylist is unsafe here; see module docstring.) Paired with
# strict_mcp=True below to drop ambient MCP servers.
ALLOWED_TOOLS = os.environ.get("EMPLOYEE_ALLOWED_TOOLS", "WebSearch WebFetch")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_TRANSCRIBE_MODEL = os.environ.get("GROQ_TRANSCRIBE_MODEL", "whisper-large-v3-turbo")
TELEGRAM_MAX_MSG = 4000

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("nightshift.employees")

# One lock per employee session: a single employee's messages serialize (claude
# can't --resume the same session concurrently), but different employees run in
# parallel. Distinct from the owner bot's single global lock.
_locks: dict[int, asyncio.Lock] = {}

# Confirm-first ad launches (mirrors Pedro): PAUSED campaigns awaiting a Launch
# tap, keyed by a short token. The campaign already exists PAUSED (no spend); the
# button only flips it ACTIVE via meta_ads.activate_campaign.
PENDING_CAMPAIGNS: dict[str, dict] = {}

META_NOT_CONFIGURED = (
    "📣 Meta Ads isn't configured yet. Ask Greg to set META_ACCESS_TOKEN and "
    "META_AD_ACCOUNT_ID in .env on the VPS."
)


def authorized(update: Update) -> bool:
    """Fail CLOSED: with no allowlist configured, nobody is allowed (the opposite
    of the owner bot, which opens up when unset). An employee bot with no list
    should expose nothing, not everything."""
    return bool(
        EMPLOYEE_USERS
        and update.effective_user
        and update.effective_user.id in EMPLOYEE_USERS
    )


def _session_file(uid: int) -> str:
    return os.path.join(SESSION_DIR, f"employee-{uid}")


async def _ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE, prompt: str) -> None:
    """Run a restricted, per-employee, memory-keeping claude turn and reply."""
    uid = update.effective_user.id
    lock = _locks.setdefault(uid, asyncio.Lock())
    if lock.locked():
        await update.message.reply_text(
            "⏳ Still working on your previous message — one moment."
        )
        return
    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    log.info("claude from %s: %s", uid, prompt[:200])
    try:
        out = await run_claude(
            prompt,
            workdir=WORKDIR,
            session_file=_session_file(uid),
            allowed_tools=ALLOWED_TOOLS,
            strict_mcp=True,
            lock=lock,
        )
    except PedroError as e:
        await update.message.reply_text(str(e))
        return
    for i in range(0, len(out), TELEGRAM_MAX_MSG):
        await update.message.reply_text(out[i:i + TELEGRAM_MAX_MSG])


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await update.message.reply_text(
            "You're not on the Nightshift team list for this bot. "
            "Ask Greg to add your Telegram ID (use /whoami to find it)."
        )
        return
    await update.message.reply_text(
        "Hi — I'm the Nightshift team assistant.\n\n"
        "Just talk to me normally, or use:\n"
        "/research <artist> [genre=hip_hop] [similar=A,B] - audience research\n"
        "/draft <name> | <ids> | <$CAD/day> | [objective] | [ticket_link] | [caption] | [image_url] - build a PAUSED campaign\n"
        "/media         - list images available for ad creative\n"
        "/pause <campaign_id> - stop a campaign's spend\n"
        "/report [id]   - last-7-day ad insights\n"
        "/new           - clear my memory of our conversation\n"
        "/whoami        - your Telegram user ID\n\n"
        "Voice notes work too. Campaigns are always drafted PAUSED; only the "
        "Launch button starts real spend."
    )


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    try:
        os.remove(_session_file(update.effective_user.id))
        await update.message.reply_text("🧹 Fresh start. I've cleared our conversation.")
    except FileNotFoundError:
        await update.message.reply_text("Already a fresh conversation.")


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    await update.message.reply_text(
        f"User ID: {u.id}\nUsername: @{u.username}\nName: {u.full_name}"
    )


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    await _ask(update, ctx, text)


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    if not GROQ_API_KEY:
        await update.message.reply_text(
            "Voice transcription is off (GROQ_API_KEY not set). Send text instead."
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
            f"Transcription failed ({e.response.status_code})."
        )
        return
    except Exception:  # noqa: BLE001 - any failure → graceful reply
        log.exception("voice transcription failed")
        await update.message.reply_text("Transcription error — try sending text.")
        return

    if not text:
        await update.message.reply_text("(empty transcription)")
        return
    log.info("voice from %s: %s", update.effective_user.id, text[:200])
    await update.message.reply_text(f"🎙 Heard: {text}")
    await _ask(update, ctx, text)


def _parse_research(query: str) -> tuple[str, str | None, list[str], str | None]:
    """Parse '<artist> [genre=..] [similar=a,b] [label=..]' (underscores→spaces).
    Returns (artist_name, genre, similar_artists, label). Mirrors bot.py."""
    artist_tokens: list[str] = []
    genre = None
    similar_artists: list[str] = []
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
    return (" ".join(artist_tokens).strip() or query, genre, similar_artists, label)


async def cmd_research(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Read-only: search Meta targeting interests for an artist/genre."""
    if not authorized(update):
        return
    if not meta_ads.configured():
        await update.message.reply_text("📣 Ad research isn't configured yet.")
        return
    query = " ".join(ctx.args).strip() if ctx.args else ""
    if not query:
        await update.message.reply_text(
            "Usage: /research <artist> [genre=hip_hop] [similar=Future,Drake]\n"
            "Example: /research Drake genre=hip_hop\n"
            "(use _ for spaces inside a value, e.g. genre=hip_hop)"
        )
        return
    artist_name, genre, similar_artists, label = _parse_research(query)

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
            f'No targeting interests found for "{artist_name}". '
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


def _format_reach(est: dict | None) -> str:
    """Best-effort one-liner from a reachestimate response (shape varies by API
    version). Returns '' if there's nothing usable."""
    if not isinstance(est, dict):
        return ""
    users = est.get("users") or est.get("estimate_mau") or est.get("estimate_dau")
    if isinstance(users, (int, float)):
        return f"Est. audience: ~{int(users):,}"
    return ""


async def cmd_draft(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a PAUSED campaign + ad set (no spend) and offer a Launch button.

    Ported from Pedro. The campaign is built PAUSED in meta_ads; only the inline
    Launch button flips it ACTIVE (via on_campaign_button → activate_campaign),
    so spend always needs an explicit per-campaign tap.
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
            f"Drop image files there, then reference them by filename in /draft:\n"
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
    """Read-only ad insights (last 7 days). Also emails a copy to
    META_REPORT_RECIPIENTS (Seba), mirroring Pedro."""
    if not authorized(update):
        return
    if not meta_ads.configured():
        await update.message.reply_text("📣 Ad reports aren't configured yet.")
        return
    obj = (ctx.args[0] if ctx.args else meta_ads.AD_ACCOUNT_ID).strip()
    if not obj:
        await update.message.reply_text("Usage: /report <campaign_id|account_id>")
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


def main() -> None:
    if not EMPLOYEE_USERS:
        log.warning(
            "EMPLOYEE_USERS is empty — the bot will refuse everyone until it's set."
        )
    # Ensure the neutral workdir + session dir exist (claude's cwd and our session
    # pointers live here).
    os.makedirs(WORKDIR, exist_ok=True)
    os.makedirs(SESSION_DIR, exist_ok=True)

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("research", cmd_research))
    app.add_handler(CommandHandler("draft", cmd_draft))
    app.add_handler(CommandHandler("media", cmd_media))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CallbackQueryHandler(on_campaign_button, pattern=r"^camp:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    log.info("Starting nightshift employee bot")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
