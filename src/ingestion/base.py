"""Abstract base class for all job sources."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class RawJob:
    """
    Normalized job representation from any source.
    Every ingestion source must map its data to this schema.
    """
    external_id: str            # ATS-specific unique ID
    source: str                 # greenhouse, lever, ashby, jobspy
    company_name: str
    title: str
    location: Optional[str] = None
    department: Optional[str] = None
    description: Optional[str] = None
    apply_url: str = ""
    posted_at: Optional[datetime] = None
    raw_data: dict = field(default_factory=dict)  # Original API response for debugging


class JobSource(ABC):
    """Abstract interface for job data sources."""

    @abstractmethod
    async def fetch_jobs(self, company_name: str, slug: str, **kwargs) -> list[RawJob]:
        """
        Fetch all current job postings from this source.

        Args:
            company_name: Human-readable company name
            slug: ATS-specific identifier for the company
            **kwargs: Source-specific parameters (e.g., geo_filter)

        Returns:
            List of RawJob objects
        """
        ...

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Identifier for this source (e.g., 'greenhouse')."""
        ...
