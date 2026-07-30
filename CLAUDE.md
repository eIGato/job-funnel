# CLAUDE.md

Project context for Claude Code. Loaded every session. Keep it short and high-signal.
The step-by-step plan lives in `PLAN.md` (phases, "done when" criteria, open questions).
This file is about how things are wired and what must not be broken.

---

## What this is

A deterministic job-search funnel: it finds postings across several sources, filters and
ranks them against the user's profile with local embeddings, drafts a cover letter, and
tracks what was sent and what came back. **A batch tool, not a web service.** The entry
points are CLI commands driven by a systemd timer. The UI is only a thin review admin.

The goal is to widen the pool of postings (web → web + ETL + AI orchestration, globally)
and to automate the drudgery of applying. It doubles as a portfolio piece for those skills.

---

## Invariants (DO NOT BREAK)

1. **We do not scrape LinkedIn.** The account is in active use for a job search. We use
   LinkedIn saved-search email alerts and **parse those emails through the Gmail API**.
   Same trick for any board that offers alerts.
2. **Human in the loop.** The system NEVER sends applications or emails on its own. The
   most it does is put a draft in the database and wait. A human sends it, by hand.
3. **Embeddings are local, via fastembed (ONNX). No torch.** Not a single token and not a
   single API call for matching or filtering.
4. **The LLM lives only in `drafting/` and `replies/`.** Cover letters and reply
   classification. Not in ingest, not in filters, not in matching. Cheap model by default;
   a frontier model only on an explicit decision.
5. **Everything is local.** The scheduler is a host systemd timer. AWS is a deferred phase
   (see `PLAN.md` §8) and we do not touch it yet.
6. **No heavy framework.** No Django. CLI on Typer, admin on sqladmin, and the admin is for
   review only.
7. **Secrets live only in `.env`** (which is in `.gitignore`). We commit `.env.example`.
   No hardcoded keys, OAuth tokens or connection strings.
8. **Do not invent decisions.** If something is not in this file or in `PLAN.md` (whether a
   source's API is available, the path to the CV, the LLM provider), see "Open questions" in
   `PLAN.md` and **ask the human**.
9. **Data-fetch policy (ToS).** Job data comes only from official public APIs/feeds and from
   email/Telegram alerts. We **never** crawl a board's HTML with any auth token or cookie.
   **Telegram and LinkedIn accounts are sacred** — no automation that risks the human's main
   account: LinkedIn is never touched at all (invariant 1); Telegram is read only through a
   **dedicated ingest account** (Telethon, read-only) or bypassed entirely via teletype RSS.
   For other boards an anonymous request (no token, no cookies) for a handful of pages a day is
   acceptable where ToS enforcement is known to be lax — no aggressive crawling.

---

## Stack (latest stable, mid-2026)

- **uv** — packages and environment (`pyproject.toml`, `uv.lock`)
- **Python 3.14**
- **Postgres 18** (in docker; 19 is in beta, we do not take it)
- **SQLAlchemy 2.0** (typed, `Mapped[...]`) + **Alembic** — ORM and migrations
- **Pydantic v2** — schemas, config (Pydantic Settings), validation
- **Typer** — CLI, the pipeline entry points
- **fastembed** (ONNX, no torch) + **numpy** — embeddings and cosine
- **pydantic-ai** — the LLM layer (typed, provider-agnostic calls, structured output)
- **sqladmin** (on Starlette) — the review admin
- **httpx** — HTTP adapters
- **google-api-python-client** — Gmail (OAuth)
- **LangGraph** _or_ **pydantic-graph** — optional agent layer over top-N (choice in `PLAN.md`)
- Tooling: **ruff** (lint + format), **mypy** (types), **pytest** (tests)

**Do not add torch.** fastembed on ONNX was chosen partly to sidestep torch's lagging CUDA
wheels on Python 3.14. Embedding inference on CPU is fast enough here.

---

## Repository layout

```
.
├── CLAUDE.md
├── PLAN.md
├── pyproject.toml
├── uv.lock
├── .env.example
├── docker-compose.yml          # services: db (postgres:18), app
├── Dockerfile
├── alembic.ini
├── migrations/versions/        # Alembic migrations
├── src/funnel/
│   ├── cli.py                  # Typer app: ingest / match / draft / run-funnel
│   ├── config.py               # Pydantic Settings (reads .env)
│   ├── db.py                   # engine, session
│   ├── models.py               # Source, Job, Application (SQLAlchemy 2.0)
│   ├── schemas.py              # Pydantic: NormalizedJob and friends
│   ├── admin.py                # sqladmin
│   ├── adapters/               # job sources
│   │   ├── base.py             # BaseAdapter.fetch() -> list[NormalizedJob]
│   │   ├── gmail.py            # parser for Gmail alerts
│   │   └── remotive.py         # example JSON/RSS board
│   ├── matching/
│   │   ├── filters.py          # hard filters (code, no LLM)
│   │   └── embed.py            # fastembed + cosine
│   ├── drafting/
│   │   ├── cover_letter.py     # pydantic-ai (the only place that generates)
│   │   └── screen.py           # soft stop-stack: one call, run before any letter
│   ├── replies/
│   │   └── classify.py         # pydantic-ai, structured reply_type output
│   └── orchestration/
│       └── agent.py            # opt. LangGraph/pydantic-graph, top-N only
└── tests/
```

---

## Commands

Locally everything goes through `uv run`. In docker, through `docker compose run --rm app ...`.

```bash
# environment
uv sync                                   # install dependencies from the lockfile
cp .env.example .env                      # fill in the secrets

# database
docker compose up -d db                   # start Postgres 18
uv run alembic revision --autogenerate -m "..."   # new migration (then REVIEW IT)
uv run alembic upgrade head               # apply

# pipeline
uv run funnel ingest                      # collect postings from the sources
uv run funnel match                       # filters + embedding ranking
uv run funnel draft                       # draft cover letters (DOES NOT SEND)
uv run funnel run-funnel                  # ingest -> match -> draft, end to end
uv run funnel doctor                      # check config, database, adapters, CV

# review admin
uv run funnel admin                       # serve sqladmin (or uvicorn funnel.admin:app)

# quality
uv run ruff check . && uv run ruff format .
uv run mypy src
uv run pytest

# how systemd drives it (--build: the code is baked into the image, not bind-mounted,
# so without it the timer runs the checkout as of the last build)
docker compose run --rm --build app uv run funnel run-funnel
```

---

## Conventions

- **English everywhere in the repo.** Code, comments, docstrings, CLI strings and the docs
  are in English. Russian is for conversation only.
- **Types everywhere.** Python 3.14, full annotations. SQLAlchemy in 2.0 style only
  (`Mapped[...]`, `mapped_column`), never the legacy `Column`.
- **A new source is a new adapter.** Subclass `BaseAdapter`, implement `fetch()`, return
  `list[NormalizedJob]`, register it in the adapter registry. **The pipeline must not know
  about specific sources** — no `if source == "linkedin"` in shared code.
- **Dedup at the door.** Every posting has a `content_hash`: company+title plus the board's own
  `external_id` scoped by source, falling back to a normalized URL when the adapter has no id.
  A repeat `ingest` creates no duplicates. **A URL is not an identity** — Adzuna mints a fresh
  `se=` token per API call, which minted a new row (and a new cover letter) on every run until
  2026-07-30. Adapters should always supply `external_id` when the source has one.
- **Schema changes go through Alembic only.** Autogenerate → **read the migration with your
  own eyes** → upgrade. Never touch the database by hand.
- **LLM boundaries.** Model calls live exclusively in `drafting/` and `replies/`, via
  pydantic-ai. Everything else is deterministic code.
- **Two-stage rejection.** `matching/filters.py` decides "is this a job posting, and does it
  clear geography/seniority?" in pure code. `drafting/screen.py` decides "is it the right *kind*
  of job?" in one cheap model call before drafting. Keep them apart: a regex judging emphasis is
  whack-a-mole, and a model re-judging geography overrules a decision the human already made.
- **We send nothing.** There is no code path that sends an email or an application. `draft`
  writes to the database; the human sends it and then sets the status to `sent` in the admin.
- **Multilingual embeddings.** e5, because letters are EN but RU postings must still embed
  sensibly. Decided `intfloat/multilingual-e5-small`, but it is not in fastembed 0.8.0, so we run
  `intfloat/multilingual-e5-large` (same family, human-confirmed 2026-07-22). **e5 requires
  prefixes: profile text gets `query: `, posting text gets `passage: `** (handled in
  `matching/embed.py`). Omitting them degrades scores silently.
- **One active profile (multi-profile shelved).** `data/profiles/` (gitignored) holds
  `_experience.md` (shared) prepended to the active header, `backend.md`. `match_score` is just
  cosine(job, that profile) — no `max`, no `matched_profile`. `backend.md` carries one truthful
  gameplay/UE line so a hybrid posting ("Unreal dev with backend experience") still surfaces.
  `gameplay.md`/`techdesign.md` are dormant (refreshed from the CVs, consumed by nothing) so
  multi-profile can be revived if shipped game work appears. See `PLAN.md` §4.
- **Invariants are tested.** `tests/test_invariants.py` guards the boundaries above (no
  torch, no LLM outside `drafting/`+`replies/`, Gmail scope read-only, no Django, no
  hardcoded secrets). A failure there means a boundary was broken, not that the test is wrong.

---

## Where to look

- **Step-by-step plan, phases, done-when criteria** → `PLAN.md`
- **Open questions for the human** (CV path, available boards, LLM provider, filter
  criteria, letter language, LangGraph vs pydantic-graph) → `PLAN.md` §7
- **Concepts** (where embeddings come from, numpy vs pgvector, what counts as RAG here)
  → `PLAN.md` §6
