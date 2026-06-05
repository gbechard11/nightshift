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

SENTINEL=/data/greg/.deploy-now
MODE="$(head -c 64 "$SENTINEL" 2>/dev/null || true)"
rm -f "$SENTINEL"   # consume immediately so a fresh touch re-triggers

# Always safe to restart — these are not the process that triggered this.
systemctl restart nightshift-employees 2>/dev/null || true
systemctl restart nightshift-mcp 2>/dev/null || true

# Restarting the OWNER bot kills Pedro's current turn, so only do it when the
# sentinel explicitly asks for it (contents contain "owner" or "all").
if printf '%s' "$MODE" | grep -qiE 'owner|all'; then
    systemctl restart nightshift 2>/dev/null || true
fi

logger -t nightshift-deploy "instant restart (mode='${MODE}')"
