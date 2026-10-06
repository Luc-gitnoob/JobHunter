"""Job description normalizer — cleans HTML, extracts plain text."""

import re
import html
import logging

from src.ingestion.base import RawJob

logger = logging.getLogger(__name__)


def normalize_description(raw_html: str) -> str:
    """
    Convert HTML job description to clean plain text.
    Preserves structure (headings, lists) but strips all HTML tags.
    """
    if not raw_html:
        return ""

    text = raw_html

    # Decode HTML entities
    text = html.unescape(text)

    # Convert block elements to newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|h[1-6]|li|tr)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<(p|div|h[1-6])[\s>]", "\n", text, flags=re.IGNORECASE)

    # Convert list items to bullet points
    text = re.sub(r"<li[\s>]", "\n• ", text, flags=re.IGNORECASE)

    # Strip all remaining HTML tags
    text = re.sub(r"<[^>]+>", "", text)

    # Clean up whitespace
    text = re.sub(r"[ \t]+", " ", text)          # Collapse horizontal whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)        # Limit consecutive newlines
    text = re.sub(r"^\s+", "", text, flags=re.MULTILINE)  # Strip leading whitespace per line

    return text.strip()


def normalize_title(title: str) -> str:
    """Normalize job title for consistent matching."""
    if not title:
        return ""
    # Remove extra whitespace, standardize separators
    title = re.sub(r"\s+", " ", title).strip()
    return title


def normalize_location(location: str) -> str:
    """Normalize location string for consistent geo matching."""
    if not location:
        return ""
    location = re.sub(r"\s+", " ", location).strip()
    return location


def normalize_job(raw_job: RawJob) -> RawJob:
    """
    Apply all normalization steps to a RawJob.
    Returns a new RawJob with cleaned fields.
    """
    return RawJob(
        external_id=raw_job.external_id,
        source=raw_job.source,
        company_name=raw_job.company_name.strip(),
        title=normalize_title(raw_job.title),
        location=normalize_location(raw_job.location or ""),
        department=raw_job.department,
        description=normalize_description(raw_job.description or ""),
        apply_url=raw_job.apply_url,
        posted_at=raw_job.posted_at,
        raw_data=raw_job.raw_data,
    )
