"""TikTok Marketing API client for Pedro.

Mirrors the meta_ads.py safety contract exactly:

    Nothing this module creates ever goes live on its own.

Every campaign, ad group, and ad is created with status DISABLE (TikTok's
equivalent of PAUSED). The only functions that can start spend are
activate_campaign() / activate_full(), and bot.py only calls them after an
explicit per-campaign approval (same inline-button flow as Meta). Do not set
status="ENABLE" anywhere else.

API version: TikTok Marketing API v1.3
Base URL: https://business-api.tiktok.com/open_api/v1.3/
Auth: Access-Token request header + advertiser_id body/query param.

Profile model
-------------
Each ad account is a TikProfile carrying its own access_token, advertiser_id,
identity_id (TikTok Page / identity for creatives), and pixel_id. Profiles
load from env vars: TIKTOK_PROFILES (comma list of keys), and for each key <K>:
  TIKTOK_<K>_ACCESS_TOKEN
  TIKTOK_<K>_ADVERTISER_ID
  TIKTOK_<K>_IDENTITY_ID   (TikTok Business Center identity / Spark Ads author)
  TIKTOK_<K>_PIXEL_ID      (optional — enables conversion campaigns)
  TIKTOK_<K>_LABEL         (human name, e.g. "Nightshift Entertainment")
  TIKTOK_<K>_CURRENCY      (guard against wrong account; default CAD)
  TIKTOK_<K>_COUNTRIES     (comma list, default CA)

The default profile key is "nightshift" and reads bare TIKTOK_* vars for
backwards compat once Gabe adds those to .env.
"""
import dataclasses
import logging
import os

import httpx

log = logging.getLogger("nightshift.tiktok_ads")

# ---------------------------------------------------------------------------
# Default / legacy env vars — mapped to the "nightshift" profile automatically.
# ---------------------------------------------------------------------------
ACCESS_TOKEN = os.environ.get("TIKTOK_ACCESS_TOKEN", "")
ADVERTISER_ID = os.environ.get("TIKTOK_ADVERTISER_ID", "")
IDENTITY_ID = os.environ.get("TIKTOK_IDENTITY_ID", "")
PIXEL_ID = os.environ.get("TIKTOK_PIXEL_ID", "")
REQUIRED_CURRENCY = os.environ.get("TIKTOK_REQUIRED_CURRENCY", "CAD")
MEDIA_DIR = os.environ.get("TIKTOK_MEDIA_DIR", "/data/greg/ads")

API_VERSION = "v1.3"
API_BASE = f"https://business-api.tiktok.com/open_api/{API_VERSION}"

REPORT_RECIPIENTS = [
    x.strip()
    for x in os.environ.get("TIKTOK_REPORT_RECIPIENTS", "seba@nightshiftent.ca").split(",")
    if x.strip()
]


# ---------------------------------------------------------------------------
# Profile model
# ---------------------------------------------------------------------------
class TikTokError(Exception):
    """User-facing failure from the TikTok Marketing API."""


@dataclasses.dataclass
class TikProfile:
    key: str
    label: str
    access_token: str
    advertiser_id: str
    identity_id: str = ""
    pixel_id: str = ""
    currency: str = "CAD"
    default_countries: list = dataclasses.field(default_factory=lambda: ["CA"])

    @property
    def has_token(self) -> bool:
        return bool(self.access_token)

    @property
    def ready(self) -> bool:
        return bool(self.access_token and self.advertiser_id)

    def status_line(self) -> str:
        if self.ready:
            state = "✅ ready"
        elif self.access_token:
            state = "⚠️ token set, missing advertiser_id"
        else:
            state = "❌ not configured (no token)"
        return f"@{self.key} — {self.label} [{self.currency}] {state} (acct {self.advertiser_id or '—'})"


def _csv(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _load_profiles() -> dict[str, "TikProfile"]:
    keys = _csv(os.environ.get("TIKTOK_PROFILES", "nightshift")) or ["nightshift"]
    profiles: dict[str, TikProfile] = {}
    for key in keys:
        ku = key.upper()
        if key == "nightshift":
            profiles[key] = TikProfile(
                key="nightshift",
                label=os.environ.get("TIKTOK_NIGHTSHIFT_LABEL", "Nightshift Entertainment"),
                access_token=ACCESS_TOKEN,
                advertiser_id=ADVERTISER_ID,
                identity_id=IDENTITY_ID,
                pixel_id=PIXEL_ID,
                currency=REQUIRED_CURRENCY,
                default_countries=_csv(os.environ.get("TIKTOK_NIGHTSHIFT_COUNTRIES")) or ["CA"],
            )
        else:
            profiles[key] = TikProfile(
                key=key,
                label=os.environ.get(f"TIKTOK_{ku}_LABEL", key.replace("_", " ").title()),
                access_token=os.environ.get(f"TIKTOK_{ku}_ACCESS_TOKEN", ""),
                advertiser_id=os.environ.get(f"TIKTOK_{ku}_ADVERTISER_ID", ""),
                identity_id=os.environ.get(f"TIKTOK_{ku}_IDENTITY_ID", ""),
                pixel_id=os.environ.get(f"TIKTOK_{ku}_PIXEL_ID", ""),
                currency=os.environ.get(f"TIKTOK_{ku}_CURRENCY", "CAD"),
                default_countries=_csv(os.environ.get(f"TIKTOK_{ku}_COUNTRIES")) or ["CA"],
            )
    return profiles


PROFILES: dict[str, TikProfile] = _load_profiles()
DEFAULT_PROFILE_KEY = next(iter(PROFILES), "nightshift")


def list_profiles() -> list[TikProfile]:
    return list(PROFILES.values())


def get_profile(key: str | None) -> TikProfile:
    if not key:
        return PROFILES[DEFAULT_PROFILE_KEY]
    k = key.strip().lstrip("@").lower()
    if k not in PROFILES:
        known = ", ".join("@" + p for p in PROFILES)
        raise TikTokError(f"Unknown TikTok profile '@{k}'. Known: {known}.")
    return PROFILES[k]


def _resolve(acct: "TikProfile | None") -> TikProfile:
    return acct if acct is not None else PROFILES[DEFAULT_PROFILE_KEY]


def configured(acct: "TikProfile | None" = None) -> bool:
    return bool(_resolve(acct).access_token)


# ---------------------------------------------------------------------------
# HTTP primitives — TikTok uses Access-Token header, JSON body, code==0 for success.
# ---------------------------------------------------------------------------
def _headers(token: str) -> dict:
    return {
        "Access-Token": token,
        "Content-Type": "application/json",
    }


def _handle(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 300:
        raise TikTokError(f"TikTok API HTTP {resp.status_code}: {resp.text[:400]}")
    code = body.get("code", -1)
    if code != 0:
        msg = body.get("message") or body.get("msg") or str(body)[:400]
        raise TikTokError(f"TikTok API error {code}: {msg}")
    return body.get("data", body)


async def _get(
    client: httpx.AsyncClient, path: str, params: dict | None = None, acct: "TikProfile | None" = None
) -> dict:
    a = _resolve(acct)
    p = dict(params or {})
    p.setdefault("advertiser_id", a.advertiser_id)
    resp = await client.get(f"{API_BASE}/{path.lstrip('/')}", params=p, headers=_headers(a.access_token))
    return _handle(resp)


async def _post(
    client: httpx.AsyncClient, path: str, body: dict, acct: "TikProfile | None" = None
) -> dict:
    a = _resolve(acct)
    payload = dict(body)
    payload.setdefault("advertiser_id", a.advertiser_id)
    resp = await client.post(f"{API_BASE}/{path.lstrip('/')}", json=payload, headers=_headers(a.access_token))
    return _handle(resp)


# ---------------------------------------------------------------------------
# Account discovery — verify credentials and find the right advertiser.
# ---------------------------------------------------------------------------
async def get_advertiser_info(client: httpx.AsyncClient, acct: "TikProfile | None" = None) -> dict:
    """Return basic info for the configured advertiser (name, currency, status)."""
    a = _resolve(acct)
    return await _get(client, "advertiser/info/", {"fields": '["name","currency","status","timezone"]'}, acct=a)


async def verify_credentials(client: httpx.AsyncClient, acct: "TikProfile | None" = None) -> str:
    """Human-readable credential check. Returns a status line."""
    a = _resolve(acct)
    if not a.has_token:
        return f"@{a.key}: ❌ No access token set. Ask Gabe to complete setup."
    if not a.advertiser_id:
        return f"@{a.key}: ⚠️ Token present but no advertiser_id. Run tiktok_find_assets.py."
    try:
        info = await get_advertiser_info(client, a)
        advertiser = (info.get("list") or [{}])[0] if isinstance(info.get("list"), list) else info
        name = advertiser.get("name", "—")
        currency = advertiser.get("currency", "—")
        status = advertiser.get("status", "—")
        return f"@{a.key}: ✅ {name} | currency={currency} | status={status}"
    except TikTokError as e:
        return f"@{a.key}: ❌ API error — {e}"


# ---------------------------------------------------------------------------
# Audience / targeting research — read-only, safe.
# ---------------------------------------------------------------------------
async def search_interests(
    client: httpx.AsyncClient, query: str, acct: "TikProfile | None" = None
) -> list[dict]:
    """Search TikTok interest categories matching a keyword (artist, genre, etc.).

    Returns dicts with id and name. Feed the ids into build_targeting().
    """
    a = _resolve(acct)
    data = await _get(
        client,
        "targeting/search/",
        {"scene": "INTEREST_KEYWORD", "keyword": query},
        acct=a,
    )
    return data.get("interest_categories", data.get("list", []))


async def research_artist_targeting(
    client: httpx.AsyncClient,
    artist_name: str,
    genre: str | None = None,
    similar_artists: list[str] | None = None,
    acct: "TikProfile | None" = None,
) -> dict:
    """Search TikTok interest categories for an artist and optional genre/related artists.

    Returns {"all_ids": [...], "summary": human-readable string}.
    """
    a = _resolve(acct)
    queries: list[tuple[str, str]] = [("Artist", artist_name)]
    if genre:
        queries.append(("Genre", genre))
    for s in similar_artists or []:
        if s.strip():
            queries.append(("Similar", s.strip()))

    seen: set[str] = set()
    all_ids: list[str] = []
    sections: list[str] = []
    for tag, q in queries:
        try:
            results = await search_interests(client, q, acct=a)
        except TikTokError:
            results = []
        lines: list[str] = []
        for it in results[:5]:
            iid = str(it.get("id") or it.get("interest_category_id", ""))
            name = it.get("name") or it.get("interest_category_name", "—")
            if iid and iid not in seen:
                seen.add(iid)
                all_ids.append(iid)
                lines.append(f"  {name} (id {iid})")
        if lines:
            sections.append(f"{tag}: {q}\n" + "\n".join(lines))

    summary = "\n\n".join(sections) if sections else "No interests found."
    return {"all_ids": all_ids, "summary": summary}


def build_targeting(
    interest_ids: list[str] | None = None,
    countries: list[str] | None = None,
    age_groups: list[str] | None = None,
    gender: str = "GENDER_UNLIMITED",
    custom_audience_ids: list[str] | None = None,
    excluded_audience_ids: list[str] | None = None,
    languages: list[str] | None = None,
) -> dict:
    """Build a TikTok ad group targeting spec. Defaults: Canada, all ages 18+, all genders.

    age_groups options: AGE_18_24, AGE_25_34, AGE_35_44, AGE_45_54, AGE_55_100
    gender options: GENDER_FEMALE, GENDER_MALE, GENDER_UNLIMITED
    """
    spec: dict = {
        "location_ids": _country_codes_to_ids(countries or ["CA"]),
        "gender": gender,
        "age_groups": age_groups or ["AGE_18_24", "AGE_25_34", "AGE_35_44", "AGE_45_54", "AGE_55_100"],
    }
    if interest_ids:
        spec["interest_category_ids"] = [str(i) for i in interest_ids]
    if custom_audience_ids:
        spec["custom_audience_ids"] = [str(i) for i in custom_audience_ids]
    if excluded_audience_ids:
        spec["excluded_custom_audience_ids"] = [str(i) for i in excluded_audience_ids]
    if languages:
        spec["languages"] = languages
    return spec


# TikTok requires numeric location IDs, not ISO codes.
# These are the most common ones for Nightshift's markets.
_COUNTRY_ID_MAP = {
    "CA": "6252001",   # Canada
    "US": "6252001",   # USA — TikTok uses GeoNames IDs; use the real ones from targeting/search
}

# Province/city IDs for Canadian markets (from TikTok targeting API).
_CA_CITY_IDS = {
    "Edmonton": "5946768",
    "Winnipeg": "6183235",
    "Calgary": "5913490",
    "Vancouver": "6173331",
    "Toronto": "6167865",
    "Montreal": "6077243",
}


def _country_codes_to_ids(codes: list[str]) -> list[str]:
    """Convert ISO-2 country codes to TikTok GeoNames location IDs.

    Falls back to raw string if the code isn't in our map (TikTok also accepts
    numeric IDs passed directly). The real production IDs should be fetched via
    targeting/search/ with scene=GEO — this map covers Nightshift's typical markets.
    """
    return [_COUNTRY_ID_MAP.get(c.upper(), c) for c in codes]


def city_id(city: str) -> str | None:
    """Look up a TikTok location ID for a Canadian city by name."""
    return _CA_CITY_IDS.get(city)


def plan_objective(ticket_link: str | None) -> dict:
    """Pick the right TikTok campaign objective for a ticket link.

    With a ticket link: TRAFFIC (drive clicks to ticket page).
    Without: REACH (pure awareness).
    Returns {"objective_type": ..., "optimization_goal": ..., "billing_event": ...}.
    """
    if ticket_link:
        return {
            "objective_type": "TRAFFIC",
            "optimization_goal": "CLICK",
            "billing_event": "CPC",
        }
    return {
        "objective_type": "REACH",
        "optimization_goal": "REACH",
        "billing_event": "CPM",
    }


# ---------------------------------------------------------------------------
# Campaign building — ALWAYS created DISABLE. None of these start spend.
# ---------------------------------------------------------------------------
def _require_advertiser(a: TikProfile) -> None:
    if not a.advertiser_id:
        raise TikTokError(
            f"No advertiser_id set for @{a.key} ({a.label}). "
            f"Run scripts/tiktok_find_assets.py to discover it, then set TIKTOK_ADVERTISER_ID in .env."
        )


async def create_campaign(
    client: httpx.AsyncClient,
    name: str,
    objective_type: str = "TRAFFIC",
    budget: float = 0,
    budget_mode: str = "BUDGET_MODE_TOTAL",
    acct: "TikProfile | None" = None,
) -> dict:
    """Create a DISABLED campaign. Returns the full API response data dict.

    budget is in the account's currency (CAD). 0 = no campaign-level budget
    (budget set per ad group instead). budget_mode: BUDGET_MODE_TOTAL or BUDGET_MODE_DAY.
    """
    a = _resolve(acct)
    _require_advertiser(a)
    payload: dict = {
        "campaign_name": name,
        "objective_type": objective_type,
        "operation_status": "DISABLE",  # never ENABLE here — gated by activate_campaign()
        "budget_mode": budget_mode if budget > 0 else "BUDGET_MODE_INFINITE",
    }
    if budget > 0:
        payload["budget"] = budget
    return await _post(client, "campaign/create/", payload, acct=a)


async def create_adgroup(
    client: httpx.AsyncClient,
    campaign_id: str,
    name: str,
    budget: float,
    targeting: dict,
    budget_mode: str = "BUDGET_MODE_DAY",
    optimization_goal: str = "CLICK",
    billing_event: str = "CPC",
    bid_type: str = "BID_TYPE_NO_BID",
    placement_type: str = "PLACEMENT_TYPE_AUTOMATIC",
    acct: "TikProfile | None" = None,
) -> dict:
    """Create a DISABLED ad group. budget is daily spend in CAD (or profile currency).

    placement_type: PLACEMENT_TYPE_AUTOMATIC (recommended) or PLACEMENT_TYPE_NORMAL.
    """
    a = _resolve(acct)
    _require_advertiser(a)
    payload: dict = {
        "campaign_id": campaign_id,
        "adgroup_name": name,
        "budget_mode": budget_mode,
        "budget": budget,
        "optimization_goal": optimization_goal,
        "billing_event": billing_event,
        "bid_type": bid_type,
        "placement_type": placement_type,
        "operation_status": "DISABLE",  # never ENABLE here
        **targeting,
    }
    return await _post(client, "adgroup/create/", payload, acct=a)


async def upload_image(
    client: httpx.AsyncClient, file_path: str, acct: "TikProfile | None" = None
) -> str:
    """Upload a local image to TikTok's ad image library.

    Returns the image_id string for use in create_ad().
    """
    import pathlib
    a = _resolve(acct)
    _require_advertiser(a)
    path = pathlib.Path(file_path)
    if not path.exists():
        raise TikTokError(f"Image file not found: {file_path}")
    with open(path, "rb") as f:
        files = {"image_file": (path.name, f, "image/jpeg")}
        resp = await client.post(
            f"{API_BASE}/file/image/ad/upload/",
            files=files,
            data={"advertiser_id": a.advertiser_id},
            headers={"Access-Token": a.access_token},
        )
    data = _handle(resp)
    image_id = data.get("image_id") or (data.get("list") or [{}])[0].get("image_id")
    if not image_id:
        raise TikTokError(f"TikTok image upload returned no image_id: {data}")
    log.info("Uploaded %s → image_id %s (@%s)", path.name, image_id, a.key)
    return image_id


def resolve_media_path(filename: str) -> str:
    import pathlib
    p = pathlib.Path(filename)
    if not p.is_absolute():
        p = pathlib.Path(MEDIA_DIR) / filename
    if not p.exists():
        available = [f.name for f in pathlib.Path(MEDIA_DIR).iterdir() if f.is_file()] if pathlib.Path(MEDIA_DIR).exists() else []
        hint = f" Available: {', '.join(available)}" if available else f" (media folder {MEDIA_DIR} is empty)"
        raise TikTokError(f"Media file not found: {p}.{hint}")
    return str(p)


def list_media() -> list[str]:
    import pathlib
    d = pathlib.Path(MEDIA_DIR)
    if not d.exists():
        return []
    files = [f for f in d.iterdir() if f.is_file() and not f.name.startswith(".")]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return [f.name for f in files]


async def create_ad(
    client: httpx.AsyncClient,
    adgroup_id: str,
    name: str,
    image_id: str,
    ad_text: str,
    landing_page_url: str,
    call_to_action: str = "LEARN_MORE",
    identity_id: str | None = None,
    acct: "TikProfile | None" = None,
) -> dict:
    """Create a DISABLED ad under an ad group.

    image_id comes from upload_image(). call_to_action options include:
    LEARN_MORE, SHOP_NOW, BOOK_NOW, GET_TICKETS, SIGN_UP.
    identity_id defaults to the profile's identity_id (TikTok Business identity).
    """
    a = _resolve(acct)
    _require_advertiser(a)
    iid = identity_id or a.identity_id
    if not iid:
        raise TikTokError(
            f"No identity_id for @{a.key}. Set TIKTOK_IDENTITY_ID in .env "
            f"(found in TikTok Ads Manager → Assets → Creative → TikTok Account)."
        )
    payload = {
        "adgroup_id": adgroup_id,
        "ad_name": name,
        "ad_format": "SINGLE_IMAGE",
        "image_ids": [image_id],
        "ad_text": ad_text,
        "landing_page_url": landing_page_url,
        "call_to_action": call_to_action,
        "identity_id": iid,
        "operation_status": "DISABLE",  # never ENABLE here
    }
    return await _post(client, "ad/create/", payload, acct=a)


# ---------------------------------------------------------------------------
# Activation — the ONLY path to start spend. Called only after explicit approval.
# ---------------------------------------------------------------------------
async def activate_campaign(
    client: httpx.AsyncClient, campaign_id: str, acct: "TikProfile | None" = None
) -> dict:
    """Set a campaign ENABLE — starts spend. ONLY call after Greg's explicit approval."""
    a = _resolve(acct)
    return await _post(
        client,
        "campaign/status/update/",
        {"campaign_ids": [campaign_id], "operation_status": "ENABLE"},
        acct=a,
    )


async def pause_campaign(
    client: httpx.AsyncClient, campaign_id: str, acct: "TikProfile | None" = None
) -> dict:
    """Pause a campaign — stops spend immediately. Always safe."""
    a = _resolve(acct)
    return await _post(
        client,
        "campaign/status/update/",
        {"campaign_ids": [campaign_id], "operation_status": "DISABLE"},
        acct=a,
    )


async def activate_full(
    client: httpx.AsyncClient,
    campaign_id: str,
    adgroup_ids: list[str],
    ad_ids: list[str],
    acct: "TikProfile | None" = None,
) -> dict:
    """Enable campaign + all ad groups + all ads in one call. Use after approval."""
    a = _resolve(acct)
    results = {}
    results["campaign"] = await _post(
        client, "campaign/status/update/",
        {"campaign_ids": [campaign_id], "operation_status": "ENABLE"}, acct=a,
    )
    if adgroup_ids:
        results["adgroups"] = await _post(
            client, "adgroup/status/update/",
            {"adgroup_ids": adgroup_ids, "operation_status": "ENABLE"}, acct=a,
        )
    if ad_ids:
        results["ads"] = await _post(
            client, "ad/status/update/",
            {"ad_ids": ad_ids, "operation_status": "ENABLE"}, acct=a,
        )
    return results


# ---------------------------------------------------------------------------
# Reporting — read-only, safe.
# ---------------------------------------------------------------------------
async def get_campaign_report(
    client: httpx.AsyncClient,
    campaign_ids: list[str],
    start_date: str,
    end_date: str,
    metrics: list[str] | None = None,
    acct: "TikProfile | None" = None,
) -> list[dict]:
    """Fetch integrated report for given campaign IDs.

    start_date / end_date: YYYY-MM-DD strings.
    Default metrics: spend, impressions, clicks, CTR, CPM, CPC.
    """
    a = _resolve(acct)
    _require_advertiser(a)
    default_metrics = ["spend", "impressions", "clicks", "ctr", "cpm", "cpc", "reach", "frequency"]
    payload = {
        "report_type": "BASIC",
        "dimensions": ["campaign_id"],
        "metrics": metrics or default_metrics,
        "start_date": start_date,
        "end_date": end_date,
        "filtering": [{"field_name": "campaign_ids", "filter_type": "IN", "filter_value": str(campaign_ids)}],
        "page_size": 100,
    }
    data = await _post(client, "report/integrated/get/", payload, acct=a)
    return data.get("list", [])


def format_report(rows: list[dict]) -> str:
    """Format a report row list into a human-readable summary for Telegram."""
    if not rows:
        return "No data yet."
    lines = []
    for row in rows:
        dims = row.get("dimensions", {})
        metrics = row.get("metrics", {})
        cid = dims.get("campaign_id", "—")
        spend = metrics.get("spend", "—")
        impr = metrics.get("impressions", "—")
        clicks = metrics.get("clicks", "—")
        ctr = metrics.get("ctr", "—")
        cpc = metrics.get("cpc", "—")
        lines.append(
            f"Campaign {cid}\n"
            f"  Spend: ${spend} | Impressions: {impr} | Clicks: {clicks}\n"
            f"  CTR: {ctr}% | CPC: ${cpc}"
        )
    return "\n\n".join(lines)
