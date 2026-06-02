"""Pedro's Google Drive CLI (read + write).

Pulls and pushes files for the shared Drive folders Pedro works with (Meta-ad
media, Summer Rap marketing assets, etc.). Read: list, search, download. Write:
create folders, upload files. No delete by design.

Reads OAuth creds from token.json (self-contained: includes client
id/secret/refresh token). Same token as gcal.py once the consent flow has been
re-run with the calendar + drive scopes.

Env:
    GCAL_TOKEN   path to token.json (default: ./token.json)
    GDRIVE_FOLDER default folder id for list/find (optional)

Examples:
    python gdrive.py list --folder <FOLDER_ID>
    python gdrive.py find --name banner --folder <FOLDER_ID>
    python gdrive.py download --file-id <FILE_ID> --out ./creative.mp4
    python gdrive.py mkdir --name "WEBSITE REVISED" --parent <FOLDER_ID>
    python gdrive.py upload --file ./revised.png --parent <FOLDER_ID>
"""

import argparse
import io
import mimetypes
import os
import sys
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
]
TOKEN_PATH = Path(os.environ.get("GCAL_TOKEN", "token.json"))
DEFAULT_FOLDER = os.environ.get("GDRIVE_FOLDER", "")

# Shared-folder/Shared-Drive support flags, applied to every call.
SHARED = {"supportsAllDrives": True, "includeItemsFromAllDrives": True}
FOLDER_MIME = "application/vnd.google-apps.folder"


def get_service():
    if not TOKEN_PATH.is_file():
        sys.exit(f"token not found at {TOKEN_PATH} (set GCAL_TOKEN)")
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        else:
            sys.exit("token invalid and not refreshable; re-run the consent flow")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _print_files(files):
    if not files:
        print("(no files)")
        return
    for f in files:
        size = f.get("size", "-")
        print(f'{f["id"]}  {f.get("mimeType", "?"):<28}  {size:>12}  {f["name"]}')


def cmd_list(args):
    service = get_service()
    folder = args.folder or DEFAULT_FOLDER
    if not folder:
        sys.exit("no folder given (--folder or GDRIVE_FOLDER)")
    q = f"'{folder}' in parents and trashed = false"
    result = (
        service.files()
        .list(
            q=q,
            pageSize=args.max,
            fields="files(id, name, mimeType, size, modifiedTime)",
            orderBy="modifiedTime desc",
            **SHARED,
        )
        .execute()
    )
    _print_files(result.get("files", []))


def cmd_find(args):
    service = get_service()
    parts = [f"name contains '{args.name}'", "trashed = false"]
    folder = args.folder or DEFAULT_FOLDER
    if folder:
        parts.append(f"'{folder}' in parents")
    q = " and ".join(parts)
    result = (
        service.files()
        .list(
            q=q,
            pageSize=args.max,
            fields="files(id, name, mimeType, size, modifiedTime)",
            orderBy="modifiedTime desc",
            **SHARED,
        )
        .execute()
    )
    _print_files(result.get("files", []))


def cmd_download(args):
    service = get_service()
    out = Path(args.out)
    request = service.files().get_media(fileId=args.file_id, **{"supportsAllDrives": True})
    buf = io.FileIO(str(out), "wb")
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
        if status:
            print(f"  {int(status.progress() * 100)}%", end="\r")
    buf.close()
    print(f"saved {out} ({out.stat().st_size} bytes)")


def cmd_mkdir(args):
    """Create a folder. If --parent is given, create it inside that folder."""
    service = get_service()
    parent = args.parent or DEFAULT_FOLDER
    body = {"name": args.name, "mimeType": FOLDER_MIME}
    if parent:
        body["parents"] = [parent]
    folder = (
        service.files()
        .create(body=body, fields="id, name, parents", **{"supportsAllDrives": True})
        .execute()
    )
    print(f'created folder {folder["id"]}  {folder["name"]}'
          + (f'  (in {parent})' if parent else ""))


def cmd_upload(args):
    """Upload a local file into a folder (or update an existing file by id)."""
    service = get_service()
    src = Path(args.file)
    if not src.is_file():
        sys.exit(f"file not found: {src}")
    name = args.name or src.name
    mime = mimetypes.guess_type(src.name)[0] or "application/octet-stream"
    media = MediaFileUpload(str(src), mimetype=mime, resumable=True)

    if args.file_id:
        # Update the contents of an existing Drive file (keeps its id/links).
        meta = (
            service.files()
            .update(fileId=args.file_id, media_body=media, fields="id, name",
                    **{"supportsAllDrives": True})
            .execute()
        )
        print(f'updated {meta["id"]}  {meta["name"]}')
        return

    parent = args.parent or DEFAULT_FOLDER
    body = {"name": name}
    if parent:
        body["parents"] = [parent]
    meta = (
        service.files()
        .create(body=body, media_body=media, fields="id, name, parents",
                **{"supportsAllDrives": True})
        .execute()
    )
    print(f'uploaded {meta["id"]}  {meta["name"]}'
          + (f'  (in {parent})' if parent else ""))


def main():
    parser = argparse.ArgumentParser(description="Pedro's Google Drive CLI (read + write)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list files in a folder")
    p_list.add_argument("--folder", default="", help="folder id (or set GDRIVE_FOLDER)")
    p_list.add_argument("--max", type=int, default=50)
    p_list.set_defaults(func=cmd_list)

    p_find = sub.add_parser("find", help="search files by name substring")
    p_find.add_argument("--name", required=True)
    p_find.add_argument("--folder", default="", help="restrict to this folder id")
    p_find.add_argument("--max", type=int, default=50)
    p_find.set_defaults(func=cmd_find)

    p_dl = sub.add_parser("download", help="download a file by id")
    p_dl.add_argument("--file-id", required=True)
    p_dl.add_argument("--out", required=True, help="local output path")
    p_dl.set_defaults(func=cmd_download)

    p_mk = sub.add_parser("mkdir", help="create a folder")
    p_mk.add_argument("--name", required=True, help="new folder name")
    p_mk.add_argument("--parent", default="", help="parent folder id (or GDRIVE_FOLDER)")
    p_mk.set_defaults(func=cmd_mkdir)

    p_up = sub.add_parser("upload", help="upload a local file into a folder")
    p_up.add_argument("--file", required=True, help="local file to upload")
    p_up.add_argument("--parent", default="", help="destination folder id (or GDRIVE_FOLDER)")
    p_up.add_argument("--name", default="", help="name in Drive (default: local filename)")
    p_up.add_argument("--file-id", default="", help="update this existing file instead of creating new")
    p_up.set_defaults(func=cmd_upload)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
