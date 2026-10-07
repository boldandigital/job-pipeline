"""
Apply orchestrator — turns approved dashboard cards into real applications.

The coordinator for the ADOPT-12 apply pipeline. It owns four things and
delegates everything else:

  1. **The approval gate** — a job is only ever applied to when its
     ``jobs.status == 'approved'``. Anything else is refused with
     ``error='status_gate'`` before an adapter is touched. This is the
     single most important safety invariant in the file.

  2. **Channel routing** — each job is classified into one channel:
       - ``api``     — Greenhouse / Lever / Ashby, submitted by HTTP POST
       - ``browser`` — XING / LinkedIn / Indeed / Workday, driven by
                       Playwright with the user's stored cookies
       - ``unknown`` — anything else, refused (never guessed at)

  3. **The daily cap** — 20 submissions per user per calendar day,
     persisted in an atomic JSON file so two concurrent runs cannot
     double-bump past the limit.

  4. **Persistence + notification** — writes ``status``/``applied_at`` to
     SQLite and fires a Discord webhook per outcome.

Channel contracts — WHAT WE ACTUALLY VERIFIED
---------------------------------------------
An earlier draft of this module (and the brief that seeded it) asserted
that Greenhouse, Lever and Ashby expose "public apply APIs that explicitly
accept email + resume uploads" usable by a candidate. That is not true.
Each endpoint was probed live before this implementation was written, and
each refused an unauthenticated caller:

  * **Greenhouse** — the real submission endpoint is
    ``POST https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{id}``
    and it requires **HTTP Basic auth with an employer-issued Job Board API
    key**. Greenhouse's docs state: "Only the application submission
    endpoint requires Basic Auth." Probed: a POST without a key returns
    ``401 HTTP Basic: Access denied.`` There is no ``/v1/applications``
    endpoint at all — the earlier draft's URL does not exist.
    Requires ``GREENHOUSE_JOB_BOARD_API_KEY``.

  * **Lever** — the real endpoint is
    ``POST https://api.lever.co/v0/postings/{site}/{posting_id}?key={APIKEY}``
    — note the ``{site}`` path segment the earlier draft omitted — and
    ``key`` is a posting-form API key minted by a Lever **Super Admin** for
    their own account. Lever also only accepts a resume in
    multipart/form-data mode. Probed: without a valid key it returns
    ``400 {"ok":false}``. Requires ``LEVER_API_KEY``.

  * **Ashby** — the public posting API is **read-only**
    (``GET /posting-api/job-board/{board}``). Probing the apply route
    unauthenticated returns ``401 Unauthorized``, and the bare
    ``/job-board/apply`` path the earlier draft used (no board segment) is
    not a candidate endpoint. Requires ``ASHBY_API_KEY``.

The consequence is the important part: **the API channel is real but
credential-gated, and in practice a candidate almost never holds those
keys — they belong to the employer.** So the browser channel is the default
and always-available path. When the API channel is unavailable we do NOT
fail the job; we downgrade to the browser channel, which pauses for human
submit exactly as the safety contract requires anyway.

Safety invariants (HARD — never relax):
  - ``status != 'approved'``   -> refused, no adapter invoked.
  - ``applied_at IS NOT NULL`` -> skipped. Idempotent.
  - Browser-channel adapters MUST return ``paused_for_review=True`` and
    MUST NOT invoke any submit-like driver call. The user clicks Submit.
  - Over the daily cap -> refused with ``error='daily_cap'``, not queued.
  - Failures leave ``applied_at`` NULL and status ``failed`` so the next
    run retries; nothing is lost.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.apply.ats_adapters.base import (
    ApplyResult,
    Profile,
    apply_pause_for_review,
    parse_master_profile,
)

log = logging.getLogger("apply.orchestrator")


def get_credential(user_id: str, platform: str) -> Any:  # pragma: no cover — see _credentials
    """Wrapper around :func:`src.auth.credentials.get_credential`.

    Imported lazily on first call so the orchestrator still loads where the
    auth module is absent, and so tests can patch one module attribute
    instead of reaching into ``sys.modules``.
    """
    from src.auth.credentials import get_credential as _real
    return _real(user_id, platform)


# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent  # src/apply -> job-pipeline/

DEFAULT_DB_PATH = os.getenv("DB_PATH") or str(_PROJECT_ROOT / "data" / "jobs.db")
DEFAULT_RATE_LIMIT_PATH = os.getenv("DAILY_APPLY_LIMIT_PATH") or str(
    _PROJECT_ROOT / "data" / "daily_apply_count.json"
)
DEFAULT_PROFILE_PATH = os.getenv("CV_MASTER_PROFILE")
DEFAULT_DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")

#: Hard cap: 20 submissions per user per calendar day.
DEFAULT_DAILY_CAP = int(os.getenv("DAILY_APPLY_CAP", "20"))

#: Whose jobs this SQLite file holds. Drives credential + rate-limit scoping.
DEFAULT_USER_ID = os.getenv("CAPTAIN_USER_ID", "lars")

# ---------------------------------------------------------------------------
# Channel classification
# ---------------------------------------------------------------------------
#
# Three sources of truth in this codebase, and they disagree in useful ways:
#   1. ``jobs.ats_type`` — which ATS hosts the destination form
#   2. the job URL host  — what ATS detection actually observed
#   3. ``jobs.source``   — where the listing was scraped from
#
# ``ats_type`` wins because a Greenhouse-hosted role scraped off LinkedIn
# must apply *through Greenhouse* — that is what actually accepts the
# application. The URL host is next, then ``source``.

API_CHANNELS = frozenset({"greenhouse", "lever", "ashby"})
BROWSER_CHANNELS = frozenset({"xing", "linkedin", "indeed", "workday"})

#: Host -> platform. Mirrors ``src.mail.matcher._KNOWN_ATS_DOMAINS`` plus the
#: job-board hosts we route on, so we don't add a fourth inconsistent table.
_HOST_PLATFORM: Tuple[Tuple[str, str], ...] = (
    ("boards.greenhouse.io", "greenhouse"),
    ("job-boards.greenhouse.io", "greenhouse"),
    ("greenhouse.io", "greenhouse"),
    ("jobs.lever.co", "lever"),
    ("lever.co", "lever"),
    ("jobs.ashbyhq.com", "ashby"),
    ("ashbyhq.com", "ashby"),
    ("myworkdayjobs.com", "workday"),
    ("myworkdaysite.com", "workday"),
    ("workday.com", "workday"),
    ("xing.com", "xing"),
    ("linkedin.com", "linkedin"),
    ("indeed.com", "indeed"),
)


def _platform_from_url(url: Optional[str]) -> str:
    """Best-effort platform name from a job URL. '' when unrecognised."""
    if not url:
        return ""
    lowered = url.lower()
    # Longest host first so boards.greenhouse.io beats greenhouse.io.
    for host, platform in sorted(_HOST_PLATFORM, key=lambda kv: -len(kv[0])):
        if host in lowered:
            return platform
    return ""


def _classify(job: Mapping[str, Any]) -> Tuple[str, str]:
    """Return ``(channel, platform)`` where channel is api/browser/unknown."""
    ats_type = (job.get("ats_type") or "").strip().lower()
    source = (job.get("source") or "").strip().lower()

    # 1. An explicit ATS type is the strongest signal.
    if ats_type in API_CHANNELS:
        return "api", ats_type
    if ats_type in BROWSER_CHANNELS:
        return "browser", ats_type

    # 2. Otherwise classify the destination URL host.
    url_platform = (
        _platform_from_url(job.get("career_url")) or _platform_from_url(job.get("url"))
    )
    if url_platform in API_CHANNELS:
        return "api", url_platform
    if url_platform in BROWSER_CHANNELS:
        return "browser", url_platform

    # 3. Finally fall back to the scrape source.
    if source in API_CHANNELS:
        return "api", source
    if source in BROWSER_CHANNELS:
        return "browser", source

    return "unknown", source or ats_type or "unknown"


# ---------------------------------------------------------------------------
# Rate limit — atomic JSON file
# ---------------------------------------------------------------------------

_rate_limit_lock = threading.Lock()


def _today_iso() -> str:
    return date.today().isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_rate_limit(path: str) -> Dict[str, Any]:
    """Return the on-disk rate-limit state. Missing/corrupt -> empty state."""
    p = Path(path)
    empty: Dict[str, Any] = {"version": 1, "users": {}}
    if not p.exists():
        return empty
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("rate-limit file unreadable (%s) — treating as empty", exc)
        return empty
    try:
        data = json.loads(raw) if raw.strip() else empty
    except json.JSONDecodeError as exc:
        log.warning("rate-limit file corrupt (%s) — treating as empty", exc)
        return empty
    if not isinstance(data, dict):
        return empty
    data.setdefault("version", 1)
    if not isinstance(data.get("users"), dict):
        data["users"] = {}
    return data


def _write_rate_limit_atomic(path: str, state: Dict[str, Any]) -> None:
    """Atomic JSON write — temp file + fsync + os.replace."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, indent=2, sort_keys=True).encode("utf-8")
    fd, tmp_path = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _user_today_count(
    state: Mapping[str, Any], user_id: str, day: Optional[str] = None
) -> int:
    """Submissions already used by ``user_id`` on ``day`` (default today)."""
    key = day or _today_iso()
    try:
        return int(state.get("users", {}).get(user_id, {}).get(key, {}).get("count", 0))
    except (TypeError, ValueError, AttributeError):
        return 0


def _bump_user_today_count(
    path: str, user_id: str, job_id: Optional[int] = None
) -> Dict[str, Any]:
    """Increment today's count atomically (lock spans read-modify-write)."""
    with _rate_limit_lock:
        state = _read_rate_limit(path)
        users = state.setdefault("users", {})
        if not isinstance(users, dict):
            users = {}
            state["users"] = users
        user_state = users.setdefault(user_id, {})
        if not isinstance(user_state, dict):
            user_state = {}
            users[user_id] = user_state
        today = user_state.setdefault(_today_iso(), {"count": 0})
        today["count"] = int(today.get("count", 0)) + 1
        today["last_apply_at"] = _now_iso()
        if job_id is not None:
            today["last_job_id"] = int(job_id)
        _write_rate_limit_atomic(path, state)
        return today


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_JOB_COLUMNS = (
    "id, title, company, location, url, career_url, source, score, "
    "ats_type, status, cv_path, cover_letter_path, applied_at"
)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection) -> set:
    """Columns present on ``jobs``.

    The self-host schema in ``src/db/jobs_db.py`` is slimmer than the admin
    DB (no ``ats_type``/``score``), so we probe rather than assume.
    """
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    except sqlite3.Error:
        return set()


def _load_job(conn: sqlite3.Connection, job_id: int) -> Optional[Dict[str, Any]]:
    """Load one job, projecting only the columns that actually exist."""
    available = _table_columns(conn)
    if not available:
        return None
    wanted = [c.strip() for c in _JOB_COLUMNS.split(",")]
    selected = ", ".join(c for c in wanted if c in available)
    if "id" not in selected:  # pragma: no cover — schema is always sane
        return None
    row = conn.execute(
        f"SELECT {selected} FROM jobs WHERE id = ?", (int(job_id),)
    ).fetchone()
    return dict(row) if row else None


def _update_status(conn: sqlite3.Connection, job_id: int,
                   extra: Optional[Mapping[str, Any]] = None) -> None:
    """Set ``status`` (+ any extra columns) on one job, schema-aware.

    The self-host schema in ``src/db/jobs_db.py`` carries neither
    ``updated_at`` nor ``applied_channel``, so a hard-coded UPDATE would raise
    ``no such column`` on exactly the deployments that need this code most.
    We build the SET list from the columns that actually exist.
    """
    available = _table_columns(conn)
    extra = dict(extra or {})
    # `status` is always present — every schema in this repo defines it.
    assignments = ["status = ?"]
    params: List[Any] = [extra.pop("__status__")]
    for col, value in extra.items():
        if col in available:
            assignments.append(f"{col} = ?")
            params.append(value)
    if "updated_at" in available:
        assignments.append("updated_at = CURRENT_TIMESTAMP")
    params.append(int(job_id))
    conn.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?", params)
    conn.commit()


def _set_applied(conn: sqlite3.Connection, job_id: int, applied_at: str,
                 channel: str = "") -> None:
    """Mark a job submitted.

    ``status='submitted'`` matches the ADOPT-12 runner, the dashboard and the
    mail matcher, so the orchestrator reuses that terminal value instead of
    inventing ``applied``. ``applied_channel`` is recorded when the column
    exists, so the audit trail says *how* it went out.
    """
    extra = {"__status__": "submitted", "applied_at": applied_at,
             "applied_channel": channel}
    _update_status(conn, job_id, extra)


def _set_needs_human(conn: sqlite3.Connection, job_id: int, note: str = "") -> None:
    """Browser channel filled the form and stopped. The human clicks Submit."""
    _update_status(conn, job_id, {"__status__": "needs_human"})
    if note:
        log.info("job %s needs human: %s", job_id, note)


def _set_failed(conn: sqlite3.Connection, job_id: int, error: str) -> None:
    """Hard failure. ``applied_at`` stays NULL so the next run retries."""
    _update_status(conn, job_id, {"__status__": "failed"})
    log.info("job %s marked failed: %s", job_id, error)


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


def _discord_notify(webhook: str, msg: str) -> bool:
    """POST one message to a Discord webhook. Never raises, never logs the URL."""
    if not webhook:
        return False
    try:
        req = urllib.request.Request(
            webhook,
            data=json.dumps({"content": msg}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        log.warning("discord webhook failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


def _http_post(url: str, payload: Mapping[str, Any],
               headers: Optional[Mapping[str, str]] = None,
               timeout: int = 30) -> Tuple[int, str]:
    """POST a JSON body. Returns ``(status, body)``; status 0 = transport error."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    for k, v in (headers or {}).items():
        hdrs[k] = v
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return exc.code, ""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def _http_multipart(url: str, fields: Mapping[str, str],
                    file_field: str = "", file_path: str = "",
                    timeout: int = 60) -> Tuple[int, str]:
    """POST multipart/form-data with at most one file part (the ATS resume).

    Lever only accepts a resume in multipart mode, so this exists for it.
    Returns ``(status, body)``; status 0 = transport error.
    """
    boundary = "----CaptainApplyBoundary7f3a9c"
    sep = f"--{boundary}\r\n"
    parts: List[bytes] = []
    for name, value in fields.items():
        if value is None or value == "":
            continue
        parts.append(
            f'{sep}Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    if file_field and file_path and Path(file_path).exists():
        filename = Path(file_path).name
        ctype = "application/pdf" if filename.lower().endswith(".pdf") else "text/html"
        try:
            blob = Path(file_path).read_bytes()
        except OSError as exc:
            return 0, f"read {file_path}: {exc}"
        head = (
            f'{sep}Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'
        ).encode("utf-8")
        parts.append(head + blob + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return exc.code, ""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# URL parsing — the ids each vendor's endpoint actually needs
# ---------------------------------------------------------------------------

_GH_JOB_ID_RE = re.compile(r"/jobs/(\d+)")
_ANY_UUID_RE = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                          r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")


def _board_token(job: Mapping[str, Any]) -> str:
    """Board/tenant slug from career_url (``boards.greenhouse.io/stripe`` -> ``stripe``)."""
    career = (job.get("career_url") or job.get("url") or "").strip()
    if not career:
        return ""
    try:
        parts = urllib.parse.urlparse(career).path.strip("/").split("/")
    except ValueError:
        return ""
    return parts[0] if parts and parts[0] else ""


def _posting_id(job: Mapping[str, Any]) -> str:
    """Posting id from either URL — numeric (Greenhouse) or UUID (Lever/Ashby)."""
    for field in ("career_url", "url"):
        raw = (job.get(field) or "").strip()
        if not raw:
            continue
        m = _GH_JOB_ID_RE.search(raw)
        if m:
            return m.group(1)
        m = _ANY_UUID_RE.search(raw)
        if m:
            return m.group(1)
    return ""


def _first_existing_file(*paths: Optional[str]) -> str:
    """Return the first path that exists on disk, else ''."""
    for p in paths:
        if p and Path(p).exists():
            return str(p)
    return ""


def _cover_text(path: Optional[str], limit: int = 3000) -> str:
    """Read a cover letter as plain text (HTML stripped, truncated)."""
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    try:
        raw = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    if p.suffix.lower() in (".html", ".htm"):
        raw = re.sub(r"<[^>]+>", " ", raw)
        raw = re.sub(r"\s+", " ", raw)
    return raw[:limit].strip()


# ---------------------------------------------------------------------------
# API channel — Greenhouse / Lever / Ashby
#
# Every one of these is credential-gated (see the module docstring for the
# live probes). Each returns a pause-for-review ApplyResult when the
# credential is absent, so the caller downgrades to the browser channel
# rather than failing the job.
# ---------------------------------------------------------------------------


def _missing_key_result(platform: str, job_id: Optional[int], env_var: str) -> ApplyResult:
    return apply_pause_for_review(
        f"{env_var} not set — {platform} submission needs an employer-issued "
        f"API key; falling back to the browser channel",
        ats_type=platform,
        job_id=job_id,
    )


def _apply_greenhouse_api(job: Mapping[str, Any], profile: Profile) -> ApplyResult:
    """POST an application to the Greenhouse Job Board API.

    ``POST https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{id}``
    with HTTP Basic auth using the board's Job Board API key. Resumes go up
    as base64 in ``resume_content`` + ``resume_content_filename``.
    """
    import base64

    board = _board_token(job)
    posting = _posting_id(job)
    api_key = os.getenv("GREENHOUSE_JOB_BOARD_API_KEY", "")
    job_id = job.get("id")

    if not board or not posting:
        return ApplyResult(success=False, error="missing greenhouse board token or job id",
                           ats_type="greenhouse", job_id=job_id)
    if not api_key:
        return _missing_key_result("greenhouse", job_id, "GREENHOUSE_JOB_BOARD_API_KEY")

    payload: Dict[str, Any] = {
        "first_name": profile.first_name,
        "last_name": profile.last_name,
        "email": profile.email,
    }
    if profile.phone:
        payload["phone"] = profile.phone

    cv = _first_existing_file(job.get("cv_path"), job.get("resume_path"))
    if cv:
        payload["resume_content"] = base64.b64encode(Path(cv).read_bytes()).decode("ascii")
        payload["resume_content_filename"] = Path(cv).name
    cover = _first_existing_file(job.get("cover_letter_path"))
    if cover:
        payload["cover_letter_content"] = base64.b64encode(Path(cover).read_bytes()).decode("ascii")
        payload["cover_letter_content_filename"] = Path(cover).name

    auth = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("ascii")
    url = (f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(board)}"
           f"/jobs/{posting}")
    status, body = _http_post(url, payload, headers={"Authorization": f"Basic {auth}"})
    if 200 <= status < 300:
        return ApplyResult(success=True, submitted=True, ats_type="greenhouse", job_id=job_id)
    return ApplyResult(success=False, submitted=False,
                       error=f"greenhouse API HTTP {status}: {body[:300]}",
                       ats_type="greenhouse", job_id=job_id)


def _apply_lever_api(job: Mapping[str, Any], profile: Profile) -> ApplyResult:
    """POST an application to the Lever postings API.

    ``POST https://api.lever.co/v0/postings/{site}/{posting_id}?key={APIKEY}``
    Lever only accepts a resume in multipart mode, so we post multipart.
    """
    site = _board_token(job)
    posting = _posting_id(job)
    api_key = os.getenv("LEVER_API_KEY", "")
    job_id = job.get("id")

    if not site or not posting:
        return ApplyResult(success=False, error="missing lever site or posting id",
                           ats_type="lever", job_id=job_id)
    if not api_key:
        return _missing_key_result("lever", job_id, "LEVER_API_KEY")

    fields = {
        "name": profile.display_name(),
        "email": profile.email,
        "phone": profile.phone or "",
        "comments": _cover_text(job.get("cover_letter_path")),
    }
    if profile.linkedin:
        fields["urls[LinkedIn]"] = profile.linkedin
    if profile.portfolio:
        fields["urls[Portfolio]"] = profile.portfolio

    cv = _first_existing_file(job.get("cv_path"), job.get("resume_path"))
    url = (f"https://api.lever.co/v0/postings/{urllib.parse.quote(site)}/"
           f"{urllib.parse.quote(posting)}?key={urllib.parse.quote(api_key, safe='')}")
    status, body = _http_multipart(url, fields, "resume", cv)
    if status in (200, 201):
        return ApplyResult(success=True, submitted=True, ats_type="lever", job_id=job_id)
    return ApplyResult(success=False, submitted=False,
                       error=f"lever API HTTP {status}: {body[:300]}",
                       ats_type="lever", job_id=job_id)


def _apply_ashby_api(job: Mapping[str, Any], profile: Profile) -> ApplyResult:
    """POST an application to the Ashby job-board apply endpoint.

    ``POST https://api.ashbyhq.com/posting-api/job-board/{board}/apply?key=``
    The public posting API is otherwise read-only; the apply route is board-
    scoped and key-gated.
    """
    board = _board_token(job)
    posting = _posting_id(job)
    api_key = os.getenv("ASHBY_API_KEY", "")
    job_id = job.get("id")

    if not board or not posting:
        return ApplyResult(success=False, error="missing ashby board or posting id",
                           ats_type="ashby", job_id=job_id)
    if not api_key:
        return _missing_key_result("ashby", job_id, "ASHBY_API_KEY")

    payload: Dict[str, Any] = {
        "jobId": posting,
        "name": profile.display_name(),
        "email": profile.email,
        "phoneNumber": profile.phone or "",
    }
    if profile.linkedin:
        payload["linkedInUrl"] = profile.linkedin
    if profile.portfolio:
        payload["portfolioUrl"] = profile.portfolio

    url = (f"https://api.ashbyhq.com/posting-api/job-board/"
           f"{urllib.parse.quote(board)}/apply?key={urllib.parse.quote(api_key, safe='')}")
    status, body = _http_post(url, payload)
    if 200 <= status < 300:
        return ApplyResult(success=True, submitted=True, ats_type="ashby", job_id=job_id)
    return ApplyResult(success=False, submitted=False,
                       error=f"ashby API HTTP {status}: {body[:300]}",
                       ats_type="ashby", job_id=job_id)


_API_DISPATCH = {
    "greenhouse": _apply_greenhouse_api,
    "lever": _apply_lever_api,
    "ashby": _apply_ashby_api,
}


def _apply_api_channel(job: Mapping[str, Any], profile: Profile, platform: str) -> ApplyResult:
    """Route to the right API applier. Unknown platform -> pause, never guess."""
    fn = _API_DISPATCH.get(platform)
    if fn is None:
        return apply_pause_for_review(
            f"no API applier for platform {platform!r}",
            ats_type=platform, job_id=job.get("id"),
        )
    return fn(job, profile)


# ---------------------------------------------------------------------------
# Browser channel — Playwright + stored cookies
#
# Every helper here fills what it can and returns paused_for_review=True.
# NONE of them clicks a submit control. That is the whole point.
# ---------------------------------------------------------------------------

_XING_SELECTORS: Tuple[Tuple[str, str], ...] = (
    ('input[name="firstName"], input[data-testid="first-name"]', "first_name"),
    ('input[name="lastName"], input[data-testid="last-name"]', "last_name"),
    ('input[name="email"], input[data-testid="email"]', "email"),
    ('input[name="phone"], input[data-testid="phone"]', "phone"),
)

_LINKEDIN_SELECTORS = (
    ('input#input-artemis-modal-firstName', "first_name"),
    ('input#input-artemis-modal-lastName', "last_name"),
    ('input#input-artemis-modal-email', "email"),
    ('input#input-artemis-modal-phoneNumber', "phone"),
)

_INDEED_SELECTORS = (
    ('input[id*="first"][name*="name" i]', "first_name"),
    ('input[id*="last"][name*="name" i]', "last_name"),
    ('input[type="email"]', "email"),
    ('input[type="tel"]', "phone"),
)

_WORKDAY_SELECTORS = (
    ('input[data-automation-id="legalNameSection_firstName"]', "first_name"),
    ('input[data-automation-id="legalNameSection_lastName"]', "last_name"),
    ('input[data-automation-id="email"]', "email"),
    ('input[data-automation-id="phone-number"]', "phone"),
)

_BROWSER_SELECTORS: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "xing": _XING_SELECTORS,
    "linkedin": _LINKEDIN_SELECTORS,
    "indeed": _INDEED_SELECTORS,
    "workday": _WORKDAY_SELECTORS,
}


def _open_browser(cookies: List[Dict[str, Any]], headless: bool = False) -> Dict[str, Any]:
    """Launch Chromium with the user's cookies injected. Returns a handle."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "playwright is required for browser-channel applies — install with "
            "`pip install playwright` then `playwright install chromium`"
        ) from exc

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context()
        if cookies:
            context.add_cookies(cookies)
        page = context.new_page()
        return {"playwright": pw, "browser": browser, "context": context, "page": page}
    except Exception:  # noqa: BLE001
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass
        raise


def _close_browser(handle: Optional[Mapping[str, Any]]) -> None:
    """Tear down a browser handle. Best-effort, never raises."""
    if not handle:
        return
    for key in ("context", "browser", "playwright"):
        obj = handle.get(key)
        if obj is None:
            continue
        try:
            obj.close() if key != "playwright" else obj.stop()
        except Exception:  # noqa: BLE001
            pass


def _fill_browser_channel(
    job: Mapping[str, Any],
    profile: Profile,
    platform: str,
    cookies: List[Dict[str, Any]],
    headless: bool = False,
) -> ApplyResult:
    """Open the job, fill identifiable fields, screenshot, and PAUSE.

    Never clicks a submit control — the user reviews the filled form and
    clicks Submit themselves.
    """
    job_id = job.get("id")
    career_url = job.get("career_url") or job.get("url") or ""
    if not career_url:
        return apply_pause_for_review("no career_url for job",
                                      ats_type=platform, job_id=job_id)
    if not cookies:
        # Every browser-channel board gates behind a logged-in session. With no
        # cookie jar we cannot even reach the real form, so pause and say how to
        # fix it rather than guessing at fields.
        return apply_pause_for_review(
            f"no stored {platform} session — capture one with `captainauth` first",
            ats_type=platform, job_id=job_id,
        )

    selectors = _BROWSER_SELECTORS.get(platform, ())
    try:
        handle = _open_browser(cookies, headless=headless)
    except RuntimeError as exc:
        return apply_pause_for_review(str(exc), ats_type=platform, job_id=job_id)
    except Exception as exc:  # noqa: BLE001
        return ApplyResult(success=False, error=f"browser launch failed: {exc}",
                           ats_type=platform, job_id=job_id)

    page = handle["page"]
    values = {
        "first_name": profile.first_name,
        "last_name": profile.last_name,
        "email": profile.email,
        "phone": profile.phone,
    }
    try:
        page.goto(career_url, timeout=45_000, wait_until="domcontentloaded")
        filled = 0
        for selector, key in selectors:
            val = values.get(key) or ""
            if not val:
                continue
            try:
                page.fill(selector, val, timeout=4_000)
                filled += 1
            except Exception:  # noqa: BLE001 — field simply isn't there
                pass
        screenshot_path = ""
        try:
            shot_dir = _PROJECT_ROOT / "screenshots"
            shot_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = str(shot_dir / f"{platform}-{job_id or 'unknown'}.png")
            page.screenshot(path=screenshot_path)
        except Exception:  # noqa: BLE001
            screenshot_path = ""
        return apply_pause_for_review(
            f"{platform} form filled ({filled} field(s)) — awaiting human Submit",
            ats_type=platform, job_id=job_id, screenshot_path=screenshot_path,
            fields_filled=[k for _, k in selectors if values.get(k)],
        )
    except Exception as exc:  # noqa: BLE001
        return ApplyResult(success=False, error=f"{platform} browser fill failed: {exc}",
                           ats_type=platform, job_id=job_id)
    finally:
        _close_browser(handle)


def _apply_browser_channel(job: Mapping[str, Any], profile: Profile, platform: str,
                           cookies: List[Dict[str, Any]], headless: bool = False) -> ApplyResult:
    return _fill_browser_channel(job, profile, platform, cookies, headless=headless)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class ApplyOrchestrator:
    """Turn approved dashboard cards into applications.

    Usage::

        orch = ApplyOrchestrator(user_id="lars")
        result = orch.apply_job(79)              # one job
        n = orch.apply_all_pending()             # the approved queue

    Routing: greenhouse/lever/ashby -> API (credential-gated, else browser);
    xing/linkedin/indeed/workday -> browser (always pauses); else refused.
    """

    def __init__(
        self,
        user_id: str = DEFAULT_USER_ID,
        *,
        db_path: str = DEFAULT_DB_PATH,
        rate_limit_path: str = DEFAULT_RATE_LIMIT_PATH,
        profile: Optional[Profile] = None,
        discord_webhook: str = DEFAULT_DISCORD_WEBHOOK,
        daily_cap: int = DEFAULT_DAILY_CAP,
        dry_run: bool = False,
        headless: bool = False,
    ) -> None:
        if not user_id or not isinstance(user_id, str):
            raise ValueError("user_id must be a non-empty string")
        self.user_id = user_id
        self.db_path = db_path
        self.rate_limit_path = rate_limit_path
        self.profile = profile
        self.discord_webhook = discord_webhook
        self.daily_cap = max(1, int(daily_cap))
        self.dry_run = bool(dry_run)
        self.headless = bool(headless)

    # ----- credentials -----------------------------------------------------

    def _credentials(self, platform: str) -> List[Dict[str, Any]]:
        """Stored cookies for ``platform`` as Playwright cookies. [] on any miss.

        Logs only whether a session exists — never a cookie value.
        """
        try:
            cred = get_credential(self.user_id, platform)
            return list(cred.to_playwright_cookies() or [])
        except Exception as exc:  # noqa: BLE001 — no profile / no cred / locked
            log.debug("no %s credential for %s: %s", platform, self.user_id,
                      type(exc).__name__)
            return []

    def _load_profile(self) -> Profile:
        if self.profile is not None:
            return self.profile
        return parse_master_profile(DEFAULT_PROFILE_PATH)

    def _check_daily_cap(self) -> Optional[Dict[str, Any]]:
        """Refusal dict when today's cap is already reached, else None."""
        used = _user_today_count(_read_rate_limit(self.rate_limit_path), self.user_id)
        if used >= self.daily_cap:
            return {
                "ok": False, "channel": "rate_limit", "status": "refused",
                "error": "daily_cap", "applied_today": used, "cap": self.daily_cap,
            }
        return None

    # ----- public API ------------------------------------------------------

    def apply_job(self, job_id: int) -> Dict[str, Any]:
        """Apply to one job.

        Returns ``{ok, channel, platform, status, job_id, applied_at, error}``
        where status is one of applied / paused / failed / refused / skipped.
        """
        if self.dry_run:
            return self._preview_job(job_id)

        cap = self._check_daily_cap()
        if cap is not None:
            return {**cap, "job_id": int(job_id)}

        try:
            conn = _connect(self.db_path)
        except sqlite3.Error as exc:
            return self._fail(dict(id=int(job_id)), "db", f"db open: {exc}")

        try:
            job = _load_job(conn, int(job_id))
            if job is None:
                return self._fail(dict(id=int(job_id)), "db", "job not found")

            # SAFETY GATE: never apply to a card Lars has not approved.
            if (job.get("status") or "").lower() != "approved":
                return {
                    "ok": False, "channel": "status_gate", "status": "refused",
                    "error": "status_gate", "job_id": int(job_id),
                    "applied_at": None, "job_status": job.get("status"),
                }

            # Idempotency: an already-submitted job is never re-sent.
            if job.get("applied_at"):
                return {
                    "ok": False, "channel": "status_gate", "status": "skipped",
                    "error": "already_applied", "job_id": int(job_id),
                    "applied_at": job["applied_at"],
                }

            channel, platform = _classify(job)
            profile = self._load_profile()

            if channel == "api":
                result = _apply_api_channel(job, profile, platform)
                if result.paused_for_review:
                    # No API key -> downgrade to the browser channel rather
                    # than failing the job. Both paths pause for review.
                    log.info("api channel unavailable for %s — using browser", platform)
                    result = _apply_browser_channel(job, profile, platform,
                                                    self._credentials(platform),
                                                    headless=self.headless)
            elif channel == "browser":
                result = _apply_browser_channel(job, profile, platform,
                                                self._credentials(platform),
                                                headless=self.headless)
            else:
                return {
                    "ok": False, "channel": "unknown", "status": "refused",
                    "error": f"unsupported platform: {platform!r}",
                    "job_id": int(job_id), "applied_at": None,
                }

            return self._finalize(conn, job, channel, platform, result)
        finally:
            conn.close()

    def _fail(self, job: Mapping[str, Any], channel: str, error: str) -> Dict[str, Any]:
        self._notify(f"❌ Failed (job {job.get('id')}): {job.get('title')} @ "
                     f"{job.get('company')} — {error}")
        return {
            "ok": False, "channel": channel, "status": "failed",
            "error": error, "job_id": job.get("id"), "applied_at": None,
        }

    def _finalize(self, conn: sqlite3.Connection, job: Mapping[str, Any],
                  channel: str, platform: str, result: ApplyResult) -> Dict[str, Any]:
        """ApplyResult -> DB write + cap bump + Discord + return dict."""
        job_id = int(job["id"])
        applied_at = _now_iso()
        label = f"{job.get('title')} @ {job.get('company')}"

        if result.success and result.submitted:
            _set_applied(conn, job_id, applied_at, channel=channel)
            _bump_user_today_count(self.rate_limit_path, self.user_id, job_id)
            self._notify(f"✅ Applied (job {job_id}): {label} via {channel}/{platform}")
            return {
                "ok": True, "channel": channel, "platform": platform,
                "status": "applied", "job_id": job_id,
                "applied_at": applied_at, "error": None,
            }

        if result.paused_for_review:
            _set_needs_human(conn, job_id, note=result.error or "")
            # A filled form still consumed a real application attempt on that
            # board — count it so one afternoon can't fire 30 XING applies.
            _bump_user_today_count(self.rate_limit_path, self.user_id, job_id)
            self._notify(f"⚠️ Paused (job {job_id}): {label} — "
                         f"{result.error or 'awaiting human submit'}")
            return {
                "ok": False, "channel": channel, "platform": platform,
                "status": "paused", "job_id": job_id,
                "applied_at": None, "error": result.error,
                "screenshot": result.screenshot_path,
            }

        _set_failed(conn, job_id, error=result.error or "unknown failure")
        self._notify(f"❌ Failed (job {job_id}): {label} — {result.error or 'unknown'}")
        return {
            "ok": False, "channel": channel, "platform": platform,
            "status": "failed", "job_id": job_id,
            "applied_at": None, "error": result.error,
        }

    def _preview_job(self, job_id: int) -> Dict[str, Any]:
        """Dry-run: classify + report, no side effects at all."""
        try:
            conn = _connect(self.db_path)
        except sqlite3.Error as exc:
            return self._fail(dict(id=int(job_id)), "db", f"db open: {exc}")
        try:
            job = _load_job(conn, int(job_id))
            if job is None:
                return self._fail(dict(id=int(job_id)), "db", "job not found")
            channel, platform = _classify(job)
            used = _user_today_count(_read_rate_limit(self.rate_limit_path), self.user_id)
            return {
                "ok": True, "channel": f"preview:{channel}", "platform": platform,
                "status": "dry_run", "job_id": int(job_id), "applied_at": None,
                "error": None, "applied_today": used, "cap": self.daily_cap,
                "job_status": job.get("status"),
                "applied_at_present": bool(job.get("applied_at")),
            }
        finally:
            conn.close()

    def apply_all_pending(self, limit_per_day: Optional[int] = None) -> int:
        """Apply to every ``status='approved' AND applied_at IS NULL`` job.

        Returns the number of jobs actually attempted. Jobs refused by the cap
        or the status gate are not counted.
        """
        cap = int(limit_per_day) if limit_per_day is not None else self.daily_cap
        attempted = 0
        for job_id in self._pending_ids():
            if not self.dry_run:
                if _user_today_count(_read_rate_limit(self.rate_limit_path),
                                     self.user_id) >= cap:
                    log.info("daily cap reached (%d) — stopping", cap)
                    break
            result = self.apply_job(job_id)
            if result.get("status") != "refused":
                attempted += 1
        return attempted

    def _pending_ids(self) -> List[int]:
        """Approved, not-yet-applied job ids, best score first.

        ``score`` is absent from the slim self-host schema, so the ORDER BY
        degrades rather than throwing.
        """
        conn = _connect(self.db_path)
        try:
            available = _table_columns(conn)
            order = "score DESC, id ASC" if "score" in available else "id ASC"
            rows = conn.execute(
                "SELECT id FROM jobs WHERE status = 'approved' AND applied_at IS NULL "
                f"ORDER BY {order}"
            ).fetchall()
            return [int(r["id"]) for r in rows]
        except sqlite3.Error as exc:
            log.warning("could not list pending jobs: %s", exc)
            return []
        finally:
            conn.close()

    def pending_count(self) -> int:
        """How many approved, not-yet-applied jobs are queued."""
        return len(self._pending_ids())

    def _notify(self, msg: str) -> None:
        """Discord notification. Never raises, never logs the webhook URL."""
        if not self.discord_webhook:
            return
        try:
            _discord_notify(self.discord_webhook, msg)
        except Exception as exc:  # noqa: BLE001
            log.warning("discord notify failed: %s", exc)


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


def build_orchestrator(user_id: str = DEFAULT_USER_ID, *, dry_run: bool = False,
                       **kwargs: Any) -> ApplyOrchestrator:
    """Factory used by the CLI. Defaults come from env unless overridden."""
    return ApplyOrchestrator(user_id=user_id, dry_run=dry_run, **kwargs)


__all__ = [
    "API_CHANNELS",
    "ApplyOrchestrator",
    "BROWSER_CHANNELS",
    "DEFAULT_DAILY_CAP",
    "DEFAULT_USER_ID",
    "build_orchestrator",
]