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

# Write the complete desired crontab in one shot every run.
# This avoids the append-then-lose race that caused duplicate social_brief entries.
# Any stale managed entries (guestlist one-shot, old duplicates) are also cleaned up.
ensure_crons() {
    local DESIRED
    DESIRED="$(printf '%s\n' \
        "0 14 * * * /home/gregnightshift/nightshift/scripts/seba_briefing.py >> /tmp/seba_briefing.log 2>&1" \
        "* * * * * /home/gregnightshift/nightshift/.venv/bin/python /home/gregnightshift/nightshift/scripts/blast_queue.py fire-scheduled >> /data/greg/blast_queue/scheduled_fire.log 2>&1" \
        "0 15 * * * /home/gregnightshift/nightshift/scripts/social_brief.sh >> /tmp/social-brief.log 2>&1" \
        "30 * * * * /home/gregnightshift/nightshift/scripts/social_auto_post.sh >> /tmp/social-auto-post.log 2>&1" \
    )"

    # Strip all managed lines from current crontab, then append the desired set.
    # Using grep -vF with each pattern prevents duplicates and removes stale entries.
    local CURRENT
    CURRENT="$(crontab -l 2>/dev/null \
        | grep -vF "seba_briefing.py" \
        | grep -vF "fire-scheduled" \
        | grep -vF "social_brief.sh" \
        | grep -vF "social_auto_post.sh" \
        | grep -vF "guestlist_finalize_mina.py" \
        || true)"

    local NEW_CRONTAB
    if [ -n "$CURRENT" ]; then
        NEW_CRONTAB="$(printf '%s\n%s\n' "$CURRENT" "$DESIRED")"
    else
        NEW_CRONTAB="$DESIRED"
    fi

    # Only write if the crontab would actually change.
    local EXISTING
    EXISTING="$(crontab -l 2>/dev/null || true)"
    if [ "$EXISTING" != "$NEW_CRONTAB" ]; then
        echo "$NEW_CRONTAB" | crontab -
        logger -t nightshift-update "ensure_crons: crontab updated"
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

# If drops unit file changed, reinstall + daemon-reload
if echo "$CHANGED" | grep -q '^nightshift-drops.service$'; then
    sudo cp nightshift-drops.service /etc/systemd/system/nightshift-drops.service
    sudo systemctl daemon-reload
fi

sudo systemctl restart nightshift
sudo systemctl restart nightshift-employees 2>/dev/null || true
sudo systemctl restart nightshift-mcp 2>/dev/null || true
sudo systemctl restart nightshift-drops 2>/dev/null || true

logger -t nightshift-update "Deployed $LOCAL -> $REMOTE. Changed: $(echo "$CHANGED" | tr '\n' ' ')"
