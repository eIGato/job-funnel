"""Data model (PLAN.md section 4).

SQLAlchemy 2.0 typed style: Mapped/mapped_column, never the legacy Column.
"""

from __future__ import annotations

import enum
import hashlib
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def compute_content_hash(company: str, title: str, url: str) -> str:
    """The dedup key: normalized company+title+url.

    The single definition of the hash. `NormalizedJob` (the ingest path) and the `Job` insert
    hook (every other path, including the admin) both call this: two implementations would
    drift and silently break dedup.
    """
    raw = "|".join((company.strip().casefold(), title.strip().casefold(), url.strip().casefold()))
    return hashlib.sha256(raw.encode()).hexdigest()


class SourceKind(enum.StrEnum):
    RSS = "rss"
    API = "api"
    GMAIL = "gmail"


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
    SENT = "sent"
    REJECTED = "rejected"
    INTERVIEW = "interview"
    NO_REPLY = "no_reply"


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
    match_score: Mapped[float | None] = mapped_column(Float)

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
    target.content_hash = compute_content_hash(target.company, target.title, target.url)


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
    reply_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reply_type: Mapped[ReplyType | None] = mapped_column(Enum(ReplyType, native_enum=False))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    job: Mapped[Job] = relationship(back_populates="application")

    def __str__(self) -> str:
        return f"{self.job_id}: {self.status}"
