# Deployment Guide

Step-by-step guide for deploying the job search pipeline on a Linux VPS.

## Recommended Setup

| Component | Recommendation | Monthly Cost |
|-----------|---------------|:---:|
| VPS | Hetzner CX22 (2 vCPU, 4GB RAM) | 4.50 EUR |
| OS | Ubuntu 24.04 LTS | -- |
| Proxy | iProyal or similar rotating proxy | 1-3 EUR |
| LLM API | Anthropic Claude (Haiku model) | 8-10 EUR |

## Step 1: Provision VPS

1. Create a VPS at [Hetzner](https://www.hetzner.com/cloud), [DigitalOcean](https://www.digitalocean.com/), or any provider
2. Choose Ubuntu 24.04 LTS, minimum 2 vCPU / 4GB RAM
3. Add your SSH key during setup

```bash
ssh root@your-server-ip
```

## Step 2: Run Setup Script

```bash
# Clone the repository
git clone https://github.com/yourusername/job-search-pipeline.git /opt/job-pipeline
cd /opt/job-pipeline

# Run automated setup (installs Docker, Python, Chromium, SQLite, creates DB)
sudo bash scripts/setup.sh
```

The setup script will:
- Install system dependencies (Docker, Python 3, SQLite)
- Install Python packages (requests, patchright, pyyaml)
- Install Chromium for browser automation
- Create 2GB swap space
- Initialize the SQLite database
- Configure UFW firewall (SSH + port 8080)
- Install cron jobs

## Step 3: Configure

```bash
# Edit environment variables
nano /opt/job-pipeline/.env

# Required:
# - ANTHROPIC_API_KEY (for LLM scoring)
# - PROXY_URL (for Indeed/LinkedIn scraping)
# Optional:
# - TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (for alerts)
```

### Create Telegram Bot (Optional)

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the bot token to `.env`
4. Send `/start` to your new bot
5. Get your chat ID: `curl https://api.telegram.org/bot<TOKEN>/getUpdates`

### Customize Search Config

```bash
cp config/example.yaml config/settings.yaml
nano config/settings.yaml
# Edit queries, locations, scoring weights for YOUR profile
```

### Create Candidate Profile

```bash
cp config/candidate_profile.example.md config/candidate_profile.md
nano config/candidate_profile.md
# Add your education, experience, skills, preferences
```

## Step 4: Test

```bash
# Run a quick test search
python3 -m src.scrapers.stepstone_scraper \
  --queries "Data Analyst" \
  --location Deutschland \
  --db ./data/jobs.db

# Check results
sqlite3 ./data/jobs.db "SELECT COUNT(*) FROM jobs;"

# Start dashboard
python3 -m src.pipeline.dashboard --db ./data/jobs.db &
# Visit http://your-server-ip:8080
```

## Step 5: Enable Cron Jobs

```bash
bash scripts/cron-setup.sh
```

This installs 5 scheduled jobs:

| Time (UTC) | Job | Description |
|:---:|------|-------------|
| 02:00 | `daily_pipeline.sh` | Indeed + LinkedIn + Arbeitsagentur scraping |
| 03:00 | `score_jobs.py` | Keyword-based scoring of new jobs |
| 04:00 | `batch_pipeline.py` | Top 50 jobs: generate CV + cover letter, ZIP, send via Telegram |
| 08:30 | `stepstone_pipeline.sh` | StepStone browser scraping |
| 09:00 | `score_jobs.py` | Score StepStone results |

## Step 6: Run Dashboard as Service (Optional)

```bash
cat > /etc/systemd/system/job-dashboard.service << 'EOF'
[Unit]
Description=Job Search Dashboard
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/job-pipeline
ExecStart=/usr/bin/python3 -m src.pipeline.dashboard --db /opt/job-pipeline/data/jobs.db --host 0.0.0.0 --port 8080
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl enable job-dashboard
systemctl start job-dashboard
```

## Optional: AI Agent Orchestration

For fully autonomous operation, you can use an AI agent framework like [OpenClaw](https://github.com/openclaw/openclaw) to:
- Automatically fill out application forms
- Handle screening questions via LLM
- Take screenshots before/after applying
- Self-optimize search queries based on results

This is an advanced setup and requires additional configuration. The core pipeline works fully without it.

## Monitoring

```bash
# Check cron logs
tail -f /opt/job-pipeline/logs/daily.log

# Check job counts
sqlite3 /opt/job-pipeline/data/jobs.db "
  SELECT source, COUNT(*) as total,
    SUM(CASE WHEN score >= 150 THEN 1 ELSE 0 END) as premium,
    SUM(CASE WHEN score >= 80 AND score < 150 THEN 1 ELSE 0 END) as standard
  FROM jobs GROUP BY source;
"

# Dashboard
# http://your-server-ip:8080
```

## Troubleshooting

| Issue | Solution |
|-------|---------|
| StepStone returns 0 jobs | Cookie consent might have changed. Check browser screenshots. |
| Indeed blocks requests | Ensure proxy is configured in `.env`. Rotate IP. |
| LLM scorer errors | Check `ANTHROPIC_API_KEY` is valid. Monitor API credits. |
| High memory usage | Browser scraping needs ~1GB. Ensure swap is configured. |
| Patchright install fails | Run `python3 -m patchright install --with-deps chromium` manually. |
