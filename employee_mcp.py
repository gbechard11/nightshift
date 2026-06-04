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
import sys
import time
from dataclasses import dataclass, field

import mailer
import employee_email

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
from starlette.responses import HTMLResponse, RedirectResponse

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
                 "and send email from your own Nightshift address. Cannot edit, "
                 "overwrite, or delete anything that already exists.",
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
    await asyncio.to_thread(mailer.send, subject, body, recipients, sender)
    return f"Sent '{subject}' from {sender.get('from')} to {', '.join(recipients)}."


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


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
