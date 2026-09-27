# DAILY RUN — Runbook

Cron setup, Discord delivery wiring, and operational playbook for the daily 09:00 Brussels batch.

Owner: **Lars** (`contact@boldandigital.com`)
Project: `/Users/lars/Documents/Projects/job-pipeline/`
Schedule: **09:00 Europe/Brussels** daily (`cron` uses local time on macOS — note no DST shift, `cron` follows the Mac's `TZ`).
Status at last review: **awaiting human "go" to enable.** The cron entry below is provided as a comment in your crontab for cut-and-paste, but is NOT installed by any command in this repo (constraint enforced by the task).

---

## 1. ONE-TIME SETUP

### 1.1 Use the existing Hermes Discord bot (zero setup if you already run Hermes)

ADOPT-8: job-pipeline no longer creates its own Discord webhook. We reuse the **same Hermes bot** that already lives in `~/.hermes/.env` and is already posting to your home channel (`DISCORD_HOME_CHANNEL=1497988205788663991`). The bot is in the channel; job-pipeline just points at it.

**Two flavors — pick (a) unless you want a dedicated `#job-pipeline` channel:**

#### (a) Recommended — share the Hermes home channel (zero new config)

Do nothing. `lars-daily-run.sh` automatically:

1. Sources `/Users/lars/Documents/Projects/job-pipeline/.env`
2. Falls back to `~/.hermes/.env` for any missing `DISCORD_*` vars
3. Defaults `DISCORD_CHANNEL_ID` to `DISCORD_HOME_CHANNEL`

Result: daily batches land in the same Discord channel you're already using to chat with Hermes. No new bot invite, no new token rotation, no new auth surface.

#### (b) Dedicated `#job-pipeline` channel

1. In Discord, create the channel (right-click category → Create Channel → `#job-pipeline`)
2. Invite the Hermes bot to that channel (the bot is already in your server; just `/invite @<bot-name>` or mention it once to register the channel — Discord bots post anywhere they have `Send Messages` permission)
3. Copy the channel ID: Discord → ⚙️ Settings → Advanced → enable **Developer Mode** → right-click the new channel → **Copy Channel ID**
4. Add to `/Users/lars/Documents/Projects/job-pipeline/.env`:
   ```env
   DISCORD_CHANNEL_ID=<the-new-snowflake>
   DISCORD_BOT_TOKEN=<paste from ~/.hermes/.env>
   ```
   Or leave `DISCORD_BOT_TOKEN` blank to inherit it from Hermes automatically.

**That's it.** No new bot token, no `/start`, no chat_id. The same Hermes bot posts there.

**Why this beats the old webhook path:**

- **Zero duplicate infra** — no parallel webhook to rotate, no parallel bot to invite
- **Secrets live in one place** — `~/.hermes/.env`. job-pipeline READS from it (see `docs/SECRETS.md`)
- **Richer Markdown** — embeds, code blocks, tables all render; the daily batch summary ships with a structured embed (`title` + `fields` + `footer`)
- **Native file preview** — PDFs render inline, images get thumbnails
- **25 MB per file** — `config/delivery.yaml` keeps 24 MB headroom (`max_zip_size_mb: 24`)

> **Migration from ADOPT-7 webhook:** if you have an old `DISCORD_WEBHOOK_URL` in `.env`, leave it — the wrapper falls back to it ONLY when no bot creds are set. Once you cut the bot over, delete the line (or move it to `daily_pipeline.sh` for the dome317 Docker path).

### 1.2 Wire `.env`

```bash
cd /Users/lars/Documents/Projects/job-pipeline
cp .env.example .env
nano .env   # fill in ANTHROPIC_API_KEY
# Optional: pin a dedicated DISCORD_CHANNEL_ID (otherwise inherits from Hermes)
chmod 600 .env                 # keep secrets off prying eyes
```

`DISCORD_BOT_TOKEN` does NOT need to live here — the wrapper sources `~/.hermes/.env` automatically. See `docs/SECRETS.md` if you want to override that path.

### 1.3 Set up the Google Sheet (ADOPT-10 + ADOPT-11)

The daily batch lands in a per-date tab on a Google Sheet, so you can sort, mark ★ Approved, and feed rejection reasons back into scoring. ADOPT-10 writes, ADOPT-11 reads back. Reuses the same Hermes service account as gpl-love — **no new Google project, no new OAuth client, no new secret**.

**One-time setup (≈ 2 minutes):**

1. Open Google Drive → **New** → **Google Sheets** → name it `Lars Job Pipeline`.
2. Copy the **Spreadsheet ID** from the URL: `https://docs.google.com/spreadsheets/d/<THIS_PART>/edit`
3. Share the Sheet with the SA: open `~/.hermes/credentials/google-service-account.json`, copy the `client_email` value (something like `hermes-agent@flawless-point-496620-d6.iam.gserviceaccount.com`), then in the Sheet → **Share** → paste that email → **Editor** permission → send.
4. Add to `/Users/lars/Documents/Projects/job-pipeline/.env`:
   ```env
   GOOGLE_SPREADSHEET_ID=<the-spreadsheet-id-from-step-2>
   ```
   `GOOGLE_APPLICATION_CREDENTIALS` is already inherited from `~/.hermes/.env` (or uncomment the line in `.env.example` to set it explicitly).

**Daily review flow:**

```bash
# After the morning batch (or with --sheet to write today's tab on demand):
bash scripts/lars-daily-run.sh --sheet
```

1. Open the Sheet → click today's `YYYY-MM-DD` tab.
2. **Sort** column F (score) descending — top of the list is your best matches.
3. Mark **column K (`status`)** with one of:
   - `approved` (or `★`) — you'll apply to these
   - `rejected` (or `✗`) — see step 4
   - leave blank — still considering
4. Mark **column L (`rejection_reason`)** for rejected rows with one of:
   - `too_junior`, `too_senior`, `wrong_location`, `wrong_domain`, `recruiter`, `language`
   - `other:<free text>` for anything that doesn't fit
5. Sync the marks back to the DB before the next scrape:
   ```bash
   bash scripts/lars-daily-run.sh --sync-from-sheet   # defaults to yesterday's tab
   bash scripts/lars-daily-run.sh --sync-from-sheet 2026-09-26   # or an explicit tab
   ```

**Why this beats the Discord digest for review:** the Sheet is sortable, filterable, and persists your decisions in one place — no scrolling 50 messages to find what you marked. Discord is still your morning nudge ("the batch is in"); the Sheet is where the actual triage happens.

**Idempotent re-runs:** `bash scripts/lars-daily-run.sh --sheet` overwrites today's tab cleanly — re-running never produces duplicate rows. Yesterday's tab is untouched.

**If you skip setup:** `--sheet` silently no-ops (cron won't break). The daily run still finishes with the Discord summary.

### 1.4 Daily review flow (ADOPT-11)

After the cron fires and updates the Sheet, here's the daily loop:

```bash
# (a) Sync yesterday's marks into SQLite (runs offline, no Discord needed)
bash scripts/lars-daily-run.sh --sync-from-sheet

# (b) Or sync a specific tab (e.g. when you're catching up on the weekend)
bash scripts/lars-daily-run.sh --sync-from-sheet 2026-09-26

# (c) Preview what would be applied today (no files written)
bash bin/apply-from-sheet.sh --dry-run

# (d) Generate the ready-to-apply ZIP (tailored CV + Anschreiben per approved job)
bash scripts/lars-daily-run.sh --apply
# equivalently:
bash bin/apply-from-sheet.sh

# (e) Limit batch size or pick a single job
bash bin/apply-from-sheet.sh --limit 10
bash bin/apply-from-sheet.sh --job 42
```

The sync step is idempotent — running it twice in a row produces the same
DB state. `approved_at` is set on first approval and **never** overwritten
on re-sync. Rejections get enum-keyed analytics so you can tune `config/lars.yaml`
without spelunking through raw Sheet rows.

If `GOOGLE_SPREADSHEET_ID` is not set in `.env`, `--sync-from-sheet` exits
0 with a warning (cron-friendly: doesn't break the run, just skips the sync).

### 1.5 Rejection-driven tuning (ADOPT-11)

Every rejection in the Sheet is a data point. Run the dashboard weekly:

```bash
python3 -m src.analytics.rejection_dashboard --db ./data/jobs.db --window-days 30
# → writes research/rejection-patterns-YYYY-MM.md
```

The report groups rejections by reason (`too_junior`, `wrong_location`,
`salary_too_low`, etc.) and surfaces the top 3 with **suggested** config
tweaks — e.g. "if `too_junior` is dominant, raise `scoring.threshold` from
20 → 30 in `config/lars.yaml`". Suggestions are comments; you apply them
by hand. The script is read-only against SQLite.

Raw JSON for piping into other tools:

```bash
python3 -m src.analytics.rejection_dashboard --db ./data/jobs.db --window-days 30 --json
```

The `--window-days` flag lets you backfill: `7` for the last week, `90` for
a quarterly view, etc.

### 1.6 Smoke test — Discord ping

```bash
bash scripts/lars-daily-run.sh --test
```

You should receive `⚓ job-pipeline ping — creds OK, ready for daily run.` in your Discord channel (the Hermes home channel, unless you set `DISCORD_CHANNEL_ID`) within 5 seconds.

If you don't, check the wrapper's startup banner:

```text
Discord: mode=bot  channel=1497988205788663991     # ✅ what you want
Discord: mode=…     channel=                       # ❌ no creds resolved — see docs/SECRETS.md
```

---

## 2. INSTALL THE CRON JOB

```bash
crontab -e
```

Add this single line (everything after the `# === ADOPT-5 ===` marker — leave the marker in so a future `crontab -l | grep ADOPT-5` finds it):

```cron
# === ADOPT-5: daily 09:00 Brussels job-pipeline (Lars) ===
0 9 * * * /bin/bash /Users/lars/Documents/Projects/job-pipeline/scripts/lars-daily-run.sh >/dev/null 2>&1
```

Verify:

```bash
crontab -l | grep ADOPT-5
```

macOS uses `launchd` by default, but `cron` still works out of the box. If `cron` was disabled (some managed macOS setups do this), fallback to `launchd`:

```bash
# see: launchctl load ~/Library/LaunchAgents/com.boldandigital.jobpipeline.plist
```

A ready-to-go `~/Library/LaunchAgents/com.boldandigital.jobpipeline.plist` is below in §6 — paste + load if cron is dead.

---

## 3. INSPECT / DEBUG

| Question | Command |
|---|---|
| What fired last night? | `tail -n 200 logs/lars-daily-$(date +%Y%m%d).log` |
| What went wrong last night? | `tail -n 200 logs/lars-error-$(date +%Y%m%d).log` |
| Is cron actually running? | `log show --predicate 'process == "cron"' --last 4h` |
| Did my crontab survive a reboot? | `crontab -l` |
| How big are the logs? | `du -sh logs/` |
| Top scorers right now? | `sqlite3 data/jobs.db "SELECT title, company, score FROM jobs WHERE score >= 100 ORDER BY score DESC LIMIT 10;"` |
| Full batch preview | `bash scripts/lars-daily-run.sh --dry-run` |
| Re-send today's batch ZIP via Discord | `bash scripts/lars-daily-run.sh` (re-runs full pipeline, idempotent on DB) |
| Scrape a single source only | `bash scripts/lars-daily-run.sh --source xing --skip-scrape` (use with caution — full Discord flow still runs) |
| Scrape with no source change | `bash scripts/lars-daily-run.sh --skip-scrape` (re-score + batch existing DB) |

---

## 4. PAUSE / UNPAUSE / REMOVE

| Action | Command |
|---|---|
| Pause for one day | comment out the cron line in `crontab -e` |
| Pause indefinitely | `crontab -e` → delete the `ADOPT-5` line |
| Nuclear: kill cron entirely | `crontab -r` (removes ALL your cron jobs — be sure) |
| Re-enable after pause | uncomment the line in your crontab, save, exit |
| Disable Discord only (cron keeps running) | `discord: enabled: false` in `config/delivery.yaml`, then touch `./logs/.muted` |

Logs keep writing when paused — that's intentional (you want a paper trail).

---

## 4b. SCRAPING — sources, flags, and DACH tuning (ADOPT-13)

The daily pipeline now starts with a **scrape step** before scoring. Default sources:

- `stepstone` — the German job-board leader, biggest surface
- `xing` — the DACH-native professional network (see §4b.1 below)
- `arbeitsagentur` — official Bundesagentur für Arbeit API, no scraping

LinkedIn is intentionally NOT in the default — `jobspy` (the upstream Docker scraper) is not set up in this checkout. To opt back in, run `daily_pipeline.sh` (the dome317 Docker path) instead.

### 4b.1 XING (the DACH-native 4th platform)

XING is the German/Austrian/Swiss professional network — the original 4-platform scope was "LinkedIn + StepStone + XING + official career pages". XING is where mid-to-senior roles in DACH appear earliest, especially `Geschäftsführer`, `Founder`, and `Head of`-track listings that LinkedIn doesn't surface well in German.

| Field | Value |
|---|---|
| Implementation | `src/scrapers/xing_scraper.py` (Patchright, headless Chromium) |
| Public URL | `https://www.xing.com/jobs/search?keywords=...&location=...&page=N` |
| Pagination | `?page=N` works from page 1, capped at 3 pages by the wrapper |
| Anti-bot | DSGVO cookie banner (auto-dismissed), captcha (bail + warn), login-wall beyond ~5 pages |
| Locale | `de-DE`, TZ `Europe/Berlin`, geo = Germany |
| DB | Appends to `jobs` table with `source = 'xing'` |
| Dedupe | Exact (UNIQUE title+company) + fuzzy (SequenceMatcher 0.85 against last 5,000) |

**Queries (`config/lars.yaml → xing_queries`):**

```yaml
xing_queries:
  - "founder digital"
  - "cto startup"
  - "head of digital"
  - "managing director agency"
  - "geschäftsführer digital"   # DE equivalent for higher recall
xing_location: "Deutschland"      # DACH-heavy by default
```

Tune either list and the daily run picks it up next time `run_scrapers` runs.

### 4b.2 Manual scrape flags

```bash
# Scrape XING only — single source
bash scripts/lars-daily-run.sh --source xing

# Scrape XING + Arbeitsagentur (skip StepStone today)
bash scripts/lars-daily-run.sh --source xing,arbeitsagentur

# Re-score + batch the existing DB without re-scraping
bash scripts/lars-daily-run.sh --skip-scrape

# Full reset to defaults: scrape all three, then score + batch + Discord
bash scripts/lars-daily-run.sh
```

Each source failure is logged but does NOT halt the pipeline — if XING captcha-triggers, you'll still get the StepStone + Arbeitsagentur jobs in the batch. The next cron run will retry XING.

### 4b.3 Inspect XING rows

```bash
# All XING rows in the DB
sqlite3 data/jobs.db "SELECT title, company, location, url FROM jobs WHERE source='xing' ORDER BY id DESC LIMIT 20;"

# Count by status
sqlite3 data/jobs.db "SELECT status, COUNT(*) FROM jobs WHERE source='xing' GROUP BY status;"

# Force a clean re-scrape (delete XING rows, then --source xing)
sqlite3 data/jobs.db "DELETE FROM jobs WHERE source='xing';"
```

---

## 5. WHEN YOU REJECT ALL JOBS IN A BATCH

This is the rejection-tuning loop:

1. Read the Discord summary — which company/role was the lowest-rated you tolerated?
2. Open `config/example.yaml` (or `config/settings.yaml` if you've copied it) → `scoring.weights.negative_signals`
3. Bump `recruiters: -200` → `-400` (kills Agency spam)
4. Lower `threshold: 20` → `threshold: 30` (stricter minimum) or raise it to `40` if you're drowning in noise
5. Add a negative weight: `-99` for keywords in your "will never apply" list (e.g. `staff_engineer`)
6. `bash scripts/lars-daily-run.sh --dry-run` — confirm the new floor kills the spam
7. Wait for tomorrow's 09:00 run to see the live effect

If a SOURCE is consistently bad (e.g. LinkedIn is 80% recruiters), gate it:

```yaml
# config/example.yaml  →  search.sites
sites:
  - "indeed"      # keep
  # - "linkedin"   # nuke it
```

---

## 6. FALLBACK: launchd PLIST (if cron is dead)

```bash
mkdir -p ~/Library/LaunchAgents
cat > ~/Library/LaunchAgents/com.boldandigital.jobpipeline.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.boldandigital.jobpipeline</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/Users/lars/Documents/Projects/job-pipeline/scripts/lars-daily-run.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>   <integer>9</integer>
    <key>Minute</key> <integer>0</integer>
  </dict>
  <key>StandardOutPath</key>  <string>/Users/lars/Documents/Projects/job-pipeline/logs/launchd.out.log</string>
  <key>StandardErrorPath</key> <string>/Users/lars/Documents/Projects/job-pipeline/logs/launchd.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key> <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
PLIST

launchctl load ~/Library/LaunchAgents/com.boldandigital.jobpipeline.plist
launchctl list | grep jobpipeline      # confirm registered
```

Unload to disable:

```bash
launchctl unload ~/Library/LaunchAgents/com.boldandigital.jobpipeline.plist
```

---

## 7. CONSTRAINTS / GUARDRAILS (do NOT skip)

- **Do not install cron** until you have a working Discord ping — that's a 5-minute smoke test, not optional.
- **Do not commit `.env`.** It's gitignored. If you accidentally commit a Discord bot token, rotate it immediately via the Discord Developer Portal → Bot → **Reset Token**. Webhook URLs (ADOPT-7 legacy) get rotated via Discord channel ⚙️ Settings → Integrations → Webhooks.
- **Bot token reuse:** the job-pipeline bot token IS the Hermes bot token. Rotating it kills both. Coordinate rotations with any other project that shares `~/.hermes/.env`.
- **DB growth:** `data/jobs.db` grows ~5 MB/week. Vacuum monthly:
  ```bash
  sqlite3 data/jobs.db "VACUUM;"
  ```
- **Cost ceiling:** the daily run caps at Haiku ~€0.30/day (15 jobs × 2 Haiku calls). If you see costs spike, your scoring threshold is letting in too many jobs.
- **Mac asleep?** Cron doesn't wake a sleeping Mac. If you want the run to happen even when closed, enable "Wake for network access" in System Settings → Energy, or use a `pmset` wake event.

---

## 8. QUICK REFERENCE — file map

```
/Users/lars/Documents/Projects/job-pipeline/
├── .env                       ← SECRETS (gitignored, chmod 600)
│                                  ADOPT-8: only ANTHROPIC_API_KEY + (optional) DISCORD_CHANNEL_ID.
│                                  DISCORD_BOT_TOKEN is inherited from ~/.hermes/.env.
├── .env.example               ← template (committed)
├── config/
│   ├── delivery.yaml          ← Discord framing (committed, no secrets). ADOPT-8: bot + webhook modes.
│   ├── lars.yaml              ← Lars's tuned scoring + queries (committed). ADOPT-14: career_discovery section.
│   └── example.yaml           ← scoring + queries (committed)
├── scripts/
│   └── lars-daily-run.sh      ← cron target (chmod +x). ADOPT-8: bot-mode default + webhook fallback.
│                                  ADOPT-14: dry-run prints "Career URLs discovered: N" + ats count.
├── src/
│   ├── discovery/
│   │   └── career_discovery.py   ← ADOPT-14: 3-layer career URL discovery + 18 ATS detectors
│   └── pipeline/
│       └── batch_pipeline.py     ← ADOPT-14: hooks career discovery after scoring, before docs
├── tests/
│   ├── test_career_discovery.py  ← ADOPT-14: 60+ tests covering ATS detection, robots, idempotency
│   └── fixtures/
│       └── ats_test_cases.json   ← ADOPT-14: 20 known ATS deployments
├── docs/
│   ├── DAILY-RUN.md           ← this file
│   └── SECRETS.md            ← cross-project secret-sharing pattern (ADOPT-8)
└── logs/                      ← gitignored, self-rotating
    ├── lars-daily-YYYYMMDD.log
    └── lars-error-YYYYMMDD.log
```

**Where the Discord secrets actually live:** `~/.hermes/.env` (gitignored, owned by the Hermes project).
`docs/SECRETS.md` explains the cross-project sharing pattern and how to override the fallback path.

---

## 9. ADOPT-14: CAREER URL COLUMNS

After ADOPT-14, two new columns feed ADOPT-12 (CUA submit):

| Column        | Type     | What it holds                                              |
|---------------|----------|------------------------------------------------------------|
| `career_url`  | `TEXT`   | Direct company career page URL (NOT a portal URL)          |
| `ats_type`    | `TEXT`   | One of: workday, greenhouse, lever, ashby, smartrecruiters, icims, sap_sf, taleo, softgarden, personio, workable, breezy, bamboohr, jobvite, recruitee, join, coveto, umantis, unknown |

**When does it run?** After scoring (step 2/4), inside `batch_pipeline.py` step 3/4 (top 50 jobs). Also runs in `--dry-run` so you can preview before applying.

**Why it matters:**
- Senior roles (Founder / CTO / Head-of-Digital) rarely post to LinkedIn / StepStone — they live on the company's own career page.
- 80% of senior leadership roles are NOT on job boards. Without career URL discovery, we miss them entirely.
- ATS detection enables ATS-aware resume tailoring (e.g. Workday needs different formatting than Greenhouse).

**Query examples:**
```sql
-- Jobs with a known ATS (ready for CUA submit)
SELECT title, company, career_url, ats_type
FROM jobs WHERE career_url IS NOT NULL AND career_url != '';

-- Coverage report
SELECT ats_type, COUNT(*) AS n
FROM jobs WHERE ats_type IS NOT NULL AND ats_type != ''
GROUP BY ats_type ORDER BY n DESC;

-- Jobs that still need discovery (career_url is NULL)
SELECT COUNT(*) FROM jobs WHERE career_url IS NULL OR career_url = '';
```

**Override knobs (env vars):**
- `CAREER_DISCOVERY_ENABLED=0` — skip the entire layer (fall back to portal-only URLs)
- `CAREER_DISCOVERY_MAX_JOBS=20` — lower the cap for faster dry-runs

**Constraints (enforced in `src/discovery/career_discovery.py`):**
- `robots.txt` respected — if `Disallow: /careers`, that path is skipped and logged
- Login walls skipped — no jobs visible without auth → don't waste CUA attempts
- 1 request/sec per domain (politeness)
- 30-day in-memory cache — once we find `boldandigital.com/careers`, don't re-probe for 30 days
- Idempotent — never overwrites a populated `career_url` (reruns are safe)
