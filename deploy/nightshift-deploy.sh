#!/usr/bin/env bash
# Root-owned instant-deploy watcher target. Triggered by deploy-now.path when
# Pedro (owner bot, runs as gregnightshift) creates /data/greg/.deploy-now.
#
# SECURITY: this file MUST stay root:root 0755. Pedro can ONLY create the
# sentinel flag; it cannot edit this script. The script never executes
# repo/Pedro-writable code as root — it only restarts fixed, named services.
# It deliberately does NOT run `git reset` (that would wipe an uncommitted
# in-progress edit before the restart loads it). Code is already on disk from
# Pedro's edit; this just reloads it. Durability against the :17 reset still
# requires Pedro to commit+push (see /data/greg/CLAUDE.md).
set -uo pipefail

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

SENTINEL=/data/greg/.deploy-now
MODE="$(head -c 64 "$SENTINEL" 2>/dev/null || true)"
rm -f "$SENTINEL"   # consume immediately so a fresh touch re-triggers

# A restart kills any in-flight claude run in the target service, losing
# half-done work - wait for the run flags to clear first.
wait_for_idle_runs

# Always safe to restart — these are not the process that triggered this.
systemctl restart nightshift-employees 2>/dev/null || true
systemctl restart nightshift-mcp 2>/dev/null || true

# Restarting the OWNER bot kills Pedro's current turn, so only do it when the
# sentinel explicitly asks for it (contents contain "owner" or "all").
if printf '%s' "$MODE" | grep -qiE 'owner|all'; then
    systemctl restart nightshift 2>/dev/null || true
fi

logger -t nightshift-deploy "instant restart (mode='${MODE}')"
