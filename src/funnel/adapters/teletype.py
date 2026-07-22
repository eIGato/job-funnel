"""Adapter: teletype.in author feeds (Phase 3.5).

The @Remoteit Telegram network publishes every vacancy as a teletype.in post and the channel
carries only a subset, so we read the **author's whole feed** rather than the channel — and we
never touch Telegram (invariant 9). `teletype.in/rss/{author}` is a valid RSS 2.0 feed whose
`<content:encoded>` holds the full post body, which is exactly the full text a cover letter needs.

The handle rotates (config lists every known one — `kovesh` old, `courierus` current); it is
append-only and the current handle can be rediscovered from a fresh post later.

Two honest limitations of this source, reflected below:
  - **No employer.** These are blind recruiter posts: the only contact is the recruiter's own
    LinkedIn/email, never a company. `company` is therefore a source sentinel, not an employer.
  - **Geo is free text.** The post leads with an uppercase geo line ("REMOTE UZ/KZ/GE/PL"); we
    lift that into `location` so the RU/BY hard filter (which keys on location) can still bite.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from funnel.adapters.base import BaseAdapter, register
from funnel.adapters.util import clip, from_rfc822, get_text, strip_html
from funnel.schemas import NormalizedJob

#: <content:encoded> in the RSS 1.0 content module namespace.
_CONTENT_ENCODED = "{http://purl.org/rss/1.0/modules/content/}encoded"
#: The post slug in a teletype URL: teletype.in/@author/<slug>.
_POST_SLUG = re.compile(r"teletype\.in/@[^/]+/([^/?#]+)")
#: A leading line worth keeping as the location (the recruiter's geo/remote header).
_GEO_LINE = re.compile(r"\b(remote|relocation|on-?site|hybrid|only)\b", re.IGNORECASE)
_REMOTE = re.compile(r"\bremote\b", re.IGNORECASE)
#: Location is free text here; cap it well under the column width, it is a header not a paragraph.
_MAX_LOCATION = 200


@register
class TeletypeAdapter(BaseAdapter):
    """Pulls postings from one or more teletype.in author RSS feeds.

    Expected config keys (Source.config JSONB):
      authors: list[str], every known handle for the author (e.g. ["kovesh", "courierus"]).
      base_url: str, optional, defaults to https://teletype.in/rss
    """

    name = "teletype"

    async def fetch(self) -> list[NormalizedJob]:
        authors = [str(a) for a in self.config.get("authors", []) if a]
        base_url = str(self.config.get("base_url", "https://teletype.in/rss")).rstrip("/")
        jobs: list[NormalizedJob] = []
        for author in authors:
            body = await get_text(f"{base_url}/{author}")
            jobs.extend(self.parse(body, author))
        return jobs

    @staticmethod
    def parse(body: str, author: str) -> list[NormalizedJob]:
        channel = ET.fromstring(body).find("channel")
        if channel is None:
            return []
        jobs: list[NormalizedJob] = []
        for item in channel.findall("item"):
            title = _text(item, "title")
            link = _text(item, "link") or _text(item, "guid")
            content = item.find(_CONTENT_ENCODED)
            html = (content.text or "") if content is not None else _text(item, "description")
            description = clip(strip_html(html))
            if not title or not link or not description:
                continue
            lines = [line for line in strip_html(html).splitlines() if line.strip()]
            header = lines[0] if lines else ""
            location = header[:_MAX_LOCATION] if _GEO_LINE.search(header) else None
            slug = _POST_SLUG.search(_text(item, "guid") or link)
            jobs.append(
                NormalizedJob(
                    url=link.split("?", 1)[0],  # drop the utm_* tracking query
                    company=f"Teletype ({author})",  # blind recruiter post — no employer to name
                    title=title,
                    description=description,
                    location=location,
                    is_remote=bool(_REMOTE.search(html)),
                    posted_at=from_rfc822(_text(item, "pubDate")),
                    external_id=slug.group(1) if slug else None,
                )
            )
        return jobs


def _text(item: ET.Element, tag: str) -> str:
    element = item.find(tag)
    return (element.text or "").strip() if element is not None else ""
