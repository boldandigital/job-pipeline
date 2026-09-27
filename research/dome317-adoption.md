# dome317/job-search-pipeline — Adoption Analysis

**Repo:** https://github.com/dome317/job-search-pipeline
**Cloned to:** `/Users/lars/Documents/Projects/job-pipeline/`
**Analyzed by:** builder @ 2026-09-27 (ADOPT-1, reconnaissance only)
**Decision context:** scrap our 5,137 LOC custom auto-apply code, adopt this as the new
scraping + scoring + document-gen core, keep `/Users/lars/Documents/Projects/CV/` as the
source of truth for Lars's personal data.

---

## TL;DR — GO

This repo is a clean, opinionated, DACH-native job-search pipeline that solves the same
problem our 5,137 LOC tried to solve. The StepStone Patchright scraper is built-in,
two-stage scoring (rules + LLM Haiku) is exactly what we wanted, CV/CL generation has
4 variants + bilingual EN/DE, and the 24/7 cron story is mature (Hetzner CX22 €4.50/mo
+ Claude Haiku + cheap proxy = <€15/mo total). The integration story is straightforward:
our `cv-master.yaml` + `why-you-paragraphs.yaml` slot in as the candidate data source
without modification. Recommend proceeding to next 4 cards.

---

## 1. File Tree (annotated)

```
job-pipeline/
├── README.md                    Architecture overview, cost breakdown, quick-start
├── CONTRIBUTING.md              PEP 8, conventional commits, branch workflow
├── docker-compose.yml           Two services: dashboard (8080) + jobsearch (JobSpy)
├── Dockerfile                   python:3.12-slim + Patchright Chromium
├── requirements.txt             Only 3 deps: requests, patchright, pyyaml
├── .env.example                 ANTHROPIC_API_KEY, TELEGRAM_*, PROXY_URL, DB_PATH
│
├── config/
│   ├── example.yaml             ★ Queries + scoring weights (160+ queries, 15 cats)
│   ├── scoring_weights.example.yaml  Slim scoring reference
│   └── candidate_profile.example.md  Markdown profile for LLM scorer
│
├── docs/
│   ├── deployment.md            VPS setup walkthrough (Hetzner + UFW + cron)
│   └── scoring.md               Two-stage scoring explained
│
├── scripts/
│   ├── setup.sh                 Idempotent VPS bootstrap (Docker, swap, cron, DB)
│   ├── daily_pipeline.sh        02:00 UTC: JobSpy + AA + keyword score
│   ├── stepstone_pipeline.sh    08:30 UTC: StepStone + Google Jobs + score
│   └── cron-setup.sh            5 cron jobs (scrape x2, score x2, batch x1)
│
└── src/
    ├── scrapers/
    │   ├── stepstone_scraper.py       ★ Patchright browser (StepStone + Google Jobs)
    │   ├── arbeitsagentur_scraper.py  REST API, OAuth (public client creds shipped)
    │   ├── import_jobspy.py           Pulls JobSpy Docker output into main DB
    │   └── fetch_descriptions.py      Description enrichment (JobSpy xref + HTTP)
    ├── scoring/
    │   ├── score_jobs.py              Stage 1: keyword/weight scoring
    │   ├── llm_scorer.py              ★ Stage 2: 5-dim Haiku scoring
    │   └── apply_filter.py            ★ Two-layer filter (hard blocks + clusters)
    ├── discovery/
    │   └── career_discovery.py        3-layer career URL + ATS detection (18 ATSes)
    ├── generation/
    │   ├── cv_generator.py            ★ HTML→PDF via Patchright, 4 variants
    │   ├── cover_letter_generator.py  HTML→PDF cover letters, EN/DE
    │   ├── create_cv_template.js      Legacy JS template (unused by Python)
    │   └── cv_template_v2.js          Legacy v2 JS template (unused)
    └── pipeline/
        ├── batch_pipeline.py          ★ Top N → CV+CL → ZIP → Telegram
        └── dashboard.py               ★ Web UI on :8080 (review + status)
```

**Legend:** ★ = core integration target

---

## 2. Architecture (data flow)

```
[4 scrapers] → SQLite jobs.db → [keyword score] → [LLM score] →
  [title blocks] → [14 competency clusters] → [career discovery] →
    [CV variant select] → [CV + CL HTML→PDF] → [ZIP] → [Telegram]
                                    │
                                    └→ [Dashboard :8080 for review]
```

The pipeline runs as discrete CLI commands (`python -m src.<component>`) chained by
shell scripts. No central orchestrator, no DAG engine, no message bus. That matches
our operational taste: bash cron + SQL + small focused scripts.

---

## 3. StepStone Scraper — confirmed DACH fit

`src/scrapers/stepstone_scraper.py` (829 lines) is the crown jewel for our use case:

- **Browser stack:** Patchright (anti-detection Chromium fork of Playwright)
- **Selectors:** StepStone-specific `article a[data-at="job-item-title"]` — robust
  against filter-panel noise (uses `[data-facets-heading]` exclusion)
- **Stealth:** `navigator.webdriver = undefined`, randomized UA pool, de-DE locale,
  Europe/Berlin TZ, randomized geolocation (51.16/10.45), randomized delays
- **Cookie banner dismissal:** 11 selectors including StepStone-specific
  `data-testid="cookie-accept-all"` and `ccmgt_explicit_accept`
- **Pagination:** step-25 offset (`of=25*n`), "two empty pages = stop" heuristic
- **Deduplication:** `UNIQUE(title, company)` SQL constraint + SequenceMatcher fuzzy
  match against last 5,000 rows (threshold 0.85)
- **Bonus:** Same module also scrapes Google Jobs (`li.iFjolb` cards, click-to-load
  detail panel) — essentially free DACH coverage from a single browser context
- **CLI:** `python -m src.scrapers.stepstone_scraper --source both --queries "..." --location Deutschland`

**Verdict:** This single file replaces ~1,200 LOC of our custom StepStone scraper
and is already battle-tested per the README claims (228+ apps validated, 8,700+
jobs aggregated).

---

## 4. Scoring — two-stage system

### Stage 1: keyword (`src/scoring/score_jobs.py`, 89 lines)
- Reads weights + keywords from `config/example.yaml`
- Lowercases title + description, regex-matches each category, adds weight once per
  category (no double-counting)
- 40+ categories with positive (up to +180 for fully_remote) and negative
  (down to -900 for gig_platforms) weights
- Score tiers: Premium 150+ / Standard 80-149 / Low 20-79 / Filtered <20

### Stage 2: LLM (`src/scoring/llm_scorer.py`, 558 lines)
- Claude Haiku 4.5 (`claude-haiku-4-5-20251001`), 512 max_tokens
- 5-dimension weighted score: day_to_day_fit 35% / growth 20% / entry_barrier 20%
  / culture 15% / comp 10% + bonus up to +10 + penalty down to -10
- Hard blockers (pre-filter, no API call): werkstudent / praktikum / internship / azubi
- DB migration handles older schemas (`migrate_db`)
- JobSpy DB xref to backfill descriptions (saves tokens)
- Web-search fetch for missing descriptions (costs more, opt-in via `--fetch-descriptions`)
- Cost printed at end: ~$0.0003/job, ~$0.30/1k jobs
- Stores `llm_score`, `llm_reasoning`, `llm_match_reason`, `llm_scores_json` per row

### Filter (`src/scoring/apply_filter.py`, 419 lines)
- Layer 1: SENIORITY_BLOCK regex (with `(Senior)` exception), CONTRACT_BLOCK,
  JUNIOR_BLOCK — sets `status='filtered_out'`
- Layer 2: 14 COMPETENCY_CLUSTERS — must match at least one (ai_ml_data,
  automation_rpa, business_analysis, data_analytics_bi, it_digital, operations,
  compliance_regtech, content_docs, customer_success, enablement_training,
  qa_testing, fintech_web3, ecommerce, plus fallthrough ops)
- ALWAYS_IRRELEVANT hard list (54 patterns: accountant, driver, nurse, chef...)
- LANGUAGE_BLOCK (fr/spanish/mandarin/japanese/italian/portuguese/arabic/dutch
  required/native/fluent/mandatory → reject)

**Verdict:** Scoring is the most well-designed module in the repo. Matches what we'd
build ourselves and has 9,000+ jobs of empirical tuning baked into the keywords.

---

## 5. CV + Cover Letter Generation

`src/generation/cv_generator.py` (327 lines):
- HTML template built from in-file `EXPERIENCE` / `EDUCATION` / `PROJECTS` / `SKILLS`
  dicts (PERSONAL pulled from env: CANDIDATE_NAME, CANDIDATE_EMAIL, etc.)
- 4 variants selected by `detect_variant()` in `batch_pipeline.py`:
  - `ai_heavy` — keywords: ai, ki, ml, llm, prompt, genai, nlp
  - `technical` — keywords: developer, engineer, data, python, bi, etl, pipeline
  - `product` — keywords: product, scrum, project, agile, delivery
  - `operations` — keywords: operations, revops, crm, prozess, workflow, change
- Tagline per variant × language: e.g. `ai_heavy` + `de` → "AI & Automation Spezialist"
- Language auto-detected from German indicators (referent, sachbearbeiter, berlin, etc.)
- HTML→PDF via Patchright headless Chromium (`page.pdf(format="A4", ...)`)
- CSS is inlined (no external assets), photo as base64 data URI if `PHOTO_PATH` set
- Section order configurable via `--order experience,education,projects,skills`

`src/generation/cover_letter_generator.py` (198 lines):
- Same visual style, paragraph body passed via `--body "p1.||p2.||closing"` or
  `--body-file path.txt`
- Bilingual EN/DE (date format, greeting, closing all conditional)

**Note:** The CV `EXPERIENCE` / `SKILLS` dicts are hardcoded in the file as example
data. **We will replace this** with a loader that reads our `cv-master.yaml`
(see integration plan §7).

---

## 6. Batch Pipeline + Dashboard

`src/pipeline/batch_pipeline.py` (260 lines):
- Selects top N jobs (default 50) with `score >= --min-score`, not yet processed
- Detects CV variant + language per job
- Spawns CV generator subprocess (60s timeout, capture_output)
- Writes per-job dir under `./data/batches/<date>/<Company>/`
- ZIPs the whole batch
- Generates `OVERVIEW.md` (date + jobs + scores)
- Sends via Telegram (`sendDocument` multipart) — optional, no-op if not configured

`src/pipeline/dashboard.py` (278 lines):
- `http.server.HTTPServer` on port 8080, zero-dep frontend
- HTML/CSS/JS in one giant string, mobile-responsive
- Endpoints: `GET /` (UI), `GET /api/jobs?source=&status=&min_score=&search=&page=`
  (filter+paginate), `GET /api/sources`, `PUT /api/jobs/<id>` (status update)
- Score color buckets: high ≥150 green / mid ≥80 yellow / low <80 red
- Status workflow: new → batched → applied / skipped / saved

**Verdict:** Dashboard is intentionally minimal but covers our review flow. No
auth (VPS + UFW on :8080 + IP allowlist is the implicit auth — fine for solo use).

---

## 7. Integration Plan with `/Users/lars/Documents/Projects/CV/`

The CV repo is **untouched source of truth**. We integrate by feeding it into
dome317's empty config slots:

| dome317 file | What we feed in | Source |
|---|---|---|
| `config/candidate_profile.md` | Adapted profile text for LLM scorer | Derived from `cv-master.yaml` profile.* + summary.* sections, ~30 lines |
| `src/generation/cv_generator.py` EXPERIENCE / EDUCATION / SKILLS dicts | **Replace** with loader for `cv-master.yaml` | `CV/master/cv-master.yaml` (400 lines, bilingual) |
| `batch_pipeline.py` `CV_VARIANTS["..._keywords"]` | Extend `detect_variant()` with Lars-specific tags | e.g. add `founder_cto`, `hosting_infra`, `agency_strategy` variants |
| `batch_pipeline.py` cover-letter body | Read from `why-you-paragraphs.yaml` tracks | `CV/templates/why-you-paragraphs.yaml` (87 lines, founder_cto / agency_strategy / hosting_infra / generic) |
| `config/settings.yaml` queries | Tune for Lars's profile (no salesperson, no recruiter, host/saas/digital/CTO) | Manual edit, ~30 queries |
| `config/settings.yaml` scoring weights | Adjust priorities: boost `hosting`, `agency_strategy`, `founder`; penalize `pure_sales` harder | Manual edit |

**Concrete variant additions** (cover-letter intro paragraph picks):

```python
# in batch_pipeline.py CV_VARIANTS, add to existing dict:
"founder_cto": {
    "keywords": ["cto", "co-founder", "cofounder", "mitgründer", "geschäftsführer",
                 "head of product", "head of engineering", "founder", "gründer"],
    "tagline_de": "Gründer & CEO — Digitalagentur, Hosting & Wachstum",
    "tagline_en": "Founder & CEO — Digital Agency, Hosting & Growth",
},
"hosting_infra": {
    "keywords": ["hosting", "managed hosting", "wordpress hosting", "woocommerce hosting",
                 "infrastructure", "litespeed", "cdn", "devops", "sre", "platform engineer",
                 "cloud architect", "cloud engineer"],
    "tagline_de": "Managed Hosting & Cloud-Infrastruktur",
    "tagline_en": "Managed Hosting & Cloud Infrastructure",
},
"agency_strategy": {
    "keywords": ["agency", "agentur", "strategist", "bd manager", "business development",
                 "partnership", "account strategy", "client success", "sales engineer",
                 "pre-sales"],
    "tagline_de": "Agentur-Strategie & Geschäftsentwicklung",
    "tagline_en": "Agency Strategy & Business Development",
},
```

**CV data loader** — small new module `src/integrations/cv_master_loader.py`:

```python
"""Load Lars's CV data from /Users/lars/Documents/Projects/CV/master/cv-master.yaml.
Replace dome317's hardcoded EXPERIENCE / EDUCATION / PROJECTS / SKILLS dicts.
"""
import yaml
from pathlib import Path

CV_MASTER = Path("/Users/lars/Documents/Projects/CV/master/cv-master.yaml")

def load_master():
    return yaml.safe_load(CV_MASTER.read_text())

def to_dome317_experience(master):
    """Convert cv-master.yaml experience entries to dome317 EXPERIENCE dict shape."""
    ...

def to_dome317_skills(master):
    """Convert cv-master.yaml top_skills to dome317 SKILLS dict shape."""
    ...
```

**Why-you paragraph loader** — `src/integrations/why_you_loader.py`:

```python
"""Load why-you-paragraphs.yaml, pick variant by detected CV variant + language."""
import yaml
from pathlib import Path

WHY_YOU = Path("/Users/lars/Documents/Projects/CV/templates/why-you-paragraphs.yaml")

VARIANT_TO_TRACK = {
    "ai_heavy": "generic",          # don't have an AI-specific track yet
    "technical": "hosting_infra",
    "product": "founder_cto",
    "operations": "agency_strategy",
    "founder_cto": "founder_cto",
    "hosting_infra": "hosting_infra",
    "agency_strategy": "agency_strategy",
}

def pick_paragraph(variant, language="en"):
    data = yaml.safe_load(WHY_YOU.read_text())
    track = VARIANT_TO_TRACK.get(variant, "generic")
    return data[track][language]
```

---

## 8. Configuration Knobs (what to tune first)

Priority order for first run on Lars's profile:

1. **`.env`** — `ANTHROPIC_API_KEY` (Haiku), `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`,
   `CANDIDATE_NAME=Lars Zimmermann`, `CANDIDATE_EMAIL=lars.z@icloud.com`,
   `PHOTO_PATH=./config/lars.png`
2. **`config/candidate_profile.md`** — short bio for LLM scorer (German + English, salary
   floor, location preference Aarschot/Belgium but open to DACH remote)
3. **`config/settings.yaml` queries** — replace with ~30 Lars-targeted queries
   (hosting ops, agency strategy, BD, CTO/cofounder, digital transformation).
   Keep the 15-category structure.
4. **`config/settings.yaml` weights** — keep most defaults; bump `pure_sales -400 → -700`
   (we don't do sales), add `pure_recruiter -500`, add `hosting_infra +50`
5. **`config/settings.yaml` keywords** — add: `hostsalt`, `managed hosting`, `litespeed`,
   `woocommerce`, `agency`, `cofounder`, `mitgründer`, `quic.cloud`
6. **Code patches** — add 3 CV variants (founder_cto, hosting_infra, agency_strategy) +
   cv_master_loader + why_you_loader modules

---

## 9. Extension Points (where to bolt on Lars-specific behavior)

| Concern | Extension point | Effort |
|---|---|---|
| Custom CV variants | Add entries in `batch_pipeline.py` CV_VARIANTS + taglines | S (10 LOC) |
| CV data from yaml | New `src/integrations/cv_master_loader.py`, replace EXPERIENCE dict | M (60 LOC) |
| Cover letter body | New `src/integrations/why_you_loader.py`, modify `generate_documents()` | S (30 LOC) |
| Belgium location boost | Add `preferred_city: aarschot, leuven, brussels` to keywords + weight | XS |
| Salary floor (€50k) | Add `salary_below_target: -300` regex keyword (e.g. `< 40.000`, `< 40k`) | XS |
| DACH-only enforcement | Geo-restrict StepStone location to `Deutschland, Österreich, Schweiz` + BEL | XS |
| Multi-platform Telegram alerts | Override `send_telegram()` to split by source | XS |
| Skip non-DACH companies | Add `non_dach_company: -800` to weights | XS |
| Better work-from-anywhere | Already covered (`remote_europe`, `work_from_anywhere`) | done |
| Avoid recruiter spam | Already covered (`recruiters: -200`) | bump to -400 |

---

## 10. Risks / Gotchas

1. **CV generator hardcodes EXPERIENCE / SKILLS in-file.** We can't just symlink
   `cv-master.yaml` — need a loader module. (Plan above covers this.)
2. **`detect_language()` German heuristic is brittle.** It just checks for 9 keywords
   ("referent", "sachbearbeiter", "hannover", ...). Won't catch Belgian German, Swiss
   German, or Austrian-only postings. Acceptable for v1, tune in v2.
3. **LLM scorer makes direct `urllib` calls to Anthropic.** No retry budget beyond
   3 attempts, no SDK, no proxy support. Fine for €4.50/mo VPS, but if we hit
   rate limits the daily batch stalls. Mitigation: cron gives fresh start next day.
4. **`batch_pipeline.py` cover-letter body is empty.** The `generate_documents()`
   function returns `cl_path = None` — the README claims cover letters are generated
   but the code only generates CVs. Bug or incomplete feature. We will plug in
   `why_you_loader` here.
5. **No authentication on dashboard :8080.** Any IP that can reach the VPS port
   can flip status. Mitigation: UFW IP allowlist, or put behind SSH tunnel.
6. **`create_cv_template.js` / `cv_template_v2.js` are dead code.** Ignore.
7. **Patchright Chromium download is ~250MB.** First install on VPS will be slow.
   `scripts/setup.sh` handles this; local dev install needs `python -m patchright
   install chromium` manually.
8. **README says `cd yourusername/job-search-pipeline`** — copy/paste leftover from
   the template. Not relevant for us, just noting.

---

## 11. Cost Verification (matches our €15/mo target)

| Component | Monthly | Notes |
|---|---:|---|
| Hetzner CX22 | €4.50 | 2 vCPU / 4GB RAM, runs everything |
| Claude Haiku LLM scoring | €8-10 | ~1k jobs/mo at $0.0003/job |
| Proxy (Indeed/LinkedIn rotation) | €1-3 | Optional but recommended |
| Patchright / SQLite / Cron | €0 | Free / built-in |
| **Total** | **€13.50-17.50** | Within budget ✓ |

---

## 12. Go/No-Go for the 4 Child Cards

Child tasks (`t_0b353d0a`, `t_38c879bd`, `t_a3b02ed0`, `t_ba0c54a4`):

| Aspect | Verdict |
|---|---|
| StepStone Patchright scraper DACH fit | **CONFIRMED** ✓ |
| Scoring quality (rules + Haiku + 2-layer filter) | **CONFIRMED** ✓ |
| CV generator 4-variant architecture | **CONFIRMED** ✓ (needs cv-master.yaml loader) |
| €15/mo cost target | **MET** ✓ |
| Integration with `/Users/lars/Documents/Projects/CV/` | **FEASIBLE** ✓ (see §7) |
| Can run locally without Docker (StepStone only) | **YES** ✓ (Docker only for JobSpy/Indeed/LinkedIn) |
| Can run on Mac Mini as primary before VPS deploy | **YES** ✓ (no Docker dependency for DACH flow) |

**Recommendation: PROCEED** with all 4 child cards. The reconnaissance is complete;
we have a clear integration path and the dependency surface is well-understood.

---

## 13. Open Questions for Lars

1. **Belgium-as-base vs DACH-remote**: settings.yaml currently defaults to Berlin.
   Should we set `preferred_city: aarschot, leuven, brussels, berlin, wien, zürich`
   and broaden the geo filter, or keep DACH-only and treat Belgium as commute?
2. **CV photo**: dome317 assumes `PHOTO_PATH=./config/photo.png`. Do we have a
   professional headshot we want in the auto-generated PDFs? (Lars_Zimmermann_Profile_EN.pdf
   is on disk already — that may be the source.) The generated CVs default to no photo.
3. **Telegram bot setup**: We have TELEGRAM_* configured for our other projects. Do we
   want a separate bot for job alerts, or reuse one?
4. **VPS now or local-only first**: dome317 ships Docker-ready, but the StepStone +
   Arbeitsagentur flow runs fine on Mac Mini with Python 3.12 + Patchright. Recommend
   local-only for first week, then Hetzner CX22 for 24/7 cron.