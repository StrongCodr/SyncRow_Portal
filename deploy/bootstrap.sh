#!/usr/bin/env bash
#
# SyncRow Portal — first-time server bootstrap. Run as root ON THE VPS.
#
#   sudo bash bootstrap.sh
#
# Idempotent: safe to re-run. It will NOT overwrite existing secrets
# (/etc/syncrow/portal.env, credentials.json) once created.
#
# Prerequisites you handle:
#   - A domain's DNS A record pointing at this server (for TLS).
#   - Ports 80 and 443 open in your VPS firewall.
#   - A GitHub deploy key added to the SyncRow_Portal repo (this script
#     generates it and prints it if missing, then exits so you can add it).
#
set -euo pipefail

# ─── Config ──────────────────────────────────────────────────────────────────
APP_USER="syncrow"
APP_HOME="/opt/syncrow"
APP_DIR="${APP_HOME}/SyncRow_Portal"
REPO_URL="git@github.com:StrongCodr/SyncRow_Portal.git"
ETC_DIR="/etc/syncrow"
# App requires Python >= 3.11 (Ubuntu 22.04 ships 3.10, so we install 3.11).
PYTHON_BIN="${PYTHON_BIN:-python3.11}"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Run as root."; exit 1; }

# ─── 1. Packages ─────────────────────────────────────────────────────────────
log "Installing packages (nginx, certbot, git)"
apt-get update -qq
apt-get install -y -qq nginx certbot python3-certbot-nginx git openssl software-properties-common

# The app requires Python >= 3.11; Ubuntu 22.04 only ships 3.10. Pull a stable
# 3.11 from the deadsnakes PPA (the default-repo 3.11 is an rc build).
if ! command -v "$PYTHON_BIN" >/dev/null; then
    log "Installing $PYTHON_BIN (deadsnakes PPA)"
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -qq
    apt-get install -y -qq "$PYTHON_BIN" "${PYTHON_BIN}-venv" "${PYTHON_BIN}-dev"
fi

# ─── 2. Service user + dirs ──────────────────────────────────────────────────
if ! id "$APP_USER" &>/dev/null; then
    log "Creating system user '$APP_USER'"
    useradd --system --create-home --home-dir "$APP_HOME" --shell /usr/sbin/nologin "$APP_USER"
fi
mkdir -p "$APP_HOME"
chown "$APP_USER:$APP_USER" "$APP_HOME"

# ─── 3. GitHub deploy key (read-only) ────────────────────────────────────────
KEY="$APP_HOME/.ssh/id_ed25519"
sudo -u "$APP_USER" mkdir -p "$APP_HOME/.ssh"
chmod 700 "$APP_HOME/.ssh"
if [ ! -f "$KEY" ]; then
    log "Generating GitHub deploy key"
    sudo -u "$APP_USER" ssh-keygen -t ed25519 -N "" -f "$KEY" -C "syncrow-portal-deploy"
fi
# Trust github.com host key
sudo -u "$APP_USER" bash -c "ssh-keyscan -t ed25519 github.com >> '$APP_HOME/.ssh/known_hosts' 2>/dev/null" || true
sort -u "$APP_HOME/.ssh/known_hosts" -o "$APP_HOME/.ssh/known_hosts" 2>/dev/null || true

# ─── 4. Clone (or verify) the repo ───────────────────────────────────────────
if [ ! -d "$APP_DIR/.git" ]; then
    log "Cloning repo"
    if ! sudo -u "$APP_USER" git clone "$REPO_URL" "$APP_DIR" 2>/tmp/clone.err; then
        echo
        echo "!!! Clone failed — the deploy key is not on GitHub yet."
        echo "    Add this PUBLIC key to the repo:"
        echo "    GitHub → StrongCodr/SyncRow_Portal → Settings → Deploy keys → Add key"
        echo "    (read-only is fine)"
        echo
        echo "──────── DEPLOY PUBLIC KEY ────────"
        cat "${KEY}.pub"
        echo "───────────────────────────────────"
        echo
        echo "Then re-run this script."
        cat /tmp/clone.err >&2 || true
        exit 2
    fi
fi

# ─── 5. Python venv + deps ───────────────────────────────────────────────────
log "Building virtualenv ($PYTHON_BIN) + installing dependencies"
# Self-heal: if a venv exists but is older than 3.11, rebuild it.
if [ -d "$APP_DIR/.venv" ]; then
    VENV_OK="$("$APP_DIR/.venv/bin/python" -c 'import sys; print(sys.version_info[:2] >= (3, 11))' 2>/dev/null || echo False)"
    if [ "$VENV_OK" != "True" ]; then
        log "Existing venv is too old — recreating with $PYTHON_BIN"
        rm -rf "$APP_DIR/.venv"
    fi
fi
if [ ! -d "$APP_DIR/.venv" ]; then
    sudo -u "$APP_USER" "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
fi
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR"
sudo -u "$APP_USER" mkdir -p "$APP_DIR/.cache"

# ─── 6. Secrets + config (/etc/syncrow) ──────────────────────────────────────
mkdir -p "$ETC_DIR"
chmod 750 "$ETC_DIR"
chown root:"$APP_USER" "$ETC_DIR"

if [ ! -f "$ETC_DIR/portal.env" ]; then
    log "Creating $ETC_DIR/portal.env (FILL IN the Influx token + domain)"
    install -m 640 -o root -g "$APP_USER" "$APP_DIR/deploy/portal.env.example" "$ETC_DIR/portal.env"
    # Auto-generate the cookie secret so it's never a placeholder.
    SECRET="$(openssl rand -hex 32)"
    sed -i "s|^COOKIE_SECRET=.*|COOKIE_SECRET=${SECRET}|" "$ETC_DIR/portal.env"
    NEED_EDIT=1
fi

if [ ! -f "$ETC_DIR/credentials.json" ]; then
    log "Creating $ETC_DIR/credentials.json with an initial 'admin' user"
    ADMIN_PW="$(openssl rand -base64 18)"
    printf '{\n  "admin": "%s"\n}\n' "$ADMIN_PW" > "$ETC_DIR/credentials.json"
    chmod 640 "$ETC_DIR/credentials.json"
    chown root:"$APP_USER" "$ETC_DIR/credentials.json"
    echo
    echo "  >>> Initial login:  admin / ${ADMIN_PW}"
    echo "  >>> (edit $ETC_DIR/credentials.json to add users, then restart the service)"
    echo
fi

# ─── 7. systemd service ──────────────────────────────────────────────────────
log "Installing systemd service"
install -m 644 "$APP_DIR/deploy/syncrow-portal.service" /etc/systemd/system/syncrow-portal.service
systemctl daemon-reload
systemctl enable syncrow-portal

# ─── 8. nginx site ───────────────────────────────────────────────────────────
DOMAIN="$(grep -E '^PORTAL_DOMAIN=' "$ETC_DIR/portal.env" | cut -d= -f2)"
if [ -n "$DOMAIN" ] && [ "$DOMAIN" != "portal.example.com" ]; then
    log "Installing nginx site for $DOMAIN"
    mkdir -p /var/www/certbot                      # webroot for ACME renewal
    rm -f /etc/nginx/sites-enabled/default         # kill the stock welcome page
    sed "s/PORTAL_DOMAIN/${DOMAIN}/g" "$APP_DIR/deploy/nginx-syncrow.conf" > /etc/nginx/sites-available/syncrow
    ln -sf /etc/nginx/sites-available/syncrow /etc/nginx/sites-enabled/syncrow
    nginx -t && systemctl reload nginx
fi

# ─── 9. Start (only if config is filled in) ──────────────────────────────────
if [ "${NEED_EDIT:-0}" = "1" ] || grep -q "__PASTE_YOUR_INFLUX_TOKEN_HERE__" "$ETC_DIR/portal.env"; then
    echo
    echo "──────────────────────────────────────────────────────────────"
    echo "  ALMOST DONE. Before the portal can start, edit:"
    echo "    $ETC_DIR/portal.env"
    echo "      - INFLUX_TOKEN / INFLUX_ORG / INFLUX_ORG_ID  (from your existing .env)"
    echo "      - PORTAL_DOMAIN  (your real domain)"
    echo
    echo "  Then run:"
    echo "    sudo bash $APP_DIR/deploy/bootstrap.sh     # re-run: installs nginx site + starts"
    echo "    sudo certbot --nginx -d <your-domain>      # once DNS + ports 80/443 are live"
    echo "──────────────────────────────────────────────────────────────"
else
    systemctl restart syncrow-portal
    log "Portal started. Status:"
    systemctl --no-pager --lines=5 status syncrow-portal || true
    echo
    echo "Next (if not already done):  sudo certbot --nginx -d ${DOMAIN}"
fi
