"""
JobHunter — Main Entry Point

This is the orchestrator that ties the entire pipeline together:
1. Loads configuration
2. Initializes the database
3. Runs the polling scheduler
4. Starts the web dashboard

Usage:
    python -m src.main              # Run full system (poller + dashboard)
    python -m src.main --poll-once  # Run a single poll cycle (for testing)
    python -m src.main --dashboard  # Run only the dashboard
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Load environment variables
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

from src.database.db import init_db, AsyncSessionLocal
from src.database.models import Job, PollLog
from src.ingestion.base import RawJob
from src.ingestion.greenhouse import GreenhouseSource
from src.ingestion.lever import LeverSource
from src.ingestion.ashby import AshbySource
from src.ingestion.smartrecruiters import SmartRecruitersSource
from src.ingestion.jobspy_source import JobSpySource
from src.pipeline.normalizer import normalize_job
from src.pipeline.filters import PreFilter
from src.pipeline.dedup import DedupEngine
from src.pipeline.scorer import JobScorer
from src.resume.tailor import ResumeTailor
from src.resume.compiler import ResumeCompiler
from src.notifications.telegram import TelegramNotifier, generate_referral_message

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)-25s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(PROJECT_ROOT / "data" / "jobhunter.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("jobhunter")


# ===================== CONFIG =====================
def load_config() -> dict:
    """Load all YAML config files."""
    config_dir = PROJECT_ROOT / "config"

    with open(config_dir / "config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    with open(config_dir / "companies.yaml", "r", encoding="utf-8") as f:
        companies = yaml.safe_load(f)

    with open(config_dir / "profile.yaml", "r", encoding="utf-8") as f:
        profile = yaml.safe_load(f)

    return {
        "config": config,
        "companies": companies,
        "profile": profile,
    }


# ===================== SOURCE REGISTRY =====================
SOURCES = {
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "ashby": AshbySource,
    "smartrecruiters": SmartRecruitersSource,
}


# ===================== PIPELINE =====================
async def run_poll_cycle(all_config: dict):
    """
    Execute one full polling cycle:
    1. Fetch jobs from all sources
    2. Normalize
    3. Pre-filter
    4. Dedup
    5. LLM score
    6. Generate tailored resume + referral message
    7. Notify via Telegram
    """
    config = all_config["config"]
    companies_config = all_config["companies"]
    profile = all_config["profile"]

    pre_filter = PreFilter(config.get("filters", {}))
    scorer = JobScorer(config.get("scoring", {}))
    tailor = ResumeTailor(config.get("scoring", {}))
    compiler = ResumeCompiler(config.get("resume", {}))
    notifier = TelegramNotifier()

    min_score = config.get("scoring", {}).get("min_score", 70)
    delay = config.get("scheduler", {}).get("request_delay_seconds", 2)

    logger.info("=" * 60)
    logger.info("Starting poll cycle")
    logger.info("=" * 60)

    # --- Phase 1: Fetch from ATS APIs ---
    all_raw_jobs: list[RawJob] = []

    import httpx
    async with httpx.AsyncClient(timeout=30.0) as client:
        for company in companies_config.get("companies", []):
            platform = company["ats_platform"]
            slug = company["ats_slug"]
            name = company["name"]
            geo_filter = company.get("geo_filter", [])

            source_class = SOURCES.get(platform)
            if not source_class:
                logger.warning(f"Unknown ATS platform: {platform} for {name}")
                continue

            source = source_class(client=client)

            # Log this poll
            async with AsyncSessionLocal() as session:
                poll_log = PollLog(
                    source=platform,
                    company_name=name,
                    status="running",
                )
                session.add(poll_log)
                await session.commit()
                poll_log_id = poll_log.id

            try:
                jobs = await source.fetch_jobs(name, slug)
                logger.info(f"  [{platform}] {name}: {len(jobs)} raw postings")

                # Attach geo_filter as metadata for filtering
                for job in jobs:
                    job.raw_data["_geo_filter"] = geo_filter

                all_raw_jobs.extend(jobs)

                # Update poll log
                async with AsyncSessionLocal() as session:
                    from sqlalchemy import update
                    await session.execute(
                        update(PollLog)
                        .where(PollLog.id == poll_log_id)
                        .values(
                            completed_at=datetime.now(timezone.utc),
                            jobs_found=len(jobs),
                            status="success",
                        )
                    )
                    await session.commit()

            except Exception as e:
                logger.error(f"  [{platform}] {name}: ERROR — {e}")
                async with AsyncSessionLocal() as session:
                    from sqlalchemy import update
                    await session.execute(
                        update(PollLog)
                        .where(PollLog.id == poll_log_id)
                        .values(
                            completed_at=datetime.now(timezone.utc),
                            error=str(e),
                            status="error",
                        )
                    )
                    await session.commit()

            # Rate limiting between companies
            await asyncio.sleep(delay)

    # --- Phase 1b: JobSpy fallback searches ---
    jobspy_source = JobSpySource()
    for search in companies_config.get("jobspy_searches", []):
        try:
            jobs = await jobspy_source.fetch_jobs(
                company_name="",  # JobSpy discovers the company
                slug="",
                search_term=search.get("search_term", ""),
                location=search.get("location", ""),
                sites=search.get("sites", ["linkedin"]),
                results_wanted=search.get("results_wanted", 20),
            )
            all_raw_jobs.extend(jobs)
        except Exception as e:
            logger.error(f"  [jobspy] Search '{search.get('search_term')}': ERROR — {e}")

    logger.info(f"\nTotal raw jobs fetched: {len(all_raw_jobs)}")

    # --- Phase 2: Normalize ---
    normalized = [normalize_job(job) for job in all_raw_jobs]
    logger.info(f"Normalized: {len(normalized)} jobs")

    # --- Phase 3: Pre-filter + Dedup ---
    new_jobs = []
    async with AsyncSessionLocal() as session:
        dedup = DedupEngine(session)

        for job in normalized:
            # Dedup first (cheaper than filtering)
            if await dedup.is_duplicate(job):
                continue

            # Pre-filter
            geo_filter = job.raw_data.get("_geo_filter", [])
            passes, reason = pre_filter.apply(job, geo_filter)

            if not passes:
                # Still insert as filtered for analytics
                await dedup.insert_job(job, is_filtered_out=True, filter_reason=reason)
                continue

            # Insert as new, unscored job
            db_job = await dedup.insert_job(job)
            new_jobs.append((job, db_job))

    logger.info(f"New jobs after dedup + filter: {len(new_jobs)}")

    if not new_jobs:
        logger.info("No new jobs to process. Cycle complete.")
        return

    # --- Phase 4: LLM Scoring ---
    scored_jobs = []
    for raw_job, db_job in new_jobs:
        if getattr(scorer, "quota_exhausted", False):
            logger.warning("  [LLM] Daily quota reached. Skipping remaining unscored jobs until next cycle.")
            break

        try:
            score_result = await scorer.score_job(
                company=raw_job.company_name,
                title=raw_job.title,
                location=raw_job.location or "",
                department=raw_job.department or "",
                description=raw_job.description or "",
            )

            if getattr(scorer, "quota_exhausted", False):
                logger.warning("  [LLM] Daily quota reached during scoring. Halting scoring phase.")
                break

            if score_result:
                async with AsyncSessionLocal() as session:
                    from sqlalchemy import update
                    await session.execute(
                        update(Job)
                        .where(Job.id == db_job.id)
                        .values(
                            match_score=score_result["match_score"],
                            match_analysis=json.dumps(score_result),
                            status="scored",
                        )
                    )
                    await session.commit()

                if score_result["match_score"] >= min_score:
                    scored_jobs.append((raw_job, db_job, score_result))
                    logger.info(
                        f"  ⭐ HIGH MATCH: {raw_job.company_name} — {raw_job.title} "
                        f"(score: {score_result['match_score']})"
                    )

        except Exception as e:
            logger.error(f"  Scoring error for {raw_job.company_name}/{raw_job.title}: {e}")

        # Rate limit Gemini calls (safe for Free Tier RPM)
        await asyncio.sleep(4)

    logger.info(f"High-match jobs (≥{min_score}): {len(scored_jobs)}")

    # --- Phase 5: Resume Generation + Notification ---
    for raw_job, db_job, score_result in scored_jobs:
        try:
            # Generate tailored resume
            tailored = await tailor.tailor(
                company=raw_job.company_name,
                title=raw_job.title,
                description=raw_job.description or "",
                match_analysis=score_result,
            )

            resume_path = None
            if tailored:
                context = tailor.build_template_context(tailored)
                try:
                    safe_name = f"{raw_job.company_name}_{raw_job.title}_{db_job.id}"
                    resume_path = compiler.compile(context, safe_name)
                except Exception as e:
                    logger.warning(f"  Resume generation failed: {e}")

            # Generate referral message
            referral_msg = generate_referral_message(
                candidate_name=profile["name"],
                company=raw_job.company_name,
                title=raw_job.title,
                apply_url=raw_job.apply_url,
                matching_skills=score_result.get("matching_skills", []),
            )

            # Update DB
            async with AsyncSessionLocal() as session:
                from sqlalchemy import update
                update_values = {
                    "referral_message": referral_msg,
                    "status": "resume_generated" if resume_path else "scored",
                }
                if resume_path:
                    update_values["resume_path"] = str(resume_path)
                    update_values["resume_generated_at"] = datetime.now(timezone.utc)

                await session.execute(
                    update(Job).where(Job.id == db_job.id).values(**update_values)
                )
                await session.commit()

            # Send Telegram notification
            sent = await notifier.send_job_alert(
                company=raw_job.company_name,
                title=raw_job.title,
                location=raw_job.location or "",
                apply_url=raw_job.apply_url,
                match_score=score_result["match_score"],
                match_summary=score_result.get("summary", ""),
                matching_skills=score_result.get("matching_skills", []),
                referral_message=referral_msg,
                resume_path=resume_path,
            )

            if sent:
                async with AsyncSessionLocal() as session:
                    from sqlalchemy import update
                    await session.execute(
                        update(Job)
                        .where(Job.id == db_job.id)
                        .values(
                            notified=True,
                            notified_at=datetime.now(timezone.utc),
                            status="notified",
                        )
                    )
                    await session.commit()

        except Exception as e:
            logger.error(
                f"  Resume/notification error for "
                f"{raw_job.company_name}/{raw_job.title}: {e}"
            )

    logger.info("=" * 60)
    logger.info("Poll cycle complete!")
    logger.info("=" * 60)


# ===================== ENTRY POINTS =====================

async def run_scheduler(all_config: dict):
    """Run the poller on a schedule using APScheduler."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    interval = all_config["config"].get("scheduler", {}).get("poll_interval_minutes", 15)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_poll_cycle,
        trigger=IntervalTrigger(minutes=interval),
        args=[all_config],
        id="job_poller",
        name="Job Poller",
        next_run_time=datetime.now(timezone.utc),  # Run immediately on start
    )
    scheduler.start()

    logger.info(f"Scheduler started — polling every {interval} minutes")

    # Keep running
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


async def run_dashboard(all_config: dict):
    """Run only the FastAPI dashboard."""
    import uvicorn
    from src.dashboard.app import app

    host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.getenv("DASHBOARD_PORT", "8080"))

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def run_full(all_config: dict):
    """Run both the scheduler and dashboard concurrently."""
    import uvicorn
    from src.dashboard.app import app

    host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.getenv("DASHBOARD_PORT", "8080"))

    # Start dashboard in background
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)

    # Run both concurrently
    await asyncio.gather(
        server.serve(),
        run_scheduler(all_config),
    )


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="JobHunter — Autonomous Job Hunting Engine"
    )
    parser.add_argument(
        "--poll-once",
        action="store_true",
        help="Run a single poll cycle and exit",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Run only the web dashboard",
    )
    args = parser.parse_args()

    # Ensure data directory exists
    (PROJECT_ROOT / "data").mkdir(exist_ok=True)
    (PROJECT_ROOT / "output" / "resumes").mkdir(parents=True, exist_ok=True)

    # Load config
    all_config = load_config()

    # Initialize database
    asyncio.run(init_db())

    if args.poll_once:
        logger.info("Running single poll cycle...")
        asyncio.run(run_poll_cycle(all_config))
    elif args.dashboard:
        logger.info("Starting dashboard only...")
        asyncio.run(run_dashboard(all_config))
    else:
        logger.info("Starting full system (poller + dashboard)...")
        asyncio.run(run_full(all_config))


if __name__ == "__main__":
    main()
