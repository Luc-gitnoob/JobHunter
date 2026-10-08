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
    Extrapolates dynamically from career start date:
    - Started Jan 2025:
      - Throughout 2025 (0 to <1 YOE): strict limit is <1 YOE (max_allowed = 1)
      - Throughout 2026 (1 to <2 YOE): strict limit is <2 YOE (max_allowed = 2)
      - Throughout 2027 (2 to <3 YOE): strict limit is <3 YOE (max_allowed = 3)
      - Dynamically extrapolates each year from career_start_date without manual edits.
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

        # 2. Title must NOT contain any exclude keywords (using delimiter boundaries to prevent substring collisions)
        for kw in self.title_exclude:
            pat = r"(?:^|[\s\-_,.:/()|])" + re.escape(kw) + r"(?:$|[\s\-_,.:/()|])"
            if re.search(pat, title_lower):
                return False, f"Title '{job.title}' matches exclude keyword: '{kw}'"

        # 3. Title YOE check — reject senior roles specifying YOE in title (e.g. "Software Engineer (3+ YOE)")
        if self.max_yoe and job.title:
            is_disqualified, reason = self.check_disqualifying_yoe(job.title, self.max_yoe)
            if is_disqualified:
                return False, f"Title mentions disqualifying experience: {reason} (candidate upper bound: <{self.max_yoe} YOE)"

        # 4. Geographic filter (if provided per-company)
        if geo_filter and job.location:
            location_lower = job.location.lower()
            is_geo_match = any(geo.lower() in location_lower for geo in geo_filter)
            if not is_geo_match and "remote" in location_lower:
                is_geo_match = True
            if not is_geo_match and any(g.lower() == "india" for g in geo_filter):
                indian_hubs = {"bangalore", "bengaluru", "hyderabad", "pune", "gurgaon", "gurugram", "noida", "delhi", "mumbai", "chennai"}
                if any(hub in location_lower for hub in indian_hubs):
                    is_geo_match = True
            if not is_geo_match:
                return False, f"Location '{job.location}' not in geo filter: {geo_filter}"

        # 5. YOE check — strict hard requirement (< max_yoe)
        if self.max_yoe and job.description:
            is_disqualified, reason = self.check_disqualifying_yoe(job.description, self.max_yoe)
            if is_disqualified:
                return False, f"JD exceeds candidate experience threshold: {reason} (candidate upper bound: <{self.max_yoe} YOE)"

        return True, None

    def check_disqualifying_yoe(self, description: str, max_allowed: int) -> tuple[bool, Optional[str]]:
        """
        Check if the job description mentions any experience requirement that is
        disqualifying (>= max_allowed YOE).
        Handles ranges ('2 to 6 years', '4–6 years'), complex phrases ('3+ years of non-internship professional...'),
        parenthetical plurals ('Year(s)'), and filters out false positives like company age ('founded 25 years ago').
        """
        if not description:
            return False, None

        # Normalize unicode dashes (en-dash, em-dash) and whitespace
        text = description.replace("–", "-").replace("—", "-")
        text = " " + re.sub(r"\s+", " ", text) + " "

        year_suffix = r"(?:years?|yrs?|year\(s\)|yr\(s\))"

        # 1. Check ranges: e.g. '2 to 6 years', '4-6 years', '3-5 yrs'
        range_pattern = rf"(\d+)\s*(?:-|to)\s*(\d+)\s*{year_suffix}\b"
        for m in re.finditer(range_pattern, text, re.IGNORECASE):
            lower = int(m.group(1))
            upper = int(m.group(2))
            after_snippet = text[m.end():m.end() + 25].lower()
            if "ago" in after_snippet or "old" in after_snippet:
                continue
            if lower >= max_allowed:
                return True, f"Requires {lower}-{upper} years experience"

        # 2. Mask valid junior ranges (e.g. '0-1 year', '0-2 years', '1-2 yrs') so they don't trigger standalone '2 years' checks
        cleaned_text = re.sub(
            rf"\b(?:0|1)\s*(?:-|to)\s*(?:1|2)\s*{year_suffix}\b",
            "JUNIOR_RANGE",
            text,
            flags=re.IGNORECASE,
        )

        # 3. Disqualifying standalone YOE patterns
        yoe_patterns = [
            # 'X+ years of [anything up to 5 words] (experience|engineering|development|building|coding|architecture)'
            rf"(\d+)\+?\s*{year_suffix}\b\s+(?:of\s+)?(?:[\w-]+\s+){{0,5}}(?:experience|exp|development|engineering|coding|building|architecture)",
            # 'X+ years in [anything up to 4 words] (roles|positions|domains|environments)'
            rf"(\d+)\+?\s*{year_suffix}\b\s+(?:in\s+)?(?:[\w-]+\s+){{0,4}}(?:roles?|positions?|domains?|environments?)",
            # 'X+ years (writing|developing|building|delivering|designing|architecting|working|operating)'
            rf"(\d+)\+?\s*{year_suffix}\b\s+(?:writing|developing|building|delivering|designing|architecting|working|operating)",
            # 'minimum / at least / must have X(+) years'
            rf"(?:minimum|at\s+least|must\s+have|requires?|looking\s+for)\s+(?:of\s+)?(\d+)\+?\s*{year_suffix}",
            # 'X+ years with / in [technology]'
            rf"(\d+)\+\s*{year_suffix}\b(?:\s+(?:in|with)\s+[\w#+.]+)?",
            # 'Experience: X+ years'
            rf"experience[:\s]+(?:of\s+)?(\d+)\+?\s*{year_suffix}",
            # 'X+ years (of) (overall) experience'
            rf"(\d+)\+?\s*{year_suffix}\b\s+(?:of\s+)?(?:overall\s+)?experience",
        ]

        for pat in yoe_patterns:
            for m in re.finditer(pat, cleaned_text, re.IGNORECASE):
                num = int(m.group(1))
                after_snippet = cleaned_text[m.end():m.end() + 25].lower()
                if "ago" in after_snippet or "old" in after_snippet:
                    continue
                if num >= max_allowed:
                    matched_snippet = m.group(0).strip()
                    return True, f"Requires {num}+ years ({matched_snippet})"

        return False, None

    def _extract_yoe(self, description: str) -> Optional[int]:
        """Backwards-compatible helper returning the disqualifying YOE found or None."""
        is_disqualified, reason = self.check_disqualifying_yoe(description, self.max_yoe or 2)
        if is_disqualified and reason:
            match = re.search(r"(\d+)", reason)
            if match:
                return int(match.group(1))
        return None
