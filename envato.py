"""Envato Elements client for Claude / Pedro / NS Team Bot.

Envato Elements is our unlimited creative-asset subscription: stock video,
video templates, fonts, graphics, music, SFX, photos, etc. Account:
seba@nightshiftent.ca. As of 2026 the logged-in product lives at
**app.envato.com** (elements.envato.com redirects there once signed in).

WHY THIS IS COOKIE-BASED, NOT API-KEY-BASED
--------------------------------------------
Envato Elements has **no official download API** for subscribers, and the
sign-in page sits behind a Cloudflare Turnstile challenge that no automated
browser clears from this VPS (datacenter IP). So we DON'T automate login. We
seed the session ONCE with cookies exported from a real logged-in browser
(see `login` / `/envatologin`) and replay them with httpx. The VPS IP is NOT
blocked for content/API calls — only the JS login is gated — so a seeded
session works fine for search + download.

HOW IT WORKS (verified live 2026-06-11)
---------------------------------------
app.envato.com is a React-Router (Remix) app. Its data routes return a
turbo-stream payload (index-referenced JSON) which `_ts_obj` decodes.
- Search:   GET /search/all.data?term=<q>     -> items [{itemUuid,itemType,title,image,...}]
- Download: GET /download.data?itemUuid=<u>&itemType=<t>[&assetUuid][&projectName]
            -> {downloadUrl: "https://dam-assets.envatousercontent.com/..."} then GET it.
Auth/identity is the `envatoid` JWT cookie (decodable for account + expiry).

Cookies live in `envato_cookies.json` (gitignored), last ~weeks. `status`
reports validity; the bot pings Greg to re-seed via /envatologin when stale.

Design mirrors showpass.py / gdrive.py: env config up top, a `configured()`
gate, a custom error type, sync httpx (async callers use asyncio.to_thread),
Drive upload reusing token.json.
"""
import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

BASE = "https://app.envato.com"
COOKIE_FILE = Path(os.environ.get("ENVATO_COOKIE_FILE",
                                  Path(__file__).with_name("envato_cookies.json")))
DRIVE_FOLDER = os.environ.get("ENVATO_DRIVE_FOLDER", "")
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
DOWNLOAD_DIR = Path(os.environ.get("ENVATO_DOWNLOAD_DIR", "/data/greg/envato"))

# The cookie that carries the logged-in identity (+others kept as-is).
SESSION_COOKIE_HINTS = ("_elements", "session", "remember", "token", "sso",
                        "auth", "envatoid", "envato_client")

ITEM_TYPES = {"stock-video", "video-templates", "fonts", "graphics", "music",
              "sound-effects", "photos", "graphic-templates", "3d", "add-ons",
              "presentation-templates", "web-templates", "cms-templates",
              "wordpress", "luts"}

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# item URL: /search/all/{type}/{uuid}  or  /{type}/{slug}/{uuid}
_URL_TYPE_UUID_RE = re.compile(
    r"/([a-z0-9-]+)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


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
    return bool(_load().get("cookies"))


def _cookies() -> dict:
    return _load().get("cookies", {})


def _ua() -> str:
    return _load().get("user_agent") or DEFAULT_UA


def _jwt_payload(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


# --- cookie parsing -----------------------------------------------------------

def parse_cookie_header(header: str) -> dict:
    out = {}
    for part in header.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def parse_curl(text: str) -> tuple[dict, str]:
    """Parse a 'Copy as cURL' command -> (cookies, user_agent). Captures HttpOnly
    cookies and the matching UA in one paste."""
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


def detect_and_parse(text: str) -> tuple[dict, str]:
    """Auto-detect a pasted blob: curl / cookies.txt / JSON / bare header."""
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


def parse_cookies_txt(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        f = line.split("\t")
        if len(f) >= 7 and "envato.com" in f[0]:
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
            if not dom or "envato.com" in dom:
                out[c["name"]] = c.get("value", "")
    return out


def seed_cookies(cookies: dict, user_agent: str = "") -> dict:
    cookies = {k: v for k, v in cookies.items() if v}
    if not cookies:
        raise EnvatoError("No cookies parsed — check the export format.")
    has_session = any(any(h in k.lower() for h in SESSION_COOKIE_HINTS)
                      for k in cookies)
    if not has_session:
        raise EnvatoError(
            "None of the cookies look like an Envato session (expected something "
            "like envatoid/_elements/session/token). Export them while LOGGED IN "
            "to app.envato.com / elements.envato.com.")
    _save({"cookies": cookies, "user_agent": user_agent or DEFAULT_UA,
           "saved_at": int(time.time())})
    pl = _jwt_payload(cookies.get("envatoid", ""))
    return {"count": len(cookies), "has_session": has_session,
            "account": (pl.get("given_name", "") + " " + pl.get("family_name", "")).strip()
            or pl.get("nickname", ""), "names": sorted(cookies)[:20]}


# --- http + turbo-stream ------------------------------------------------------

def _client(timeout: float = 60.0) -> httpx.Client:
    if not configured():
        raise EnvatoError(
            "No Envato session seeded. Export cookies from a logged-in "
            "app.envato.com browser and run:  envato.py login  (or /envatologin).")
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
    return "just a moment" in low or "/sign-in" in str(resp.url).lower()


def _ts_obj(txt: str):
    """Decode a React-Router turbo-stream payload (index-referenced JSON)."""
    arr = json.loads(txt)
    sys.setrecursionlimit(1_000_000)
    memo: dict = {}

    def res(i):
        if not isinstance(i, int):
            return i
        if i in memo:
            return memo[i]
        v = arr[i]
        if isinstance(v, dict):
            out: dict = {}
            memo[i] = out
            for k, val in v.items():
                kk = res(int(k[1:])) if isinstance(k, str) and k[:1] == "_" else k
                out[kk] = res(val)
            return out
        if isinstance(v, list):
            out2: list = []
            memo[i] = out2
            for x in v:
                out2.append(res(x))
            return out2
        memo[i] = v
        return v

    return res(0)


def _walk_collect(obj, key: str) -> list:
    """All values for `key` found anywhere in a nested structure."""
    found, stack = [], [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if key in cur:
                found.append(cur[key])
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return found


def _walk_dicts_with(obj, key: str) -> list:
    """All dicts that contain `key`."""
    found, stack, seen = [], [obj], set()
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if id(cur) in seen:
                continue
            seen.add(id(cur))
            if key in cur:
                found.append(cur)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return found


# --- session status -----------------------------------------------------------

def status() -> dict:
    blob = _load()
    if not blob.get("cookies"):
        return {"configured": False, "reason": "no cookies seeded"}
    age_days = round((time.time() - blob.get("saved_at", 0)) / 86400, 1)
    pl = _jwt_payload(blob["cookies"].get("envatoid", ""))
    name = (pl.get("given_name", "") + " " + pl.get("family_name", "")).strip() \
        or pl.get("nickname", "")
    exp = pl.get("exp", 0)
    now = time.time()
    token_ok = bool(exp) and exp > now
    days_left = round((exp - now) / 86400, 1) if exp else None
    live = None
    try:
        with _client(30) as c:
            r = c.get(BASE + "/search/all.data", params={"term": "test"})
            live = (r.status_code == 200 and not _looks_logged_out(r))
    except Exception:
        live = None
    return {"configured": True, "valid": bool(token_ok and live is not False),
            "account": name, "expires_in_days": days_left, "age_days": age_days,
            "live_ok": live, "cookie_count": len(blob["cookies"])}


# --- item id helpers ----------------------------------------------------------

def resolve_item(url_or_id: str, item_type: str = "") -> tuple[str, str]:
    """Return (item_uuid, item_type) from a URL, 'type:uuid', or bare uuid+--type."""
    s = url_or_id.strip()
    m = _URL_TYPE_UUID_RE.search(s)
    if m:
        seg, uuid = m.group(1), m.group(2)
        # URL form /search/all/{type}/{uuid}: seg may be 'all' -> need next part
        parts = [p for p in s.split("/") if p]
        itype = item_type
        for i, p in enumerate(parts):
            if p == uuid and i > 0 and parts[i - 1] in ITEM_TYPES:
                itype = parts[i - 1]
        if not itype and seg in ITEM_TYPES:
            itype = seg
        return uuid, itype
    if ":" in s and not s.startswith("http"):
        t, _, u = s.partition(":")
        if _UUID_RE.fullmatch(u.strip()):
            return u.strip(), (item_type or t.strip())
    if _UUID_RE.fullmatch(s):
        if not item_type:
            raise EnvatoError("Item type required for a bare UUID — pass --type "
                              "or use the item URL.")
        return s, item_type
    raise EnvatoError("Couldn't parse an item UUID from: %s" % url_or_id)


# --- search -------------------------------------------------------------------

def autosuggest(keyword: str) -> list[str]:
    """Keyword suggestions with the routed category (public, no auth)."""
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


def _img_url(image) -> str:
    if isinstance(image, str):
        return image
    if isinstance(image, dict):
        for k in ("coverUrl", "url", "src", "previewUrl", "thumbnailUrl"):
            if image.get(k):
                return image[k]
    return ""


def search(query: str, item_type: str = "", page: int = 1, limit: int = 24) -> list[dict]:
    """Search Elements. Returns [{id,title,url,type,thumbnail,author}]."""
    with _client() as c:
        r = c.get(BASE + "/search/all.data", params={"term": query})
        if _looks_logged_out(r):
            raise EnvatoError("Session rejected (logged out). Re-seed cookies via "
                              "`envato.py login` / /envatologin.")
        if r.status_code != 200:
            raise EnvatoError("search %d: %s" % (r.status_code, r.text[:200]))
        obj = _ts_obj(r.text)
    items, seen = [], set()
    for it in _walk_dicts_with(obj, "itemUuid"):
        uuid = it.get("itemUuid")
        itype = it.get("itemType") or ""
        if not uuid or uuid in seen:
            continue
        if item_type and itype != item_type:
            continue
        seen.add(uuid)
        items.append({
            "id": uuid,
            "type": itype,
            "title": it.get("title") or it.get("name") or "",
            "author": it.get("authorUsername") or "",
            "thumbnail": _img_url(it.get("image")),
            "url": "%s/search/all/%s/%s" % (BASE, itype or "all", uuid),
        })
        if len(items) >= limit:
            break
    return items


# --- download -----------------------------------------------------------------

def download(url_or_id: str, dest_dir: Path | None = None,
             project_name: str = "Nightshift", item_type: str = "") -> Path:
    """Download an Elements item to local disk. Returns the saved file path."""
    uuid, itype = resolve_item(url_or_id, item_type)
    if not itype:
        raise EnvatoError("Item type required — pass --type or use the item URL.")
    dest_dir = Path(dest_dir or DOWNLOAD_DIR)
    dest_dir.mkdir(parents=True, exist_ok=True)
    with _client() as c:
        params = {"itemUuid": uuid, "itemType": itype}
        if project_name:
            params["projectName"] = project_name
        dl_url = _request_download_url(c, params)
        out = dest_dir / _filename_from_url(dl_url, uuid)
        with c.stream("GET", dl_url) as resp:
            resp.raise_for_status()
            with open(out, "wb") as fh:
                for chunk in resp.iter_bytes(1 << 16):
                    fh.write(chunk)
    if out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        raise EnvatoError("Downloaded 0 bytes — the asset URL may have expired.")
    return out


def _request_download_url(c: httpx.Client, params: dict) -> str:
    r = c.get(BASE + "/download.data", params=params)
    if _looks_logged_out(r):
        raise EnvatoError("Session rejected (logged out / Cloudflare). Re-seed "
                          "cookies with `envato.py login` (or /envatologin).")
    if r.status_code >= 400:
        raise EnvatoError("download.data %d: %s" % (r.status_code, r.text[:200]))
    obj = _ts_obj(r.text)
    urls = _walk_collect(obj, "downloadUrl")
    url = next((u for u in urls if isinstance(u, str) and u.startswith("http")), "")
    if url:
        return url
    # Some item types (video) require choosing a derivative (resolution) first.
    fmts = _walk_collect(obj, "downloadFormats")
    fmts = fmts[0] if fmts else []
    if isinstance(fmts, list) and fmts:
        asset = None
        for f in fmts:
            if isinstance(f, dict) and f.get("assetUuid"):
                asset = f["assetUuid"]  # last = usually highest quality
        if asset:
            r2 = c.get(BASE + "/download.data", params={**params, "assetUuid": asset})
            obj2 = _ts_obj(r2.text)
            urls2 = _walk_collect(obj2, "downloadUrl")
            url2 = next((u for u in urls2 if isinstance(u, str) and u.startswith("http")), "")
            if url2:
                return url2
    err = _walk_collect(obj, "error") + _walk_collect(obj, "errorCode")
    raise EnvatoError("No downloadUrl returned%s" %
                      ((" — " + str(err[0])) if err else
                       " (item may need a license confirmation in the Envato UI)."))


def _filename_from_url(url: str, item_id: str) -> str:
    base = os.path.basename(urlparse(url).path) or "asset"
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    if "." not in base:
        base += ".bin"
    # Prefix with the item id so generic names (source.zip) stay unique + traceable.
    return "%s_%s" % (item_id[:8], base)


# --- Google Drive upload (reuses gdrive.py's token.json) -----------------------

def _drive_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    token_path = Path(os.environ.get("GCAL_TOKEN", "token.json"))
    if not token_path.is_file():
        raise EnvatoError("Drive token.json not found (set GCAL_TOKEN).")
    scopes = ["https://www.googleapis.com/auth/calendar",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_authorized_user_file(str(token_path), scopes)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def upload_to_drive(path: Path, folder_id: str = "") -> dict:
    import mimetypes
    from googleapiclient.http import MediaFileUpload
    svc = _drive_service()
    folder_id = folder_id or DRIVE_FOLDER
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    media = MediaFileUpload(str(path), mimetype=mime, resumable=True)
    body = {"name": path.name}
    if folder_id:
        body["parents"] = [folder_id]
    meta = svc.files().create(body=body, media_body=media,
                              fields="id,name,webViewLink",
                              supportsAllDrives=True).execute()
    return {"id": meta["id"], "name": meta["name"], "link": meta.get("webViewLink", "")}


def ensure_drive_folder(name: str = "Envato Assets", parent: str = "") -> str:
    svc = _drive_service()
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
    return svc.files().create(body=body, fields="id", supportsAllDrives=True).execute()["id"]


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
    lg.add_argument("--auto", default="", help="auto-detect a pasted blob (curl/cookies.txt/json/header); path or '-' for stdin")
    lg.add_argument("--curl", default="", help="a 'Copy as cURL' command; path or '-' for stdin")
    lg.add_argument("--cookie-header", default="", help='"name=val; name2=val2"')
    lg.add_argument("--cookies-txt", default="", help="path to Netscape cookies.txt")
    lg.add_argument("--cookies-json", default="", help="path to EditThisCookie JSON")
    lg.add_argument("--user-agent", default="")

    se = sub.add_parser("search", help="search assets")
    se.add_argument("query")
    se.add_argument("--type", default="", help="stock-video|video-templates|fonts|graphics|music|sound-effects|photos|...")
    se.add_argument("--limit", type=int, default=24)
    se.add_argument("--json", action="store_true")

    sg = sub.add_parser("suggest", help="keyword autosuggest (public)")
    sg.add_argument("query")

    dl = sub.add_parser("download", help="download an item by url, 'type:uuid', or uuid+--type")
    dl.add_argument("item")
    dl.add_argument("--type", default="", help="item type (if passing a bare uuid)")
    dl.add_argument("--out", default="", help="local dir (default ENVATO_DOWNLOAD_DIR)")
    dl.add_argument("--to-drive", action="store_true")
    dl.add_argument("--project", default="Nightshift")
    dl.add_argument("--json", action="store_true")

    sub.add_parser("init-drive-folder", help="create/find the 'Envato Assets' Drive folder")

    args = p.parse_args()
    try:
        if args.cmd == "status":
            s = status()
            if args.json:
                print(json.dumps(s, indent=1))
            elif not s.get("configured"):
                print("NOT CONFIGURED — seed cookies via login")
            else:
                print("%s  account=%s  expires_in=%sd  (cookies %sd old)" % (
                    "VALID" if s.get("valid") else "INVALID/EXPIRED",
                    s.get("account") or "?", s.get("expires_in_days"), s.get("age_days")))
        elif args.cmd == "login":
            ua = args.user_agent
            if args.auto:
                cookies, det = detect_and_parse(_read_arg_or_file(args.auto)); ua = ua or det
            elif args.curl:
                cookies, cu = parse_curl(_read_arg_or_file(args.curl)); ua = ua or cu
            elif args.cookie_header:
                cookies = parse_cookie_header(args.cookie_header)
            elif args.cookies_txt:
                cookies = parse_cookies_txt(_read_arg_or_file(args.cookies_txt))
            elif args.cookies_json:
                cookies = parse_cookies_json(_read_arg_or_file(args.cookies_json))
            else:
                raise EnvatoError("Give --auto, --curl, --cookie-header, --cookies-txt, or --cookies-json.")
            print(json.dumps({"ok": True, **seed_cookies(cookies, ua)}, indent=1))
            print("Now verify:  envato.py status", file=sys.stderr)
        elif args.cmd == "suggest":
            print("\n".join(autosuggest(args.query)) or "(no suggestions)")
        elif args.cmd == "search":
            res = search(args.query, args.type, limit=args.limit)
            if args.json:
                print(json.dumps(res, indent=1))
            else:
                for it in res:
                    print("%s:%s  %s" % (it["type"], it["id"], it["title"][:70]))
                print("(%d results)" % len(res), file=sys.stderr)
        elif args.cmd == "download":
            out = download(args.item, Path(args.out) if args.out else None,
                           args.project, args.type)
            result = {"saved": str(out), "bytes": out.stat().st_size}
            if args.to_drive:
                result["drive"] = upload_to_drive(out)
            if args.json:
                print(json.dumps(result, indent=1))
            else:
                print("saved %s (%d bytes)%s" % (out, out.stat().st_size,
                      ("\nDrive: " + result["drive"]["link"]) if args.to_drive else ""))
        elif args.cmd == "init-drive-folder":
            fid = ensure_drive_folder()
            print(json.dumps({"folder_id": fid}))
            print("Set ENVATO_DRIVE_FOLDER=%s in .env" % fid, file=sys.stderr)
    except EnvatoError as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
