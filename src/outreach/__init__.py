"""Outreach package — automated referral email finder and sender."""

from src.outreach.email_finder import find_referrers
from src.outreach.sender import EmailSender

__all__ = ["find_referrers", "EmailSender"]
