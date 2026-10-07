"""
Auth credential manager for CaptainApply.

Package root. Import as::

    from src.auth import CredentialStore, get_credential, credentials_status

Layering:
    platforms.py    — static per-board auth requirements (no secrets)
    credentials.py  — encrypted, per-user storage (no network)
    capture.py      — interactive Playwright cookie capture
"""

from .platforms import (  # noqa: F401
    NO_AUTH_PLATFORMS,
    PLATFORMS,
    PlatformNotSupported,
    PlatformSpec,
    UnknownPlatform,
    get_platform,
    normalize_platform,
    public_platforms,
    requires_auth,
)
from .credentials import (  # noqa: F401
    Credential,
    CredentialNotFoundError,
    CredentialStore,
    CredentialsError,
    DecryptionError,
    NoSuchUserError,
    PassphraseRequiredError,
    auth_file_path,
    credentials_status,
    decrypt_blob,
    default_data_dir,
    encrypt_blob,
    fingerprint,
    get_credential,
    get_store,
    list_stored_platforms,
    redact,
    redact_mapping,
    set_credential,
    validate_user_id,
)

__all__ = [
    "PLATFORMS",
    "NO_AUTH_PLATFORMS",
    "PlatformSpec",
    "UnknownPlatform",
    "PlatformNotSupported",
    "get_platform",
    "normalize_platform",
    "requires_auth",
    "public_platforms",
    "Credential",
    "CredentialStore",
    "CredentialsError",
    "CredentialNotFoundError",
    "NoSuchUserError",
    "DecryptionError",
    "PassphraseRequiredError",
    "get_store",
    "set_credential",
    "get_credential",
    "credentials_status",
    "list_stored_platforms",
    "auth_file_path",
    "default_data_dir",
    "encrypt_blob",
    "decrypt_blob",
    "redact",
    "redact_mapping",
    "fingerprint",
    "validate_user_id",
]