"""Tests for the Workday adapter.

Workday is multi-step; the test seeds a flat DOM and lets the adapter walk
through ``next``-clicks. We assert:
  - happy path submits (one page form here, no actual wizard)
  - login wall pauses for review
  - captcha pauses for review
  - missing CV pauses for review
  - submit failure surfaces an error (no pause)

Run: pytest tests/test_apply_workday.py -v
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict

import pytest

from tests.conftest import apply_base as base_mod, apply_workday  # noqa: E402
from tests.conftest import FakeDriver  # noqa: E402

WorkdayAdapter = apply_workday.WorkdayAdapter


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_workday_is_registered_in_registry():
    assert base_mod.resolve_adapter("workday") is WorkdayAdapter
    assert base_mod.resolve_adapter("WORKDAY") is WorkdayAdapter
    assert WorkdayAdapter.ats_name == "workday"


@pytest.fixture
def adapter():
    return apply_workday.WorkdayAdapter()


def _dom_with_email(login_wall=False, captcha=False, no_submit=False):
    dom = {
        'input[data-automation-id*="legalNameSection_firstName"]': {"tag": "input"},
        'input[data-automation-id*="legalNameSection_lastName"]':  {"tag": "input"},
        'input[data-automation-id="email"]': {"tag": "input"},
        'input[data-automation-id*="phone"]': {"tag": "input"},
        'input[data-automation-id="fileUploadInput"]': {"tag": "input", "type": "file"},
        'button[data-automation-id="bottom-navigation-next-button"]': {"tag": "button"},
        'button[data-automation-id="submit"]': {"tag": "button"},
    }
    if login_wall:
        # Seed each comma-clause from the selector as its own key so the
        # fake's _matches OR-handles the alternation.
        dom['[data-automation-id="signInLink"]'] = {"tag": "a"}
        dom['button[data-automation-id="useMyCurrentPassword"]'] = {"tag": "button"}
    if captcha:
        dom['iframe[src*="recaptcha"]'] = {"tag": "iframe"}
        dom['iframe[src*="hcaptcha"]'] = {"tag": "iframe"}
        dom['div[data-automation-id="captcha"]'] = {"tag": "div"}
    if no_submit:
        dom.pop('button[data-automation-id="submit"]', None)
    return dom


def test_workday_happy_path_submits(adapter, profile, base_job, tmp_cv):
    d = FakeDriver()
    d.page.dom = _dom_with_email()

    result = _run(adapter.apply(base_job, profile, tmp_cv, "", d))

    assert result.success is True
    assert result.submitted is True
    assert result.ats_type == "workday"
    assert result.paused_for_review is False
    assert "resume" in result.fields_filled
    assert any("first_name" in f for f in result.fields_filled)


def test_workday_login_wall_pauses_for_review(adapter, profile, base_job, tmp_cv):
    d = FakeDriver()
    d.page.dom = _dom_with_email(login_wall=True)

    result = _run(adapter.apply(base_job, profile, tmp_cv, "", d))

    assert result.paused_for_review is True
    assert "login" in (result.error or "").lower()


def test_workday_captcha_pauses_for_review(adapter, profile, base_job, tmp_cv):
    d = FakeDriver()
    d.page.dom = _dom_with_email(captcha=True)

    result = _run(adapter.apply(base_job, profile, tmp_cv, "", d))

    assert result.paused_for_review is True
    assert "captcha" in (result.error or "").lower()


def test_workday_missing_cv_pauses_for_review(adapter, profile, base_job):
    d = FakeDriver()
    d.page.dom = _dom_with_email()

    result = _run(adapter.apply(base_job, profile, "/tmp/__no_cv__.pdf", "", d))

    assert result.paused_for_review is True
    assert "resume" in (result.error or "").lower()


def test_workday_no_submit_selector_returns_error(adapter, profile, base_job, tmp_cv):
    d = FakeDriver()
    d.page.dom = _dom_with_email(no_submit=True)

    result = _run(adapter.apply(base_job, profile, tmp_cv, "", d))

    assert result.success is False
    assert result.paused_for_review is False
    assert "submit" in (result.error or "").lower()


def test_workday_missing_career_url_pauses_for_review(adapter, profile, tmp_cv):
    job = {"id": 1, "title": "x", "company": "y", "ats_type": "workday"}
    d = FakeDriver()
    result = _run(adapter.apply(job, profile, tmp_cv, "", d))
    assert result.paused_for_review is True
    assert "career_url" in (result.error or "").lower()
