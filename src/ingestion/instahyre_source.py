"""Instahyre API-based job source with Cloudflare bypass and structured YOE filtering."""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup
from curl_cffi import requests

from src.ingestion.base import JobSource, RawJob

logger = logging.getLogger(__name__)

INSTAHYRE_SEARCH_URL = "https://www.instahyre.com/api/v1/job_search"
INSTAHYRE_DETAIL_URL = "https://www.instahyre.com/api/v1/employer_public_jobs/{job_id}"


class InstahyreSource(JobSource):
    """
    Ingests curated high-paying tech jobs directly from Instahyre's internal API.
    Uses curl_cffi with browser impersonation to seamlessly bypass Cloudflare.
    """

    def __init__(self, request_delay: float = 1.0):
        self.request_delay = request_delay

    @property
    def source_name(self) -> str:
        return "instahyre"

    async def fetch_jobs(self, company_name: str = "", slug: str = "", **kwargs) -> list[RawJob]:
        """
        Fetch jobs from Instahyre asynchronously.

        kwargs:
            - job_functions: str (e.g. "10" for Backend Development)
            - years: str (e.g. "1" for 1 YOE, "0" for entry level)
            - skills: list[str] or str (e.g. ["Golang", "Java", "Python"] or "Golang")
            - locations: list[str] or str (e.g. ["Bangalore", "Hyderabad", "Work From Home"])
            - results_wanted: int (total jobs to fetch, default 30)
            - max_yoe: int (optional, upper bound for workex_min)
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._fetch_jobs_sync, kwargs)

    def _fetch_jobs_sync(self, params: dict) -> list[RawJob]:
        """Synchronously query Instahyre API with rate limit pauses."""
        job_functions = params.get("job_functions", "10")  # Default 10 = Backend Development
        years = str(params.get("years", "1"))
        skills_param = params.get("skills")
        locations_param = params.get("locations")
        results_wanted = int(params.get("results_wanted", 30))
        max_yoe = params.get("max_yoe")

        if isinstance(skills_param, str):
            skill_queries = [skills_param]
        elif isinstance(skills_param, list):
            skill_queries = skills_param
        else:
            skill_queries = [None]

        session = requests.Session(impersonate="chrome120")
        collected_jobs: dict[str, RawJob] = {}  # Keyed by job_id to deduplicate across skill queries

        for skill in skill_queries:
            if len(collected_jobs) >= results_wanted:
                break

            query_params = {
                "limit": min(results_wanted, 50),
                "offset": 0,
            }
            if job_functions:
                query_params["job_functions"] = job_functions
            if years:
                query_params["years"] = years
            if skill:
                query_params["skills"] = skill
            if isinstance(locations_param, str):
                query_params["jobLocations"] = locations_param
            elif isinstance(locations_param, list) and locations_param:
                query_params["jobLocations"] = locations_param[0]

            query_label = f"skill={skill or 'all'}, YOE={years}, func={job_functions}"
            logger.info(f"[Instahyre] Querying jobs for {query_label}...")

            try:
                resp = session.get(INSTAHYRE_SEARCH_URL, params=query_params, timeout=15)
                if resp.status_code == 429:
                    logger.warning("[Instahyre] Rate limited (429). Pausing 4 seconds...")
                    time.sleep(4)
                    resp = session.get(INSTAHYRE_SEARCH_URL, params=query_params, timeout=15)

                if resp.status_code != 200:
                    logger.error(f"[Instahyre] Search returned HTTP {resp.status_code}: {resp.text[:200]}")
                    continue

                data = resp.json()
                objects = data.get("objects", [])
                logger.info(f"[Instahyre] Found {len(objects)} candidate postings for {query_label}")

                for obj in objects:
                    if len(collected_jobs) >= results_wanted:
                        break

                    job_id = str(obj.get("id"))
                    if not job_id or job_id in collected_jobs:
                        continue

                    raw_job = self._process_job_object(session, obj, max_yoe=max_yoe)
                    if raw_job:
                        collected_jobs[job_id] = raw_job

                    time.sleep(self.request_delay)

            except Exception as e:
                logger.error(f"[Instahyre] Error during search for {query_label}: {e}")

            time.sleep(self.request_delay)

        logger.info(f"[Instahyre] Completed ingestion. Fetched {len(collected_jobs)} unique postings.")
        return list(collected_jobs.values())

    def _process_job_object(self, session: requests.Session, obj: dict, max_yoe: Optional[int] = None) -> Optional[RawJob]:
        """Fetch full job details and construct a normalized RawJob."""
        job_id = str(obj.get("id"))
        title = obj.get("title") or obj.get("candidate_title") or "Software Engineer"
        employer = obj.get("employer") or {}
        company_name = employer.get("company_name", "Unknown Company")
        locations = obj.get("locations") or "India"
        public_url = obj.get("public_url") or f"https://www.instahyre.com/job-{job_id}/"
        keywords = obj.get("keywords") or []

        # Fetch full description and exact experience range
        detail_url = INSTAHYRE_DETAIL_URL.format(job_id=job_id)
        description_text = ""
        workex_min, workex_max = None, None

        try:
            det_resp = session.get(detail_url, timeout=12)
            if det_resp.status_code == 200:
                det_data = det_resp.json()
                workex_min = det_data.get("workex_min")
                workex_max = det_data.get("workex_max")

                # If explicit min experience is >= max_yoe, skip early
                if max_yoe and workex_min is not None and workex_min >= max_yoe:
                    logger.debug(f"  [Instahyre] Skipping {company_name} — {title} (requires {workex_min}+ YOE)")
                    return None

                raw_html = det_data.get("description") or ""
                if raw_html:
                    soup = BeautifulSoup(raw_html, "html.parser")
                    description_text = soup.get_text(separator="\n").strip()

        except Exception as e:
            logger.debug(f"  [Instahyre] Detail fetch failed for job {job_id}: {e}")

        # Fallback description if detail API was unparseable
        if not description_text:
            notes = employer.get("instahyre_note", "")
            description_text = (
                f"Role: {title} at {company_name}\n"
                f"Location: {locations}\n"
                f"Required Skills: {', '.join(keywords)}\n"
            )
            if notes:
                description_text += f"\nAbout Company: {notes}\n"

        # Prepend explicit experience summary so pipeline filters have clean context
        if workex_min is not None and workex_max is not None:
            description_text = f"Experience Required: {workex_min} to {workex_max} years.\n\n" + description_text

        return RawJob(
            external_id=job_id,
            source="instahyre",
            company_name=company_name,
            title=title,
            location=locations,
            department="Engineering",
            description=description_text,
            apply_url=public_url,
            posted_at=datetime.now(timezone.utc),
            raw_data={
                "job_id": job_id,
                "workex_min": workex_min,
                "workex_max": workex_max,
                "keywords": keywords,
                "employer": employer,
                "_geo_filter": ["India", "Remote"],
            },
        )
