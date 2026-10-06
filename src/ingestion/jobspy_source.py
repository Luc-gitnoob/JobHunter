"""python-jobspy based job source for LinkedIn/Indeed/Glassdoor fallback."""

import logging
from datetime import datetime, timezone
from typing import Optional

from src.ingestion.base import JobSource, RawJob

logger = logging.getLogger(__name__)


class JobSpySource(JobSource):
    """
    Uses python-jobspy to scrape job listings from LinkedIn, Indeed,
    Glassdoor, and ZipRecruiter.

    This is the fallback source for companies not on Greenhouse/Lever/Ashby.
    It's also useful for broad keyword-based discovery.

    Note: python-jobspy is synchronous, so we run it in a thread executor.
    """

    @property
    def source_name(self) -> str:
        return "jobspy"

    async def fetch_jobs(self, company_name: str, slug: str, **kwargs) -> list[RawJob]:
        """
        For JobSpy, `slug` is unused. Instead we use kwargs:
        - search_term: str
        - location: str
        - sites: list[str]
        - results_wanted: int
        """
        import asyncio

        search_term = kwargs.get("search_term", "Backend Engineer")
        location = kwargs.get("location", "India")
        sites = kwargs.get("sites", ["linkedin", "indeed"])
        results_wanted = kwargs.get("results_wanted", 20)

        loop = asyncio.get_event_loop()
        try:
            raw_jobs = await loop.run_in_executor(
                None,
                self._scrape_sync,
                search_term,
                location,
                sites,
                results_wanted,
            )
            logger.info(
                f"[JobSpy] Search '{search_term}' in '{location}': found {len(raw_jobs)} postings"
            )
            return raw_jobs
        except Exception as e:
            logger.error(f"[JobSpy] Error scraping '{search_term}': {e}")
            return []

    def _scrape_sync(
        self,
        search_term: str,
        location: str,
        sites: list[str],
        results_wanted: int,
    ) -> list[RawJob]:
        """Synchronous wrapper around python-jobspy's scrape_jobs."""
        try:
            from jobspy import scrape_jobs
        except ImportError:
            logger.error(
                "[JobSpy] python-jobspy not installed. "
                "Run: pip install python-jobspy"
            )
            return []

        try:
            df = scrape_jobs(
                site_name=sites,
                search_term=search_term,
                location=location,
                results_wanted=results_wanted,
                hours_old=24,  # Only jobs posted in last 24 hours
            )
        except Exception as e:
            logger.error(f"[JobSpy] scrape_jobs failed: {e}")
            return []

        raw_jobs = []
        for _, row in df.iterrows():
            try:
                posted_at = None
                if hasattr(row, "date_posted") and row.date_posted is not None:
                    try:
                        posted_at = datetime.combine(
                            row.date_posted, datetime.min.time()
                        ).replace(tzinfo=timezone.utc)
                    except Exception:
                        pass

                raw_jobs.append(
                    RawJob(
                        external_id=str(row.get("id", row.get("job_url", ""))),
                        source="jobspy",
                        company_name=str(row.get("company", "Unknown")),
                        title=str(row.get("title", "Unknown")),
                        location=str(row.get("location", "")),
                        department=None,
                        description=str(row.get("description", "")),
                        apply_url=str(row.get("job_url", "")),
                        posted_at=posted_at,
                        raw_data=row.to_dict() if hasattr(row, "to_dict") else {},
                    )
                )
            except Exception as e:
                logger.warning(f"[JobSpy] Failed to parse row: {e}")

        return raw_jobs
