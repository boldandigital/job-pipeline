"""MAIL-1 — match an incoming email back to a job row.

Three signals, in priority order:

    1. Sender domain
       careers@<company>, <company>.com, or known ATS domains
       (workday.com, greenhouse.io, lever.co, ashbyhq.com,
       smartrecruiters.com). If we recognize the domain → lookup company in
       the jobs table.

    2. Subject substring
       company name + job title substring (e.g. "Re: Senior CTO at Bold
       and Digital"). Looks for either " at <company>" or "<company> —
       <title>" — the two common patterns recruiters use.

    3. Body regex
       Position:\\s*(.+) / Role:\\s*(.+) — niche but appears in
       Greenhouse / Workday confirmation templates.

If 0 signals match → return ``None`` (caller logs + skips). Otherwise pick
the most recently applied job for that company (newest ``applied_at``,
ties broken by ``id DESC``). The picker prefers rows that have an
``applied_at`` set — replies for jobs we never submitted are noise.

Pure function: takes a DB connection, an email dict (from / subject /
body), and returns either a :class:`Match` or ``None``. Easy to test.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Any, Dict, List, Mapping, Optional

# ATS domains that hide the actual employer — match these to the company
# we previously discovered in ADOPT-14 (the career-page discovery pass
# stamps ``ats_type`` so we know which ATS serves which company).
_KNOWN_ATS_DOMAINS: dict[str, str] = {
    "myworkdayjobs.com": "workday",
    "workday.com": "workday",
    "greenhouse.io": "greenhouse",
    "greenhouse-mail.io": "greenhouse",
    "boards.greenhouse.io": "greenhouse",
    "lever.co": "lever",
    "ashbyhq.com": "ashby",
    "smartrecruiters.com": "smartrecruiters",
    "smartrecruiters-mail.com": "smartrecruiters",
}

# Subject substring patterns. The (a) and (b) forms below correspond to
# the two ways recruiters compose subjects:
#   (a) "Re: Senior CTO at Bold and Digital"
#   (b) "Bold and Digital — Senior CTO / Ihre Bewerbung"
_SUBJECT_AT_COMPANY = re.compile(
    r"\b(?:at|bei|für|fur|@)\s+(?P<company>[A-Z][\w&.,\- ]{1,60})",
    re.IGNORECASE,
)
_SUBJECT_COMPANY_DASH = re.compile(
    r"\b(?P<company>[A-Z][\w&.,\- ]{1,60})\s+[—–\-]\s+(?P<title>[\w &/.,()\-]{2,80})",
    re.IGNORECASE,
)

# Body regex — rare but worth trying. Greenhouse confirmation template
# includes "Position: Senior CTO" for example.
_BODY_POSITION = re.compile(r"Position:\s*(?P<title>.+)", re.IGNORECASE)
_BODY_ROLE = re.compile(r"Role:\s*(?P<title>.+)", re.IGNORECASE)


@dataclass(frozen=True)
class Match:
    """Result of a successful match.

    Attributes:
        job_id:         the SQLite ``jobs.id`` we matched against.
        company:        the company name we matched (echoed back so the
                        Discord notification can show "Bold and Digital").
        title:          the job title we matched.
        signal:         which of the three signals fired — for logging:
                          'sender_domain' | 'subject_at' | 'subject_dash'
                          | 'body_position' | 'body_role'.
        match_detail:   human-readable detail of the match (the company
                        substring, the sender domain, etc.) — for Discord.
    """

    job_id: int
    company: str
    title: str
    signal: str
    match_detail: str


def _parse_from(from_header: str) -> tuple[str, str]:
    """``"Lars <careers@bold.digital>"`` → ``("Lars", "careers@bold.digital")``.
    Defensive: handles empty / weird values without raising."""
    if not from_header:
        return ("", "")
    try:
        return parseaddr(from_header)
    except Exception:
        return ("", from_header)


def _domain_of(addr: str) -> str:
    """``"careers@bold.digital"`` → ``"bold.digital"``. Empty on garbage."""
    if "@" not in addr:
        return ""
    return addr.split("@", 1)[1].strip().lower().rstrip(">")


def _all_jobs(conn: sqlite3.Connection) -> List[Mapping[str, Any]]:
    """Read every (id, title, company, ats_type, applied_at) row once.

    Cheap on this size of corpus (~hundreds of rows) — avoids hammering
    SQLite with one SELECT per email. Filters in Python so we can do
    case-insensitive substring matching + domain extraction without
    pulling each row separately.
    """
    return [dict(r) for r in conn.execute(
        """
        SELECT id, title, company, ats_type, applied_at
          FROM jobs
        """
    ).fetchall()]


def _company_matches_domain(company: str, domain: str) -> bool:
    """``"bold.digital"`` matches ``"Bold and Digital"`` by token overlap.

    Heuristic (cheap, fast):
      1. Strip the TLD from the domain → stem (e.g. ``boldandigital``).
      2. Split the company into words → tokens (``bold``, ``digital``).
      3. If the stem contains *every* token, OR if the stem equals the
         company-with-spaces-stripped, it's a match.

    Returns False if either side is empty. The "every token" check makes
    "Bold and Digital" → True on ``boldandigital.com`` (both ``bold``
    and ``digital`` are substrings of ``boldandigital``), while
    "Stripe" → False on ``shopify.com`` (Stripe ≠ shopify).
    """
    if not domain or not company:
        return False

    # 1. stem — strip the last dot-separated piece (TLD).
    stem = domain.lower().rsplit(".", 1)[0]  # boldandigital
    # 2. tokens — split on non-alphanumerics.
    tokens = [t for t in re.split(r"[^a-z0-9]+", company.lower()) if t]
    if not tokens:
        return False
    # 3. every token must appear as a substring of the stem.
    if all(t in stem for t in tokens):
        return True
    # 4. fallback: exact match against company-with-spaces-stripped.
    return re.sub(r"[^a-z0-9]+", "", company.lower()) == stem


def _pick_best_job(
    candidates: List[Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    """Most recently applied job wins; tie-break by id DESC.

    Returns ``None`` if no candidate has applied_at set — replies for jobs
    we never submitted are dropped to keep the DB clean.
    """
    eligible = [c for c in candidates if c.get("applied_at")]
    if not eligible:
        return None
    eligible.sort(
        key=lambda c: (c["applied_at"] or "", c["id"] or 0),
        reverse=True,
    )
    return eligible[0]


def _match_by_sender_domain(
    from_header: str, jobs: List[Mapping[str, Any]],
) -> Optional[Match]:
    """Signal 1 — sender domain → known ATS or company domain.

    Order:
      (a) careers@<company>  → match against company name
      (b) known ATS domain   → match against ``ats_type`` on jobs
      (c) <company>.com      → match against company name (subdomain-tolerant)
    """
    _, addr = _parse_from(from_header)
    domain = _domain_of(addr)
    if not domain:
        return None

    # (a) ATS provider domain — many ATS send from a sub-domain of their
    # own brand. Match jobs by ats_type, regardless of company.
    for ats_domain, ats_key in _KNOWN_ATS_DOMAINS.items():
        if domain.endswith(ats_domain):
            by_ats = [j for j in jobs if (j.get("ats_type") or "").lower() == ats_key]
            best = _pick_best_job(by_ats)
            if best:
                return Match(
                    job_id=best["id"],
                    company=best["company"],
                    title=best["title"],
                    signal="sender_domain",
                    match_detail=f"ATS {ats_key} ({domain})",
                )

    # (b) careers@<company> or anything matching the company name.
    by_company = [
        j for j in jobs if _company_matches_domain(j["company"], domain)
    ]
    best = _pick_best_job(by_company)
    if best:
        return Match(
            job_id=best["id"],
            company=best["company"],
            title=best["title"],
            signal="sender_domain",
            match_detail=f"domain {domain}",
        )
    return None


def _match_by_subject(
    subject: str, jobs: List[Mapping[str, Any]],
) -> Optional[Match]:
    """Signal 2 — company + title substring in subject.

    Looks for "Title at Company" (EN) and "Company — Title" (DE / DACH).
    """
    if not subject:
        return None

    # Pass 1: "<title> at <company>" — primary EN form.
    m = _SUBJECT_AT_COMPANY.search(subject)
    if m:
        company_guess = m.group("company").strip().rstrip(".,;:")
        title_guess = subject[: m.start()].strip()
        for j in jobs:
            if (
                company_guess.lower() in j["company"].lower()
                or j["company"].lower() in company_guess.lower()
            ) and (
                title_guess.lower() in j["title"].lower()
                or j["title"].lower() in title_guess.lower()
            ):
                return Match(
                    job_id=j["id"],
                    company=j["company"],
                    title=j["title"],
                    signal="subject_at",
                    match_detail=f'"{company_guess} at {title_guess}"',
                )

    # Pass 2: "<company> — <title>"
    m = _SUBJECT_COMPANY_DASH.search(subject)
    if m:
        company_guess = m.group("company").strip().rstrip(".,;:")
        title_guess = m.group("title").strip().rstrip(".,;:")
        for j in jobs:
            if (
                company_guess.lower() in j["company"].lower()
                or j["company"].lower() in company_guess.lower()
            ) and (
                title_guess.lower() in j["title"].lower()
                or j["title"].lower() in title_guess.lower()
            ):
                return Match(
                    job_id=j["id"],
                    company=j["company"],
                    title=j["title"],
                    signal="subject_dash",
                    match_detail=f'"{company_guess} — {title_guess}"',
                )
    return None


def _match_by_body(
    body: str, jobs: List[Mapping[str, Any]],
) -> Optional[Match]:
    """Signal 3 — body regex ``Position: …`` or ``Role: …``."""
    if not body:
        return None
    for regex in (_BODY_POSITION, _BODY_ROLE):
        m = regex.search(body)
        if not m:
            continue
        title_guess = m.group("title").strip().rstrip(".,;:")
        for j in jobs:
            if (
                title_guess.lower() in j["title"].lower()
                or j["title"].lower() in title_guess.lower()
            ):
                return Match(
                    job_id=j["id"],
                    company=j["company"],
                    title=j["title"],
                    signal="body_position" if regex is _BODY_POSITION else "body_role",
                    match_detail=f'"{title_guess}"',
                )
    return None


def match_email(
    conn: sqlite3.Connection,
    from_header: str,
    subject: str,
    body: str = "",
) -> Optional[Match]:
    """Try the three signals in order; return the first hit, else None."""
    jobs = _all_jobs(conn)
    if not jobs:
        return None
    return (
        _match_by_sender_domain(from_header, jobs)
        or _match_by_subject(subject, jobs)
        or _match_by_body(body, jobs)
    )