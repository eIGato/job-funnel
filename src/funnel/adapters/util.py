"""Shared helpers for the HTTP/RSS source adapters.

Deliberately dependency-light: HTML is turned into text with the standard library only,
so no BeautifulSoup/lxml is pulled in for what the adapters need here.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from io import StringIO
from typing import Any

import httpx

#: Sent on every outbound request. Some boards (RemoteOK) reject the default client UA.
USER_AGENT = "job-funnel/0.1 (personal job-search batch tool)"

#: Postings can carry very long HTML bodies; the embedder only reads the first few hundred
#: tokens anyway (PLAN.md section 6). Cap the stored text so the table stays sane.
MAX_DESCRIPTION_CHARS = 4000

_REQUEST_TIMEOUT = httpx.Timeout(30.0)


class _TextExtractor(HTMLParser):
    """Collect text nodes, dropping tags. Block-level tags become newlines."""

    _BLOCK = frozenset(
        {"p", "br", "div", "li", "ul", "ol", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out = StringIO()

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in self._BLOCK:
            self._out.write("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK:
            self._out.write("\n")

    def handle_data(self, data: str) -> None:
        self._out.write(data)

    def text(self) -> str:
        return self._out.getvalue()


def strip_html(raw: str | None) -> str:
    """Turn an HTML fragment into readable plain text, collapsing runs of whitespace."""
    if not raw:
        return ""
    parser = _TextExtractor()
    parser.feed(unescape(raw))
    lines = [" ".join(line.split()) for line in parser.text().splitlines()]
    return "\n".join(line for line in lines if line).strip()


def clip(text: str, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    """Trim to `limit` characters, never mid-nonsense: cut on a boundary when close."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sep = cut.rfind("\n")
    if sep < limit - 200:  # only honor a newline that is reasonably near the end
        sep = cut.rfind(" ")
    return cut[:sep].rstrip() if sep > 0 else cut.rstrip()


#: "This job is remote" said outright, which outranks any later mention of office days.
_EXPLICIT_REMOTE = re.compile(
    r"\b(?:fully|100\s*%|entirely|completely)[\s-]*remote\b|\bremote[\s-]*(?:first|only)\b",
    re.IGNORECASE,
)
#: A split arrangement. The word "remote" appears in these too — that is the whole problem.
_HYBRID = re.compile(
    r"\bhybrid\b|\b\d+\s*days?\s*(?:a\s*week\s*)?(?:on-?site|in\s*(?:the\s*)?office)\b",
    re.IGNORECASE,
)
#: A bare mention, believed only when nothing above contradicts it.
_REMOTE_HINT = re.compile(r"\bremote|\bwork from home\b|\bwfh\b|удал", re.IGNORECASE)


def looks_remote(title: str, location: str | None, description: str) -> bool:
    """Decide `is_remote` from free text, for sources that expose no structured flag.

    Most adapters read a real field (arbeitnow's `remote`, RemoteOK's whole premise) and should
    keep doing that — this is for the ones with nothing but prose. Searching that prose for
    "remote" is what put a hybrid Chandler, AZ posting ("3 Days onsite, 2 Days work from home")
    on the remote-first shortlist, twice: the word is there, the job is not remote.

    Order is the point. An outright claim wins; failing that, a hybrid marker is decisive
    *against*, because a posting that spells out office days has already told us what it is;
    only then does a bare mention count. Measured over the 190 Adzuna rows in the table
    (2026-07-31): 28 were flagged remote, 19 of them on the body alone, and 14 carried
    hybrid/on-site wording. The middle branch is what those 14 needed.
    """
    text = f"{title}\n{location or ''}\n{description}"
    if _EXPLICIT_REMOTE.search(text):
        return True
    if _HYBRID.search(text):
        return False
    return bool(_REMOTE_HINT.search(text))


def _aware(dt: datetime) -> datetime:
    """Assume UTC for a naive datetime; boards report in UTC when they omit the zone."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def from_epoch(value: str | int | float | None) -> datetime | None:
    """Unix seconds -> aware UTC datetime, or None if unparseable."""
    if value in (None, "", "0"):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except TypeError, ValueError:
        return None


def from_iso(value: str | None) -> datetime | None:
    """ISO-8601 string -> aware UTC datetime (assuming UTC if no zone), or None."""
    if not value:
        return None
    try:
        return _aware(datetime.fromisoformat(value))
    except ValueError:
        return None


def from_rfc822(value: str | None) -> datetime | None:
    """RFC-822 date (RSS pubDate) -> aware UTC datetime, or None."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except TypeError, ValueError:
        return None
    return _aware(parsed) if parsed else None


async def get_json(base_url: str, params: dict[str, Any] | None = None) -> Any:
    """GET and decode JSON, with the shared UA and timeout."""
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, timeout=_REQUEST_TIMEOUT, follow_redirects=True
    ) as client:
        response = await client.get(base_url, params=params)
        response.raise_for_status()
        return response.json()


async def get_text(base_url: str, params: dict[str, Any] | None = None) -> str:
    """GET and return the body as text (for RSS/XML feeds)."""
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, timeout=_REQUEST_TIMEOUT, follow_redirects=True
    ) as client:
        response = await client.get(base_url, params=params)
        response.raise_for_status()
        return response.text


__all__ = [
    "USER_AGENT",
    "clip",
    "from_epoch",
    "from_iso",
    "from_rfc822",
    "get_json",
    "get_text",
    "looks_remote",
    "strip_html",
]
