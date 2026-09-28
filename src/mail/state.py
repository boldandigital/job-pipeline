"""MAIL-1 — persistence for the iCloud IMAP watcher.

Tiny JSON-backed key/value store for the per-folder ``last_seen_uid`` cursor.
Mirrors the lightweight pattern used elsewhere in job-pipeline (e.g. the
apply runner's audit log directory), and exists in its own module so:

  * tests can substitute a tmp path without monkey-patching the watcher
  * the watcher's read/write sites are obvious and reviewable
  * we can grow the schema later (multiple mailboxes, OAuth tokens, etc.)

Format on disk::

    {
        "INBOX": 12345,
        "Sent": 678,         # for matching our own "withdrawing" replies
        "_version": 1
    }

``_version`` lets us migrate the on-disk schema in future revisions without
guessing at file age.

Concurrency: a flock around the read-modify-write cycle (mtime race guard
when two watcher instances are running) — the watcher runs every 15 min
cron, but multiple invocations can overlap on slow IMAP handshakes.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict

VERSION = 1


def default_state_path(project_root: str | os.PathLike) -> Path:
    """``<project>/data/mail_state.json`` — alongside jobs.db."""
    return Path(project_root) / "data" / "mail_state.json"


def load_state(path: str | os.PathLike) -> Dict[str, Any]:
    """Read the state file. Returns ``{}`` if missing or corrupt.

    A corrupt file is logged but never fatal — the watcher treats it as
    "process every unseen UID", which is safe because every DB write is
    itself idempotent.
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(path: str | os.PathLike, state: Dict[str, Any]) -> None:
    """Atomic write: tmp file + rename, so a kill mid-write leaves the old
    file intact rather than a half-written corrupt one.

    Caller is responsible for fcntl if concurrent writers; we accept a
    file handle via :func:`with_lock` below for the canonical
    read-modify-write cycle.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".mail_state.", suffix=".tmp", dir=p.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp_path, p)
    except Exception:
        # Don't leave the .tmp lying around if rename fails.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def with_lock(path: str | os.PathLike):
    """Context manager: acquire an exclusive flock for the duration.

    Used as::

        with with_lock(state_path) as lock_path:
            state = load_state(lock_path)
            ... mutate ...
            save_state(lock_path, state)

    The flock is on a sibling ``.lock`` file (separate from the JSON file
    itself) so a failed atomic write doesn't leave the lock stuck mid-write.
    """
    return _with_lock_impl(path)


@contextmanager
def _with_lock_impl(path: str | os.PathLike):
    lock_path = str(path) + ".lock"
    p = Path(lock_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield lock_path
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()