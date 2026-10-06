# JobPipeline Platform — Commercial SaaS Plan

> **For Lars:** This is a multi-quarter roadmap, not a single sprint. It's designed so Ye can read it once, pick a phase, and execute that phase without re-deriving context.

**Goal:** Turn the personal Lars-Zimmermann job-application pipeline into a multi-tenant SaaS that other job seekers pay for. Ship two flavours: (a) a self-hostable Docker image (closed beta), (b) a fully-managed cloud tier once Ye have 20+ paying customers.

**Architecture:** Refactor the existing single-user `data/jobs.db` + global config into multi-tenant: each user gets isolated SQLite database, isolated `data/batches/<user_id>/` PDF directory, embedded HTTP auth (not just shared Basic), Stripe-backed billing via `customers` + `subscriptions` tables, and a Docker image with multi-process support (uvicorn web + cron per-scrape + Discord webhook endpoint).

**Tech Stack:** FastAPI + SQLite (per-user) + Jinja2 templates + Stripe (Customer Portal) + Docker + GitHub Actions for CI + (optional) Fly.io / Railway for managed cloud tier. The web UI is already done (the v2 OD design in `web/static/`).

---

## Phase 0 — Product positioning (≈₠1-2 days of conversation, no code)

**Do this before Phase 1.** Every later decision depends on it.

### 0.1 Target customer

Default assumed (write assumptions to RISKS section if You want different):

| Lane | Profile | Salary band |
|---|---|---|
| Primary | DACH Senior IC (Marketing/BD/Digital/Product) | €55-80k |
| Secondary | EU-remote English-speaking Senior IC | €60-90k |
| Out of scope v1 | US/UK Senior PM, junior roles, executive roles | — |

Reasoning: the pipeline's scoring weights (`config/lars.yaml`) are already tuned for the DACH + EU remote market. Expanding US+ CE/UK requires salary-floor recalibration (USD 80k+ vs €60-80k), source mix (LinkedIn > XING), and timezone handling. Not impossible but doubles v1 scope.

### 0.2 Business model

**Recommended:** Hybrid — self-serve SaaS as the main product, with a `done-for-you` retainership as a premium add-on at €297/mo.

Reasoning: Lars's network (Bold and Digital clients, ex-Strichpunkt colleagues, Munich/Berlin/Vienna startup founders) is high-trust + high-discount sensitive. Pure self-serve SaaS at €49/mo needs 100+ customers to match a single €297 retainership. Hybrid is the right shape for first 50 customers, then pure SaaS after month 6.

| Tier | Price | What they get |
|---|---|---|
| **Free (lifetime for first 100 users)** | €0 | 10 jobs/day quota, 1 CV template, manual apply only (download CV + letter, no CUA) |
| **Solo** | €19/mo or €190/yr | 50 jobs/day, all CV templates, manual apply, daily digest email, Discord/Telegram notif |
| **Pro** | €49/mo or €490/yr | Everything in Solo + CUA auto-apply (browser fills forms, pauses for user click), unlimited jobs |
| **Captain's Service** | €297/mo | Everything in Pro + Lars (or a trained VA) personally reviews 3 CVs/quarter, places 5 manual applications, prioritizes 5 leads/quarter |
| **Self-host Docker** | €99 one-time | Full source code under AGPL, run on own server, 1 year of updates |

### 0.3 What NOT to build v1

Out of scope (save for v2):
- Mobile app (web is mobile-responsive)
- Multi-CVs per job (1 CV + 1 letter per job, ATS-safe is the point)
- LinkedIn easy-apply integration (separate ATS adapter, separate cost)
- Resume-review marketplace (would need human reviewer pool — too much v1)
- AI-generated cover letters (current pipeline uses templated paragraphs, not LLM — good, keeps costs low)

### 0.4 Open questions for Lars

Write answers into the RISKS section below once you decide:

1. **Brand name?** Suggested: **CaptainApply** (`.com`, `.io`, `.app` defensible), or **JobBridge** (more literal), or **FirstMast** (DACH-premium feel). Run DNS + TM screen before committing — see `brand-naming-and-domains` skill.
2. **Domain registrar?** Cloudflare (at-cost) recommended. €60-100/yr for 4 TLDs.
3. **Stripe account under which?** Bold and Digital LLC (USA LLC for Stripe Atlas) or set up HostSalt EU entity?
4. **GitHub org?** Personal `boldandigital` (already created `job-pipeline`) or new `captainapply` org?

---

## Phase 1 — Multi-tenant refactor (≈₠2-3 weeks)

### 1.1 Architecture: per-user SQLite, shared Postgres schema

**Decision:** Per-user SQLite database (one file per user under `data/users/<user_id>/jobs.db`). Shared Postgres only for billing + auth metadata (at `/api/v1/auth/login`).

Reasoning:
- SQLite per user = same isolation as Postgres row-level security, but **zero infra cost** for self-hosted Docker
- Lars's existing `data/jobs.db` becomes the template, copied per user on signup
- Batches directory becomes `data/users/<user_id>/batches/`
- Stripe webhooks (in Phase 3) only touch Postgres, never SQLite

### 1.2 Tasks

#### Task 1.1: Add `users` table to Postgres + migration script

**Objective:** Track user identity, plan tier, Stripe customer id.

**Files:**
- Create: `src/db/models.py` — SQLAlchemy ORM for Postgres
- Create: `src/db/migrations/001_init.sql` — initial schema
- Modify: `requirements.txt` — add `sqlalchemy>=2.0`, `psycopg2-binary>=2.9`, `alembic>=1.13`

**Step 1-5** (full TDD per `plan` skill): Write test → verify fail → migrate → write model → verify pass → commit.

```sql
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,        -- bcrypt
    name TEXT,
    locale TEXT DEFAULT 'en',           -- 'en' or 'de'
    plan TEXT DEFAULT 'free',           -- 'free', 'solo', 'pro', 'captain'
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    is_active BOOLEAN DEFAULT TRUE
);

CREATE INDEX users_email_idx ON users(email);
CREATE INDEX users_stripe_idx ON users(stripe_customer_id);
```

#### Task 1.2: Per-user SQLite factory

**Objective:** Replace global `DB_PATH = PROJECT_ROOT / "data" / "jobs.db"` with `get_user_db(user_id)` that returns `Path("data/users/<user_id>/jobs.db")`.

**Files:**
- Modify: `src/web/app.py` (every `_connect()` call)
- Modify: `src/pipeline/batch_pipeline.py` (every `sqlite3.connect` callsite — there are ~5)
- Modify: `src/generation/cv_generator.py`, `cover_letter_generator.py`, `ats_templates.py` (env-var based DB access)

**Pattern:** All DB access goes through `src/db/jobs_db.py` which exposes:
```python
def get_jobs_db(user_id: str) -> sqlite3.Connection: ...
def get_jobs_path(user_id: str) -> Path: ...
def get_batches_dir(user_id: str) -> Path: ...
```

This is a 1-2 day refactor but unblocks all multi-tenancy.

#### Task 1.3: Per-user batches directory + CV data

**Objective:** Move `data/batches/<date>/<company>/` to `data/users/<user_id>/batches/<date>/<company>/`. Move `config/lars-cv-data.json` to `data/users/<user_id>/profile.json` (1 file per user, NOT shared).

**Files:**
- Modify: `src/db/jobs_db.py` (add `get_user_profile_path`)
- Create: `src/web/profile_routes.py` — GET/PUT `/api/v1/profile`
- Modify: `src/pipeline/batch_pipeline.py` (read profile from user path)

**Why profile-per-user:** Every customer has different languages, salary floor, contact info, experience. Lars's CV is just one example.

#### Task 1.4: Auth: replace Basic with session cookies + JWT

**Objective:** HTTP Basic auth (`LARS_USER=captain`) is unacceptable for paid customers. Use `itsdangerous` (already in FastAPI deps) for signed session cookies, backed by Postgres `users` table.

**Files:**
- Create: `src/web/auth.py` — `login`, `logout`, `signup`, `_get_current_user(request)`
- Modify: `src/web/app.py` — replace `_require_auth` with `Depends(get_current_user)`
- Modify: `src/web/template.py` — JS no longer needs `?token=` query auth, sends session cookie

**Pattern:**
```python
async def get_current_user(request: Request) -> User:
    session_id = request.cookies.get("session")
    if not session_id:
        raise HTTPException(401, "login required")
    user = await db.get_user_by_session(session_id)
    if not user:
        raise HTTPException(401, "session expired")
    return user
```

PDF iframe auth: server-rendered signed URL that expires in 60 seconds. No more `?token=` URL.

#### Task 1.5: Refactor template.py — no auth tokens in HTML

**Objective:** Remove `AUTH_TOKEN` from `src/web/template.py`. Update all `btoa(...)` calls. Use signed URLs instead.

**Files:**
- Modify: `src/web/template.py`
- Modify: `src/web/app.py` — add `/api/v1/batches/{signed_url_token}/{filename}` endpoint that verifies signed URL + serves PDF

#### Task 1.6: Multi-tenant tests

**Objective:** No user can see another user's jobs.

**Files:**
- Create: `tests/test_multi_tenant.py` — 8 tests covering:
  - User A approves job → User B sees it as `new` (status is per-user, not global)
  - User A's PDF preview URL returns 403 when accessed by User B
  - User A's `/api/v1/profile` returns User A's data, not User B's
  - Stripe webhook for User A's subscription expiry doesn't deactivate User B
  - Session cookie for User A doesn't grant access to User B's dashboard

Run `pytest tests/test_multi_tenant.py -v` — expected 8 passed.

### 1.3 Phase 1 completion criteria

- [ ] `src/db/jobs_db.py` is the single source of DB paths
- [ ] All `sqlite3.connect(PROJECT_ROOT / "data" / "jobs.db")` calls removed from codebase
- [ ] `tests/test_multi_tenant.py` passes
- [ ] `bin/webapp.sh --user=lars` still works (backward compat for self-host)
- [ ] Single Postgres instance runs in Docker Compose alongside the app
- [ ] Per-user SQLite files are gitignored but per-user *template* lives at `data/users/_template/`

---

## Phase 2 — Billing & subscription tiers (≈₠1-2 weeks)

### 2.1 Stripe setup

**Decision:** Use Stripe Checkout + Customer Portal (NOT Stripe Elements). 1-page conversion, Apple Pay/Google Pay built-in, 3DS handled.

**Steps:**
1. Create Stripe products + prices in dashboard:
   - `solo_monthly`: €19/mo, `solo_yearly`: €190/yr
   - `pro_monthly`: €49/mo, `pro_yearly`: €490/yr
   - `captain_monthly`: €297/mo
   - `selfhost_lifetime`: €99 one-time (deliver license key via email)
2. Store price IDs in `.env`: `STRIPE_PRICE_SOLO_M=price_xxx`, etc.
3. Test mode first, switch to live keys when ready to launch.

### 2.2 Tasks

#### Task 2.1: Stripe Checkout session endpoint

**Files:**
- Create: `src/billing/stripe_checkout.py`
- Modify: `src/web/app.py` — add `POST /api/v1/billing/checkout` endpoint
- Create: `tests/test_billing.py` — 6 tests

```python
@app.post("/api/v1/billing/checkout")
async def checkout(user: User = Depends(get_current_user), plan: str):
    price_id = PRICE_MAP[plan]
    session = stripe.checkout.Session.create(
        customer=user.stripe_customer_id,
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{BASE_URL}/billing/success",
        cancel_url=f"{BASE_URL}/billing/cancel",
    )
    return {"url": session.url}
```

#### Task 2.2: Stripe webhook handler

**Files:**
- Create: `src/billing/webhook.py`
- Modify: `src/web/app.py` — add `POST /api/v1/billing/webhook` endpoint (no auth — Stripe verifies signature)
- Test: 5 tests for `customer.subscription.created`, `.updated`, `.deleted`, `invoice.payment_failed`

**Critical:** Verify webhook signature with `stripe.Webhook.construct_event()` — reject everything else.

#### Task 2.3: Quota enforcement

**Files:**
- Modify: `src/db/jobs_db.py` — add `count_jobs_today(user_id)` returning today's job count
- Modify: `src/pipeline/scrapers/` — check quota before scraping, return `429` if exceeded
- Create: `tests/test_quotas.py` — 4 tests verifying 11th job on Free tier is rejected

Quota table:
| Tier | Jobs/day | CV templates | CUA auto-apply |
|---|---|---|---|
| Free | 10 | 1 (Classic) | No |
| Solo | 50 | All 4 | No |
| Pro | unlimited | All 4 + future | Yes |
| Captain | unlimited | All 4 + future | Yes + human review |

#### Task 2.4: Customer Portal redirect

**Objective:** Customers can update card, cancel, download invoices via Stripe-hosted portal.

**Files:**
- Modify: `src/billing/stripe_checkout.py` — add `create_portal_session(user)`

### 2.3 Phase 2 completion criteria

- [ ] Test card `4242 4242 4242 4242` can buy Solo Pro → user.plan updates to 'pro'
- [ ] Webhook sets user.is_active = false on `customer.subscription.deleted`
- [ ] Free user hitting 11 jobs/day gets 429 with helpful "upgrade to Solo" message
- [ ] Customer Portal link works for Pro user

---

## Phase 3 — Docker + self-hosting (≈₠1 week)

### 3.1 Existing assets

Ye already have `Dockerfile` + `docker-compose.yml`. They need updating.

### 3.2 Tasks

#### Task 3.1: Multi-process Dockerfile

**Objective:** One container runs:
- uvicorn web (port 8080)
- cron-driven scrape loop (Lars's existing `lars-daily-run.sh`)
- Discord webhook relay

**Files:**
- Modify: `Dockerfile` — install `supervisor` for multi-process
- Create: `docker/supervisord.conf`

#### Task 3.2: Docker Compose for self-host

**Objective:** Single `docker compose up` brings up:
- Web app (uvicorn)
- Postgres (for users + Stripe billing metadata)
- Optional: Watchtower for auto-updates from GitHub Container Registry

**Files:**
- Modify: `docker-compose.yml` — add Postgres service, named volumes

#### Task 3.3: GHCR publishing

**Objective:** Every push to `main` triggers `docker build` + `docker push ghcr.io/boldandigital/captainapply:latest`.

**Files:**
- Create: `.github/workflows/docker-publish.yml`

#### Task 3.4: Self-host license key flow

**Objective:** When `STRIPE_LICENSE_KEY` env var is present, Docker image unlocks Pro features. Without key, it runs as Free forever.

**Files:**
- Modify: `src/web/app.py` — at startup, if `STRIPE_LICENSE_KEY` env matches a Stripe-generated JWT, set `app.state.license_tier = 'pro'`
- Create: `scripts/generate_license.py` — Stripe webhook creates license JWT on `selfhost_lifetime` purchase

### 3.3 Phase 3 completion criteria

- [ ] `docker compose up` brings up working stack on a Hetzner 5500 (8GB) VPS in <2 min
- [ ] `docker push ghcr.io/boldandigital/captainapply:latest` works from main
- [ ] License key unlocks Pro features in Docker mode
- [ ] Self-host docs at `docs/SELF_HOST.md` walk through VPS setup in <30 min

---

## Phase 4 — Cloud tier (managed multi-tenant) (≈₠2 weeks)

This is the SaaS version where You run the infra, customers sign up on Ye.r domain.

### 4.1 Platform choice

**Recommended:** Fly.io (simple Docker deploy, EU regions, PostgreSQL built-in, $5/mo minimum). Alternative: Railway (similar). NOT AWS — overkill for v1.

Reasoning: Fly.io has `fly machines` that scale to zero, EU-Frankfurt region (data residency for DACH customers), $1.94/mo for shared CPU, $0.20/GB Postgres. = $20/mo infrastructure to support first 100. 100 paying Pro customers at €49/mo = €4900/mo gross, $400-800 in infra + Stripe fees + email = €4000+/mo net.

### 4.2 Tasks

#### Task 4.1: Fly.io deployment config

**Files:**
- Create: `fly.toml`
- Create: `Dockerfile.cloud` — production variant (no SQLite per-user fallback, requires Postgres)

#### Task 4.2: Customer signup + email verification

**Objective:** `/signup` form → email with verification link → /verify/<token> → first dashboard view.

**Files:**
- Create: `src/web/signup.py` — POST /api/v1/signup, GET /api/v1/verify/{token}
- Modify: `src/web/app.py` — add public `/signup` route
- Add: `src/email/resend_client.py` for verification emails (Resend.com, $20/mo for 50k emails)

#### Task 4.3: Marketing site

**Decision:** Single-page marketing site at the root domain, app at `app.<domain>`. Static HTML + Tailwind. Hosted on the same Fly.io deployment.

**Files:**
- Create: `static/marketing/index.html` — hero, 3-step "How it works", pricing table, FAQ, footer
- Modify: `src/web/app.py` — if request host is `<root>` and path is not `/api/*` or `/app/*`, serve marketing

#### Task 4.4: Daily digest email

**Objective:** Every morning at 8am local time, send email with top 5 jobs for that day (scored by pipeline).

**Files:**
- Create: `src/email/digest.py` — template + dispatch
- Modify: `src/pipeline/daily_runner.py` — schedule digest per user

#### Task 4.5: Privacy + GDPR compliance

**Decision:** DACH customers = GDPR required. Data residency in EU-Frankfurt Fly.io region.

**Files:**
- Create: `docs/PRIVACY.md`, `docs/TERMS.md`, `docs/DPA.md` (Data Processing Agreement)
- Modify: `src/web/app.py` — `/api/v1/account/delete` endpoint that hard-deletes user data within 24h

### 4.3 Phase 4 completion criteria

- [ ] Customer can sign up at `https://captainapply.com/signup`, receive verification email, click link, land on dashboard with empty job list
- [ ] Free tier user gets 10 jobs/day, quota enforced at scrape time
- [ ] Upgrading to Solo via Stripe Checkout flips user.plan to 'solo' within 30 seconds of payment
- [ ] Customer receives daily email at 8am with their top 5 jobs
- [ ] `/api/v1/account/delete` removes user data, returns 204

---

## Phase 5 — Growth + integrations (post-launch, ongoing)

Out of scope for v1. Plan these after seeing first 50 customers.

### 5.1 Candidate list (in priority order)

1. **LinkedIn Easy Apply integration** — biggest gap, but ATS adapter is non-trivial (OAuth flow + rate limits)
2. **Multi-CVs per job** — when customer has 3 different careers they want to position for
3. **Mobile app** — only if mobile traffic > 40% of dashboard sessions
4. **AI-generated cover letters** — only if cost/letter stays < €0.10 (use local LLM, not OpenAI)
5. **Calendar integration** — for Captain's Service tier to schedule CV-review calls
6. **Discord bot** — Ye already have Hermes Discord, embed `/jobs` command

### 5.2 Compliance milestones

- **Month 3:** GDPR audit (use https://gdpr.eu/checklist/)
- **Month 6:** SOC2 Type 1 (required for selling to enterprise HR teams)
- **Month 12:** ISO 27001 (only if enterprise customers require it)

---

## Files likely to change (summary)

### Create
```
src/db/
  __init__.py
  models.py                   # SQLAlchemy ORM (Postgres)
  jobs_db.py                  # Per-user SQLite factory
  migrations/
    001_init.sql              # users + stripe_subscriptions tables

src/billing/
  __init__.py
  stripe_checkout.py
  webhook.py

src/email/
  __init__.py
  resend_client.py
  digest.py
  templates/
    digest.html

src/web/
  auth.py                     # Session-based auth
  profile_routes.py           # /api/v1/profile
  signup.py                   # /api/v1/signup + /verify

docs/
  SELF_HOST.md
  PRIVACY.md
  TERMS.md
  DPA.md

static/marketing/
  index.html                  # Marketing site
  pricing.html

.github/workflows/
  docker-publish.yml
  test.yml
  deploy-fly.yml

fly.toml
Dockerfile.cloud
docker/supervisord.conf
scripts/generate_license.py
tests/test_multi_tenant.py
tests/test_billing.py
tests/test_quotas.py
tests/test_signup.py
```

### Modify (heavy refactor)
```
src/web/app.py                 # session auth, multi-tenant routing
src/web/template.py            # remove AUTH_TOKEN
src/pipeline/batch_pipeline.py # per-user DB + batches
src/generation/                # all read user profile via jobs_db factory
Dockerfile
docker-compose.yml             # add postgres
requirements.txt               # +sqlalchemy +psycopg2 +alembic +resend +itsdangerous
```

### Modify (small)
```
config/lars.yaml               # becomes default config, users override
config/lars-cv-data.json       # moved into per-user data/users/_template/profile.json
scripts/lars-daily-run.sh      # becomes per-user runner
```

---

## Tests + validation

| Layer | Test target | Expected |
|---|---|---|
| Multi-tenant isolation | `tests/test_multi_tenant.py` | 8 passed |
| Billing webhook idempotency | `tests/test_billing.py` | 6 passed |
| Quota enforcement | `tests/test_quotas.py` | 4 passed |
| Self-host Docker | manual `docker compose up` on Hetzner | 5 min from zero to dashboard |
| Cloud tier signup | manual Playwright test on fly.io | 2 min from signup to first job |
| GDPR data deletion | manual API test + Postgres query | user rows + SQLite files removed |

Total new tests: ~25 (target 493 total, from current 468)

---

## Risks + tradeoffs

### Brand + legal (Phase 0)
- **Risk:** Brand name conflicts with existing trademark in EUIPO. **Mitigation:** Run TM screen in Phase 0.1 before any code.
- **Risk:** Stripe Atlas USA LLC for Bold and Digital = US tax filing burden. **Mitigation:** Consider HostSalt BV (NL) for EU-only customers to defer US compliance.
- **Open:** Which brand name? My pick is **CaptainApply** (matches the in-product persona, easy to spell, defensible across .com/.io/.app/.co).

### Multi-tenant refactor (Phase 1)
- **Risk:** Existing single-user code has global state everywhere (`PROJ_ROOT`, `DB_PATH`, env vars). Refactoring 30+ callsites is a big change. **Mitigation:** Phase 1.2 introduces `get_user_db()` factory first, then migrates callsites one at a time. Each migration = 1 commit + 1 test.
- **Risk:** Per-user SQLite files in `data/users/<id>/jobs.db` become backup nightmare. **Mitigation:** Daily cron tars + encrypts `data/users/<id>/` and ships to S3. ~30 lines of bash.

### Billing (Phase 2)
- **Risk:** Stripe webhook signature verification breaks when behind reverse proxy. **Mitigation:** Always use `request.url` not `request.headers["Host"]` for absolute URL in webhook handler.
- **Risk:** Customer disputes a charge, Stripe refunds, but user data still exists. **Mitigation:** webhook handler on `charge.dispute.created` immediately sets `user.is_active = false` until dispute resolved.

### Docker (Phase 3)
- **Risk:** Browser-based auto-apply needs Chromium, ~300MB image. **Mitigation:** Multi-stage Dockerfile separates Chromium install from app code. Total image ~600MB, acceptable for self-host.

### Cloud (Phase 4)
- **Risk:** Fly.io EU-Frankfurt pricing scales linearly with customers — 200 customers = $400/mo infra. **Mitigation:** At 100 customers, evaluate moving to Hetzner dedicated (€50/mo for 8GB + 4 cores, runs everything including Postgres on bare metal).
- **Risk:** GDPR requires data export on request. **Mitigation:** `/api/v1/account/export` endpoint returns ZIP of user data + JSON profile.

### Scope creep (Phase 5)
- **Risk:** Customers ask for features that bloat v1. **Mitigation:** "v1 is fixed" rule — only ship what's in this plan. New features go to v2 backlog.

---

## Time + cost estimate

| Phase | Wall clock | Engineer effort | Infra cost |
|---|---|---|---|
| Phase 0 (positioning) | 1-2 days | 4 hours | €0 |
| Phase 1 (multi-tenant) | 2-3 weeks | 2 weeks | €0 (dev), €20/mo (staging) |
| Phase 2 (billing) | 1-2 weeks | 1.5 weeks | €0 (Stripe test mode) |
| Phase 3 (Docker) | 1 week | 4-5 days | €0 |
| Phase 4 (cloud) | 2 weeks | 2 weeks | €50/mo (Fly.io staging) |
| **Total to launch** | **7-10 weeks** | **6-8 weeks** | **€70-100/mo infra** |
| Phase 5 (growth) | ongoing | 0.5 FTE | €200-500/mo at 50-100 customers |

**Pricing math:** 100 paying customers × €49/mo Pro = €4900/mo gross. €500/mo infra + Stripe fees + Resend + domain = €700/mo cost. = €4200/mo net profit. At 200 customers = €8400/mo net.

**Path to €10k/mo profit:** ~250 paying customers, achievable in 6-9 months with light marketing (LinkedIn + Reddit r/jobs + IndieHackers).

---

## Decision needed before Phase 1

**Ye, Lars, need to confirm:**

1. **Brand name** — CaptainApply? JobBridge? Something else? (Use brand-naming-and-domains skill if uncertain)
2. **Business entity** — Bold and Digital LLC, HostSalt BV, or new entity?
4. **Self-host OR cloud-first** — start with Docker-only, or start with Fly.io?
3. **GitHub org** — personal `boldandigital/job-pipeline` or new `captainapply/platform`?

Answer those 4 in chat and I'll start Phase 1 (multi-tenant refactor) once Ye say go.

Yo ho! Smooth sailing! 🏴‍☠️