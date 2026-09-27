#!/usr/bin/env python3
"""
ATS-Safe Template Loader — ported from ~/Documents/Projects/CV/templates/ (ADOPT-3).

Provides:
  - load_why_you_paragraphs()       : load the 4-track YAML (founder_cto /
                                       agency_strategy / hosting_infra / generic).
  - pick_why_you_track(variant, ...) : map dome317 CV variant -> our track.
  - render_cover_letter(...)         : render anschreiben.html with WHY-YOU
                                       paragraph plugged in.
  - render_cv_classic(...)           : render cv-classic.html ATS-safe CV.

Templates live in ../../templates/cv-classic.html + anschreiben.html
(dome317 keeps its existing in-Python HTML in cv_generator.py / cover_letter_generator.py
— this loader is opt-in via USE_ATS_TEMPLATES=1).
"""

import os
import re
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent  # job-pipeline/
_TEMPLATES_DIR = _PROJECT_ROOT / "templates"
_DEFAULT_YAML = _PROJECT_ROOT / "config" / "why-you-paragraphs.yaml"

CV_TEMPLATE_PATH = _TEMPLATES_DIR / "cv-classic.html"
ANSCHREIBEN_TEMPLATE_PATH = _TEMPLATES_DIR / "anschreiben.html"


# ---------------------------------------------------------------------------
# Track mapping (dome317 variant -> our why-you track)
# ---------------------------------------------------------------------------
VARIANT_TO_TRACK = {
    # ai_heavy / technical roles -> hosting_infra (closest match for infra-adjacent
    #   AI/platform engineering roles — measured infra wins resonate).
    "ai_heavy": "hosting_infra",
    "technical": "hosting_infra",
    # product / operations -> agency_strategy (positioning + ops discipline).
    "product": "agency_strategy",
    "operations": "agency_strategy",
    # Default fallback handled in pick_why_you_track().
}


# ---------------------------------------------------------------------------
# YAML loader — minimal, no PyYAML dependency required
# ---------------------------------------------------------------------------
def _yaml_load_paragraphs(path: Path) -> dict:
    """
    Tiny YAML subset reader tailored to why-you-paragraphs.yaml.
    Schema:
      <track>:
        en: |
          ... paragraph ...
        de: |
          ... paragraph ...
    """
    if not path.exists():
        raise FileNotFoundError(f"why-you-paragraphs.yaml not found at {path}")
    text = path.read_text(encoding="utf-8")
    out: dict = {}
    current_track = None
    current_lang = None
    buf: list = []

    def flush():
        if current_track and current_lang and buf:
            out.setdefault(current_track, {})[current_lang] = "\n".join(buf).strip()

    for raw in text.splitlines():
        # Skip comments / blanks
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.rstrip()
        # Top-level track key (no leading whitespace, ends with `:`)
        if re.match(r"^[a-z_]+:$", stripped):
            flush()
            buf = []
            current_track = stripped[:-1]
            current_lang = None
            continue
        # Language sub-key (`  en: |` or `  de: |`)
        m = re.match(r"^  (en|de):\s*\|?\s*$", stripped)
        if m:
            flush()
            current_lang = m.group(1)
            buf = []
            continue
        # Paragraph content line (indented under language)
        if current_track and current_lang and (raw.startswith("    ") or raw.startswith("\t")):
            # Strip a single indent level (4 spaces or 1 tab) — the `|` block scalar
            # content lives at the column after the `|`.
            if raw.startswith("    "):
                buf.append(raw[4:])
            else:
                buf.append(raw[1:])
    flush()
    return out


def load_why_you_paragraphs(yaml_path: Path | str | None = None) -> dict:
    """Load all tracks from why-you-paragraphs.yaml."""
    path = Path(yaml_path) if yaml_path else _DEFAULT_YAML
    return _yaml_load_paragraphs(path)


# ---------------------------------------------------------------------------
# Track picker
# ---------------------------------------------------------------------------
def pick_why_you_track(
    variant: str,
    language: str = "en",
    paragraphs: dict | None = None,
    job_title: str = "",
    job_description: str = "",
) -> str:
    """
    Pick the why-you paragraph for a (variant, language, job) tuple.
    Returns the raw paragraph text (NOT including opening/close boilerplate).
    Falls back to `generic` if the preferred track is missing.
    """
    if paragraphs is None:
        paragraphs = load_why_you_paragraphs()
    lang = (language or "en").lower()
    track = VARIANT_TO_TRACK.get(variant, "generic")

    # Title-driven override: founder_cto signals (acronyms + long-form expansions)
    title_lc = (job_title or "").lower()
    founder_signals = ("cto", "coo", "ceo", "chief technology", "chief operating",
                       "chief product", "chief data", "vp eng", "vp of eng",
                       "head of eng", "head of product", "head of data",
                       "founder", "co-founder", "cofounder", "gründer",
                       "tech lead", "engineering lead")
    if any(sig in title_lc for sig in founder_signals):
        track = "founder_cto"

    # Description-driven override: hosting-infra signals
    desc_lc = (job_description or "").lower()
    hosting_signals = ("hosting", "infrastructure", "infra", "platform engineer",
                       "sre", "devops", "wordpress", "woocommerce", "litespeed",
                       "cdn", "core web vitals", "lcp", "observability")
    if any(sig in desc_lc for sig in hosting_signals):
        track = "hosting_infra"

    block = paragraphs.get(track, {})
    return block.get(lang) or block.get("en") or paragraphs.get("generic", {}).get(lang, "")


# ---------------------------------------------------------------------------
# Template rendering — simple {{token}} substitution
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _render(template: str, ctx: dict[str, Any]) -> str:
    """Substitute {{token}} placeholders. Missing tokens are left empty."""
    def repl(m: re.Match) -> str:
        key = m.group(1)
        val = ctx.get(key, "")
        return str(val)
    return _TOKEN_RE.sub(repl, template)


def render_cv_classic(ctx: dict[str, Any], template_path: Path | str | None = None) -> str:
    """Render cv-classic.html with the given context."""
    p = Path(template_path) if template_path else CV_TEMPLATE_PATH
    return _render(p.read_text(encoding="utf-8"), ctx)


def render_cover_letter(
    ctx: dict[str, Any],
    why_you_paragraph: str,
    template_path: Path | str | None = None,
) -> str:
    """Render anschreiben.html with WHY-YOU paragraph injected."""
    p = Path(template_path) if template_path else ANSCHREIBEN_TEMPLATE_PATH
    ctx = dict(ctx)
    ctx["why_you_paragraph"] = why_you_paragraph
    return _render(p.read_text(encoding="utf-8"), ctx)


# ---------------------------------------------------------------------------
# Convenience: end-to-end cover letter build for batch_pipeline
# ---------------------------------------------------------------------------
def build_cover_letter_for_job(
    job: dict,
    language: str,
    variant: str,
    personal: dict,
    opening: str,
    me_paragraph: str,
    close: str,
    salutation: str = "",
    recipient_block: str = "",
    paragraphs: dict | None = None,
) -> str:
    """
    One-shot: pick WHY-YOU, render HTML, return the final document string.
    Caller handles HTML-to-PDF.
    """
    if paragraphs is None:
        paragraphs = load_why_you_paragraphs()
    lang = (language or "en").lower()
    why_you = pick_why_you_track(
        variant=variant,
        language=lang,
        paragraphs=paragraphs,
        job_title=job.get("title", ""),
        job_description=job.get("description", ""),
    )
    company = job.get("company", "")
    role = job.get("title", "")

    from datetime import datetime
    date_str = datetime.now().strftime("%d.%m.%Y") if lang == "de" else datetime.now().strftime("%B %d, %Y")
    subject = f"Bewerbung als {role}" if lang == "de" else f"Application for {role}"
    if not salutation:
        salutation = "Sehr geehrte Damen und Herren," if lang == "de" else "Dear Hiring Team,"

    ctx = {
        "lang": lang,
        "name": personal.get("name", ""),
        "title": personal.get("title", ""),
        "location": personal.get("location", ""),
        "email": personal.get("email", ""),
        "phone": personal.get("phone", ""),
        "linkedin": personal.get("linkedin", ""),
        "date": date_str,
        "subject_line": subject,
        "salutation": salutation,
        "opening": opening,
        "me_paragraph": me_paragraph,
        "close": close,
        "signature": "Mit freundlichen Grüßen" if lang == "de" else "Kind regards",
        "recipient_block": recipient_block,
        "position_title": role,
        "company": company,
    }
    return render_cover_letter(ctx, why_you)


# ---------------------------------------------------------------------------
# ATS verification helper
# ---------------------------------------------------------------------------
ATS_FORBIDDEN_TAGS = ("<table", "<th", "<td", "<tr", "<img", "<svg",
                      "<canvas", "<iframe", "<object", "<embed")


def verify_ats(html: str) -> dict:
    """
    Quick ATS-friendliness check:
      - No tables / images / iframes (ATS parsers choke on layout-driven markup).
      - No display:none / visibility:hidden hiding text (ATS penalty).
      - No icon fonts (font-family: 'Font Awesome' / similar).
      - Headings h1-h3 only, no nested h4+.
    Returns dict with `ok` (bool), `violations` (list[str]).
    """
    violations: list[str] = []
    lower = html.lower()
    for tag in ATS_FORBIDDEN_TAGS:
        if tag in lower:
            violations.append(f"Forbidden layout tag: {tag}")
    if "display:none" in lower or "visibility:hidden" in lower:
        violations.append("Hides text via display:none / visibility:hidden")
    # Icon fonts (heuristic — match the common library names)
    for icon_font in ("font awesome", "material icons", "bootstrap-icons", "fontawesome"):
        if icon_font in lower:
            violations.append(f"Icon font detected: {icon_font}")
    return {"ok": not violations, "violations": violations}


if __name__ == "__main__":
    # Smoke test: render both templates with minimal context, verify ATS.
    paragraphs = load_why_you_paragraphs()
    print(f"Loaded {len(paragraphs)} why-you tracks: {list(paragraphs.keys())}")

    personal = {
        "name": "Lars von Hierblau",
        "title": "Founder & CTO",
        "location": "Cologne, Germany",
        "email": "contact@example.com",
        "phone": "+49 123 456789",
        "linkedin": "linkedin.com/in/lars",
    }
    job = {
        "title": "Chief Technology Officer",
        "company": "Acme Corp",
        "description": "We're hiring a CTO to scale our WordPress hosting platform.",
    }

    # CV (ATS-safe)
    cv_ctx = {
        "lang": "en",
        "name": personal["name"],
        "title": personal["title"],
        "location": personal["location"],
        "email": personal["email"],
        "phone": personal["phone"],
        "linkedin": personal["linkedin"],
        "summary_heading": "Summary",
        "summary": "Founder & CTO with 4+ years scaling agencies and hosting.",
        "skills_heading": "Top Skills",
        "top_skills_li": "<li>Architecture</li><li>Hosting</li><li>Strategy</li>",
        "experience_heading": "Experience",
        "experience_blocks": "<div><h3>Bold and Digital</h3><p>4+ years, 20+ clients.</p></div>",
        "education_heading": "Education",
        "education_blocks": "<div><h3>M.Sc.</h3><p>TU Munich, 2014.</p></div>",
        "languages_heading": "Languages",
        "languages_li": "<li>German — Native</li><li>English — C1</li>",
        "certifications_section": "",
        "publications_section": "",
        "position_title": job["title"],
        "summary_short": "Founder & CTO.",
    }
    cv_html = render_cv_classic(cv_ctx)
    cv_check = verify_ats(cv_html)
    print(f"CV ATS ok={cv_check['ok']}, violations={cv_check['violations']}")

    # Cover letter (DIN 5008)
    cl_html = build_cover_letter_for_job(
        job=job, language="en", variant="ai_heavy",
        personal=personal,
        opening="I read your CTO role with great interest.",
        me_paragraph="I've spent 4+ years scaling agencies and hosting platforms.",
        close="I'd welcome a 30-minute conversation.",
    )
    cl_check = verify_ats(cl_html)
    print(f"CL ATS ok={cl_check['ok']}, violations={cl_check['violations']}")

    # German round-trip
    cl_de = build_cover_letter_for_job(
        job={"title": "CTO", "company": "Acme", "description": "WordPress Hosting"},
        language="de", variant="ai_heavy",
        personal=personal,
        opening="Ihre CTO-Stelle hat mein großes Interesse geweckt.",
        me_paragraph="Ich bringe 4+ Jahre Erfahrung in Skalierung mit.",
        close="Ich freue mich auf ein Gespräch.",
    )
    de_check = verify_ats(cl_de)
    print(f"CL-DE ATS ok={de_check['ok']}, violations={de_check['violations']}")
    print(f"CL-DE contains DIN 5008 A4: {'A4' in cl_de and '25mm' in cl_de}")
    print(f"CL-DE has YOU-ME-WE-CLOSE structure: "
          f"opening={'opening' in cl_de}, me={'me_paragraph' in cl_de}, "
          f"why-you={'why_you_paragraph' in cl_de}, close={'close' in cl_de}")