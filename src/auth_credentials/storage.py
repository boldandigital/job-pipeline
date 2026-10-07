"""
Encrypted, per-user cookie vault for the apply orchestrator.

Spec (from the Phase 2.2 brief):

    save(user_id, platform, cookie_blob)     # store
    load(user_id, platform) -> cookie_blob | None
    delete(user_id, platform)                 # remove
    list_platforms(user_id) -> list[str]      # what platforms have creds
    has_credentials(user_id, platform) -> bool

What is ``cookie_blob``? A list of cookie dicts, in the shape Playwright's
``BrowserContext.add_cookies()`` expects::

    [
        {"name": "li_at", "value": "AQED...", "domain": ".linkedin.com"},
        {"name": "JSESSIONID", "value": "ajax:...", "domain": ".xing.com"},
        ...
    ]

That's the same shape exported by the capture script and by Chrome devtools.
It is stored verbatim, just with the secret ``value`` field encrypted at rest.

The crypto itself lives in :mod:`src.auth.credentials` — this module is a
spec-shaped façade. That keeps the keyring / PBKDF2 / keyfile logic in one
place and the Phase 2.2 deliverable easy to reason about.

Constraints we enforce here, on top of what the lower layer already does:

  1. ``user_id`` is validated to ``[A-Za-z0-9][A-Za-z0-9._-]{0,63}`` — anything
     else raises ``CredentialsError`` and never reaches the disk layer.
  2. The list of platforms known to need a credential is closed. We refuse to
     store a "credential" for Greenhouse / Lever / Ashby because they need no
     login — a stored secret for them would be a lie that breaks apply runs.
  3. Returned cookie blobs only contain the fields Playwright needs. Anything
     else (HttpOnly markers, SameSite variants, capture timestamps) is
     preserved but normalised so callers don't have to.
  4. ``delete`` of a missing platform is a no-op. ``load`` of a missing
     platform returns ``None``. Never raises on the read path.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# Compose, don't reinvent. The lower layer is already a sealed AES-GCM vault
# with keyring / PBKDF2 / keyfile providers and per-user data keys.
from src.auth.credentials import (  # noqa: F401 — re-exported below
    CredentialsError,
    NoSuchUserError as _NoSuchUserError,
    CredentialNotFoundError,
    DecryptionError,
    PassphraseRequiredError,
    PlatformNotSupported,
    UnknownPlatform,
    get_store as _get_lower_store,
)

log = logging.getLogger("auth_credentials.storage")

#: All platforms that need a real login. The Phase 2.2 brief lists
#: LinkedIn / XING / Indeed / Workday; we additionally allow password for
#: LinkedIn / Indeed because their PlatformSpec permits it.
_PLATFORMS_NEEDING_AUTH = {"linkedin", "xing", "indeed", "workday"}


# Backwards-compatible alias for callers that already use the lower-layer
# name. Both names raise the same exception class.
NoSuchUserError = _NoSuchUserError


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

@dataclass
class StoredCredential:
    """Decrypted view of one platform's stored cookie blob.

    Exposed for tests + the CLI; the apply orchestrator should keep using
    :func:`load` and the opaque list-of-dicts return type.
    """

    platform: str
    user_id: str
    cookies: List[Dict[str, Any]]
    captured_at: str = ""
    expires_at: Optional[str] = None
    method: str = "cookies"

    def to_cookie_list(self) -> List[Dict[str, Any]]:
        """Pure cookie list (Playwright-friendly)."""
        return list(self.cookies)

    def public(self) -> Dict[str, Any]:
        """Secret-free view, safe for logs / API responses."""
        return {
            "platform": self.platform,
            "user_id": self.user_id,
            "method": self.method,
            "cookie_count": len(self.cookies),
            "captured_at": self.captured_at,
            "expires_at": self.expires_at,
        }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_user_id(user_id: str) -> str:
    """Reject anything that isn't a safe identifier before it touches disk."""
    from src.auth.credentials import validate_user_id as _lower
    return _lower(user_id)


def _validate_platform(platform: str) -> str:
    """Reject platforms that don't need a credential, plus unknown ones."""
    from src.auth.credentials import normalize_platform as _norm
    plat = _norm(platform)
    if plat not in _PLATFORMS_NEEDING_AUTH:
        raise PlatformNotSupported(
            f"'{plat}' does not need a stored credential — apply runs are "
            f"already authenticated by the ATS form. Known platforms needing "
            f"auth: {', '.join(sorted(_PLATFORMS_NEEDING_AUTH))}"
        )
    return plat


def _validate_cookie_blob(cookies: Any) -> List[Dict[str, Any]]:
    """Coerce a cookie blob to a list-of-dicts. Raise on anything unworkable."""
    if cookies is None:
        raise CredentialsError("cookie_blob is None — nothing to store")
    if isinstance(cookies, str):
        try:
            cookies = json.loads(cookies)
        except json.JSONDecodeError as exc:
            raise CredentialsError(
                f"cookie_blob is a string but is not valid JSON: {exc}"
            ) from exc
    if not isinstance(cookies, list):
        raise CredentialsError(
            f"cookie_blob must be a list of cookie dicts, got {type(cookies).__name__}"
        )
    out: List[Dict[str, Any]] = []
    for c in cookies:
        if not isinstance(c, dict):
            raise CredentialsError(
                f"each cookie must be a dict, got {type(c).__name__}"
            )
        name = c.get("name")
        if not isinstance(name, str) or not name:
            raise CredentialsError(f"cookie missing 'name': {list(c.keys())}")
        if "value" not in c:
            raise CredentialsError(f"cookie {name!r} missing 'value'")
        out.append({
            "name": name,
            "value": str(c["value"]),
            "domain": str(c.get("domain", "")),
            "path": str(c.get("path", "/")),
            **{k: v for k, v in c.items()
               if k in ("name", "value", "domain", "path",
                        "expires", "httpOnly", "secure", "sameSite",
                        "same_site", "hostOnly")},
        })
    if not out:
        raise CredentialsError("cookie_blob is empty — nothing to store")
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class CredentialsStore:
    """Encrypted cookie vault, one envelope per process.

    Args:
        path: envelope location. ``None`` → platform-native auth.json
              (already produced by :mod:`src.auth.credentials`).
        key_provider: force a specific strategy (``"keychain"``,
            ``"passphrase"``, ``"keyfile"``). ``None`` → auto.
    """

    def __init__(
        self,
        path: Optional[Union[str, Path]] = None,
        key_provider: Optional[str] = None,
    ):
        self._store = _get_lower_store(
            path=Path(path) if path is not None else None,
            key_provider=key_provider,
        )

    # -- properties ------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._store.path

    @property
    def key_provider_name(self) -> str:
        return self._store.key_provider_name

    # -- core API --------------------------------------------------------

    def save(
        self,
        user_id: str,
        platform: str,
        cookie_blob: Union[List[Dict[str, Any]], str],
        *,
        captured_at: Optional[str] = None,
        expires_at: Optional[str] = None,
        notes: str = "",
    ) -> StoredCredential:
        """Store (or replace) a cookie blob for one (user, platform) pair.

        ``cookie_blob`` is the list of cookies captured by the capture
        script — see module docstring for the shape.
        """
        uid = _validate_user_id(user_id)
        plat = _validate_platform(platform)
        cookies = _validate_cookie_blob(cookie_blob)

        # We stash captured_at / expires_at on the ``notes`` field of the
        # underlying Credential — the lower layer doesn't have those fields,
        # but it round-trips arbitrary notes text. The end-to-end test
        # proves the round trip; the value is metadata, not a secret.
        meta_bits = []
        if captured_at:
            meta_bits.append(f"captured_at={captured_at}")
        if expires_at:
            meta_bits.append(f"expires_at={expires_at}")
        meta_note = " | ".join(meta_bits)
        full_notes = " | ".join(x for x in (notes, meta_note) if x)

        self._store.put(
            uid, plat,
            cookies=cookies,
            method="cookies",
            notes=full_notes,
        )

        return StoredCredential(
            platform=plat,
            user_id=uid,
            cookies=cookies,
            captured_at=captured_at or _now_iso(),
            expires_at=expires_at,
            method="cookies",
        )

    def load(self, user_id: str, platform: str) -> Optional[StoredCredential]:
        """Return the stored cookie blob, or ``None`` if missing.

        Never raises on a missing record. A wrong master key still raises
        :class:`DecryptionError` — that's a hard failure, not a missing
        record, and silently returning ``None`` would let callers think
        they just need to log in again.
        """
        uid = _validate_user_id(user_id)
        plat = _validate_platform(platform)
        try:
            cred = self._store.get(uid, plat)
        except CredentialNotFoundError:
            return None
        except NoSuchUserError as exc:
            # The lower layer's _load_user_record raises NoSuchUserError on
            # either "user absent" or "wrapped_key didn't decrypt" (with
            # the original DecryptionError as __cause__). A wrong key must
            # NOT look like a missing credential — re-raise.
            cause = exc.__cause__
            if isinstance(cause, DecryptionError):
                raise DecryptionError(str(cause)) from cause
            return None
        # Extract the metadata we tucked into notes.
        captured_at = ""
        expires_at: Optional[str] = None
        if cred.notes:
            for part in cred.notes.split(" | "):
                if part.startswith("captured_at="):
                    captured_at = part.split("=", 1)[1]
                elif part.startswith("expires_at="):
                    expires_at = part.split("=", 1)[1]
        return StoredCredential(
            platform=plat,
            user_id=uid,
            cookies=list(cred.cookies or []),
            captured_at=captured_at,
            expires_at=expires_at,
            method=cred.method or "cookies",
        )

    def delete(self, user_id: str, platform: str) -> bool:
        """Remove a single platform's cookie blob. No-op if missing.

        Returns ``True`` if something was actually deleted.

        Unknown platform names are tolerated as a no-op — ``delete`` is
        about removing things, and asking to remove a thing that doesn't
        exist (and was never valid in the first place) is the same outcome
        as removing it. This keeps callers from having to know the exact
        platform list to clean up.
        """
        uid = _validate_user_id(user_id)
        try:
            plat = _validate_platform(platform)
        except (UnknownPlatform, PlatformNotSupported):
            return False
        try:
            self._store.delete(uid, plat)
            return True
        except (NoSuchUserError, CredentialNotFoundError):
            return False

    def list_platforms(self, user_id: str) -> List[str]:
        """Sorted list of platforms that have stored creds for this user.

        Raises :class:`NoSuchUserError` if the user has no profile at all
        (different from "user exists but has no creds" — the former means
        nothing was ever stored, which a caller probably wants to know).
        """
        uid = _validate_user_id(user_id)
        try:
            return self._store.platforms_with_credentials(uid)
        except NoSuchUserError:
            return []

    def has_credentials(self, user_id: str, platform: str) -> bool:
        """True iff a stored cookie blob exists for this (user, platform)."""
        return self.load(user_id, platform) is not None

    # -- conveniences ----------------------------------------------------

    def status(self, user_id: str) -> Dict[str, Any]:
        """Secret-free view of every platform stored for this user."""
        uid = _validate_user_id(user_id)
        return self._store.status(uid)


# ---------------------------------------------------------------------------
# Module-level convenience API (the spec's exact shape)
# ---------------------------------------------------------------------------

_DEFAULT_STORE: Optional[CredentialsStore] = None


def _get_default_store() -> CredentialsStore:
    """Lazy singleton — the lower layer's default path is fine for most uses.

    The cache key is the current ``CAPTAIN_AUTH_DIR`` + ``CAPTAIN_AUTH_KEYFILE``
    so tests that override those env vars via ``monkeypatch`` get a fresh
    store. The env vars win over the singleton for the next call.
    """
    global _DEFAULT_STORE
    cache_key = (
        os.environ.get("CAPTAIN_AUTH_DIR"),
        os.environ.get("CAPTAIN_AUTH_KEYFILE"),
        os.environ.get("CAPTAIN_MASTER_PASSPHRASE"),
    )
    if _DEFAULT_STORE is None or getattr(_DEFAULT_STORE, "_env_cache_key", None) != cache_key:
        s = CredentialsStore()
        s._env_cache_key = cache_key  # type: ignore[attr-defined]
        _DEFAULT_STORE = s
    return _DEFAULT_STORE


def reset_default_store() -> None:
    """Drop the cached default store.

    Exposed for tests that change ``CAPTAIN_AUTH_DIR`` between cases. Not
    intended for application use — production code should pass ``path=``
    explicitly to :class:`CredentialsStore` so the choice is obvious.
    """
    global _DEFAULT_STORE
    _DEFAULT_STORE = None


def save(
    user_id: str,
    platform: str,
    cookie_blob: Union[List[Dict[str, Any]], str],
    *,
    captured_at: Optional[str] = None,
    expires_at: Optional[str] = None,
) -> StoredCredential:
    """Module-level wrapper for :meth:`CredentialsStore.save`."""
    return _get_default_store().save(
        user_id, platform, cookie_blob,
        captured_at=captured_at, expires_at=expires_at,
    )


def load(user_id: str, platform: str) -> Optional[StoredCredential]:
    """Module-level wrapper for :meth:`CredentialsStore.load`."""
    return _get_default_store().load(user_id, platform)


def delete(user_id: str, platform: str) -> bool:
    """Module-level wrapper for :meth:`CredentialsStore.delete`."""
    return _get_default_store().delete(user_id, platform)


def list_platforms(user_id: str) -> List[str]:
    """Module-level wrapper for :meth:`CredentialsStore.list_platforms`."""
    return _get_default_store().list_platforms(user_id)


def has_credentials(user_id: str, platform: str) -> bool:
    """Module-level wrapper for :meth:`CredentialsStore.has_credentials`."""
    return _get_default_store().has_credentials(user_id, platform)


__all__ = [
    "CredentialsStore",
    "StoredCredential",
    "CredentialsError",
    "NoSuchUserError",
    "CredentialNotFoundError",
    "DecryptionError",
    "PassphraseRequiredError",
    "PlatformNotSupported",
    "UnknownPlatform",
    "save",
    "load",
    "delete",
    "list_platforms",
    "has_credentials",
]
