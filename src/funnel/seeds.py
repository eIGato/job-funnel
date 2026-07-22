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
    # Teletype author feeds behind the @Remoteit Telegram network (Phase 3.5). We read the whole
    # author feed, not the channel, and never touch Telegram. Full descriptions; blind recruiter
    # posts (no employer). The handle rotates — append every known one here.
    SourceConfig(
        name="teletype",
        kind=SourceKind.RSS,
        config={"authors": ["kovesh", "courierus"]},
    ),
    # Adzuna aggregator (Phase 3.5): broad EU + remote-US, human-chosen 2026-07-22. Keys in .env.
    SourceConfig(
        name="adzuna",
        kind=SourceKind.API,
        config={
            "countries": ["gb", "de", "nl", "pl", "es", "at", "us", "ca"],
            "what": "python backend developer",
            "results_per_page": 50,
            "max_days_old": 14,
        },
    ),
    # The Muse aggregator (Phase 3.5): full descriptions, keyless (optional key raises the limit).
    SourceConfig(
        name="themuse",
        kind=SourceKind.API,
        config={
            "categories": ["Software Engineering", "Data Science", "Data and Analytics"],
            "pages": 2,
        },
    ),
    # The token is already in place (`funnel auth-gmail`); the query spans every board that
    # emails alerts. Parsers exist for hh, Habr, LinkedIn, Wellfound and Glassdoor; add senders
    # here as more boards come online (Indeed next). Left disabled by default — enable in the
    # admin once a real alert has landed in the mailbox, so a first run has something to read.
    SourceConfig(
        name="gmail-alerts",
        kind=SourceKind.GMAIL,
        enabled=False,
        config={
            "query": (
                "newer_than:7d (from:hh.ru OR from:career.habr.com "
                "OR from:jobalerts-noreply@linkedin.com "
                "OR from:wellfound.com OR from:glassdoor.com)"
            ),
            "max_results": 100,
        },
    ),
]
