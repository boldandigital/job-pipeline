#!/usr/bin/env python3
"""
Career page discovery -- finds direct career page URLs for jobs.

Three-layer strategy (no search engine dependency):
  1. JobSpy DB cross-reference -- uses job_url_direct from scraper data
  2. StepStone redirect -- extracts external apply link from StepStone pages
  3. Direct website probing -- guesses company domain, checks /careers /jobs /karriere

Wired into batch_pipeline.py (ADOPT-14): runs after scoring, before doc
generation. The careers.career_url + careers.ats_type columns feed ADOPT-12
(CUA submission).

Safety:
  - Respects robots.txt per company domain (urllib.robotparser)
  - Skips login-walled pages ("Sign in", "Login required", etc.)
  - Rate-limited: 1 request/second per domain with jitter
  - Caches results 30 days (in-memory) so re-probing is rare
  - Idempotent: never overwrites a populated career_url

Usage:
    from src.discovery.career_discovery import discover_career_for_jobs
    discover_career_for_jobs(conn, max_jobs=50, config=config_dict)

CLI:
    python -m src.discovery.career_discovery --db ./data/jobs.db --max 50
    python -m src.discovery.career_discovery --db ./data/jobs.db --max 5 --dry-run
"""

import argparse
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.robotparser
from typing import Optional, Tuple

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%%H:%M:%S",
)
log = logging.getLogger("career_discovery")

DB_PATH = os.getenv("DB_PATH", "./data/jobs.db")
JOBSPY_DB = os.getenv("JOBSPY_DB_PATH", "./data/jobspy.db")

# ──────────────────────────────────────────────────────────────────────
# ATS detection — 18 systems covered.
# ──────────────────────────────────────────────────────────────────────
# Two layers:
#   1. URL pattern match (fast, used everywhere)
#   2. HTML signature match (slower, used when probing the page itself)
ATS_PATTERNS = [
    (r"softgarden\.io|softgarden\.de", "softgarden"),
    (r"myworkday\.com|workday\.com|workdayjobs\.com", "workday"),
    (r"smartrecruiters\.com", "smartrecruiters"),
    (r"join\.com", "join"),
    (r"ashbyhq\.com", "ashby"),
    (r"boards\.greenhouse\.io|greenhouse\.io", "greenhouse"),
    (r"coveto\.de", "coveto"),
    (r"umantis\.com", "umantis"),
    (r"workable\.com", "workable"),
    (r"lever\.co", "lever"),
    (r"recruitee\.com", "recruitee"),
    (r"personio\.de|personio\.com", "personio"),
    (r"successfactors\.com|successfactors\.eu|sap\.com/sapbydesign", "sap_sf"),
    (r"taleo\.net", "taleo"),
    (r"icims\.com", "icims"),
    (r"breezy\.hr", "breezy"),
    (r"bamboohr\.com", "bamboohr"),
    (r"jobvite\.com", "jobvite"),
]

# HTML signature fallbacks — used when the URL doesn't match but the page
# embeds the ATS iframe / script. Matches common class names, script hosts,
# and meta-generator tags.
ATS_HTML_SIGNATURES = [
    (r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"'][^\"']*workday", "workday"),
    (r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"'][^\"']*greenhouse", "greenhouse"),
    (r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"'][^\"']*lever", "lever"),
    (r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"'][^\"']*ashby", "ashby"),
    (r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"'][^\"']*smartrecruiters", "smartrecruiters"),
    (r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"'][^\"']*workable", "workable"),
    (r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"'][^\"']*personio", "personio"),
    (r"<script[^>]+src=[\"'][^\"']*boards\.greenhouse\.io", "greenhouse"),
    (r"<script[^>]+src=[\"'][^\"']*jobs\.lever\.co", "lever"),
    (r"<script[^>]+src=[\"'][^\"']*jobs\.ashbyhq\.com", "ashby"),
    (r"<iframe[^>]+src=[\"'][^\"']*myworkdayjobs\.com|workdayjobs\.com", "workday"),
    (r"<iframe[^>]+src=[\"'][^\"']*jobs\.lever\.co", "lever"),
    (r"<iframe[^>]+src=[\"'][^\"']*jobs\.ashbyhq\.com", "ashby"),
    (r"<iframe[^>]+src=[\"'][^\"']*boards\.greenhouse\.io", "greenhouse"),
    (r"<iframe[^>]+src=[\"'][^\"']*smartrecruiters\.com", "smartrecruiters"),
    (r"<iframe[^>]+src=[\"'][^\"']*bamboohr\.com", "bamboohr"),
    (r"<iframe[^>]+src=[\"'][^\"']*workable\.com", "workable"),
    (r"<iframe[^>]+src=[\"'][^\"']*personio\.com|personio\.de", "personio"),
    (r"<iframe[^>]+src=[\"'][^\"']*icims\.com", "icims"),
    (r"<iframe[^>]+src=[\"'][^\"']*jobvite\.com", "jobvite"),
    (r"<iframe[^>]+src=[\"'][^\"']*taleo\.net", "taleo"),
    (r"<iframe[^>]+src=[\"'][^\"']*recruitee\.com", "recruitee"),
    (r"<iframe[^>]+src=[\"'][^\"']*softgarden\.de|softgarden\.io", "softgarden"),
    (r"<iframe[^>]+src=[\"'][^\"']*join\.com", "join"),
    (r"<iframe[^>]+src=[\"'][^\"']*breezy\.hr", "breezy"),
    (r"<iframe[^>]+src=[\"'][^\"']*successfactors\.com|successfactors\.eu", "sap_sf"),
    (r"<iframe[^>]+src=[\"'][^\"']*umantis\.com", "umantis"),
    (r"<iframe[^>]+src=[\"'][^\"']*coveto\.de", "coveto"),
    (r"id=[\"']gh_jk[\"']|class=[\"'][^\"']*greenhouse[^\"']*", "greenhouse"),
    (r"id=[\"']lever-jobs[\"']|class=[\"'][^\"']*lever-jobs[^\"']*", "lever"),
    (r"id=[\"']ashby-jobs[\"']|class=[\"'][^\"']*ashby[^\"']*", "ashby"),
]

PORTAL_DOMAINS = {
    "indeed.com", "indeed.de", "de.indeed.com",
    "linkedin.com", "www.linkedin.com",
    "stepstone.de", "www.stepstone.de",
    "glassdoor.de", "glassdoor.com", "www.glassdoor.de", "www.glassdoor.com",
    "xing.com", "www.xing.com",
    "monster.de", "www.monster.de",
    "kimeta.de", "www.kimeta.de",
    "stellenanzeigen.de", "www.stellenanzeigen.de",
    "jobware.de", "www.jobware.de",
    "absolventa.de", "www.absolventa.de",
    "equest.com", "ars2.equest.com",
    "gohiring.com", "t.gohiring.com",
    "jobspreader.com", "www.jobspreader.com",
}

CAREER_PATHS = [
    "/karriere", "/careers", "/jobs", "/career",
    "/stellenangebote", "/en/careers", "/de/karriere",
    "/work-with-us", "/join-us", "/vacancies", "/job-openings",
    "/offene-stellen", "/stellen", "/positionen", "/open-positions",
]

# Login-wall markers — pages that load but require auth to see jobs.
# Per the constraint: "if a company requires login to see jobs, skip and log."
LOGIN_WALL_MARKERS = [
    r"<title>[^<]*sign\s*in[^<]*</title>",
    r"<title>[^<]*log\s*in[^<]*</title>",
    r"<title>[^<]*anmelden[^<]*</title>",
    r"<title>[^<]*login[^<]*</title>",
    r"please\s+(sign|log)\s+in",
    r"bitte\s+anmelden",
    r"authentication\s+required",
    r"single\s*sign[\s-]?on",
    r"<form[^>]+action=[\"'][^\"']*sso[^\"']*",
    r"<form[^>]+action=[\"'][^\"']*login[^\"']*",
    r"<form[^>]+action=[\"'][^\"']*authenticate[^\"']*",
]

CACHE_TTL_SECONDS = 30 * 24 * 3600  # 30 days

# Module-level cache: domain → (url, ats, ts)
# Per company (not per job) so probing 5 jobs at the same company shares the result.
_cache: dict = {}


def detect_ats(url: str, html: Optional[str] = None) -> Optional[str]:
    """
    Detect ATS type from URL, falling back to HTML signature if URL is generic.
    Returns the ATS name (workday, greenhouse, lever, ...) or None.

    `html` is the page HTML when available; pass it to enable signature matching.
    """
    if not url:
        return None
    # 1. URL pattern match (fast path)
    for pattern, ats_name in ATS_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return ats_name
    # 2. HTML signature match (when page is loaded)
    if html:
        for pattern, ats_name in ATS_HTML_SIGNATURES:
            if re.search(pattern, html, re.IGNORECASE):
                return ats_name
    return None


def is_portal_url(url: str) -> bool:
    """Check if URL is a job portal (not a direct career page)."""
    if not url:
        return True
    try:
        domain = urllib.parse.urlparse(url).netloc.lower()
        return any(d in domain for d in PORTAL_DOMAINS)
    except Exception:
        return True


def is_login_wall(html: str) -> bool:
    """Detect if a page is a login wall (no jobs visible without auth)."""
    if not html:
        return False
    html_lower = html[:8192].lower()  # only check first 8 KB — login markers are early
    for marker in LOGIN_WALL_MARKERS:
        if re.search(marker, html_lower, re.IGNORECASE):
            return True
    return False


def robots_allowed(url: str, user_agent: str = "*", timeout: int = 5) -> bool:
    """
    Check robots.txt for the URL. Returns True if allowed.

    Fail-open policy:
      - If robots.txt can't be fetched (timeout / 5xx / connection error): ALLOW.
      - If robots.txt returns HTML (e.g. parked-domain marketing page on sedo.com):
        ALLOW — that's not a real robots.txt.
      - If robots.txt returns a valid Disallow rule: DENY only that path.
      - If robots.txt returns "Disallow: /" (full deny): DENY.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        resp = requests.get(robots_url, timeout=timeout,
                            headers={"User-Agent": "Mozilla/5.0"},
                            allow_redirects=True)
        if resp.status_code >= 400:
            log.debug("robots.txt %s returned %d — allowing", robots_url, resp.status_code)
            return True
        # Real robots.txt is text/plain or empty body. HTML responses = parked domain
        # or marketing redirect — not a real robots.txt, allow probing.
        content_type = (resp.headers.get("content-type", "") or "").lower()
        body = resp.text.strip()
        if not body or "text/html" in content_type or body.startswith("<"):
            log.debug("robots.txt %s returned HTML/empty (parked domain?) — allowing", robots_url)
            return True
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(body.splitlines())
        return rp.can_fetch(user_agent, url)
    except Exception as e:
        log.debug("robots.txt check failed for %s: %s (defaulting to allow)", url, e)
        return True


# Per-domain last-request timestamp — enforces 1 req/sec.
_last_request_per_domain: dict = {}

# Per-domain robots.txt cache (the rule set, not the URL).
# Many URLs on the same domain share one robots.txt fetch.
_robots_cache: dict = {}


def _robots_allowed_cached(url: str, user_agent: str = "*", timeout: int = 5) -> bool:
    """robots_allowed + in-memory cache so we fetch /robots.txt once per domain."""
    try:
        domain = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return True
    if domain in _robots_cache:
        return _robots_cache[domain]
    allowed = robots_allowed(url, user_agent=user_agent, timeout=timeout)
    _robots_cache[domain] = allowed
    return allowed


def _polite_request(url: str, method: str = "GET", timeout: int = 8,
                    probe_only: bool = False) -> Optional[requests.Response]:
    """
    HTTP request with 1 req/sec rate limit per domain + robots.txt respect.

    probe_only=True uses HEAD when possible to save bandwidth; falls back to GET
    for servers that reject HEAD (most ATS portals do).

    Rate limit: 1 req/sec per domain — matches the spec ("be polite").
    """
    try:
        domain = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return None

    # 1. Rate limit: 1 req/sec per domain (per the spec).
    last = _last_request_per_domain.get(domain, 0)
    elapsed = time.time() - last
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed + random.uniform(0.0, 0.2))
    _last_request_per_domain[domain] = time.time()

    # 2. robots.txt gate (cached per-domain so we only fetch it once).
    if not _robots_allowed_cached(url):
        log.info("robots.txt disallows probing %s -- skipping", url)
        return None

    # 3. Actual request.
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/124.0 Safari/537.36"}
    try:
        if probe_only and method == "HEAD":
            resp = requests.head(url, allow_redirects=True, timeout=timeout, headers=headers)
            # Some servers return 405 on HEAD; retry as GET.
            if resp.status_code == 405:
                resp = requests.get(url, allow_redirects=True, timeout=timeout, headers=headers,
                                    stream=True)
                resp.close()
        else:
            resp = requests.get(url, allow_redirects=True, timeout=timeout, headers=headers)
        return resp
    except Exception as e:
        log.debug("request failed for %s: %s", url, e)
        return None


def _cache_get(company: str) -> Optional[Tuple[str, Optional[str]]]:
    """Return (url, ats) for a previously-probed company, if fresh."""
    entry = _cache.get(company.lower().strip())
    if not entry:
        return None
    url, ats, ts = entry
    if time.time() - ts > CACHE_TTL_SECONDS:
        _cache.pop(company.lower().strip(), None)
        return None
    return url, ats


def _cache_put(company: str, url: str, ats: Optional[str]) -> None:
    """Store result in the in-memory cache for 30 days."""
    _cache[company.lower().strip()] = (url, ats, time.time())


def layer1_jobspy_xref(conn, jobspy_db: str) -> int:
    """Layer 1: Cross-reference with JobSpy DB for job_url_direct."""
    if not os.path.exists(jobspy_db):
        log.info("JobSpy DB not found, skipping Layer 1")
        return 0

    try:
        jobspy = sqlite3.connect(jobspy_db)
        jobspy.row_factory = sqlite3.Row
        # Tolerate empty/schemaless JobSpy DBs (fresh containers, partial scrapes).
        try:
            rows = conn.execute(
                "SELECT id, title, company FROM jobs "
                "WHERE (career_url IS NULL OR career_url = '') "
                "AND status NOT IN ('applied', 'filtered_out')"
            ).fetchall()
        except sqlite3.OperationalError:
            log.info("Main DB jobs table not ready, skipping Layer 1")
            jobspy.close()
            return 0
    except sqlite3.OperationalError:
        log.info("JobSpy DB has no jobs table, skipping Layer 1")
        return 0

    updated = 0
    for row in rows:
        try:
            match = jobspy.execute(
                "SELECT job_url_direct, company_url FROM jobs WHERE title = ? AND company_name = ?",
                (row[1], row[2]),
            ).fetchone()
        except sqlite3.OperationalError:
            log.info("JobSpy DB has no jobs table on lookup, skipping Layer 1")
            break

        if match:
            direct_url = match["job_url_direct"] or match["company_url"] or ""
            if direct_url and not is_portal_url(direct_url):
                ats = detect_ats(direct_url)
                conn.execute(
                    "UPDATE jobs SET career_url = ?, ats_type = ? WHERE id = ? "
                    "AND (career_url IS NULL OR career_url = '')",  # idempotent guard
                    (direct_url, ats, row[0]),
                )
                if conn.execute("SELECT changes()", ).fetchone()[0]:
                    updated += 1
                _cache_put(row[2], direct_url, ats)

    conn.commit()
    jobspy.close()
    log.info("Layer 1 (JobSpy xref): updated %d career URLs", updated)
    return updated


def layer2_stepstone_redirect(conn, max_jobs: int = 50) -> int:
    """Layer 2: Follow StepStone apply redirects to find career pages."""
    rows = conn.execute(
        "SELECT id, url, company FROM jobs "
        "WHERE (career_url IS NULL OR career_url = '') "
        "AND source = 'stepstone' AND url IS NOT NULL "
        "LIMIT ?",
        (max_jobs,),
    ).fetchall()

    updated = 0
    for row in rows:
        try:
            resp = _polite_request(row[1], method="GET", timeout=15)
            if not resp:
                continue
            if is_login_wall(resp.text):
                log.info("StepStone redirect hit login wall for job %d -- skipping", row[0])
                continue
            final_url = resp.url
            if not is_portal_url(final_url):
                ats = detect_ats(final_url, resp.text)
                conn.execute(
                    "UPDATE jobs SET career_url = ?, ats_type = ? WHERE id = ? "
                    "AND (career_url IS NULL OR career_url = '')",
                    (final_url, ats, row[0]),
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    updated += 1
                _cache_put(row[2], final_url, ats)
        except Exception as e:
            log.debug("StepStone redirect failed for job %d: %s", row[0], e)

    conn.commit()
    log.info("Layer 2 (StepStone redirect): updated %d career URLs", updated)
    return updated


def layer3_website_probe(conn, max_jobs: int = 50) -> int:
    """Layer 3: Probe company websites for career pages."""
    rows = conn.execute(
        "SELECT id, company FROM jobs "
        "WHERE (career_url IS NULL OR career_url = '') "
        "AND company IS NOT NULL AND company != '' "
        "LIMIT ?",
        (max_jobs,),
    ).fetchall()

    updated = 0
    for row in rows:
        company = (row[1] or "").lower().strip()
        if not company:
            continue

        # Cache check first.
        cached = _cache_get(company)
        if cached:
            url, ats = cached
            conn.execute(
                "UPDATE jobs SET career_url = ?, ats_type = ? WHERE id = ? "
                "AND (career_url IS NULL OR career_url = '')",
                (url, ats, row[0]),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                updated += 1
            continue

        slug = re.sub(r"[^a-z0-9]", "", company.replace(" ", ""))
        if not slug:
            continue
        # Try .de before .com for DACH-heavy role mix (Lars's geography).
        # We probe 3 high-probability paths first; if all fail on .de, we try .com.
        # Going wider is wasteful — most companies use /careers or /jobs.
        domains = [f"https://{slug}.de", f"https://{slug}.com"]
        # Prioritize: /careers (most common), /jobs (US-style), /karriere (DACH)
        probe_paths = ["/careers", "/jobs", "/karriere"]

        found = None
        for domain in domains:
            for path in probe_paths:
                url = domain + path
                resp = _polite_request(url, method="HEAD", timeout=4, probe_only=True)
                if not resp:
                    continue
                if resp.status_code < 400:
                    found_url = resp.url
                    ats_from_url = detect_ats(found_url)
                    # Only do the GET upgrade when the URL itself doesn't reveal
                    # the ATS — saves ~1 round-trip on the common case.
                    if ats_from_url:
                        found = (found_url, ats_from_url)
                        break
                    get_resp = _polite_request(found_url, method="GET", timeout=5)
                    html = get_resp.text if get_resp else ""
                    if is_login_wall(html):
                        log.info("Login wall at %s -- skipping", found_url)
                        continue
                    found = (found_url, detect_ats(found_url, html))
                    break
                # Fail fast on 404 — domain exists but no careers path. No point
                # probing other paths on this domain (they'll all 404).
                if resp.status_code == 404:
                    break
            if found:
                break

        if found:
            found_url, ats = found
            conn.execute(
                "UPDATE jobs SET career_url = ?, ats_type = ? WHERE id = ? "
                "AND (career_url IS NULL OR career_url = '')",
                (found_url, ats, row[0]),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                updated += 1
            _cache_put(company, found_url, ats)

    conn.commit()
    log.info("Layer 3 (website probe): updated %d career URLs", updated)
    return updated


def discover_career_for_jobs(conn, max_jobs: int = 50,
                             config: Optional[dict] = None,
                             layers: Tuple[int, ...] = (1, 2, 3)) -> dict:
    """
    Run all three layers and return a stats dict.

    `config` keys (all optional):
      jobspy_db       path to JobSpy SQLite (default: ./data/jobspy.db)
      enabled_layers  tuple of layer numbers to run (default: all three)

    Used by batch_pipeline.py after scoring, before doc generation.
    """
    config = config or {}
    jobspy_db = config.get("jobspy_db", JOBSPY_DB)
    enabled = config.get("enabled_layers", layers)

    counts = {"layer1": 0, "layer2": 0, "layer3": 0, "total": 0}
    if 1 in enabled:
        counts["layer1"] = layer1_jobspy_xref(conn, jobspy_db)
    if 2 in enabled:
        counts["layer2"] = layer2_stepstone_redirect(conn, max_jobs)
    if 3 in enabled:
        counts["layer3"] = layer3_website_probe(conn, max_jobs)

    counts["total"] = counts["layer1"] + counts["layer2"] + counts["layer3"]
    log.info("Career discovery total: %d URLs (L1=%d L2=%d L3=%d)",
             counts["total"], counts["layer1"], counts["layer2"], counts["layer3"])
    return counts


def main():
    parser = argparse.ArgumentParser(description="Career page URL discovery")
    parser.add_argument("--db", default=DB_PATH, help="Path to main database")
    parser.add_argument("--jobspy-db", default=JOBSPY_DB, help="Path to JobSpy database")
    parser.add_argument("--max", type=int, default=50, help="Max jobs to process per layer")
    parser.add_argument("--dry-run", action="store_true", help="Print results without saving")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    if args.dry_run:
        # Show what would be probed without touching the DB.
        rows = conn.execute(
            "SELECT id, title, company, source, career_url "
            "FROM jobs WHERE (career_url IS NULL OR career_url = '') "
            "AND status NOT IN ('applied', 'filtered_out')"
        ).fetchall()
        print(f"[dry-run] {len(rows)} jobs without career_url:")
        for r in rows:
            print(f"  id={r[0]:3d}  {r[1][:40]:40s} | {r[2][:25]:25s} ({r[3]})")
        conn.close()
        return

    counts = discover_career_for_jobs(conn, max_jobs=args.max)

    # Show stats
    stats = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN career_url IS NOT NULL AND career_url != '' THEN 1 ELSE 0 END) as with_career_url,
            SUM(CASE WHEN ats_type IS NOT NULL AND ats_type != '' THEN 1 ELSE 0 END) as with_ats
        FROM jobs
    """).fetchone()

    log.info("DB stats: %d total jobs, %d with career URL, %d with ATS detected",
             stats[0], stats[1], stats[2])

    conn.close()


if __name__ == "__main__":
    main()
