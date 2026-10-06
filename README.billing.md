# Phase 1.4 — Stripe billing

This is the subscription layer for CaptainApply. It adds four HTTP
endpoints, a quota helper, and a webhook receiver, all built on the
official `stripe` Python SDK and gated behind a **dev-mode** toggle so
the app can run end-to-end without ever talking to the live Stripe API.

---

## What's in this phase

| Endpoint                            | Method | Purpose |
|-------------------------------------|--------|---------|
| `/api/v1/billing/checkout`          | POST   | Create a Stripe Checkout session for `plan ∈ {solo, pro}`. Returns `{url, session_id}`. In dev mode the URL points at `/api/v1/billing/mock-checkout`. |
| `/api/v1/billing/portal`            | POST   | Create a Stripe Customer Portal session for the current user. |
| `/api/v1/billing/mock-checkout`     | GET    | **DEV-only.** Confirmation page that synthesises a `checkout.session.completed` webhook and applies it — so dev users see the same flow prod users do. 404s in production. |
| `/api/v1/billing/webhook`           | POST   | Stripe webhook receiver. Verifies the signature (or skips in dev), dispatches to `handle_event()`. |
| `/api/v1/billing/me`                | GET    | Current user's plan + limits + usage. The dashboard renders upgrade CTAs off this. |

Plus:
- `X-Plan`, `X-Usage-Jobs-Used`, `X-Usage-Jobs-Cap`, `X-Usage-Near-Limit`,
  `X-Usage-Exhausted` headers on `/api/jobs` (soft-warn, no 403).
- `set_plan(uid, plan)` on the users DB — writes from the webhook handler
  are belt-and-braced against ever demoting the bootstrap admin ("lars").

---

## Quick start (dev mode)

The defaults just work — leave `STRIPE_SECRET_KEY=test` (or set it to any
`sk_test_…` value) and the app will route every Checkout call to the
local mock endpoint, which in turn synthesises a webhook event and feeds
it through the same handler a real Stripe would hit. No network. No API
quota. No card required.

```bash
# 1. Activate the venv
cd ~/Documents/Projects/job-pipeline
source .venv/bin/activate

# 2. (optional) install stripe
pip install -r requirements.txt

# 3. Run the server
uvicorn src.web.app:app --host 0.0.0.0 --port 8742 --reload

# 4. In a browser, sign up → click "Start 14-day trial" on a pricing card
#    → you land on the mock-checkout "Welcome aboard" page → /me now
#    shows plan=solo (or pro) and 50 (or unlimited) job slots.
```

The default `.env.example` already has `STRIPE_SECRET_KEY=test`, so dev
mode is on by default. To prove it's wired up correctly:

```bash
# In one terminal: run the server
uvicorn src.web.app:app --port 8742

# In another: log in as a verified user, then ask for a checkout URL
COOKIE=$(curl -s -c - -X POST localhost:8742/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"lars","password":"captain"}' \
  | awk '/captain_session/ {print $7}')

curl -s -X POST localhost:8742/api/v1/billing/checkout \
  -H "Cookie: captain_session=$COOKIE" \
  -H 'Content-Type: application/json' \
  -d '{"plan":"solo"}'
# → { "ok": true, "url": "/api/v1/billing/mock-checkout?session_id=…", "session_id": "…" }

# Follow the URL — the server fires a synthetic webhook, then returns
# the "Welcome aboard" HTML page.
```

---

## Local testing — fire a mock webhook by hand

Sometimes you want to test a specific event shape (e.g. an `invoice.payment_failed`)
without going through the full Checkout flow. You can `POST` the event
JSON directly to the webhook endpoint — dev mode skips signature
verification, so any well-formed JSON works.

```bash
# 1. Log in as a verified user
COOKIE=$(curl -s -c - -X POST localhost:8742/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"<your-user-id>","password":"<your-password>"}' \
  | awk '/captain_session/ {print $7}')

# 2. Send a fake subscription-updated event
curl -X POST localhost:8742/api/v1/billing/webhook \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "evt_test_001",
    "type": "customer.subscription.updated",
    "data": {
      "object": {
        "status": "active",
        "customer": {"metadata": {"user_id": "<your-user-id>"}},
        "items": {"data": [{"price": {"id": "price_pro_test"}}]}
      }
    }
  }'

# 3. Verify the plan flipped
curl -s localhost:8742/api/v1/billing/me \
  -H "Cookie: captain_session=$COOKIE" | jq .plan
# → "pro"
```

---

## Production setup

### 1. Create Stripe products + prices

1. Open <https://dashboard.stripe.com/products>.
2. Create two recurring products:
   - **Solo** — €19 / month, recurring
   - **Pro**  — €49 / month, recurring
3. Copy the `price_…` IDs for each.

### 2. Configure environment

```bash
# .env (NEVER commit)
STRIPE_SECRET_KEY=sk_live_…               # from https://dashboard.stripe.com/apikeys
STRIPE_WEBHOOK_SECRET=whsec_…             # see step 3
STRIPE_PRICE_SOLO=price_…                 # from step 1
STRIPE_PRICE_PRO=price_…
STRIPE_SUCCESS_URL=https://captainapply.com/app/?upgraded=1
STRIPE_CANCEL_URL=https://captainapply.com/?cancelled=1
```

### 3. Register the webhook endpoint

In the Stripe dashboard:

1. Go to <https://dashboard.stripe.com/webhooks> → **Add endpoint**.
2. URL: `https://<your-domain>/api/v1/billing/webhook`
3. Events to send:
   - `checkout.session.completed`
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.payment_failed`
4. Copy the **Signing secret** (`whsec_…`) into `STRIPE_WEBHOOK_SECRET`.
5. (Optional but recommended) Switch to **EU-Frankfurt** as the data
   residency region under *Settings → Business settings → Data
   residency* — see GDPR note below.

### 4. Smoke-test the live webhook

```bash
# From the Stripe dashboard → Webhooks → your endpoint → "Send test event"
# → pick checkout.session.completed → Send test event.
#
# Or via the Stripe CLI:
stripe trigger checkout.session.completed
stripe listen --forward-to https://<your-domain>/api/v1/billing/webhook
```

You should see the user's `plan` column flip in the users DB.

### 5. Customer Portal

The Customer Portal is enabled by default. Tune branding, return URL,
and cancellation behaviour under *Settings → Customer Portal*.

---

## Plan limits

| Tier   | €/mo | Max jobs (per day) | CV renders/day | Auto-apply | Telegram |
|--------|------|--------------------|----------------|------------|----------|
| Free   | 0    | 10                 | 5              | ❌         | ❌       |
| Solo   | 19   | 50                 | 50             | ❌         | ✅       |
| Pro    | 49   | unlimited          | unlimited      | ✅         | ✅       |
| Admin  | —    | unlimited          | unlimited      | ✅         | ✅       |

`max_jobs = -1` is the convention for "unlimited". The
`src/billing/quota.py` helpers (`check_jobs_quota`, `check_render_quota`,
`can_auto_apply`) all understand the sentinel. The `/api/jobs` response
exposes the cap via headers so the UI can render "5 / 10 used" without
a second round-trip; **no 403s are emitted in Phase 1.4** — we soft-warn
and add hard enforcement in 1.5.

The bootstrap admin (`user_id="lars"`) is hard-coded to `plan="admin"`
everywhere: `get_user_plan`, `set_plan`, and the webhook handler all
short-circuit if the incoming `user_id` is `lars`. The row is *never*
overwritten by a Stripe event.

---

## GDPR

Card data, invoices, and Stripe customer records are stored by Stripe
(we never see PANs). For full GDPR parity set Stripe to its
**EU-Frankfurt** data residency:

- Dashboard → Settings → Business settings → Data residency → Frankfurt.

We additionally:

- never log the webhook payload (only the event `type` and `id`)
- exclude the signing secret from the `/api/health` response
- delete the user row + per-user SQLite + batches on account deletion
  within 24h of an email request (handled by the account-deletion flow
  added in Phase 1.7)

The Data Processing Agreement is available on request at
<contact@boldandigital.com>.

---

## Module map

```
src/billing/
  __init__.py         docstring
  stripe_client.py    lazy SDK singleton, checkout/portal/webhook + event handler
  quota.py            plan limit table + check_* helpers
src/web/app.py        POST /api/v1/billing/{checkout,portal,webhook,me}
                      GET  /api/v1/billing/mock-checkout  (dev only)
                      usage headers on /api/jobs
src/db/users_db.py    set_plan(uid, plan)  — refuses to mutate "lars"
tests/test_billing.py 40 tests covering every layer (no live Stripe)
```

---

## Troubleshooting

| Symptom                                          | Cause / fix |
|--------------------------------------------------|-------------|
| `STRIPE_WEBHOOK_SECRET is not configured`        | Production mode + empty webhook secret. Set the env var, then retry the `Send test event` button in the dashboard. |
| `/api/v1/billing/checkout` 500 with "no such plan" | The `plan` field must be `solo` or `pro`. The `free` and `selfhost` tiers are not purchasable. |
| `invalid signature` from real webhook            | The signing secret in `.env` doesn't match the one shown on the Stripe dashboard. Re-copy the `whsec_…` value. |
| User upgraded on the dashboard but the app still says "free" | The webhook endpoint isn't reachable from the public internet. Check the dashboard → Webhooks → your endpoint → "Logs" tab for the HTTP status. |
| `/api/v1/billing/mock-checkout` returns 404       | You're hitting dev-mode-only in a production build (i.e. `STRIPE_SECRET_KEY` is `sk_live_…`). That's correct — production has no mock. |

