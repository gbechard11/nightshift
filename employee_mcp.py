"""Remote MCP server exposing the NS bot's SAFE capabilities to employees'
Claude apps (custom connector / sidebar).

Security model:
  * OAuth 2.1 + PKCE (S256) is mandatory for Claude custom connectors. This file
    implements a minimal authorization server via the MCP SDK's provider hooks.
  * Login is NOT a password. The employee runs /connect in the NS Telegram bot,
    which mints a one-time code; they paste it on this server's /login page. That
    reuses their existing Telegram trust and binds the Claude session to their
    Telegram user id (the token's `subject`).
  * Tools are SAFE-ONLY: read/browse/search Drive, create NEW folders/files, and
    send email from the employee's OWN identity. There is deliberately NO
    overwrite/replace and NO delete tool here -- not even for full-write users --
    so a connected Claude can never change or destroy anything already in Drive.

Binds 127.0.0.1 by default; exposure is a separate, explicit step (Tailscale
Funnel on :8443 -> this port). See nightshift-mcp.service and the runbook.
"""
import json
import os
import secrets
import subprocess
import sys
import time
import zoneinfo
from dataclasses import dataclass, field
from datetime import datetime

import mailer
import employee_email
import pending_email

from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

import asyncio

# --------------------------------------------------------------------------- #
# Advertise PUBLIC-client auth ("none") in the OAuth authorization-server
# metadata. The MCP SDK (pinned 1.27.2) hardcodes
# token_endpoint_auth_methods_supported to only ["client_secret_post",
# "client_secret_basic"] in build_metadata(). Claude's custom-connector client
# registers as a PUBLIC PKCE client (token_endpoint_auth_method="none"); when it
# reads our metadata and doesn't find "none", it ABORTS before ever calling
# /register and surfaces a generic "Couldn't reach the MCP server" error. Our
# /register handler already accepts "none" -- only the advertisement was missing.
# Wrap build_metadata so the served metadata lists "none" too. create_auth_routes
# calls build_metadata as a module global at app-build time, so patching the
# module attribute here (import time) takes effect.
# --------------------------------------------------------------------------- #
import mcp.server.auth.routes as _auth_routes

_orig_build_metadata = _auth_routes.build_metadata


def _build_metadata_with_none(*args, **kwargs):
    md = _orig_build_metadata(*args, **kwargs)
    methods = list(md.token_endpoint_auth_methods_supported or [])
    if "none" not in methods:
        methods.append("none")
    md.token_endpoint_auth_methods_supported = methods
    return md


_auth_routes.build_metadata = _build_metadata_with_none

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
# Public base URL Claude reaches us at. For LOCAL testing this is the loopback
# address; going live = set MCP_PUBLIC_URL to the Funnel URL (https, :8443).
PUBLIC_URL = os.environ.get("MCP_PUBLIC_URL", "http://127.0.0.1:8780").rstrip("/")
HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8780"))
SCOPE = "nsbot"

HERE = os.path.dirname(os.path.abspath(__file__))
GDRIVE_BIN = os.path.join(HERE, "gdrive.py")
DRIVE_TOKEN = os.environ.get("EMPLOYEE_GDRIVE_TOKEN", "/data/employees/token.json")
DL_DIR = os.environ.get("EMPLOYEE_DL_DIR", "/data/employees/dl")

STATE_DIR = os.environ.get("EMPLOYEE_STATE_DIR", "/data/employees")
STORE_PATH = os.path.join(STATE_DIR, "mcp-store.json")          # clients + tokens
CONNECT_PATH = os.path.join(STATE_DIR, "mcp-connect-codes.json")  # written by the bot

_USERS = {int(x) for x in os.environ.get("EMPLOYEE_USERS", "").split(",") if x.strip()}

# The only gdrive.py subcommands this server may run. No --file-id (overwrite),
# no delete (gdrive.py has none). READ + CREATE only.
_ALLOWED = {"list", "find", "download", "mkdir", "upload"}

# --------------------------------------------------------------------------- #
# Brain (assistant knowledge base) access.
#   * Greg (OWNER_UID) reads/appends his REAL personal brain at /data/greg/brain.
#   * Every other connected user reads/appends a SHARED TEAM brain at
#     /data/employees/brain -- they never see or touch Greg's private brain.
# Writes are APPEND-ONLY: a new dated entry is added (atomically, with an
# automatic timestamped backup); existing notes are never edited or deleted.
# This mirrors the create-only Drive model -- nothing already written can be
# destroyed through this connector.
# --------------------------------------------------------------------------- #
OWNER_UID = int(
    (os.environ.get("OWNER_UID")
     or os.environ.get("ALLOWED_USERS", "").split(",")[0]
     or "0").strip() or 0
)
GREG_BRAIN_DIR = os.environ.get("GREG_BRAIN_DIR", "/data/greg/brain")
TEAM_BRAIN_DIR = os.environ.get("TEAM_BRAIN_DIR", "/data/employees/brain")


# --------------------------------------------------------------------------- #
# Tiny persistent store (clients + tokens survive restarts; codes are ephemeral)
# --------------------------------------------------------------------------- #
@dataclass
class _Store:
    clients: dict = field(default_factory=dict)        # client_id -> client json
    access: dict = field(default_factory=dict)         # token -> {subject, client_id, scopes, exp}
    refresh: dict = field(default_factory=dict)        # token -> {subject, client_id, scopes}
    auth_codes: dict = field(default_factory=dict)     # code  -> AuthorizationCode-ish dict (ephemeral)
    pending: dict = field(default_factory=dict)        # login_id -> params snapshot (ephemeral)

    def load(self) -> None:
        try:
            with open(STORE_PATH, encoding="utf-8") as fh:
                d = json.load(fh)
            self.clients = d.get("clients", {})
            self.access = d.get("access", {})
            self.refresh = d.get("refresh", {})
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STORE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {"clients": self.clients, "access": self.access, "refresh": self.refresh},
                fh,
            )
        os.replace(tmp, STORE_PATH)
        try:
            os.chmod(STORE_PATH, 0o600)
        except OSError:
            pass


STORE = _Store()
STORE.load()


def _consume_connect_code(code: str) -> int | None:
    """Validate a one-time code minted by the bot's /connect; return uid, once."""
    code = (code or "").strip()
    if not code:
        return None
    try:
        with open(CONNECT_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    rec = data.get(code)
    now = int(time.time())
    if not rec or int(rec.get("exp", 0)) < now:
        return None
    uid = int(rec["uid"])
    # one-time: remove it and any other expired codes
    data = {k: v for k, v in data.items() if k != code and int(v.get("exp", 0)) >= now}
    tmp = CONNECT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, CONNECT_PATH)
    try:
        os.chmod(CONNECT_PATH, 0o600)
    except OSError:
        pass
    return uid if uid in _USERS else None


# --------------------------------------------------------------------------- #
# OAuth provider
# --------------------------------------------------------------------------- #
class NSProvider(OAuthAuthorizationServerProvider):
    async def get_client(self, client_id: str):
        c = STORE.clients.get(client_id)
        return OAuthClientInformationFull.model_validate(c) if c else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        STORE.clients[client_info.client_id] = client_info.model_dump(mode="json")
        STORE.save()

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        login_id = secrets.token_urlsafe(24)
        STORE.pending[login_id] = {
            "client_id": client.client_id,
            "scopes": params.scopes or [SCOPE],
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "state": params.state,
            "resource": params.resource,
            "exp": int(time.time()) + 600,
        }
        return f"{PUBLIC_URL}/login?lid={login_id}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        rec = STORE.auth_codes.get(authorization_code)
        if not rec or rec["client_id"] != client.client_id or rec["expires_at"] < time.time():
            return None
        return AuthorizationCode(**rec)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code) -> OAuthToken:
        STORE.auth_codes.pop(authorization_code.code, None)
        return self._issue(client.client_id, authorization_code.subject, authorization_code.scopes)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        rec = STORE.refresh.get(refresh_token)
        if not rec or rec["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=rec["client_id"],
                            scopes=rec["scopes"], expires_at=None)

    async def exchange_refresh_token(self, client, refresh_token, scopes) -> OAuthToken:
        rec = STORE.refresh.pop(refresh_token.token, None)  # rotate
        STORE.save()
        subject = rec["subject"] if rec else None
        use_scopes = scopes or (rec["scopes"] if rec else [SCOPE])
        return self._issue(client.client_id, subject, use_scopes)

    async def load_access_token(self, token: str):
        rec = STORE.access.get(token)
        if not rec or rec["exp"] < time.time():
            return None
        return AccessToken(token=token, client_id=rec["client_id"], scopes=rec["scopes"],
                           expires_at=rec["exp"], subject=str(rec["subject"]),
                           claims={"uid": rec["subject"]})

    async def revoke_token(self, token) -> None:
        STORE.access.pop(getattr(token, "token", token), None)
        STORE.refresh.pop(getattr(token, "token", token), None)
        STORE.save()

    def _issue(self, client_id: str, subject, scopes) -> OAuthToken:
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        exp = int(time.time()) + 3600
        STORE.access[access] = {"subject": subject, "client_id": client_id, "scopes": scopes, "exp": exp}
        STORE.refresh[refresh] = {"subject": subject, "client_id": client_id, "scopes": scopes}
        STORE.save()
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=3600,
                          scope=" ".join(scopes), refresh_token=refresh)


# --------------------------------------------------------------------------- #
# FastMCP app
# --------------------------------------------------------------------------- #
# DNS-rebinding protection trusts only loopback by default, which 421s the
# public Funnel host. Allow the Funnel host (derived from PUBLIC_URL) plus
# loopback so both the live connector and local tests pass. Claude connects
# server-side (no Origin header), so Origin validation passes when absent.
from urllib.parse import urlparse as _urlparse

_pub = _urlparse(PUBLIC_URL)
_ALLOWED_HOSTS = list(dict.fromkeys(filter(None, [
    _pub.netloc, _pub.hostname, f"{HOST}:{PORT}", "127.0.0.1:8780", "localhost:8780",
])))
_ALLOWED_ORIGINS = list(dict.fromkeys(filter(None, [
    PUBLIC_URL, f"{_pub.scheme}://{_pub.hostname}" if _pub.hostname else None,
])))

mcp = FastMCP(
    name="Nightshift Team Bot",
    instructions="Browse/search/read Greg's Google Drive, create new folders/files, "
                 "send email from your own Nightshift address, and read/append "
                 "the team brain (shared knowledge base) with brain_list / "
                 "brain_read / brain_append. Cannot edit, overwrite, or delete "
                 "anything that already exists.",
    auth_server_provider=NSProvider(),
    auth=AuthSettings(
        issuer_url=PUBLIC_URL,
        resource_server_url=f"{PUBLIC_URL}/mcp",
        required_scopes=[SCOPE],
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
        ),
    ),
    host=HOST,
    port=PORT,
    streamable_http_path="/mcp",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_ALLOWED_HOSTS,
        allowed_origins=_ALLOWED_ORIGINS,
    ),
)


def _uid() -> int:
    at = get_access_token()
    if not at or at.subject is None:
        raise ValueError("Not authenticated.")
    return int(at.subject)


# --------------------------------------------------------------------------- #
# Brain helpers
# --------------------------------------------------------------------------- #
def _brain_root(uid: int) -> tuple[str, str, bool]:
    """Return (root_dir, main_log_path, is_owner) for this caller's brain.
    Greg gets his real personal brain; everyone else the shared team brain."""
    if uid == OWNER_UID:
        return GREG_BRAIN_DIR, os.path.join(GREG_BRAIN_DIR, "BRAIN.md"), True
    return TEAM_BRAIN_DIR, os.path.join(TEAM_BRAIN_DIR, "TEAM_BRAIN.md"), False


def _brain_resolve(root: str, name: str) -> str:
    """Resolve a brain filename safely inside root (blocks path traversal)."""
    name = (name or "").strip().lstrip("/").lstrip("\\")
    cand = os.path.normpath(os.path.join(root, name))
    if cand != os.path.normpath(root) and not cand.startswith(os.path.normpath(root) + os.sep):
        raise ValueError("That path is outside your brain.")
    return cand


def _brain_who(uid: int) -> str:
    """Human label for brain-entry attribution."""
    if uid == OWNER_UID:
        return "Greg"
    try:
        sender = employee_email.sender_for(uid)
        if sender and sender.get("from"):
            return sender["from"]
    except Exception:
        pass
    return f"uid {uid}"


def _brain_append_entry(uid: int, text: str) -> str:
    """Append a dated, attributed entry to the caller's brain log. Append-only:
    a timestamped .bak is written first, then the file is rewritten atomically
    with the new entry added (top for Greg's newest-first log, else bottom)."""
    root, main, is_owner = _brain_root(uid)
    os.makedirs(root, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    day = datetime.now().strftime("%Y-%m-%d")
    who = _brain_who(uid)
    entry = f"## {day} [note] (via {who} @ {stamp})\n\n{text.strip()}\n"

    old = ""
    if os.path.exists(main):
        with open(main, encoding="utf-8") as fh:
            old = fh.read()
        # safety backup before any rewrite -- never lose existing notes
        bak = f"{main}.bak-{int(time.time())}"
        with open(bak, "w", encoding="utf-8") as fh:
            fh.write(old)

    if is_owner and "\n---\n" in old:
        # Greg's BRAIN.md keeps newest entries on top, just under the preamble.
        head, rest = old.split("\n---\n", 1)
        new = f"{head}\n---\n\n{entry}\n{rest.lstrip()}"
    elif old:
        new = old.rstrip() + "\n\n" + entry
    else:
        title = "Brain — Greg" if is_owner else "Nightshift Team Brain"
        new = (f"# {title}\n\nRunning log. Header format: "
               f"`## YYYY-MM-DD [tag] headline`.\n\n---\n\n{entry}")

    tmp = main + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(new)
    os.replace(tmp, main)
    return f"Added to {'your personal brain' if is_owner else 'the team brain'} ({os.path.basename(main)})."


async def _gdrive(args: list[str], timeout: int = 120) -> str:
    if not args or args[0] not in _ALLOWED:
        raise ValueError(f"drive subcommand not allowed: {args[:1]}")
    if args[0] == "upload" and "--file-id" in args:  # the only overwrite path
        raise ValueError("overwriting existing Drive files is not allowed here")
    env = {**os.environ, "GCAL_TOKEN": DRIVE_TOKEN}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, GDRIVE_BIN, *args, cwd=HERE, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return "Drive command timed out."
    return out.decode("utf-8", errors="replace").strip() or "(no output)"


@mcp.tool()
async def whoami() -> str:
    """Show which Nightshift identity this connector is signed in as."""
    return f"Signed in as Nightshift Telegram user id {_uid()}."


@mcp.tool()
async def drive_list(folder_id: str = "") -> str:
    """List Google Drive items. No folder_id (or 'root') lists the top level
    (My Drive root, which includes the folders shared into it); otherwise pass
    a folder id from a previous listing to look inside it."""
    _uid()
    arg = folder_id.strip()
    if not arg or arg.lower() in ("root", "shared"):
        folder = "root"
    else:
        folder = arg
    return await _gdrive(["list", "--folder", folder, "--max", "50"])


@mcp.tool()
async def drive_find(query: str) -> str:
    """Search across all of Greg's Drive for files/folders whose name matches."""
    _uid()
    if not query.strip():
        raise ValueError("Provide a name to search for.")
    return await _gdrive(["find", "--name", query.strip(), "--max", "50"])


@mcp.tool()
async def drive_read_text(file_id: str) -> str:
    """Download a Drive file by id and return its text contents. Binary files
    (images, PDFs) are reported but not returned -- use the Telegram bot's /get."""
    _uid()
    fid = file_id.strip()
    if not fid:
        raise ValueError("Provide a file id (from drive_list / drive_find).")
    os.makedirs(DL_DIR, exist_ok=True)
    out_path = os.path.join(DL_DIR, "mcp_" + secrets.token_hex(8))
    msg = await _gdrive(["download", "--file-id", fid, "--out", out_path])
    if not os.path.exists(out_path):
        return msg or "Download failed."
    try:
        data = open(out_path, "rb").read()
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return f"That file is binary ({len(data)} bytes) — open it via the Telegram bot's /get {fid}."
    return text[:100_000]


@mcp.tool()
async def drive_make_folder(name: str, parent_id: str = "") -> str:
    """Create a NEW folder in Drive. parent_id optional (omit for My Drive root)."""
    _uid()
    if not name.strip():
        raise ValueError("Give the new folder a name.")
    args = ["mkdir", "--name", name.strip()]
    if parent_id.strip():
        args += ["--parent", parent_id.strip()]
    return await _gdrive(args)


@mcp.tool()
async def drive_create_text_file(name: str, content: str, parent_id: str = "") -> str:
    """Create a NEW text file in Drive with the given contents. Never overwrites
    an existing file (create-only)."""
    _uid()
    if not name.strip():
        raise ValueError("Give the file a name.")
    os.makedirs(DL_DIR, exist_ok=True)
    local = os.path.join(DL_DIR, "mcp_" + secrets.token_hex(8))
    with open(local, "w", encoding="utf-8") as fh:
        fh.write(content)
    try:
        args = ["upload", "--file", local, "--name", name.strip()]
        if parent_id.strip():
            args += ["--parent", parent_id.strip()]
        out = await _gdrive(args)
    finally:
        try:
            os.remove(local)
        except OSError:
            pass
    return f"Created new file:\n{out}"


@mcp.tool()
async def email_send(to: str, subject: str, body: str) -> str:
    """Send a plain-text email FROM your own configured Nightshift address.
    If you haven't set up your email yet, run /setupemail in the Telegram bot."""
    uid = _uid()
    sender = employee_email.sender_for(uid)
    if not sender:
        raise ValueError("You have no sending address set up. Run /setupemail in the NS Telegram bot first.")
    recipients = [r.strip() for r in to.replace(";", ",").split(",") if r.strip()]
    if not recipients:
        raise ValueError("No recipient address given.")
    token = pending_email.stage(uid, sender.get("from"), recipients, [], subject, body)
    ok = await asyncio.to_thread(pending_email.send_confirm_prompt, pending_email.load(token))
    if not ok:
        pending_email.discard(token)
        return ("I prepared the email but couldn't reach you on Telegram to confirm it. "
                "Open the NS Team Bot in Telegram (send /start) and try again.")
    return ("Staged for your confirmation -- NOT sent. I've sent the exact draft to your "
            "Telegram (NS Team Bot) with a Send / Cancel button. Tap Send there to send it; "
            "nothing goes out until you do. Do not tell the user it was already sent.")


@mcp.tool()
async def brain_list() -> str:
    """List the notes in your brain (the assistant's knowledge base). Greg sees
    his personal brain; team members see the shared Nightshift team brain.
    Returns each file's path (relative to the brain) and size."""
    uid = _uid()
    root, main, is_owner = _brain_root(uid)
    if not os.path.isdir(root):
        which = "personal brain" if is_owner else "team brain"
        return f"Your {which} is empty so far. Use brain_append to add the first note."
    rows = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in sorted(files):
            if fn.endswith((".tmp",)) or ".bak-" in fn:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            rows.append(f"  {rel}  ({size:,} bytes)")
    label = "Your personal brain" if is_owner else "Nightshift team brain"
    body = "\n".join(sorted(rows)) or "  (empty)"
    return f"{label} — read any with brain_read(name=...):\n{body}"


@mcp.tool()
async def brain_read(name: str = "") -> str:
    """Read a note from your brain. With no name, returns the main running log
    (Greg: BRAIN.md; team: TEAM_BRAIN.md). Pass a path from brain_list (e.g.
    'memory/project_nightshift.md') to read a specific note."""
    uid = _uid()
    root, main, is_owner = _brain_root(uid)
    path = main if not name.strip() else _brain_resolve(root, name)
    if not os.path.exists(path):
        return f"No such note: {name or os.path.basename(main)}. Use brain_list to see what's there."
    if os.path.isdir(path):
        raise ValueError("That's a folder, not a note. Use brain_list.")
    with open(path, "rb") as fh:
        data = fh.read()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return f"That note is binary ({len(data)} bytes) and can't be shown as text."
    return text[:100_000]


@mcp.tool()
async def brain_append(text: str) -> str:
    """Add a new dated, attributed note to your brain's running log. This ONLY
    ADDS — it never edits or deletes anything already written (the file is
    backed up before each write). Greg's notes go to his personal brain; team
    members' notes go to the shared team brain."""
    uid = _uid()
    if not text.strip():
        raise ValueError("Give me the note text to add.")
    return await asyncio.to_thread(_brain_append_entry, uid, text)


# --------------------------------------------------------------------------- #
# Login page (where the employee pastes the /connect code)
# --------------------------------------------------------------------------- #
_LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect Nightshift Team Bot</title>
<style>body{{font-family:system-ui,sans-serif;background:#0b0b0c;color:#eee;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}}
.card{{background:#161618;padding:28px 26px;border-radius:14px;max-width:360px;width:90%}}
h1{{font-size:18px;margin:0 0 6px}}p{{color:#aaa;font-size:14px;line-height:1.5}}
input{{width:100%;box-sizing:border-box;padding:12px;font-size:18px;letter-spacing:3px;
text-align:center;border-radius:10px;border:1px solid #333;background:#0b0b0c;color:#fff;margin:14px 0}}
button{{width:100%;padding:12px;font-size:15px;border:0;border-radius:10px;background:#5b8cff;color:#fff;cursor:pointer}}
.err{{color:#ff6b6b;font-size:13px}}</style></head><body><div class="card">
<h1>Connect Nightshift Team Bot</h1>
<p>In the Nightshift Telegram bot, send <b>/connect</b>. It replies with a 6-digit code. Enter it below.</p>
{err}<form method="post" action="/login">
<input type="hidden" name="lid" value="{lid}">
<input name="code" inputmode="numeric" autocomplete="one-time-code" placeholder="000000" autofocus>
<button type="submit">Connect</button></form></div></body></html>"""



# --------------------------------------------------------------------------- #
# Envato Elements: search the company subscription + download assets to Drive.
# Shares Greg's seeded session (envato_cookies.json); downloads land in the
# shared "Envato Assets" Drive folder. SAFE: search + download only. The session
# is seeded only by Greg via the Telegram bot's /envatologin.
# --------------------------------------------------------------------------- #
ENVATO_BIN = os.path.join(HERE, "envato.py")
ENVATO_TOKEN = os.path.join(HERE, "token.json")  # company Drive token (shared folder)
ENVATO_ALLOWED = {"search", "download", "status", "suggest"}


async def _envato(args, timeout=600):
    if not args or args[0] not in ENVATO_ALLOWED:
        raise ValueError("envato subcommand not allowed: %s" % args[:1])
    env = {**os.environ, "GCAL_TOKEN": ENVATO_TOKEN}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, ENVATO_BIN, *args, cwd=HERE, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return "Envato command timed out."
    return out.decode("utf-8", errors="replace").strip() or "(no output)"


@mcp.tool()
async def envato_search(query: str, item_type: str = "") -> str:
    """Search the Nightshift Envato Elements subscription (stock video, video
    templates, fonts, graphics, music, sound effects, photos). Returns matching
    item ids + links. Optional item_type narrows results: stock-video, fonts,
    graphics, music, video-templates, sound-effects, photos. Then call
    envato_download to pull one into the shared Drive."""
    _uid()
    if not query.strip():
        raise ValueError("Provide search terms.")
    args = ["search", query.strip(), "--json", "--limit", "15"]
    if item_type.strip():
        args += ["--type", item_type.strip()]
    return await _envato(args, timeout=120)


@mcp.tool()
async def envato_download(url_or_id: str) -> str:
    """Download an Envato Elements asset (item URL or id from envato_search) and
    save it into the shared 'Envato Assets' Google Drive folder; returns the
    Drive link. Licensed under the company subscription."""
    _uid()
    if not url_or_id.strip():
        raise ValueError("Provide an Envato item URL or id (from envato_search).")
    return await _envato(["download", url_or_id.strip(), "--to-drive", "--json"], timeout=900)


# --------------------------------------------------------------------------- #
# ROSTR (rostr.cc) — music-industry intelligence for offer creation.
# Read-only. Search is public (Typesense); profile/team/tours/company need the
# session Greg seeds via the bot's /rostrlogin. Same subprocess pattern as Envato.
# --------------------------------------------------------------------------- #
ROSTR_BIN = os.path.join(HERE, "rostr.py")
ROSTR_ALLOWED = {"search", "artist", "team", "tours", "company", "brief", "status"}


async def _rostr(args, timeout=120):
    if not args or args[0] not in ROSTR_ALLOWED:
        raise ValueError("rostr subcommand not allowed: %s" % args[:1])
    proc = await asyncio.create_subprocess_exec(
        sys.executable, ROSTR_BIN, *args, cwd=HERE, env={**os.environ},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return "ROSTR command timed out."
    return out.decode("utf-8", errors="replace").strip() or "(no output)"


@mcp.tool()
async def rostr_offer_brief(artist: str) -> str:
    """ROSTR offer brief for an artist — the one-stop pull for building an offer.
    Resolves the artist by name and returns: who to reach out to (booking AGENT and
    MANAGER, with names, emails and territories), audience/market metrics (Spotify,
    Instagram, YouTube, TikTok), touring status, and recent show history. Use this
    first when Seba or staff want to make an offer to an artist."""
    _uid()
    if not artist.strip():
        raise ValueError("Provide an artist name.")
    return await _rostr(["brief", artist.strip()], timeout=150)


@mcp.tool()
async def rostr_search(query: str, kind: str = "") -> str:
    """Search ROSTR for an artist, company (agency/label/management) or person.
    Returns names + ROSTR slugs to use with the other tools. kind: artist|company|
    person (optional). Public data — works without a seeded session."""
    _uid()
    if not query.strip():
        raise ValueError("Provide search terms.")
    args = ["search", query.strip()]
    if kind.strip():
        args += ["--type", kind.strip()]
    return await _rostr(args)


@mcp.tool()
async def rostr_team(artist: str) -> str:
    """ROSTR booking AGENT + MANAGER for an artist: company, the specific people,
    their emails and territories — i.e. exactly who an offer is sent to. Resolves
    the artist by name or slug."""
    _uid()
    if not artist.strip():
        raise ValueError("Provide an artist name or slug.")
    return await _rostr(["team", artist.strip()])


@mcp.tool()
async def rostr_artist(artist: str) -> str:
    """ROSTR artist profile: audience metrics (Spotify/Instagram/YouTube/TikTok),
    genres, type, origin, touring status and bio. Resolves by name or slug."""
    _uid()
    if not artist.strip():
        raise ValueError("Provide an artist name or slug.")
    return await _rostr(["artist", artist.strip()])


@mcp.tool()
async def rostr_tours(artist: str) -> str:
    """ROSTR tour / show history for an artist: dates, venues, cities, countries —
    useful for routing and gauging draw. Resolves by name or slug."""
    _uid()
    if not artist.strip():
        raise ValueError("Provide an artist name or slug.")
    return await _rostr(["tours", artist.strip()])


@mcp.tool()
async def rostr_company(company: str) -> str:
    """ROSTR company profile (agency / label / management): locations, website and
    staff roster (agents/managers who work there). Resolves by name or slug."""
    _uid()
    if not company.strip():
        raise ValueError("Provide a company name or slug.")
    return await _rostr(["company", company.strip()])


@mcp.custom_route("/login", methods=["GET"])
async def login_get(request: Request) -> HTMLResponse:
    lid = request.query_params.get("lid", "")
    if lid not in STORE.pending:
        return HTMLResponse("<p>This login link expired. Start again from your Claude app.</p>", status_code=400)
    return HTMLResponse(_LOGIN_HTML.format(lid=lid, err=""))


@mcp.custom_route("/login", methods=["POST"])
async def login_post(request: Request):
    form = await request.form()
    lid = str(form.get("lid", ""))
    code = str(form.get("code", ""))
    pend = STORE.pending.get(lid)
    if not pend or pend["exp"] < time.time():
        return HTMLResponse("<p>This login expired. Start again from your Claude app.</p>", status_code=400)
    uid = _consume_connect_code(code)
    if uid is None:
        err = '<p class="err">That code is wrong or expired. Send /connect again in the bot.</p>'
        return HTMLResponse(_LOGIN_HTML.format(lid=lid, err=err), status_code=401)

    STORE.pending.pop(lid, None)
    auth_code = secrets.token_urlsafe(24)
    STORE.auth_codes[auth_code] = {
        "code": auth_code,
        "scopes": pend["scopes"],
        "expires_at": time.time() + 300,
        "client_id": pend["client_id"],
        "code_challenge": pend["code_challenge"],
        "redirect_uri": pend["redirect_uri"],
        "redirect_uri_provided_explicitly": pend["redirect_uri_provided_explicitly"],
        "resource": pend.get("resource"),
        "subject": str(uid),
    }
    redirect = construct_redirect_uri(pend["redirect_uri"], code=auth_code, state=pend["state"])
    return RedirectResponse(url=redirect, status_code=302)


# --------------------------------------------------------------------------- #
# Guest List — Ne-Yo @ Pawn Shop Live, June 19 2026
# Closes 8:00 PM MDT. Access-code gated. Up to 2 guest names per email/cell;
# resubmitting the same email or cell tops you up to (never past) 2 names.
# Submissions -> /data/greg/blast_queue/guestlist_neyo_20260619.json
# --------------------------------------------------------------------------- #
_GL_FILE = "/data/greg/blast_queue/guestlist_neyo_20260619.json"
_GL_CLOSE_EPOCH = 1781920800  # 2026-06-19 20:00 MDT = 2026-06-20 02:00 UTC
_GL_IMG = "/data/greg/neyo_promo/neyo_ps_9x16.png"
_GL_MAX = 2  # max names per email/cell
_GL_CODES = {"JEUNETAGS", "NSENT", "MAKORE"}  # valid access codes
_EMAIL_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "email_send.py")

_GL_FORM = """<!doctype html><html lang=en><head>
<meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Guest List — Ne-Yo @ Pawn Shop</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:#0a0a0c;color:#f0f0f0;min-height:100vh;
  display:flex;align-items:center;justify-content:center;padding:20px}}
.wrap{{max-width:420px;width:100%}}
.brand{{color:#888;font-size:11px;letter-spacing:.15em;text-transform:uppercase;margin-bottom:10px}}
h1{{font-size:26px;font-weight:700;line-height:1.2;margin-bottom:6px}}
.sub{{color:#aaa;font-size:15px;margin-bottom:28px}}
label{{display:block;font-size:13px;color:#999;margin-bottom:6px;margin-top:18px}}
input{{width:100%;background:#1a1a1f;border:1px solid #333;border-radius:8px;
  color:#f0f0f0;font-size:16px;padding:14px 16px;outline:none;transition:border .15s}}
input:focus{{border-color:#666}}
.code-in{{text-transform:uppercase;letter-spacing:.08em;font-weight:600}}
.req{{color:#e05555;font-size:12px;margin-top:4px}}
.hint{{color:#777;font-size:12px;margin-top:4px}}
.opt-in{{font-size:12px;color:#666;margin-top:22px;line-height:1.6;
  padding:14px;background:#111;border-radius:8px;border:1px solid #222}}
.opt-in a{{color:#888}}
button{{width:100%;margin-top:24px;background:#fff;color:#000;border:none;
  border-radius:8px;font-size:16px;font-weight:700;padding:16px;cursor:pointer;
  letter-spacing:.02em;transition:opacity .15s}}
button:hover{{opacity:.85}}
.note{{font-size:12px;color:#555;text-align:center;margin-top:12px}}
.err{{color:#e05555;font-size:13px;margin-top:16px;padding:12px;
  background:#1a0a0a;border-radius:8px;border:1px solid #4a1a1a}}
</style></head>
<body><div class=wrap>
<img src=/guestlist/img alt="Ne-Yo" style="width:100%;border-radius:12px;margin-bottom:20px;display:block">
<div class=brand>Pawn Shop Live · Edmonton</div>
<h1>Ne-Yo<br>Guest List</h1>
<div class=sub>Tonight — Friday, June 19, 2026</div>
{err}
<form method=POST action=/guestlist>
  <label>Access Code <span style="color:#e05555">*</span></label>
  <input name=code type=text class=code-in placeholder="Enter your access code" required maxlength=40 value="{code}">
  <div class=hint>You need a valid code from your promoter to submit.</div>
  <label>Guest Name 1 <span style="color:#e05555">*</span></label>
  <input name=name1 type=text placeholder="First &amp; last name" required maxlength=120 value="{name1}">
  <label>Guest Name 2 <span style="color:#777">(optional)</span></label>
  <input name=name2 type=text placeholder="Bringing someone? Add their name" maxlength=120 value="{name2}">
  <div class=hint>Up to 2 names per email / cell.</div>
  <label>Cell Phone</label>
  <input name=phone type=tel placeholder="+1 (780) 555-0000" maxlength=30 value="{phone}">
  <label>Email Address</label>
  <input name=email type=email placeholder="you@example.com" maxlength=200 value="{email}">
  <div class=req>Cell phone or email required</div>
  <div class=opt-in>By submitting this form you consent to receive further communications
  and marketing from Nightshift Entertainment and Pawn Shop Live, including upcoming
  event announcements, promotions, and exclusive offers. You may unsubscribe at any time.</div>
  <button type=submit>Add Us to the Guest List</button>
</form>
<div class=note>Guest list closes at 8:00 PM tonight</div>
</div></body></html>"""

_GL_THANKS = """<!doctype html><html lang=en><head>
<meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>You're on the list!</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:#0a0a0c;color:#f0f0f0;min-height:100vh;
  display:flex;align-items:center;justify-content:center;padding:20px;text-align:center}}
.wrap{{max-width:380px}}
.check{{font-size:52px;margin-bottom:16px}}
h1{{font-size:24px;font-weight:700;margin-bottom:10px}}
p{{color:#999;font-size:15px;line-height:1.6}}
.names{{margin:18px 0;padding:14px;background:#111;border:1px solid #222;border-radius:8px;
  color:#f0f0f0;font-size:16px;line-height:1.8}}
.brand{{color:#555;font-size:11px;letter-spacing:.15em;text-transform:uppercase;margin-top:32px}}
</style></head>
<body><div class=wrap>
<div class=check>✓</div>
<h1>You're on the list!</h1>
<div class=names>{names}</div>
<p>See you tonight at Pawn Shop Live.<br>Just mention your name at the door.</p>
<div class=brand>Nightshift Entertainment</div>
</div></body></html>"""

_GL_FULL = """<!doctype html><html lang=en><head>
<meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Already on the list</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:#0a0a0c;color:#f0f0f0;min-height:100vh;
  display:flex;align-items:center;justify-content:center;padding:20px;text-align:center}}
.wrap{{max-width:380px}}
h1{{font-size:24px;font-weight:700;margin-bottom:10px}}
p{{color:#999;font-size:15px;line-height:1.6}}
.names{{margin:18px 0;padding:14px;background:#111;border:1px solid #222;border-radius:8px;
  color:#f0f0f0;font-size:16px;line-height:1.8}}
</style></head>
<body><div class=wrap>
<h1>You're already set</h1>
<div class=names>{names}</div>
<p>This email/cell already has its 2 guests on the Ne-Yo list. See you tonight at Pawn Shop Live!</p>
</div></body></html>"""

_GL_CLOSED = """<!doctype html><html lang=en><head>
<meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Guest List Closed</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:#0a0a0c;color:#f0f0f0;min-height:100vh;
  display:flex;align-items:center;justify-content:center;padding:20px;text-align:center}}
.wrap{{max-width:380px}}
h1{{font-size:24px;font-weight:700;margin-bottom:10px}}
p{{color:#999;font-size:15px;line-height:1.6}}
</style></head>
<body><div class=wrap>
<h1>Guest List is Closed</h1>
<p>The guest list for tonight's Ne-Yo show has closed. See you at the door!</p>
</div></body></html>"""


def _gl_digits(s: str) -> str:
    return "".join(c for c in s if c.isdigit())


def _gl_save(phone: str, email: str, new_names: list, code: str) -> tuple:
    """Merge names onto the record keyed by this email/phone, capped at _GL_MAX.

    Returns (added_names, all_names_for_contact, was_already_full).
    """
    data = []
    try:
        with open(_GL_FILE) as f:
            data = json.load(f)
    except Exception:
        pass

    email_k = email.strip().lower()
    phone_k = _gl_digits(phone)

    rec = None
    for r in data:
        r_email = str(r.get("email", "")).strip().lower()
        r_phone = _gl_digits(str(r.get("phone", "")))
        if email_k and r_email and r_email == email_k:
            rec = r
            break
        if phone_k and r_phone and r_phone == phone_k:
            rec = r
            break

    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())

    if rec is None:
        names = new_names[:_GL_MAX]
        data.append({"names": names, "phone": phone, "email": email,
                     "code": code, "ts": ts})
        with open(_GL_FILE, "w") as f:
            json.dump(data, f, indent=2)
        return names, names, False

    existing = rec.get("names") or []
    if len(existing) >= _GL_MAX:
        return [], existing, True

    capacity = _GL_MAX - len(existing)
    # de-dupe (case-insensitive) so the same name isn't listed twice
    have = {n.strip().lower() for n in existing}
    added = []
    for n in new_names:
        if len(added) >= capacity:
            break
        if n.strip().lower() in have:
            continue
        added.append(n)
        have.add(n.strip().lower())

    rec["names"] = existing + added
    if not rec.get("phone") and phone:
        rec["phone"] = phone
    if not rec.get("email") and email:
        rec["email"] = email
    if not rec.get("code") and code:
        rec["code"] = code
    rec["ts"] = ts
    with open(_GL_FILE, "w") as f:
        json.dump(data, f, indent=2)
    return added, rec["names"], False


def _gl_send_confirmation(names: list, email: str) -> None:
    who = " and ".join(names) if names else "You"
    body = (
        f"Hi {names[0] if names else 'there'},\n\n"
        f"{who} " + ("are" if len(names) > 1 else "is") + " officially on the guest list "
        "for tonight's show!\n\n"
        "  Event: Ne-Yo\n"
        "  Venue: Pawn Shop Live\n"
        "  Date: Friday, June 19, 2026\n\n"
        "Just mention your name at the door. See you tonight!\n\n"
        "— Pawn Shop Live / Nightshift Entertainment\n\n"
        "---\n"
        "You're receiving this because you signed up for our guest list. "
        "By signing up you consented to receive further communications from "
        "Nightshift Entertainment. To unsubscribe from future marketing, "
        "reply STOP to this email."
    )
    try:
        subprocess.Popen(
            [sys.executable, _EMAIL_BIN,
             "--to", email,
             "--subject", "You're on the guest list — Ne-Yo @ Pawn Shop Tonight",
             "--body", body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


@mcp.custom_route("/guestlist/img", methods=["GET"])
async def guestlist_img(request: Request) -> Response:
    with open(_GL_IMG, "rb") as f:
        return Response(f.read(), media_type="image/png")


@mcp.custom_route("/guestlist", methods=["GET"])
async def guestlist_get(request: Request) -> HTMLResponse:
    if time.time() > _GL_CLOSE_EPOCH:
        return HTMLResponse(_GL_CLOSED)
    return HTMLResponse(_GL_FORM.format(err="", code="", name1="", name2="", phone="", email=""))


@mcp.custom_route("/guestlist", methods=["POST"])
async def guestlist_post(request: Request) -> HTMLResponse:
    if time.time() > _GL_CLOSE_EPOCH:
        return HTMLResponse(_GL_CLOSED)
    form = await request.form()
    code = str(form.get("code", "")).strip()
    name1 = str(form.get("name1", "")).strip()
    name2 = str(form.get("name2", "")).strip()
    phone = str(form.get("phone", "")).strip()
    email = str(form.get("email", "")).strip()

    def _form(err):
        return HTMLResponse(_GL_FORM.format(
            err=f'<div class=err>{err}</div>',
            code=code, name1=name1, name2=name2, phone=phone, email=email))

    if code.upper() not in _GL_CODES:
        return _form("Invalid access code. Please check the code from your promoter and try again.")
    if not name1:
        return _form("Please enter at least one guest name.")
    if not phone and not email:
        return _form("Please enter your cell phone or email address.")

    new_names = [n for n in (name1, name2) if n]
    added, all_names, was_full = await asyncio.to_thread(
        _gl_save, phone, email, new_names, code.upper())

    names_html = "<br>".join(all_names) if all_names else "—"

    if was_full:
        return HTMLResponse(_GL_FULL.format(names=names_html))

    if email and added:
        await asyncio.to_thread(_gl_send_confirmation, added, email)

    return HTMLResponse(_GL_THANKS.format(names=names_html))


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
