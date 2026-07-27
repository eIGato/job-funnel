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
8. **Data-fetch policy (ToS).** Data comes only from official public APIs/feeds and from
   email/Telegram alerts — never a crawl of a board's HTML with a token or cookie. **Telegram
   and LinkedIn accounts are sacred**: LinkedIn is never touched; Telegram is read only through a
   dedicated ingest account (Telethon, read-only) or bypassed via teletype RSS. For other boards,
   an anonymous few-pages-a-day request is fine where ToS enforcement is lax; no heavy crawling.

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
- `apply_channel` (Enum: `email` / `telegram` / `form`) — how the human *replies*, which is a
  different thing from `Source.kind` (how the posting was *found*). It decides the shape of the
  draft: a chat message is two lines with no attachment, an email has a greeting and mentions
  the attached CV, a web form must never mention an attachment. Derived from the URL on insert
  and only when unset, so an adapter that genuinely knows the channel can declare it and the
  human can correct a bad guess in the admin without the next edit clobbering it.
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
- `job` (FK), `status` (Enum: `shortlisted` / `drafted` / `declined` / `sent` / `rejected` /
  `interview` / `no_reply`). `declined` = *we* chose not to apply (the Phase 7 agent's
  decide-worth-it node), distinct from `rejected` = *they* declined us.
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

### [x] Phase 3 — Ingest layer
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

Progress 2026-07-18 — API/RSS side done and verified live; Gmail parser still pending, so the
box stays open. Four boards were verified reachable at build time and wired as adapters
(`remoteok`, `remotive`, `arbeitnow` JSON; `weworkremotely` RSS). `funnel seed-sources` seeds
their verified endpoints into `Source.config` (endpoints live there, not in the modules).
`funnel ingest` pulled 339 real postings; a second run added 0 (dedup on `content_hash`
holds). Two traps, both found only by running it against live data:
- **`api.hh.ru` returns 403** from here regardless of User-Agent (IP/geo block on their API).
  So hh stays on the **email-alert** path, not a direct API. The sample is already captured.
- **`varchar(255)` overflow on ingest.** WeWorkRemotely packs a country list into `region`
  (seen: 1077 chars); `location`/`external_id` were unbounded in `NormalizedJob` while the
  columns are `String(255)`, so the whole batch died on commit with `StringDataRightTruncation`
  (nothing persisted — the run is atomic). Fixed in the contract: `NormalizedJob` now truncates
  those two fields to the column width (they are noise past that, unlike title/company which
  still reject). Guarded by a test.

Progress 2026-07-20 — **Gmail alert parser done; Phase 3 closed.** One `gmail-alerts` source,
three senders: `fetch()` runs the source query, pulls each message `format=raw`, and dispatches
by sender to a per-board parser (hh.ru, career.habr.com, LinkedIn). The pipeline never learns
which boards these are. Parsers are **structural, not textual** — they key on the job-link shape
(`/vacancy/<id>`, `/vacancies/<id>`, `/jobs/view/<id>`) and the per-card lines around it, so the
one-off LinkedIn "your alert has been created" wording (and whatever the later "new jobs" emails
say) is irrelevant. Habr wraps every link in an email-tracking redirect; the real URL is decoded
out of the `url` query param. Verified live against the mailbox: **197 real postings** in one run
(habr 125, hh 60, LinkedIn 12) with company/location/remote extracted. Offline tests run on
redacted `.eml` fixtures under `tests/fixtures/emails/` (real board markup, synthetic postings,
no personal data). The source stays `enabled=False` in the seed — flip it on in the admin once a
fresh alert is in the mailbox. Adding a board later = a new sender branch + a query term; no
pipeline change. Indeed/Glassdoor await their first alert emails.

Progress 2026-07-22 — **Two more senders: Wellfound and Glassdoor.** Both verified against real
sample alerts. Wellfound forced a small generalization: its HTML wraps every posting in an opaque
`links.wellfound.com/s/c/...` tracking redirect with no job id, so a parser now receives both
rendered bodies (`_Alert(html, text)`) and Wellfound reads the `text/plain` alternative, which keeps
the real `wellfound.com/jobs?job_listing_slug=<id>` URLs; it anchors each card on the "Company / N
Employees" line. Glassdoor is HTML: each posting is one `<a>` card, id in the href's `jobListingId`,
with company/title/location lines inside (employer rating and the salary/Easy-Apply/age chrome
stripped). **Glassdoor URL is a chosen canonical** `…/job-listing/j?jl=<id>` built from the id (not
the volatile tracking href) so dedup stays stable across re-alerts — worth a human confirm that it
resolves. Redacted fixtures + tests added. Indeed is the last sender still awaiting a first alert.

Progress 2026-07-27 — **Indeed and Landing.Jobs parsed; every alert sender now has a parser.**
Both verified against real samples (Indeed: 22/22 cards; Landing.Jobs: its single posting).
Indeed reads `text/plain`, one blank-line block per card, and is the first parser to read a card
by **counting in from both ends** — head is title + "Company - Location", tail is snippet +
posting age + link, and the variable middle (salary estimate, "Schnellbewerbung", employer
badges) is skipped by position because those labels are localized to the country site that sent
the alert (the sample is de.indeed.com, in German). The plain-text card also hands us a snippet,
which the HTML buries in table chrome. Landing.Jobs is the thinnest alert yet: an `<ol>` of bare
"Title @ Company" links grouped by subscription — no location, no snippet — and one posting
matching two subscriptions is listed twice, so the parser dedups on the posting path. Its hrefs
are per-recipient `ahoy` click redirects, same shape as Habr's, so `_habr_destination` became the
shared `_redirect_target`. **Both URLs are rebuilt from the id** (`…/viewjob?jk=<id>`,
`landing.jobs/at/<company>/<slug>`): Indeed states outright that its links are personalized, and
none of that belongs in the database. `_paragraphs` now sits on a line-preserving `_blocks`
(Wellfound wants the lines joined, Indeed wants them apart). Redacted fixtures + tests added; the
seed query and the reply-side sender exclusions grew both senders.

### [ ] Phase 3.5 — Widen the source pool (ongoing)

The funnel's goal is a wider pool (web → web + ETL + AI, globally). Every source below is a new
`BaseAdapter` + `@register`; the pipeline stays source-agnostic. All of it is governed by the
data-fetch policy (invariant 9 / section 2.8): official APIs/feeds and email/Telegram alerts only;
Telegram and LinkedIn accounts are sacred. This phase also feeds Phase 5 — the aggregator/ATS/
teletype sources carry **full descriptions**, which is what makes a real cover letter possible
(email alerts give only card snippets).

- **[x] A. Teletype RSS (`teletype`).** The @Remoteit network publishes every vacancy as a
  `teletype.in` post; the channel is only a subset, so we read the **author's whole feed**, not the
  channel. `teletype.in/rss/{author}` is valid RSS 2.0 with the full body in `content:encoded`
  (verified 2026-07-22). Config holds the author handles — `kovesh` (old), `courierus` (current);
  the handle rotates, so the list is append-only and the current one can be rediscovered from a
  fresh post. Full descriptions, no Telegram, no account risk. Parse position/company/location/
  apply-link structurally (no LLM). **Done when:** `teletype` ingests real posts with full text;
  RU-located ones are dropped by the 4a filter.
- **[x] B. Aggregator APIs (`adzuna`, `themuse`).** Public, free-tier keys, no slugs, full descriptions
  — the answer to "aggregator, not a hand-kept slug list" (a free, current, ToS-clean slug
  aggregator does not exist; the ones that discover slugs are paid Apify scrapers). Adzuna spans
  ~12 countries (broad EU coverage). Keys in `.env` (`ADZUNA_APP_ID`/`ADZUNA_APP_KEY` provided
  2026-07-22; `THEMUSE_API_KEY` optional). OPEN sub-decision: which Adzuna countries and the search
  query (default: the active profile's role keywords). **Done when:** both ingest real postings with
  full descriptions; a repeat run dedups.
- **[ ] C. Telegram channels (`telegram`).** BLOCKED on the human: needs the dedicated ingest
  account (a phone number and a login code). Nothing here can be built ahead of that. Telethon user-session on a **dedicated ingest account —
  not disposable** (the human has 3 phone numbers, one on the main account; keep the session strictly
  read-only and gentle, the account is not expendable). One-time `funnel auth-telegram` stores a
  session under `secrets/`. Per-channel structural parsers (no LLM, invariant 4): `python_vakansii1`
  (structured), `BlockHire` (blockchain, incl. non-smart-contract roles like Zubr), and the
  `@g_jobbot` getmatch bot (getmatch has no public API — read its filtered notifications). Deferred:
  `opento_relocate` (mixed; partly-paywalled WantApply digests). Dropped: `remotegeekjob`
  (heterogeneous LinkedIn/geekjob links, no full text, not parseable without an LLM). **Done when:**
  `telegram` ingests from ≥1 channel via a read-only session.
- **[x] D. Self-growing ATS slugs (`greenhouse` / `lever` / `ashby`).** No hand-kept slug list. Instead:
  when any ingested posting links to an ATS board, extract the company slug and record it; the ATS
  adapters then monitor those slugs via the public no-auth board APIs (full descriptions — Greenhouse
  `boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true`, Lever `api.lever.co/v0/postings/
  {slug}?mode=json`, Ashby `api.ashbyhq.com/posting-api/job-board/{slug}`). Optionally cross-probe a
  new slug on the other two ATSs. Bound the set: prune a slug that 404s or yields no filter-passing
  posting after N runs. OPEN sub-decisions: where slugs live (a small `AtsBoard` table vs
  `Source.config`), the pruning threshold, and whether to cross-probe. **Done when:** a slug spotted
  in a link is auto-monitored on its ATS and yields full-description postings, and the set stays
  bounded.

**Done when (phase):** at least `teletype` + `adzuna` are live and ingesting full-description
postings; the Telegram and ATS-slug tracks are wired or explicitly deferred with their sub-decisions
recorded here.

Progress 2026-07-23 — **D done; only C (Telegram) is left, and it is blocked on the human.** The
three ATS adapters share one base: each run scans the most recent postings for links to *its own*
ATS, records new company slugs, then polls every enabled board. Discovery lives in the adapter, not
in ingest, so the pipeline still knows nothing about specific sources. The plan's three open
sub-decisions are now answered in `models.AtsBoard`: slugs live in their **own table** (each carries
mutable per-slug state that a JSONB blob rewritten by three adapters would turn into lost updates);
a board is **disabled after 4 consecutive barren runs** or a 404, never deleted, so a dead slug is
not rediscovered and re-probed forever; and there is **no cross-probing** a slug on the other two
ATSs (slugs are vendor-namespaced, the hit rate is tiny, and it would triple outbound requests).
All three endpoints verified live: Greenhouse `stripe` → 527 postings with ~3 kB descriptions,
Lever `ro` → 51, Ashby `ramp` → 121 with `isRemote` parsed. Slug discovery over the 339 postings
already in the database finds **nothing yet**, which is expected and not a bug: those came from
boards whose clipped descriptions carry no apply-links. The mechanism starts paying out once
teletype/Adzuna/alert postings — which do carry them — have been ingested.

### [x] Phase 4 — Matching
- **4a. Hard filters (code, free):** remote, timezone, seniority, stack, stop-list
  (`security clearance` and friends) → `hard_filter_passed`.
- **4b. Embedding ranking (fastembed):** the CV is embedded once and cached; every posting
  that passes the filter is embedded; numpy cosine → `match_score`; top-k.
- The `funnel match` command.

**Done when:** `funnel match` sets the score and sqladmin shows a ranked shortlist.

Done 2026-07-22 — verified end to end against the 339 real postings already in the DB. `funnel
match` is incremental (only `match_score IS NULL` is considered) and idempotent: a re-run scores
nothing, and the only repeated work is re-applying the cheap deterministic filters to the hard
rejects. Result: 337 scored, 2 filtered. The top of the shortlist is exactly the target role
(Proxify "Senior Backend/Fullstack Developer (Python)", ~0.855), and a RU-language backend
posting scores on par with its EN twin — the reason for the multilingual e5 model.

- **4a filters** implement PLAN section 7's *answered* geography rules only: RU/BY location is a
  hard stop (keyed on the location field, not a passing mention); a *remote* posting with an
  explicit geo lock is rejected **unless** it welcomes a contractor/B2B arrangement **or still
  admits Europe / worldwide** (the human is in Montenegro). That last clause was added after the
  first live run wrongly filtered A·Team's "must be located in the Americas, Europe, or Israel" —
  a permissive net, not an exclusion. On-site postings are kept (they rank below remote via the
  sort, not this predicate). The seniority floor and stop-stack stay OPEN (section 7) — a small
  `security clearance` seed is the only unconditional stop.
- **4b ranking**: the active profile (`backend.md` header first, then `_experience.md`, so the
  desired-position lines survive e5's ~512-token truncation) is embedded once, `query:`-side;
  each posting is embedded `passage:`-side; numpy cosine → `match_score`. Vectors are cached in
  `Job.embedding` (raw float32). e5-large on CPU took ~11 min cold for 339 postings incl. the
  2.24 GB weight fetch; steady-state is incremental (new postings only), so a timer run is cheap.
- **Shortlist**: the admin sorts `(is_remote DESC, match_score DESC)` — "rank below remote" is
  this sort, not a score penalty. No Application rows are created here; drafting (Phase 5) picks
  top-N.

### [x] Phase 5 — Cover letter (RAG)
- The CV is split into bullets/sections. For a given posting, **the same cosine** picks the
  relevant bullets (rather than the whole CV) → that is the RAG: the prompt only gets the
  hooks that match the requirements.
- Generation via **pydantic-ai** (cheap model; the provider changes by config).
- The draft goes into `Application.cover_letter`, status → `drafted`.
- **It does not send. Ever.**

**Done when:** `funnel draft` puts a draft on a shortlisted posting that is readable and
editable in sqladmin.

Done 2026-07-23 — live-verified: `funnel draft --limit 3` drafted three real letters against the
top of the shortlist (Proxify backend/fullstack) grounded in the retrieved bullets and stored under
`Application.cover_letter` (status `drafted`), readable in the admin. Cost is ~$0.003–0.004/letter
on `claude-haiku-4-5` ($1/$5 per 1M). `funnel draft` walks the top of the
shortlist (`(is_remote DESC, match_score DESC)`, capped at `match_top_k` or `--limit`), drafts a
letter per posting, and writes `Application.cover_letter` with status `drafted`. It never sends
(invariant 2). Idempotent: a posting whose Application has moved past `shortlisted` is skipped, so a
re-run neither regenerates nor clobbers a letter (or a human edit). Details:
- **RAG.** `retrieve_cv_bullets` splits the active profile into bullets, embeds them once (e5
  `passage:` side), and cosine-ranks them against the posting (`query:` side) — the same machinery
  as matching. The prompt gets only the top bullets, with an explicit "invent nothing beyond this".
- **Generation** is pydantic-ai only (invariant 4), model `settings.llm_model` (chosen
  `anthropic:claude-haiku-4-5`, cheap — invariant 5), structured output `CoverLetterDraft`
  (subject / body / matched_points). Letter **language is detected in code** — Cyrillic in the
  posting → Russian, else the configured default (invariant 4), never by the LLM.
- **Key bridge.** pydantic-ai reads the provider's own env var; the adapter copies our single
  `LLM_API_KEY` onto it (`anthropic`→`ANTHROPIC_API_KEY`, etc.). With the key empty, `draft` exits
  with a clear pointer and sends nothing.
- Offline tests use a pydantic-ai `TestModel` + monkeypatched retrieval — no network, no model
  download. `LLM_API_KEY` is now set in `.env` (gitignored).

### [x] Phase 6 — Tracking + reply handling
- The human sets status `sent` from sqladmin (after sending by hand).
- Incoming: the Gmail API pulls replies; a **pydantic-ai** classifier with structured output
  (Enum `rejection` / `interview` / `no_reply`) sets `reply_type`.

**Done when:** sqladmin shows the funnel with statuses, and incoming replies get a `reply_type`.

Built 2026-07-23 — `funnel check-replies`, offline-tested but **not yet live-verified**, because
nothing has been marked `sent` yet: with no sent application there is nothing to correlate, and a
live run would only classify unrelated personal mail and store its bodies in the database. Run it
against the first real answer instead. Three passes, none of which guesses:
1. **Link.** For applications marked sent with no thread yet, search *Sent* mail (the read-only
   scope covers it) for the message the human sent by hand. Only an unambiguous hit is stored, so
   the system learns the thread without ever touching the outbox.
2. **Fetch.** Read incoming mail, skipping anything already recorded. Idempotent on Gmail's
   message id: nothing is re-classified or re-billed.
3. **Classify and apply.** pydantic-ai structured output sets `reply_type`. An unmatched reply, or
   one below `reply_confidence_threshold` (0.7), is **recorded but leaves the Application status
   alone** — a wrong auto-status hides a real interview, an unread row does not.

Correlation (`replies/match.py`) is deterministic and offline-testable: by thread, then sender
domain, then company name in the subject, each requiring a *unique* hit. ATS and freemail domains
are excluded from domain matching — which is exactly the case a `form` application produces, hence
the subject fallback. A new **`Reply`** table rather than columns on Application, because a reply
can match nothing and still need review, the classifier's confidence and reasoning need a home,
and one application can draw several replies (auto-ack, then the real answer). `ON DELETE SET
NULL`: losing an Application must not erase the record that somebody wrote back. The admin gained
a Replies view — unmatched rows have no Application, and linking one by hand is how a human
corrects the machine.

### [x] Phase 7 — Orchestration
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

Progress 2026-07-23 — **units written, install is the human's to run.** `deploy/funnel.service`
(oneshot) + `deploy/funnel.timer` (3x/day, `Persistent=true` so a sleeping laptop runs the missed
occurrence once on the next boot), verified with `systemd-analyze verify`, plus `deploy/README.md`.
A **user** timer, so no root — at the cost of two things that silently break it and are documented
there: `loginctl enable-linger` (or it only runs while logged in) and `docker` group membership.
`check-replies` is deliberately **not** on the schedule: it needs applications the human has marked
sent, it reads the mailbox and calls the LLM, and a Gmail token due for re-auth must not fail a
nightly ingest.

Progress 2026-07-24 — **agent layer built on pydantic-graph; phase closed.** `funnel agent-draft
--limit N` runs the graph `decide-worth-it → research-company → draft → critic` over the very top
of the shortlist. It is a deliberate MANUAL command, NOT on the timer (`run-funnel` stays the cheap
ingest→match→plain-draft path — the funnel already squeezed the stream down for free; the agent
spends tokens only on the handful a human is about to act on). Design:
- **decide-worth-it** carries the soft stop-stack from §7 (is PHP/Node/fullstack the *emphasis*? is
  *training* models the job?) — the whole-posting judgment the 4a regex filters deliberately skip. A
  "no" writes the new `ApplicationStatus.DECLINED` with the reason in notes, and is never re-drafted.
- **research-company** does a provider-native web search (`pydantic_ai.capabilities.WebSearch`, local
  fallback) so the opener is company-specific. A nicety: on failure or `--no-research` the draft still
  happens.
- **draft** reuses the exact Phase 5 core (`cover_letter.generate_letter`, extracted for this), so the
  grounding backstop and anti-cliché rules are identical; it only gets research + critic feedback as
  extra context. The backstop still refuses a fabrication → `DECLINED`, reason recorded.
- **critic** is a second LLM pass; it can bounce the draft back once (`max_revisions=1`), then the
  letter is kept even if unapproved, with the unresolved critique surfaced in notes.
All four calls go through pydantic-ai (invariant 4); the module lives under `orchestration/`, which
the invariant test already allows. Agents are injected via `AgentDeps`, so `tests/test_orchestration.py`
drives the whole graph with a `TestModel` — decline, draft, critic loop, ungrounded refusal — offline.
Not yet live-verified (it spends real tokens + web search); run it against the top of a real shortlist.
**pydantic-graph 2.9.1** is the builder-based redesign: BaseNode nodes wired via `GraphBuilder`, edges
inferred from node return annotations.

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
- **The embedding model.** e5, since RU postings must embed sensibly. e5 requires prefixes —
  **profile text gets `query: `, posting text gets `passage: `**; missing prefixes degrade e5
  silently, with no error. Decided `intfloat/multilingual-e5-small`, but it is absent from
  fastembed 0.8.0, so we run `intfloat/multilingual-e5-large` (same family, human-confirmed
  2026-07-22): 1024-dim, 2.24 GB weights, acceptable on CPU for a 3×/day batch. Verified that a
  RU backing posting then scores on par with its EN twin.
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

- **The LLM provider and model** (2026-07-23). `anthropic:claude-sonnet-5`. Haiku drafted
  competently but the letters read as machine-written; Sonnet 5 at roughly 2x the cost is the
  accepted trade, and it is the "explicit decision" invariant 5 asks for (Sonnet is a mid tier,
  not frontier). Still ~$0.01/letter against a handful of letters per run. The style work that
  came with it lives in `data/profiles/_writing_style.md` (gitignored): a tone anchor built
  from the human's real messages, plus the anti-cliché rules in `drafting/`. The style file
  also drives `Job.apply_channel` (§4) — the channel decides length, greeting and whether an
  attachment may be mentioned.

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

- **Seniority floor and stop-stack (Phase 4a)** (2026-07-24).
  - **Floor is Middle.** A posting whose *title* names a level below Middle (junior / intern /
    trainee / entry-level) with no Middle-or-above level is dropped. A range that reaches the
    floor ("Junior/Middle") is kept; an unspecified level is kept. Read from the title only, so
    "we mentor junior engineers" in a senior role's body does not trip it.
  - **Stop-stack: one hard item — training neural networks as the primary role**, keyed on the
    title. Working *with* AI/LLMs is wanted (the funnel widens toward AI orchestration), so this
    targets model-training titles ("ML Engineer", "Deep Learning Scientist"), not "AI Engineer".
    An ML *platform/infra/backend* title is ordinary backend work and is kept.
  - **Soft preferences are NOT hard filters.** PHP / Node / fullstack as a *secondary* focus, or
    with extra pay, and "is training the priority here?" are judgments about a role's emphasis
    that pure code cannot make well. They are deferred to the **decide-worth-it** node of the
    Phase 7 agent layer, which reads the whole posting — not forced into a regex. This preserves
    the pure-cosine `match_score` decision above.

- **Montenegro on-site** (2026-07-24). **Not filtered.** The local IT market is a fraction of a
  percent of the input stream and the human would take a cheap local gig, so a hard filter would
  cost more in false positives than it could ever save. The DNV question is moot for the funnel.

- **The agent layer: pydantic-graph** (2026-07-24). Coherent with the existing pydantic /
  pydantic-ai stack, lighter, no second ecosystem. LangGraph's resume-line recognizability was
  weighed and declined. Not built yet — this only records the library choice for Phase 7.

### Still open

- **Which boards actually have an available API/alerts today** — verify while building Phase 3.
- **Telegram ingest account** (Phase 3.5 C): a dedicated number + login code. Deferred by the
  human (2026-07-24) — it only widens the ingest funnel and is not needed for the MVP.
