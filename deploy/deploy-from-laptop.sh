#!/usr/bin/env bash
#
# Run THIS from your laptop, inside the repo. It drives the VPS deploy over SSH
# so you never have to log in by hand.
#
#   ./deploy/deploy-from-laptop.sh bootstrap   # first-time setup (or re-run)
#   ./deploy/deploy-from-laptop.sh update      # pull latest + restart
#   ./deploy/deploy-from-laptop.sh status      # health + recent logs
#   ./deploy/deploy-from-laptop.sh logs        # tail live logs
#
# Target host defaults to the production VPS; override with SSH_TARGET:
#   SSH_TARGET=root@1.2.3.4 ./deploy/deploy-from-laptop.sh update
#
set -euo pipefail

SSH_TARGET="${SSH_TARGET:-root@104.152.48.213}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CMD="${1:-}"

ssh_run() { ssh -o ConnectTimeout=15 "$SSH_TARGET" "$@"; }

case "$CMD" in
  bootstrap)
    echo "==> First-time bootstrap on $SSH_TARGET"
    # Stream the local bootstrap script into a remote root shell. On the first
    # run it prints a GitHub deploy key and stops — add that key to the repo
    # (Settings -> Deploy keys, read-only), then run this again.
    ssh_run 'sudo bash -s' < "$SCRIPT_DIR/bootstrap.sh"
    ;;
  update)
    echo "==> Updating $SSH_TARGET (git pull + restart)"
    ssh_run 'sudo bash /opt/syncrow/SyncRow_Portal/deploy/deploy.sh'
    ;;
  status)
    ssh_run 'systemctl --no-pager --lines=10 status syncrow-portal'
    ;;
  logs)
    echo "==> Tailing logs (Ctrl-C to stop)"
    ssh_run -t 'journalctl -u syncrow-portal -f'
    ;;
  *)
    echo "Usage: $0 {bootstrap|update|status|logs}"
    echo
    echo "  bootstrap  first-time setup, or re-run after adding the deploy key /"
    echo "             editing /etc/syncrow/portal.env"
    echo "  update     git pull + conditional dep reinstall + restart"
    echo "  status     service health + last log lines"
    echo "  logs       follow live logs"
    exit 1
    ;;
esac
