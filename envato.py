"""Envato Elements client for Claude / Pedro / NS Team Bot.

Envato Elements (elements.envato.com) is our unlimited creative-asset
subscription: stock video, motion/video templates, fonts, graphics, music,
SFX, photos, presentations, etc. Account: seba@nightshiftent.ca.

WHY THIS IS COOKIE-BASED, NOT API-KEY-BASED
--------------------------------------------
Envato Elements has **no official download API** for subscribers. The
api.envato.com personal-token API only covers Market (purchase/author) data,
not Elements downloads. The only working way to pull an Elements asset is to
replay the authenticated browser flow:

    POST https://elements.envato.com/elements-api/items/{item_id}/download_and_license.json
         (with the logged-in session cookies + CSRF token)
    -> returns a short-lived signed `downloadUrl` you then GET.

The login page itself sits behind a Cloudflare **Turnstile** interactive
challenge that no automated browser clears from this VPS (datacenter IP =
forced hard challenge — verified with headless Chromium, headful Chromium under
xvfb, and real Google Chrome; all blocked). So we DON'T automate login. Instead
we seed the session ONCE with cookies exported from a real logged-in browser
(see `login` command / `/envatologin` in the bot) and reuse them here. The VPS
IP is NOT blocked for content/API calls — only the JS login flow is gated — so
a seeded session works fine for search + download.

Cookies live in `envato_cookies.json` (gitignored). They last ~weeks; the
`status` command reports validity and the bot pings Greg to re-seed when they
lapse.

Design mirrors showpass.py / gdrive.py: env config up top, a `configured()`
gate, a custom error type, sync httpx (async callers use asyncio.to_thread),
and Drive upload reusing token.json.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

BASE = "https://elements.envato.com"
COOKIE_FILE = Path(os.environ.get("ENVATO_COOKIE_FILE",
                                  Path(__file__).with_name("envato_cookies.json")))
# Google Drive folder downloads are filed under (set in .env after first run).
DRIVE_FOLDER = os.environ.get("ENVATO_DRIVE_FOLDER", "")
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
DOWNLOAD_DIR = Path(os.environ.get("ENVATO_DOWNLOAD_DIR", "/data/greg/envato"))

# Cookies that actually matter for an authenticated Elements session. We keep
# whatever the browser exported but these are the load-bearing ones.
SESSION_COOKIE_HINTS = ("_elements", "session", "remember", "token", "sso", "auth")

# Item URLs look like  https://elements.envato.com/{category}/{slug}-{ITEM_ID}
# where ITEM_ID is a ~22-char base64url token.
_ITEM_ID_RE = re.compile(r"/[a-z0-9-]+/[A-Za-z0-9-]+-([A-Za-z0-9_-]{20,24})(?:[/?#]|$)")
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,24}$")


class EnvatoError(Exception):
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
    """True once a session has been seeded with cookies."""
    return bool(_load().get("cookies"))


def _cookies() -> dict:
    return _load().get("cookies", {})


def _ua() -> str:
    return _load().get("user_agent") or DEFAULT_UA


def parse_cookie_header(header: str) -> dict:
    """`name=value; name2=value2` (copied from a browser request) -> dict."""
    out = {}
    for part in header.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def parse_cookies_txt(text: str) -> dict:
    """Netscape cookies.txt (from a 'Get cookies.txt' browser extension)."""
    out = {}
    for line in text.splitlines():
        line = line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        f = line.split("\t")
        if len(f) >= 7 and "envato.com" in f[0]:
            out[f[5]] = f[6]
    return out


def parse_curl(text: str) -> tuple[dict, str]:
    """Parse a 'Copy as cURL' command (Chrome/Firefox DevTools).

    Returns (cookies, user_agent). This is the easiest seed path: it captures
    HttpOnly session cookies (which `document.cookie` can't see) AND the exact
    UA the cookies were minted with, in one copy-paste.
    """
    cookies, ua = {}, ""
    # -b/--cookie "a=1; b=2"
    m = re.search(r"(?:-b|--cookie)\s+([\"'])(.*?)\1", text, re.S)
    if m:
        cookies.update(parse_cookie_header(m.group(2)))
    # -H 'cookie: ...' and -H 'user-agent: ...'
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


def detect_and_parse(text: str) -> tuple[dict, str]:
    """Auto-detect a pasted credential blob and return (cookies, user_agent).

    Handles, in order: a 'Copy as cURL' command, a Netscape cookies.txt, an
    EditThisCookie/Cookie-Editor JSON export, and finally a bare
    'name=val; name2=val2' cookie header. Lets the bot accept whatever Greg
    pastes without him having to say which format it is.
    """
    t = text.strip()
    low = t[:300].lower()
    if "curl " in low or "-H " in t or "--header" in t or "--cookie" in t:
        c, ua = parse_curl(t)
        if c:
            return c, ua
    if "\t" in t and "envato" in t.lower():
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


def parse_cookies_json(text: str) -> dict:
    """EditThisCookie / Cookie-Editor JSON export -> dict."""
    data = json.loads(text)
    if isinstance(data, dict) and "cookies" in data:
        data = data["cookies"]
    out = {}
    for c in data:
        if isinstance(c, dict) and c.get("name"):
            dom = c.get("domain", "")
            if not dom or "envato.com" in dom:
                out[c["name"]] = c.get("value", "")
    return out


def seed_cookies(cookies: dict, user_agent: str = "") -> dict:
    """Store a freshly-exported cookie set. Returns a summary."""
    cookies = {k: v for k, v in cookies.items() if v}
    if not cookies:
        raise EnvatoError("No cookies parsed — check the export format.")
    has_session = any(any(h in k.lower() for h in SESSION_COOKIE_HINTS)
                      for k in cookies)
    if not has_session:
        raise EnvatoError(
            "None of the cookies look like an Elements session cookie "
            "(expected something containing _elements/session/token). "
            "Make sure you exported them while LOGGED IN to elements.envato.com.")
    _save({"cookies": cookies, "user_agent": user_agent or DEFAULT_UA,
           "saved_at": int(time.time())})
    return {"count": len(cookies), "has_session": has_session,
            "names": sorted(cookies)[:20]}


# --- http ---------------------------------------------------------------------

def _client(timeout: float = 60.0) -> httpx.Client:
    if not configured():
        raise EnvatoError(
            "No Envato session seeded. Export cookies from a logged-in "
            "elements.envato.com browser and run:  envato.py login  "
            "(or /envatologin in the bot).")
    headers = {
        "User-Agent": _ua(),
        "Accept": "application/json, text/plain, */*",
        "Referer": BASE + "/",
        "Origin": BASE,
    }
    return httpx.Client(timeout=timeout, headers=headers, cookies=_cookies(),
                        follow_redirects=True)


def _looks_logged_out(resp: httpx.Response) -> bool:
    if resp.status_code in (401, 403):
        return True
    low = resp.text[:4000].lower()
    return "just a moment" in low or "sign-in" in str(resp.url).lower()


def status() -> dict:
    """Report whether the seeded session is still valid."""
    blob = _load()
    if not blob.get("cookies"):
        return {"configured": False, "reason": "no cookies seeded"}
    age_days = round((time.time() - blob.get("saved_at", 0)) / 86400, 1)
    try:
        with _client(timeout=30) as c:
            # An authenticated account endpoint; falls back to homepage marker.
            r = c.get(BASE + "/elements-api/account/details.json")
            ok = r.status_code == 200 and "json" in r.headers.get("content-type", "")
            who = ""
            if ok:
                try:
                    d = r.json()
                    who = (d.get("data", {}) or {}).get("email") or d.get("email", "")
                except Exception:
                    pass
            if not ok and r.status_code == 404:
                # endpoint shape unknown until validated live; treat reachable
                # non-auth-error as "cookies present, needs live check"
                ok = not _looks_logged_out(r)
            return {"configured": True, "valid": bool(ok), "age_days": age_days,
                    "account": who, "cookie_count": len(blob["cookies"])}
    except EnvatoError as e:
        return {"configured": True, "valid": False, "reason": str(e),
                "age_days": age_days}


# --- item id helpers ----------------------------------------------------------

def extract_item_id(url_or_id: str) -> str:
    s = url_or_id.strip()
    if _BARE_ID_RE.match(s):
        return s
    m = _ITEM_ID_RE.search(s)
    if m:
        return m.group(1)
    raise EnvatoError("Couldn't find an item id in: %s" % url_or_id)


def _csrf_tokens(client: httpx.Client, item_url: str) -> dict:
    """Fetch the item page and scrape the CSRF token(s) the download POST needs."""
    r = client.get(item_url)
    html = r.text
    headers = {}
    for pat, hdr in [
        (r'name="csrf-token"\s+content="([^"]+)"', "X-CSRF-Token"),
        (r'"csrfToken"\s*:\s*"([^"]+)"', "X-CSRF-Token"),
        (r'name="_csrf"\s+value="([^"]+)"', "X-CSRF-Token"),
    ]:
        m = re.search(pat, html)
        if m:
            headers[hdr] = m.group(1)
            break
    return headers


# --- search -------------------------------------------------------------------

def autosuggest(keyword: str) -> list[str]:
    """Keyword suggestions with the category Envato routes them to (public, no
    auth). Returns strings like 'drone city  [stock-video]'."""
    url = "https://autosuggest.aws.elements.envato.com/?keyword=" + quote(keyword)
    out = []
    try:
        data = httpx.get(url, headers={"User-Agent": DEFAULT_UA}, timeout=20).json()
    except Exception:
        return out
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict):
            out.append(str(d))
            continue
        term = d.get("term") or d.get("value") or ""
        itype = d.get("itemType") or ""
        out.append("%s  [%s]" % (term, itype) if itype else term)
    return out


def search(query: str, item_type: str = "", page: int = 1, limit: int = 24) -> list[dict]:
    """Search Elements. Returns [{id,title,url,type,thumbnail}].

    Uses the authenticated session so results match what the account sees.
    NOTE: the exact results-API shape is validated live once cookies are seeded;
    this targets the elements-api the frontend uses and falls back to parsing
    the server-rendered results page.
    """
    with _client() as c:
        # Primary: the frontend search API.
        params = {"terms": query, "page": page, "sort_by": "relevance"}
        if item_type:
            params["item_type"] = item_type
        for path in ("/elements-api/search/items.json", "/elements-api/items/search.json"):
            try:
                r = c.get(BASE + path, params=params)
                if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                    return _normalize_search(r.json(), limit)
            except Exception:
                continue
        # Fallback: parse the server-rendered all-items page.
        seg = item_type or "all-items"
        r = c.get("%s/%s/%s" % (BASE, seg, quote(query)))
        return _parse_results_html(r.text, limit)


def _normalize_search(data, limit: int) -> list[dict]:
    items = []
    blob = data
    if isinstance(data, dict):
        blob = data.get("data") or data.get("items") or data.get("results") or []
    for it in (blob or [])[:limit]:
        if not isinstance(it, dict):
            continue
        iid = it.get("id") or it.get("itemId") or ""
        title = it.get("title") or it.get("name") or ""
        slug = it.get("slug") or ""
        cat = (it.get("itemType") or it.get("item_type") or it.get("category") or "")
        url = it.get("url") or (("%s/%s/%s-%s" % (BASE, cat, slug, iid)) if iid else "")
        thumb = ""
        for k in ("previewUrl", "preview_url", "thumbnailUrl", "coverImageUrl"):
            if it.get(k):
                thumb = it[k]
                break
        items.append({"id": iid, "title": title, "url": url, "type": cat,
                      "thumbnail": thumb})
    return items


def _parse_results_html(html: str, limit: int) -> list[dict]:
    seen, items = set(), []
    for m in _ITEM_ID_RE.finditer(html):
        iid = m.group(1)
        if iid in seen:
            continue
        seen.add(iid)
        full = m.group(0).rstrip("/?#")
        items.append({"id": iid, "title": "", "url": BASE + full, "type": "",
                      "thumbnail": ""})
        if len(items) >= limit:
            break
    return items


# --- download -----------------------------------------------------------------

def download(url_or_id: str, dest_dir: Path | None = None,
             project_name: str = "Nightshift") -> Path:
    """Download an Elements item to local disk. Returns the saved file path."""
    item_id = extract_item_id(url_or_id)
    dest_dir = Path(dest_dir or DOWNLOAD_DIR)
    dest_dir.mkdir(parents=True, exist_ok=True)
    item_url = url_or_id if url_or_id.startswith("http") else "%s/item/%s" % (BASE, item_id)
    with _client() as c:
        csrf = _csrf_tokens(c, item_url)
        api = "%s/elements-api/items/%s/download_and_license.json" % (BASE, item_id)
        payload = {"licenseType": "project", "projectName": project_name,
                   "searchCorrelationId": ""}
        r = c.post(api, json=payload, headers={**csrf, "Content-Type": "application/json"})
        if _looks_logged_out(r):
            raise EnvatoError(
                "Session rejected (logged out / Cloudflare). Re-seed cookies "
                "with `envato.py login` (or /envatologin).")
        if r.status_code >= 400:
            raise EnvatoError("download_and_license %d: %s" % (r.status_code, r.text[:300]))
        data = r.json()
        attrs = (data.get("data", {}) or {}).get("attributes", data)
        dl_url = attrs.get("downloadUrl") or attrs.get("download_url")
        if not dl_url:
            raise EnvatoError("No downloadUrl in response: %s" % json.dumps(data)[:300])
        fname = _filename_from_url(dl_url, item_id)
        out = dest_dir / fname
        with c.stream("GET", dl_url) as resp:
            resp.raise_for_status()
            with open(out, "wb") as fh:
                for chunk in resp.iter_bytes(1 << 16):
                    fh.write(chunk)
    return out


def _filename_from_url(url: str, item_id: str) -> str:
    path = urlparse(url).path
    base = os.path.basename(path) or ("envato_%s" % item_id)
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    if "." not in base:
        base += ".bin"
    return base


# --- Google Drive upload (reuses gdrive.py's token.json) -----------------------

def upload_to_drive(path: Path, folder_id: str = "") -> dict:
    """Upload a downloaded asset to Drive. Returns {id,name,link}."""
    import mimetypes
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    token_path = Path(os.environ.get("GCAL_TOKEN", "token.json"))
    if not token_path.is_file():
        raise EnvatoError("Drive token.json not found (set GCAL_TOKEN).")
    scopes = ["https://www.googleapis.com/auth/calendar",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_authorized_user_file(str(token_path), scopes)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)
    shared = {"supportsAllDrives": True}

    folder_id = folder_id or DRIVE_FOLDER
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    media = MediaFileUpload(str(path), mimetype=mime, resumable=True)
    body = {"name": path.name}
    if folder_id:
        body["parents"] = [folder_id]
    meta = svc.files().create(body=body, media_body=media,
                              fields="id,name,webViewLink", **shared).execute()
    return {"id": meta["id"], "name": meta["name"],
            "link": meta.get("webViewLink", "")}


def ensure_drive_folder(name: str = "Envato Assets", parent: str = "") -> str:
    """Find-or-create the Drive folder downloads are filed under; return its id."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    token_path = Path(os.environ.get("GCAL_TOKEN", "token.json"))
    scopes = ["https://www.googleapis.com/auth/calendar",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_authorized_user_file(str(token_path), scopes)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)
    parent = parent or os.environ.get("DRIVE_FOLDER_ID", "")
    q = ("name='%s' and mimeType='application/vnd.google-apps.folder' "
         "and trashed=false" % name.replace("'", "\\'"))
    if parent:
        q += " and '%s' in parents" % parent
    res = svc.files().list(q=q, fields="files(id,name)", supportsAllDrives=True,
                           includeItemsFromAllDrives=True).execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent:
        body["parents"] = [parent]
    created = svc.files().create(body=body, fields="id", supportsAllDrives=True).execute()
    return created["id"]


# --- CLI ----------------------------------------------------------------------

def _read_arg_or_file(value: str) -> str:
    if value == "-":
        return sys.stdin.read()
    if value and os.path.isfile(value):
        return Path(value).read_text(encoding="utf-8")
    return value or sys.stdin.read()


def main() -> None:
    p = argparse.ArgumentParser(description="Envato Elements client (cookie-seeded)")
    sub = p.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser("status", help="is the seeded session valid?")
    st.add_argument("--json", action="store_true")

    lg = sub.add_parser("login", help="seed/refresh session from exported cookies")
    lg.add_argument("--auto", default="", help="auto-detect a pasted blob (curl/cookies.txt/json/header); file path or '-' for stdin")
    lg.add_argument("--curl", default="", help="paste a 'Copy as cURL' command (easiest; file path or '-' for stdin)")
    lg.add_argument("--cookie-header", default="", help='"name=val; name2=val2" from devtools')
    lg.add_argument("--cookies-txt", default="", help="path to Netscape cookies.txt")
    lg.add_argument("--cookies-json", default="", help="path to EditThisCookie JSON export")
    lg.add_argument("--user-agent", default="", help="browser UA the cookies were minted with")

    se = sub.add_parser("search", help="search assets")
    se.add_argument("query")
    se.add_argument("--type", default="", help="stock-video|video-templates|fonts|graphics|music|sound-effects|photos|...")
    se.add_argument("--limit", type=int, default=24)
    se.add_argument("--json", action="store_true")

    sg = sub.add_parser("suggest", help="keyword autosuggest (public)")
    sg.add_argument("query")

    dl = sub.add_parser("download", help="download an item by url or id")
    dl.add_argument("item", help="item URL or 22-char id")
    dl.add_argument("--out", default="", help="local dir (default ENVATO_DOWNLOAD_DIR)")
    dl.add_argument("--to-drive", action="store_true", help="also upload to Drive")
    dl.add_argument("--project", default="Nightshift")
    dl.add_argument("--json", action="store_true")

    fd = sub.add_parser("init-drive-folder", help="create/find the Drive 'Envato Assets' folder")

    args = p.parse_args()
    try:
        if args.cmd == "status":
            s = status()
            print(json.dumps(s, indent=1) if args.json else
                  ("OK valid=%(valid)s account=%(account)s age=%(age_days)sd"
                   % {**{"account": "", "valid": s.get("valid"),
                         "age_days": s.get("age_days")}, **s}
                   if s.get("configured") else "NOT CONFIGURED — seed cookies via login"))
        elif args.cmd == "login":
            ua = args.user_agent
            if args.auto:
                cookies, det_ua = detect_and_parse(_read_arg_or_file(args.auto))
                ua = ua or det_ua
            elif args.curl:
                cookies, curl_ua = parse_curl(_read_arg_or_file(args.curl))
                ua = ua or curl_ua
            elif args.cookie_header:
                cookies = parse_cookie_header(args.cookie_header)
            elif args.cookies_txt:
                cookies = parse_cookies_txt(_read_arg_or_file(args.cookies_txt))
            elif args.cookies_json:
                cookies = parse_cookies_json(_read_arg_or_file(args.cookies_json))
            else:
                raise EnvatoError("Give --auto, --curl, --cookie-header, --cookies-txt, or --cookies-json.")
            summary = seed_cookies(cookies, ua)
            print(json.dumps({"ok": True, **summary}, indent=1))
            print("Now verify:  envato.py status", file=sys.stderr)
        elif args.cmd == "suggest":
            print("\n".join(autosuggest(args.query)) or "(no suggestions)")
        elif args.cmd == "search":
            res = search(args.query, args.type, limit=args.limit)
            if args.json:
                print(json.dumps(res, indent=1))
            else:
                for it in res:
                    print("%s  %s  %s" % (it["id"], it.get("type", ""), it["url"]))
                print("(%d results)" % len(res), file=sys.stderr)
        elif args.cmd == "download":
            out = download(args.item, Path(args.out) if args.out else None, args.project)
            result = {"saved": str(out), "bytes": out.stat().st_size}
            if args.to_drive:
                result["drive"] = upload_to_drive(out)
            print(json.dumps(result, indent=1) if args.json else
                  ("saved %s (%d bytes)%s" % (out, out.stat().st_size,
                   ("\nDrive: " + result["drive"]["link"]) if args.to_drive else "")))
        elif args.cmd == "init-drive-folder":
            fid = ensure_drive_folder()
            print(json.dumps({"folder_id": fid}))
            print("Set ENVATO_DRIVE_FOLDER=%s in .env" % fid, file=sys.stderr)
    except EnvatoError as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
