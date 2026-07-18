"""Adapter: RemoteOK JSON API (verified live 2026-07-18).

The endpoint returns a JSON array whose first element is a legal/metadata notice, not a
job. Every posting is remote by definition. The URL lives in Source.config, not here.
"""

from __future__ import annotations

from typing import Any

from funnel.adapters.base import BaseAdapter, register
from funnel.adapters.util import clip, from_iso, get_json, strip_html
from funnel.schemas import NormalizedJob


@register
class RemoteOKAdapter(BaseAdapter):
    """Pulls postings from RemoteOK's public JSON feed.

    Expected config keys (Source.config JSONB):
      base_url: str, e.g. https://remoteok.com/api  (verified at build time)
      tags: str, optional comma-separated tag filter passed as ?tags=
    """

    name = "remoteok"

    async def fetch(self) -> list[NormalizedJob]:
        base_url = str(self.config["base_url"])
        params = {"tags": self.config["tags"]} if self.config.get("tags") else None
        payload = await get_json(base_url, params)
        return self.parse(payload)

    @staticmethod
    def parse(payload: Any) -> list[NormalizedJob]:
        jobs: list[NormalizedJob] = []
        for row in payload:
            # The metadata element (and any malformed row) lacks these fields.
            if not isinstance(row, dict) or not row.get("position") or not row.get("company"):
                continue
            url = row.get("url") or row.get("apply_url")
            if not url:
                continue
            jobs.append(
                NormalizedJob(
                    url=url,
                    company=row["company"],
                    title=row["position"],
                    description=clip(strip_html(row.get("description"))),
                    location=row.get("location") or None,
                    is_remote=True,
                    posted_at=from_iso(row.get("date")),
                    external_id=str(row["id"]) if row.get("id") else None,
                )
            )
        return jobs
