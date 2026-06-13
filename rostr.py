#!/usr/bin/env python3
"""ROSTR (rostr.cc) client — cookie-seeded, read-only.

WHY THIS IS COOKIE-BASED, NOT API-KEY-BASED
============================================
ROSTR has no public API. hq.rostr.cc is a server-rendered Rails app behind
Cloudflare; auth is a session cookie set at rostr.cc/auth/signin. The VPS is a
datacenter IP, so we DON'T automate the JS login. We seed the session ONCE with
cookies exported from a real logged-in browser (see `login` / `/rostrlogin`)
and replay them with httpx for read-only GETs. Content pages are not IP-gated
for a seeded session — only the interactive login is.

This is the SAME pattern as envato.py. Cookies live in `rostr_cookies.json`
(gitignored), last ~weeks. `status` reports validity; the bot pings Greg to
re-seed via /rostrlogin when stale.

PURPOSE: let Pedro + Nightshift staff (Seba et al.) pull offer-creation data —
artist -> booking agent / manager contacts, tour & ticket history, audience /
market data, and company (agency/label/management) rosters.

Read-only by design: only GET. No verb writes to ROSTR.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, urljoin

import httpx

BASE = os.environ.get("ROSTR_BASE", "https://hq.rostr.cc")
SIGNIN = "https://rostr.cc/auth/signin"
COOKIE_FILE = Path(os.environ.get("ROSTR_COOKIE_FILE",
                                  Path(__file__).with_name("rostr_cookies.json")))
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

# Cookies that carry the logged-in Rails identity (others are kept as-is).
SESSION_COOKIE_HINTS = ("session", "_rostr", "remember", "auth", "token",
                        "user", "_session", "cf_clearance")


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
    """Parse a 'Copy as cURL' command -> (cookies, user_agent). Captures
    HttpOnly cookies and the matching UA in one paste."""
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
            "None of the cookies look like a ROSTR session (expected something "
            "like _rostr_session/session/remember). Export them while LOGGED IN "
            "to hq.rostr.cc.")
    _save({"cookies": cookies, "user_agent": user_agent or DEFAULT_UA,
           "saved_at": int(time.time())})
    return {"count": len(cookies), "has_session": has_session,
            "names": sorted(cookies)[:30]}


# --- http ---------------------------------------------------------------------

def _client(timeout: float = 60.0) -> httpx.Client:
    if not configured():
        raise RostrError(
            "No ROSTR session seeded. Export cookies from a logged-in "
            "hq.rostr.cc browser and run:  rostr.py login  (or /rostrlogin).")
    headers = {
        "User-Agent": _ua(),
        "Accept": "text/html,application/json,application/xhtml+xml,*/*",
        "Referer": BASE + "/",
    }
    return httpx.Client(timeout=timeout, headers=headers, cookies=_cookies(),
                        follow_redirects=True)


def _looks_logged_out(resp: httpx.Response) -> bool:
    if resp.status_code in (401, 403):
        return True
    u = str(resp.url).lower()
    if "/auth/signin" in u or "/auth/signup" in u or "/welcome" in u:
        return True
    low = resp.text[:4000].lower()
    return "just a moment" in low or "sign in to rostr" in low


def get(path: str, as_json: bool = False, params: dict | None = None) -> httpx.Response:
    """Read-only GET against ROSTR. `path` may be absolute or relative to BASE.
    Set as_json to append `.json` to a resource path (Rails respond_to)."""
    url = path if path.startswith("http") else urljoin(BASE + "/", path.lstrip("/"))
    if as_json and not url.split("?")[0].endswith(".json"):
        head, _, qs = url.partition("?")
        url = head + ".json" + (("?" + qs) if qs else "")
    with _client() as c:
        r = c.get(url, params=params)
    if _looks_logged_out(r):
        raise RostrError(
            "ROSTR session looks logged out / expired (got %s at %s). "
            "Re-seed with rostr.py login (or /rostrlogin)." % (r.status_code, r.url))
    return r


# --- status -------------------------------------------------------------------

def status() -> dict:
    blob = _load()
    if not blob.get("cookies"):
        return {"configured": False, "reason": "no cookies seeded"}
    age_days = round((time.time() - blob.get("saved_at", 0)) / 86400, 1)
    live = None
    detail = ""
    try:
        # The home/dashboard differs logged-in vs logged-out; use it as a probe.
        with _client(30) as c:
            r = c.get(BASE + "/")
        live = not _looks_logged_out(r)
        detail = "%s %s" % (r.status_code, r.url)
    except Exception as e:  # noqa: BLE001
        live = None
        detail = str(e)
    return {"configured": True, "valid": bool(live), "live_ok": live,
            "age_days": age_days, "cookie_count": len(blob["cookies"]),
            "probe": detail}


# --- business verbs (finalized against the live seeded session) ---------------
# NOTE: exact ROSTR route paths are confirmed live after the first seed; the
# constants below are the discovery defaults. `get` is the raw escape hatch.

ROUTES = {
    "search": os.environ.get("ROSTR_SEARCH_ROUTE", "/search"),
    "artist": os.environ.get("ROSTR_ARTIST_ROUTE", "/artists"),
    "company": os.environ.get("ROSTR_COMPANY_ROUTE", "/companies"),
}


def search(query: str, kind: str = "", limit: int = 20) -> dict:
    """Search ROSTR for artists / companies / people. Returns raw JSON when the
    endpoint supports it, else the page HTML (trimmed) for parsing."""
    params = {"q": query}
    if kind:
        params["type"] = kind
    r = get(ROUTES["search"], as_json=True, params=params)
    ct = r.headers.get("content-type", "")
    if "json" in ct:
        return {"format": "json", "data": r.json()}
    return {"format": "html", "url": str(r.url), "html": r.text[:200000]}


def main() -> None:
    p = argparse.ArgumentParser(description="ROSTR client (cookie-seeded, read-only)")
    sub = p.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser("status", help="is the seeded session valid?")
    st.add_argument("--json", action="store_true")

    lg = sub.add_parser("login", help="seed/refresh session from exported cookies")
    lg.add_argument("--auto", default="", help="auto-detect a pasted blob (curl/cookies.txt/json/header); path or '-' for stdin")
    lg.add_argument("--curl", default="", help="a 'Copy as cURL' command; path or '-' for stdin")
    lg.add_argument("--cookie-header", default="", help='"name=val; name2=val2"')
    lg.add_argument("--user-agent", default="")

    g = sub.add_parser("get", help="raw read-only GET (discovery/escape hatch)")
    g.add_argument("path", help="absolute URL or path relative to BASE")
    g.add_argument("--json", action="store_true", help="append .json (Rails respond_to)")
    g.add_argument("--raw", action="store_true", help="print full body (default trims)")

    se = sub.add_parser("search", help="search artists/companies/people")
    se.add_argument("query")
    se.add_argument("--type", default="", help="artist|company|person (if supported)")
    se.add_argument("--limit", type=int, default=20)
    se.add_argument("--json", action="store_true")

    args = p.parse_args()
    try:
        if args.cmd == "status":
            s = status()
            if args.json:
                print(json.dumps(s, indent=1))
            elif not s.get("configured"):
                print("NOT CONFIGURED — seed cookies via login / /rostrlogin")
            else:
                print("%s  (cookies %sd old)  probe=%s" % (
                    "VALID" if s.get("valid") else "INVALID/EXPIRED",
                    s.get("age_days"), s.get("probe")))
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
        elif args.cmd == "get":
            r = get(args.path, as_json=args.json)
            body = r.text if args.raw else r.text[:8000]
            print("HTTP %s  %s  (%s, %d bytes)" % (
                r.status_code, r.url, r.headers.get("content-type", "?"),
                len(r.content)), file=sys.stderr)
            print(body)
        elif args.cmd == "search":
            res = search(args.query, args.type, limit=args.limit)
            print(json.dumps(res, indent=1) if args.json else
                  (json.dumps(res["data"], indent=1) if res["format"] == "json"
                   else res["html"][:8000]))
    except RostrError as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
