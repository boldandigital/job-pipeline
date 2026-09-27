"""Tests for the generic fallback adapter.

CRITICAL: this adapter must NEVER auto-submit. Every test below asserts
either ``paused_for_review=True`` or the absence of a submit click.

Run: pytest tests/test_apply_generic.py -v
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest

from tests.conftest import apply_base as base_mod, apply_generic  # noqa: E402
from tests.conftest import FakeDriver  # noqa: E402

GenericAdapter = apply_generic.GenericAdapter


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def adapter():
    return GenericAdapter()


def test_generic_is_registered_in_registry():
    assert base_mod.resolve_adapter("generic") is GenericAdapter
    assert GenericAdapter.ats_name == "generic"


# ---------------------------------------------------------------------------
# CRITICAL: pause_for_review + is_generic_fallback always set
# ---------------------------------------------------------------------------


def test_generic_always_sets_safety_flags(adapter, profile, base_job, tmp_cv):
    """Even on the happy path, generic must NOT auto-submit."""
    d = FakeDriver()
    # Seed with one recognisable field so the adapter doesn't bail early
    # on the no-fields branch.
    d.page.dom = {'input[type="file"]': {"tag": "input", "type": "file"}}

    result = _run(adapter.apply(base_job, profile, tmp_cv, "", d))

    assert result.paused_for_review is True
    assert result.is_generic_fallback is True
    assert base_mod.is_generic_fallback_result(result) is True
    assert result.submitted is False
    # No submit click recorded.
    assert not any("submit" in c.lower() for c in d.page.log.click)


def test_generic_no_career_url_pauses_for_review(adapter, profile, tmp_cv):
    job = {"id": 1, "title": "x", "company": "y", "ats_type": "unknown"}
    d = FakeDriver()
    result = _run(adapter.apply(job, profile, tmp_cv, "", d))
    assert result.paused_for_review is True
    assert result.is_generic_fallback is True


def test_generic_no_form_fields_still_pauses(adapter, profile, base_job, tmp_cv):
    d = FakeDriver()
    d.page.dom = {"body": {"tag": "body"}}
    result = _run(adapter.apply(base_job, profile, tmp_cv, "", d))
    assert result.paused_for_review is True
    assert result.is_generic_fallback is True
    assert "no recognisable form fields" in (result.error or "")


def test_generic_captcha_pauses(adapter, profile, base_job, tmp_cv):
    d = FakeDriver()
    d.page.dom = {
        'iframe[src*="recaptcha"]': {"tag": "iframe"},
        'input[type="file"]': {"tag": "input"},
    }
    result = _run(adapter.apply(base_job, profile, tmp_cv, "", d))
    assert result.paused_for_review is True
    assert "captcha" in (result.error or "").lower()


def test_generic_login_wall_pauses(adapter, profile, base_job, tmp_cv):
    d = FakeDriver()
    d.page.dom = {'input[type="file"]': {"tag": "input"}}
    d.page.body_text = "Please sign in to apply for this role."
    result = _run(adapter.apply(base_job, profile, tmp_cv, "", d))
    assert result.paused_for_review is True
    assert "login" in (result.error or "").lower()


def test_generic_captures_pre_and_post_screenshots(
    adapter, profile, base_job, tmp_cv,
):
    d = FakeDriver()
    d.page.dom = {'input[type="file"]': {"tag": "input", "type": "file"}}

    result = _run(adapter.apply(base_job, profile, tmp_cv, "", d))

    screenshot_calls = d.page.log.screenshots
    assert len(screenshot_calls) >= 2
    # First is pre-fill, second is post-fill.
    assert screenshot_calls[0].endswith(".png")
    assert any("filled" in s for s in screenshot_calls)


def test_generic_uploads_resume_when_file_input_present(
    adapter, profile, base_job, tmp_cv,
):
    d = FakeDriver()
    d.page.dom = {'input[type="file"]': {"tag": "input", "type": "file"}}

    result = _run(adapter.apply(base_job, profile, tmp_cv, "", d))

    uploads = [u["file_path"] for u in d.page.log.upload_file]
    assert tmp_cv in uploads
    assert "resume" in result.fields_filled
