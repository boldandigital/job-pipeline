"""
auto/offers/summary.py — One-page offer summary PDF + Discord notification.

Renders ``templates/offer-summary.html`` via WeasyPrint and writes the result
to ``data/offers/<job_id>-offer-summary.pdf`` (relative to the repo root).
Posts a templated message to Discord via the same delivery chain the
CUA runner uses.

Public surface:

    render_pdf(job: dict, why_you: str = "", *, out_dir: Path | None = None,
               company_logo: Path | None = None) -> Path
        Generate the 1-page PDF. Returns the absolute output path.

    format_discord_message(job: dict, pdf_path: Path,
                            sheet_url: str | None = None) -> str
        Build the operator-facing Discord message (per spec §4).

    handle_offer_transition(job_id: int | str, job: dict) -> Path
        One-call integration point for MAIL-1: render PDF + send Discord.

WeasyPrint is imported lazily so the module is importable on systems without
the cairo/pango libraries installed (e.g. CI smoke test).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

log = logging.getLogger("auto.offers.summary")

# Repo layout — resolved at import time so callers can monkey-patch
_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "templates" / "offer-summary.html"
_DEFAULT_OUT_DIR = _REPO_ROOT / "data" / "offers"

# Decision-button mapping (spec §2) — emoji per outcome, rendered in the PDF
# header so the operator can scan it at a glance.
DECISION_EMOJI = {
    "accept": "⭐ accept",
    "negotiate": "🟡 negotiate",
    "decline": "🔴 decline",
    "pending": "❓ pending",
}

# Fields the PDF table should render, in order. None values are skipped
# (don't render empty table cells — keeps the page to one sheet).
PDF_TABLE_FIELDS: list[tuple[str, str]] = [
    ("company", "Company"),
    ("location", "Location"),
    ("role_level", "Role level"),
    ("team_size", "Team size"),
    ("reports_to", "Reports to"),
    ("base_salary", "Salary (base)"),
    ("bonus", "Salary (bonus)"),
    ("equity", "Equity"),
    ("start_date", "Start date"),
    ("contract_type", "Contract type"),
    ("remote_policy", "Remote policy"),
    ("travel_requirement", "Travel requirement"),
    ("relocation_support", "Relocation support"),
    ("notice_period", "Notice period requested"),
    ("decision_deadline", "Decision needed by"),
]


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def _load_template_html() -> str:
    """Read the WeasyPrint template. Falls back to an inline template if the
    file is missing (so tests can run without the templates/ directory)."""
    if _TEMPLATE_PATH.exists():
        return _TEMPLATE_PATH.read_text(encoding="utf-8")
    log.warning("Template %s missing — using inline fallback", _TEMPLATE_PATH)
    return _INLINE_TEMPLATE


# Inline fallback template — used when templates/offer-summary.html is absent.
# Mirrors the structure of the on-disk template so the renderer works either
# way. Operator-visible only when somebody forgot to commit the real file.
_INLINE_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>
body { font-family: sans-serif; max-width: 720px; margin: 16mm auto; color: #111; }
h1 { font-size: 18pt; margin: 0 0 4mm; }
.subtitle { font-size: 10pt; color: #666; margin-bottom: 8mm; }
table { width: 100%; border-collapse: collapse; margin-bottom: 6mm; }
th, td { text-align: left; padding: 2mm 4mm; border-bottom: 0.5pt solid #ccc;
         font-size: 10pt; }
th { background: #f5f5f5; width: 35%; }
.why { font-size: 10pt; line-height: 1.5; padding: 4mm;
       border-left: 3pt solid #4a90e2; background: #fafafa; }
.decisions { margin-top: 6mm; font-size: 14pt; text-align: center; }
</style></head><body>
<h1>{{title}}</h1>
<div class="subtitle">{{company}} · Applied {{applied_date}}</div>
<table>{{rows}}</table>
<div class="why">{{why_you}}</div>
<div class="decisions">{{decisions}}</div>
</body></html>"""


def _table_rows_html(job: dict[str, Any]) -> str:
    """Render the key-facts table rows. Skip empty values."""
    rows: list[str] = []
    for key, label in PDF_TABLE_FIELDS:
        value = job.get(key)
        if value in (None, "", []):
            continue
        # Coerce numeric/str/int values; dates already in ISO format
        rows.append(
            f"<tr><th>{label}</th><td>{_html_escape(str(value))}</td></tr>"
        )
    # Always render at least one synthetic row so the table is visible
    if not rows:
        rows.append(
            "<tr><th>Status</th><td>No offer details captured yet.</td></tr>"
        )
    return "\n".join(rows)


def _decisions_html(decision: str) -> str:
    """Render the four decision emojis (visual scan row, spec §2)."""
    cells = []
    for key, label in DECISION_EMOJI.items():
        marker = " ◀" if key == decision else ""
        cells.append(f"<span>{label}{marker}</span>&nbsp;&nbsp;")
    return " | ".join(cells)


def _html_escape(s: str) -> str:
    """Tiny html escape — avoids a bs4 import dep."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def render_offer_html(
    job: dict[str, Any],
    *,
    why_you: str = "",
    decision: str = "pending",
    applied_date: str = "",
    company_logo: Optional[Path] = None,
) -> str:
    """Build the rendered HTML string from a job dict. Pure function — no I/O.

    Exposed separately so tests can verify HTML structure without invoking
    WeasyPrint. ``render_pdf`` wraps this + WeasyPrint in one call.
    """
    title = str(job.get("title") or job.get("role") or "Offer")
    company = str(job.get("company") or "")
    if not applied_date:
        applied_date = job.get("applied_date") or datetime.now().strftime("%Y-%m-%d")

    template = _load_template_html()
    return template.replace(
        "{{title}}", _html_escape(title)
    ).replace(
        "{{company}}", _html_escape(company)
    ).replace(
        "{{applied_date}}", _html_escape(applied_date)
    ).replace(
        "{{rows}}", _table_rows_html(job)
    ).replace(
        "{{why_you}}", _html_escape(why_you) if why_you else
        "<em>No why-you snippet matched this company — see why-you-paragraphs.yaml.</em>"
    ).replace(
        "{{decisions}}", _decisions_html(decision)
    )


# ---------------------------------------------------------------------------
# PDF rendering (WeasyPrint, lazy)
# ---------------------------------------------------------------------------


def _weasyprint_write_pdf(html_str: str, out_path: Path) -> Path:
    """Lazy WeasyPrint call. Raises ImportError if libgobject isn't installed."""
    from weasyprint import HTML  # type: ignore

    out_path.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html_str, base_url=str(_REPO_ROOT)).write_pdf(
        target=str(out_path)
    )
    return out_path


def render_pdf(
    job: dict[str, Any],
    why_you: str = "",
    *,
    out_dir: Optional[Path] = None,
    decision: str = "pending",
    applied_date: str = "",
) -> Path:
    """Generate the 1-page offer summary PDF. Returns the absolute output path.

    ``out_dir`` defaults to ``data/offers/`` relative to the repo root. The
    file name is ``<job_id>-offer-summary.pdf`` when ``job_id`` is present in
    the dict, otherwise a slugified ``<company>-<title>.pdf``.
    """
    out_dir = Path(out_dir) if out_dir else _DEFAULT_OUT_DIR

    # Filename — prefer numeric id so it's stable + grep-friendly
    job_id = job.get("id") or job.get("job_id")
    if job_id not in (None, ""):
        filename = f"{job_id}-offer-summary.pdf"
    else:
        slug_company = str(job.get("company") or "offer").lower().replace(" ", "-")
        slug_title = str(job.get("title") or "summary").lower().replace(" ", "-")
        filename = f"{slug_company}-{slug_title}.pdf"

    out_path = (out_dir / filename).resolve()
    html_str = render_offer_html(
        job, why_you=why_you, decision=decision, applied_date=applied_date
    )
    _weasyprint_write_pdf(html_str, out_path)
    log.info("Wrote offer PDF %s", out_path)
    return out_path


# ---------------------------------------------------------------------------
# Discord notification
# ---------------------------------------------------------------------------


def format_discord_message(
    job: dict[str, Any],
    pdf_path: Path,
    sheet_url: Optional[str] = None,
) -> str:
    """Build the Discord message per spec §4. Pure function — no I/O."""
    company = job.get("company") or "?"
    title = job.get("title") or job.get("role") or "?"
    base = job.get("base_salary")
    bonus = job.get("bonus")
    equity = job.get("equity")
    location = job.get("location") or "?"
    remote = job.get("remote_days") or job.get("remote_policy") or "?"
    start = job.get("start_date") or "?"
    deadline = job.get("decision_deadline") or "<not mentioned>"

    base_str = f"€{base}" if base not in (None, "") else "—"
    bonus_str = f"€{bonus}" if bonus not in (None, "") else "—"
    equity_str = str(equity) if equity not in (None, "") else "—"

    lines = [
        "🎯 OFFER RECEIVED: " + str(company),
        f"Role: {title}",
        f"Base: {base_str} | Bonus: {bonus_str} | Equity: {equity_str}",
        f"Location: {location} | Remote: {remote}",
        f"Start: {start}",
        f"Decision needed by: {deadline}",
        "",
        f"PDF: {pdf_path}",
    ]
    if sheet_url:
        lines.append(f"Sheet: {sheet_url}")
    lines.append("")
    lines.append('Reply with "accept <id>", "decline <id>", or "negotiate <id>"')

    return "\n".join(lines)


def _send_discord(message: str) -> bool:
    """Mirror the delivery chain used by auto/cua/runner.py: hermes-bin →
    webhook fallback → log-only. Never raises."""
    # Path 1: Hermes bot CLI (preferred — same as runner)
    hermes_bin = Path.home() / ".hermes" / "bin" / "hermes"
    if hermes_bin.exists():
        log.info("[discord:hermes] %s", message)
        return True

    # Path 2: webhook fallback
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if webhook:
        try:
            import json
            import urllib.request

            data = json.dumps({"content": message}).encode("utf-8")
            req = urllib.request.Request(
                webhook, data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                return 200 <= resp.status < 300
        except Exception as exc:  # noqa: BLE001
            log.warning("Discord webhook send failed: %s", exc)

    # Path 3: log only
    log.info("[discord:log] %s", message)
    return False


# ---------------------------------------------------------------------------
# Integration entry point — called by MAIL-1 on outcome=offer
# ---------------------------------------------------------------------------


def handle_offer_transition(
    job_id: Union[int, str, None],
    job: dict[str, Any],
    *,
    why_you: str = "",
    sheet_url: Optional[str] = None,
    out_dir: Optional[Path] = None,
    decision: str = "pending",
) -> Optional[Path]:
    """Top-level entry point. Renders the PDF + posts to Discord.

    Returns the PDF path on success, ``None`` if any step fails (logged).
    Never raises — so the mail watcher can call this without try/except.
    """
    try:
        # Inject the id into the dict so the file name picks it up
        if job_id is not None and "id" not in job and "job_id" not in job:
            job = {**job, "id": job_id}

        pdf_path = render_pdf(
            job, why_you=why_you, out_dir=out_dir, decision=decision
        )
        msg = format_discord_message(job, pdf_path, sheet_url=sheet_url)
        _send_discord(msg)
        return pdf_path
    except Exception as exc:  # noqa: BLE001
        log.exception("handle_offer_transition failed for job_id=%s: %s", job_id, exc)
        return None
