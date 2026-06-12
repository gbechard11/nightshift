#!/usr/bin/env bash
# Auto-poster — runs hourly, posts any calendar entries that are now due.
# Silently exits if no posts are due. Sends Telegram notification per post.
# Cron: 30 * * * *  (every hour at :30)
#
# NOTE: Auto-posting only works once Meta tokens have pages_manage_posts +
# instagram_content_publish permissions. Until then, posts stay pending and
# the daily briefing (social_brief.sh) sends them for manual posting.
set -euo pipefail

PY=/home/gregnightshift/nightshift/.venv/bin/python
SOCIAL=/home/gregnightshift/nightshift/scripts/social.py
SEND=/home/gregnightshift/nightshift/scripts/telegram_send.py

set -a
. /home/gregnightshift/nightshift/.env
set +a

# Find posts that are due (scheduled_for in the past, status=pending)
DUE_IDS=$(
    "$PY" - <<'EOF'
import json, sys
from pathlib import Path
from datetime import datetime, timezone

cal_file = Path("/data/greg/social/calendar.json")
if not cal_file.exists():
    sys.exit(0)

posts = json.loads(cal_file.read_text())
now = datetime.now(tz=timezone.utc)

for p in posts:
    if p["status"] != "pending":
        continue
    try:
        scheduled = datetime.fromisoformat(p["scheduled_for"])
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        if scheduled <= now:
            print(p["id"])
    except Exception:
        pass
EOF
)

if [ -z "$DUE_IDS" ]; then
    exit 0
fi

while IFS= read -r post_id; do
    [ -z "$post_id" ] && continue

    OUT="$("$PY" "$SOCIAL" post-now "$post_id" 2>&1 || true)"

    # Notify Greg if something posted (or failed)
    if echo "$OUT" | grep -q "✓ Posted"; then
        "$PY" "$SEND" --to greg --msg "📤 Auto-posted: $OUT" 2>/dev/null || true
    elif echo "$OUT" | grep -q "✗"; then
        "$PY" "$SEND" --to greg --msg "⚠️ Social post failed [$post_id]: $OUT" 2>/dev/null || true
    elif echo "$OUT" | grep -q "copy for manual posting"; then
        # Token lacks permissions — brief already handles this, stay quiet
        :
    fi
done <<< "$DUE_IDS"
