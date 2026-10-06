# CaptainApply — Self-Host (Phase 1.5)

Run CaptainApply on **your own VPS** with a single command. The self-host
image is a slim Python webapp — no Chromium, no scrapers, no dashboard. It
serves the Captain's Bridge UI on port 8742 and stores everything in a single
named Docker volume.

> Looking for the cloud-hosted version? Same product, hosted by us. See
> [captainapply.com](https://captainapply.com).

---

## Quick start (5 commands)

```bash
# 1. Install Docker (skip if already installed)
curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER

# 2. Clone the repo (or just copy the docker-compose file + image)
git clone https://github.com/boldandigital/captainapply.git
cd captainapply

# 3. Start the stack
docker compose -f docker-compose.selfhost.yml up -d

# 4. Open the UI
open http://localhost:8742            # macOS
xdg-open http://localhost:8742        # Linux

# 5. Log in with the default admin and CHANGE THE PASSWORD
#    user: lars
#    pass: captain
```

The first start takes ~30s. Watch progress with:

```bash
docker compose -f docker-compose.selfhost.yml logs -f
```

You should see:

```
INFO:     Started server process [1]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8742
```

---

## Configuration

All config is via environment variables. Set them in a `.env` file next to
`docker-compose.selfhost.yml`, or pass them through your process manager
(systemd, Portainer, Coolify, etc).

| Variable | Default | Purpose |
|---|---|---|
| `LARS_USER` | `lars` | Bootstrap admin user id |
| `LARS_PASS` | `captain` | Bootstrap admin password — **change this** |
| `CAPTAIN_SESSION_SECRET` | (insecure fallback) | HMAC key for session cookies — **must be set in prod**. 44+ random chars, e.g. `openssl rand -base64 48`. |
| `EXTRA_USERS` | _(empty)_ | Optional comma-separated `user:pass` pairs (Phase 1.5; not yet exposed in the UI) |
| `CAPTAIN_PROD` | `1` | When `1`, session cookies are marked `Secure` (HTTPS-only) |

### Example `.env`

```bash
LARS_USER=lars
LARS_PASS=please-change-me-now
CAPTAIN_SESSION_SECRET=$(openssl rand -base64 48)
```

Restart to apply:

```bash
docker compose -f docker-compose.selfhost.yml up -d --force-recreate
```

---

## Putting it on a VPS (behind HTTPS)

This image listens on plain HTTP port 8742. **Don't expose it directly to
the internet** — put it behind a reverse proxy that terminates TLS.

### Caddy (easiest)

```caddy
# /etc/caddy/Caddyfile
apply.example.com {
    reverse_proxy 127.0.0.1:8742
}
```

```bash
sudo systemctl reload caddy
```

Caddy auto-issues and renews Let's Encrypt certs.

### nginx

```nginx
server {
    server_name apply.example.com;
    listen 443 ssl http2;
    ssl_certificate     /etc/letsencrypt/live/apply.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/apply.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8742;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

After putting TLS in front, set `CAPTAIN_PROD=1` so session cookies are
`Secure` (HTTPS-only). The compose file already does this.

---

## Backup

All state lives in the `captainapply-data` Docker volume, mounted at
`/app/data` inside the container. Back it up by snapshotting the volume or
copying the directory out:

```bash
# Snapshot to a timestamped directory on the host
docker run --rm \
    -v captainapply-data:/from:ro \
    -v "$PWD":/to \
    alpine sh -c 'cd /from && tar czf /to/captainapply-backup-$(date +%F).tar.gz .'
```

Restore by extracting into the same volume:

```bash
docker run --rm \
    -v captainapply-data:/to \
    -v "$PWD":/from \
    alpine sh -c 'cd /to && tar xzf /from/captainapply-backup-2026-10-06.tar.gz'
```

Stop the web service before restoring so SQLite doesn't complain about a
locked WAL file:

```bash
docker compose -f docker-compose.selfhost.yml stop web
# ... restore ...
docker compose -f docker-compose.selfhost.yml start web
```

---

## Update

```bash
docker compose -f docker-compose.selfhost.yml pull
docker compose -f docker-compose.selfhost.yml up -d --force-recreate
```

Your data volume is untouched. To roll back: `docker compose ... down` and
re-run with the previous image tag.

---

## Files in this directory

| File | Purpose |
|---|---|
| `Dockerfile.selfhost` | Slim Python 3.11 image — webapp only |
| `docker-compose.selfhost.yml` | One-service stack with `captainapply-data` volume |
| `bin/build-selfhost.sh` | Build + boot-test the image |
| `bin/run-selfhost.sh` | Run the image locally for smoke testing |
| `tests/test_selfhost_docker.py` | Docker integration test (`pytest -m docker --run-docker`) |
| `README.selfhost.md` | This file |

The original `Dockerfile` and `docker-compose.yml` at the repo root run the
**full pipeline** (Patchright scrapers, dashboard, cron). Don't use those
for self-host — use the `.selfhost` variants.

---

## License & data

- **License:** AGPL-3.0. Modifications to the self-host tier must be
  published under the same terms. See `LICENSE`.
- **Data residency:** Your data lives on your VPS. Nothing leaves the
  container. GDPR / EU data residency: trivially satisfied — the volume
  is wherever your VPS is.
- **Telemetry:** This image makes **no outbound network calls** besides
  what you configure (Discord webhooks, OAuth, etc).

---

## Troubleshooting

**Container exits immediately with `python: can't open file`**

You're running an older image. Pull the latest:

```bash
docker compose -f docker-compose.selfhost.yml pull
```

**`/api/health` returns 404**

You're hitting the wrong port. The container listens on **8742**; map
whichever host port you like (`"8742:8742"` in the compose file).

**Login fails with "Invalid credentials"**

Check `LARS_USER` and `LARS_PASS` env vars. The defaults are `lars` /
`captain`. If you've set them in a `.env` file, make sure it's next to
the compose file and you're using the `--env-file` flag (compose loads
it automatically when named `.env`).

**Slow first request**

The webapp is reading PDFs and SQLite on first hit. Subsequent requests
are fast. If it's persistently slow, your volume is probably on slow
storage — check with `docker volume inspect captainapply-data`.

---

## Support

- GitHub issues: [github.com/boldandigital/captainapply](https://github.com/boldandigital/captainapply)
- Email: `support@captainapply.com`
