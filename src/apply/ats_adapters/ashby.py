"""Ashby ATS adapter.

Ashby (jobs.ashbyhq.com) is common at modern startups (Linear, Ramp,
Notion, etc.). Apply flow is one page with progressive disclosure:

    1. GET  <career_url>                          (jobs.ashbyhq.com/<tenant>)
    2. Click "Apply for this job"                 (button:contains("Apply"))
    3. Single form with sections:
         - Name, email, phone
         - Resume upload (input[type="file"][name="resumeFile"])
         - Optional cover letter (textarea[name="coverLetter"])
         - Optional custom questions (textarea[name^="customField-"])
         - Voluntary self-ID (often skipped)
    4. Submit (button[type="submit"]:contains("Submit application"))

Ashby uses ``name`` attributes prefixed with the section — keep the selector
map aligned with the actual DOM. Same shape as Lever: simpler than Workday,
no multi-step wizard.
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

log = logging.getLogger("apply.ashby")


_SELECTORS = {
    "apply_button":  'button:contains("Apply"), a:contains("Apply for this job")',
    "name":          'input[name="name"], input[name="fullName"]',
    "first_name":    'input[name="firstName"]',
    "last_name":     'input[name="lastName"]',
    "email":         'input[name="email"]',
    "phone":         'input[name="phone"], input[type="tel"]',
    "resume":        'input[type="file"][name="resumeFile"], input[type="file"][name="resume"]',
    "cover_letter":  'textarea[name="coverLetter"]',
    "submit":        'button[type="submit"]',
    "question":      'textarea[name^="customField-"], input[name^="customField-"]',
    "login_wall":    'input[name="signInEmail"], form#login',
    "captcha":       'iframe[src*="recaptcha"], iframe[src*="hcaptcha"]',
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
        log.warning("ashby: fill %s failed: %s", selector, exc)
        return False


class AshbyAdapter:
    """Ashby single-page form apply."""

    ats_name = "ashby"

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

        if await page.exists(_SELECTORS["captcha"]):
            return apply_pause_for_review(
                "captcha detected", ats_type=self.ats_name, job_id=job_id,
            )
        if await page.exists(_SELECTORS["login_wall"]):
            return apply_pause_for_review(
                "login wall detected", ats_type=self.ats_name, job_id=job_id,
            )

        # Ashby sometimes shows the apply form directly; sometimes the user
        # has to click "Apply for this job" first.
        if await page.exists(_SELECTORS["apply_button"]):
            try:
                await page.click(_SELECTORS["apply_button"])
                await _wait_or_timeout(page, _SELECTORS["email"])
            except Exception as exc:  # noqa: BLE001
                log.warning("ashby: apply click failed: %s", exc)

        # Single name field, OR split first/last — handle both.
        if await page.exists(_SELECTORS["name"]):
            await _safe_fill(page, _SELECTORS["name"], profile.display_name())
            fields_filled.append("name")
        else:
            await _safe_fill(page, _SELECTORS["first_name"], profile.first_name)
            await _safe_fill(page, _SELECTORS["last_name"], profile.last_name)
            fields_filled.extend(["first_name", "last_name"])

        if not await _wait_or_timeout(page, _SELECTORS["email"]):
            return apply_pause_for_review(
                "email selector never appeared — form layout unexpected",
                ats_type=self.ats_name, job_id=job_id,
            )

        if await _safe_fill(page, _SELECTORS["email"], profile.email):
            fields_filled.append("email")
        if await _safe_fill(page, _SELECTORS["phone"], profile.phone):
            fields_filled.append("phone")

        # Resume required.
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

        # Cover letter — paste text if path given.
        if (
            cover_letter_path
            and Path(cover_letter_path).exists()
            and await page.exists(_SELECTORS["cover_letter"])
        ):
            try:
                cl_text = Path(cover_letter_path).read_text(encoding="utf-8", errors="ignore")[:3000]
                await _safe_fill(page, _SELECTORS["cover_letter"], cl_text)
                fields_filled.append("cover_letter")
            except Exception as exc:  # noqa: BLE001
                log.warning("ashby: cover letter fill failed: %s", exc)

        # Custom questions.
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

        screenshot_path = ""
        try:
            screenshot_path = f"screenshots/ashby-{job_id or 'unknown'}.png"
            Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(screenshot_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("ashby: screenshot failed: %s", exc)

        if not await page.exists(_SELECTORS["submit"]):
            return ApplyResult(
                success=False, error="submit selector not found",
                screenshot_path=screenshot_path,
                ats_type=self.ats_name, job_id=job_id, fields_filled=fields_filled,
            )

        # SAFETY CONTRACT (memory rule): NEVER auto-submit. Pause here so
        # the human can review the form, take their own screenshot, and
        # click Submit themselves. The driver remains open.
        log.info(
            "ashby: form filled, fields=%s, screenshot=%s — PAUSING for human review "
            "(no auto-submit per memory rule)",
            fields_filled, screenshot_path,
        )
        return apply_pause_for_review(
            f"form filled, awaiting human submit click (fields: {', '.join(fields_filled)})",
            ats_type=self.ats_name, job_id=job_id,
            screenshot_path=screenshot_path,
            fields_filled=fields_filled,
        )

# register_adapter stays below
register_adapter(AshbyAdapter.ats_name, AshbyAdapter)
