"""Ingestion package — ATS API pollers and scraping fallbacks."""

from src.ingestion.base import JobSource, RawJob
from src.ingestion.greenhouse import GreenhouseSource
from src.ingestion.lever import LeverSource
from src.ingestion.ashby import AshbySource
from src.ingestion.smartrecruiters import SmartRecruitersSource
from src.ingestion.jobspy_source import JobSpySource

__all__ = [
    "JobSource",
    "RawJob",
    "GreenhouseSource",
    "LeverSource",
    "AshbySource",
    "SmartRecruitersSource",
    "JobSpySource",
]
