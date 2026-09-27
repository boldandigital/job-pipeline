"""Tests for the Greenhouse adapter.

Uses the in-memory FakeDriver/FakePage from conftest.py — no real browser,
no real network.

Covers:
  - Happy path: all fields filled, resume + cover letter uploaded, submit clicked
  - Login wall → paused_for_review (no submit)
  - Captcha → paused_for_review (no submit)
  - Missing CV file → paused_for_review (no submit)
  - Missing career_url → paused_for_review
  - Custom questions (LinkedIn / years / sponsorship) get answered
  - Submit click failure → success=False, error populated
  - ats_name field is set to 'greenhouse'
  - Greenhouse is in the ATS_REGISTRY after import

Run: pytest tests/test_apply_greenhouse.py -v
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest

from tests.conftest import apply_base as base_mod, apply_greenhouse  # noqa: E402
from tests.conftest import FakeDriver  # noqa: E402

GreenhouseAdapter = apply_greenhouse.GreenhouseAdapter


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_greenhouse_is_registered_in_registry():
    assert base_mod.resolve_adapter("greenhouse") is GreenhouseAdapter
    assert base_mod.resolve_adapter("GREENHOUSE") is GreenhouseAdapter
    assert GreenhouseAdapter.ats_name == "greenhouse"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_fills_required_fields(
    adapter, profile, greenhouse_driver, base_job, tmp_cv, tmp_cover_letter,
):
    result = _run(adapter.apply(
        base_job, profile, tmp_cv, tmp_cover_letter, greenhouse_driver,
    ))

    assert result.success is True
    assert result.submitted is True
    assert result.is_generic_fallback is False
    assert result.paused_for_review is False
    assert result.ats_type == "greenhouse"
    assert result.job_id == 123
    assert result.error is None

    page = greenhouse_driver.page
    fills = {f["selector"]: f["value"] for f in page.log.fill}
    assert fills["#first_name"] == "Lars"
    assert fills["#last_name"] == "Zimmermann"
    assert fills["#email"] == "lars.z@icloud.com"
    assert fills["#phone"] == "+32 470 123 456"

    uploads = {u["selector"]: u["file_path"] for u in page.log.upload_file}
    assert uploads["input#resume"] == tmp_cv
    assert uploads["input#cover_letter"] == tmp_cover_letter

    assert any("submit" in s for s in page.log.click)


def test_happy_path_skips_cover_letter_when_not_present(
    adapter, profile, tmp_cv, base_job,
):
    dom = {
        "#first_name":  {"tag": "input", "type": "text"},
        "#last_name":   {"tag": "input", "type": "text"},
        "#email":       {"tag": "input", "type": "email"},
        "#phone":       {"tag": "input", "type": "tel"},
        "input#resume": {"tag": "input", "type": "file"},
        'input[type="submit"][name="submit"]': {"tag": "input", "type": "submit"},
    }
    d = FakeDriver()
    d.page.dom = dom

    result = _run(adapter.apply(base_job, profile, tmp_cv, "", d))

    assert result.success is True
    uploads = [u["selector"] for u in d.page.log.upload_file]
    assert "input#cover_letter" not in uploads
    assert "input#resume" in uploads


# ---------------------------------------------------------------------------
# Safety: never submits on login wall / captcha / missing CV
# ---------------------------------------------------------------------------


def test_login_wall_pauses_for_review(
    adapter, profile, base_job, tmp_cv, tmp_cover_letter,
):
    dom = {
        "#first_name":  {"tag": "input", "type": "text"},
        "#last_name":   {"tag": "input", "type": "text"},
        "#email":       {"tag": "input", "type": "email"},
        "#phone":       {"tag": "input", "type": "tel"},
        "input#resume": {"tag": "input", "type": "file"},
        'input[type="submit"][name="submit"]': {"tag": "input", "type": "submit"},
    }
    d = FakeDriver()
    d.page.dom = dom
    d.page.body_text = "Please sign in to apply for this role."

    result = _run(adapter.apply(base_job, profile, tmp_cv, tmp_cover_letter, d))

    assert result.paused_for_review is True
    assert result.submitted is False
    assert "login wall" in (result.error or "").lower()
    assert not any("submit" in c for c in d.page.log.click)


def test_captcha_pauses_for_review(
    adapter, profile, base_job, tmp_cv, tmp_cover_letter,
):
    dom = {
        "#first_name":  {"tag": "input", "type": "text"},
        "#last_name":   {"tag": "input", "type": "text"},
        "#email":       {"tag": "input", "type": "email"},
        "#phone":       {"tag": "input", "type": "tel"},
        "input#resume": {"tag": "input", "type": "file"},
        'input[type="submit"][name="submit"]': {"tag": "input", "type": "submit"},
        'iframe[src*="recaptcha"]': {"tag": "iframe"},
    }
    d = FakeDriver()
    d.page.dom = dom

    result = _run(adapter.apply(base_job, profile, tmp_cv, tmp_cover_letter, d))

    assert result.paused_for_review is True
    assert "captcha" in (result.error or "").lower()
    assert not any("submit" in c for c in d.page.log.click)


def test_missing_cv_file_pauses_for_review(
    adapter, profile, greenhouse_driver, base_job,
):
    result = _run(adapter.apply(
        base_job, profile, "/tmp/__no_cv__.pdf", "", greenhouse_driver,
    ))

    assert result.paused_for_review is True
    assert "resume" in (result.error or "").lower()
    assert not any("submit" in c for c in greenhouse_driver.page.log.click)


def test_missing_career_url_pauses_for_review(
    adapter, profile, greenhouse_driver, tmp_cv,
):
    job = {"id": 1, "title": "X", "company": "Y", "ats_type": "greenhouse"}
    result = _run(adapter.apply(job, profile, tmp_cv, "", greenhouse_driver))

    assert result.paused_for_review is True
    assert "career_url" in (result.error or "").lower()
    assert greenhouse_driver.page.log.goto == []


# ---------------------------------------------------------------------------
# Custom questions
# ---------------------------------------------------------------------------


def test_custom_questions_filled_from_profile(
    adapter, profile, base_job, tmp_cv, tmp_cover_letter,
):
    dom = {
        "#first_name":  {"tag": "input", "type": "text"},
        "#last_name":   {"tag": "input", "type": "text"},
        "#email":       {"tag": "input", "type": "email"},
        "#phone":       {"tag": "input", "type": "tel"},
        "input#resume": {"tag": "input", "type": "file"},
        "input#cover_letter": {"tag": "input", "type": "file"},
        'input[type="submit"][name="submit"]': {"tag": "input", "type": "submit"},
        "question_linkedin": {"tag": "textarea", "label": "LinkedIn URL"},
        "question_years":    {"tag": "textarea", "label": "Years of experience"},
        "question_permit":   {"tag": "textarea", "label": "Are you authorized to work?"},
        "question_other":    {"tag": "textarea", "label": "Why do you want this role?"},
    }
    d = FakeDriver()
    d.page.dom = dom

    result = _run(adapter.apply(base_job, profile, tmp_cv, tmp_cover_letter, d))

    fills = {f["selector"]: f["value"] for f in d.page.log.fill}
    assert fills["#question_linkedin"] == profile.linkedin
    assert fills["#question_years"] == "12"
    assert fills["#question_permit"] == "Yes"
    assert "#question_other" not in fills
    assert result.success is True


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_submit_click_failure_returns_error_not_paused(
    adapter, profile, greenhouse_driver, base_job, tmp_cv, tmp_cover_letter,
):
    greenhouse_driver.page.submit_should_raise = True
    result = _run(adapter.apply(
        base_job, profile, tmp_cv, tmp_cover_letter, greenhouse_driver,
    ))
    assert result.success is False
    assert result.submitted is False
    assert result.paused_for_review is False
    assert "submit" in (result.error or "").lower()


def test_no_submit_selector_found_returns_error(
    adapter, profile, tmp_cv, base_job,
):
    dom = {
        "#first_name": {"tag": "input", "type": "text"},
        "#last_name":  {"tag": "input", "type": "text"},
        "#email":      {"tag": "input", "type": "email"},
        "input#resume": {"tag": "input", "type": "file"},
    }
    d = FakeDriver()
    d.page.dom = dom
    result = _run(adapter.apply(base_job, profile, tmp_cv, "", d))
    assert result.success is False
    assert "submit" in (result.error or "").lower()
