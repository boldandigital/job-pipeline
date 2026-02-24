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
    """Detect job language (de/en) from title and location."""
    german_indicators = [
        "referent", "sachbearbeiter", "koordinator", "berater",
        "leiter", "mitarbeiter", "fachkraft", "beauftragter",
        "deutschland", "germany", "hannover", "berlin", "muenchen",
    ]
    text = f"{title} {company} {location}".lower()
    for indicator in german_indicators:
        if indicator in text:
            return "de"
    return "en"


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

    # Generate CV
    cv_cmd = f'{CV_GENERATOR} --company "{job.get("company", "")}" --tagline "{tagline}" --language {lang} --output "{cv_path}"'
    try:
        subprocess.run(cv_cmd, shell=True, timeout=60, check=True, capture_output=True)
        log.info("  CV generated: %s", cv_path)
    except Exception as e:
        log.error("  CV generation failed: %s", e)
        cv_path = None

    return {"cv_path": cv_path, "cl_path": None, "variant": variant, "language": lang}


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
        for r in rows:
            print(f"  {r['score']:4d} | {r['source']:12s} | {r['title'][:55]:55s} | {r['company'][:25]}")
        conn.close()
        return

    if not rows:
        log.info("No jobs to process")
        conn.close()
        return

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
