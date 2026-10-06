"""Database models for JobHunter."""

from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Text,
    DateTime,
    Boolean,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all database models."""
    pass


class Job(Base):
    """
    Represents a discovered job posting.
    This is the central table — every job flows through here.
    """
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # === Identity ===
    external_id = Column(String(255), nullable=False)  # ATS-specific job ID
    source = Column(String(50), nullable=False)         # greenhouse, lever, ashby, jobspy
    company_name = Column(String(255), nullable=False)

    # === Job Details ===
    title = Column(String(500), nullable=False)
    location = Column(String(500), nullable=True)
    department = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)           # Full JD text
    apply_url = Column(String(2000), nullable=False)
    posted_at = Column(DateTime, nullable=True)         # When the job was posted (from ATS)

    # === Pipeline State ===
    discovered_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    match_score = Column(Float, nullable=True)          # 0-100 from LLM
    match_analysis = Column(Text, nullable=True)        # JSON: matching skills, gaps, emphasis
    is_filtered_out = Column(Boolean, default=False)    # True if pre-filter dropped it
    filter_reason = Column(String(500), nullable=True)  # Why it was filtered

    # === Resume & Notification ===
    resume_path = Column(String(1000), nullable=True)   # Path to generated PDF
    resume_generated_at = Column(DateTime, nullable=True)
    notified = Column(Boolean, default=False)
    notified_at = Column(DateTime, nullable=True)

    # === Application Tracking ===
    status = Column(
        String(50),
        default="discovered",
        nullable=False,
    )
    # Possible statuses: discovered, scored, resume_generated, notified,
    #                     applied, referral_sent, interviewing, rejected, offer
    status_updated_at = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)                 # User's personal notes

    # === Referral ===
    referral_message = Column(Text, nullable=True)      # Pre-drafted referral pitch
    referral_contact = Column(String(500), nullable=True)

    # === Dedup ===
    __table_args__ = (
        UniqueConstraint("external_id", "source", "company_name", name="uq_job_identity"),
        Index("ix_jobs_company", "company_name"),
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_score", "match_score"),
        Index("ix_jobs_discovered", "discovered_at"),
    )

    def __repr__(self):
        return f"<Job({self.id}: {self.company_name} — {self.title})>"


class PollLog(Base):
    """Tracks each polling run for observability and debugging."""
    __tablename__ = "poll_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(50), nullable=False)
    company_name = Column(String(255), nullable=True)
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime, nullable=True)
    jobs_found = Column(Integer, default=0)
    new_jobs = Column(Integer, default=0)
    error = Column(Text, nullable=True)
    status = Column(String(50), default="running")  # running, success, error

    def __repr__(self):
        return f"<PollLog({self.id}: {self.source}/{self.company_name} — {self.status})>"
