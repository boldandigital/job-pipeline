#!/usr/bin/env python3
"""
CV Generator — HTML to PDF via Patchright.

Generates pixel-perfect CVs from structured data. Supports multiple variants
(ai_heavy, technical, product, operations) with different emphasis.

Usage:
    python -m src.generation.cv_generator \
        --company "Acme Corp" \
        --tagline "AI & Automation Specialist" \
        --language en \
        --output ./output/CV_Acme.pdf
"""

import argparse
import base64
import json
import os
import sys
import tempfile

# ============================================================================
# CONFIGURATION — loaded from environment or config files
# ============================================================================
PERSONAL = {
    "name": os.getenv("CANDIDATE_NAME", "Your Name"),
    "email": os.getenv("CANDIDATE_EMAIL", "your.email@example.com"),
    "phone": os.getenv("CANDIDATE_PHONE", "+49 123 456789"),
    "location": os.getenv("CANDIDATE_LOCATION", "Berlin"),
}

PHOTO_PATH = os.getenv("PHOTO_PATH", "./config/photo.png")

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

EDUCATION = {
    "masters": {
        "degree_en": "M.Sc. Business Informatics",
        "degree_de": "M.Sc. Wirtschaftsinformatik",
        "university": "University of Berlin",
        "period": "2022 – 2024",
        "focus_en": "Data Analytics, AI/ML, Digital Business",
        "focus_de": "Data Analytics, AI/ML, Digital Business",
    },
    "bachelors": {
        "degree_en": "B.Sc. Business Informatics",
        "degree_de": "B.Sc. Wirtschaftsinformatik",
        "university": "University of Munich",
        "period": "2018 – 2022",
    },
}

PROJECTS = [
    {
        "name": "Job Search Pipeline",
        "desc_en": "Autonomous job aggregation system — 4 platforms, scoring engine, Telegram alerts",
        "desc_de": "Autonomes Job-Aggregationssystem — 4 Plattformen, Scoring-Engine, Telegram-Alerts",
        "tech": "Python, SQLite, Docker, Claude API, Playwright",
    },
    {
        "name": "AI Content Engine",
        "desc_en": "Content generation platform with scheduling, quality detection, API endpoints",
        "desc_de": "Content-Generierungsplattform mit Scheduling, Qualitätserkennung, API-Endpoints",
        "tech": "TypeScript, Node.js, PostgreSQL, Redis",
    },
]

SKILLS = {
    "ai_llm": ["Claude API", "GPT-4", "LangChain", "RAG", "Prompt Engineering", "ComfyUI"],
    "automation": ["n8n", "Zapier", "UiPath", "Power Automate", "Docker"],
    "programming": ["Python", "TypeScript/JavaScript", "SQL", "Bash"],
    "cloud": ["AWS", "Azure", "Docker", "Git", "CI/CD", "Linux"],
    "business": ["Jira", "Confluence", "Power BI", "Tableau", "Excel"],
}

LANGUAGES = [
    {"name": "German", "level": "Native"},
    {"name": "English", "level": "Fluent (C1)"},
]


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
        order = ["experience", "education", "projects", "skills"]

    photo_b64 = ""
    if os.path.exists(PHOTO_PATH):
        with open(PHOTO_PATH, "rb") as f:
            photo_b64 = base64.b64encode(f.read()).decode()

    sections = []

    for section in order:
        if section == "experience":
            html = f'<h2>{"Berufserfahrung" if lang == "de" else "Experience"}</h2>'
            for key, exp in EXPERIENCE.items():
                title = exp.get(f"title_{lang}", exp.get("title_en", ""))
                role = exp.get(f"role_{lang}", exp.get("role_en", ""))
                bullets = exp.get(f"bullets_{lang}", exp.get("bullets_en", []))
                html += f"""
                <div class="entry">
                    <div class="entry-header">
                        <span>{title}</span>
                        <span>{exp['period']}</span>
                    </div>
                    <div class="entry-role">{role}</div>
                    <ul>{"".join(f"<li>{b}</li>" for b in bullets)}</ul>
                </div>"""
            sections.append(html)

        elif section == "education":
            html = f'<h2>{"Ausbildung" if lang == "de" else "Education"}</h2>'
            for key, edu in EDUCATION.items():
                degree = edu.get(f"degree_{lang}", edu.get("degree_en", ""))
                html += f"""
                <div class="entry">
                    <div class="entry-header">
                        <span>{degree}</span>
                        <span>{edu['period']}</span>
                    </div>
                    <div class="entry-role">{edu['university']}</div>
                </div>"""
            sections.append(html)

        elif section == "projects":
            html = f'<h2>{"Projekte" if lang == "de" else "Projects"}</h2>'
            for proj in PROJECTS:
                desc = proj.get(f"desc_{lang}", proj.get("desc_en", ""))
                html += f"""
                <div class="entry">
                    <div class="entry-header"><span>{proj['name']}</span></div>
                    <div>{desc}</div>
                    <div style="color:#718096;font-size:8.5pt">{proj['tech']}</div>
                </div>"""
            sections.append(html)

        elif section == "skills":
            html = f'<h2>{"Kenntnisse" if lang == "de" else "Skills"}</h2><div class="skills-grid">'
            for cat, items in SKILLS.items():
                label = cat.replace("_", " ").title()
                html += f'<div><span class="skill-category">{label}:</span> {", ".join(items)}</div>'
            html += "</div>"

            html += f'<h2>{"Sprachen" if lang == "de" else "Languages"}</h2>'
            html += ", ".join(f"{l['name']} ({l['level']})" for l in LANGUAGES)
            sections.append(html)

    photo_html = ""
    if photo_b64:
        photo_html = f'<img class="photo" src="data:image/png;base64,{photo_b64}" alt="Photo">'

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{get_css()}</style></head>
<body>
    <div class="header">
        <div class="header-left">
            <h1>{PERSONAL['name']}</h1>
            <div class="tagline">{tagline}</div>
        </div>
        <div style="display:flex;align-items:flex-start">
            <div class="contact">
                {PERSONAL['email']}<br>
                {PERSONAL['phone']}<br>
                {PERSONAL['location']}
            </div>
            {photo_html}
        </div>
    </div>
    {"".join(sections)}
</body></html>"""


def html_to_pdf(html, output_path):
    """Convert HTML to PDF using Patchright (headless Chromium)."""
    from patchright.sync_api import sync_playwright

    with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8") as f:
        f.write(html)
        tmp_path = f.name

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
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
    parser.add_argument("--order", default="experience,education,projects,skills",
                        help="Section order (comma-separated)")
    parser.add_argument("--output", default=None, help="Output PDF path")
    args = parser.parse_args()

    order = [s.strip() for s in args.order.split(",")]
    output = args.output or f"./output/CV_{PERSONAL['name'].replace(' ', '_')}_{args.company}.pdf"

    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    html = build_html(args.tagline, args.language, order)
    html_to_pdf(html, output)


if __name__ == "__main__":
    main()
