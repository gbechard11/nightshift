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
- Read-only ad commands (/report, /research) reuse meta_ads' GET endpoints.
  There is intentionally NO /draft, /pause, /call, or any spend/action path —
  employees cannot create, launch, or pause campaigns or spend money.

Runs as its own process / systemd service (nightshift-employees.service),
separate from nightshift.service.
"""
import asyncio
import logging
import os

import httpx
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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
        "/report [id]   - last-7-day ad insights (read-only)\n"
        "/new           - clear my memory of our conversation\n"
        "/whoami        - your Telegram user ID\n\n"
        "Voice notes work too. I can answer questions and look things up on the "
        "web, but I can't access company files, the server, or spend money."
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

    if not result["all_ids"]:
        await update.message.reply_text(
            f'No targeting interests found for "{artist_name}". '
            "Try adding genre= or similar= to broaden the search."
        )
        return
    summary = result["summary"]
    if len(summary) > 3800:  # Telegram hard-caps at 4096
        summary = summary[:3800] + "\n…(truncated)"
    await update.message.reply_text(summary)


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Read-only ad insights (last 7 days). No email, no campaign changes."""
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
    await update.message.reply_text("\n".join(lines))


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
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    log.info("Starting nightshift employee bot")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
