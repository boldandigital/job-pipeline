#!/usr/bin/env python3
"""
captainauth — encrypted credential manager for CaptainApply.

Usage
-----
    bin/captainauth.py set       <user_id> <platform> <path/to/cookies.json>
    bin/captainauth.py show      <user_id>
    bin/captainauth.py delete    <user_id> <platform>
    bin/captainauth.py status    <user_id>
    bin/captainauth.py import-cookies <user_id> <platform>
    bin/captainauth.py platforms
    bin/captainauth.py init      [user_id]                # ensure profile exists

The credentials file must be a JSON array of cookie dicts, e.g.::

    [
      {"name": "li_at", "value": "AQED...", "domain": ".linkedin.com",
       "path": "/", "secure": true}
    ]

Secret values are NEVER printed. ``show`` lists platforms, ``status`` lists
counts and the key provider in use.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

# Make `src.*` importable when run as a script.
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


def _make_store(args: argparse.Namespace) -> CredentialsStore:
    """Build a store honouring --path / --key-provider if supplied."""
    path = getattr(args, "path", None)
    provider = getattr(args, "key_provider", None)
    return CredentialsStore(path=path, key_provider=provider)


def _read_cookies_file(path: Path) -> list:
    if not path.exists():
        raise SystemExit(f"cookies file not found: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"could not read {path}: {exc}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}")
    if not isinstance(data, list):
        raise SystemExit(
            f"{path} must contain a JSON array of cookie objects, "
            f"got {type(data).__name__}"
        )
    return data


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_set(args: argparse.Namespace) -> int:
    store = _make_store(args)
    cookies = _read_cookies_file(Path(args.cookies_file))
    try:
        result = store.save(
            args.user_id, args.platform, cookies,
            captured_at=args.captured_at, expires_at=args.expires_at,
        )
    except (CredentialsError, PlatformNotSupported, UnknownPlatform) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps({
            "ok": True,
            "user_id": result.user_id,
            "platform": result.platform,
            "cookie_count": len(result.cookies),
            "captured_at": result.captured_at,
            "expires_at": result.expires_at,
        }, indent=2, sort_keys=True)
    )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = _make_store(args)
    try:
        platforms = store.list_platforms(args.user_id)
    except CredentialsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "user_id": args.user_id,
        "platforms": platforms,
        "count": len(platforms),
    }, indent=2, sort_keys=True))
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    store = _make_store(args)
    if not store.delete(args.user_id, args.platform):
        print(
            json.dumps({
                "ok": False, "reason": "no such credential",
                "user_id": args.user_id, "platform": args.platform,
            }, indent=2, sort_keys=True)
        )
        return 0
    print(
        json.dumps({
            "ok": True, "deleted": args.platform,
            "user_id": args.user_id,
        }, indent=2, sort_keys=True)
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = _make_store(args)
    try:
        s = store.status(args.user_id)
    except CredentialsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Strip already-redacted platforms list to a count summary.
    platforms: List[dict] = s.get("platforms") or []
    summary = [{
        "platform": p["platform"],
        "method": p.get("method"),
        "cookie_count": p.get("cookie_count", 0),
        "has_password": p.get("has_password", False),
        "fingerprint": p.get("fingerprint"),
        "updated_at": p.get("updated_at"),
    } for p in platforms]
    print(json.dumps({
        "user_id": s.get("user_id"),
        "key_provider": s.get("key_provider"),
        "profile_exists": s.get("profile_exists"),
        "readable": s.get("readable"),
        "platform_count": s.get("platform_count", 0),
        "platforms": summary,
        "path": s.get("path"),
    }, indent=2, sort_keys=True))
    return 0


def cmd_import_cookies(args: argparse.Namespace) -> int:
    """Guide the user through exporting cookies from Chrome devtools."""
    plat = args.platform.lower()
    hints = {
        "xing": (
            "To export XING cookies from Chrome:\n"
            "  1. Open https://login.xing.com/ and sign in normally.\n"
            "  2. DevTools → Application → Cookies → https://www.xing.com\n"
            "  3. Select all rows, right-click → 'Delete' is NOT what you want —\n"
            "     instead, install the \"EditThisCookie\" or \"Cookie-Editor\"\n"
            "     extension and use its Export → JSON feature.\n"
            "  4. Save the export as cookies.json and re-run:\n"
            "         bin/captainauth.py set {uid} {plat} cookies.json"
        ),
        "linkedin": (
            "To export LinkedIn cookies from Chrome:\n"
            "  1. Open https://www.linkedin.com/ and sign in.\n"
            "  2. DevTools → Application → Cookies → https://www.linkedin.com\n"
            "  3. Use the Cookie-Editor extension → Export → JSON.\n"
            "  4. Save as cookies.json and re-run:\n"
            "         bin/captainauth.py set {uid} {plat} cookies.json\n"
            "  NOTE: li_at and JSESSIONID are the two cookies that matter most."
        ),
        "indeed": (
            "To export Indeed cookies from Chrome:\n"
            "  1. Open https://secure.indeed.com/account/login and sign in.\n"
            "  2. DevTools → Application → Cookies → https://secure.indeed.com\n"
            "  3. Cookie-Editor extension → Export → JSON.\n"
            "  4. Save as cookies.json and re-run:\n"
            "         bin/captainauth.py set {uid} {plat} cookies.json"
        ),
        "workday": (
            "Workday is per-tenant — the login URL is\n"
            "  https://<tenant>.myworkdaysite.com/d/<instance>/login\n"
            "  1. Open your tenant's login page and sign in.\n"
            "  2. DevTools → Application → Cookies → that hostname.\n"
            "  3. Cookie-Editor extension → Export → JSON.\n"
            "  4. Save as cookies.json and re-run:\n"
            "         bin/captainauth.py set {uid} {plat} cookies.json"
        ),
    }
    if plat not in hints:
        print(f"error: unknown platform {plat!r}", file=sys.stderr)
        return 2
    print(hints[plat].format(uid=args.user_id, plat=plat))
    return 0


def cmd_platforms(_args: argparse.Namespace) -> int:
    """List known platforms — secret-free."""
    from src.auth.platforms import public_platforms
    print(json.dumps(public_platforms(), indent=2, sort_keys=True))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Ensure a profile exists for the given user (creates empty if not)."""
    store = _make_store(args)
    # We use the lower layer for the actual create-empty step; the
    # CredentialsStore facade has no direct "init" because every `save`
    # already creates the profile lazily.
    from src.auth.credentials import get_store as _lower
    result = _lower(path=store.path).init_user(args.user_id)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# Arg parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="captainauth",
        description=(
            "Encrypted cookie vault for LinkedIn / XING / Indeed / Workday. "
            "Secrets never leave the local machine."
        ),
    )
    p.add_argument(
        "--path", default=None,
        help="Override the auth.json envelope path (default: platform-native).",
    )
    p.add_argument(
        "--key-provider", default=None,
        choices=("keychain", "passphrase", "keyfile"),
        help="Force a specific master-key strategy (default: auto-detect).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("set", help="Store a cookie blob from a JSON file.")
    s.add_argument("user_id")
    s.add_argument("platform")
    s.add_argument("cookies_file", help="JSON file with the cookie list.")
    s.add_argument("--captured-at", default=None,
                   help="ISO-8601 capture timestamp (default: now).")
    s.add_argument("--expires-at", default=None,
                   help="ISO-8601 expiry timestamp (optional).")
    s.set_defaults(func=cmd_set)

    s = sub.add_parser("show", help="List platforms that have stored creds.")
    s.add_argument("user_id")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("delete", help="Delete one platform's stored cookies.")
    s.add_argument("user_id")
    s.add_argument("platform")
    s.set_defaults(func=cmd_delete)

    s = sub.add_parser("status", help="Secret-free summary for one user.")
    s.add_argument("user_id")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("import-cookies",
                       help="Print platform-specific export instructions.")
    s.add_argument("user_id")
    s.add_argument("platform")
    s.set_defaults(func=cmd_import_cookies)

    s = sub.add_parser("platforms", help="List all known platforms.")
    s.set_defaults(func=cmd_platforms)

    s = sub.add_parser("init", help="Create an empty profile for a user.")
    s.add_argument("user_id")
    s.set_defaults(func=cmd_init)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except (CredentialsError, PlatformNotSupported, UnknownPlatform) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
