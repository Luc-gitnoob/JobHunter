"""Lever Postings API poller."""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from src.ingestion.base import JobSource, RawJob

logger = logging.getLogger(__name__)

# Lever public Postings API — no auth required
LEVER_API_BASE = "https://api.lever.co/v0/postings/{slug}"


class LeverSource(JobSource):
    """
    Polls the Lever Postings API.

    Endpoint: GET /v0/postings/{company-slug}
    - No authentication required
    - Returns JSON array of all public postings
    - Supports CORS for browser-based access
    """

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._client = client

    @property
    def source_name(self) -> str:
        return "lever"

    async def fetch_jobs(self, company_name: str, slug: str, **kwargs) -> list[RawJob]:
        client = self._client or httpx.AsyncClient(timeout=30.0)
        should_close = self._client is None

        try:
            url = LEVER_API_BASE.format(slug=slug)
            response = await client.get(url)
            response.raise_for_status()

            jobs_data = response.json()

            # Lever returns a flat list (not wrapped in an object)
            if not isinstance(jobs_data, list):
                jobs_data = []

            logger.info(
                f"[Lever] {company_name} ({slug}): found {len(jobs_data)} postings"
            )

            raw_jobs = []
            for job in jobs_data:
                try:
                    raw_jobs.append(self._parse_job(job, company_name))
                except Exception as e:
                    logger.warning(
                        f"[Lever] Failed to parse job {job.get('id')}: {e}"
                    )

            return raw_jobs

        except httpx.HTTPStatusError as e:
            logger.error(
                f"[Lever] HTTP {e.response.status_code} for {company_name} ({slug})"
            )
            return []
        except Exception as e:
            logger.error(f"[Lever] Error fetching {company_name}: {e}")
            return []
        finally:
            if should_close:
                await client.aclose()

    def _parse_job(self, data: dict, company_name: str) -> RawJob:
        """Parse a single Lever posting object into RawJob."""
        # Location
        location = data.get("categories", {}).get("location", "")

        # Department / Team
        department = data.get("categories", {}).get("team", "")

        # Posted date (Lever uses millisecond timestamps)
        posted_at = None
        created_at_ms = data.get("createdAt")
        if created_at_ms:
            try:
                posted_at = datetime.fromtimestamp(
                    created_at_ms / 1000, tz=timezone.utc
                )
            except (ValueError, TypeError, OSError):
                pass

        # Description — Lever puts it in descriptionPlain or lists
        description_parts = []
        desc_plain = data.get("descriptionPlain", "")
        if desc_plain:
            description_parts.append(desc_plain)

        # Also grab additional fields from the `lists` array
        for lst in data.get("lists", []):
            list_text = lst.get("text", "")
            list_content = lst.get("content", "")
            if list_text:
                description_parts.append(f"\n{list_text}")
            if list_content:
                # Content is HTML; keep it for now (we strip later in normalizer)
                description_parts.append(list_content)

        description = "\n".join(description_parts)

        # Apply URL
        apply_url = data.get("applyUrl") or data.get("hostedUrl", "")

        return RawJob(
            external_id=str(data["id"]),
            source="lever",
            company_name=company_name,
            title=data.get("text", "Unknown"),
            location=location,
            department=department,
            description=description,
            apply_url=apply_url,
            posted_at=posted_at,
            raw_data=data,
        )
