"""LLM-based job match scorer using Google Gemini API."""

import asyncio
import json
import logging
import os
from typing import Optional

import yaml
from pathlib import Path

logger = logging.getLogger(__name__)

# Load candidate profile once at module level
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PROFILE_PATH = PROJECT_ROOT / "config" / "profile.yaml"


def _load_profile() -> dict:
    """Load candidate profile from YAML."""
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


SCORING_PROMPT = """You are an expert technical recruiter evaluating job fit.

## Candidate Profile
{profile_text}

## Job Posting
**Company:** {company}
**Title:** {title}
**Location:** {location}
**Department:** {department}

**Job Description:**
{description}

## Your Task
Evaluate how well this candidate matches this job posting in the following strict priority order (Priority 1 > Priority 2 > Priority 3 > Priority 4):

1. [PRIORITY 1 - HIGHEST] Experience Level & Role Seniority Match (CRITICAL HARD REQUIREMENT):
   The candidate started their career in Jan 2025 and has only {current_yoe:.1f} years of professional experience. Target roles requiring strictly <{max_allowed_yoe} YOE.
   - REJECT MID/SENIOR ROLES (match_score < 40): If the role or JD targets SDE-2 / SDE II / SWE II, mid-level, experienced, or strictly requires >={max_allowed_yoe} years of experience (e.g. 2+ YOE, 2-4 years, 3+ YOE), you MUST reject this role by giving match_score < 40. DO NOT grant exceptions or score boosts for competitive programming, fintech pedigree, or technical competence if the role is SDE-2 or requires >={max_allowed_yoe} YOE.
   - REJECT DEGREE MISMATCH (match_score < 30): The candidate holds a B.Tech degree (NOT a PhD, Master's, or Doctorate). If the job description strictly requires a PhD or Doctorate, you MUST reject this role with match_score < 30.
   - REJECT FUTURE GRADUATION BATCHES (match_score < 30): The candidate is an active industry professional who graduated in 2025. If the role targets future college batches (e.g. 2026/2027/2028 campus hires or interns), reject with match_score < 30.
   - ONLY EARLY CAREER PASSES: Roles open to 0-1 YOE, fresh graduates, junior brackets (0-2 years, 1-3 years where the minimum required experience is <{max_allowed_yoe} YOE), associate, or early-career engineers should pass.

2. [PRIORITY 2 - HIGH] Company Pedigree & Role Type Benchmark (CRITICAL TIER REQUIREMENT):
   The candidate is an SDE at NAV Fund Services targeting high-tier product engineering roles paying >= 18 LPA INR (or $25k+ USD for remote roles).
   - REJECT NON-ENGINEERING / OPERATIONS (match_score < 30): The candidate is strictly seeking core software engineering, backend, platform, or distributed systems roles. Strictly REJECT non-engineering roles such as Risk Management, AML, Compliance, Operations, Fraud Analyst, or Business Analysis with match_score < 30, even if posted by a top-tier fintech.
   - REJECT CONTRACTOR NETWORKS & STAFFING (match_score < 40): Freelancer networks (e.g. Flexiple, Toptal), IT service consultancies, bodyshops, or staffing agencies must be rejected with match_score < 40.
   - EXCELLENT FIT (Score boost): Direct product-based tech companies, high-growth VC-backed tech startups, unicorns, quantitative trading / hedge funds, high-scale fintechs, and tier-1 MNC engineering centers (archetypes include: Google, Uber, Atlassian, Microsoft, Stripe, Tower Research, D. E. Shaw, Amazon, Salesforce, Adobe, PhonePe, Flipkart, Razorpay, CRED, Swiggy, Groww, Juspay, Intuit, ServiceNow, OCI, BrowserStack, Zepto, Meta, Databricks, Rubrik, Cloudflare, NVIDIA, Cohesity, Nutanix, Arcesium, Goldman Sachs, Morgan Stanley, PayPal, Twilio, Postman, Zeta, Zomato, Meesho, Sprinklr, Walmart Global Tech, Tekion, Coinbase, or ANY startup/company with a similar high-bar product engineering culture paying >= 18 LPA).

3. [PRIORITY 3 - MEDIUM] Technical Stack Alignment:
   Alignment with candidate core technologies: Golang, C#/.NET, Java/Spring Boot, Python, SQL, Docker, Kubernetes, microservices, Kafka, Redis, and distributed systems.

4. [PRIORITY 4 - LOWEST] Domain Relevance:
   Relevance to fintech, backend systems, high-throughput distributed systems, or high-scale web platforms.

Respond with ONLY a valid JSON object (no markdown, no code blocks):
{{
    "match_score": <integer 0-100>,
    "matching_skills": [<list of candidate skills that match the JD>],
    "summary": "<1-2 line concise summary of why this is or isn't a good fit, covering YOE, compensation/tier, and tech fit>"
}}
"""


def _format_profile(profile: dict) -> str:
    """Format the candidate profile into a readable text block for the prompt."""
    lines = [f"**Name:** {profile['name']}"]

    # Education
    for edu in profile.get("education", []):
        lines.append(
            f"**Education:** {edu['degree']} from {edu['institution']} "
            f"({edu['duration']}), CGPA: {edu['cgpa']}"
        )

    # Experience
    for exp in profile.get("experience", []):
        lines.append(f"\n**{exp['title']} at {exp['company']}** ({exp['duration']})")
        lines.append(f"  Stack: {', '.join(exp.get('stack', []))}")
        for h in exp.get("highlights", []):
            lines.append(f"  • {h}")

    # Projects
    for proj in profile.get("projects", []):
        lines.append(f"\n**Project: {proj['name']}**")
        lines.append(f"  Stack: {', '.join(proj.get('stack', []))}")
        for h in proj.get("highlights", []):
            lines.append(f"  • {h}")

    # Competitive programming
    for cp in profile.get("competitive_programming", []):
        lines.append(f"**{cp['platform']}:** {cp['title']} — {cp['details']}")

    # Core skills
    lines.append(f"\n**Core Skills:** {', '.join(profile.get('core_skills', []))}")

    return "\n".join(lines)


class JobScorer:
    """
    Uses Google Gemini API to score job-candidate match quality.
    Returns structured JSON with score, analysis, and resume guidance.
    """

    def __init__(self, config: dict):
        """
        Args:
            config: The 'scoring' section from config.yaml
        """
        self.model_name = config.get("model", "gemini-2.0-flash")
        self.temperature = config.get("temperature", 0.3)
        self.min_score = config.get("min_score", 70)
        self._profile = _load_profile()
        self._profile_text = _format_profile(self._profile)
        career_start = self._profile.get("career_start_date", "2025-01")
        from src.pipeline.filters import calculate_yoe, get_max_allowed_yoe
        self.candidate_yoe = calculate_yoe(career_start)
        self.max_allowed_yoe = get_max_allowed_yoe(career_start)
        self._model = None
        self.quota_exhausted = False

    def _get_model(self):
        """Lazy-initialize the Gemini model."""
        if self._model is None:
            import google.generativeai as genai

            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key:
                raise ValueError(
                    "GEMINI_API_KEY environment variable not set. "
                    "Get one from https://aistudio.google.com/app/apikey"
                )
            genai.configure(api_key=api_key)
            self._model = genai.GenerativeModel(self.model_name)
        return self._model

    async def score_job(
        self,
        company: str,
        title: str,
        location: str,
        department: str,
        description: str,
    ) -> Optional[dict]:
        """
        Score a job posting against the candidate profile.

        Returns:
            dict with keys: match_score, matching_skills, skill_gaps,
                           resume_emphasis, summary, recommended_keywords
            or None if scoring fails
        """
        if self.quota_exhausted:
            return None

        prompt = SCORING_PROMPT.format(
            profile_text=self._profile_text,
            company=company,
            title=title,
            location=location or "Not specified",
            department=department or "Not specified",
            description=description[:4000],  # Truncate very long JDs
            current_yoe=self.candidate_yoe,
            max_allowed_yoe=self.max_allowed_yoe,
        )

        try:
            model = self._get_model()
            response = await asyncio.to_thread(
                model.generate_content,
                prompt,
                generation_config={
                    "temperature": self.temperature,
                    "response_mime_type": "application/json",
                },
            )

            # Parse the JSON response
            result = json.loads(response.text)

            # Validate the response has required fields
            required_keys = ["match_score", "matching_skills", "summary"]
            for key in required_keys:
                if key not in result:
                    logger.warning(f"LLM response missing key: {key}")
                    return None

            result.setdefault("skill_gaps", [])
            result.setdefault("resume_emphasis", [])

            # Ensure score is an integer
            result["match_score"] = int(float(result["match_score"]))

            # Programmatic safety valve: if JD mentions >= max_allowed_yoe, clamp score below threshold
            if result["match_score"] >= self.min_score:
                from src.pipeline.filters import PreFilter
                is_disqualified, reason = PreFilter(config={}).check_disqualifying_yoe(description, self.max_allowed_yoe)
                if is_disqualified:
                    logger.warning(
                        f"  [Scorer Safety] Clamping score for {company} — {title}: "
                        f"{reason} (hard limit <{self.max_allowed_yoe} YOE)."
                    )
                    result["match_score"] = 30
                    result["summary"] = (
                        f"Automatically rejected: {reason}. "
                        f"Candidate has {self.candidate_yoe:.1f} YOE (hard limit <{self.max_allowed_yoe} YOE)."
                    )

                # Safety clamp: if summary or title indicates SDE-2, PhD, future batch, or non-SWE compliance
                summary_lower = result.get("summary", "").lower()
                title_lower = title.lower()
                disqualify_indicators = [
                    "sde ii", "sde 2", "sde-ii", "sde-2", "swe ii", "swe 2",
                    "phd", "ph.d", "doctorate",
                    "risk management", "aml", "sanctions", "compliance analyst",
                    "2027 graduate", "2027 campus", "2028 graduate", "2028 campus"
                ]
                for ind in disqualify_indicators:
                    if ind in summary_lower or ind in title_lower:
                        logger.warning(
                            f"  [Scorer Safety] Clamping score for {company} — {title}: "
                            f"Detected disqualified archetype ('{ind}')."
                        )
                        result["match_score"] = 30
                        break

            logger.info(
                f"[Scorer] {company} — {title}: score={result['match_score']}/100 | {result.get('summary', '')}"
            )
            return result

        except json.JSONDecodeError as e:
            logger.error(f"[Scorer] Failed to parse LLM response as JSON: {e}")
            return None
        except Exception as e:
            err_msg = str(e)
            if "429" in err_msg or "quota" in err_msg.lower() or "ResourceExhausted" in err_msg:
                self.quota_exhausted = True
                logger.error(
                    f"[Scorer] Quota exceeded for model '{self.model_name}': {e}. "
                    f"Halting scoring to prevent redundant API calls."
                )
            else:
                logger.error(f"[Scorer] Error scoring {company}/{title}: {e}")
            return None
