#!/usr/bin/env bash
# Belt-and-suspenders watchdog for the Pedro bot. Run every few minutes via cron.
# If the service isn't active, start it and log the recovery.

if ! systemctl is-active --quiet nightshift; then
    sudo systemctl start nightshift
    logger -t nightshift-watchdog "nightshift was down — restarted by watchdog"
fi
