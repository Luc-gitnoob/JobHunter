# 🎯 JobHunter — Autonomous Job Hunting & Referral Engine

An automated pipeline that monitors high-value company job boards, scores openings against your profile using Gemini LLM, generates tailored resumes, and pushes instant alerts to Telegram — so your only job is the high-leverage action: applying and sending referrals.

## Architecture

```
ATS APIs (Greenhouse/Lever/Ashby/SmartRecruiters) ──→ Poller ──→ Normalizer ──→ Pre-Filter
                                                                                    │
     python-jobspy (LinkedIn/Indeed) ───────────────────────────────────────────────┘
                                                                                    │
                                                                              Dedup (SQLite)
                                                                                    │
                                                                            LLM Scorer (Gemini)
                                                                                    │
                                                                     ┌──────────────┼──────────────┐
                                                                     │              │              │
                                                              Resume Tailor   Referral Pitch   Telegram Alert
                                                               (LaTeX → PDF)                   (+ attachment)
                                                                     │
                                                               Web Dashboard
                                                            (FastAPI + Jinja2)
```

## Quick Start

### 1. Prerequisites
- Python 3.10+ (tested on Python 3.13)
- Google Gemini API key (free tier: https://aistudio.google.com/app/apikey)
- Telegram Bot token & chat ID (from @BotFather and @userinfobot)
- Optional: LaTeX distribution (`pdflatex` via TeX Live or MiKTeX) for PDF compilation; if not installed, tailored `.tex` source files are generated and ready for Overleaf or local compilation.

### 2. Setup Virtual Environment & Dependencies
```bash
cd JobHunter
python -m venv .venv
.venv\Scripts\activate       # Windows
# source .venv/bin/activate  # Linux/Mac

pip install -r requirements.txt
```

### 3. Configure API Keys
Edit `.env` (already created in project root):
```env
GEMINI_API_KEY=your_gemini_api_key_here
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

### 4. Target Companies
Review `config/companies.yaml` for pre-configured tier 1 companies across:
- **Greenhouse**: Stripe, Coinbase, Cloudflare, Datadog, Figma, Discord, Anthropic, Tower Research Capital, Jane Street, Rubrik, Reddit, Brex
- **Ashby**: Notion, Ramp, Linear, Perplexity, OpenAI
- **Lever**: Kraken
- **SmartRecruiters**: Bosch Group, Delivery Hero
- **JobSpy Fallbacks**: Targeted queries for India, Bangalore, and specific startups

### 5. Run the System

```bash
# Option A: Full system (scheduler runs every 15 min + web dashboard)
python -m src.main

# Option B: Run a single poll cycle (fetches, scores, tailors resume)
python -m src.main --poll-once

# Option C: Start only the web dashboard
python -m src.main --dashboard
```

Dashboard is accessible at: **http://localhost:8080**

## Features

### 📡 Multi-Source ATS Ingestion
- Native JSON APIs for **Greenhouse, Lever, Ashby, and SmartRecruiters** (no scraping, zero anti-bot risk)
- `python-jobspy` fallback for LinkedIn and Indeed searches
- Parallel async fetching with rate-limiting between companies

### 🧠 LLM-Powered Matching
- Google Gemini scores each role 0-100 against candidate profile (`config/profile.yaml`)
- Extracts matching skills, skill gaps, and custom resume emphasis
- Structured JSON output with strict error handling

### 📄 Dynamic Resume Customization
- Parameterized LaTeX single-column ATS-friendly template
- Custom bullet points tailored to the specific job description without hallucinating or fabricating facts
- Automatically produces `.tex` source and compiles to `.pdf` if `pdflatex` is installed
- Source code viewable and downloadable directly from the dashboard

### 📱 Telegram Alerts
- Immediate notifications for jobs scoring ≥ 70
- Contains match breakdown, direct apply link, and pre-drafted referral pitch
- Resilient message delivery with automatic plain text fallback if Markdown formatting fails
- Sends resume document as attachment

### 📊 Modern Web Dashboard
- Sleek dark theme with responsive navigation
- Full application tracking (`Discovered` → `Applied` → `Interviewing` → `Offer`)
- Filter by score, company, and status
- Analytics overview and polling logs

## Project Structure

```
JobHunter/
├── config/
│   ├── config.yaml          # Scheduler, filter rules, LLM settings
│   ├── profile.yaml         # Candidate resume ground truth
│   └── companies.yaml       # Target company catalog with ATS slugs
├── src/
│   ├── main.py              # CLI entry point & orchestrator
│   ├── ingestion/           # Greenhouse, Lever, Ashby, SmartRecruiters, JobSpy
│   ├── pipeline/            # Normalizer, filters, dedup, Gemini scorer
│   ├── resume/              # LaTeX template, tailor, compiler
│   ├── notifications/       # Telegram bot notifier
│   ├── dashboard/           # FastAPI app, templates, dark mode styling
│   └── database/            # SQLAlchemy async models & SQLite session
├── data/                    # SQLite DB (jobhunter.db) & logs
├── output/resumes/          # Generated resumes (.tex and .pdf)
├── requirements.txt
├── .env.example
├── .env
└── README.md
```

## How to Find ATS Slugs

| ATS | Career Page URL Pattern | Slug Location |
|-----|------------------------|---------------|
| Greenhouse | `boards.greenhouse.io/{slug}` | URL path segment |
| Lever | `jobs.lever.co/{slug}` | URL path segment |
| Ashby | `jobs.ashbyhq.com/{slug}` | URL path segment |
| SmartRecruiters | `jobs.smartrecruiters.com/{slug}` | URL path segment |
