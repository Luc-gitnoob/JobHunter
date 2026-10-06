"""Deduplication engine — prevents re-processing of already-seen jobs."""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import Job
from src.ingestion.base import RawJob

logger = logging.getLogger(__name__)


class DedupEngine:
    """
    SQLite-backed deduplication.
    A job is considered a duplicate if (external_id, source, company_name)
    already exists in the database.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def is_duplicate(self, job: RawJob) -> bool:
        """Check if this job already exists in the database."""
        stmt = select(Job).where(
            Job.external_id == job.external_id,
            Job.source == job.source,
            Job.company_name == job.company_name,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def filter_new(self, jobs: list[RawJob]) -> list[RawJob]:
        """Filter a list of RawJobs, returning only those not yet in the DB."""
        new_jobs = []
        for job in jobs:
            if not await self.is_duplicate(job):
                new_jobs.append(job)
        return new_jobs

    async def insert_job(
        self,
        raw_job: RawJob,
        is_filtered_out: bool = False,
        filter_reason: str = None,
    ) -> Job:
        """
        Insert a new job into the database.
        Returns the created Job model instance.
        """
        job = Job(
            external_id=raw_job.external_id,
            source=raw_job.source,
            company_name=raw_job.company_name,
            title=raw_job.title,
            location=raw_job.location,
            department=raw_job.department,
            description=raw_job.description,
            apply_url=raw_job.apply_url,
            posted_at=raw_job.posted_at,
            is_filtered_out=is_filtered_out,
            filter_reason=filter_reason,
            status="discovered" if not is_filtered_out else "filtered",
        )
        self.session.add(job)
        await self.session.commit()
        await self.session.refresh(job)
        logger.debug(f"Inserted job: {job}")
        return job
