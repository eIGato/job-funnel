"""Adapter: job alerts parsed out of Gmail (Phase 3).

We do not scrape LinkedIn (invariant 1). Instead we turn on saved-search email alerts
and read those messages through the Gmail API. The same trick works for any board that
can send alerts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from funnel.adapters.base import BaseAdapter, register

if TYPE_CHECKING:
    from funnel.schemas import NormalizedJob

#: Read-only on purpose. This system cannot send mail and never will, so a write scope
#: would be strictly more authority than it has any use for.
GMAIL_SCOPES: list[str] = ["https://www.googleapis.com/auth/gmail.readonly"]


@register
class GmailAlertsAdapter(BaseAdapter):
    """Reads alert emails and extracts job postings from them.

    Expected config keys (Source.config JSONB):
      query: str, a Gmail query such as
             'from:jobalerts-noreply@linkedin.com newer_than:7d'
      max_results: int
    """

    name = "gmail-alerts"

    async def fetch(self) -> list[NormalizedJob]:
        # TODO Phase 3:
        #   1. OAuth flow: settings.gmail_credentials_path -> token_path (InstalledAppFlow).
        #   2. users().messages().list(q=self.config["query"]), then get() per message id.
        #   3. Parse the email HTML into NormalizedJob.
        # Every board marks up its alerts differently, so write this parser against real
        # messages rather than from memory: dump a couple into tests/fixtures/ first.
        raise NotImplementedError("Phase 3: Gmail alert parser")
