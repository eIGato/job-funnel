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

import pytest

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


def test_wellfound_reads_the_plaintext_body_not_the_opaque_html_links() -> None:
    # Wellfound's HTML wraps each posting in a link-less tracking redirect; the real URL and id
    # live only in text/plain. Parsing must come from there.
    jobs = _jobs("wellfound")
    assert len(jobs) == 2
    first = jobs[0]
    assert first.title == "Senior Python Engineer"
    assert first.company == "Nimbus Labs"  # taken from the "Company / N Employees" line
    assert first.is_remote is True
    assert first.location == "Berlin, Lisbon, Remote"  # "Remote only, " prefix stripped
    assert (
        str(first.url)
        == "https://wellfound.com/jobs?job_listing_slug=4468480-senior-python-engineer"
    )
    assert first.external_id == "4468480"
    # A single-city, non-remote posting keeps its city and is not flagged remote.
    assert jobs[1].company == "Orbital Freight"
    assert jobs[1].location == "Amsterdam"
    assert jobs[1].is_remote is False


def test_glassdoor_reads_the_card_anchor_and_builds_a_stable_url() -> None:
    jobs = _jobs("glassdoor")
    assert len(jobs) == 3  # the trailing "See more jobs" link is not a jobListing anchor
    first = jobs[0]
    assert first.title == "Senior Python Developer (m/w/d)"
    assert first.company == "Acme Analytics"  # the " 4.5 ★" employer rating was stripped
    assert first.location == "Berlin"  # salary / Easy Apply / age lines were skipped
    assert first.is_remote is False
    # The URL is a stable canonical built from jobListingId, not the volatile tracking href.
    assert str(first.url) == "https://www.glassdoor.com/job-listing/j?jl=1010200000001"
    assert first.external_id == "1010200000001"
    # Remote is read from the title text.
    assert jobs[1].title.startswith("Backend Engineer") and jobs[1].title.endswith("Remote")
    assert jobs[1].is_remote is True


def test_indeed_counts_the_card_in_from_both_ends_past_the_localized_middle() -> None:
    # The salary / "Aktiver Arbeitgeber" / "Schnellbewerbung" lines sit between the head and the
    # tail of a card and are localized to the country site; the parser must skip them by
    # position, not by label. The stored URL is rebuilt from the job key: every link in the
    # mail is a per-recipient tracking URL.
    jobs = _jobs("indeed")
    assert len(jobs) == 3  # the header and the two footer blocks are not cards
    first = jobs[0]
    assert first.title == "Senior Python Developer"
    assert first.company == "Acme Robotics"
    assert first.location == "Remote"
    assert first.is_remote is True
    assert str(first.url) == "https://de.indeed.com/viewjob?jk=0cbc44a1c73f2ecc"
    assert first.external_id == "0cbc44a1c73f2ecc"
    assert first.description is not None and first.description.startswith("Strong Python")
    # The long middle: company and location still come off the head, the snippet off the tail.
    assert jobs[1].company == "Globex Systems GmbH"
    assert jobs[1].location == "Berlin"
    assert jobs[1].description is not None and jobs[1].description.startswith("Sehr gute")
    # A card with no " - " on its second line is all company, no location.
    assert jobs[2].company == "Initech"
    assert jobs[2].location is None


def test_landing_jobs_decodes_the_click_redirect_and_splits_title_from_company() -> None:
    # Every href is an opaque per-recipient `ahoy` redirect; the posting path only exists in
    # its `url` param. One posting matching two subscriptions is listed twice and must not
    # become two jobs.
    jobs = _jobs("landing.jobs")
    assert len(jobs) == 2
    first = jobs[0]
    assert first.title == "Senior Python Engineer"
    assert first.company == "Acme Robotics"
    assert str(first.url) == "https://landing.jobs/at/acme-robotics/senior-python-engineer"
    assert first.external_id == "acme-robotics/senior-python-engineer"
    assert first.location is None  # the alert carries no location at all
    assert first.is_remote is False
    assert jobs[1].title == "Backend Developer (Remote)"
    assert jobs[1].is_remote is True
    # The "here" / "change your settings" / logo links are not postings.
    assert all("/at/" in str(job.url) for job in jobs)


def test_content_hashes_are_distinct_within_a_message() -> None:
    for name in ("hh", "habr", "linkedin", "wellfound", "glassdoor", "indeed", "landing.jobs"):
        jobs = _jobs(name)
        hashes = [j.content_hash_for(1) for j in jobs]
        assert len(set(hashes)) == len(hashes)


def test_unknown_sender_yields_nothing() -> None:
    assert parse_message("newsletter@unknown-board.example", "<a href='/x'>Job</a>") == []


def test_parsing_ignores_the_subject_wording() -> None:
    """A board's confirmation wording must not gate extraction (see the LinkedIn fixture)."""
    html = (FIXTURES / "linkedin.eml").read_text(encoding="utf-8").split("\n\n", 1)[1]
    assert len(parse_message("jobalerts-noreply@linkedin.com", html)) == 2


def test_adapter_is_registered_under_its_name() -> None:
    assert registry()["gmail-alerts"] is GmailAlertsAdapter


# --------------------------------------------------------------------------------------
# OAuth token handling. These drive `get_credentials`, which WRITES the token file, so the
# settings it reads must be redirected at `funnel.config.get_settings` — the module
# `gmail.get_settings` is a function-local import and patching it there silently does
# nothing. `_real_secrets_untouched` is the seatbelt: an early version of this test patched
# the wrong target and overwrote the developer's actual Gmail token with a stub.
# --------------------------------------------------------------------------------------


@pytest.fixture
def gmail_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """Point Gmail's token/secret paths into tmp, and prove the real ones stay untouched."""
    import funnel.config
    from funnel.config import Settings

    token, secret = tmp_path / "token.json", tmp_path / "client.json"
    secret.write_text("{}", encoding="utf-8")
    real = Settings()
    before = [
        (p, p.read_bytes() if p.exists() else None)
        for p in (real.gmail_token_path, real.gmail_credentials_path)
    ]

    # No raising=False: if this attribute ever stops existing, the test must fail loudly
    # rather than quietly fall through to the real settings again.
    monkeypatch.setattr(
        funnel.config,
        "get_settings",
        lambda: Settings(gmail_token_path=token, gmail_credentials_path=secret),
    )
    yield token, secret

    for path, content in before:
        now = path.read_bytes() if path.exists() else None
        assert now == content, f"the test wrote to a real secrets file: {path}"


class _DeadCreds:
    """A token Google has revoked: it still looks refreshable, but refreshing fails."""

    valid = False
    expired = True
    refresh_token = "stale"

    def refresh(self, _request: object) -> None:
        from google.auth.exceptions import RefreshError

        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    def to_json(self) -> str:
        return '{"token": "fresh"}'


class _FreshCreds(_DeadCreds):
    valid = True

    def refresh(self, _request: object) -> None:  # pragma: no cover - must never be called
        raise AssertionError("a freshly minted token must not need refreshing")


def _mock_browser_flow(monkeypatch: pytest.MonkeyPatch, reached: list[bool]) -> None:
    class _Flow:
        @staticmethod
        def from_client_secrets_file(*_a: object, **_kw: object) -> _Flow:
            return _Flow()

        def run_local_server(self, **_kw: object) -> _FreshCreds:
            reached.append(True)
            return _FreshCreds()

    monkeypatch.setattr("google_auth_oauthlib.flow.InstalledAppFlow", _Flow)


def test_a_revoked_token_does_not_block_reauthorization(
    monkeypatch: pytest.MonkeyPatch, gmail_paths: tuple[Path, Path]
) -> None:
    """Regression: `auth-gmail` could not run while the token it exists to replace was on disk.

    Google revokes a refresh token on a password change, a "remove access", or six months of
    disuse. `creds.refresh()` then raises RefreshError, which used to propagate straight out of
    get_credentials — so the browser flow below it was never reached and the command appeared to
    do nothing at all ("the browser doesn't open").
    """
    from funnel.adapters import gmail

    token, _ = gmail_paths
    token.write_text('{"refresh_token": "stale"}', encoding="utf-8")
    monkeypatch.setattr(
        "google.oauth2.credentials.Credentials.from_authorized_user_file",
        staticmethod(lambda *_a, **_kw: _DeadCreds()),
    )
    reached: list[bool] = []
    _mock_browser_flow(monkeypatch, reached)

    creds = gmail.get_credentials(interactive=True)

    assert reached == [True], "the browser flow was never reached"
    assert creds.valid is True
    assert token.read_text(encoding="utf-8") == '{"token": "fresh"}'


def test_a_malformed_token_file_does_not_block_reauthorization(
    monkeypatch: pytest.MonkeyPatch, gmail_paths: tuple[Path, Path]
) -> None:
    """An empty or truncated token file raises ValueError out of google-auth's loader.

    Same stance as the revoked token: the file that is broken must not be what stops the
    command that replaces it.
    """
    from funnel.adapters import gmail

    token, _ = gmail_paths
    token.write_text("{}", encoding="utf-8")  # what google-auth rejects outright
    reached: list[bool] = []
    _mock_browser_flow(monkeypatch, reached)

    creds = gmail.get_credentials(interactive=True)

    assert reached == [True], "a malformed token file blocked re-authorization"
    assert creds.valid is True


def test_a_malformed_token_is_reported_clearly_when_non_interactive(
    gmail_paths: tuple[Path, Path],
) -> None:
    """The pipeline path must not open a browser; it points at `auth-gmail` instead."""
    from funnel.adapters import gmail

    token, _ = gmail_paths
    token.write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeError, match="auth-gmail"):
        gmail.get_credentials(interactive=False)
