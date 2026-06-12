#!/usr/bin/env bash
# Hourly auto-deploy: pull main from GitHub and restart nightshift if anything
# changed. Run by gregnightshift via cron.

set -euo pipefail

# Single instance: a 30-min idle-wait must not overlap the next hourly run.
exec 9>/tmp/update-bot.lock
flock -n 9 || exit 0

# Never restart over a live claude run: pedro_brain flags each in-flight run
# with a file here (named <pid>-<token>). Wait up to 30 min for the flags to
# clear; drop flags whose owning process is gone.
wait_for_idle_runs() {
    local deadline now busy f base pid ts dir
    deadline=$(( $(date +%s) + 1800 ))
    while :; do
        busy=0
        now=$(date +%s)
        for dir in /data/greg/.pedro-running /data/employees/.pedro-running; do
            [ -d "$dir" ] || continue
            for f in "$dir"/*; do
                [ -e "$f" ] || continue
                base=${f##*/}; pid=${base%%-*}
                if ! [ -d "/proc/$pid" ]; then rm -f "$f" 2>/dev/null || true; continue; fi
                ts=$(stat -c %Y "$f" 2>/dev/null || echo 0)
                if [ $(( now - ts )) -ge 7200 ]; then rm -f "$f" 2>/dev/null || true; continue; fi
                busy=1
            done
        done
        [ "$busy" -eq 0 ] && return 0
        [ "$now" -ge "$deadline" ] && { logger -t nightshift-deploy "wait_for_idle_runs: still busy after 30 min - restarting anyway"; return 0; }
        sleep 15
    done
}

cd /home/gregnightshift/nightshift

# Install/refresh managed crons every run (idempotent) - must happen even
# when there is nothing to deploy, since the early exit below skips the rest.
ensure_crons() {
    # Ensure seba daily briefing cron is installed (idempotent)
    SEBA_CRON="0 14 * * * /home/gregnightshift/nightshift/scripts/seba_briefing.py >> /tmp/seba_briefing.log 2>&1"
    if ! crontab -l 2>/dev/null | grep -qF "seba_briefing.py"; then
        (crontab -l 2>/dev/null; echo "$SEBA_CRON") | crontab -
        logger -t nightshift-update "Installed seba_briefing cron"
    fi

    # Ensure scheduled-blast fire cron is installed (idempotent)
    FIRE_CRON="* * * * * /home/gregnightshift/nightshift/.venv/bin/python /home/gregnightshift/nightshift/scripts/blast_queue.py fire-scheduled >> /data/greg/blast_queue/scheduled_fire.log 2>&1"
    if ! crontab -l 2>/dev/null | grep -qF "fire-scheduled"; then
        (crontab -l 2>/dev/null; echo "$FIRE_CRON") | crontab -
        logger -t nightshift-update "Installed blast fire-scheduled cron"
    fi

    # Ensure social media daily brief cron is installed (idempotent)
    # 9am MDT = 15:00 UTC (summer). Sends today's posts to Greg via Telegram.
    SOCIAL_BRIEF_CRON="0 15 * * * /home/gregnightshift/nightshift/scripts/social_brief.sh >> /tmp/social-brief.log 2>&1"
    if ! crontab -l 2>/dev/null | grep -qF "social_brief.sh"; then
        (crontab -l 2>/dev/null; echo "$SOCIAL_BRIEF_CRON") | crontab -
        logger -t nightshift-update "Installed social_brief cron"
    fi

    # Ensure social media auto-poster cron is installed (idempotent)
    # Runs every hour at :30. Posts due calendar entries when token has posting perms.
    SOCIAL_POST_CRON="30 * * * * /home/gregnightshift/nightshift/scripts/social_auto_post.sh >> /tmp/social-auto-post.log 2>&1"
    if ! crontab -l 2>/dev/null | grep -qF "social_auto_post.sh"; then
        (crontab -l 2>/dev/null; echo "$SOCIAL_POST_CRON") | crontab -
        logger -t nightshift-update "Installed social_auto_post cron"
    fi
}

ensure_crons


LOCAL=$(git rev-parse HEAD)
git fetch origin main --quiet
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi

CHANGED=$(git diff --name-only "$LOCAL" "$REMOTE" || true)

# New code is ready to land - but never yank it out from under a live run.
wait_for_idle_runs

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
