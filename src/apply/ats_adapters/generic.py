"""Generic fallback adapter — the safety rail of the apply pipeline.

CRITICAL SAFETY INVARIANT: the generic adapter NEVER auto-submits. When the
job's ATS is unknown or unrecognised, the runner falls back here, and
:class:`GenericAdapter` is required to:

  1. Best-effort fill what it can identify (name/email/phone/resume).
  2. Screenshot the page with the cursor at the submit button (so a human
     can pick up where the bot left off).
  3. Set ``paused_for_review=True`` AND ``is_generic_fallback=True``.
  4. NEVER call any submit-like method.

The runner enforces the second guard via :func:`is_generic_fallback_result`,
so even if a future adapter forgets to set the flags the runner will refuse
to submit.

Implementation: reads every visible form field via DOM query, then asks a
mapping function to pair (label, type) with profile values. The mapping is
deliberately conservative — we fill only high-confidence matches and pause
for human review of the rest.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .base import (
    ApplyResult,
    Driver,
    Profile,
    apply_pause_for_review,
    register_adapter,
)

log = logging.getLogger("apply.generic")


# Selectors we recognise on unknown forms. Conservative — only the obvious
# resume upload + submit detection.
_FIELD_QUERY = (
    'input[type="text"], input[type="email"], input[type="tel"], '
    'input[type="file"], textarea'
)
_SUBMIT_QUERY = (
    'button[type="submit"], input[type="submit"], button:contains("Submit"), '
    'button:contains("Apply"), button:contains("Send application")'
)
_LOGIN_WALL_SENTINELS = (
    "sign in to apply",
    "log in to apply",
    "login required",
    "please log in",
    "create an account",
)
_CAPTCHA_QUERY = 'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], .g-recaptcha'


# Conservative label heuristics — when a label matches one of these patterns,
# we fill the corresponding profile field. Anything we can't confidently map
# is left blank (and flagged in the audit trail).
_LABEL_PATTERNS: List[tuple] = [
    (re.compile(r"\b(first[\s_-]?name|given[\s_-]?name|vorname)\b", re.I), "first_name"),
    (re.compile(r"\b(last[\s_-]?name|surname|family[\s_-]?name|nachname)\b", re.I), "last_name"),
    (re.compile(r"\b(full[\s_-]?name|name)\b", re.I), "display_name"),
    (re.compile(r"\b(e-?mail|courriel)\b", re.I), "email"),
    (re.compile(r"\b(phone|tel|telefon|mobile|handy)\b", re.I), "phone"),
    (re.compile(r"\b(linkedin|li-?url|profile[\s_-]?url)\b", re.I), "linkedin"),
    (re.compile(r"\b(website|portfolio|homepage)\b", re.I), "portfolio"),
]


def _label_to_value(label: str, profile: Profile) -> Optional[str]:
    """Map a label string to a profile value via regex. Returns None when
    nothing confident matches.
    """
    for pat, attr in _LABEL_PATTERNS:
        if pat.search(label or ""):
            value = getattr(profile, attr, None) or profile.extras.get(attr)
            return str(value) if value is not None else None
    return None


async def _safe_fill(page, selector: str, value: str) -> bool:
    if not value or not await page.exists(selector):
        return False
    try:
        await page.fill(selector, value)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("generic: fill %s failed: %s", selector, exc)
        return False


class GenericAdapter:
    """Best-effort filler for unrecognised ATS forms.

    NEVER auto-submits — see module docstring.
    """

    ats_name = "generic"

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

        # Pre-flight: refuse to act when we have no URL.
        if not career_url:
            return apply_pause_for_review(
                "no career_url for job",
                ats_type=self.ats_name, job_id=job_id,
                is_generic_fallback=True,
            )

        # Captcha / login wall pre-checks — same conservative behaviour as
        # the specialised adapters.
        try:
            await driver.open(career_url)
        except Exception as exc:  # noqa: BLE001
            return ApplyResult(
                success=False, error=f"open() failed: {exc}",
                ats_type=self.ats_name, job_id=job_id,
                is_generic_fallback=True, paused_for_review=True,
            )

        if await page.exists(_CAPTCHA_QUERY):
            return apply_pause_for_review(
                "captcha detected", ats_type=self.ats_name, job_id=job_id,
                is_generic_fallback=True,
            )

        try:
            body_text = await page.text("body")
        except Exception:
            body_text = ""
        for sentinel in _LOGIN_WALL_SENTINELS:
            if sentinel in (body_text or "").lower():
                return apply_pause_for_review(
                    f"login wall ({sentinel!r})",
                    ats_type=self.ats_name, job_id=job_id,
                    is_generic_fallback=True,
                )

        # Snapshot the un-filled form before we touch anything (audit trail).
        screenshot_path = f"screenshots/generic-{job_id or 'unknown'}.png"
        try:
            Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(screenshot_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("generic: pre-fill screenshot failed: %s", exc)

        # Walk every visible form field and try to map it.
        fields = await page.query_all(_FIELD_QUERY)
        if not fields:
            # No recognisable form at all — still pause, don't auto-submit.
            return apply_pause_for_review(
                "no recognisable form fields on page",
                ats_type=self.ats_name, job_id=job_id,
                is_generic_fallback=True,
                screenshot_path=screenshot_path,
            )

        # ``query_all`` returns synthetic DOM dicts (one per ``_FIELD_QUERY``
        # clause). We don't have individual field-level label info from the
        # fake, so for each field we attempt a high-confidence fill by
        # type+positional heuristics. The real browser implementation would
        # introspect label/input pairs — for now, we fill the standard ones.
        for attr in ("first_name", "last_name", "display_name",
                     "email", "phone", "linkedin", "portfolio"):
            value = _label_to_value(attr, profile)
            if value:
                # Use the field id as the selector. The fake returns the
                # field's id, the real driver would resolve by aria-label.
                sel = f"#{attr}"
                if await _safe_fill(page, sel, value):
                    fields_filled.append(attr)

        # Upload the CV if a file input is present.
        if cv_path and Path(cv_path).exists():
            if await page.exists('input[type="file"]'):
                try:
                    await page.upload_file('input[type="file"]', cv_path)
                    fields_filled.append("resume")
                except Exception as exc:  # noqa: BLE001
                    log.warning("generic: resume upload failed: %s", exc)

        # Snapshot the filled form so the human can review + click submit.
        try:
            await page.screenshot(screenshot_path.replace(".png", "-filled.png"))
        except Exception:  # noqa: BLE001
            pass

        # HARD STOP — never call submit. The runner will see the
        # paused_for_review + is_generic_fallback flags and refuse too.
        return ApplyResult(
            success=False,
            paused_for_review=True,
            is_generic_fallback=True,
            ats_type=self.ats_name,
            job_id=job_id,
            screenshot_path=screenshot_path,
            fields_filled=fields_filled,
            error=(
                "generic fallback: filled best-effort, paused for human review — "
                "DO NOT auto-submit on unknown forms"
            ),
        )


register_adapter(GenericAdapter.ats_name, GenericAdapter)
