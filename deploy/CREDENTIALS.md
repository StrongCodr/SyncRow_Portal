# Where the credentials live

**None of the real secrets are in this repo** (they're gitignored / server-only).
This file just points to where they are kept on the production VPS.

**Server:** `root@104.152.48.213` (hostname `syncro`) — live site: https://syncrow.cloud

| What | Location (on the VPS) | Perms | Notes |
|---|---|---|---|
| **Dashboard login** (basic-auth users) | `/etc/syncrow/credentials.json` | root:640 | JSON `{ "user": "password" }`. Edit, then `systemctl restart syncrow-portal`. |
| **App config + Influx creds + cookie secret** | `/etc/syncrow/portal.env` | root:640 | `INFLUX_TOKEN/ORG/ORG_ID/BUCKET`, `PORTAL_DOMAIN`, `COOKIE_SECRET`. Loaded by systemd. |
| **GitHub deploy key** (VPS → repo) | `/opt/syncrow/.ssh/id_ed25519` (+`.pub`) | syncrow:600 | Private half never leaves the box. Public half is on GitHub → repo → Settings → Deploy keys. |
| **TLS cert + key** | `/etc/letsencrypt/live/syncrow.cloud/` | root | Managed by certbot (auto-renew via webroot). |

## Change the dashboard password / add a user
```bash
ssh root@104.152.48.213
nano /etc/syncrow/credentials.json      # e.g. {"admin":"...","coach":"..."}
systemctl restart syncrow-portal
```

## Rotate Influx token / cookie secret
Edit `/etc/syncrow/portal.env`, then `systemctl restart syncrow-portal`.

See `deploy/DEPLOY.md` for the full runbook.
