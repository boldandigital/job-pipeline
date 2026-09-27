"""Lever ATS adapter.

Lever (jobs.lever.co) is common at mid-to-late-stage tech startups
(Shopify, Box, etc., pre-Greenhouse migration). The apply flow is simpler
than Workday's wizard:

    1. GET  <career_url>                  (jobs.lever.co/<tenant>)
    2. Click "Apply for this job"          (a[class*="apply"])
    3. Single-page form with:
         - Full name, email, phone
         - Resume upload (input[name="resume"])
         - Optional cover letter (textarea[name="comments"])
         - Optional custom questions (textarea[name^="cards["])
    4. Submit (button:contains("Submit"))

Lever keeps selectors simple — no ``data-automation-id`` soup. Form fields
live inside ``<input>`` and ``<textarea>`` elements with stable ``name``
attributes.
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

log = logging.getLogger("apply.lever")


_SELECTORS = {
    "apply_button":  'a[class*="apply"], a[href*="/apply"]',
    "name":          'input[name="name"], input[name="fullName"]',
    "first_name":    'input[name="firstName"]',
    "last_name":     'input[name="lastName"]',
    "email":         'input[name="email"]',
    "phone":         'input[name="phone"], input[type="tel"]',
    "resume":        'input[name="resume"], input[type="file"][name*="resume"]',
    "comments":      'textarea[name="comments"]',
    "submit":        'button[type="submit"], input[type="submit"]',
    "question":      'textarea[name^="cards["], input[name^="cards["]',
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
        log.warning("lever: fill %s failed: %s", selector, exc)
        return False


class LeverAdapter:
    """Lever single-page form apply."""

    ats_name = "lever"

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

        # Click "Apply for this job" if the board shows a landing page.
        if await page.exists(_SELECTORS["apply_button"]):
            try:
                await page.click(_SELECTORS["apply_button"])
                await _wait_or_timeout(page, _SELECTORS["email"])
            except Exception as exc:  # noqa: BLE001
                log.warning("lever: apply click failed: %s", exc)

        # Some Lever forms have a single `name` field, others split into
        # first_name/last_name. Handle both.
        if await page.exists(_SELECTORS["name"]):
            full = profile.display_name()
            await _safe_fill(page, _SELECTORS["name"], full)
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

        # Resume upload — required.
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

        # Lever has a `comments` textarea that doubles as the cover letter.
        if cover_letter_path and Path(cover_letter_path).exists():
            try:
                cl_text = Path(cover_letter_path).read_text(encoding="utf-8", errors="ignore")[:3000]
                await _safe_fill(page, _SELECTORS["comments"], cl_text)
                fields_filled.append("comments")
            except Exception as exc:  # noqa: BLE001
                log.warning("lever: comments fill failed: %s", exc)

        # Custom questions — best-effort.
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
            screenshot_path = f"screenshots/lever-{job_id or 'unknown'}.png"
            Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(screenshot_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("lever: screenshot failed: %s", exc)

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


register_adapter(LeverAdapter.ats_name, LeverAdapter)
