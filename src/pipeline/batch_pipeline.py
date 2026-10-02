#!/usr/bin/env python3
"""
Daily batch pipeline: Top N jobs -> CV + Cover Letter -> ZIP -> Telegram.

Selects the highest-scored unprocessed jobs, generates tailored application
documents for each, packages them into a ZIP archive, and sends via Telegram
for manual review before applying.

Usage:
    python -m src.pipeline.batch_pipeline --limit 50
    python -m src.pipeline.batch_pipeline --limit 3 --dry-run
"""

import argparse
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("batch")

DB_PATH = os.getenv("DB_PATH", "./data/jobs.db")
BATCHES_DIR = os.getenv("BATCHES_DIR", "./data/batches")
CV_GENERATOR = os.getenv("CV_GENERATOR", "python -m src.generation.cv_generator")
CL_GENERATOR = os.getenv("CL_GENERATOR", "python -m src.generation.cover_letter_generator")
ENV_FILE = os.getenv("ENV_FILE", "./.env")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# When 1, cover letters use our ATS-safe templates + 4-track WHY-YOU YAML
# (port ADOPT-3). When 0, fall back to dome317's in-Python cover_letter_generator.py.
USE_ATS_TEMPLATES = os.getenv("USE_ATS_TEMPLATES", "1") == "1"
# ADOPT-14: career page auto-discovery (3-layer strategy + 18 ATS detectors).
# When 1, after scoring and before doc generation we run layer1 (JobSpy xref),
# layer2 (StepStone redirect), and layer3 (website probe) to populate the
# jobs.career_url + jobs.ats_type columns. These columns feed ADOPT-12 (CUA submit).
CAREER_DISCOVERY_ENABLED = os.getenv("CAREER_DISCOVERY_ENABLED", "1") == "1"
CAREER_DISCOVERY_MAX_JOBS = int(os.getenv("CAREER_DISCOVERY_MAX_JOBS", "50"))

# CV variant mapping: keywords -> variant tagline
CV_VARIANTS = {
    "ai_heavy": {
        "keywords": ["ai", "ki", "ml", "llm", "prompt", "machine learning",
                     "artificial intelligence", "deep learning", "nlp", "genai"],
        "tagline_de": "AI & Automation Spezialist",
        "tagline_en": "AI & Automation Specialist",
    },
    "technical": {
        "keywords": ["developer", "engineer", "data", "software", "backend",
                     "frontend", "full-stack", "devops", "cloud", "python",
                     "analytics", "bi ", "etl", "pipeline"],
        "tagline_de": "Technical Solutions & Data Engineering",
        "tagline_en": "Technical Solutions & Data Engineering",
    },
    "product": {
        "keywords": ["product", "scrum", "project", "agile", "produkt",
                     "projektmanag", "program", "delivery", "release"],
        "tagline_de": "Produkt- & Projektmanagement",
        "tagline_en": "Product & Project Management",
    },
    "operations": {
        "keywords": ["operations", "revops", "crm", "prozess", "process",
                     "salesforce", "hubspot", "implementation", "rollout",
                     "change", "transformation", "digitalisierung",
                     "automatisierung", "workflow", "excellence"],
        "tagline_de": "Operations & Prozessoptimierung",
        "tagline_en": "Operations & Process Optimization",
    },
}


def load_env():
    """Load environment variables from .env file."""
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())


def send_telegram(message, file_path=None):
    """Send message or file via Telegram Bot API."""
    token = TELEGRAM_BOT_TOKEN or os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = TELEGRAM_CHAT_ID or os.getenv("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        log.warning("Telegram not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")
        return False

    try:
        if file_path and os.path.exists(file_path):
            url = f"https://api.telegram.org/bot{token}/sendDocument"
            import urllib.request
            import mimetypes
            boundary = "----FormBoundary" + str(int(time.time()))
            body = []
            body.append(f"--{boundary}".encode())
            body.append(f'Content-Disposition: form-data; name="chat_id"\r\n'.encode())
            body.append(chat_id.encode())
            body.append(f"\r\n--{boundary}".encode())
            body.append(f'Content-Disposition: form-data; name="caption"\r\n'.encode())
            body.append(message[:1024].encode())
            body.append(f"\r\n--{boundary}".encode())
            filename = os.path.basename(file_path)
            body.append(f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode())
            with open(file_path, "rb") as f:
                body.append(f.read())
            body.append(f"\r\n--{boundary}--".encode())
            data = b"\r\n".join(body)
            req = Request(url, data=data, method="POST")
            req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
            with urlopen(req, timeout=60) as resp:
                return resp.status == 200
        else:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = json.dumps({"chat_id": chat_id, "text": message, "parse_mode": "HTML"}).encode()
            req = Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            with urlopen(req, timeout=30) as resp:
                return resp.status == 200
    except Exception as e:
        log.error("Telegram send failed: %s", e)
        return False


def detect_variant(title, description=""):
    """Detect which CV variant best matches the job."""
    text = f"{title} {description}".lower()
    for variant, config in CV_VARIANTS.items():
        for kw in config["keywords"]:
            if kw in text:
                return variant
    return "operations"  # default


def detect_language(title, company, location=""):
    """Detect job language (de/en) from title, location, and company."""
    german_indicators = [
        # Job-title German keywords (any gender suffix /w/d, /m/w, m/f/d/x etc.)
        "referent", "sachbearbeiter", "koordinator", "berater", "beraterin",
        "leiter", "leiterin", "mitarbeiter", "mitarbeiterin", "fachkraft",
        "beauftragter", "beauftragte", "geschäftsführer", "geschaeftsfuehrer",
        "vertriebsleiter", "marketingleiter", "teamleiter", "bereichsleiter",
        "abteilungsleiter", "geschäftsführung", "vorstand", "aufsichtsrat",
        "spezialist", "spezialistin", "experte", "expertin",
        # German job-title suffixes
        "(m/w)", "(m/w/d)", "(m/w/d/x)", "(f/m/d)", "(w/m/d)",
        # All DACH + DE-speaking cities (any DE company is likely DE-speaking)
        "deutschland", "germany", "österreich", "oesterreich", "austria",
        "schweiz", "switzerland",
        # Top 50+ German cities
        "berlin", "hamburg", "münchen", "muenchen", "munich", "köln", "koeln",
        "cologne", "frankfurt", "stuttgart", "düsseldorf", "duesseldorf",
        "dortmund", "essen", "leipzig", "bremen", "dresden", "hannover",
        "nürnberg", "nuernberg", "duisburg", "bochum", "wuppertal", "bielefeld",
        "bonn", "münster", "muenster", "karlsruhe", "mannheim", "augsburg",
        "wiesbaden", "mönchengladbach", "moenchengladbach", "gelsenkirchen",
        "braunschweig", "chemnitz", "kiel", "aachen", "magdeburg", "freiburg",
        "krefeld", "lübeck", "luebeck", "oberhausen", "erfurt", "mainz",
        "rostock", "kassel", "hagen", "saarbrücken", "saarbruecken",
        "potsdam", "hamm", "ludwigshafen", "oldenburg", "leverkusen", "osnabrück",
        "osnabrueck", "solingen", "heidelberg", "darmstadt", "regensburg",
        "würzburg", "wuernburg", "ingolstadt", "ulm", "heilbronn", "reutlingen",
        "tübingen", "tuebingen", "konstanz", "flensburg", "rostock",
        "kaiserslautern", "trier", "jena", "cottbus", "göttingen", "goettingen",
        # German-speaking Swiss cities
        "zürich", "zurich", "genf", "geneva", "bern", "basel", "lausanne",
        "luzern", "lucerne", "winterthur", "st. gallen",
        # Austrian cities
        "wien", "vienna", "salzburg", "innsbruck", "graz", "linz",
    ]
    text = f"{title} {company} {location}".lower()
    for indicator in german_indicators:
        if indicator in text:
            return "de"
    return "en"


def _personal_from_env() -> dict:
    """Read CANDIDATE_* env vars into a personal dict for templates.

    Falls back to JSON if env vars missing — keeps the ATS-template renderer
    in sync with the JSON source-of-truth that cv_generator / cover_letter
    also use (avoids 'Aarschot' in CV header + 'Brussels' in letter bug).
    """
    # Try JSON first (source of truth)
    cv_data_path = Path(os.getenv(
        "CV_DATA_PATH",
        str(Path(__file__).resolve().parents[2] / "config" / "lars-cv-data.json"),
    ))
    json_data = {}
    if cv_data_path.exists():
        try:
            json_data = json.loads(cv_data_path.read_text())
        except Exception:
            pass
    personal = json_data.get("personal", {}) if isinstance(json_data, dict) else {}

    # If env vars contain placeholder strings ("xxx", "example", etc.) or
    # legacy values that disagree with JSON (Brussels vs Aarschot), let
    # the JSON source-of-truth win.
    def _is_placeholder(val: str) -> bool:
        v = val.lower()
        return any(p in v for p in ("xx xx", "xxx", "example", "your.", "brussels"))

    env_location = os.getenv("CANDIDATE_LOCATION", "")
    if _is_placeholder(env_location):
        env_location = ""
    env_phone = os.getenv("CANDIDATE_PHONE", "")
    if _is_placeholder(env_phone):
        env_phone = ""
    env_email = os.getenv("CANDIDATE_EMAIL", "")
    if _is_placeholder(env_email):
        env_email = ""

    return {
        "name": os.getenv("CANDIDATE_NAME", personal.get("name", "Your Name")),
        "title": os.getenv("CANDIDATE_TITLE", personal.get("title_de", personal.get("title_en", ""))),
        "location": env_location or personal.get("location", "Berlin"),
        "email": env_email or personal.get("email", "your.email@example.com"),
        "phone": env_phone or personal.get("phone", "+49 123 456789"),
        "linkedin": os.getenv("CANDIDATE_LINKEDIN", personal.get("linkedin", "")),
    }


def _generate_cover_letter_ats(job, cl_path, variant, lang, variant_tagline):
    """Render ATS-safe cover letter (HTML -> PDF via Patchright)."""
    try:
        from src.generation.ats_templates import build_cover_letter_for_job
        from playwright.sync_api import sync_playwright
        import tempfile
    except ImportError as e:
        log.error("  ATS templates not importable, skipping CL: %s", e)
        return None

    personal = _personal_from_env()
    if lang == "de":
        opening = (
            f"Ihre Ausschreibung für die Position {job.get('title', '')} bei "
            f"{job.get('company', '')} hat mein großes Interesse geweckt."
        )
        me_paragraph = (
            "Als Unternehmer mit zwei aktiven Firmen — Bold and Digital (Digitalagentur) "
            "und HostSalt (Managed-Hosting) — bringe ich 4+ Jahre operative Erfahrung "
            "in der Skalierung digitaler Agenturen und Hosting-Plattformen mit."
        )
        close = "Ich freue mich auf ein persönliches Gespräch."
    else:
        opening = (
            f"Your opening for the {job.get('title', '')} role at "
            f"{job.get('company', '')} caught my attention immediately."
        )
        me_paragraph = (
            "As an entrepreneur running two active companies — Bold and Digital "
            "(digital agency) and HostSalt (managed hosting) — I bring 4+ years "
            "of operational experience scaling digital agencies and hosting platforms."
        )
        close = "I'd welcome a 30-minute conversation to explore the fit."

    # Recipient block — show JOB's location, NOT candidate's Aarschot address
    job_location = job.get("location", "") or ""
    if lang == "de":
        recipient_block = (
            f"{job.get('company', '')}<br>"
            f"— Personalabteilung —<br>"
            f"{job_location}"
        )
        salutation = "Sehr geehrte Damen und Herren,"
    else:
        recipient_block = (
            f"{job.get('company', '')}<br>"
            f"— Hiring Team —<br>"
            f"{job_location}"
        )
        salutation = "Dear Hiring Team,"

    html = build_cover_letter_for_job(
        job=job, language=lang, variant=variant,
        personal=personal,
        opening=opening, me_paragraph=me_paragraph, close=close,
        salutation=salutation, recipient_block=recipient_block,
    )

    with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8") as f:
        f.write(html)
        tmp = f.name
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox"],
                executable_path="/Users/lars/Library/Caches/ms-playwright/chromium-1243/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
            )
            page = browser.new_page()
            page.goto(f"file://{tmp}", wait_until="networkidle")
            page.pdf(path=cl_path, format="A4", print_background=True)
            browser.close()
        log.info("  CL generated (ATS-safe): %s", cl_path)
        return cl_path
    except Exception as e:
        log.error("  ATS CL generation failed: %s", e)
        return None
    finally:
        os.unlink(tmp)


def generate_documents(job, output_dir):
    """Generate CV and cover letter for a job."""
    variant = detect_variant(job.get("title", ""), job.get("description", ""))
    lang = detect_language(job.get("title", ""), job.get("company", ""), job.get("location", ""))
    tagline = CV_VARIANTS[variant][f"tagline_{lang}"]

    company_slug = re.sub(r"[^a-zA-Z0-9]", "_", job.get("company", "unknown"))[:30]
    job_dir = os.path.join(output_dir, company_slug)
    os.makedirs(job_dir, exist_ok=True)

    cv_path = os.path.join(job_dir, f"CV_{company_slug}.pdf")
    cl_path = os.path.join(job_dir, f"CL_{company_slug}.pdf")

    # If env vars contain placeholder strings ("xxx", "example", etc.) or
    # legacy values that disagree with JSON (Brussels vs Aarschot), strip
    # them so the JSON source-of-truth wins.
    def _is_placeholder(val: str) -> bool:
        v = val.lower()
        return any(p in v for p in ("xx xx", "xxx", "example", "your.", "brussels"))

    if _is_placeholder(os.getenv("CANDIDATE_LOCATION", "")):
        os.environ.pop("CANDIDATE_LOCATION", None)
    if _is_placeholder(os.getenv("CANDIDATE_PHONE", "")):
        os.environ.pop("CANDIDATE_PHONE", None)
    if _is_placeholder(os.getenv("CANDIDATE_NAME", "")):
        os.environ.pop("CANDIDATE_NAME", None)
    if _is_placeholder(os.getenv("CANDIDATE_EMAIL", "")):
        os.environ.pop("CANDIDATE_EMAIL", None)

    env_for_subprocess = os.environ.copy()

    # Generate CV
    cv_cmd = f'{CV_GENERATOR} --company "{job.get("company", "")}" --tagline "{tagline}" --language {lang} --output "{cv_path}"'
    try:
        subprocess.run(cv_cmd, shell=True, timeout=60, check=True, capture_output=True, env=env_for_subprocess)
        log.info("  CV generated: %s", cv_path)
    except Exception as e:
        log.error("  CV generation failed: %s", e)
        cv_path = None

    # Generate cover letter — opt-in ATS path (USE_ATS_TEMPLATES=1) uses our
    # templates + WHY-YOU YAML; otherwise fall back to dome317's subprocess CL.
    job_location = job.get("location", "") or ""
    cl_result = None
    if USE_ATS_TEMPLATES:
        cl_result = _generate_cover_letter_ats(job, cl_path, variant, lang, tagline)
    if not cl_result:
        # Pass --job-location so the letter shows Rostock/Berlin/Düsseldorf, NOT
        # the candidate's Aarschot address in the recipient block.
        cl_cmd = f'{CL_GENERATOR} --company "{job.get("company", "")}" --role "{job.get("title", "")}" --language {lang} --output "{cl_path}"'
        if job_location:
            # Escape any quotes in the location string
            loc_escaped = job_location.replace('"', '\\"')
            cl_cmd += f' --job-location "{loc_escaped}"'
        try:
            subprocess.run(cl_cmd, shell=True, timeout=60, check=True, capture_output=True, env=env_for_subprocess)
            log.info("  CL generated (dome317 fallback): %s", cl_path)
            cl_result = cl_path
        except Exception as e:
            log.error("  CL generation failed: %s", e)
            cl_result = None

    return {"cv_path": cv_path, "cl_path": cl_result, "variant": variant, "language": lang}


def main():
    parser = argparse.ArgumentParser(description="Daily batch pipeline")
    parser.add_argument("--db", default=DB_PATH, help="Path to SQLite database")
    parser.add_argument("--limit", type=int, default=50, help="Number of top jobs to process")
    parser.add_argument("--dry-run", action="store_true", help="Print jobs without generating docs")
    parser.add_argument("--min-score", type=int, default=20, help="Minimum score threshold")
    args = parser.parse_args()

    load_env()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Select top jobs not yet processed
    rows = conn.execute("""
        SELECT id, title, company, location, url, career_url, score, source, description
        FROM jobs
        WHERE score >= ? AND status NOT IN ('applied', 'filtered_out', 'batched', 'skipped')
        ORDER BY score DESC
        LIMIT ?
    """, (args.min_score, args.limit)).fetchall()

    log.info("Selected %d jobs (score >= %d)", len(rows), args.min_score)

    if args.dry_run:
        # ADOPT-14: dry-run also exercises career discovery so the count shows up.
        if CAREER_DISCOVERY_ENABLED and rows:
            try:
                from src.discovery.career_discovery import discover_career_for_jobs
                counts = discover_career_for_jobs(conn, max_jobs=min(len(rows), CAREER_DISCOVERY_MAX_JOBS))
                log.info("Career URLs discovered: %d (L1=%d L2=%d L3=%d)",
                         counts["total"], counts["layer1"], counts["layer2"], counts["layer3"])
            except Exception as e:
                log.warning("Career discovery skipped (dry-run): %s", e)
        for r in rows:
            cu = (r["career_url"] or "")[:60] if "career_url" in r.keys() else ""
            print(f"  {r['score']:4d} | {r['source']:12s} | {r['title'][:55]:55s} | {r['company'][:25]}")
            if cu:
                print(f"        └─ career: {cu}")
        conn.close()
        return

    if not rows:
        log.info("No jobs to process")
        conn.close()
        return

    # ADOPT-14: career page auto-discovery runs AFTER scoring (so we only probe
    # jobs worth applying to) and BEFORE doc generation (so the application
    # documents can link to the direct career page + ATS-aware resume).
    # Sets jobs.career_url + jobs.ats_type for ADOPT-12 (CUA submit) to consume.
    career_counts = None
    if CAREER_DISCOVERY_ENABLED:
        try:
            from src.discovery.career_discovery import discover_career_for_jobs
            career_counts = discover_career_for_jobs(
                conn, max_jobs=min(len(rows), CAREER_DISCOVERY_MAX_JOBS)
            )
            log.info("Career URLs discovered: %d (L1=%d L2=%d L3=%d)",
                     career_counts["total"], career_counts["layer1"],
                     career_counts["layer2"], career_counts["layer3"])
            # Refresh the row dicts so the doc-generation step sees the new URLs.
            if career_counts["total"] > 0:
                rows = conn.execute("""
                    SELECT id, title, company, location, url, career_url, score, source, description
                    FROM jobs
                    WHERE score >= ? AND status NOT IN ('applied', 'filtered_out', 'batched', 'skipped')
                    ORDER BY score DESC
                    LIMIT ?
                """, (args.min_score, args.limit)).fetchall()
        except Exception as e:
            log.warning("Career discovery failed (continuing without): %s", e)

    # Create batch directory
    batch_date = datetime.now().strftime("%Y-%m-%d")
    batch_dir = os.path.join(BATCHES_DIR, batch_date)
    os.makedirs(batch_dir, exist_ok=True)

    # Generate documents for each job
    results = []
    for row in rows:
        log.info("Processing: %s at %s (score: %d)", row["title"][:50], row["company"][:25], row["score"])
        docs = generate_documents(dict(row), batch_dir)
        results.append({"job": dict(row), "docs": docs})

        # Mark as batched
        conn.execute("UPDATE jobs SET status = 'batched' WHERE id = ?", (row["id"],))

    conn.commit()

    # Create ZIP
    zip_path = shutil.make_archive(batch_dir, "zip", batch_dir)
    log.info("ZIP created: %s", zip_path)

    # Create overview
    overview_path = os.path.join(batch_dir, "OVERVIEW.md")
    with open(overview_path, "w", encoding="utf-8") as f:
        f.write(f"# Batch {batch_date}\n\n")
        f.write(f"**Jobs:** {len(results)}\n\n")
        for i, r in enumerate(results, 1):
            job = r["job"]
            f.write(f"## {i}. {job.get('title', 'N/A')}\n")
            f.write(f"- **Company:** {job.get('company', 'N/A')}\n")
            f.write(f"- **Score:** {job.get('score', 0)}\n")
            f.write(f"- **URL:** {job.get('url', 'N/A')}\n")
            f.write(f"- **Variant:** {r['docs']['variant']}\n\n")

    # Send via Telegram
    summary = f"Batch {batch_date}: {len(results)} jobs processed"
    send_telegram(summary, zip_path)

    conn.close()
    log.info("Batch pipeline complete: %d jobs", len(results))


if __name__ == "__main__":
    main()
