#!/usr/bin/env bash
# ╔═══════════════════════════════════════════════════════════════════════╗
# ║  bin/apply-from-sheet.sh — ADOPT-11 entry point for ADOPT-12 (CUA)   ║
# ║                                                                     ║
# ║  Reads jobs.status = 'approved' from SQLite (already populated by    ║
# ║  --sync-from-sheet), generates tailored CV + Anschreiben per job,   ║
# ║  and packages them into a ready-to-apply ZIP. The actual submission ║
# ║  step lives in ADOPT-12 (CUA driver) — this script only stages     ║
# ║  the documents.                                                    ║
# ║                                                                     ║
# ║  Workflow (per Lars's daily review loop):                          ║
# ║    1. Cron 09:00  → Sheet updated                                  ║
# ║    2. Lars marks rows in the Sheet on phone/laptop                 ║
# ║    3. bash scripts/lars-daily-run.sh --sync-from-sheet             ║
# ║           → pushes ★ / ✗ decisions into SQLite                    ║
# ║    4. bash bin/apply-from-sheet.sh [options]                       ║
# ║           → generates CV + CL ZIP for the approved jobs            ║
# ║    5. ADOPT-12 (CUA driver) takes the ZIP and submits              ║
# ║                                                                     ║
# ║  Usage:                                                             ║
# ║    bash bin/apply-from-sheet.sh                 # all approved, gen  ║
# ║    bash bin/apply-from-sheet.sh --dry-run       # list only, no gen ║
# ║    bash bin/apply-from-sheet.sh --limit 10      # cap batch size    ║
# ║    bash bin/apply-from-sheet.sh --job 42        # single-job apply  ║
# ║    bash scripts/lars-daily-run.sh --apply       # same, via wrapper ║
# ╚═══════════════════════════════════════════════════════════════════════╝

set -euo pipefail

log()   { printf '[%s] %s\n' "$(date '+%H:%M:%S %Z')" "$*"; }
fail()  { log "[FAIL] $*" >&3; log "[FAIL] $*"; exit 1; }

# ───────────────────────────────────────────────────────────────────────
# Paths & flags
# ───────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

DRY_RUN=0
LIMIT=0
SINGLE_JOB_ID=""
for arg in "$@"; do
  case "$arg" in
    --dry-run)         DRY_RUN=1 ;;
    --limit)           LIMIT="${2:-0}"; shift ;;
    --job)             SINGLE_JOB_ID="${2:-}"; shift ;;
    -h|--help)
      sed -n '2,28p' "$0"
      exit 0
      ;;
  esac
  shift || true
done

# Defaults from the .env — same pattern as lars-daily-run.sh.
: "${DB_PATH:=$PROJECT_DIR/data/jobs.db}"
: "${BATCHES_DIR:=$PROJECT_DIR/data/batches}"

if [[ ! -f "$DB_PATH" ]]; then
  fail "DB not found at $DB_PATH — run scripts/setup.sh first."
fi
mkdir -p "$BATCHES_DIR"
log "Project: $PROJECT_DIR"
log "DB:      $DB_PATH"
log "Out:     $BATCHES_DIR"

# ───────────────────────────────────────────────────────────────────────
# Build the SELECT based on flags
# ───────────────────────────────────────────────────────────────────────
build_sql() {
  local where="status = 'approved'"
  if [[ -n "$SINGLE_JOB_ID" ]]; then
    where="id = ${SINGLE_JOB_ID} AND ${where}"
  fi
  printf "SELECT id, title, company, location, url, career_url, score, source, language FROM jobs WHERE %s ORDER BY score DESC" "$where"
}

LIST_SQL="$(build_sql)"
APPROVED_JSON="$(/usr/bin/env python3 - "$DB_PATH" "$LIST_SQL" "$LIMIT" <<'PY'
import json, sqlite3, sys
db_path, sql, limit = sys.argv[1], sys.argv[2], int(sys.argv[3] or 0)
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
rows = conn.execute(sql).fetchall()
if limit > 0:
    rows = rows[:limit]
print(json.dumps([dict(r) for r in rows], indent=2, ensure_ascii=False))
PY
)"

COUNT="$(printf '%s' "$APPROVED_JSON" | /usr/bin/env python3 -c 'import json,sys; print(len(json.loads(sys.stdin.read())))')"
log "Approved jobs matching: $COUNT"

if [[ "$COUNT" -eq 0 ]]; then
  log "Nothing to apply — exit 0 (cron-friendly)."
  exit 0
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  /usr/bin/env python3 - "$APPROVED_JSON" <<'PY'
import json, sys
rows = json.loads(sys.argv[1])
print(f"\n{'─'*72}")
print(f"  Dry-run: {len(rows)} approved job(s) — no files written")
print(f"{'─'*72}")
for i, r in enumerate(rows, 1):
    print(f"  {i:2d}. [id={r['id']:4d}] score={r['score']:4d}  {r['title'][:50]:50s} @ {r['company'][:25]}")
    print(f"      url={r['url']}")
print(f"{'─'*72}")
print("Re-run without --dry-run to generate CV + Anschreiben ZIP.")
PY
  exit 0
fi

# ───────────────────────────────────────────────────────────────────────
# Generate documents — delegates to batch_pipeline (ADOPT-3 templates).
# We use the same pipeline the daily run uses, but constrained to the
# approved set. ADOPT-12 (CUA) will pick up the resulting ZIP.
# ───────────────────────────────────────────────────────────────────────
log "Generating CV + Anschreiben for $COUNT approved job(s)…"

BATCH_DATE="$(date +%Y-%m-%d)"
BATCH_DIR="$BATCHES_DIR/apply-$BATCH_DATE"
mkdir -p "$BATCH_DIR"

# Reuse batch_pipeline's document-generation by piping the approved rows
# into a one-shot Python harness. We keep this thin — the document
# generation code lives in src/generation/* so a future ADOPT-12 change
# to ATS templates flows through automatically.
APPROVED_JSON_PATH="$BATCH_DIR/_approved.json"
printf '%s' "$APPROVED_JSON" > "$APPROVED_JSON_PATH"

set +e
/usr/bin/env python3 -m src.pipeline.batch_pipeline \
    --db "$DB_PATH" \
    --limit "$COUNT" \
    --approved-from "$APPROVED_JSON_PATH" \
    --out "$BATCH_DIR" 2>&1 | tee "$BATCH_DIR/generate.log"
gen_rc=${PIPESTATUS[0]}
set -e

if [[ $gen_rc -ne 0 ]]; then
  fail "document generation exited $gen_rc — see $BATCH_DIR/generate.log"
fi

ZIP_PATH="$(/usr/bin/env python3 -c 'import shutil, sys; print(shutil.make_archive(sys.argv[1], "zip", sys.argv[1]))' "$BATCH_DIR")"
log "Ready-to-apply ZIP: $ZIP_PATH"

# Summary line for Discord / cron output.
TOP_TITLES="$(printf '%s' "$APPROVED_JSON" | /usr/bin/env python3 -c "
import json, sys
rows = json.loads(sys.stdin.read())
for r in rows[:5]:
    print(f\"  • {r['title'][:48]:48s} @ {r['company'][:20]}\")
if len(rows) > 5:
    print(f'  … and {len(rows)-5} more in {sys.argv[1]}')
" "$ZIP_PATH")"

cat <<EOF
──────────────────────────────────────────────────────────────
⚓ Apply batch ready: $BATCH_DATE  ($COUNT job(s))
  ZIP: $ZIP_PATH
$TOP_TITLES
──────────────────────────────────────────────────────────────
EOF

log "apply-from-sheet DONE"
exit 0
