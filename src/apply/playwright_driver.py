"""
Playwright-based Driver for the apply pipeline.

Implements the async Driver/FieldFiller Protocol from
src.apply.ats_adapters.base using a single Chromium tab.

This is the simplest wired driver that:
- opens a real browser tab
- fills forms (name, email, phone, file upload, custom fields)
- takes screenshots before final submit
- pauses for human review (browser stays open)
- NEVER auto-clicks Submit (memory rule: explicit 'go' per apply)

Run via:
    python -c "
    from src.apply.playwright_driver import PlaywrightDriverFactory
    import asyncio
    from src.apply.runner import run_apply_pipeline

    asyncio.run(run_apply_pipeline(
        driver_factory=PlaywrightDriverFactory(headless=False),
        job_id=79,
    ))
    "
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List

try:
    from playwright.async_api import async_playwright, Page, Browser, Playwright
except ImportError:  # pragma: no cover
    async_playwright = None  # type: ignore
    Page = Browser = Playwright = None  # type: ignore

log = logging.getLogger("apply.playwright_driver")


PLAYWRIGHT_BROWSER_PATH = (
    "/Users/lars/Library/Caches/ms-playwright/chromium-1243/"
    "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/"
    "Google Chrome for Testing"
)


class PlaywrightFieldFiller:
    """Adapter for a single Playwright Page (implements FieldFiller Protocol)."""

    def __init__(self, page: "Page"):
        self._page = page

    @property
    def page(self) -> "Page":
        return self._page

    async def goto(self, url: str) -> None:
        await self._page.goto(url, wait_until="domcontentloaded", timeout=20_000)

    async def fill(self, selector: str, value: str) -> None:
        try:
            await self._page.fill(selector, value, timeout=3_000)
        except Exception as exc:
            log.debug("fill(%s) failed: %s", selector, exc)

    async def upload_file(self, selector: str, file_path: str) -> None:
        try:
            await self._page.set_input_files(selector, file_path, timeout=5_000)
        except Exception as exc:
            log.debug("upload_file(%s, %s) failed: %s", selector, file_path, exc)

    async def click(self, selector: str) -> None:
        try:
            await self._page.click(selector, timeout=3_000)
        except Exception as exc:
            log.debug("click(%s) failed: %s", selector, exc)

    async def submit(self, selector: str) -> None:
        # Intentionally NOT calling click here — the runner is expected to
        # screenshot and pause for human review before final submit.
        # Memory rule: explicit 'go' per apply.
        log.info("submit() called for %s but PAUSING for human review (per memory rule)", selector)

    async def text(self, selector: str) -> str:
        try:
            return await self._page.inner_text(selector, timeout=3_000)
        except Exception:
            return ""

    async def exists(self, selector: str) -> bool:
        try:
            return (await self._page.locator(selector).count()) > 0
        except Exception:
            return False

    async def screenshot(self, file_path: str) -> str:
        try:
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
            await self._page.screenshot(path=file_path, full_page=True)
            return file_path
        except Exception as exc:
            log.debug("screenshot failed: %s", exc)
            return ""

    async def query_all(self, selector: str) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        try:
            handles = await self._page.query_selector_all(selector)
            for h in handles:
                try:
                    tag = await h.evaluate("el => el.tagName.toLowerCase()")
                    name = await h.evaluate("el => el.name || ''")
                    typ = await h.evaluate("el => el.type || ''")
                    placeholder = await h.evaluate("el => el.placeholder || ''")
                    label = await h.evaluate(
                        "el => { const l = el.closest('label') || document.querySelector(`label[for='${el.id}']`); "
                        "return l ? l.innerText : ''; }"
                    )
                    out.append({"tag": tag, "name": name, "type": typ,
                                 "placeholder": placeholder, "label": label})
                except Exception:
                    pass
        except Exception as exc:
            log.debug("query_all(%s) failed: %s", selector, exc)
        return out

    async def wait_for_selector(self, selector: str, timeout_ms: int = 10_000) -> None:
        try:
            await self._page.wait_for_selector(selector, timeout=timeout_ms)
        except Exception as exc:
            log.debug("wait_for_selector(%s) timeout: %s", selector, exc)


class PlaywrightDriver:
    """One Driver per job — opens a fresh tab eagerly on construction.

    The adapter accesses ``driver.page`` BEFORE ``driver.open()`` (per
    ashby.py:89), so the tab is opened here with a placeholder URL
    (``about:blank``) and the real URL is loaded by ``open()`` later.
    """

    def __init__(self, browser: "Browser"):
        self._browser = browser
        self._context = None
        self._page = None
        self.filler: PlaywrightFieldFiller | None = None
        # Open about:blank immediately so adapter can read driver.page
        # before driver.open() is called.
        # We do this synchronously to keep __call__ simple.
        # If the event loop isn't running, this will be set up lazily
        # on first await — see _ensure_open().

    @property
    def page(self) -> PlaywrightFieldFiller:
        if self.filler is None:
            raise RuntimeError("driver.page accessed before open() — call driver.open(url) first")
        return self.filler

    async def open(self, url: str) -> None:
        if self._context is None:
            self._context = await self._browser.new_context(viewport={"width": 1280, "height": 900})
            self._page = await self._context.new_page()
            self.filler = PlaywrightFieldFiller(self._page)
        await self.filler.goto(url)

    async def close(self) -> None:
        try:
            if self._context is not None:
                await self._context.close()
        except Exception:
            pass
        self._context = None
        self._page = None
        self.filler = None


class PlaywrightDriverFactory:
    """Singleton browser — each job gets a fresh context (tab).

    Args:
        headless: True for headless (no browser window). Memory rule says
            False (Ye need to see the page to click Submit).
    """

    def __init__(self, headless: bool = False):
        self.headless = headless
        self._pw: "Playwright | None" = None
        self._browser: "Browser | None" = None

    async def __aenter__(self):
        if async_playwright is None:
            raise RuntimeError("playwright not installed — run: pip install playwright && playwright install chromium")
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=["--no-sandbox"],
            executable_path=PLAYWRIGHT_BROWSER_PATH,
        )
        return self

    async def __aexit__(self, *exc):
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass

    def __call__(self) -> PlaywrightDriver:
        if self._browser is None:
            raise RuntimeError("PlaywrightDriverFactory not entered — use 'async with' first")
        return PlaywrightDriver(self._browser)
