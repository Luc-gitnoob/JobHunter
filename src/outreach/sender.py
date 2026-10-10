"""Automated cold referral email sender via SMTP."""

import os
import smtplib
import ssl
import logging
import asyncio
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select, func
from src.database.db import AsyncSessionLocal
from src.database.models import OutreachLog

logger = logging.getLogger(__name__)


class EmailSender:
    """Manages sending automated referral outreach emails via SMTP with rate limits and logging."""

    def __init__(self, config: Optional[dict] = None):
        cfg = config or {}
        self.enabled = cfg.get("enabled", True)
        self.auto_send = cfg.get("auto_send", True)
        self.daily_limit = cfg.get("daily_limit", 5)
        self.min_score = cfg.get("min_score", 90)

        # SMTP settings
        self.smtp_host = os.getenv("SMTP_HOST", cfg.get("smtp_host", "smtp.gmail.com"))
        self.smtp_port = int(os.getenv("SMTP_PORT", cfg.get("smtp_port", 587)))
        self.sender_email = os.getenv("SMTP_USER", cfg.get("sender_email", "salilvaidya003@gmail.com"))
        self.sender_password = os.getenv("SMTP_PASSWORD") or os.getenv("GMAIL_APP_PASSWORD", "")
        self.sender_name = cfg.get("sender_name", "Salil Vaidya")

        # Resume search paths
        configured_resume = cfg.get("resume_path", "data/resume.pdf")
        self.resume_paths = [
            Path(configured_resume),
            Path("data/resume.pdf"),
            Path("data/Salil_Vaidya_Resume.pdf"),
            Path("output/resumes/Salil_Vaidya_Resume.pdf"),
            Path("Salil_Vaidya_Resume.pdf"),
        ]

    @property
    def is_configured(self) -> bool:
        """Returns True if SMTP credentials are provided."""
        return bool(self.sender_email and self.sender_password)

    async def get_daily_sent_count(self) -> int:
        """Count emails successfully sent in the past 24 hours."""
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        async with AsyncSessionLocal() as session:
            stmt = select(func.count(OutreachLog.id)).where(
                OutreachLog.status == "sent",
                OutreachLog.sent_at >= since,
            )
            result = await session.execute(stmt)
            return result.scalar() or 0

    async def has_contacted(self, recipient_email: str) -> bool:
        """Check if recipient has already received an email."""
        async with AsyncSessionLocal() as session:
            stmt = select(func.count(OutreachLog.id)).where(
                OutreachLog.recipient_email == recipient_email,
                OutreachLog.status == "sent",
            )
            result = await session.execute(stmt)
            return (result.scalar() or 0) > 0

    def _find_resume_path(self) -> Optional[Path]:
        """Locate existing resume PDF if available."""
        for p in self.resume_paths:
            if p.is_file():
                return p
        return None

    def _build_email_message(
        self,
        recipient_name: str,
        recipient_email: str,
        company: str,
        job_title: str,
        apply_url: str,
    ) -> tuple[str, MIMEMultipart]:
        """Construct MIMEMultipart email message."""
        first_name = recipient_name.split()[0] if recipient_name else "there"
        subject = f"Referral Request: {job_title} | Salil Vaidya"

        body = (
            f"Hi {first_name},\n\n"
            f"I hope you're doing well.\n\n"
            f"I'm an SDE at NAV Fund Services (ex-Gap Inc.), working on backend systems, "
            f"Golang/Java microservices, and Kubernetes infrastructure.\n\n"
            f"I came across the {job_title} opening at {company} and my background is a "
            f"strong fit for what the team is building.\n\n"
            f"Role Link: {apply_url}\n\n"
            f"Could you please submit a referral for me for this opening? "
            f"I've attached my resume for your review. Please let me know if you need any "
            f"additional details from my end.\n\n"
            f"Thanks for your help!\n\n"
            f"Salil Vaidya\n"
            f"salilvaidya003@gmail.com | +91-9082854061\n"
            f"LinkedIn: https://linkedin.com/in/salil-v\n"
            f"GitHub: https://github.com/Luc-gitnoob"
        )

        msg = MIMEMultipart()
        msg["From"] = f"{self.sender_name} <{self.sender_email}>"
        msg["To"] = recipient_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        # Attach Resume if present
        resume_file = self._find_resume_path()
        if resume_file:
            try:
                with open(resume_file, "rb") as f:
                    part = MIMEApplication(f.read(), _subtype="pdf")
                    part.add_header(
                        "Content-Disposition",
                        "attachment",
                        filename="Salil_Vaidya_Resume.pdf",
                    )
                    msg.attach(part)
            except Exception as e:
                logger.warning(f"[EmailSender] Could not attach resume from {resume_file}: {e}")
        else:
            logger.info("[EmailSender] No resume PDF found in data/ or output/, sending text-only email.")

        return subject, msg

    def _send_smtp_sync(self, recipient_email: str, msg: MIMEMultipart) -> None:
        """Synchronous SMTP transmission wrapped for asyncio."""
        context = ssl.create_default_context()
        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=20.0) as server:
            server.starttls(context=context)
            server.login(self.sender_email, self.sender_password)
            server.send_message(msg)

    async def send_referral_email(
        self,
        job_id: int,
        company: str,
        job_title: str,
        apply_url: str,
        recipient_email: str,
        recipient_name: Optional[str] = None,
        recipient_title: Optional[str] = None,
    ) -> bool:
        """
        Check rate limits and send a direct cold email request for referral.
        Logs every attempt to database for auditability.
        """
        if not self.enabled or not self.auto_send:
            logger.info("[EmailSender] Outreach auto_send is disabled in config.")
            return False

        if not self.is_configured:
            logger.warning("[EmailSender] SMTP credentials not set (SMTP_USER/SMTP_PASSWORD). Cannot send email.")
            return False

        # Deduplication check
        if await self.has_contacted(recipient_email):
            logger.info(f"[EmailSender] Already contacted {recipient_email}. Skipping.")
            return False

        # Daily limit throttle
        sent_today = await self.get_daily_sent_count()
        if sent_today >= self.daily_limit:
            logger.warning(
                f"[EmailSender] Daily outreach limit reached ({sent_today}/{self.daily_limit}). "
                f"Skipping email to {recipient_email}."
            )
            return False

        subject, msg = self._build_email_message(
            recipient_name=recipient_name or "",
            recipient_email=recipient_email,
            company=company,
            job_title=job_title,
            apply_url=apply_url,
        )

        success = False
        error_msg = None

        try:
            await asyncio.to_thread(self._send_smtp_sync, recipient_email, msg)
            success = True
            logger.info(f"[EmailSender] Successfully sent referral email to {recipient_email} at {company}")
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[EmailSender] Failed to send email to {recipient_email}: {e}")

        # Record to database
        try:
            async with AsyncSessionLocal() as session:
                log_entry = OutreachLog(
                    job_id=job_id,
                    company_name=company,
                    recipient_email=recipient_email,
                    recipient_name=recipient_name,
                    recipient_title=recipient_title,
                    subject=subject,
                    status="sent" if success else "failed",
                    error=error_msg,
                    sent_at=datetime.now(timezone.utc),
                )
                session.add(log_entry)
                await session.commit()
        except Exception as db_err:
            logger.error(f"[EmailSender] Error logging outreach to database: {db_err}")

        return success
