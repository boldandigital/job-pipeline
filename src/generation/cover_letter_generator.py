#!/usr/bin/env python3
"""
Cover Letter Generator — HTML to PDF via Patchright.

Generates matching cover letters with the same visual style as the CV.

Usage:
    python -m src.generation.cover_letter_generator \
        --company "Acme Corp" \
        --role "AI Specialist" \
        --language en \
        --body "First paragraph.||Second paragraph.||Closing." \
        --output ./output/CL_Acme.pdf
"""

import argparse
import base64
import json
import os
import sys
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CV_DATA_PATH = Path(os.getenv("CV_DATA_PATH", str(_PROJECT_ROOT / "config" / "lars-cv-data.json")))

def _load_cv_data() -> dict:
    if _CV_DATA_PATH.exists():
        with open(_CV_DATA_PATH) as f:
            return json.load(f)
    return {
        "personal": {
            "name": os.getenv("CANDIDATE_NAME", "Lars Zimmermann"),
            "email": os.getenv("CANDIDATE_EMAIL", "lars.z@icloud.com"),
            "phone": os.getenv("CANDIDATE_PHONE", ""),
            "location": os.getenv("CANDIDATE_LOCATION", "Aarschot, Flemish Region, Belgium"),
        }
    }

CV_DATA = _load_cv_data()
PERSONAL = CV_DATA["personal"]

PHOTO_PATH = os.getenv("PHOTO_PATH", "./config/photo.png")


def get_css():
    """Return CSS matching the CV template style."""
    return """
    @page {
        size: A4;
        margin: 20mm 22mm 20mm 22mm;
    }
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
        font-family: 'Liberation Serif', 'DejaVu Serif', Georgia, serif;
        font-size: 10.5pt;
        line-height: 1.5;
        color: #1a1a1a;
    }
    .header {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        border-bottom: 2px solid #2c5282;
        padding-bottom: 10px;
        margin-bottom: 20px;
    }
    .header h1 {
        font-size: 18pt;
        color: #2c5282;
    }
    .contact {
        font-size: 9pt;
        color: #4a5568;
        text-align: right;
    }
    .photo {
        width: 70px;
        height: 88px;
        object-fit: cover;
        border-radius: 4px;
        margin-left: 12px;
    }
    .date {
        text-align: right;
        margin-bottom: 20px;
        color: #4a5568;
    }
    .recipient {
        margin-bottom: 20px;
    }
    .subject {
        font-weight: bold;
        margin-bottom: 15px;
    }
    .body p {
        margin-bottom: 12px;
        text-align: justify;
    }
    .signature {
        margin-top: 25px;
        page-break-after: avoid;
        break-after: avoid;
    }
    /* Force everything to fit on one page if possible */
    .header, .date, .recipient, .subject, .body, .signature {
        page-break-inside: avoid;
    }
    """


def build_html(company, role, recipient, body_paragraphs, language="en", job_location=None):
    """Build cover letter HTML.

    job_location: if provided, used in the recipient address block
                 (e.g., "Rostock, Germany" for Zasta Karriere).
                 If None, falls back to candidate's own location.
    """
    lang = language.lower()
    date_str = datetime.now().strftime("%d.%m.%Y") if lang == "de" else datetime.now().strftime("%B %d, %Y")

    if lang == "de":
        subject = f"Bewerbung als {role}"
        greeting = f"Sehr geehrte(r) {recipient},"
        closing = "Mit freundlichen Grüßen"
    else:
        subject = f"Application for {role}"
        greeting = f"Dear {recipient},"
        closing = "Kind regards"

    photo_b64 = ""
    if os.path.exists(PHOTO_PATH):
        with open(PHOTO_PATH, "rb") as f:
            photo_b64 = base64.b64encode(f.read()).decode()

    photo_html = f'<img class="photo" src="data:image/png;base64,{photo_b64}" alt="">' if photo_b64 else ""

    body_html = "\n".join(f"<p>{p.strip()}</p>" for p in body_paragraphs if p.strip())

    # Date line uses candidate's own city (sender = applicant)
    sender_city = PERSONAL['location']

    # Recipient address block uses the JOB location (where the company is)
    job_loc_display = job_location or PERSONAL['location']

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{get_css()}</style></head>
<body>
    <div class="header">
        <div>
            <h1>{PERSONAL['name']}</h1>
        </div>
        <div style="display:flex;align-items:flex-start">
            <div class="contact">
                {PERSONAL['email']}<br>
                {PERSONAL['phone']}<br>
                {sender_city}
            </div>
            {photo_html}
        </div>
    </div>

    <div class="date">{sender_city}, {date_str}</div>

    <div class="recipient">
        {company}<br>
        {job_loc_display}
    </div>

    <div class="subject">{subject}</div>

    <div class="body">
        <p>{greeting}</p>
        {body_html}
    </div>

    <div class="signature">
        {closing}<br><br>
        {PERSONAL['name']}
    </div>
</body></html>"""


def html_to_pdf(html, output_path):
    """Convert HTML to PDF using Patchright."""
    import tempfile
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
    parser = argparse.ArgumentParser(description="Cover Letter Generator")
    parser.add_argument("--company", required=True, help="Company name")
    parser.add_argument("--role", required=True, help="Job title/role")
    parser.add_argument("--recipient", default="Hiring Manager", help="Letter recipient")
    parser.add_argument("--language", default="en", choices=["en", "de"])
    parser.add_argument("--body", default=None, help="Paragraphs separated by ||")
    parser.add_argument("--body-file", default=None, help="Read body from file")
    parser.add_argument("--output", default=None, help="Output PDF path")
    parser.add_argument("--job-location", default=None,
                        help="Location of the JOB (used in the letter — NOT candidate's location)")
    args = parser.parse_args()

    if args.body_file and os.path.exists(args.body_file):
        with open(args.body_file) as f:
            paragraphs = f.read().split("||")
    elif args.body:
        paragraphs = args.body.split("||")
    else:
        paragraphs = ["[Your cover letter content here. Use --body or --body-file to provide content.]"]

    output = args.output or f"./output/CL_{args.company.replace(' ', '_')}.pdf"
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)

    html = build_html(
        args.company, args.role, args.recipient,
        paragraphs, args.language,
        job_location=args.job_location,
    )
    html_to_pdf(html, output)


if __name__ == "__main__":
    main()
