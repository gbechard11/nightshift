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
        # NOTE (corrected 2026-06-11): Prism's Cognito app client is actually
        # PUBLIC (no secret) — verified live, REFRESH_TOKEN_AUTH succeeds with just
        # ClientId. Earlier "needs a secret" failures were a MISDIAGNOSIS of an
        # expired/revoked refresh token. This branch is kept only as a defensive
        # guard in case Prism ever switches to a confidential client.
        if "SECRET_HASH" in msg or "secret" in msg.lower():
            raise PrismError(
                "Prism's Cognito client requires a secret we don't have, so Pedro "
                "can't auto-refresh. Paste a fresh access token instead: in a "
                "logged-in app.prism.fm browser console run "
                "localStorage.getItem('token'), set PRISM_ACCESS_TOKEN in .env, and "
                "restart. (It lasts ~24h; the durable fix is official Prism API access.)"
            )
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


# --- Holds ------------------------------------------------------------------
# The calendar's /confirmed-and-meta-dates does NOT include holds — the app loads
# those from a separate endpoint. Availability questions MUST factor holds in: a
# date with no confirmed show can still carry a 1st/2nd/… hold. Each row exposes
# hold_level (1=1st/lead, 2=2nd…), cleared, is_pending, the date at date.date
# (nested), the artist at event.name, and the venue at stage[0].venue.name.
def _normalize_hold(row: dict) -> dict:
    d = row.get("date") or {}
    stage = row.get("stage") or []
    venue = None
    if isinstance(stage, list) and stage:
        v = (stage[0] or {}).get("venue") or {}
        venue = v.get("name")
    ev = row.get("event") or {}
    return {
        "date": str(d.get("date") or "")[:10],
        "venue": venue,
        "artist": (ev.get("name") or "").strip() or "(untitled)",
        "level": row.get("hold_level"),
        "cleared": bool(row.get("cleared")),
        "pending": bool(row.get("is_pending")),
    }


async def list_holds(
    client: httpx.AsyncClient, start: str, end: str, include_cleared: bool = False
) -> list[dict]:
    """Return holds between `start` and `end` (YYYY-MM-DD), normalized.

    Hits /api/holds. By default excludes cleared holds (so the result is the set
    of LIVE holds that affect availability) but includes pending ones.
    """
    params = {
        "start": start,
        "end": end,
        "includeClearedHolds": "true" if include_cleared else "false",
        "includePendingHolds": "true",
    }
    data = await _get(client, "holds", params)
    rows = data if isinstance(data, list) else (data.get("data") or [])
    holds = [_normalize_hold(r) for r in rows]
    holds.sort(key=lambda h: (h.get("date") or "", h.get("level") or 0))
    return holds


def next_hold_position(holds: list[dict]) -> int:
    """Next available hold position = highest uncleared hold_level + 1 (1 if none)."""
    levels = [h.get("level") or 0 for h in holds if not h.get("cleared")]
    return (max(levels) + 1) if levels else 1


# --- Settlement / financials -----------------------------------------------
# The single endpoint /api/events/{id}/build returns the COMPLETE event record at
# its top level (not the small `data` sub-object): tickets[], cost_groups[].costs[],
# promoter_data, tax_rate/tax_type, facility_fee, attendance, currency, etc. The
# app's settlement screens are computed client-side from exactly this payload.
def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


async def get_event_full(client: httpx.AsyncClient, event_id: int | str) -> dict:
    """Fetch the full event record (the whole financial model in one call)."""
    data = await _get(client, f"events/{event_id}/build")
    if not isinstance(data, dict):
        raise PrismError(f"Unexpected event payload for {event_id}.")
    return data


def compute_settlement(event: dict) -> dict:
    """Derive a settlement summary from a full event record (get_event_full).

    VALIDATED to the penny against Prism's rendered figures:
      Gross Ticket Revenue = Σ(sold × ticket_price)   [or actual_gross if set]
      taxes (tax_type 'divisor') = gross − gross/(1 + rate/100)
      Net Gross = gross − taxes
    Expenses are surfaced as raw line items (budget → reported_cost). The FINAL
    settled expense total / Net Profit run through Prism's settlement engine
    (co-pro splits, per-line reported-vs-budget, cost taxes) and are deliberately
    NOT recomputed here — we link to Prism for the official bottom line rather
    than risk a wrong number.
    """
    tiers: list[dict] = []
    gross = 0.0
    sold_total = 0
    for tk in event.get("tickets") or []:
        sold = _num(tk.get("sold"))
        price = _num(tk.get("ticket_price"))
        actual = tk.get("actual_gross")
        tier_gross = _num(actual) if actual not in (None, "") else sold * price
        gross += tier_gross
        sold_total += int(sold)
        tiers.append(
            {"name": tk.get("name"), "sold": int(sold), "price": price, "gross": tier_gross}
        )

    rate = _num(event.get("tax_rate"))
    tax_type = (event.get("tax_type") or "").lower()
    if rate and tax_type == "divisor":
        taxes = gross - gross / (1 + rate / 100.0)
    elif rate:
        taxes = gross * rate / 100.0
    else:
        taxes = 0.0

    budget_exp = 0.0
    reported_exp = 0.0
    expense_lines: list[dict] = []
    for cg in event.get("cost_groups") or []:
        for c in cg.get("costs") or []:
            b = _num(c.get("budget"))
            rep = _num(c.get("reported_cost"))
            budget_exp += b
            reported_exp += rep
            expense_lines.append({"name": c.get("name"), "budget": b, "reported": rep})

    pd = event.get("promoter_data") or {}
    return {
        "title": event.get("name"),
        "currency": event.get("currency") or "",
        "status_label": status_label(event.get("confirmed")),
        "gross_ticket_revenue": round(gross, 2),
        "taxes": round(taxes, 2),
        "tax_rate": rate,
        "tax_type": tax_type,
        "net_gross": round(gross - taxes, 2),
        "tickets_sold": sold_total,
        "tiers": tiers,
        "budgeted_expenses": round(budget_exp, 2),
        "reported_expenses": round(reported_exp, 2),
        "expense_lines": expense_lines,
        "actual_attendance": event.get("actual_attendance"),
        "estimated_attendance": event.get("estimated_attendance"),
        "room_fee": pd.get("room_fee"),
        "promoter_percentage": pd.get("promoter_percentage"),
        "facility_fee": event.get("facility_fee"),
    }


async def get_settlement(client: httpx.AsyncClient, event_id: int | str) -> dict:
    """Convenience: fetch + compute the settlement summary for an event."""
    return compute_settlement(await get_event_full(client, event_id))


def format_settlement(s: dict, event_id: int | str) -> str:
    """Human-readable settlement summary for Telegram."""
    cur = s.get("currency") or "CA$"

    def m(v) -> str:
        return f"{cur}{_num(v):,.2f}"

    lines = [
        f"💰 {s.get('title') or 'Event'}  (#{event_id})  [{s['status_label']}]",
        "",
        f"Tickets sold: {s['tickets_sold']:,}",
        f"Gross Ticket Revenue: {m(s['gross_ticket_revenue'])}",
        f"Taxes & Fees ({s['tax_rate']:g}% {s['tax_type'] or 'tax'}): -{m(s['taxes'])}",
        f"Net Gross: {m(s['net_gross'])}",
    ]
    if s["tiers"]:
        lines.append("\nTicket tiers:")
        for t in s["tiers"][:12]:
            lines.append(
                f"  • {t['name']}: {t['sold']:,} × {cur}{_num(t['price']):,.2f} = {m(t['gross'])}"
            )
    if s["expense_lines"]:
        lines.append(f"\nExpenses (budget → reported), {len(s['expense_lines'])} lines:")
        for c in s["expense_lines"][:12]:
            lines.append(
                f"  • {c['name']}: {cur}{_num(c['budget']):,.2f} → {cur}{_num(c['reported']):,.2f}"
            )
        lines.append(
            f"  Budgeted total: {m(s['budgeted_expenses'])} | "
            f"Reported total: {m(s['reported_expenses'])}"
        )
    if _num(s.get("room_fee")):
        lines.append(f"\nRoom fee: {m(s['room_fee'])}")
    if s.get("actual_attendance"):
        lines.append(f"Attendance (actual): {int(s['actual_attendance']):,}")
    lines.append(
        "\n⚠️ Net Profit / final settled expenses are computed by Prism's "
        "settlement engine (co-pro splits, per-line logic). Official figure:"
    )
    lines.append(f"https://app.prism.fm/event/{event_id}/internal-settlement")
    return "\n".join(lines)


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
