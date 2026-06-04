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
import json
import os
import secrets
import time

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
import employee_notify
import employee_requests
import employee_drive
import employee_email
import employee_notes
import imap_email
from pedro_brain import PedroError, run_claude

TOKEN = os.environ["EMPLOYEE_BOT_TOKEN"]
EMPLOYEE_USERS = {
    int(x) for x in os.environ.get("EMPLOYEE_USERS", "").split(",") if x.strip()
}
# Neutral workdir — NOT /data/greg. Also scopes claude's session store, so
# employee sessions live in their own namespace away from the owner's.
WORKDIR = os.environ.get("EMPLOYEE_WORKDIR", "/data/employees")
SESSION_DIR = os.environ.get("EMPLOYEE_SESSION_DIR", "/data/employees/sessions")
CONNECT_CODES = os.environ.get(
    "EMPLOYEE_CONNECT_CODES", "/data/employees/mcp-connect-codes.json"
)
CONNECT_TTL = 600  # seconds a /connect code stays valid
# The security boundary: an ALLOWLIST of the only tools that exist for the run.
# Locked-down lane: a comprehensive DENYLIST removes every shell/file/sub-agent/
# cron/skill/worktree tool from the model's context. Bare tool names are
# context-gated (the CLI strips them from the tool definitions sent to the
# model), so they are ABSENT, not merely permission-gated — unrecoverable even
# via ToolSearch. We must use a denylist (not the --tools allowlist) because
# --tools is built-in-only and silently drops MCP tools; the denylist is the
# only way to keep WebSearch/WebFetch + the submit_request MCP tool while
# removing escalation tools. ToolSearch MUST stay allowed — the CLI surfaces
# MCP tools through it (deny it and submit_request never loads). Verified live.
# Paired with strict_mcp=True to drop ambient MCP servers.
EMPLOYEE_DENY_TOOLS = os.environ.get(
    "EMPLOYEE_DENY_TOOLS",
    "Bash Read Edit Write Agent Glob Grep NotebookEdit Task TodoWrite "
    "Cron Monitor BashOutput KillShell CronCreate CronDelete CronList "
    "Skill Workflow EnterWorktree ExitWorktree RemoteTrigger "
    "PushNotification ScheduleWakeup EnterPlanMode ExitPlanMode",
)
REQUEST_MCP_CONFIG = os.environ.get(
    "EMPLOYEE_REQUEST_MCP_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "employee_requests.mcp.json"),
)
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

# Per-user in-progress email setup (uid -> {step, email, host, port}).
EMAIL_SETUP: dict[int, dict] = {}
# Per-user in-progress inbox (IMAP) setup (uid -> {step, email, imap_host, ...}).
INBOX_SETUP: dict[int, dict] = {}

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


EMPLOYEE_HANDOFF = (
    "SYSTEM: your conversation memory is about to be rotated to stay within "
    "context limits. A fresh session will only know what you save now. You "
    "CANNOT write files here -- your ONLY way to carry anything forward is the "
    "`remember` tool. Before you reply, you MUST call `remember` at least once. "
    "Call it ONCE for EACH open item, in-progress task, pending request, and key "
    "fact a fresh session would need to continue seamlessly (names, addresses, "
    "amounts, dates, the exact next action). If genuinely nothing is pending, "
    "still call `remember` once with a one-line summary of what was discussed. "
    "Only after you have called `remember`, reply 'saved'."
)


def _with_notes(uid: int, prompt: str) -> str:
    """Prepend the employee's saved notes so preferences survive session
    resets. Notes come from the agent's `remember` MCP tool."""
    notes = employee_notes.read(uid)
    if not notes:
        return prompt
    header = ("[Saved notes about this person -- apply them, and use the "
              "remember tool to add more; do not say you cannot keep memory]")
    return header + chr(10) + notes + chr(10) + chr(10) + prompt


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
            _with_notes(uid, prompt),
            workdir=WORKDIR,
            session_file=_session_file(uid),
            disallowed_tools=EMPLOYEE_DENY_TOOLS,
            strict_mcp=True,
            mcp_config=REQUEST_MCP_CONFIG,
            handoff_prompt=EMPLOYEE_HANDOFF,
            env={
                "NS_REQUESTER_ID": str(uid),
                "NS_REQUESTER_NAME": employee_notify.who(uid),
            },
            lock=lock,
        )
    except PedroError as e:
        await update.message.reply_text(str(e))
        return
    for i in range(0, len(out), TELEGRAM_MAX_MSG):
        await update.message.reply_text(out[i:i + TELEGRAM_MAX_MSG])


async def cmd_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Submit a feature request to Greg for approval."""
    if not authorized(update):
        await update.message.reply_text(
            "Not authorized. Ask Greg to add your Telegram ID (use /whoami)."
        )
        return
    text = " ".join(ctx.args).strip() if ctx.args else ""
    if not text:
        await update.message.reply_text(
            "Tell me what you'd like Pedro to be able to do, e.g.\n"
            "/request a morning briefing of my email inbox"
        )
        return
    name = employee_notify.who(update.effective_user.id)
    rec = employee_requests.submit(update.effective_user.id, name, text)
    employee_notify.notify_owner_request(rec)
    await update.message.reply_text(
        "✅ Sent to Greg for approval. You'll hear back here when he decides."
    )


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
        "/setupemail    - set up YOUR email so reports send from you\n"
        "/setupinbox    - connect YOUR inbox so Greg can see your unread mail\n"
        "/connect       - link this bot to your Claude app\n"
        "/new           - clear my memory of our conversation\n"
        "/whoami        - your Telegram user ID\n\n"
        "Drive (read everything, create new only - cannot edit or delete existing):\n  /files [folderId]  (no arg = shared folders)\n  /find <name>\n  /get <fileId>\n  /mkdir <name> | <parentId>\n  attach a file captioned: /upload <folderId>\n  /replace <fileId> overwrites it (write access required)\n\nVoice notes work too. Campaigns are always drafted PAUSED; only the "
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
    if update.effective_user.id in EMAIL_SETUP:
        await _email_setup_step(update, ctx, text)
        return
    if update.effective_user.id in INBOX_SETUP:
        await _inbox_setup_step(update, ctx, text)
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
    asyncio.create_task(asyncio.to_thread(
        employee_notify.notify_owner,
        f"\U0001F680 {employee_notify.who(update.effective_user.id)} launched Meta campaign "
        f"{draft['campaign_id']} - spending up to ${draft['daily_cad']:.2f} CAD/day.",
    ))


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Route an uploaded file to Drive: caption /upload (create) or /replace (overwrite)."""
    if not authorized(update):
        return
    caption = (update.message.caption or "").strip()
    if caption.lower().startswith("/upload"):
        await employee_drive.handle_upload(update, ctx)
        return
    if caption.lower().startswith("/replace"):
        await employee_drive.handle_replace(update, ctx)
        return
    await update.message.reply_text(
        "To save a file to Drive, attach it with a caption:\n"
        "/upload <folderId>   - add it as a NEW file\n"
        "/replace <fileId>    - overwrite an existing file (write access required)"
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
    sender = employee_email.sender_for(update.effective_user.id)
    if sender is None:
        await update.message.reply_text(
            "📧 (Not emailed — you don't have your own email setup yet. "
            "Ask Greg to add your sending address so reports go out from you.)"
        )
        return
    try:
        await asyncio.to_thread(
            mailer.send,
            f"Nightshift Ads report — {obj} (last 7 days)",
            report_text,
            recipients,
            sender,
        )
        sent_from = sender.get('from') or sender.get('smtp_user')
        await update.message.reply_text(
            f"📧 Report emailed to {', '.join(recipients)} from {sent_from}."
        )
    except mailer.MailError as e:
        await update.message.reply_text(f"⚠️ Report shown above but email failed: {e}")


_APP_PW_HELP = (
    "For Gmail / Workspace you need a Google *App Password* (not your normal password):\n"
    "1. Turn on 2-Step Verification at myaccount.google.com/security\n"
    "2. Create one at myaccount.google.com/apppasswords (name it \"Nightshift bot\")\n"
    "3. Paste the 16-character code here."
)


async def cmd_setupemail(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Start the guided self-service email setup."""
    if not authorized(update):
        return
    EMAIL_SETUP[update.effective_user.id] = {"step": "email"}
    await update.message.reply_text(
        "Let's set up your email so reports send from *you*, not a shared address.\n\n"
        "What email address will you send from? (e.g. you@nightshiftent.ca)\n\n"
        "You can type /cancelemail anytime to stop.",
        parse_mode="Markdown",
    )


async def cmd_cancelemail(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    EMAIL_SETUP.pop(update.effective_user.id, None)
    await update.message.reply_text("Email setup cancelled.")


async def cmd_setupinbox(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Start the guided self-service INBOX (IMAP read) setup."""
    if not authorized(update):
        return
    INBOX_SETUP[update.effective_user.id] = {"step": "email"}
    await update.message.reply_text(
        "Let's connect YOUR inbox so Greg can see your unread mail at a glance.\n\n"
        "What's your email address? (e.g. you@pawnshop-live.ca)\n\n"
        "Type /cancelinbox anytime to stop.",
    )


async def cmd_cancelinbox(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    INBOX_SETUP.pop(update.effective_user.id, None)
    await update.message.reply_text("Inbox setup cancelled.")


async def _inbox_setup_step(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    uid = update.effective_user.id
    st = INBOX_SETUP.get(uid)
    if not st:
        return

    if st["step"] == "email":
        em = text.strip()
        if "@" not in em or " " in em:
            await update.message.reply_text("That doesn't look like an email - try again, or /cancelinbox.")
            return
        st["email"] = em
        host, port = employee_email.infer_imap(em)
        if host:
            st["imap_host"], st["imap_port"], st["step"] = host, port, "pass"
            await update.message.reply_text(
                f"Got it - {em}. I'll read via {host}:{port}.\n\n"
                "Now paste your email password (or app password).\n"
                "(I'll delete the message with your password right after.)"
            )
        else:
            st["step"] = "host"
            await update.message.reply_text(
                f"Got it - {em}. I don't know your mail server.\n"
                "Send your incoming (IMAP) host and port, like:\n"
                "  mail.example.com 993"
            )
        return

    if st["step"] == "host":
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            await update.message.reply_text("Send it as:  mail.example.com 993")
            return
        st["imap_host"], st["imap_port"], st["step"] = parts[0], int(parts[1]), "pass"
        await update.message.reply_text(
            f"Using {st['imap_host']}:{st['imap_port']}.\n"
            "Now paste your email password (or app password).\n"
            "(I'll delete the message with your password right after.)"
        )
        return

    if st["step"] == "pass":
        password = text.strip()
        chat_id = update.message.chat_id
        try:
            await update.message.delete()
        except Exception:  # noqa: BLE001
            pass
        inbox = {
            "email": st["email"],
            "password": password,
            "imap_host": st["imap_host"],
            "imap_port": st["imap_port"],
            "smtp_host": st["imap_host"],
            "smtp_port": 465,
        }
        await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
        await ctx.bot.send_message(chat_id, "Testing those details by logging into your inbox...")
        try:
            await asyncio.to_thread(imap_email.check_imap, inbox)
        except Exception as e:  # noqa: BLE001
            await ctx.bot.send_message(
                chat_id,
                f"\u274c Couldn't log in with those details: {e}\n\n"
                "Paste the password again, or /cancelinbox to stop.",
            )
            return  # stay on the 'pass' step
        try:
            employee_email.save_inbox(uid, inbox)
        except Exception as e:  # noqa: BLE001
            INBOX_SETUP.pop(uid, None)
            await ctx.bot.send_message(chat_id, f"Login worked but saving failed: {e}. Tell Greg.")
            return
        INBOX_SETUP.pop(uid, None)
        await ctx.bot.send_message(
            chat_id,
            "\u2705 Done! Your inbox is connected. Greg can now see your unread mail.",
        )
        return



def _mint_connect_code(uid: int) -> str:
    """Write a short-lived one-time code the MCP server trades for this uid.

    Format matches employee_mcp._consume_connect_code: {code: {uid, exp}}.
    Prunes expired codes and any prior code for the same uid so a fresh
    /connect always supersedes an older unused one.
    """
    now = int(time.time())
    code = f"{secrets.randbelow(1_000_000):06d}"
    try:
        with open(CONNECT_CODES, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data = {
        k: v
        for k, v in data.items()
        if int(v.get("exp", 0)) >= now and int(v.get("uid", 0)) != uid
    }
    data[code] = {"uid": uid, "exp": now + CONNECT_TTL}
    os.makedirs(os.path.dirname(CONNECT_CODES), exist_ok=True)
    tmp = CONNECT_CODES + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, CONNECT_CODES)
    try:
        os.chmod(CONNECT_CODES, 0o600)
    except OSError:
        pass
    return code


async def cmd_connect(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Give the employee a one-time code to link this bot to their Claude app."""
    if not authorized(update):
        return
    code = await asyncio.to_thread(_mint_connect_code, update.effective_user.id)
    await update.message.reply_text(
        "*Connect to your Claude app*\n\n"
        f"Your one-time code is:  `{code}`\n\n"
        "In the Claude app, add the Nightshift Team Bot connector, sign in, "
        "and paste this code when asked. It expires in 10 minutes and works once.",
        parse_mode="Markdown",
    )


async def _email_setup_step(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    uid = update.effective_user.id
    st = EMAIL_SETUP.get(uid)
    if not st:
        return

    if st["step"] == "email":
        email = text.strip()
        if "@" not in email or " " in email:
            await update.message.reply_text("That doesn't look like an email — try again, or /cancelemail.")
            return
        st["email"] = email
        host, port = employee_email.infer_smtp(email)
        if host:
            st["host"], st["port"], st["user"], st["step"] = host, port, email, "pass"
            await update.message.reply_text(
                f"Got it - {email}. I'll send via {host}:{port}.\n\n" + _APP_PW_HELP,
                parse_mode="Markdown",
            )
        else:
            st["step"] = "host"
            await update.message.reply_text(
                f"Got it - {email}. I don't know your mail server.\n"
                "Send your outgoing (SMTP) host and port, like:\n"
                "  smtp.example.com 587   (STARTTLS)\n"
                "  smtp.example.com 465   (SSL)"
            )
        return

    if st["step"] == "host":
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            await update.message.reply_text("Send it as:  smtp.example.com 587")
            return
        st["host"], st["port"], st["step"] = parts[0], int(parts[1]), "user"
        await update.message.reply_text(
            f"Using {st['host']}:{st['port']}.\n"
            "What's the SMTP *login username*? For some providers (Amazon SES, "
            "Postmark, Mailgun) the login is a separate key, not your email address.\n\n"
            f"Reply with the username, or send  same  to use {st['email']}.",
            parse_mode="Markdown",
        )
        return

    if st["step"] == "user":
        user = text.strip()
        st["user"] = st["email"] if user.lower() == "same" else user
        st["step"] = "pass"
        await update.message.reply_text(
            f"Login user: {st['user']}. Now paste your SMTP password / app password.\n\n"
            "(I'll delete the message with your password right after.)"
        )
        return

    if st["step"] == "pass":
        password = text.strip()
        if st["host"] == "smtp.gmail.com":
            password = password.replace(" ", "")
        chat_id = update.message.chat_id
        # scrub the password message from the chat
        try:
            await update.message.delete()
        except Exception:  # noqa: BLE001
            pass
        sender = {
            "from": st["email"],
            "smtp_host": st["host"],
            "smtp_port": st["port"],
            "smtp_user": st["user"],
            "smtp_pass": password,
        }
        await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
        await ctx.bot.send_message(chat_id, "Testing those details by sending you a quick test email...")
        try:
            await asyncio.to_thread(
                mailer.send,
                "Nightshift bot - email test",
                "Your Nightshift bot email is set up correctly. Reports will now send from this address.",
                [st["email"]],
                sender,
            )
        except mailer.MailError as e:
            await ctx.bot.send_message(
                chat_id,
                f"\u274c Couldn't send with those details: {e}\n\n"
                "Paste the app password again, or /cancelemail to stop.",
            )
            return  # stay on the 'pass' step
        try:
            employee_email.save_sender(uid, sender)
        except Exception as e:  # noqa: BLE001
            EMAIL_SETUP.pop(uid, None)
            await ctx.bot.send_message(chat_id, f"Test worked but saving failed: {e}. Tell Greg.")
            return
        EMAIL_SETUP.pop(uid, None)
        await ctx.bot.send_message(
            chat_id,
            f"\u2705 Done! Check {st['email']} for the test email. "
            "Your reports will now send from your own address.",
        )
        return


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Bot can't OCR images; guide the user to send the value as text."""
    if not authorized(update):
        return
    if update.effective_user.id in EMAIL_SETUP or update.effective_user.id in INBOX_SETUP:
        await update.message.reply_text(
            "I can't read screenshots - please *type* the value as text "
            "(the mail server, login username, or password).",
            parse_mode="Markdown",
        )
        return
    await update.message.reply_text(
        "I can't read images yet - please send the details as text."
    )


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
    app.add_handler(CommandHandler("setupemail", cmd_setupemail))
    app.add_handler(CommandHandler("cancelemail", cmd_cancelemail))
    app.add_handler(CommandHandler("setupinbox", cmd_setupinbox))
    app.add_handler(CommandHandler("cancelinbox", cmd_cancelinbox))
    app.add_handler(CommandHandler("connect", cmd_connect))
    app.add_handler(CommandHandler("research", cmd_research))
    app.add_handler(CommandHandler("draft", cmd_draft))
    app.add_handler(CommandHandler("media", cmd_media))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("files", employee_drive.cmd_files))
    app.add_handler(CommandHandler("find", employee_drive.cmd_find))
    app.add_handler(CommandHandler("get", employee_drive.cmd_get))
    app.add_handler(CommandHandler("mkdir", employee_drive.cmd_mkdir))
    app.add_handler(CommandHandler("request", cmd_request))
    app.add_handler(CallbackQueryHandler(on_campaign_button, pattern=r"^camp:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    log.info("Starting nightshift employee bot")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
