# job-funnel

A deterministic job-search funnel: it finds postings across several sources, filters and
ranks them against a profile using local embeddings (fastembed/ONNX, zero tokens), drafts
a cover letter, and tracks what was sent and what came back.

A batch tool, not a web service. The entry points are CLI commands driven by a systemd
timer. The only UI is a thin admin for review.

**The system never sends applications.** It writes a draft to the database. A human sends it.

- How it is put together and what must not be broken → [`CLAUDE.md`](CLAUDE.md)
- Step-by-step plan, phases, open questions → [`PLAN.md`](PLAN.md)

## Quick start

```bash
uv sync                        # install dependencies from the lockfile
cp .env.example .env           # fill in the secrets
docker compose up -d db        # start Postgres 18
uv run alembic upgrade head    # apply migrations
uv run funnel doctor           # check config, database, adapters, CV
```

## Pipeline

```bash
uv run funnel ingest       # collect postings from the sources
uv run funnel match        # hard filters + embedding ranking
uv run funnel draft        # draft cover letters (DOES NOT SEND)
uv run funnel run-funnel   # ingest -> match -> draft, end to end
uv run funnel admin        # review UI at http://localhost:8000/admin
```

## Quality

```bash
uv run ruff check . && uv run ruff format .
uv run mypy src
uv run pytest
```

## How systemd drives it

```bash
docker compose run --rm app uv run funnel run-funnel
```
