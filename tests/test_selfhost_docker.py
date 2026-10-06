"""
Docker self-host integration test for CaptainApply Phase 1.5.

This test is gated by the `docker` marker, so the default test run
(`pytest` or `pytest tests/`) skips it. To run it:

    pytest -m docker tests/test_selfhost_docker.py -v

What it does:
  1. Skips itself if `docker` isn't installed or the daemon isn't reachable.
  2. Builds `captainapply-selfhost-test` from `Dockerfile.selfhost`.
  3. Runs the container on host port 18742, mapped to container 8742.
  4. Polls /api/health up to 30s — expects HTTP 200.
  5. POSTs to /api/v1/auth/login with default admin creds (lars / captain).
  6. Tears the container down (stop + rm).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
IMAGE_TAG = "captainapply-selfhost-test"
CONTAINER_NAME = "captainapply-test"
HOST_PORT = 18742
CONTAINER_PORT = 8742
HEALTH_URL = f"http://127.0.0.1:{HOST_PORT}/api/health"
LOGIN_URL = f"http://127.0.0.1:{HOST_PORT}/api/v1/auth/login"
DEFAULT_USER = os.environ.get("LARS_USER", "lars")
DEFAULT_PASS = os.environ.get("LARS_PASS", "captain")
HEALTH_TIMEOUT_S = 30


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(
            ["docker", "version"],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return True


def _run(args, **kwargs):
    """Helper: run a docker command, raise on failure."""
    return subprocess.run(
        ["docker", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
        **kwargs,
    )


def _cleanup_container() -> None:
    """Best-effort stop + rm so a failed test doesn't leave junk behind."""
    for cmd in (
        ["docker", "stop", CONTAINER_NAME],
        ["docker", "rm", "-f", CONTAINER_NAME],
    ):
        try:
            subprocess.run(cmd, capture_output=True, timeout=30)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            pass


def _wait_for_health(timeout_s: int = HEALTH_TIMEOUT_S) -> bool:
    """Poll /api/health; return True on first 200, False on timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(1)
    return False


@pytest.mark.docker
def test_selfhost_image_builds_and_serves_health():
    """Build, run, health-check, login, tear down."""
    if not _docker_available():
        pytest.skip("docker is not installed or daemon is not reachable")

    _cleanup_container()

    try:
        # 1) Build
        _run(
            [
                "build",
                "-f", "Dockerfile.selfhost",
                "-t", IMAGE_TAG,
                REPO_ROOT,
            ],
        )

        # 2) Run
        _run(
            [
                "run",
                "-d",
                "--name", CONTAINER_NAME,
                "-p", f"{HOST_PORT}:{CONTAINER_PORT}",
                "-e", f"LARS_USER={DEFAULT_USER}",
                "-e", f"LARS_PASS={DEFAULT_PASS}",
                "-e", "CAPTAIN_PROD=1",
                IMAGE_TAG,
            ],
        )

        # 3) Wait for /api/health
        assert _wait_for_health(), (
            f"/api/health did not respond within {HEALTH_TIMEOUT_S}s "
            f"on {HEALTH_URL}"
        )

        # 4) Login round-trip
        req = urllib.request.Request(
            LOGIN_URL,
            data=json.dumps(
                {"user_id": DEFAULT_USER, "password": DEFAULT_PASS}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200, f"login returned HTTP {resp.status}"
            body = json.loads(resp.read())
            assert body.get("ok") is True, f"login response not ok: {body}"
            assert body.get("user", {}).get("id") == DEFAULT_USER, (
                f"login returned wrong user: {body}"
            )

    finally:
        # 5) Cleanup
        _cleanup_container()
