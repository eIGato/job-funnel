"""Seed data for the source table.

The verified endpoints live here, not in the adapter modules (PLAN.md section 7: the URL
belongs in Source.config). Every URL below was reachable when this file was written
(2026-07-18); an adapter's own docstring repeats that date. `funnel seed-sources` upserts
these rows; the admin remains the place to tweak, disable, or add sources by hand.

One Source per adapter: `Source.name` is the registry key the pipeline resolves on, so all
Gmail board alerts are a single `gmail-alerts` source whose query spans every sender, and
the Gmail adapter sorts senders out internally.
"""

from __future__ import annotations

from funnel.models import SourceKind
from funnel.schemas import SourceConfig

DEFAULT_SOURCES: list[SourceConfig] = [
    SourceConfig(
        name="remoteok",
        kind=SourceKind.API,
        config={"base_url": "https://remoteok.com/api"},
    ),
    SourceConfig(
        name="remotive",
        kind=SourceKind.API,
        config={"base_url": "https://remotive.com/api/remote-jobs"},
    ),
    SourceConfig(
        name="arbeitnow",
        kind=SourceKind.API,
        config={"base_url": "https://www.arbeitnow.com/api/job-board-api"},
    ),
    SourceConfig(
        name="weworkremotely",
        kind=SourceKind.RSS,
        config={"base_url": "https://weworkremotely.com/remote-jobs.rss"},
    ),
    # Disabled until the Gmail alert parser lands (Phase 3). The token is already in place
    # (`funnel auth-gmail`); the query spans the boards that email alerts. Enable in the admin.
    SourceConfig(
        name="gmail-alerts",
        kind=SourceKind.GMAIL,
        enabled=False,
        config={
            "query": "newer_than:2d (from:career.habr.com OR from:hh.ru)",
            "max_results": 50,
        },
    ),
]
