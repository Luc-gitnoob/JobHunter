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

1. [PRIORITY 1 - HIGHEST] Experience Level Match (CRITICAL HARD REQUIREMENT):
   The candidate started their career in Jan 2025 and has only {current_yoe:.1f} years of professional experience. Target roles requiring strictly <{max_allowed_yoe} YOE.
   If the job description requires or expects >={max_allowed_yoe} years of experience (e.g. 2+ YOE, 2-4 years, 3+ YOE, mid-level, experienced, or senior), you MUST reject this role by giving match_score < 40. Under no circumstances should a role requiring >={max_allowed_yoe} YOE score 70 or above. Only roles open to 0-1 YOE, fresh graduates, junior, associate, or early-career engineers should pass.

2. [PRIORITY 2 - HIGH] Company Pedigree & Compensation Benchmark (CRITICAL TIER REQUIREMENT):
   The candidate is an SDE at NAV Fund Services (high-scale fintech/distributed systems) targeting high-tier product engineering roles paying >= 18 LPA INR (or $25k+ USD for remote roles).
   - EXCELLENT FIT (Score boost): Direct product-based tech companies, high-growth VC-backed tech startups, unicorns, quantitative trading / hedge funds, high-scale fintechs, and tier-1 MNC engineering centers (archetypes include: Google, Uber, Atlassian, Microsoft, Stripe, Tower Research, D. E. Shaw, Amazon, Salesforce, Adobe, PhonePe, Flipkart, Razorpay, CRED, Swiggy, Groww, Juspay, Intuit, ServiceNow, OCI, BrowserStack, Zepto, Meta, Databricks, Rubrik, Cloudflare, NVIDIA, Cohesity, Nutanix, Arcesium, Goldman Sachs, Morgan Stanley, PayPal, Twilio, Postman, Zeta, Zomato, Meesho, Sprinklr, Walmart Global Tech, Tekion, Coinbase, or ANY startup/company with a similar high-bar product engineering culture paying >= 18 LPA).
   - REJECT / STRICT PENALTY (match_score < 45): Mass IT service consultancies, third-party recruitment agencies, staffing bodyshops, outsourcing vendors, non-tech publications/media companies, or small IT shops that typically pay below 18 LPA (e.g. 3-10 LPA). Even if technical keywords match (e.g. Java, Python, Go), reject the role with match_score < 45 if the employer is not a high-paying product company.

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
            required_keys = [
                "match_score", "matching_skills", "skill_gaps",
                "resume_emphasis", "summary"
            ]
            for key in required_keys:
                if key not in result:
                    logger.warning(f"LLM response missing key: {key}")
                    return None

            # Ensure score is an integer
            result["match_score"] = int(result["match_score"])

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

            logger.info(
                f"[Scorer] {company} — {title}: score={result['match_score']}"
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
