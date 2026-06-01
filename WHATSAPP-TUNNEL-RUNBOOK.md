# Pedro WhatsApp Tunnel — Runbook

How Pedro receives WhatsApp messages, and what to do if anything breaks.

## Architecture (current: Tailscale Funnel)

```
WhatsApp ──> Twilio Sandbox ──HTTPS POST──> Tailscale Funnel ──> localhost:8770 (bot.py webhook)
```

- The bot's webhook listens on `127.0.0.1:${WHATSAPP_WEBHOOK_PORT:-8770}` (not public).
- **Tailscale Funnel** exposes a *stable* public HTTPS endpoint and proxies it to
  that local port. The public URL is:

  ```
  https://nightshift-vps.tail6f5de5.ts.net/whatsapp
  ```

- Twilio is configured (in the Sandbox console) to POST inbound WhatsApp messages
  to that URL.

### Why Funnel

- **Stable hostname** — `nightshift-vps.tail6f5de5.ts.net` never changes (unlike
  the old Cloudflare quick tunnel, which got a new random hostname every restart).
  Set it in Twilio once; never touch it again.
- **Survives reboots** — `tailscaled` persists the Funnel config, so it comes back
  automatically after a reboot or crash. No per-reboot manual step.
- **No open inbound ports** — Funnel is outbound-only, so the VPS firewall stays
  locked to Tailscale-only (ufw allows just 22/tcp over `tailscale0`). SSH posture
  is unchanged.
- Automatic TLS (valid Let's Encrypt cert for the `.ts.net` name).

> Note: enabling Funnel publishes the hostname `nightshift-vps.tail6f5de5.ts.net`
> to the public Certificate Transparency log. That's expected and was approved.

## Common commands (run on the VPS)

```bash
# Is Funnel serving the webhook port?
sudo tailscale funnel status

# Tailscale daemon health
sudo tailscale status

# Confirm the public endpoint is up (should print: health HTTP 200)
curl -sS -o /dev/null -w "health HTTP %{http_code}\n" \
  https://nightshift-vps.tail6f5de5.ts.net/health

# Bot service
sudo systemctl status nightshift.service
journalctl -u nightshift.service -n 50 --no-pager
```

If Funnel ever needs to be re-pointed at the webhook port:

```bash
sudo tailscale funnel --bg 8770
```

## Relevant .env keys

```
WHATSAPP_WEBHOOK_PORT=8770
WHATSAPP_WEBHOOK_PATH=/whatsapp
WHATSAPP_PUBLIC_URL=https://nightshift-vps.tail6f5de5.ts.net
```

`WHATSAPP_PUBLIC_URL` must match the URL configured in Twilio exactly — it's used
for Twilio HMAC-SHA1 signature validation. If it's unset/mismatched, the webhook
falls back to allowlist-only (`ALLOWED_WHATSAPP`), so a mismatch just means
messages get rejected, nothing leaks.

## Troubleshooting

**Pedro stops replying on WhatsApp:**

1. Check the public endpoint: the `curl .../health` above should return `200`.
   - If it fails: `sudo tailscale status` (is the node up?) and
     `sudo tailscale funnel status` (is it still proxying 8770?). Re-run
     `sudo tailscale funnel --bg 8770` if needed.
2. Check the bot: `sudo systemctl status nightshift.service`; restart with
   `sudo systemctl restart nightshift.service` if it's down.
3. Check Twilio: Console → Messaging → Try it out → WhatsApp Sandbox Settings →
   "When a message comes in" should be
   `https://nightshift-vps.tail6f5de5.ts.net/whatsapp` (method **POST**).

**Signature-validation rejects (403) but health is 200:**
Make sure `WHATSAPP_PUBLIC_URL` in `.env` exactly matches the Twilio webhook URL
(no trailing slash mismatch, https, correct host), then restart nightshift.

## History / superseded approach

The first iteration used a **Cloudflare quick tunnel** (`trycloudflare.com`) run
as `cloudflared-tunnel.service`. Quick tunnels get a new random hostname on every
restart, which required pasting the new URL into Twilio after every reboot. That
service has been **disabled** in favour of Tailscale Funnel. The old unit and the
`scripts/tunnel.sh` launcher remain in the repo for reference but are no longer
active.
