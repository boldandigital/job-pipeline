# CaptainApply — Production Deploy Guide

This guide covers the three supported ways to ship CaptainApply in
production. Pick one — they are independent and interchangeable.

| # | Path               | Landing page        | Backend       | Best for                      |
|---|--------------------|---------------------|---------------|-------------------------------|
| 1 | Vercel + Render/Fly| Vercel static       | Docker host   | Cheapest, fastest to ship     |
| 2 | Self-host          | served by FastAPI   | same image    | Full control, single command  |
| 3 | All-Vercel         | Vercel static       | Vercel Funcs  | Hobby demos only (10s limit)  |

The prebuilt self-host image and compose file are already documented in
`README.selfhost.md`. This file is the *production* counterpart.

---

## 1. Deploy the landing page to Vercel

The landing site is a static HTML page. Vercel serves it from a CDN for
free; the only required setup is linking the repo and pointing a domain
at it.

### One-time setup

```bash
# 1. Install the Vercel CLI (skip if you already have it)
npm i -g vercel

# 2. Authenticate (opens a browser)
vercel login

# 3. Link the repo to a Vercel project (creates the project on first run)
vercel link --project captainapply-web
```

### Per-environment env vars (Vercel dashboard → Settings → Environment Variables)

Vercel needs to know where the API lives so the static forms can POST
to it. These are baked into the HTML at build time via a small
replacement step below; for now the forms use the same origin, so you
only need to set them if the API is on a different host.

```
PUBLIC_API_BASE   = https://api.captainapply.com
```

`PUBLIC_API_BASE` is optional. If unset, the static pages POST to the
same origin (`/api/...`) which works when you proxy `/api/*` to the
backend (e.g. via Vercel rewrites or a Cloudflare Worker).

### Deploy

```bash
# 4. Promote to production
vercel --prod

# 5. Add a custom domain (optional)
vercel domains add captainapply.com
vercel domains add www.captainapply.com
```

Vercel will issue a Let's Encrypt cert automatically. The marketing
site is live at `https://captainapply.com` within ~60 seconds.

### Update flow

```bash
git pull
vercel --prod
```

There is no build step — the static files in `web/static/` are served
verbatim.

---

## 2. Deploy the FastAPI backend (Fly.io or Render)

Vercel Functions have a 10-second timeout on the hobby tier. The
apply endpoints (Playwright, Greenhouse, Lever, Workday form filling)
can run longer. For real production traffic, run the same image on
Fly.io, Render, or Railway.

### Option A: Fly.io (recommended — €0–5/mo for small traffic)

```bash
# 1. Install fly
brew install flyctl          # macOS
fly auth login

# 2. Launch from this repo (uses vercel-backend/Dockerfile)
fly launch --dockerfile vercel-backend/Dockerfile \
           --name captainapply-api \
           --region fra \    # Frankfurt — close to your DACH candidates
           --no-deploy

# 3. Set secrets (these become env vars in the container)
fly secrets set \
  CAPTAIN_SESSION_SECRET="$(openssl rand -hex 32)" \
  STRIPE_SECRET_KEY=sk_live_... \
  STRIPE_WEBHOOK_SECRET=whsec_... \
  STRIPE_PRICE_ID=price_... \
  LARS_USER=admin \
  LARS_PASS="$(openssl rand -hex 16)"

# 4. Create a persistent volume for SQLite + PDFs
fly volumes create captainapply_data --size 1

# 5. Wire the volume into the app (add to fly.toml after launch)
#    [[mounts]]
#      source = "captainapply_data"
#      destination = "/app/data"

# 6. Deploy
fly deploy
```

### Option B: Render (simpler, less control)

1. Create a new **Web Service** in Render.
2. Point it at this repo, branch `main`.
3. **Dockerfile path:** `vercel-backend/Dockerfile`
4. **Plan:** Starter (€0/mo) is enough for 100 signups.
5. **Health check path:** `/api/health`
6. Add the same env vars from step 3 above under **Environment**.
7. Click **Create Web Service**. Render builds and deploys in ~3 min.

### Wire landing → backend

In Vercel dashboard → Settings → Environment Variables, set:

```
PUBLIC_API_BASE   = https://captainapply-api.fly.dev
```

Then re-run `vercel --prod` so the build picks up the new value.

### Update flow

```bash
git pull
fly deploy         # or click "Manual Deploy" in Render
```

---

## 3. Self-host (existing flow, unchanged)

If you have a Mac mini / NUC / VPS, the Phase 1.5 self-host image
covers this. The only change in 1.6: the env-var list is larger (Stripe
keys), so the `.env` you ship has more entries.

```bash
# From the repo root
docker compose -f docker-compose.selfhost.yml --env-file .env up -d
open http://localhost:8742
```

See `README.selfhost.md` for the full flow.

---

## 4. Update flow — at a glance

| Path          | Command                                  | Notes                          |
|---------------|------------------------------------------|--------------------------------|
| Vercel        | `vercel --prod`                          | No build, just rsync           |
| Fly.io        | `fly deploy`                             | Rebuilds image, ~2 min         |
| Render        | "Manual Deploy" button                   | Same                           |
| Self-host     | `git pull && docker compose ... up -d`   | Pulls + recreates container    |

---

## 5. GDPR & EU data residency

CaptainApply stores:

- **Account data** (email, name, hashed password) — Postgres or SQLite
- **Job applications** (company, role, status, dates) — same DB
- **Generated CVs / cover letters** — same DB + a `/app/data/batches/` folder
- **Session cookies** — `Secure`, `HttpOnly`, `SameSite=Lax`

For EU users, choose a hosting region in the EU:

| Provider   | EU region code     | Notes                              |
|------------|--------------------|------------------------------------|
| Fly.io     | `fra` (Frankfurt)  | Closest to DACH, GDPR-friendly     |
| Render     | `frankfurt`        | Same                               |
| Vercel     | `fra1`             | Edge network; static site only     |
| Hetzner    | `fsn1`, `nbg1`     | Cheapest EU option (self-host)     |

Set `DB_PATH=/app/data/jobs.db` to keep data on the same volume as
the container. For multi-tenant Postgres, set `DATABASE_URL=postgres://...`
once Phase 2 lands (the migration is already written in
`src/db/users_db.py`).

For Stripe, use `eu` as the Stripe account region (account settings →
Company details → Headquarters) so payouts stay in EUR and Stripe data
is stored in the EU.

---

## 6. TLS termination

Vercel handles TLS at the edge for the static landing site (no
configuration needed).

For the backend:

- **Fly.io / Render / Railway:** TLS is terminated at the platform
  proxy. The container only sees HTTP. Don't expose port 8742
  directly.
- **Self-host:** Put Caddy or nginx in front of the container:

  ```caddyfile
  captainapply.com {
      reverse_proxy localhost:8742
  }
  ```

  Caddy auto-issues Let's Encrypt certs. No renewal scripts.

- **Direct Docker (testing only):** run with
  `ports: 8742:8742` and a Cloudflare proxy in front. Cloudflare
  terminates TLS and forwards HTTPS to the origin.

---

## Cheat sheet

```bash
# Deploy static landing
vercel --prod

# Deploy backend (Fly)
fly deploy

# Set a new secret
fly secrets set CAPTAIN_SESSION_SECRET="$(openssl rand -hex 32)"

# Tail backend logs
fly logs

# Roll back a bad deploy
fly releases list
fly releases rollback <version>
```

Questions? Open an issue. The deploy was tested end-to-end with the
`scripts/check-deploy-readiness.sh` script.
