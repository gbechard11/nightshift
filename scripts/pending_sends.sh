#!/usr/bin/env bash
# Auto-send the queued Pawn Shop emails once GreenGeeks SMTP is reachable again
# (it blocked the sender after today's runaway). Runs every 5 min via cron.
#
# State machine, each stage gated + idempotent:
#   1. apology (494)  -> send first, must land before the big blast
#   2. bb1024 (47K)   -> clean re-send, launched detached, skips the 524 already
#                        reached (ledger seeded)
#   3. health watch    -> if the running bb1024 is failing >70% of sends
#                        (GreenGeeks throttling again), pause it and ping Greg
# Safe: flock (no overlap), blast.py ledger-dedup (no double-send), low-vol
# apology, auto-abort on the big one.
set -uo pipefail

NS=/home/gregnightshift/nightshift
PY=$NS/.venv/bin/python
Q=/data/greg/blast_queue
LOCK=$Q/.pending-sends.lock
AP_SENT=$Q/.apology-c49273.sent
BB_STARTED=$Q/.bb1024.started
TG="$PY $NS/scripts/telegram_send.py --to greg --msg"

exec 9>"$LOCK"; flock -n 9 || exit 0

# SMTP reachable again? (bare TCP connect, 8s) — exit quietly if still blocked.
"$PY" - <<'PYEOF' || exit 0
import socket, sys
socket.setdefaulttimeout(8)
try:
    socket.create_connection(("mail.pawnshop-live.ca", 465), 8).close()
except Exception:
    sys.exit(1)
PYEOF

# delivered/failed counts for a campaign (ignores seeded skip-rows)
counts() {
"$PY" - "$1" <<'PYEOF'
import json, sys
ok = fail = 0
try:
    for l in open("/home/gregnightshift/nightshift/blast-ledger/%s.jsonl" % sys.argv[1]):
        try: d = json.loads(l)
        except Exception: continue
        if str(d.get("note", "")).startswith("seed"): continue
        if d.get("ok"): ok += 1
        else: fail += 1
except FileNotFoundError:
    pass
print(ok, fail)
PYEOF
}

# ---- STAGE 1: apology (must deliver before the big blast starts) ----
if [ ! -f "$AP_SENT" ]; then
    logger -t ns-pending-sends "SMTP up — sending apology"
    "$PY" "$NS/scripts/blast.py" --list "$Q/apology_recipients.csv" --channel email \
        --from gm@pawnshop-live.ca --subject "Sorry for the repeat emails" \
        --html-file "$Q/apology-c49273.html" --campaign apology-c49273 --yes \
        >> "$Q/apology_send.log" 2>&1
    read ok fail < <(counts apology-c49273)
    if [ "${ok:-0}" -ge 1 ]; then
        { date "+%F %T"; echo "$ok delivered, $fail failed"; } > "$AP_SENT"
        $TG "✅ Apology sent to $ok Pawn Shop customers ($fail failed). Starting the clean 47K re-send now." >/dev/null 2>&1 || true
    else
        logger -t ns-pending-sends "apology still 100% failing — SMTP not truly up; will retry"
        exit 0
    fi
fi

# ---- STAGE 2: bb1024 clean re-send, launched detached once apology landed ----
if [ ! -f "$BB_STARTED" ]; then
    st=$("$PY" -c "import json;print(json.load(open('$Q/emp-edmonton-bb1024.json'))['status'])" 2>/dev/null || echo "?")
    if [ "$st" = "sent" ]; then date "+%F %T" > "$BB_STARTED"; exit 0; fi
    logger -t ns-pending-sends "starting bb1024 clean re-send (detached)"
    nohup "$PY" "$NS/scripts/blast_queue.py" send emp-edmonton-bb1024 --yes \
        >> "$Q/bb1024_send.log" 2>&1 &
    date "+%F %T" > "$BB_STARTED"
    $TG "📣 Clean 47K Pawn Shop re-send started (skips the 524 already reached). I'll auto-pause it if GreenGeeks starts throttling again." >/dev/null 2>&1 || true
    exit 0
fi

# ---- STAGE 3: health watch on the running bb1024 ----
if [ ! -f "$BB_STARTED.aborted" ] && pgrep -f "emp-edmonton-bb1024" >/dev/null; then
    read ok fail < <(counts emp-edmonton-bb1024)
    tot=$((ok + fail))
    if [ "$tot" -ge 60 ] && [ "$fail" -gt $((tot * 7 / 10)) ]; then
        pkill -f "emp-edmonton-bb1024" 2>/dev/null || true
        pkill -f "blast_queue.py send" 2>/dev/null || true
        date "+%F %T" > "$BB_STARTED.aborted"
        $TG "⚠️ Paused the 47K re-send — GreenGeeks is failing $fail of $tot sends (throttling again). $ok delivered so far. It needs a real ESP; tell me to resume or switch sender." >/dev/null 2>&1 || true
    fi
fi
