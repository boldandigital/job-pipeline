"""End-to-end smoke test for ``bin/apply-cua.sh``.

Verifies:
  - ``bash -n`` passes (syntax OK)
  - ``--dry-run`` runs without crashing on `set -u` empty arrays
    (QA-12 bug regression: pre-initialize ``ARGS=()`` and ``SUBCMD_ARGS=()``).
  - ``--help`` exits 0 and prints usage.
  - Bad invocation exits non-zero with a clear error message.
  - Unknown flag rejected.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "apply-cua.sh"
VENV_PY = Path("/Users/lars/Documents/Projects/job-pipeline/.venv/bin/python")


def _run(*args: str, env_extra=None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # Point DB_PATH at the real repo's jobs.db so the smoke test exercises
    # the live data path. The worktree's data/ may not have it (fresh
    # checkout / incomplete setup).
    env["DB_PATH"] = "/Users/lars/Documents/Projects/job-pipeline/data/jobs.db"
    env["VENV_PY"] = str(VENV_PY)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, env=env, cwd=str(ROOT),
        timeout=30,
    )


def test_syntax_check():
    """`bash -n` should always pass — catches shell-syntax regressions."""
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


def test_help_exits_zero():
    result = _run("--help")
    assert result.returncode == 0
    assert "apply-cua" in result.stdout


def test_dry_run_does_not_crash_on_set_u():
    """QA-12 regression: ``ARGS=()`` and ``SUBCMD_ARGS=()`` must be initialised.

    Before the fix, ``${ARGS[@]}`` raised an unbound-variable error under
    ``set -u`` when no positional args were given.
    """
    result = _run("--dry-run")
    assert result.returncode == 0, (
        f"dry-run crashed (likely set -u + empty array):\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )


def test_dry_run_with_max_does_not_crash():
    result = _run("--dry-run", "--max", "3")
    assert result.returncode == 0


def test_dry_run_with_job_id_does_not_crash():
    result = _run("--dry-run", "--job-id", "42")
    assert result.returncode == 0


def test_max_clamped_to_5():
    """The ADOPT-12 hard cap is 5 applies per run; >5 must be clamped."""
    result = _run("--dry-run", "--max", "10")
    assert result.returncode == 0
    assert "clamping to 5" in result.stdout or "Max applies:  5" in result.stdout


def test_unknown_flag_rejected():
    result = _run("--this-flag-does-not-exist")
    assert result.returncode != 0
    assert "unknown flag" in (result.stdout + result.stderr).lower()


def test_missing_arg_for_job_id_rejected():
    result = _run("--job-id")
    assert result.returncode != 0
    assert "requires an argument" in (result.stdout + result.stderr).lower()


def test_missing_arg_for_max_rejected():
    result = _run("--max")
    assert result.returncode != 0


def test_missing_arg_for_delay_rejected():
    result = _run("--delay")
    assert result.returncode != 0
