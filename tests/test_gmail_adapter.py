"""Parser tests for the Gmail alert adapter.

These run fully offline against redacted .eml fixtures (real board markup, synthetic
postings, no personal data). The `parse_message`/`parse_raw_email` seam is pure, so the
OAuth flow and the Gmail network calls in `fetch()` are never touched here.

The point the fixtures guard: parsing keys on the job-link shapes and the per-card layout,
never on the subject or greeting. The LinkedIn fixture keeps the one-off "has been created"
subject on purpose — a later "new jobs" email must parse the same way.
"""

from __future__ import annotations

from pathlib import Path

from funnel.adapters import registry
from funnel.adapters.gmail import GmailAlertsAdapter, parse_message, parse_raw_email

FIXTURES = Path(__file__).parent / "fixtures" / "emails"


def _jobs(name: str):
    return parse_raw_email((FIXTURES / f"{name}.eml").read_bytes())


def test_hh_extracts_company_location_and_skips_salary() -> None:
    jobs = _jobs("hh")
    assert len(jobs) == 2
    first = jobs[0]
    assert first.title == "Senior Python Developer"
    assert first.company == "Acme Robotics"  # the salary lines above it were skipped
    assert first.location is not None and first.location.startswith("Belgrade")
    assert first.is_remote is True
    assert str(first.url) == "https://hh.ru/vacancy/100000001"  # query stripped
    assert first.external_id == "100000001"
    # The second posting has no salary block and is not marked remote.
    assert jobs[1].company == "Globex LLC"
    assert jobs[1].is_remote is False


def test_habr_reads_labelled_fields_and_decodes_the_tracking_link() -> None:
    jobs = _jobs("habr")
    assert len(jobs) == 2
    first = jobs[0]
    assert first.company == "Raft Digital"
    assert first.location == "Moscow, Saint Petersburg"
    assert first.is_remote is True
    assert "Golang" in first.description  # skills line becomes the description
    assert str(first.url) == "https://career.habr.com/vacancies/1000200001"
    # A remote-only posting carries no city.
    assert jobs[1].location is None
    assert jobs[1].is_remote is True


def test_linkedin_splits_company_and_reads_remote_from_location() -> None:
    jobs = _jobs("linkedin")
    assert len(jobs) == 2
    first = jobs[0]
    assert first.company == "Gurtam"
    assert first.location == "Poland"
    assert first.is_remote is False
    assert str(first.url) == "https://www.linkedin.com/jobs/view/4400000001"
    # The empty logo anchor shares the job id; it must not create a second, title-less card.
    assert jobs[1].company == "Helprise"
    assert jobs[1].is_remote is True  # "Remote (Europe)"


def test_content_hashes_are_distinct_within_a_message() -> None:
    for name in ("hh", "habr", "linkedin"):
        jobs = _jobs(name)
        hashes = [j.content_hash for j in jobs]
        assert len(set(hashes)) == len(hashes)


def test_unknown_sender_yields_nothing() -> None:
    assert parse_message("newsletter@unknown-board.example", "<a href='/x'>Job</a>") == []


def test_parsing_ignores_the_subject_wording() -> None:
    """A board's confirmation wording must not gate extraction (see the LinkedIn fixture)."""
    html = (FIXTURES / "linkedin.eml").read_text(encoding="utf-8").split("\n\n", 1)[1]
    assert len(parse_message("jobalerts-noreply@linkedin.com", html)) == 2


def test_adapter_is_registered_under_its_name() -> None:
    assert registry()["gmail-alerts"] is GmailAlertsAdapter
