#!/usr/bin/env python3
"""
Two-layer job filter for the automated job search pipeline.

Layer 1: Hard title blocks (seniority, contract type, level)
Layer 2: Profile relevance gate (must match >= 1 competency cluster)

Jobs that fail either layer get status='filtered_out' and won't be auto-applied.

Usage:
    python -m src.scoring.apply_filter
    python -m src.scoring.apply_filter --db ./data/jobs.db
"""

import argparse
import os
import re
import sqlite3
import sys

DB_PATH = os.getenv("DB_PATH", "./data/jobs.db")

# ============================================================
# LAYER 1: HARD TITLE BLOCKS
# ============================================================

SENIORITY_BLOCK = [
    r'\bsenior\b',
    r'\blead\b',
    r'\bprincipal\b',
    r'\bstaff\b',
    r'\bhead of\b',
    r'\bdirector\b',
    r'\bleiter\b',
    r'\bleitung\b',
    r'\bteamlead\b',
    r'\bteam lead\b',
    r'\bvp\b',
    r'\bvice president\b',
    r'\bgeschaeftsfuehrer\b',
    r'\bvorstand\b',
    r'\bc-level\b',
    r'\bceo\b',
    r'\bcto\b',
    r'\bcfo\b',
    r'\bcoo\b',
    r'\barchitect\b',
    r'\b7\+\s*year',
    r'\b8\+\s*year',
    r'\b10\+\s*year',
    r'\b7\+\s*jahr',
    r'\b10\+\s*jahr',
]

# "(Senior)" or "(Senior/Junior)" in parens = flexible posting, OK
SENIORITY_EXCEPTION = re.compile(r'\((?:senior|lead|sr\.?)\)', re.IGNORECASE)

CONTRACT_BLOCK = [
    r'\bfreelance\b',
    r'\bfreelancer\b',
    r'\bcontractor\b',
    r'\bself[- ]?employed\b',
    r'\bbefristet\b',
    r'\bphd\b',
    r'\bpromotion\b',
    r'\bmaster thesis\b',
    r'\bbachelor thesis\b',
    r'\babschlussarbeit\b',
]

JUNIOR_BLOCK = [
    r'\bwerkstudent\b',
    r'\bworking student\b',
    r'\bwerkstudium\b',
    r'\bpraktikum\b',
    r'\bpraktikant\b',
    r'\binternship\b',
    r'\bintern\b',
    r'\btrainee\b',
    r'\bausbildung\b',
    r'\bazubi\b',
]


def check_hard_blocks(title_lower):
    """Return (blocked: bool, reason: str)."""
    has_exception = bool(SENIORITY_EXCEPTION.search(title_lower))
    if not has_exception:
        for pattern in SENIORITY_BLOCK:
            if re.search(pattern, title_lower):
                return True, "seniority"

    for pattern in CONTRACT_BLOCK:
        if re.search(pattern, title_lower):
            return True, "contract"

    for pattern in JUNIOR_BLOCK:
        if re.search(pattern, title_lower):
            return True, "junior"

    return False, ""


# ============================================================
# LAYER 2: PROFILE RELEVANCE (Positive Matching)
# ============================================================

COMPETENCY_CLUSTERS = {
    "ai_ml_data": [
        r'\bai\b', r'\bki\b', r'\bml\b', r'\bllm\b', r'\bnlp\b',
        r'\bgenai\b', r'\bgen ai\b', r'\bgenerative ai\b',
        r'\bprompt\b', r'\bchatbot\b', r'\bdata scien',
        r'\bmachine learning\b', r'\bartificial intellig',
        r'\bai trainer\b', r'\bai evaluator\b', r'\bai quality\b',
        r'\bai specialist\b', r'\bai analyst\b', r'\bai engineer\b',
        r'\bai consultant\b', r'\bai strateg\b', r'\bai governance\b',
        r'\bai compliance\b', r'\bai ethics\b', r'\bresponsible ai\b',
        r'\brag\b', r'\bembedding', r'\bvector\b',
        r'\bconversational ai\b', r'\bvoice assistant\b',
    ],
    "automation_rpa": [
        r'\bautomat', r'\brpa\b', r'\brobotic process\b',
        r'\bn8n\b', r'\bzapier\b', r'\bmake\.com\b',
        r'\bworkflow\s*automat', r'\bprocess\s*automat',
        r'\blow[- ]code\b', r'\bno[- ]code\b',
        r'\bintegration\s*specialist\b', r'\bpower automate\b',
        r'\buipath\b', r'\bblue prism\b',
        r'\bintelligent automation\b',
        r'\bprozessautomatisierung\b',
        r'\bpower platform\b',
        r'\bpower apps\b',
    ],
    "business_analysis": [
        r'\bbusiness analyst\b', r'\bit business analyst\b',
        r'\bprocess analyst\b', r'\bprozess\s*analyst\b',
        r'\brequirements\s*engineer\b', r'\banforderungsmanag',
        r'\bproduct analyst\b', r'\bproduct manager\b', r'\bproduct owner\b',
        r'\bbusiness process\b', r'\bdigital transformation\b',
        r'\bdigitale transformation\b', r'\bscrum master\b',
        r'\bagile\b', r'\bprojektmanag', r'\bproject manag',
        r'\bproduct management\b', r'\bproduct builder\b',
    ],
    "data_analytics_bi": [
        r'\bdata analyst\b', r'\bdatenanalyst\b',
        r'\banalytics\b', r'\breporting\b', r'\bberichts',
        r'\bbi analyst\b', r'\bbusiness intelligence\b',
        r'\bdashboard\b', r'\btableau\b', r'\bpower bi\b',
        r'\breport analyst\b', r'\binsight analyst\b',
        r'\bdata\s*operations\b', r'\bdata\s*quality\b',
        r'\bdata\s*platform\b',
    ],
    "it_digital": [
        r'\bit[- ]projekt', r'\bit[- ]koordinat',
        r'\bdigitalisierung\b', r'\bdigital officer\b',
        r'\bsystem analyst\b', r'\bapplication analyst\b',
        r'\bwirtschaftsinformatik', r'\binhouse consultant\b',
        r'\bit berater\b',
        r'\breferent\s*(it|digitalisierung|ki)',
        r'\bdigital\s*process', r'\bprozessdigitalisierung\b',
        r'\bprozessoptimier', r'\bprozessmanag',
        r'\bfachkoordinator\b', r'\bprojektkoordinator\b',
        r'\bkonfiguration', r'\bconfiguration\b',
        r'\bimplementation\b', r'\bonboarding\b',
        r'\bdeployment\b',
        r'\bsolution\s*(specialist|consultant)\b',
        r'\bit modernisierung\b',
        r'\bsachbearbeiter\s*digitalisierung\b',
        r'\bdigital\s*product\b',
        r'\bsystems\s*engineer\b',
    ],
    "operations": [
        r'\boperations\s*(analyst|specialist|manager|coordinator)\b',
        r'\brevops\b', r'\brevenue operations\b',
        r'\bsales\s*operations\b', r'\bsales\s*ops\b',
        r'\bmarketing\s*operations\b', r'\bmarketing\s*ops\b',
        r'\bgtm\s*operations\b', r'\bcommercial\s*operations\b',
        r'\bdeal\s*desk\b', r'\bcrm\b', r'\bhubspot\b', r'\bsalesforce\b',
        r'\boperational excellence\b',
        r'\bcustomer\s*operations\b',
        r'\bmarketing\s*analyst\b',
        r'\bdemand\s*generation\b',
    ],
    "compliance_regtech": [
        r'\bcompliance\s*(analyst|specialist|officer)\b',
        r'\bkyc\b', r'\baml\b', r'\bregtech\b',
        r'\bfraud\s*(analyst|specialist)\b',
        r'\btransaction monitoring\b', r'\brisk\s*analyst\b',
        r'\bdata governance\b', r'\blegal\s*operations\b',
        r'\bcrypto\s*compliance\b', r'\bdigital assets\b',
    ],
    "content_docs": [
        r'\btechnical writ', r'\btechnischer redakteur\b',
        r'\bdocumentation\s*(specialist|manager)\b',
        r'\bknowledge\s*(manager|management|base)\b',
        r'\bux writer\b', r'\bcontent\s*operat',
        r'\binformation architect\b',
        r'\bproduct documentation\b',
    ],
    "customer_success": [
        r'\bcustomer success\b', r'\bclient success\b',
        r'\bcustomer experience\b', r'\bpartner success\b',
        r'\bcustomer enablement\b', r'\brenewal manager\b',
        r'\btechnical account manager\b',
    ],
    "enablement_training": [
        r'\benablement\b', r'\btechnical trainer\b',
        r'\bproduct trainer\b',
        r'\blearning\s*(development|specialist)\b',
        r'\btraining\s*(coordinator|specialist)\b',
    ],
    "qa_testing": [
        r'\bqa\s*analyst\b', r'\bquality\s*analyst\b',
        r'\btest\s*analyst\b', r'\buat\b',
    ],
    "fintech_web3": [
        r'\bfintech\b', r'\bblockchain\b', r'\bweb3\b',
        r'\bcrypto(?!.*compliance)\b', r'\bdefi\b',
    ],
    "ecommerce": [
        r'\be[- ]commerce\b', r'\bonline\s*(handel|shop)\b',
        r'\bshopify\b', r'\bretail\s*tech\b',
    ],
}

# Title patterns that are ALWAYS irrelevant
ALWAYS_IRRELEVANT = [
    r'\bsteuerberater\b', r'\bsteuerfachangestellt',
    r'\bbuchhalter\b', r'\bbuchhaltung\b', r'\baccountant\b',
    r'\bbookkeeper\b', r'\bpayroll\b',
    r'\bproperty\s*manager\b', r'\bimmobilien',
    r'\bmechatroniker\b', r'\belektroniker\b', r'\bmonteur\b',
    r'\bpflegefach', r'\bkrankenpfleg', r'\barzt\b', r'\bmedizin',
    r'\bhausmeister\b', r'\breinigung',
    r'\blagerarbeiter\b', r'\bkommissionier',
    r'\bfahrer\b', r'\bdriver\b',
    r'\bkoch\b', r'\bkellner\b', r'\bgastronomie\b',
    r'\berzieher\b', r'\blehrer\b',
    r'\brechtsanwalt\b', r'\bjurist\b', r'\bnotar\b',
    r'\bgraphic\b', r'\billustrat',
    r'\bsocial\s*media\s*manager\b',
    r'\bhr\s*(manager|business\s*partner|generalist)\b',
    r'\brecruiter\b', r'\btalent\s*acquisition\b',
    r'\baccount\s*executive\b', r'\bsales\s*rep\b',
    r'\bfinancial\s*modell', r'\bfinance\s*associate\b',
    r'\btax\s*(associate|manager|specialist|advisor|berater)\b',
    r'\bkey\s*account\s*manager\b',
    r'\bfield\s*marketing\b',
    r'\bsap\s*(key\s*user|berater|consultant|develop|engineer|architect)\b',
    r'\bscooter\b', r'\bbus\s*driver\b',
    r'\bgeophysicist\b', r'\bgeolog',
    r'\bmission\s*design\s*engineer\b',
    r'\bsustainability\b',
    r'\bpeople\s*(and|&)\s*administ',
    r'\bentrepreneur\s*in\s*residence\b',
    r'\bsolutions\s*engineer\b',
    r'\bservicenow\s*developer\b',
    r'\bassistenz\s*(abrechnung|verwaltung|sekretariat)\b',
    r'\bsachbearbeitung\s*buchhaltung\b',
    r'\bchief\s*executive\b',
]

LANGUAGE_BLOCK = [
    r'\bfrench\s*(required|native|fluent|mandatory)\b',
    r'\bspanish\s*(required|native|fluent|mandatory)\b',
    r'\bmandarin\s*(required|native|fluent)\b',
    r'\bjapanese\s*(required|native|fluent)\b',
    r'\bitalian\s*(required|native|fluent)\b',
    r'\bportuguese\s*(required|native|fluent)\b',
    r'\barabic\s*(required|native|fluent)\b',
    r'\bdutch\s*(required|native|fluent)\b',
]


def check_profile_relevance(title_lower):
    """Check if job matches at least one competency cluster."""
    for pattern in ALWAYS_IRRELEVANT:
        if re.search(pattern, title_lower):
            return False, "irrelevant_role"

    for pattern in LANGUAGE_BLOCK:
        if re.search(pattern, title_lower):
            return False, "language_mismatch"

    matched = []
    for cluster_name, patterns in COMPETENCY_CLUSTERS.items():
        for pattern in patterns:
            if re.search(pattern, title_lower):
                matched.append(cluster_name)
                break

    if matched:
        return True, ",".join(matched)

    return False, "no_cluster_match"


def main():
    parser = argparse.ArgumentParser(description="Two-layer job filter")
    parser.add_argument("--db", default=os.getenv("DB_PATH", "./data/jobs.db"),
                        help="Path to SQLite database")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, title, company, location, score, source, status FROM jobs WHERE score >= 20"
    ).fetchall()

    print(f"[INFO] Checking {len(rows)} jobs with score >= 20")

    stats = {
        "eligible": 0,
        "blocked_seniority": 0,
        "blocked_contract": 0,
        "blocked_junior": 0,
        "blocked_irrelevant": 0,
        "blocked_language": 0,
        "blocked_no_cluster": 0,
    }

    blocked_examples = {"seniority": [], "irrelevant": [], "no_cluster": []}
    eligible_jobs = []

    for row in rows:
        title_lower = (row["title"] or "").lower()

        blocked, reason = check_hard_blocks(title_lower)
        if blocked:
            stats[f"blocked_{reason}"] += 1
            if reason == "seniority" and len(blocked_examples["seniority"]) < 8:
                blocked_examples["seniority"].append(
                    f"  {row['score']:4d} | {row['title'][:60]}"
                )
            conn.execute(
                "UPDATE jobs SET status = 'filtered_out' WHERE id = ?", (row["id"],)
            )
            continue

        relevant, info = check_profile_relevance(title_lower)
        if not relevant:
            if info == "irrelevant_role":
                stats["blocked_irrelevant"] += 1
                if len(blocked_examples["irrelevant"]) < 10:
                    blocked_examples["irrelevant"].append(
                        f"  {row['score']:4d} | {row['title'][:60]}"
                    )
            elif info == "language_mismatch":
                stats["blocked_language"] += 1
            else:
                stats["blocked_no_cluster"] += 1
                if len(blocked_examples["no_cluster"]) < 15:
                    blocked_examples["no_cluster"].append(
                        f"  {row['score']:4d} | {row['title'][:60]}"
                    )
            conn.execute(
                "UPDATE jobs SET status = 'filtered_out' WHERE id = ?", (row["id"],)
            )
            continue

        stats["eligible"] += 1
        eligible_jobs.append(row)
        if row["status"] == "filtered_out":
            conn.execute(
                "UPDATE jobs SET status = 'new' WHERE id = ?", (row["id"],)
            )

    conn.commit()

    total_blocked = sum(v for k, v in stats.items() if k.startswith("blocked_"))
    print(f"\n{'='*70}")
    print(f"FILTER RESULTS")
    print(f"{'='*70}")
    print(f"  Total checked:        {len(rows)}")
    print(f"  ELIGIBLE for apply:   {stats['eligible']}")
    print(f"  BLOCKED total:        {total_blocked}")
    print(f"    - Seniority/Lead:   {stats['blocked_seniority']}")
    print(f"    - Contract type:    {stats['blocked_contract']}")
    print(f"    - Too junior:       {stats['blocked_junior']}")
    print(f"    - Irrelevant role:  {stats['blocked_irrelevant']}")
    print(f"    - Language:         {stats['blocked_language']}")
    print(f"    - No cluster match: {stats['blocked_no_cluster']}")

    if blocked_examples["seniority"]:
        print(f"\n[BLOCKED: Seniority/Lead examples]")
        for ex in blocked_examples["seniority"]:
            print(ex)

    if blocked_examples["irrelevant"]:
        print(f"\n[BLOCKED: Irrelevant role examples]")
        for ex in blocked_examples["irrelevant"]:
            print(ex)

    if blocked_examples["no_cluster"]:
        print(f"\n[BLOCKED: No cluster match - REVIEW THESE]")
        for ex in blocked_examples["no_cluster"]:
            print(ex)

    eligible_sorted = sorted(eligible_jobs, key=lambda r: r["score"], reverse=True)
    print(f"\n[TOP 30 ELIGIBLE JOBS]")
    for r in eligible_sorted[:30]:
        print(
            f"  {r['score']:4d} | {r['source']:10s} | {r['title'][:55]:55s} | {r['company'][:25]}"
        )

    premium = sum(1 for r in eligible_sorted if r["score"] >= 150)
    standard = sum(1 for r in eligible_sorted if 80 <= r["score"] < 150)
    low = sum(1 for r in eligible_sorted if 20 <= r["score"] < 80)
    print(f"\n[ELIGIBLE TIERS]")
    print(f"  Premium (150+):    {premium}")
    print(f"  Standard (80-149): {standard}")
    print(f"  Low (20-79):       {low}")

    conn.close()
    print(f"\n[DONE] {stats['eligible']} jobs ready for auto-apply")


if __name__ == "__main__":
    main()
