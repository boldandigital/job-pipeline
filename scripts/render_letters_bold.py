#!/usr/bin/env python3
"""
Render BOLD-style cover letter + motivation letter for a job.

Output: data/batches/<slug>/{anschreiben-bold,motivationsschreiben-bold}.pdf

Usage:
  python scripts/render_letters_bold.py --slug picard-b2b-ecom --lang de
"""
import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

from src.generation.ats_templates import render_cv_classic


def _today_de() -> str:
    import locale
    try:
        locale.setlocale(locale.LC_TIME, "de_DE.UTF-8")
    except locale.Error:
        pass
    from datetime import datetime
    return datetime.now().strftime("%d. %B %Y")


def _today_en() -> str:
    from datetime import datetime
    return datetime.now().strftime("%B %d, %Y")


def build_anschreiben_context(job: dict, profile: dict) -> dict:
    """Build the token context for the Anschreiben BOLD template.

    The body paragraphs are tailored to PICARD: B2B e-commerce + sales role.
    For other jobs, swap with LLM-generated body paragraphs.
    """
    import sqlite3
    db = sqlite3.connect(str(_PROJECT_ROOT / "data" / "jobs.db"))
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT title, company, url FROM jobs WHERE id = ?",
        (job["id"],),
    ).fetchone()
    db.close()
    if row:
        company = row["company"]
        role = row["title"]
        recipient_name = "die Personalverantwortlichen" if profile["lang"] == "de" else "Hiring Manager"
    else:
        company = profile.get("company", "Ihr Unternehmen")
        role = profile.get("role", "die ausgeschriebene Position")
        recipient_name = "die Personalverantwortlichen" if profile["lang"] == "de" else "Hiring Manager"

    cv_data = json.loads((_PROJECT_ROOT / "config" / "lars-cv-data.json").read_text())
    personal = cv_data["personal"]
    photo_url = personal.get("photo_path", "") or "config/photo.jpg"

    if profile["lang"] == "de":
        date_str = _today_de()
        subject = f"Bewerbung als {role}"
        salutation = f"Sehr geehrte(r) {recipient_name},"
        opening = (
            "Mit großem Interesse habe ich Ihre Stellenausschreibung für die Position "
            f"als {role} bei {company} gelesen. Als Gründer und CEO einer "
            "Digitalagentur mit Standorten in Belgien und einem über 12-jährigen "
            "track record in B2B-Vertrieb, Pricing-Strategie und "
            "Marktpositionierung — darunter 6 Jahre in Shanghai mit DACH- und APAC-Kunden — "
            "bringe ich genau die Erfahrung mit, die Sie suchen."
        )
        me_paragraph = (
            "Mein beruflicher Hintergrund verbindet strategisches Marketing mit "
            "hands-on technischer Umsetzung. Als Gründer der Bold and Digital LLC "
            "(seit 2020) betreue ich 20+ internationale Kunden — darunter LiteSpeed "
            "Technologies und QUIC.cloud — durch 4Ps/4S-Marketing-Frameworks und "
            "Preisstrategie-Refits, die Margen um 20-30% verbessern. Parallel "
            "betreibe ich HostSalt (seit 2021), eine Cloud-Hosting-Plattform mit "
            "500+ gehosteten Sites, 99,9% Verfügbarkeit und einer LCP-Verbesserung "
            "von median 35-50% pro optimierter Site."
        )
        why_you_paragraph = (
            f"Was {company} besonders macht, ist die Verbindung von industrieller "
            "Tradition mit digitalem Wachstum — eine Dynamik, die ich aus meiner "
            "Tätigkeit für Axus Stationery (Lieferant von Faber-Castell, Maped, BIC) "
            "und Neobear (Qualcomm-/ZTE-backed) kenne. Ich habe in beiden Fällen "
            "OEM-zu-ODM-Übergänge orchestriert, Amazon-Portfolios von 50+ Listings "
            "skaliert und ROAS-Werte von 4-6x erreicht. Diese Erfahrung in der "
            "Schnittstelle zwischen Handel und digitaler Wertschöpfung passt "
            "präzise zu Ihrem Fokus auf B2B-E-Commerce."
        )
        close = (
            "Ich freue mich auf ein persönliches Gespräch, um zu erörtern, wie "
            "mein Profil Ihre Vertriebs- und Digitalstrategie bei "
            f"{company} in den kommenden Quartalen konkret stärken kann. "
            "Bitte zögern Sie nicht, mich jederzeit zu kontaktieren."
        )
        recipient_block = (
            f"{company}<br>"
            "— Personalabteilung —<br>"
            "Bochum, Deutschland"
        )
    else:
        date_str = _today_en()
        subject = f"Application for {role}"
        salutation = f"Dear {recipient_name},"
        opening = (
            f"I am writing to express my strong interest in the {role} position at "
            f"{company}. As founder and CEO of a digital agency with 12+ years "
            "of experience in B2B sales, pricing strategy, and market positioning — "
            "including six years in Shanghai serving DACH and APAC clients — I bring "
            "exactly the cross-cultural e-commerce expertise you are seeking."
        )
        me_paragraph = (
            "My background blends strategic marketing with hands-on technical "
            "execution. As founder of Bold and Digital LLC (since 2020), I serve 20+ "
            "international clients — including LiteSpeed Technologies and QUIC.cloud — "
            "applying 4Ps/4S frameworks and pricing-strategy refits that deliver "
            "20-30% margin improvements. In parallel, I run HostSalt (since 2021), "
            "a managed-hosting platform hosting 500+ sites with 99.9% uptime and a "
            "median LCP improvement of 35-50% per optimized site."
        )
        why_you_paragraph = (
            f"What sets {company} apart is its unique blend of industrial tradition "
            "and digital growth — a dynamic I know well from my time at Axus "
            "Stationery (supplier to Faber-Castell, Maped, BIC) and Neobear "
            "(Qualcomm/ZTE-backed AR-toys). In both roles I orchestrated OEM-to-ODM "
            "transitions, scaled 50+ Amazon listings, and achieved ROAS of 4-6x. "
            "That experience at the intersection of trade and digital value "
            "creation aligns precisely with your focus on B2B e-commerce."
        )
        close = (
            "I look forward to a personal conversation about how my profile can "
            f"concretely strengthen {company}'s sales and digital strategy in the "
            "coming quarters. Please do not hesitate to reach out at any time."
        )
        recipient_block = (
            f"{company}<br>"
            "— Hiring Team —<br>"
            "Bochum, Germany"
        )

    return {
        "lang": profile["lang"],
        "name": personal.get("name", ""),
        "position_title": personal.get("title_de" if profile["lang"] == "de" else "title_en", ""),
        "company": company,
        "role": role,
        "location": personal.get("location", ""),
        "email": personal.get("email", ""),
        "phone": personal.get("phone", ""),
        "linkedin": personal.get("linkedin", ""),
        "linkedin_short": personal.get("linkedin", "").replace("https://", ""),
        "photo_url": photo_url,
        "date": date_str,
        "subject_line": subject,
        "salutation": salutation,
        "opening": opening,
        "me_paragraph": me_paragraph,
        "why_you_paragraph": why_you_paragraph,
        "close": close,
        "signature": "Mit freundlichen Grüßen" if profile["lang"] == "de" else "Kind regards",
        "recipient_block": recipient_block,
        "attachments_list": "",  # override per-job via --attachments flag
    }


def build_motivation_context(job: dict, profile: dict) -> dict:
    """Build the token context for the Motivationsschreiben BOLD template.

    Longer, more personal narrative. Each section is a separate paragraph
    that gets a labeled card in the template.
    """
    base = build_anschreiben_context(job, profile)
    attachments = profile.get("attachments") or []
    base["attachments_list"] = " · ".join(attachments) if attachments else ""
    if profile["lang"] == "de":
        intro = (
            "Es ist nicht selbstverständlich, dass ein deutscher Gründer mit sechs "
            "Jahren Shanghai-Erfahrung, einem belgischen Cloud-Hosting-Unternehmen "
            "und einer Vorliebe für präzise Preisstrategien sich bei einem "
            "traditionsreichen Industrieunternehmen wie Ihrem bewirbt. Aber genau "
            "diese Schnittmenge — internationale Wertschöpfung, digitale Skalierung "
            "und Respekt vor etablierten Marken — macht das Gespräch mit Ihnen für "
            "mich besonders spannend."
        )
        story = (
            "Mein Werdegang ist ein bewusst gewähltes Pendeln zwischen den Welten. "
            "Universität Passau (B.A. Kulturwirtschaft), dann Praktikum im "
            "Deutschen Konsulat Shanghai, sechs Jahre Marketing & Sales bei "
            "Strichpunkt, Axus und Neobear in China, und schließlich der Sprung "
            "nach Belgien mit zwei parallelen Gründungen — Bold and Digital (2020) "
            "und HostSalt (2021). In jeder Station habe ich gelernt, wie man "
            "Vertriebsprozesse so aufsetzt, dass sie internationale und digitale "
            "Logiken miteinander verbinden."
        )
        motivation = (
            "Was mich an Ihrer Position reizt, ist die Aufgabe, einen etablierten "
            "Industrievertrieb in eine digitale Zukunft zu führen. Ich bringe nicht "
            "nur die Marketing-Kompetenz mit, sondern auch die unternehmerische "
            "Disziplin, ROI-zentriert zu denken, Pipeline-Aufbau systematisch zu "
            "betreiben und CRM-Daten wirklich für Entscheidungen zu nutzen — nicht "
            "nur für Reports."
        )
        value = (
            "Konkret kann ich für Sie (1) eine Pricing- und Marktpositionierungs-"
            "Strategie für DACH-B2B-Kunden liefern, (2) einen 12-Monats-Plan für "
            "digitale Vertriebskanäle (SEO/SEM, Performance-Marketing, "
            "Salesforce-Workflows) aufsetzen, und (3) ein KPI-Framework etablieren, "
            "das Marketing- und Sales-Erfolg wirklich messbar macht. Mein "
            "bisheriger Track Record: 20-30% Margenverbesserung, 4-6x ROAS, "
            "Pipeline-Wachstum von 30-50% YoY."
        )
        close = (
            "Ich würde mich freuen, in einem persönlichen Gespräch zu erörtern, "
            "wie ich konkret zum Wachstum Ihres digitalen Geschäfts beitragen kann. "
            "Vielen Dank für Ihre Zeit und Ihre Aufmerksamkeit."
        )
    else:
        intro = (
            "It is not obvious that a German founder with six years in Shanghai, "
            "a Belgian cloud-hosting company, and a fondness for precise pricing "
            "strategy would apply to a heritage industrial company like yours. But "
            "exactly that intersection — international value creation, digital "
            "scaling, and respect for established brands — makes this conversation "
            "with you especially compelling for me."
        )
        story = (
            "My career is a deliberate pendulum between worlds. University of "
            "Passau (B.A. Cultural Economics), internship at the German Consulate "
            "in Shanghai, six years of marketing and sales at Strichpunkt, Axus, "
            "and Neobear in China, and finally the move to Belgium with two parallel "
            "foundings — Bold and Digital (2020) and HostSalt (2021). At every "
            "station I learned how to set up sales processes that genuinely "
            "connect international and digital logic."
        )
        motivation = (
            "What draws me to your role is the challenge of leading an established "
            "industrial sales operation into a digital future. I bring not only "
            "marketing competence but also the entrepreneurial discipline of "
            "ROI-centric thinking, systematic pipeline building, and using CRM "
            "data for actual decisions — not just reports."
        )
        value = (
            "Concretely, I can deliver (1) a pricing and market-positioning strategy "
            "for DACH B2B customers, (2) a 12-month plan for digital sales channels "
            "(SEO/SEM, performance marketing, Salesforce workflows), and (3) a KPI "
            "framework that genuinely makes marketing and sales success measurable. "
            "My track record so far: 20-30% margin improvements, 4-6x ROAS, "
            "pipeline growth of 30-50% YoY."
        )
        close = (
            "I would welcome the opportunity to discuss in person how I can "
            "concretely contribute to growing your digital business. Thank you for "
            "your time and attention."
        )

    base.update({
        "intro_paragraph": intro,
        "story_paragraph": story,
        "motivation_paragraph": motivation,
        "value_paragraph": value,
        "close_paragraph": close,
    })
    return base


def html_to_pdf(html: str, output_path: Path) -> None:
    """Inline local images as base64, render HTML to PDF."""
    import re
    import base64
    import tempfile
    from patchright.sync_api import sync_playwright

    def _inline(match):
        prefix, src, suffix = match.group(1), match.group(2), match.group(3)
        if src.startswith(("http://", "https://", "data:", "file://")):
            return match.group(0)
        full = (_PROJECT_ROOT / src).resolve()
        if not full.exists():
            return match.group(0)
        ext = full.suffix.lower().lstrip(".") or "jpeg"
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png"}.get(ext, "jpeg")
        b64 = base64.b64encode(full.read_bytes()).decode()
        return f'{prefix}data:image/{mime};base64,{b64}{suffix}'

    html = re.sub(r'(<img[^>]*src=")([^"]+)("[^>]*>)', _inline, html)

    with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8") as f:
        f.write(html)
        tmp = f.name
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            page.goto(f"file://{tmp}", wait_until="networkidle")
            page.pdf(path=str(output_path), format="A4", print_background=True)
            browser.close()
    finally:
        import os
        os.unlink(tmp)


def main():
    parser = argparse.ArgumentParser(description="Render BOLD cover letter + motivation letter")
    parser.add_argument("--job-id", type=int, default=83)
    parser.add_argument("--lang", choices=["en", "de"], default="de")
    parser.add_argument("--slug", required=True)
    parser.add_argument("--attachments", type=str, default="",
                        help="Comma-separated list of attachments, e.g. 'CV,Foto,Zeugnisse'")
    args = parser.parse_args()

    profile = {"lang": args.lang}
    if args.attachments:
        profile["attachments"] = [a.strip() for a in args.attachments.split(",") if a.strip()]

    out_dir = _PROJECT_ROOT / "data" / "batches" / args.slug
    out_dir.mkdir(parents=True, exist_ok=True)

    # === Anschreiben ===
    print(f"  → Rendering anschreiben-bold.html ({args.lang})...")
    ans_ctx = build_anschreiben_context({"id": args.job_id}, {"lang": args.lang})
    ans_tpl = _PROJECT_ROOT / "templates" / "anschreiben-bold.html"
    ans_html = render_cv_classic(ans_ctx, ans_tpl)
    ans_pdf = out_dir / "anschreiben-bold.pdf"
    html_to_pdf(ans_html, ans_pdf)
    print(f"    ✓ {ans_pdf.relative_to(_PROJECT_ROOT)}  ({ans_pdf.stat().st_size} bytes)")
    (out_dir / "anschreiben-bold.html").write_text(ans_html, encoding="utf-8")

    # === Motivationsschreiben ===
    print(f"  → Rendering motivationsschreiben-bold.html ({args.lang})...")
    mot_ctx = build_motivation_context({"id": args.job_id}, {"lang": args.lang})
    mot_tpl = _PROJECT_ROOT / "templates" / "motivationsschreiben-bold.html"
    mot_html = render_cv_classic(mot_ctx, mot_tpl)
    mot_pdf = out_dir / "motivationsschreiben-bold.pdf"
    html_to_pdf(mot_html, mot_pdf)
    print(f"    ✓ {mot_pdf.relative_to(_PROJECT_ROOT)}  ({mot_pdf.stat().st_size} bytes)")
    (out_dir / "motivationsschreiben-bold.html").write_text(mot_html, encoding="utf-8")

    print(f"\n  🏴‍☠️ Both letters rendered to {out_dir.relative_to(_PROJECT_ROOT)}")


if __name__ == "__main__":
    main()