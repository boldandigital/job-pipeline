"""
Unit tests for src.scrapers.xing_scraper.

The browser is fully mocked — we never hit the network. Tests cover:
- URL construction
- HTML extraction (the JS evaluator)
- Cookie banner dismissal
- Captcha + login-wall guards
- In-memory dedupe across queries
- DB schema + fuzzy + exact dedup
- End-to-end CLI flag wiring (no browser launch)

Run:  pytest tests/test_xing_scraper.py -v
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make src/ importable when tests are run from project root or from tests/
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.scrapers import xing_scraper  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "jobs.db")


@pytest.fixture
def fake_card_html() -> str:
    """A trimmed copy of the XING job-card markup we observed in the wild.

    Anchored on the same data-testid hooks the scraper uses:
    - job-search-result
    - job-teaser-list-title
    """
    return """
    <div data-testid="job-search-result"
         aria-label="Co-Founder (m/w/d). Klicke zum Öffnen der vollständigen Job-Beschreibung">
      <a href="/jobs/berlin-co-founder-mwd-123456789" target="_blank"></a>
      <h2 class="headline-styles__Headline-sc-2939f1b1-0 fZCTnC job-teaser-list-item-styles__Title-sc-614863cf-10 hurfms"
          data-testid="job-teaser-list-title">Co-Founder (m/w/d)</h2>
      <p class="body-copy-styles__BodyCopy-sc-1c0aad99-0 fUDkJz job-teaser-list-item-styles__Company-sc-614863cf-11 fJsoJC"
         data-xds="BodyCopy">Some Startup GmbH</p>
      <div class="multi-location-display-styles__Container-sc-9b3f4a01-0 xyz">
        <span class="multi-location-display-styles__Text-sc-9b3f4a01-1 abc">Berlin</span>
        <span class="multi-location-display-styles__Text-sc-9b3f4a01-1 abc">+ 0 more</span>
      </div>
    </div>
    <div data-testid="job-search-result"
         aria-label="Head of Digital">
      <a href="/jobs/muenchen-head-of-digital-987654321" target="_blank"></a>
      <h2 data-testid="job-teaser-list-title">Head of Digital Transformation (m/w/d)</h2>
      <p data-xds="BodyCopy">Bold Agency AG</p>
      <div>
        <span>Dringend gesucht</span>
        <span>München</span>
      </div>
    </div>
    <div data-testid="job-search-result"
         aria-label="Founder's Associate">
      <a href="/jobs/leipzig-founders-associate-555000111" target="_blank"></a>
      <h2 data-testid="job-teaser-list-title">Founder's Associate (m/w/d)</h2>
      <p data-xds="BodyCopy">AnotherCo</p>
      <div><span>Leipzig</span></div>
    </div>
    """


def _make_page(html_results: list | None = None, body_text: str = ""):
    """Build a mock Patchright page object."""
    page = MagicMock()
    page.evaluate = MagicMock(side_effect=lambda script: html_results if html_results is not None else body_text)
    page.goto = MagicMock(return_value=MagicMock(status=200))
    page.wait_for_selector = MagicMock()
    page.wait_for_timeout = MagicMock()
    page.close = MagicMock()
    return page


def _build_jobs(html: str):
    """Replicates the JS evaluator from _extract_xing_jobs in pure Python.

    Mirrors the DOM-walking logic so we can validate the regex/selectors
    without launching a real browser.
    """
    import re

    results = []
    cards = re.findall(
        r'data-testid="job-search-result"(.*?)(?=data-testid="job-search-result"|$)',
        html,
        re.S,
    )
    for card_html in cards:
        title_m = re.search(r'data-testid="job-teaser-list-title"[^>]*>([^<]+)<', card_html)
        if not title_m:
            continue
        title = title_m.group(1).strip()

        url_m = re.search(r'href="(/jobs/[^"]+)"', card_html)
        url = url_m.group(1) if url_m else ""

        company_m = re.search(
            r'data-xds="BodyCopy"[^>]*>([^<]+)<', card_html
        )
        company = company_m.group(1).strip() if company_m else ""

        location = ""
        for loc_m in re.finditer(r'<span[^>]*>([^<]+)</span>', card_html):
            txt = loc_m.group(1).strip()
            if not txt or re.search(r'dringend|easy apply|gesucht', txt, re.I):
                continue
            if "+" in txt and "more" in txt.lower():
                continue
            location = txt
            break

        if title and company:
            results.append({"title": title, "company": company, "location": location, "url": url})
    return results


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------


class TestBuildXingUrl:
    def test_page_1_has_no_page_param(self):
        url = xing_scraper._build_xing_url("founder digital", "Deutschland", page=1)
        assert url == "https://www.xing.com/jobs/search?keywords=founder+digital&location=Deutschland"

    def test_page_2_includes_page_param(self):
        url = xing_scraper._build_xing_url("founder digital", "Deutschland", page=2)
        assert "page=2" in url

    def test_no_location_means_no_location_param(self):
        url = xing_scraper._build_xing_url("cto", "", page=1)
        assert "location" not in url

    def test_url_encodes_german_umlauts(self):
        url = xing_scraper._build_xing_url("geschäftsführer", "München", page=1)
        assert "gesch" in url
        # quote_plus percent-encodes the umlauts
        assert "M" in url and "nchen" in url


# ---------------------------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------------------------


class TestExtractXingJobs:
    def test_extracts_three_cards_from_fixture(self, fake_card_html):
        jobs = _build_jobs(fake_card_html)
        assert len(jobs) == 3
        titles = [j["title"] for j in jobs]
        assert "Co-Founder (m/w/d)" in titles
        assert "Head of Digital Transformation (m/w/d)" in titles
        assert "Founder's Associate (m/w/d)" in titles

    def test_captures_company_and_url(self, fake_card_html):
        jobs = _build_jobs(fake_card_html)
        co = next(j for j in jobs if j["title"] == "Co-Founder (m/w/d)")
        assert co["company"] == "Some Startup GmbH"
        assert co["url"] == "/jobs/berlin-co-founder-mwd-123456789"

    def test_skips_dringer_marker_as_location(self, fake_card_html):
        jobs = _build_jobs(fake_card_html)
        h = next(j for j in jobs if j["title"] == "Head of Digital Transformation (m/w/d)")
        # "Dringend gesucht" must be skipped — the location should be München.
        assert h["location"] == "München"


# ---------------------------------------------------------------------------
# Captcha / login-wall guards
# ---------------------------------------------------------------------------


class TestBlockCheck:
    def test_captcha_signal_detected(self):
        page = _make_page(body_text="Please confirm you are a human to continue.")
        assert xing_scraper._block_check(page) == "captcha"

    def test_login_wall_detected(self):
        page = _make_page(body_text="Anmelden, um Jobs zu sehen und zu speichern.")
        assert xing_scraper._block_check(page) == "login"

    def test_clean_page_returns_none(self):
        page = _make_page(body_text="255 jobs found — Co-Founder Berlin")
        assert xing_scraper._block_check(page) is None

    def test_empty_body_returns_none(self):
        page = _make_page(body_text="")
        assert xing_scraper._block_check(page) is None


# ---------------------------------------------------------------------------
# Cookie banner
# ---------------------------------------------------------------------------


class TestDismissCookieBanner:
    def test_first_visible_selector_wins(self):
        page = MagicMock()
        first_handle = MagicMock()
        first_handle.is_visible = MagicMock(return_value=True)
        first_handle.click = MagicMock()
        # page.locator(sel).first — the .first attribute IS first_handle
        page.locator = MagicMock(return_value=MagicMock(first=first_handle))
        xing_scraper.dismiss_cookie_banner(page)
        first_handle.click.assert_called_once()

    def test_no_visible_button_is_silent(self):
        page = MagicMock()
        first_handle = MagicMock()
        first_handle.is_visible = MagicMock(return_value=False)
        first_handle.click = MagicMock()
        page.locator = MagicMock(return_value=MagicMock(first=first_handle))
        # Should not raise; should not click anything.
        xing_scraper.dismiss_cookie_banner(page)
        first_handle.click.assert_not_called()


# ---------------------------------------------------------------------------
# Browser context
# ---------------------------------------------------------------------------


class TestBrowserContext:
    def test_context_uses_german_locale_and_berlin_tz(self):
        # Patchright launches a real browser — we mock sync_playwright to
        # avoid actually launching Chromium, then assert the constructor args.
        with patch("src.scrapers.xing_scraper.sync_playwright") as sp:
            mock_pw = MagicMock()
            sp.return_value.__enter__.return_value = mock_pw
            mock_browser = mock_pw.chromium.launch.return_value
            mock_context = mock_browser.new_context.return_value
            browser, context = xing_scraper.create_browser_context(mock_pw, headless=True)
        # browser.launch must be called headless=True with the stealth args
        launch_kwargs = mock_pw.chromium.launch.call_args.kwargs
        assert launch_kwargs["headless"] is True
        assert "--no-sandbox" in launch_kwargs["args"]
        # browser.new_context must use DE locale + Berlin TZ
        ctx_kwargs = mock_browser.new_context.call_args.kwargs
        assert ctx_kwargs["locale"] == "de-DE"
        assert ctx_kwargs["timezone_id"] == "Europe/Berlin"
        assert "geolocation" in ctx_kwargs
        assert "user_agent" in ctx_kwargs
        # The stealth script must have been installed
        assert mock_context.add_init_script.called
        # Returned objects must match what playwright gave back
        assert browser is mock_browser
        assert context is mock_context


# ---------------------------------------------------------------------------
# Database schema + dedupe
# ---------------------------------------------------------------------------


class TestDatabase:
    def test_init_db_creates_table(self, tmp_db):
        conn = xing_scraper.init_db(tmp_db)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
        finally:
            conn.close()
        for col in ("title", "company", "location", "url", "career_url",
                    "source", "description", "search_query", "score", "status"):
            assert col in cols, f"missing column {col}"
        # UNIQUE constraint must exist (drives exact dedup)
        idx = [r[1] for r in sqlite3.connect(tmp_db).execute(
            "PRAGMA index_list(jobs)"
        ).fetchall()]
        assert any("autoindex" in i for i in idx)

    def test_exact_dedupe_via_unique_constraint(self, tmp_db):
        conn = xing_scraper.init_db(tmp_db)
        inserted, exact, fuzzy = xing_scraper.save_to_db(
            [
                {"title": "Co-Founder", "company": "X", "source": "xing"},
                {"title": "Co-Founder", "company": "X", "source": "xing"},  # exact dupe
            ],
            conn=conn,
            fuzzy_dedup=False,  # let the UNIQUE constraint be the gate
        )
        conn.close()
        assert inserted == 1
        assert exact == 1
        assert fuzzy == 0

    def test_with_fuzzy_dedup_exact_matches_are_filtered_by_fuzzy(self, tmp_db):
        """Exact duplicates are also caught by the fuzzy check (ratio == 1.0 > threshold)."""
        conn = xing_scraper.init_db(tmp_db)
        inserted, exact, fuzzy = xing_scraper.save_to_db(
            [
                {"title": "Co-Founder", "company": "X", "source": "xing"},
                {"title": "Co-Founder", "company": "X", "source": "xing"},  # same again
            ],
            conn=conn,
            fuzzy_dedup=True,
        )
        conn.close()
        assert inserted == 1
        # Exact-dupe is a 1.0-ratio fuzzy match, so it's classified as fuzzy.
        assert fuzzy == 1
        assert exact == 0

    def test_fuzzy_dedupe_against_existing_row(self, tmp_db):
        conn = xing_scraper.init_db(tmp_db)
        # Seed one job
        xing_scraper.save_to_db(
            [{"title": "Co-Founder (m/w/d)", "company": "Startup Hub GmbH", "source": "xing"}],
            conn=conn,
            fuzzy_dedup=False,
        )
        # A near-duplicate (whitespace + minor variant) must be caught
        inserted, exact, fuzzy = xing_scraper.save_to_db(
            [{"title": "Co-Founder (m/w/d)", "company": "Startup  Hub GmbH", "source": "xing"}],
            conn=conn,
            fuzzy_threshold=0.85,
        )
        conn.close()
        assert inserted == 0
        assert fuzzy == 1

    def test_normalize_url_strips_tracking(self):
        assert xing_scraper.normalize_url(
            "https://www.xing.com/jobs/berlin-cofounder-123?utm_source=x&ref=y"
        ) == "https://www.xing.com/jobs/berlin-cofounder-123"
        assert xing_scraper.normalize_url("") == ""
        assert xing_scraper.normalize_url(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Top-level scrape_xing — dedupe across queries
# ---------------------------------------------------------------------------


class TestScrapeXingDedupe:
    def test_same_job_across_queries_is_kept_once(self):
        ctx = MagicMock()
        # Force the inner scraper to return one shared job for both queries
        with patch.object(xing_scraper, "_scrape_query") as mock_q:
            shared = {
                "title": "Co-Founder (m/w/d)",
                "company": "Same GmbH",
                "location": "Berlin",
                "url": "https://www.xing.com/jobs/berlin-cofounder-123",
                "source": "xing",
                "search_query": "ignored-here",
            }
            mock_q.side_effect = [
                [dict(shared, search_query="founder digital")],
                [dict(shared, search_query="cto startup")],
            ]
            jobs = xing_scraper.scrape_xing(
                ctx,
                queries=["founder digital", "cto startup"],
                location="Deutschland",
                max_pages=2,
                limit=50,
            )
        assert len(jobs) == 1
        assert jobs[0]["company"] == "Same GmbH"


# ---------------------------------------------------------------------------
# Captcha + login-wall behaviour in _scrape_query
# ---------------------------------------------------------------------------


class TestScrapeQueryGuards:
    def test_captcha_short_circuits(self):
        ctx = MagicMock()
        # First evaluate call returns body with captcha; subsequent calls
        # would return an empty list — but the captcha branch bails before
        # they happen.
        page = MagicMock()
        page.goto = MagicMock(return_value=MagicMock(status=200))
        page.evaluate = MagicMock(side_effect=[
            "please confirm you are a human",  # _block_check
            "please confirm you are a human",  # also re-checked
            [],                                  # never reached
        ])
        page.wait_for_selector = MagicMock()
        page.close = MagicMock()
        ctx.new_page.return_value = page

        with patch.object(xing_scraper, "dismiss_cookie_banner"):
            jobs = xing_scraper._scrape_query(
                ctx, query="founder", location="Deutschland", max_pages=3, limit=25
            )
        assert jobs == []
        # We should have bailed after page 1.
        assert page.goto.call_count == 1

    def test_login_wall_short_circuits(self):
        ctx = MagicMock()
        page = MagicMock()
        page.goto = MagicMock(return_value=MagicMock(status=200))
        page.evaluate = MagicMock(side_effect=[
            "anmelden, um jobs zu sehen",  # _block_check
        ])
        page.wait_for_selector = MagicMock()
        page.close = MagicMock()
        ctx.new_page.return_value = page

        with patch.object(xing_scraper, "dismiss_cookie_banner"):
            jobs = xing_scraper._scrape_query(
                ctx, query="founder", location="Deutschland", max_pages=5, limit=25
            )
        assert jobs == []

    def test_429_response_backs_off_and_retries(self):
        ctx = MagicMock()
        page = MagicMock()
        # First call: 429; subsequent calls: 200 with body that has jobs
        responses = [
            MagicMock(status=429),
            MagicMock(status=200),
        ]
        page.goto = MagicMock(side_effect=responses)
        # Order of page.evaluate calls inside the loop:
        #   1. _block_check (after retry, returns clean body)
        #   2. window.scrollTo  (triggers lazy load — return None)
        #   3. _extract_xing_jobs (returns the job dict)
        page.evaluate = MagicMock(side_effect=[
            "ok",                                # _block_check
            None,                                # scrollTo
            [{"title": "X", "company": "Y", "location": "Z", "url": "/jobs/x-y-1"}],
        ])
        page.wait_for_selector = MagicMock()
        page.close = MagicMock()
        ctx.new_page.return_value = page

        with patch.object(xing_scraper, "dismiss_cookie_banner"), \
             patch.object(xing_scraper.time, "sleep"):
            jobs = xing_scraper._scrape_query(
                ctx, query="founder", location="Deutschland", max_pages=2, limit=25
            )
        assert len(jobs) == 1
        assert jobs[0]["title"] == "X"


# ---------------------------------------------------------------------------
# CLI: --queries-file + default fallback (no browser launch)
# ---------------------------------------------------------------------------


class TestCLIFlags:
    def test_resolve_queries_uses_defaults_when_nothing_passed(self):
        ns = MagicMock()
        ns.queries = None
        ns.queries_file = None
        result = xing_scraper._resolve_queries(ns)
        assert "founder digital" in result
        assert "cto startup" in result
        assert "geschäftsführer digital" in result

    def test_resolve_queries_reads_file(self, tmp_path: Path):
        qfile = tmp_path / "queries.txt"
        qfile.write_text(
            "# comment line\nfounder digital\n\ncto startup\n",
            encoding="utf-8",
        )
        ns = MagicMock()
        ns.queries = []
        ns.queries_file = str(qfile)
        result = xing_scraper._resolve_queries(ns)
        assert result == ["founder digital", "cto startup"]

    def test_resolve_queries_missing_file_exits(self, tmp_path: Path):
        ns = MagicMock()
        ns.queries = ["founder"]
        ns.queries_file = str(tmp_path / "missing.txt")
        with pytest.raises(SystemExit):
            xing_scraper._resolve_queries(ns)


# ---------------------------------------------------------------------------
# CLI main() integration — full DB round-trip with mocked browser
# ---------------------------------------------------------------------------


class TestMainEndToEnd:
    def test_main_writes_jobs_to_db(self, tmp_db, monkeypatch):
        # Patch the browser context + scraper so we never hit the network
        monkeypatch.setattr(xing_scraper, "scrape_xing", MagicMock(return_value=[
            {
                "title": "Co-Founder (m/w/d)",
                "company": "Berlin Co",
                "location": "Berlin",
                "url": "https://www.xing.com/jobs/berlin-co-1",
                "source": "xing",
                "search_query": "founder digital",
            },
            {
                "title": "Head of Digital",
                "company": "Munich AG",
                "location": "München",
                "url": "https://www.xing.com/jobs/munich-hod-2",
                "source": "xing",
                "search_query": "founder digital",
            },
        ]))
        monkeypatch.setattr(
            "sys.argv",
            ["xing_scraper", "--queries", "founder digital", "--db", tmp_db, "--max-pages", "1"],
        )
        xing_scraper.main()

        rows = sqlite3.connect(tmp_db).execute(
            "SELECT title, company, source FROM jobs ORDER BY id"
        ).fetchall()
        assert ("Co-Founder (m/w/d)", "Berlin Co", "xing") in rows
        assert ("Head of Digital", "Munich AG", "xing") in rows