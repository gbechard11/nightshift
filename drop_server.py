#!/usr/bin/env python3
"""Public drop-page server for Nightshift Drops.

Runs behind the same Tailscale Funnel as the unsubscribe/click endpoints:
   Funnel :443  /d  -> 127.0.0.1:8783 (here)
Funnel's --set-path STRIPS the /d prefix, so this backend sees "/" with the
?id= query. GET renders the drop landing page; POST captures a fan signup
(email + mobile) into the master owned list that scripts/blast.py sends from.

Stdlib only — no framework — matching unsubscribe_server.py / click_server.py.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drop_common as dc  # noqa: E402

BIND = ("127.0.0.1", int(os.environ.get("DROP_PORT", "8783")))

_NOTFOUND = """<!doctype html><meta charset=utf-8>
<title>Nightshift</title>
<body style="background:#0a0a0a;color:#fafafa;font-family:Arial;text-align:center;
padding:80px 20px"><h1 style="letter-spacing:.2em;text-transform:uppercase">Nightshift</h1>
<p style="color:#8a8a8a">Nothing dropping here right now.</p></body>"""

_THANKS = """<!doctype html><meta charset=utf-8><meta name=viewport
content="width=device-width,initial-scale=1"><title>You're in</title>
<body style="background:#0a0a0a;color:#fafafa;font-family:'Helvetica Neue',Arial;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0">
<div style="text-align:center;padding:40px 24px;max-width:420px">
<h1 style="text-transform:uppercase;letter-spacing:.02em;font-size:26px;margin:0 0 12px">
You're on the list</h1>
<p style="color:#9a9a9a;line-height:1.5;margin:0">We'll email and text you the
moment it drops.</p></div></body>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "nsdrops/1.0"
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _drop_id(self, u):
        return (parse_qs(u.query).get("id") or [""])[0]

    def do_GET(self):
        u = urlparse(self.path)
        if u.path.rstrip("/").endswith("health"):
            return self._send(200, b"ok", "text/plain; charset=utf-8")
        q = parse_qs(u.query)
        drop_id = self._drop_id(u)
        # Serve uploaded artwork for the drop page <img>.
        if drop_id and (q.get("asset") or [""])[0] == "art":
            path = dc.art_file(drop_id)
            if not path or not os.path.exists(path):
                return self._send(404, b"", "text/plain")
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", dc.art_content_type(path))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            return self.wfile.write(data)
        drop = dc.load_drop(drop_id) if drop_id else None
        if not drop or drop.get("status") == "closed":
            return self._send(404, _NOTFOUND)
        return self._send(200, dc.render_page(drop))

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        form = {k: (v[0] if v else "") for k, v in parse_qs(raw).items()}
        wants_json = "application/json" in (self.headers.get("Accept") or "")

        drop_id = self._drop_id(u) or form.get("id", "")
        drop = dc.load_drop(drop_id) if drop_id else None
        if not drop:
            if wants_json:
                return self._send(404, json.dumps({"ok": False, "error": "no such drop"}),
                                  "application/json")
            return self._send(404, _NOTFOUND)

        # Honeypot: real users never fill the hidden "website" field.
        if form.get("website", "").strip():
            if wants_json:
                return self._send(200, json.dumps({"ok": True}), "application/json")
            return self._send(200, _THANKS)

        ok, msg = dc.add_signup(
            drop_id, email=form.get("email", ""), phone=form.get("phone", ""),
            name=form.get("name", ""), city=form.get("city", "") or drop.get("city", ""),
        )
        if wants_json:
            if not ok:
                return self._send(400, json.dumps({"ok": False, "msg": msg}),
                                  "application/json")
            title = "You're on the list"
            body = "We'll email and text you the moment it drops."
            if drop.get("buy_url") and drop.get("status") == "live":
                body = "We'll keep you posted. Grab your tickets while they last."
            return self._send(200, json.dumps({"ok": True, "title": title, "msg": body}),
                              "application/json")
        return self._send(200 if ok else 400, _THANKS if ok else _NOTFOUND)

    def log_message(self, fmt, *args):
        return


def main():
    httpd = ThreadingHTTPServer(BIND, Handler)
    print(f"drops endpoint listening on {BIND[0]}:{BIND[1]} (path /d)", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
