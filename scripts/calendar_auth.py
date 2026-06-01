#!/usr/bin/env python3
"""
One-time OAuth2 auth flow for Google Calendar API.
Run this locally (on a machine with a browser), then copy google-token.json to the VPS.

Usage:
    python3 calendar_auth.py --credentials /path/to/client_secret.json
    python3 calendar_auth.py --credentials /path/to/client_secret.json --token /path/to/output-token.json
"""
import sys
sys.path.insert(0, '/data/greg/lib')

import argparse
import json
import os

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ['https://www.googleapis.com/auth/calendar']


def main():
    parser = argparse.ArgumentParser(description='Authorize Google Calendar API access')
    parser.add_argument('--credentials', required=True, help='Path to client_secret.json downloaded from Google Cloud Console')
    parser.add_argument('--token', default='google-token.json', help='Output token file (default: google-token.json)')
    args = parser.parse_args()

    if not os.path.exists(args.credentials):
        print(f"ERROR: credentials file not found: {args.credentials}")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(args.credentials, SCOPES)

    # run_local_server opens a browser tab and handles the redirect on localhost
    creds = flow.run_local_server(port=0)

    token_data = {
        'token': creds.token,
        'refresh_token': creds.refresh_token,
        'token_uri': creds.token_uri,
        'client_id': creds.client_id,
        'client_secret': creds.client_secret,
        'scopes': list(creds.scopes),
    }

    with open(args.token, 'w') as f:
        json.dump(token_data, f, indent=2)

    print(f"\nSuccess! Token saved to: {args.token}")
    print(f"Copy this file to the VPS: /home/gregnightshift/nightshift/google-token.json")
    print(f"  refresh_token: {creds.refresh_token[:20]}...")


if __name__ == '__main__':
    main()
