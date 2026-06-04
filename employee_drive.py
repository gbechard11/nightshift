"""Drive access for employees.

Wraps gdrive.py as restricted slash commands so an employee can READ all of
Greg's Drive (list / find / download) and CREATE new items (mkdir / upload).

Two write tiers, gated per Telegram user id:
  - default (create-only): can add NEW folders/files but can NEVER modify or
    delete anything that already exists.
  - full-write (EMPLOYEE_DRIVE_FULL_WRITE): may also OVERWRITE an existing file's
    contents via /replace (gdrive.py `upload --file-id`). Still no delete — gdrive
    .py has no delete subcommand at all.

gdrive.py runs as a subprocess (NOT through the employee claude turn, which has
no shell), using an isolated token copy in the writable sandbox dir so OAuth
refresh can persist under the unit's ProtectHome=read-only.
"""
import asyncio
import os
import sys

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

HERE = os.path.dirname(os.path.abspath(__file__))
GDRIVE_BIN = os.path.join(HERE, "gdrive.py")
DRIVE_TOKEN = os.environ.get("EMPLOYEE_GDRIVE_TOKEN", "/data/employees/token.json")
DL_DIR = os.environ.get("EMPLOYEE_DL_DIR", "/data/employees/dl")
DEFAULT_PARENT = os.environ.get("EMPLOYEE_DRIVE_DEFAULT_FOLDER", "")
TELEGRAM_MAX = 4000

# Auth: fail CLOSED. Mirrors employee_bot.authorized without importing it.
_USERS = {int(x) for x in os.environ.get("EMPLOYEE_USERS", "").split(",") if x.strip()}
# Users allowed to OVERWRITE existing files (full write). Everyone else is create-only.
_FULL_WRITE = {int(x) for x in os.environ.get("EMPLOYEE_DRIVE_FULL_WRITE", "").split(",") if x.strip()}

# The ONLY subcommands this lane may run. No delete exists anywhere.
_ALLOWED = {"list", "find", "download", "mkdir", "upload"}


def _ok(update: Update) -> bool:
    u = update.effective_user
    return bool(_USERS and u and u.id in _USERS)


def _can_overwrite(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id in _FULL_WRITE)


async def _gdrive(args: list[str], timeout: int = 120, allow_overwrite: bool = False) -> str:
    """Run gdrive.py with the sandbox token. Enforces the write tier."""
    if not args or args[0] not in _ALLOWED:
        raise ValueError(f"drive subcommand not allowed: {args[:1]}")
    if args[0] == "upload" and "--file-id" in args and not allow_overwrite:
        # the one overwrite path in gdrive.py — only full-write users reach it.
        raise ValueError("overwriting an existing Drive file is not allowed")
    env = {**os.environ, "GCAL_TOKEN": DRIVE_TOKEN}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, GDRIVE_BIN, *args,
        cwd=HERE, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return "Drive command timed out."
    return out.decode("utf-8", errors="replace").strip() or "(no output)"


async def _reply_long(update: Update, text: str) -> None:
    for i in range(0, len(text), TELEGRAM_MAX):
        await update.message.reply_text(text[i:i + TELEGRAM_MAX])


async def _save_attachment(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> tuple[str, str] | None:
    """Download the message's document into the sandbox. Returns (local_path, name)."""
    doc = update.message.document
    if not doc:
        return None
    os.makedirs(DL_DIR, exist_ok=True)
    fn = doc.file_name or "upload.bin"
    local = os.path.join(DL_DIR, fn)
    tg_file = await ctx.bot.get_file(doc.file_id)
    data = bytes(await tg_file.download_as_bytearray())
    with open(local, "wb") as fh:
        fh.write(data)
    return local, fn


async def cmd_files(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """List Drive. No arg -> shared-with-me; 'root' -> My Drive; else a folder id."""
    if not _ok(update):
        return
    arg = ctx.args[0].strip() if ctx.args else ""
    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    if not arg or arg.lower() == "shared":
        args = ["list", "--shared", "--max", "50"]
    elif arg.lower() == "root":
        args = ["list", "--folder", "root", "--max", "50"]
    else:
        args = ["list", "--folder", arg, "--max", "50"]
    try:
        out = await _gdrive(args)
    except ValueError as e:
        out = str(e)
    await _reply_long(update, out)


async def cmd_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _ok(update):
        return
    name = " ".join(ctx.args).strip()
    if not name:
        await update.message.reply_text("Usage: /find <name to search for>")
        return
    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    out = await _gdrive(["find", "--name", name, "--max", "50"])
    await _reply_long(update, out)


async def cmd_get(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Download a Drive file by id and send it back over Telegram."""
    if not _ok(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /get <file_id>  (get a file id from /files or /find)")
        return
    fid = ctx.args[0].strip()
    os.makedirs(DL_DIR, exist_ok=True)
    out_path = os.path.join(DL_DIR, fid)
    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    msg = await _gdrive(["download", "--file-id", fid, "--out", out_path])
    if not os.path.exists(out_path):
        await update.message.reply_text(msg or "Download failed.")
        return
    try:
        with open(out_path, "rb") as fh:
            await update.message.reply_document(fh, filename=os.path.basename(out_path))
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(
            f"Downloaded, but couldn't send it via Telegram ({e}). "
            "It may exceed Telegram's ~50MB send limit."
        )
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass


async def cmd_mkdir(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/mkdir <name> | <parent_folder_id>  -> create a NEW folder."""
    if not _ok(update):
        return
    raw = " ".join(ctx.args).strip()
    if not raw:
        await update.message.reply_text(
            "Usage: /mkdir <folder name> | <parent_folder_id>\n"
            "(parent optional; get a folder id from /files or /find)"
        )
        return
    name, _, parent = raw.partition("|")
    name = name.strip()
    parent = parent.strip() or DEFAULT_PARENT
    if not name:
        await update.message.reply_text("Give the new folder a name.")
        return
    args = ["mkdir", "--name", name]
    if parent:
        args += ["--parent", parent]
    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    out = await _gdrive(args)
    await _reply_long(update, out)


async def handle_upload(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Document captioned '/upload <parent_folder_id>' -> create a NEW Drive file."""
    if not _ok(update):
        return
    caption = (update.message.caption or "").strip()
    parent = caption.partition(" ")[2].strip() or DEFAULT_PARENT
    if not parent:
        await update.message.reply_text(
            "Attach a file with caption:  /upload <destination_folder_id>\n"
            "Get a folder id with /files or /find."
        )
        return
    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    saved = await _save_attachment(update, ctx)
    if not saved:
        return
    local, fn = saved
    try:
        out = await _gdrive(["upload", "--file", local, "--parent", parent, "--name", fn])
    finally:
        try:
            os.remove(local)
        except OSError:
            pass
    await _reply_long(update, f"Uploaded as a NEW file:\n{out}")


async def handle_replace(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Document captioned '/replace <file_id>' -> OVERWRITE an existing file.

    Full-write users only; create-only users are refused.
    """
    if not _ok(update):
        return
    if not _can_overwrite(update):
        await update.message.reply_text(
            "You have create-only Drive access — you can't overwrite existing files. "
            "Use /upload to add a new file instead."
        )
        return
    caption = (update.message.caption or "").strip()
    fid = caption.partition(" ")[2].strip()
    if not fid:
        await update.message.reply_text(
            "Attach a file with caption:  /replace <file_id_to_overwrite>\n"
            "Get a file id with /find or /files."
        )
        return
    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)
    saved = await _save_attachment(update, ctx)
    if not saved:
        return
    local, fn = saved
    try:
        out = await _gdrive(
            ["upload", "--file", local, "--file-id", fid, "--name", fn],
            allow_overwrite=True,
        )
    finally:
        try:
            os.remove(local)
        except OSError:
            pass
    await _reply_long(update, f"Overwrote existing file {fid}:\n{out}")
