"""Adapter: The Muse public jobs API (Phase 3.5).

Another aggregator with no slug list to maintain. Works keyless (a free key only raises the
rate limit — an optional secret in settings/.env, never Source.config). Unlike Adzuna, the
`contents` field is the **full** posting HTML, so these are good Phase 5 material.

Endpoint: /api/public/jobs?category=...&page=...&location=... — the URL has no secrets and lives
here (there is nothing per-deployment about it); category/page/location are Source.config.
"""

from __future__ import annotations

from typing import Any

from funnel.adapters.base import BaseAdapter, register
from funnel.adapters.util import clip, from_iso, get_json, strip_html
from funnel.config import get_settings
from funnel.schemas import NormalizedJob

_API = "https://www.themuse.com/api/public/jobs"


@register
class TheMuseAdapter(BaseAdapter):
    """Pulls postings from The Muse.

    Expected config keys (Source.config JSONB):
      categories: list[str], e.g. ["Software Engineering", "Data Science"].
      location: str, optional, e.g. "Flexible / Remote".
      pages: int, optional number of pages to walk (50 postings each), defaults to 1.
    Optional secret (settings/.env): themuse_api_key.
    """

    name = "themuse"

    async def fetch(self) -> list[NormalizedJob]:
        settings = get_settings()
        api_key = settings.themuse_api_key.get_secret_value() if settings.themuse_api_key else None
        categories = [str(c) for c in self.config.get("categories", [])]
        location = self.config.get("location")
        pages = int(self.config.get("pages", 1))

        jobs: list[NormalizedJob] = []
        for page in range(pages):
            params: dict[str, Any] = {"page": page}
            if categories:
                params["category"] = categories  # httpx repeats the key for a list
            if location:
                params["location"] = str(location)
            if api_key:
                params["api_key"] = api_key
            payload = await get_json(_API, params)
            jobs.extend(self.parse(payload))
        return jobs

    @staticmethod
    def parse(payload: Any) -> list[NormalizedJob]:
        jobs: list[NormalizedJob] = []
        for row in payload.get("results", []):
            title = row.get("name")
            company = (row.get("company") or {}).get("name")
            url = (row.get("refs") or {}).get("landing_page")
            if not title or not company or not url:
                continue
            locations = [loc["name"] for loc in row.get("locations", []) if loc.get("name")]
            jobs.append(
                NormalizedJob(
                    url=url,
                    company=company,
                    title=title,
                    description=clip(strip_html(row.get("contents"))),
                    location=", ".join(locations) or None,
                    is_remote=any(
                        "remote" in loc.casefold() or "flexible" in loc.casefold()
                        for loc in locations
                    ),
                    posted_at=from_iso(row.get("publication_date")),
                    external_id=str(row["id"]) if row.get("id") else None,
                )
            )
        return jobs
