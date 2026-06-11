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
    now = _dt.datetime.now(_dt.UTC).replace(microsecond=0, tzinfo=None)
    if upcoming:
        params["starts_on__gte"] = now.isoformat()
    if days:
        params["starts_on__lte"] = (now + _dt.timedelta(days=days)).isoformat()
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
    except ShowpassError as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
