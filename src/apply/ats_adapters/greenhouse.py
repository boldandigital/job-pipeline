"""Greenhouse ATS adapter.

Greenhouse (boards.greenhouse.io) is the most common ATS in Lars's target
segment — Stripe, Shopify, Airbnb, Notion, Linear, etc. The apply flow
looks like:

    1. GET <career_url>                  (e.g. boards.greenhouse.io/stripe)
    2. Click the "Apply" button          (#apply_button or a[href*="apply"])
    3. Fill personal info                 (#first_name, #last_name, #email, #phone)
    4. Upload resume                      (input#resume)
    5. Optionally upload cover letter     (input#cover_letter, when present)
    6. Fill custom questions (textareas)
    7. Submit                             (input[name="submit"], button[type="submit"])

We deliberately use ID selectors when available (more stable than name
attributes which Greenhouse occasionally obfuscates), and fall back to
name-based selectors with a clear error if neither resolves.

Submission: greenhouse always submits (it's a known ATS). Only the generic
fallback path pauses for human review.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping

from .base import (
    ApplyResult,
    Driver,
    Profile,
    apply_pause_for_review,
    register_adapter,
)

log = logging.getLogger("apply.greenhouse")


# ---------------------------------------------------------------------------
# Selectors — Greenhouse IDs are stable across tenants (the platform
# standardises them), so a single selector map covers every board.
# ---------------------------------------------------------------------------

_SELECTORS = {
    "apply_button":  '#apply_button',
    "first_name":    '#first_name',
    "last_name":     '#last_name',
    "email":         '#email',
    "phone":         '#phone',
    "resume":        'input#resume',
    "cover_letter":  'input#cover_letter',
    "submit":        'input[type="submit"][name="submit"], button[type="submit"]#submit',
    "question":      'textarea[id^="question_"]',
    "login_wall":    'form#login, input[name="username"], .login-required',
    "captcha":       'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], .g-recaptcha',
}

_LOGIN_WALL_SENTINELS = (
    "sign in to apply",
    "log in to apply",
    "login required",
    "please log in",
)


async def _safe_fill(page, selector: str, value: str) -> bool:
    """Fill only if the field is present. Returns True if we filled."""
    if not value:
        return False
    try:
        if not await page.exists(selector):
            return False
        await page.fill(selector, value)
        return True
    except Exception as exc:  # noqa: BLE001 — driver raises are adapter-internal
        log.warning("greenhouse: fill %s failed: %s", selector, exc)
        return False


async def _wait_or_timeout(page, selector: str, timeout_ms: int = 10_000) -> bool:
    try:
        await page.wait_for_selector(selector, timeout_ms=timeout_ms)
        return True
    except Exception:  # noqa: BLE001
        return False


class GreenhouseAdapter:
    """Fills and submits Greenhouse application forms.

    Conforms to :class:`ApplyAdapter` via duck-typing (the base class is a
    runtime-checkable Protocol).
    """

    ats_name = "greenhouse"

    async def apply(
        self,
        job: Mapping[str, Any],
        profile: Profile,
        cv_path: str,
        cover_letter_path: str,
        driver: Driver,
    ) -> ApplyResult:
        page = driver.page
        job_id = job.get("id")
        career_url = job.get("career_url") or job.get("url") or ""
        fields_filled: List[str] = []

        if not career_url:
            return apply_pause_for_review(
                "no career_url for job", ats_type=self.ats_name, job_id=job_id,
            )

        try:
            await driver.open(career_url)
        except Exception as exc:  # noqa: BLE001
            return ApplyResult(
                success=False, error=f"open() failed: {exc}",
                ats_type=self.ats_name, job_id=job_id,
            )

        # Login wall / captcha guard — never try to bypass.
        for sentinel in _LOGIN_WALL_SENTINELS:
            try:
                body_text = await page.text("body")
            except Exception:
                body_text = ""
            if sentinel in body_text.lower():
                return apply_pause_for_review(
                    f"login wall detected ({sentinel!r})",
                    ats_type=self.ats_name, job_id=job_id,
                )
        if await page.exists(_SELECTORS["captcha"]):
            return apply_pause_for_review(
                "captcha detected", ats_type=self.ats_name, job_id=job_id,
            )

        # Click "Apply" if the board shows a landing page before the form.
        if await page.exists(_SELECTORS["apply_button"]):
            try:
                await page.click(_SELECTORS["apply_button"])
                await _wait_or_timeout(page, _SELECTORS["first_name"])
            except Exception as exc:  # noqa: BLE001
                log.warning("greenhouse: apply button click failed: %s", exc)

        # Required personal info.
        if not await _wait_or_timeout(page, _SELECTORS["first_name"]):
            return apply_pause_for_review(
                "first_name selector never appeared — form layout unexpected",
                ats_type=self.ats_name, job_id=job_id,
            )

        if await _safe_fill(page, _SELECTORS["first_name"], profile.first_name):
            fields_filled.append("first_name")
        if await _safe_fill(page, _SELECTORS["last_name"], profile.last_name):
            fields_filled.append("last_name")
        if await _safe_fill(page, _SELECTORS["email"], profile.email):
            fields_filled.append("email")
        if await _safe_fill(page, _SELECTORS["phone"], profile.phone):
            fields_filled.append("phone")

        # Resume upload — required by greenhouse; missing CV path is a hard fail.
        if cv_path and Path(cv_path).exists() and await page.exists(_SELECTORS["resume"]):
            await page.upload_file(_SELECTORS["resume"], cv_path)
            fields_filled.append("resume")
        else:
            return apply_pause_for_review(
                "resume file missing or selector not found",
                ats_type=self.ats_name, job_id=job_id,
            )

        # Cover letter — optional on Greenhouse; only upload when present.
        if (
            cover_letter_path
            and Path(cover_letter_path).exists()
            and await page.exists(_SELECTORS["cover_letter"])
        ):
            await page.upload_file(_SELECTORS["cover_letter"], cover_letter_path)
            fields_filled.append("cover_letter")

        # Custom questions — best-effort; nothing critical lives in these.
        questions = await page.query_all(_SELECTORS["question"])
        for q in questions:
            qid = q.get("id") or q.get("name") or ""
            qtext = q.get("label", "").lower()
            value = ""
            if "linkedin" in qtext:
                value = profile.linkedin
            elif "website" in qtext or "portfolio" in qtext:
                value = profile.portfolio
            elif "phone" in qtext:
                value = profile.phone
            elif "years" in qtext or "experience" in qtext:
                value = str(profile.extras.get("years_experience", ""))
            elif "authorized" in qtext or "sponsorship" in qtext:
                value = "Yes"  # EU citizen — safe default for Lars
            elif "relocate" in qtext or "relocation" in qtext:
                value = "Yes"
            if qid and value:
                await _safe_fill(page, f"#{qid}", value)
                fields_filled.append(qid)

        # Screenshot the filled form before clicking submit (audit trail).
        screenshot_path = ""
        try:
            screenshot_path = f"screenshots/greenhouse-{job_id or 'unknown'}.png"
            Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(screenshot_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("greenhouse: screenshot failed: %s", exc)

        # Submit.
        if not await page.exists(_SELECTORS["submit"]):
            return ApplyResult(
                success=False, error="submit selector not found",
                screenshot_path=screenshot_path,
                ats_type=self.ats_name, job_id=job_id, fields_filled=fields_filled,
            )

        try:
            await page.click(_SELECTORS["submit"])
        except Exception as exc:  # noqa: BLE001
            return ApplyResult(
                success=False, error=f"submit click failed: {exc}",
                screenshot_path=screenshot_path,
                ats_type=self.ats_name, job_id=job_id, fields_filled=fields_filled,
            )

        # Brief settle — gives the post-submit page time to render.
        await asyncio.sleep(0.5)
        try:
            await page.screenshot(screenshot_path.replace(".png", "-post.png"))
        except Exception:  # noqa: BLE001
            pass

        return ApplyResult(
            success=True,
            screenshot_path=screenshot_path,
            submitted=True,
            ats_type=self.ats_name,
            job_id=job_id,
            fields_filled=fields_filled,
        )


# Auto-register on import so resolve_adapter("greenhouse") finds it.
register_adapter(GreenhouseAdapter.ats_name, GreenhouseAdapter)
