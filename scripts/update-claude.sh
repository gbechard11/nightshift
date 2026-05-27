#!/usr/bin/env bash
# Weekly: bump Claude Code CLI to latest, then refresh marketplace caches and
# update every installed plugin. Run by gregnightshift via cron.

set -euo pipefail

LOG_TAG="nightshift-claude-update"

# Claude CLI itself (global npm install)
if sudo npm install -g @anthropic-ai/claude-code@latest 2>&1 | logger -t "$LOG_TAG"; then
    logger -t "$LOG_TAG" "claude CLI upgrade OK ($(claude --version))"
fi

# Refresh marketplace caches (one for each registered marketplace)
/usr/bin/claude plugin marketplace update 2>&1 | logger -t "$LOG_TAG" || true

# Update each installed plugin
for plugin in \
    claude-mem@thedotmack \
    agent-skills@addy-agent-skills \
    last30days@last30days-skill \
    dx@ykdojo \
    claude-seo@agricidaniel-claude-seo
do
    /usr/bin/claude plugin update "$plugin" 2>&1 | logger -t "$LOG_TAG" || true
done

logger -t "$LOG_TAG" "weekly update pass complete"
