"""Re-run the Google consent flow to get a token.json with calendar + full
drive scopes (drive write is needed so Pedro can create folders and upload
files, not just read).

The VPS is headless, so this binds a local server on a FIXED port and you
complete consent from YOUR machine over an SSH tunnel:

    # 1. On your laptop/desktop, open an SSH session that forwards the port:
    ssh -L 8765:localhost:8765 nightshift

    # 2. In that session:
    cd ~/nightshift && .venv/bin/python reauth_token.py

    # 3. It prints an authorization URL. Open it in your browser, pick the
    #    Google account, approve calendar + Drive. The browser redirects to
    #    http://localhost:8765/ (tunneled to the VPS) and the flow completes.

token.json is overwritten with the new scopes. gcal.py and gdrive.py both
keep working (full drive supersedes the old read-only scope).
"""
from google_auth_oauthlib.flow import InstalledAppFlow
from pathlib import Path

PORT = 8765
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
]

flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
creds = flow.run_local_server(
    host="localhost",
    port=PORT,
    open_browser=False,
    authorization_prompt_message=(
        "Open this URL in your browser to authorize:\n\n{url}\n"
    ),
    success_message="Authorized. You can close this tab and return to the terminal.",
)
Path("token.json").write_text(creds.to_json())
print("token.json updated with scopes:", SCOPES)
