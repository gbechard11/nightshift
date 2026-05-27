import asyncio
import logging
import os
import platform
import shutil
import subprocess
from datetime import datetime

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


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    await update.message.reply_text(
        "Hi, I'm agentpedro — your nightshift assistant.\n\n"
        "/ask <prompt>  - hand a prompt to Claude on the VPS\n"
        "/status        - VPS health\n"
        "/whoami        - your Telegram user ID"
    )


async def _call_claude(update: Update, ctx: ContextTypes.DEFAULT_TYPE, prompt: str) -> None:
    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    log.info("claude from %s: %s", update.effective_user.id, prompt[:200])

    try:
        proc = await asyncio.create_subprocess_exec(
            CLAUDE_BIN,
            "--permission-mode", "bypassPermissions",
            "-p", prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=CLAUDE_WORKDIR,
        )
    except FileNotFoundError:
        await update.message.reply_text(f"claude binary not found at {CLAUDE_BIN}")
        return

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=CLAUDE_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        await update.message.reply_text(f"claude timed out after {CLAUDE_TIMEOUT}s")
        return

    if proc.returncode != 0:
        err = (stderr.decode(errors="replace") or "(no stderr)").strip()[:1500]
        await update.message.reply_text(f"claude exited {proc.returncode}:\n{err}")
        return

    out = stdout.decode(errors="replace").strip() or "(empty response)"
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
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Starting agentpedro bot")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
