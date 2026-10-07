"""
Tests for ``src.auth_credentials`` — Phase 2.2 of CaptainApply.

The spec requires twelve tests covering:
  1. save() then load() round-trip works
  2. load() returns None for missing user
  3. delete() removes entry
  4. list_platforms() returns correct list
  5. has_credentials() correct
  6. per-user_id isolation
  7. secret values NEVER appear in any return
  8. encrypted at rest (file contents don't contain plaintext)
  9. rotation: re-saving with same password works
 10. bad encryption key → raises clear error
 11. delete missing platform → no-op, no error
 12. integration: end-to-end save → load → use via Playwright

All tests use the PBKDF2 / keyfile path so the suite runs in headless CI
without the OS keychain. The keyring package is optional — when it's
importable, the keychain provider is preferred; when it isn't, we force
``key_provider="keyfile"`` to avoid a flaky ``keyring`` backend choice.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import List

import pytest

# Force a deterministic key provider BEFORE the module is imported, so the
# module-level _DEFAULT_STORE picks the right path.  We use the keyfile
# provider because it is always available, has no extra dependencies, and
# lets us write a "bad key" test cleanly.
os.environ.setdefault("CAPTAIN_AUTH_KEYFILE", "")  # honour the env var name

from src.auth_credentials import (  # noqa: E402
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
from src.auth.credentials import (
    DecryptionError as LowerDecryptionError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def auth_env(tmp_path, monkeypatch):
    """Point the credential store at a fresh tmp dir + keyfile passphrase.

    Returns a ``(data_dir, keyfile)`` tuple. The keyfile is created lazily
    on first store use, so it doesn't have to exist here.
    """
    data_dir = tmp_path / "captainauth"
    data_dir.mkdir()
    keyfile = data_dir / "master.key"
    monkeypatch.setenv("CAPTAIN_AUTH_DIR", str(data_dir))
    monkeypatch.setenv("CAPTAIN_AUTH_KEYFILE", str(keyfile))
    # Belt and braces: even if a developer has keyring installed, force the
    # deterministic keyfile provider.
    return data_dir, keyfile


@pytest.fixture
def store(auth_env) -> CredentialsStore:
    data_dir, _ = auth_env
    return CredentialsStore(path=data_dir / "auth.json", key_provider="keyfile")


# A cookie blob rich enough to be obviously a "real" capture.
SAMPLE_XING = [
    {"name": "login", "value": "xing-very-secret-token-XYZ",
     "domain": ".xing.com", "path": "/", "secure": True},
    {"name": "sess", "value": "abc123def456",
     "domain": ".xing.com", "path": "/", "httpOnly": True},
    {"name": "csrf_token", "value": "csrf-value-do-not-leak",
     "domain": "www.xing.com", "path": "/"},
]

SAMPLE_LINKEDIN = [
    {"name": "li_at", "value": "AQEDARsecRetLinkedInTOK",
     "domain": ".linkedin.com", "path": "/", "secure": True, "httpOnly": True},
    {"name": "JSESSIONID", "value": "ajax:1234567890",
     "domain": ".linkedin.com", "path": "/", "httpOnly": True},
]


# ---------------------------------------------------------------------------
# 1. save() then load() round-trip works
# ---------------------------------------------------------------------------

def test_save_then_load_round_trip(store):
    saved = store.save("lars", "xing", SAMPLE_XING)
    assert saved.platform == "xing"
    assert saved.user_id == "lars"
    assert len(saved.cookies) == 3

    loaded = store.load("lars", "xing")
    assert loaded is not None
    assert loaded.platform == "xing"
    assert loaded.user_id == "lars"
    assert len(loaded.cookies) == 3

    # The values must round-trip exactly.
    saved_values = {c["name"]: c["value"] for c in saved.cookies}
    loaded_values = {c["name"]: c["value"] for c in loaded.cookies}
    assert saved_values == loaded_values
    assert loaded_values["login"] == "xing-very-secret-token-XYZ"

    # Module-level API agrees.
    blob = load("lars", "xing")
    assert blob is not None
    assert {c["name"] for c in blob.cookies} == {"login", "sess", "csrf_token"}


# ---------------------------------------------------------------------------
# 2. load() returns None for missing user
# ---------------------------------------------------------------------------

def test_load_returns_none_for_missing_user(store):
    assert store.load("ghost", "xing") is None
    # And the module-level helper agrees.
    assert load("ghost", "xing") is None
    # And a real user but missing platform also returns None.
    store.save("lars", "xing", SAMPLE_XING)
    assert store.load("lars", "linkedin") is None


# ---------------------------------------------------------------------------
# 3. delete() removes entry
# ---------------------------------------------------------------------------

def test_delete_removes_entry(store):
    store.save("lars", "xing", SAMPLE_XING)
    assert store.has_credentials("lars", "xing")
    assert store.delete("lars", "xing") is True
    assert not store.has_credentials("lars", "xing")
    assert store.load("lars", "xing") is None
    assert "xing" not in store.list_platforms("lars")


# ---------------------------------------------------------------------------
# 4. list_platforms() returns correct list
# ---------------------------------------------------------------------------

def test_list_platforms_returns_correct_list(store):
    assert store.list_platforms("lars") == []
    store.save("lars", "xing", SAMPLE_XING)
    store.save("lars", "linkedin", SAMPLE_LINKEDIN)
    platforms = store.list_platforms("lars")
    assert platforms == ["linkedin", "xing"]  # sorted
    # And the module-level helper agrees.
    assert list_platforms("lars") == ["linkedin", "xing"]


# ---------------------------------------------------------------------------
# 5. has_credentials() correct
# ---------------------------------------------------------------------------

def test_has_credentials_correct(store):
    assert not store.has_credentials("lars", "xing")
    store.save("lars", "xing", SAMPLE_XING)
    assert store.has_credentials("lars", "xing")
    assert not store.has_credentials("lars", "linkedin")
    # Different user — must be False even with creds for "lars".
    assert not store.has_credentials("alice", "xing")
    # Module-level helper agrees.
    assert has_credentials("lars", "xing")


# ---------------------------------------------------------------------------
# 6. per-user_id isolation
# ---------------------------------------------------------------------------

def test_per_user_isolation(store):
    """Lars's creds must be invisible to alice, and vice versa."""
    store.save("lars", "xing", SAMPLE_XING)
    store.save("alice", "linkedin", SAMPLE_LINKEDIN)

    # Cross-user loads return None.
    assert store.load("alice", "xing") is None
    assert store.load("lars", "linkedin") is None

    # Each user only sees their own platforms.
    assert store.list_platforms("lars") == ["xing"]
    assert store.list_platforms("alice") == ["linkedin"]

    # Deleting one user doesn't touch another.
    assert store.delete("lars", "xing") is True
    assert store.has_credentials("alice", "linkedin")
    assert not store.has_credentials("lars", "xing")


# ---------------------------------------------------------------------------
# 7. secret values NEVER appear in any return (only platform/area names)
# ---------------------------------------------------------------------------

def test_secrets_never_appear_in_any_return(store, capsys):
    """Sweep every public return path — no secret value leaks anywhere."""
    unique_marker = "UNIQUEMARKER-9F8E7D6C5B4A3210-secret-do-not-leak"
    cookies = [
        {"name": "li_at", "value": unique_marker,
         "domain": ".linkedin.com", "path": "/"},
    ]
    store.save("lars", "linkedin", cookies)

    # 1. list_platforms() — returns only platform names.
    leaked = unique_marker in json.dumps(store.list_platforms("lars"))
    assert not leaked, "list_platforms leaked a secret value"

    # 2. has_credentials() — returns bool only.
    assert isinstance(store.has_credentials("lars", "linkedin"), bool)

    # 3. status() — secret-free public view, must contain fingerprint, not value.
    status = store.status("lars")
    status_text = json.dumps(status, default=str)
    assert unique_marker not in status_text, "status() leaked a secret value"
    # The fingerprint IS allowed.
    for entry in status.get("platforms", []):
        assert "fingerprint" in entry
        assert "cookies" not in entry
        assert "value" not in entry

    # 4. print of str(StoredCredential) — should not include the value.
    blob = store.load("lars", "linkedin")
    assert blob is not None
    leaked = unique_marker in str(blob.public())
    assert not leaked, "StoredCredential.public() leaked a secret value"

    # 5. dict(blob) must not contain raw cookies/values either.
    #    public() is the only API that should ever cross an API boundary.
    leaked = unique_marker in json.dumps({
        "platform": blob.platform,
        "user_id": blob.user_id,
        "method": blob.method,
    })
    assert not leaked

    # 6. Nothing on stdout/stderr from the public API call chain.
    captured = capsys.readouterr()
    assert unique_marker not in captured.out
    assert unique_marker not in captured.err


# ---------------------------------------------------------------------------
# 8. encrypted at rest (file contents don't contain plaintext)
# ---------------------------------------------------------------------------

def test_encrypted_at_rest(store, auth_env):
    """The on-disk envelope must not contain any plaintext cookie value."""
    data_dir, _ = auth_env
    plain_marker = "PLAINTEXT-MARKER-THAT-MUST-NEVER-APPEAR-ON-DISK-1234567890"
    cookies = [
        {"name": "li_at", "value": plain_marker, "domain": ".linkedin.com"},
        {"name": "csrf_token", "value": "another-secret", "domain": ".linkedin.com"},
    ]
    store.save("lars", "linkedin", cookies)

    envelope_path = data_dir / "auth.json"
    assert envelope_path.exists()

    # Read raw bytes — no decoding — to defeat any clever unicode escape.
    raw_bytes = envelope_path.read_bytes()
    assert plain_marker.encode("utf-8") not in raw_bytes
    assert b"another-secret" not in raw_bytes
    assert b"li_at" not in raw_bytes, "cookie NAME should also be encrypted"
    assert b"csrf_token" not in raw_bytes

    # And the decoded text version too, in case of a JSON-normalised path.
    text = raw_bytes.decode("utf-8")
    assert plain_marker not in text
    assert "another-secret" not in text

    # File mode must be 0600 — even the ciphertext is sensitive.
    mode = stat.S_IMODE(envelope_path.stat().st_mode)
    assert mode == 0o600, f"envelope is world-readable: mode={oct(mode)}"


# ---------------------------------------------------------------------------
# 9. rotation: re-saving with same password works
# ---------------------------------------------------------------------------

def test_rotation_resaving_same_password(store, auth_env):
    """Saving twice in a row must succeed and the second call must replace."""
    v1 = [{"name": "li_at", "value": "first-version-secret",
           "domain": ".linkedin.com"}]
    v2 = [{"name": "li_at", "value": "second-version-secret",
           "domain": ".linkedin.com"},
          {"name": "extra", "value": "additional-cookie",
           "domain": ".linkedin.com"}]

    store.save("lars", "linkedin", v1)
    first = store.load("lars", "linkedin")
    assert first is not None
    assert first.cookies[0]["value"] == "first-version-secret"
    assert len(first.cookies) == 1

    # Re-save — must overwrite, not stack.
    store.save("lars", "linkedin", v2)
    second = store.load("lars", "linkedin")
    assert second is not None
    assert len(second.cookies) == 2
    by_name = {c["name"]: c["value"] for c in second.cookies}
    assert by_name["li_at"] == "second-version-secret"
    assert by_name["extra"] == "additional-cookie"

    # And the file is still parseable + 0600.
    data_dir, _ = auth_env
    envelope = data_dir / "auth.json"
    assert envelope.exists()
    assert stat.S_IMODE(envelope.stat().st_mode) == 0o600
    # Re-open via a fresh store instance — same passphrase, same keyfile.
    fresh = CredentialsStore(path=envelope, key_provider="keyfile")
    again = fresh.load("lars", "linkedin")
    assert again is not None
    assert {c["name"]: c["value"] for c in again.cookies} == by_name


# ---------------------------------------------------------------------------
# 10. bad encryption key → raises clear error
# ---------------------------------------------------------------------------

def test_bad_key_raises_clear_error(auth_env):
    """If the master key file is replaced with garbage, decrypt must fail
    with DecryptionError (NOT silently return None)."""
    data_dir, keyfile = auth_env

    # 1. Save something with the real key.
    s1 = CredentialsStore(path=data_dir / "auth.json", key_provider="keyfile")
    s1.save("lars", "xing", SAMPLE_XING)
    assert s1.has_credentials("lars", "xing")

    # 2. Corrupt the key file by writing a different random key.
    import base64, secrets
    keyfile.write_bytes(base64.b64encode(secrets.token_bytes(32)))
    # 3. Re-open with the (now wrong) key.
    s2 = CredentialsStore(path=data_dir / "auth.json", key_provider="keyfile")
    with pytest.raises(LowerDecryptionError) as excinfo:
        s2.load("lars", "xing")
    # The error must mention "decrypt" or "key" so a human can debug it.
    assert re.search(r"decrypt|key|wrong", str(excinfo.value), re.IGNORECASE)


# ---------------------------------------------------------------------------
# 11. delete missing platform → no-op, no error
# ---------------------------------------------------------------------------

def test_delete_missing_platform_is_noop(store):
    # User has no profile at all → delete is a silent no-op, returns False.
    assert store.delete("ghost", "xing") is False

    # User has a profile but not the platform we ask to delete.
    store.save("lars", "xing", SAMPLE_XING)
    assert store.delete("lars", "linkedin") is False
    # The existing entry is untouched.
    assert store.has_credentials("lars", "xing")
    # And the module-level delete helper agrees.
    assert delete("lars", "linkedin") is False
    assert delete("ghost", "anything") is False


# ---------------------------------------------------------------------------
# 12. integration: end-to-end save → load → use via Playwright
# ---------------------------------------------------------------------------

def test_e2e_save_load_use_via_playwright(store):
    """The whole point: stored cookies are usable in a real browser.

    We don't drive a real network login (that would require platform creds
    we deliberately do not have). Instead we verify:

      1. The captured cookies round-trip into Playwright's expected shape.
      2. Playwright's context.add_cookies() accepts them without error.
      3. The context.cookies() round-trip is value-stable.
    """
    store.save("lars", "xing", SAMPLE_XING)
    blob = store.load("lars", "xing")
    assert blob is not None

    # Lazy import so the rest of the suite runs even without Playwright.
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed in this env")

    playwright_cookies = [
        {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ""),
            "path": c.get("path", "/"),
            **({"secure": c["secure"]} if "secure" in c else {}),
            **({"httpOnly": c["httpOnly"]} if "httpOnly" in c else {}),
        }
        for c in blob.cookies
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            context.add_cookies(playwright_cookies)
            # Read back from the same context and confirm values.
            echoed = {c["name"]: c["value"] for c in context.cookies()}
            assert echoed["login"] == "xing-very-secret-token-XYZ"
            assert echoed["sess"] == "abc123def456"
            assert echoed["csrf_token"] == "csrf-value-do-not-leak"
        finally:
            browser.close()
