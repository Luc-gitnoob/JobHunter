"""Telegram notification bot for JobHunter."""

import json
import logging
import os
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Telegram Bot API base URL
TELEGRAM_API = "https://api.telegram.org/bot{token}"


class TelegramNotifier:
    """
    Sends job alerts via Telegram Bot API.

    Each notification includes:
    - Job title, company, location
    - Match score and analysis summary
    - Direct apply link
    - Pre-drafted referral message (candidate attaches master resume directly)
    """

    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")

        if not self.token or not self.chat_id:
            logger.warning(
                "Telegram credentials not configured. "
                "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env"
            )

    @property
    def is_configured(self) -> bool:
        return bool(self.token and self.chat_id)

    async def send_job_alert(
        self,
        company: str,
        title: str,
        location: str,
        apply_url: str,
        match_score: int,
        match_summary: str,
        matching_skills: list[str],
        referral_message: str,
        resume_path: Optional[Path] = None,
    ) -> bool:
        """
        Send a rich job alert notification.

        Returns True if sent successfully, False otherwise.
        """
        if not self.is_configured:
            logger.warning("[Telegram] Not configured, skipping notification")
            return False

        # Build the message
        score_emoji = self._score_emoji(match_score)
        skills_text = ", ".join(matching_skills[:8])

        message = (
            f"{score_emoji} *New Match: {match_score}/100*\n\n"
            f"🏢 *{self._escape_md(company)}*\n"
            f"💼 {self._escape_md(title)}\n"
            f"📍 {self._escape_md(location or 'Not specified')}\n\n"
            f"📊 *Match Analysis:*\n{self._escape_md(match_summary)}\n\n"
            f"🔧 *Key Skills:* {self._escape_md(skills_text)}\n\n"
            f"🔗 [Apply Now]({apply_url})\n\n"
            f"{'─' * 30}\n\n"
            f"📨 *Referral Pitch \\(copy\\-paste\\):*\n"
            f"```\n{referral_message}\n```"
        )

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Send the text message (try MarkdownV2, fallback to plain text)
                try:
                    text_response = await client.post(
                        f"{TELEGRAM_API.format(token=self.token)}/sendMessage",
                        json={
                            "chat_id": self.chat_id,
                            "text": message,
                            "parse_mode": "MarkdownV2",
                            "disable_web_page_preview": True,
                        },
                    )
                    text_response.raise_for_status()
                except httpx.HTTPStatusError as e:
                    logger.warning(f"[Telegram] MarkdownV2 failed ({e}), falling back to plain text")
                    plain_msg = (
                        f"{score_emoji} New Match: {match_score}/100\n\n"
                        f"Company: {company}\n"
                        f"Title: {title}\n"
                        f"Location: {location or 'Not specified'}\n\n"
                        f"Match Analysis:\n{match_summary}\n\n"
                        f"Key Skills: {skills_text}\n\n"
                        f"Apply: {apply_url}\n\n"
                        f"{'─' * 30}\n\n"
                        f"Referral Pitch:\n{referral_message}"
                    )
                    text_response = await client.post(
                        f"{TELEGRAM_API.format(token=self.token)}/sendMessage",
                        json={
                            "chat_id": self.chat_id,
                            "text": plain_msg,
                            "disable_web_page_preview": True,
                        },
                    )
                    text_response.raise_for_status()

                # Note: Master resume PDF is managed directly by the candidate; do not attach PDFs to Telegram alerts.
            logger.info(f"[Telegram] Sent alert for {company} — {title}")
            return True

        except Exception as e:
            logger.error(f"[Telegram] Failed to send alert: {e}")
            return False

    def _score_emoji(self, score: int) -> str:
        """Return appropriate emoji for match score."""
        if score >= 90:
            return "🔥"
        elif score >= 80:
            return "⭐"
        elif score >= 70:
            return "✅"
        else:
            return "📋"

    def _escape_md(self, text: str) -> str:
        """Escape special characters for Telegram MarkdownV2."""
        if not text:
            return ""
        special_chars = [
            '_', '*', '[', ']', '(', ')', '~', '`', '>',
            '#', '+', '-', '=', '|', '{', '}', '.', '!'
        ]
        for char in special_chars:
            text = text.replace(char, f"\\{char}")
        return text


def generate_referral_message(
    candidate_name: str,
    company: str,
    title: str,
    apply_url: str,
    matching_skills: list[str],
) -> str:
    """
    Generate a referral request message that Salil can copy-paste
    when reaching out to an engineer/alumnus at the company.
    """
    skills_highlight = ", ".join(matching_skills[:5])

    message = (
        f"Hi, I hope you’re doing well. "
        f"I’m currently working as a Software Development Engineer at NAV Fund Services. "
        f"I came across the {title} role at {company} and wanted to reach out.\n\n"
        f"My background is a close fit: I have hands-on experience with "
        f"{skills_highlight}, building high-throughput backend services and infrastructure "
        f"supporting 60+ hedge fund clients.\n\n"
        f"Here is the role link: {apply_url}\n\n"
        f"Would you be open to passing along a referral for this opening? "
        f"I've attached my resume, and I'd be happy to chat or provide any details needed.\n\n"
        f"Thanks for considering!"
    )

    return message
