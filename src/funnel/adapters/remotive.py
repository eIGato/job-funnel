"""Adapter: a JSON/RSS job board (Phase 3).

OPEN QUESTION (PLAN.md section 7 and Phase 3): which boards actually expose a usable API
today. The endpoint is deliberately not written down from memory. Verify it is live while
building Phase 3 and put the URL in Source.config, not in this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from funnel.adapters.base import BaseAdapter, register

if TYPE_CHECKING:
    from funnel.schemas import NormalizedJob


@register
class RemotiveAdapter(BaseAdapter):
    """Pulls postings from a JSON/RSS board.

    Expected config keys (Source.config JSONB):
      base_url: str, the endpoint verified at build time
      search: str, the query or category
    """

    name = "remotive"

    async def fetch(self) -> list[NormalizedJob]:
        # TODO Phase 3:
        #   1. Verify a real, reachable API (Remotive / RemoteOK / WeWorkRemotely).
        #   2. httpx.AsyncClient -> GET base_url -> parse into NormalizedJob.
        #   3. Respect the source's rate limit.
        raise NotImplementedError("Phase 3: JSON board adapter; verify the endpoint first")
