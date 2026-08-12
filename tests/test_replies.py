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
from funnel.replies.match import (
    company_slug,
    display_name,
    is_board_sender,
    match_reply,
    names_company,
    registrable_domain,
    sender_domain,
)


def _application(
    company: str,
    *,
    thread_id: str | None = None,
    app_id: int = 1,
    sent_at: datetime | None = None,
    title: str = "Backend Developer",
) -> Application:
    """An in-memory Application; no session, no flush — matching never touches the DB."""
    application = Application(id=app_id, thread_id=thread_id, sent_at=sent_at)
    application.job = Job(company=company, title=title, url="https://x.test/1")
    return application


def _matched(*args: object, **kwargs: object) -> Application | None:
    """The application `match_reply` found, or None. Most tests care about nothing else."""
    found = match_reply(*args, **kwargs)  # type: ignore[arg-type]
    return found.application if found else None


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
    matched = _matched(_message(thread_id="t-42", sender="anyone@elsewhere.test"), apps)
    assert matched is apps[0]


def test_matches_on_company_domain() -> None:
    apps = [_application("Proxify AB", app_id=1), _application("Acme Ltd", app_id=2)]
    assert _matched(_message(sender="hr@proxify.io"), apps) is apps[0]


def test_matches_on_company_in_subject_when_sender_is_an_ats() -> None:
    """A form application replies from the ATS, whose domain identifies nobody."""
    apps = [_application("Proxify AB", app_id=1), _application("Acme Ltd", app_id=2)]
    message = _message(sender="no-reply@greenhouse.io", subject="Your application to Proxify")
    assert _matched(message, apps) is apps[0]


def test_freemail_sender_alone_does_not_match() -> None:
    """Nothing about gmail.com says which company wrote — refuse rather than guess."""
    apps = [_application("Acme Ltd", app_id=1)]
    assert _matched(_message(sender="recruiter@gmail.com"), apps) is None


def test_ambiguity_refuses_to_match() -> None:
    """Two equally plausible applications must leave the reply for a human."""
    apps = [_application("Acme Ltd", app_id=1), _application("Acme Group", app_id=2)]
    assert _matched(_message(sender="hr@acme.com"), apps) is None


def test_short_company_names_do_not_match_by_substring() -> None:
    """'Ai' must not match aircall.com."""
    apps = [_application("Ai", app_id=1)]
    assert _matched(_message(sender="jobs@aircall.com"), apps) is None


def test_a_board_name_containing_a_company_name_does_not_match() -> None:
    """The regression: 'join' sits inside 'justjoin', and a job alert became JOIN's answer.

    Two independent guards now stop it — justjoin is a known board, and the containment is
    anchored — so this asserts the outcome rather than either mechanism.
    """
    apps = [_application("JOIN", app_id=1)]
    message = _message(sender='"justjoin.it" <no-reply@justjoin.it>', subject="New jobs for you")
    assert _matched(message, apps) is None


def test_containment_is_anchored_not_free_substring() -> None:
    """A legal tail the domain drops still matches; a name buried mid-word does not."""
    itds = [_application("ITDS Polska Sp. z o.o.", app_id=1)]
    assert _matched(_message(sender="barbara@itds.pl"), itds) is itds[0]
    buried = [_application("Cast", app_id=1)]
    assert _matched(_message(sender="hr@podcastly.test"), buried) is None


@pytest.mark.parametrize(
    "sender",
    [
        "no-reply@us.greenhouse-mail.io",  # the ATS list held the parent domain only
        "no-reply@app.bamboohr.com",
        "no-reply@wysylka.pracuj.pl",  # a board, on one of its several subdomains
        "no-reply@adzuna.nl",  # and on one of its many country domains
    ],
)
def test_platform_subdomains_do_not_reach_company_matching(sender: str) -> None:
    """Every one of these passed an exact-membership test and matched on the company name."""
    from funnel.replies.match import is_generic_sender

    assert is_generic_sender(sender_domain(sender))


def test_unknown_sender_is_unmatched() -> None:
    assert _matched(_message(sender="hr@nowhere.test"), [_application("Acme")]) is None


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


# --------------------------------------------------------------------------------------
# Matching by words. Every case below is a message this mailbox actually received and the
# old slug-containment rule got wrong (measured over 166 stored replies, 2026-08-12).
# --------------------------------------------------------------------------------------


def test_a_company_name_buried_in_a_longer_word_is_not_a_mention() -> None:
    """The dry run's one false positive: 'profil' sits inside 'profile'.

    A Toptal acknowledgement whose body said "your profile" was matched to a Profil Software
    application, because both sides were compared as one run-together string of letters.
    """
    assert not names_company("Your Toptal Application: update your profile", "Profil Software")
    assert names_company("Thanks for applying to Profil Software", "Profil Software")


def test_diacritics_fold() -> None:
    """'Szkoła' lost its ł to the slug and could never match its own subject line."""
    apps = [_application("Fundacja Szkoła w Chmurze", app_id=1)]
    message = _message(sender="hr@szkolawchmurze.test", subject="Szkoła w Chmurze - rekrutacja")
    assert _matched(message, apps) is apps[0]


def test_a_legal_form_the_company_never_writes_in_email() -> None:
    """ "MindPal Sp. z o. o." welcomes you to plain "Mindpal"."""
    apps = [_application("MindPal Sp. z o. o.", app_id=1)]
    assert _matched(_message(subject="Welcome to Mindpal"), apps) is apps[0]


def test_a_three_letter_company_matches_as_a_word() -> None:
    """The four-character floor dropped CGF; a whole-word match does not need it."""
    apps = [_application("CGF", app_id=1)]
    message = _message(
        sender="notifications@app.bamboohr.com", subject="Thank you for applying at CGF"
    )
    assert _matched(message, apps) is apps[0]
    # Still not two characters: an initialism that short means something else too often.
    assert not names_company("Thanks for applying at AI", "AI")


def test_the_display_name_identifies_the_company_when_nothing_else_does() -> None:
    """An ATS mails as itself and signs as the employer; the subject may not name it at all."""
    apps = [_application("Moon Active", app_id=1), _application("Acme Ltd", app_id=2)]
    message = _message(
        sender="Moon Active Hiring Team <no-reply@ashbyhq.com>",
        subject="We've got your application!",
    )
    found = match_reply(message, apps)
    assert found is not None
    assert found.application is apps[0]
    assert found.strategy == "display-name"
    assert found.conclusive is True


def test_two_roles_at_one_company_are_a_tie_broken_by_send_time() -> None:
    """Reddit answered three times while two Reddit applications were open.

    The company is certain and the role is a guess, so the match is inconclusive: it links the
    reply for a human to read and must not move a status.
    """
    older = _application("Reddit", app_id=1, sent_at=datetime(2026, 7, 1, tzinfo=UTC))
    newer = _application("reddit", app_id=2, sent_at=datetime(2026, 8, 1, tzinfo=UTC))
    message = _message(
        sender="no-reply@us.greenhouse-mail.io", subject="Thank you for your interest in Reddit"
    )
    found = match_reply(message, [older, newer])
    assert found is not None
    assert found.application is newer
    assert found.conclusive is False


def test_an_application_sent_after_the_email_arrived_never_wins_the_tie() -> None:
    before = _application("Reddit", app_id=1, sent_at=datetime(2026, 7, 1, tzinfo=UTC))
    after = _application("Reddit", app_id=2, sent_at=datetime(2026, 9, 1, tzinfo=UTC))
    message = _message(subject="Thank you for your interest in Reddit")
    found = match_reply(message, [before, after])
    assert found is not None and found.application is before


def test_two_different_companies_are_still_an_ambiguity() -> None:
    """The tie-break is for one company's several roles, never for two companies."""
    apps = [_application("Acme Ltd", app_id=1), _application("Acme Group", app_id=2)]
    assert _matched(_message(subject="Your application to Acme"), apps) is None


def test_a_name_found_only_in_the_body_is_never_conclusive() -> None:
    """Footers, disclaimers and "powered by" lines live here too."""
    apps = [_application("EuroCert", app_id=1)]
    message = _message(
        sender="no-reply@traffit-mail.com",
        subject="Dziękujemy za Twoją aplikację",
        body="Dzień dobry, dziękujemy za przesłanie aplikacji.\nPozdrawiamy, zespół EuroCert",
    )
    found = match_reply(message, apps)
    assert found is not None
    assert found.strategy == "body"
    assert found.conclusive is False


def test_the_body_is_read_only_at_its_head() -> None:
    """Past the letter it is quoted history and legal boilerplate."""
    apps = [_application("EuroCert", app_id=1)]
    message = _message(subject="Hello", body="x" * 900 + " EuroCert")
    assert _matched(message, apps) is None


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("Moon Active Hiring Team <no-reply@ashbyhq.com>", "Moon Active Hiring Team"),
        ('"justjoin.it" <no-reply@justjoin.it>', "justjoin.it"),
        ("plain@address.test", ""),
    ],
)
def test_display_name(address: str, expected: str) -> None:
    assert display_name(address) == expected


@pytest.mark.parametrize(
    ("sender", "is_board"),
    [
        ("rekomendacje@wysylka.pracuj.pl", True),
        ("no-reply@adzuna.nl", True),
        ("no-reply@jobs.reed.co.uk", True),
        ("jobalerts-noreply@linkedin.com", True),
        # A person at a board is still a board sender: the classification is withheld, the row
        # is not, and a message that matched an application is classified before we get here.
        ("pykarpenko@avito.ru", True),
        # The ATSs must never be skipped — they carry the acknowledgements.
        ("no-reply@us.greenhouse-mail.io", False),
        ("no-reply@ashbyhq.com", False),
        ("hr@acme.com", False),
    ],
)
def test_is_board_sender(sender: str, is_board: bool) -> None:
    assert is_board_sender(sender_domain(sender)) is is_board


def test_a_run_together_company_name_matches_its_written_out_form() -> None:
    """ATS boards hand us the slug; the ATS's own mail writes the words out."""
    apps = [_application("Chaosindustries", app_id=1)]
    message = _message(
        sender="no-reply@us.greenhouse-mail.io",
        subject="Thank you for applying to CHAOS Industries!",
    )
    assert _matched(message, apps) is apps[0]
    # And the other way round, which is how the same company arrives from a different board.
    assert names_company("Applying to Chaosindustries", "CHAOS Industries")
    # Still equality on the run, not containment: no prefix of a longer word counts.
    assert not names_company("chaosindustriesgroup pays well", "CHAOS Industries")


def test_a_board_can_only_match_by_thread() -> None:
    """A digest lists companies by the dozen; one of them naming ours proves nothing.

    justjoin's "Proponowane oferty" recommended a EuroCert role and was matched to the
    EuroCert application through the body (2026-08-12, dry run).
    """
    apps = [_application("EuroCert", app_id=1, thread_id="t-9")]
    digest = _message(
        sender='"justjoin.it" <jobs@hello.justjoin.it>',
        subject="Proponowane oferty - justjoin.it",
        body="Stanowisko: Python Developer w EuroCert",
    )
    assert _matched(digest, apps) is None
    # A board relaying an answer inside a known thread is still a match.
    assert _matched(digest._replace(thread_id="t-9"), apps) is apps[0]
