"""Rejection analytics for the Sheet→DB feedback loop (ADOPT-11).

Why
---
Lars's rejections in the daily Sheet are the highest-signal feedback the
pipeline gets. They tell us which categories of jobs the scorer is letting
through that he never wants — which means we should either:

  a) Add a stricter filter (negative_signal in config/lars.yaml), or
  b) Lower a positive weight that's over-firing, or
  c) Tighten the seniority threshold.

This script is the slowest of those feedback loops: it produces a markdown
report ``research/rejection-patterns-YYYY-MM.md`` grouping the last 30
days of rejections by enum key, and *suggests* config tweaks.

Hard rules
----------
- Read-only. Never writes to SQLite or the Sheet.
- The suggestions are COMMENTS in the markdown report — Lars applies them
  to config/lars.yaml himself, no automation.
- Window is configurable (default 30 days) so we can backfill historical
  analytics when this first runs against an existing DB.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Reuse the rejection enum from sync_approvals — single source of truth.
from src.sheet.sync_approvals import REJECTION_REASONS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("analytics.rejections")

DB_PATH = os.getenv("DB_PATH", "./data/jobs.db")
DEFAULT_WINDOW_DAYS = 30


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def fetch_rejections(
    db_path: str,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Return every rejection in the last ``window_days``.

    Each dict has: title, company, reason (enum key), note (free text),
    updated_at. Includes jobs that were rejected then later flipped (we
    don't have a dedicated audit log — the current state is the state).

    If the DB doesn't exist, returns [].
    """
    if not Path(db_path).exists():
        log.warning("DB not found at %s — no analytics to produce.", db_path)
        return []

    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(days=window_days)
    cutoff_iso = cutoff.isoformat(sep=" ")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, title, company, score, source, rejection_reason,
                   rejection_note, updated_at
              FROM jobs
             WHERE status = 'rejected'
               AND rejection_reason IS NOT NULL
               AND updated_at >= ?
             ORDER BY updated_at DESC
            """,
            (cutoff_iso,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def group_by_reason(rejections: list[dict]) -> dict[str, list[dict]]:
    """Bucket rejections by enum key. Preserves insertion order of REJECTION_REASONS
    so the rendered report shows them in a stable order, not by frequency."""
    out: dict[str, list[dict]] = {k: [] for k in REJECTION_REASONS}
    for r in rejections:
        key = r.get("rejection_reason") or "other"
        out.setdefault(key, []).append(r)
    return out


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------

# Heuristic suggestions. Kept intentionally conservative — Lars applies them
# by hand, never auto-applied. Each key maps to one paragraph of guidance.
_SUGGESTIONS: dict[str, str] = {
    "too_junior": (
        "If **too_junior** is dominant, the seniority filter in "
        "`src/scoring/apply_filter.py` is letting Werkstudent/Intern/Trainee "
        "posts through. Add the offending titles to `JUNIOR_BLOCK` or raise "
        "`config/lars.yaml` `scoring.threshold` from 20 → 30."
    ),
    "too_senior": (
        "If **too_senior** is dominant, the scoring is over-firing on "
        "leadership keywords (CTO/VP/Head of). Drop those positive weights "
        "or add a `negative_signals` entry weighting them -200 in "
        "`config/lars.yaml`."
    ),
    "wrong_location": (
        "If **wrong_location** is dominant, your `search.locations` list "
        "in `config/lars.yaml` is too broad. Drop the cities/regions you "
        "never apply to. For remote-only, set all locations to "
        "'Remote, Europe' and remove the geographic ones."
    ),
    "wrong_sector": (
        "If **wrong_sector** is dominant, add a sector negative weight in "
        "`config/lars.yaml` `scoring.weights.negative_signals` (e.g. "
        "`-150` for `gambling`, `defense`, `crypto`) or expand the "
        "`ALWAYS_IRRELEVANT` regex list in `apply_filter.py`."
    ),
    "salary_too_low": (
        "If **salary_too_low** is dominant, your `candidate_profile.md` "
        "salary floor is being ignored at the scraper level. Filter at the "
        "scraper: add a `min_salary_eur` to `config/lars.yaml` and have the "
        "scrapers drop rows below it."
    ),
    "language_mismatch": (
        "If **language_mismatch** is dominant, tighten `LANGUAGE_BLOCK` "
        "in `src/scoring/apply_filter.py` or add per-language negative "
        "weights in `config/lars.yaml`."
    ),
    "not_interested": (
        "**not_interested** is a grab-bag — inspect the per-row notes in "
        "the report below to extract recurring patterns, then codify them "
        "as either a filter regex or a negative weight."
    ),
    "duplicate": (
        "If **duplicate** is high, the scraper is hitting the same job "
        "across multiple sources. The UNIQUE(title, company) constraint "
        "should prevent this — investigate which scraper is bypassing it."
    ),
    "company_blacklist": (
        "If **company_blacklist** is high, you have an explicit blacklist. "
        "Lift it into `config/lars.yaml` so the scorer filters them out "
        "before you spend tokens scoring them."
    ),
    "other": (
        "**other** reasons usually point to a missing enum key. Review "
        "the rejection_note column — when 3+ rows say the same thing in "
        "free text, promote that string into `REJECTION_REASONS` in "
        "`src/sheet/sync_approvals.py` and ship a new enum key."
    ),
}


def _suggestions_for(top: list[tuple[str, int]], total: int) -> list[str]:
    """Pick the top 3 reasons and emit one suggestion paragraph each."""
    bullets: list[str] = []
    for key, count in top[:3]:
        share = (count / total) * 100 if total else 0
        para = _SUGGESTIONS.get(key, "")
        if para:
            bullets.append(
                f"- **{REJECTION_REASONS[key]}** "
                f"({count} jobs, {share:.0f}% of rejections) — {para}"
            )
    return bullets


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def render_markdown(
    rejections: list[dict],
    *,
    window_days: int,
    generated_at: datetime,
) -> str:
    """Return the markdown report as a string. Caller writes to disk."""
    grouped = group_by_reason(rejections)
    counts: Counter[str] = Counter()
    for key, rows in grouped.items():
        counts[key] = len(rows)
    total = sum(counts.values())
    top = counts.most_common()

    lines: list[str] = []
    lines.append(f"# Rejection patterns — last {window_days} days")
    lines.append("")
    lines.append(f"_Generated {generated_at.strftime('%Y-%m-%d %H:%M %Z')}_")
    lines.append("")
    lines.append(f"**Total rejections:** {total}")
    lines.append("")

    if total == 0:
        lines.append("_No rejections in the window. Either nobody's been rejected, "
                     "or `--sync-from-sheet` hasn't been run yet._")
        lines.append("")
        return "\n".join(lines)

    # Summary table
    lines.append("## Summary by reason")
    lines.append("")
    lines.append("| Reason | Count | Share |")
    lines.append("|---|---:|---:|")
    for key, count in top:
        share = (count / total) * 100 if total else 0
        lines.append(
            f"| {REJECTION_REASONS.get(key, key)} (`{key}`) "
            f"| {count} | {share:.0f}% |"
        )
    lines.append("")

    # Suggestions
    lines.append("## Suggested config tweaks")
    lines.append("")
    lines.extend(_suggestions_for(top, total))
    lines.append("")

    # Per-bucket detail
    lines.append("## Per-reason detail")
    lines.append("")
    for key, count in top:
        rows = grouped.get(key, [])
        lines.append(
            f"### {REJECTION_REASONS.get(key, key)} — {count} job(s)"
        )
        lines.append("")
        for r in rows[:10]:  # cap detail rows; full data is in SQLite
            note = r.get("rejection_note") or ""
            note_str = f" — _{note}_" if note else ""
            lines.append(
                f"- `{r.get('updated_at', '')[:10]}` "
                f"`{r.get('source') or '?'}` "
                f"**{r.get('title') or '?'}** @ {r.get('company') or '?'}"
                f" (score {r.get('score') or 0}){note_str}"
            )
        if len(rows) > 10:
            lines.append(f"- … and {len(rows) - 10} more (see SQLite)")
        lines.append("")

    # Methodology footer — keeps the report trustworthy.
    lines.append("---")
    lines.append("")
    lines.append(
        "Methodology: pulls every `jobs` row with "
        "`status='rejected' AND rejection_reason IS NOT NULL AND updated_at >= now-window_days`. "
        "Counts are derived from the live SQLite state — the report reflects "
        "the *current* rejection per job, not a history. To preserve history, "
        "snapshot the `jobs` table weekly."
    )
    return "\n".join(lines)


def write_report(
    db_path: str,
    *,
    out_dir: str = "./research",
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: Optional[datetime] = None,
) -> Optional[Path]:
    """Build the report and write it under ``out_dir``.

    Returns the written path, or None if there's nothing to report / DB missing.
    """
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    rejections = fetch_rejections(db_path, window_days=window_days, now=now)
    body = render_markdown(rejections, window_days=window_days, generated_at=now)

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    fname = f"rejection-patterns-{now.strftime('%Y-%m')}.md"
    path = Path(out_dir) / fname
    path.write_text(body, encoding="utf-8")
    log.info("Wrote %s (%d bytes)", path, len(body))
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ADOPT-11 rejection-pattern dashboard",
    )
    p.add_argument("--db", dest="db_path", default=os.getenv("DB_PATH", DB_PATH))
    p.add_argument("--out-dir", default="./research",
                   help="Where to write the markdown report (default ./research)")
    p.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS,
                   help="Lookback window in days (default 30)")
    p.add_argument("--json", action="store_true",
                   help="Emit counts as JSON instead of writing a markdown file")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    rejections = fetch_rejections(
        args.db_path, window_days=args.window_days,
    )
    grouped = group_by_reason(rejections)
    counts = {k: len(v) for k, v in grouped.items()}
    total = sum(counts.values())

    if args.json:
        print(json.dumps({
            "window_days": args.window_days,
            "total": total,
            "by_reason": counts,
        }, indent=2, ensure_ascii=False))
        return 0

    path = write_report(
        args.db_path,
        out_dir=args.out_dir,
        window_days=args.window_days,
    )
    if path is None:
        return 0
    print(f"⚓ rejection report → {path}")
    print(f"   {total} rejection(s) in the last {args.window_days} days.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
