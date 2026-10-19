"""The `check-replies` scan: read mail, decide what it answers, record it. Sends nothing.

A service, not a CLI body, so the transaction boundaries are visible in one place. The rule
they follow: **no transaction is open across a call to Gmail or to the model.** The scan used to
run in a single session from the first Gmail call to the last classification — a connection
and a snapshot held for minutes, every write of the run lost if any message near the end
raised, and every classification already paid for billed again on the retry. Now:

1. The applications worth matching are read once, in one `REPEATABLE READ, READ ONLY` snapshot,
   and worked on as detached objects (the matcher reads `job.company`, `sent_at`, `thread_id`).
2. **Link.** Sent mail is searched for applications that have no thread yet (Gmail, no
   transaction); a hit is written with a conditional `UPDATE … WHERE thread_id IS NULL`.
3. **Fetch.** Incoming mail (Gmail), minus what is already recorded (snapshot read).
4. **Classify and apply,** oldest first, because an acknowledgement teaches the thread the
   answer arrives in. Each message is classified with no transaction open and then written in
   one of its own: the Reply goes in with `ON CONFLICT DO NOTHING` (a second scan running
   alongside cannot duplicate it, and the loser does not touch the application), the
   application is locked `FOR UPDATE` and re-read, and a status moves only if the row is still
   in a status a reply may move. A crash halfway keeps every message already written.
5. **hh.ru notices** (`replies.hh`), which `fetch_recent` cannot see: a rejection matched by
   vacancy id moves its application like any conclusive rejection; a chat notice, or a
   rejection or message for no application on file, is marked for the Trash.
6. **Relink** stored orphan replies — pure database work, one transaction.

Every pass refuses to guess. An unmatched reply, one matched only inconclusively, or one
classified below the confidence threshold is stored for review and moves nothing: a wrong
auto-status hides a real interview, an unread row does not. A job board's bulk mail that
matches nothing is stored unclassified (no model call), and mail the classifier calls
unrelated to a job search is not stored at all — only its id, so it is never billed again.

The mailbox is only changed after all of that has committed (`clear_mailbox`).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import selectinload

from funnel.db import session_scope, snapshot_scope
from funnel.models import (
    REPLYABLE_STATUSES,
    Application,
    ApplicationStatus,
    Job,
    Reply,
    ReplyType,
    SkippedMessage,
)
from funnel.replies import hh
from funnel.replies.inbox import fetch_recent, find_sent_thread
from funnel.replies.link import is_unrelated, link, relink_stored
from funnel.replies.match import Match, is_board_sender, match_reply, names_company

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from funnel.replies.classify import ReplyClassification
    from funnel.replies.inbox import IncomingMessage

#: (subject, from, body) -> verdict. The model call, injected so the scan never imports a
#: provider and a test can hand it a deterministic fake.
type Classifier = Callable[[str, str, str], Awaitable[ReplyClassification]]


@dataclass
class ScanReport:
    fresh: int = 0
    applied: int = 0
    uncertain: int = 0
    weak: int = 0
    unmatched: int = 0
    bulk: int = 0
    skipped: int = 0
    errors: int = 0
    threads_linked: int = 0
    relinked: int = 0
    hh_rejections: int = 0
    #: One line per status moved, for the operator.
    lines: list[str] = field(default_factory=list)
    #: Applications whose conversations may now be cleared out (`GMAIL_TRASH_REJECTIONS`).
    to_clear: set[int] = field(default_factory=set)
    #: hh notices nothing on file answers, to Trash: (message id, which switch allows it).
    hh_trash: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class _Record:
    """What one incoming email turned out to be, ready to write."""

    message: IncomingMessage
    match: Match | None
    reply_type: ReplyType | None = None
    confidence: float | None = None
    reasoning: str | None = None
    company: str | None = None
    role: str | None = None


class ReplyScan:
    """One run of `check-replies`. Owns its transactions; borrows its Gmail client and model."""

    def __init__(self, service: Any, classify: Classifier, *, threshold: float) -> None:
        self._service = service
        self._classify = classify
        self._threshold = threshold

    # -- reads ------------------------------------------------------------------------------

    @staticmethod
    def _applications() -> list[Application]:
        """Every application a reply may still move, detached, with its job loaded."""
        with snapshot_scope() as session:
            return list(
                session.scalars(
                    select(Application)
                    .where(Application.status.in_(REPLYABLE_STATUSES))
                    .options(selectinload(Application.job))
                )
            )

    @staticmethod
    def _recorded(ids: Sequence[str]) -> set[str]:
        if not ids:
            return set()
        with snapshot_scope() as session:
            return set(
                session.scalars(
                    select(Reply.gmail_message_id).where(Reply.gmail_message_id.in_(ids))
                )
            ) | set(
                session.scalars(
                    select(SkippedMessage.gmail_message_id).where(
                        SkippedMessage.gmail_message_id.in_(ids)
                    )
                )
            )

    @staticmethod
    def _rejected(ids: Sequence[int]) -> set[int]:
        with snapshot_scope() as session:
            return set(
                session.scalars(
                    select(Application.id).where(
                        Application.id.in_(ids), Application.status == ApplicationStatus.REJECTED
                    )
                )
            )

    # -- writes -----------------------------------------------------------------------------

    @staticmethod
    def _write_thread(application: Application, thread_id: str) -> bool:
        """Store a thread learned from Sent mail, unless someone stored one meanwhile."""
        with session_scope() as session:
            written = session.execute(
                update(Application)
                .where(Application.id == application.id, Application.thread_id.is_(None))
                .values(thread_id=thread_id)
            )
        if written.rowcount:  # type: ignore[attr-defined]
            application.thread_id = thread_id
        return bool(written.rowcount)  # type: ignore[attr-defined]

    @staticmethod
    def _skip(message: IncomingMessage) -> None:
        with session_scope() as session:
            session.execute(
                insert(SkippedMessage)
                .values(gmail_message_id=message.gmail_message_id, received_at=message.received_at)
                .on_conflict_do_nothing(index_elements=[SkippedMessage.gmail_message_id])
            )

    def _write(self, record: _Record) -> bool:
        """Store one reply and apply it. Returns whether an application's status moved.

        The application is locked and re-read: it was loaded at the start of the scan, and the
        human may have moved it in the admin since. A status out of `REPLYABLE_STATUSES` now
        keeps the link and loses the verdict, and so does an email older than the reply the
        application's status already rests on.
        """
        message, match = record.message, record.match
        with session_scope() as session:
            reply_id = session.scalar(
                insert(Reply)
                .values(
                    application_id=match.application.id if match else None,
                    gmail_message_id=message.gmail_message_id,
                    thread_id=message.thread_id or None,
                    from_address=message.from_address,
                    subject=message.subject,
                    body=message.body,
                    received_at=message.received_at,
                    reply_type=record.reply_type,
                    confidence=record.confidence,
                    reasoning=record.reasoning,
                    detected_company=record.company,
                    detected_role=record.role,
                )
                .on_conflict_do_nothing(index_elements=[Reply.gmail_message_id])
                .returning(Reply.id)
            )
            if reply_id is None or match is None:
                return False  # recorded by a concurrent scan, or nothing to apply it to
            application = session.get(Application, match.application.id, with_for_update=True)
            reply = session.get(Reply, reply_id)
            if application is None or reply is None:
                return False
            # The thread of a conversation we are now sure about. Learned from the incoming
            # side, which is the only side a web-form application has.
            if message.thread_id and application.thread_id is None:
                application.thread_id = message.thread_id
            moved = False
            # Older than the answer already on file: a late-read email must not overwrite a
            # status a newer one set (an hh rejection notice read after the interview it
            # predates would otherwise undo the interview).
            #
            # An INTERVIEW with no reply on file was set by the human, from evidence that never
            # reached the table (an hh chat, a call); its `updated_at` is the closest thing to
            # when. Application 1521 was moved off such an interview by an hh rejection (received
            # 2026-10-17) on 2026-10-19, before this check existed.
            as_of = application.reply_at
            if as_of is None and application.status is ApplicationStatus.INTERVIEW:
                as_of = application.updated_at
            stale = (
                as_of is not None
                and message.received_at is not None
                and message.received_at < as_of
            )
            if application.status in REPLYABLE_STATUSES and not stale:
                sure = (record.confidence or 0.0) >= self._threshold
                moved = link(reply, application, conclusive=match.conclusive and sure)
            session.flush()
            # Mirror onto the detached copy the rest of the scan matches against.
            match.application.thread_id = application.thread_id
            match.application.status = application.status
        return moved

    # -- passes -----------------------------------------------------------------------------

    def run(self, *, days: int) -> ScanReport:
        report = ScanReport()
        applications = self._applications()
        rejected_before = {a.id for a in applications if a.status is ApplicationStatus.REJECTED}
        answered: set[int] = set()

        for application in applications:
            if application.thread_id or application.sent_at is None:
                continue
            thread_id = find_sent_thread(
                self._service, company=application.job.company, sent_at=application.sent_at
            )
            if thread_id and self._write_thread(application, thread_id):
                report.threads_linked += 1

        messages = fetch_recent(self._service, days=days)
        recorded = self._recorded([m.gmail_message_id for m in messages])
        fresh = sorted(
            (m for m in messages if m.gmail_message_id not in recorded),
            key=lambda m: (m.received_at is not None, m.received_at),
        )
        report.fresh = len(fresh)
        for message in fresh:
            self._one(message, applications, report, answered)

        self._hh(applications, report, answered)

        with session_scope() as session:
            current = list(
                session.scalars(
                    select(Application).where(Application.status.in_(REPLYABLE_STATUSES))
                )
            )
            report.relinked, relinked_applied = relink_stored(session, current)
        report.applied += relinked_applied

        rejected_now = self._rejected([a.id for a in applications])
        report.to_clear = (rejected_now - rejected_before) | (answered & rejected_now)
        return report

    def _one(
        self,
        message: IncomingMessage,
        applications: list[Application],
        report: ScanReport,
        answered: set[int],
    ) -> None:
        match = match_reply(message, applications)
        if match is None and is_board_sender(message.from_address):
            self._write(_Record(message, None))
            report.bulk += 1
            return
        try:
            verdict = asyncio.run(
                self._classify(message.subject, message.from_address, message.body)
            )
        except Exception as exc:  # one bad message must not sink the batch
            report.errors += 1
            report.lines.append(f"{message.subject[:40]}: ERROR {type(exc).__name__}: {exc}")
            return
        if is_unrelated(verdict.job_related, match):
            self._skip(message)
            report.skipped += 1
            return

        record = _Record(
            message,
            match,
            reply_type=verdict.reply_type,
            confidence=verdict.confidence,
            reasoning=verdict.reasoning,
            # A proposal for the admin's "Record as sent application", nothing more.
            company=verdict.company,
            role=verdict.role,
        )
        moved = self._write(record)
        if match is None:
            report.unmatched += 1
            return
        answered.add(match.application.id)
        if not match.conclusive:
            report.weak += 1
        elif verdict.confidence < self._threshold:
            report.uncertain += 1
        elif moved:
            report.applied += 1
            report.lines.append(
                f"{verdict.reply_type.value}: {match.application.job.company} "
                f"({verdict.confidence:.2f}, by {match.strategy})"
            )

    def _hh(self, applications: list[Application], report: ScanReport, answered: set[int]) -> None:
        """hh.ru's notices: a rejection by vacancy id, a message by employer, a chat by nothing."""
        notices = list(hh.fetch(self._service))
        recorded = self._recorded([n.gmail_message_id for n in notices])
        by_vacancy = self._hh_applications({n.vacancy_id for n in notices if n.vacancy_id})
        on_hh = [a for a in applications if "hh.ru/vacancy/" in a.job.url]

        for notice in notices:
            if notice.gmail_message_id in recorded:
                continue  # stored on an earlier run and kept on purpose
            message = _as_message(notice)
            if notice.kind is hh.NoticeKind.REJECTION:
                application = by_vacancy.get(notice.vacancy_id or "")
                if application is None:
                    report.hh_trash.append((notice.gmail_message_id, "rejections"))
                    continue
                record = _Record(
                    message,
                    Match(application, "hh vacancy id", conclusive=True),
                    reply_type=ReplyType.REJECTION,
                    confidence=1.0,
                    reasoning=f"hh.ru rejection notice for vacancy {notice.vacancy_id}",
                    company=notice.company,
                )
                if self._write(record):
                    report.hh_rejections += 1
                    report.lines.append(f"rejection: {application.job.company} (hh vacancy id)")
                answered.add(application.id)
            elif notice.kind is hh.NoticeKind.MESSAGE_FROM and notice.company:
                named = [a for a in on_hh if names_company(notice.company, a.job.company)]
                if len(named) != 1:
                    report.hh_trash.append((notice.gmail_message_id, "alerts"))
                    continue
                # Kept in the inbox and linked for the human; a name is not proof of which
                # application, so no status moves on it.
                self._write(_Record(message, Match(named[0], "hh employer", conclusive=False)))
                report.weak += 1
            else:
                report.hh_trash.append((notice.gmail_message_id, "alerts"))

    @staticmethod
    def _hh_applications(vacancy_ids: set[str]) -> dict[str, Application]:
        """Replyable applications on these hh vacancies, keyed by vacancy id."""
        if not vacancy_ids:
            return {}
        vacancy = func.substring(Job.url, r"hh\.ru/vacancy/(\d+)")
        with snapshot_scope() as session:
            rows = session.execute(
                select(vacancy, Application)
                .join(Application, Application.job_id == Job.id)
                .where(vacancy.in_(vacancy_ids), Application.status.in_(REPLYABLE_STATUSES))
                .options(selectinload(Application.job))
            ).all()
        return {str(vacancy_id): application for vacancy_id, application in rows}


def _as_message(notice: hh.Notice) -> IncomingMessage:
    from funnel.replies.inbox import IncomingMessage

    return IncomingMessage(
        gmail_message_id=notice.gmail_message_id,
        thread_id=notice.thread_id,
        from_address="noreply@hh.ru",
        subject=notice.subject,
        body=notice.body,
        received_at=notice.received_at,
    )


def clear_mailbox(
    service: Any, report: ScanReport, *, trash_alerts: bool, trash_rejections: bool
) -> list[str]:
    """After every write of the scan has committed: Trash what each consent switch allows.

    Two things, each behind its own switch: hh notices nothing on file answers, and the
    conversations of applications this scan moved to REJECTED. A failure here loses nothing —
    `funnel tidy-mailbox` reaches the same mail.
    """
    from funnel.adapters.gmail import failures_note, trash_messages

    lines: list[str] = []
    allowed = {"alerts": trash_alerts, "rejections": trash_rejections}
    ids = [message_id for message_id, switch in report.hh_trash if allowed[switch]]
    if ids:
        trashed, failures = trash_messages(service.users().messages(), ids)
        lines.append(
            f"hh notices: trashed {trashed} with no application on file" + failures_note(failures)
        )
    if trash_rejections and report.to_clear:
        from funnel.mailbox import clear_rejected_mail

        lines.append(clear_rejected_mail(report.to_clear, apply=True))
    return lines


__all__ = ["Classifier", "ReplyScan", "ScanReport", "clear_mailbox"]
