"""Typer CLI: the pipeline entry points. This is what the systemd timer invokes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import typer
from sqlalchemy import select

from funnel import adapters
from funnel.config import get_settings
from funnel.db import session_scope
from funnel.models import Job, Source

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.orm import Session

    from funnel.schemas import NormalizedJob

app = typer.Typer(
    name="funnel",
    help="Deterministic job-search funnel. Never sends applications, only drafts them.",
    no_args_is_help=True,
)


def _persist(session: Session, source: Source, fetched: list[NormalizedJob]) -> int:
    """Store postings, skipping ones already known. Deduplicated on content_hash."""
    if not fetched:
        return 0
    hashes = [j.content_hash for j in fetched]
    known = set(session.scalars(select(Job.content_hash).where(Job.content_hash.in_(hashes))).all())
    new = 0
    for item in fetched:
        if item.content_hash in known:
            continue
        known.add(item.content_hash)  # a source can repeat a posting within one batch
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
                content_hash=item.content_hash,
            )
        )
        new += 1
    return new


@app.command()
def ingest() -> None:
    """Collect postings from every enabled source. Re-running creates no duplicates."""
    with session_scope() as session:
        sources = session.scalars(select(Source).where(Source.enabled.is_(True))).all()
        if not sources:
            typer.secho("No enabled sources. Add them in the admin.", fg=typer.colors.YELLOW)
            raise typer.Exit(0)

        total = 0
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
            typer.echo(f"  {source.name}: fetched {len(fetched)}, new {new}")

        typer.secho(f"ingest: +{total} postings", fg=typer.colors.GREEN)


@app.command()
def match() -> None:
    """Hard filters plus embedding ranking. No LLM, no tokens.

    Incremental: only postings not yet scored (match_score IS NULL) are considered, so a
    re-run is cheap. Rejects stay unscored (NULL) — re-running just re-applies the cheap,
    deterministic filters to them; only survivors are embedded, and once scored they drop out.
    """
    from funnel.matching.embed import cosine_similarity, embed_texts, to_bytes
    from funnel.matching.filters import passes_hard_filters
    from funnel.matching.profile import get_profile_vector

    with session_scope() as session:
        pending = session.scalars(select(Job).where(Job.match_score.is_(None))).all()
        if not pending:
            typer.secho("match: nothing to score", fg=typer.colors.YELLOW)
            return

        survivors: list[Job] = []
        for job in pending:
            job.hard_filter_passed = passes_hard_filters(job)
            if job.hard_filter_passed:
                survivors.append(job)

        if not survivors:
            typer.secho(
                f"match: {len(pending)} scanned, 0 passed the hard filters", fg=typer.colors.YELLOW
            )
            return

        # Postings are the passage side of the e5 pair; the profile is the query side.
        texts = [f"{j.title}\n{j.company}\n{j.description}".strip() for j in survivors]
        matrix = embed_texts(texts, is_query=False)
        scores = cosine_similarity(matrix, get_profile_vector())
        for job, vector, score in zip(survivors, matrix, scores, strict=True):
            job.embedding = to_bytes(vector)
            job.match_score = float(score)

        top = max(scores)
        typer.secho(
            f"match: {len(pending)} scanned, {len(survivors)} scored "
            f"({len(pending) - len(survivors)} filtered out), top score {top:.3f}",
            fg=typer.colors.GREEN,
        )


@app.command()
def draft(
    limit: int = typer.Option(None, help="How many shortlisted postings to process."),
) -> None:
    """Draft cover letters for the top of the shortlist. DOES NOT SEND (invariant 2).

    Idempotent: a posting whose Application has moved past `shortlisted` (already drafted, or
    touched by the human) is skipped, so a re-run neither regenerates nor clobbers a letter.
    """
    from sqlalchemy import desc

    from funnel.drafting.cover_letter import draft_cover_letter
    from funnel.models import Application, ApplicationStatus

    settings = get_settings()
    if not (settings.llm_api_key and settings.llm_api_key.get_secret_value()):
        typer.secho(
            "draft: LLM_API_KEY is empty. Set it in .env (provider for llm_model="
            f"{settings.llm_model}). Drafting needs it; nothing was sent.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)

    # The shortlist order (PLAN.md section 7): remote first, then by score.
    top_n = limit or settings.match_top_k
    with session_scope() as session:
        jobs = session.scalars(
            select(Job)
            .where(Job.match_score.isnot(None), Job.hard_filter_passed.is_(True))
            .order_by(desc(Job.is_remote), desc(Job.match_score))
            .limit(top_n)
        ).all()

        drafted = 0
        for job in jobs:
            application = job.application
            if application is not None and application.status != ApplicationStatus.SHORTLISTED:
                continue  # already drafted or human-touched — never overwrite

            try:
                letter = asyncio.run(draft_cover_letter(job))
            except Exception as exc:  # one bad posting must not sink the batch
                typer.secho(f"  {job.company} — {job.title[:40]}: ERROR {exc}", fg=typer.colors.RED)
                continue

            if application is None:
                application = Application(job_id=job.id)
                session.add(application)
            application.cover_letter = f"Subject: {letter.subject}\n\n{letter.body}"
            application.status = ApplicationStatus.DRAFTED
            if letter.matched_points:
                application.notes = "Leans on: " + "; ".join(letter.matched_points)
            drafted += 1
            typer.echo(f"  drafted: {job.company} — {job.title[:50]}")

        typer.secho(f"draft: {drafted} letters drafted (NOT sent)", fg=typer.colors.GREEN)


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
        jobs = session.scalars(
            select(Job)
            .where(Job.match_score.isnot(None), Job.hard_filter_passed.is_(True))
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
       Sent mail for the message they sent. Only an unambiguous hit is stored.
    2. **Fetch.** Read incoming mail, skipping any message already recorded — `check-replies`
       is idempotent and never re-bills a classification.
    3. **Classify and apply.** An unmatched reply, or one classified below
       `reply_confidence_threshold`, is stored for review but leaves the Application status
       untouched. A wrong auto-status would hide a real interview; an unread row would not.
    """
    from funnel.models import Application, ApplicationStatus, Reply, ReplyType
    from funnel.replies.classify import classify_reply
    from funnel.replies.inbox import build_service, fetch_recent, find_sent_thread
    from funnel.replies.match import match_reply

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
                select(Application).where(
                    Application.status.in_(
                        [
                            ApplicationStatus.SENT,
                            ApplicationStatus.INTERVIEW,
                            ApplicationStatus.REJECTED,
                        ]
                    )
                )
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
        fresh = [m for m in messages if m.gmail_message_id not in seen]

        # 3. Match, classify, apply.
        applied = unmatched = uncertain = 0
        for message in fresh:
            matched = match_reply(message, sent)
            try:
                verdict = asyncio.run(
                    classify_reply(message.subject, message.from_address, message.body)
                )
            except Exception as exc:  # one bad message must not sink the batch
                typer.secho(f"  {message.subject[:40]}: ERROR {exc}", fg=typer.colors.RED)
                continue

            session.add(
                Reply(
                    application_id=matched.id if matched else None,
                    gmail_message_id=message.gmail_message_id,
                    thread_id=message.thread_id or None,
                    from_address=message.from_address,
                    subject=message.subject,
                    body=message.body,
                    received_at=message.received_at,
                    reply_type=verdict.reply_type,
                    confidence=verdict.confidence,
                    reasoning=verdict.reasoning,
                )
            )

            if matched is None:
                unmatched += 1
                continue
            if verdict.confidence < settings.reply_confidence_threshold:
                uncertain += 1
                continue
            if verdict.reply_type is ReplyType.NO_REPLY:
                continue  # an auto-acknowledgement is not an answer; leave the status alone

            matched.reply_type = verdict.reply_type
            matched.reply_at = message.received_at
            matched.status = (
                ApplicationStatus.INTERVIEW
                if verdict.reply_type is ReplyType.INTERVIEW
                else ApplicationStatus.REJECTED
            )
            applied += 1
            typer.echo(
                f"  {verdict.reply_type.value}: {matched.job.company} ({verdict.confidence:.2f})"
            )

        typer.secho(
            f"check-replies: {len(fresh)} new, {applied} applied, {uncertain} below threshold, "
            f"{unmatched} unmatched, {linked} threads linked",
            fg=typer.colors.GREEN,
        )
        if unmatched or uncertain:
            typer.echo("  Review them in the admin under Replies (unmatched have no Application).")


@app.command(name="run-funnel")
def run_funnel(ctx: typer.Context) -> None:
    """Run ingest, then match, then draft. This is what the systemd timer calls.

    `check-replies` is deliberately NOT part of this: it depends on the human having marked
    things sent, and it should not fail a nightly ingest when the Gmail token needs a refresh.
    """
    ctx.invoke(ingest)
    ctx.invoke(match)
    ctx.invoke(draft)


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
    """Authorize Gmail read-only access (one-time, opens a browser). DOES NOT SEND."""
    from funnel.adapters.gmail import get_credentials

    settings = get_settings()
    typer.echo(f"Using client secret : {settings.gmail_credentials_path}")
    typer.echo(f"Token will be saved : {settings.gmail_token_path}")
    typer.echo("A browser window will open for consent (scope: gmail.readonly)...")
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
        typer.secho(f"gmail token     : ok ({settings.gmail_token_path})", fg=typer.colors.GREEN)
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
