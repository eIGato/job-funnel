"""Acting on a matched reply: statuses, the re-match pass, and recording a missing application.

`replies.match` decides *which* application an email belongs to and stays pure. This module is
the half that touches the session, so both callers — the `check-replies` scan and the admin's
buttons — go through one implementation and cannot drift apart.

Nothing here sends anything or contacts anything: no Gmail, no model, no network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from funnel.config import get_settings
from funnel.models import Application, ApplicationStatus, Job, Reply, ReplyType, Source, SourceKind
from funnel.replies.inbox import IncomingMessage
from funnel.replies.match import match_reply

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

#: The Source that owns rows the human tells us about rather than rows we found. Its adapter
#: does not exist and must not: `ingest` resolves adapters by `Source.name`, so this row is
#: kept **disabled** and the pipeline never looks at it.
MANUAL_SOURCE = "manual"


def apply_verdict(application: Application, reply: Reply) -> bool:
    """Move an application to what its reply says, if the reply is sure enough to say it.

    The one place a classification becomes a status. An acknowledgement (`NO_REPLY`) is not an
    answer and changes nothing; neither is anything under `reply_confidence_threshold`, because
    a wrong auto-status hides a real interview and an unread row does not.
    """
    if reply.reply_type is None or reply.confidence is None:
        return False
    if reply.confidence < get_settings().reply_confidence_threshold:
        return False
    if reply.reply_type is ReplyType.NO_REPLY:
        return False

    application.reply_type = reply.reply_type
    application.reply_at = reply.received_at
    application.status = (
        ApplicationStatus.INTERVIEW
        if reply.reply_type is ReplyType.INTERVIEW
        else ApplicationStatus.REJECTED
    )
    return True


def as_message(reply: Reply) -> IncomingMessage:
    """A stored reply in the shape the matcher reads, so re-matching is the same code path."""
    return IncomingMessage(
        gmail_message_id=reply.gmail_message_id,
        thread_id=reply.thread_id or "",
        from_address=reply.from_address,
        subject=reply.subject,
        body=reply.body,
        received_at=reply.received_at,
    )


def link(reply: Reply, application: Application, *, conclusive: bool) -> bool:
    """Attach a reply to an application, and let a conclusive one teach us its thread.

    The thread write-back is the important half: almost every application here goes through a
    web form, leaves nothing in Sent mail, and would otherwise never have a thread at all
    (1 of 36 did). With one, every further message in the conversation matches outright.

    Returns whether the application's status moved.
    """
    reply.application_id = application.id
    reply.application = application
    if not conclusive:
        return False
    if reply.thread_id and application.thread_id is None:
        application.thread_id = reply.thread_id
    return apply_verdict(application, reply)


def relink_stored(session: Session, applications: Sequence[Application]) -> tuple[int, int]:
    """Re-match every stored reply that never found an application. No mail, no model call.

    Matching is pure and free, so it re-runs over the whole table rather than over new mail
    only — the same reasoning that makes `match` rescore every posting. A change to the rules,
    or an application that exists only now because the human pressed
    `record_as_application`, then reaches the backlog by itself instead of applying to future
    mail alone. That failure mode has shipped twice in this pipeline already.

    Returns (newly linked, statuses moved).
    """
    linked = applied = 0
    orphans = session.scalars(select(Reply).where(Reply.application_id.is_(None))).all()
    for reply in orphans:
        match = match_reply(as_message(reply), applications)
        if match is None:
            continue
        linked += 1
        applied += link(reply, match.application, conclusive=match.conclusive)
    return linked, applied


def _manual_source(session: Session) -> Source:
    """The disabled Source that owns hand-recorded rows, created on first use."""
    source = session.scalar(select(Source).where(Source.name == MANUAL_SOURCE))
    if source is None:
        source = Source(
            name=MANUAL_SOURCE,
            kind=SourceKind.API,  # nothing fetches it; the kind is bookkeeping
            config={"note": "Applications the human made outside the funnel. No adapter."},
            enabled=False,
        )
        session.add(source)
        session.flush()
    return source


def record_as_application(session: Session, reply: Reply) -> Application | None:
    """Turn an acknowledgement into the sent application it is evidence of.

    Most acknowledgements in this mailbox answer applications the funnel never saw: the human
    applied through a board or a referral, and nothing recorded it, so every later message in
    that conversation is unmatchable and the sent-to-reply rate counts the wrong denominator
    (~20 of 166 replies on 2026-08-12). The email is the only record that exists, so this
    builds the row from it: the classifier's proposed employer and role, `sent_at` from when
    the email arrived, and the thread it came in.

    Deliberately conservative:

    - **The human presses this.** The classifier only ever proposed the names (invariant 2 is
      about not acting for them, and this is an action).
    - **No employer, no row.** A null `detected_company` means the model refused to guess, and
      a row named after a guess is worse than no row.
    - **`sent_at` is when the acknowledgement arrived**, which is the closest honest bound on
      when the letter went out — never a fabricated exact time (see CLAUDE.md on invented
      timestamps). The human corrects it in the admin if they know better.
    - **The same employer and role reuse their row**, so two acknowledgements from one company
      do not collide on `content_hash` or mint two applications.

    Returns the application, or None when the reply cannot become one.
    """
    if reply.application_id is not None or not (reply.detected_company or "").strip():
        return None

    company = reply.detected_company.strip() if reply.detected_company else ""
    #: A role we were not told is left explicit rather than invented: it shows up in the admin
    #: as something to fill in, and it keeps the (company, title) identity honest.
    title = (reply.detected_role or "").strip() or "(role not stated)"
    source = _manual_source(session)

    job = session.scalar(
        select(Job).where(
            Job.source_id == source.id,
            Job.company == company,
            Job.title == title,
        )
    )
    if job is None:
        job = Job(
            source_id=source.id,
            # No posting URL exists — this row is a record of an application, not a candidate
            # for one. An empty URL keeps `detect_apply_channel` on its safe default (form).
            url="",
            company=company,
            title=title,
            description="",
            hard_filter_passed=False,
        )
        session.add(job)
        session.flush()

    application = session.scalar(select(Application).where(Application.job_id == job.id))
    if application is None:
        application = Application(
            job_id=job.id,
            status=ApplicationStatus.SENT,
            sent_at=reply.received_at,
            thread_id=reply.thread_id,
            notes=f"Recorded from reply {reply.id}: {reply.subject}".strip(),
        )
        session.add(application)
        session.flush()

    link(reply, application, conclusive=True)
    return application


__all__ = [
    "MANUAL_SOURCE",
    "apply_verdict",
    "as_message",
    "link",
    "record_as_application",
    "relink_stored",
]
