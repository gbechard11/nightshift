# Instant-deploy trigger (reference copies)

Lets the owner Pedro bot (runs as gregnightshift, NO sudo) deploy its own code
changes instantly without a human, while keeping the privilege boundary intact.

Pedro can only CREATE the flag `/data/greg/.deploy-now`. A root-owned systemd
path unit watches it and runs the restart. Pedro never gets sudo and cannot edit
the root script, so there is no privilege escalation beyond pushing to origin/main
(which it can already do).

## Live locations (authoritative; root:root)
- `/usr/local/sbin/nightshift-deploy.sh`        (0755 root:root)
- `/etc/systemd/system/nightshift-deploy.service`
- `/etc/systemd/system/deploy-now.path`         (enabled)

The copies in this dir are for reproducibility only; the live files are the ones
that run. To reinstall after a rebuild:

    sudo install -o root -g root -m 0755 deploy/nightshift-deploy.sh /usr/local/sbin/nightshift-deploy.sh
    sudo install -o root -g root -m 0644 deploy/nightshift-deploy.service /etc/systemd/system/nightshift-deploy.service
    sudo install -o root -g root -m 0644 deploy/deploy-now.path /etc/systemd/system/deploy-now.path
    sudo systemctl daemon-reload && sudo systemctl enable --now deploy-now.path

## Usage (Pedro)
- `touch /data/greg/.deploy-now`        -> restart employee + mcp bots only (safe)
- `echo owner > /data/greg/.deploy-now` -> also restart the owner bot (ends Pedro\x27s turn)

The flag only RESTARTS (loads on-disk code); it does NOT git-reset. Commit+push
first or the hourly :17 `git reset --hard origin/main` will still revert tracked files.
