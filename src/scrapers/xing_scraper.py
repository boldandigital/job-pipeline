#!/usr/bin/env python3
"""
XING job scraper using Patchright (browser automation).

XING is the DACH-focused professional network — Germany/Austria/Switzerland.
It is the 4th job-platform target in the dome317 adoption scope (alongside
LinkedIn + StepStone + official career pages). DACH-heavy: ~80% of listings
are DE/AT/CH-based, and many senior/leadership roles in the German market
appear on XING before LinkedIn.

Public search URL: https://www.xing.com/jobs/search?keywords=...&location=...
Pagination: ?page=N (works from page 1).

Anti-bot behaviour observed:
- Cookie banner (DSGVO consent) on first hit
- Captcha triggered under heavy use (don't solve — just bail)
- Login wall after ~5 pages without a session (skip silently if seen)

Usage:
    python -m src.scrapers.xing_scraper --queries "founder digital" --location "Deutschland" --limit 25
    python -m src.scrapers.xing_scraper --queries-file ./data/queries.txt --limit 50
    python -m src.scrapers.xing_scraper --no-fuzzy-dedup
"""

import argparse
import logging
import os
import random
import re
import sqlite3
import sys
import time
from difflib import SequenceMatcher
from urllib.parse import quote_plus, urljoin, urlparse

from patchright.sync_api import sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("xing_scraper")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = os.getenv("DB_PATH", "./data/jobs.db")

# Geolocation: center of Germany. XING uses this to localise listings + currency.
GEO_LATITUDE = float(os.getenv("GEO_LATITUDE", "51.1657"))
GEO_LONGITUDE = float(os.getenv("GEO_LONGITUDE", "10.4515"))

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:123.0) Gecko/20100101 Firefox/123.0",
]

XING_BASE = "https://www.xing.com"

# Canonical jobs table schema — must match stepstone_scraper / arbeitsagentur_scraper /
# src/db/__init__.py:init_db(). Career_url and description were added in ADOPT-6.
DB_SCHEMA = """
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
    status TEXT DEFAULT 'new',
    cv_path TEXT,
    cover_letter_path TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(title, company)
);
"""

# Signals that XING has blocked us — bail rather than burn the session.
CAPTCHA_SIGNALS = [
    "captcha",
    "please confirm you are a human",
    "bitte bestätige",
    "security check",
    "are you a robot",
    "access denied",
]
LOGIN_WALL_SIGNALS = [
    "anmelden, um jobs zu sehen",
    "log in to view",
    "sign in to continue",
    "registration required",
]

# ---------------------------------------------------------------------------
# Helpers — same shape as stepstone_scraper.py for consistency
# ---------------------------------------------------------------------------


def random_delay(low: float = 2.0, high: float = 3.5) -> None:
    """Sleep a random duration. XING rate-limits aggressively — keep delays polite."""
    delay = random.uniform(low, high)
    time.sleep(delay)


def pick_user_agent() -> str:
    return random.choice(USER_AGENTS)


def fuzzy_match(a: str, b: str, threshold: float = 0.85) -> bool:
    if not a or not b:
        return False
    a_clean = re.sub(r"\s+", " ", a.strip().lower())
    b_clean = re.sub(r"\s+", " ", b.strip().lower())
    return SequenceMatcher(None, a_clean, b_clean).ratio() >= threshold


def normalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        return clean.rstrip("/")
    except Exception:
        return url


def _block_check(page) -> str | None:
    """Return 'captcha', 'login', or None if the current page is OK."""
    try:
        body = page.evaluate("() => document.body && document.body.innerText")
    except Exception:
        return None
    if not body:
        return None
    body_lc = body.lower()
    for sig in CAPTCHA_SIGNALS:
        if sig in body_lc:
            return "captcha"
    for sig in LOGIN_WALL_SIGNALS:
        if sig in body_lc:
            return "login"
    return None


def dismiss_cookie_banner(page, timeout: int = 4000) -> None:
    """Best-effort DSGVO cookie dismissal. XING uses a consent layer — try several."""
    selectors = [
        # Common consent frameworks
        "#onetrust-accept-btn-handler",
        'button[id="onetrust-accept-btn-handler"]',
        ".onetrust-close-btn-handler",
        # XING-specific (best-effort, names may rotate)
        'button[data-testid="cookie-accept-all"]',
        'button[data-testid="cookie-consent-accept"]',
        'button[aria-label*="akzeptieren" i]',
        'button[aria-label*="accept" i]',
        # German text fallbacks
        "button:has-text('Alle akzeptieren')",
        "button:has-text('Akzeptieren')",
        "button:has-text('Zustimmen')",
        "button:has-text('Einverstanden')",
        # English fallback
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
        "button:has-text('Agree')",
        # Generic
        '[data-action="accept"]',
        ".js-accept-cookies",
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=timeout):
                btn.click()
                log.debug("Dismissed cookie banner with: %s", sel)
                time.sleep(0.5)
                return
        except Exception:
            continue
    log.debug("No cookie banner found or could not dismiss")


def create_browser_context(playwright, headless: bool = True):
    """Build a Patchright context with anti-detection (mirrors stepstone_scraper)."""
    browser = playwright.chromium.launch(
        headless=headless,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    )
    context = browser.new_context(
        user_agent=pick_user_agent(),
        viewport={"width": 1920, "height": 1080},
        locale="de-DE",
        timezone_id="Europe/Berlin",
        geolocation={"latitude": GEO_LATITUDE, "longitude": GEO_LONGITUDE},
        permissions=["geolocation"],
        java_script_enabled=True,
    )
    # Stealth: hide the webdriver flag + spoof languages/plugins.
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['de-DE', 'de', 'en-US', 'en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    """)
    return browser, context


# ---------------------------------------------------------------------------
# XING scraper
# ---------------------------------------------------------------------------


def _build_xing_url(query: str, location: str = "", page: int = 1) -> str:
    """Build a XING /jobs/search URL. ?page=N works from page 1."""
    params = [f"keywords={quote_plus(query)}"]
    if location:
        params.append(f"location={quote_plus(location)}")
    if page > 1:
        params.append(f"page={page}")
    return f"{XING_BASE}/jobs/search?{'&'.join(params)}"


def _extract_xing_jobs(page) -> list[dict]:
    """
    Pull job cards out of the current XING search results page.

    XING uses CSS-module hashes that rotate on every deploy, so we anchor on
    the stable data-testid attributes:
        [data-testid="job-search-result"]   — each card
        [data-testid="job-teaser-list-title"] — title headline
        a[href*="/jobs/"]                    — job-detail link (relative href)
        .multi-location-display-styles__Container  — the <p> inside holds the city
    The CSS-module hashes on the title/company classes are best-effort;
    data-testid is the stable contract.

    Location parsing: the multi-location container is a <p data-xds="BodyCopy">
    like "Dresden<b>+ 0 weitere</b>". We read the first text node (city),
    strip the "+ N weitere" suffix, and ignore cards where the location
    looks like a salary/employment marker (digits, €, Vollzeit, etc.).
    """
    return page.evaluate(
        """
        () => {
            const results = [];
            const cards = document.querySelectorAll('[data-testid="job-search-result"]');
            const NOISE_RE = /^\\s*(€|\\d|vollzeit|teilzeit|easy apply|dringer|gesucht|studenten|praktikant)/i;
            for (const card of cards) {
                const titleEl = card.querySelector('[data-testid="job-teaser-list-title"]');
                if (!titleEl) continue;
                const title = (titleEl.textContent || '').trim();

                let url = '';
                const linkEl = card.querySelector('a[href*="/jobs/"]');
                if (linkEl) url = linkEl.getAttribute('href') || '';

                let company = '';
                const companySelectors = [
                    '[class*="job-teaser-list-item-styles__Company"]',
                    '[class*="Company"]',
                    'p[data-xds="BodyCopy"]'
                ];
                for (const sel of companySelectors) {
                    const el = card.querySelector(sel);
                    if (el && el !== titleEl) {
                        company = (el.textContent || '').trim();
                        if (company) break;
                    }
                }

                // Location — first <p> inside the multi-location container.
                // Strip the trailing "+ N weitere" overflow label.
                let location = '';
                const locContainer = card.querySelector(
                    '[class*="multi-location-display-styles__Container"]'
                );
                if (locContainer) {
                    const p = locContainer.querySelector('p');
                    if (p && p.firstChild) {
                        location = (p.firstChild.textContent || '').trim();
                    } else if (p) {
                        location = (p.textContent || '').trim().split('+')[0].trim();
                    }
                }
                if (NOISE_RE.test(location)) location = '';

                if (title && company) {
                    results.push({ title, company, location, url });
                }
            }
            return results;
        }
        """
    )


def _scrape_query(
    context,
    query: str,
    location: str,
    max_pages: int,
    limit: int,
) -> list[dict]:
    """Run one query across up to max_pages of XING results."""
    page = context.new_page()
    jobs: list[dict] = []
    consecutive_empty = 0
    consecutive_blocks = 0
    consecutive_429 = 0

    for page_num in range(1, max_pages + 1):
        url = _build_xing_url(query, location, page_num)
        log.info("  Page %d: %s", page_num, url)
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # XING sometimes serves 429 before the JS ever runs — back off hard.
            if response and response.status == 429:
                consecutive_429 += 1
                log.warning("  HTTP 429 (rate-limited). Backing off 60s (attempt %d/3).",
                            consecutive_429)
                if consecutive_429 >= 3:
                    log.warning("  Three rate-limits in a row, giving up on '%s'", query)
                    break
                time.sleep(60)
                continue
            consecutive_429 = 0

            random_delay(2.0, 3.5)

            # First-page housekeeping: dismiss DSGVO cookies
            if page_num == 1:
                dismiss_cookie_banner(page)
                random_delay(1.0, 2.0)

            # Captcha / login-wall guard
            block = _block_check(page)
            if block == "captcha":
                log.warning("  Captcha triggered on page %d — skipping remaining pages", page_num)
                break
            if block == "login":
                log.info("  Login wall hit (page %d) — XING ~5-page limit, stopping", page_num)
                break

            # Wait for at least one card to render
            try:
                page.wait_for_selector(
                    '[data-testid="job-search-result"]', timeout=12000
                )
            except Exception:
                log.warning("  No job cards on page %d (empty or last page)", page_num)
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    log.info("  Two empty pages in a row, stopping")
                    break
                continue

            # Trigger lazy loading
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            random_delay(1.0, 2.0)

            extracted = _extract_xing_jobs(page)
            if not extracted:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break
                continue
            consecutive_empty = 0
            consecutive_blocks = 0

            # Stamp source + query, normalise URL to absolute
            for job in extracted:
                job["source"] = "xing"
                job["search_query"] = query
                if job.get("url") and job["url"].startswith("/"):
                    job["url"] = urljoin(XING_BASE, job["url"])

            jobs.extend(extracted)
            log.info("  Extracted %d jobs from page %d (running total: %d)",
                     len(extracted), page_num, len(jobs))

            # Hit the user's per-query cap early
            if limit and len(jobs) >= limit:
                log.info("  Hit limit (%d), stopping pagination", limit)
                break

            # Polite gap before the next page
            random_delay(2.5, 4.5)

        except Exception as e:
            log.error("  Error on page %d: %s", page_num, e)
            consecutive_blocks += 1
            if consecutive_blocks >= 2:
                break
            continue

    page.close()
    return jobs


def scrape_xing(
    context,
    queries: list[str],
    location: str = "Deutschland",
    max_pages: int = 5,
    limit: int = 25,
) -> list[dict]:
    """Top-level XING scrape: iterate queries, dedupe in-memory, return flat list."""
    all_jobs: list[dict] = []
    seen: set[tuple[str, str]] = set()  # in-run dedupe on (title, company)
    for query in queries:
        log.info("XING: '%s' (location=%s, max_pages=%d)", query, location or "any", max_pages)
        q_jobs = _scrape_query(context, query, location, max_pages, limit)
        for job in q_jobs:
            key = (job["title"].strip().lower(), job["company"].strip().lower())
            if key in seen:
                continue
            seen.add(key)
            all_jobs.append(job)
        log.info("XING: '%s' -> %d jobs (after in-run dedupe)", query,
                 sum(1 for j in all_jobs if j["search_query"] == query))
        # Be extra polite between queries
        random_delay(2.0, 4.0)
    log.info("XING total: %d jobs from %d queries", len(all_jobs), len(queries))
    return all_jobs


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def init_db(db_path: str) -> sqlite3.Connection:
    """Create the jobs table if it doesn't exist. Idempotent."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(DB_SCHEMA)
    return conn


def _is_fuzzy_duplicate(
    conn: sqlite3.Connection, title: str, company: str, threshold: float = 0.85
) -> bool:
    """Compare against the last 5,000 jobs (SequenceMatcher ratio)."""
    rows = conn.execute(
        "SELECT title, company FROM jobs ORDER BY id DESC LIMIT 5000"
    ).fetchall()
    for existing_title, existing_company in rows:
        if fuzzy_match(title, existing_title, threshold) and fuzzy_match(
            company, existing_company, threshold
        ):
            return True
    return False


def save_to_db(
    jobs: list[dict],
    db_path: "str | None" = None,
    conn: "sqlite3.Connection | None" = None,
    fuzzy_dedup: bool = True,
    fuzzy_threshold: float = 0.85,
) -> tuple[int, int, int]:
    """Insert scraped jobs into SQLite. Returns (inserted, exact_dupes, fuzzy_dupes)."""
    if db_path is None:
        db_path = DB_PATH
    own_conn = False
    if conn is None:
        conn = init_db(db_path)
        own_conn = True

    inserted = exact_dupes = fuzzy_dupes = 0
    for job in jobs:
        title = (job.get("title") or "").strip()
        company = (job.get("company") or "").strip()
        location = (job.get("location") or "").strip()
        url = normalize_url(job.get("url", ""))
        source = job.get("source", "xing")
        search_query = job.get("search_query", "")

        if not title or not company:
            continue

        if fuzzy_dedup and _is_fuzzy_duplicate(conn, title, company, fuzzy_threshold):
            fuzzy_dupes += 1
            log.debug("Fuzzy duplicate: %s @ %s", title, company)
            continue

        try:
            conn.execute(
                """INSERT OR IGNORE INTO jobs
                   (title, company, location, url, source, search_query)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (title, company, location, url, source, search_query),
            )
            if conn.execute("SELECT changes()").fetchone()[0] > 0:
                inserted += 1
            else:
                exact_dupes += 1
        except sqlite3.IntegrityError:
            exact_dupes += 1

    conn.commit()
    if own_conn:
        conn.close()
    log.info(
        "DB: %d inserted, %d exact dupes, %d fuzzy dupes (from %d total)",
        inserted, exact_dupes, fuzzy_dupes, len(jobs),
    )
    return inserted, exact_dupes, fuzzy_dupes


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _resolve_queries(args) -> list[str]:
    """Pull queries from CLI args or a one-per-line file. Falls back to a small default."""
    queries = list(args.queries or [])
    if args.queries_file:
        try:
            with open(args.queries_file, "r", encoding="utf-8") as fh:
                file_queries = [
                    line.strip()
                    for line in fh
                    if line.strip() and not line.startswith("#")
                ]
                queries.extend(file_queries)
        except FileNotFoundError:
            log.error("Queries file not found: %s", args.queries_file)
            sys.exit(1)
    if not queries:
        queries = [
            "founder digital",
            "cto startup",
            "head of digital",
            "managing director agency",
            "geschäftsführer digital",
        ]
        log.info("No queries specified, using defaults: %s", queries)
    return queries


def main() -> None:
    parser = argparse.ArgumentParser(
        description="XING (DACH) browser scraper for the job-pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --queries "founder digital" --location "Deutschland" --limit 25
  %(prog)s --queries-file ./data/queries.txt --limit 50 --max-pages 3
  %(prog)s --queries "cto startup" --location "Berlin" --no-fuzzy-dedup
        """,
    )
    parser.add_argument("--queries", nargs="+", default=None,
                        help="Search queries (space-separated).")
    parser.add_argument("--queries-file", default=None,
                        help="File with one query per line.")
    parser.add_argument("--location", default="Deutschland",
                        help='Location filter, e.g. "Deutschland", "Berlin", "Wien".')
    parser.add_argument("--limit", type=int, default=25,
                        help="Max jobs to keep per query (default: 25).")
    parser.add_argument("--max-pages", type=int, default=5,
                        help="Max result pages per query (default: 5 — XING login-wall beyond).")
    parser.add_argument("--db", default=os.getenv("DB_PATH", "./data/jobs.db"),
                        help="Path to SQLite database.")
    parser.add_argument("--no-fuzzy-dedup", action="store_true",
                        help="Disable fuzzy duplicate detection.")
    parser.add_argument("--fuzzy-threshold", type=float, default=0.85,
                        help="Fuzzy matching threshold (default: 0.85).")
    parser.add_argument("--headless", action="store_true", default=True,
                        help="Run browser in headless mode (default).")
    parser.add_argument("--no-headless", action="store_true",
                        help="Show browser window (for debugging).")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging.")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    headless = not args.no_headless
    queries = _resolve_queries(args)

    conn = init_db(args.db)
    all_jobs: list[dict] = []

    with sync_playwright() as pw:
        browser, context = create_browser_context(pw, headless=headless)
        try:
            all_jobs = scrape_xing(
                context,
                queries=queries,
                location=args.location,
                max_pages=args.max_pages,
                limit=args.limit,
            )
        finally:
            context.close()
            browser.close()

    if all_jobs:
        inserted, exact_dupes, fuzzy_dupes = save_to_db(
            all_jobs,
            conn=conn,
            fuzzy_dedup=not args.no_fuzzy_dedup,
            fuzzy_threshold=args.fuzzy_threshold,
        )
        log.info("DONE: %d new, %d exact dupes, %d fuzzy dupes",
                 inserted, exact_dupes, fuzzy_dupes)
    else:
        log.warning("No jobs scraped")

    # Summary log
    summary = sqlite3.connect(args.db)
    total = summary.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    xing_count = summary.execute(
        "SELECT COUNT(*) FROM jobs WHERE source='xing'"
    ).fetchone()[0]
    summary.close()
    log.info("Database: %d total jobs, %d from xing", total, xing_count)
    conn.close()


if __name__ == "__main__":
    main()