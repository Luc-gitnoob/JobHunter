"""Amazon Jobs API ingestion source."""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from src.ingestion.base import JobSource, RawJob

logger = logging.getLogger(__name__)


class AmazonSource(JobSource):
    """
    Direct ingestion from Amazon's public Jobs API (https://www.amazon.jobs/en/search.json).
    Targeted specifically for India SDE postings.
    """

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self.client = client
        self._owns_client = client is None

    @property
    def source_name(self) -> str:
        return "amazon"

    async def fetch_jobs(self, company_name: str = "Amazon", slug: str = "amazon", **kwargs) -> list[RawJob]:
        """
        Fetch Software Development Engineer jobs from Amazon Jobs API.
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        }
        url = "https://www.amazon.jobs/en/search.json"
        params = {
            "base_query": "Software Development Engineer",
            "country": "IND",
            "result_limit": 50,
            "sort": "recent",
        }

        client = self.client or httpx.AsyncClient(headers=headers, timeout=30.0)
        try:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code != 200:
                logger.error(f"[Amazon] Failed to fetch jobs: HTTP {resp.status_code}")
                return []

            data = resp.json()
            jobs_list = data.get("jobs", [])
            raw_jobs = []

            for item in jobs_list:
                job_id = str(item.get("id_icims") or item.get("id") or "")
                title = item.get("title", "").strip()
                location = item.get("normalized_location") or item.get("location") or "India"
                job_path = item.get("job_path", "")
                apply_url = f"https://www.amazon.jobs{job_path}" if job_path else "https://www.amazon.jobs"

                # Combine description and qualifications for complete JD evaluation
                desc = item.get("description", "")
                basic_quals = item.get("basic_qualifications", "")
                pref_quals = item.get("preferred_qualifications", "")
                full_description = f"{desc}\n\nBasic Qualifications:\n{basic_quals}\n\nPreferred Qualifications:\n{pref_quals}".strip()

                raw_jobs.append(
                    RawJob(
                        external_id=job_id,
                        source="amazon",
                        company_name="Amazon",
                        title=title,
                        location=location,
                        department=item.get("job_category") or "Software Development",
                        description=full_description,
                        apply_url=apply_url,
                        posted_at=datetime.now(timezone.utc),
                        raw_data=item,
                    )
                )

            logger.info(f"[Amazon] Found {len(raw_jobs)} SDE postings")
            return raw_jobs

        except Exception as e:
            logger.error(f"[Amazon] Error fetching jobs: {e}")
            return []
        finally:
            if self._owns_client and client:
                await client.aclose()
