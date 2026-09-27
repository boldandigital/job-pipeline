# DAILY RUN — Runbook

Cron setup, Telegram bot wiring, and operational playbook for the daily 09:00 Brussels batch.

Owner: **Lars** (`contact@boldandigital.com`)
Project: `/Users/lars/Documents/Projects/job-pipeline/`
Schedule: **09:00 Europe/Brussels** daily (`cron` uses local time on macOS — note no DST shift, `cron` follows the Mac's `TZ`).
Status at last review: **awaiting human "go" to enable.** The cron entry below is provided as a comment in your crontab for cut-and-paste, but is NOT installed by any command in this repo (constraint enforced by the task).

---

## 1. ONE-TIME SETUP

### 1.1 Create the Telegram bot (do this on your phone or laptop, ~2 minutes)

1. Open Telegram, message **[@BotFather](https://t.me/BotFather)**
2. Send `/newbot`
3. Name it: `Lars Job Pipeline Bot` (or whatever you like)
4. Username: `lars_jobp_bot` (must end in `bot`, must be unique)
5. Copy the **token** BotFather replies with → paste into `.env` as `TELEGRAM_BOT_TOKEN`
6. From your Telegram account (with the bot open), send `/start` to the new bot — this is required so the bot can message you
7. Get your **chat_id** (numeric, not your @handle):
   ```bash
   curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" | python3 -c "import sys,json; d=json.load(sys.stdin); [print(m['message']['chat']['id']) for m in d['result']]"
   ```
   → paste that number into `.env` as `TELEGRAM_CHAT_ID`

### 1.2 Wire `.env`

```bash
cd /Users/lars/Documents/Projects/job-pipeline
cp .env.example .env
nano .env   # fill in ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
chmod 600 .env                 # keep secrets off prying eyes
```

### 1.3 Smoke test — Telegram ping

```bash
bash scripts/lars-daily-run.sh --test
```

You should receive `⚓ job-pipeline ping — creds OK, ready for daily run.` on Telegram within 30 seconds. If you don't, re-check `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and that you actually sent `/start` to the bot.

### 1.4 Smoke test — what would batch produce today?

```bash
bash scripts/lars-daily-run.sh --dry-run
```

Outputs the top 20 jobs (by score) but generates no documents and sends no Telegram.

> **Day-1 note:** On a fresh checkout the `jobs` table doesn't exist yet — scrapers create it on first run. So `--dry-run` is expected to fail with `no such table: jobs` until you run at least one scrape:
>
> ```bash
> python3 -m src.scrapers.arbeitsagentur_scraper --db ./data/jobs.db
> bash scripts/lars-daily-run.sh --dry-run       # now produces real output
> ```

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
| Re-send today's batch ZIP via Telegram | `bash scripts/lars-daily-run.sh` (re-runs full pipeline, idempotent on DB) |

---

## 4. PAUSE / UNPAUSE / REMOVE

| Action | Command |
|---|---|
| Pause for one day | comment out the cron line in `crontab -e` |
| Pause indefinitely | `crontab -e` → delete the `ADOPT-5` line |
| Nuclear: kill cron entirely | `crontab -r` (removes ALL your cron jobs — be sure) |
| Re-enable after pause | uncomment the line in `crontab -e`, save |
| Disable Telegram only (cron keeps running) | `telegram: enabled: false` in `config/delivery.yaml`, then touch `./logs/.muted` |

Logs keep writing when paused — that's intentional (you want a paper trail).

---

## 5. WHEN YOU REJECT ALL JOBS IN A BATCH

This is the rejection-tuning loop:

1. Read the Telegram summary — which company/role was the lowest-rated you tolerated?
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

- **Do not install cron** until you have a working Telegram ping — that's a 5-minute smoke test, not optional.
- **Do not commit `.env`.** It's gitignored. If you accidentally commit a token, rotate it via @BotFather → `/revoke`.
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
├── .env.example               ← template (committed)
├── config/
│   ├── delivery.yaml          ← Telegram framing (committed, no secrets)
│   └── example.yaml           ← scoring + queries (committed)
├── scripts/
│   └── lars-daily-run.sh      ← cron target (chmod +x)
├── docs/
│   └── DAILY-RUN.md           ← this file
└── logs/                      ← gitignored, self-rotating
    ├── lars-daily-YYYYMMDD.log
    └── lars-error-YYYYMMDD.log
```
