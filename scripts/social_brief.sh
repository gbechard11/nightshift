#!/usr/bin/env bash
# Daily social media brief — DM'd to Greg via Telegram.
# Runs each morning. Shows posts due today + upcoming this week.
# Cron: 15 15 * * *  (9am MDT / 3:15pm UTC)
set -euo pipefail

PY=/home/gregnightshift/nightshift/.venv/bin/python
SOCIAL=/home/gregnightshift/nightshift/scripts/social.py
SEND=/home/gregnightshift/nightshift/scripts/telegram_send.py

set -a
. /home/gregnightshift/nightshift/.env
set +a

OUT="$("$PY" "$SOCIAL" briefing 2>/dev/null || true)"
[ -z "$OUT" ] && OUT="📱 Social brief: nothing scheduled for today."

"$PY" "$SEND" --to greg --msg "$OUT"
