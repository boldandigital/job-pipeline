#!/usr/bin/env python3
"""
LLM-based job scorer using Anthropic Haiku.

Scores jobs on 5 dimensions based on full description + candidate profile.
Replaces keyword-based scoring with context-aware LLM evaluation.

Usage:
    python -m src.scoring.llm_scorer
    python -m src.scoring.llm_scorer --rescore
    python -m src.scoring.llm_scorer --limit 10
    python -m src.scoring.llm_scorer --dry-run
    python -m src.scoring.llm_scorer --profile config/candidate_profile.md
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import HTTPError

DB_PATH = os.getenv("DB_PATH", "./data/jobs.db")
JOBSPY_DB_PATH = os.getenv("JOBSPY_DB_PATH", "./data/jobspy.db")
ENV_PATH = os.getenv("ENV_FILE", "./.env")
PROFILE_PATH = os.getenv("CANDIDATE_PROFILE", "./config/candidate_profile.md")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("llm_scorer")


def load_candidate_profile(path=None):
    """Load candidate profile from config file."""
    profile_path = path or PROFILE_PATH
    if os.path.exists(profile_path):
        with open(profile_path) as f:
            return f.read().strip()
    log.warning("No candidate profile found at %s. Using empty profile.", profile_path)
    return (
        "No candidate profile configured. "
        "Create config/candidate_profile.md with your background, "
        "skills, preferences, and salary expectations."
    )


SCORING_PROMPT = """
You are a job-matching expert. Score how well this job fits the candidate.

SCORING DIMENSIONS (score each 0-100):

1. DAY_TO_DAY_FIT (weight: 35%):
   Does the actual daily work match what the candidate wants?
   High: process analysis, building automations, tool research, AI implementation
   Low: pure coding, pure management, pure sales, compliance/reporting only

2. GROWTH_POTENTIAL (weight: 20%):
   Can the candidate learn, grow, and shape the role?
   High: greenfield, new team, "you build this from scratch", startup energy
   Low: maintenance of existing systems, rigid role definition, no learning

3. ENTRY_BARRIER (weight: 20%):
   How realistic is it that this candidate gets an interview?
   High (easy entry): "Berufseinsteiger willkommen", no hard requirements, open culture
   Low (hard entry): requires 5+ years specific experience, hard certifications, senior leadership

4. CULTURE_SIGNALS (weight: 15%):
   Does the company culture match?
   High: "Macher" culture, hands-on, tool-agnostic, modern, AI-friendly, flat hierarchy
   Low: bureaucratic, traditional, rigid, no mention of modern tools

5. COMPENSATION_FIT (weight: 10%):
   Is the salary likely in the candidate's range? Consider market norms.
   High: salary mentioned in range or above
   Medium: no salary mentioned but role/company suggests fair pay
   Low: typically underpaid role, or salary mentioned below range

BONUS SIGNALS (add up to +10 to final score):
- "Erste Erfahrung reicht" / "Berufseinsteiger willkommen" -> +3
- "Du gestaltest neue Prozesse" / greenfield language -> +3
- "Macher" / "hands-on" / action-oriented language -> +2
- Remote or candidate's preferred location -> +2
- FinTech, AI startup, digital transformation context -> +2
- Tool-agnostic / freedom to choose solutions -> +2

HARD BLOCKERS (immediately score 0, no exceptions):
- Werkstudent / Working Student / Praktikum / Praktikant / Intern / Internship
- These are student positions. The candidate needs a full position.
- If the title contains any of these words, return weighted_score: 0 immediately.

PENALTY SIGNALS (subtract up to -10 from final score):
- Pure development role (80%+ coding) -> -5
- SAP/ERP-specific -> -5
- Hard certification requirement -> -3
- Senior requiring 8+ years leadership -> -3
- On-site only, far from candidate's location -> -3

Respond ONLY with valid JSON, no markdown:
{
  "day_to_day_fit": <0-100>,
  "growth_potential": <0-100>,
  "entry_barrier": <0-100>,
  "culture_signals": <0-100>,
  "compensation_fit": <0-100>,
  "bonus": <0 to 10>,
  "penalty": <0 to -10>,
  "weighted_score": <calculated weighted average + bonus + penalty, 0-100>,
  "reasoning": "<2-3 sentences: why this score, what's the key match/mismatch>",
  "top_match_reason": "<1 short phrase for dashboard display, e.g. 'AI process automation at FinTech'>"
}
""".strip()


def load_api_key():
    """Load Anthropic API key from env file or environment variable."""
    # Check environment variable first
    key = os.getenv("ANTHROPIC_API_KEY")
    if key:
        return key

    # Fall back to .env file
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("ANTHROPIC_API_KEY="):
                    return line.split("=", 1)[1]

    raise RuntimeError(
        "ANTHROPIC_API_KEY not found. Set it as an environment variable "
        "or in the .env file at " + ENV_PATH
    )


def call_haiku(api_key, system_prompt, user_prompt, max_retries=3):
    """Call Anthropic Haiku API and return parsed JSON response."""
    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 512,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }).encode("utf-8")

    for attempt in range(max_retries):
        try:
            req = Request(
                "https://api.anthropic.com/v1/messages",
                data=payload,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                method="POST",
            )
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            text = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    text += block["text"]

            # Parse JSON from response
            text = text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

            return json.loads(text), data.get("usage", {})

        except HTTPError as e:
            if e.code == 429:
                wait = min(2 ** attempt * 5, 30)
                log.warning("Rate limited, waiting %ds...", wait)
                time.sleep(wait)
                continue
            elif e.code == 529:
                log.warning("API overloaded, waiting 10s...")
                time.sleep(10)
                continue
            raise
        except json.JSONDecodeError as e:
            log.warning("JSON parse error (attempt %d): %s — raw: %s", attempt + 1, e, text[:200])
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return None, {}

    return None, {}


def migrate_db(conn):
    """Add columns needed for LLM scoring if they don't exist."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}

    migrations = {
        "description": "TEXT",
        "llm_score": "INTEGER",
        "llm_reasoning": "TEXT",
        "llm_match_reason": "TEXT",
        "llm_scores_json": "TEXT",
        "llm_scored_at": "TIMESTAMP",
    }

    for col, dtype in migrations.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {dtype}")
            log.info("Added column: %s %s", col, dtype)

    conn.commit()


def copy_descriptions_from_jobspy(conn, jobspy_db_path):
    """Copy descriptions from JobSpy DB to main DB for matching jobs."""
    if not os.path.exists(jobspy_db_path):
        log.warning("JobSpy DB not found at %s, skipping description copy.", jobspy_db_path)
        return 0

    try:
        conn.execute(f"ATTACH DATABASE '{jobspy_db_path}' AS jobspy")
    except Exception as e:
        log.warning("Could not attach JobSpy DB: %s", e)
        return 0

    # Match by URL (Indeed/LinkedIn jobs share URLs)
    updated = conn.execute("""
        UPDATE jobs
        SET description = (
            SELECT j.description
            FROM jobspy.jobs j
            WHERE j.job_url = jobs.url
            AND j.description IS NOT NULL
            AND length(j.description) > 100
            LIMIT 1
        )
        WHERE jobs.description IS NULL
        AND jobs.source IN ('indeed', 'linkedin')
        AND EXISTS (
            SELECT 1 FROM jobspy.jobs j
            WHERE j.job_url = jobs.url
            AND j.description IS NOT NULL
            AND length(j.description) > 100
        )
    """).rowcount

    conn.commit()
    conn.execute("DETACH DATABASE jobspy")
    return updated


def fetch_description_via_api(api_key, url, title, company):
    """Use Anthropic web search to fetch job description."""
    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 2048,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "messages": [{
            "role": "user",
            "content": (
                f"Find the full job description for this position:\n"
                f"Title: {title}\n"
                f"Company: {company}\n"
                f"URL: {url}\n\n"
                f"Return ONLY the job description text (responsibilities, requirements, "
                f"what we offer). No commentary. If you cannot find it, return 'NOT_FOUND'."
            ),
        }],
    }).encode("utf-8")

    try:
        req = Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block["text"]

        if "NOT_FOUND" in text or len(text) < 100:
            return None, data.get("usage", {})

        return text.strip(), data.get("usage", {})

    except Exception as e:
        log.warning("Web fetch failed for %s: %s", url, e)
        return None, {}


TITLE_BLOCKLIST = [
    "werkstudent", "working student", "praktikum", "praktikant",
    "internship", "intern ", "azubi", "ausbildung", "quereinsteiger",
    "weiterbildung",
]


def score_single_job(api_key, candidate_profile, title, company, location, description, url, source):
    """Score a single job using Haiku."""
    # Pre-filter: block student/intern positions without calling API
    title_lower = (title or "").lower()
    for blocked in TITLE_BLOCKLIST:
        if blocked in title_lower:
            return {
                "day_to_day_fit": 0, "growth_potential": 0, "entry_barrier": 0,
                "culture_signals": 0, "compensation_fit": 0, "bonus": 0, "penalty": 0,
                "weighted_score": 0,
                "reasoning": f"BLOCKED: title contains '{blocked}' — not a full position",
                "top_match_reason": f"BLOCKED: {blocked}",
            }, {"input_tokens": 0, "output_tokens": 0}

    user_prompt = f"""
JOB POSTING:
============
Title: {title}
Company: {company}
Location: {location}
Source: {source}
URL: {url}

DESCRIPTION:
{description[:4000] if description else "No description available — score based on title/company/location only."}

Score this job for the candidate described in the system prompt.
""".strip()

    system = f"{candidate_profile}\n\n---\n\n{SCORING_PROMPT}"
    result, usage = call_haiku(api_key, system, user_prompt)

    if result is None:
        return None, usage

    # Calculate weighted score if not provided
    if "weighted_score" not in result:
        result["weighted_score"] = round(
            result.get("day_to_day_fit", 0) * 0.35
            + result.get("growth_potential", 0) * 0.20
            + result.get("entry_barrier", 0) * 0.20
            + result.get("culture_signals", 0) * 0.15
            + result.get("compensation_fit", 0) * 0.10
            + result.get("bonus", 0)
            + result.get("penalty", 0)
        )

    return result, usage


def main():
    parser = argparse.ArgumentParser(description="LLM-based job scorer")
    parser.add_argument("--db", default=DB_PATH, help="Path to main SQLite database")
    parser.add_argument("--jobspy-db", default=JOBSPY_DB_PATH,
                        help="Path to JobSpy SQLite database")
    parser.add_argument("--profile", default=PROFILE_PATH,
                        help="Path to candidate profile markdown file")
    parser.add_argument("--rescore", action="store_true", help="Re-score all jobs")
    parser.add_argument("--limit", type=int, default=0, help="Max jobs to score")
    parser.add_argument("--dry-run", action="store_true", help="Print without saving")
    parser.add_argument("--fetch-descriptions", action="store_true",
                        help="Fetch missing descriptions via web search (costs tokens)")
    parser.add_argument("--fetch-limit", type=int, default=50,
                        help="Max descriptions to fetch per run")
    parser.add_argument("--min-keyword-score", type=int, default=0,
                        help="Only LLM-score jobs with keyword score >= this (saves tokens)")
    args = parser.parse_args()

    api_key = load_api_key()
    log.info("API key loaded")

    candidate_profile = load_candidate_profile(args.profile)
    log.info("Candidate profile loaded (%d chars)", len(candidate_profile))

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Step 1: Migrate DB
    migrate_db(conn)

    # Step 2: Copy descriptions from JobSpy
    copied = copy_descriptions_from_jobspy(conn, args.jobspy_db)
    if copied > 0:
        log.info("Copied %d descriptions from JobSpy DB", copied)

    # Step 3: Optionally fetch missing descriptions
    if args.fetch_descriptions:
        missing = conn.execute("""
            SELECT id, title, company, url, source FROM jobs
            WHERE description IS NULL
            AND score >= ?
            ORDER BY score DESC
            LIMIT ?
        """, (args.min_keyword_score, args.fetch_limit)).fetchall()

        log.info("Fetching descriptions for %d jobs...", len(missing))
        fetched = 0
        total_tokens = 0

        for row in missing:
            desc, usage = fetch_description_via_api(
                api_key, row["url"], row["title"], row["company"]
            )
            total_tokens += usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

            if desc:
                conn.execute(
                    "UPDATE jobs SET description = ? WHERE id = ?",
                    (desc, row["id"])
                )
                fetched += 1
                log.info("  [%d] Fetched: %s @ %s (%d chars)",
                         row["id"], row["title"][:40], row["company"][:20], len(desc))
            else:
                log.info("  [%d] No description found: %s", row["id"], row["title"][:40])

            time.sleep(0.5)  # Rate limit

        conn.commit()
        log.info("Fetched %d/%d descriptions (tokens: %d)", fetched, len(missing), total_tokens)

    # Step 4: Score jobs
    if args.rescore:
        where = "WHERE description IS NOT NULL AND length(description) > 100"
    else:
        where = "WHERE description IS NOT NULL AND length(description) > 100 AND llm_score IS NULL"

    if args.min_keyword_score > 0:
        where += f" AND (score >= {args.min_keyword_score} OR score IS NULL)"

    query = f"""
        SELECT id, title, company, location, description, url, source, score
        FROM jobs {where}
        ORDER BY COALESCE(score, 0) DESC
    """
    if args.limit > 0:
        query += f" LIMIT {args.limit}"

    rows = conn.execute(query).fetchall()
    log.info("Scoring %d jobs...", len(rows))

    scored = 0
    failed = 0
    total_input_tokens = 0
    total_output_tokens = 0
    score_dist = {"90+": 0, "70-89": 0, "50-69": 0, "30-49": 0, "<30": 0}

    for i, row in enumerate(rows):
        result, usage = score_single_job(
            api_key,
            candidate_profile,
            row["title"], row["company"], row["location"],
            row["description"], row["url"], row["source"],
        )

        total_input_tokens += usage.get("input_tokens", 0)
        total_output_tokens += usage.get("output_tokens", 0)

        if result is None:
            failed += 1
            log.warning("  [%d] FAILED: %s", row["id"], row["title"][:50])
            continue

        ws = result.get("weighted_score", 0)

        if not args.dry_run:
            conn.execute("""
                UPDATE jobs SET
                    llm_score = ?,
                    llm_reasoning = ?,
                    llm_match_reason = ?,
                    llm_scores_json = ?,
                    llm_scored_at = ?
                WHERE id = ?
            """, (
                ws,
                result.get("reasoning", ""),
                result.get("top_match_reason", ""),
                json.dumps(result),
                datetime.utcnow().isoformat(),
                row["id"],
            ))

            if (i + 1) % 10 == 0:
                conn.commit()

        scored += 1

        # Distribution
        if ws >= 90:
            score_dist["90+"] += 1
        elif ws >= 70:
            score_dist["70-89"] += 1
        elif ws >= 50:
            score_dist["50-69"] += 1
        elif ws >= 30:
            score_dist["30-49"] += 1
        else:
            score_dist["<30"] += 1

        log.info(
            "  [%d/%d] %3d | %s @ %s — %s",
            i + 1, len(rows), ws,
            row["title"][:40], row["company"][:20],
            result.get("top_match_reason", "")[:50],
        )

        time.sleep(0.3)  # Rate limit

    conn.commit()

    # Cost estimate
    input_cost = total_input_tokens * 0.80 / 1_000_000
    output_cost = total_output_tokens * 4.0 / 1_000_000
    total_cost = input_cost + output_cost

    log.info("\n=== RESULTS ===")
    log.info("Scored: %d | Failed: %d", scored, failed)
    log.info("Score distribution:")
    for bracket, count in score_dist.items():
        log.info("  %5s: %d", bracket, count)
    log.info("Tokens: %d input, %d output", total_input_tokens, total_output_tokens)
    log.info("Estimated cost: $%.4f (in: $%.4f, out: $%.4f)", total_cost, input_cost, output_cost)

    # Show top LLM-scored jobs
    top = conn.execute("""
        SELECT title, company, location, llm_score, llm_match_reason, source
        FROM jobs WHERE llm_score IS NOT NULL
        ORDER BY llm_score DESC LIMIT 20
    """).fetchall()

    log.info("\n=== TOP 20 BY LLM SCORE ===")
    for r in top:
        log.info(
            "  %3d | %s | %s @ %s — %s",
            r["llm_score"], r["source"][:6],
            r["title"][:45], r["company"][:25],
            (r["llm_match_reason"] or "")[:40],
        )

    conn.close()
    log.info("Done.")


if __name__ == "__main__":
    main()
