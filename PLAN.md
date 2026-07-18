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

### Profiles (multi-profile shelved 2026-07-18)

Originally planned as three profiles (backend, gameplay, technical game design) scored by
`max` cosine, because the three CVs differ **only** in the header and share their work
experience verbatim (verified by diffing the PDFs) — a single concatenated profile would have
scored "knows these technologies" instead of "wants this job".

That is **shelved.** There are no shipped game projects (only unfinished demos), so a
dedicated gameplay profile is not justified yet. Matching embeds **one active profile**,
`backend.md`. To still catch a hybrid posting ("Unreal developer with backend experience"),
`backend.md` carries one truthful line about gameplay-backend work (LimeCity) and Unreal/C++
from game projects: such a posting overlaps the backend profile and surfaces, while a
pure-gamedev role has little backend overlap and lands lower — which is what we want.

So there is no `matched_profile` field and no `max`/argmax: `match_score` is just
cosine(job, active profile). If shipped game work appears, revive multi-profile — the code
below reads a directory of profiles, so it generalizes; for now the directory has one active
header.

Profiles live in `data/profiles/` (gitignored — personal data). `_experience.md` holds the
shared part and is prepended to the active role header. Files starting with `_` are not
profiles. `gameplay.md` and `techdesign.md` are kept **dormant** (refreshed from the CVs, but
consumed by nothing) so multi-profile can be revived without re-extracting.

The profile is not a CV: nobody reads it, it only feeds the embedding. Contact details are
left out (no semantic value against a posting); detail the public CV omits is included.

No CTO profile: the human declined one. CTO alerts will therefore be scored against the
backend profile and land mid-table. Revisit if the CTO alerts turn out to be noisy.

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

### [x] Phase 2 — Data model + admin
The models from §4, the first Alembic migration, **sqladmin** with CRUD views over the three
models (list_display, filters by status/score).
**Done when:** sqladmin at `/admin` shows the tables and you can create/edit a Job and an
Application by hand.

Verified 2026-07-16 by driving the real forms over HTTP: `/admin` lists Source/Job/Application,
and a Source, a Job and an Application were created and then edited by hand. The criterion
earned its keep — the code looked finished but failed it twice, and both failures were only
visible by actually submitting the forms:

- **Creating a Job was impossible.** `content_hash` is NOT NULL but excluded from the form
  (nobody types a sha256), and only `NormalizedJob` knew how to compute one — the ingest path.
  Every other path hit `NotNullViolation`. Fixed by making `models.compute_content_hash` the
  single definition and deriving the value in a `before_insert`/`before_update` hook, so
  ingest and the admin cannot drift apart and quietly break dedup.
- **The admin deleted data on save.** sqladmin renders relationship fields by default, and
  both `Source.jobs` and `Job.application` are `cascade="all, delete-orphan"`: submitting the
  form with the field empty deleted the orphans. Saving a Source wiped every Job of that
  source; saving a Job deleted its Application together with the cover letter. For a
  review-only admin (invariant 6) guarding a human-in-the-loop draft (invariant 2), that is
  the worst possible failure. Both relationships are now off their forms.

Both are guarded by tests in `tests/test_invariants.py` (verified to fail without the fixes).

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

### Answered (2026-07-15)

- **The CV path and format.** Source of truth: three PDFs in `../eIGato.github.io/cv/`
  (also published at `https://eigato.github.io/cv/Evgenii-Denisov-CV-{backend,developer,game-designer}.pdf`).
  Converted once into `data/profiles/` (gitignored); the markdown there is the working
  artifact, the PDFs stay what the human sends to people. **Awaiting the human's proofread.**
  Splitting into bullets for Phase 5 happens over that markdown (headings + paragraphs).
- **Profiles.** Multi-profile shelved (2026-07-18): one active profile, `backend.md`, with a
  gameplay/UE line so hybrid postings still surface. No shipped game work justifies more. See §4.
- **The cover letter language.** EN by default; RU when the posting itself is Russian. The
  posting's language is detected in **code, not by the LLM** (invariant 4).
- **The embedding model.** Follows from the above: `intfloat/multilingual-e5-small`, since
  RU postings must embed sensibly. e5 requires prefixes — **profile text gets `query: `,
  posting text gets `passage: `**. Missing prefixes degrade e5 silently, with no error.
- **Non-CV experience to include in the profile.** Only the 220 Volt material (load testing,
  Yandex.Tank, the area-code scraper, Docker/GitLab CI). Explicitly **excluded**: TTK
  (networks/switches/DSLAM — would pull network-engineer postings) and Nexign (would pull the
  legacy telecom enterprise the human left). ML stays as the CV already states it.
- **Right to work.** RU citizenship, RU international passport, no other passports or visas.
  Montenegro residence permit via the **digital nomad visa**; lives in Montenegro (CET).
  Sole proprietor / self-employed in **Georgia** at a 1% tax rate — B2B contracts are the
  natural shape here, and net ≈ gross under them. This is what the V4Scale contract used.

  Consequence: **"can travel there" ≠ "can legally work there".** Visa-free entry on a RU
  passport is tourist entry and grants no work rights, so a visa-free country list is useless
  as a filter basis. Filter on **signals in the posting text** instead — `is_remote`, geo
  restrictions, sponsorship markers — which is deterministic and survives rule changes.

- **Geography / filter rules (Phase 4a).**
  - RU and BY locations: hard stop.
  - Remote on a foreign employer: the main stream (exactly what the DNV permits).
  - **Timezone: not filtered at all.** The human will adapt to any zone.
  - **Remote but geo-restricted** ("US only", "must be authorized to work in the UK"):
    **reject**, unless the posting explicitly allows contractor / B2B / independent
    contractor — the human has a Georgian entity for exactly that.
  - **On-site / hybrid without explicit sponsorship: keep, but rank below remote.** Many
    companies sponsor on request without saying so in the text.

  "Rank below remote" is *not* `hard_filter_passed` — that is a bool. `match_score` stays a
  pure cosine (no invented penalty multiplier); the ordering is a composite sort
  **`(is_remote DESC, match_score DESC)`** in the admin and when picking top-N for drafting.

### Still open

- **Montenegro on-site postings**: the DNV is believed to forbid working for local employers,
  which would make them a reject. Not a lawyer, rules change — the human confirms.
- **Which boards actually have an available API/alerts today** — verify while building Phase 3.
- **The LLM provider and model** to default to for pydantic-ai (the human has the key).
  `.env.example` currently suggests a cheap Anthropic model; unconfirmed, and `LLM_API_KEY`
  is empty.
- **The rest of the hard-filter criteria** (Phase 4a): seniority floor, stop-stack.
- **The agent layer:** LangGraph or pydantic-graph.
