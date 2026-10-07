"""LLM-driven resume tailoring — customizes bullet points, projects, and summary for each job."""

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


TAILOR_PROMPT = """You are an elite technical resume strategist for top-tier software engineers (FAANG / Tier-1 tech).
Your objective is to tailor the candidate's resume to maximize alignment and interview conversion for a specific target job.

## Candidate Ground Truth Profile
{profile_text}

## Target Job
**Company:** {company}
**Role Title:** {title}
**Job Description Summary:** {description}

## Role Match Insights
**Matching Skills:** {matching_skills}
**Skill Gaps:** {skill_gaps}
**Recommended Resume Emphasis:** {resume_emphasis}
**Target Keywords:** {keywords}

## Strict Tailoring Guidelines
1. **Never fabricate ungrounded facts** (do not invent fake companies, degrees, or unlisted core achievements).
2. **Professional Summary**: Write a sharp, high-impact 2-sentence summary tailored specifically to {company}'s requirements and tech stack.
3. **Google XYZ Formula**: Rewrite experience highlights using the formula: *"Accomplished [X] as measured by [Y], by doing [Z]"*. Lead with strong action verbs (Architected, Engineered, Optimized, Scaled, Streamlined) and include concrete technical metrics where possible.
4. **Keyword Integration**: Naturally incorporate target keywords from the JD (e.g., Azure, Golang, Kafka, Kubernetes, gRPC, Distributed Caching) into existing bullet points and stack descriptors.
5. **Project Selection**: Select the **2 most relevant projects** from the candidate's portfolio that best demonstrate competency for THIS role, and tailor their bullet points to emphasize relevant architecture and technologies.
6. **Technical Skills Categorization**: Organize skills into 3-4 clean categories (e.g., "Languages", "Backend & Distributed Systems", "Cloud & DevOps", "Databases & Tools") placing the job's most requested technologies first.
7. **Brevity & Density**: Keep each bullet to 1-2 lines. Maintain high information density suitable for a tight 1-page resume.

Respond with ONLY a valid JSON object (no markdown, no code blocks):
{{
    "summary": "<sharp 2-sentence tailored summary highlighting relevant tech stack & experience for this specific role>",
    "experience": [
        {{
            "company": "<company name>",
            "title": "<job title>",
            "duration": "<duration>",
            "stack": ["<tech1>", "<tech2>", "<tech3>"],
            "highlights": [
                "<tailored XYZ bullet 1>",
                "<tailored XYZ bullet 2>",
                "<tailored XYZ bullet 3>",
                "<tailored XYZ bullet 4>",
                "<tailored XYZ bullet 5>"
            ]
        }}
    ],
    "projects": [
        {{
            "name": "<chosen project name from profile>",
            "stack": ["<tech1>", "<tech2>", "<tech3>"],
            "highlights": [
                "<tailored XYZ bullet 1>",
                "<tailored XYZ bullet 2>"
            ]
        }}
    ],
    "skill_categories": [
        {{
            "name": "Languages",
            "skills": ["<lang1>", "<lang2>", "<lang3>"]
        }},
        {{
            "name": "Backend & Distributed Systems",
            "skills": ["<tech1>", "<tech2>", "<tech3>"]
        }},
        {{
            "name": "Cloud, DevOps & Tools",
            "skills": ["<tech1>", "<tech2>", "<tech3>"]
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
    lines.append(f"Candidate: {profile.get('name', 'Salil Vaidya')}")
    lines.append(f"Career Start: {profile.get('career_start_date', '2025-01')}\n")

    lines.append("### Experience:")
    for exp in profile.get("experience", []):
        lines.append(f"**{exp['title']} at {exp['company']}** ({exp['duration']}) — Domain: {exp.get('domain', 'Tech')}")
        lines.append(f"Stack: {', '.join(exp.get('stack', []))}")
        for h in exp.get("highlights", []):
            lines.append(f"• {h}")
        lines.append("")

    lines.append("### Projects Portfolio:")
    for proj in profile.get("projects", []):
        lines.append(f"**Project: {proj['name']}** — Domain: {proj.get('domain', 'Software')}")
        lines.append(f"Stack: {', '.join(proj.get('stack', []))}")
        for h in proj.get("highlights", []):
            lines.append(f"• {h}")
        lines.append("")

    lines.append("### Competitive Programming & Achievements:")
    for cp in profile.get("competitive_programming", []):
        lines.append(f"• {cp['platform']} {cp['title']}: {cp['details']}")

    lines.append(f"\n### Core Skills: {', '.join(profile.get('core_skills', []))}")

    return "\n".join(lines)


class ResumeTailor:
    """
    Uses Gemini to tailor resume content for a specific job posting.
    Generates dynamic summaries, Google XYZ bullets, and selects optimal projects.
    """

    def __init__(self, config: dict):
        self.model_name = config.get("model", "gemini-3.5-flash-lite")
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
            dict with tailored summary, experience, projects, and skill_categories
        """
        prompt = TAILOR_PROMPT.format(
            profile_text=self._profile_text,
            company=company,
            title=title,
            description=description[:3500],
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
            "summary": tailored.get("summary", ""),
            "education": profile.get("education", []),
            "experience": tailored.get("experience", profile.get("experience", [])),
            "projects": tailored.get("projects", profile.get("projects", [])),
            "competitive_programming": profile.get("competitive_programming", []),
            "core_skills": profile.get("core_skills", []),
            "skill_categories": tailored.get("skill_categories", None),
        }

        return context
