import asyncio
import logging
import os
import platform
import re
import shutil
import subprocess
import uuid
from datetime import datetime

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

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USERS = {
    int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip()
}
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/usr/bin/claude")
CLAUDE_WORKDIR = os.environ.get("CLAUDE_WORKDIR", "/data/greg")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "300"))
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_TRANSCRIBE_MODEL = os.environ.get("GROQ_TRANSCRIBE_MODEL", "whisper-large-v3-turbo")
INBOX_DIR = os.environ.get("PEDRO_INBOX", "/data/greg/inbox")
SESSION_FILE = os.environ.get("PEDRO_SESSION_FILE", "/data/greg/.pedro_session_id")
SAFE_DISALLOWED_TOOLS = os.environ.get(
    "PEDRO_SAFE_DISALLOWED_TOOLS", "Bash Edit Write NotebookEdit"
)
TELEGRAM_MAX_MSG = 4000

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("nightshift")


def authorized(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True
    return bool(update.effective_user and update.effective_user.id in ALLOWED_USERS)


def _get_session_id() -> tuple[str, bool]:
    """Return (session_id, is_new). is_new=True means we just created it and the
    first claude call should use --session-id; otherwise use --resume."""
    try:
        with open(SESSION_FILE) as f:
            sid = f.read().strip()
        if sid:
            return sid, False
    except FileNotFoundError:
        pass
    sid = str(uuid.uuid4())
    os.makedirs(os.path.dirname(SESSION_FILE) or ".", exist_ok=True)
    with open(SESSION_FILE, "w") as f:
        f.write(sid)
    return sid, True


# Serialize claude invocations — only one session-using claude can run at a time.
# Restricted mode (one-shot, no session) is exempt.
_claude_lock = asyncio.Lock()


async def _run_claude(args: list[str]):
    """Run claude; return (returncode, stdout_str, stderr_str).
    returncode is None on timeout or missing binary (message is in stderr_str)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=CLAUDE_WORKDIR,
        )
    except FileNotFoundError:
        return None, "", f"claude binary not found at {CLAUDE_BIN}"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=CLAUDE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None, "", f"claude timed out after {CLAUDE_TIMEOUT}s"
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    await update.message.reply_text(
        "Hi, I'm agentpedro — your nightshift assistant.\n\n"
        "Just talk to me normally, or use:\n"
        "/ask <prompt>  - same as plain text\n"
        "/safe <prompt> - read-only, no memory, no shell/file writes\n"
        "/new           - clear conversation memory, start fresh\n"
        "/status        - VPS health\n"
        "/whoami        - your Telegram user ID\n\n"
        "Voice notes work. PDFs/photos/documents also work — attach with caption."
    )


async def _call_claude(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    prompt: str,
    restricted: bool = False,
) -> None:
    # Serialize stateful (session-using) invocations. Restricted is stateless so it can run concurrently.
    if not restricted and _claude_lock.locked():
        await update.message.reply_text(
            "⏳ Pedro is still working on your previous request. Try again in a moment, "
            "or use /safe <prompt> for an independent one-shot."
        )
        return

    lock = _claude_lock if not restricted else asyncio.Lock()  # dummy lock for restricted
    async with lock:
        await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
        log.info(
            "claude%s from %s: %s",
            " [safe]" if restricted else "",
            update.effective_user.id,
            prompt[:200],
        )

        base = [CLAUDE_BIN, "--permission-mode", "bypassPermissions"]
        if restricted:
            # One-shot, no memory, no dangerous tools.
            args = base + ["--disallowed-tools", SAFE_DISALLOWED_TOOLS, "-p", prompt]
        else:
            sid, is_new = _get_session_id()
            # First message of a conversation creates the session (--session-id);
            # every later message RESUMES it (--resume) so context carries over.
            session_flag = "--session-id" if is_new else "--resume"
            args = base + [session_flag, sid, "-p", prompt]

        rc, out, err = await _run_claude(args)

        # Recover from a broken/locked/missing session: wipe the pointer, start a
        # genuinely fresh session, retry once. (Loses memory only when the session
        # was unusable anyway.)
        if (
            rc not in (0, None)
            and not restricted
            and any(s in err.lower() for s in ("already in use", "no conversation", "not found", "no such session"))
        ):
            log.warning("session unusable, starting fresh: %s", err[:200])
            try:
                os.remove(SESSION_FILE)
            except FileNotFoundError:
                pass
            sid, _ = _get_session_id()  # creates a new one
            args = base + ["--session-id", sid, "-p", prompt]
            rc, out, err = await _run_claude(args)

        if rc is None:
            await update.message.reply_text(err)  # timeout / missing-binary message
            return
        if rc != 0:
            await update.message.reply_text(
                f"claude exited {rc}:\n{(err.strip() or '(no stderr)')[:1500]}"
            )
            return

        out = out.strip() or "(empty response)"
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

    await update.message.reply_text(
        f"Host: {platform.node()}\n"
        f"Uptime: {uptime}\n"
        f"Disk /: {disk_pct:.1f}% used "
        f"({disk.free / 1e9:.1f} GB free of {disk.total / 1e9:.1f} GB)\n"
        f"Memory: {mem_used_pct:.1f}% used "
        f"({(mem_total - mem_avail) / 1e9:.2f} / {mem_total / 1e9:.2f} GB)\n"
        f"Now: {datetime.now().isoformat(timespec='seconds')}"
    )


def main() -> None:
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("safe", cmd_safe))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_attachment))
    log.info("Starting agentpedro bot")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
