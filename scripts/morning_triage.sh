#!/usr/bin/env bash
# Daily inbox attention-triage brief, DM'd to Greg via the NS bot.
# READ-ONLY: surfaces unanswered real-person mail; deletes/moves nothing.
# Installed in gregnightshift's crontab.
set -euo pipefail

PY=/home/gregnightshift/nightshift/.venv/bin/python
TRIAGE=/home/gregnightshift/nightshift/scripts/attention_triage.py
SEND=/home/gregnightshift/nightshift/scripts/telegram_send.py

OUT="$("$PY" "$TRIAGE" --brief 2>/dev/null || true)"
[ -z "$OUT" ] && OUT="🗂️ Attention triage: no output this morning (pipeline may need a look)."

"$PY" "$SEND" --to greg --msg "$OUT"
