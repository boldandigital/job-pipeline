#!/usr/bin/env bash
# ╔═══════════════════════════════════════════════════════════════════════╗
# ║  JOB SEARCH PIPELINE — VPS SETUP SCRIPT                             ║
# ║  Ubuntu 22.04+ · Idempotent · Run as root                           ║
# ║  Run: chmod +x setup.sh && sudo ./setup.sh                          ║
# ╚═══════════════════════════════════════════════════════════════════════╝
set -euo pipefail

# ============================================================
# COLORS & LOGGING
# ============================================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'
LOGFILE="/var/log/job-pipeline-setup.log"

log()  { echo -e "${GREEN}[OK]${NC} $1" | tee -a "$LOGFILE"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1" | tee -a "$LOGFILE"; }
err()  { echo -e "${RED}[ERR]${NC} $1" | tee -a "$LOGFILE"; }
info() { echo -e "${BLUE}[INFO]${NC} $1" | tee -a "$LOGFILE"; }

trap 'err "Script failed at line $LINENO. See $LOGFILE"' ERR

echo "" | tee -a "$LOGFILE"
info "=== Job Search Pipeline Setup started $(date) ==="

# ============================================================
# CHECK: Root?
# ============================================================
if [[ $EUID -ne 0 ]]; then
  err "This script must be run as root: sudo ./setup.sh"
  exit 1
fi

# ============================================================
# PHASE 1: System Updates + Dependencies
# ============================================================
info "Phase 1: System Updates + Dependencies"

export DEBIAN_FRONTEND=noninteractive

apt-get update -qq 2>>"$LOGFILE"
apt-get upgrade -y -qq 2>>"$LOGFILE"
apt-get install -y -qq \
  curl wget git unzip jq sqlite3 \
  python3 python3-pip python3-venv \
  ca-certificates gnupg lsb-release \
  htop tmux \
  2>>"$LOGFILE"
log "System packages installed"

# ============================================================
# PHASE 2: Docker
# ============================================================
info "Phase 2: Docker"

if command -v docker &>/dev/null; then
  log "Docker already installed: $(docker --version)"
else
  curl -fsSL https://get.docker.com | sh 2>>"$LOGFILE"
  log "Docker installed: $(docker --version)"
fi

if docker compose version &>/dev/null; then
  log "Docker Compose available: $(docker compose version --short)"
else
  apt-get install -y docker-compose-plugin 2>>"$LOGFILE"
fi

systemctl enable docker 2>>"$LOGFILE"
systemctl start docker 2>>"$LOGFILE"
log "Docker service running"

# ============================================================
# PHASE 3: Python Dependencies
# ============================================================
info "Phase 3: Python Dependencies"

pip3 install --break-system-packages requests patchright pyyaml 2>>"$LOGFILE"
python3 -m patchright install --with-deps chromium 2>>"$LOGFILE" || true
log "Python dependencies installed"

# ============================================================
# PHASE 4: Swap Space (important for 4-8GB RAM VPS)
# ============================================================
info "Phase 4: Swap Space"

if swapon --show | grep -q /swapfile; then
  log "Swap already active"
else
  if [[ ! -f /swapfile ]]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile 2>>"$LOGFILE"
  fi
  swapon /swapfile 2>>"$LOGFILE"
  if ! grep -q '/swapfile' /etc/fstab; then
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
  sysctl vm.swappiness=10 2>>"$LOGFILE"
  log "2GB Swap configured"
fi

# ============================================================
# PHASE 5: Directory Structure
# ============================================================
info "Phase 5: Directory Structure"

INSTALL_DIR="${INSTALL_DIR:-/opt/job-pipeline}"

mkdir -p "$INSTALL_DIR"/{data,config,logs,batches,screenshots}
log "Directories created at $INSTALL_DIR"

# ============================================================
# PHASE 6: Environment File (template)
# ============================================================
info "Phase 6: Environment Variables"

ENV_FILE="$INSTALL_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<'ENVEOF'
# Job Search Pipeline — Environment Variables
# NEVER commit this file to git!

# Anthropic API Key (from console.anthropic.com)
ANTHROPIC_API_KEY=your-key-here

# Telegram Bot Token (from @BotFather)
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_CHAT_ID=your-chat-id

# Proxy for scraping (optional, recommended for Indeed)
PROXY_URL=http://user:pass@proxy-host:port

# Database paths
DB_PATH=/opt/job-pipeline/data/jobs.db
JOBSPY_DB_PATH=/opt/job-pipeline/data/jobspy.db

# Candidate info (for CV/CL generation)
CANDIDATE_NAME=Your Name
CANDIDATE_EMAIL=your@email.com
CANDIDATE_PHONE=+49 123 456789
CANDIDATE_LOCATION=Berlin
ENVEOF
  chmod 600 "$ENV_FILE"
  log "Environment file created: $ENV_FILE"
  warn "Fill in your secrets! Edit: nano $ENV_FILE"
else
  log "Environment file already exists"
fi

# ============================================================
# PHASE 7: SQLite Database
# ============================================================
info "Phase 7: SQLite Database"

DB_PATH="$INSTALL_DIR/data/jobs.db"
if [[ ! -f "$DB_PATH" ]]; then
  sqlite3 "$DB_PATH" <<'SQLEOF'
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  company TEXT NOT NULL,
  location TEXT,
  url TEXT,
  career_url TEXT,
  source TEXT,
  description TEXT,
  search_query TEXT,
  score INTEGER DEFAULT 0,
  ats_type TEXT,
  status TEXT DEFAULT 'new',
  cv_path TEXT,
  cover_letter_path TEXT,
  applied_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(title, company)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
SQLEOF
  log "SQLite database created: $DB_PATH"
else
  log "SQLite database already exists"
fi

# Schema migration: ensure jobs.career_url + jobs.description exist on older
# DBs whose CREATE TABLE pre-dated ADOPT-6 (arbeitsagentur/stepstone scrapers
# omitted them). Idempotent — safe on every setup re-run.
if command -v python3 &>/dev/null; then
  python3 "$INSTALL_DIR/scripts/migrate_schema_add_career_description.py" --db "$DB_PATH" 2>>"$LOGFILE" \
    && log "Schema migration applied" \
    || warn "Schema migration failed (non-fatal — see $LOGFILE)"
else
  warn "python3 not found — skipping schema migration; run scripts/migrate_schema_add_career_description.py manually"
fi

# ============================================================
# PHASE 8: UFW Firewall
# ============================================================
info "Phase 8: Firewall"

if command -v ufw &>/dev/null; then
  ufw default deny incoming 2>>"$LOGFILE"
  ufw default allow outgoing 2>>"$LOGFILE"
  ufw allow ssh 2>>"$LOGFILE"
  ufw allow 8080/tcp comment "Dashboard" 2>>"$LOGFILE"
  ufw --force enable 2>>"$LOGFILE"
  log "UFW Firewall configured (SSH + Dashboard)"
fi

# ============================================================
# PHASE 9: Crontab
# ============================================================
info "Phase 9: Cron Jobs"

CRON_FILE="/tmp/job-pipeline-cron"
cat > "$CRON_FILE" <<CRONEOF
# Job Search Pipeline — Cron Schedule (all times UTC)
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
DB_PATH=$INSTALL_DIR/data/jobs.db
JOBSPY_DB_PATH=$INSTALL_DIR/data/jobspy.db

# Daily job search (Indeed + LinkedIn via Docker + Arbeitsagentur)
0 2 * * * cd $INSTALL_DIR && bash scripts/daily_pipeline.sh >> $INSTALL_DIR/logs/daily.log 2>&1

# StepStone browser scrape
30 8 * * * cd $INSTALL_DIR && bash scripts/stepstone_pipeline.sh >> $INSTALL_DIR/logs/stepstone.log 2>&1
CRONEOF

crontab "$CRON_FILE"
rm "$CRON_FILE"
log "Cron jobs installed"

# ============================================================
# SUMMARY
# ============================================================
echo ""
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║                    SETUP COMPLETE                            ║"
echo "╠═══════════════════════════════════════════════════════════════╣"
echo "║                                                               ║"
echo "║  NEXT STEPS:                                                  ║"
echo "║                                                               ║"
echo "║  1. Fill in secrets:                                          ║"
echo "║     nano $INSTALL_DIR/.env                                    ║"
echo "║                                                               ║"
echo "║  2. Copy your config:                                        ║"
echo "║     cp config/example.yaml $INSTALL_DIR/config/settings.yaml  ║"
echo "║                                                               ║"
echo "║  3. Copy candidate profile:                                  ║"
echo "║     cp config/candidate_profile.example.md                    ║"
echo "║        $INSTALL_DIR/config/candidate_profile.md               ║"
echo "║                                                               ║"
echo "║  4. Start the dashboard:                                     ║"
echo "║     python3 -m src.pipeline.dashboard --db \$DB_PATH          ║"
echo "║                                                               ║"
echo "║  5. Run first search manually:                               ║"
echo "║     bash scripts/daily_pipeline.sh                            ║"
echo "║                                                               ║"
echo "╚═══════════════════════════════════════════════════════════════╝"

log "Setup complete! $(date)"
