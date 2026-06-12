#!/usr/bin/env bash
# Conservative scratch cleanup for the Pedro VPS. Weekly cron.
#
# DELETES ONLY unambiguously-regenerable scratch:
#   - /tmp build/video artifacts older than 3 days
#   - contents of any /data/greg/*/frames/ ffmpeg-extract dir older than 14 days
# It NEVER touches deliverables, build scripts, the /data/greg/ads asset
# library, or anything outside those paths. Everything removed is logged.
# It also appends a size report of the big video dirs so Greg can prune the
# rest by hand — we deliberately do NOT auto-delete mixed deliverable dirs.
set -uo pipefail

LOG=/data/greg/disk_cleanup.log
ts() { date "+%Y-%m-%d %H:%M:%S"; }
echo "===== $(ts) disk_cleanup start =====" >>"$LOG"

freed_before=$(df --output=avail / | tail -1)

# 1) /tmp video/build scratch older than 3 days. Restrict to known scratch
#    name patterns so we never touch unrelated /tmp files.
find /tmp -maxdepth 1 -type f -mtime +3 \
    \( -name '*.mp4' -o -name '*_frame*.png' -o -name 'frame_*.png' \
       -o -name 'ownboss*' -o -name 'webby*' -o -name 'mina*' \
       -o -name 'build-*.json' -o -name 'build-*.err' \
       -o -name 'ownboss24*.png' \) \
    -print -delete 2>/dev/null >>"$LOG"

# 2) ffmpeg frame-extract dirs: any directory literally named "frames" under a
#    /data/greg project dir is scratch. Remove files in it older than 14 days.
find /data/greg -type d -name frames -mindepth 2 -maxdepth 3 2>/dev/null | while read -r d; do
    find "$d" -type f -mtime +14 -print -delete 2>/dev/null >>"$LOG"
done

freed_after=$(df --output=avail / | tail -1)
freed_mb=$(( (freed_after - freed_before) / 1024 ))
echo "$(ts) reclaimed ~${freed_mb} MB" >>"$LOG"

# 3) Size report (informational — NOT deleted). Flags dirs to prune by hand.
echo "$(ts) top /data/greg dirs (prune deliverables manually if needed):" >>"$LOG"
du -sh /data/greg/* 2>/dev/null | sort -rh | head -8 >>"$LOG"
echo "===== $(ts) disk_cleanup done =====" >>"$LOG"
