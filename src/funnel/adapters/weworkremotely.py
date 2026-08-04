"""Adapter: WeWorkRemotely RSS feed (verified live 2026-07-18).

An RSS 2.0 feed of remote-only postings. Item <title> is "Company: Position"; the body
is HTML escaped inside <description>. The feed URL lives in Source.config, not here.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from funnel.adapters.base import BaseAdapter, register
from funnel.adapters.util import clip, from_rfc822, get_text, strip_html
from funnel.schemas import NormalizedJob


@register
class WeWorkRemotelyAdapter(BaseAdapter):
    """Pulls postings from a WeWorkRemotely RSS feed.

    Expected config keys (Source.config JSONB):
      base_url: str, e.g. https://weworkremotely.com/remote-jobs.rss  (verified at build);
                category feeds like .../categories/remote-programming-jobs.rss also work.
    """

    name = "weworkremotely"

    async def fetch(self) -> list[NormalizedJob]:
        base_url = str(self.config["base_url"])
        body = await get_text(base_url)
        return self.parse(body)

    @staticmethod
    def parse(body: str) -> list[NormalizedJob]:
        root = ET.fromstring(body)
        channel = root.find("channel")
        if channel is None:
            return []
        jobs: list[NormalizedJob] = []
        for item in channel.findall("item"):
            title_text = _text(item, "title")
            link = _text(item, "link")
            company, _, position = title_text.partition(": ")
            # Feed convention is "Company: Position"; without the separator we cannot tell
            # the two apart, so skip rather than guess.
            if not position or not company or not link:
                continue
            region = _text(item, "region")
            country = _text(item, "country")
            location = ", ".join(p for p in (region, country) if p) or None
            jobs.append(
                NormalizedJob(
                    url=link,
                    company=company,
                    title=position,
                    description=clip(strip_html(_text(item, "description"))),
                    location=location,
                    is_remote=True,
                    posted_at=from_rfc822(_text(item, "pubDate")),
                    external_id=_text(item, "guid") or link,
                )
            )
        return jobs


def _text(item: ET.Element, tag: str) -> str:
    element = item.find(tag)
    return (element.text or "").strip() if element is not None else ""
