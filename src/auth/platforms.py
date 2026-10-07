"""
Per-platform auth configuration for browser-based apply.

CaptainApply needs a logged-in browser session on a handful of job boards
before it can reach the "Easy Apply" / "Schnell bewerben" button. Each board
wants a different thing, so this module is the single source of truth for:

  - where the login page lives
  - WHICH auth method is viable (cookies vs. username/password)
  - whether a credential is needed AT ALL
  - the operational risk of using the wrong method

Design decisions baked in here (see also docs/AUTH.md):

  * ATS boards (Greenhouse / Lever / Ashby) host the *company's* form on the
    company's own domain and are CSRF-token-per-session — there is NO account
    to log into. Storing credentials for them is not just useless, it is
    misleading. They are listed explicitly with ``auth_required=False`` so
    ``captainauth`` can refuse and say why.

  * XING / Indeed / LinkedIn all gate hard behind a logged-in session. Cookie
    injection (``context.add_cookies``) is the only method that survives their
    bot detection: no Selenium, no fingerprint to spoof, no password replay.

  * LinkedIn additionally supports username/password, but the platform's
    anti-automation actively flags password-driven logins from a fresh browser
    profile. That path risks a shadow ban on the user's *real* job-search
    account, so it is supported but flagged ``warn=True``.

The specs are plain data — no network calls, no secrets. A credential record
stores only what the spec says the platform needs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class PlatformSpec:
    """Static description of one job board's auth requirements."""

    name: str
    login_url: str
    auth_required: bool
    #: Preferred storage method: "cookies" | "password" | "none"
    preferred_method: str = "cookies"
    #: Methods we know work, best first.
    supported_methods: Tuple[str, ...] = ("cookies",)
    #: Human-readable note shown by `captainauth show` / the API status route.
    note: str = ""
    #: True when using this platform carries a real risk for the user.
    warn: bool = False
    #: Cookie domains we expect to see after a successful login. Used to
    #: sanity-check a captured session before we store it as valid.
    expected_cookie_domains: Tuple[str, ...] = ()

    @property
    def accepts_cookies(self) -> bool:
        return "cookies" in self.supported_methods

    @property
    def accepts_password(self) -> bool:
        return "password" in self.supported_methods

    def to_public_dict(self) -> Dict[str, Any]:
        """Secret-free view, safe to serve over the API."""
        return {
            "name": self.name,
            "login_url": self.login_url if self.auth_required else "",
            "auth_required": self.auth_required,
            "preferred_method": self.preferred_method,
            "supported_methods": list(self.supported_methods),
            "note": self.note,
            "warn": self.warn,
        }


#: Every board CaptainApply knows how to log into.
PLATFORMS: Dict[str, PlatformSpec] = {
    "linkedin": PlatformSpec(
        name="linkedin",
        login_url="https://www.linkedin.com/login",
        auth_required=True,
        preferred_method="cookies",
        supported_methods=("cookies", "password"),
        note=(
            "Cookie injection is safest. Username/password also works but "
            "password-driven logins from a fresh browser profile trip "
            "LinkedIn's anti-automation and risk a shadow ban on the real "
            "account — treat it as a last resort."
        ),
        warn=True,
        expected_cookie_domains=(".linkedin.com",),
    ),
    "xing": PlatformSpec(
        name="xing",
        login_url="https://login.xing.com/",
        auth_required=True,
        preferred_method="cookies",
        supported_methods=("cookies",),
        note=(
            "Cookies only, injected via context.add_cookies() — no Selenium "
            "required. XING rate-limits hard on fresh profiles."
        ),
        expected_cookie_domains=(".xing.com",),
    ),
    "indeed": PlatformSpec(
        name="indeed",
        login_url="https://secure.indeed.com/account/login",
        auth_required=True,
        preferred_method="cookies",
        supported_methods=("cookies", "password"),
        note=(
            "Cookies strongly preferred — Indeed runs aggressive captcha on "
            "password-driven form submissions."
        ),
        warn=True,
        expected_cookie_domains=(".indeed.com",),
    ),
    "workday": PlatformSpec(
        name="workday",
        login_url="https://wd5.myworkdaysite.com/",
        auth_required=True,
        preferred_method="cookies",
        supported_methods=("cookies",),
        note=(
            "Each tenant runs its own Workday domain. Capture the session "
            "once per tenant; there is no single global login."
        ),
        expected_cookie_domains=(".myworkday.com", ".myworkdaysite.com"),
    ),
    # --- ATS boards: no credential, ever. Listed so `captainauth` can say so.
    "greenhouse": PlatformSpec(
        name="greenhouse",
        login_url="",
        auth_required=False,
        preferred_method="none",
        supported_methods=(),
        note=(
            "No auth needed — Greenhouse embeds the company's form on the "
            "company's domain with a per-session CSRF token. Do not store a "
            "credential for this platform."
        ),
    ),
    "lever": PlatformSpec(
        name="lever",
        login_url="",
        auth_required=False,
        preferred_method="none",
        supported_methods=(),
        note=(
            "No auth needed — Lever's form is public and token-per-session. "
            "Do not store a credential for this platform."
        ),
    ),
    "ashby": PlatformSpec(
        name="ashby",
        login_url="",
        auth_required=False,
        preferred_method="none",
        supported_methods=(),
        note=(
            "No auth needed — Ashby applies are public JSON form posts. Do "
            "not store a credential for this platform."
        ),
    ),
}

#: Platforms that must be skipped entirely by any apply run.
NO_AUTH_PLATFORMS: Tuple[str, ...] = tuple(
    name for name, spec in PLATFORMS.items() if not spec.auth_required
)


class UnknownPlatform(KeyError):
    """Raised when a platform name is not in PLATFORMS."""

    def __init__(self, platform: str):
        self.platform = platform
        super().__init__(
            f"unknown platform '{platform}'. Known: {', '.join(sorted(PLATFORMS))}"
        )

    def __str__(self) -> str:  # KeyError would otherwise repr() the message
        return self.args[0]


class PlatformNotSupported(ValueError):
    """Raised when a platform exists but cannot accept the requested method."""


def get_platform(name: str) -> PlatformSpec:
    """Look up a platform spec by name (case-insensitive). Raises UnknownPlatform."""
    if not name or not name.strip():
        raise UnknownPlatform(str(name))
    key = name.strip().lower()
    spec = PLATFORMS.get(key)
    if spec is None:
        raise UnknownPlatform(key)
    return spec


def normalize_platform(name: str) -> str:
    """Return the canonical lowercase platform key."""
    return get_platform(name).name


def requires_auth(platform: str) -> bool:
    """True when a credential is actually needed for this platform."""
    return get_platform(platform).auth_required


def public_platforms() -> List[Dict[str, Any]]:
    """Secret-free catalog for `captainauth platforms` and the API."""
    return [PLATFORMS[n].to_public_dict() for n in sorted(PLATFORMS)]