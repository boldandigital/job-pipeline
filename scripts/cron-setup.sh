#!/usr/bin/env bash
# ╔═══════════════════════════════════════════════════════════════════════╗
# ║  CRON SETUP — Installs all scheduled jobs                           ║
# ║  Run after initial setup is complete                                ║
# ╚═══════════════════════════════════════════════════════════════════════╝
set -euo pipefail

GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log()  { echo -e "${GREEN}[OK]${NC} $1"; }

INSTALL_DIR="${INSTALL_DIR:-/opt/job-pipeline}"

info "Installing cron jobs..."

CRON_FILE=$(mktemp)
cat > "$CRON_FILE" <<CRONEOF
# Job Search Pipeline — Cron Schedule (all times UTC)
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin

# Load environment
DB_PATH=$INSTALL_DIR/data/jobs.db
JOBSPY_DB_PATH=$INSTALL_DIR/data/jobspy.db

# 02:00 — Daily job search (Indeed + LinkedIn + Arbeitsagentur)
0 2 * * * cd $INSTALL_DIR && bash scripts/daily_pipeline.sh >> logs/daily.log 2>&1

# 03:00 — Score and filter new jobs
0 3 * * * cd $INSTALL_DIR && python3 -m src.scoring.score_jobs --db \$DB_PATH >> logs/scoring.log 2>&1

# 04:00 — Batch pipeline (Top 50 → CV + CL → ZIP → Discord)
0 4 * * * cd $INSTALL_DIR && python3 -m src.pipeline.batch_pipeline --db \$DB_PATH --limit 50 >> logs/batch.log 2>&1

# 08:30 — Extended scraping (StepStone browser automation)
30 8 * * * cd $INSTALL_DIR && bash scripts/stepstone_pipeline.sh >> logs/stepstone.log 2>&1

# 09:00 — Score extended results
0 9 * * * cd $INSTALL_DIR && python3 -m src.scoring.score_jobs --db \$DB_PATH >> logs/scoring.log 2>&1
CRONEOF

crontab "$CRON_FILE"
rm "$CRON_FILE"

log "Daily job search (02:00)"
log "Score & filter (03:00)"
log "Batch pipeline (04:00)"
log "StepStone scrape (08:30)"
log "Score extended (09:00)"

echo ""
log "All 5 cron jobs installed!"
echo ""
info "Verify with: crontab -l"
