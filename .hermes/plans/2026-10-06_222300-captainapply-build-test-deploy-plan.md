# CaptainApply — Build/Test/Deploy Plan
*Written 2026-10-06 22:23 CEST*

## Captain's intent (one-sentence each)
1. **Build it first locally** — get the core functionality working end-to-end on this Mac
2. **Test for real** — exercise every flow, not just unit tests
3. **Deploy later** — Vercel/Fly.io after we know it works
4. **Wire Stripe later** — after deploy so we have live keys ready
5. **See designs in OD** — design exploration alongside the build
6. **More job portals + max automation** — research what else is out there (we did the GitHub scan)
7. **Best of GitHub** — copy the patterns that work

## Current state (verified)
- 588/588 tests pass
- Webapp live at http://127.0.0.1:8742/
- Docker 29.8.2 available — can run self-host image
- OD 0.24.1 daemon running on ephemeral port (verified)
- 8 features shipped across Phase 1.1–1.8

## Phase A — Local-first build verification (DONE)
**Goal:** confirm core loop works on this Mac without touching prod or Stripe.

| # | Step | Status |
|---|---|---|
| A1 | Run the selfhost Docker image locally | ✅ DONE |
| A2 | Smoke-test the container end-to-end | ✅ DONE |
| A3 | Run signup flow → verify page | ✅ DONE |
| A4 | Run jobs dashboard (per-user isolation) | ✅ DONE |
| A5 | Stop container, keep localhost dev server | ✅ DONE |
| A6 | Confirm OD designs visible in canvas | ✅ DONE |

**Result:** Live at `http://localhost:18742/` (Docker) and `http://127.0.0.1:8742/` (dev). Both tested end-to-end with real jobs.

## Phase B — OD designs (DONE)
**v3 4-gate dashboard:** generated via OD routine `routine-9a3e8435-df93-4a4a-980a-90e3e133b387`, project `routine-b54bba0d-2c5c-4288-ba94-36e1a0036317`. Source-of-truth at `brand/captainapply/dashboards/v3-4gate-approval/index.html`. Now LIVE in the webapp at `web/static/index.html`.

## Phase C — More portals research (DONE)
9 comparable GitHub projects surveyed. Universal patterns documented. Greenhouse/Lever/Ashby public APIs are the obvious next add.

## Phase D — Decide what to copy (DONE)
Decision card not needed: 4-gate dashboard + per-user isolation already covers the Ye.r unique-safety-rule. Greenhouse/Lever/Ashby direct API discovery is the obvious next add (3-5 days).

## Phase E — Deploy (PENDING)
After Ye're satisfied with local test.

## Phase F — Stripe wire (PENDING)
After deploy.

## What's now live (as of 2026-10-06 23:50)

| Feature | Status |
|---|---|
| Per-user DB isolation | ✅ Phase 1.0 |
| Multi-tenant schema bootstrap | ✅ Phase 1.0 |
| Session-cookie auth | ✅ Phase 1.1 |
| Marketing landing page | ✅ Phase 1.3 |
| Signup + email verification | ✅ Phase 1.2 |
| Self-host Docker image | ✅ Phase 1.5 |
| Stripe Checkout + quota | ✅ Phase 1.4 (dev mode) |
| XING Apply Assistant | ✅ Phase 1.7 |
| Job unblock (Lightcone + cfab) | ✅ Phase 1.8 |
| **v3 4-gate progressive approval** | ✅ Phase 1.9 (just shipped) |
| 603/603 tests green | ✅ |
| Localhost + Docker both tested | ✅ |

## Next moves (per Ye.r "yes to all")
- Ye takes the dashboard for a spin at http://127.0.0.1:8742/
- If Ye find issues, tell me what breaks
- Then: build Greenhouse/Lever/Ashby API discovery (Phase 2.1)
- Then: deploy (Phase 2.2)
- Then: wire real Stripe keys (Phase 2.3)

| Project | URL | ATS | Apply mode | Key idea |
|---|---|---|---|---|
| devdattatalele/auto-apply | github.com/devdattatalele/auto-apply | 12 ATSs incl Greenhouse/Ashby/Lever/Workday | Playwright + self-learning + Claude Code | Best for **scrape + fill** |
| joeyspagnoli/agentic-job-applier | github.com/joeyspagnoli/agentic-job-applier | Greenhouse + Ashby (auto) | FastAPI + React + Simplify autofill | Best **for SaaS pattern** (matches ours) |
| liruihan000/claude-job-auto-apply | github.com/liruihan000/claude-job-auto-apply | 10+ ATS | CC subagent parallelism | **Subagent parallelism** |
| yvandanasrisai/job-radar-2.0 | github.com/yvandanasrisai/job-radar-2.0 | Greenhouse/Lever/Ashby/Workday/iCIMS/SuccessFactors | Playwright + Review gate | **"fill never submits" rule** (matches us) |
| thisisvk45/Jobforge | github.com/thisisvk45/Jobforge | Greenhouse/Lever/Ashby/Workday | Playwright + stealth + humanizer | Beta, not production |
| Ankush523/job_applyer (Velto) | github.com/Ankush523/job_applyer | Greenhouse/Lever/Ashby/Workday | Playwright + IMAP sync | Ollama local LLM |
| Null-Phnix/jobhound-mcp | github.com/Null-Phnix/jobhound-mcp | Ashby/Greenhouse/Lever | MCP for Claude Code | MCP-based discovery |
| Abdrakib/job-agent | github.com/Abdrakib/job-agent | Greenhouse + Lever only | Public API | JSearch + Claude |
| nperry248/JobHunter | github.com/nperry248/JobHunter | Greenhouse + Lever | Playwright | Claude tool-use orchestrator |

**Job portals we can use:**

| Portal | API | Method | Free? | Coverage |
|---|---|---|---|---|
| **Greenhouse** | Public JSON | API direct | Free | 5000+ companies |
| **Lever** | Public JSON | API direct | Free | 1500+ companies |
| **Ashby** | Public JSON | API direct | Free | 1500+ companies (OpenAI, Anthropic, etc.) |
| **Workday** | Per-tenant API | tenant-by-tenant | Free but flaky | Big corps |
| **SmartRecruiters** | Public API | API direct | Free | 1000+ companies |
| **Recruitee** | Public API | API direct | Free | SMB |
| **BambooHR** | Per-company | varies | varies | Some companies |
| **Personio** | Per-company | scraping | varies | DACH heavy ← **ours** |
| **XING** | no API | XING scraping (we have it) | paid login | DACH/EU |
| **LinkedIn** | no public API | scraping (auth) | against ToS | US/EU |
| **Indeed** | no public API | scraping | against ToS | US heavy |
| **StepStone** | no API | scraping | paid login | DACH |

**Automation ladder (max → min):**

1. **API-direct** (Greenhouse/Lever/Ashby) — push JSON, get response, done
2. **Playwright + profile.json** (Workday/SmartRecruiters) — Playwright fills, pauses for Submit
3. **Clipboard + iframe** (XING — work our current path) — Ye.r hands click Submit
4. **Email** (Andercore) — Ye.r hands click Send
5. **Manual paste** (LinkedIn/Indeed) — Ye.r hands copy fields

## Phase D — Decide what to copy (decision card)
Based on the GitHub research, here's what to add to CaptainApply:

| Feature | Source | Worth adding? | Effort |
|---|---|---|---|
| Greenhouse/Lever/Ashby public API discovery | All 9 projects | **YES, Phase 2** | 3-5 days |
| Playwright form-filler for Workday/SmartRecruiters | job-radar-2.0 + devdattatalele | **YES, Phase 3** | 5-7 days |
| Deterministic-replay submit (fil = review = submit, zero LLM) | job-radar-2.0 | **YES, easy** | 1 day |
| Playwright-stealth + fingerprint randomization | Jobforge | **NO** — we never auto-submit |
| CAPTCHAsolver | devdattatalele | **NO** — manual-only flow |
| Subagent parallelism | liruihan000 | **MAYBE Phase 5** | 2 weeks |
| Ollama local LLM for ranking | Velto | **YES, fast mode** | 1 day |
| MCP server (jobhound pattern) | jobhound-mcp | **YES, nice DX** | 3 days |

## Phase E — Deploy (after Phase A passes)
1. `docker compose -f docker-compose.selfhost.yml up -d` — runs on localhost
2. Ye.r hands eyeball at `http://localhost:8742`
3. When happy: Vercel `vercel --prod` for landing + Fly.io for backend

## Phase F — Stripe wire (after deploy)
1. Get real Stripe keys (Ye.r hands)
2. Set `STRIPE_SECRET_KEY=*** + `STRIPE_WEBHOOK_SECRET=*** in prod env
3. Run `stripe listen --forward-to localhost:8742/api/v1/billing/webhook` for local testing
4. Update `quota.py` if pricing tiers change

## Decision points for Lars (one at a time)
- D1: Which design variants in OD should we keep? (after Phase B)
- D2: Which portals to add first? (after Phase C research)
- D3: Self-host first OR deploy first? (recommend: self-host first, per Ye.r words)
- D4: When to wire Stripe? (recommend: after deploy is live)

---

## Current execution path (just for this turn)

Phase A1 — Run the selfhost Docker image locally and smoke-test it end-to-end.

That's the immediate next thing — proves "the core functionality is working" with a real Docker deployment.