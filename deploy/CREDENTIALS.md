# Where the credentials live

**None of the real secrets are in this repo** (they're gitignored / server-only).
This file just points to where they are kept on the production VPS.

**Server:** `root@104.152.48.213` (hostname `syncro`) — live site: https://syncrow.cloud

| What | Location (on the VPS) | Perms | Notes |
|---|---|---|---|
| **Dashboard login** (nginx basic-auth) | `/etc/nginx/.htpasswd` | root:640 | htpasswd file. Auth is enforced by nginx in front of the app. |
| **App config + Influx creds** | `/etc/syncrow/portal.env` | root:640 | `INFLUX_URL/TOKEN/ORG/ORG_ID/BUCKET`, `PORTAL_DOMAIN`. Loaded by systemd. |
| **GitHub deploy key** (VPS → repo) | `/opt/syncrow/.ssh/id_ed25519` (+`.pub`) | syncrow:600 | Private half never leaves the box. Public half is on GitHub → repo → Settings → Deploy keys. |
| **TLS cert + key** | `/etc/letsencrypt/live/syncrow.cloud/` | root | Managed by certbot (auto-renew via webroot). |

## Change the dashboard password / add a user
```bash
ssh root@104.152.48.213
# add or update a user (installs apache2-utils for htpasswd if needed):
htpasswd /etc/nginx/.htpasswd <username>
systemctl reload nginx
```

## Rotate Influx token / cookie secret
Edit `/etc/syncrow/portal.env`, then `systemctl restart syncrow-portal`.

See `deploy/DEPLOY.md` for the full runbook.
