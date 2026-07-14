# Job-Search Funnel — implementation plan

A deterministic funnel that finds postings across several sources, ranks them against your
profile, drafts a cover letter, and tracks what was sent and what came back. The LLM is
called in exactly one place. A human presses "send".

**There is no framework skeleton.** This is a batch tool: a set of CLI commands plus a thin
review admin. No Django, and no heavy web framework of any kind.

---

## 0. How to use this file

- Keep it at the repository root.
- Copy the **"Invariants"** section into `CLAUDE.md` so it is always in context.
- Work the phases in order. Each has its own "Done when". Do not start the next one until
  the previous passes its criterion.
- Track progress here: `[ ]` → `[x]`.
- For decisions that are not in this file (source API availability, keys, paths), see
  "Open questions" and **ask the human**. Do not invent them.

---

## 1. The model in one paragraph

Thousands of postings are squeezed down to dozens by **deterministic code and local
embeddings** — free, no tokens. Only the final shortlist gets an LLM: one generated cover
letter per posting. The system delivers a ranked list plus drafts to the human's desk; the
human does the sending.

---

## 2. Invariants (do not break)

1. **We do not scrape LinkedIn.** The account is in active use for a job search and a ban is
   unacceptable. We turn on saved-search email alerts and **parse those emails through the
   Gmail API**. Same trick for any board with alerts.
2. **Human in the loop.** The system NEVER sends applications automatically. The most it does
   is leave a draft and wait.
3. **Embeddings are local, via ONNX (fastembed), without torch.** Not a single token for matching.
4. **The LLM is for the cover letter only**, plus optionally for classifying replies. Not for
   matching, not for filtering, not for ingest.
5. **Cheap model by default.** Frontier only if you explicitly decide so.
6. **Everything is local.** The scheduler is a host systemd timer. AWS is a separate deferred
   phase (Phase 8), not now.
7. **No heavy framework.** The entry points are CLI commands (Typer). The review UI is
   sqladmin, and it is for review only.

---

## 3. Stack (fixed; latest stable as of mid-2026)

| Layer | Choice | Why |
|-------|--------|-----|
| Packages/venv | **uv** (Astral) | the modern standard, one fast tool, `pyproject.toml` |
| Language | **Python 3.14** | the current stable feature series |
| Database | **Postgres 18** (docker) | latest stable (19 is in beta — we skip it) |
| ORM + migrations | **SQLAlchemy 2.0 (typed) + Alembic** | mature typed ORM; SQLModel is sugar, but thinner |
| Schemas/config/validation | **Pydantic v2** | normalized postings, config, structured LLM output |
| CLI / entry points | **Typer** | `ingest`/`match`/`draft`/`run-funnel`, called by systemd |
| Embeddings | **fastembed** (ONNX, no torch) | "text → numpy vector" without torch/CUDA; CPU is plenty |
| Embedding model | `BAAI/bge-small-en-v1.5` or multilingual `intfloat/multilingual-e5-small` | the CV is English, boards are sometimes Russian |
| Vector comparison | **numpy** (default); pgvector optional | at this scale numpy is instant, pgvector is overkill (see §6) |
| LLM layer | **pydantic-ai** | typed, provider-agnostic calls, structured output; relevant to AI roles |
| Admin/review | **sqladmin** (on Starlette) | free CRUD over the shortlist and drafts — Django admin without Django |
| HTTP adapters | **httpx** | async |
| Gmail | **google-api-python-client** | for OAuth; dated, but it handles authorization |
| Agent layer (opt.) | **LangGraph** OR **pydantic-graph** | top-N only (Phase 7); choice below |
| Orchestration | host **systemd timer** → `docker compose run --rm` | your home turf |

**Version notes.**
- Do not pin minor versions from memory — `uv add <package>` will take what is current at
  build time.
- **We do not install torch** (fastembed runs on ONNX). This is a deliberate dodge: on
  Python 3.14 torch's CUDA wheels lag and you end up CPU-only anyway — and for embedding
  inference, ONNX on CPU is enough here. If you later need torch inference on a GPU,
  configure indexes via `[tool.uv.sources]`, but this funnel does not need it.

Docker: `docker compose` (v2) with two services — `db` (Postgres 18) and `app`. The
scheduler lives on the host: `docker compose run --rm app uv run funnel run-funnel`.

---

## 4. Data model (SQLAlchemy 2.0, typed)

**`Source`** — a job source
- `name`, `kind` (`rss` / `api` / `gmail`), `config` (JSONB), `enabled`, `last_run_at`

**`Job`** — a posting
- `source` (FK), `external_id`, `url`, `company`, `title`, `description`, `location`,
  `is_remote` (bool), `posted_at`, `fetched_at`
- `content_hash` (unique — dedup on company+title+url)
- `embedding` (nullable — a float32 vector; stored as `BYTEA`/`JSONB` for the numpy path,
  or as a `vector` column if pgvector is added)
- `hard_filter_passed` (bool), `match_score` (float, nullable)

**`Application`** — an application (one-to-one with Job)
- `job` (FK), `status` (Enum: `shortlisted` / `drafted` / `sent` / `rejected` /
  `interview` / `no_reply`)
- `cover_letter` (text), `sent_at`, `reply_at`, `reply_type`, `notes`

---

## 5. Implementation phases

### [x] Phase 1 — Skeleton
`uv init`, `pyproject.toml`, `docker compose` (`db` + `app`), config via env (Pydantic
Settings), `alembic init`.
**Done when:** `docker compose up` starts Postgres 18; `uv run alembic upgrade head` passes;
the app container connects to the database.

Verified: Postgres 18.4 starts; `alembic upgrade head` applies the initial migration and a
re-run of autogenerate finds no drift; `docker compose run --rm app uv run funnel doctor`
reports `database: ok` from inside the app container. Two traps worth remembering, both
found only by actually running things:
- Postgres 18+ wants a single volume mount at `/var/lib/postgresql`, not at
  `.../data` — the old layout makes the container restart-loop.
- `uv run` re-syncs by default, which pulled dev tooling into the container and needed PyPI
  on every run; the image sets `UV_NO_SYNC=1` so a timer run stays offline-safe.

### [ ] Phase 2 — Data model + admin
The models from §4, the first Alembic migration, **sqladmin** with CRUD views over the three
models (list_display, filters by status/score).
**Done when:** sqladmin at `/admin` shows the tables and you can create/edit a Job and an
Application by hand.

### [ ] Phase 3 — Ingest layer
- The base interface `BaseAdapter.fetch() -> list[NormalizedJob]` (NormalizedJob is a
  Pydantic model).
- Dedup on `content_hash` (a repeat run breeds no duplicates).
- **Adapter 1:** a parser for Gmail alerts (LinkedIn saved-search + board alerts) via the
  Gmail API.
- **Adapter 2:** one JSON/RSS board (Remotive / RemoteOK / WeWorkRemotely — **verify that
  the API is actually available at build time**, do not hardcode it from memory).
- The `funnel ingest` command (Typer).

**Done when:** `funnel ingest` fills `jobs` from ≥1 real source, and a repeat run creates no
duplicates.

### [ ] Phase 4 — Matching
- **4a. Hard filters (code, free):** remote, timezone, seniority, stack, stop-list
  (`security clearance` and friends) → `hard_filter_passed`.
- **4b. Embedding ranking (fastembed):** the CV is embedded once and cached; every posting
  that passes the filter is embedded; numpy cosine → `match_score`; top-k.
- The `funnel match` command.

**Done when:** `funnel match` sets the score and sqladmin shows a ranked shortlist.

### [ ] Phase 5 — Cover letter (RAG)
- The CV is split into bullets/sections. For a given posting, **the same cosine** picks the
  relevant bullets (rather than the whole CV) → that is the RAG: the prompt only gets the
  hooks that match the requirements.
- Generation via **pydantic-ai** (cheap model; the provider changes by config).
- The draft goes into `Application.cover_letter`, status → `drafted`.
- **It does not send. Ever.**

**Done when:** `funnel draft` puts a draft on a shortlisted posting that is readable and
editable in sqladmin.

### [ ] Phase 6 — Tracking + reply handling
- The human sets status `sent` from sqladmin (after sending by hand).
- Incoming: the Gmail API pulls replies; a **pydantic-ai** classifier with structured output
  (Enum `rejection` / `interview` / `no_reply`) sets `reply_type`.

**Done when:** sqladmin shows the funnel with statuses, and incoming replies get a `reply_type`.

### [ ] Phase 7 — Orchestration
- `funnel run-funnel` = `ingest` → `match` → `draft` (no sending).
- A host systemd timer with `Persistent=true`, 3×/day, driving
  `docker compose run --rm app uv run funnel run-funnel`.
- **Optional, agent layer over top-N only:** the graph `decide-worth-it → research-company
  (web search) → draft → critic`.
  - **LangGraph** — a recognizable "agent orchestration" line on a resume.
  - **pydantic-graph** — coherent with the rest of the pydantic stack.
  - The funnel has already squeezed thousands into dozens for free; the agent applies to a
    handful, not to the stream.

**Done when:** the timer runs the whole funnel end to end and a shortlist + drafts arrive at
your desk.

### [ ] Phase 8 — AWS (DEFERRED; do not start until Phases 1–7 work)
A portfolio artifact, not the funnel's home. The shape is **serverless batch**, NOT
EC2 + ALB + RDS.
- EventBridge Scheduler → a task on Fargate (the docker image runs and dies; you pay for
  minutes, not idle time). If a run is < 15 min, use Lambda with a container image.
- State: S3 (index) + DynamoDB (tracking), always-free tier. Do not stand up an
  always-alive RDS.
- Terraform.
- **A budget alarm as the very first action** (it is also an onboarding task AWS pays
  credits for). A Free-Plan account closes itself after 6 months — keep nothing important
  on it.

---

## 6. Concepts you asked to have cleared up

### Where the embeddings for the "numpy filter" come from

An embedding is **not a service and not an API key — it is a model's output.** An
off-the-shelf embedding model (weights download once) turns a string into a fixed-length
vector locally (384 floats, say). We use **fastembed**, which runs the model through the
ONNX runtime: no torch, fast on CPU.

```python
from fastembed import TextEmbedding
import numpy as np

model = TextEmbedding("BAAI/bge-small-en-v1.5")   # or "intfloat/multilingual-e5-small"

cv_vec  = next(model.embed(["...CV text..."]))       # numpy array
job_vec = next(model.embed(["...posting text..."]))  # numpy array

score = np.dot(cv_vec, job_vec) / (np.linalg.norm(cv_vec) * np.linalg.norm(job_vec))  # cosine
```

That is the whole "numpy filter": `embed` gives a numpy array, `dot` gives closeness. No
external service, no torch, and no network after the first weight download.

### Two questions that are easy to conflate (this was the sticking point)

1. **Where the vector comes from** — a local model (the code above). Always local.
2. **Where to store it and how to compare** — two equally valid options:
   - **numpy in memory (default):** at a scale of hundreds to thousands of postings, load
     all vectors, do one matrix cosine against the CV vector, take top-k. Instant, zero
     extra infrastructure.
   - **pgvector (optional):** the vector becomes a Postgres column and the database computes
     similarity. Justified at 100k+ vectors OR for the resume line. At your volume it is
     overkill.

**Matching works without pgvector.** Bolt it on as a separate step if you want the skill.

### What counts as "RAG" here

In Phase 5 the model is fed not the whole CV but the **retrieved** bullets relevant to the
specific posting (via the same cosine). Retrieval → augmentation → generation. The same
trick as "chat with your PDF", except the retrieval is over CV bullets.

---

## 7. Open questions (ask the human, do not invent)

- **The CV path** and format (md / txt / PDF?). How to split it into bullets for Phase 5.
- **Which boards actually have an available API/alerts today** — verify while building Phase 3.
- **The LLM provider and model** to default to for pydantic-ai (the human has the key).
- **The exact hard-filter criteria** (Phase 4a): timezone, seniority floor, stop-stack.
- **The cover letter language** (EN by default?).
- **The agent layer:** LangGraph or pydantic-graph.
