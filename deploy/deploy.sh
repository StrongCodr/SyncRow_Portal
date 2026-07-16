#!/usr/bin/env bash
#
# SyncRow Portal — update deploy. Run as root ON THE VPS after bootstrap.sh.
#
#   sudo bash /opt/syncrow/SyncRow_Portal/deploy/deploy.sh
#
# Pulls latest code, reinstalls deps if they changed, restarts the service.
# Zero server config here — that lives in /etc/syncrow (untouched).
#
set -euo pipefail

APP_USER="syncrow"
APP_DIR="/opt/syncrow/SyncRow_Portal"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "Run as root."; exit 1; }

log "Fetching latest code"
BEFORE="$(sudo -u "$APP_USER" git -C "$APP_DIR" rev-parse HEAD)"
sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only
AFTER="$(sudo -u "$APP_USER" git -C "$APP_DIR" rev-parse HEAD)"

if [ "$BEFORE" = "$AFTER" ]; then
    log "Already up to date ($AFTER). Restarting anyway."
else
    log "Updated $BEFORE -> $AFTER"
    # Reinstall deps only if pyproject changed.
    if sudo -u "$APP_USER" git -C "$APP_DIR" diff --name-only "$BEFORE" "$AFTER" | grep -q "pyproject.toml"; then
        log "pyproject.toml changed — reinstalling dependencies"
        sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR"
    fi
fi

log "Restarting service"
systemctl restart syncrow-portal
sleep 2
systemctl --no-pager --lines=8 status syncrow-portal || true
