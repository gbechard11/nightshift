import logging
import os
import platform
import shutil
import subprocess
from datetime import datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USERS = {
    int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip()
}

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
        "/status  - VPS health\n"
        "/whoami  - your Telegram user ID"
    )


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
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    log.info("Starting agentpedro bot")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
