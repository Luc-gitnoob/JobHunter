"""Greenhouse Job Board API poller."""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from src.ingestion.base import JobSource, RawJob

logger = logging.getLogger(__name__)

# Greenhouse public Job Board API — no auth required
GREENHOUSE_API_BASE = "https://api.greenhouse.io/v1/boards/{slug}/jobs"


class GreenhouseSource(JobSource):
    """
    Polls the Greenhouse Job Board API.

    API docs: https://developers.greenhouse.io/job-board.html
    Endpoint: GET /v1/boards/{board_token}/jobs?content=true
    - No authentication required
    - Returns JSON with all public job postings
    - `content=true` includes the full HTML job description
    """

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._client = client

    @property
    def source_name(self) -> str:
        return "greenhouse"

    async def fetch_jobs(self, company_name: str, slug: str, **kwargs) -> list[RawJob]:
        client = self._client or httpx.AsyncClient(timeout=30.0)
        should_close = self._client is None

        try:
            url = GREENHOUSE_API_BASE.format(slug=slug)
            params = {"content": "true"}

            response = await client.get(url, params=params)
            response.raise_for_status()

            data = response.json()
            jobs_data = data.get("jobs", [])

            logger.info(
                f"[Greenhouse] {company_name} ({slug}): found {len(jobs_data)} postings"
            )

            raw_jobs = []
            for job in jobs_data:
                try:
                    raw_jobs.append(self._parse_job(job, company_name))
                except Exception as e:
                    logger.warning(
                        f"[Greenhouse] Failed to parse job {job.get('id')}: {e}"
                    )

            return raw_jobs

        except httpx.HTTPStatusError as e:
            logger.error(
                f"[Greenhouse] HTTP {e.response.status_code} for {company_name} ({slug})"
            )
            return []
        except Exception as e:
            logger.error(f"[Greenhouse] Error fetching {company_name}: {e}")
            return []
        finally:
            if should_close:
                await client.aclose()

    def _parse_job(self, data: dict, company_name: str) -> RawJob:
        """Parse a single Greenhouse job object into RawJob."""
        # Extract location from the location object
        location = None
        loc_data = data.get("location", {})
        if loc_data:
            location = loc_data.get("name", "")

        # Parse departments
        departments = data.get("departments", [])
        department = departments[0].get("name") if departments else None

        # Parse posted date
        posted_at = None
        updated_str = data.get("updated_at") or data.get("created_at")
        if updated_str:
            try:
                posted_at = datetime.fromisoformat(
                    updated_str.replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass

        # Build apply URL
        apply_url = data.get("absolute_url", "")

        # Get description (HTML content)
        description = data.get("content", "")

        return RawJob(
            external_id=str(data["id"]),
            source="greenhouse",
            company_name=company_name,
            title=data.get("title", "Unknown"),
            location=location,
            department=department,
            description=description,
            apply_url=apply_url,
            posted_at=posted_at,
            raw_data=data,
        )
