"""Data model (PLAN.md section 4).

SQLAlchemy 2.0 typed style: Mapped/mapped_column, never the legacy Column.
"""

from __future__ import annotations

import enum
import hashlib
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


#: Query parameters that identify a *fetch* rather than a posting. Adzuna mints a fresh `se=`
#: search token on every API call, so the same ad came back under a new URL — and therefore a
#: new content_hash — on every ingest run: 548 rows for 165 real ads, ten identical postings in
#: a row in the admin, and a cover letter drafted for each. Seed list; extend as boards show new
#: volatile parameters. `utm_*` is handled by prefix, below.
_VOLATILE_QUERY_PARAMS = frozenset(
    {"se", "ref", "referer", "referrer", "src", "gclid", "fbclid", "msclkid", "trk", "mc_cid"}
)


def normalize_url(url: str) -> str:
    """Drop the parts of a URL that differ between two fetches of the same posting.

    Casefolds scheme/host, strips `www.` and a trailing slash, removes tracking and session
    parameters, and sorts what survives so parameter order cannot fork the hash. The path keeps
    its case: plenty of boards use case-sensitive slugs.
    """
    parsed = urlparse(url.strip())
    kept = sorted(
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in _VOLATILE_QUERY_PARAMS and not key.casefold().startswith("utm_")
    )
    return urlunparse(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold().removeprefix("www."),
            parsed.path.rstrip("/"),
            "",
            urlencode(kept),
            "",
        )
    )


def compute_content_hash(
    company: str,
    title: str,
    url: str,
    *,
    source_id: int | None = None,
    external_id: str | None = None,
) -> str:
    """The dedup key: company+title, plus the most stable identity available for the posting.

    The single definition of the hash. `NormalizedJob` (the ingest path) and the `Job` insert
    hook (every other path, including the admin) both call this: two implementations would
    drift and silently break dedup.

    The board's own id is preferred over the URL, because a URL is not a stable identity on
    every board (see `_VOLATILE_QUERY_PARAMS`). It is scoped by `source_id` — ids are unique
    within a board, not across boards, and two boards numbering from 1 must not collide. When
    the adapter cannot supply an id, the normalized URL stands in.

    company+title stay in the key either way, so a board that recycles an external id for a
    different posting still cannot collapse two roles into one row.
    """
    if source_id is not None and external_id and external_id.strip():
        identity = f"src:{source_id}|ext:{external_id.strip().casefold()}"
    else:
        identity = normalize_url(url)
    raw = "|".join((company.strip().casefold(), title.strip().casefold(), identity))
    return hashlib.sha256(raw.encode()).hexdigest()


class SourceKind(enum.StrEnum):
    RSS = "rss"
    API = "api"
    GMAIL = "gmail"


class AtsProvider(enum.StrEnum):
    """Applicant tracking systems with a public, no-auth board API (PLAN.md Phase 3.5 D).

    The last two were added 2026-08-03 with name-based board discovery: the point of guessing a
    slug from a company name is to reach the *employer's* own posting, and European employers
    are spread across more vendors than the original three. Measured over 26 real employers in
    the table, greenhouse/lever/ashby covered 14 of 16 boards found — the tail is real but thin,
    which is why these are additions and not replacements.

    Workable is deliberately absent. Its public widget API still names a company but returns an
    empty `jobs` array for every board (checked against veriff, bolt, wolt, glovo, adeva on
    2026-08-03), and there is no other listing endpoint that does not need a per-posting
    shortcode. An adapter for it would spend a request per board per run to return nothing,
    then disable itself on `_MAX_EMPTY_RUNS`.
    """

    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    RECRUITEE = "recruitee"
    SMARTRECRUITERS = "smartrecruiters"


class ApplyChannel(enum.StrEnum):
    """How the human will send the application — NOT how the posting was discovered.

    `Source.kind` is the discovery channel (a Gmail alert, an RSS feed); this is the reply
    channel, and it is what decides the shape of the draft: a chat message is two sentences
    with no attachment, an email has a greeting line and mentions the attached CV, a web form
    must never mention an attachment at all.
    """

    EMAIL = "email"
    TELEGRAM = "telegram"
    FORM = "form"


#: Telegram web hosts. `t.me` is the canonical one; the others are legacy aliases.
_TELEGRAM_HOSTS = frozenset({"t.me", "telegram.me", "telegram.dog"})


def detect_apply_channel(url: str) -> ApplyChannel:
    """Guess the reply channel from the posting URL.

    A guess, not a fact: it is right for `mailto:` and for Telegram links, and falls back to
    `form` for everything else — which is the safe default, since the form rules are the most
    conservative (no attachment mentioned). The human corrects it in the admin when wrong.
    """
    raw = url.strip()
    if raw.lower().startswith("mailto:"):
        return ApplyChannel.EMAIL
    host = (urlparse(raw).hostname or "").lower().removeprefix("www.")
    if host in _TELEGRAM_HOSTS:
        return ApplyChannel.TELEGRAM
    return ApplyChannel.FORM


class ApplicationStatus(enum.StrEnum):
    SHORTLISTED = "shortlisted"
    DRAFTED = "drafted"
    # We chose not to apply — the Phase 7 agent's decide-worth-it node judged the role a poor
    # fit on the soft stop-stack (PLAN.md section 7). Distinct from REJECTED, which is *them*
    # declining *us*. Kept off the plain-draft path so it is never re-drafted by the timer.
    DECLINED = "declined"
    # The posting carries a mandatory requirement the human does not meet and cannot acquire:
    # a language he does not speak, a work authorization nobody will file for him, a licence or
    # a clearance he does not hold. The mirror image of DECLINED — that one is "we do not want
    # this role", this one is "they cannot take us" — and mixing them made both unreadable.
    #
    # It exists for the same reason UNREACHABLE does: it is the input to a hand-maintained list.
    # Every row here is a hard filter that does not exist yet, and it cost a screening call and
    # a letter to discover. Measured on 2026-08-27, before the status existed: of 547 DECLINED
    # rows, 502 were the screen's own verdict (its notes say so) and 45 were the human changing
    # the status by hand after reading the posting — 36 of those after a letter had already been
    # written for it. Re-running today's filters over those 45 explains 5. The other 40 are
    # unreadable: their `notes` hold the drafter's "Leans on:" audit trail and no reason at all.
    #
    # **Put the requirement's own words in `notes`.** Not a paraphrase — the posting's sentence.
    # That is what makes the next filter measurable before it is written: `_NO_SPONSORSHIP` was
    # built by grepping the whole table for the phrasing of three such notes and finding 1,257
    # rows carrying 47 variants of it. A paraphrase greps nothing.
    #
    # Terminal, nothing was sent, so the same NULL contract as CLOSED and absent from
    # REPLYABLE_STATUSES. Geography is deliberately included here even though `filters.py` owns
    # it: the human is recording what he found, not deciding where a rule belongs.
    INELIGIBLE = "ineligible"
    # The posting was closed by the time the human went to apply — no letter ever went out.
    # Neither them declining us (REJECTED) nor us judging the fit (DECLINED), so it gets its own
    # value: recording it as REJECTED counted a refusal against an application that was never
    # sent, and needed a fabricated `reply_at` to look consistent. A CLOSED row keeps `sent_at`,
    # `reply_at` and `reply_type` NULL; `updated_at` is when it was found closed, and anything
    # more precise goes in `notes`. Terminal like the rest, so `shortlist` treats the role as
    # handled and `check-replies` never scans it.
    CLOSED = "closed"
    # The human went to apply and there was no way to submit: the site answers 403 from his
    # region, the apply button is behind a paid tier, the form demands an account he will not
    # open, the link 404s or lands on a board's front page. Not CLOSED — the employer is still
    # hiring, and saying otherwise misreads a wall of ours as a decision of theirs. Not DECLINED
    # either: nobody judged the fit, and DECLINED is what the screen writes.
    #
    # Its point is that it is a defect signal about the *funnel*, not bookkeeping about a role.
    # `apply_route.BLOCKED_HOSTS` already keeps whole hosts out of the shortlist, and both of its
    # entries were found by the human hitting a wall and mentioning it once — when they were
    # found, they held 131 of the ~640 rows above the floor. This status is that report's input:
    # `funnel doctor` groups these rows by host, and a host that keeps appearing is the next
    # entry. Which wall it was goes in `notes`; nothing in `src/` branches on it, so it stays
    # free text rather than an enum column.
    #
    # No letter went out, so `sent_at`/`reply_at`/`reply_type` stay NULL and it is absent from
    # REPLYABLE_STATUSES, on the same contract as CLOSED. `updated_at` is when the wall was hit.
    #
    # It arrives with no data migration on purpose. The 29 CLOSED rows carry no record of what
    # happened — every one of their `notes` holds the drafter's own "Leans on:" audit trail, so
    # which of them were really unreachable is not knowable, and inventing an answer is the
    # habit that produced the fabricated `reply_at` values migration a1c7e35f9b04 had to undo.
    UNREACHABLE = "unreachable"
    # The posting was not a real job: an ad placed to collect passports, fees or documents.
    # Its own value because it is the one terminal status that says nothing about fit and may
    # sit on either side of the send. `sent_at` is kept when a letter did go out — that is what
    # happened, and it is the part worth being able to count (application 166, sent 2026-08-11,
    # a Berlin "Software Developer" that wanted a passport; `filters._is_relocation_scam` now
    # drops that shape before it can be drafted). Recording it as DECLINED said we judged the
    # fit and never applied, which was wrong in both halves.
    SCAM = "scam"
    SENT = "sent"
    REJECTED = "rejected"
    INTERVIEW = "interview"
    NO_REPLY = "no_reply"


#: The statuses `check-replies` scans, i.e. the ones where an incoming email could be an answer
#: to us. Membership means a letter actually went out — which is why CLOSED, UNREACHABLE,
#: INELIGIBLE and DECLINED are not here. Matching a reply to an application that was never sent is a mismatch by construction,
#: and it has already happened: a justjoin.it *job alert* was linked to a never-sent row that had
#: been filed as REJECTED, because the fallback matches on the sender's domain.
#:
#: SCAM is the one status a letter *may* have gone out for that is still kept out of the set.
#: A scam answers — that is the whole point of it — and every answer would be a classification
#: call whose verdict `replies/link.py` writes straight back onto the status, so
#: "we would like to schedule an interview" from the people who wanted the passport would move
#: the row to INTERVIEW. Terminal means terminal.
REPLYABLE_STATUSES = frozenset(
    {ApplicationStatus.SENT, ApplicationStatus.INTERVIEW, ApplicationStatus.REJECTED}
)


class ReplyType(enum.StrEnum):
    REJECTION = "rejection"
    INTERVIEW = "interview"
    NO_REPLY = "no_reply"


class Source(Base):
    """A job source.

    Source-specific details live in `config` (JSONB), never in pipeline code.
    """

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[SourceKind] = mapped_column(Enum(SourceKind, native_enum=False))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    jobs: Mapped[list[Job]] = relationship(back_populates="source", cascade="all, delete-orphan")

    def __str__(self) -> str:
        return self.name


class Job(Base):
    """A job posting.

    Deduplicated on content_hash (company+title+url), so re-running ingest is a no-op
    for postings already seen.
    """

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_shortlist", "hard_filter_passed", "match_score"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    external_id: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    company: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str | None] = mapped_column(String(255))
    is_remote: Mapped[bool] = mapped_column(Boolean, default=False)
    # Derived from `url` on insert (see _fill_apply_channel), then owned by the human: the
    # admin can correct a wrong guess and nothing overwrites it afterwards.
    apply_channel: Mapped[ApplyChannel] = mapped_column(Enum(ApplyChannel, native_enum=False))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    content_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    # Raw float32 bytes: the numpy path is the default (PLAN.md section 6). pgvector is an
    # optional upgrade behind its own migration; matching works without it.
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary)

    hard_filter_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    #: True when this URL leads nowhere the human can apply — a geo-blocked site, a paywalled
    #: apply button (`matching/apply_route.py`). Such a posting keeps its score and its place in
    #: the corpus but gets no shortlist slot. Derived, not typed: `match` recomputes it for every
    #: row on every run, exactly as it does `hard_filter_passed`, so editing `BLOCKED_HOSTS`
    #: takes effect by itself instead of applying to newly ingested rows only.
    apply_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    # Cosine against the profile with the corpus mean removed (matching/embed.centered_similarity),
    # so this is roughly -0.2..0.3 and not the 0.72..0.86 band raw e5 cosine produces. Both are
    # rewritten wholesale on every `match`; NULL means "not on the shortlist".
    match_score: Mapped[float | None] = mapped_column(Float)
    #: Percentage of the shortlist scoring below this posting, 0-100. The reviewable number.
    match_percentile: Mapped[float | None] = mapped_column(Float)

    source: Mapped[Source] = relationship(back_populates="jobs")
    application: Mapped[Application | None] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False
    )

    def __str__(self) -> str:
        return f"{self.company} — {self.title}"


@event.listens_for(Job, "before_insert")
@event.listens_for(Job, "before_update")
def _fill_content_hash(_mapper: object, _connection: object, target: Job) -> None:
    """Derive content_hash on every write path.

    content_hash is NOT NULL but is kept out of the admin form — nobody types a sha256 by
    hand. Without this hook, creating a Job anywhere other than ingest raises NotNullViolation.
    Recomputed on update too, so editing company/title/url keeps the dedup key honest.
    """
    target.content_hash = compute_content_hash(
        target.company,
        target.title,
        target.url,
        source_id=target.source_id,
        external_id=target.external_id,
    )


@event.listens_for(Job, "before_insert")
def _fill_apply_channel(_mapper: object, _connection: object, target: Job) -> None:
    """Derive apply_channel from the URL, but only when nobody has set one.

    Insert-only and non-clobbering on purpose, unlike content_hash: the whole point of putting
    this column in the admin is that the human can override a bad guess. Recomputing it on
    update would silently undo that correction on the next edit.
    """
    if target.apply_channel is None:
        target.apply_channel = detect_apply_channel(target.url)


class Application(Base):
    """An application, one-to-one with a Job.

    There is no code path here that sends anything, and there must never be one. `draft`
    writes cover_letter; the human sends it and then sets status=sent in the admin.
    """

    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), unique=True)
    status: Mapped[ApplicationStatus] = mapped_column(
        Enum(ApplicationStatus, native_enum=False),
        default=ApplicationStatus.SHORTLISTED,
        index=True,
    )
    cover_letter: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The Gmail thread of the application the human sent by hand. Found by `check-replies`
    # searching Sent mail (the read-only scope sees it), or pasted in by the human. Null for a
    # web-form application, where there is no thread to follow and matching falls back to the
    # sender's domain.
    thread_id: Mapped[str | None] = mapped_column(String(255), index=True)
    reply_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reply_type: Mapped[ReplyType | None] = mapped_column(Enum(ReplyType, native_enum=False))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    job: Mapped[Job] = relationship(back_populates="application")
    # No delete-orphan here: a Reply outlives its Application on purpose (see Reply).
    replies: Mapped[list[Reply]] = relationship(back_populates="application")

    def __str__(self) -> str:
        return f"{self.job_id}: {self.status}"


class AtsBoard(Base):
    """A company board on an ATS, discovered rather than hand-listed (PLAN.md Phase 3.5 D).

    DECIDED (2026-07-23), resolving the plan's open sub-decisions:
    - **Its own table, not `Source.config`.** Each slug carries mutable per-slug state — when
      it was last productive, how many empty runs it has had — and a JSONB blob rewritten by
      three adapters on every run would be both awkward and a lost-update waiting to happen.
    - **Pruned after `_MAX_EMPTY_RUNS` consecutive barren runs** (or a 404). Disabled, never
      deleted, so the same slug is not rediscovered and re-probed forever.
    - **No cross-probing** a new slug against the other two ATSs: slugs are namespaced per
      vendor, the hit rate is tiny, and it would triple outbound requests for it.
    """

    __tablename__ = "ats_boards"
    __table_args__ = (UniqueConstraint("provider", "slug", name="uq_ats_boards_provider_slug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[AtsProvider] = mapped_column(Enum(AtsProvider, native_enum=False), index=True)
    slug: Mapped[str] = mapped_column(String(120))
    #: The posting URL the slug was spotted in — the audit trail for "why are we watching this".
    discovered_from: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Consecutive runs that returned nothing. Reset on any success; at the cap, `enabled` goes
    #: false and the board stops costing a request per run.
    empty_runs: Mapped[int] = mapped_column(Integer, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __str__(self) -> str:
        return f"{self.provider}:{self.slug}"


class Reply(Base):
    """One incoming email that looks like an answer to an application.

    Its own table rather than a couple of columns on Application, for three reasons:
    a reply can match nothing (an ATS auto-acknowledgement for a form application, a recruiter
    writing out of the blue) and still needs to be seen; the classifier's confidence and
    reasoning need somewhere to live so a human can audit a call; and one application can draw
    several replies (auto-ack, then a real answer). `Application.reply_type` stays the summary,
    this is the evidence behind it.

    `application_id` is nullable and ON DELETE SET NULL: losing an Application must not erase
    the record that somebody actually wrote back.
    """

    __tablename__ = "replies"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"), index=True
    )
    #: Gmail's message id. Unique, and it is what makes `check-replies` idempotent — a message
    #: already stored is never re-fetched, re-classified or re-billed.
    gmail_message_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    thread_id: Mapped[str | None] = mapped_column(String(255), index=True)
    from_address: Mapped[str] = mapped_column(String(320), default="")
    subject: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    reply_type: Mapped[ReplyType | None] = mapped_column(Enum(ReplyType, native_enum=False))
    confidence: Mapped[float | None] = mapped_column(Float)
    reasoning: Mapped[str | None] = mapped_column(Text)

    #: Who the classifier thinks the email is about. A *proposal*, never a fact: it is what the
    #: admin's "Record as sent application" fills a new row from, and nothing else reads it.
    #: Most acknowledgements this mailbox receives belong to applications the funnel never saw
    #: — the human applied outside it — and this is how such a row gets created from the only
    #: record that exists of it (~20 of 166 replies on 2026-08-12).
    detected_company: Mapped[str | None] = mapped_column(String(255))
    detected_role: Mapped[str | None] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    application: Mapped[Application | None] = relationship(back_populates="replies")

    def __str__(self) -> str:
        return f"{self.from_address}: {self.subject[:60]}"
