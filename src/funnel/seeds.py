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
        # Three pages of the plain feed plus three of the visa-sponsorship slice. Germany is the
        # one relocation destination open to the human, and this feed is where it lives; one
        # page of a fast-rotating board was too thin a sample to reach the shortlist at all.
        config={
            "base_url": "https://www.arbeitnow.com/api/job-board-api",
            "pages": 3,
            "variants": [{}, {"visa_sponsorship": "true"}],
        },
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
    # Self-growing ATS boards (Phase 3.5 D). Config is empty on purpose: these adapters keep
    # their company slugs in the `ats_boards` table, because each slug carries mutable state
    # (last run, consecutive empty runs) that does not belong in a JSONB blob three adapters
    # rewrite concurrently. Each run discovers boards two ways: from apply-links in postings
    # already ingested, and by guessing a slug from the company name of the best-ranked ones
    # (`_probe_by_name`) — the second is what reaches employers behind an aggregator that only
    # ever links to itself. Both need a content source to have run first.
    SourceConfig(name="greenhouse", kind=SourceKind.API, config={}),
    SourceConfig(name="lever", kind=SourceKind.API, config={}),
    SourceConfig(name="ashby", kind=SourceKind.API, config={}),
    SourceConfig(name="recruitee", kind=SourceKind.API, config={}),
    SourceConfig(name="smartrecruiters", kind=SourceKind.API, config={}),
    # The token is already in place (`funnel auth-gmail`); the query spans every board that
    # emails alerts. Parsers exist for hh, Habr, LinkedIn, Wellfound, Glassdoor, Indeed,
    # Landing.Jobs, justjoin.it and pracuj.pl; add senders here as more boards come online.
    # Left disabled by default — enable in the admin once a real alert has landed in the
    # mailbox, so a first run has something to read.
    #
    # **The window is 30 days, not a week.** With `GMAIL_TRASH_PARSED_ALERTS` on, an alert is
    # read and Trashed within a run of arriving, so nothing older than a day is ever in scope
    # and the window costs nothing in the steady state — it is what lets the pipeline catch up
    # after a parser is added or the flag is turned on. Both happened on 2026-08-17: 247 board
    # mails were sitting in the inbox, 187 of them older than a week, including 27 pracuj and
    # justjoin alerts whose postings the funnel had never seen because the parsers did not yet
    # exist. A 7-day window could never have reached them. `max_results` caps a single run, so
    # a backlog that size clears over the next few timer ticks rather than in one burst.
    #
    # **Name the alert address, not the whole domain, wherever the board has one.** A board
    # that mails alerts also mails the human personally, and this query decides what
    # `GMAIL_TRASH_PARSED_ALERTS` is allowed to touch. Habr Career is the measured case
    # (2026-08-13): `subscribe@` sent 29 subscription digests in a year and `noreply@` sent 4
    # "Вы откликнулись на вакансию X" acknowledgements, each naming a company and linking the
    # posting. Only the parser returning nothing on those kept them out of the Trash, and
    # resting that on a parser is resting it on the wrong thing. hh.ru mails both from
    # `noreply@hh.ru`, so it cannot be split this way and stays a whole domain.
    #
    # **justjoin.it needs a third split: by subject.** `no-reply@justjoin.it` sends both the
    # daily "New jobs for you" alert and the "You applied for X" receipt, and unlike hh's the
    # receipt is *not* harmless to parse — it carries a "similar offers" block in the identical
    # card markup, so the parser reads five postings out of it and the message becomes
    # trash-eligible. Three such receipts were in the mailbox on 2026-08-17 and the exclusion
    # holds all three out. If justjoin rewords the subject the failure is one lost receipt in
    # Trash, recoverable for 30 days; the alternative — dropping the address — costs 20 alerts a
    # month, since `jobs@hello.justjoin.it` mails a different, Polish-language selection.
    SourceConfig(
        name="gmail-alerts",
        kind=SourceKind.GMAIL,
        enabled=False,
        config={
            "query": (
                "newer_than:30d (from:hh.ru OR from:subscribe@career.habr.com "
                "OR from:jobalerts-noreply@linkedin.com "
                "OR from:wellfound.com OR from:noreply@glassdoor.com "
                "OR from:jobalert.indeed.com OR from:landing.jobs "
                "OR from:jobs@hello.justjoin.it "
                'OR (from:no-reply@justjoin.it -subject:"You applied for") '
                "OR from:rekomendacje@wysylka.pracuj.pl)"
            ),
            # Mail this funnel has decided it will never read, Trashed unread. Every entry is a
            # measured "we already have this by another route", never "this looked like spam":
            #   adzuna.com   - already an API source for eight countries, and the alert host is
            #                  in BLOCKED_HOSTS (403 from Montenegro), so the rows would be
            #                  duplicates no shortlist could select. adzuna.nl is marketing.
            #   weworkremotely - already an RSS source.
            #   getmatch.ru  - a weekly digest of what the human has already seen in the Telegram
            #                  bot the same subscription feeds, where applying is one click
            #                  (human, 2026-08-17). The parser written that morning was removed
            #                  that afternoon: the postings were real, they were just not new.
            #   info@glassdoor.com - the marketing address. `noreply@glassdoor.com` is the alert
            #                  address and is parsed above; this is why both are named as
            #                  addresses and neither as `glassdoor.com`.
            # Reed, Totaljobs, 24recruitment, match.indeed.com and spelljob are NOT here. They
            # have no parser either, but for the opposite reason — no stable id in any link —
            # and their postings are genuinely new (34 of 47 Totaljobs postings measured were
            # not in the table). Discarding those would be throwing away mail unread that a
            # human might want; the fix for them is to unsubscribe, which is his call.
            "discard_query": (
                "newer_than:400d (from:no-reply@adzuna.com OR from:no-reply@adzuna.nl "
                "OR from:hello@m.weworkremotely.com OR from:gmate@getmatch.ru "
                "OR from:info@glassdoor.com)"
            ),
            "max_results": 100,
        },
    ),
]
