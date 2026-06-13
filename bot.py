import asyncio
import json
import logging
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
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
from pedro_brain import CLAUDE_TIMEOUT, PedroError, PedroTimeout, run_claude
import imap_email
import employee_email
from imap_email import get_unread_emails
import employee_requests

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

# Long-task continuation: when a claude run is killed at its per-run time cap,
# we automatically resume the same session ("continue where you left off")
# instead of stranding half-done work. Chat keeps a short first round so the
# common case stays snappy; continuation rounds are longer. Approved employee
# builds (video renders, code changes) get the longest rounds.
CHAT_CONTINUE_SECONDS = int(os.environ.get("PEDRO_CHAT_CONTINUE_SECONDS", "900"))
CHAT_CONTINUE_ROUNDS = int(os.environ.get("PEDRO_CHAT_CONTINUE_ROUNDS", "8"))
BUILD_ROUND_SECONDS = int(os.environ.get("PEDRO_BUILD_ROUND_SECONDS", "1500"))
BUILD_MAX_ROUNDS = int(os.environ.get("PEDRO_BUILD_MAX_ROUNDS", "6"))
BUILD_SESSION_DIR = os.environ.get("PEDRO_BUILD_SESSION_DIR", "/data/greg")

CONTINUE_PROMPT = (
    "SYSTEM: your previous run was killed at its per-run time cap mid-task. "
    "Resume EXACTLY where you left off and finish the job — do not start over "
    "and do not redo completed steps. When done, reply as you normally would."
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
# Envato search results awaiting a download tap, keyed by token.
PENDING_ENVATO: dict[str, dict] = {}

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


def _pop_account(text: str):
    """Pull an optional leading '@account' selector off a command's text.

    Lets any ad command pick which account to launch from, e.g.
    '/draft @pawnshop My Show | ids | 20'. Returns (profile, remaining_text);
    with no '@key' prefix it falls back to the default profile (Nightshift).
    Raises meta_ads.MetaError on an unknown key.
    """
    text = (text or "").strip()
    if text.startswith("@"):
        head, _, rest = text.partition(" ")
        return meta_ads.get_profile(head), rest.strip()
    return meta_ads.get_profile(None), text


def _meta_not_ready(acct) -> str:
    """Per-account 'not configured yet' message naming exactly what's missing."""
    KU = acct.key.upper()
    return (
        f"📣 Ad account @{acct.key} ({acct.label}) isn't ready yet.\n"
        f"{acct.status_line()}\n\n"
        f"Set in .env on the VPS, then restart nightshift.service:\n"
        f"  META_{KU}_ACCESS_TOKEN, META_{KU}_AD_ACCOUNT_ID, META_{KU}_PAGE_ID\n"
        f"Once the token is set, run  scripts/find_meta_assets.py @{acct.key}  "
        f"to discover the account/page/pixel ids."
    )


# Serialize the owner's claude runs — there is one shared, persistent session,
# so only one stateful run may proceed at a time. Restricted runs are one-shot
# and stateless, so they stay concurrent (no lock passed).
_claude_lock = asyncio.Lock()

# Set synchronously the instant a stateful chat run is queued, so a second
# message arriving in the SAME event-loop tick gets the "still working" reply
# instead of silently queueing behind the first (the async lock alone has a
# check-then-acquire gap — both messages would see it unlocked).
_owner_run_active = False

# Serialize approved-request builds: they edit shared code/files, so two
# concurrent builds could trample each other. Independent of _claude_lock —
# a build never blocks Greg's chat.
_build_lock = asyncio.Lock()


async def _run_with_continues(
    prompt: str,
    *,
    session_file: str,
    lock: asyncio.Lock,
    first_timeout: int,
    round_timeout: int,
    max_rounds: int,
    notify=None,
    resume_hint: str = "say 'continue'",
) -> str:
    """Run a stateful claude task, auto-resuming every time a run is killed at
    its time cap, so long jobs FINISH instead of stranding half-done work.

    Holds `lock` across all rounds so nothing interleaves mid-task. `notify`
    (async callable taking the round number) fires after each capped round so
    the owner knows work is still going. Never raises — errors come back as
    user-facing text.
    """
    async with lock:
        p, t = prompt, first_timeout
        for rnd in range(1, max_rounds + 1):
            try:
                return await run_claude(
                    p, workdir=CLAUDE_WORKDIR, session_file=session_file, timeout=t
                )
            except PedroTimeout:
                if rnd == max_rounds:
                    total_min = (first_timeout + (max_rounds - 1) * round_timeout) // 60
                    return (
                        f"⚠️ Still not finished after {max_rounds} continued runs "
                        f"(~{total_min} min of work). Progress is saved in the "
                        f"session — {resume_hint} and I'll pick it right back up."
                    )
                if notify is not None:
                    try:
                        await notify(rnd)
                    except Exception:  # noqa: BLE001 - progress ping must never kill the run
                        pass
                p, t = CONTINUE_PROMPT, round_timeout
            except PedroError as e:
                return str(e)
    return "(empty response)"


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
        "/accounts - list ad accounts you can launch from (Nightshift, Pawnshop, …)\n"
        "/research [@acct] <artist> [genre=hip_hop] [similar=A,B] [label=Label] - smart Meta targeting research\n"
        "/draft [@acct] <name> | <ids> | <$/day> | [objective] | [ticket_link] | [caption] | [flyer.jpg] - build a PAUSED campaign + ad\n"
        "/media - list images available for ads (drop files in /data/greg/ads/)\n"
        "/pause [@acct] <campaign_id> - stop a campaign's spend\n"
        "/report [@acct] [id] - last-7-day ad insights (defaults to the account)\n"
        "/shows [days|all] - upcoming Prism shows (default: next 60 days)\n"
        "/show <event_id>  - details for one Prism show\n"
        "/settlement <event_id> - ticket revenue, taxes, expenses for a show\n"
        "/envato <terms> - search Envato Elements (stock video, fonts, graphics, music) + download to Drive\n"
        "/envatostatus - check the Envato Elements session\n"
        "/new           - clear conversation memory, start fresh\n"
        "/status        - VPS health\n"
        "/whoami        - your Telegram user ID\n\n"
        "Voice notes work. PDFs/photos/documents also work — attach with caption."
    )
    if _is_owner(update):
        await update.message.reply_text(
            "Owner tools:\n"
            "/triage [days|full] - inbox attention queue: unanswered, real-person "
            "mail, deal-critical first (read-only; default 90 days)\n"
            "/wire <recipient> <amount_usd> - prep an Agility Forex wire "
            "(info only, never sends money)\n"
            "/wire list - known wire recipients\n"
            "/prismtoken <token> - update Prism auth (click the Prism-Token "
            "bookmarklet on app.prism.fm, then paste here). Paste the refresh "
            "token and Pedro auto-renews for ~30 days.\n"
            "/envatologin - connect/refresh the Envato Elements session "
            "(paste a 'Copy as cURL' from a logged-in elements.envato.com browser)."
        )


async def _call_claude(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    prompt: str,
    restricted: bool = False,
) -> None:
    global _owner_run_active
    # Fast "busy" feedback for stateful runs (restricted is stateless, never blocks).
    if not restricted and (_owner_run_active or _claude_lock.locked()):
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
    if restricted:
        try:
            out = await run_pedro(prompt, restricted=True)
        except PedroError as e:
            await update.message.reply_text(str(e))
            return
        for i in range(0, len(out), TELEGRAM_MAX_MSG):
            await update.message.reply_text(out[i:i + TELEGRAM_MAX_MSG])
        return

    # Stateful runs go to the background so a long task never blocks the
    # update loop. Mark active synchronously (before any await) so a same-tick
    # second message gets the busy reply. On a time-capped round the task
    # auto-continues instead of dying — see _run_with_continues.
    _owner_run_active = True
    ctx.application.create_task(_chat_run(update, ctx, prompt))


async def _chat_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE, prompt: str) -> None:
    global _owner_run_active

    async def _notify(rnd: int) -> None:
        if rnd == 1:  # only the first cap is news; after that, silence until done
            await update.message.reply_text(
                "⏳ This one's big — hit my first run limit, continuing in the "
                "background. I'll post the result here when it's done."
            )

    try:
        out = await _run_with_continues(
            prompt,
            session_file=SESSION_FILE,
            lock=_claude_lock,
            first_timeout=CLAUDE_TIMEOUT,
            round_timeout=CHAT_CONTINUE_SECONDS,
            max_rounds=CHAT_CONTINUE_ROUNDS,
            notify=_notify,
        )
    except Exception:  # noqa: BLE001 - background task; surface, never vanish
        log.exception("chat run failed")
        out = "⚠️ Something broke mid-run — check the service logs."
    finally:
        _owner_run_active = False
    for i in range(0, len(out), TELEGRAM_MAX_MSG):
        try:
            await update.message.reply_text(out[i:i + TELEGRAM_MAX_MSG])
        except Exception:  # noqa: BLE001
            log.exception("failed to deliver chat reply chunk")


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
    if ctx.user_data.get("awaiting_envato_cookie"):
        await _handle_envato_cookie(update, ctx, text)
        return
    if ctx.user_data.get("awaiting_rostr_cookie"):
        await _handle_rostr_cookie(update, ctx, text)
        return
    if _is_owner(update):
        hit = _match_build_command(text)
        if hit:
            verb, req_id, session_file = hit
            prompt = BUILD_DEPLOY_PROMPT if verb != "continue" else CONTINUE_PROMPT
            await update.message.reply_text(
                f"🔧 {'Shipping' if verb != 'continue' else 'Resuming'} build {req_id}…"
            )
            _active_builds.add(req_id)
            ctx.application.create_task(
                _continue_build(ctx.bot, update.message.chat_id, req_id,
                                "the requester", session_file, prompt)
            )
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
    try:
        acct, query = _pop_account(" ".join(ctx.args).strip() if ctx.args else "")
    except meta_ads.MetaError as e:
        await update.message.reply_text(str(e))
        return
    if not meta_ads.configured(acct):
        await update.message.reply_text(_meta_not_ready(acct))
        return
    if not query:
        await update.message.reply_text(
            "Usage: /research [@account] <artist> [genre=<genre>] [similar=artist1,artist2] [label=<label>]\n"
            "Example: /research Drake genre=hip_hop similar=Future,Travis_Scott label=OVO_Sound\n"
            "Simple: /research Drake   •   Other account: /research @pawnshop Comedy Night\n"
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
                acct=acct,
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
    acct_prefix = "" if acct.key == meta_ads.DEFAULT_PROFILE_KEY else f"@{acct.key} "
    summary += (
        f"\n\nTo draft a campaign on {acct.label} with all {len(all_ids)} interests:\n"
        f"/draft {acct_prefix}{artist_name} fans | {ids_csv} | 20"
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
    raw = (update.message.text or "").partition(" ")[2].strip()
    try:
        acct, raw = _pop_account(raw)
    except meta_ads.MetaError as e:
        await update.message.reply_text(str(e))
        return
    if not (acct.token and acct.ad_account_id):
        await update.message.reply_text(_meta_not_ready(acct))
        return
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 3 or not parts[0]:
        await update.message.reply_text(
            "Usage: /draft [@account] <name> | <interest_ids csv> | <daily $> | [objective] | [ticket_link] | [caption] | [image_url]\n"
            "Example: /draft Drake fans YEG | 6003123456789 | 20 | OUTCOME_TRAFFIC | https://showpass.com/event | Get tickets before they sell out!\n"
            "Other account: /draft @pawnshop Comedy Night | 6003123456789 | 20 | ... (see /accounts for keys)\n"
            "Get interest ids from /research. Objective defaults to OUTCOME_TRAFFIC.\n"
            "ticket_link, caption, image_url are optional — include them to create the full ad creative."
        )
        return
    name = parts[0]
    interest_ids = [x.strip() for x in parts[1].split(",") if x.strip()]
    try:
        daily_cad = float(parts[2])
    except ValueError:
        await update.message.reply_text(f"Daily budget must be a number in {acct.currency}. Got: {parts[2]}")
        return
    if daily_cad <= 0:
        await update.message.reply_text("Daily budget must be greater than 0.")
        return
    objective = parts[3] if len(parts) > 3 and parts[3] else "OUTCOME_TRAFFIC"
    ticket_link = parts[4] if len(parts) > 4 and parts[4] else None
    caption = parts[5] if len(parts) > 5 and parts[5] else None
    image_url = parts[6] if len(parts) > 6 and parts[6] else None
    daily_cents = int(round(daily_cad * 100))
    targeting = meta_ads.build_targeting(interest_ids, countries=acct.default_countries)

    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    creative_id = None
    ad_id = None
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            camp = await meta_ads.create_campaign(client, name, objective=objective, acct=acct)
            campaign_id = camp.get("id")
            if not campaign_id:
                await update.message.reply_text(f"Meta returned no campaign id:\n{camp}")
                return
            adset = await meta_ads.create_adset(
                client, campaign_id, f"{name} — ad set", daily_cents, targeting, acct=acct
            )
            adset_id = adset.get("id")
            creative_error = None
            image_hash = None
            image_label = None
            if ticket_link and caption and adset_id:
                try:
                    # If image_url looks like a filename (not http), treat it as a
                    # local media file — resolve it, upload to Meta, use the hash.
                    video_id = None
                    if image_url and not image_url.startswith("http"):
                        file_path = meta_ads.resolve_media_path(image_url)
                        if meta_ads.is_video(file_path):
                            video_id = await meta_ads.upload_ad_video(client, file_path, acct=acct)
                        else:
                            image_hash = await meta_ads.upload_ad_image(client, file_path, acct=acct)
                        image_label = image_url  # show the original filename in confirmation
                        image_url = None  # clear so create_adcreative uses hash path
                    if video_id:
                        creative = await meta_ads.create_adcreative_video(
                            client, f"{name} — creative", ticket_link, caption, video_id, acct=acct,
                        )
                    else:
                        creative = await meta_ads.create_adcreative(
                            client, f"{name} — creative", ticket_link, caption,
                            image_hash=image_hash, image_url=image_url, acct=acct,
                        )
                    creative_id = creative.get("id")
                    if creative_id:
                        ad = await meta_ads.create_ad(
                            client, adset_id, f"{name} — ad", creative_id, acct=acct
                        )
                        ad_id = ad.get("id")
                except meta_ads.MetaError as ce:
                    creative_error = str(ce)
            try:
                est = await meta_ads.reach_estimate(client, targeting, acct=acct)
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
        "acct_key": acct.key,
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
        f"Account: {acct.label} (@{acct.key})\n"
        f"Name: {name}\n"
        f"Campaign id: {campaign_id}\n"
        f"Objective: {objective}\n"
        f"Daily budget: ${daily_cad:.2f} {acct.currency}\n"
        f"Interests: {', '.join(interest_ids) or '(none — broad)'}\n"
        f"Geo: {', '.join(acct.default_countries)}\n"
        f"{creative_lines}"
        + (f"{reach_line}\n" if reach_line else "")
        + f"\nLaunching starts real spend on the {acct.label} account. Launch now?",
        reply_markup=keyboard,
    )


async def cmd_campaign(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Build a full PAUSED, sales-structured campaign for one show (3 ad sets:
    retargeting / lookalike / cold) wired to the NS custom audiences, confirm-first.
    Objective is auto-picked from the ticket link (Sales+Purchase for pixel-trackable
    ticketing, Traffic+LandingPageViews otherwise). Spend starts only on Launch."""
    if not authorized(update):
        return
    if not meta_ads.configured():
        await update.message.reply_text(META_NOT_CONFIGURED)
        return
    raw = (update.message.text or "").partition(" ")[2].strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 4 or not parts[0] or not parts[2] or not parts[3]:
        await update.message.reply_text(
            "Usage: /campaign <name> | <daily $CAD> | <ticket_link> | <caption> | [flyer.jpg or image_url] | [interest_ids csv]\n\n"
            "Builds a PAUSED sales campaign: 3 ad sets (retargeting / lookalike / cold), "
            "objective auto-picked from the ticket link, your NS audiences attached, UTM "
            "tracking on the link. Confirm-first — nothing spends until you tap Launch.\n\n"
            "Example:\n/campaign Webby Hamilton | 30 | https://www.ticketweb.ca/event/x | "
            "Chris Webby live in Hamilton — tickets on sale now! | webby.jpg"
        )
        return
    name = parts[0]
    try:
        daily_cad = float(parts[1])
    except ValueError:
        await update.message.reply_text(f"Daily budget must be a number in CAD. Got: {parts[1]}")
        return
    if daily_cad <= 0:
        await update.message.reply_text("Daily budget must be greater than 0.")
        return
    ticket_link = parts[2]
    caption = parts[3]
    image = parts[4] if len(parts) > 4 and parts[4] else None
    interest_ids = [x.strip() for x in parts[5].split(",") if x.strip()] if len(parts) > 5 and parts[5] else None

    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            image_hash = None
            image_url = None
            image_label = None
            video_id = None
            if image:
                if image.startswith("http"):
                    image_url = image
                    image_label = image
                else:
                    file_path = meta_ads.resolve_media_path(image)
                    if meta_ads.is_video(file_path):
                        video_id = await meta_ads.upload_ad_video(client, file_path)
                    else:
                        image_hash = await meta_ads.upload_ad_image(client, file_path)
                    image_label = image
            res = await meta_ads.build_show_campaign(
                client, name, daily_cad, ticket_link, caption,
                interest_ids=interest_ids, image_hash=image_hash, image_url=image_url, video_id=video_id,
            )
    except meta_ads.MetaError as e:
        await update.message.reply_text(f"Campaign build failed (nothing launched): {e}")
        return
    except Exception as e:  # noqa: BLE001
        log.exception("campaign build failed")
        await update.message.reply_text(f"Campaign build error (nothing launched): {e}")
        return

    token = secrets.token_urlsafe(8)
    PENDING_CAMPAIGNS[token] = {
        "campaign_id": res["campaign_id"],
        "name": name,
        "daily_cad": daily_cad,
        "acct_key": meta_ads.DEFAULT_PROFILE_KEY,
        "full": True,
    }
    adset_lines = "\n".join(
        f"  • {a['layer']}: ${a['daily_cents'] / 100:.2f}/day" for a in res["adsets"]
    )
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("\U0001F680 Launch (start spend)", callback_data=f"camp:go:{token}"),
            InlineKeyboardButton("✖️ Keep paused", callback_data=f"camp:hold:{token}"),
        ]]
    )
    await update.message.reply_text(
        "\U0001F4CB Sales campaign built — PAUSED, not spending:\n\n"
        f"Name: {name}\n"
        f"Campaign id: {res['campaign_id']}\n"
        f"Objective: {res['objective']} ({res['platform']})\n"
        f"Total daily: ${daily_cad:.2f} CAD, split across:\n"
        f"{adset_lines}\n"
        f"Creative: {image_label or '(OG preview from link)'}\n"
        f"Ticket link: {ticket_link}\n\n"
        "Launching starts real spend on the Nightshift CAD account. Launch now?",
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
    try:
        acct = meta_ads.get_profile(draft.get("acct_key"))
    except meta_ads.MetaError as e:
        await query.message.reply_text(f"Launch failed (campaign stays paused): {e}")
        return
    await query.edit_message_text(f"🚀 Launching campaign {draft['campaign_id']} on {acct.label}…")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if draft.get("full"):
                await meta_ads.activate_full(client, draft["campaign_id"])
            else:
                await meta_ads.activate_campaign(client, draft["campaign_id"], acct=acct)
    except meta_ads.MetaError as e:
        await query.message.reply_text(f"Launch failed (campaign stays paused): {e}")
        return
    except Exception as e:  # noqa: BLE001 - surface any failure to the user
        log.exception("campaign activate failed")
        await query.message.reply_text(f"Launch error (campaign stays paused): {e}")
        return
    pause_hint = f"/pause {draft['campaign_id']}" if acct.key == meta_ads.DEFAULT_PROFILE_KEY else f"/pause @{acct.key} {draft['campaign_id']}"
    await query.message.reply_text(
        f"✅ Campaign {draft['campaign_id']} on {acct.label} is ACTIVE, spending up to "
        f"${draft['daily_cad']:.2f} {acct.currency}/day.\n"
        f"Pause anytime with {pause_hint}."
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


async def cmd_accounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """List the ad accounts you can launch from and whether each is ready."""
    if not authorized(update):
        return
    profiles = meta_ads.list_profiles()
    if not profiles:
        await update.message.reply_text("No ad accounts configured.")
        return
    lines = ["📒 Ad accounts (use @key with /draft, /research, /pause, /report):\n"]
    for p in profiles:
        default = " (default)" if p.key == meta_ads.DEFAULT_PROFILE_KEY else ""
        lines.append(f"• {p.status_line()}{default}")
    lines.append("\nExample: /draft @pawnshop Comedy Night | 6003123456789 | 20 | OUTCOME_TRAFFIC | https://...")
    await update.message.reply_text("\n".join(lines))


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Pause a campaign — always safe, stops spend immediately."""
    if not authorized(update):
        return
    try:
        acct, rest = _pop_account(" ".join(ctx.args) if ctx.args else "")
    except meta_ads.MetaError as e:
        await update.message.reply_text(str(e))
        return
    if not meta_ads.configured(acct):
        await update.message.reply_text(_meta_not_ready(acct))
        return
    cid = rest.strip().split()[0] if rest.strip() else ""
    if not cid:
        await update.message.reply_text("Usage: /pause [@account] <campaign_id>")
        return
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await meta_ads.pause_campaign(client, cid, acct=acct)
    except meta_ads.MetaError as e:
        await update.message.reply_text(f"Pause failed: {e}")
        return
    await update.message.reply_text(f"⏸ Campaign {cid} paused on {acct.label} — spend stopped.")


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Read-only insights for a campaign/adset/ad/account (last 7 days)."""
    if not authorized(update):
        return
    try:
        acct, rest = _pop_account(" ".join(ctx.args) if ctx.args else "")
    except meta_ads.MetaError as e:
        await update.message.reply_text(str(e))
        return
    if not meta_ads.configured(acct):
        await update.message.reply_text(_meta_not_ready(acct))
        return
    obj = (rest.strip().split()[0] if rest.strip() else acct.ad_account_id).strip()
    if not obj:
        await update.message.reply_text(
            "Usage: /report [@account] <campaign_id|account_id>\n"
            f"(defaults to the @{acct.key} account id once that's set)"
        )
        return
    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            rows = await meta_ads.get_insights(client, obj, acct=acct)
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
            f"{acct.label} Ads report — {obj} (last 7 days)",
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


PRISM_ENV_PATH = os.environ.get("PRISM_ENV_PATH", "/home/gregnightshift/nightshift/.env")


def _set_env_key(key: str, val: str) -> None:
    """Update (or append) KEY=val in the .env file, backing it up first."""
    try:
        shutil.copy(PRISM_ENV_PATH, "%s.bak.%d" % (PRISM_ENV_PATH, int(datetime.now().timestamp())))
    except Exception:  # noqa: BLE001
        pass
    try:
        lines = open(PRISM_ENV_PATH).read().splitlines()
    except FileNotFoundError:
        lines = []
    out, found = [], False
    for ln in lines:
        if ln.startswith(key + "="):
            out.append("%s=%s" % (key, val)); found = True
        else:
            out.append(ln)
    if not found:
        out.append("%s=%s" % (key, val))
    open(PRISM_ENV_PATH, "w").write("\n".join(out) + "\n")


async def cmd_prismtoken(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: update Prism auth. Accepts EITHER token from a logged-in
    app.prism.fm tab:

      • the REFRESH token (localStorage 'refreshToken', not a JWT) — DURABLE: Pedro
        then auto-mints its own ~1h access tokens via Cognito for ~30 days, so you
        only do this about once a month. PREFERRED.
      • the access token (localStorage 'token', a JWT) — one-off, lasts ~1h.

    Prism's Cognito app client is public (no secret), so REFRESH_TOKEN_AUTH works
    headless — the old 'needs a secret' belief was a misdiagnosis; it only ever
    failed on an expired refresh token. Written to .env + the running module; the
    per-turn employee MCP re-reads .env, so both bots pick it up with no restart.
    """
    if not authorized(update) or not _is_owner(update):
        return
    raw = (update.message.text or "").partition(" ")[2].strip()
    token = raw.split()[0].strip() if raw else ""
    if not token:
        await update.message.reply_text(
            "Usage: /prismtoken <token>\n\n"
            "Best: paste the DURABLE refresh token — click the Prism-Token bookmarklet on a "
            "logged-in app.prism.fm tab (it copies the right one), then send /prismtoken and paste. "
            "Pedro then auto-refreshes itself for ~30 days."
        )
        return

    is_jwt = token.count(".") == 2 and len(token) > 100
    # Delete the message first so the raw credential doesn't linger in chat.
    try:
        await ctx.bot.delete_message(update.effective_chat.id, update.message.message_id)
    except Exception:  # noqa: BLE001
        pass

    if is_jwt:
        # One-off access token (~1h).
        exp = prism._jwt_exp(token)
        now = int(datetime.now().timestamp())
        if exp <= now:
            await update.message.reply_text("That access token is already expired — grab a fresh one.")
            return
        hrs = round((exp - now) / 3600, 1)
        _set_env_key("PRISM_ACCESS_TOKEN", token)
        prism.ACCESS_TOKEN_ENV = token
        ok_msg = ("✅ Prism access token updated — valid ~%sh. Live read OK (%%d shows). "
                  "Tip: paste the *refresh* token instead and Pedro auto-renews for ~30 days." % hrs)
    else:
        # Durable refresh token (~30d) — Pedro mints access tokens itself.
        if len(token) < 100:
            await update.message.reply_text(
                "That doesn't look like a Prism token. Use the Prism-Token bookmarklet, "
                "or paste localStorage.getItem('refreshToken')."
            )
            return
        _set_env_key("PRISM_REFRESH_TOKEN", token)
        _set_env_key("PRISM_ACCESS_TOKEN", "")  # drop any stale pinned token so refresh-mint is used
        prism.REFRESH_TOKEN = token
        prism.ACCESS_TOKEN_ENV = ""
        ok_msg = ("✅ Prism refresh token saved — Pedro now auto-mints its own access tokens for "
                  "~30 days (no more hourly pasting). Live read OK (%d shows).")

    today = datetime.now().date()
    start, end = today.isoformat(), (today + timedelta(days=120)).isoformat()
    result = ok_msg.replace("%d", "?") if "%d" not in ok_msg else None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=40.0) as client:
                shows = await prism.list_shows(client, start, end)
            result = ok_msg % len(shows)
            break
        except prism.PrismError as e:
            m = re.search(r"(\d+\.\d+\.\d+)", str(e))
            if "App-Version" in str(e) and attempt == 0 and m:
                _set_env_key("PRISM_APP_VERSION", m.group(1))
                prism.APP_VERSION = m.group(1)
                continue
            result = "⚠️ Saved, but the live read failed: %s" % (str(e)[:300])
            break
        except Exception as e:  # noqa: BLE001
            result = "⚠️ Saved, but the live read failed: %s: %s" % (type(e).__name__, str(e)[:200])
            break
    await update.message.reply_text(result)


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


TRIAGE_SCRIPT = "/home/gregnightshift/nightshift/scripts/attention_triage.py"
TRIAGE_PYTHON = "/home/gregnightshift/nightshift/.venv/bin/python"


async def cmd_triage(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only, read-only: surface unanswered, real-person inbox mail,
    deal-critical first. /triage [days] [full]  (default: 90-day brief)."""
    if not _is_owner(update):
        return
    days, mode = "90", "brief"
    for a in ctx.args or []:
        if a.isdigit():
            days = a
        elif a.lower() in ("full", "all", "more"):
            mode = "full"
        else:
            await update.message.reply_text(
                "Usage: /triage [days] [full]  e.g. /triage 30  ·  /triage full"
            )
            return
    cmd = [TRIAGE_PYTHON, TRIAGE_SCRIPT, "--days", days]
    cmd += ["--brief"] if mode == "brief" else ["--top", "50"]
    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    try:
        proc = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, timeout=240
        )
    except subprocess.TimeoutExpired:
        await update.message.reply_text(
            "⏳ Triage timed out scanning the inbox. Try a shorter window, e.g. /triage 30."
        )
        return
    out = (proc.stdout or "").strip() or (proc.stderr or "").strip() or "No output."
    for i in range(0, len(out), TELEGRAM_MAX_MSG):
        await update.message.reply_text(out[i:i + TELEGRAM_MAX_MSG])


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    await update.message.reply_text(
        f"User ID: {u.id}\nUsername: @{u.username}\nName: {u.full_name}"
    )


def _collect_status() -> dict:
    """Gather host stats. Blocking (subprocess + /proc read), so callers must
    run it off the event loop via asyncio.to_thread."""
    uptime = subprocess.check_output(["uptime", "-p"], text=True).strip()
    disk = shutil.disk_usage("/")
    meminfo: dict[str, int] = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, rest = line.partition(":")
            meminfo[k.strip()] = int(rest.split()[0]) * 1024
    mem_total = meminfo["MemTotal"]
    mem_avail = meminfo.get("MemAvailable", meminfo["MemFree"])
    return {
        "uptime": uptime,
        "disk_pct": disk.used / disk.total * 100,
        "disk_free": disk.free, "disk_total": disk.total,
        "mem_total": mem_total, "mem_avail": mem_avail,
    }


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    s = await asyncio.to_thread(_collect_status)
    mem_total, mem_avail = s["mem_total"], s["mem_avail"]
    mem_used_pct = (mem_total - mem_avail) / mem_total * 100

    wa = "on" if whatsapp.configured() else "off"
    await update.message.reply_text(
        f"Host: {platform.node()}\n"
        f"Uptime: {s['uptime']}\n"
        f"Disk /: {s['disk_pct']:.1f}% used "
        f"({s['disk_free'] / 1e9:.1f} GB free of {s['disk_total'] / 1e9:.1f} GB)\n"
        f"Memory: {mem_used_pct:.1f}% used "
        f"({(mem_total - mem_avail) / 1e9:.2f} / {mem_total / 1e9:.2f} GB)\n"
        f"WhatsApp: {wa}\n"
        f"Now: {datetime.now().isoformat(timespec='seconds')}"
    )


async def _post_init(application: Application) -> None:
    """Start the auto-build watcher and the Twilio WhatsApp webhook server on
    the bot's event loop, alongside Telegram long-polling."""
    # Plain asyncio task (not Application.create_task): post_init runs before
    # the app is "running", where PTB neither tracks nor awaits such tasks.
    # Keep our own reference so it can't be garbage-collected.
    application.bot_data["_auto_build_task"] = asyncio.create_task(
        _auto_build_watcher(application)
    )
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


SEBA_UID = 8722742818  # Seba's employee-bot Telegram id


async def cmd_sebamail(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await update.message.reply_text("Not authorized.")
        return
    creds = employee_email.inbox_for(SEBA_UID)
    if not creds:
        await update.message.reply_text(
            "Seba hasn't connected his inbox yet - have him run /setupinbox in the Team Bot."
        )
        return
    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    try:
        emails = await asyncio.to_thread(get_unread_emails, creds, 24)
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"Seba inbox check failed: {e}")
        return
    if not emails:
        await update.message.reply_text("\U0001F4EC No unread emails in Seba inbox (last 24h).")
        return
    lines = [f"\U0001F4EC Seba inbox — {len(emails)} unread (last 24h):", ""]
    for e in emails:
        lines.append(f"From: {e['from']}\nSubject: {e['subject']}\nPreview: {e['body'][:150]}")
        lines.append("—")
    text = "\n".join(lines)
    for i in range(0, len(text), TELEGRAM_MAX_MSG):
        await update.message.reply_text(text[i:i + TELEGRAM_MAX_MSG])


ANDREW_UID = 8621126122  # Andrew Devlin's employee-bot Telegram id


async def cmd_andrewmail(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await update.message.reply_text("Not authorized.")
        return
    creds = employee_email.inbox_for(ANDREW_UID)
    if not creds:
        await update.message.reply_text(
            "Andrew hasn't connected his inbox yet - have him run /setupinbox in the Team Bot."
        )
        return
    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    try:
        emails = await asyncio.to_thread(get_unread_emails, creds, 24)
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"Andrew inbox check failed: {e}")
        return
    if not emails:
        await update.message.reply_text("\U0001F4EC No unread emails in Andrew inbox (last 24h).")
        return
    lines = [f"\U0001F4EC Andrew inbox \u2014 {len(emails)} unread (last 24h):", ""]
    for e in emails:
        lines.append(f"From: {e['from']}\nSubject: {e['subject']}\nPreview: {e['body'][:150]}")
        lines.append("\u2014")
    text = "\n".join(lines)
    for i in range(0, len(text), TELEGRAM_MAX_MSG):
        await update.message.reply_text(text[i:i + TELEGRAM_MAX_MSG])


APPROVED_IMPL_PROMPT = (
    "An employee feature request was just APPROVED by Greg. You have full tools "
    "and self-deploy — actually implement it, don't punt it back.\n\n"
    "Request from {name}:\n{text}\n\n"
    "Diagnose first (read the relevant code/state), then make the change. "
    "Long tasks are fine: if you hit a run limit you will be resumed "
    "automatically — just keep working, never wrap up early because of time. "
    "CONFIRM-FIRST before anything reaches production: when done, reply with a "
    "concise summary of exactly what you changed (files + what/why). If anything "
    "needs a git commit / push / deploy trigger, do NOT do it yet — end your "
    "summary with this exact line so Greg can ship it:\n"
    "Reply 'deploy build {req_id}' to ship it.\n"
    "If nothing needs deploying (pure content/media work, or read-only "
    "diagnosis), omit that line and just report what's done and where. "
    "Keep it tight."
)


# Builds currently running IN THIS PROCESS. The watcher requeues approved
# builds that are started-but-not-done on disk (restart recovery), and this
# set stops it re-picking one that's still alive here.
_active_builds: set[str] = set()


async def _run_approved_build(bot, chat_id: int, req_id: str, name: str, text: str) -> None:
    """Build an approved employee request in the background, in its own session,
    auto-continuing across run caps. Reports the result (or a clear failure) to
    the owner chat — an approved build never dies silently."""
    session_file = os.path.join(BUILD_SESSION_DIR, f".pedro-build-{req_id}.session")
    if os.path.exists(session_file):
        # A previous attempt was interrupted (bot restart) — resume it rather
        # than replaying the request from scratch.
        prompt = CONTINUE_PROMPT
    else:
        prompt = APPROVED_IMPL_PROMPT.format(name=name, text=text, req_id=req_id)
    await _continue_build(bot, chat_id, req_id, name, session_file, prompt)


async def _continue_build(bot, chat_id, req_id, name, session_file, prompt) -> None:
    async def _notify(rnd: int) -> None:
        await bot.send_message(
            chat_id,
            f"⏳ Still building {name}'s request (run limit {rnd}/{BUILD_MAX_ROUNDS} "
            "hit — continuing where it left off)…",
        )

    try:
        reply = await _run_with_continues(
            prompt,
            session_file=session_file,
            lock=_build_lock,
            first_timeout=BUILD_ROUND_SECONDS,
            round_timeout=BUILD_ROUND_SECONDS,
            max_rounds=BUILD_MAX_ROUNDS,
            notify=_notify,
            resume_hint=f"reply 'continue build {req_id}'",
        )
    except Exception:  # noqa: BLE001 - background task; surface, never vanish
        log.exception("approved build failed (req %s)", req_id)
        reply = f"⚠️ Build for {name}'s request crashed — check the service logs."
    finally:
        employee_requests.mark_build_done(req_id)
        _active_builds.discard(req_id)
    for i in range(0, len(reply), TELEGRAM_MAX_MSG):
        try:
            await bot.send_message(chat_id, reply[i:i + TELEGRAM_MAX_MSG])
        except Exception:  # noqa: BLE001
            log.exception("failed to deliver build reply chunk")


MEDIA_IMPL_PROMPT = (
    "A media/content request from {name} was AUTO-APPROVED — Greg has given "
    "{name} a standing green light for media work, so do NOT ask Greg for "
    "approval and do NOT wait for sign-off. Build it now.\n\n"
    "Request:\n{text}\n\n"
    "Scope guardrail — this lane is CONTENT PRODUCTION ONLY: creating/revising "
    "videos, graphics, copy, ad creatives, Drive assets. Any campaign you touch "
    "stays PAUSED, zero spend. If the task turns out to need code changes, a "
    "deploy, money movement, or a mass send, STOP that part and say it needs "
    "Greg's normal approval.\n"
    "Long tasks are fine: you are auto-resumed at run limits — never wrap up "
    "early because of time. Deliver the assets (save under /data/greg and "
    "upload to Drive where it makes sense), then reply with a tight summary: "
    "what you made, where it lives (Drive links/paths), and anything {name} "
    "should check."
)


async def _run_media_build(bot, rec: dict) -> None:
    """Build an auto-approved media request and deliver the result to the
    requesting employee (their lane), with an FYI copy to Greg."""
    req_id = rec["id"]
    name = rec.get("requester_name", "employee")
    session_file = os.path.join(BUILD_SESSION_DIR, f".pedro-build-{req_id}.session")
    prompt = MEDIA_IMPL_PROMPT.format(name=name, text=rec.get("text", ""))

    async def _notify(rnd: int) -> None:
        if rnd == 1:
            employee_requests.notify_employee(
                rec["requester_id"],
                "⏳ Your media request is a bigger job — still on it, I'll "
                "message you here when it's ready.",
            )

    try:
        reply = await _run_with_continues(
            prompt,
            session_file=session_file,
            lock=_build_lock,
            first_timeout=BUILD_ROUND_SECONDS,
            round_timeout=BUILD_ROUND_SECONDS,
            max_rounds=BUILD_MAX_ROUNDS,
            notify=_notify,
            resume_hint=f"reply 'continue build {req_id}'",
        )
    except Exception:  # noqa: BLE001 - background task; surface, never vanish
        log.exception("media build failed (req %s)", req_id)
        reply = "⚠️ The build crashed — flag it to Greg."
    finally:
        employee_requests.mark_build_done(req_id)
        _active_builds.discard(req_id)
    employee_requests.notify_employee(
        rec["requester_id"], ("✅ Your media request is done:\n\n" + reply)[:4000]
    )
    fyi = f"🎨 [auto-approved media] {name}'s request {req_id} finished:\n\n{reply}"
    for i in range(0, len(fyi), TELEGRAM_MAX_MSG):
        try:
            await bot.send_message(OWNER_ID, fyi[i:i + TELEGRAM_MAX_MSG])
        except Exception:  # noqa: BLE001
            log.exception("failed to deliver media build FYI chunk")


AUTO_BUILD_POLL_SECONDS = int(os.environ.get("PEDRO_AUTO_BUILD_POLL_SECONDS", "20"))


async def _auto_build_watcher(application: Application) -> None:
    """Pick up buildable requests from disk and run them. Covers two cases:

    - fresh auto-approved (green-light media) requests written by the employee
      MCP, which runs in another process — disk is the handoff;
    - approved builds whose asyncio task was destroyed by a bot restart
      (started-but-not-done on disk, not active in this process) — these are
      resumed so an Approve tap can never be silently lost again."""
    log.info("auto-build watcher started (poll %ss)", AUTO_BUILD_POLL_SECONDS)
    while True:
        try:
            for rec in employee_requests.list_buildable():
                req_id = rec["id"]
                if req_id in _active_builds:
                    continue
                _active_builds.add(req_id)
                employee_requests.mark_build_started(req_id)
                resumed = " (resumed after restart)" if rec.get("build_started") else ""
                log.info("auto-build pickup%s: %s from %s", resumed, req_id,
                         rec.get("requester_name"))
                if rec.get("category") == "media":
                    application.create_task(_run_media_build(application.bot, rec))
                else:
                    application.create_task(_run_approved_build(
                        application.bot, OWNER_ID, req_id,
                        rec.get("requester_name", "employee"), rec.get("text", ""),
                    ))
        except Exception:  # noqa: BLE001 - the watcher must never die
            log.exception("auto-build watcher tick failed")
        await asyncio.sleep(AUTO_BUILD_POLL_SECONDS)


# Owner chat hooks for background builds: 'deploy build <id>' ships a finished
# build's pending commit/push/deploy; 'continue build <id>' resumes one that
# ran out of continuation rounds.
_BUILD_CMD_RE = re.compile(r"^\s*(deploy|ship|push|continue)\s+build\s+([0-9a-f]{4,16})\s*$", re.I)

BUILD_DEPLOY_PROMPT = (
    "Greg just approved deployment in chat. Execute the pending commit / push / "
    "deploy steps now, exactly as you summarized them, then report what shipped."
)


def _match_build_command(text: str):
    """Return (verb, req_id, session_file) when the owner's message addresses a
    background build and its session exists; None otherwise."""
    m = _BUILD_CMD_RE.match(text or "")
    if not m:
        return None
    verb, req_id = m.group(1).lower(), m.group(2)
    session_file = os.path.join(BUILD_SESSION_DIR, f".pedro-build-{req_id}.session")
    if not os.path.exists(session_file):
        return None
    return verb, req_id, session_file


async def on_request_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner taps Approve/Reject on an employee feature request."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:  # noqa: BLE001 - stale query ("too old"); ignore
        pass
    if not authorized(update):
        return
    try:
        _, action, req_id = query.data.split(":", 2)
    except ValueError:
        return
    rec = employee_requests.load(req_id)
    if not rec:
        try:
            await query.edit_message_text("This request expired or was already handled.")
        except Exception:  # noqa: BLE001
            pass
        return
    if rec.get("status") != "pending":
        try:
            await query.edit_message_text(
                f"Already {rec['status']}: {rec.get('text', '')[:200]}"
            )
        except Exception:  # noqa: BLE001
            pass
        return
    status = "approved" if action == "approve" else "rejected"
    employee_requests.set_status(req_id, status)
    name = rec.get("requester_name", "employee")
    text = rec.get("text", "")
    if status == "approved":
        owner_msg = (
            f"✅ Approved {name}'s request — handing it to Pedro to implement now. "
            "I'll report back here with what changed and wait for your OK before "
            "anything deploys to production."
        )
        emp_msg = (
            f"✅ Greg approved your request:\n\n{text}\n\n"
            "It's being implemented now — I'll let you know once it's live."
        )
    else:
        owner_msg = f"❌ Rejected {name}'s request. They've been notified."
        emp_msg = f"❌ Greg declined your request:\n\n{text}"
    try:
        await query.edit_message_text(owner_msg)
    except Exception:  # noqa: BLE001
        pass
    employee_requests.notify_employee(rec["requester_id"], emp_msg)

    # On approval, actually BUILD it — in the background, in its own session,
    # auto-continuing across run caps so big jobs (video renders, code changes)
    # finish instead of dying at a 5-minute wall. Confirm-first is baked into
    # the prompt: production deploys wait for Greg's 'deploy build <id>'.
    if status == "approved":
        chat_id = query.message.chat_id if query.message else rec["requester_id"]
        # Mark started on disk BEFORE spawning: if a restart destroys the task,
        # the watcher finds started-but-not-done and resumes it.
        _active_builds.add(req_id)
        employee_requests.mark_build_started(req_id)
        ctx.application.create_task(
            _run_approved_build(ctx.bot, chat_id, req_id, name, text)
        )


async def _post_shutdown(application: Application) -> None:
    runner = application.bot_data.get("_wh_runner")
    if runner is not None:
        await runner.cleanup()



# --- Envato Elements: search the subscription + download assets to Drive ------
_ENVATO_TYPE_ALIASES = {
    "video": "stock-video", "videos": "stock-video", "stockvideo": "stock-video",
    "footage": "stock-video", "clip": "stock-video", "template": "video-templates",
    "templates": "video-templates", "photo": "photos", "photos": "photos",
    "image": "photos", "images": "photos", "pic": "photos", "font": "fonts",
    "fonts": "fonts", "music": "music", "track": "music", "song": "music",
    "sfx": "sound-effects", "sound": "sound-effects", "graphic": "graphics",
    "graphics": "graphics", "3d": "3d", "presentation": "presentation-templates",
}
ENVATO_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "envato.py")
ROSTR_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rostr.py")


async def _run_envato(args, timeout=300, stdin_text=None):
    """Run the envato.py CLI in a worker thread; return the CompletedProcess."""
    cmd = [sys.executable, ENVATO_PY, *args]
    return await asyncio.to_thread(
        subprocess.run, cmd, capture_output=True, text=True, timeout=timeout,
        input=stdin_text, cwd=os.path.dirname(ENVATO_PY),
    )


def _envato_err(proc):
    blob = (proc.stderr or proc.stdout or "").strip()
    try:
        return (json.loads(blob).get("error") or blob)[:300]
    except Exception:  # noqa: BLE001
        return (blob or "unknown error")[:300]


async def cmd_envato(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    args = ctx.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /envato [type] <search terms>\n"
            "Examples: /envato neon city skyline   ·   /envato video drone city   ·   /envato fonts retro\n\n"
            "Optional leading type: video, photos, fonts, music, sfx, graphics, templates, 3d.\n"
            "Without one I search everything (video/photos/graphics first) and you tap a result to download to Drive.\n"
            "/envatostatus checks the session; /envatologin (re)connects it."
        )
        return
    item_type = ""
    if len(args) >= 2 and args[0].lower() in _ENVATO_TYPE_ALIASES:
        item_type = _ENVATO_TYPE_ALIASES[args[0].lower()]
        query = " ".join(args[1:]).strip()
    else:
        query = " ".join(args).strip()
    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    _cmd = ["search", query, "--json", "--limit", "6"]
    if item_type:
        _cmd += ["--type", item_type]
    proc = await _run_envato(_cmd)
    if proc.returncode != 0:
        await update.message.reply_text("\U0001F3AC Envato: " + _envato_err(proc))
        return
    try:
        results = json.loads(proc.stdout or "[]")
    except Exception:  # noqa: BLE001
        results = []
    if not results:
        await update.message.reply_text("No Envato results for \u201c%s\u201d." % query)
        return
    rows, lines = [], []
    for it in results[:6]:
        tok = secrets.token_urlsafe(6)
        PENDING_ENVATO[tok] = {"url": it.get("url", ""), "id": it.get("id", ""),
                               "type": it.get("type", "")}
        label = (it.get("title") or it.get("type") or it.get("id") or "asset")[:40]
        lines.append("\u2022 %s \u2014 %s" % (it.get("type") or "asset", it.get("url")))
        rows.append([InlineKeyboardButton("\u2B07\uFE0F " + label, callback_data="env:dl:" + tok)])
    await update.message.reply_text(
        "\U0001F3AC Envato results for \u201c%s\u201d \u2014 tap to download to Drive:\n\n%s"
        % (query, "\n".join(lines)),
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_envato_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not authorized(update):
        return
    try:
        _, _action, tok = q.data.split(":", 2)
    except ValueError:
        return
    item = PENDING_ENVATO.pop(tok, None)
    if not item:
        await q.edit_message_text("That Envato result expired \u2014 search again with /envato.")
        return
    await q.edit_message_text("\u2B07\uFE0F Downloading %s and saving to Drive\u2026"
                              % (item.get("type") or "asset"))
    ctx.application.create_task(_envato_download_task(q, item))


async def _envato_download_task(q, item) -> None:
    try:
        proc = await _run_envato(
            ["download", item.get("url") or item.get("id"), "--to-drive", "--json"],
            timeout=900)
    except Exception:  # noqa: BLE001
        log.exception("envato download failed")
        await q.message.reply_text("\U0001F3AC Envato download crashed \u2014 check service logs.")
        return
    if proc.returncode != 0:
        await q.message.reply_text("\U0001F3AC Envato download failed: " + _envato_err(proc))
        return
    try:
        res = json.loads(proc.stdout or "{}")
    except Exception:  # noqa: BLE001
        res = {}
    link = (res.get("drive") or {}).get("link", "")
    size = res.get("bytes", 0)
    if link:
        await q.message.reply_text("\u2705 Saved to Drive (%s bytes)\n%s" % (format(size, ","), link))
    else:
        await q.message.reply_text("\u2705 Downloaded (%s bytes): %s" % (format(size, ","), res.get("saved", "")))


async def cmd_envatostatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    proc = await _run_envato(["status", "--json"])
    try:
        s = json.loads(proc.stdout or proc.stderr or "{}")
    except Exception:  # noqa: BLE001
        s = {}
    if not s.get("configured"):
        await update.message.reply_text(
            "\U0001F3AC Envato isn't connected yet. Use /envatologin to seed the session.")
        return
    ok = s.get("valid")
    await update.message.reply_text(
        "\U0001F3AC Envato session: %s \u00b7 account %s \u00b7 age %sd \u00b7 %s cookies%s"
        % ("\u2705 valid" if ok else "\u26a0\uFE0F invalid/expired",
           s.get("account") or "?", s.get("age_days"), s.get("cookie_count"),
           "" if ok else "\nRe-seed with /envatologin."))


async def cmd_envatologin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        await update.message.reply_text("Only Greg can connect the Envato session.")
        return
    ctx.user_data["awaiting_envato_cookie"] = True
    await update.message.reply_text(
        "\U0001F3AC Connect Envato Elements (one-time, ~2 min):\n\n"
        "1. Log into elements.envato.com in Chrome.\n"
        "2. Press F12 \u2192 Network tab.\n"
        "3. Reload the page (Ctrl+R).\n"
        "4. Click the top request (named elements.envato.com, type 'document').\n"
        "5. Right-click \u2192 Copy \u2192 Copy as cURL (bash).\n"
        "6. Paste it here as your next message.\n\n"
        "Send /cancel to abort.")


async def _handle_envato_cookie(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text) -> None:
    ctx.user_data.pop("awaiting_envato_cookie", None)
    if text.strip().lower() in ("/cancel", "cancel"):
        await update.message.reply_text("Cancelled \u2014 Envato not changed.")
        return
    await update.message.reply_text("\U0001F3AC Seeding the Envato session\u2026")
    proc = await _run_envato(["login", "--auto", "-"], stdin_text=text)
    if proc.returncode != 0:
        await update.message.reply_text(
            "Couldn't read those cookies: " + _envato_err(proc)
            + "\n\nTry /envatologin again and paste the full 'Copy as cURL'.")
        return
    st = await _run_envato(["status", "--json"])
    try:
        s = json.loads(st.stdout or "{}")
    except Exception:  # noqa: BLE001
        s = {}
    await update.message.reply_text(
        "\u2705 Envato connected \u2014 session "
        + ("valid" if s.get("valid") else "seeded (validates on first search)")
        + ((", account %s" % s.get("account")) if s.get("account") else "")
        + ".\n\nTry it: /envato neon city skyline")



# --------------------------------------------------------------------------- #
# ROSTR (rostr.cc) — music-industry intelligence for offer creation.
# Cookie-seeded (no public API), read-only. Greg seeds via /rostrlogin; staff
# pull data through the employee MCP. Same pattern as Envato.
# --------------------------------------------------------------------------- #
async def _run_rostr(args, timeout=120, stdin_text=None):
    """Run the rostr.py CLI in a worker thread; return the CompletedProcess."""
    cmd = [sys.executable, ROSTR_PY, *args]
    return await asyncio.to_thread(
        subprocess.run, cmd, capture_output=True, text=True, timeout=timeout,
        input=stdin_text, cwd=os.path.dirname(ROSTR_PY),
    )


async def cmd_rostr(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        await update.message.reply_text("Staff pull ROSTR data via the Team Bot. Ask Pedro.")
        return
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("Usage: /rostr <artist or company>")
        return
    proc = await _run_rostr(["search", q, "--json"])
    if proc.returncode != 0:
        await update.message.reply_text("\U0001F50E ROSTR: " + _envato_err(proc))
        return
    out = (proc.stdout or "").strip()
    await update.message.reply_text("\U0001F50E " + (out[:3500] or "(no results)"))


async def cmd_rostrstatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    proc = await _run_rostr(["status", "--json"])
    try:
        s = json.loads(proc.stdout or "{}")
    except Exception:  # noqa: BLE001
        s = {}
    if not s.get("configured"):
        await update.message.reply_text(
            "\U0001F50E ROSTR isn't connected yet. Use /rostrlogin to seed the session.")
        return
    await update.message.reply_text(
        "\U0001F50E ROSTR session %s (cookies %s days old).%s" % (
            "valid" if s.get("valid") else "INVALID/expired",
            s.get("age_days"),
            "" if s.get("valid") else "\nRe-seed with /rostrlogin."))


async def cmd_rostrlogin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        await update.message.reply_text("Only Greg can connect the ROSTR session.")
        return
    ctx.user_data["awaiting_rostr_cookie"] = True
    await update.message.reply_text(
        "\U0001F50E Connect ROSTR (one-time, ~2 min):\n\n"
        "1. Log into hq.rostr.cc in Chrome.\n"
        "2. Press F12 → Network tab.\n"
        "3. Reload the page (Ctrl+R).\n"
        "4. Click the top request (named hq.rostr.cc, type 'document').\n"
        "5. Right-click → Copy → Copy as cURL (bash).\n"
        "6. Paste it here as your next message.\n\n"
        "Send /cancel to abort.")


async def _handle_rostr_cookie(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text) -> None:
    ctx.user_data.pop("awaiting_rostr_cookie", None)
    if text.strip().lower() in ("/cancel", "cancel"):
        await update.message.reply_text("Cancelled — ROSTR not changed.")
        return
    await update.message.reply_text("\U0001F50E Seeding the ROSTR session…")
    proc = await _run_rostr(["login", "--auto", "-"], stdin_text=text)
    if proc.returncode != 0:
        await update.message.reply_text(
            "Couldn't read those cookies: " + _envato_err(proc)
            + "\n\nTry /rostrlogin again and paste the full 'Copy as cURL'.")
        return
    st = await _run_rostr(["status", "--json"])
    try:
        s = json.loads(st.stdout or "{}")
    except Exception:  # noqa: BLE001
        s = {}
    await update.message.reply_text(
        "✅ ROSTR connected — session "
        + ("valid" if s.get("valid") else "seeded (validates on first search)")
        + ".\n\nTry it: /rostr <artist name>")


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
    app.add_handler(CommandHandler("campaign", cmd_campaign))
    app.add_handler(CommandHandler("accounts", cmd_accounts))
    app.add_handler(CommandHandler("media", cmd_media))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("shows", cmd_shows))
    app.add_handler(CommandHandler("show", cmd_show))
    app.add_handler(CommandHandler("settlement", cmd_settlement))
    app.add_handler(CommandHandler("wire", cmd_wire))
    app.add_handler(CommandHandler("prismtoken", cmd_prismtoken))
    app.add_handler(CommandHandler("triage", cmd_triage))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("sebamail", cmd_sebamail))
    app.add_handler(CommandHandler("andrewmail", cmd_andrewmail))
    app.add_handler(CommandHandler("envato", cmd_envato))
    app.add_handler(CommandHandler("envatologin", cmd_envatologin))
    app.add_handler(CommandHandler("envatostatus", cmd_envatostatus))
    app.add_handler(CommandHandler("rostr", cmd_rostr))
    app.add_handler(CommandHandler("rostrlogin", cmd_rostrlogin))
    app.add_handler(CommandHandler("rostrstatus", cmd_rostrstatus))
    app.add_handler(CallbackQueryHandler(on_call_button, pattern=r"^call:"))
    app.add_handler(CallbackQueryHandler(on_campaign_button, pattern=r"^camp:"))
    app.add_handler(CallbackQueryHandler(on_wire_button, pattern=r"^wire:"))
    app.add_handler(CallbackQueryHandler(on_request_button, pattern=r"^req:"))
    app.add_handler(CallbackQueryHandler(on_envato_button, pattern=r"^env:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_attachment))
    log.info("Starting agentpedro bot")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
