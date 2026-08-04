"""Adapter: Remotive JSON API (verified live 2026-07-18).

Response shape: {"jobs": [ ... ]}. Every posting is remote. The leading "warning"/"legal"
keys in the payload are noise we ignore. The URL lives in Source.config, not here.
"""

from __future__ import annotations

from typing import Any

from funnel.adapters.base import BaseAdapter, register
from funnel.adapters.util import clip, from_iso, get_json, strip_html
from funnel.schemas import NormalizedJob


@register
class RemotiveAdapter(BaseAdapter):
    """Pulls postings from Remotive's public JSON feed.

    Expected config keys (Source.config JSONB):
      base_url: str, e.g. https://remotive.com/api/remote-jobs  (verified at build time)
      search: str, optional free-text query passed as ?search=
      category: str, optional category slug passed as ?category=
    """

    name = "remotive"

    async def fetch(self) -> list[NormalizedJob]:
        base_url = str(self.config["base_url"])
        params = {
            key: self.config[key] for key in ("search", "category", "limit") if self.config.get(key)
        }
        payload = await get_json(base_url, params or None)
        return self.parse(payload)

    @staticmethod
    def parse(payload: Any) -> list[NormalizedJob]:
        jobs: list[NormalizedJob] = []
        for row in payload.get("jobs", []):
            if not row.get("title") or not row.get("company_name") or not row.get("url"):
                continue
            jobs.append(
                NormalizedJob(
                    url=row["url"],
                    company=row["company_name"],
                    title=row["title"],
                    description=clip(strip_html(row.get("description"))),
                    location=row.get("candidate_required_location") or None,
                    is_remote=True,
                    posted_at=from_iso(row.get("publication_date")),
                    external_id=str(row["id"]) if row.get("id") else None,
                )
            )
        return jobs
