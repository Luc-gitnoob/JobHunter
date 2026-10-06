"""LLM-driven resume tailoring — customizes bullet points and emphasis for each job."""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PROFILE_PATH = PROJECT_ROOT / "config" / "profile.yaml"


TAILOR_PROMPT = """You are an expert resume writer. Your job is to tailor a candidate's resume for a specific job posting.

## Candidate Profile (Ground Truth — DO NOT fabricate facts)
{profile_text}

## Target Job
**Company:** {company}
**Title:** {title}
**Job Description Summary:** {description}

## LLM Match Analysis
**Matching Skills:** {matching_skills}
**Skill Gaps:** {skill_gaps}
**Recommended Resume Emphasis:** {resume_emphasis}
**Recommended Keywords:** {keywords}

## Instructions
Rewrite the candidate's resume content to maximize relevance for this specific role.

Rules:
1. DO NOT fabricate experiences, skills, or achievements. Only rephrase what exists.
2. Reorder and rephrase bullet points to lead with the most relevant accomplishments.
3. Incorporate keywords from the JD naturally into existing bullet points.
4. Emphasize relevant technical skills and quantified achievements.
5. If a skill gap exists but the candidate has transferable experience, frame it positively.
6. Keep bullet points concise (1-2 lines each) and action-oriented.

Respond with ONLY a valid JSON object (no markdown, no code blocks):
{{
    "experience": [
        {{
            "company": "<company name>",
            "title": "<job title>",
            "duration": "<duration>",
            "stack": ["<tech1>", "<tech2>"],
            "highlights": ["<tailored bullet 1>", "<tailored bullet 2>", ...]
        }}
    ],
    "projects": [
        {{
            "name": "<project name>",
            "stack": ["<tech1>", "<tech2>"],
            "highlights": ["<tailored bullet 1>", ...]
        }}
    ],
    "skill_categories": [
        {{
            "name": "Languages",
            "skills": ["Go", "C#", "Java", "Python", "SQL"]
        }},
        {{
            "name": "Frameworks & Tools",
            "skills": [".NET 10", "Spring Boot", "Docker", "Kubernetes"]
        }},
        {{
            "name": "Infrastructure",
            "skills": ["Azure DevOps", "ELK Stack", "HashiCorp Vault"]
        }}
    ]
}}
"""


def _load_profile() -> dict:
    """Load candidate profile from YAML."""
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _format_profile_for_tailor(profile: dict) -> str:
    """Format profile for the tailor prompt."""
    lines = []
    for exp in profile.get("experience", []):
        lines.append(f"**{exp['title']} at {exp['company']}** ({exp['duration']})")
        lines.append(f"Stack: {', '.join(exp.get('stack', []))}")
        for h in exp.get("highlights", []):
            lines.append(f"• {h}")
        lines.append("")

    for proj in profile.get("projects", []):
        lines.append(f"**Project: {proj['name']}**")
        lines.append(f"Stack: {', '.join(proj.get('stack', []))}")
        for h in proj.get("highlights", []):
            lines.append(f"• {h}")
        lines.append("")

    for cp in profile.get("competitive_programming", []):
        lines.append(f"{cp['platform']} {cp['title']}: {cp['details']}")

    return "\n".join(lines)


class ResumeTailor:
    """
    Uses Gemini to rewrite resume content for a specific job posting.
    Does NOT fabricate — only rephrases and reorders existing content.
    """

    def __init__(self, config: dict):
        self.model_name = config.get("model", "gemini-2.0-flash")
        self.temperature = config.get("temperature", 0.3)
        self._profile = _load_profile()
        self._profile_text = _format_profile_for_tailor(self._profile)
        self._model = None

    def _get_model(self):
        if self._model is None:
            import google.generativeai as genai

            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key:
                raise ValueError("GEMINI_API_KEY not set")
            genai.configure(api_key=api_key)
            self._model = genai.GenerativeModel(self.model_name)
        return self._model

    async def tailor(
        self,
        company: str,
        title: str,
        description: str,
        match_analysis: dict,
    ) -> Optional[dict]:
        """
        Generate tailored resume content for a specific job.

        Args:
            company: Company name
            title: Job title
            description: Job description (plain text)
            match_analysis: Output from JobScorer (matching_skills, etc.)

        Returns:
            dict with tailored experience, projects, and skill_categories
        """
        prompt = TAILOR_PROMPT.format(
            profile_text=self._profile_text,
            company=company,
            title=title,
            description=description[:3000],
            matching_skills=", ".join(match_analysis.get("matching_skills", [])),
            skill_gaps=", ".join(match_analysis.get("skill_gaps", [])),
            resume_emphasis=", ".join(match_analysis.get("resume_emphasis", [])),
            keywords=", ".join(match_analysis.get("recommended_keywords", [])),
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

            result = json.loads(response.text)
            logger.info(f"[Tailor] Generated tailored resume for {company} — {title}")
            return result

        except Exception as e:
            logger.error(f"[Tailor] Error tailoring for {company}/{title}: {e}")
            return None

    def build_template_context(self, tailored: dict) -> dict:
        """
        Merge tailored content with static profile data to create
        the full template context for LaTeX rendering.
        """
        profile = self._profile

        context = {
            "name": profile["name"],
            "email": profile["email"],
            "phone": profile["phone"],
            "linkedin": profile.get("linkedin", ""),
            "github": profile.get("github", ""),
            "portfolio": profile.get("portfolio", ""),
            "education": profile.get("education", []),
            "experience": tailored.get("experience", profile.get("experience", [])),
            "projects": tailored.get("projects", profile.get("projects", [])),
            "competitive_programming": profile.get("competitive_programming", []),
            "core_skills": profile.get("core_skills", []),
            "skill_categories": tailored.get("skill_categories", None),
        }

        return context
