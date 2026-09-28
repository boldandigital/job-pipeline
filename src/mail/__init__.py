"""MAIL-1 — iCloud IMAP reply watcher.

Submodules:
  classifier  — pure-Python subject -> outcome regex mapping (EN + DE)
  matcher     — sender-domain / subject / body -> job_id lookup
  state       — JSON-backed last_seen_uid cursor
  watcher     — orchestrator (IMAP poll -> classify -> match -> DB write)
"""
from src.mail import classifier, matcher, state, watcher  # noqa: F401

__all__ = ["classifier", "matcher", "state", "watcher"]