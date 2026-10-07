#!/usr/bin/env python3
"""
captainauth_capture — drive a real browser to capture login cookies.

Usage
-----
    bin/captainauth_capture.py xing     <user_id>
    bin/captainauth_capture.py linkedin <user_id>
    bin/captainauth_capture.py indeed   <user_id>
    bin/captainauth_capture.py workday  <user_id> [--tenant myworkdaysite.com]

What it does
------------
1. Opens a Chromium window (visible, headed) via Playwright.
2. Navigates to the platform's login page.
3. You sign in manually — handle 2FA, captcha, anything the platform wants.
4. When you press Enter in the terminal, the script reads the browser's
   cookies, validates the domain against the platform spec, and stores
   them via ``src.auth_credentials``.

NO live logins happen from this script. The browser is yours, the cookies
are yours, the capture runs locally. Nothing is uploaded.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.auth_credentials import (
    CredentialsError,
    CredentialsStore,
    PlatformNotSupported,
    UnknownPlatform,
)
from src.auth.platforms import get_platform


def _domain_ok(cookies: list, expected_domains: Sequence[str]) -> bool:
    if not expected_domains:
        return True
    cookie_domains = {c.get("domain", "") for c in cookies}
    for expected in expected_domains:
        if any(d == expected or d.endswith(expected) for d in cookie_domains):
            return True
    return False


def _print_browser_instructions(platform: str, login_url: str) -> None:
    print()
    print("=" * 70)
    print(f"  captainauth_capture — {platform}")
    print("=" * 70)
    print()
    print(f"A browser window will open to:  {login_url}")
    print()
    print("  1. Sign in to the platform normally.")
    print("  2. Complete any 2FA / captcha / verification step.")
    print("  3. When the page shows you are signed in, return here.")
    print("  4. Press Enter to capture the cookies.")
    print()
    print("  Or type 'cancel' and press Enter to abort.")
    print("=" * 70)
    print()


def capture(platform: str, user_id: str, *,
            workday_tenant: Optional[str] = None,
            headless: bool = False,
            path: Optional[str] = None) -> int:
    try:
        spec = get_platform(platform)
    except UnknownPlatform as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not spec.auth_required:
        print(f"error: {spec.name} needs no login — {spec.note}", file=sys.stderr)
        return 2

    login_url = spec.login_url
    if spec.name == "workday" and workday_tenant:
        login_url = f"https://{workday_tenant}/"

    _print_browser_instructions(spec.name, login_url)

    # Lazy import — Playwright is heavy and not needed for status queries.
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("error: playwright is not installed. Run: pip install playwright",
              file=sys.stderr)
        return 3

    cookies: List[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(login_url, wait_until="domcontentloaded")
        except Exception as exc:
            print(f"warning: could not auto-navigate ({exc}); "
                  f"please navigate manually to {login_url}", file=sys.stderr)
        try:
            while True:
                ans = input(
                    "\nPress Enter to capture, or type 'cancel': "
                ).strip().lower()
                if ans in ("", "capture", "y", "yes"):
                    break
                if ans in ("cancel", "q", "quit", "n", "no"):
                    print("aborted.")
                    return 130
                print("press Enter, or type 'cancel'")
            cookies = context.cookies()
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

    if not cookies:
        print("error: no cookies captured — did you actually sign in?",
              file=sys.stderr)
        return 4

    if spec.expected_cookie_domains and not _domain_ok(
            cookies, spec.expected_cookie_domains):
        print(
            "warning: none of the captured cookies match the expected "
            f"domains for {spec.name}: {list(spec.expected_cookie_domains)}",
            file=sys.stderr,
        )
        ans = input("Save anyway? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("aborted.")
            return 130

    # Persist via the encrypted store.
    try:
        store = CredentialsStore(path=path)
        result = store.save(user_id, spec.name, cookies)
    except (CredentialsError, PlatformNotSupported, UnknownPlatform) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print()
    print(json.dumps({
        "ok": True,
        "user_id": result.user_id,
        "platform": result.platform,
        "cookie_count": len(result.cookies),
        "captured_at": result.captured_at,
    }, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="captainauth_capture",
        description=(
            "Open a real browser, let the user sign in to a job board, and "
            "capture the resulting cookies into the encrypted vault."
        ),
    )
    p.add_argument("platform",
                   help="One of: xing, linkedin, indeed, workday.")
    p.add_argument("user_id")
    p.add_argument("--tenant", default=None,
                   help="Workday tenant hostname (e.g. 'acme.myworkdaysite.com').")
    p.add_argument("--headless", action="store_true",
                   help="Run Chromium headless (for CI / testing only).")
    p.add_argument("--path", default=None,
                   help="Override the auth.json envelope path.")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return capture(
        args.platform, args.user_id,
        workday_tenant=args.tenant,
        headless=args.headless,
        path=args.path,
    )


if __name__ == "__main__":
    sys.exit(main())
