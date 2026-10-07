"""
Interactive session capture — drives a real browser so the user logs in
themselves, then harvests the cookies.

Why this exists
---------------
The alternative to storing a session is replaying a username/password on
every apply. LinkedIn and Indeed both flag that behaviour, and XING rate-
limits it. So we never do it: we open a real browser at the platform's login
page, the human signs in, and we snapshot the resulting cookies.

Flow::

    bin/captainauth login lars --platform xing
      → opens Chromium at https://login.xing.com/
      → waits (up to --timeout) while the human types their password
      → verifies a session cookie actually appeared
      → stores it encrypted via CredentialStore.put()
      → closes the browser

Everything stays on this machine. The browser talks to the platform; this
process talks only to the local envelope file.

Detect-login heuristic
----------------------
Rather than scraping the DOM for "logged in" markers (which change weekly),
we check whether the cookie jar grew past its pre-login size AND contains a
cookie whose domain matches the platform spec. That is stable, cheap, and
does not require the page to render.

The user can also force completion with --force when a platform uses an
unusual session cookie name.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from .credentials import Credential, CredentialStore, redact
from .platforms import PlatformSpec, get_platform

log = logging.getLogger("auth.capture")

DEFAULT_TIMEOUT = 300  # seconds a human is given to sign in
POLL_INTERVAL = 2.0


class CaptureError(RuntimeError):
    """Interactive capture failed. Message is safe to print — never includes a secret."""


class LoginTimeout(CaptureError):
    """The human did not complete the login within the timeout."""


class PlaywrightMissing(CaptureError):
    """playwright is not importable in this interpreter."""


@dataclass
class CaptureResult:
    """Outcome of one capture attempt."""

    platform: str
    user_id: str
    cookies: List[Dict[str, Any]]
    login_detected: bool
    elapsed_s: float

    def summary(self) -> Dict[str, Any]:
        """Secret-free summary for logs and the CLI."""
        domains = sorted({c.get("domain", "?") for c in self.cookies})
        return {
            "platform": self.platform,
            "user_id": self.user_id,
            "cookie_count": len(self.cookies),
            "domains": domains,
            "login_detected": self.login_detected,
            "elapsed_s": round(self.elapsed_s, 1),
        }


def _domain_matches(cookie_domain: str, expected: Sequence[str]) -> bool:
    """Cookie domain ``.linkedin.com`` matches expected ``.linkedin.com``."""
    cd = (cookie_domain or "").lstrip(".")
    for exp in expected:
        ed = exp.lstrip(".")
        if cd == ed or cd.endswith("." + ed):
            return True
    return False


def looks_logged_in(
    cookies: List[Dict[str, Any]], spec: PlatformSpec, baseline: int = 0
) -> bool:
    """True when the jar has a plausible session cookie for this platform."""
    if not cookies:
        return False
    # A jar that grew past the pre-login baseline is the primary signal.
    grew = len(cookies) > baseline
    matched = any(_domain_matches(c.get("domain", ""), spec.expected_cookie_domains)
                  for c in cookies) if spec.expected_cookie_domains else False
    return grew and matched


def cookies_to_header(cookies: List[Dict[str, Any]]) -> str:
    """`Cookie:` header string. SECRET — never log the return value."""
    return "; ".join(
        f"{c['name']}={c['value']}" for c in cookies if c.get("name")
    )


async def _capture_async(
    platform: str,
    user_id: str,
    timeout: int = DEFAULT_TIMEOUT,
    headless: bool = False,
    force: bool = False,
    store: Optional[CredentialStore] = None,
    browser_path: Optional[str] = None,
    slow_mo: int = 0,
) -> CaptureResult:
    """Open a browser, wait for a human login, return the cookies."""
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError as exc:
        raise PlaywrightMissing(
            "playwright is not installed — run: "
            "pip install playwright && playwright install chromium"
        ) from exc

    spec = get_platform(platform)
    if not spec.auth_required:
        raise CaptureError(f"'{platform}' needs no login — {spec.note}")

    started = time.monotonic()
    cookies: List[Dict[str, Any]] = []
    baseline = 0
    detected = False

    async with async_playwright() as pw:
        launch_kwargs: Dict[str, Any] = {"headless": headless}
        if browser_path:
            launch_kwargs["executable_path"] = browser_path
        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()

        try:
            log.info("opening %s login page for user '%s'", spec.name, user_id)
            await page.goto(spec.login_url, wait_until="domcontentloaded",
                            timeout=30_000)
            # Baseline = whatever the site sets before we touch anything.
            baseline = len(await context.cookies())

            if headless:
                raise CaptureError(
                    "--headless cannot work for an interactive login: there is "
                    "no window to type into. Re-run without it."
                )

            print()
            print("  ┌─────────────────────────────────────────────────────────┐")
            print("  │  Sign in now in the browser window that just opened.    │")
            print(f"  │  Platform: {spec.name:<44}│")
            print(f"  │  Timeout:  {timeout}s. This window is NOT shared.        │")
            print("  └─────────────────────────────────────────────────────────┘")
            print()

            deadline = started + timeout
            while time.monotonic() < deadline:
                cookies = await context.cookies()
                if force or looks_logged_in(cookies, spec, baseline):
                    detected = True
                    break
                await asyncio.sleep(POLL_INTERVAL)

            if not detected:
                # One last look before giving up — the human may have finished
                # in the gap between the poll and the deadline.
                cookies = await context.cookies()
                if force or looks_logged_in(cookies, spec, baseline):
                    detected = True
                else:
                    raise LoginTimeout(
                        f"no sign-in detected within {timeout}s. Nothing was "
                        f"stored. Re-run and complete the login in the browser "
                        f"window, or pass --force to capture whatever cookies "
                        f"are present."
                    )
        finally:
            await context.close()
            await browser.close()

    if not cookies:
        raise CaptureError("browser produced no cookies — was the login completed?")

    log.info("captured %d cookie(s) for %s (%s)",
             len(cookies), spec.name, sorted({c.get("domain", "?") for c in cookies}))

    result = CaptureResult(
        platform=spec.name,
        user_id=user_id,
        cookies=cookies,
        login_detected=detected,
        elapsed_s=time.monotonic() - started,
    )

    if store is not None:
        cred = store.put(user_id, spec.name, cookies=cookies, method="cookies")
        # Shape only — never the values.
        log.info("stored captured session for %s (%s, fingerprint=%s)",
                 spec.name, user_id, cred.fingerprint)
    return result


def capture_session(
    platform: str,
    user_id: str,
    timeout: int = DEFAULT_TIMEOUT,
    headless: bool = False,
    force: bool = False,
    store: Optional[CredentialStore] = None,
    browser_path: Optional[str] = None,
) -> CaptureResult:
    """Synchronous wrapper around :func:`_capture_async` for the CLI."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise CaptureError(
            "capture_session() is sync and cannot run inside an event loop — "
            "await _capture_async() instead"
        )
    return asyncio.run(
        _capture_async(
            platform, user_id, timeout=timeout, headless=headless, force=force,
            store=store, browser_path=browser_path,
        )
    )


async def inject_cookies(context: Any, cred: Credential) -> int:
    """Load a stored session into a Playwright BrowserContext.

    Returns the number of cookies injected. Never logs values.
    """
    cookies = cred.to_playwright_cookies()
    if not cookies:
        return 0
    await context.add_cookies(cookies)
    log.info("injected %d stored cookie(s) for %s (%s)",
             len(cookies), cred.platform, cred.user_id)
    return len(cookies)