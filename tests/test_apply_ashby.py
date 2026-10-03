"""Tests for the Ashby adapter.

Covers:
  - happy path with combined name field
  - happy path with split first/last name fields
  - login wall pauses for review
  - captcha pauses for review
  - missing CV pauses for review
  - submit failure surfaces an error
  - registered in ATS_REGISTRY

Run: pytest tests/test_apply_ashby.py -v
"""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import apply_base as base_mod, apply_ashby  # noqa: E402
from tests.conftest import FakeDriver  # noqa: E402

AshbyAdapter = apply_ashby.AshbyAdapter


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_ashby_is_registered_in_registry():
    assert base_mod.resolve_adapter("ashby") is AshbyAdapter
    assert AshbyAdapter.ats_name == "ashby"


def _dom(name_combined=True, login_wall=False, captcha=False, no_submit=False):
    dom = {
        'input[name="email"]': {"tag": "input"},
        'input[name="phone"]': {"tag": "input"},
        'input[type="tel"]': {"tag": "input"},
        'input[type="file"][name="resumeFile"]': {"tag": "input", "type": "file"},
        'input[type="file"][name="resume"]': {"tag": "input", "type": "file"},
        'button[type="submit"]': {"tag": "button"},
    }
    if name_combined:
        dom['input[name="name"]'] = {"tag": "input"}
        dom['input[name="fullName"]'] = {"tag": "input"}
    else:
        dom['input[name="firstName"]'] = {"tag": "input"}
        dom['input[name="lastName"]'] = {"tag": "input"}
    if login_wall:
        dom['input[name="signInEmail"]'] = {"tag": "input"}
        dom['form#login'] = {"tag": "form"}
    if captcha:
        dom['iframe[src*="recaptcha"]'] = {"tag": "iframe"}
        dom['iframe[src*="hcaptcha"]'] = {"tag": "iframe"}
    if no_submit:
        dom.pop('button[type="submit"]', None)
    return dom


def test_ashby_happy_path_combined_name(
    adapter, profile, base_job, tmp_cv, tmp_cover_letter,
):
    # Override the conftest fixture to return an AshbyAdapter instead.
    ashby = AshbyAdapter()
    d = FakeDriver()
    d.page.dom = _dom(name_combined=True)

    result = _run(ashby.apply(
        base_job, profile, tmp_cv, tmp_cover_letter, d,
    ))
    # Ashby now pauses for human submit (safety rule per memory).
    assert result.success is False
    assert result.submitted is False
    assert result.paused_for_review is True
    assert result.ats_type == "ashby"
    assert "name" in result.fields_filled


def test_ashby_happy_path_split_name(
    adapter, profile, base_job, tmp_cv, tmp_cover_letter,
):
    ashby = AshbyAdapter()
    d = FakeDriver()
    d.page.dom = _dom(name_combined=False)

    result = _run(ashby.apply(
        base_job, profile, tmp_cv, tmp_cover_letter, d,
    ))
    # Ashby pauses for human submit (safety rule).
    assert result.success is False
    assert result.submitted is False
    assert result.paused_for_review is True
    assert "first_name" in result.fields_filled
    assert "last_name" in result.fields_filled


def test_ashby_login_wall_pauses_for_review(adapter, profile, base_job, tmp_cv):
    ashby = AshbyAdapter()
    d = FakeDriver()
    d.page.dom = _dom(login_wall=True)

    result = _run(ashby.apply(base_job, profile, tmp_cv, "", d))

    assert result.paused_for_review is True
    assert "login" in (result.error or "").lower()


def test_ashby_captcha_pauses_for_review(adapter, profile, base_job, tmp_cv):
    ashby = AshbyAdapter()
    d = FakeDriver()
    d.page.dom = _dom(captcha=True)

    result = _run(ashby.apply(base_job, profile, tmp_cv, "", d))

    assert result.paused_for_review is True
    assert "captcha" in (result.error or "").lower()


def test_ashby_missing_cv_pauses_for_review(adapter, profile, base_job):
    ashby = AshbyAdapter()
    d = FakeDriver()
    d.page.dom = _dom()

    result = _run(ashby.apply(base_job, profile, "/tmp/__no_cv__.pdf", "", d))

    assert result.paused_for_review is True


def test_ashby_no_submit_returns_error(adapter, profile, base_job, tmp_cv):
    ashby = AshbyAdapter()
    d = FakeDriver()
    d.page.dom = _dom(no_submit=True)

    result = _run(ashby.apply(base_job, profile, tmp_cv, "", d))

    assert result.success is False
    assert result.paused_for_review is False
    assert "submit" in (result.error or "").lower()


def test_ashby_missing_career_url_pauses_for_review(adapter, profile, tmp_cv):
    ashby = AshbyAdapter()
    job = {"id": 1, "title": "x", "company": "y", "ats_type": "ashby"}
    d = FakeDriver()
    result = _run(ashby.apply(job, profile, tmp_cv, "", d))
    assert result.paused_for_review is True
    assert "career_url" in (result.error or "").lower()
