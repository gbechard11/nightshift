#!/usr/bin/env python3
"""
Google Calendar OAuth setup — run once to authorize Pedro.
Starts a local HTTP server on port 8765 so the redirect lands on the VPS.
Greg visits the auth URL on his phone, approves, and the token is saved automatically.

Usage:
  python3 gcal_setup.py
  # Or with explicit credentials file:
  python3 gcal_setup.py /path/to/client_secret.json
"""
import sys
import os
import json
import threading
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, '/data/greg/lib')

from google_auth_oauthlib.flow import Flow

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
TOKEN_PATH = Path('/home/gregnightshift/nightshift/google-token.json')
CREDS_PATH = Path('/data/greg/brain/gcal_credentials.json')
REDIRECT_PORT = 8765
TAILSCALE_IP = '100.109.25.82'

auth_code = None
server_done = threading.Event()


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if 'code' in params:
            auth_code = params['code'][0]
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<h2>Authorized! You can close this tab.</h2>')
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'No code received.')
        server_done.set()

    def log_message(self, *args):
        pass


def run():
    creds_path = Path(sys.argv[1]) if len(sys.argv) > 1 else CREDS_PATH

    if not creds_path.exists():
        print(f"\nCredentials file not found at {creds_path}")
        print("\nSteps to get it:")
        print("1. Go to console.cloud.google.com on your phone")
        print("2. Select your project (or create one)")
        print("3. APIs & Services → Credentials → Create Credentials → OAuth client ID")
        print("4. Application type: Desktop app")
        print(f"5. Copy the JSON content and paste it here, then press Enter twice:")
        lines = []
        while True:
            line = input()
            if line == '' and lines:
                break
            lines.append(line)
        raw = '\n'.join(lines).strip()
        creds_path.parent.mkdir(parents=True, exist_ok=True)
        creds_path.write_text(raw)
        print(f"Saved to {creds_path}")

    redirect_uri = f'http://{TAILSCALE_IP}:{REDIRECT_PORT}'

    flow = Flow.from_client_secrets_file(
        str(creds_path),
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )

    auth_url, _ = flow.authorization_url(
        access_type='offline',
        prompt='consent',
        include_granted_scopes='true'
    )

    print(f"\n{'='*60}")
    print("Open this URL on your phone and approve access:")
    print(f"\n{auth_url}\n")
    print(f"Waiting for Google to redirect back to the VPS...")
    print(f"{'='*60}\n")

    httpd = HTTPServer(('0.0.0.0', REDIRECT_PORT), CallbackHandler)
    t = threading.Thread(target=httpd.handle_request)
    t.start()
    server_done.wait(timeout=300)
    httpd.server_close()

    if not auth_code:
        print("Timed out waiting for authorization.")
        sys.exit(1)

    flow.fetch_token(code=auth_code)
    creds = flow.credentials

    TOKEN_PATH.write_text(json.dumps({
        'token': creds.token,
        'refresh_token': creds.refresh_token,
        'token_uri': creds.token_uri,
        'client_id': creds.client_id,
        'client_secret': creds.client_secret,
        'scopes': list(creds.scopes),
    }))

    print(f"Token saved to {TOKEN_PATH}")
    print("Google Calendar integration is ready.")


if __name__ == '__main__':
    run()
