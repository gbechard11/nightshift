#!/usr/bin/env python3
"""Public click-tracking endpoint for Nightshift blasts.

Mirrors unsubscribe_server.py: runs behind Tailscale Funnel at the path /c
(Funnel :443 /c -> here on 127.0.0.1:8782). Verifies the HMAC click token,
logs the click to blast-clicks/<campaign>.jsonl, then 302-redirects the reader
to the (signed) destination URL. Because the destination is part of the signed
token, this can only ever redirect to a link we minted — never an open redirect.
No per-send database: the signature IS the proof.
"""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import unsub_common as uc  # noqa: E402

BIND = ("127.0.0.1", int(os.environ.get("CLICK_PORT", "8782")))
CLICK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blast-clicks")
# Where to send a reader whose token is invalid/garbled (never an open redirect).
FALLBACK_URL = os.environ.get("CLICK_FALLBACK_URL", "https://www.ticketweb.ca/")


def _log_click(campaign: str, email: str, url: str) -> None:
    os.makedirs(CLICK_DIR, exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "campaign": campaign, "email": email, "url": url}
    with open(os.path.join(CLICK_DIR, f"{campaign}.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")


class Handler(BaseHTTPRequestHandler):
    server_version = "nsclick/1.0"
    protocol_version = "HTTP/1.1"

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path.rstrip("/").endswith("health"):
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        token = (parse_qs(u.query).get("t") or [""])[0]
        res = uc.verify_click_token(token) if token else None
        if not res:
            return self._redirect(FALLBACK_URL)
        email, campaign, dest = res
        try:
            _log_click(campaign, email, dest)
        except Exception:
            pass
        return self._redirect(dest)

    def log_message(self, fmt, *args):  # keep the journal quiet
        return


def main():
    httpd = ThreadingHTTPServer(BIND, Handler)
    print(f"click endpoint listening on {BIND[0]}:{BIND[1]} (path /c)", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
