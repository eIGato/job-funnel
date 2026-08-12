"""Typer CLI: the pipeline entry points. This is what the systemd timer invokes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import typer
from sqlalchemy import case, func, select

from funnel import adapters
from funnel.config import get_settings
from funnel.db import session_scope
from funnel.models import Job, Source

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy import ColumnElement, Select
    from sqlalchemy.orm import InstrumentedAttribute, Session

    from funnel.models import Application, Reply
    from funnel.replies.inbox import IncomingMessage
    from funnel.schemas import NormalizedJob

app = typer.Typer(
    name="funnel",
    help="Deterministic job-search funnel. Never sends applications, only drafts them.",
    no_args_is_help=True,
)

#: Postings scored per commit in `match`. Embedding a backlog takes minutes, and the box is a
#: laptop that gets suspended or runs out of memory; committing as we go means a killed run
#: keeps what it already computed and the next one resumes where it stopped, instead of
#: throwing away the whole pass. Independent of the ONNX batch size (config), which is about
#: peak memory rather than durability.
_SCORE_CHUNK = 100

#: Below this many characters of body, the batch does not draft for a posting — the admin's
#: "Screen & draft letter" button does, after the human has pasted the real text into the row.
#:
#: A letter written from a title and a company name is a letter about nothing, and that is what
#: the top of the shortlist was spending its slots on: of the 554 rows above the floor on
#: 2026-08-06, 124 had an empty body (gmail alerts carry a subject line and a link, no posting)
#: and 71 more were a single short line — a technology tag list, or arbeitnow's 46-character
#: stub. Together 36% of every shortlist, one screening call and one useless letter apiece.
#:
#: A length floor, not "the body has no newline in it". Adzuna serves its teaser as one
#: unbroken paragraph, and those 500 characters are real: salary, requirements, stack. The
#: literal reading would have dropped 68 of them along with the junk. The exact value is not
#: load-bearing either — measured over the same rows, nothing at all has a body between 162 and
#: 369 characters, so any floor in that gap selects the identical 197 rows.
#:
#: This is a *selection* rule, like the apply-route exclusion below it: the row keeps its score
#: and its rank, `funnel draft --job <id>` and the admin button still draft for it on demand.
#: The hard filters deliberately have no minimum-body rule (see `matching/filters.py`) — a terse
#: posting is still a real posting, and throwing it out would put it beyond the button's reach.
MIN_DRAFTABLE_BODY = 300


def _role_key(column: InstrumentedAttribute[str]) -> ColumnElement[str]:
    """The (company, title) identity a cover letter is written for, folded for comparison.

    The SQL twin of `value.strip().casefold()`: one role, however many rows carry it.
    """
    return func.lower(func.trim(column))


def _company_rank(candidates: Any) -> ColumnElement[int]:
    """Where a role stands among its own employer's roles, best first."""
    from sqlalchemy import desc

    return (
        func.row_number()
        .over(partition_by=candidates.c.company, order_by=desc(candidates.c.rank_score))
        .label("company_rank")
    )


def shortlist_rank(remote_bonus: float) -> ColumnElement[float | None]:
    """What the shortlist sorts on: the match score, with remote work preferred.

    A preference, expressed as a bonus rather than a sort key. `matching/filters.py` keeps
    on-site postings on purpose — "it merely ranks below remote, and ranking is a sort, not
    this predicate" — but `ORDER BY is_remote DESC, match_score DESC` is not a sort, it is a
    partition: every remote row outranks every on-site one no matter the scores. With 893
    remote rows ahead of 1718 on-site ones that put the single best-matching posting in the
    database at rank 894, while "3D Environment Artist" made the top 25 (measured 2026-08-03).
    Adding `remote_bonus` instead lets the two pools interleave on merit.
    """
    return Job.match_score + case((Job.is_remote, remote_bonus), else_=0.0)


def shortlist_select(
    *, top_n: int, floor: float, remote_bonus: float, per_company: int = 3
) -> Select[tuple[Job]]:
    """The `top_n` highest-ranked postings nobody has decided on yet.

    Kept a pure query builder so it can be read (and tested) without a database.

    **The exclusion has to happen before the LIMIT.** Taking the top `top_n` and skipping the
    handled rows afterwards does not advance a window, it empties one: a decided posting keeps
    its rank forever, so each run draws the same rows and drafts less than the last. Measured
    2026-08-03 — all 25 slots held a decided row, `draft` had been a silent no-op for days, and
    2853 ingested postings had yielded 49 shortlist entries.

    Handled means "some row of this (company, title) has an Application past `shortlisted`".
    One letter per role, not per row: a board can list one role once per city — arbeitnow
    carries "Senior Platform Engineer (Remote UK Only)" under seven city slugs, each with its
    own id and location, so ingest cannot merge them. A `shortlisted` twin does not count;
    nothing was written for it yet, so it is not evidence the role has been handled.

    **One employer may hold at most `per_company` slots.** An ATS board arrives as a whole
    careers page, not as a posting: the first board registered put 14 of 25 slots in Reddit's
    hands, a frontend role and an engineering manager among them. A company's twelfth-best
    opening outranking another company's best is not what the ranking is for.

    **Twins are collapsed before the LIMIT too**, for the same reason. Deduplicating after the
    fact still lets five rows of one role hold five of the 25 slots and simply draft less: the
    2026-08-03 shortlist opened with EuroCert five times, Rose International four and STAFIDE
    twice, twelve slots for six roles. The highest-scoring row of a role represents it, which
    also picks the best of a board's per-city variants.

    **A posting the human cannot apply to takes no slot** (`matching/apply_route.py`). Some
    boards' links are dead ends — a site that answers 403 from where the human lives, an apply
    button behind a paywall — and 131 of the ~640 rows above the floor were such links on
    2026-08-05, a fifth of the shortlist spent on letters that could not be sent. This is the
    only place that acts on it: the rows stay scored, because they hold the corpus centre steady
    and because they are what ATS discovery probes for a company's own board (its link is the
    direct one).

    **A posting with too little text to write from takes no slot either**
    (`MIN_DRAFTABLE_BODY`). An empty or one-line body yields a letter about nothing, and 197 of
    the 554 rows above the floor were such bodies on 2026-08-06. The admin's per-row button is
    what these are for: the human pastes the real description into the row and draws the letter
    from there, which is exactly the loop that button was added for.
    """
    from sqlalchemy import desc
    from sqlalchemy.orm import aliased

    from funnel.models import Application, ApplicationStatus

    decided = aliased(Job)
    role_is_handled = (
        select(1)
        .select_from(decided)
        .join(Application, Application.job_id == decided.id)
        .where(
            Application.status != ApplicationStatus.SHORTLISTED,
            _role_key(decided.company) == _role_key(Job.company),
            _role_key(decided.title) == _role_key(Job.title),
        )
        .exists()
    )
    rank = shortlist_rank(remote_bonus).label("rank_score")
    twin = (
        func.row_number()
        .over(
            partition_by=(_role_key(Job.company), _role_key(Job.title)),
            order_by=desc(rank),
        )
        .label("twin_rank")
    )
    candidates = (
        select(Job, rank, twin)
        .where(
            Job.match_score.isnot(None),
            Job.hard_filter_passed.is_(True),
            Job.apply_blocked.is_(False),
            func.length(func.trim(Job.description)) >= MIN_DRAFTABLE_BODY,
            Job.match_percentile >= floor,
            ~role_is_handled,
        )
        .subquery()
    )
    # One role per row first, then at most `per_company` roles per employer. The cap has to come
    # second and in its own pass: ranking companies before twins are collapsed would let five
    # copies of one posting spend a company's whole allowance.
    roles = (
        select(candidates, _company_rank(candidates)).where(candidates.c.twin_rank == 1).subquery()
    )
    best_of_role = aliased(Job, roles)
    return (
        select(best_of_role)
        .where(roles.c.company_rank <= per_company)
        .order_by(desc(roles.c.rank_score))
        .limit(top_n)
    )


def _reply_row(message: IncomingMessage, application: Application | None) -> Reply:
    """The record of one incoming email, without a verdict on it yet.

    Separate from the classification on purpose: a board's bulk alert gets a row and no model
    call, so it stays visible in the admin and stays out of the bill.
    """
    from funnel.models import Reply

    return Reply(
        application_id=application.id if application else None,
        gmail_message_id=message.gmail_message_id,
        thread_id=message.thread_id or None,
        from_address=message.from_address,
        subject=message.subject,
        body=message.body,
        received_at=message.received_at,
    )


def _persist(session: Session, source: Source, fetched: list[NormalizedJob]) -> int:
    """Store postings, skipping ones already known. Deduplicated on content_hash."""
    if not fetched:
        return 0
    hashes = {item: item.content_hash_for(source.id) for item in fetched}
    known = set(
        session.scalars(
            select(Job.content_hash).where(Job.content_hash.in_(list(hashes.values())))
        ).all()
    )
    new = 0
    for item in fetched:
        content_hash = hashes[item]
        if content_hash in known:
            continue
        known.add(content_hash)  # a source can repeat a posting within one batch
        session.add(
            Job(
                source_id=source.id,
                external_id=item.external_id,
                url=str(item.url),
                company=item.company,
                title=item.title,
                description=item.description,
                location=item.location,
                is_remote=item.is_remote,
                posted_at=item.posted_at,
                # None here means "adapter doesn't know" — the before_insert hook then
                # derives it from the URL. Passing it through is what lets a source that
                # does know (Telegram) override that guess.
                apply_channel=item.apply_channel,
                content_hash=content_hash,
            )
        )
        new += 1
    return new


@app.command()
def ingest() -> None:
    """Collect postings from every enabled source. Re-running creates no duplicates."""
    total = 0
    used: list[tuple[str, adapters.BaseAdapter]] = []

    with session_scope() as session:
        sources = session.scalars(select(Source).where(Source.enabled.is_(True))).all()
        if not sources:
            typer.secho("No enabled sources. Add them in the admin.", fg=typer.colors.YELLOW)
            raise typer.Exit(0)

        for source in sources:
            try:
                adapter = adapters.get_adapter(source)
                fetched = asyncio.run(adapter.fetch())
            except NotImplementedError as exc:
                typer.secho(f"  {source.name}: not implemented ({exc})", fg=typer.colors.YELLOW)
                continue
            except Exception as exc:  # one broken source must not sink the whole run
                typer.secho(f"  {source.name}: ERROR {exc}", fg=typer.colors.RED)
                continue

            new = _persist(session, source, fetched)
            total += new
            used.append((source.name, adapter))
            typer.echo(f"  {source.name}: fetched {len(fetched)}, new {new}")

    # Outside the transaction, and only because it committed: this is where an adapter is
    # allowed a side effect on its source that a rollback could not take back — Gmail's
    # trashes the alerts it parsed (BaseAdapter.on_committed). A source that failed above
    # never reached `used`, so nothing is cleaned up on its behalf.
    for name, adapter in used:
        try:
            note = asyncio.run(adapter.on_committed())
        except Exception as exc:  # housekeeping must not fail a run whose postings are saved
            typer.secho(f"  {name}: post-commit ERROR {exc}", fg=typer.colors.RED)
            continue
        if note:
            typer.echo(f"  {name}: {note}")

    typer.secho(f"ingest: +{total} postings", fg=typer.colors.GREEN)


@app.command()
def match() -> None:
    """Hard filters plus embedding ranking. No LLM, no tokens.

    Three passes. Only the expensive one is incremental:

    1. **Filtering is global**, because `passes_hard_filters` is pure and cheap — no I/O, no
       model. Re-judging every row on every run is what makes a filter change take effect by
       itself; the alternative is a data migration per rule (`c7a5f2e91d34` was exactly that),
       and the rule that never gets one silently applies to new postings only.
    2. **Embedding is incremental**, because it is the only expensive step. A row keeps its
       vector once computed, including a row the filters have since retired: re-embedding it
       if a rule is later relaxed would be waste. Rejects are never embedded in the first place.
    3. **Scoring is global.** Scores are centered on the mean posting vector
       (`centered_similarity`), which is a property of the whole corpus — a score is comparable
       only against scores computed around the same centre, so scoring a new batch on its own
       would produce numbers that cannot be ranked against the existing ones. Rescoring
       everything is one matmul over vectors already in the database: no model call, no
       network, milliseconds at this size. An edited profile lands on the next run too.
    """
    import numpy as np

    from funnel.matching.apply_route import is_blocked
    from funnel.matching.embed import (
        centered_similarity,
        embed_texts,
        from_bytes,
        percentile_ranks,
        to_bytes,
    )
    from funnel.matching.filters import passes_hard_filters
    from funnel.matching.profile import get_profile_vector

    with session_scope() as session:
        every = session.scalars(select(Job)).all()

        retired = 0
        blocked = 0
        needs_embedding: list[Job] = []
        for job in every:
            passed = passes_hard_filters(job)
            if job.hard_filter_passed and not passed:
                retired += 1
            job.hard_filter_passed = passed
            # Scored like everything else, but off the shortlist: no apply route (see
            # matching/apply_route.py). Recomputed here rather than at ingest so a change to the
            # host list lands on the whole table at once.
            job.apply_blocked = is_blocked(job.url)
            blocked += job.apply_blocked and passed
            if passed and job.embedding is None:
                needs_embedding.append(job)

        # Postings are the passage side of the e5 pair; the profile is the query side.
        for start in range(0, len(needs_embedding), _SCORE_CHUNK):
            chunk = needs_embedding[start : start + _SCORE_CHUNK]
            texts = [f"{j.title}\n{j.company}\n{j.description}".strip() for j in chunk]
            for job, vector in zip(chunk, embed_texts(texts, is_query=False), strict=True):
                job.embedding = to_bytes(vector)
            session.commit()
            typer.echo(f"  embedded {start + len(chunk)}/{len(needs_embedding)}")

        # A rejected row must not keep a score: it is off the shortlist, and leaving it in the
        # population would also drag the centre towards postings we decided against.
        population = [j for j in every if j.hard_filter_passed and j.embedding is not None]
        for job in every:
            if job.hard_filter_passed is False:
                job.match_score = None
                job.match_percentile = None
        if not population:
            session.commit()
            typer.secho(
                f"match: {len(every)} scanned, 0 passed the hard filters", fg=typer.colors.YELLOW
            )
            return

        # One float32 matrix of the whole shortlist: 1024 dims is ~4 KB a row, so a few thousand
        # postings is a few MB. Chunk this only if the table grows by an order of magnitude.
        matrix = np.vstack([from_bytes(job.embedding) for job in population if job.embedding])
        scores = centered_similarity(matrix, get_profile_vector())
        percentiles = percentile_ranks(scores)
        for job, score, percentile in zip(population, scores, percentiles, strict=True):
            job.match_score = float(score)
            job.match_percentile = float(percentile)
        session.commit()

        typer.secho(
            f"match: {len(every)} scanned, {len(needs_embedding)} newly embedded, "
            f"{len(population)} scored ({len(every) - len(population)} filtered out"
            + (f", {retired} newly retired" if retired else "")
            + (f", {blocked} scored but unapplyable" if blocked else "")
            + f"), top score {float(scores.max()):.3f}",
            fg=typer.colors.GREEN,
        )


def _screen_and_draft(session: Session, job: Job, *, do_screen: bool) -> str:
    """`drafting.run.screen_and_draft` at a CLI boundary: run the loop, report the line."""
    from funnel.drafting.run import screen_and_draft

    outcome = asyncio.run(screen_and_draft(session, job, do_screen=do_screen))
    colour = typer.colors.RED if outcome.verdict == "error" else None
    label = f"  {outcome.verdict}: {job.company} — {job.title[:50]}"
    typer.secho(f"{label} ({outcome.detail})" if outcome.verdict != "drafted" else label, fg=colour)
    return outcome.verdict


@app.command()
def draft(
    limit: int | None = typer.Option(None, help="How many shortlisted postings to process."),
    screen: bool | None = typer.Option(
        None,
        help="Run the stop-stack screen before drafting. Defaults to settings.draft_screen.",
    ),
    job_id: int | None = typer.Option(
        None,
        "--job",
        help="Screen and draft this one posting, whatever its rank or status. Use after "
        "pasting a full description into a posting the board only served a teaser for.",
    ),
) -> None:
    """Draft cover letters for the top of the shortlist. DOES NOT SEND (invariant 2).

    Each posting is screened first (`drafting/screen.py`): one cheap model call that declines a
    role whose emphasis is PHP/Node/fullstack, or that is an outright content mismatch the
    ranking let through. Without it the human got a finished cover letter for every such
    posting and had to mark it DECLINED by hand — the screen is far cheaper than the letter it
    prevents. Hard geography/seniority filters stay upstream in `matching/filters.py`.

    Idempotent: a posting whose Application has moved past `shortlisted` (already drafted, or
    touched by the human) is skipped, so a re-run neither regenerates nor clobbers a letter.
    A `declined` posting is likewise never re-screened or re-drafted. Only one posting per
    (company, title) is drafted for, however many rows a board splits that role across.

    Both of those happen in `shortlist_select`, in SQL, **before** the LIMIT. Applied afterwards
    they do not advance the window, they empty it: a decided posting or a twin keeps its rank
    and its slot, so the command draws the same `top_n` rows every run and does less each time.
    Measured 2026-08-03 — all 25 slots held a decided row and `draft` had been a silent no-op
    for days, with 2853 ingested postings behind 49 shortlist entries.

    A posting with under `MIN_DRAFTABLE_BODY` characters of body is passed over as well: some
    boards serve a teaser rather than a posting, and a gmail alert carries no body at all. The
    batch has nothing to write from, so it writes nothing and leaves the row for the human.

    `--job <id>` is that row's way back in, and the only way to redo a decided posting. Paste
    the real description into the row in the admin — or press "Screen & draft letter" there,
    which runs this same step — and the letter follows from the text as it now stands, rank and
    existing status ignored. Nothing is sent, here as everywhere (invariant 2).
    """
    settings = get_settings()
    if not (settings.llm_api_key and settings.llm_api_key.get_secret_value()):
        typer.secho(
            "draft: LLM_API_KEY is empty. Set it in .env (provider for llm_model="
            f"{settings.llm_model}). Drafting needs it; nothing was sent.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)

    do_screen = settings.draft_screen if screen is None else screen
    if job_id is not None:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if job is None:
                typer.secho(f"draft: no posting with id {job_id}.", fg=typer.colors.RED)
                raise typer.Exit(1)
            typer.echo(f"draft: {job.company} — {job.title} (id {job_id}, by hand)")
            _screen_and_draft(session, job, do_screen=do_screen)
        return

    # The shortlist order (PLAN.md section 7): by score, with remote preferred.
    top_n = limit or settings.match_top_k
    floor = settings.match_percentile_threshold
    with session_scope() as session:
        jobs = session.scalars(
            shortlist_select(
                top_n=top_n,
                floor=floor,
                remote_bonus=settings.remote_bonus,
                per_company=settings.shortlist_per_company,
            )
        ).all()
        if not jobs:
            typer.secho(
                f"draft: nothing undecided at or above the {floor:.0f}th percentile "
                "(match_percentile_threshold). Run `funnel match` if the shortlist is stale.",
                fg=typer.colors.YELLOW,
            )
            return

        drafted = declined = 0
        for job in jobs:
            # Every row here is one undecided role: the query excluded decided ones and kept a
            # single row per (company, title), so nothing can be overwritten or written twice.
            outcome = _screen_and_draft(session, job, do_screen=do_screen)
            drafted += outcome == "drafted"
            declined += outcome == "declined"

        typer.secho(
            f"draft: {drafted} letters drafted, {declined} declined by the screen, "
            f"over {len(jobs)} undecided roles (NOT sent)",
            fg=typer.colors.GREEN,
        )


@app.command(name="agent-draft")
def agent_draft(
    limit: int = typer.Option(5, help="How many top shortlisted postings to run the agent over."),
    research: bool = typer.Option(True, help="Do the web-search company research node."),
) -> None:
    """Run the Phase 7 agent over the very top of the shortlist. DOES NOT SEND (invariant 2).

    The graph is `decide-worth-it -> research-company -> draft -> critic`. It is the richer,
    more expensive pass — four model calls plus a web search per posting — so it is a deliberate
    MANUAL command over a handful, and is deliberately NOT part of `run-funnel` (the timer stays
    cheap). Three outcomes per posting:

    - the decide-worth-it node judges the role a poor fit on the soft stop-stack (PLAN.md
      section 7) -> status `declined`, with the reason in notes; never drafted by the timer after;
    - a letter is drafted (grounded, critiqued, possibly revised once) -> status `drafted`;
    - the grounding backstop refuses a fabrication -> status `declined`, reason recorded.

    Re-running re-bills: it reprocesses `shortlisted`/`drafted` postings (upgrading a plain
    draft), and skips anything the human has moved on (`sent` and beyond) or already `declined`.
    """
    from sqlalchemy import desc

    from funnel.models import Application, ApplicationStatus
    from funnel.orchestration.agent import JobBrief, build_agent_deps, run_agent

    settings = get_settings()
    if not (settings.llm_api_key and settings.llm_api_key.get_secret_value()):
        typer.secho(
            "agent-draft: LLM_API_KEY is empty. Set it in .env (provider for llm_model="
            f"{settings.llm_model}). The agent needs it; nothing was sent.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)

    # Only fresh or plainly-drafted postings — never clobber a human-progressed application.
    processable = {ApplicationStatus.SHORTLISTED, ApplicationStatus.DRAFTED}
    deps = build_agent_deps(do_research=research)

    with session_scope() as session:
        # Same quality floor as `draft`, and the same apply-route rule: this pass is four model
        # calls and a web search per posting, so spending it below the floor — or on a posting
        # with no way to apply (`matching/apply_route.py`) — is the expensive version of the
        # mistake `draft` would have made for one call.
        jobs = session.scalars(
            select(Job)
            .where(
                Job.match_score.isnot(None),
                Job.hard_filter_passed.is_(True),
                Job.apply_blocked.is_(False),
                Job.match_percentile >= settings.match_percentile_threshold,
            )
            .order_by(desc(Job.is_remote), desc(Job.match_score))
            .limit(limit)
        ).all()

        drafted = declined = 0
        for job in jobs:
            application = job.application
            if application is not None and application.status not in processable:
                continue

            try:
                result = asyncio.run(run_agent(JobBrief.from_job(job), deps))
            except Exception as exc:  # one bad posting must not sink the batch
                typer.secho(f"  {job.company} — {job.title[:40]}: ERROR {exc}", fg=typer.colors.RED)
                continue

            if application is None:
                application = Application(job_id=job.id)
                session.add(application)

            if not result.worth_it:
                application.status = ApplicationStatus.DECLINED
                application.notes = f"Agent declined: {result.reasoning}"
                declined += 1
                typer.echo(f"  declined: {job.company} — {job.title[:50]} ({result.reasoning})")
                continue

            if result.draft is None:  # the grounding backstop suppressed a fabrication
                application.status = ApplicationStatus.DECLINED
                application.notes = f"Agent refused (ungrounded): {result.critique}"
                declined += 1
                typer.echo(f"  refused (ungrounded): {job.company} — {job.title[:50]}")
                continue

            application.cover_letter = f"Subject: {result.draft.subject}\n\n{result.draft.body}"
            application.status = ApplicationStatus.DRAFTED
            notes: list[str] = []
            if result.draft.matched_points:
                notes.append("Leans on: " + "; ".join(result.draft.matched_points))
            if result.critique and not result.approved:
                notes.append("Reviewer (unresolved): " + result.critique)
            if result.research:
                notes.append("Company: " + result.research)
            application.notes = "\n".join(notes) or None
            drafted += 1
            flag = "" if result.approved else " [critic not satisfied]"
            typer.echo(f"  drafted: {job.company} — {job.title[:50]}{flag}")

        typer.secho(
            f"agent-draft: {drafted} drafted, {declined} declined (NOT sent)",
            fg=typer.colors.GREEN,
        )


@app.command(name="check-replies")
def check_replies(
    days: int = typer.Option(None, help="How far back to scan the mailbox."),
) -> None:
    """Pull replies, classify them, and update the funnel. Reads mail only; sends nothing.

    Three passes, and every one of them refuses to guess:

    1. **Link.** For applications the human marked `sent` but that have no thread yet, look in
       Sent mail for the message they sent. Only an unambiguous hit is stored. Most
       applications go through a web form and have no sent message at all, so the thread is
       more often learned the other way round — see pass 3.
    2. **Fetch.** Read incoming mail, skipping any message already recorded — `check-replies`
       is idempotent and never re-bills a classification.
    3. **Classify and apply.** Oldest message first, because an acknowledgement teaches us the
       thread that the real answer arrives in later. A conclusive match writes its thread back
       onto the application, so every further message in that conversation matches by thread
       alone. An unmatched reply, one matched only inconclusively, or one classified below
       `reply_confidence_threshold`, is stored for review but leaves the Application status
       untouched. A wrong auto-status would hide a real interview; an unread row would not.

    Bulk mail from a job board that matches no application is recorded unclassified: it is
    over half of everything the mailbox turns up (95 of 166 on 2026-08-12) and it is never an
    answer to anything, so paying a model to call it `no_reply` bought nothing. The row is
    still written, which keeps it visible in the admin and keeps the scan idempotent.
    """
    from funnel.models import (
        REPLYABLE_STATUSES,
        Application,
        Reply,
    )
    from funnel.replies.classify import classify_reply
    from funnel.replies.inbox import build_service, fetch_recent, find_sent_thread
    from funnel.replies.link import link, relink_stored
    from funnel.replies.match import is_board_sender, match_reply, sender_domain

    settings = get_settings()
    if not (settings.llm_api_key and settings.llm_api_key.get_secret_value()):
        typer.secho(
            "check-replies: LLM_API_KEY is empty. Set it in .env (provider for llm_model="
            f"{settings.llm_model}). Classification needs it.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)

    service = build_service()
    lookback = days or settings.reply_lookback_days

    with session_scope() as session:
        sent = list(
            session.scalars(
                select(Application).where(Application.status.in_(REPLYABLE_STATUSES))
            ).all()
        )

        # 1. Link applications to their Gmail thread.
        linked = 0
        for application in sent:
            if application.thread_id or application.sent_at is None:
                continue
            thread_id = find_sent_thread(
                service, company=application.job.company, sent_at=application.sent_at
            )
            if thread_id:
                application.thread_id = thread_id
                linked += 1

        # 2. Fetch what is new.
        messages = fetch_recent(service, days=lookback)
        seen = set(
            session.scalars(
                select(Reply.gmail_message_id).where(
                    Reply.gmail_message_id.in_([m.gmail_message_id for m in messages] or [""])
                )
            ).all()
        )
        # Oldest first, so an acknowledgement matched in this very batch has already taught us
        # its thread by the time the answer to it is looked at.
        fresh = sorted(
            (m for m in messages if m.gmail_message_id not in seen),
            key=lambda m: (m.received_at is not None, m.received_at),
        )

        # 3. Match, classify, apply.
        applied = unmatched = uncertain = weak = bulk = 0
        for message in fresh:
            match = match_reply(message, sent)

            if match is None and is_board_sender(sender_domain(message.from_address)):
                session.add(_reply_row(message, None))
                bulk += 1
                continue

            try:
                verdict = asyncio.run(
                    classify_reply(message.subject, message.from_address, message.body)
                )
            except Exception as exc:  # one bad message must not sink the batch
                typer.secho(f"  {message.subject[:40]}: ERROR {exc}", fg=typer.colors.RED)
                continue

            row = _reply_row(message, match.application if match else None)
            row.reply_type = verdict.reply_type
            row.confidence = verdict.confidence
            row.reasoning = verdict.reasoning
            # A proposal for the admin's "Record as sent application", nothing more. Free:
            # the call that produced the verdict read the same email.
            row.detected_company = verdict.company
            row.detected_role = verdict.role
            session.add(row)

            if match is None:
                unmatched += 1
                continue

            # The thread of a conversation we are now sure about. Learned from the incoming
            # side, which is the only side a web-form application has: pass 1 finds nothing
            # for those, and without this the answer that follows an acknowledgement would be
            # matched from scratch, on whatever the company happened to put in the subject.
            if message.thread_id and match.application.thread_id is None:
                match.application.thread_id = message.thread_id

            if not match.conclusive:
                weak += 1
                continue
            if verdict.confidence < settings.reply_confidence_threshold:
                uncertain += 1
                continue
            if link(row, match.application, conclusive=True):
                applied += 1
                typer.echo(
                    f"  {verdict.reply_type.value}: {match.application.job.company} "
                    f"({verdict.confidence:.2f}, by {match.strategy})"
                )

        # 4. And the backlog: replies that found nothing when they arrived, re-matched against
        #    the applications (and the threads) that exist now. Free, so it runs every time.
        relinked, relinked_applied = relink_stored(session, sent)

        typer.secho(
            f"check-replies: {len(fresh)} new, {applied + relinked_applied} applied, "
            f"{uncertain} below threshold, {weak} linked without a status, {unmatched} "
            f"unmatched, {bulk} board alerts unclassified, {linked} threads linked, "
            f"{relinked} stored replies relinked",
            fg=typer.colors.GREEN,
        )
        if unmatched or uncertain:
            typer.echo("  Review them in the admin under Replies (unmatched have no Application).")


@app.command(name="relink-replies")
def relink_replies() -> None:
    """Re-match stored replies against today's applications. Touches no mail and no model.

    The same pass `check-replies` ends with, on its own so it can be run after a matching rule
    changes or after an orphan reply is turned into an application, without waiting for the
    next scan and without a Gmail token.
    """
    from funnel.models import REPLYABLE_STATUSES, Application
    from funnel.replies.link import relink_stored

    with session_scope() as session:
        applications = list(
            session.scalars(
                select(Application).where(Application.status.in_(REPLYABLE_STATUSES))
            ).all()
        )
        linked, applied = relink_stored(session, applications)

    typer.secho(
        f"relink-replies: {linked} replies linked, {applied} statuses moved",
        fg=typer.colors.GREEN,
    )


@app.command(name="run-funnel")
def run_funnel(ctx: typer.Context) -> None:
    """Run ingest, then match, then draft. This is what the systemd timer calls.

    `check-replies` is deliberately NOT part of this: it is a no-op until the human has marked
    applications `sent`, so the clock is the wrong trigger for it. (Not because it calls the
    LLM — `draft`, right below, does that on every timer tick.)
    """
    ctx.invoke(ingest)
    ctx.invoke(match)
    # `limit=None`/`screen=None` explicitly: an omitted parameter here keeps Python's declared
    # default, which for a Typer command is the OptionInfo sentinel, not the value inside it.
    # Typer only swaps that in when the parser runs, so leaving it out sends an OptionInfo into
    # SQLAlchemy's .limit() and the timer run dies after match. Pass every defaulted parameter
    # by hand. `screen=None` means "follow settings.draft_screen".
    ctx.invoke(draft, limit=None, screen=None, job_id=None)


@app.command(name="seed-sources")
def seed_sources(
    update_config: bool = typer.Option(
        False, help="Refresh config/kind of sources that already exist (never touches enabled)."
    ),
) -> None:
    """Create the verified default sources. Idempotent; the admin stays the source of truth."""
    from funnel.seeds import DEFAULT_SOURCES

    with session_scope() as session:
        existing = {s.name: s for s in session.scalars(select(Source)).all()}
        created = updated = 0
        for seed in DEFAULT_SOURCES:
            row = existing.get(seed.name)
            if row is None:
                session.add(
                    Source(
                        name=seed.name,
                        kind=seed.kind,
                        config=seed.config,
                        enabled=seed.enabled,
                    )
                )
                created += 1
                typer.echo(f"  + {seed.name} ({seed.kind})")
            elif update_config:
                row.kind = seed.kind
                row.config = seed.config
                updated += 1
                typer.echo(f"  ~ {seed.name} (config refreshed)")
            else:
                typer.echo(f"  = {seed.name} (exists, unchanged)")
    typer.secho(f"seed-sources: +{created} created, {updated} updated", fg=typer.colors.GREEN)


@app.command(name="auth-gmail")
def auth_gmail() -> None:
    """Authorize Gmail access (one-time, opens a browser). DOES NOT SEND.

    Re-run this after flipping GMAIL_TRASH_PARSED_ALERTS: the scope changes with it, and a
    token minted for the narrower one is refused rather than left to fail with a bare 403.
    """
    from funnel.adapters.gmail import get_credentials, gmail_scopes

    settings = get_settings()
    scope = gmail_scopes()[0].rsplit("/", 1)[-1]
    typer.echo(f"Using client secret : {settings.gmail_credentials_path}")
    typer.echo(f"Token will be saved : {settings.gmail_token_path}")
    typer.echo(f"A browser window will open for consent (scope: {scope}).")
    typer.echo("If it does not, copy the URL printed below into a browser by hand.")
    try:
        get_credentials(interactive=True)
    except (FileNotFoundError, RuntimeError) as exc:
        typer.secho(f"auth-gmail: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    typer.secho("auth-gmail: token stored, Gmail access ready.", fg=typer.colors.GREEN)


@app.command()
def admin() -> None:
    """Serve the sqladmin review UI for the shortlist and drafts."""
    import uvicorn

    settings = get_settings()
    uvicorn.run("funnel.admin:app", host=settings.admin_host, port=settings.admin_port)


@app.command(name="init-db")
def init_db() -> None:
    """Explain how to apply the schema. Schema changes go through Alembic only."""
    typer.echo("Schema is managed by Alembic only:\n  uv run alembic upgrade head")


@app.command()
def doctor() -> None:
    """Check the environment: config, database, adapter registry, CV."""
    settings = get_settings()
    ok = True

    typer.echo(f"embedding model : {settings.embedding_model}")
    typer.echo(f"llm model       : {settings.llm_model}")

    try:
        with session_scope() as session:
            session.execute(select(1))
        typer.secho("database        : ok", fg=typer.colors.GREEN)
    except Exception as exc:
        typer.secho(f"database        : FAILED - {exc}", fg=typer.colors.RED)
        ok = False

    typer.echo(f"adapters        : {', '.join(sorted(adapters.registry())) or 'none'}")

    if settings.gmail_token_path.exists():
        from funnel.adapters.gmail import gmail_scopes

        scope = gmail_scopes()[0].rsplit("/", 1)[-1]
        trash = "on" if settings.gmail_trash_parsed_alerts else "off"
        typer.secho(
            f"gmail token     : ok ({settings.gmail_token_path}), scope {scope}, "
            f"trash parsed alerts {trash}",
            fg=typer.colors.GREEN,
        )
    else:
        typer.secho(
            "gmail token     : missing (run `uv run funnel auth-gmail`)",
            fg=typer.colors.YELLOW,
        )

    profile: Path = settings.profiles_dir / f"{settings.active_profile}.md"
    if profile.is_file():
        typer.secho(f"profile         : ok ({profile})", fg=typer.colors.GREEN)
    else:
        typer.secho(f"profile         : missing file {profile}", fg=typer.colors.YELLOW)

    raise typer.Exit(0 if ok else 1)


if __name__ == "__main__":
    app()
