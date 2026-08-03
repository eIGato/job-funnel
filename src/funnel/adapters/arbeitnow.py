"""Adapter: Arbeitnow job-board API (verified live 2026-07-18, re-verified 2026-08-03).

Response shape: {"data": [ ... ], "links": ..., "meta": ...}. A mix of remote and on-site
postings; `remote` is a real boolean. The URL lives in Source.config, not here.

Mostly Germany, which used to look like ballast — 1653 rows and nothing in the shortlist. It is
not ballast, it is crowded out: the feed carries real Berlin/Hamburg backend roles at the 93rd
to 97th percentile, and Adzuna's Poland-heavy Python listings sit at 98-100 and take every slot.
Germany is also the one relocation destination actually open to the human, so the fix is to see
more of this feed rather than less of it.

Two knobs, both Source.config, because the shape of the *request* is the adapter's business
while what to do with a posting is not:

  - `pages` — the feed rotates fast and one page is a thin sample of it. Page 1 held 175 rows
    and page 3 still held 100; three pages a run is a fuller look at the same free API.
  - `variants` — extra query-parameter sets fetched alongside the plain feed. `visa_sponsorship`
    is a genuinely different slice (74 of 175 slugs overlapped the plain feed, measured
    2026-08-03), and it is the slice that matters for relocation.

Requests are spaced by `_PAGE_DELAY`: the API answered 429 after roughly five rapid calls, and
its own terms ask not to abuse it (invariant 9 — no aggressive crawling). Overlap between
variants costs nothing; `_persist` dedups on content_hash, within a batch as well as against
the table.
"""

from __future__ import annotations

import asyncio
from typing import Any

from funnel.adapters.base import BaseAdapter, register
from funnel.adapters.util import clip, from_epoch, get_json, strip_html
from funnel.schemas import NormalizedJob

#: Seconds between requests. The board is a free public API that rate-limits at about five
#: rapid calls and asks in its own `meta.terms` not to be abused.
_PAGE_DELAY = 2.0


@register
class ArbeitnowAdapter(BaseAdapter):
    """Pulls postings from Arbeitnow's public JSON feed.

    Expected config keys (Source.config JSONB):
      base_url: str, e.g. https://www.arbeitnow.com/api/job-board-api  (verified at build)
      pages: int, optional — how many pages of each variant to walk (default 1)
      variants: list[dict], optional — query-parameter sets to fetch (default: the plain feed)
    """

    name = "arbeitnow"

    async def fetch(self) -> list[NormalizedJob]:
        base_url = str(self.config["base_url"])
        pages = max(1, int(self.config.get("pages", 1)))
        variants: list[dict[str, Any]] = list(self.config.get("variants") or [{}])

        jobs: list[NormalizedJob] = []
        first = True
        for params in variants:
            for page in range(1, pages + 1):
                if not first:
                    await asyncio.sleep(_PAGE_DELAY)
                first = False
                payload = await get_json(base_url, {**params, "page": page})
                batch = self.parse(payload)
                if not batch:
                    break  # walked off the end of this variant
                jobs.extend(batch)
        return jobs

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
