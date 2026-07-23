"""Adapters: self-growing ATS boards (Phase 3.5 D) — Greenhouse, Lever, Ashby.

No hand-kept slug list. Each run first **scans postings already ingested** for links to its own
ATS, records the company slugs it finds, and then polls those boards through their public,
no-auth APIs. A slug spotted once in an aggregator posting keeps paying out afterwards, with
full descriptions — which is what Phase 5 needs.

Discovery lives here rather than in ingest on purpose: the pipeline must not learn about
specific sources (a new source is a new adapter, nothing else moves). Each adapter owns both
halves of its own lifecycle.

The set stays bounded: a board that 404s or returns nothing for `_MAX_EMPTY_RUNS` consecutive
runs is disabled, so a dead slug costs one request and then nothing.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from sqlalchemy import select

from funnel.adapters.base import BaseAdapter, register
from funnel.adapters.util import clip, from_iso, get_json, strip_html
from funnel.db import session_scope
from funnel.models import AtsBoard, AtsProvider, Job
from funnel.schemas import NormalizedJob

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.orm import Session

#: Consecutive barren runs before a board is switched off. Four is roughly a day and a half at
#: 3 runs/day: long enough to ride out an empty-but-live board, short enough to stop wasting
#: requests on a dead one.
_MAX_EMPTY_RUNS = 4

#: How many recently-fetched postings to re-scan for slugs each run. Discovery only needs to
#: see a link once, and the boards it finds are remembered, so there is no value in walking the
#: whole table every time.
_SCAN_LIMIT = 400

#: A slug is the company identifier in an ATS board URL. Kept strict — a greedy pattern would
#: happily record "jobs" or a tracking token as a company and then poll it forever.
_SLUG = r"([A-Za-z0-9][A-Za-z0-9_-]{1,78}[A-Za-z0-9])"
_PATTERNS: dict[AtsProvider, tuple[re.Pattern[str], ...]] = {
    AtsProvider.GREENHOUSE: (
        re.compile(rf"(?:job-)?boards\.greenhouse\.io/{_SLUG}", re.I),
        re.compile(rf"boards\.eu\.greenhouse\.io/{_SLUG}", re.I),
        re.compile(rf"https?://{_SLUG}\.greenhouse\.io", re.I),
    ),
    AtsProvider.LEVER: (re.compile(rf"jobs\.(?:eu\.)?lever\.co/{_SLUG}", re.I),),
    AtsProvider.ASHBY: (re.compile(rf"jobs\.ashbyhq\.com/{_SLUG}", re.I),),
}

#: Path segments that are never a company. Without this, "boards.greenhouse.io/embed/job_board"
#: becomes a board called "embed".
_NOT_A_SLUG = frozenset({
    "embed", "jobs", "job", "board", "boards", "api", "www", "search", "apply", "careers",
    "job_board", "job-boards", "static", "assets", "images", "css", "js", "s", "c",
})  # fmt: skip


def discover_slugs(provider: AtsProvider, text: str) -> set[str]:
    """Every company slug for `provider` mentioned in a blob of text. Pure; no network."""
    found: set[str] = set()
    for pattern in _PATTERNS[provider]:
        for match in pattern.finditer(text or ""):
            slug = match.group(1).lower()
            if slug not in _NOT_A_SLUG:
                found.add(slug)
    return found


def _record_slugs(session: Session, provider: AtsProvider) -> int:
    """Scan recent postings for this provider's boards and remember any new ones."""
    rows = session.execute(
        select(Job.url, Job.description).order_by(Job.id.desc()).limit(_SCAN_LIMIT)
    ).all()
    known = set(session.scalars(select(AtsBoard.slug).where(AtsBoard.provider == provider)).all())
    added = 0
    for url, description in rows:
        for slug in discover_slugs(provider, f"{url}\n{description}"):
            if slug in known:
                continue
            known.add(slug)
            session.add(AtsBoard(provider=provider, slug=slug, discovered_from=url))
            added += 1
    return added


class _AtsAdapter(BaseAdapter):
    """Shared shape: record slugs, poll each enabled board, prune the barren ones."""

    provider: ClassVar[AtsProvider]

    async def fetch(self) -> list[NormalizedJob]:
        with session_scope() as session:
            _record_slugs(session, self.provider)
            boards = list(
                session.scalars(
                    select(AtsBoard).where(
                        AtsBoard.provider == self.provider, AtsBoard.enabled.is_(True)
                    )
                ).all()
            )

            jobs: list[NormalizedJob] = []
            for board in boards:
                board.last_run_at = datetime.now(tz=UTC)
                try:
                    postings = await self.fetch_board(board.slug)
                except Exception:
                    # A dead or renamed slug is the expected failure here, not an incident.
                    # Treat it as a barren run and let the pruning counter deal with it.
                    postings = []
                if postings:
                    board.empty_runs = 0
                    jobs.extend(postings)
                else:
                    board.empty_runs += 1
                    if board.empty_runs >= _MAX_EMPTY_RUNS:
                        board.enabled = False
            return jobs

    async def fetch_board(self, slug: str) -> list[NormalizedJob]:
        raise NotImplementedError


@register
class GreenhouseAdapter(_AtsAdapter):
    """Greenhouse job boards. `content=true` is what returns the full description."""

    name = "greenhouse"
    provider = AtsProvider.GREENHOUSE

    async def fetch_board(self, slug: str) -> list[NormalizedJob]:
        payload = await get_json(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", {"content": "true"}
        )
        return [j for j in (self._one(slug, raw) for raw in payload.get("jobs", [])) if j]

    @staticmethod
    def _one(slug: str, raw: dict[str, Any]) -> NormalizedJob | None:
        url, title = raw.get("absolute_url"), raw.get("title")
        if not url or not title:
            return None
        location = (raw.get("location") or {}).get("name")
        return NormalizedJob(
            url=url,
            company=slug,
            title=title,
            description=clip(strip_html(raw.get("content"))),
            location=location,
            is_remote=bool(location and "remote" in location.lower()),
            posted_at=from_iso(raw.get("updated_at")),
            external_id=str(raw.get("id")) if raw.get("id") else None,
        )


@register
class LeverAdapter(_AtsAdapter):
    """Lever postings. `mode=json` returns the full description inline."""

    name = "lever"
    provider = AtsProvider.LEVER

    async def fetch_board(self, slug: str) -> list[NormalizedJob]:
        payload = await get_json(f"https://api.lever.co/v0/postings/{slug}", {"mode": "json"})
        return [j for j in (self._one(slug, raw) for raw in payload or []) if j]

    @staticmethod
    def _one(slug: str, raw: dict[str, Any]) -> NormalizedJob | None:
        url, title = raw.get("hostedUrl"), raw.get("text")
        if not url or not title:
            return None
        categories = raw.get("categories") or {}
        location = categories.get("location")
        workplace = str(categories.get("commitment") or "") + str(raw.get("workplaceType") or "")
        posted = raw.get("createdAt")
        return NormalizedJob(
            url=url,
            company=slug,
            title=title,
            description=clip(strip_html(raw.get("descriptionPlain") or raw.get("description"))),
            location=location,
            is_remote="remote" in f"{location} {workplace}".lower(),
            posted_at=datetime.fromtimestamp(int(posted) / 1000, tz=UTC) if posted else None,
            external_id=str(raw.get("id")) if raw.get("id") else None,
        )


@register
class AshbyAdapter(_AtsAdapter):
    """Ashby job boards. The posting API is public and needs no key."""

    name = "ashby"
    provider = AtsProvider.ASHBY

    async def fetch_board(self, slug: str) -> list[NormalizedJob]:
        payload = await get_json(
            f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
            {"includeCompensation": "false"},
        )
        return [j for j in (self._one(slug, raw) for raw in payload.get("jobs", [])) if j]

    @staticmethod
    def _one(slug: str, raw: dict[str, Any]) -> NormalizedJob | None:
        url, title = raw.get("jobUrl"), raw.get("title")
        if not url or not title:
            return None
        return NormalizedJob(
            url=url,
            company=raw.get("companyName") or slug,
            title=title,
            description=clip(strip_html(raw.get("descriptionHtml") or raw.get("descriptionPlain"))),
            location=raw.get("location"),
            is_remote=bool(raw.get("isRemote")),
            posted_at=from_iso(raw.get("publishedAt")),
            external_id=str(raw.get("id")) if raw.get("id") else None,
        )


def known_boards(providers: Iterable[AtsProvider] | None = None) -> list[tuple[str, str, bool]]:
    """(provider, slug, enabled) for `funnel doctor` and manual inspection."""
    wanted = list(providers or list(AtsProvider))
    with session_scope() as session:
        rows = session.execute(
            select(AtsBoard.provider, AtsBoard.slug, AtsBoard.enabled)
            .where(AtsBoard.provider.in_(wanted))
            .order_by(AtsBoard.provider, AtsBoard.slug)
        ).all()
        return [(str(p), s, e) for p, s, e in rows]
