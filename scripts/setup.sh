#!/usr/bin/env bash
# ╔═══════════════════════════════════════════════════════════════════════╗
# ║  JOB SEARCH PIPELINE — SETUP SCRIPT                                  ║
# ║  Portable: Ubuntu 22.04+ AND macOS (Darwin)                          ║
# ║  Idempotent · Run as root on Linux, normal user on macOS             ║
# ║  Run: bash scripts/setup.sh                   (auto-detects OS)      ║
# ║       sudo bash scripts/setup.sh              (Linux: full system)    ║
# ║                                                                       ║
# ║  History:                                                             ║
# ║    Original:  Ubuntu-only (apt, ufw, docker, swap)                   ║
# ║    ADOPT-9:   Split into OS branches. macOS skips Docker/ufw/swap.   ║
# ║               Both branches: create data/ + logs/, run init_db.       ║
# ╚═══════════════════════════════════════════════════════════════════════╝
set -euo pipefail

# ============================================================
# Paths & colors
# ============================================================
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_DIR="${INSTALL_DIR:-$PROJECT_DIR}"
LOG_DIR="$INSTALL_DIR/logs"
mkdir -p "$LOG_DIR"
LOGFILE="$LOG_DIR/setup.log"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[OK]${NC} $1" | tee -a "$LOGFILE"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1" | tee -a "$LOGFILE"; }
err()  { echo -e "${RED}[ERR]${NC} $1" | tee -a "$LOGFILE"; }
info() { echo -e "${BLUE}[INFO]${NC} $1" | tee -a "$LOGFILE"; }
head() { echo -e "${CYAN}── $1 ──${NC}" | tee -a "$LOGFILE"; }

trap 'err "Script failed at line $LINENO. See $LOGFILE"' ERR

echo "" | tee -a "$LOGFILE"
info "=== Job Search Pipeline Setup started $(date) ==="
info "Project: $INSTALL_DIR"
info "Log:     $LOGFILE"

# ============================================================
# OS detect — branch into Linux (full VPS) or macOS (dev only)
# ============================================================
OS="$(uname -s)"
case "$OS" in
  Linux)
    OS_FLAVOR="linux"
    IS_ROOT=0
    [[ $EUID -eq 0 ]] && IS_ROOT=1
    if [[ $IS_ROOT -ne 1 ]]; then
      err "Linux setup must be run as root: sudo bash scripts/setup.sh"
      exit 1
    fi
    ;;
  Darwin)
    OS_FLAVOR="macos"
    ;;
  *)
    err "Unsupported OS: $OS (this script handles Linux and Darwin)."
    exit 1
    ;;
esac

info "Detected OS: $OS_FLAVOR"

# ============================================================
# PHASE 1: System dependencies + DB schema
# Both branches converge here (Linux: via apt / macOS: assume present)
# ============================================================
head "Phase 1: System dependencies + project layout"

if [[ "$OS_FLAVOR" == "linux" ]]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq 2>>"$LOGFILE"
  apt-get upgrade -y -qq 2>>"$LOGFILE"
  apt-get install -y -qq \
    curl wget git unzip jq sqlite3 \
    python3 python3-pip python3-venv \
    ca-certificates gnupg lsb-release \
    htop tmux \
    2>>"$LOGFILE"
  log "APT system packages installed"
else
  # macOS: assume brew + python3 already on PATH. Quick checks, no install.
  for cmd in python3 sqlite3 git curl; do
    if ! command -v "$cmd" &>/dev/null; then
      warn "$cmd not on PATH — install via 'brew install <pkg>' (Homebrew) or Xcode CLT, then re-run."
    else
      _p=$(command -v "$cmd")
      log "$cmd available: $_p"
    fi
  done
fi

# Project-local directories (idempotent — always safe)
mkdir -p "$INSTALL_DIR"/{data,logs,config,batches,screenshots}
log "Project directories ready at $INSTALL_DIR"

# ============================================================
# PHASE 2: Python dependencies (both OS)
# ============================================================
head "Phase 2: Python dependencies"

# pyyaml for score_jobs; patchright for browser scraper. requests for HTTP.
PY_DEPS=(requests pyyaml)
if [[ "$OS_FLAVOR" == "macos" ]]; then
  # patchright only needed for the headless scraper — skip if user hasn't
  # asked. They can run `python3 -m patchright install chromium` later.
  :
else
  # Linux (the original VPS path) installs patchright with chromium.
  pip3 install --break-system-packages "${PY_DEPS[@]}" patchright 2>>"$LOGFILE" || \
    warn "pip3 install failed — re-run after fixing the python env."
  python3 -m patchright install --with-deps chromium 2>>"$LOGFILE" || \
    warn "patchright chromium install failed — non-fatal; needed only for stepstone_scraper."
fi
log "Python core deps installed (${PY_DEPS[*]})"

# ============================================================
# PHASE 3: SQLite schema (canonical)
# ADOPT-9: src/db/__init__.py:init_db() is the single source of truth.
# ============================================================
head "Phase 3: SQLite schema"

DB_PATH="$INSTALL_DIR/data/jobs.db"
echo "$DB_PATH" >> "$LOGFILE"

# Run init_db via Python — creates table + all indexes in one shot.
init_out="$(python3 - <<PY 2>>"$LOGFILE"
import sys
sys.path.insert(0, "$INSTALL_DIR")
from src.db import init_db, ensure_data_dirs
ensure_data_dirs("$INSTALL_DIR")
conn = init_db("$DB_PATH")
cols = conn.execute("PRAGMA table_info(jobs)").fetchall()
print(f"jobs table ready: {len(cols)} columns")
conn.close()
PY
)" || {
  err "init_db failed — see $LOGFILE"
  exit 1
}
log "$init_out"

# Idempotent migration for DBs created BEFORE ADOPT-9. ADOPT-6 added
# career_url + description at runtime; ADOPT-9 promoted them into the
# canonical schema. Existing DBs still get them via ALTER TABLE.
if python3 "$INSTALL_DIR/scripts/migrate_schema_add_career_description.py" --db "$DB_PATH" 2>>"$LOGFILE"; then
  log "Schema migration applied (additive, idempotent)"
else
  warn "Schema migration reported an issue (non-fatal — schema.sql already covers it)"
fi

# ============================================================
# PHASE 4: OS-specific extras — Docker / Swap / Firewall / Cron (Linux only)
# ============================================================
if [[ "$OS_FLAVOR" == "linux" ]]; then
  head "Phase 4 (Linux): Docker + Swap + Firewall + Cron"

  # Docker
  if command -v docker &>/dev/null; then
    log "Docker already installed: $(docker --version)"
  else
    curl -fsSL https://get.docker.com | sh 2>>"$LOGFILE"
    log "Docker installed: $(docker --version)"
  fi
  if docker compose version &>/dev/null; then
    log "Docker Compose available: $(docker compose version --short)"
  else
    apt-get install -y docker-compose-plugin 2>>"$LOGFILE" || true
  fi
  systemctl enable docker 2>>"$LOGFILE" || true
  systemctl start docker 2>>"$LOGFILE" || true
  log "Docker service running"

  # Swap (helps 4-8GB VPS)
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
    sysctl vm.swappiness=10 2>>"$LOGFILE" || true
    log "2GB Swap configured"
  fi

  # UFW
  if command -v ufw &>/dev/null; then
    ufw default deny incoming 2>>"$LOGFILE" || true
    ufw default allow outgoing 2>>"$LOGFILE" || true
    ufw allow ssh 2>>"$LOGFILE" || true
    ufw allow 8080/tcp comment "Dashboard" 2>>"$LOGFILE" || true
    ufw --force enable 2>>"$LOGFILE" || true
    log "UFW configured (SSH + Dashboard)"
  else
    warn "ufw not installed — skipping firewall config (install via apt if needed)"
  fi

  # Cron — schedule daily_pipeline + stepstone_pipeline
  CRON_FILE="/tmp/job-pipeline-cron-$$"
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

  if crontab "$CRON_FILE" 2>>"$LOGFILE"; then
    log "Cron jobs installed (daily + stepstone)"
  else
    warn "crontab install failed — set up cron manually with scripts/cron.snippet"
  fi
  rm -f "$CRON_FILE"

else
  head "Phase 4 (macOS): skipped"
  info "Linux-only steps (Docker, swap, UFW, cron) are N/A on macOS."
  info "macOS users schedule jobs via 'crontab -e' or a launchd plist — see scripts/cron.snippet."
fi

# ============================================================
# PHASE 5: .env template (both OS — idempotent)
# ============================================================
head "Phase 5: .env template"

ENV_FILE="$INSTALL_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<'ENVEOF'
# Job Search Pipeline — Environment Variables
# NEVER commit this file to git!

# Anthropic API Key (from console.anthropic.com)
ANTHROPIC_API_KEY=your-key-here

# Discord delivery (ADOPT-8)
# We use the SAME Hermes Discord bot — share ~/Documents/Projects/job-pipeline/.env's
# pattern. For this Docker container, either paste a bot token + channel ID, or
# set DISCORD_WEBHOOK_URL as a fallback. See ../docs/SECRETS.md for context.
DISCORD_BOT_TOKEN=<your-bot-token>
DISCORD_CHANNEL_ID=<channel-snowflake>
# Optional display name override
DISCORD_USERNAME=Lars Job Pipeline
# Legacy webhook (ADOPT-7) — kept for backwards compat; bot mode wins if both are set
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/<your_id>/<your_token>

# Proxy for scraping (optional, recommended for Indeed)
PROXY_URL=http://user:pass@proxy-host:port

# Database paths (default: project-relative, ADOPT-9)
DB_PATH=./data/jobs.db
JOBSPY_DB_PATH=./data/jobspy.db

# Scoring config — ADOPT-9 wiring
# Default points to Lars's tuned config so daily runs use his weights.
# Override with SCORING_CONFIG=./config/example.yaml for stock behavior.
SCORING_CONFIG=./config/lars.yaml

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
# SUMMARY
# ============================================================
echo ""
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║                    SETUP COMPLETE ($OS_FLAVOR)                          ║"
echo "╠═══════════════════════════════════════════════════════════════╣"
echo "║                                                               ║"
echo "║  NEXT STEPS:                                                  ║"
echo "║                                                               ║"
echo "║  1. Fill in secrets:                                          ║"
echo "║     nano $INSTALL_DIR/.env                                    ║"
echo "║                                                               ║"
echo "║  2. Copy your config (if not already):                        ║"
echo "║     cp config/example.yaml $INSTALL_DIR/config/settings.yaml  ║"
echo "║     # OR use the tuned config (already shipped as lars.yaml): ║"
echo "║     # lars-daily-run.sh defaults to config/lars.yaml          ║"
echo "║                                                               ║"
echo "║  3. Copy candidate profile:                                   ║"
echo "║     cp config/candidate_profile.example.md                    ║"
echo "║        $INSTALL_DIR/config/candidate_profile.md               ║"
echo "║                                                               ║"
echo "║  4. Start the dashboard:                                      ║"
echo "║     python3 -m src.pipeline.dashboard --db ./data/jobs.db     ║"
echo "║                                                               ║"
echo "║  5. Run first search manually (macOS dev):                    ║"
echo "║     bash scripts/lars-daily-run.sh --dry-run                  ║"
echo "║                                                               ║"
echo "╚═══════════════════════════════════════════════════════════════╝"

log "Setup complete! OS=$OS_FLAVOR  $(date)"
