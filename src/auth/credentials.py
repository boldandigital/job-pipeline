"""
Encrypted, per-user credential storage for CaptainApply.

Threat model
------------
The machine is trusted; the *disk* is not. An `auth.json` containing a
LinkedIn session cookie in the clear would let anyone with a copy of the
laptop resume the user's job search as them. So everything at rest is
AES-256-GCM encrypted with a key that never touches the disk.

Key hierarchy
-------------
  master key  — 32 random bytes, generated once, stored in the OS keychain
                 (macOS Keychain / Windows Credential Manager / libsecret).
                 Falls back to a PBKDF2 key from CAPTAIN_MASTER_PASSPHRASE,
                 and finally to a 0600 key file, so the module still works on
                 a headless box with no keychain daemon.
  data key    — a separate 32-byte key per user_id, encrypted under the
                 master key. Gives per-user isolation at the crypto layer:
                 a leaked data key never unlocks another tenant's file.

On-disk layout
--------------
One JSON envelope per user at::

    ~/Library/Application Support/CaptainApply/auth.json          (macOS)
    $XDG_DATA_HOME/captainapply/auth.json                       (Linux)

::

    {
      "version": 1,
      "created_at": "...",
      "users": {
        "lars": {
          "salt": "<b64>",              # PBKDF2 salt for the user's data key
          "iterations": 200000,
          "wrapped_key": "<b64>",       # user's data key, AES-GCM under master
          "nonce": "<b64>",
          "credentials": "<b64>",       # AES-GCM ciphertext of the credential map
          "cipher_nonce": "<b64>",
          "updated_at": "..."
        }
      }
    }

The envelope is written atomically (temp file + os.replace) with mode 0600
because even the ciphertext is sensitive: the user_id → platform mapping
leaks a job-search profile.

What is deliberately NOT here
-----------------------------
* No logging of any secret value. ``redact`` exists so error paths and log
  lines can reference a credential without printing it.
* No network. This module imports nothing that can open a socket.
* No plaintext fallback file. If the key is wrong, you get an error — not a
  silent downgrade to cleartext.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .platforms import (
    PLATFORMS,
    PlatformNotSupported,
    UnknownPlatform,
    get_platform,
    normalize_platform,
)

log = logging.getLogger("auth.credentials")

ENVELOPE_VERSION = 1
PBKDF2_ITERATIONS = 200_000
KEY_BYTES = 32
NONCE_BYTES = 12
SALT_BYTES = 16
KEYCHAIN_SERVICE = "CaptainApply"
KEYCHAIN_ACCOUNT = "master-key"

#: Only these chars are allowed in a user_id / platform key. Anything else is
#: rejected outright — this is what stops ``../../etc/passwd`` and
#: ``lars:admin`` from ever reaching the filesystem or a JSON key.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

REDACTED = "***REDACTED***"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class CredentialsError(Exception):
    """Base class for every error this module raises."""


class NoSuchUserError(CredentialsError, KeyError):
    """No profile stored for this user_id."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        super().__init__(f"no credential profile for user_id '{user_id}'")

    def __str__(self) -> str:
        return self.args[0]


class CredentialNotFoundError(CredentialsError, KeyError):
    """The user profile exists but has no credential for this platform."""

    def __init__(self, user_id: str, platform: str):
        self.user_id = user_id
        self.platform = platform
        super().__init__(
            f"no '{platform}' credential stored for user_id '{user_id}'"
        )

    def __str__(self) -> str:
        return self.args[0]


class DecryptionError(CredentialsError):
    """Ciphertext failed authentication — wrong key, or the file was tampered with."""


class PassphraseRequiredError(CredentialsError):
    """Storage is passphrase-protected and no passphrase was supplied."""


# ---------------------------------------------------------------------------
# Redaction helpers
# ---------------------------------------------------------------------------

def redact(value: Any, keep: int = 0) -> str:
    """Render a secret as a non-reversible marker for logs and API responses.

    Never returns any part of ``value`` by default — a cookie's first few
    chars are enough to identify a session server-side.
    """
    if value is None:
        return "<none>"
    if not isinstance(value, str):
        return f"<{type(value).__name__} len={len(value) if hasattr(value, '__len__') else '?'}>"
    if not value:
        return "<empty>"
    if keep > 0 and len(value) > keep:
        return f"{REDACTED}(len={len(value)}, tail=...)"
    return f"{REDACTED}(len={len(value)})"


def redact_mapping(data: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of ``data`` with every known secret-bearing field replaced by a marker.

    Used for the API status route: the shape of the credential survives, the
    values never do.
    """
    secret_fields = {
        "cookies", "cookie_header", "password", "username", "email",
        "csrf_token", "token", "secret", "local_storage", "session",
    }
    out: Dict[str, Any] = {}
    for key, value in data.items():
        if key in secret_fields:
            out[key] = redact(value) if isinstance(value, str) else f"{REDACTED}"
        else:
            out[key] = value
    return out


def fingerprint(value: str, length: int = 8) -> str:
    """Stable, non-reversible short digest — lets `show` prove which secret is
    stored without revealing it."""
    import hashlib
    if not isinstance(value, str):
        return REDACTED
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def default_data_dir() -> Path:
    """Platform-native data directory for CaptainApply."""
    override = os.getenv("CAPTAIN_AUTH_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":  # pragma: no cover — not exercised on macOS CI
        base = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "CaptainApply"
    if os.sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CaptainApply"
    base = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "captainapply"


def auth_file_path(data_dir: Optional[Path] = None) -> Path:
    """Absolute path of the encrypted envelope."""
    base = Path(data_dir) if data_dir is not None else default_data_dir()
    return base / "auth.json"


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, stat.S_IRWXU)  # 0700
    except OSError:  # pragma: no cover — non-POSIX / permission edge
        pass


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` with 0600 perms, atomically."""
    _ensure_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    # Create with 0600 from the start — never briefly world-readable.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(path))
        try:
            os.chmod(str(path), 0o600)
        except OSError:  # pragma: no cover
            pass
    finally:
        if tmp.exists():  # pragma: no cover — only on a failed write
            try:
                tmp.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Master key — keychain first, passphrase second, keyfile last
# ---------------------------------------------------------------------------

class KeyProvider:
    """Resolves the master key. Subclasses implement one strategy each."""

    name = "base"

    def get_or_create(self) -> bytes:  # pragma: no cover — interface
        raise NotImplementedError

    def available(self) -> bool:
        try:
            self.get_or_create()
            return True
        except Exception:
            return False


class KeychainKeyProvider(KeyProvider):
    """Master key held in the OS keychain via the `keyring` package.

    `keyring` is an optional dependency — the project ships without it, so the
    caller must fall through to the next provider. We import lazily so merely
    importing this module never fails on a machine without keyring.
    """

    name = "keychain"

    def _keyring(self):
        try:
            import keyring  # type: ignore
        except ImportError:
            return None
        return keyring

    def available(self) -> bool:
        return self._keyring() is not None

    def get_or_create(self) -> bytes:
        keyring = self._keyring()
        if keyring is None:
            raise PassphraseRequiredError(
                "keyring is not installed; install it or set "
                "CAPTAIN_MASTER_PASSPHRASE"
            )
        try:
            stored = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
            if stored:
                return _b64decode(stored)
            fresh = secrets.token_bytes(KEY_BYTES)
            keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, _b64encode(fresh))
            return fresh
        except Exception as exc:
            raise CredentialsError(f"keychain unavailable: {type(exc).__name__}") from exc


class PassphraseKeyProvider(KeyProvider):
    """PBKDF2 key derived from CAPTAIN_MASTER_PASSPHRASE.

    Used when there is no interactive keychain (headless server, Docker). The
    passphrase itself never reaches disk.
    """

    name = "passphrase"

    def __init__(self, passphrase: Optional[str] = None):
        self._passphrase = passphrase

    def _pass(self) -> str:
        p = self._passphrase or os.getenv("CAPTAIN_MASTER_PASSPHRASE")
        if not p:
            raise PassphraseRequiredError(
                "no CAPTAIN_MASTER_PASSPHRASE set — run "
                "`bin/captainauth init` to create a master key first"
            )
        return p

    def available(self) -> bool:
        return bool(self._passphrase or os.getenv("CAPTAIN_MASTER_PASSPHRASE"))

    def get_or_create(self) -> bytes:
        salt = _b64encode(secrets.token_bytes(SALT_BYTES))
        derived = _pbkdf2(self._pass(), salt, PBKDF2_ITERATIONS)
        # The salt must persist, or the next process derives a different key.
        # Storing it leaks nothing (salt is public by definition) and we keep
        # it beside the envelope in a 0600 file rather than in the envelope
        # itself, so the envelope stays a pure ciphertext container.
        _ensure_dir(default_data_dir())
        _atomic_write(
            default_data_dir() / "master.salt",
            salt.encode("ascii"),
        )
        return derived

    def load(self) -> bytes:
        """Derive using the persisted salt — the read path (never creates)."""
        p = self._pass()
        salt_path = default_data_dir() / "master.salt"
        if not salt_path.exists():
            raise CredentialsError(
                f"master salt missing at {salt_path} — run `bin/captainauth init`"
            )
        salt = salt_path.read_text(encoding="ascii").strip()
        return _pbkdf2(p, salt, PBKDF2_ITERATIONS)


class KeyfileKeyProvider(KeyProvider):
    """Last resort: a 0600 key file next to the envelope.

    Not as strong as the keychain (the key sits on the same disk as the
    ciphertext), but it beats plaintext and it means `captainauth` works on a
    fresh machine with zero setup.
    """

    name = "keyfile"

    def _path(self) -> Path:
        override = os.getenv("CAPTAIN_AUTH_KEYFILE")
        if override:
            return Path(override).expanduser()
        return default_data_dir() / "master.key"

    def available(self) -> bool:
        return True

    def get_or_create(self) -> bytes:
        path = self._path()
        if path.exists():
            raw = path.read_bytes().strip()
            try:
                return _b64decode(raw)
            except Exception as exc:
                raise CredentialsError(f"master key file corrupt at {path}") from exc
        fresh = secrets.token_bytes(KEY_BYTES)
        _atomic_write(path, _b64encode(fresh).encode("ascii"))
        log.info("generated new master key at %s (mode 0600)", path)
        return fresh


def _resolve_key_provider(preferred: Optional[str] = None) -> KeyProvider:
    """Pick the best available key provider.

    Order: keychain (if `keyring` is importable) → passphrase (if env set) →
    keyfile. ``preferred`` forces a specific one and raises if it is not ready.
    """
    providers = [KeychainKeyProvider(), PassphraseKeyProvider(), KeyfileKeyProvider()]
    by_name = {p.name: p for p in providers}
    if preferred:
        provider = by_name.get(preferred)
        if provider is None:
            raise CredentialsError(
                f"unknown key provider '{preferred}'. Known: {', '.join(by_name)}"
            )
        return provider
    for provider in providers:
        if provider.name == "keychain" and not provider.available():
            continue
        if provider.name == "passphrase" and not provider.available():
            continue
        return provider
    return providers[-1]  # pragma: no cover — keyfile is always available


# ---------------------------------------------------------------------------
# Crypto primitives
# ---------------------------------------------------------------------------

def _pbkdf2(passphrase: str, salt_b64: str, iterations: int) -> bytes:
    import hashlib
    return hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), _b64decode(salt_b64), iterations
    )


def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64decode(text: Union[str, bytes]) -> bytes:
    """Decode base64 from either str (JSON envelope, salt file) or raw bytes
    (the master key file, which we read off disk before decoding)."""
    if isinstance(text, (bytes, bytearray)):
        return base64.b64decode(bytes(text))
    return base64.b64decode(text.encode("ascii"))


def encrypt_blob(plaintext: str, key: bytes) -> Tuple[str, str]:
    """AES-256-GCM encrypt → (ciphertext_b64, nonce_b64)."""
    nonce = secrets.token_bytes(NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return _b64encode(ct), _b64encode(nonce)


def decrypt_blob(ciphertext_b64: str, nonce_b64: str, key: bytes) -> str:
    """AES-256-GCM decrypt. Raises DecryptionError on tamper/wrong key."""
    try:
        ct = _b64decode(ciphertext_b64)
        nonce = _b64decode(nonce_b64)
        return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
    except (InvalidTag, ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise DecryptionError(
            "could not decrypt credentials — wrong master key or the file was "
            "modified. Nothing was overwritten."
        ) from exc


def _new_data_key() -> bytes:
    """A fresh 32-byte per-user data key, as raw bytes.

    Raw bytes everywhere it is used as an AES key. It is base64-encoded only
    at the storage boundary, when wrapping it under the master key — because
    the envelope stores text, and a 32-byte key is not valid UTF-8 on its own.
    """
    return secrets.token_bytes(KEY_BYTES)


# ---------------------------------------------------------------------------
# Credential record
# ---------------------------------------------------------------------------

@dataclass
class Credential:
    """One platform's stored credential for one user.

    Holds EITHER cookies OR a username/password pair, never both implicitly —
    `has_cookies` / `has_password` are what callers branch on.
    """

    platform: str
    user_id: str
    cookies: Optional[List[Dict[str, Any]]] = None
    username: Optional[str] = None
    password: Optional[str] = None
    method: str = "cookies"
    created_at: str = ""
    updated_at: str = ""
    last_used_at: Optional[str] = None
    notes: str = ""

    def __post_init__(self) -> None:
        now = _utc_now()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    @property
    def has_cookies(self) -> bool:
        return bool(self.cookies)

    @property
    def has_password(self) -> bool:
        return bool(self.username and self.password)

    @property
    def cookie_count(self) -> int:
        return len(self.cookies or [])

    @property
    def fingerprint(self) -> str:
        """Short digest of the primary secret — proves which one is stored
        without revealing it."""
        if self.has_cookies:
            return fingerprint(_cookie_material(self.cookies or []))
        if self.has_password:
            return fingerprint(f"{self.username}:{self.password}")
        return REDACTED

    def cookie_header(self) -> Optional[str]:
        """Render cookies as a `Cookie:` header value for HTTP-based paths.

        Values ARE secrets; callers must never log the return value.
        """
        if not self.cookies:
            return None
        return "; ".join(
            f"{c['name']}={c['value']}" for c in self.cookies if c.get("name")
        )

    def to_dict(self) -> Dict[str, Any]:
        """Full, secret-bearing dict — for encryption only."""
        return {
            "platform": self.platform,
            "user_id": self.user_id,
            "cookies": self.cookies,
            "username": self.username,
            "password": self.password,
            "method": self.method,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_used_at": self.last_used_at,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Credential":
        return cls(
            platform=data["platform"],
            user_id=data["user_id"],
            cookies=data.get("cookies"),
            username=data.get("username"),
            password=data.get("password"),
            method=data.get("method", "cookies"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            last_used_at=data.get("last_used_at"),
            notes=data.get("notes", ""),
        )

    def public(self) -> Dict[str, Any]:
        """Secret-free view. This is the ONLY shape allowed to leave the machine."""
        return {
            "platform": self.platform,
            "user_id": self.user_id,
            "method": self.method,
            "has_cookies": self.has_cookies,
            "cookie_count": self.cookie_count,
            "has_password": self.has_password,
            "username_present": bool(self.username),
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_used_at": self.last_used_at,
            "notes": self.notes,
        }

    def to_playwright_cookies(self) -> List[Dict[str, Any]]:
        """Cookie list shaped for ``BrowserContext.add_cookies()``.

        Playwright rejects cookies missing name/value, and only accepts
        sameSite in {Strict, Lax, None} — we drop anything it would choke on
        rather than let a bad capture break an apply run.
        """
        out: List[Dict[str, Any]] = []
        for c in self.cookies or []:
            if not c.get("name") or "value" not in c:
                continue
            entry: Dict[str, Any] = {
                "name": c["name"],
                "value": str(c["value"]),
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
            }
            same_site = c.get("sameSite") or c.get("same_site")
            if same_site in ("Strict", "Lax", "None"):
                entry["sameSite"] = same_site
            for src, dst in (("httpOnly", "httpOnly"), ("secure", "secure"),
                             ("expires", "expires")):
                if src in c:
                    entry[dst] = c[src]
            out.append(entry)
        return out


def _cookie_material(cookies: List[Dict[str, Any]]) -> str:
    """Deterministic string of a cookie jar, for fingerprinting."""
    parts = sorted(f"{c.get('name', '')}={c.get('value', '')}" for c in cookies)
    return "|".join(parts)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def validate_user_id(user_id: str) -> str:
    """Validate + normalise a user_id. Raises on anything unsafe."""
    if not isinstance(user_id, str) or not user_id.strip():
        raise CredentialsError("user_id must be a non-empty string")
    candidate = user_id.strip()
    if not _SAFE_ID_RE.match(candidate):
        raise CredentialsError(
            f"invalid user_id '{redact(candidate)}' — allowed: letters, digits, "
            "dot, underscore, hyphen; must start alphanumeric; max 64 chars"
        )
    return candidate


class CredentialStore:
    """Encrypted, multi-tenant credential store backed by one JSON envelope.

    Args:
        path: envelope location. Defaults to the platform-native path.
        key_provider: force a specific master-key strategy ("keychain",
            "passphrase", "keyfile"). Defaults to auto-detect.

    Every read decrypts; every write re-encrypts the WHOLE file. That is fine
    at our scale (a handful of users, a handful of platforms each) and it
    keeps the file format trivially auditable.
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        key_provider: Optional[str] = None,
    ):
        self._path = Path(path) if path is not None else auth_file_path()
        self._key_provider_name = key_provider
        self._provider: Optional[KeyProvider] = None

    # -- key plumbing -------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def key_provider_name(self) -> str:
        """Name of the strategy in use, for `show` / the API."""
        if self._provider is None:
            self._provider = _resolve_key_provider(self._key_provider_name)
        return self._provider.name

    def _key(self) -> bytes:
        if self._provider is None:
            self._provider = _resolve_key_provider(self._key_provider_name)
        provider = self._provider
        if isinstance(provider, PassphraseKeyProvider):
            # Prefer the load() path so the salt is not silently re-created
            # on every read (which would derive a different key).
            if (default_data_dir() / "master.salt").exists():
                return provider.load()
        return provider.get_or_create()

    # -- raw envelope -------------------------------------------------------

    def _read_envelope(self) -> Dict[str, Any]:
        if not self._path.exists():
            return {"version": ENVELOPE_VERSION, "created_at": _utc_now(), "users": {}}
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CredentialsError(
                f"could not read credential file at {self._path}: {type(exc).__name__}"
            ) from exc
        if not raw.strip():
            return {"version": ENVELOPE_VERSION, "created_at": _utc_now(), "users": {}}
        try:
            env = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CredentialsError(
                f"credential file at {self._path} is not valid JSON (line {exc.lineno})"
            ) from exc
        if not isinstance(env, dict) or "users" not in env:
            raise CredentialsError(
                f"credential file at {self._path} has an unexpected layout"
            )
        env.setdefault("version", ENVELOPE_VERSION)
        return env

    def _write_envelope(self, env: Dict[str, Any]) -> None:
        payload = json.dumps(env, indent=2, sort_keys=True).encode("utf-8")
        _atomic_write(self._path, payload)

    # -- per-user key + payload --------------------------------------------

    def _load_user_record(self, env: Dict[str, Any], user_id: str) -> Tuple[bytes, Dict[str, Any]]:
        """Return (data_key_bytes, user_record). Raises NoSuchUserError.

        The data key is stored as the b64 of 32 raw bytes. The unwrap step
        gives us the b64 form; we re-decode to raw bytes so the caller has
        a real 32-byte AES key in hand.
        """
        users = env.get("users") or {}
        rec = users.get(user_id)
        if not rec:
            raise NoSuchUserError(user_id)
        master = self._key()
        try:
            data_key_b64 = decrypt_blob(rec["wrapped_key"], rec["nonce"], master)
        except DecryptionError as exc:
            raise NoSuchUserError(user_id) from exc
        return _b64decode(data_key_b64), rec

    def _user_payload(self, rec: Dict[str, Any], data_key: bytes) -> Dict[str, Credential]:
        plain = decrypt_blob(rec["credentials"], rec["cipher_nonce"], data_key)
        try:
            data = json.loads(plain)
        except json.JSONDecodeError as exc:
            raise CredentialsError("stored credential payload is corrupt") from exc
        return {
            name: Credential.from_dict({**v, "user_id": rec.get("user_id", "")})
            for name, v in (data or {}).items()
        }

    def _make_user_record(
        self, data: Dict[str, Credential], data_key: bytes, user_id: str = ""
    ) -> Dict[str, Any]:
        # Wrap the raw 32-byte data key as b64 for storage under the master
        # key. AES-GCM on the credential payload uses the raw bytes directly.
        wrapped, nonce = encrypt_blob(_b64encode(data_key), self._key())
        payload = json.dumps(
            {n: c.to_dict() for n, c in data.items()}, sort_keys=True
        )
        cipher, cipher_nonce = encrypt_blob(payload, data_key)
        return {
            "user_id": user_id,
            "iterations": PBKDF2_ITERATIONS,
            "wrapped_key": wrapped,
            "nonce": nonce,
            "credentials": cipher,
            "cipher_nonce": cipher_nonce,
            "updated_at": _utc_now(),
        }

    # -- public API ---------------------------------------------------------

    def list_users(self) -> List[str]:
        """Every user_id with a stored profile. Sorted."""
        env = self._read_envelope()
        return sorted((env.get("users") or {}).keys())

    def has_user(self, user_id: str) -> bool:
        return validate_user_id(user_id) in self._read_envelope().get("users", {})

    def init_user(self, user_id: str) -> Dict[str, Any]:
        """Create an empty encrypted profile for a user (idempotent)."""
        uid = validate_user_id(user_id)
        env = self._read_envelope()
        users = env.setdefault("users", {})
        if uid in users:
            return {"user_id": uid, "created": False, "path": str(self._path)}
        data_key = _new_data_key()
        users[uid] = self._make_user_record({}, data_key, user_id=uid)
        env["created_at"] = env.get("created_at") or _utc_now()
        self._write_envelope(env)
        log.info("initialised credential profile for user '%s' (key=%s)",
                 uid, self.key_provider_name)
        return {"user_id": uid, "created": True, "path": str(self._path)}

    def put(
        self,
        user_id: str,
        platform: str,
        *,
        cookies: Optional[List[Dict[str, Any]]] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        method: Optional[str] = None,
        notes: str = "",
    ) -> Credential:
        """Store (or replace) one platform credential.

        Refuses platforms that need no auth — a stored Greenhouse "credential"
        would be a lie and would leak the user into thinking they are
        authenticated when they are not.
        """
        uid = validate_user_id(user_id)
        plat = normalize_platform(platform)
        spec = get_platform(plat)

        if not spec.auth_required:
            raise PlatformNotSupported(
                f"'{plat}' needs no login — {spec.note}"
            )

        cookies = _validate_cookies(cookies)
        has_pw = bool(username and password)
        if not cookies and not has_pw:
            raise CredentialsError(
                "nothing to store — supply cookies, or a username+password pair"
            )

        chosen = method or ("cookies" if cookies else "password")
        if chosen == "cookies" and not cookies:
            raise CredentialsError("method='cookies' but no cookies supplied")
        if chosen == "password" and not has_pw:
            raise CredentialsError("method='password' but username/password missing")
        if chosen == "password" and not spec.accepts_password:
            raise PlatformNotSupported(
                f"'{plat}' only supports cookie-based auth — {spec.note}"
            )
        if chosen not in spec.supported_methods:
            raise PlatformNotSupported(
                f"'{plat}' does not support method '{chosen}'. "
                f"Supported: {', '.join(spec.supported_methods) or 'none'}"
            )
        if chosen == "password" and spec.warn:
            log.warning(
                "storing username+password for %s (%s) — %s",
                plat, uid, spec.note,
            )

        env = self._read_envelope()
        users = env.setdefault("users", {})
        if uid in users:
            data_key, rec = self._load_user_record(env, uid)
            data = self._user_payload(rec, data_key)
        else:
            data_key = _new_data_key()
            data = {}

        cred = Credential(
            platform=plat,
            user_id=uid,
            cookies=cookies,
            username=username if has_pw else None,
            password=password if has_pw else None,
            method=chosen,
            notes=notes,
        )
        data[plat] = cred
        record = self._make_user_record(data, data_key, user_id=uid)
        users[uid] = record
        self._write_envelope(env)
        # Log the shape only — never the values.
        log.info("stored %s credential for '%s' via %s (%d cookies, fingerprint=%s)",
                 plat, uid, chosen, cred.cookie_count, cred.fingerprint)
        return cred

    def get(self, user_id: str, platform: str) -> Credential:
        """Fetch one credential with its secrets. Callers must not log it."""
        uid = validate_user_id(user_id)
        plat = normalize_platform(platform)
        env = self._read_envelope()
        data_key, rec = self._load_user_record(env, uid)
        data = self._user_payload(rec, data_key)
        cred = data.get(plat)
        if cred is None:
            raise CredentialNotFoundError(uid, plat)
        return cred

    def status(self, user_id: str) -> Dict[str, Any]:
        """Secret-free status for one user. This is what the API returns."""
        uid = validate_user_id(user_id)
        env = self._read_envelope()
        users = env.get("users") or {}
        rec = users.get(uid)
        stored: List[Dict[str, Any]] = []
        decodable = True
        if rec:
            try:
                data_key, rec2 = self._load_user_record(env, uid)
                data = self._user_payload(rec2, data_key)
                stored = [data[k].public() for k in sorted(data)]
            except (DecryptionError, CredentialsError):
                # A wrong key must NOT look like "no credentials" — that would
                # silently push the user to re-login and overwrite good data.
                decodable = False
        return {
            "user_id": uid,
            "path": str(self._path),
            "key_provider": self.key_provider_name,
            "profile_exists": bool(rec),
            "readable": decodable,
            "platforms": stored,
            "platform_count": len(stored),
            "platforms_needing_auth": [
                n for n in sorted(PLATFORMS) if PLATFORMS[n].auth_required
            ],
            "platforms_not_needing_auth": list(
                PLATFORMS[n].name for n in sorted(PLATFORMS)
                if not PLATFORMS[n].auth_required
            ),
        }

    def platforms_with_credentials(self, user_id: str) -> List[str]:
        uid = validate_user_id(user_id)
        env = self._read_envelope()
        if uid not in (env.get("users") or {}):
            raise NoSuchUserError(uid)
        data_key, rec = self._load_user_record(env, uid)
        return sorted(self._user_payload(rec, data_key).keys())

    def delete(self, user_id: str, platform: Optional[str] = None) -> Dict[str, Any]:
        """Delete one platform credential, or the whole user profile."""
        uid = validate_user_id(user_id)
        env = self._read_envelope()
        users = env.setdefault("users", {})
        if uid not in users:
            raise NoSuchUserError(uid)
        if platform is None:
            users.pop(uid, None)
            self._write_envelope(env)
            log.info("deleted credential profile for user '%s'", uid)
            return {"user_id": uid, "deleted": "all", "path": str(self._path)}

        plat = normalize_platform(platform)
        data_key, rec = self._load_user_record(env, uid)
        data = self._user_payload(rec, data_key)
        if plat not in data:
            raise CredentialNotFoundError(uid, plat)
        data.pop(plat)
        record = self._make_user_record(data, data_key, user_id=uid)
        users[uid] = record
        self._write_envelope(env)
        log.info("deleted %s credential for user '%s'", plat, uid)
        return {"user_id": uid, "deleted": plat, "path": str(self._path)}

    def touch(self, user_id: str, platform: str) -> None:
        """Record that a credential was just used (for staleness checks)."""
        uid = validate_user_id(user_id)
        plat = normalize_platform(platform)
        env = self._read_envelope()
        users = env.setdefault("users", {})
        if uid not in users:
            return
        try:
            data_key, rec = self._load_user_record(env, uid)
            data = self._user_payload(rec, data_key)
        except CredentialsError:
            return
        if plat not in data:
            return
        data[plat].last_used_at = _utc_now()
        record = self._make_user_record(data, data_key, user_id=uid)
        users[uid] = record
        self._write_envelope(env)

    # -- rotation -----------------------------------------------------------

    def rotate_master_key(self, new_provider: Optional[str] = None) -> Dict[str, Any]:
        """Re-wrap every user's data key under a freshly generated master key.

        The credential payloads are NOT re-encrypted — only the wrapper changes.
        That keeps rotation fast and means a failure mid-rotation cannot corrupt
        the data itself: the old envelope is only replaced once every user
        re-wraps successfully.
        """
        env = self._read_envelope()
        users = env.get("users") or {}
        old_master = self._key()

        # Build the whole new envelope in memory first — atomic by construction.
        new_master = _fresh_master_key(new_provider)
        new_users: Dict[str, Any] = {}
        for uid in sorted(users):
            rec = users[uid]
            data_key_b64 = decrypt_blob(rec["wrapped_key"], rec["nonce"], old_master)
            # data_key_b64 is the b64 of 32 raw bytes; wrap it verbatim under
            # the new master so the format stays stable.
            wrapped, nonce = encrypt_blob(data_key_b64, new_master)
            new_users[uid] = {
                **rec,
                "wrapped_key": wrapped,
                "nonce": nonce,
                "rotated_at": _utc_now(),
            }

        new_env = {
            "version": ENVELOPE_VERSION,
            "created_at": env.get("created_at", _utc_now()),
            "rotated_at": _utc_now(),
            "users": new_users,
        }
        self._write_envelope(new_env)
        # Persist the new master key in its provider's home.
        _persist_master_key(new_provider, new_master)
        self._provider = _resolve_key_provider(new_provider)
        log.info("rotated master key for %d user profile(s)", len(new_users))
        return {
            "rotated": True,
            "users": len(new_users),
            "key_provider": self.key_provider_name,
            "path": str(self._path),
        }

    def rotate_platform_credential(self, user_id: str, platform: str, **kwargs) -> Credential:
        """Replace a platform credential in place (re-`put`)."""
        return self.put(user_id, platform, **kwargs)

    def export_plaintext(self, user_id: str) -> str:
        """Decrypted JSON for one user — OFFLINE USE ONLY.

        There is no legitimate reason for a web request to call this; it exists
        so `captainauth export --to <path>` can write a backup the user asked
        for. The returned string is plaintext and must never be logged.
        """
        uid = validate_user_id(user_id)
        env = self._read_envelope()
        data_key, rec = self._load_user_record(env, uid)
        data = self._user_payload(rec, data_key)
        return json.dumps({n: c.to_dict() for n, c in data.items()}, indent=2,
                          sort_keys=True)


def _validate_cookies(
    cookies: Optional[Iterable[Dict[str, Any]]]
) -> Optional[List[Dict[str, Any]]]:
    """Normalise a cookie jar; drop unusable entries rather than failing."""
    if cookies is None:
        return None
    out: List[Dict[str, Any]] = []
    for c in cookies:
        if not isinstance(c, dict):
            continue
        if not c.get("name") or "value" not in c:
            log.warning("dropping cookie with no name/value during normalisation")
            continue
        entry = dict(c)
        entry["value"] = str(entry["value"])
        entry.setdefault("path", "/")
        if "domain" not in entry:
            log.warning("cookie %r stored without a domain", entry.get("name"))
        out.append(entry)
    return out


def _fresh_master_key(provider: Optional[str]) -> bytes:
    resolved = _resolve_key_provider(provider)
    if isinstance(resolved, PassphraseKeyProvider):
        return resolved.get_or_create()
    return secrets.token_bytes(KEY_BYTES)


def _persist_master_key(provider: Optional[str], key: bytes) -> None:
    """Write the master key where the chosen provider will look for it."""
    resolved = _resolve_key_provider(provider)
    if isinstance(resolved, KeyfileKeyProvider):
        _atomic_write(resolved._path(), _b64encode(key).encode("ascii"))
    elif isinstance(resolved, KeychainKeyProvider):
        kr = resolved._keyring()
        if kr is not None:
            kr.set_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, _b64encode(key))
    elif isinstance(resolved, PassphraseKeyProvider):
        _ensure_dir(default_data_dir())
        _atomic_write(
            default_data_dir() / "master.salt",
            _b64encode(secrets.token_bytes(SALT_BYTES)).encode("ascii"),
        )


# ---------------------------------------------------------------------------
# Module-level convenience API — the shape tests and the CLI both use
# ---------------------------------------------------------------------------

def get_store(path: Optional[Path] = None, key_provider: Optional[str] = None) -> CredentialStore:
    """Return a store. `path=None` means the platform-native auth.json."""
    return CredentialStore(path=path, key_provider=key_provider)


def set_credential(
    user_id: str,
    platform: str,
    path: Optional[Path] = None,
    **kwargs,
) -> Credential:
    return get_store(path).put(user_id, platform, **kwargs)


def get_credential(user_id: str, platform: str, path: Optional[Path] = None) -> Credential:
    return get_store(path).get(user_id, platform)


def credentials_status(user_id: str, path: Optional[Path] = None) -> Dict[str, Any]:
    return get_store(path).status(user_id)


def list_stored_platforms(user_id: str, path: Optional[Path] = None) -> List[str]:
    return get_store(path).platforms_with_credentials(user_id)


#: Re-export the platform catalog from :mod:`src.auth.platforms` so the web
#: layer can ``from .credentials import public_platforms`` without an extra
#: import line. ``public_platforms`` is pure spec data — no secrets.
public_platforms = None  # populated lazily to avoid a circular import


def _bind_public_platforms() -> None:
    global public_platforms
    if public_platforms is None:
        from .platforms import public_platforms as _pp
        public_platforms = _pp


_bind_public_platforms()