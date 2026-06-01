#!/usr/bin/env bash
# Cloudflare quick-tunnel launcher for Pedro's WhatsApp webhook.
#
# Runs as a systemd service (see cloudflared-tunnel.service). On every (re)start
# it spins up a Cloudflare quick tunnel to the local webhook port, captures the
# freshly-assigned https URL, records it, syncs it into the bot's .env, and
# bounces nightshift so signature validation uses the live URL.
#
# NOTE: quick tunnels (trycloudflare.com) get a NEW random hostname every restart.
# That means after any reboot/crash-restart you must paste the new URL (found in
# tunnel-url.txt) into the Twilio WhatsApp Sandbox webhook config. See the runbook.
set -uo pipefail

NS_DIR="/home/gregnightshift/nightshift"
ENVFILE="$NS_DIR/.env"
URLFILE="$NS_DIR/tunnel-url.txt"
LOG="/var/log/cloudflared-pedro.log"
PORT="${WHATSAPP_WEBHOOK_PORT:-8770}"

: > "$LOG"

/usr/local/bin/cloudflared tunnel --no-autoupdate --url "http://localhost:${PORT}" >>"$LOG" 2>&1 &
CF_PID=$!

# Wait (up to ~60s) for cloudflared to print the assigned hostname.
URL=""
for _ in $(seq 1 30); do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)
  [ -n "$URL" ] && break
  # Bail early if cloudflared died.
  kill -0 "$CF_PID" 2>/dev/null || break
  sleep 2
done

if [ -n "$URL" ]; then
  echo "$URL" > "$URLFILE"
  chown gregnightshift:gregnightshift "$URLFILE" 2>/dev/null || true

  if grep -q '^WHATSAPP_PUBLIC_URL=' "$ENVFILE"; then
    sed -i "s#^WHATSAPP_PUBLIC_URL=.*#WHATSAPP_PUBLIC_URL=${URL}#" "$ENVFILE"
  else
    echo "WHATSAPP_PUBLIC_URL=${URL}" >> "$ENVFILE"
  fi

  # Pick up the new URL in the running bot (signature validation needs the match).
  systemctl restart nightshift.service || true
  echo "[tunnel.sh] live URL: ${URL} (synced to .env, nightshift restarted)" >> "$LOG"
else
  echo "[tunnel.sh] WARNING: no trycloudflare URL captured within timeout" >> "$LOG"
fi

# Hand the process back to systemd: stay attached to cloudflared for the
# service lifetime so Restart=always works.
wait "$CF_PID"
