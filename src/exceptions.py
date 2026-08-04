"""Custom exception hierarchy for the fb-scraper package.

Other modules (scrapers, orchestrator) import these exact names to
distinguish between per-URL failures and batch-stopping conditions.
"""

from __future__ import annotations


class ScrapeError(Exception):
    """Base exception for all scraping-related errors."""


class LoginRequiredError(ScrapeError):
    """The target post is behind a login wall / private content.

    Raised to signal that the current URL should be *skipped* while the
    rest of the batch continues.
    """


class RateLimitError(ScrapeError):
    """The platform is rate-limiting us.

    Raised to signal that the whole batch should be stopped (plus cooldown)
    so the account/IP is not blocked further.
    """
