"""Reply-channel detection: it decides the shape of every draft, so it gets its own test."""

from __future__ import annotations

import pytest

from funnel.models import ApplyChannel, detect_apply_channel


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("mailto:jobs@acme.com", ApplyChannel.EMAIL),
        ("MAILTO:Jobs@Acme.com?subject=Hi", ApplyChannel.EMAIL),
        ("  mailto:jobs@acme.com  ", ApplyChannel.EMAIL),
        ("https://t.me/some_hr", ApplyChannel.TELEGRAM),
        ("https://www.t.me/some_hr", ApplyChannel.TELEGRAM),
        ("http://telegram.me/some_hr", ApplyChannel.TELEGRAM),
        ("https://remoteok.com/remote-jobs/123", ApplyChannel.FORM),
        ("https://teletype.in/@courierus/EXXvMk6FF8w", ApplyChannel.FORM),
        ("", ApplyChannel.FORM),
    ],
)
def test_detect_apply_channel(url: str, expected: ApplyChannel) -> None:
    assert detect_apply_channel(url) == expected


def test_detect_apply_channel_is_not_fooled_by_lookalike_hosts() -> None:
    """`t.me.evil.com` is not Telegram — match the host, never a substring of the URL."""
    assert detect_apply_channel("https://t.me.evil.com/phish") == ApplyChannel.FORM
    assert detect_apply_channel("https://example.com/?ref=t.me") == ApplyChannel.FORM


def test_only_sent_statuses_are_scanned_for_replies() -> None:
    """A status that means "nothing went out" must never be scanned for a reply.

    `check-replies` falls back to matching on the sender's domain, so any never-sent application
    left in that set will eventually collect somebody else's mail — a job-alert newsletter
    already landed on one that had been filed as REJECTED (migration 3f9c1d7e5b28).
    """
    from funnel.models import REPLYABLE_STATUSES, ApplicationStatus

    never_sent = {
        ApplicationStatus.SHORTLISTED,
        ApplicationStatus.DRAFTED,
        ApplicationStatus.DECLINED,
        ApplicationStatus.CLOSED,
        ApplicationStatus.UNREACHABLE,
        ApplicationStatus.INELIGIBLE,
    }
    assert never_sent.isdisjoint(REPLYABLE_STATUSES)
    assert ApplicationStatus.SENT in REPLYABLE_STATUSES


def test_a_scam_is_terminal_even_though_a_letter_went_out() -> None:
    """The one status a letter may have gone out for that `check-replies` must still not scan.

    A scam answers — that is what it is for — and `replies/link.py` writes the classifier's
    verdict straight onto the status, so "we would like to schedule an interview" from the
    people who wanted the passport would move the row to INTERVIEW.
    """
    from funnel.models import REPLYABLE_STATUSES, ApplicationStatus

    assert ApplicationStatus.SCAM not in REPLYABLE_STATUSES


def test_a_new_status_still_fits_the_column_in_the_database() -> None:
    """Adding a status costs no DDL only while it fits the width already in the database.

    `Enum(..., native_enum=False)` is a plain VARCHAR sized from the longest member name, and
    the schema was created with "SHORTLISTED" (migration 96f1cdbee2a2). SCAM fit, which is why
    b6e4a90c17d2 is pure data. A longer name than that would silently need an ALTER, and the
    model would go on declaring a width the column does not have.
    """
    from funnel.models import Application

    created_width = len("SHORTLISTED")
    assert Application.__table__.c.status.type.length == created_width
