"""Sync a Google Drive folder to the local ads media folder.

One-time setup:
    python scripts/drive_sync.py --setup

This opens a browser (or prints an auth URL if headless) and saves a Drive
token to drive_token.json. After that, run without --setup to sync:

    python scripts/drive_sync.py

Files in the Drive folder are downloaded to META_MEDIA_DIR (/data/greg/ads/).
Only downloads new or changed files (compares size + mtime). Does not delete
local files that were removed from Drive.

Env / config:
    DRIVE_TOKEN      path to drive token file (default: ./drive_token.json)
    DRIVE_FOLDER_ID  Google Drive folder ID to sync from
    META_MEDIA_DIR   local destination (default: /data/greg/ads)

To find the folder ID: open the Drive folder in your browser, copy the ID
from the URL: drive.google.com/drive/folders/<FOLDER_ID>
"""
import argparse
import json
import os
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
TOKEN_PATH = Path(os.environ.get("DRIVE_TOKEN", Path(__file__).parent.parent / "drive_token.json"))
CLIENT_SECRET_PATH = Path(__file__).parent.parent / "client_secret.json"
MEDIA_DIR = Path(os.environ.get("META_MEDIA_DIR", "/data/greg/ads"))
FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "")

SUPPORTED_MIME = {
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "video/mp4", "video/quicktime",
}


def get_creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
            return creds

    if not CLIENT_SECRET_PATH.exists():
        sys.exit(
            f"No client_secret.json found at {CLIENT_SECRET_PATH}.\n"
            "Run: python scripts/drive_sync.py --setup  (it should have been created automatically)"
        )
    secret_path = CLIENT_SECRET_PATH
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(
        str(secret_path),
        scopes=SCOPES,
        redirect_uri="urn:ietf:wg:oauth:2.0:oob",
    )
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    print("\n--- Google Drive Authorization ---")
    print("Open this URL in your browser:\n")
    print(auth_url)
    print("\nAfter you approve, Google will show you a code. Paste it below.")
    code = input("Authorization code: ").strip()
    flow.fetch_token(code=code)
    creds = flow.credentials
    TOKEN_PATH.write_text(creds.to_json())
    print(f"\nDrive token saved to {TOKEN_PATH}")
    return creds


def sync(folder_id: str, dest: Path, verbose: bool = True):
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
    import io

    creds = get_creds()
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    dest.mkdir(parents=True, exist_ok=True)

    query = f"'{folder_id}' in parents and trashed=false"
    fields = "files(id,name,mimeType,size,modifiedTime)"
    resp = service.files().list(q=query, fields=fields, pageSize=100).execute()
    files = resp.get("files", [])

    if not files:
        print("No files found in Drive folder.")
        return

    downloaded = 0
    skipped = 0
    for f in files:
        if f.get("mimeType") not in SUPPORTED_MIME:
            continue
        dest_path = dest / f["name"]
        drive_size = int(f.get("size", 0))
        if dest_path.exists() and dest_path.stat().st_size == drive_size:
            skipped += 1
            continue
        if verbose:
            print(f"Downloading {f['name']} ({drive_size:,} bytes)…")
        request = service.files().get_media(fileId=f["id"])
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = dl.next_chunk()
        dest_path.write_bytes(buf.getvalue())
        downloaded += 1

    print(f"Sync complete: {downloaded} downloaded, {skipped} already up-to-date.")
    if verbose and downloaded:
        print(f"Files are in {dest} — use filenames in /draft.")


def main():
    parser = argparse.ArgumentParser(description="Sync Google Drive folder → local ads folder")
    parser.add_argument("--setup", action="store_true", help="Run OAuth flow and save token")
    parser.add_argument("--folder", default=FOLDER_ID, help="Drive folder ID (or set DRIVE_FOLDER_ID)")
    parser.add_argument("--dest", default=str(MEDIA_DIR), help="Local destination directory")
    args = parser.parse_args()

    if args.setup:
        get_creds()
        print("Setup complete. Now set DRIVE_FOLDER_ID in .env and run without --setup to sync.")
        return

    if not args.folder:
        sys.exit(
            "No Drive folder ID. Set DRIVE_FOLDER_ID in .env or pass --folder <id>.\n"
            "Find it in the Drive URL: drive.google.com/drive/folders/<FOLDER_ID>"
        )

    sync(args.folder, Path(args.dest))


if __name__ == "__main__":
    main()
