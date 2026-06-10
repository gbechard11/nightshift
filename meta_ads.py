"""Meta (Facebook/Instagram) Marketing API client for Pedro.

Gives Pedro the primitives to research audiences and build ad campaigns on the
Nightshift **CAD** ad account, while enforcing one hard rule in code:

    Nothing this module creates ever goes live on its own.

Every campaign, ad set and ad is created **PAUSED**. The only function that can
start spend is `activate_campaign()`, and bot.py only calls it after an explicit
per-campaign approval (the inline-button flow, mirroring /call). Do not add an
`ACTIVE` status anywhere else.

Design mirrors whatsapp.py: env config up top, a `configured()` gate, and async
httpx calls. The module never imports bot.py.

Auth: set META_ACCESS_TOKEN to a long-lived **System User token** with the
`ads_management` permission (read-only insights only need `ads_read`). The CAD
ad account id is discovered with `find_ad_accounts()` / scripts/find_cad_account.py
and then pinned in META_AD_ACCOUNT_ID so we never accidentally touch the USD one.
"""
import logging
import os

import httpx

log = logging.getLogger("nightshift.meta_ads")

ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN", "")
# Pinned CAD ad account, e.g. "act_1234567890". Discover it once via
# scripts/find_cad_account.py, then set it here so we never touch the USD account.
AD_ACCOUNT_ID = os.environ.get("META_AD_ACCOUNT_ID", "")
# Facebook Page ID — required for ad creatives. Find it at facebook.com/your-page → About.
PAGE_ID = os.environ.get("META_PAGE_ID", "")
GRAPH_VERSION = os.environ.get("META_GRAPH_VERSION", "v21.0")
# Local folder where ad media (flyers, images) are stored. Drop files here and
# reference them by filename in /draft. Synced from Drive via scripts/drive_sync.py.
MEDIA_DIR = os.environ.get("META_MEDIA_DIR", "/data/greg/ads")
# Currency the account MUST be in. A guard against pointing at the wrong account.
REQUIRED_CURRENCY = os.environ.get("META_REQUIRED_CURRENCY", "CAD")
# Who gets copied on reports / optimization updates (comma-separated).
REPORT_RECIPIENTS = [
    x.strip()
    for x in os.environ.get("META_REPORT_RECIPIENTS", "seba@nightshiftent.ca").split(",")
    if x.strip()
]

GRAPH_BASE = "https://graph.facebook.com/{ver}"


class MetaError(Exception):
    """User-facing failure from a Graph API call."""


def configured() -> bool:
    """True if we at least have a token. Account id is needed for writes but not
    for discovery, so it isn't required here."""
    return bool(ACCESS_TOKEN)


def _base() -> str:
    return GRAPH_BASE.format(ver=GRAPH_VERSION)


async def _get(client: httpx.AsyncClient, path: str, params: dict | None = None) -> dict:
    params = dict(params or {})
    params["access_token"] = ACCESS_TOKEN
    resp = await client.get(f"{_base()}/{path.lstrip('/')}", params=params)
    return _handle(resp)


async def _post(client: httpx.AsyncClient, path: str, data: dict) -> dict:
    data = dict(data)
    data["access_token"] = ACCESS_TOKEN
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
# Account discovery — how we find the CAD ad account id.
# --------------------------------------------------------------------------
async def find_ad_accounts(client: httpx.AsyncClient) -> list[dict]:
    """Return every ad account the token can see, with name + currency + status."""
    out: list[dict] = []
    params = {
        # NB: no `business` field — that requires business_management permission,
        # which our ads_management/ads_read token doesn't carry. We don't need it.
        "fields": "id,account_id,name,currency,account_status",
        "limit": 200,
    }
    data = await _get(client, "me/adaccounts", params)
    while True:
        out.extend(data.get("data", []))
        next_url = (data.get("paging") or {}).get("next")
        if not next_url:
            break
        resp = await client.get(next_url)
        data = _handle(resp)
    return out


def pick_required_currency_account(accounts: list[dict]) -> dict:
    """From a list of accounts, return the single one in REQUIRED_CURRENCY (CAD).

    Raises if there are zero or more than one — we never guess which to spend on.
    """
    matches = [a for a in accounts if (a.get("currency") or "").upper() == REQUIRED_CURRENCY.upper()]
    if not matches:
        raise MetaError(
            f"No ad account in {REQUIRED_CURRENCY} found among "
            f"{[a.get('currency') for a in accounts]}."
        )
    if len(matches) > 1:
        listing = ", ".join(f"{a.get('id')} ({a.get('name')})" for a in matches)
        raise MetaError(
            f"Multiple {REQUIRED_CURRENCY} accounts found: {listing}. "
            f"Set META_AD_ACCOUNT_ID explicitly to the right one."
        )
    return matches[0]


# --------------------------------------------------------------------------
# Audience research — read-only, safe.
# --------------------------------------------------------------------------
async def search_interests(client: httpx.AsyncClient, query: str, limit: int = 20) -> list[dict]:
    """Search Meta's targeting interests (e.g. an artist or genre name).

    Returns dicts with id, name, audience_size_lower_bound/upper_bound, path.
    """
    params = {
        "type": "adinterest",
        "q": query,
        "limit": limit,
        "fields": "id,name,audience_size_lower_bound,audience_size_upper_bound,path,topic",
    }
    data = await _get(client, "search", params)
    return data.get("data", [])

def build_targeting(
    interest_ids: list[str] | None = None,
    countries: list[str] | None = None,
    age_min: int = 18,
    age_max: int = 65,
    custom_audiences: list[str] | None = None,
    excluded_custom_audiences: list[str] | None = None,
) -> dict:
    """Build a Meta targeting spec. Defaults to Canada (the CAD account's market).

    Centralizes the Graph API targeting shape here so callers (bot.py) don't have
    to know it. `interest_ids` come from search_interests().
    """
    spec: dict = {
        "geo_locations": {"countries": countries or ["CA"]},
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
) -> dict:
    """Aggregate Meta targeting interests for an artist plus optional genre, similar
    artists, and label. Read-only — searches interests and de-dupes them.

    Returns {"all_ids": [interest_id, ...], "summary": human-readable text}.
    `all_ids` feeds straight into a /draft command; `summary` is shown to the user.
    """
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
            results = await search_interests(client, q, limit=15)
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
    client: httpx.AsyncClient, targeting: dict, optimization_goal: str = "REACH"
) -> dict:
    """Estimate the daily reach of a targeting spec on the CAD account.

    `targeting` is a Meta targeting spec dict (geo, age, interests, etc.).
    """
    _require_account()
    import json

    params = {
        "targeting_spec": json.dumps(targeting),
        "optimization_goal": optimization_goal,
    }
    data = await _get(client, f"{AD_ACCOUNT_ID}/reachestimate", params)
    return data.get("data", data)


# --------------------------------------------------------------------------
# Campaign building — ALWAYS created PAUSED. None of these start spend.
# --------------------------------------------------------------------------
def _require_account() -> None:
    if not AD_ACCOUNT_ID:
        raise MetaError(
            "META_AD_ACCOUNT_ID is not set. Run scripts/find_cad_account.py to "
            "discover the CAD account, then pin it in .env."
        )


def _require_page() -> None:
    if not PAGE_ID:
        raise MetaError(
            "META_PAGE_ID is not set. Add your Facebook Page ID to .env as META_PAGE_ID. "
            "Find it at facebook.com/your-page → About → Page transparency."
        )


async def create_campaign(
    client: httpx.AsyncClient,
    name: str,
    objective: str = "OUTCOME_TRAFFIC",
    special_ad_categories: list[str] | None = None,
) -> dict:
    """Create a PAUSED campaign. Returns {'id': ...}.

    `special_ad_categories` defaults to [] (none). Meta requires the param to be
    present even when empty.
    """
    _require_account()
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
    return await _post(client, f"{AD_ACCOUNT_ID}/campaigns", data)


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
) -> dict:
    """Create a PAUSED ad set. `daily_budget_cents` is in the account currency's
    minor units (CAD cents)."""
    _require_account()
    import json

    data = {
        "name": name,
        "campaign_id": campaign_id,
        "daily_budget": int(daily_budget_cents),
        "billing_event": billing_event,
        "optimization_goal": optimization_goal,
        "bid_strategy": bid_strategy,
        "targeting": json.dumps(targeting),
        "status": "PAUSED",  # never ACTIVE here
    }
    if promoted_object:
        data["promoted_object"] = json.dumps(promoted_object)
    return await _post(client, f"{AD_ACCOUNT_ID}/adsets", data)


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


async def upload_ad_image(client: httpx.AsyncClient, file_path: str) -> str:
    """Upload a local image file to Meta's ad image library.

    Returns the image hash string, which can be used in create_adcreative()
    as `image_hash`. Meta deduplicates by content, so uploading the same file
    twice returns the same hash.
    """
    _require_account()
    import base64
    import pathlib

    path = pathlib.Path(file_path)
    raw = path.read_bytes()
    encoded = base64.b64encode(raw).decode()
    data = {
        "bytes": encoded,
        "name": path.name,
        "access_token": ACCESS_TOKEN,
    }
    resp = await client.post(f"{_base()}/{AD_ACCOUNT_ID}/adimages", data=data)
    body = _handle(resp)
    images = body.get("images", {})
    if not images:
        raise MetaError(f"Meta returned no image data after upload: {body}")
    info = next(iter(images.values()))
    h = info.get("hash")
    if not h:
        raise MetaError(f"Meta image upload succeeded but returned no hash: {info}")
    log.info("Uploaded %s → hash %s", path.name, h)
    return h


async def create_adcreative(
    client: httpx.AsyncClient,
    name: str,
    link: str,
    caption: str,
    image_hash: str | None = None,
    image_url: str | None = None,
    call_to_action_type: str = "GET_EVENT_TICKETS",
    url_tags: str | None = None,
) -> dict:
    """Create an ad creative for a ticket-link ad. Returns {'id': ...}.

    `link` is the direct ticket URL (Showpass, Eventbrite, etc.).
    `caption` is the ad body text shown above the link preview.
    `image_hash` takes priority — use upload_ad_image() to get one from a local file.
    `image_url` is a fallback for remote images.
    If neither is provided, Meta pulls the OG preview from the link.
    Requires META_PAGE_ID to be set.
    """
    _require_account()
    _require_page()
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
        "page_id": PAGE_ID,
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
    return await _post(client, f"{AD_ACCOUNT_ID}/adcreatives", data)


async def create_ad(
    client: httpx.AsyncClient,
    adset_id: str,
    name: str,
    creative_id: str,
) -> dict:
    """Create a PAUSED ad from an existing creative id."""
    _require_account()
    import json

    data = {
        "name": name,
        "adset_id": adset_id,
        "creative": json.dumps({"creative_id": creative_id}),
        "status": "PAUSED",  # never ACTIVE here
    }
    return await _post(client, f"{AD_ACCOUNT_ID}/ads", data)


# --------------------------------------------------------------------------
# THE SPEND GATE. This is the only function that can start spending money.
# bot.py must only call it after an explicit per-campaign approval.
# --------------------------------------------------------------------------
async def activate_campaign(client: httpx.AsyncClient, campaign_id: str) -> dict:
    """Set a campaign ACTIVE — this STARTS SPEND. Caller is responsible for having
    obtained Greg's explicit, per-campaign go-ahead before calling."""
    log.warning("ACTIVATING campaign %s — spend will begin", campaign_id)
    return await _post(client, campaign_id, {"status": "ACTIVE"})


async def pause_campaign(client: httpx.AsyncClient, campaign_id: str) -> dict:
    """Set a campaign PAUSED — stops spend. Always safe to call."""
    return await _post(client, campaign_id, {"status": "PAUSED"})


# --------------------------------------------------------------------------
# Reporting / optimization data — read-only.
# --------------------------------------------------------------------------
async def get_insights(
    client: httpx.AsyncClient,
    object_id: str,
    date_preset: str = "last_7d",
    fields: str = "campaign_name,impressions,reach,clicks,ctr,cpc,spend,actions",
) -> list[dict]:
    """Pull insights for any object (campaign/adset/ad/account id)."""
    params = {"date_preset": date_preset, "fields": fields, "limit": 200}
    data = await _get(client, f"{object_id}/insights", params)
    return data.get("data", [])


# --------------------------------------------------------------------------
# Sales campaign framework (added 2026-06-09) — auto-objective + 3-ad-set build.
# --------------------------------------------------------------------------
PIXEL_ID = os.environ.get("META_PIXEL_ID", "")

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

# Ticketing domains where our pixel CAN fire (so optimize for purchases) vs not.
_PIXEL_TRACKABLE = ("ticketweb", "showpass", "eventbrite", "dice.fm", "seetickets")
_NO_PIXEL = ("ticketmaster", "livenation", "axs.com")


def plan_objective(ticket_link: str | None) -> dict:
    """Pick objective + optimization for a ticket link's platform.
    Pixel-trackable (TicketWeb/Showpass/...) -> OUTCOME_SALES optimized for PURCHASE.
    Otherwise (Ticketmaster/unknown) -> OUTCOME_TRAFFIC optimized for landing-page views."""
    link = (ticket_link or "").lower()
    if PIXEL_ID and any(d in link for d in _PIXEL_TRACKABLE):
        return {
            "objective": "OUTCOME_SALES",
            "optimization_goal": "OFFSITE_CONVERSIONS",
            "promoted_object": {"pixel_id": PIXEL_ID, "custom_event_type": "PURCHASE"},
            "platform": "pixel-trackable → purchase-optimized",
        }
    return {
        "objective": "OUTCOME_TRAFFIC",
        "optimization_goal": "LANDING_PAGE_VIEWS",
        "promoted_object": None,
        "platform": "no pixel → landing-page-view optimized",
    }


async def find_audiences(client: httpx.AsyncClient, names: list[str]) -> dict:
    """Return {name: id} for existing custom audiences matching the given names."""
    data = await _get(client, f"{AD_ACCOUNT_ID}/customaudiences", {"fields": "name", "limit": 500})
    by = {a.get("name"): a.get("id") for a in data.get("data", [])}
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
    countries: list[str] | None = None,
) -> dict:
    """Build a PAUSED, sales-structured campaign for one show:
      - objective/optimization auto-picked from the ticket link platform
      - up to 3 ad sets: Retargeting (warm), Lookalike, Cold (interests)
      - one creative, cloned into an ad per ad set
    Budget split 25/35/40 (retarget/lookalike/cold), min $1/day each. All PAUSED."""
    plan = plan_objective(ticket_link)
    camp = await create_campaign(client, name, objective=plan["objective"])
    campaign_id = camp["id"]

    auds = await find_audiences(client, RETARGET_AUDIENCES + LOOKALIKE_AUDIENCES)
    retarget_ids = [auds[n] for n in RETARGET_AUDIENCES if n in auds]
    lal_ids = [auds[n] for n in LOOKALIKE_AUDIENCES if n in auds]

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

    creative = await create_adcreative(client, f"{name} — creative", ticket_link, caption,
                                       image_hash=image_hash, image_url=image_url)
    creative_id = creative["id"]

    adsets = []
    for layer in ("Retargeting", "Lookalike", "Cold"):
        tgt = targetings[layer]
        if not tgt:
            continue
        aset = await create_adset(client, campaign_id, f"{name} — {layer}", splits[layer], tgt,
                                  optimization_goal=plan["optimization_goal"],
                                  promoted_object=plan["promoted_object"])
        await create_ad(client, aset["id"], f"{name} — {layer} ad", creative_id)
        adsets.append({"layer": layer, "adset_id": aset.get("id"), "daily_cents": splits[layer]})

    return {"campaign_id": campaign_id, "objective": plan["objective"],
            "optimization": plan["optimization_goal"], "platform": plan["platform"],
            "adsets": adsets, "creative_id": creative_id}


async def activate_full(client: httpx.AsyncClient, campaign_id: str) -> dict:
    """Activate a campaign AND all its ad sets + ads (STARTS SPEND). A campaign alone
    going ACTIVE won't deliver if its children are PAUSED, so flip every level."""
    log.warning("ACTIVATING campaign %s (full: adsets+ads) — spend will begin", campaign_id)
    adsets = await _get(client, f"{campaign_id}/adsets", {"fields": "id", "limit": 50})
    for a in adsets.get("data", []):
        await _post(client, a["id"], {"status": "ACTIVE"})
        ads = await _get(client, f"{a['id']}/ads", {"fields": "id", "limit": 50})
        for ad in ads.get("data", []):
            await _post(client, ad["id"], {"status": "ACTIVE"})
    return await _post(client, campaign_id, {"status": "ACTIVE"})
