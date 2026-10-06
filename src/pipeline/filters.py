"""Pre-filters to drop obvious non-matches before LLM scoring.

These filters are cheap (string matching) and run before the expensive
LLM call. The goal: eliminate noise so we only score high-potential jobs.
"""

import re
import logging
from datetime import datetime, timezone
from typing import Optional

from src.ingestion.base import RawJob

logger = logging.getLogger(__name__)


def calculate_yoe(start_date_str: str = "2025-01", as_of: Optional[datetime] = None) -> float:
    """
    Calculate candidate's exact years of professional experience
    from career start date (e.g. '2025-01') to as_of (default: current UTC date).
    """
    try:
        parts = [int(p) for p in start_date_str.split("-")]
        start_year = parts[0]
        start_month = parts[1] if len(parts) > 1 else 1
    except Exception:
        start_year, start_month = 2025, 1

    now = as_of or datetime.now(timezone.utc)
    months = (now.year - start_year) * 12 + (now.month - start_month)
    return max(0.0, months / 12.0)


def get_max_allowed_yoe(start_date_str: str = "2025-01", as_of: Optional[datetime] = None) -> int:
    """
    Compute the strict upper bound for YOE (< N years requirement).
    Rule:
    - Started Jan 2025:
      - Throughout 2025 (0 to <1 YOE): strict limit is <1 YOE (max_allowed = 1)
      - Throughout 2026 (1 to <2 YOE): strict limit is <2 YOE (max_allowed = 2)
      - Throughout 2027 (2 to <3 YOE): strict limit is <3 YOE (max_allowed = 3)
      - Dynamically updates each year without manual code changes.
    """
    yoe = calculate_yoe(start_date_str, as_of)
    return int(yoe) + 1


class PreFilter:
    """
    Rule-based pre-filter for job postings.
    Configured via config.yaml filter rules and profile.yaml career start date.
    """

    def __init__(self, config: dict, profile: dict = None):
        """
        Args:
            config: The 'filters' section from config.yaml
            profile: The candidate profile dict from profile.yaml
        """
        self.title_include = [
            t.lower() for t in config.get("title_include", [])
        ]
        self.title_exclude = [
            t.lower() for t in config.get("title_exclude", [])
        ]

        # Calculate dynamic YOE from candidate's career start date (Jan 2025)
        career_start = (profile or {}).get("career_start_date", "2025-01")
        self.candidate_yoe = calculate_yoe(career_start)
        self.max_yoe = get_max_allowed_yoe(career_start)

        # Allow explicit numeric override from config if provided
        configured_max = config.get("max_yoe_mentioned")
        if isinstance(configured_max, int) and configured_max > 0:
            self.max_yoe = configured_max

        logger.info(
            f"[PreFilter] Dynamic candidate experience: {self.candidate_yoe:.2f} yrs "
            f"(started {career_start}). Hard YOE upper bound: <{self.max_yoe} YOE."
        )

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

        # 4. YOE check — strict hard requirement (< max_yoe)
        if self.max_yoe and job.description:
            yoe_mentioned = self._extract_yoe(job.description)
            if yoe_mentioned is not None and yoe_mentioned >= self.max_yoe:
                return False, f"JD requires {yoe_mentioned}+ YOE (hard limit: <{self.max_yoe} YOE)"

        return True, None

    def _extract_yoe(self, description: str) -> Optional[int]:
        """
        Extract minimum years of experience from job description.
        Covers phrasing like: "2+ years developing", "3+ years building",
        "minimum 2 years", "2-4 years", "3+ yrs in Go", "must have 2+ years".
        """
        patterns = [
            # Explicit requirements markers: 'requires 3+ years', 'minimum 2 years', 'must have 3 years'
            r"(?:require[sd]?|minimum|at least|must have|looking for)[:\s]+(?:of\s+)?(\d+)\+?\s*(?:years?|yrs?)",
            # X+ years of / X+ years building / developing / writing / software / engineering / backend / full-stack
            r"(\d+)\+?\s*(?:years?|yrs?)\s*(?:of\s+)?(?:experience|exp|working|building|developing|writing|coding|software|engineering|backend|frontend|fullstack|full-stack|systems|industry|professional|production|relevant)",
            # X+ years with / in a technology: '2+ years with Python', '3+ years in Go'
            r"(\d+)\+?\s*(?:years?|yrs?)\s+(?:in|with)\s+[A-Za-z#+.]+",
            # X-Y years or X to Y years: '2-4 years', '3 to 5 years', '3-5 yrs'
            r"(\d+)\s*(?:-|to)\s*\d+\s*(?:years?|yrs?)",
            # X+ years or X+ yrs standalone (e.g. '2+ years', '3+ yrs')
            r"(\d+)\+\s*(?:years?|yrs?)",
            # 'minimum of X years', 'at least X years'
            r"(?:minimum|at least)\s+(?:of\s+)?(\d+)\s*(?:years?|yrs?)",
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
