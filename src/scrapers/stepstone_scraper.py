#!/usr/bin/env python3
"""
StepStone & Google Jobs browser-based scraper using Patchright.

Scrapes job listings from StepStone.de and Google Jobs using a headless
Chromium browser. Includes cookie dismissal, pagination, fuzzy dedup,
and SQLite persistence.

Requires: patchright (pip install patchright)
          Then: python -m patchright install chromium

Usage:
    python -m src.scrapers.stepstone_scraper --source stepstone --queries "Data Analyst" "AI"
    python -m src.scrapers.stepstone_scraper --source google --queries "Business Analyst Hannover"
    python -m src.scrapers.stepstone_scraper --source both --db ./data/jobs.db
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
from datetime import datetime
from difflib import SequenceMatcher
from urllib.parse import quote_plus, urljoin, urlparse

from patchright.sync_api import sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("stepstone_scraper")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = os.getenv("DB_PATH", "./data/jobs.db")

# Geolocation for browser context — center of Germany by default
GEO_LATITUDE = float(os.getenv("GEO_LATITUDE", "51.1657"))
GEO_LONGITUDE = float(os.getenv("GEO_LONGITUDE", "10.4515"))

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:123.0) Gecko/20100101 Firefox/123.0",
]

STEPSTONE_BASE = "https://www.stepstone.de"
GOOGLE_JOBS_BASE = "https://www.google.com/search"

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

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def random_delay(low: float = 1.5, high: float = 4.0) -> None:
    """Sleep for a random duration to avoid detection."""
    delay = random.uniform(low, high)
    time.sleep(delay)


def pick_user_agent() -> str:
    """Pick a random user agent string."""
    return random.choice(USER_AGENTS)


def fuzzy_match(a: str, b: str, threshold: float = 0.85) -> bool:
    """Check if two strings are a fuzzy match above the given threshold."""
    if not a or not b:
        return False
    a_clean = re.sub(r"\s+", " ", a.strip().lower())
    b_clean = re.sub(r"\s+", " ", b.strip().lower())
    return SequenceMatcher(None, a_clean, b_clean).ratio() >= threshold


def normalize_url(url: str) -> str:
    """Normalize a URL by removing tracking parameters and fragments."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        # Keep scheme, netloc, and path — drop query params and fragment
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        # Remove trailing slashes for consistency
        return clean.rstrip("/")
    except Exception:
        return url


def dismiss_cookie_banner(page, timeout: int = 5000) -> None:
    """Try to dismiss common cookie consent banners."""
    selectors = [
        # StepStone-specific
        'button[data-testid="cookie-accept-all"]',
        'button[id="ccmgt_explicit_accept"]',
        # Generic patterns
        "button:has-text('Alle akzeptieren')",
        "button:has-text('Accept all')",
        "button:has-text('Akzeptieren')",
        "button:has-text('Accept')",
        "button:has-text('Zustimmen')",
        "button:has-text('Agree')",
        # Shadow DOM consent managers
        "#onetrust-accept-btn-handler",
        ".js-accept-cookies",
        '[data-action="accept"]',
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
    """Create a Patchright browser context with anti-detection settings."""
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

    # Stealth: override navigator.webdriver
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['de-DE', 'de', 'en-US', 'en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    """)

    return browser, context


# ---------------------------------------------------------------------------
# StepStone scraper
# ---------------------------------------------------------------------------


def _build_stepstone_url(query: str, location: str = "", page: int = 1, radius: int = 0) -> str:
    """Build a StepStone search URL."""
    params = [f"q={quote_plus(query)}"]
    if location:
        params.append(f"li={quote_plus(location)}")
    if radius:
        params.append(f"radius={radius}")
    if page > 1:
        params.append(f"of={25 * (page - 1)}")
    return f"{STEPSTONE_BASE}/jobs?{'&'.join(params)}"


def _extract_stepstone_jobs(page) -> list[dict]:
    """Extract job listings from the current StepStone page using JS evaluate."""
    jobs = page.evaluate("""
        () => {
            const results = [];
            // Select article elements that are actual job cards (not filter panels)
            const articles = document.querySelectorAll('article:not([data-facets-heading])');
            for (const art of articles) {
                // Skip if it has facets heading (filter panel)
                if (art.querySelector('[data-facets-heading]')) continue;

                const titleEl = art.querySelector('a[data-at="job-item-title"]');
                if (!titleEl) continue;

                const title = (titleEl.textContent || '').trim();
                const url = titleEl.href || '';

                // Company name — try data attribute first, then image alt
                let company = '';
                const compEl = art.querySelector('[data-at="job-item-company-name"]');
                if (compEl) {
                    company = (compEl.textContent || '').trim();
                } else {
                    const imgEl = art.querySelector('img[alt]');
                    if (imgEl) {
                        company = (imgEl.alt || '').trim();
                    }
                }

                // Location
                let location = '';
                const locEl = art.querySelector('[data-at="job-item-location"]');
                if (locEl) {
                    location = (locEl.textContent || '').trim();
                }

                if (title && company) {
                    results.push({ title, company, location, url });
                }
            }
            return results;
        }
    """)
    return jobs


def scrape_stepstone(
    context,
    queries: list[str],
    location: str = "",
    max_pages: int = 5,
    radius: int = 0,
) -> list[dict]:
    """Scrape StepStone for job listings.

    Args:
        context: Patchright browser context
        queries: List of search queries
        location: Location filter (e.g. "Hannover")
        max_pages: Maximum number of pages to scrape per query
        radius: Search radius in km

    Returns:
        List of job dicts with title, company, location, url, source, search_query
    """
    all_jobs = []
    page = context.new_page()

    for query in queries:
        log.info("StepStone: searching '%s' (location=%s, radius=%d)", query, location or "any", radius)
        query_jobs = []
        consecutive_empty = 0

        for page_num in range(1, max_pages + 1):
            url = _build_stepstone_url(query, location, page_num, radius)
            log.info("  Page %d: %s", page_num, url)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                random_delay(2.0, 4.0)

                # Dismiss cookies on first page
                if page_num == 1:
                    dismiss_cookie_banner(page)
                    random_delay(1.0, 2.0)

                # Wait for job listings to load
                try:
                    page.wait_for_selector('article a[data-at="job-item-title"]', timeout=10000)
                except Exception:
                    log.warning("  No job cards found on page %d, might be last page", page_num)
                    consecutive_empty += 1
                    if consecutive_empty >= 2:
                        log.info("  Two empty pages in a row, stopping pagination")
                        break
                    continue

                # Scroll down to trigger lazy loading
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                random_delay(1.0, 2.0)

                jobs = _extract_stepstone_jobs(page)
                if not jobs:
                    consecutive_empty += 1
                    if consecutive_empty >= 2:
                        break
                    continue
                else:
                    consecutive_empty = 0

                for job in jobs:
                    job["source"] = "stepstone"
                    job["search_query"] = query

                query_jobs.extend(jobs)
                log.info("  Extracted %d jobs from page %d (total so far: %d)", len(jobs), page_num, len(query_jobs))

                # Check for next page
                next_btn = page.locator('a[data-at="pagination-next"]').first
                try:
                    if not next_btn.is_visible(timeout=3000):
                        log.info("  No next page button, stopping")
                        break
                except Exception:
                    log.info("  No next page button, stopping")
                    break

                random_delay(2.5, 5.0)

            except Exception as e:
                log.error("  Error on page %d: %s", page_num, e)
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break
                continue

        log.info("StepStone: '%s' -> %d jobs", query, len(query_jobs))
        all_jobs.extend(query_jobs)

    page.close()
    log.info("StepStone total: %d jobs from %d queries", len(all_jobs), len(queries))
    return all_jobs


# ---------------------------------------------------------------------------
# Google Jobs scraper
# ---------------------------------------------------------------------------


def _build_google_jobs_url(query: str) -> str:
    """Build a Google Jobs search URL."""
    return f"{GOOGLE_JOBS_BASE}?q={quote_plus(query)}&ibp=htl;jobs"


def _extract_google_jobs(page) -> list[dict]:
    """Extract job listings from Google Jobs using JS evaluate and card clicking.

    Google Jobs loads details lazily when clicking on job cards in the left panel.
    We click each card and extract the details from the right panel.
    """
    # First, get the number of job cards
    card_count = page.evaluate("""
        () => {
            const cards = document.querySelectorAll('li.iFjolb');
            return cards.length;
        }
    """)

    if card_count == 0:
        # Try alternative selectors
        card_count = page.evaluate("""
            () => {
                const cards = document.querySelectorAll('[jsname="yEVEwb"]');
                return cards.length;
            }
        """)

    log.info("  Found %d Google Jobs cards", card_count)
    jobs = []

    for i in range(card_count):
        try:
            # Click the card to load details
            page.evaluate(f"""
                () => {{
                    const cards = document.querySelectorAll('li.iFjolb');
                    if (cards[{i}]) {{
                        cards[{i}].click();
                    }} else {{
                        const alt = document.querySelectorAll('[jsname="yEVEwb"]');
                        if (alt[{i}]) alt[{i}].click();
                    }}
                }}
            """)
            time.sleep(0.8)

            # Extract job data from the detail panel
            job = page.evaluate("""
                () => {
                    // Title
                    const titleEl = document.querySelector('.BjJfJf') ||
                                    document.querySelector('h2[data-attrid="title"]') ||
                                    document.querySelector('.PwjeAc');
                    const title = titleEl ? titleEl.textContent.trim() : '';

                    // Company
                    const compEl = document.querySelector('.vNEEBe') ||
                                   document.querySelector('[data-attrid="subtitle"]') ||
                                   document.querySelector('.nJlQNd');
                    const company = compEl ? compEl.textContent.trim() : '';

                    // Location
                    const locEl = document.querySelector('.sMzDkb') ||
                                  document.querySelector('[data-attrid="location"]');
                    const location = locEl ? locEl.textContent.trim() : '';

                    // Apply link — look for the "Apply" button
                    let url = '';
                    const applyLinks = document.querySelectorAll('a.pMhGee, a[data-share-url], .B8oxKe a');
                    for (const a of applyLinks) {
                        const href = a.href || '';
                        if (href && !href.includes('google.com')) {
                            url = href;
                            break;
                        }
                    }

                    // Fallback: any external link in the detail panel
                    if (!url) {
                        const links = document.querySelectorAll('.whazf a, .pMhGee');
                        for (const a of links) {
                            const href = a.href || '';
                            if (href && !href.includes('google.com')) {
                                url = href;
                                break;
                            }
                        }
                    }

                    return { title, company, location, url };
                }
            """)

            if job.get("title") and job.get("company"):
                jobs.append(job)

        except Exception as e:
            log.debug("  Error extracting card %d: %s", i, e)
            continue

    return jobs


def scrape_google_jobs(
    context,
    queries: list[str],
    max_scrolls: int = 5,
) -> list[dict]:
    """Scrape Google Jobs for job listings.

    Args:
        context: Patchright browser context
        queries: List of search queries
        max_scrolls: Maximum number of scroll actions per query

    Returns:
        List of job dicts with title, company, location, url, source, search_query
    """
    all_jobs = []
    page = context.new_page()

    for query in queries:
        log.info("Google Jobs: searching '%s'", query)
        url = _build_google_jobs_url(query)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            random_delay(2.0, 4.0)

            # Dismiss cookies
            dismiss_cookie_banner(page)
            random_delay(1.0, 2.0)

            # Wait for job list to appear
            try:
                page.wait_for_selector("li.iFjolb, [jsname='yEVEwb']", timeout=15000)
            except Exception:
                log.warning("  No Google Jobs results found for '%s'", query)
                continue

            # Scroll the job list to load more results
            for scroll in range(max_scrolls):
                page.evaluate("""
                    () => {
                        const list = document.querySelector('.zxU94d') ||
                                     document.querySelector('[role="list"]');
                        if (list) {
                            list.scrollTop = list.scrollHeight;
                        }
                    }
                """)
                random_delay(1.5, 3.0)

                # Check if "Show more" button exists and click it
                try:
                    more_btn = page.locator("button:has-text('More jobs'), button:has-text('Weitere')").first
                    if more_btn.is_visible(timeout=2000):
                        more_btn.click()
                        random_delay(1.5, 3.0)
                except Exception:
                    pass

            # Extract all jobs
            jobs = _extract_google_jobs(page)
            for job in jobs:
                job["source"] = "google_jobs"
                job["search_query"] = query

            all_jobs.extend(jobs)
            log.info("Google Jobs: '%s' -> %d jobs", query, len(jobs))

            random_delay(3.0, 6.0)

        except Exception as e:
            log.error("Google Jobs error for '%s': %s", query, e)
            continue

    page.close()
    log.info("Google Jobs total: %d jobs from %d queries", len(all_jobs), len(queries))
    return all_jobs


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def init_db(db_path: str) -> sqlite3.Connection:
    """Initialize SQLite database and return connection."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(DB_SCHEMA)
    return conn


def _is_fuzzy_duplicate(
    conn: sqlite3.Connection,
    title: str,
    company: str,
    threshold: float = 0.85,
) -> bool:
    """Check if a job is a fuzzy duplicate of an existing entry.

    Compares against recent entries (last 5000) to avoid full table scan.
    Uses SequenceMatcher for fuzzy title+company matching.
    """
    rows = conn.execute(
        "SELECT title, company FROM jobs ORDER BY id DESC LIMIT 5000"
    ).fetchall()

    for row in rows:
        existing_title, existing_company = row
        if fuzzy_match(title, existing_title, threshold) and fuzzy_match(
            company, existing_company, threshold
        ):
            return True
    return False


def save_to_db(
    jobs: list[dict],
    db_path: str = None,
    conn: sqlite3.Connection = None,
    fuzzy_dedup: bool = True,
    fuzzy_threshold: float = 0.85,
) -> tuple[int, int, int]:
    """Save scraped jobs to SQLite database.

    Args:
        jobs: List of job dicts
        db_path: Path to SQLite DB (used if conn is None)
        conn: Existing connection (takes priority over db_path)
        fuzzy_dedup: Enable fuzzy duplicate detection
        fuzzy_threshold: Similarity threshold for fuzzy matching

    Returns:
        Tuple of (inserted, exact_dupes, fuzzy_dupes)
    """
    if db_path is None:
        db_path = DB_PATH

    own_conn = False
    if conn is None:
        conn = init_db(db_path)
        own_conn = True

    inserted = 0
    exact_dupes = 0
    fuzzy_dupes = 0

    for job in jobs:
        title = (job.get("title") or "").strip()
        company = (job.get("company") or "").strip()
        location = (job.get("location") or "").strip()
        url = normalize_url(job.get("url", ""))
        source = job.get("source", "unknown")
        search_query = job.get("search_query", "")

        if not title or not company:
            continue

        # Fuzzy dedup check
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
            # Check if the row was actually inserted (not an exact dupe)
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
        inserted,
        exact_dupes,
        fuzzy_dupes,
        len(jobs),
    )
    return inserted, exact_dupes, fuzzy_dupes


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="StepStone & Google Jobs browser scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --source stepstone --queries "Data Analyst" "Product Manager"
  %(prog)s --source google --queries "AI Engineer Berlin"
  %(prog)s --source both --queries "Business Analyst" --location "Hannover" --radius 50
  %(prog)s --source stepstone --queries-file ./data/queries.txt --db ./data/jobs.db
        """,
    )

    parser.add_argument(
        "--source",
        choices=["stepstone", "google", "both"],
        default="stepstone",
        help="Which job site to scrape (default: stepstone)",
    )
    parser.add_argument(
        "--queries",
        nargs="+",
        default=None,
        help="Search queries (space-separated)",
    )
    parser.add_argument(
        "--queries-file",
        default=None,
        help="Path to file with one query per line",
    )
    parser.add_argument(
        "--location",
        default="",
        help="Location filter for StepStone (e.g. 'Hannover')",
    )
    parser.add_argument(
        "--radius",
        type=int,
        default=0,
        help="Search radius in km for StepStone",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=5,
        help="Max pages per query for StepStone (default: 5)",
    )
    parser.add_argument(
        "--max-scrolls",
        type=int,
        default=5,
        help="Max scroll actions per query for Google Jobs (default: 5)",
    )
    parser.add_argument(
        "--db",
        default=os.getenv("DB_PATH", "./data/jobs.db"),
        help="Path to SQLite database",
    )
    parser.add_argument(
        "--no-fuzzy-dedup",
        action="store_true",
        help="Disable fuzzy duplicate detection",
    )
    parser.add_argument(
        "--fuzzy-threshold",
        type=float,
        default=0.85,
        help="Fuzzy matching threshold (default: 0.85)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=True,
        help="Run browser in headless mode (default: True)",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Show browser window (for debugging)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    headless = not args.no_headless

    # Resolve queries
    queries = args.queries or []
    if args.queries_file:
        try:
            with open(args.queries_file, "r", encoding="utf-8") as f:
                file_queries = [line.strip() for line in f if line.strip() and not line.startswith("#")]
                queries.extend(file_queries)
        except FileNotFoundError:
            log.error("Queries file not found: %s", args.queries_file)
            sys.exit(1)

    if not queries:
        queries = [
            "Data Analyst",
            "Business Analyst",
            "AI Specialist",
            "Product Manager",
            "Projektmanager IT",
        ]
        log.info("No queries specified, using defaults: %s", queries)

    # Initialize DB
    conn = init_db(args.db)

    # Scrape
    all_jobs = []

    with sync_playwright() as pw:
        browser, context = create_browser_context(pw, headless=headless)

        try:
            if args.source in ("stepstone", "both"):
                stepstone_jobs = scrape_stepstone(
                    context,
                    queries,
                    location=args.location,
                    max_pages=args.max_pages,
                    radius=args.radius,
                )
                all_jobs.extend(stepstone_jobs)

            if args.source in ("google", "both"):
                google_jobs = scrape_google_jobs(
                    context,
                    queries,
                    max_scrolls=args.max_scrolls,
                )
                all_jobs.extend(google_jobs)

        finally:
            context.close()
            browser.close()

    # Save to DB
    if all_jobs:
        inserted, exact_dupes, fuzzy_dupes = save_to_db(
            all_jobs,
            conn=conn,
            fuzzy_dedup=not args.no_fuzzy_dedup,
            fuzzy_threshold=args.fuzzy_threshold,
        )
        log.info(
            "DONE: %d new jobs saved, %d exact dupes, %d fuzzy dupes",
            inserted,
            exact_dupes,
            fuzzy_dupes,
        )
    else:
        log.warning("No jobs scraped")

    conn.close()

    # Summary
    summary_conn = sqlite3.connect(args.db)
    total = summary_conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    new_count = summary_conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status = 'new'"
    ).fetchone()[0]
    summary_conn.close()

    log.info("Database: %d total jobs, %d with status 'new'", total, new_count)


if __name__ == "__main__":
    main()
