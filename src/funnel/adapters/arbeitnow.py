"""Adapter: Arbeitnow job-board API (verified live 2026-07-18).

Response shape: {"data": [ ... ], "links": ..., "meta": ...}. A mix of remote and on-site
postings; `remote` is a real boolean. The URL lives in Source.config, not here.
"""

from __future__ import annotations

from typing import Any

from funnel.adapters.base import BaseAdapter, register
from funnel.adapters.util import clip, from_epoch, get_json, strip_html
from funnel.schemas import NormalizedJob


@register
class ArbeitnowAdapter(BaseAdapter):
    """Pulls postings from Arbeitnow's public JSON feed.

    Expected config keys (Source.config JSONB):
      base_url: str, e.g. https://www.arbeitnow.com/api/job-board-api  (verified at build)
    """

    name = "arbeitnow"

    async def fetch(self) -> list[NormalizedJob]:
        base_url = str(self.config["base_url"])
        payload = await get_json(base_url)
        return self.parse(payload)

    @staticmethod
    def parse(payload: Any) -> list[NormalizedJob]:
        jobs: list[NormalizedJob] = []
        for row in payload.get("data", []):
            if not row.get("title") or not row.get("company_name") or not row.get("url"):
                continue
            jobs.append(
                NormalizedJob(
                    url=row["url"],
                    company=row["company_name"],
                    title=row["title"],
                    description=clip(strip_html(row.get("description"))),
                    location=row.get("location") or None,
                    is_remote=bool(row.get("remote")),
                    posted_at=from_epoch(row.get("created_at")),
                    external_id=row.get("slug") or None,
                )
            )
        return jobs
