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
    """Hard filters plus embedding ranking. No LLM, no tokens."""
    # TODO Phase 4:
    #   1. Run passes_hard_filters over postings with no hard_filter_passed yet.
    #   2. Embed the survivors in batches into Job.embedding (embed.to_bytes).
    #   3. Embed the CV (cached), cosine_similarity -> Job.match_score, take top-k.
    raise NotImplementedError("Phase 4: matching")


@app.command()
def draft(
    limit: int = typer.Option(None, help="How many shortlisted postings to process."),
) -> None:
    """Draft cover letters for the shortlist. DOES NOT SEND."""
    # TODO Phase 5: take top-k by match_score, draft_cover_letter(job), store into
    #   Application.cover_letter, set status to drafted.
    raise NotImplementedError("Phase 5: drafting")


@app.command(name="run-funnel")
def run_funnel(ctx: typer.Context) -> None:
    """Run ingest, then match, then draft. This is what the systemd timer calls."""
    ctx.invoke(ingest)
    ctx.invoke(match)
    ctx.invoke(draft)


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

    cv: Path = settings.cv_path
    if cv.is_file():
        typer.secho(f"cv              : ok ({cv})", fg=typer.colors.GREEN)
    else:
        typer.secho(f"cv              : missing file {cv}", fg=typer.colors.YELLOW)

    raise typer.Exit(0 if ok else 1)


if __name__ == "__main__":
    app()
