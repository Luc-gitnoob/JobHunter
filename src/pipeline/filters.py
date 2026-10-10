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


# Canonical city aliases for interchangeable ATS location matching
CITY_ALIASES = {
    "bangalore": {"bangalore", "bengaluru"},
    "bengaluru": {"bangalore", "bengaluru"},
    "gurgaon": {"gurgaon", "gurugram"},
    "gurugram": {"gurgaon", "gurugram"},
    "mumbai": {"mumbai", "bombay"},
    "bombay": {"mumbai", "bombay"},
    "chennai": {"chennai", "madras"},
    "madras": {"chennai", "madras"},
    "kolkata": {"kolkata", "calcutta"},
    "calcutta": {"kolkata", "calcutta"},
    "delhi": {"delhi", "new delhi", "ncr", "delhi ncr"},
    "new delhi": {"delhi", "new delhi", "ncr", "delhi ncr"},
}

INDIAN_TECH_HUBS = {
    "bangalore", "bengaluru", "hyderabad", "pune", "gurgaon", "gurugram",
    "noida", "delhi", "new delhi", "mumbai", "chennai", "kolkata", "ahmedabad", "kochi",
}

NON_INDIA_REGIONS = [
    "united states", "usa", "u.s.", "u.s.a.", "us",
    "canada", "united kingdom", "uk", "u.k.", "london",
    "germany", "berlin", "france", "paris", "netherlands", "amsterdam",
    "ireland", "dublin", "poland", "spain", "madrid", "barcelona",
    "australia", "sydney", "melbourne", "brazil", "sao paulo",
    "mexico", "singapore", "japan", "tokyo", "emea", "latam",
    "americas", "north america", "europe", "san francisco", "new york", "seattle",
]


class PreFilter:
    """
    Rule-based pre-filter for job postings.
    Configured via config.yaml filter rules and profile.yaml career start date.
    """

    def __init__(self, config: dict, profile: dict = None, curated_companies: list[str] = None):
        """
        Args:
            config: The 'filters' section from config.yaml
            profile: The candidate profile dict from profile.yaml
            curated_companies: List of direct target company names to bypass agency filtering
        """
        self.title_include = [
            t.lower() for t in config.get("title_include", [])
        ]
        self.title_exclude = [
            t.lower() for t in config.get("title_exclude", [])
        ]

        # Precompile word-bounded regex patterns for title matching
        # Uses boundary checks to eliminate substring collisions on short tokens like \b(sr|vp|sre|qa)\b
        self.include_patterns = [
            (kw, re.compile(rf"(?<![a-zA-Z0-9]){re.escape(kw.lower())}(?![a-zA-Z0-9])", re.IGNORECASE))
            for kw in config.get("title_include", [])
        ]
        self.exclude_patterns = [
            (kw, re.compile(rf"(?<![a-zA-Z0-9]){re.escape(kw.lower())}(?![a-zA-Z0-9])", re.IGNORECASE))
            for kw in config.get("title_exclude", [])
        ]

        # Curated direct target companies (bypass company_exclude agency filter)
        self.curated_companies = [c.lower() for c in (curated_companies or [])]

        # Calculate dynamic YOE from candidate's career start date (Jan 2025)
        career_start = (profile or {}).get("career_start_date", "2025-01")
        self.candidate_yoe = calculate_yoe(career_start)
        self.max_yoe = get_max_allowed_yoe(career_start)

        self.company_exclude = [
            c.lower() for c in config.get("company_exclude", [])
        ]

        # Minimum salary benchmark (in LPA INR)
        self.min_salary_lpa = float(config.get("min_salary_lpa", 18.0))

        # Allow explicit numeric override from config if provided
        configured_max = config.get("max_yoe_mentioned")
        if isinstance(configured_max, int) and configured_max > 0:
            self.max_yoe = configured_max

        logger.info(
            f"[PreFilter] Dynamic candidate experience: {self.candidate_yoe:.2f} yrs "
            f"(started {career_start}). Hard YOE upper bound: <{self.max_yoe} YOE. "
            f"Min compensation benchmark: >={self.min_salary_lpa:.0f} LPA."
        )

    def apply(self, job: RawJob, geo_filter: list[str] = None) -> tuple[bool, Optional[str]]:
        """
        Apply all filter rules to a job.

        Returns:
            (passes, reason) — passes=True if job should continue in pipeline,
                                reason=str if it was filtered out
        """
        title_lower = job.title.lower()

        # 1. Title must contain at least one include keyword (word-bounded: e.g. \bmts\b, \bswe\b)
        if self.include_patterns:
            matched_include = any(pat.search(title_lower) for _, pat in self.include_patterns)
            if not matched_include:
                return False, f"Title '{job.title}' doesn't match any include keywords"

        # 2. Title must NOT contain any exclude keywords (word-bounded: e.g. \b(sr|vp|sre|qa)\b)
        for kw, pat in self.exclude_patterns:
            if kw == "staff" and "member of technical staff" in title_lower:
                continue
            if pat.search(title_lower):
                return False, f"Title '{job.title}' matches exclude keyword: '{kw}'"

        # 3. Title YOE check — reject senior roles specifying YOE in title (e.g. "Software Engineer (3+ YOE)")
        if self.max_yoe and job.title:
            is_disqualified, reason = self.check_disqualifying_yoe(job.title, self.max_yoe)
            if is_disqualified:
                return False, f"Title mentions disqualifying experience: {reason} (candidate upper bound: <{self.max_yoe} YOE)"

        # 4. Geographic filter (if provided per-company)
        if geo_filter and job.location:
            location_lower = job.location.lower()

            # Expand target geo_filter with interchangeable city aliases
            expanded_geos = set()
            for g in geo_filter:
                g_str = g.strip().lower()
                expanded_geos.add(g_str)
                if g_str in CITY_ALIASES:
                    expanded_geos.update(CITY_ALIASES[g_str])

            is_geo_match = False

            # Check direct city or region match (excluding remote for dedicated handling)
            for target_geo in expanded_geos:
                if target_geo != "remote" and target_geo in location_lower:
                    is_geo_match = True
                    break

            # If India is in the filter, any Indian tech hub matches
            if not is_geo_match and "india" in expanded_geos:
                if "india" in location_lower or any(hub in location_lower for hub in INDIAN_TECH_HUBS):
                    is_geo_match = True

            # Remote matching and sanitization
            if not is_geo_match and "remote" in location_lower:
                # Disqualify remote roles restricted to non-India locations (e.g. Remote - US, Remote (UK), etc.)
                has_non_india_restriction = any(
                    re.search(rf"\b{re.escape(reg)}\b", location_lower)
                    for reg in NON_INDIA_REGIONS
                )
                has_explicit_india = "india" in location_lower or any(hub in location_lower for hub in INDIAN_TECH_HUBS)

                if has_explicit_india or (not has_non_india_restriction and "remote" in expanded_geos):
                    is_geo_match = True

            if not is_geo_match:
                return False, f"Location '{job.location}' not in geo filter: {geo_filter}"

        # 5. Company / Staffing Agency filter (strictly for uncurated third-party scrapers)
        # Direct ATS catalog targets (e.g. Honeywell Technology Solutions, Amazon Web Services, Uber India Systems Private Limited)
        # must bypass company_exclude keywords.
        raw_data = getattr(job, "raw_data", None) or {}
        is_curated_ats = (
            raw_data.get("_is_curated", False)
            or getattr(job, "source", "") in ["greenhouse", "lever", "ashby", "smartrecruiters", "amazon"]
            or any(
                c_name.lower() in (job.company_name or "").lower()
                for c_name in self.curated_companies
            )
        )
        if not is_curated_ats:
            company_lower = (job.company_name or "").lower()
            for kw in self.company_exclude:
                pat = r"(?:^|[\s\-_,.:/()|])" + re.escape(kw) + r"(?:$|[\s\-_,.:/()|])"
                if re.search(pat, company_lower):
                    return False, f"Company '{job.company_name}' matches excluded agency keyword: '{kw}'"

            # Check description for third-party staffing indicators
            if job.description:
                desc_lower = job.description.lower()
                agency_phrases = [
                    "hiring for our client",
                    "hiring for client",
                    "our client is looking for",
                    "client of ",
                    "recruiting on behalf of",
                    "staffing partner",
                    "third party payroll",
                    "contract to hire",
                    "c2h role",
                ]
                for phrase in agency_phrases:
                    if phrase in desc_lower:
                        return False, f"Staffing agency indicator detected in JD: '{phrase}'"

        # 6. YOE check — strict hard requirement (< max_yoe)
        if self.max_yoe and job.description:
            is_disqualified, reason = self.check_disqualifying_yoe(job.description, self.max_yoe)
            if is_disqualified:
                return False, f"JD exceeds candidate experience threshold: {reason} (candidate upper bound: <{self.max_yoe} YOE)"

        # 7. Minimum compensation check (unlisted salaries pass through)
        # Only drop a posting if salary_max is explicitly parsed and strictly less than 18 LPA
        if self.min_salary_lpa:
            salary_max = getattr(job, "salary_max", None) or raw_data.get("salary_max")
            if salary_max is not None:
                try:
                    val = float(salary_max)
                    if 0 < val < self.min_salary_lpa:
                        return False, f"Parsed salary_max {val:.1f} LPA is below {self.min_salary_lpa:.0f} LPA"
                except (ValueError, TypeError):
                    pass

            if job.description:
                is_low_pay, reason = self.check_low_salary(job.description, self.min_salary_lpa)
                if is_low_pay:
                    return False, f"Compensation below target tier: {reason}"

        return True, None

    def check_low_salary(self, description: str, min_lpa: float = 18.0) -> tuple[bool, Optional[str]]:
        """
        Check if the job description explicitly mentions a compensation package below min_lpa.
        If salary is unmentioned, passes through (over 85% of Tier-1 Indian tech roles do not list salary).
        """
        if not description:
            return False, None

        # 1. Explicit salary ranges in LPA / Lacs / Lakhs:
        # Require explicit compensation / CTC context or explicit 'LPA' unit
        # e.g. 'CTC: 6-10 LPA', 'Salary: 8 to 12 Lacs', '10 - 14 LPA'
        # Does NOT match bare '10-20 lacs users' or '5-10 lakhs records'
        range_patterns = [
            r"(?:ctc|salary|package|compensation|stipend|pay|remuneration)\s*(?:is|of|:|-|\b)\s*(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*(?:lpa|lacs?|lakhs?|inr\s*lpa)\b",
            r"\b(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*(?:lpa|inr\s*lpa)\b",
        ]
        for pat in range_patterns:
            for m in re.finditer(pat, description, re.IGNORECASE):
                upper = float(m.group(2))
                if upper < min_lpa:
                    return True, f"Listed range '{m.group(0).strip()}' is below {min_lpa:.0f} LPA"

        # 2. Standalone explicit salary statements:
        # Require explicit compensation keyword before the number
        # e.g. 'CTC: 12 LPA', 'Salary: 10 Lacs', 'Package: 15 LPA'
        single_pattern = (
            r"(?:ctc|salary|package|compensation|remuneration)\s*(?:is|of|:|-|\b)\s*"
            r"(\d+(?:\.\d+)?)\s*(?:lpa|lacs?|lakhs?|inr\s*lpa)\b"
        )
        for m in re.finditer(single_pattern, description, re.IGNORECASE):
            val = float(m.group(1))
            if val < min_lpa:
                return True, f"Listed package '{m.group(0).strip()}' is below {min_lpa:.0f} LPA"

        return False, None

    def check_disqualifying_yoe(self, description: str, max_allowed: int) -> tuple[bool, Optional[str]]:
        """
        Check if the job description mentions any experience requirement that is
        disqualifying (>= max_allowed YOE).
        Handles ranges ('2 to 6 years', '4–6 years'), complex phrases ('3+ years of non-internship professional...'),
        parenthetical plurals ('Year(s)'), and filters out false positives like company age ('founded 25 years ago').

        Evaluates against lower bound of YOE (min_yoe):
        Listings stating '0-2 years' or '1-3 years' are preserved because their lower bound is within
        early-career brackets.
        """
        if not description:
            return False, None

        # Normalize unicode dashes (en-dash, em-dash) and whitespace
        text = description.replace("–", "-").replace("—", "-")
        text = " " + re.sub(r"\s+", " ", text) + " "

        year_suffix = r"(?:years?|yrs?|year\(s\)|yr\(s\))"

        # 1. Check ranges: e.g. '0-2 years', '1-3 years', '3-5 years'
        range_pattern = rf"(\d+)\s*(?:-|to)\s*(\d+)\s*{year_suffix}\b"
        for m in re.finditer(range_pattern, text, re.IGNORECASE):
            lower = int(m.group(1))
            upper = int(m.group(2))
            after_snippet = text[m.end():m.end() + 25].lower()
            if "ago" in after_snippet or "old" in after_snippet:
                continue
            # If the minimum required experience is >= max_allowed, candidate is disqualified
            if lower >= max_allowed:
                return True, f"Requires minimum {lower} years experience ({lower}-{upper} yrs)"

        # 2. Mask ALL ranges where lower bound is within candidate bracket (< max_allowed)
        # e.g. '0-2 years', '0-3 years', '1-3 years' -> masked so upper bound does not trigger standalone checks
        def mask_valid_range(m):
            lower = int(m.group(1))
            if lower < max_allowed:
                return "JUNIOR_RANGE"
            return m.group(0)

        cleaned_text = re.sub(range_pattern, mask_valid_range, text, flags=re.IGNORECASE)

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
