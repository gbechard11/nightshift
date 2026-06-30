"""Meta (Facebook/Instagram) Marketing API client for Pedro.

Gives Pedro the primitives to research audiences and build ad campaigns across
**multiple ad accounts** (e.g. Nightshift Entertainment CAD and Pawn Shop Live),
while enforcing one hard rule in code:

    Nothing this module creates ever goes live on its own.

Every campaign, ad set and ad is created **PAUSED**. The only functions that can
start spend are `activate_campaign()` / `activate_full()`, and bot.py only calls
them after an explicit per-campaign approval (the inline-button flow, mirroring
/call). Do not add an `ACTIVE` status anywhere else.

Multi-account model
-------------------
Each ad account is an `AdProfile` carrying its OWN access token, ad-account id,
Facebook Page id, pixel id, currency and audience names. Profiles are loaded from
env (see `_load_profiles`). The default profile is `nightshift`, built from the
legacy `META_*` env vars so existing callers behave exactly as before. Every API
function takes an optional `acct=<AdProfile>`; when omitted it uses the default
profile. Adding a new account = add a profile to `META_PROFILES` and set its
`META_<KEY>_*` env vars. Nothing else changes.

Auth: each profile's token is a long-lived **System User token** with the
`ads_management` permission (read-only insights only need `ads_read`), generated
in the Business Manager that owns that ad account + page, with those assets
assigned to the system user.
"""
import dataclasses
import logging
import os

import httpx

log = logging.getLogger("nightshift.meta_ads")

# --------------------------------------------------------------------------
# Legacy / default (Nightshift) config. These remain the source of truth for the
# default `nightshift` profile so existing callers and scripts keep working.
# --------------------------------------------------------------------------
ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN", "")
# Pinned CAD ad account, e.g. "act_1234567890". Discover it once via
# scripts/find_meta_assets.py, then set it here so we never touch the USD account.
AD_ACCOUNT_ID = os.environ.get("META_AD_ACCOUNT_ID", "")
# Facebook Page ID — required for ad creatives. Find it at facebook.com/your-page → About.
PAGE_ID = os.environ.get("META_PAGE_ID", "")
PIXEL_ID = os.environ.get("META_PIXEL_ID", "")
GRAPH_VERSION = os.environ.get("META_GRAPH_VERSION", "v21.0")
# Local folder where ad media (flyers, images) are stored. Drop files here and
# reference them by filename in /draft. Synced from Drive via scripts/drive_sync.py.
MEDIA_DIR = os.environ.get("META_MEDIA_DIR", "/data/greg/ads")
# Currency the default account MUST be in. A guard against pointing at the wrong account.
REQUIRED_CURRENCY = os.environ.get("META_REQUIRED_CURRENCY", "CAD")
# Who gets copied on reports / optimization updates (comma-separated).
REPORT_RECIPIENTS = [
    x.strip()
    for x in os.environ.get("META_REPORT_RECIPIENTS", "seba@nightshiftent.ca").split(",")
    if x.strip()
]

GRAPH_BASE = "https://graph.facebook.com/{ver}"

# Default Nightshift custom-audience names (used when a profile doesn't override).
RETARGET_AUDIENCES = [
    "NS | Ticket page viewers 180d",
    "NS | Initiated checkout 180d",
    "NS | Purchasers 180d",
    "NS | Website - All visitors 180d",
    "NS | FB Page engagers 365d",
]
LOOKALIKE_AUDIENCES = [
    "NS | LAL 1% Purchasers (CA)",
    "NS | LAL 1% Ticket viewers (CA)",
    "NS | LAL 2% Website visitors (CA)",
]

# Ticketing domains where a pixel CAN fire (so optimize for purchases) vs not.
_PIXEL_TRACKABLE = ("ticketweb", "showpass", "eventbrite", "dice.fm", "seetickets")
_NO_PIXEL = ("ticketmaster", "livenation", "axs.com")


class MetaError(Exception):
    """User-facing failure from a Graph API call."""


# --------------------------------------------------------------------------
# Ad-account profiles — one per account we can launch from.
# --------------------------------------------------------------------------
@dataclasses.dataclass
class AdProfile:
    """Everything needed to operate on ONE ad account: its token, ids and naming."""
    key: str
    label: str
    token: str
    ad_account_id: str
    page_id: str = ""
    pixel_id: str = ""
    currency: str = "CAD"
    retarget_audiences: list = dataclasses.field(default_factory=list)
    lookalike_audiences: list = dataclasses.field(default_factory=list)
    default_countries: list = dataclasses.field(default_factory=lambda: ["CA"])
    ig_actor_id: str = ""

    @property
    def has_token(self) -> bool:
        return bool(self.token)

    @property
    def ready(self) -> bool:
        """True if this profile can actually build campaigns (token + account + page)."""
        return bool(self.token and self.ad_account_id and self.page_id)

    def status_line(self) -> str:
        if self.ready:
            state = "✅ ready"
        elif self.token:
            missing = [n for n, v in (("account", self.ad_account_id), ("page", self.page_id)) if not v]
            state = "⚠️ token set, missing " + "+".join(missing)
        else:
            state = "❌ not configured (no token)"
        acct = self.ad_account_id or "—"
        return f"@{self.key} — {self.label} [{self.currency}] {state} (acct {acct})"


def _csv(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _load_profiles() -> dict[str, "AdProfile"]:
    """Build the profile registry from env.

    `META_PROFILES` is a comma list of profile keys (default "nightshift,pawnshop").
    The `nightshift` profile is built from the legacy bare `META_*` vars. Every
    other key `<k>` reads `META_<K>_ACCESS_TOKEN`, `META_<K>_AD_ACCOUNT_ID`,
    `META_<K>_PAGE_ID`, `META_<K>_PIXEL_ID`, `META_<K>_CURRENCY`, `META_<K>_LABEL`,
    `META_<K>_RETARGET_AUDIENCES`, `META_<K>_LOOKALIKE_AUDIENCES`, `META_<K>_COUNTRIES`.
    """
    keys = _csv(os.environ.get("META_PROFILES", "nightshift,pawnshop")) or ["nightshift"]
    profiles: dict[str, AdProfile] = {}
    for key in keys:
        ku = key.upper()
        if key == "nightshift":
            profiles[key] = AdProfile(
                key="nightshift",
                label=os.environ.get("META_NIGHTSHIFT_LABEL", "Nightshift Entertainment"),
                token=ACCESS_TOKEN,
                ad_account_id=AD_ACCOUNT_ID,
                page_id=PAGE_ID,
                pixel_id=PIXEL_ID,
                currency=REQUIRED_CURRENCY,
                retarget_audiences=_csv(os.environ.get("META_NIGHTSHIFT_RETARGET_AUDIENCES")) or list(RETARGET_AUDIENCES),
                lookalike_audiences=_csv(os.environ.get("META_NIGHTSHIFT_LOOKALIKE_AUDIENCES")) or list(LOOKALIKE_AUDIENCES),
                default_countries=_csv(os.environ.get("META_NIGHTSHIFT_COUNTRIES")) or ["CA"],
                ig_actor_id=os.environ.get("META_NIGHTSHIFT_IG_ACTOR_ID", ""),
            )
        else:
            profiles[key] = AdProfile(
                key=key,
                label=os.environ.get(f"META_{ku}_LABEL", key.replace("_", " ").title()),
                token=os.environ.get(f"META_{ku}_ACCESS_TOKEN", ""),
                ad_account_id=os.environ.get(f"META_{ku}_AD_ACCOUNT_ID", ""),
                page_id=os.environ.get(f"META_{ku}_PAGE_ID", ""),
                pixel_id=os.environ.get(f"META_{ku}_PIXEL_ID", ""),
                currency=os.environ.get(f"META_{ku}_CURRENCY", "CAD"),
                retarget_audiences=_csv(os.environ.get(f"META_{ku}_RETARGET_AUDIENCES")),
                lookalike_audiences=_csv(os.environ.get(f"META_{ku}_LOOKALIKE_AUDIENCES")),
                default_countries=_csv(os.environ.get(f"META_{ku}_COUNTRIES")) or ["CA"],
                ig_actor_id=os.environ.get(f"META_{ku}_IG_ACTOR_ID", ""),
            )
    return profiles


PROFILES: dict[str, AdProfile] = _load_profiles()
DEFAULT_PROFILE_KEY = next(iter(PROFILES), "nightshift")


def list_profiles() -> list[AdProfile]:
    return list(PROFILES.values())


def get_profile(key: str | None) -> AdProfile:
    """Look up a profile by key (case-insensitive, leading '@' allowed)."""
    if not key:
        return PROFILES[DEFAULT_PROFILE_KEY]
    k = key.strip().lstrip("@").lower()
    if k not in PROFILES:
        known = ", ".join("@" + p for p in PROFILES)
        raise MetaError(f"Unknown ad account '@{k}'. Known accounts: {known}.")
    return PROFILES[k]


def _resolve(acct: "AdProfile | None") -> AdProfile:
    return acct if acct is not None else PROFILES[DEFAULT_PROFILE_KEY]


def configured(acct: "AdProfile | None" = None) -> bool:
    """True if the (default or given) profile at least has a token. Account id is
    needed for writes but not for discovery, so it isn't required here."""
    return bool(_resolve(acct).token)


def _base() -> str:
    return GRAPH_BASE.format(ver=GRAPH_VERSION)


async def _get(
    client: httpx.AsyncClient, path: str, params: dict | None = None, token: str | None = None
) -> dict:
    params = dict(params or {})
    params["access_token"] = token or ACCESS_TOKEN
    resp = await client.get(f"{_base()}/{path.lstrip('/')}", params=params)
    return _handle(resp)


async def _post(
    client: httpx.AsyncClient, path: str, data: dict, token: str | None = None
) -> dict:
    data = dict(data)
    data["access_token"] = token or ACCESS_TOKEN
    resp = await client.post(f"{_base()}/{path.lstrip('/')}", data=data)
    return _handle(resp)


def _handle(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 300:
        err = (body.get("error") or {}) if isinstance(body, dict) else {}
        msg = err.get("error_user_msg") or err.get("message") or resp.text[:400]
        raise MetaError(f"Meta API {resp.status_code}: {msg}")
    return body


# --------------------------------------------------------------------------
# Account discovery — how we find an ad account id for a profile's token.
# --------------------------------------------------------------------------
async def find_ad_accounts(client: httpx.AsyncClient, acct: "AdProfile | None" = None) -> list[dict]:
    """Return every ad account the token can see, with name + currency + status."""
    a = _resolve(acct)
    out: list[dict] = []
    params = {
        # NB: no `business` field — that requires business_management permission,
        # which our ads_management/ads_read token doesn't carry. We don't need it.
        "fields": "id,account_id,name,currency,account_status",
        "limit": 200,
    }
    data = await _get(client, "me/adaccounts", params, token=a.token)
    while True:
        out.extend(data.get("data", []))
        next_url = (data.get("paging") or {}).get("next")
        if not next_url:
            break
        resp = await client.get(next_url)
        data = _handle(resp)
    return out


async def find_pages(client: httpx.AsyncClient, acct: "AdProfile | None" = None) -> list[dict]:
    """Return every Facebook Page the token can see, with id + name + category."""
    a = _resolve(acct)
    data = await _get(client, "me/accounts", {"fields": "id,name,category", "limit": 200}, token=a.token)
    return data.get("data", [])


def pick_required_currency_account(accounts: list[dict], currency: str | None = None) -> dict:
    """From a list of accounts, return the single one in the required currency.

    Raises if there are zero or more than one — we never guess which to spend on.
    """
    cur = (currency or REQUIRED_CURRENCY).upper()
    matches = [a for a in accounts if (a.get("currency") or "").upper() == cur]
    if not matches:
        raise MetaError(
            f"No ad account in {cur} found among {[a.get('currency') for a in accounts]}."
        )
    if len(matches) > 1:
        listing = ", ".join(f"{a.get('id')} ({a.get('name')})" for a in matches)
        raise MetaError(
            f"Multiple {cur} accounts found: {listing}. "
            f"Set the account id explicitly."
        )
    return matches[0]


# --------------------------------------------------------------------------
# Audience research — read-only, safe.
# --------------------------------------------------------------------------
async def search_interests(
    client: httpx.AsyncClient, query: str, limit: int = 20, acct: "AdProfile | None" = None
) -> list[dict]:
    """Search Meta's targeting interests (e.g. an artist or genre name).

    Returns dicts with id, name, audience_size_lower_bound/upper_bound, path.
    """
    a = _resolve(acct)
    params = {
        "type": "adinterest",
        "q": query,
        "limit": limit,
        "fields": "id,name,audience_size_lower_bound,audience_size_upper_bound,path,topic",
    }
    data = await _get(client, "search", params, token=a.token)
    return data.get("data", [])


async def search_locations(
    client: httpx.AsyncClient,
    query: str,
    country_codes: list[str] | None = None,
    location_types: list[str] | None = None,
    limit: int = 10,
    acct: "AdProfile | None" = None,
) -> list[dict]:
    """Search Meta's geo-location targeting database, scoped to Canada by default.

    Passing country_codes=["CA"] ensures "Edmonton" resolves to Alberta, not Venezuela.
    Returns dicts with key, name, region, country_code, type, region_id.
    """
    a = _resolve(acct)
    params: dict = {
        "type": "adgeolocation",
        "q": query,
        "limit": limit,
    }
    for i, cc in enumerate(country_codes or ["CA"]):
        params[f"country_codes[{i}]"] = cc
    for i, lt in enumerate(location_types or ["city"]):
        params[f"location_types[{i}]"] = lt
    data = await _get(client, "search", params, token=a.token)
    return data.get("data", [])


def build_targeting(
    interest_ids: list[str] | None = None,
    countries: list[str] | None = None,
    cities: list[dict] | None = None,
    age_min: int = 18,
    age_max: int = 65,
    custom_audiences: list[str] | None = None,
    excluded_custom_audiences: list[str] | None = None,
) -> dict:
    """Build a Meta targeting spec. Defaults to Canada.

    Centralizes the Graph API targeting shape here so callers (bot.py) don't have
    to know it. `interest_ids` come from search_interests().
    `cities` is a list of dicts like {"key": "293225", "radius": 40, "distance_unit": "kilometer"}
    from search_locations(). When cities is provided it takes precedence over countries — use it
    for city-specific campaigns so "Edmonton" never resolves to a non-Canadian city.
    """
    if cities:
        geo: dict = {"cities": cities}
    else:
        geo = {"countries": countries or ["CA"]}
    spec: dict = {
        "geo_locations": geo,
        "age_min": age_min,
        "age_max": age_max,
    }
    ids = [str(i).strip() for i in (interest_ids or []) if str(i).strip()]
    if ids:
        spec["flexible_spec"] = [{"interests": [{"id": i} for i in ids]}]
    if custom_audiences:
        spec["custom_audiences"] = [{"id": a} for a in custom_audiences]
    if excluded_custom_audiences:
        spec["excluded_custom_audiences"] = [{"id": a} for a in excluded_custom_audiences]
    # Meta now requires an explicit Advantage Audience decision. 0 = respect our exact
    # audience definitions (no algorithmic expansion); right for retarget/lookalike.
    spec["targeting_automation"] = {"advantage_audience": 0}
    return spec


def _name_matches(name: str, query: str) -> bool:
    """True if an interest `name` is relevant to a search `query` — used to drop
    Meta's tangential search noise. Matches if the full query is a substring of the
    name, or (for multi-word queries) every significant word appears in the name."""
    name_l = name.lower()
    q = query.lower().strip()
    if not q:
        return False
    if q in name_l:
        return True
    words = [w for w in q.split() if len(w) > 2]
    return bool(words) and all(w in name_l for w in words)


async def research_artist_targeting(
    client: httpx.AsyncClient,
    artist_name: str,
    genre: str | None = None,
    similar_artists: list[str] | None = None,
    label: str | None = None,
    acct: "AdProfile | None" = None,
) -> dict:
    """Aggregate Meta targeting interests for an artist plus optional genre, similar
    artists, and label. Read-only — searches interests and de-dupes them.

    Returns {"all_ids": [interest_id, ...], "summary": human-readable text}.
    `all_ids` feeds straight into a /draft command; `summary` is shown to the user.
    """
    a = _resolve(acct)
    queries: list[tuple[str, str]] = [("Artist", artist_name)]
    if genre:
        queries.append(("Genre", genre))
    for s in similar_artists or []:
        if s.strip():
            queries.append(("Similar", s.strip()))
    if label:
        queries.append(("Label", label))

    seen: set[str] = set()
    all_ids: list[str] = []
    sections: list[str] = []
    for tag, q in queries:
        try:
            results = await search_interests(client, q, limit=15, acct=a)
        except MetaError:
            results = []
        # Relevance filter: Meta returns lots of tangential interests for a plain
        # query (e.g. "Sitcoms"/"Uncharted" for "Drake"), and its `topic` field
        # doesn't separate them. The reliable signal is the interest NAME containing
        # the query term. Keep only those; the genuine match (e.g. "Drake (rapper)")
        # always contains it, and noise gets dropped.
        relevant = [it for it in results if _name_matches(it.get("name") or "", q)]
        # If nothing matched by name (unusual phrasing), fall back to the top hit so
        # the query still contributes something rather than vanishing silently.
        if not relevant and results:
            relevant = results[:1]
        # An artist (or similar artist) resolves to ONE interest — Meta ranks the
        # real entity first, so keep only the top match and drop homonyms like
        # "Back to the Future" / "Stock market index future" for a query of "Future".
        # Genres and labels legitimately span many sub-interests, so keep all of those.
        if tag in ("Artist", "Similar"):
            relevant = relevant[:1]
        lines: list[str] = []
        for it in relevant:
            iid = str(it.get("id") or "").strip()
            if not iid or iid in seen:
                continue
            seen.add(iid)
            all_ids.append(iid)
            lo = it.get("audience_size_lower_bound")
            hi = it.get("audience_size_upper_bound")
            size = f"{lo:,}-{hi:,}" if isinstance(lo, int) and isinstance(hi, int) else "size n/a"
            lines.append(f"  - {it.get('name')} (id {iid}) ~{size}")
        if lines:
            sections.append(f"{tag}: {q}\n" + "\n".join(lines[:8]))

    if not all_ids:
        return {"all_ids": [], "summary": f"No targeting interests found for {artist_name!r}."}
    summary = (
        f"Targeting research for {artist_name!r} - {len(all_ids)} interests\n\n"
        + "\n\n".join(sections)
    )
    return {"all_ids": all_ids, "summary": summary}


async def reach_estimate(
    client: httpx.AsyncClient,
    targeting: dict,
    optimization_goal: str = "REACH",
    acct: "AdProfile | None" = None,
) -> dict:
    """Estimate the daily reach of a targeting spec on the account.

    `targeting` is a Meta targeting spec dict (geo, age, interests, etc.).
    """
    a = _resolve(acct)
    _require_account(a)
    import json

    params = {
        "targeting_spec": json.dumps(targeting),
        "optimization_goal": optimization_goal,
    }
    data = await _get(client, f"{a.ad_account_id}/reachestimate", params, token=a.token)
    return data.get("data", data)


# --------------------------------------------------------------------------
# Campaign building — ALWAYS created PAUSED. None of these start spend.
# --------------------------------------------------------------------------
def _require_account(a: AdProfile) -> None:
    if not a.ad_account_id:
        raise MetaError(
            f"No ad account set for @{a.key} ({a.label}). Run "
            f"scripts/find_meta_assets.py @{a.key} to discover it, then pin it in .env."
        )


def _require_page(a: AdProfile) -> None:
    if not a.page_id:
        raise MetaError(
            f"No Facebook Page set for @{a.key} ({a.label}). Add its Page ID to .env "
            f"(find it at facebook.com/your-page → About → Page transparency)."
        )


async def create_campaign(
    client: httpx.AsyncClient,
    name: str,
    objective: str = "OUTCOME_TRAFFIC",
    special_ad_categories: list[str] | None = None,
    acct: "AdProfile | None" = None,
) -> dict:
    """Create a PAUSED campaign. Returns {'id': ...}.

    `special_ad_categories` defaults to [] (none). Meta requires the param to be
    present even when empty.
    """
    a = _resolve(acct)
    _require_account(a)
    import json

    data = {
        "name": name,
        "objective": objective,
        "status": "PAUSED",  # never ACTIVE here — spend is gated by activate_campaign()
        "special_ad_categories": json.dumps(special_ad_categories or []),
        # Meta requires this boolean when the campaign is NOT using campaign budget
        # optimization (we budget at the ad-set level). "false" = ad sets don't pool
        # budget; keeps spend predictable per ad set.
        "is_adset_budget_sharing_enabled": "false",
    }
    return await _post(client, f"{a.ad_account_id}/campaigns", data, token=a.token)


async def create_adset(
    client: httpx.AsyncClient,
    campaign_id: str,
    name: str,
    daily_budget_cents: int,
    targeting: dict,
    optimization_goal: str = "LINK_CLICKS",
    billing_event: str = "IMPRESSIONS",
    bid_strategy: str = "LOWEST_COST_WITHOUT_CAP",
    promoted_object: dict | None = None,
    lifetime_budget_cents: int = 0,
    start_time: str | None = None,
    end_time: str | None = None,
    acct: "AdProfile | None" = None,
) -> dict:
    """Create a PAUSED ad set.

    Budget: pass `daily_budget_cents` for a daily budget, or `lifetime_budget_cents`
    (with `end_time` required and `start_time` optional) for a lifetime budget. The
    two are mutually exclusive — lifetime takes precedence when both are set.
    Times are ISO8601 strings in local timezone, e.g. '2026-06-20T21:00:00-06:00'.
    `daily_budget_cents` / `lifetime_budget_cents` are in the account currency's
    minor units (e.g. CAD cents).
    """
    a = _resolve(acct)
    _require_account(a)
    import json

    data: dict = {
        "name": name,
        "campaign_id": campaign_id,
        "billing_event": billing_event,
        "optimization_goal": optimization_goal,
        "bid_strategy": bid_strategy,
        "targeting": json.dumps(targeting),
        "status": "PAUSED",  # never ACTIVE here
    }
    if lifetime_budget_cents:
        if not end_time:
            raise MetaError("end_time is required when using a lifetime budget.")
        data["lifetime_budget"] = int(lifetime_budget_cents)
        data["end_time"] = end_time
        if start_time:
            data["start_time"] = start_time
    else:
        data["daily_budget"] = int(daily_budget_cents)
        # end_time is valid on daily-budget ad sets too (auto-stop). Apply it so
        # every campaign honours the stop-at-show-end rule, not just lifetime ones.
        if end_time:
            data["end_time"] = end_time
        if start_time:
            data["start_time"] = start_time
    if promoted_object:
        data["promoted_object"] = json.dumps(promoted_object)
    return await _post(client, f"{a.ad_account_id}/adsets", data, token=a.token)


def resolve_media_path(filename: str) -> str:
    """Resolve a bare filename to its full path in MEDIA_DIR.

    Accepts either a plain filename ('flyer.jpg') or an absolute path.
    Raises MetaError if the file doesn't exist.
    """
    import pathlib
    p = pathlib.Path(filename)
    if not p.is_absolute():
        p = pathlib.Path(MEDIA_DIR) / filename
    if not p.exists():
        available = [f.name for f in pathlib.Path(MEDIA_DIR).iterdir() if f.is_file()] if pathlib.Path(MEDIA_DIR).exists() else []
        hint = f" Available: {', '.join(available)}" if available else f" (media folder {MEDIA_DIR} is empty — drop files there)"
        raise MetaError(f"Media file not found: {p}.{hint}")
    return str(p)


def list_media() -> list[str]:
    """Return filenames in the local media folder, sorted newest-first."""
    import pathlib
    d = pathlib.Path(MEDIA_DIR)
    if not d.exists():
        return []
    files = [f for f in d.iterdir() if f.is_file() and not f.name.startswith(".")]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return [f.name for f in files]


async def upload_ad_image(
    client: httpx.AsyncClient, file_path: str, acct: "AdProfile | None" = None
) -> str:
    """Upload a local image file to Meta's ad image library.

    Returns the image hash string, which can be used in create_adcreative()
    as `image_hash`. Meta deduplicates by content, so uploading the same file
    twice returns the same hash.
    """
    a = _resolve(acct)
    _require_account(a)
    import base64
    import pathlib

    path = pathlib.Path(file_path)
    raw = path.read_bytes()
    encoded = base64.b64encode(raw).decode()
    data = {
        "bytes": encoded,
        "name": path.name,
        "access_token": a.token,
    }
    resp = await client.post(f"{_base()}/{a.ad_account_id}/adimages", data=data)
    body = _handle(resp)
    images = body.get("images", {})
    if not images:
        raise MetaError(f"Meta returned no image data after upload: {body}")
    info = next(iter(images.values()))
    h = info.get("hash")
    if not h:
        raise MetaError(f"Meta image upload succeeded but returned no hash: {info}")
    log.info("Uploaded %s → hash %s (@%s)", path.name, h, a.key)
    return h


async def upload_ad_video(
    client: httpx.AsyncClient, file_path: str, acct: "AdProfile | None" = None
) -> str:
    """Upload a local video file to Meta's ad video library.

    Returns the video id string for use in create_adcreative_video() as `video_id`.
    Meta processes the video asynchronously; the id is available immediately for
    creative creation even before processing completes.
    """
    import pathlib
    a = _resolve(acct)
    _require_account(a)
    path = pathlib.Path(file_path)
    raw = path.read_bytes()
    resp = await client.post(
        f"{_base()}/{a.ad_account_id}/advideos",
        data={"access_token": a.token},
        files={"source": (path.name or "ad_video.mp4", raw, "video/mp4")},
    )
    body = _handle(resp)
    vid = body.get("id")
    if not vid:
        raise MetaError(f"Meta video upload returned no id: {body}")
    log.info("Uploaded video %s → id %s (@%s)", path.name, vid, a.key)
    return vid


def is_video(filename: str) -> bool:
    """True if a filename looks like a video (so callers route to the video path)."""
    return str(filename).lower().rsplit(".", 1)[-1] in {"mp4", "mov", "m4v", "webm", "avi"}


# --------------------------------------------------------------------------
# Instagram existing-post creative helpers.
# --------------------------------------------------------------------------
_IG_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def shortcode_to_id(shortcode: str) -> str:
    """Decode an Instagram post shortcode (the /p/<code>/ part) to its numeric media ID."""
    n = 0
    for ch in shortcode:
        n = n * 64 + _IG_ALPHABET.index(ch)
    return str(n)


def ig_post_url_to_media_id(url: str) -> str:
    """Extract the numeric Instagram media ID from a post URL or bare shortcode.

    Accepts full URLs (https://www.instagram.com/p/DZoDE8Ysrhj/) and bare shortcodes.
    """
    import re
    m = re.search(r"/p/([A-Za-z0-9_-]+)", url)
    if m:
        return shortcode_to_id(m.group(1))
    # Bare shortcode
    if re.fullmatch(r"[A-Za-z0-9_-]{10,12}", url.strip()):
        return shortcode_to_id(url.strip())
    raise MetaError(f"Cannot parse Instagram post URL/shortcode: {url!r}")


async def create_adcreative_from_ig_post(
    client: httpx.AsyncClient,
    name: str,
    ig_media_id: str,
    link: str,
    call_to_action_type: str = "GET_EVENT_TICKETS",
    url_tags: str | None = None,
    acct: "AdProfile | None" = None,
) -> dict:
    """Create an ad creative that uses an existing Instagram post as its visual.

    `ig_media_id` is the numeric post ID (use ig_post_url_to_media_id() to convert
    a shortcode/URL). The profile MUST have ig_actor_id set. `link` is the destination
    URL (e.g. a Showpass ticket page); it's added as a CTA button overlay on the post.
    """
    import json as _json
    a = _resolve(acct)
    _require_account(a)
    _require_page(a)
    if not a.ig_actor_id:
        raise MetaError(
            f"No Instagram actor id for @{a.key}. "
            f"Add META_{a.key.upper()}_IG_ACTOR_ID to .env "
            f"(find the numeric IG account id in Meta Business Suite → Accounts → Instagram accounts)."
        )
    story_spec: dict = {
        "instagram_actor_id": a.ig_actor_id,
        "source_instagram_media_id": ig_media_id,
    }
    if link:
        story_spec["link_data"] = {
            "link": link,
            "call_to_action": {"type": call_to_action_type, "value": {"link": link}},
        }
    data: dict = {
        "name": name,
        "object_story_spec": _json.dumps(story_spec),
    }
    if url_tags is None:
        url_tags = (
            "utm_source=instagram&utm_medium=paid_social"
            "&utm_campaign={{campaign.name}}&utm_term={{adset.name}}"
            "&utm_content={{ad.name}}&utm_id={{ad.id}}"
        )
    data["url_tags"] = url_tags
    return await _post(client, f"{a.ad_account_id}/adcreatives", data, token=a.token)


async def wait_for_video(client, video_id, acct=None, timeout=180, interval=5):
    """Poll until Meta finishes processing an uploaded video. Returns True if ready,
    False on timeout (caller may still proceed). Raises on processing error."""
    import asyncio
    a = _resolve(acct)
    waited = 0
    while waited < timeout:
        resp = await client.get(f"{_base()}/{video_id}", params={"access_token": a.token, "fields": "status"})
        st = (_handle(resp).get("status") or {})
        vs = st.get("video_status")
        if vs == "ready":
            return True
        if vs == "error":
            raise MetaError(f"Video {video_id} processing failed: {st}")
        await asyncio.sleep(interval)
        waited += interval
    return False


async def get_video_thumbnail(client, video_id, acct=None):
    """Return a thumbnail URI for a processed video (Meta's preferred frame if any)."""
    a = _resolve(acct)
    resp = await client.get(f"{_base()}/{video_id}/thumbnails", params={"access_token": a.token, "fields": "uri,is_preferred"})
    data = _handle(resp).get("data", []) or []
    if not data:
        return None
    pref = [t for t in data if t.get("is_preferred")]
    return (pref[0] if pref else data[0]).get("uri")


async def create_adcreative_video(
    client: httpx.AsyncClient,
    name: str,
    link: str,
    caption: str,
    video_id: str,
    call_to_action_type: str = "GET_EVENT_TICKETS",
    url_tags: str | None = None,
    image_hash: str | None = None,
    image_url: str | None = None,
    acct: "AdProfile | None" = None,
) -> dict:
    """Create a video ad creative. Returns {'id': ...}.

    Video ads REQUIRE a thumbnail. If image_hash/image_url aren't supplied, this
    waits for Meta to finish processing the video and uses its auto-generated
    poster frame. video_id comes from upload_ad_video(). Works for 9x16 Stories
    and 3x4 Feed — Meta crops per placement.
    """
    import json
    a = _resolve(acct)
    _require_account(a)
    _require_page(a)
    if not image_hash and not image_url:
        await wait_for_video(client, video_id, acct=a)
        image_url = await get_video_thumbnail(client, video_id, acct=a)
    video_data = {
        "video_id": video_id,
        "message": caption,
        "call_to_action": {"type": call_to_action_type, "value": {"link": link}},
    }
    if image_hash:
        video_data["image_hash"] = image_hash
    elif image_url:
        video_data["image_url"] = image_url
    story_spec = {"page_id": a.page_id, "video_data": video_data}
    data = {"name": name, "object_story_spec": json.dumps(story_spec)}
    if url_tags is None:
        url_tags = (
            "utm_source=facebook&utm_medium=paid_social"
            "&utm_campaign={{campaign.name}}&utm_term={{adset.name}}"
            "&utm_content={{ad.name}}&utm_id={{ad.id}}"
        )
    data["url_tags"] = url_tags
    return await _post(client, f"{a.ad_account_id}/adcreatives", data, token=a.token)


async def create_adcreative(
    client: httpx.AsyncClient,
    name: str,
    link: str,
    caption: str,
    image_hash: str | None = None,
    image_url: str | None = None,
    call_to_action_type: str = "GET_EVENT_TICKETS",
    url_tags: str | None = None,
    acct: "AdProfile | None" = None,
) -> dict:
    """Create an ad creative for a ticket-link ad. Returns {'id': ...}.

    `link` is the direct ticket URL (Showpass, Eventbrite, etc.).
    `caption` is the ad body text shown above the link preview.
    `image_hash` takes priority — use upload_ad_image() to get one from a local file.
    `image_url` is a fallback for remote images.
    If neither is provided, Meta pulls the OG preview from the link.
    Requires the profile's Page ID to be set.
    """
    a = _resolve(acct)
    _require_account(a)
    _require_page(a)
    import json

    link_data: dict = {
        "link": link,
        "message": caption,
        "call_to_action": {
            "type": call_to_action_type,
            "value": {"link": link},
        },
    }
    if image_hash:
        link_data["image_hash"] = image_hash
    elif image_url:
        link_data["picture"] = image_url

    story_spec = {
        "page_id": a.page_id,
        "link_data": link_data,
    }
    data = {
        "name": name,
        "object_story_spec": json.dumps(story_spec),
    }
    # Standardized UTM tracking auto-appended to every outbound ticket link.
    if url_tags is None:
        url_tags = (
            "utm_source=facebook&utm_medium=paid_social"
            "&utm_campaign={{campaign.name}}&utm_term={{adset.name}}"
            "&utm_content={{ad.name}}&utm_id={{ad.id}}"
        )
    data["url_tags"] = url_tags
    return await _post(client, f"{a.ad_account_id}/adcreatives", data, token=a.token)


async def create_ad(
    client: httpx.AsyncClient,
    adset_id: str,
    name: str,
    creative_id: str,
    acct: "AdProfile | None" = None,
) -> dict:
    """Create a PAUSED ad from an existing creative id."""
    a = _resolve(acct)
    _require_account(a)
    import json

    data = {
        "name": name,
        "adset_id": adset_id,
        "creative": json.dumps({"creative_id": creative_id}),
        "status": "PAUSED",  # never ACTIVE here
    }
    return await _post(client, f"{a.ad_account_id}/ads", data, token=a.token)


# --------------------------------------------------------------------------
# THE SPEND GATE. These are the only functions that can start spending money.
# bot.py must only call them after an explicit per-campaign approval.
# --------------------------------------------------------------------------
async def activate_campaign(
    client: httpx.AsyncClient, campaign_id: str, acct: "AdProfile | None" = None
) -> dict:
    """Set a campaign ACTIVE — this STARTS SPEND. Caller is responsible for having
    obtained Greg's explicit, per-campaign go-ahead before calling."""
    a = _resolve(acct)
    log.warning("ACTIVATING campaign %s on @%s — spend will begin", campaign_id, a.key)
    return await _post(client, campaign_id, {"status": "ACTIVE"}, token=a.token)


async def pause_campaign(
    client: httpx.AsyncClient, campaign_id: str, acct: "AdProfile | None" = None
) -> dict:
    """Set a campaign PAUSED — stops spend. Always safe to call."""
    a = _resolve(acct)
    return await _post(client, campaign_id, {"status": "PAUSED"}, token=a.token)


# --------------------------------------------------------------------------
# Reporting / optimization data — read-only.
# --------------------------------------------------------------------------
async def get_insights(
    client: httpx.AsyncClient,
    object_id: str,
    date_preset: str = "last_7d",
    fields: str = "campaign_name,impressions,reach,clicks,ctr,cpc,spend,actions",
    acct: "AdProfile | None" = None,
) -> list[dict]:
    """Pull insights for any object (campaign/adset/ad/account id)."""
    a = _resolve(acct)
    params = {"date_preset": date_preset, "fields": fields, "limit": 200}
    data = await _get(client, f"{object_id}/insights", params, token=a.token)
    return data.get("data", [])


# --------------------------------------------------------------------------
# Sales campaign framework — auto-objective + 3-ad-set build.
# --------------------------------------------------------------------------
def plan_objective(ticket_link: str | None, acct: "AdProfile | None" = None) -> dict:
    """Pick objective + optimization for a ticket link's platform.
    Pixel-trackable (TicketWeb/Showpass/...) -> OUTCOME_SALES optimized for PURCHASE.
    Otherwise (Ticketmaster/unknown) -> OUTCOME_TRAFFIC optimized for landing-page views."""
    a = _resolve(acct)
    link = (ticket_link or "").lower()
    if a.pixel_id and any(d in link for d in _PIXEL_TRACKABLE):
        return {
            "objective": "OUTCOME_SALES",
            "optimization_goal": "OFFSITE_CONVERSIONS",
            "promoted_object": {"pixel_id": a.pixel_id, "custom_event_type": "PURCHASE"},
            "platform": "pixel-trackable → purchase-optimized",
        }
    return {
        "objective": "OUTCOME_TRAFFIC",
        "optimization_goal": "LANDING_PAGE_VIEWS",
        "promoted_object": None,
        "platform": "no pixel → landing-page-view optimized",
    }


async def find_audiences(
    client: httpx.AsyncClient, names: list[str], acct: "AdProfile | None" = None
) -> dict:
    """Return {name: id} for existing custom audiences matching the given names."""
    a = _resolve(acct)
    data = await _get(client, f"{a.ad_account_id}/customaudiences",
                      {"fields": "name", "limit": 500}, token=a.token)
    by = {x.get("name"): x.get("id") for x in data.get("data", [])}
    return {n: by[n] for n in names if n in by}


async def build_show_campaign(
    client: httpx.AsyncClient,
    name: str,
    daily_cad: float,
    ticket_link: str,
    caption: str,
    interest_ids: list[str] | None = None,
    image_hash: str | None = None,
    image_url: str | None = None,
    video_id: str | None = None,
    countries: list[str] | None = None,
    acct: "AdProfile | None" = None,
) -> dict:
    """Build a PAUSED, sales-structured campaign for one show:
      - objective/optimization auto-picked from the ticket link platform
      - up to 3 ad sets: Retargeting (warm), Lookalike, Cold (interests)
      - one creative, cloned into an ad per ad set
    Budget split 25/35/40 (retarget/lookalike/cold), min 1.00/day each. All PAUSED.
    `daily_cad` is in the profile's account currency (kept named for back-compat)."""
    a = _resolve(acct)
    countries = countries or a.default_countries
    plan = plan_objective(ticket_link, acct=a)
    camp = await create_campaign(client, name, objective=plan["objective"], acct=a)
    campaign_id = camp["id"]

    aud_names = list(a.retarget_audiences) + list(a.lookalike_audiences)
    auds = await find_audiences(client, aud_names, acct=a) if aud_names else {}
    retarget_ids = [auds[n] for n in a.retarget_audiences if n in auds]
    lal_ids = [auds[n] for n in a.lookalike_audiences if n in auds]

    total = int(round(daily_cad * 100))
    splits = {
        "Retargeting": max(100, int(total * 0.25)),
        "Lookalike": max(100, int(total * 0.35)),
        "Cold": max(100, int(total * 0.40)),
    }
    targetings = {
        "Retargeting": build_targeting(custom_audiences=retarget_ids, countries=countries) if retarget_ids else None,
        "Lookalike": build_targeting(custom_audiences=lal_ids, excluded_custom_audiences=retarget_ids, countries=countries) if lal_ids else None,
        "Cold": build_targeting(interest_ids=interest_ids, excluded_custom_audiences=retarget_ids + lal_ids, countries=countries),
    }

    if video_id:
        creative = await create_adcreative_video(client, f"{name} — creative", ticket_link, caption,
                                                 video_id, image_hash=image_hash, image_url=image_url, acct=a)
    else:
        creative = await create_adcreative(client, f"{name} — creative", ticket_link, caption,
                                           image_hash=image_hash, image_url=image_url, acct=a)
    creative_id = creative["id"]

    adsets = []
    for layer in ("Retargeting", "Lookalike", "Cold"):
        tgt = targetings[layer]
        if not tgt:
            continue
        aset = await create_adset(client, campaign_id, f"{name} — {layer}", splits[layer], tgt,
                                  optimization_goal=plan["optimization_goal"],
                                  promoted_object=plan["promoted_object"], acct=a)
        await create_ad(client, aset["id"], f"{name} — {layer} ad", creative_id, acct=a)
        adsets.append({"layer": layer, "adset_id": aset.get("id"), "daily_cents": splits[layer]})

    return {"campaign_id": campaign_id, "objective": plan["objective"],
            "optimization": plan["optimization_goal"], "platform": plan["platform"],
            "adsets": adsets, "creative_id": creative_id}


async def activate_full(
    client: httpx.AsyncClient, campaign_id: str, acct: "AdProfile | None" = None
) -> dict:
    """Activate a campaign AND all its ad sets + ads (STARTS SPEND). A campaign alone
    going ACTIVE won't deliver if its children are PAUSED, so flip every level."""
    a = _resolve(acct)
    log.warning("ACTIVATING campaign %s on @%s (full: adsets+ads) — spend will begin", campaign_id, a.key)
    adsets = await _get(client, f"{campaign_id}/adsets", {"fields": "id", "limit": 50}, token=a.token)
    for aset in adsets.get("data", []):
        await _post(client, aset["id"], {"status": "ACTIVE"}, token=a.token)
        ads = await _get(client, f"{aset['id']}/ads", {"fields": "id", "limit": 50}, token=a.token)
        for ad in ads.get("data", []):
            await _post(client, ad["id"], {"status": "ACTIVE"}, token=a.token)
    return await _post(client, campaign_id, {"status": "ACTIVE"}, token=a.token)
