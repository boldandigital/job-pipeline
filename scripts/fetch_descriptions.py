#!/usr/bin/env python3
"""Fetch full job descriptions for XING jobs that don't have one yet.

After the v1 XING scraper saves jobs (with title/company/location/url but
no description), this script opens each URL in Patchright and saves the
description. Scoring needs the description text to match keywords against
— otherwise even a great match scores 0.

Usage:
  python scripts/fetch_descriptions.py --limit 20   # max jobs to fetch
  python scripts/fetch_descriptions.py --source xing
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def _clean_xing_description(text: str) -> str:
    """Strip XING sidebar noise ('Ähnliche Jobs', similar-jobs blocks, footer).

    XING renders the full page (including 'Similar Jobs' recommendations) into
    `main article`, so we get the actual JD plus 5-15 unrelated job titles below
    it. This post-processor trims at the first noise marker.
    """
    if not text:
        return ""
    # Markers that signal end of real JD + start of sidebar/recs
    noise_markers = [
        "\nÄhnliche Jobs\n",
        "\nÄhnliche Stellen\n",
        "\nWeitere Jobs\n",
        "\nSimilar Jobs\n",
        "\nNoch 2 Tage",  # XING "deadline" tags
        "\nVor 7 Tagen",
        "\nVor 4 Tagen",
        "\nVor einem Tag",
        "\nJob merken\n",
        "\nSuchauftrag erstellen\n",
    ]
    cut_at = len(text)
    for marker in noise_markers:
        idx = text.find(marker)
        if idx > 0 and idx < cut_at:
            cut_at = idx
    return text[:cut_at].strip()


def fetch_xing_description(url: str) -> str:
    """Open the XING job URL in headless Chromium and return the description."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox"],
            executable_path="/Users/lars/Library/Caches/ms-playwright/chromium-1243/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
        )
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            # XING job description typically lives in a section with class
            # "jobs-description" or similar. Try structured selectors first,
            # then fall back to generic selectors.
            for selector in (
                "[data-testid='job-description']",
                ".jobs-description",
                ".job-description",
                # XING uses class names like 'job-ad-content' or wraps the JD
                # in a <section> with class containing 'job' but NOT 'similar'
                "section[class*='job']:not([class*='similar']):not([class*='recommend'])",
                "div[class*='job-ad']",
                "main article > section",
            ):
                try:
                    el = page.locator(selector).first
                    if el.count() > 0:
                        text = el.inner_text(timeout=5000)
                        # Filter: must be >200 chars AND not start with "Similar jobs"
                        if len(text) > 200 and not text.lower().startswith("similar jobs"):
                            return _clean_xing_description(text[:8000])
                except Exception:
                    pass
            # No structured description found — grab main content
            try:
                text = page.locator("main").first.inner_text(timeout=5000)
                return _clean_xing_description(text[:8000]) if text else ""
            except Exception:
                return ""
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "jobs.db"))
    parser.add_argument("--source", default="xing",
                        help="Only fetch for jobs from this source")
    parser.add_argument("--limit", type=int, default=20,
                        help="Max jobs to fetch in this run")
    parser.add_argument("--delay", type=float, default=1.5,
                        help="Seconds between fetches (rate-limit)")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Find jobs without descriptions
    rows = conn.execute(
        """
        SELECT id, url, title, company
        FROM jobs
        WHERE source = ?
          AND (description IS NULL OR description = '')
          AND url IS NOT NULL AND url != ''
        ORDER BY id DESC
        LIMIT ?
        """,
        (args.source, args.limit),
    ).fetchall()

    if not rows:
        print(f"  No {args.source} jobs need descriptions — all up to date.")
        return

    print(f"  Fetching descriptions for {len(rows)} {args.source} jobs...")
    fetched = 0
    failed = 0

    for row in rows:
        url = row["url"]
        print(f"  → [{row['id']}] {row['title'][:50]} @ {row['company'][:30]}")
        try:
            desc = fetch_xing_description(url)
            if desc and len(desc) > 100:
                conn.execute(
                    "UPDATE jobs SET description = ? WHERE id = ?",
                    (desc, row["id"]),
                )
                conn.commit()
                fetched += 1
                print(f"    ✓ {len(desc)} chars")
            else:
                failed += 1
                print(f"    ✗ description too short or empty")
        except Exception as exc:
            failed += 1
            print(f"    ✗ error: {exc}")
        time.sleep(args.delay)

    conn.close()
    print(f"\n  🏴‍☠️ {fetched} descriptions fetched, {failed} failed")


if __name__ == "__main__":
    main()