"""Ashby Job Board API poller."""

import logging
from datetime import datetime
from typing import Optional

import httpx

from src.ingestion.base import JobSource, RawJob

logger = logging.getLogger(__name__)

# Ashby public posting API — no auth required
ASHBY_API_BASE = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


class AshbySource(JobSource):
    """
    Polls the Ashby Job Board API.

    Endpoint: GET /posting-api/job-board/{board-name}
    - No authentication required
    - Returns JSON with jobs grouped by department
    - ?includeCompensation=true for salary data
    """

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._client = client

    @property
    def source_name(self) -> str:
        return "ashby"

    async def fetch_jobs(self, company_name: str, slug: str, **kwargs) -> list[RawJob]:
        client = self._client or httpx.AsyncClient(timeout=30.0)
        should_close = self._client is None

        try:
            url = ASHBY_API_BASE.format(slug=slug)
            params = {"includeCompensation": "true"}

            response = await client.get(url, params=params)
            response.raise_for_status()

            data = response.json()
            jobs_data = data.get("jobs", [])

            logger.info(
                f"[Ashby] {company_name} ({slug}): found {len(jobs_data)} postings"
            )

            raw_jobs = []
            for job in jobs_data:
                try:
                    raw_jobs.append(self._parse_job(job, company_name))
                except Exception as e:
                    logger.warning(
                        f"[Ashby] Failed to parse job {job.get('id')}: {e}"
                    )

            return raw_jobs

        except httpx.HTTPStatusError as e:
            logger.error(
                f"[Ashby] HTTP {e.response.status_code} for {company_name} ({slug})"
            )
            return []
        except Exception as e:
            logger.error(f"[Ashby] Error fetching {company_name}: {e}")
            return []
        finally:
            if should_close:
                await client.aclose()

    def _parse_job(self, data: dict, company_name: str) -> RawJob:
        """Parse a single Ashby job object into RawJob."""
        # Location
        location = data.get("location", "")

        # Department
        department = data.get("department", "")

        # Posted date
        posted_at = None
        published_str = data.get("publishedAt")
        if published_str:
            try:
                posted_at = datetime.fromisoformat(
                    published_str.replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass

        # Description — Ashby provides descriptionHtml and descriptionPlain
        description = data.get("descriptionPlain", "") or data.get("descriptionHtml", "")

        # Apply URL
        job_url = data.get("jobUrl", "")
        apply_url = data.get("applyUrl", job_url)

        # Compensation info (bonus)
        compensation = data.get("compensation", {})
        if compensation:
            comp_str = self._format_compensation(compensation)
            if comp_str:
                description += f"\n\nCompensation: {comp_str}"

        return RawJob(
            external_id=str(data["id"]),
            source="ashby",
            company_name=company_name,
            title=data.get("title", "Unknown"),
            location=location,
            department=department,
            description=description,
            apply_url=apply_url,
            posted_at=posted_at,
            raw_data=data,
        )

    def _format_compensation(self, comp: dict) -> str:
        """Format compensation data into a readable string."""
        parts = []
        if comp.get("compensationTierSummary"):
            parts.append(comp["compensationTierSummary"])
        elif comp.get("summaryComponents"):
            for component in comp["summaryComponents"]:
                min_val = component.get("minValue", "")
                max_val = component.get("maxValue", "")
                currency = component.get("currencyCode", "")
                interval = component.get("interval", "")
                if min_val and max_val:
                    parts.append(f"{currency} {min_val}-{max_val}/{interval}")
        return " | ".join(parts) if parts else ""
