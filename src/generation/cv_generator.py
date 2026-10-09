#!/usr/bin/env python3
"""
CV Generator — HTML to PDF via Patchright.

Generates pixel-perfect CVs from structured data. Loads real profile from
config/lars-cv-data.json (sourced from profiles/lars-zimmermann.md).

Usage:
    python -m src.generation.cv_generator \
        --company "Acme Corp" \
        --tagline "Founder & CEO — Digital Agency, Cloud Hosting, Branding & Growth" \
        --language en \
        --output ./output/CV_Acme.pdf
"""

import argparse
import base64
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# ============================================================================
# CONFIGURATION — load real profile from config/lars-cv-data.json
# ============================================================================
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CV_DATA_PATH = Path(os.getenv("CV_DATA_PATH", str(_PROJECT_ROOT / "config" / "lars-cv-data.json")))

def _load_cv_data() -> dict:
    """Load Lars's real CV data. Falls back to env vars if file missing."""
    if _CV_DATA_PATH.exists():
        with open(_CV_DATA_PATH) as f:
            return json.load(f)
    # Fallback: minimal env-var only mode
    return {
        "personal": {
            "name": os.getenv("CANDIDATE_NAME", "Lars Zimmermann"),
            "email": os.getenv("CANDIDATE_EMAIL", "lars.z@icloud.com"),
            "phone": os.getenv("CANDIDATE_PHONE", ""),
            "location": os.getenv("CANDIDATE_LOCATION", "Aarschot, Flemish Region, Belgium"),
        }
    }


def _load_profile_override(user_id: str, profile_id: str) -> dict:
    """Phase 2.7: build a CV_DATA-shaped dict from a profile row.

    The shape mirrors ``config/lars-cv-data.json`` so the existing
    rendering code (PERSONAL, SKILLS, EXPERIENCE, LANGUAGES, …) keeps
    working without touching it. ``headline`` and ``summary`` are
    mapped onto the legacy ``title_en`` / ``summary_en`` keys.
    """
    from src.db import profiles_db as _profiles_db
    profile = _profiles_db.get_profile(user_id, profile_id)
    if profile is None:
        return {}
    # Pull name + email + location from the user row when present.
    personal: dict[str, Any] = {
        "name": "",
        "email": "",
        "phone": "",
        "location": "",
    }
    if user_id:
        try:
            from src.db import users_db as _users_db
            row = _users_db.get_user_by_id(user_id)
            if row is not None:
                personal["name"] = row["name"] or personal["name"]
                personal["email"] = row["email"] or personal["email"]
        except Exception:
            pass
    # Links → flat linkedin / github / websites.
    for link in profile.get("links") or []:
        kind = (link.get("kind") or "").lower()
        url = link.get("url") or ""
        if kind == "linkedin":
            personal["linkedin"] = url
        elif kind == "github":
            personal["github"] = url
        elif kind in ("portfolio", "other"):
            # CV personal has a `websites` list; append rather than replace.
            sites = list(personal.get("websites") or [])
            if url and url not in sites:
                sites.append(url)
            personal["websites"] = sites

    # Experience profile rows use {company, role, start, end, bullets};
    # lars-cv-data.json uses {period, role_en, bullets_en}. Map between
    # them so the rendering code can iterate the same shape.
    experience: list[dict[str, Any]] = []
    for entry in profile.get("experience") or []:
        period = " → ".join(
            x for x in (entry.get("start", ""), entry.get("end", "")) if x
        )
        experience.append({
            "period": period,
            "company": entry.get("company", ""),
            "title_en": entry.get("company", ""),
            "title_de": entry.get("company", ""),
            "role_en": entry.get("role", ""),
            "role_de": entry.get("role", ""),
            "bullets_en": entry.get("bullets", []) or [],
            "bullets_de": entry.get("bullets", []) or [],
        })

    return {
        "personal": personal,
        "title_en": profile.get("headline", ""),
        "title_de": profile.get("headline", ""),
        "summary_en": profile.get("summary", ""),
        "summary_de": profile.get("summary", ""),
        "experience": experience,
        "skills": {"en": profile.get("skills") or []},
        "languages": [],
        "certifications": [],
    }


def _apply_profile_data(data: dict) -> None:
    """Patch the module-level CV_DATA / PERSONAL / SKILLS globals."""
    global CV_DATA, PERSONAL, SKILLS, EXPERIENCE, LANGUAGES
    CV_DATA = data
    PERSONAL = CV_DATA.get("personal", PERSONAL)
    raw_skills = CV_DATA.get("skills", {})
    if isinstance(raw_skills, dict):
        SKILLS = {
            "en": list(raw_skills.get("en") or []),
            "de": list(raw_skills.get("de") or raw_skills.get("en") or []),
        }
    else:
        SKILLS = {"en": list(raw_skills or [])}
    EXPERIENCE = CV_DATA.get("experience", EXPERIENCE)
    LANGUAGES = CV_DATA.get("languages", LANGUAGES)


CV_DATA = _load_cv_data()
PERSONAL = CV_DATA["personal"]

PHOTO_PATH = os.getenv("PHOTO_PATH", str(_PROJECT_ROOT / "config" / "photo.jpg"))

# ============================================================================
# EXPERIENCE DATA — customize in config/cv_data.json or override below
# ============================================================================
EXPERIENCE = {
    "company_a": {
        "period": "01/2024 – present",
        "title_en": "TechCorp, Berlin (Remote)",
        "role_en": "Data Analyst",
        "title_de": "TechCorp, Berlin (Remote)",
        "role_de": "Data Analyst",
        "bullets_en": [
            "Built automated reporting pipelines using Python and SQL",
            "Developed AI-powered data quality monitoring system",
            "Created dashboards for executive KPI tracking (Tableau, Power BI)",
        ],
        "bullets_de": [
            "Aufbau automatisierter Reporting-Pipelines mit Python und SQL",
            "Entwicklung eines KI-gestützten Datenqualitäts-Monitoring-Systems",
            "Erstellung von Dashboards für KPI-Tracking (Tableau, Power BI)",
        ],
    },
    "company_b": {
        "period": "06/2023 – 12/2023",
        "title_en": "StartupXYZ, Munich",
        "role_en": "Intern, Digital Transformation",
        "title_de": "StartupXYZ, München",
        "role_de": "Praktikant, Digitale Transformation",
        "bullets_en": [
            "Process documentation and optimization for onboarding workflows",
            "Data analysis and reporting using Excel and SQL",
            "CRM administration and data migration",
        ],
        "bullets_de": [
            "Prozessdokumentation und -optimierung für Onboarding-Workflows",
            "Datenanalyse und Reporting mit Excel und SQL",
            "CRM-Administration und Datenmigration",
        ],
    },
}

EXPERIENCE = CV_DATA.get("experience", [])
EDUCATION = CV_DATA.get("education", [])
SKILLS = CV_DATA.get("skills", {"en": [], "de": []})
LANGUAGES = CV_DATA.get("languages", [])


# ============================================================================
# HTML TEMPLATE
# ============================================================================
def get_css():
    """Return CSS for the CV template."""
    return """
    @page {
        size: A4;
        margin: 15mm 18mm 15mm 18mm;
    }
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
        font-family: 'Liberation Sans', 'DejaVu Sans', Arial, sans-serif;
        font-size: 9.5pt;
        line-height: 1.4;
        color: #1a1a1a;
    }
    .header {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        border-bottom: 2px solid #2c5282;
        padding-bottom: 10px;
        margin-bottom: 12px;
    }
    .header-left h1 {
        font-size: 20pt;
        color: #2c5282;
        margin-bottom: 4px;
    }
    .header-left .tagline {
        font-size: 10pt;
        color: #4a5568;
        font-style: italic;
    }
    .contact {
        font-size: 8.5pt;
        color: #4a5568;
        text-align: right;
    }
    .photo {
        width: 80px;
        height: 100px;
        object-fit: cover;
        border-radius: 4px;
        margin-left: 15px;
    }
    h2 {
        font-size: 11pt;
        color: #2c5282;
        border-bottom: 1px solid #e2e8f0;
        padding-bottom: 3px;
        margin: 10px 0 6px 0;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    .entry { margin-bottom: 8px; }
    .entry-header {
        display: flex;
        justify-content: space-between;
        font-weight: bold;
    }
    .entry-role { color: #4a5568; font-style: italic; }
    ul { padding-left: 18px; margin: 3px 0; }
    li { margin-bottom: 2px; }
    .skills-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 4px 20px;
    }
    .skill-category { font-weight: bold; color: #2c5282; }
    """


def build_html(tagline, language="en", order=None):
    """Build the CV HTML document."""
    lang = language.lower()
    if order is None:
        order = ["summary", "experience", "education", "certifications", "skills"]

    photo_b64 = ""
    if os.path.exists(PHOTO_PATH):
        with open(PHOTO_PATH, "rb") as f:
            photo_b64 = base64.b64encode(f.read()).decode()

    sections = []

    for section in order:
        if section == "summary":
            summary = CV_DATA.get(f"summary_{lang}", CV_DATA.get("summary_en", ""))
            if summary:
                html = f'<h2>{"Profil" if lang == "de" else "Profile"}</h2>'
                html += f'<p style="margin-bottom:10px;font-size:9.5pt">{summary}</p>'
                sections.append(html)

        elif section == "experience":
            html = f'<h2>{"Berufserfahrung" if lang == "de" else "Experience"}</h2>'
            for exp in EXPERIENCE:
                title = exp.get(f"title_{lang}", exp.get("title_en", ""))
                role = exp.get(f"role_{lang}", exp.get("role_en", ""))
                bullets = exp.get(f"bullets_{lang}", exp.get("bullets_en", []))
                period = exp.get("period", "")
                location = exp.get("location", "")
                header_parts = []
                if title:
                    header_parts.append(title)
                if location:
                    header_parts.append(location)
                header = " · ".join(header_parts)
                html += f"""
                <div class="entry">
                    <div class="entry-header">
                        <span>{header}</span>
                        <span>{period}</span>
                    </div>
                    <div class="entry-role">{role}</div>
                    <ul>{"".join(f"<li>{b}</li>" for b in bullets)}</ul>
                </div>"""
            sections.append(html)

        elif section == "education":
            html = f'<h2>{"Ausbildung" if lang == "de" else "Education"}</h2>'
            for edu in EDUCATION:
                degree = edu.get("degree", "")
                html += f"""
                <div class="entry">
                    <div class="entry-header">
                        <span>{degree}</span>
                        <span>{edu.get("period", "")}</span>
                    </div>
                    <div class="entry-role">{edu.get("university", "")}</div>
                </div>"""
            sections.append(html)

        elif section == "certifications":
            certs = CV_DATA.get("certifications", [])
            if certs:
                html = f'<h2>{"Zertifikate" if lang == "de" else "Certifications"}</h2><ul>'
                for cert in certs:
                    html += f'<li>{cert}</li>'
                html += "</ul>"
                sections.append(html)

        elif section == "skills":
            skills = SKILLS.get(lang, SKILLS.get("en", []))
            html = f'<h2>{"Kenntnisse" if lang == "de" else "Skills"}</h2><ul style="margin-bottom:10px">'
            for skill in skills:
                html += f'<li>{skill}</li>'
            html += "</ul>"

            html += f'<h2>{"Sprachen" if lang == "de" else "Languages"}</h2>'
            html += "<ul>" + "".join(
                f'<li>{l.get("lang_en", l.get("lang_de", ""))} — {l.get("level", "")}</li>'
                for l in LANGUAGES
            ) + "</ul>"
            sections.append(html)

    photo_html = ""
    if photo_b64:
        photo_html = f'<img class="photo" src="data:image/png;base64,{photo_b64}" alt="Photo">'

    contact = []
    if PERSONAL.get("email"):
        contact.append(f'<a href="mailto:{PERSONAL["email"]}">{PERSONAL["email"]}</a>')
    if PERSONAL.get("linkedin"):
        contact.append(f'<a href="{PERSONAL["linkedin"]}">{PERSONAL["linkedin"].replace("https://", "")}</a>')
    if PERSONAL.get("location"):
        contact.append(PERSONAL["location"])
    contact_html = " · ".join(contact)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{get_css()}</style></head>
<body>
    <div class="header">
        <div class="header-left">
            <h1>{PERSONAL.get('name', 'Lars Zimmermann')}</h1>
            <div class="tagline">{tagline}</div>
        </div>
        <div class="contact">{contact_html}</div>
        {photo_html}
    </div>
    {''.join(sections)}
</body>
</html>
"""


def html_to_pdf(html, output_path):
    """Convert HTML to PDF using Playwright (headless Chromium)."""
    from playwright.sync_api import sync_playwright

    with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8") as f:
        f.write(html)
        tmp_path = f.name

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox"],
                executable_path="/Users/lars/Library/Caches/ms-playwright/chromium-1243/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
            )
            page = browser.new_page()
            page.goto(f"file://{tmp_path}", wait_until="networkidle")
            page.pdf(path=output_path, format="A4", print_background=True)
            browser.close()
        print(f"PDF saved: {output_path}")
    finally:
        os.unlink(tmp_path)


def main():
    parser = argparse.ArgumentParser(description="CV Generator — HTML to PDF")
    parser.add_argument("--company", required=True, help="Company name for filename")
    parser.add_argument("--tagline", default="AI & Automation Specialist", help="Tagline under name")
    parser.add_argument("--language", default="en", choices=["en", "de"], help="Language")
    parser.add_argument("--order", default="summary,experience,education,certifications,skills",
                        help="Section order (comma-separated)")
    parser.add_argument("--output", default=None, help="Output PDF path")
    # Phase 2.7: optional profile override. When --profile-id is set,
    # the CV renders from that profile row instead of the global
    # config/lars-cv-data.json. --user-id defaults to the bootstrap
    # admin when not provided.
    parser.add_argument("--user-id", default=os.getenv("LARS_USER_ID", "lars"),
                        help="Owner of the profile (defaults to admin/LARS_USER_ID)")
    parser.add_argument("--profile-id", default=None,
                        help="Phase 2.7: render from a specific profile row. "
                             "Falls back to config/lars-cv-data.json when omitted.")
    args = parser.parse_args()

    # Phase 2.7: when a profile-id is given, override the global CV_DATA
    # with the profile's headline / summary / experience / skills /
    # links before the HTML is built. Without --profile-id, behaviour
    # is unchanged (the legacy global config drives everything).
    if args.profile_id:
        profile_data = _load_profile_override(args.user_id, args.profile_id)
        if profile_data:
            _apply_profile_data(profile_data)
            # Also use the profile's headline as the tagline when the
            # caller didn't pass --tagline explicitly.
            if args.tagline == "AI & Automation Specialist":
                default_headline = (
                    (profile_data.get("title_en") or "").strip()
                    or "AI & Automation Specialist"
                )
                args.tagline = default_headline

    order = [s.strip() for s in args.order.split(",")]
    output = args.output or f"./output/CV_{PERSONAL.get('name', 'candidate').replace(' ', '_')}_{args.company}.pdf"

    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    html = build_html(args.tagline, args.language, order)
    html_to_pdf(html, output)


if __name__ == "__main__":
    main()
