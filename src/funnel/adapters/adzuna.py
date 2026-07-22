"""Adapter: Adzuna aggregator API (Phase 3.5).

Adzuna is the "aggregator, not a hand-kept slug list" answer: a public REST API spanning ~12
countries that already normalizes postings from many boards/ATSs. The free tier needs an
app_id/app_key pair — secrets, so they come from settings (.env), never Source.config
(invariant 7). Non-secret params (countries, query) live in Source.config.

Endpoint is per-country: /v1/api/jobs/{country}/search/{page}. The `description` field is a
**teaser**, not the full posting (Adzuna links out via redirect_url), so these carry less text
than teletype/TheMuse — still a real breadth source, and enough to filter and rank on.
"""

from __future__ import annotations

from typing import Any

from funnel.adapters.base import BaseAdapter, register
from funnel.adapters.util import clip, from_iso, get_json, strip_html
from funnel.config import get_settings
from funnel.schemas import NormalizedJob

_API = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"
_REMOTE_HINTS = ("remote", "work from home", "wfh", "удал")


@register
class AdzunaAdapter(BaseAdapter):
    """Pulls postings from Adzuna across a list of countries.

    Expected config keys (Source.config JSONB):
      countries: list[str], ISO-2 codes Adzuna supports, e.g. ["gb", "de", "us"].
      what: str, the search query (e.g. "python backend developer").
      results_per_page: int, optional, defaults to 50.
      max_days_old: int, optional, restrict to postings newer than N days.
    Secrets (settings/.env): adzuna_app_id, adzuna_app_key.
    """

    name = "adzuna"

    async def fetch(self) -> list[NormalizedJob]:
        settings = get_settings()
        app_key = settings.adzuna_app_key.get_secret_value() if settings.adzuna_app_key else None
        if not settings.adzuna_app_id or not app_key:
            raise RuntimeError(
                "Adzuna needs ADZUNA_APP_ID and ADZUNA_APP_KEY in .env "
                "(register a free app at https://developer.adzuna.com/)."
            )
        countries = [str(c) for c in self.config.get("countries", ["gb"]) if c]
        params: dict[str, Any] = {
            "app_id": settings.adzuna_app_id,
            "app_key": app_key,
            "what": str(self.config.get("what", "")),
            "results_per_page": int(self.config.get("results_per_page", 50)),
            "content-type": "application/json",
        }
        if self.config.get("max_days_old"):
            params["max_days_old"] = int(self.config["max_days_old"])

        jobs: list[NormalizedJob] = []
        for country in countries:
            payload = await get_json(_API.format(country=country), params)
            jobs.extend(self.parse(payload))
        return jobs

    @staticmethod
    def parse(payload: Any) -> list[NormalizedJob]:
        jobs: list[NormalizedJob] = []
        for row in payload.get("results", []):
            company = (row.get("company") or {}).get("display_name")
            title = strip_html(row.get("title"))
            url = row.get("redirect_url")
            if not company or not title or not url:
                continue
            location = (row.get("location") or {}).get("display_name")
            description = clip(strip_html(row.get("description")))
            haystack = f"{title} {description} {location or ''}".casefold()
            jobs.append(
                NormalizedJob(
                    url=url,
                    company=company,
                    title=title,
                    description=description,
                    location=location,
                    is_remote=any(hint in haystack for hint in _REMOTE_HINTS),
                    posted_at=from_iso(row.get("created")),
                    external_id=str(row["id"]) if row.get("id") else None,
                )
            )
        return jobs
