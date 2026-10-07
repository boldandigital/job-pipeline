"""
Phase 2.2 — auth credential manager for CaptainApply.

Public surface:
  - src.auth_credentials.storage.CredentialsStore  ← the per-user cookie vault
  - src.auth_credentials.storage.save / load / delete / list_platforms /
    has_credentials                                ← module-level helpers
  - src.auth_credentials.storage.CredentialsError and friends

This package is a thin, spec-shaped façade over the battle-tested crypto
layer in ``src.auth.credentials`` (AES-256-GCM, keyring / PBKDF2 / keyfile
providers, atomic 0600 writes). It exists because the apply orchestrator
asks for a simpler API:

    save(user_id, platform, cookie_blob)
    load(user_id, platform) -> cookie_blob | None
    delete(user_id, platform)
    list_platforms(user_id) -> list[str]
    has_credentials(user_id, platform) -> bool

Credentials never leave the local machine. No network in this module. No
plaintext fallback file. The keyring PyPI package is the primary backend on
macOS; a PBKDF2-derived key from CAPTAIN_MASTER_PASSPHRASE is the fallback
for headless / CI.
"""
from __future__ import annotations

from .storage import (
    CredentialsError,
    CredentialsStore,
    NoSuchUserError,
    PlatformNotSupported,
    UnknownPlatform,
    delete,
    has_credentials,
    list_platforms,
    load,
    save,
)

__all__ = [
    "CredentialsStore",
    "CredentialsError",
    "NoSuchUserError",
    "PlatformNotSupported",
    "UnknownPlatform",
    "save",
    "load",
    "delete",
    "list_platforms",
    "has_credentials",
]
