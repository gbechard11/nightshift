"""Pedro's Google Calendar CLI.

Confirm-first scheduling: Pedro drafts an event, asks Greg to approve in
Telegram, then calls `create-event` to actually create it (and email invites
+ a Google Meet link). Reads OAuth creds from token.json (self-contained:
includes client id/secret/refresh token).

Env:
    GCAL_TOKEN   path to token.json (default: ./token.json)
    GCAL_TZ      default IANA timezone (default: America/Edmonton)

Examples:
    python gcal.py list --max 10
    python gcal.py create \\
        --summary "Intro call" \\
        --start 2026-06-03T14:00:00 --end 2026-06-03T14:30:00 \\
        --attendee jane@example.com --meet
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
]
TOKEN_PATH = Path(os.environ.get("GCAL_TOKEN", "token.json"))
DEFAULT_TZ = os.environ.get("GCAL_TZ", "America/Edmonton")


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
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def cmd_list(args):
    service = get_service()
    now = datetime.now(timezone.utc).isoformat()
    result = (
        service.events()
        .list(
            calendarId=args.calendar,
            timeMin=now,
            maxResults=args.max,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    events = result.get("items", [])
    if not events:
        print("(no upcoming events)")
        return
    for e in events:
        start = e["start"].get("dateTime", e["start"].get("date"))
        print(f'{start}  {e.get("summary", "(no title)")}  [{e["id"]}]')


def cmd_create(args):
    service = get_service()
    event = {
        "summary": args.summary,
        "start": {"dateTime": args.start, "timeZone": args.tz},
        "end": {"dateTime": args.end, "timeZone": args.tz},
    }
    if args.description:
        event["description"] = args.description
    if args.location:
        event["location"] = args.location
    if args.attendee:
        event["attendees"] = [{"email": a} for a in args.attendee]

    conference_version = 0
    if args.meet:
        event["conferenceData"] = {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
        conference_version = 1

    created = (
        service.events()
        .insert(
            calendarId=args.calendar,
            body=event,
            conferenceDataVersion=conference_version,
            sendUpdates="all" if args.attendee else "none",
        )
        .execute()
    )
    print(
        json.dumps(
            {
                "id": created["id"],
                "htmlLink": created.get("htmlLink"),
                "meetLink": created.get("hangoutLink"),
                "start": created["start"].get("dateTime"),
            },
            indent=2,
        )
    )


def cmd_delete(args):
    service = get_service()
    service.events().delete(
        calendarId=args.calendar, eventId=args.event_id, sendUpdates="all"
    ).execute()
    print(f"deleted {args.event_id}")


def main():
    parser = argparse.ArgumentParser(description="Pedro's Google Calendar CLI")
    parser.add_argument("--calendar", default="primary", help="calendar id")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list upcoming events")
    p_list.add_argument("--max", type=int, default=10)
    p_list.set_defaults(func=cmd_list)

    p_create = sub.add_parser("create", help="create an event")
    p_create.add_argument("--summary", required=True)
    p_create.add_argument("--start", required=True, help="ISO, e.g. 2026-06-03T14:00:00")
    p_create.add_argument("--end", required=True, help="ISO, e.g. 2026-06-03T14:30:00")
    p_create.add_argument("--tz", default=DEFAULT_TZ)
    p_create.add_argument("--description", default="")
    p_create.add_argument("--location", default="")
    p_create.add_argument("--attendee", action="append", help="repeatable")
    p_create.add_argument("--meet", action="store_true", help="add a Google Meet link")
    p_create.set_defaults(func=cmd_create)

    p_del = sub.add_parser("delete", help="delete an event by id")
    p_del.add_argument("--event-id", required=True)
    p_del.set_defaults(func=cmd_delete)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
