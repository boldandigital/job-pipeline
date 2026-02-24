# Scoring System

The pipeline uses a two-stage scoring system to evaluate job relevance.

## Stage 1: Keyword-Based Scoring

**Script:** `src/scoring/score_jobs.py`

Fast, rule-based scoring using configurable keyword categories and weights defined in `config/settings.yaml`.

### How It Works

1. For each job, combine title + description into a single text
2. Check each keyword category against the text
3. If a keyword matches, add the category's weight to the score
4. Each category is counted only once (prevents double-counting)

### Scoring Categories

| Category | Weight | Example Keywords |
|----------|:---:|-----------------|
| `fully_remote` | +180 | "100% remote", "fully remote", "komplett remote" |
| `remote_first_culture` | +160 | "remote-first", "distributed team", "async-first" |
| `four_day_week` | +120 | "4-day week", "4-Tage-Woche", "32 hours" |
| `ai_llm_operations` | +65 | "ai operations", "llm ops", "ai trainer" |
| `automation_specialist` | +55 | "automation specialist", "ai automation" |
| `office_required` | -400 | "office required", "on-site required" |
| `gig_platforms` | -900 | "fiverr", "upwork", "commission only" |
| `pure_sales` | -400 | "cold calling", "door to door", "quota" |

Full list: 40+ categories with positive and negative weights.

### Score Tiers

| Tier | Score Range | Meaning |
|------|:---:|---------|
| Premium | 150+ | Strong match on multiple dimensions |
| Standard | 80-149 | Good match, worth reviewing |
| Low | 20-79 | Marginal match, review if time permits |
| Filtered | <20 | Below threshold, not shown |

## Stage 2: LLM-Based Scoring

**Script:** `src/scoring/llm_scorer.py`

Uses Claude API (Haiku model) for context-aware scoring. Only runs on jobs that have a description available.

### 5 Scoring Dimensions

| Dimension | Weight | What It Measures |
|-----------|:---:|-----------------|
| Day-to-Day Fit | 35% | Does the actual daily work match what you want? |
| Growth Potential | 20% | Can you learn, grow, and shape the role? |
| Culture Fit | 15% | Remote-friendly? Flexible? Autonomous? |
| Skills Match | 20% | Do your skills match requirements? |
| Compensation Match | 10% | Is the salary likely in your range? |

### How It Works

1. Load your candidate profile from `config/candidate_profile.md`
2. For each job with a description, send to Claude:
   - Your profile
   - The job title, company, location, description
   - Scoring prompt with the 5 dimensions
3. Claude returns a JSON with 0-100 scores per dimension
4. Weighted average becomes the final LLM score
5. Score is stored in the database

### Cost

- **Model:** Claude Haiku (cheapest Anthropic model)
- **Per job:** ~800 input tokens + ~200 output tokens = approximately $0.0003
- **50 jobs/day:** ~$0.015/day, roughly $0.45/month
- **Bulk scoring:** 1000 jobs = approximately $0.30

## Two-Layer Filter

**Script:** `src/scoring/apply_filter.py`

After scoring, a separate filter removes obviously irrelevant jobs.

### Layer 1: Hard Title Blocks

Regex patterns that block jobs by title:

| Block Type | Examples |
|-----------|---------|
| Seniority | "Senior", "Lead", "Director", "Head of", "VP", "C-Level" |
| Contract | "Freelance", "PhD", "Master Thesis" |
| Too Junior | "Werkstudent", "Praktikum", "Internship", "Azubi" |

**Exception:** "(Senior)" in parentheses = flexible posting, NOT blocked.

### Layer 2: Profile Relevance Gate

Jobs must match at least one of 14 competency clusters:

| Cluster | Example Patterns |
|---------|-----------------|
| AI/ML/Data | ai, llm, genai, machine learning, prompt |
| Automation/RPA | automation, rpa, n8n, zapier, workflow |
| Business Analysis | business analyst, process analyst, scrum master |
| Data Analytics/BI | data analyst, analytics, reporting, power bi |
| IT/Digital | digitalisierung, it koordinator, system analyst |
| Operations | revops, sales ops, crm, hubspot |
| Compliance/RegTech | compliance, kyc, aml, data governance |
| Content/Docs | technical writer, documentation, ux writer |
| Customer Success | customer success, client success, renewal manager |
| FinTech/Web3 | fintech, blockchain, crypto, defi |
| E-Commerce | e-commerce, shopify, online handel |

### Always Irrelevant

54 patterns for roles that are never relevant (accountant, driver, nurse, chef, lawyer, etc.).

### Language Block

Blocks jobs requiring languages you do not speak (configurable).
