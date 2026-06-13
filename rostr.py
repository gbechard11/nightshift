#!/usr/bin/env python3
"""ROSTR (rostr.cc) client — read-only, for offer-creation data.

AUTH: SELF-HEALING AUTO-LOGIN
=============================
ROSTR has no public API. The web app (www.rostr.cc) calls a private JSON API at
api.rostr.cc/v1/* authed by the user's .rostr.cc session cookie (an unauthed call
returns {"error":"UserNotLoggedInError"}). ROSTR uses Firebase email/password auth
but exposes its own login endpoint POST /v1/auth/rostr {email,password} that sets
the session cookie — callable directly from the VPS (it's Google/Cloudflare-clean).

So the PRIMARY auth path is auto-login: set ROSTR_EMAIL + ROSTR_PASSWORD in .env
once and the client logs itself in, caches the cookie in `rostr_cookies.json`
(gitignored), and silently re-logs-in + retries whenever the session expires.
No manual step ever. `status` / `autologin` drive it.

FALLBACK: a manual cookie seed (`login --curl` / Telegram `/rostrlogin`) still
works if you ever want to paste a browser session instead of storing a password.

SEARCH is the exception: it runs on a public Typesense index (entities-current)
with a client-side search key, so `search` works even without a seeded session.

PURPOSE: let Pedro + Nightshift staff (Seba et al.) pull offer-creation data —
given an artist: their booking AGENT / MANAGER (name + email + territory), tour
& venue history, audience/market metrics, and company rosters.

Read-only by design: only GET / Typesense search. Nothing writes to ROSTR.

API MAP (discovered live 2026-06-13)
  GET  /v1/artist/{slug}                         profile + audience metrics
  GET  /v1/artist/{slug}/team/{TYPE}             TYPE in MANAGEMENT|AGENCY|RECORD_LABEL|PUBLISHER
  GET  /v1/artist/{slug}/events                  tour / show history
  GET  /v1/company/{slug}                        company + staff (people)
  GET  /v1/auth/me                               session probe
  POST typesense .../multi_search                public search over entities-current
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx

API = os.environ.get("ROSTR_API", "https://api.rostr.cc")
WEB = "https://www.rostr.cc"
# Auto-login (self-healing): email+password posted to ROSTR's own login endpoint.
# Set these once in .env and the client re-logs-in by itself whenever the session
# expires — no manual /rostrlogin re-seed ever needed.
ROSTR_EMAIL = os.environ.get("ROSTR_EMAIL", "")
ROSTR_PASSWORD = os.environ.get("ROSTR_PASSWORD", "")
LOGIN_ENDPOINT = os.environ.get("ROSTR_LOGIN_ENDPOINT", "/v1/auth/rostr")
TS_URL = os.environ.get("ROSTR_TS_URL",
                        "https://8btzopr7xawl4qicp.a1.typesense.net/multi_search")
TS_KEY = os.environ.get("ROSTR_TS_KEY", "rRowrliJLt6X7dGmNViU7jaWp4MQOKPz")
TS_COLLECTION = os.environ.get("ROSTR_TS_COLLECTION", "entities-current")

COOKIE_FILE = Path(os.environ.get("ROSTR_COOKIE_FILE",
                                  Path(__file__).with_name("rostr_cookies.json")))
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

# Cookies that carry the logged-in identity (others are kept as-is).
SESSION_COOKIE_HINTS = ("session", "_rostr", "remember", "auth", "token",
                        "user", "_session", "cf_clearance")

TEAM_TYPES = ("MANAGEMENT", "AGENCY", "RECORD_LABEL", "PUBLISHER")
OFFER_TEAM_TYPES = ("AGENCY", "MANAGEMENT")  # who you actually send an offer to


class RostrError(Exception):
    pass


# --- cookie store -------------------------------------------------------------

def _load() -> dict:
    if not COOKIE_FILE.is_file():
        return {}
    try:
        return json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(blob: dict) -> None:
    COOKIE_FILE.write_text(json.dumps(blob, indent=1), encoding="utf-8")
    try:
        os.chmod(COOKIE_FILE, 0o600)
    except OSError:
        pass


def configured() -> bool:
    return bool(_load().get("cookies"))


def _cookies() -> dict:
    return _load().get("cookies", {})


def _ua() -> str:
    return _load().get("user_agent") or DEFAULT_UA


# --- cookie parsing (mirrors envato.py) ---------------------------------------

def parse_cookie_header(header: str) -> dict:
    out = {}
    for part in header.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def parse_curl(text: str) -> tuple[dict, str]:
    """Parse a 'Copy as cURL' command -> (cookies, user_agent)."""
    cookies, ua = {}, ""
    m = re.search(r"(?:-b|--cookie)\s+([\"'])(.*?)\1", text, re.S)
    if m:
        cookies.update(parse_cookie_header(m.group(2)))
    for hm in re.finditer(r"(?:-H|--header)\s+([\"'])(.*?)\1", text, re.S):
        h = hm.group(2)
        if ":" not in h:
            continue
        name, _, val = h.partition(":")
        name, val = name.strip().lower(), val.strip()
        if name == "cookie":
            cookies.update(parse_cookie_header(val))
        elif name == "user-agent":
            ua = val
    return cookies, ua


def parse_cookies_txt(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        f = line.split("\t")
        if len(f) >= 7 and "rostr" in f[0].lower():
            out[f[5]] = f[6]
    return out


def parse_cookies_json(text: str) -> dict:
    data = json.loads(text)
    if isinstance(data, dict) and "cookies" in data:
        data = data["cookies"]
    out = {}
    for c in data:
        if isinstance(c, dict) and c.get("name"):
            dom = c.get("domain", "")
            if not dom or "rostr" in dom:
                out[c["name"]] = c.get("value", "")
    return out


def detect_and_parse(text: str) -> tuple[dict, str]:
    """Auto-detect a pasted blob: curl / cookies.txt / JSON / bare header."""
    t = text.strip()
    low = t[:300].lower()
    if "curl " in low or "-H " in t or "--header" in t or "--cookie" in t:
        c, ua = parse_curl(t)
        if c:
            return c, ua
    if "\t" in t and "rostr" in t.lower():
        c = parse_cookies_txt(t)
        if c:
            return c, ""
    if t[:1] in "[{":
        try:
            c = parse_cookies_json(t)
            if c:
                return c, ""
        except Exception:
            pass
    return parse_cookie_header(t), ""


def _read_arg_or_file(val: str) -> str:
    if val == "-":
        return sys.stdin.read()
    p = Path(val)
    if len(val) < 4096 and p.is_file():
        return p.read_text(encoding="utf-8")
    return val


def seed_cookies(cookies: dict, user_agent: str = "") -> dict:
    cookies = {k: v for k, v in cookies.items() if v}
    if not cookies:
        raise RostrError("No cookies parsed — check the export format.")
    has_session = any(any(h in k.lower() for h in SESSION_COOKIE_HINTS)
                      for k in cookies)
    if not has_session:
        raise RostrError(
            "None of the cookies look like a ROSTR session. Export them while "
            "LOGGED IN to www.rostr.cc (Copy as cURL on the document request).")
    _save({"cookies": cookies, "user_agent": user_agent or DEFAULT_UA,
           "saved_at": int(time.time())})
    return {"count": len(cookies), "has_session": has_session,
            "names": sorted(cookies)[:30]}


# --- auto-login (email + password) --------------------------------------------

def _have_creds() -> bool:
    return bool(ROSTR_EMAIL and ROSTR_PASSWORD)


def _safe_msg(r: httpx.Response):
    try:
        j = r.json()
        return j.get("msg") or j.get("error")
    except Exception:
        return None


def login_password(email: str = "", password: str = "") -> dict:
    """Log in with email+password via ROSTR's own endpoint and persist the session
    cookies. Credentials default to ROSTR_EMAIL/ROSTR_PASSWORD (.env). This is what
    makes the client self-healing — no manual cookie paste needed."""
    email = email or ROSTR_EMAIL
    password = password or ROSTR_PASSWORD
    if not (email and password):
        raise RostrError("No ROSTR credentials — set ROSTR_EMAIL and ROSTR_PASSWORD in .env.")
    headers = {"User-Agent": DEFAULT_UA, "Content-Type": "application/json",
               "Accept": "application/json", "Origin": WEB, "Referer": WEB + "/"}
    with httpx.Client(timeout=30, headers=headers, follow_redirects=True) as c:
        r = c.post(API + LOGIN_ENDPOINT, json={"email": email, "password": password})
        if r.status_code in (401, 403):
            raise RostrError("ROSTR login failed: %s"
                             % (_safe_msg(r) or "invalid email or password"))
        r.raise_for_status()
        jar = dict(c.cookies) or dict(r.cookies)
    if not jar:
        raise RostrError("Login succeeded but set no session cookie "
                         "(endpoint may have changed).")
    _save({"cookies": jar, "user_agent": DEFAULT_UA,
           "saved_at": int(time.time()), "via": "password", "account": email})
    return {"count": len(jar), "names": sorted(jar)[:20], "account": email}


# --- http ---------------------------------------------------------------------

def _client(timeout: float = 60.0) -> httpx.Client:
    if not configured():
        if _have_creds():
            login_password()          # self-heal: log in on first use
        else:
            raise RostrError(
                "No ROSTR session. Set ROSTR_EMAIL/ROSTR_PASSWORD in .env for "
                "auto-login, or seed once via rostr.py login (/rostrlogin).")
    headers = {
        "User-Agent": _ua(),
        "Accept": "application/json, text/plain, */*",
        "Origin": WEB,
        "Referer": WEB + "/",
    }
    return httpx.Client(timeout=timeout, headers=headers, cookies=_cookies(),
                        follow_redirects=True)


def _api(path: str, _retry: bool = True) -> dict | list:
    """GET an api.rostr.cc/v1 path and return parsed JSON. On auth loss, auto
    re-logs-in (if creds are set) and retries once — so the session self-heals."""
    url = urljoin(API + "/", path.lstrip("/"))
    with _client() as c:
        r = c.get(url)
    if r.status_code in (401, 403) or '"UserNotLoggedInError"' in r.text:
        if _retry and _have_creds():
            login_password()
            return _api(path, _retry=False)
        raise RostrError(
            "ROSTR session expired (HTTP %s). %s" % (r.status_code,
            "Set ROSTR_EMAIL/ROSTR_PASSWORD in .env for auto-login, or re-seed "
            "via /rostrlogin." if not _have_creds() else "Auto-login also failed — "
            "check the credentials in .env."))
    if r.status_code == 404:
        raise RostrError("Not found: %s (check the slug)." % path)
    r.raise_for_status()
    return r.json()


# --- search (public Typesense) ------------------------------------------------

_TYPE_MAP = {"artist": "ARTIST", "company": "COMPANY", "person": "PERSON",
             "agent": "PERSON", "manager": "PERSON"}


def search(query: str, kind: str = "", limit: int = 10) -> list[dict]:
    types = [_TYPE_MAP[kind.lower()]] if kind.lower() in _TYPE_MAP else list(
        ("COMPANY", "ARTIST", "PERSON"))
    filt = "type: [%s] && published: [true]" % ",".join("'%s'" % t for t in types)
    body = {"searches": [{"collection": TS_COLLECTION, "q": query,
                          "query_by": "name", "filter_by": filt,
                          "per_page": max(1, min(limit, 30))}]}
    with httpx.Client(timeout=30) as c:
        r = c.post(TS_URL, params={"x-typesense-api-key": TS_KEY}, json=body,
                   headers={"Content-Type": "application/json"})
    r.raise_for_status()
    res = (r.json().get("results") or [{}])[0]
    out = []
    for h in res.get("hits", []):
        d = h.get("document", {})
        out.append({"name": d.get("name"), "type": d.get("type"),
                    "slug": d.get("rostr_id"), "subtitle": d.get("subtitle"),
                    "roles": d.get("roles_array"),
                    "spotify": d.get("sp_metric")})
    return out


def resolve_slug(name_or_slug: str, kind: str = "artist") -> str:
    """Return a ROSTR slug for an artist/company name. If it already looks like a
    slug (single token, resolves), use it; else Typesense-search by name."""
    s = name_or_slug.strip()
    hits = search(s, kind=kind, limit=5)
    want = _TYPE_MAP.get(kind.lower())
    for h in hits:
        if not want or h.get("type") == want:
            if h.get("slug"):
                return h["slug"]
    # fall back to a naive slug (lowercase alnum), which is ROSTR's convention
    naive = re.sub(r"[^a-z0-9]", "", s.lower())
    if naive:
        return naive
    raise RostrError("No %s found on ROSTR matching %r." % (kind, name_or_slug))


# --- business verbs -----------------------------------------------------------

def artist(slug: str) -> dict:
    """Artist profile trimmed to offer-relevant fields."""
    a = _api("/v1/artist/%s" % slug)
    return {
        "name": a.get("name"), "slug": a.get("rostrId"),
        "type": a.get("artistType"), "gender": a.get("gender"),
        "age": a.get("age"), "genres": a.get("genres"),
        "origin": " ".join(filter(None, [a.get("aiOriginCity"),
                  a.get("aiOriginState"), a.get("aiOriginCountry")])) or a.get("location"),
        "on_tour": a.get("bitOnTour"),
        "audience": {"spotify_listeners": a.get("spMetric"),
                     "instagram": a.get("igMetric"), "youtube": a.get("ytMetric"),
                     "tiktok": a.get("ttMetric"), "facebook": a.get("fbMetric"),
                     "bandsintown_trackers": a.get("bitMetric")},
        "socials": {"spotify": a.get("spUrl"), "instagram": a.get("igUrl"),
                    "tiktok": a.get("aiTiktokUrl"), "website": a.get("aiOfficialWebsiteUrl")},
        "bio": a.get("aiAboutSection"),
        "profile": "%s/artist/%s" % (WEB, a.get("rostrId")),
    }


def _contacts_from_team(block: dict) -> list[dict]:
    """Pull the artist's specific people (with emails/territories) from a team block."""
    out = []
    for grp in block.get("team", []):
        for p in grp.get("people", []):
            out.append({"name": p.get("name"), "role": p.get("role"),
                        "email": p.get("email"),
                        "company": p.get("companyName"),
                        "territories": p.get("territories") or grp.get("territories"),
                        "genres": p.get("genres"),
                        "profile": "%s/person/%s" % (WEB, p.get("rostrId")) if p.get("rostrId") else None})
    return out


def team(slug: str, types=OFFER_TEAM_TYPES) -> dict:
    """Booking agent + manager (and optionally label/publisher) for an artist.
    Returns per-type: company contact + the artist's specific people w/ emails."""
    result = {}
    for t in types:
        try:
            data = _api("/v1/artist/%s/team/%s" % (slug, t))
        except RostrError:
            continue
        blocks = data if isinstance(data, list) else [data]
        entries = []
        for b in blocks:
            co = b.get("company", {}) or {}
            entries.append({
                "company": co.get("name"),
                "company_website": co.get("websiteUrl"),
                "company_domain": co.get("radarDomain"),
                "company_locations": co.get("hqLocations"),
                "contacts": _contacts_from_team(b),
            })
        if entries:
            result[t] = entries
    return result


def tours(slug: str, limit: int = 40) -> dict:
    """Tour / show history (date, venue, city, country)."""
    data = _api("/v1/artist/%s/events" % slug)
    evs = data.get("events", []) if isinstance(data, dict) else (data or [])
    shows = []
    for e in evs[:limit]:
        loc = e.get("location", {}) or {}
        shows.append({"date": (e.get("date") or "")[:10],
                      "venue": loc.get("name"), "city": loc.get("location") or loc.get("city"),
                      "country": loc.get("country"),
                      "tickets_available": e.get("ticketsAvailable")})
    return {"count": len(evs), "shows": shows}


def company(slug: str, staff_limit: int = 40) -> dict:
    """Company profile + staff (agents/managers who work there)."""
    c = _api("/v1/company/%s" % slug)
    people = c.get("people", []) or []
    return {
        "name": c.get("name"), "slug": c.get("rostrId"),
        "role": c.get("role"), "website": c.get("websiteUrl"),
        "domain": c.get("radarDomain"), "hq": c.get("hqLocations"),
        "other_locations": c.get("otherLocations"),
        "founded": c.get("aiYearFounded"), "genres": c.get("genres"),
        "staff_count": len(people),
        "staff": [{"name": p.get("name"), "role": p.get("role"),
                   "slug": p.get("rostrId")} for p in people[:staff_limit]],
        "profile": "%s/company/%s" % (WEB, c.get("rostrId")),
    }


def brief(name: str) -> dict:
    """One-shot offer brief: resolve artist, then bundle profile + agent/manager
    contacts (with emails) + recent tour history. The thing staff want for an offer."""
    slug = resolve_slug(name, kind="artist")
    prof = artist(slug)
    tm = team(slug)
    tr = tours(slug, limit=12)
    # flatten the people you'd actually email
    reachout = []
    for t in ("AGENCY", "MANAGEMENT"):
        for entry in tm.get(t, []):
            for p in entry["contacts"]:
                reachout.append({"role": p["role"] or t, "name": p["name"],
                                 "email": p["email"], "company": p["company"],
                                 "territories": p["territories"]})
    return {"artist": prof, "reach_out": reachout, "team": tm,
            "recent_shows": tr, "note": "Offer goes to AGENCY (booking) for the "
            "relevant territory; MANAGEMENT is the manager. Verify emails before sending."}


# --- status -------------------------------------------------------------------

def status(auto: bool = True) -> dict:
    blob = _load()
    # self-heal: if there's no session but creds exist, log in
    if not blob.get("cookies") and auto and _have_creds():
        try:
            login_password()
            blob = _load()
        except RostrError:
            pass
    if not blob.get("cookies"):
        return {"configured": False, "creds_available": _have_creds(),
                "reason": "no session and no .env credentials"}
    age_days = round((time.time() - blob.get("saved_at", 0)) / 86400, 1)
    valid, who, detail = None, blob.get("account"), ""
    try:
        with _client(30) as c:
            r = c.get(API + "/v1/auth/me")
        valid = r.status_code == 200 and "UserNotLoggedInError" not in r.text
        if not valid and auto and _have_creds():     # expired -> re-login + re-probe
            login_password()
            with _client(30) as c:
                r = c.get(API + "/v1/auth/me")
            valid = r.status_code == 200 and "UserNotLoggedInError" not in r.text
        if valid:
            j = r.json()
            who = " ".join(filter(None, [j.get("firstName"), j.get("lastName")])) or j.get("email")
        detail = "HTTP %s" % r.status_code
    except Exception as e:  # noqa: BLE001
        valid, detail = None, str(e)
    return {"configured": True, "valid": bool(valid), "account": who,
            "auto_login": _have_creds(), "via": blob.get("via", "seed"),
            "age_days": age_days, "cookie_count": len(blob["cookies"]),
            "probe": detail}


def main() -> None:
    p = argparse.ArgumentParser(description="ROSTR client (read-only, offer data)")
    sub = p.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser("status", help="is the seeded session valid?")
    st.add_argument("--json", action="store_true")

    al = sub.add_parser("autologin", help="log in with ROSTR_EMAIL/ROSTR_PASSWORD (.env) and cache the session")
    al.add_argument("--json", action="store_true")

    lg = sub.add_parser("login", help="seed/refresh session from exported cookies (manual fallback)")
    lg.add_argument("--auto", default="", help="auto-detect a pasted blob; path or '-' for stdin")
    lg.add_argument("--curl", default="", help="a 'Copy as cURL' command; path or '-' for stdin")
    lg.add_argument("--cookie-header", default="", help='"name=val; name2=val2"')
    lg.add_argument("--user-agent", default="")

    se = sub.add_parser("search", help="search artists/companies/people (public)")
    se.add_argument("query")
    se.add_argument("--type", default="", help="artist|company|person")
    se.add_argument("--limit", type=int, default=10)

    ar = sub.add_parser("artist", help="artist profile + audience metrics")
    ar.add_argument("slug", help="ROSTR slug or name")

    tm = sub.add_parser("team", help="booking agent + manager contacts for an artist")
    tm.add_argument("slug", help="ROSTR slug or name")
    tm.add_argument("--all", action="store_true", help="include label + publisher")

    tr = sub.add_parser("tours", help="tour / show history for an artist")
    tr.add_argument("slug", help="ROSTR slug or name")
    tr.add_argument("--limit", type=int, default=40)

    co = sub.add_parser("company", help="company profile + staff")
    co.add_argument("slug", help="ROSTR slug or name")

    br = sub.add_parser("brief", help="one-shot offer brief (profile+agent+tours)")
    br.add_argument("name", help="artist name or slug")

    args = p.parse_args()

    def _slug(v, kind="artist"):
        # if it's plainly a slug (one lowercase token) try it as-is first
        return v if re.fullmatch(r"[a-z0-9]+", v or "") else resolve_slug(v, kind)

    try:
        if args.cmd == "status":
            s = status()
            print(json.dumps(s, indent=1) if args.json else (
                "NOT CONFIGURED — seed via /rostrlogin" if not s.get("configured")
                else "%s  account=%s  (cookies %sd old, %s)" % (
                    "VALID" if s.get("valid") else "INVALID/EXPIRED",
                    s.get("account") or "?", s.get("age_days"), s.get("probe"))))
        elif args.cmd == "autologin":
            info = login_password()
            print(json.dumps({"ok": True, **info}, indent=1) if args.json
                  else "Logged in as %s — session cached (%d cookies)."
                  % (info["account"], info["count"]))
        elif args.cmd == "login":
            ua = args.user_agent
            if args.auto:
                cookies, det = detect_and_parse(_read_arg_or_file(args.auto)); ua = ua or det
            elif args.curl:
                cookies, cu = parse_curl(_read_arg_or_file(args.curl)); ua = ua or cu
            elif args.cookie_header:
                cookies = parse_cookie_header(args.cookie_header)
            else:
                raise RostrError("Give --auto, --curl, or --cookie-header.")
            print(json.dumps({"ok": True, **seed_cookies(cookies, ua)}, indent=1))
            print("Now verify:  rostr.py status", file=sys.stderr)
        elif args.cmd == "search":
            print(json.dumps(search(args.query, args.type, args.limit), indent=1))
        elif args.cmd == "artist":
            print(json.dumps(artist(_slug(args.slug)), indent=1))
        elif args.cmd == "team":
            types = TEAM_TYPES if args.all else OFFER_TEAM_TYPES
            print(json.dumps(team(_slug(args.slug), types), indent=1))
        elif args.cmd == "tours":
            print(json.dumps(tours(_slug(args.slug), args.limit), indent=1))
        elif args.cmd == "company":
            print(json.dumps(company(_slug(args.slug, "company")), indent=1))
        elif args.cmd == "brief":
            print(json.dumps(brief(args.name), indent=1))
    except RostrError as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
