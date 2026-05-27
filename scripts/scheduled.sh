#!/usr/bin/env bash
# Run a prompt through Claude on the VPS and post the answer to Greg's Telegram.
#
# Usage:
#   scheduled.sh "your prompt here"
#
# Add to crontab via `crontab -e` while ssh'd in as gregnightshift, e.g.:
#   0 14 * * *  /home/gregnightshift/nightshift/scripts/scheduled.sh "Brief me on today: weather in Edmonton, Pawn Shop Live shows in the next 7 days, anything notable in EDM tour news from the last 24h."
#   30 9 * * 1  /home/gregnightshift/nightshift/scripts/scheduled.sh "Monday recap: ticket sales for the upcoming weekend across both venues."
#
# Cron uses VPS time (UTC). Edmonton is UTC-6 (MST) or UTC-7 (MDT).
# 8am Edmonton MDT = 14:00 UTC. 8am MST = 15:00 UTC.

set -euo pipefail

PROMPT="${1:?prompt required as first argument}"

# Pull bot token + chat id from the same .env the systemd unit uses
set -a
. /home/gregnightshift/nightshift/.env
set +a

WORKDIR="${CLAUDE_WORKDIR:-/data/greg}"
CHAT="${ALLOWED_USERS%%,*}"  # first allowed user id

# Run claude. Capture stdout+stderr together; preserve exit code via fallback.
RESPONSE=$(
    cd "$WORKDIR" && \
    /usr/bin/claude --permission-mode bypassPermissions -p "$PROMPT" 2>&1
) || RESPONSE="(claude error: exit $?)
${RESPONSE:-}"

if [ -z "${RESPONSE// }" ]; then
    RESPONSE="(empty response)"
fi

if [ ${#RESPONSE} -gt 4000 ]; then
    TMP=$(mktemp --suffix=.txt)
    printf '%s\n' "$RESPONSE" > "$TMP"
    curl -s -F "chat_id=${CHAT}" \
         -F "document=@${TMP}" \
         -F "caption=📋 ${PROMPT:0:100}" \
         "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendDocument" \
         > /dev/null
    rm -f "$TMP"
else
    curl -s -X POST \
         "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
         --data-urlencode "chat_id=${CHAT}" \
         --data-urlencode "text=${RESPONSE}" \
         > /dev/null
fi
