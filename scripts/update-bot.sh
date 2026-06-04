#!/usr/bin/env bash
# Hourly auto-deploy: pull main from GitHub and restart nightshift if anything
# changed. Run by gregnightshift via cron.

set -euo pipefail

cd /home/gregnightshift/nightshift

LOCAL=$(git rev-parse HEAD)
git fetch origin main --quiet
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi

CHANGED=$(git diff --name-only "$LOCAL" "$REMOTE" || true)
git reset --hard origin/main --quiet

# If requirements changed, update venv
if echo "$CHANGED" | grep -q '^requirements.txt$'; then
    ./.venv/bin/pip install --quiet -r requirements.txt
fi

# If unit file changed, reinstall + daemon-reload
if echo "$CHANGED" | grep -q '^nightshift.service$'; then
    sudo cp nightshift.service /etc/systemd/system/nightshift.service
    sudo systemctl daemon-reload
fi

# If employee unit file changed, reinstall + daemon-reload
if echo "$CHANGED" | grep -q '^nightshift-employees.service$'; then
    sudo cp nightshift-employees.service /etc/systemd/system/nightshift-employees.service
    sudo systemctl daemon-reload
fi

# If mcp unit file changed, reinstall + daemon-reload
if echo "$CHANGED" | grep -q '^nightshift-mcp.service$'; then
    sudo cp nightshift-mcp.service /etc/systemd/system/nightshift-mcp.service
    sudo systemctl daemon-reload
fi

sudo systemctl restart nightshift
sudo systemctl restart nightshift-employees 2>/dev/null || true
sudo systemctl restart nightshift-mcp 2>/dev/null || true

logger -t nightshift-update "Deployed $LOCAL -> $REMOTE. Changed: $(echo "$CHANGED" | tr '\n' ' ')"
