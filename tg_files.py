"""Send agent-produced files / images to Telegram.

Both Pedro front-ends (bot.py owner, employee_bot.py) talk to the user purely
through the *text* returned by the Claude subprocess (pedro_brain.run_claude).
That left no way for the agent to hand back a file or show an image. This module
adds one: the agent emits a marker line in its reply,

    [[SEND_FILE: /abs/path/to/file.png]]
    [[SEND_FILE: /abs/path/to/deck.pdf | optional caption]]

`deliver()` / `deliver_chat()` are drop-in replacements for the bots' chunked
reply_text loop: they pull those markers out, send each file to the chat (photo
for images, video for mp4/mov, document otherwise), then send whatever text is
left over.

`allow_root` is a security boundary: when set, only files that resolve to inside
that directory are sent. The employee bot is sandboxed and must never be able to
exfiltrate owner files (token.json, .env, Greg's inbox), so it passes its work
dir; the owner bot leaves it None and can send anything Greg owns.
"""
import logging
import os
import re

_log = logging.getLogger("nightshift.tgfiles")

TELEGRAM_MAX_MSG = 4000
# Telegram bot API upload ceiling for a normal bot.
_MAX_BYTES = 50 * 1024 * 1024

_MARKER = re.compile(r"\[\[\s*SEND_FILE\s*:\s*(.+?)\s*\]\]", re.IGNORECASE)
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}


def extract_files(text):
    """Return (cleaned_text, [(path, caption_or_None), ...]).

    Markers are stripped from the returned text; everything else is preserved.
    """
    files = []
    for m in _MARKER.finditer(text or ""):
        body = m.group(1).strip()
        if "|" in body:
            path, caption = body.split("|", 1)
            files.append((path.strip(), caption.strip() or None))
        else:
            files.append((body, None))
    cleaned = _MARKER.sub("", text or "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, files


def _resolve(path, allow_root):
    """Realpath the target; if allow_root is set, reject anything outside it."""
    rp = os.path.realpath(path)
    if allow_root is not None:
        root = os.path.realpath(allow_root)
        if rp != root and not rp.startswith(root + os.sep):
            return None
    return rp


async def _send_one(sender, path, caption, allow_root):
    """sender is a Sender bound to a chat (see _MsgSender / _ChatSender)."""
    rp = _resolve(path, allow_root)
    if rp is None:
        await sender.text(f"⚠️ Can't send {path} — outside my allowed folder.")
        return
    if not os.path.isfile(rp):
        await sender.text(f"⚠️ I pointed at a file that isn't there: {path}")
        return
    size = os.path.getsize(rp)
    if size > _MAX_BYTES:
        await sender.text(
            f"⚠️ {os.path.basename(rp)} is {size // (1024 * 1024)}MB — over "
            "Telegram's 50MB bot limit, so I couldn't attach it. It's still on "
            "the server / in Drive."
        )
        return
    ext = os.path.splitext(rp)[1].lower()
    try:
        with open(rp, "rb") as fh:
            if ext in _IMAGE_EXT:
                await sender.photo(fh, caption)
            elif ext in _VIDEO_EXT:
                await sender.video(fh, caption)
            else:
                await sender.document(fh, caption, os.path.basename(rp))
    except Exception:  # noqa: BLE001
        _log.exception("failed to send file %s", rp)
        await sender.text(f"⚠️ Couldn't upload {os.path.basename(rp)} to Telegram.")


class _MsgSender:
    """Send back to the chat that produced `message` (reply_* methods)."""
    def __init__(self, message):
        self.m = message

    async def text(self, t):
        await self.m.reply_text(t)

    async def photo(self, fh, caption):
        await self.m.reply_photo(fh, caption=caption)

    async def video(self, fh, caption):
        await self.m.reply_video(fh, caption=caption, supports_streaming=True)

    async def document(self, fh, caption, filename):
        await self.m.reply_document(fh, caption=caption, filename=filename)


class _ChatSender:
    """Send to an explicit chat_id via the Bot object (background tasks)."""
    def __init__(self, bot, chat_id):
        self.bot = bot
        self.chat_id = chat_id

    async def text(self, t):
        await self.bot.send_message(self.chat_id, t)

    async def photo(self, fh, caption):
        await self.bot.send_photo(self.chat_id, fh, caption=caption)

    async def video(self, fh, caption):
        await self.bot.send_video(self.chat_id, fh, caption=caption,
                                  supports_streaming=True)

    async def document(self, fh, caption, filename):
        await self.bot.send_document(self.chat_id, fh, caption=caption,
                                    filename=filename)


async def _deliver(sender, text, allow_root):
    cleaned, files = extract_files(text)
    for path, caption in files:
        await _send_one(sender, path, caption, allow_root)
    body = cleaned if (cleaned or files) else (text or "")
    if body:
        for i in range(0, len(body), TELEGRAM_MAX_MSG):
            await sender.text(body[i:i + TELEGRAM_MAX_MSG])


async def deliver(message, text, allow_root=None):
    """Send [[SEND_FILE]] attachments in `text`, then the remaining text, as a
    reply to `message`. Drop-in for the bots' chunked reply_text loop."""
    await _deliver(_MsgSender(message), text, allow_root)


async def deliver_chat(bot, chat_id, text, allow_root=None):
    """Same as deliver(), but to an explicit chat_id via the Bot object — for
    background tasks that don't hold the original Update/message."""
    await _deliver(_ChatSender(bot, chat_id), text, allow_root)
