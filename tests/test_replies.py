"""Phase 6. Correlation is deterministic code, so it is tested offline and hard.

A wrong match stamps a rejection onto the wrong application and hides a real interview, so
these tests care as much about the refusals (ambiguous -> None) as about the hits.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from pydantic_ai.models.test import TestModel

from funnel.models import Application, Job
from funnel.replies import inbox
from funnel.replies.classify import ReplyClassification, classify_reply, make_agent
from funnel.replies.match import company_slug, match_reply, registrable_domain, sender_domain


def _application(company: str, *, thread_id: str | None = None, app_id: int = 1) -> Application:
    """An in-memory Application; no session, no flush — matching never touches the DB."""
    application = Application(id=app_id, thread_id=thread_id)
    application.job = Job(company=company, title="Backend Developer", url="https://x.test/1")
    return application


def _message(
    *, subject: str = "", sender: str = "", thread_id: str = "", body: str = ""
) -> inbox.IncomingMessage:
    return inbox.IncomingMessage(
        gmail_message_id="m1",
        thread_id=thread_id,
        from_address=sender,
        subject=subject,
        body=body,
        received_at=datetime.now(tz=UTC),
    )


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("jane@acme.com", "acme.com"),
        ("Jane Doe <jane.doe+tag@careers.acme.co.uk>", "careers.acme.co.uk"),
        ("no-reply@ACME.COM", "acme.com"),
        ("not an address", ""),
    ],
)
def test_sender_domain(header: str, expected: str) -> None:
    assert sender_domain(header) == expected


@pytest.mark.parametrize(
    ("domain", "expected"),
    [("acme.com", "acme"), ("careers.acme.co.uk", "acme"), ("proxify.io", "proxify")],
)
def test_registrable_domain(domain: str, expected: str) -> None:
    assert registrable_domain(domain) == expected


@pytest.mark.parametrize(
    ("company", "expected"),
    [
        ("Acme Technologies Ltd", "acme"),
        ("A.Team", "ateam"),
        ("Proxify AB", "proxify"),
        ("Epic Games", "epic"),
        # Every word is noise: keep the whole name rather than reduce it to nothing.
        ("The Studio Group", "thestudiogroup"),
    ],
)
def test_company_slug(company: str, expected: str) -> None:
    assert company_slug(company) == expected


def test_thread_id_wins() -> None:
    apps = [_application("Acme", thread_id="t-42", app_id=1), _application("Other", app_id=2)]
    matched = match_reply(_message(thread_id="t-42", sender="anyone@elsewhere.test"), apps)
    assert matched is apps[0]


def test_matches_on_company_domain() -> None:
    apps = [_application("Proxify AB", app_id=1), _application("Acme Ltd", app_id=2)]
    assert match_reply(_message(sender="hr@proxify.io"), apps) is apps[0]


def test_matches_on_company_in_subject_when_sender_is_an_ats() -> None:
    """A form application replies from the ATS, whose domain identifies nobody."""
    apps = [_application("Proxify AB", app_id=1), _application("Acme Ltd", app_id=2)]
    message = _message(sender="no-reply@greenhouse.io", subject="Your application to Proxify")
    assert match_reply(message, apps) is apps[0]


def test_freemail_sender_alone_does_not_match() -> None:
    """Nothing about gmail.com says which company wrote — refuse rather than guess."""
    apps = [_application("Acme Ltd", app_id=1)]
    assert match_reply(_message(sender="recruiter@gmail.com"), apps) is None


def test_ambiguity_refuses_to_match() -> None:
    """Two equally plausible applications must leave the reply for a human."""
    apps = [_application("Acme Ltd", app_id=1), _application("Acme Group", app_id=2)]
    assert match_reply(_message(sender="hr@acme.com"), apps) is None


def test_short_company_names_do_not_match_by_substring() -> None:
    """'Ai' must not match aircall.com."""
    apps = [_application("Ai", app_id=1)]
    assert match_reply(_message(sender="jobs@aircall.com"), apps) is None


def test_unknown_sender_is_unmatched() -> None:
    assert match_reply(_message(sender="hr@nowhere.test"), [_application("Acme")]) is None


def test_strip_quoted_cuts_the_history_and_clamps_length() -> None:
    body = "Thanks, we would like to talk.\n\nOn Mon, 1 Jan 2026, Evgenii wrote:\n> my letter"
    stripped = inbox.strip_quoted(body)
    assert "we would like to talk" in stripped
    assert "my letter" not in stripped
    assert len(inbox.strip_quoted("x" * 10_000)) <= 4000


def test_classify_reply_returns_structured_output_offline() -> None:
    verdict = asyncio.run(
        classify_reply(
            "Interview?", "hr@acme.com", "Are you free Tuesday?", agent=make_agent(TestModel())
        )
    )
    assert isinstance(verdict, ReplyClassification)
    assert 0.0 <= verdict.confidence <= 1.0


def test_matching_stays_pure() -> None:
    """Guard the boundary: correlation must not reach for the database or the network.

    Checked over the import statements rather than the raw text, so prose in a docstring
    cannot fail it.
    """
    import ast
    from pathlib import Path

    from funnel.replies import match

    tree = ast.parse(Path(match.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    stateful = [m for m in imported if m.startswith(("sqlalchemy", "funnel.db", "httpx", "google"))]
    assert not stateful, f"replies.match imports {stateful}"
