"""SmartRecruiters Job Board API poller."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from src.ingestion.base import JobSource, RawJob

logger = logging.getLogger(__name__)

SMARTRECRUITERS_API_BASE = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"


class SmartRecruitersSource(JobSource):
    """
    Polls the SmartRecruiters public postings API.

    Endpoint: GET /v1/companies/{company-identifier}/postings
    - No authentication required
    - Returns JSON array with company openings
    """

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._client = client

    @property
    def source_name(self) -> str:
        return "smartrecruiters"

    async def fetch_jobs(self, company_name: str, slug: str, **kwargs) -> list[RawJob]:
        client = self._client or httpx.AsyncClient(timeout=30.0)
        should_close = self._client is None

        try:
            url = SMARTRECRUITERS_API_BASE.format(slug=slug)
            all_postings = []
            offset = 0
            limit = 100

            while True:
                response = await client.get(url, params={"limit": limit, "offset": offset})
                response.raise_for_status()

                data = response.json()
                content = data.get("content", [])
                if not content:
                    break

                all_postings.extend(content)
                if len(content) < limit or len(all_postings) >= 500:
                    break
                offset += limit

            logger.info(
                f"[SmartRecruiters] {company_name} ({slug}): found {len(all_postings)} postings"
            )

            # Fetch details in batches with concurrency limit
            sem = asyncio.Semaphore(10)

            async def _fetch_and_parse(posting: dict) -> Optional[RawJob]:
                async with sem:
                    return await self._parse_posting(client, posting, company_name, slug)

            tasks = [_fetch_and_parse(p) for p in all_postings]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            raw_jobs = []
            for r in results:
                if isinstance(r, RawJob):
                    raw_jobs.append(r)
                elif isinstance(r, Exception):
                    logger.warning(f"[SmartRecruiters] Error parsing posting: {r}")

            return raw_jobs

        except httpx.HTTPStatusError as e:
            logger.error(
                f"[SmartRecruiters] HTTP {e.response.status_code} for {company_name} ({slug})"
            )
            return []
        except Exception as e:
            logger.error(f"[SmartRecruiters] Error fetching {company_name}: {e}")
            return []
        finally:
            if should_close:
                await client.aclose()

    async def _parse_posting(
        self, client: httpx.AsyncClient, data: dict, company_name: str, slug: str
    ) -> RawJob:
        """Fetch details if needed and parse into RawJob."""
        job_id = str(data["id"])
        title = data.get("name", "Unknown")

        # Location
        loc_data = data.get("location", {})
        location = loc_data.get("fullLocation")
        if not location:
            parts = [loc_data.get("city"), loc_data.get("region"), loc_data.get("country")]
            location = ", ".join([p for p in parts if p])

        # Department
        dept_data = data.get("department", {})
        department = dept_data.get("label") if isinstance(dept_data, dict) else str(dept_data or "")

        # Posted date
        posted_at = None
        released_date = data.get("releasedDate")
        if released_date:
            try:
                posted_at = datetime.fromisoformat(released_date.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        # Fetch full job ad description & applyUrl from detail endpoint
        description = ""
        apply_url = f"https://jobs.smartrecruiters.com/{slug}/{job_id}"

        # Optimization: only make detail API requests for titles that have software/tech indicators
        title_lower = title.lower()
        tech_keywords = ("software", "developer", "engineer", "backend", "platform", "systems", "sde", "data", "cloud", "tech")
        is_candidate_job = any(k in title_lower for k in tech_keywords)

        if is_candidate_job:
            try:
                detail_url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job_id}"
                resp = await client.get(detail_url, timeout=15.0)
                if resp.status_code == 200:
                    detail_data = resp.json()
                    apply_url = detail_data.get("applyUrl") or detail_data.get("postingUrl") or apply_url

                    sections = detail_data.get("jobAd", {}).get("sections", {})
                    desc_parts = []
                    for sec_key in ["jobDescription", "qualifications", "additionalInformation"]:
                        sec = sections.get(sec_key, {})
                        sec_title = sec.get("title", "")
                        sec_text = sec.get("text", "")
                        if sec_text:
                            if sec_title:
                                desc_parts.append(f"### {sec_title}\n{sec_text}")
                            else:
                                desc_parts.append(sec_text)
                    description = "\n\n".join(desc_parts)
            except Exception as e:
                logger.debug(f"[SmartRecruiters] Could not fetch details for {job_id}: {e}")

        return RawJob(
            external_id=job_id,
            source="smartrecruiters",
            company_name=company_name,
            title=title,
            location=location,
            department=department,
            description=description,
            apply_url=apply_url,
            posted_at=posted_at,
            raw_data=data,
        )
