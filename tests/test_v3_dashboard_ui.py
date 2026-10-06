"""
Browser-level acceptance tests for the v3 4-gate dashboard.

Real Chromium, real HTTP, real SQLite — these assert every bullet on the
v3 dashboard card against BOTH the DOM and the database, because the failure
mode that matters here is "the button looks done but nothing persisted".

Skipped unless a server is pointed at:

    python -m uvicorn src.web.app:app --port 8742
    V3_UI_BASE_URL=http://127.0.0.1:8742 pytest tests/test_v3_dashboard_ui.py

Set V3_UI_DB to the SQLite file that server writes to (default: the repo's
data/jobs.db) so the DB assertions hit the same store the browser mutated.
The test restores every row it touches, so pointing it at data/jobs.db is safe.
"""
import os
import sqlite3

import pytest

BASE_URL = os.environ.get("V3_UI_BASE_URL")
DB_PATH = os.environ.get("V3_UI_DB", str(
    __import__("pathlib").Path(__file__).resolve().parents[1] / "data" / "jobs.db"))

pytestmark = pytest.mark.skipif(
    not BASE_URL, reason="set V3_UI_BASE_URL to a running server to run browser tests")

# Companies/scores the card expects to see on the dashboard (top 6 by score).
EXPECTED_CARDS = [
    ("Andercore", "1940"),
    ("xHeron Solutions", "1810"),
    ("businessangels.de", "1500"),
    ("Lightcone Capital", "1150"),
    ("cfab", "950"),
    ("Zasta Karriere", "210"),
]


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as pw:
        b = pw.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    """A signed-in page. Every dialog is auto-accepted — in the real app the
    Captain clicks through the confirm() himself."""
    ctx = browser.new_context(viewport={"width": 1600, "height": 1100})
    p = ctx.new_page()
    p.on("dialog", lambda d: d.accept())
    p.goto(f"{BASE_URL}/static/login.html")
    p.fill("#email", "lars")
    p.fill("#password", "captain")
    p.click("#submit-btn")
    p.wait_for_url("**/app/**", timeout=20000)
    p.wait_for_selector(".card", timeout=20000)
    yield p
    ctx.close()


def db_flags(job_id):
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            "SELECT cv_ok, anschreiben_ok, motivation_ok, status FROM jobs WHERE id=?",
            (job_id,)).fetchone()
    finally:
        conn.close()


def snapshot(job_id):
    return db_flags(job_id)


def restore(job_id, state):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "UPDATE jobs SET cv_ok=?, anschreiben_ok=?, motivation_ok=?, status=? WHERE id=?",
            (*state, job_id))
        conn.commit()
    finally:
        conn.close()


def gate(page, job_id, stage):
    return page.locator(f'.card[data-job-id="{job_id}"] .gate[data-stage="{stage}"]')


def click_gate(page, job_id, stage, expect_state):
    gate(page, job_id, stage).locator(".gate__act").click()
    page.wait_for_function(
        f"() => document.querySelector('.card[data-job-id=\"{job_id}\"] "
        f".gate[data-stage=\"{stage}\"]').dataset.state === '{expect_state}'",
        timeout=10000)


# ----- render --------------------------------------------------------------

def test_session_cookie_auth_no_basic_prompt(browser):
    """Constraint: browser uses the session cookie, never an HTTP Basic box."""
    ctx = browser.new_context()
    p = ctx.new_page()
    p.goto(f"{BASE_URL}/static/login.html")
    p.fill("#email", "lars")
    p.fill("#password", "captain")
    p.click("#submit-btn")
    p.wait_for_url("**/app/**", timeout=20000)
    names = {c["name"] for c in ctx.cookies()}
    assert any("session" in n for n in names), f"no session cookie set: {names}"
    ctx.close()


def test_six_top_jobs_render_in_two_columns(page):
    assert page.locator("#jobs-grid .card").count() == 6
    cols = page.eval_on_selector_all(
        "#jobs-grid", "els => getComputedStyle(els[0]).gridTemplateColumns.split(' ').length")
    assert cols == 2, f"expected a 2-column grid, got {cols} column(s)"


def test_score_circles_show_raw_scores(page):
    seen = page.eval_on_selector_all(
        ".card",
        "els => els.map(e => [e.querySelector('.card__company').textContent.trim(),"
        " e.querySelector('.score__num').textContent.trim()])")
    assert seen == [list(pair) for pair in EXPECTED_CARDS], f"got {seen}"


def test_each_card_has_three_gates_plus_approve_and_skip(page):
    ok = page.eval_on_selector_all(
        ".card",
        "els => els.every(e => e.dataset.jobId"
        " && [...e.querySelectorAll('.gate')].map(g => g.dataset.stage).join(',')"
        " === 'cv,anschreiben,motivation'"
        " && e.querySelector('[data-act=approve]')"
        " && e.querySelector('[data-act=skip]'))")
    assert ok, "a card is missing data-job-id, a gate stage, Approve or Skip"


# ----- the 4-gate progression ---------------------------------------------

def test_full_gate_progression_persists_to_db(page):
    job = 63
    before = snapshot(job)
    restore(job, (0, 0, 0, before[3]))
    try:
        card = page.locator(f'.card[data-job-id="{job}"]')
        approve = card.locator("[data-act=approve]")

        # Only gate 01 is clickable to begin with.
        assert not gate(page, job, "cv").locator(".gate__act").is_disabled()
        assert gate(page, job, "anschreiben").locator(".gate__act").is_disabled()
        assert gate(page, job, "motivation").locator(".gate__act").is_disabled()
        assert approve.is_disabled(), "Approve must be dark until all 3 gates carry approval"

        click_gate(page, job, "cv", "done")
        assert db_flags(job)[0] == 1, "gate 1 must set cv_ok"
        assert not gate(page, job, "anschreiben").locator(".gate__act").is_disabled()

        click_gate(page, job, "anschreiben", "done")
        assert db_flags(job)[1] == 1, "gate 2 must set anschreiben_ok"
        assert not gate(page, job, "motivation").locator(".gate__act").is_disabled()

        click_gate(page, job, "motivation", "done")
        assert db_flags(job)[2] == 1, "gate 3 must set motivation_ok"
        assert not approve.is_disabled(), "Approve unlocks once all 3 gates are ok"
    finally:
        restore(job, before)


def test_approve_button_turns_cyan_when_all_gates_ok(page):
    job = 79
    before = snapshot(job)
    restore(job, (1, 1, 1, "queued"))
    try:
        page.reload()
        page.wait_for_selector(".card")
        colour = page.eval_on_selector(
            f'.card[data-job-id="{job}"] .approve__btn',
            "el => getComputedStyle(el).backgroundColor")
        assert colour == "rgb(6, 182, 212)", f"expected cyan, got {colour}"
    finally:
        restore(job, before)


def test_rolling_back_gate_one_reverts_downstream(page):
    job = 63
    before = snapshot(job)
    restore(job, (1, 1, 1, "queued"))
    try:
        page.reload()
        page.wait_for_selector(".card")
        click_gate(page, job, "cv", "active")
        flags = db_flags(job)
        assert flags[:3] == (0, 0, 0), f"downstream gates must revert, got {flags}"
        assert gate(page, job, "anschreiben").locator(".gate__act").is_disabled()
        assert gate(page, job, "motivation").locator(".gate__act").is_disabled()
        assert page.locator(
            f'.card[data-job-id="{job}"] [data-act=approve]').is_disabled()
    finally:
        restore(job, before)


def test_completed_gate_notes_read_approved_not_ready(page):
    """Regression: noteText was computed before isOn was assigned, so a finished
    gate still read "Ready to review"."""
    job = 63
    before = snapshot(job)
    restore(job, (1, 1, 1, "queued"))
    try:
        page.reload()
        page.wait_for_selector(".card")
        notes = page.eval_on_selector_all(
            f'.card[data-job-id="{job}"] .gate',
            "els => els.map(e => e.querySelector('.gate__note').textContent)")
        assert all("Approved" in n for n in notes), f"stale gate notes: {notes}"
    finally:
        restore(job, before)


# ----- final actions -------------------------------------------------------

def test_approve_flips_status_and_card_moves_to_bottom(page):
    job = 63
    before = snapshot(job)
    restore(job, (1, 1, 1, "queued"))
    try:
        page.reload()
        page.wait_for_selector(".card")
        page.locator(f'.card[data-job-id="{job}"] [data-act=approve]').click()
        page.wait_for_function(
            f"() => document.querySelector('.card[data-job-id=\"{job}\"]')"
            ".dataset.state === 'approved'", timeout=10000)
        assert db_flags(job)[3] == "approved"
        page.wait_for_timeout(800)
        last = page.eval_on_selector_all(
            "#jobs-grid .card", "els => els[els.length-1].dataset.jobId")
        assert last == str(job), f"approved card should sink to the bottom, last is {last}"
    finally:
        restore(job, before)


def test_skip_flips_status_and_card_greys_out(page):
    job = 80
    before = snapshot(job)
    restore(job, (0, 0, 0, "queued"))
    try:
        page.reload()
        page.wait_for_selector(".card")
        page.locator(f'.card[data-job-id="{job}"] [data-act=skip]').click()
        page.wait_for_function(
            f"() => document.querySelector('.card[data-job-id=\"{job}\"]')"
            ".dataset.state === 'skipped'", timeout=10000)
        assert db_flags(job)[3] == "skipped"
        # The card fades + slides to the bottom; wait for the transition to land
        # rather than sampling opacity mid-animation.
        page.wait_for_function(
            f"() => parseFloat(getComputedStyle("
            f"document.querySelector('.card[data-job-id=\"{job}\"]')).opacity) < 0.6",
            timeout=10000)
        opacity = page.eval_on_selector(
            f'.card[data-job-id="{job}"]', "el => getComputedStyle(el).opacity")
        assert float(opacity) < 0.6, f"skipped card should grey out, opacity={opacity}"
    finally:
        restore(job, before)


def test_approve_is_unreachable_without_all_three_gates(page):
    job = 63
    before = snapshot(job)
    restore(job, (1, 0, 0, "queued"))
    try:
        page.reload()
        page.wait_for_selector(".card")
        btn = page.locator(f'.card[data-job-id="{job}"] [data-act=approve]')
        assert btn.is_disabled()
        # Force-click past the disabled attribute: the handler must still refuse.
        btn.click(force=True)
        page.wait_for_timeout(400)
        assert db_flags(job)[3] == "queued", "forced click must not approve the job"
    finally:
        restore(job, before)