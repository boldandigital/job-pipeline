"""MAIL-1 — iCloud IMAP watcher.

Polls lars.z@icloud.com every N minutes, classifies each unseen email
subject, matches it back to a job in the SQLite DB, and updates the
lifecycle columns. Fires a Discord notification on interview / offer.

Usage::

    python3 src/mail/watcher.py --once           # one poll, then exit
    python3 src/mail/watcher.py --dry-run        # one poll, no DB / Discord writes
    python3 src/mail/watcher.py --interval 10    # poll every 10 min (cron override)

Env::

    ICLOUD_APP_PASSWORD      required (raises on missing)
    MAIL_WATCHER_INTERVAL_MIN default 15 (used by the loop, not --once)
    DISCORD_BOT_TOKEN        shared with lars-daily-run.sh
    DISCORD_CHANNEL_ID       defaults to DISCORD_HOME_CHANNEL
    DB_PATH                  default ./data/jobs.db
    MAIL_FOLDERS             comma list, default "INBOX"

Idempotency: ``data/mail_state.json`` tracks ``last_seen_uid`` per folder.
Re-polling the same folder after no new mail is a no-op. UPDATE statements
skip writes when the new value equals the existing value, so
``mail_received_at`` doesn't drift forward on every poll.

Safety:
- ``--dry-run`` prints every proposed change without writing.
- Failures inside the per-email loop are caught + logged, never crash the
  whole poll (one bad Subject header should not stop the cron).
- Discord is best-effort — never raises into the DB-write path.
"""
from __future__ import annotations

import argparse
import imaplib
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Project imports — keep these inline (not package imports) so the watcher
# can be invoked as a script from anywhere on the box.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.mail import classifier, matcher, state  # noqa: E402

log = logging.getLogger("mail.watcher")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_IMAP_HOST = "imap.mail.me.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_IMAP_USER = "lars.z@icloud.com"

DEFAULT_DB_PATH = os.getenv("DB_PATH") or str(_PROJECT_ROOT / "data" / "jobs.db")
DEFAULT_STATE_PATH = str(_PROJECT_ROOT / "data" / "mail_state.json")
DEFAULT_INTERVAL_MIN = int(os.getenv("MAIL_WATCHER_INTERVAL_MIN", "15"))
DEFAULT_FOLDERS = [
    f.strip() for f in os.getenv("MAIL_FOLDERS", "INBOX").split(",") if f.strip()
]

NOTIFY_OUTCOMES = {classifier.OUTCOME_INTERVIEW, classifier.OUTCOME_OFFER}
DEFAULT_REJECTION_REASON = "email_rejection"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ParsedEmail:
    """Decoded email fields. All strings are unicode-clean."""
    uid: str
    from_header: str
    subject: str
    body: str
    date: Optional[datetime]  # tz-aware UTC


@dataclass
class ApplyOutcome:
    """Result of applying a single classified email to the DB."""
    uid: str
    job_id: Optional[int]
    outcome: str
    changed: bool           # True if a DB UPDATE actually changed a row
    match_signal: str = ""  # echo of matcher.Match.signal, if any
    company: str = ""
    title: str = ""
    subject: str = ""


# ---------------------------------------------------------------------------
# IMAP helpers
# ---------------------------------------------------------------------------


def connect_imap(
    user: str,
    password: str,
    host: str = DEFAULT_IMAP_HOST,
    port: int = DEFAULT_IMAP_PORT,
) -> imaplib.IMAP4_SSL:
    """Open an IMAP_SSL connection. Raises on auth/network failure with a
    human-readable message."""
    log.info("connecting to %s:%d as %s", host, port, user)
    conn = imaplib.IMAP4_SSL(host, port)
    typ, _ = conn.login(user, password)
    if typ != "OK":
        raise RuntimeError(f"iCloud IMAP login failed: {typ}")
    return conn


def list_unseen_uids(conn: imaplib.IMAP4_SSL, folder: str) -> List[str]:
    """Return UIDs of all messages in ``folder``.

    We don't use UNSEEN here — that flag clears when the message is opened
    in another client, which would silently drop new replies. UID-based
    ``last_seen_uid`` cursor is the correct mechanism for an "everything
    new since last poll" watcher.
    """
    typ, _ = conn.select(folder, readonly=True)
    if typ != "OK":
        raise RuntimeError(f"cannot select folder {folder!r}")
    typ, data = conn.uid("SEARCH", "", "ALL")
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].decode("utf-8", "replace").split()


def fetch_message(
    conn: imaplib.IMAP4_SSL, folder: str, uid: str,
) -> ParsedEmail:
    """Pull the message + decode headers + body."""
    typ, msg_data = conn.uid("FETCH", uid, "(RFC822)")
    if typ != "OK" or not msg_data or not msg_data[0]:
        raise RuntimeError(f"FETCH failed for uid {uid}")
    raw = msg_data[0][1]
    msg: EmailMessage = email.message_from_bytes(  # type: ignore[assignment]
        raw, policy=policy.default,
    )

    subject = str(make_header(decode_header(msg.get("Subject", ""))))
    from_header = str(msg.get("From", ""))

    body = _extract_body(msg)
    date = _parse_date(msg.get("Date", ""))

    return ParsedEmail(
        uid=uid,
        from_header=from_header,
        subject=subject,
        body=body,
        date=date,
    )


def _extract_body(msg: EmailMessage) -> str:
    """Walk multipart, prefer text/plain, fall back to text/html stripped."""
    parts: List[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        try:
            payload = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True) or b""
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", "replace")
        if ctype == "text/plain":
            parts.append(str(payload))
        elif ctype == "text/html" and not parts:
            # Stash html as fallback in case nothing else arrives.
            parts.append(_strip_html(str(payload)))
    return "\n".join(parts)


_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_ENT = re.compile(r"&(?:[a-z]+|#\d+);")


def _strip_html(s: str) -> str:
    s = _HTML_TAG.sub(" ", s)
    s = _HTML_ENT.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _parse_date(date_header: str) -> Optional[datetime]:
    if not date_header:
        return None
    try:
        dt = parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_mail_columns(conn: sqlite3.Connection) -> None:
    """Best-effort safety net for tests: if the migration wasn't run, the
    ALTER TABLE would crash on UPDATE. We add missing columns inline."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    for col, decl in [
        ("outcome", "TEXT"),
        ("mail_received_at", "TIMESTAMP"),
        ("last_email_subject", "TEXT"),
        ("last_email_at", "TIMESTAMP"),
        ("interview_at", "TIMESTAMP"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
    conn.commit()


def _apply_outcome(
    conn: sqlite3.Connection,
    job_id: int,
    outcome: str,
    subject: str,
    received_at: datetime,
    interview_at: Optional[datetime] = None,
) -> bool:
    """Apply a lifecycle update to a job row. Returns True iff a *state*
    field actually changed (idempotent re-polls return False).

    ``last_email_at`` + ``last_email_subject`` are always updated — they
    are audit fields ("did the watcher touch this row at all"), not
    lifecycle state. ``outcome``, ``mail_received_at``, ``interview_at``,
    and ``rejection_reason`` are lifecycle state and only fire on change.
    """
    row = conn.execute(
        """
        SELECT outcome, mail_received_at, last_email_at, last_email_subject,
               interview_at, rejection_reason
          FROM jobs WHERE id = ?
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        log.warning("job id=%s disappeared mid-poll; skipping", job_id)
        return False

    existing = dict(row)
    new_outcome = outcome
    lifecycle_updates: Dict[str, Any] = {}

    if outcome == classifier.OUTCOME_RECEIVED:
        # First-received timestamp is idempotent. Only set if currently NULL.
        if existing.get("mail_received_at") is None:
            lifecycle_updates["mail_received_at"] = received_at.isoformat(sep=" ")
    elif outcome == classifier.OUTCOME_INTERVIEW:
        if interview_at and existing.get("interview_at") is None:
            lifecycle_updates["interview_at"] = interview_at.isoformat(sep=" ")
    elif outcome == classifier.OUTCOME_REJECTED:
        # ADOPT-11 reserved rejection_reason for manual ★ marks; we use a
        # distinct enum key so analytics can split email vs manual rejects.
        if existing.get("rejection_reason") is None:
            lifecycle_updates["rejection_reason"] = DEFAULT_REJECTION_REASON

    # Outcome transitions are forward-only. Once 'offer' / 'rejected' is
    # set, we don't overwrite it with 'received' on the next poll. But
    # same-value writes also don't count as a state change (UPDATEing
    # outcome=offer when outcome=offer is a no-op for the lifecycle).
    if (
        new_outcome != existing.get("outcome")
        and _should_set_outcome(existing.get("outcome"), new_outcome)
    ):
        lifecycle_updates["outcome"] = new_outcome

    # Audit fields — always refresh so we know "the watcher touched this
    # row on date X about subject Y". These don't count as state change.
    audit_updates: Dict[str, Any] = {
        "last_email_at": received_at.isoformat(sep=" "),
        "last_email_subject": subject,
        "updated_at": received_at.isoformat(sep=" "),
    }

    all_updates = {**audit_updates, **lifecycle_updates}
    if not all_updates:
        return False

    set_clause = ", ".join(f"{k} = ?" for k in all_updates)
    conn.execute(
        f"UPDATE jobs SET {set_clause} WHERE id = ?",
        (*all_updates.values(), job_id),
    )
    conn.commit()
    return bool(lifecycle_updates)


# Outcome precedence: a row can only move forward in this list. Going back
# from offer to received would be wrong (and almost certainly a classifier
# false positive). Going from rejected to interview IS allowed — the
# candidate could have been "moved forward" after a rejection.
_OUTCOME_RANK = {
    None: 0,
    classifier.OUTCOME_RECEIVED: 1,
    classifier.OUTCOME_INTERVIEW: 2,
    classifier.OUTCOME_OFFER: 3,
    classifier.OUTCOME_REJECTED: 2,    # parallel to interview
    classifier.OUTCOME_DECLINED: 3,
}


def _should_set_outcome(current: Optional[str], candidate: str) -> bool:
    if candidate == classifier.OUTCOME_UNKNOWN:
        return False
    if current is None:
        return True
    return _OUTCOME_RANK.get(candidate, 0) >= _OUTCOME_RANK.get(current, 0)


_INTERVIEW_AT_REGEXES = [
    # "on 12 October 2026" / "on October 12, 2026"
    re.compile(r"\bon\s+(?P<d>\d{1,2}\s+\w+\s+\d{4})\b", re.IGNORECASE),
    re.compile(
        r"\bon\s+(?P<d>(?:monday|tuesday|wednesday|thursday|friday)"
        r"(?:\s+\d{1,2}(?:st|nd|rd|th)?)?(?:,?\s+\d{4})?)",
        re.IGNORECASE,
    ),
    # "am 12.10.2026" — German DACH
    re.compile(r"\bam\s+(?P<d>\d{1,2}\.\s*\d{1,2}\.\s*\d{4})\b", re.IGNORECASE),
    # "next Monday/Tuesday/..."
    re.compile(
        r"\bnext\s+(?P<d>(?:monday|tuesday|wednesday|thursday|friday))\b",
        re.IGNORECASE,
    ),
    # "schedule|calendar link <URL or date>" — Greenhouse/Workday templates
    re.compile(r"\b(?:schedule|calendar)\s+link[^\n]*?(?P<d>\d{4}-\d{2}-\d{2})"),
    # "calendar is open until 2026-10-15" — Calendly copy
    re.compile(
        r"\b(?:calendar|booking|available|free|open)\b[^\n]*?(?P<d>\d{4}-\d{2}-\d{2})",
        re.IGNORECASE,
    ),
]


def _parse_interview_at(text: str) -> Optional[datetime]:
    """Best-effort: pull a date out of an interview-invite body.

    Returns UTC datetime or None. Only fires when classifier already said
    interview, so the noise level is acceptable.
    """
    for rx in _INTERVIEW_AT_REGEXES:
        m = rx.search(text or "")
        if not m:
            continue
        d = m.group("d")
        # Try ISO first (most precise).
        try:
            return datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        try:
            return datetime.strptime(d, "%d %B %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        try:
            return datetime.strptime(d.replace("  ", " "), "%d.%m.%Y").replace(
                tzinfo=timezone.utc,
            )
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Discord notification
# ---------------------------------------------------------------------------


def _discord_send(msg: str) -> bool:
    """Send a Discord message via the same env-var shim used by apply/runner.

    Best-effort: swallows + logs failures so the DB-write path is never
    blocked by a transient Discord 5xx.
    """
    bot_token = os.getenv("DISCORD_BOT_TOKEN")
    channel_id = os.getenv("DISCORD_CHANNEL_ID")
    if not (bot_token and channel_id):
        log.debug("Discord not configured — skipping notification")
        return False
    # Reuse the apply-runner helper to keep the wire format identical.
    try:
        # Inline import to avoid pulling apply.* into the mail unit tests.
        import importlib
        runner = importlib.import_module("src.apply.runner")
        return bool(runner.discord_send(msg))
    except Exception as e:  # pragma: no cover — best-effort
        log.warning("discord send failed: %s", e)
        return False


def _format_discord(
    outcome: str, m: matcher.Match, email: ParsedEmail,
) -> str:
    emoji = {"interview": "📞", "offer": "🎉"}.get(outcome, "📬")
    label = "interview invitation" if outcome == classifier.OUTCOME_INTERVIEW else "offer"
    when = email.date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") \
        if email.date else "unknown"
    return (
        f"{emoji} Lars: {label} detected\n"
        f"Company: {m.company}\n"
        f"Role: {m.title}\n"
        f"Subject: \"{email.subject}\"\n"
        f"Time: {when}\n"
        f"Match: {m.match_detail}"
    )


# ---------------------------------------------------------------------------
# Watcher core
# ---------------------------------------------------------------------------


def process_email(
    conn: sqlite3.Connection,
    email: ParsedEmail,
    *,
    dry_run: bool = False,
) -> ApplyOutcome:
    """Classify + match + (optionally) apply a single email. Returns the
    summary — caller (or tests) decides what to do with it."""
    classification = classifier.classify(email.subject, email.body)
    match = matcher.match_email(conn, email.from_header, email.subject, email.body)

    received_at = email.date or datetime.now(tz=timezone.utc)

    if match is None:
        log.info(
            "uid=%s subject=%r — no job match, skipping (outcome=%s)",
            email.uid, email.subject[:60], classification.outcome,
        )
        return ApplyOutcome(
            uid=email.uid, job_id=None, outcome=classification.outcome,
            changed=False,
        )

    interview_at = (
        _parse_interview_at(email.body)
        if classification.outcome == classifier.OUTCOME_INTERVIEW else None
    )

    changed = False
    if not dry_run:
        changed = _apply_outcome(
            conn, match.job_id, classification.outcome,
            subject=email.subject, received_at=received_at,
            interview_at=interview_at,
        )
    else:
        log.info(
            "[DRY-RUN] uid=%s → job=%s (%s) outcome=%s",
            email.uid, match.job_id, match.signal, classification.outcome,
        )

    if (
        not dry_run
        and changed
        and classification.outcome in NOTIFY_OUTCOMES
    ):
        _discord_send(_format_discord(classification.outcome, match, email))

    return ApplyOutcome(
        uid=email.uid, job_id=match.job_id,
        outcome=classification.outcome, changed=changed,
        match_signal=match.signal, company=match.company,
        title=match.title, subject=email.subject,
    )


def poll_once(
    *,
    db_path: str = DEFAULT_DB_PATH,
    state_path: str = DEFAULT_STATE_PATH,
    folders: Optional[List[str]] = None,
    password: Optional[str] = None,
    dry_run: bool = False,
) -> List[ApplyOutcome]:
    """One polling cycle across all configured folders.

    Returns the per-email outcomes (also logged). The caller drives the
    loop cadence (cron for production, ``--once`` from CLI).
    """
    folders = folders or DEFAULT_FOLDERS
    password = password or os.getenv("ICLOUD_APP_PASSWORD")
    if not password:
        raise RuntimeError(
            "ICLOUD_APP_PASSWORD not set — see .env.example. "
            "Generate an app-specific password at appleid.apple.com.",
        )

    conn = _connect(db_path)
    _ensure_mail_columns(conn)

    state_now = state.load_state(state_path)
    state_now.setdefault("_version", state.VERSION)

    results: List[ApplyOutcome] = []

    for folder in folders:
        try:
            imap = connect_imap(DEFAULT_IMAP_USER, password)
        except Exception as e:
            log.error("IMAP connect failed for folder=%s: %s", folder, e)
            continue
        try:
            uids = list_unseen_uids(imap, folder)
            last_seen = state_now.get(folder)
            new_uids = (
                [u for u in uids if int(u) > int(last_seen)]
                if last_seen is not None else uids
            )
            log.info(
                "folder=%s total_uids=%d last_seen=%s new=%d",
                folder, len(uids), last_seen, len(new_uids),
            )
            for uid in new_uids:
                try:
                    email = fetch_message(imap, folder, uid)
                    outcome = process_email(conn, email, dry_run=dry_run)
                    results.append(outcome)
                except Exception as e:
                    log.warning("uid=%s failed: %s", uid, e)
                    continue
            if new_uids:
                state_now[folder] = max(int(u) for u in new_uids)
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    if not dry_run and results:
        with state.with_lock(state_path):
            state_now = state.load_state(state_path)
            state_now.setdefault("_version", state.VERSION)
            for folder in folders:
                # Find max uid we saw for this folder in this run.
                folder_max = max(
                    (
                        int(r.uid) for r in results if r.changed
                    ),
                    default=None,
                )
                if folder_max is not None:
                    state_now[folder] = max(
                        state_now.get(folder, 0) or 0, folder_max,
                    )
            state.save_state(state_path, state_now)

    conn.close()
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="iCloud IMAP reply watcher.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true",
                   help="one polling cycle then exit (default for cron)")
    g.add_argument("--loop", action="store_true",
                   help="keep polling every MAIL_WATCHER_INTERVAL_MIN min")
    p.add_argument("--dry-run", action="store_true",
                   help="classify + match without writing DB or Discord")
    p.add_argument("--interval", type=int, default=None,
                   help="override MAIL_WATCHER_INTERVAL_MIN for --loop")
    p.add_argument("--folder", action="append", default=None,
                   help="IMAP folder to watch (repeatable). "
                        "Defaults to MAIL_FOLDERS env / INBOX.")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    folders = args.folder
    interval_min = args.interval or DEFAULT_INTERVAL_MIN

    if args.loop:
        log.info("entering poll loop, every %d min", interval_min)
        while True:
            try:
                poll_once(folders=folders, dry_run=args.dry_run)
            except Exception as e:
                log.exception("poll cycle failed: %s", e)
            time.sleep(max(60, interval_min * 60))
    else:
        # Default + --once are the same: one cycle, exit.
        results = poll_once(folders=folders, dry_run=args.dry_run)
        log.info("polled %d email(s)", len(results))
        for r in results:
            log.info(
                "  uid=%s job=%s outcome=%s changed=%s",
                r.uid, r.job_id, r.outcome, r.changed,
            )
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())