"""Workday ATS adapter.

Workday (myworkdayjobs.com) is the enterprise-flavored ATS — large corps
like Siemens, Allianz, BMW, Bayer, Deutsche Bank, etc. The apply flow is a
multi-step wizard:

    1. GET  <career_url>                          (often a tenant URL)
    2. Click "Apply"                              (a[data-automation-id="applyButton"])
    3. Either: "Sign in" / "Apply manually"       (a[data-automation-id*="apply"])
    4. Step 1 — Account / contact info
    5. Step 2 — Resume upload (drop or pick)
    6. Step 3 — Cover letter (optional)
    7. Step 4 — Custom questions
    8. Step 5 — Voluntary disclosures / self-ID
    9. Submit (button[data-automation-id="submit"])

Workday uses ``data-automation-id`` attributes heavily — these are stable
across tenants and are the right selectors to lean on. We never auto-skip
the optional sign-in (some tenants require it); if the wizard demands an
account we pause_for_review.

Submission: workday submits when the wizard completes successfully. Like
greenhouse, only the generic fallback path pauses for human review.
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

log = logging.getLogger("apply.workday")


_SELECTORS = {
    "apply_button":  'a[data-automation-id="applyButton"]',
    "apply_link":    'a[data-automation-id*="apply"]',
    "first_name":    'input[data-automation-id*="legalNameSection_firstName"]',
    "last_name":     'input[data-automation-id*="legalNameSection_lastName"]',
    "email":         'input[data-automation-id="email"]',
    "phone":         'input[data-automation-id*="phone"]',
    "resume":        'input[data-automation-id="fileUploadInput"]',
    "cover_letter":  'input[data-automation-id="coverLetterFileUploadInput"]',
    "next":          'button[data-automation-id="bottom-navigation-next-button"]',
    "submit":        'button[data-automation-id="submit"]',
    "review":        'button[data-automation-id="review"]',
    "question":      'textarea[data-automation-id^="formField"], input[data-automation-id^="formField"]',
    "login_wall":    '[data-automation-id="signInLink"], button[data-automation-id="useMyCurrentPassword"]',
    "captcha":       'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], div[data-automation-id="captcha"]',
}


async def _wait_or_timeout(page, selector: str, timeout_ms: int = 8_000) -> bool:
    try:
        await page.wait_for_selector(selector, timeout_ms=timeout_ms)
        return True
    except Exception:  # noqa: BLE001
        return False


async def _safe_fill(page, selector: str, value: str) -> bool:
    if not value:
        return False
    if not await page.exists(selector):
        return False
    try:
        await page.fill(selector, value)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("workday: fill %s failed: %s", selector, exc)
        return False


async def _advance(page, next_selector: str = _SELECTORS["next"], max_steps: int = 6):
    """Click the Workday 'next' button until we run out of wizard pages.

    Workday wizards can have 3-6 steps. We attempt ``max_steps`` clicks and
    stop when the next selector disappears (we're on the final review page)
    or when we can't make further progress.
    """
    for _ in range(max_steps):
        if not await page.exists(next_selector):
            return True  # we're past the wizard (review/submit page)
        try:
            await page.click(next_selector)
            # Tiny pause so the next step's fields render.
            await asyncio.sleep(0.05)
        except Exception as exc:  # noqa: BLE001
            log.warning("workday: next click failed: %s", exc)
            return False
    return False


class WorkdayAdapter:
    """Workday multi-step wizard apply.

    Conforms to :class:`ApplyAdapter` via duck-typing.
    """

    ats_name = "workday"

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

        # Captcha / login wall guards.
        if await page.exists(_SELECTORS["captcha"]):
            return apply_pause_for_review(
                "captcha detected", ats_type=self.ats_name, job_id=job_id,
            )
        if await page.exists(_SELECTORS["login_wall"]):
            return apply_pause_for_review(
                "login wall — workday account required",
                ats_type=self.ats_name, job_id=job_id,
            )

        # If we're on a landing page, click "Apply".
        if await page.exists(_SELECTORS["apply_button"]):
            try:
                await page.click(_SELECTORS["apply_button"])
            except Exception as exc:  # noqa: BLE001
                log.warning("workday: apply button click failed: %s", exc)
        elif await page.exists(_SELECTORS["apply_link"]):
            try:
                await page.click(_SELECTORS["apply_link"])
            except Exception as exc:  # noqa: BLE001
                log.warning("workday: apply link click failed: %s", exc)

        # Step 1: legal-name section.
        if not await _wait_or_timeout(page, _SELECTORS["first_name"], timeout_ms=12_000):
            return apply_pause_for_review(
                "first_name selector never appeared — wizard layout unexpected",
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

        # Step 2: resume upload — required.
        if not (cv_path and Path(cv_path).exists()):
            return apply_pause_for_review(
                "resume file missing",
                ats_type=self.ats_name, job_id=job_id,
            )
        if await page.exists(_SELECTORS["resume"]):
            await page.upload_file(_SELECTORS["resume"], cv_path)
            fields_filled.append("resume")
        else:
            return apply_pause_for_review(
                "resume upload selector not found",
                ats_type=self.ats_name, job_id=job_id,
            )

        # Step 3: cover letter — optional.
        if (
            cover_letter_path
            and Path(cover_letter_path).exists()
            and await page.exists(_SELECTORS["cover_letter"])
        ):
            await page.upload_file(_SELECTORS["cover_letter"], cover_letter_path)
            fields_filled.append("cover_letter")

        # Custom questions on the form field step.
        questions = await page.query_all(_SELECTORS["question"])
        for q in questions:
            qid = q.get("id") or q.get("name") or ""
            qtext = (q.get("label", "") or "").lower()
            value = ""
            if "linkedin" in qtext:
                value = profile.linkedin
            elif "website" in qtext or "portfolio" in qtext:
                value = profile.portfolio
            elif "years" in qtext or "experience" in qtext:
                value = str(profile.extras.get("years_experience", ""))
            elif "authorized" in qtext or "sponsorship" in qtext:
                value = "Yes"
            elif "relocate" in qtext or "relocation" in qtext:
                value = "Yes"
            if qid and value:
                await _safe_fill(page, f"#{qid}", value)
                fields_filled.append(qid)

        # Advance through the wizard to the review/submit page.
        await _advance(page)

        # Audit-trail screenshot.
        screenshot_path = ""
        try:
            screenshot_path = f"screenshots/workday-{job_id or 'unknown'}.png"
            Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(screenshot_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("workday: screenshot failed: %s", exc)

        # Final submit (only if submit button is present).
        if not await page.exists(_SELECTORS["submit"]):
            return ApplyResult(
                success=False, error="submit selector not found on review page",
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

        await asyncio.sleep(0.3)
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


register_adapter(WorkdayAdapter.ats_name, WorkdayAdapter)
