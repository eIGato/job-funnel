"""What the system does to the mailbox, beyond reading it: Trash, and only after a commit.

Two sweeps, each behind its own consent switch (CLAUDE.md, invariant 2): conversations that
ended in a rejection (`GMAIL_TRASH_REJECTIONS`) and alerts whose postings are all in the table
(`GMAIL_TRASH_PARSED_ALERTS`). Every database read here is a read-only snapshot, closed before
the first Gmail call; nothing here writes to the database.
"""

from __future__ import annotations

from sqlalchemy import select

from funnel.models import Job, Source


def clear_rejected_mail(application_ids: set[int] | None, *, apply: bool) -> str:
    """Trash the incoming mail of rejected applications' threads, or say what would go.

    `application_ids=None` is every REJECTED application (the backlog). The threads are read
    in one snapshot (`link.rejection_threads` excludes any thread an application still in
    play also owns, and that takes two statements that must agree); Gmail is only called
    once that read-only transaction is over. A status flipped back in the admin between the
    two is the one race left, and it costs a message recoverable from Trash for 30 days.
    """
    from funnel.adapters.gmail import failures_note, trash_messages
    from funnel.db import snapshot_scope
    from funnel.replies.inbox import build_service, incoming_in_threads
    from funnel.replies.link import rejection_threads

    with snapshot_scope() as session:
        threads = sorted(rejection_threads(session, application_ids))
    if not threads:
        return "rejected mail: nothing to clear"
    service = build_service()
    ids = incoming_in_threads(service, threads)
    if not apply:
        return f"rejected mail: would trash {len(ids)} messages in {len(threads)} threads"
    trashed, failures = trash_messages(service.users().messages(), ids)
    return f"rejected mail: trashed {trashed} messages in {len(threads)} threads" + failures_note(
        failures
    )


def clear_alert_backlog(*, apply: bool) -> list[str]:
    """Trash every alert in the inbox whose postings are all in the table already.

    The same promise as `GmailAlertsAdapter.on_committed` — an email goes only once what it
    carried is in the database — kept by checking instead of ingesting: these are the alerts
    beyond the 30-day window, and putting month-old postings into the table now would hand
    expired roles to the shortlist and buy letters for them. An alert with even one posting
    the table lacks is kept and counted, as is one that parsed into nothing (the parser-bug
    backlog, see CLAUDE.md).
    """
    from funnel.adapters.gmail import (
        alert_backlog,
        failures_note,
        gmail_service,
        trash_messages,
    )
    from funnel.db import snapshot_scope
    from funnel.models import SourceKind

    with snapshot_scope() as session:
        sources = [
            (s.id, s.name, dict(s.config))
            for s in session.scalars(
                select(Source).where(Source.kind == SourceKind.GMAIL, Source.enabled.is_(True))
            )
        ]

    lines = []
    for source_id, name, config in sources:
        parsed = [
            (message_id, {job.content_hash_for(source_id) for job in jobs})
            for message_id, jobs in alert_backlog(config)
        ]
        wanted = set().union(*(hashes for _, hashes in parsed))
        with snapshot_scope() as session:
            known = set(
                session.scalars(select(Job.content_hash).where(Job.content_hash.in_(wanted)))
            )
        done = [message_id for message_id, hashes in parsed if hashes and hashes <= known]
        unknown = [hashes - known for _, hashes in parsed if hashes - known]
        empty = sum(1 for _, hashes in parsed if not hashes)
        kept = (
            f"kept {len(unknown)} with {sum(map(len, unknown))} postings not in the table, "
            f"{empty} that parsed into nothing"
        )
        if not apply:
            lines.append(f"{name}: would trash {len(done)} of {len(parsed)} alerts, {kept}")
            continue
        trashed, failures = trash_messages(gmail_service().users().messages(), done)
        lines.append(
            f"{name}: trashed {trashed} of {len(parsed)} alerts, {kept}" + failures_note(failures)
        )
    return lines


__all__ = ["clear_alert_backlog", "clear_rejected_mail"]
