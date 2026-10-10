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
from typing import Optional

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
from src.ingestion.instahyre_source import InstahyreSource
from src.ingestion.amazon import AmazonSource
from src.pipeline.normalizer import normalize_job
from src.pipeline.filters import PreFilter
from src.pipeline.dedup import DedupEngine
from src.pipeline.scorer import JobScorer
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


# ===================== NOTIFICATION HELPER =====================
async def _notify_job_alert(
    db_job: Job,
    score_result: dict,
    notifier: TelegramNotifier,
    profile: dict,
) -> bool:
    """Deliver real-time Telegram alert and referral message for a high-matching job."""
    try:
        referral_msg = generate_referral_message(
            candidate_name=profile["name"],
            company=db_job.company_name,
            title=db_job.title,
            apply_url=db_job.apply_url,
            matching_skills=score_result.get("matching_skills", []),
        )

        sent = await notifier.send_job_alert(
            company=db_job.company_name,
            title=db_job.title,
            location=db_job.location or "",
            apply_url=db_job.apply_url,
            match_score=int(score_result.get("match_score", db_job.match_score or 0)),
            match_summary=score_result.get("summary", ""),
            matching_skills=score_result.get("matching_skills", []),
            referral_message=referral_msg,
        )

        async with AsyncSessionLocal() as session:
            from sqlalchemy import update
            update_values = {
                "referral_message": referral_msg,
                "status": "notified" if sent else "scored",
            }
            if sent:
                update_values["notified"] = True
                update_values["notified_at"] = datetime.now(timezone.utc)

            await session.execute(
                update(Job).where(Job.id == db_job.id).values(**update_values)
            )
            await session.commit()

        if sent:
            logger.info(f"  📬 Telegram alert delivered for {db_job.company_name} — {db_job.title}")
        return sent

    except Exception as e:
        logger.error(f"  Notification error for {db_job.company_name}/{db_job.title}: {e}")
        return False


# ===================== PIPELINE =====================
async def run_poll_cycle(all_config: dict):
    """
    Execute one full polling cycle:
    1. Fetch jobs from all sources
    2. Normalize
    3. Pre-filter
    4. Dedup
    5. LLM score (strict <2 YOE and 18+ LPA product caliber)
    6. Notify via Telegram with referral message
    """
    config = all_config["config"]
    companies_config = all_config["companies"]
    profile = all_config["profile"]

    curated_names = [c["name"] for c in companies_config.get("companies", [])]
    for s in companies_config.get("jobspy_searches", []):
        term = s.get("search_term", "")
        first_word = term.split()[0] if term else ""
        if first_word:
            curated_names.append(first_word)

    pre_filter = PreFilter(
        config.get("filters", {}),
        profile=profile,
        curated_companies=curated_names,
    )
    scorer = JobScorer(config.get("scoring", {}))
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

                # Attach geo_filter and curated flag as metadata for filtering
                for job in jobs:
                    job.raw_data["_geo_filter"] = geo_filter
                    job.raw_data["_is_curated"] = True

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

        # --- Phase 1b: Dedicated Amazon Jobs API poller ---
        try:
            amazon_source = AmazonSource(client=client)
            amazon_jobs = await amazon_source.fetch_jobs(company_name="Amazon")
            for job in amazon_jobs:
                job.raw_data["_geo_filter"] = ["India", "Bangalore", "Bengaluru", "Hyderabad", "Pune", "Remote"]
                job.raw_data["_is_curated"] = True
            all_raw_jobs.extend(amazon_jobs)
            logger.info(f"  [amazon.jobs] Amazon: {len(amazon_jobs)} raw postings")
        except Exception as e:
            logger.error(f"  [amazon.jobs] Error fetching jobs: {e}")

    # --- Phase 1c: JobSpy fallback searches ---
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

    # --- Phase 1d: Instahyre targeted searches ---
    instahyre_source = InstahyreSource()
    for search in companies_config.get("instahyre_searches", []):
        try:
            jobs = await instahyre_source.fetch_jobs(
                job_functions=search.get("job_functions", "10"),
                years=search.get("years", "1"),
                skills=search.get("skills"),
                locations=search.get("locations"),
                results_wanted=search.get("results_wanted", 30),
                max_yoe=pre_filter.max_yoe,
            )
            all_raw_jobs.extend(jobs)
        except Exception as e:
            logger.error(f"  [instahyre] Search '{search}': ERROR — {e}")

    logger.info(f"\nTotal raw jobs fetched: {len(all_raw_jobs)}")

    # --- Phase 2: Normalize ---
    normalized = [normalize_job(job) for job in all_raw_jobs]
    logger.info(f"Normalized: {len(normalized)} jobs")

    # --- Phase 3: Pre-filter + Dedup ---
    async with AsyncSessionLocal() as session:
        dedup = DedupEngine(session)
        new_inserted = 0
        promoted_count = 0

        for job in normalized:
            existing = await dedup.get_existing_job(job)
            geo_filter = job.raw_data.get("_geo_filter", [])
            passes, reason = pre_filter.apply(job, geo_filter)

            if existing:
                # If previously filtered out, but now passes with updated filters, resurrect it!
                if existing.is_filtered_out and passes:
                    existing.is_filtered_out = False
                    existing.filter_reason = None
                    existing.status = "discovered"
                    existing.match_score = None
                    if job.description:
                        existing.description = job.description
                    promoted_count += 1
                continue

            # Brand new job
            if not passes:
                await dedup.insert_job(job, is_filtered_out=True, filter_reason=reason)
            else:
                await dedup.insert_job(job, is_filtered_out=False)
                new_inserted += 1

        await session.commit()

    logger.info(
        f"Dedup & Filter: {new_inserted} newly inserted, "
        f"{promoted_count} promoted from relaxed filters."
    )

    # --- Phase 4a: Dispatch unnotified high-match jobs in database ---
    # Instantly alerts on any high matches found in earlier runs that weren't yet notified
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select, case
        source_priority = case(
            (Job.source.in_(["greenhouse", "ashby", "lever", "smartrecruiters", "amazon"]), 1),
            (Job.source == "instahyre", 2),
            else_=3,
        )
        stmt = (
            select(Job)
            .where(
                Job.is_filtered_out == False,
                Job.match_score >= min_score,
                Job.notified == False,
            )
            .order_by(source_priority.asc(), Job.match_score.desc())
        )
        res = await session.execute(stmt)
        unnotified_jobs = res.scalars().all()

    if unnotified_jobs:
        logger.info(
            f"Found {len(unnotified_jobs)} unnotified high-match jobs in database. "
            f"Dispatching Telegram alerts..."
        )
        for db_job in unnotified_jobs:
            passes, reason = pre_filter.apply(db_job)
            if not passes:
                logger.info(
                    f"  Filtering out previously discovered job {db_job.company_name} — "
                    f"{db_job.title}: {reason}"
                )
                async with AsyncSessionLocal() as session:
                    from sqlalchemy import update
                    await session.execute(
                        update(Job)
                        .where(Job.id == db_job.id)
                        .values(is_filtered_out=True, filter_reason=reason, status="filtered")
                    )
                    await session.commit()
                continue

            score_data = {}
            if db_job.match_analysis:
                try:
                    score_data = json.loads(db_job.match_analysis)
                except Exception:
                    pass
            if not score_data:
                score_data = {
                    "match_score": int(db_job.match_score),
                    "summary": f"High matching role at {db_job.company_name}",
                    "matching_skills": [],
                }
            await _notify_job_alert(
                db_job=db_job,
                score_result=score_data,
                notifier=notifier,
                profile=profile,
            )
            await asyncio.sleep(2)

    # --- Phase 4b: Score pending unscored jobs ---
    # Prioritizes Tier-1 ATS direct company boards over broad scrapers
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select, case
        source_priority = case(
            (Job.source.in_(["greenhouse", "ashby", "lever", "smartrecruiters", "amazon"]), 1),
            (Job.source == "instahyre", 2),
            else_=3,
        )
        stmt = (
            select(Job)
            .where(
                Job.is_filtered_out == False,
                Job.match_score.is_(None),
            )
            .order_by(source_priority.asc(), Job.id.desc())
        )
        res = await session.execute(stmt)
        unscored_jobs = res.scalars().all()

    logger.info(f"Total unscored eligible jobs in queue: {len(unscored_jobs)}")

    if not unscored_jobs and not unnotified_jobs:
        logger.info("No new or pending jobs to process. Cycle complete.")
        return

    high_matches_dispatched = 0
    for db_job in unscored_jobs:
        if getattr(scorer, "quota_exhausted", False):
            logger.warning("  [LLM] Daily quota reached. Skipping remaining unscored jobs until next cycle.")
            break

        # Safety: re-verify against pre_filter in case rules/thresholds were updated since insertion
        passes, filter_reason = pre_filter.apply(db_job)
        if not passes:
            logger.info(
                f"  [PreFilter Purge] Discarding queued job {db_job.company_name} — {db_job.title}: {filter_reason}"
            )
            async with AsyncSessionLocal() as session:
                from sqlalchemy import update
                await session.execute(
                    update(Job)
                    .where(Job.id == db_job.id)
                    .values(is_filtered_out=True, filter_reason=filter_reason)
                )
                await session.commit()
            continue

        try:
            score_result = await scorer.score_job(
                company=db_job.company_name,
                title=db_job.title,
                location=db_job.location or "",
                department=db_job.department or "",
                description=db_job.description or "",
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
                    high_matches_dispatched += 1
                    logger.info(
                        f"  ⭐ HIGH MATCH ({score_result['match_score']}/100): "
                        f"{db_job.company_name} — {db_job.title}. Dispatching notification..."
                    )
                    await _notify_job_alert(
                        db_job=db_job,
                        score_result=score_result,
                        notifier=notifier,
                        profile=profile,
                    )

        except Exception as e:
            logger.error(f"  Scoring error for {db_job.company_name}/{db_job.title}: {e}")

        # Rate limit Gemini calls (safe for Free Tier RPM)
        await asyncio.sleep(4)

    total_alerts = len(unnotified_jobs) + high_matches_dispatched
    logger.info(f"Cycle completed. High-match jobs dispatched: {total_alerts}")

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
