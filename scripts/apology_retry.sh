#!/usr/bin/env bash
# Auto-send the c49273 apology (494 recipients) as soon as GreenGeeks SMTP is
# reachable again — it's currently blocking the sending IP after today's bulk
# burst. Runs every 15 min via cron; self-disables once the apology is sent.
#
# Safe by construction:
#  - low volume (494 at 30/min) — won't re-trip the bulk block
#  - blast.py is ledger-deduped on campaign apology-c49273, so even a double
#    run never re-emails anyone already reached
#  - exits immediately if the sentinel exists or SMTP is still blocked
#  - flock so two cron ticks can't overlap
set -uo pipefail

NS=/home/gregnightshift/nightshift
PY=$NS/.venv/bin/python
SENTINEL=/data/greg/blast_queue/.apology-c49273.sent
LOCK=/data/greg/blast_queue/.apology-retry.lock
LIST=/data/greg/blast_queue/apology_recipients.csv
HTML=/data/greg/blast_queue/apology-c49273.html

exec 9>"$LOCK"; flock -n 9 || exit 0
[ -f "$SENTINEL" ] && exit 0

# Is the SMTP server reachable again? (bare TCP connect, 8s)
"$PY" - <<'PYEOF' || exit 0
import socket, sys
socket.setdefaulttimeout(8)
try:
    socket.create_connection(("mail.pawnshop-live.ca", 465), 8).close()
except Exception:
    sys.exit(1)
PYEOF

logger -t ns-apology-retry "SMTP reachable — sending c49273 apology (494)"
"$PY" "$NS/scripts/blast.py" --list "$LIST" --channel email \
    --from gm@pawnshop-live.ca --subject "Sorry for the repeat emails" \
    --html-file "$HTML" --campaign apology-c49273 --yes \
    >> /data/greg/blast_queue/apology_send.log 2>&1
rc=$?

# Count how many actually delivered; only stamp done + notify if some did.
DELIV=$("$PY" - <<'PYEOF'
import json
ok=0
try:
    for l in open("/home/gregnightshift/nightshift/blast-ledger/apology-c49273.jsonl"):
        try: d=json.loads(l)
        except: continue
        if d.get("ok"): ok+=1
except FileNotFoundError:
    pass
print(ok)
PYEOF
)

if [ "${DELIV:-0}" -ge 1 ]; then
    date "+%Y-%m-%dT%H:%M:%S" > "$SENTINEL"
    echo "$DELIV" >> "$SENTINEL"
    logger -t ns-apology-retry "apology sent: $DELIV delivered — disabling retry"
    "$PY" "$NS/scripts/telegram_send.py" --to greg \
        --msg "✅ The Pawn Shop apology email just went out to $DELIV customers — GreenGeeks SMTP recovered and the auto-retry fired it. (The 47K clean re-send is still held pending your ESP decision.)" >/dev/null 2>&1 || true
fi
