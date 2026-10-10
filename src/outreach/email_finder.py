"""Automated referral candidate discovery via Hunter.io and Apollo.io APIs."""

import os
import logging
import httpx
from typing import Optional

logger = logging.getLogger(__name__)


async def find_referrers(company_name: str, domain: Optional[str] = None) -> list[dict]:
    """
    Search for engineers or recruiters at the target company using Hunter.io or Apollo.io.
    
    Returns a list of candidate dicts:
    [
        {
            "name": "First Last",
            "email": "first.last@company.com",
            "title": "Software Engineer",
            "source": "hunter" | "apollo"
        }
    ]
    """
    hunter_key = os.getenv("HUNTER_API_KEY")
    apollo_key = os.getenv("APOLLO_API_KEY")

    if not hunter_key and not apollo_key:
        logger.debug(
            f"[EmailFinder] Neither HUNTER_API_KEY nor APOLLO_API_KEY configured. "
            f"Skipping auto-discovery for {company_name}."
        )
        return []

    # 1. Try Hunter.io Domain Search
    if hunter_key:
        try:
            candidates = await _search_hunter(company_name, domain, hunter_key)
            if candidates:
                logger.info(f"[EmailFinder] Found {len(candidates)} contacts for {company_name} via Hunter.io")
                return candidates
        except Exception as e:
            logger.warning(f"[EmailFinder] Hunter.io lookup failed for {company_name}: {e}")

    # 2. Try Apollo.io People Search
    if apollo_key:
        try:
            candidates = await _search_apollo(company_name, domain, apollo_key)
            if candidates:
                logger.info(f"[EmailFinder] Found {len(candidates)} contacts for {company_name} via Apollo.io")
                return candidates
        except Exception as e:
            logger.warning(f"[EmailFinder] Apollo.io lookup failed for {company_name}: {e}")

    return []


async def _search_hunter(company_name: str, domain: Optional[str], api_key: str) -> list[dict]:
    """Query Hunter.io Domain Search API for engineering contacts."""
    url = "https://api.hunter.io/v2/domain-search"
    params = {
        "api_key": api_key,
        "department": "engineering",
        "limit": 5,
    }
    if domain:
        params["domain"] = domain
    else:
        params["company"] = company_name

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            logger.debug(f"[EmailFinder] Hunter.io returned status {resp.status_code}: {resp.text}")
            return []

        data = resp.json().get("data", {})
        raw_emails = data.get("emails", [])

        results = []
        for item in raw_emails:
            email = item.get("value")
            if not email:
                continue

            first_name = (item.get("first_name") or "").strip()
            last_name = (item.get("last_name") or "").strip()
            full_name = f"{first_name} {last_name}".strip() or "Engineer"
            title = item.get("position") or "Software Engineer"
            confidence = item.get("confidence", 0)

            # Skip low confidence emails (<50%)
            if confidence and confidence < 50:
                continue

            results.append({
                "name": full_name,
                "first_name": first_name or "there",
                "email": email,
                "title": title,
                "source": "hunter",
                "confidence": confidence,
            })

        return results


async def _search_apollo(company_name: str, domain: Optional[str], api_key: str) -> list[dict]:
    """Query Apollo.io Mixed People Search API for engineering contacts."""
    url = "https://api.apollo.io/v1/mixed_people/search"
    headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": api_key,
    }
    payload = {
        "api_key": api_key,
        "organization_names": [company_name],
        "person_titles": [
            "Software Engineer",
            "Software Development Engineer",
            "Engineering Manager",
            "Tech Lead",
            "Technical Recruiter",
        ],
        "page": 1,
        "per_page": 5,
    }
    if domain:
        payload["q_organization_domains"] = [domain]

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code != 200:
            logger.debug(f"[EmailFinder] Apollo.io returned status {resp.status_code}: {resp.text}")
            return []

        data = resp.json()
        people = data.get("people", [])

        results = []
        for person in people:
            email = person.get("email")
            if not email:
                continue

            first_name = (person.get("first_name") or "").strip()
            last_name = (person.get("last_name") or "").strip()
            full_name = f"{first_name} {last_name}".strip() or "Engineer"
            title = person.get("title") or "Software Engineer"

            results.append({
                "name": full_name,
                "first_name": first_name or "there",
                "email": email,
                "title": title,
                "source": "apollo",
            })

        return results
