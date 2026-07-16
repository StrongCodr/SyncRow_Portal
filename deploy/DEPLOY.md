# SyncRow Portal — Deployment Runbook

Native deploy (no Docker): **systemd** runs the Panel app on `127.0.0.1:5006`,
**nginx** terminates TLS (Let's Encrypt) and reverse-proxies to it, **basic-auth**
gates access. InfluxDB already runs on this same box.

```
Internet ──443 HTTPS──▶ nginx (TLS, certbot) ──proxy+ws──▶ 127.0.0.1:5006 panel serve (systemd)
                                                                   │
                                                                   ▼
                                                          127.0.0.1:8086 InfluxDB
```

Auth today is **Panel basic-auth** (username/password). Google/GitHub OAuth can be
added later by switching the `--basic-auth` flag in the service unit for the
`--oauth-*` flags — the rest of this setup is unchanged.

---

## Files in this `deploy/` dir

| File | Runs on | Purpose |
|---|---|---|
| `deploy-from-laptop.sh` | **your laptop** | One-command entrypoint — drives the VPS over SSH (`bootstrap`/`update`/`status`/`logs`) |
| `bootstrap.sh` | VPS (root) | First-time setup (packages, user, clone, venv, secrets, systemd, nginx) |
| `deploy.sh` | VPS (root) | Update: `git pull` + restart |
| `syncrow-portal.service` | `/etc/systemd/system/` | The service unit |
| `nginx-syncrow.conf` | `/etc/nginx/sites-available/syncrow` | Reverse proxy |
| `portal.env.example` | `/etc/syncrow/portal.env` | Influx creds, domain, cookie secret (root:600) |
| `credentials.example.json` | `/etc/syncrow/credentials.json` | Basic-auth users (root:600) |

Secrets live in `/etc/syncrow/` only — **never** committed.

---

## First-time deploy (checklist)

**You handle (out of my control):**
1. Point a domain's **DNS A record** at `104.152.48.213`.
2. Open **ports 80 and 443** in the VPS firewall.

**Then, from your laptop** (inside the repo — the deploy key stays on the VPS,
never on your laptop or in git):
3. First run generates + prints the GitHub deploy key, then stops:
   ```bash
   ./deploy/deploy-from-laptop.sh bootstrap
   ```
   Add the printed public key at **GitHub → StrongCodr/SyncRow_Portal → Settings →
   Deploy keys** (read-only).
4. Run it again to clone + build:
   ```bash
   ./deploy/deploy-from-laptop.sh bootstrap
   ```
   This writes `/etc/syncrow/portal.env` (auto-generated `COOKIE_SECRET`) and
   `/etc/syncrow/credentials.json` (initial `admin` password — **printed once**).
5. Fill in `/etc/syncrow/portal.env` on the VPS:
   - `INFLUX_TOKEN`, `INFLUX_ORG`, `INFLUX_ORG_ID` — copy from your existing local `.env`
   - `PORTAL_DOMAIN` — your real domain
   - `INFLUX_URL` — leave as `http://localhost:8086` (Influx is local now)
6. Run it once more to install the nginx site + start the service:
   ```bash
   ./deploy/deploy-from-laptop.sh bootstrap
   ```
7. Get the TLS cert (needs DNS + ports live), on the VPS:
   ```bash
   sudo certbot --nginx -d your-domain
   ```
   certbot edits the nginx site to add `listen 443 ssl` + HTTP→HTTPS redirect and
   sets up auto-renewal.

Visit `https://your-domain` → basic-auth login → dashboard.

> Default target is `root@104.152.48.213`. Override per-run with
> `SSH_TARGET=root@host ./deploy/deploy-from-laptop.sh ...`.

---

## Updating to a new version

From your laptop, after pushing your changes to git:

```bash
./deploy/deploy-from-laptop.sh update     # VPS does git pull + restart
./deploy/deploy-from-laptop.sh status     # check it came back up
./deploy/deploy-from-laptop.sh logs       # follow logs
```

## Managing users

Edit `/etc/syncrow/credentials.json` (`{"user": "password", ...}`), then:
```bash
sudo systemctl restart syncrow-portal
```

## Operations

```bash
systemctl status syncrow-portal          # health
journalctl -u syncrow-portal -f          # live logs
systemctl restart syncrow-portal         # restart
sudo certbot renew --dry-run             # verify renewal
```

## Notes / hardening TODO

- `COOKIE_SECRET` is passed on the command line, so it is visible in `ps` to local
  users. On a single-tenant box this is low risk; to remove it, switch to Panel's
  cookie-secret env var once confirmed for this version.
- Consider firewalling InfluxDB (`:8086`) to localhost + the phone's source, now
  that the portal talks to it over loopback. The phone still needs remote write
  access, so scope this carefully.
- OAuth upgrade path: register a Google/GitHub OAuth app, then in
  `syncrow-portal.service` replace `--basic-auth ... --cookie-secret ...` with
  `--oauth-provider google --oauth-key ... --oauth-secret ... --cookie-secret ...
  --oauth-encryption-key ...` and `daemon-reload` + restart.
