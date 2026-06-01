#!/usr/bin/env python3
"""
Google Calendar CLI for Pedro/Claude.

Usage:
    gcal.py --list                                          # next 14 days
    gcal.py --list --days 7                                 # next 7 days
    gcal.py --calendars                                     # list all calendars
    gcal.py --add --title "Call" --start "2026-06-01 10:00" --end "2026-06-01 11:00"
    gcal.py --add --title "Call" --start "2026-06-01 10:00" --end "2026-06-01 11:00" \
            --attendees "client@example.com,partner@example.com" --meet
    gcal.py --cancel <eventId>                              # cancel + notify attendees
    gcal.py --delete <eventId>                              # hard delete, no notification
    gcal.py --get <eventId>
"""
import sys
sys.path.insert(0, '/data/greg/lib')

import argparse
import json
import os
import uuid
from datetime import datetime, timezone, timedelta

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

TOKEN_FILE = os.path.join(os.path.dirname(__file__), '..', 'google-token.json')
SCOPES = ['https://www.googleapis.com/auth/calendar']
DEFAULT_TIMEZONE = 'America/Edmonton'


def get_service():
    if not os.path.exists(TOKEN_FILE):
        print(f"ERROR: token file not found: {TOKEN_FILE}", file=sys.stderr)
        print("Run calendar_auth.py first to authenticate.", file=sys.stderr)
        sys.exit(1)

    with open(TOKEN_FILE) as f:
        data = json.load(f)

    creds = Credentials(
        token=data.get('token'),
        refresh_token=data['refresh_token'],
        token_uri=data['token_uri'],
        client_id=data['client_id'],
        client_secret=data['client_secret'],
        scopes=data['scopes'],
    )

    if creds.expired or not creds.valid:
        creds.refresh(Request())
        data['token'] = creds.token
        with open(TOKEN_FILE, 'w') as f:
            json.dump(data, f, indent=2)

    return build('calendar', 'v3', credentials=creds)


def parse_datetime(s):
    """Parse human-friendly datetime string to RFC3339. Assumes Edmonton time if no tz."""
    if 'T' in s and ('+' in s or s.endswith('Z')):
        return s, False

    formats = [
        '%Y-%m-%d %H:%M',
        '%Y-%m-%dT%H:%M',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d',
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            all_day = fmt == '%Y-%m-%d'
            if all_day:
                return s, True
            return dt.strftime('%Y-%m-%dT%H:%M:%S') + '-06:00', False
        except ValueError:
            continue
    raise ValueError(f"Cannot parse datetime: {s}")


def cmd_list(service, args):
    cal_id = args.calendar or 'primary'
    days = args.days or 14
    now = datetime.now(timezone.utc)
    time_max = now + timedelta(days=days)

    results = service.events().list(
        calendarId=cal_id,
        timeMin=now.isoformat(),
        timeMax=time_max.isoformat(),
        maxResults=args.limit or 25,
        singleEvents=True,
        orderBy='startTime',
    ).execute()

    events = results.get('items', [])
    if not events:
        print(json.dumps([]))
        return

    out = []
    for e in events:
        start = e['start'].get('dateTime', e['start'].get('date'))
        end = e['end'].get('dateTime', e['end'].get('date'))
        meet_link = ''
        if 'conferenceData' in e:
            for ep in e['conferenceData'].get('entryPoints', []):
                if ep.get('entryPointType') == 'video':
                    meet_link = ep.get('uri', '')
                    break
        attendees = [a.get('email') for a in e.get('attendees', [])]
        out.append({
            'id': e['id'],
            'title': e.get('summary', '(no title)'),
            'start': start,
            'end': end,
            'location': e.get('location', ''),
            'description': e.get('description', ''),
            'meet_link': meet_link,
            'attendees': attendees,
            'status': e.get('status', ''),
            'calendar': cal_id,
            'url': e.get('htmlLink', ''),
        })
    print(json.dumps(out, indent=2))


def cmd_calendars(service, args):
    result = service.calendarList().list().execute()
    cals = []
    for c in result.get('items', []):
        cals.append({
            'id': c['id'],
            'name': c.get('summary', ''),
            'primary': c.get('primary', False),
            'timezone': c.get('timeZone', ''),
        })
    print(json.dumps(cals, indent=2))


def cmd_add(service, args):
    cal_id = args.calendar or 'primary'

    start_str, start_allday = parse_datetime(args.start)
    end_str, end_allday = parse_datetime(args.end)

    if start_allday:
        start_obj = {'date': start_str}
        end_obj = {'date': end_str}
    else:
        start_obj = {'dateTime': start_str, 'timeZone': DEFAULT_TIMEZONE}
        end_obj = {'dateTime': end_str, 'timeZone': DEFAULT_TIMEZONE}

    body = {
        'summary': args.title,
        'start': start_obj,
        'end': end_obj,
    }

    if args.desc:
        body['description'] = args.desc
    if args.location:
        body['location'] = args.location

    if args.attendees:
        emails = [e.strip() for e in args.attendees.split(',') if e.strip()]
        body['attendees'] = [{'email': e} for e in emails]

    conference_data_version = 0
    if args.meet:
        body['conferenceData'] = {
            'createRequest': {
                'requestId': str(uuid.uuid4()),
                'conferenceSolutionKey': {'type': 'hangoutsMeet'},
            }
        }
        conference_data_version = 1

    send_updates = 'all' if args.attendees else 'none'

    event = service.events().insert(
        calendarId=cal_id,
        body=body,
        conferenceDataVersion=conference_data_version,
        sendUpdates=send_updates,
    ).execute()

    meet_link = ''
    if 'conferenceData' in event:
        for ep in event['conferenceData'].get('entryPoints', []):
            if ep.get('entryPointType') == 'video':
                meet_link = ep.get('uri', '')
                break

    print(json.dumps({
        'id': event['id'],
        'title': event.get('summary'),
        'start': event['start'].get('dateTime', event['start'].get('date')),
        'end': event['end'].get('dateTime', event['end'].get('date')),
        'meet_link': meet_link,
        'attendees': [a.get('email') for a in event.get('attendees', [])],
        'url': event.get('htmlLink', ''),
        'invites_sent': send_updates == 'all',
    }, indent=2))


def cmd_cancel(service, args):
    """Cancel an event — updates status to 'cancelled' and notifies attendees."""
    cal_id = args.calendar or 'primary'
    event = service.events().get(calendarId=cal_id, eventId=args.cancel).execute()
    event['status'] = 'cancelled'
    updated = service.events().update(
        calendarId=cal_id,
        eventId=args.cancel,
        body=event,
        sendUpdates='all',
    ).execute()
    print(json.dumps({
        'id': updated['id'],
        'title': updated.get('summary'),
        'status': updated.get('status'),
        'cancellation_sent': True,
    }, indent=2))


def cmd_delete(service, args):
    """Hard delete — no cancellation notice sent to attendees."""
    cal_id = args.calendar or 'primary'
    service.events().delete(calendarId=cal_id, eventId=args.delete).execute()
    print(json.dumps({'deleted': args.delete}))


def cmd_get(service, args):
    cal_id = args.calendar or 'primary'
    e = service.events().get(calendarId=cal_id, eventId=args.get).execute()
    start = e['start'].get('dateTime', e['start'].get('date'))
    end = e['end'].get('dateTime', e['end'].get('date'))
    meet_link = ''
    if 'conferenceData' in e:
        for ep in e['conferenceData'].get('entryPoints', []):
            if ep.get('entryPointType') == 'video':
                meet_link = ep.get('uri', '')
                break
    print(json.dumps({
        'id': e['id'],
        'title': e.get('summary', '(no title)'),
        'start': start,
        'end': end,
        'location': e.get('location', ''),
        'description': e.get('description', ''),
        'meet_link': meet_link,
        'attendees': [a.get('email') for a in e.get('attendees', [])],
        'status': e.get('status', ''),
        'url': e.get('htmlLink', ''),
    }, indent=2))


def main():
    parser = argparse.ArgumentParser(description='Google Calendar CLI')
    parser.add_argument('--list', action='store_true', help='List upcoming events')
    parser.add_argument('--calendars', action='store_true', help='List all calendars')
    parser.add_argument('--add', action='store_true', help='Create an event')
    parser.add_argument('--cancel', metavar='EVENT_ID', help='Cancel event and notify attendees')
    parser.add_argument('--delete', metavar='EVENT_ID', help='Hard delete (no notifications)')
    parser.add_argument('--get', metavar='EVENT_ID', help='Get a single event by ID')

    parser.add_argument('--title', help='Event title')
    parser.add_argument('--start', help='Start datetime, e.g. "2026-06-01 14:00"')
    parser.add_argument('--end', help='End datetime')
    parser.add_argument('--desc', help='Event description')
    parser.add_argument('--location', help='Event location')
    parser.add_argument('--attendees', help='Comma-separated attendee emails — invites sent automatically')
    parser.add_argument('--meet', action='store_true', help='Add a Google Meet link')
    parser.add_argument('--calendar', help='Calendar ID (default: primary)')
    parser.add_argument('--days', type=int, help='Days ahead to list (default: 14)')
    parser.add_argument('--limit', type=int, help='Max events to return (default: 25)')

    args = parser.parse_args()

    try:
        service = get_service()

        if args.list:
            cmd_list(service, args)
        elif args.calendars:
            cmd_calendars(service, args)
        elif args.add:
            if not args.title or not args.start or not args.end:
                print("ERROR: --add requires --title, --start, and --end", file=sys.stderr)
                sys.exit(1)
            cmd_add(service, args)
        elif args.cancel:
            cmd_cancel(service, args)
        elif args.delete:
            cmd_delete(service, args)
        elif args.get:
            cmd_get(service, args)
        else:
            parser.print_help()

    except HttpError as e:
        print(json.dumps({'error': str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
