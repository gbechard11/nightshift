"""Prism.fm client for Pedro.

Prism (app.prism.fm) is Nightshift's live-music booking / venue-management
platform. There is no public, self-serve API — the web app talks to its own
same-origin backend at https://app.prism.fm/api (REST) and /graphql/ (GraphQL).
This module reuses that same backend the way the browser does.

Auth is AWS Cognito. The browser stores a short-lived JWT **access token** and a
long-lived **refresh token** in localStorage and sends the access token as
`Authorization: Bearer <token>`. We do the same, and — crucially — we can mint
fresh access tokens ourselves from the refresh token via Cognito's public
`REFRESH_TOKEN_AUTH` flow (no app secret; it's a public client). So you paste the
refresh token ONCE and Pedro keeps itself authenticated until Cognito expires or
revokes it (a Prism logout on that browser revokes it; default lifetime ~30d).

One extra wrinkle: the API rejects calls that don't carry an `App-Version` header
matching its current backend build (it answers `Version_Mismatch` otherwise). We
send PRISM_APP_VERSION and surface a clear "bump this" error if it drifts.

Design mirrors meta_ads.py: env config up top, a `configured()` gate, async httpx
calls, a custom error type, and `_get`/`_handle` helpers. The module never imports
bot.py.

SAFETY: reads (calendar, event detail) are safe. WRITES (create/update bookings)
go through `_write()` and are deliberately conservative — bot.py only calls them
behind an explicit confirm-first button, mirroring the Meta ad-launch gate. The
write payload shapes are NOT yet verified against the live API (see the functions
at the bottom); treat them as experimental until confirmed.
"""
import base64
import json
import logging
import os
import time

import httpx

log = logging.getLogger("nightshift.prism")

# --- Config -----------------------------------------------------------------
# The long-lived Cognito refresh token, copied once from a logged-in browser
# (localStorage key "refreshToken" on app.prism.fm). This is the only secret you
# need to set. Leave unset and the whole Prism path stays dormant.
REFRESH_TOKEN = os.environ.get("PRISM_REFRESH_TOKEN", "")
# Optional: a pre-minted access token (localStorage "token"). Mainly for quick
# tests — it expires in ~24h. Normally leave this empty and rely on REFRESH_TOKEN.
ACCESS_TOKEN_ENV = os.environ.get("PRISM_ACCESS_TOKEN", "")
# Cognito public app client + region (discovered from the access-token claims).
COGNITO_CLIENT_ID = os.environ.get("PRISM_COGNITO_CLIENT_ID", "3pdck2a6f3o3gua1sjvfsvqjfo")
COGNITO_REGION = os.environ.get("PRISM_COGNITO_REGION", "us-east-2")
API_BASE = os.environ.get("PRISM_API_BASE", "https://app.prism.fm/api")
GRAPHQL_URL = os.environ.get("PRISM_GRAPHQL_URL", "https://app.prism.fm/graphql/")
# The API version-pins requests; bump this when Prism updates and you start
# seeing Version_Mismatch errors (the error message tells you the new value).
APP_VERSION = os.environ.get("PRISM_APP_VERSION", "1.0.618")
# Where to cache the minted access token between restarts so we don't hit Cognito
# on every call. Same /data/greg home the bot already writes to.
TOKEN_CACHE = os.environ.get("PRISM_TOKEN_CACHE", "/data/greg/.prism_token.json")

COGNITO_ENDPOINT = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/"

# Prism's event status enum (the `confirmed` field on calendar rows).
STATUS_LABELS = {1: "Hold", 2: "Confirmed", 3: "Settlement", 4: "Settled"}


class PrismError(Exception):
    """User-facing failure from a Prism API call."""


def configured() -> bool:
    """True if we have something to authenticate with."""
    return bool(REFRESH_TOKEN or ACCESS_TOKEN_ENV)


# --- Token management -------------------------------------------------------
# In-process cache: {"token": <jwt>, "exp": <epoch seconds>}.
_token_cache: dict | None = None


def _jwt_exp(token: str) -> int:
    """Best-effort: read the `exp` (epoch seconds) from a JWT without verifying
    it. Returns 0 if it can't be parsed (treated as already expired)."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # restore base64 padding
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return int(payload.get("exp", 0))
    except Exception:  # noqa: BLE001 - any malformed token -> "expired"
        return 0


def _load_cache() -> dict | None:
    global _token_cache
    if _token_cache is not None:
        return _token_cache
    try:
        with open(TOKEN_CACHE) as f:
            _token_cache = json.load(f)
    except (FileNotFoundError, ValueError):
        _token_cache = None
    return _token_cache


def _save_cache(token: str) -> None:
    global _token_cache
    _token_cache = {"token": token, "exp": _jwt_exp(token)}
    try:
        with open(TOKEN_CACHE, "w") as f:
            json.dump(_token_cache, f)
    except OSError as e:
        log.warning("could not write Prism token cache %s: %s", TOKEN_CACHE, e)


async def _refresh_access_token(client: httpx.AsyncClient) -> str:
    """Mint a fresh access token from the refresh token via Cognito."""
    if not REFRESH_TOKEN:
        raise PrismError(
            "Prism access token expired and no PRISM_REFRESH_TOKEN is set to renew "
            "it. Paste the refresh token from a logged-in app.prism.fm browser "
            "(localStorage 'refreshToken') into .env and restart."
        )
    resp = await client.post(
        COGNITO_ENDPOINT,
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
        },
        content=json.dumps(
            {
                "AuthFlow": "REFRESH_TOKEN_AUTH",
                "ClientId": COGNITO_CLIENT_ID,
                "AuthParameters": {"REFRESH_TOKEN": REFRESH_TOKEN},
            }
        ),
    )
    if resp.status_code != 200:
        # Cognito error bodies look like {"__type": "...", "message": "..."}.
        try:
            body = resp.json()
        except ValueError:
            body = {}
        kind = body.get("__type", "")
        msg = body.get("message", resp.text[:300])
        if "NotAuthorized" in kind:
            raise PrismError(
                "Prism refresh token was rejected (expired or revoked — a Prism "
                "logout invalidates it). Grab a fresh 'refreshToken' from a "
                f"logged-in browser and update PRISM_REFRESH_TOKEN. (Cognito: {msg})"
            )
        raise PrismError(f"Prism token refresh failed ({kind or resp.status_code}): {msg}")
    token = ((resp.json() or {}).get("AuthenticationResult") or {}).get("AccessToken")
    if not token:
        raise PrismError("Prism token refresh returned no access token.")
    _save_cache(token)
    log.info("minted fresh Prism access token (exp %s)", _jwt_exp(token))
    return token


async def _access_token(client: httpx.AsyncClient, force_refresh: bool = False) -> str:
    """Return a valid access token, refreshing via Cognito when needed."""
    if not force_refresh:
        # A pinned env token wins (test/debug convenience).
        if ACCESS_TOKEN_ENV and _jwt_exp(ACCESS_TOKEN_ENV) - time.time() > 60:
            return ACCESS_TOKEN_ENV
        cached = _load_cache()
        if cached and cached.get("token") and cached.get("exp", 0) - time.time() > 60:
            return cached["token"]
    return await _refresh_access_token(client)


# --- Request plumbing -------------------------------------------------------
def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "App-Version": APP_VERSION,
    }


def _handle(resp: httpx.Response) -> dict | list:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 300:
        if isinstance(body, dict) and body.get("error_type") == "Version_Mismatch":
            backend = (body.get("errors") or {}).get("Backend")
            raise PrismError(
                f"Prism rejected our App-Version. Set PRISM_APP_VERSION={backend} "
                "in .env and restart (Prism bumped its backend build)."
            )
        msg = body.get("message") if isinstance(body, dict) else None
        raise PrismError(f"Prism API {resp.status_code}: {msg or resp.text[:300]}")
    return body


async def _get(client: httpx.AsyncClient, path: str, params: dict | None = None) -> dict | list:
    """GET an /api path, transparently refreshing the token once on a 401."""
    token = await _access_token(client)
    url = f"{API_BASE}/{path.lstrip('/')}"
    resp = await client.get(url, headers=_headers(token), params=params)
    if resp.status_code == 401:
        token = await _access_token(client, force_refresh=True)
        resp = await client.get(url, headers=_headers(token), params=params)
    return _handle(resp)


# --- Reads (safe) -----------------------------------------------------------
def status_label(confirmed: int | None) -> str:
    return STATUS_LABELS.get(confirmed, f"status {confirmed}")


def _normalize_show(row: dict) -> dict:
    """Flatten a raw calendar row into the fields Pedro cares about."""
    venue = row.get("venue") or {}
    return {
        "event_id": row.get("event_id") or row.get("id"),
        "title": (row.get("title") or "").strip() or "(untitled)",
        "status": row.get("confirmed"),
        "status_label": status_label(row.get("confirmed")),
        "start": row.get("start"),
        "end": row.get("end"),
        "start_time": row.get("start_time"),
        "end_time": row.get("end_time"),
        "all_day": bool(row.get("all_day")),
        "venue": venue.get("name") if isinstance(venue, dict) else None,
        "stage": row.get("stage"),
        "genres": row.get("genre_strings") or [],
        "is_matinee": bool(row.get("is_matinee")),
    }


async def list_shows(
    client: httpx.AsyncClient, start: str, end: str
) -> list[dict]:
    """Return shows between `start` and `end` (YYYY-MM-DD), normalized + sorted.

    Hits /api/confirmed-and-meta-dates, which returns confirmed events plus
    placeholder/hold ("meta") dates across the range regardless of status.
    """
    data = await _get(
        client, "confirmed-and-meta-dates", {"start": start, "end": end}
    )
    rows = data if isinstance(data, list) else (data.get("data") or [])
    shows = [_normalize_show(r) for r in rows]
    shows.sort(key=lambda s: (s.get("start") or "", s.get("start_time") or ""))
    return shows


def format_shows(shows: list[dict], limit: int = 30) -> str:
    """Human-readable list for Telegram."""
    if not shows:
        return "No shows found in that range."
    lines = []
    for s in shows[:limit]:
        when = s["start"] or "?"
        if s.get("start_time") and not s.get("all_day"):
            when += f" {s['start_time']}"
        venue = f" @ {s['venue']}" if s.get("venue") else ""
        stage = f" ({s['stage']})" if s.get("stage") else ""
        lines.append(
            f"• {when} — {s['title']}{venue}{stage}  [{s['status_label']}]  #{s['event_id']}"
        )
    out = "\n".join(lines)
    if len(shows) > limit:
        out += f"\n…and {len(shows) - limit} more."
    return out


async def get_show(client: httpx.AsyncClient, event_id: int | str) -> dict | None:
    """Find a single show by event id within a wide window (the calendar endpoint
    is the reliable source; there is no clean per-event REST detail endpoint)."""
    eid = str(event_id)
    shows = await list_shows(client, "2015-01-01", "2035-12-31")
    for s in shows:
        if str(s.get("event_id")) == eid:
            return s
    return None


# --- GraphQL (experimental) -------------------------------------------------
# Prism serves settlement / payment / financial data through GraphQL persisted
# queries (e.g. operationName=PaginatedColumnSetsQuery, context_key="payment").
# Persisted-query hashes change when Prism redeploys the frontend, so this is
# inherently more fragile than the REST calendar. Provided as a building block;
# not yet wired to a verified settlement parser.
async def graphql(
    client: httpx.AsyncClient,
    operation_name: str,
    variables: dict,
    sha256_hash: str | None = None,
    query: str | None = None,
) -> dict:
    """Call the Prism GraphQL endpoint. Supply either a persisted-query
    `sha256_hash` (Apollo APQ) or a full `query` string."""
    token = await _access_token(client)
    if sha256_hash:
        params = {
            "operationName": operation_name,
            "variables": json.dumps(variables),
            "extensions": json.dumps(
                {"persistedQuery": {"version": 1, "sha256Hash": sha256_hash}}
            ),
        }
        resp = await client.get(GRAPHQL_URL, headers=_headers(token), params=params)
    else:
        body = {"operationName": operation_name, "variables": variables, "query": query}
        resp = await client.post(GRAPHQL_URL, headers=_headers(token), json=body)
    out = _handle(resp)
    if isinstance(out, dict) and out.get("errors"):
        raise PrismError(f"Prism GraphQL error: {out['errors']}")
    return out if isinstance(out, dict) else {}


# --- Writes (EXPERIMENTAL — gated, payload shapes UNVERIFIED) ----------------
# Nothing here is called without an explicit confirm-first button in bot.py, the
# same gate used for Meta ad launches. The request bodies below are best-effort
# guesses at Prism's internal write API and have NOT been validated against the
# live backend. Verify the real shape (capture a genuine create/update request in
# the browser dev tools) before relying on these on real bookings.
async def _write(
    client: httpx.AsyncClient, method: str, path: str, payload: dict
) -> dict | list:
    token = await _access_token(client)
    url = f"{API_BASE}/{path.lstrip('/')}"
    resp = await client.request(
        method, url, headers=_headers(token), json=payload
    )
    if resp.status_code == 401:
        token = await _access_token(client, force_refresh=True)
        resp = await client.request(method, url, headers=_headers(token), json=payload)
    return _handle(resp)


async def update_event(
    client: httpx.AsyncClient, event_id: int | str, fields: dict
) -> dict | list:
    """EXPERIMENTAL. PATCH fields on an event. Shape unverified — gate behind a
    confirm-first button and test on a throwaway event first."""
    log.warning("PRISM WRITE: update_event %s %s", event_id, list(fields))
    return await _write(client, "PATCH", f"events/{event_id}", fields)
