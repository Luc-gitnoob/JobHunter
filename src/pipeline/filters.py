"""Pre-filters to drop obvious non-matches before LLM scoring.

These filters are cheap (string matching) and run before the expensive
LLM call. The goal: eliminate noise so we only score high-potential jobs.
"""

import re
import logging
from typing import Optional

from src.ingestion.base import RawJob

logger = logging.getLogger(__name__)


class PreFilter:
    """
    Rule-based pre-filter for job postings.
    Configured via config.yaml filter rules.
    """

    def __init__(self, config: dict):
        """
        Args:
            config: The 'filters' section from config.yaml
        """
        self.title_include = [
            t.lower() for t in config.get("title_include", [])
        ]
        self.title_exclude = [
            t.lower() for t in config.get("title_exclude", [])
        ]
        self.max_yoe = config.get("max_yoe_mentioned", 3)

    def apply(self, job: RawJob, geo_filter: list[str] = None) -> tuple[bool, Optional[str]]:
        """
        Apply all filter rules to a job.

        Returns:
            (passes, reason) — passes=True if job should continue in pipeline,
                                reason=str if it was filtered out
        """
        title_lower = job.title.lower()

        # 1. Title must contain at least one include keyword
        if self.title_include:
            if not any(kw in title_lower for kw in self.title_include):
                return False, f"Title '{job.title}' doesn't match any include keywords"

        # 2. Title must NOT contain any exclude keywords
        for kw in self.title_exclude:
            if kw in title_lower:
                return False, f"Title '{job.title}' matches exclude keyword: '{kw}'"

        # 3. Geographic filter (if provided per-company)
        if geo_filter and job.location:
            location_lower = job.location.lower()
            if not any(geo.lower() in location_lower for geo in geo_filter):
                # Also check for "remote" since remote jobs match any geo
                if "remote" not in location_lower:
                    return False, f"Location '{job.location}' not in geo filter: {geo_filter}"

        # 4. YOE check — look for "X+ years" patterns in description
        if self.max_yoe and job.description:
            yoe_mentioned = self._extract_yoe(job.description)
            if yoe_mentioned is not None and yoe_mentioned > self.max_yoe:
                return False, f"JD mentions {yoe_mentioned}+ YOE (max: {self.max_yoe})"

        return True, None

    def _extract_yoe(self, description: str) -> Optional[int]:
        """
        Extract minimum years of experience from job description.
        Looks for patterns like: "3+ years", "5-7 years experience"
        Returns the minimum number found, or None if no pattern found.
        """
        patterns = [
            r"(\d+)\+?\s*(?:years?|yrs?)\s*(?:of)?\s*(?:experience|exp)",
            r"(\d+)\s*-\s*\d+\s*(?:years?|yrs?)\s*(?:of)?\s*(?:experience|exp)",
            r"minimum\s*(?:of\s*)?(\d+)\s*(?:years?|yrs?)",
            r"at\s*least\s*(\d+)\s*(?:years?|yrs?)",
        ]

        min_yoe = None
        for pattern in patterns:
            matches = re.findall(pattern, description, re.IGNORECASE)
            for match in matches:
                try:
                    yoe = int(match)
                    if min_yoe is None or yoe < min_yoe:
                        min_yoe = yoe
                except ValueError:
                    continue

        return min_yoe
