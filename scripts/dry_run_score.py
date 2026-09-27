#!/usr/bin/env python3
"""Dry-run proof harness for dome317 keyword scoring weights.

Two-mode comparison:
  --baseline  → uses dome317's shipped config/example.yaml
  --lars      → uses config/lars.yaml (this card's tuned config)

Inputs: synthetic job corpus designed to mirror the kind of roles Lars should
or shouldn't see at the top of his stack. No DB, no scraping, no LLM call.

Per ADOPT-4 constraints: READ-ONLY on production. No scraping. No scoring of
real scraped data until ADOPT-5.

Usage:
    python scripts/dry_run_score.py --lars
    python scripts/dry_run_score.py --baseline
    python scripts/dry_run_score.py --compare    # both, side by side
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Repo paths — runnable from any cwd
SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE = SCRIPT_DIR.parent
sys.path.insert(0, str(WORKSPACE))

from src.scoring.score_jobs import load_config, score_job  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# SYNTHETIC JOB CORPUS
# ─────────────────────────────────────────────────────────────────────────────
# 12 jobs. Each is a hand-written title+description matching a role archetype
# that Lars cares (or doesn't care) about. Real job-board titles and prose,
# just fabricated for tuning purposes.
JOBS = [
    # ─────────────────────────────────────────────────────────────────────────
    # GROUP A — TARGET MATCHES (Lars's ideal roles)
    # ─────────────────────────────────────────────────────────────────────────
    {
        "id": "A1",
        "title": "Founder / Co-Founder — B2B SaaS hosting platform",
        "company": "Stealth Mode Startup",
        "location": "Berlin, Germany",
        "description": (
            "Early-stage B2B SaaS startup looking for a co-founder with deep "
            "experience in managed hosting, devops, and agency partnerships. "
            "Founder-level equity package. Remote-first in CET timezone, "
            "occasional travel to Berlin. Work with managed WordPress, "
            "WooCommerce and custom stack deployments. 50k EUR base + equity. "
            "German and English required."
        ),
        "verdict": "TARGET MATCH",
    },
    {
        "id": "A2",
        "title": "Head of Digital — International Brand Agency",
        "company": "Brandmacher GmbH",
        "location": "Munich, Germany",
        "description": (
            "Leading DACH brand agency (clients: Audi, Porsche, Otto Group) "
            "is hiring a Head of Digital to own the web/cloud/devops practice. "
            "Salary €110-140k. Manage a team of 8-12 engineers across two "
            "locations. Salesforce.com integration project kicks off Q1. "
            "Remote-friendly (2 days office in Munich). German and English fluent."
        ),
        "verdict": "TARGET MATCH",
    },
    {
        "id": "A3",
        "title": "CTO — Managed Cloud Hosting (scale-up)",
        "company": "CloudPilot B.V.",
        "location": "Amsterdam, Netherlands",
        "description": (
            "CloudPilot (Series A, 35 employees) provides managed cloud "
            "hosting for European e-commerce. We are looking for a CTO with "
            "experience in devops, cloud infrastructure, and team leadership. "
            "Lead a team of 8 engineers. Remote-first across Benelux/DACH. "
            "Salary €120-150k + ESOP. Strong opinion on Core Web Vitals a plus."
        ),
        "verdict": "TARGET MATCH",
    },
    {
        "id": "A4",
        "title": "VP Engineering — Digital Marketplace Platform",
        "company": "Marktplatz AG",
        "location": "Hamburg, Germany",
        "description": (
            "Series B marketplace platform (300+ employees). Looking for a "
            "VP Engineering to scale the engineering org from 30 to 80 across "
            "4 product lines. Fully remote across EU timezone. 4-day workweek. "
            "Salary €140-180k + stock options. Hands-on with cloud architecture, "
            "devops, agency partner integrations."
        ),
        "verdict": "TARGET MATCH",
    },

    # ─────────────────────────────────────────────────────────────────────────
    # GROUP B — STRETCH MATCHES (worth surfacing, lower confidence)
    # ─────────────────────────────────────────────────────────────────────────
    {
        "id": "B1",
        "title": "Director of Agency Partnerships — Web Hosting Co.",
        "company": "NetSpace SARL",
        "location": "Brussels, Belgium",
        "description": (
            "Web hosting company with 50+ agency partners across DACH and "
            "Benelux. We need a Director of Agency Partnerships to grow the "
            "partner program 2x in 18 months. Hybrid role, 2 days in Brussels. "
            "Salary €95-115k + bonus. B2B sales-heavy with technical depth on "
            "managed WordPress, WooCommerce, devops tooling."
        ),
        "verdict": "STRETCH MATCH",
    },
    {
        "id": "B2",
        "title": "Senior Manager — Digital Transformation",
        "company": "Bankhaus Müller AG",
        "location": "Vienna, Austria",
        "description": (
            "Austrian private bank hiring a Senior Manager Digital "
            "Transformation to drive a 3-year salesforce.com + cloud migration "
            "across 18 branches. Manage a cross-functional team of 6. Hybrid "
            "Vienna, flexible hours. Salary €90-110k. German required."
        ),
        "verdict": "STRETCH MATCH",
    },
    {
        "id": "B3",
        "title": "Teamlead DevOps — E-Commerce Platform",
        "company": "Shoply GmbH",
        "location": "Remote, Europe",
        "description": (
            "E-commerce platform serving 5,000+ DACH retailers. Fully remote "
            "across Europe. Looking for a Teamlead DevOps to lead a team of "
            "6 across infrastructure, CI/CD, and Core Web Vitals. Salary "
            "€85-105k + equity. Hands-on + leadership split (60/40)."
        ),
        "verdict": "STRETCH MATCH",
    },

    # ─────────────────────────────────────────────────────────────────────────
    # GROUP C — DEAL-BREAKER MATCHES (should score LOW or be filtered out)
    # ─────────────────────────────────────────────────────────────────────────
    {
        "id": "C1",
        "title": "Junior AI Operations Specialist — Entry Level",
        "company": "AI Eval Co.",
        "location": "Berlin, Germany",
        "description": (
            "Entry-level role for fresh graduates. Assist with AI training, "
            "data labeling, quality evaluations. No management. Werkstudent "
            "experience welcome. Salary €38k. 4-day week, fully remote."
        ),
        "verdict": "DEAL BREAKER",
    },
    {
        "id": "C2",
        "title": "Werkstudent IT-Support (m/w/d)",
        "company": "Mittelstand Tech",
        "location": "Hamburg, Germany",
        "description": (
            "Werkstudent role (working student, 20h/week). IT support, "
            "first-level helpdesk. €14/hour. Remote possible within DE."
        ),
        "verdict": "DEAL BREAKER",
    },
    {
        "id": "C3",
        "title": "Senior Software Engineer (IC) — Backend",
        "company": "PureCode Inc",
        "location": "San Francisco, USA",
        "description": (
            "Individual contributor senior backend engineer. 100% coding, no "
            "management. Stock options. San Francisco on-site required. "
            "Relocation assistance provided. Salary USD 180k."
        ),
        "verdict": "DEAL BREAKER",
    },
    {
        "id": "C4",
        "title": "Account Executive — Outbound SaaS Sales",
        "company": "SalesCo GmbH",
        "location": "Munich, Germany",
        "description": (
            "Outbound SaaS sales role. Cold calling, quota carrying, "
            "commission based. €55k base + uncapped commission. 75% travel "
            "DACH. Fast-paced environment."
        ),
        "verdict": "DEAL BREAKER",
    },
    {
        "id": "C5",
        "title": "Praktikum Marketing (Pflicht-Praktikum)",
        "company": "Berlin Startup",
        "location": "Berlin, Germany",
        "description": (
            "Mandatory internship (Pflichtpraktikum) for university students. "
            "6 months, €600/month. Marketing support, social media, events. "
            "Berlin office only."
        ),
        "verdict": "DEAL BREAKER",
    },
    {
        "id": "C6",
        "title": "Director of Product (On-site, San Francisco only)",
        "company": "US BayArea Startup",
        "location": "San Francisco, USA",
        "description": (
            "Director of Product role at a 60-person Series B startup. "
            "On-site required in San Francisco, relocation assistance provided. "
            "Salary USD 220k. Manage a team of 8 PMs. No remote option."
        ),
        "verdict": "DEAL BREAKER",  # US-only + on-site + relocation
    },
    {
        "id": "A5",
        "title": "Digitalagentur Geschäftsführer (m/w/d)",
        "company": "PixelWerk GmbH",
        "location": "Vienna, Austria",
        "description": (
            "Etabliertes Design- und Branding-Studio in Wien sucht einen "
            "Geschäftsführer zur Mitgestaltung der nächsten Wachstumsphase. "
            "20 Mitarbeiter, Klienten u.a. aus DACH-Mittelstand. Verantwortung "
            "für Strategie, Branding, Pricing und Cloud-Lösungen. Remote möglich "
            "(2 Tage Office Wien), Gehalt €95-115k. Deutsch erforderlich."
        ),
        "verdict": "TARGET MATCH",  # Geschäftsführer at digital agency in DACH
    },
]


def _resolve(p: str) -> Path:
    pp = Path(p)
    if not pp.is_absolute():
        pp = WORKSPACE / p
    return pp


def _load_scoring(p: Path):
    cfg = load_config(str(p))
    scoring = cfg.get("scoring", {})
    weights = scoring.get("weights", {})
    keywords = scoring.get("keywords", {})
    threshold = scoring.get("threshold", 20)
    return weights, keywords, threshold


def _score_one(job, weights, keywords, threshold):
    score = score_job(job["title"], job["description"], keywords, weights)
    tier = "PREMIUM" if score >= 150 else "STANDARD" if score >= 80 else "LOW" if score >= threshold else "FILTERED"
    return score, tier


def _render_table(rows: list[dict], title: str, threshold: int) -> str:
    lines = []
    lines.append("")
    lines.append("=" * 90)
    lines.append(f"  {title}")
    lines.append("=" * 90)
    lines.append(f"  {'ID':<4} {'SCORE':>6}  {'TIER':<10}  {'VERDICT':<18}  TITLE")
    lines.append("-" * 90)
    # Sort by score desc, group verdict so target matches rise
    rows_sorted = sorted(rows, key=lambda r: r["score"], reverse=True)
    for r in rows_sorted:
        verdict_short = r["verdict"].replace(" TARGET MATCH", "*").replace(" STRETCH MATCH", "+").replace(" DEAL BREAKER", "X")
        lines.append(
            f"  {r['id']:<4} {r['score']:>6}  {r['tier']:<10}  {verdict_short:<18}  {r['title'][:50]}"
        )
    lines.append("-" * 90)
    targeted = [r for r in rows_sorted if r["verdict"] == "TARGET MATCH"]
    stretch = [r for r in rows_sorted if r["verdict"] == "STRETCH MATCH"]
    dealbrk = [r for r in rows_sorted if r["verdict"] == "DEAL BREAKER"]

    def rank_score(rows):
        ranks = {r["id"]: i + 1 for i, r in enumerate(rows_sorted)}
        return [ranks[r["id"]] for r in rows]

    target_ranks = rank_score(targeted)
    stretch_ranks = rank_score(stretch)
    deal_ranks = rank_score(dealbrk)

    lines.append("")
    lines.append(f"  TARGET MATCHES ({len(targeted)}):  ranks = {target_ranks}  (lower=better, want 1-{len(targeted)})")
    lines.append(f"  STRETCH MATCHES ({len(stretch)}):  ranks = {stretch_ranks}  (lower=better, want 1-{len(targeted)+len(stretch)})")
    lines.append(f"  DEAL BREAKERS  ({len(dealbrk)}):  ranks = {deal_ranks}  (HIGHER = better — we want them at the BOTTOM)")
    lines.append("")
    lines.append(f"  Threshold = {threshold}  |  Premium≥150  Standard≥80  Low≥{threshold}  Filtered<{threshold}")
    lines.append("=" * 90)
    return "\n".join(lines)


def run(label: str, cfg_path: str) -> tuple[list[dict], int]:
    weights, keywords, threshold = _load_scoring(_resolve(cfg_path))
    rows = []
    for job in JOBS:
        score, tier = _score_one(job, weights, keywords, threshold)
        rows.append({
            **job,
            "score": score,
            "tier": tier,
            "config": label,
        })
    return rows, threshold


def compare():
    baseline_rows, baseline_threshold = run("BASELINE (dome317 defaults)", "config/example.yaml")
    lars_rows, lars_threshold = run("LARS TUNED (ADOPT-4)", "config/lars.yaml")

    print(_render_table(baseline_rows, "BASELINE — dome317/config/example.yaml (entry-level AI persona)", baseline_threshold))
    print(_render_table(lars_rows, "LARS TUNED — config/lars.yaml (Founder/CTO/Head-of-Digital, DACH+Benelux)", lars_threshold))

    # ── Quantitative delta ────────────────────────────────────────────────
    by_id_b = {r["id"]: r for r in baseline_rows}
    by_id_l = {r["id"]: r for r in lars_rows}
    target_delta = []
    deal_delta = []
    for job in JOBS:
        b = by_id_b[job["id"]]
        l = by_id_l[job["id"]]
        delta = l["score"] - b["score"]
        if job["verdict"] == "TARGET MATCH":
            target_delta.append((job["id"], delta, b["score"], l["score"]))
        elif job["verdict"] == "DEAL BREAKER":
            deal_delta.append((job["id"], delta, b["score"], l["score"]))

    print()
    print("=" * 90)
    print("  DELTAS — how much Lars tuning moved each archetype")
    print("=" * 90)
    print()
    print("  TARGET MATCHES (we want these UP):")
    print(f"  {'ID':<4}  {'baseline':>9}  {'lars':>9}  {'Δ':>7}  TITLE")
    for jid, d, b, l in sorted(target_delta, key=lambda x: -x[1]):
        title = next(j["title"] for j in JOBS if j["id"] == jid)
        print(f"  {jid:<4}  {b:>9}  {l:>9}  {d:+7}  {title[:50]}")
    print()
    print("  DEAL BREAKERS (we want these DOWN / pushed below threshold):")
    print(f"  {'ID':<4}  {'baseline':>9}  {'lars':>9}  {'Δ':>7}  TITLE")
    for jid, d, b, l in sorted(deal_delta, key=lambda x: x[1]):
        title = next(j["title"] for j in JOBS if j["id"] == jid)
        print(f"  {jid:<4}  {b:>9}  {l:>9}  {d:+7}  {title[:50]}")

    # ── Pass/fail gates ──────────────────────────────────────────────────
    print()
    print("=" * 90)
    print("  PASS / FAIL GATES — must all check True for ADOPT-4 acceptance")
    print("=" * 90)

    gates = []
    # Gate 1: every target match improves (Δ > 0)
    gate1 = all(d > 0 for _, d, _, _ in target_delta)
    gates.append(("G1: all TARGET MATCHES score higher in lars.yaml",
                  gate1,
                  [f"{jid}: Δ={d:+d}" for jid, d, b, l in target_delta]))

    # Gate 2: every deal-breaker scores below threshold in lars.yaml
    gate2 = all(l < lars_threshold for jid, _, _, l in deal_delta)
    gates.append((f"G2: all DEAL BREAKERS score below threshold (<{lars_threshold}) in lars.yaml",
                  gate2,
                  [f"{jid}: lars={l}" for jid, _, _, l in deal_delta]))

    # Gate 3: at least 3 of 4 target matches land in Premium tier (≥150)
    target_tier_lars = [by_id_l[job["id"]]["tier"] for job in JOBS if job["verdict"] == "TARGET MATCH"]
    gate3 = sum(1 for t in target_tier_lars if t == "PREMIUM") >= 3
    gates.append(("G3: ≥3 of 4 TARGET MATCHES hit PREMIUM tier (≥150) in lars.yaml",
                  gate3,
                  [f"{by_id_l[job['id']]['id']}: {by_id_l[job['id']]['tier']} ({by_id_l[job['id']]['score']})"
                   for job in JOBS if job["verdict"] == "TARGET MATCH"]))

    # Gate 4: no deal-breaker in baseline is preferred-over any target match in lars
    deal_scores_l = {jid: by_id_l[jid]["score"] for jid, *_ in deal_delta}
    target_scores_l = [by_id_l[job["id"]]["score"] for job in JOBS if job["verdict"] == "TARGET MATCH"]
    gate4 = (min(target_scores_l) > max(deal_scores_l.values()))
    gates.append(("G4: lowest-scoring TARGET MATCH still outranks highest-scoring DEAL BREAKER in lars.yaml",
                  gate4,
                  [f"min(target)={min(target_scores_l)} vs max(deal)={max(deal_scores_l.values())}"]))

    all_pass = True
    for name, passed, evidence in gates:
        flag = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  [{flag}] {name}")
        for e in evidence:
            print(f"           - {e}")
    print()
    print(f"  OVERALL: {'PASS — all gates green' if all_pass else 'FAIL — see above'}")
    print("=" * 90)
    return 0 if all_pass else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", action="store_true", help="Score with dome317's default config")
    ap.add_argument("--lars", action="store_true", help="Score with Lars-tuned config")
    ap.add_argument("--compare", action="store_true", help="Side-by-side vs dome317 defaults")
    args = ap.parse_args()

    if not (args.baseline or args.lars or args.compare):
        args.compare = True

    if args.compare:
        return compare()

    if args.lars:
        rows, threshold = run("LARS TUNED", "config/lars.yaml")
        print(_render_table(rows, "LARS TUNED — config/lars.yaml", threshold))
        return 0

    if args.baseline:
        rows, threshold = run("BASELINE", "config/example.yaml")
        print(_render_table(rows, "BASELINE — dome317/config/example.yaml", threshold))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
