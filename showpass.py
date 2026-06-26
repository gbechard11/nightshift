"""Showpass client for Pedro + NS Team Bot.

Showpass (showpass.com) is the ticketing platform for Nightshift / Pawn Shop
events. Both brands live under ONE Showpass organization: Nightshift
Entertainment, org/venue ID 41.

Two API surfaces (docs: dev.showpass.com, mirrored at /data/greg/showpass-docs/):

1. PUBLIC Discovery API — read-only, NO auth needed for server-side calls.
   - List events:  GET /api/public/discovery/?venue=41&...
   - Event detail: GET /api/public/events/{slug}/
   Works today; used for "what's on sale", inventory/sold-out checks, links.

2. PRIVATE Organizer API — `Authorization: Token <SHOWPASS_API_TOKEN>`.
   Token is issued by Showpass support (requested 2026-06-11; set it in .env
   as SHOWPASS_API_TOKEN once it arrives). Endpoints wired here:
   - Discounts:      /api/venue/{org}/financials/discounts/        (full CRUD)
   - Tracking links: /api/venue/{org}/analytics/tracking/links/    (full CRUD)
   There is still NO write API for creating events / changing allotments —
   that remains dashboard-only (browser automation plan, currently paused).

Design mirrors prism.py / meta_ads.py: env config up top, a `configured()`
gate so the private path stays dormant until the token is set, a custom error
type, and sync httpx (callers in async code use asyncio.to_thread).

SAFETY: public reads are safe — just do them. Private WRITES (create/update/
delete discounts and tracking links) are confirm-first: the CLI refuses them
without --confirm, and bot flows must show Greg the exact change and wait for
his explicit yes before passing --confirm.
"""
import argparse
import datetime as _dt
import json
import os
import sys

import httpx

BASE = "https://www.showpass.com"
ORG_ID = int(os.environ.get("SHOWPASS_ORG_ID", "41"))
API_TOKEN = os.environ.get("SHOWPASS_API_TOKEN", "")

# Tracking link types (`via` field)
VIA = {"employee": 1, "venue": 2, "affiliate": 3, "quick": 4}


class ShowpassError(Exception):
    pass


def configured() -> bool:
    """True once the private Organizer API token is set. Public reads never need it."""
    return bool(API_TOKEN)


def _handle(resp: httpx.Response):
    if resp.status_code == 401 or resp.status_code == 403:
        raise ShowpassError(
            "Showpass auth failed (%d) — check SHOWPASS_API_TOKEN in .env."
            % resp.status_code
        )
    if resp.status_code == 404:
        raise ShowpassError("Not found: %s" % resp.request.url)
    if resp.status_code >= 400:
        raise ShowpassError("Showpass %d: %s" % (resp.status_code, resp.text[:300]))
    return resp.json()


def _public_get(path: str, params: dict | None = None):
    with httpx.Client(timeout=30.0) as c:
        return _handle(c.get(BASE + path, params=params or {}))


def _private(method: str, path: str, payload: dict | None = None):
    if not configured():
        raise ShowpassError(
            "Private Organizer API not available yet — SHOWPASS_API_TOKEN is not "
            "set (token requested from Showpass support 2026-06-11)."
        )
    headers = {"Authorization": "Token " + API_TOKEN}
    with httpx.Client(timeout=30.0, headers=headers) as c:
        resp = c.request(method, BASE + path, json=payload)
        if method == "DELETE" and resp.status_code in (200, 202, 204):
            return {"ok": True}
        return _handle(resp)


# --- Public Discovery API (works today, no token) ----------------------------

def list_events(query: str = "", days: int | None = None, page_size: int = 50,
                upcoming: bool = True, org_id: int | None = None) -> list[dict]:
    """Upcoming (default) public events for our org. `query` filters by
    name/venue/tags; `days` caps how far ahead to look."""
    params: dict = {
        "venue": org_id or ORG_ID,
        "page_size": page_size,
        "ordering": "starts_on",
    }
    now = _dt.datetime.now(_dt.UTC).replace(microsecond=0)
    if upcoming:
        params["starts_on__gte"] = now.isoformat().replace("+00:00", "Z")
    if days:
        params["starts_on__lte"] = (now + _dt.timedelta(days=days)).isoformat().replace("+00:00", "Z")
    if query:
        params["search_string"] = query
    data = _public_get("/api/public/discovery/", params)
    return data.get("results", [])


def get_event(slug: str) -> dict:
    """Full public detail for one event: ticket types, prices, inventory,
    sold_out flag. `slug` is the bit after showpass.com/ in the event URL."""
    return _public_get("/api/public/events/%s/" % slug.strip().strip("/"))


def format_events(events: list[dict], limit: int = 30) -> str:
    if not events:
        return "No matching Showpass events."
    lines = []
    for e in events[:limit]:
        when = (e.get("starts_on") or "")[:16].replace("T", " ")
        lines.append("%s  %s  -> showpass.com/%s/" % (when, e.get("name"), e.get("slug")))
    if len(events) > limit:
        lines.append("... and %d more" % (len(events) - limit))
    return "\n".join(lines)


def format_event_detail(e: dict) -> str:
    out = ["%s  (%s)" % (e.get("name"), "SOLD OUT" if e.get("sold_out") else "on sale")]
    out.append("When: %s -> %s" % (e.get("starts_on"), e.get("ends_on")))
    loc = e.get("location")
    if isinstance(loc, dict):
        loc = ", ".join(str(loc.get(k)) for k in ("name", "street_name", "city") if loc.get(k))
    if loc:
        out.append("Where: %s" % loc)
    out.append("Link: %s/%s/" % (BASE, e.get("slug")))
    for tt in e.get("ticket_types") or []:
        out.append("  ticket_type %s: %s — $%s, inventory_left=%s" % (
            tt.get("id"), tt.get("name"), tt.get("price"), tt.get("inventory_left")))
    return "\n".join(out)


# --- Private Organizer API: discounts ----------------------------------------

def list_discounts() -> list[dict]:
    data = _private("GET", "/api/venue/%d/financials/discounts/" % ORG_ID)
    return data.get("results", data) if isinstance(data, dict) else data


def create_discount(code: str = "", percentage: str = "", amount: str = "",
                    limit: int | None = None, per_user_limit: int | None = None,
                    ends_on: str = "", event_id: int | None = None,
                    description: str = "") -> dict:
    """Create a standard (type 1) customer-entered discount code. Use ONE of
    percentage ("25.00" = 25% off) or amount ("10.00" = $10 off). Optional
    event_id restricts the code to that event. WRITE — confirm-first."""
    if bool(percentage) == bool(amount):
        raise ShowpassError("Set exactly one of percentage or amount.")
    payload: dict = {"type": 1}
    if code:
        payload["code"] = code
    if description:
        payload["description"] = description
    if percentage:
        payload["percentage"] = percentage
    if amount:
        payload["amount"] = amount
    if limit is not None:
        payload["limit"] = limit
    if per_user_limit is not None:
        payload["per_user_limit"] = per_user_limit
    if ends_on:
        payload["ends_on"] = ends_on
    if event_id:
        payload["permission_type"] = "disc_level_ticket_type"
        payload["event_discount_permissions"] = [{"event": int(event_id)}]
    return _private("POST", "/api/venue/%d/financials/discounts/" % ORG_ID, payload)


def delete_discount(discount_id: int) -> dict:
    """Soft-deactivate a discount. WRITE — confirm-first."""
    return _private("DELETE", "/api/venue/%d/financials/discounts/%d/" % (ORG_ID, int(discount_id)))


# --- Private Organizer API: tracking links -----------------------------------

def list_links() -> list[dict]:
    data = _private("GET", "/api/venue/%d/analytics/tracking/links/" % ORG_ID)
    return data.get("results", data) if isinstance(data, dict) else data


def create_link(description: str, event_id: int | None = None,
                via: str = "affiliate") -> dict:
    """Create a tracking short-link (showpass.com/l/xxxx) that attributes
    views/sales. via: employee|venue|affiliate. WRITE — confirm-first."""
    if via not in VIA or via == "quick":
        raise ShowpassError("via must be one of: employee, venue, affiliate")
    payload: dict = {"venue": ORG_ID, "via": VIA[via], "description": description[:512]}
    if event_id:
        payload["event"] = int(event_id)
    return _private("POST", "/api/venue/%d/analytics/tracking/links/" % ORG_ID, payload)


def delete_link(link_id: int) -> dict:
    """Delete a tracking link. WRITE — confirm-first."""
    return _private("DELETE", "/api/venue/%d/analytics/tracking/links/%d/" % (ORG_ID, int(link_id)))


# --- Private Organizer API: events (UNDOCUMENTED, reverse-engineered) ----------
# Event create/read/delete live under the same token-authed venue namespace as
# discounts/links. Create is ASYNC: POST returns {"job_id": ...} and the event
# appears shortly after under its slugified name. We force DRAFT status so
# nothing goes live until an explicit publish. See reference_showpass_event_api.
import re as _re
import time as _time
from datetime import datetime as _datetime, timezone as _tzc
try:
    from zoneinfo import ZoneInfo as _ZoneInfo
except Exception:  # pragma: no cover
    _ZoneInfo = None

# region/province -> (IANA zone for UTC conversion, Showpass `timezone` string)
_TZ_BY_REGION = {
    "AB": ("America/Edmonton", "US/Mountain"), "ALBERTA": ("America/Edmonton", "US/Mountain"),
    "BC": ("America/Vancouver", "US/Pacific"), "BRITISH COLUMBIA": ("America/Vancouver", "US/Pacific"),
    "MB": ("America/Winnipeg", "US/Central"), "MANITOBA": ("America/Winnipeg", "US/Central"),
    "SK": ("America/Regina", "US/Central"), "SASKATCHEWAN": ("America/Regina", "US/Central"),
    "ON": ("America/Toronto", "US/Eastern"), "ONTARIO": ("America/Toronto", "US/Eastern"),
    "QC": ("America/Toronto", "US/Eastern"), "QUEBEC": ("America/Toronto", "US/Eastern"),
    "QUEBEC": ("America/Toronto", "US/Eastern"),
}
_DEFAULT_TZ = ("America/Edmonton", "US/Mountain")


def slugify(name: str) -> str:
    return _re.sub(r"-+", "-", _re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


def _tz_for_region(region: str):
    return _TZ_BY_REGION.get((region or "").strip().upper(), _DEFAULT_TZ)


def _to_utc_iso(local, tz_iana: str) -> str:
    """local: 'YYYY-MM-DD HH:MM' (or 'T') in tz_iana -> UTC ISO 'Z'."""
    if isinstance(local, str):
        s = local.strip().replace("T", " ")
        fmt = "%Y-%m-%d %H:%M" if ":" in s else "%Y-%m-%d %H"
        local = _datetime.strptime(s, fmt)
    if _ZoneInfo is None:
        raise ShowpassError("zoneinfo/tzdata unavailable on this host.")
    return local.replace(tzinfo=_ZoneInfo(tz_iana)).astimezone(_tzc.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_locations() -> list:
    data = _private("GET", "/api/venue/%d/events/locations/" % ORG_ID)
    return data.get("results", data) if isinstance(data, dict) else data


def resolve_location(location) -> dict:
    """Accept an int id or a (fuzzy) venue name; return the location dict."""
    locs = list_locations()
    if isinstance(location, int) or (isinstance(location, str) and location.isdigit()):
        lid = int(location)
        for L in locs:
            if L.get("id") == lid:
                return L
        raise ShowpassError("No Showpass location with id %s." % lid)
    q = str(location).strip().lower()
    exact = [L for L in locs if (L.get("name") or "").strip().lower() == q]
    part = [L for L in locs if q in (L.get("name") or "").strip().lower()]
    hit = exact or part
    if not hit:
        names = ", ".join(sorted((L.get("name") or "") for L in locs))[:600]
        raise ShowpassError("No Showpass location matching '%s'. Known: %s" % (location, names))
    return hit[0]


def _get_event_private(slug: str):
    """GET an org event (incl. drafts) by slug; None if 404/not found."""
    try:
        return _private("GET", "/api/venue/%d/events/%s/" % (ORG_ID, slug))
    except ShowpassError:
        return None


def _norm_tier(t: dict) -> dict:
    return {
        "name": str(t.get("name") or "GA"),
        "price": "%.2f" % float(t.get("price", 0) or 0),
        "inventory": int(t.get("inventory") or t.get("quantity") or 0),
        "visibility": 1,
    }


def create_event(name, location, starts_on, ends_on, tiers, *,
                 tz_iana: str = "", sp_tz: str = "", visibility: int = 1,
                 draft: bool = True, description: str = "") -> dict:
    """Create a Showpass event (ASYNC -> returns {job_id}). DRAFT by default.
    `location` is an id or venue name; tz derived from the location's region
    unless tz_iana/sp_tz given. `tiers` = [{name, price, inventory}]. WRITE."""
    loc = resolve_location(location)
    if not (tz_iana and sp_tz):
        tz_iana, sp_tz = _tz_for_region(loc.get("province") or loc.get("region") or "")
    if not tiers:
        raise ShowpassError("At least one ticket tier is required.")
    payload = {
        "name": name, "venue": ORG_ID, "location": loc["id"],
        "starts_on": _to_utc_iso(starts_on, tz_iana),
        "ends_on": _to_utc_iso(ends_on, tz_iana),
        "timezone": sp_tz, "visibility": int(visibility),
        "status": "sp_event_draft" if draft else "sp_event_active",
        "tickettype_set": [_norm_tier(t) for t in tiers],
    }
    if description:
        payload["description"] = description
    return _private("POST", "/api/venue/%d/events/" % ORG_ID, payload)


def _is_fresh(ev: dict) -> bool:
    try:
        c = (ev.get("created") or "").replace("Z", "+00:00")
        return (_datetime.now(_tzc.utc) - _datetime.fromisoformat(c)).total_seconds() < 600
    except Exception:
        return True


def create_event_and_wait(name, location, starts_on, ends_on, tiers, *,
                          poll_timeout: int = 30, **kw) -> dict:
    """create_event + poll until the event materializes; returns the event dict
    (slug, id, status, frontend_details_url). Handles slug-collision suffixes."""
    create_event(name, location, starts_on, ends_on, tiers, **kw)
    base_slug = slugify(name)
    cands = [base_slug] + ["%s-%d" % (base_slug, n) for n in range(2, 6)]
    deadline = _time.time() + poll_timeout
    while _time.time() < deadline:
        for slug in cands:
            ev = _get_event_private(slug)
            if ev and _is_fresh(ev):
                ev.setdefault("frontend_details_url", "%s/%s/" % (BASE, ev.get("slug") or slug))
                return ev
        _time.sleep(2)
    raise ShowpassError(
        "Event create job queued but it didn't appear within %ds - check the "
        "Showpass dashboard (slug ~ '%s')." % (poll_timeout, base_slug))


def publish_event(slug: str) -> dict:
    """Flip a draft event live (status -> active). Showpass only honors this via
    a FULL-object update (a sparse PATCH is a no-op), so we GET the event, flip
    status, and PUT it back. The PUT is async; we poll public visibility briefly.
    WRITE - confirm-first."""
    ev = _get_event_private(slug)
    if not ev:
        raise ShowpassError("No event '%s' to publish." % slug)
    if ev.get("status") == "sp_event_active":
        return {"ok": True, "slug": slug, "status": "sp_event_active", "already": True}
    ev["status"] = "sp_event_active"
    _private("PUT", "/api/venue/%d/events/%s/" % (ORG_ID, slug), ev)
    for _ in range(6):
        _time.sleep(2)
        cur = _get_event_private(slug)
        if cur and cur.get("status") == "sp_event_active":
            return {"ok": True, "slug": slug, "status": "sp_event_active",
                    "url": cur.get("frontend_details_url") or "%s/%s/" % (BASE, slug)}
    return {"ok": True, "slug": slug, "status": "submitted",
            "note": "publish job queued; may take a moment to go live"}


def delete_event(slug: str) -> dict:
    """Delete an event by slug. WRITE - confirm-first."""
    return _private("DELETE", "/api/venue/%d/events/%s/" % (ORG_ID, slug))


# --- CLI ----------------------------------------------------------------------

def _require_confirm(args):
    if not args.confirm:
        print("DRY RUN — this is a WRITE. Re-run with --confirm after Greg approves.",
              file=sys.stderr)
        sys.exit(2)


def main() -> None:
    p = argparse.ArgumentParser(description="Showpass CLI (org %d)" % ORG_ID)
    sub = p.add_subparsers(dest="cmd", required=True)

    ev = sub.add_parser("events", help="list upcoming public events (read-only)")
    ev.add_argument("--query", default="")
    ev.add_argument("--days", type=int, default=None)
    ev.add_argument("--json", action="store_true")

    ed = sub.add_parser("event", help="full detail for one event (read-only)")
    ed.add_argument("slug")
    ed.add_argument("--json", action="store_true")

    dl = sub.add_parser("discounts", help="list discounts (token required)")
    dl.add_argument("--json", action="store_true")

    dc = sub.add_parser("discount-create", help="create discount code (WRITE)")
    dc.add_argument("--code", default="", help="leave blank to auto-generate")
    dc.add_argument("--percent", default="", help='e.g. "25.00"')
    dc.add_argument("--amount", default="", help='e.g. "10.00"')
    dc.add_argument("--limit", type=int, default=None)
    dc.add_argument("--per-user-limit", type=int, default=None)
    dc.add_argument("--ends-on", default="", help="ISO datetime")
    dc.add_argument("--event-id", type=int, default=None)
    dc.add_argument("--description", default="")
    dc.add_argument("--confirm", action="store_true")

    dd = sub.add_parser("discount-delete", help="deactivate a discount (WRITE)")
    dd.add_argument("id", type=int)
    dd.add_argument("--confirm", action="store_true")

    ll = sub.add_parser("links", help="list tracking links (token required)")
    ll.add_argument("--json", action="store_true")

    lc = sub.add_parser("link-create", help="create tracking link (WRITE)")
    lc.add_argument("--description", required=True)
    lc.add_argument("--event-id", type=int, default=None)
    lc.add_argument("--via", default="affiliate", choices=["employee", "venue", "affiliate"])
    lc.add_argument("--confirm", action="store_true")

    ld = sub.add_parser("link-delete", help="delete tracking link (WRITE)")
    ld.add_argument("id", type=int)
    ld.add_argument("--confirm", action="store_true")

    lo = sub.add_parser("locations", help="list saved event locations (read)")
    lo.add_argument("--json", action="store_true")

    ec = sub.add_parser("event-create", help="create an event, DRAFT by default (WRITE)")
    ec.add_argument("--name", required=True)
    ec.add_argument("--location", required=True, help="venue name or location id")
    ec.add_argument("--starts", required=True, help="local 'YYYY-MM-DD HH:MM'")
    ec.add_argument("--ends", required=True, help="local 'YYYY-MM-DD HH:MM'")
    ec.add_argument("--tier", action="append", default=[], metavar="NAME:PRICE:QTY",
                    help="repeatable, e.g. --tier GA:25:200 --tier VIP:50:50")
    ec.add_argument("--description", default="")
    ec.add_argument("--live", action="store_true", help="publish now (default: draft)")
    ec.add_argument("--confirm", action="store_true")

    ep = sub.add_parser("event-publish", help="flip a draft event live (WRITE)")
    ep.add_argument("slug")
    ep.add_argument("--confirm", action="store_true")

    ex = sub.add_parser("event-delete", help="delete an event by slug (WRITE)")
    ex.add_argument("slug")
    ex.add_argument("--confirm", action="store_true")

    args = p.parse_args()
    try:
        if args.cmd == "events":
            evs = list_events(query=args.query, days=args.days)
            print(json.dumps(evs, indent=1) if args.json else format_events(evs))
        elif args.cmd == "event":
            e = get_event(args.slug)
            print(json.dumps(e, indent=1) if args.json else format_event_detail(e))
        elif args.cmd == "discounts":
            d = list_discounts()
            print(json.dumps(d, indent=1) if args.json else
                  "\n".join("%s  %s  %s%%/%s$ used=%s" % (
                      x.get("id"), x.get("code"), x.get("percentage"),
                      x.get("amount"), x.get("used", "?")) for x in d) or "No discounts.")
        elif args.cmd == "discount-create":
            _require_confirm(args)
            print(json.dumps(create_discount(
                code=args.code, percentage=args.percent, amount=args.amount,
                limit=args.limit, per_user_limit=args.per_user_limit,
                ends_on=args.ends_on, event_id=args.event_id,
                description=args.description), indent=1))
        elif args.cmd == "discount-delete":
            _require_confirm(args)
            print(json.dumps(delete_discount(args.id)))
        elif args.cmd == "links":
            ls = list_links()
            print(json.dumps(ls, indent=1) if args.json else
                  "\n".join("%s  %s  %s views=%s" % (
                      x.get("id"), x.get("short_url"), x.get("description"),
                      x.get("views")) for x in ls) or "No tracking links.")
        elif args.cmd == "link-create":
            _require_confirm(args)
            print(json.dumps(create_link(args.description, args.event_id, args.via), indent=1))
        elif args.cmd == "link-delete":
            _require_confirm(args)
            print(json.dumps(delete_link(args.id)))
        elif args.cmd == "locations":
            locs = list_locations()
            print(json.dumps(locs, indent=1) if args.json else
                  "\n".join("%s  %s - %s %s" % (L.get("id"), L.get("name"),
                       L.get("city", ""), L.get("province") or L.get("region") or "")
                       for L in locs) or "No locations.")
        elif args.cmd == "event-create":
            _require_confirm(args)
            tiers = []
            for spec in args.tier:
                parts = spec.split(":")
                if len(parts) != 3:
                    raise ShowpassError("--tier must be NAME:PRICE:QTY (got %r)" % spec)
                tiers.append({"name": parts[0], "price": parts[1], "inventory": parts[2]})
            if not tiers:
                raise ShowpassError("at least one --tier is required")
            ev = create_event_and_wait(
                args.name, args.location, args.starts, args.ends, tiers,
                draft=not args.live, description=args.description)
            print(json.dumps({"ok": True, "slug": ev.get("slug"), "id": ev.get("id"),
                              "status": ev.get("status"),
                              "url": ev.get("frontend_details_url"),
                              "starts_on": ev.get("starts_on")}, indent=1))
        elif args.cmd == "event-publish":
            _require_confirm(args)
            ev = publish_event(args.slug)
            print(json.dumps({"ok": True, "slug": args.slug,
                              "status": ev.get("status") if isinstance(ev, dict) else ev}))
        elif args.cmd == "event-delete":
            _require_confirm(args)
            print(json.dumps({"ok": True, "deleted": args.slug, "resp": delete_event(args.slug)}))
        elif args.cmd == "__never__":
            pass
    except ShowpassError as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
