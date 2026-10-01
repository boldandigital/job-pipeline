#!/usr/bin/env python3
"""
Render all 3 CV template variants for a single job.

Output: data/batches/<slug>/cv-{modern,classic,legacy}.pdf

Variants:
- modern:   templates/cv-modern-v1.html  (ATS-safe + JSON-LD + ARIA)
- classic:  templates/cv-classic.html     (original ADOPT-3 ATS-safe)
- legacy:   templates/cv-classic-v2.html  (pre-2010 CSS, max compatibility)

Usage:
  python scripts/render_cv_variants.py --job-id 83 --lang de --slug picard-b2b-ecom
"""
import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

from src.generation.ats_templates import (
    render_cv_classic,
    _PROJECT_ROOT as TPL_ROOT,
)


def build_cv_context(job: dict, profile: dict) -> dict:
    """Map jobs+profile rows to the token dict expected by all 3 templates."""
    import sqlite3
    db = sqlite3.connect(str(_PROJECT_ROOT / "data" / "jobs.db"))
    db.row_factory = sqlite3.Row

    cv_data = json.loads((_PROJECT_ROOT / "config" / "lars-cv-data.json").read_text())

    personal = cv_data["personal"]
    summary = cv_data.get(f"summary_{profile['lang']}", cv_data["summary_en"])
    experience = cv_data.get("experience", [])
    education = cv_data.get("education", [])
    skills = cv_data.get("skills", {}).get(profile["lang"], [])
    languages = cv_data.get("languages", [])
    certs = cv_data.get("certifications", [])

    # Build HTML blocks
    def exp_block(exp, lang):
        title = exp.get(f"title_{lang}", exp.get("title_en", ""))
        role = exp.get(f"role_{lang}", exp.get("role_en", ""))
        bullets = exp.get(f"bullets_{lang}", exp.get("bullets_en", []))
        header_parts = [p for p in [title, exp.get("location", "")] if p]
        header = " · ".join(header_parts)
        bullets_html = "".join(f"<li>{b}</li>" for b in bullets)
        return f"""
<article class="exp-block">
  <div class="exp-header">
    <h3>{header}</h3>
    <span class="meta">{exp.get('period', '')}</span>
  </div>
  <div class="exp-role">{role}</div>
  <ul>{bullets_html}</ul>
</article>"""

    def edu_block(edu, lang):
        return f"""
<article class="exp-block">
  <div class="exp-header">
    <h3>{edu.get('degree', '')}</h3>
    <span class="meta">{edu.get('period', '')}</span>
  </div>
  <div class="exp-role">{edu.get('university', '')}</div>
</article>"""

    lang = profile["lang"]
    label_summary = "Profil" if lang == "de" else "Profile"
    label_skills = "Kenntnisse" if lang == "de" else "Skills"
    label_exp = "Berufserfahrung" if lang == "de" else "Experience"
    label_edu = "Ausbildung" if lang == "de" else "Education"
    label_lang = "Sprachen" if lang == "de" else "Languages"
    label_cert = "Zertifikate" if lang == "de" else "Certifications"

    skills_li = "".join(f"<li>{s}</li>" for s in skills)
    langs_li = "".join(
        f"<li>{l.get(f'lang_{lang}', l.get('lang_en', l.get('lang_de', '')))} — {l.get('level', '')}</li>"
        for l in languages
    )
    certs_li = "".join(f"<li>{c}</li>" for c in certs)

    # Volunteer section (4 entries from CV data)
    volunteer = cv_data.get("volunteer", [])
    volunteer_blocks = "".join(
        f"""
<article class="exp-block">
  <div class="exp-header">
    <h3>{v.get(f'org_{lang}', v.get('org_en', v.get('org_de', '')))}</h3>
    <span class="meta">{v.get('period', '')}</span>
  </div>
  <div class="exp-role">{v.get(f'role_{lang}', v.get('role_en', ''))}</div>
  <ul>{''.join(f'<li>{b}</li>' for b in v.get(f'bullets_{lang}', v.get('bullets_en', [])))}</ul>
</article>"""
        for v in volunteer
    )

    # Hobbies (simple list)
    hobbies = cv_data.get("hobbies", [])
    hobbies_li = "".join(f"<li>{h}</li>" for h in hobbies)

    label_volunteer = "Ehrenamt" if lang == "de" else "Volunteer"
    label_hobbies = "Interessen" if lang == "de" else "Interests"

    volunteer_section = f"""
    <section aria-labelledby="vol-h">
      <h2 id="vol-h">{label_volunteer}</h2>
      {volunteer_blocks}
    </section>""" if volunteer_blocks else ""

    hobbies_section = f"""
    <section aria-labelledby="hob-h">
      <h2 id="hob-h">{label_hobbies}</h2>
      <ul class="grid">{hobbies_li}</ul>
    </section>""" if hobbies_li else ""

    photo_url = personal.get("photo_path", "") or "config/photo.jpg"

    return {
        "lang": lang,
        "name": personal.get("name", ""),
        "position_title": personal.get(f"title_{lang}", personal.get("title_en", "")),
        "tagline": personal.get(f"title_{lang}", ""),
        "title": personal.get(f"title_{lang}", ""),
        "location": personal.get("location", ""),
        "email": personal.get("email", ""),
        "phone": personal.get("phone", ""),
        "linkedin_url": personal.get("linkedin", ""),
        "linkedin_short": personal.get("linkedin", "").replace("https://", ""),
        "github_url": personal.get("github", ""),
        "github_short": personal.get("github", "").replace("https://", ""),
        "photo_url": photo_url,
        "summary_heading": label_summary,
        "summary": summary,
        "summary_short": summary[:160] + "...",
        "skills_heading": label_skills,
        "skills_li": skills_li,
        "experience_heading": label_exp,
        "experience_blocks": "".join(exp_block(e, lang) for e in experience),
        "education_heading": label_edu,
        "education_blocks": "".join(edu_block(e, lang) for e in education),
        "certifications_heading": label_cert,
        "certifications_block": (
            f"<ul class='certs'>{certs_li}</ul>" if certs_li else ""
        ),
        "certifications_li": certs_li,
        "languages_heading": label_lang,
        "languages_li": langs_li,
        "volunteer_heading": label_volunteer,
        "volunteer_blocks": volunteer_blocks,
        "volunteer_section": volunteer_section,
        "hobbies_heading": label_hobbies,
        "hobbies_li": hobbies_li,
        "hobbies_section": hobbies_section,
        # Structured data tokens (modern v1 only)
        "keywords_csv": ", ".join(skills[:8]),
        "languages_jsonld": json.dumps([
            l.get("lang_en", l.get("lang_de", "")) for l in languages
        ]),
        "education_jsonld": json.dumps([
            {"@type": "EducationalOrganization", "name": e.get("university", "")}
            for e in education
        ]),
        "experience_jsonld_first": json.dumps({
            "@type": "Organization",
            "name": experience[0].get("company", "") if experience else "",
            "member": {"@type": "OrganizationRole", "roleName": "Founder & CEO"},
        }) if experience else "{}",
        "certifications_jsonld": json.dumps([
            {"@type": "EducationalOccupationalCredential", "name": c}
            for c in certs
        ]),
    }


def html_to_pdf(html: str, output_path: Path) -> None:
    """Render HTML to PDF. Handles relative photo paths by reading the file
    and inlining it as base64 data URL — Patchright needs absolute URLs.
    """
    import re
    import base64
    import tempfile
    from playwright.sync_api import sync_playwright

    # Inline any <img src="config/photo.jpg"> or similar relative paths
    def _inline_local_img(match):
        prefix = match.group(1)
        path = match.group(2)
        if path.startswith(("http://", "https://", "data:", "file://")):
            return match.group(0)
        full_path = (_PROJECT_ROOT / path).resolve()
        if not full_path.exists():
            return match.group(0)
        ext = full_path.suffix.lower().lstrip(".") or "jpeg"
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(ext, "jpeg")
        b64 = base64.b64encode(full_path.read_bytes()).decode()
        return f'{prefix}data:image/{mime};base64,{b64}{match.group(3)}'

    html = re.sub(r'(<img[^>]*src=")([^"]+)("[^>]*>)', _inline_local_img, html)

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
            page.pdf(path=str(output_path), format="A4", print_background=True)
            browser.close()
    finally:
        import os
        os.unlink(tmp_path)


def main():
    parser = argparse.ArgumentParser(description="Render all CV variants for a job")
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--lang", choices=["en", "de"], default="de")
    parser.add_argument("--slug", required=True, help="Batch dir name (e.g. picard-b2b-ecom)")
    parser.add_argument("--template", choices=["modern", "classic", "bold", "legacy", "all"],
                        default="all",
                        help="Which CV template to render (default: all)")
    parser.add_argument("--letters", action="store_true",
                        help="Also render Anschreiben + Motivationsschreiben in BOLD style")
    args = parser.parse_args()

    templates = {
        "modern": _PROJECT_ROOT / "templates" / "cv-modern-v1.html",
        "classic": _PROJECT_ROOT / "templates" / "cv-classic.html",
        "bold": _PROJECT_ROOT / "templates" / "cv-bold.html",
        "legacy": _PROJECT_ROOT / "templates" / "cv-classic-v2.html",
    }
    if args.template != "all":
        templates = {args.template: templates[args.template]}

    out_dir = _PROJECT_ROOT / "data" / "batches" / args.slug
    out_dir.mkdir(parents=True, exist_ok=True)

    ctx = build_cv_context(
        job={"id": args.job_id},
        profile={"lang": args.lang},
    )

    for variant, tpl_path in templates.items():
        if not tpl_path.exists():
            print(f"  ✗ Template missing: {tpl_path}")
            continue
        print(f"  → Rendering {variant} ({tpl_path.name})...")
        html = render_cv_classic(ctx, tpl_path)
        out_path = out_dir / f"cv-{variant}.pdf"
        html_to_pdf(html, out_path)
        print(f"    ✓ {out_path.relative_to(_PROJECT_ROOT)}  ({out_path.stat().st_size} bytes)")

    # Also write the HTML so ATS parser can inspect
    for variant, tpl_path in templates.items():
        if not tpl_path.exists():
            continue
        html = render_cv_classic(ctx, tpl_path)
        html_path = out_dir / f"cv-{variant}.html"
        html_path.write_text(html, encoding="utf-8")
        print(f"    ✓ {html_path.relative_to(_PROJECT_ROOT)}  ({html_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
