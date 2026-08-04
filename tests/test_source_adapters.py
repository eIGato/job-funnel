"""Parser tests for the HTTP/RSS source adapters.

These run offline against committed fixtures (real payloads, trimmed): the `parse` seam is
pure, so `fetch()`'s network I/O is never exercised here. Boards change their markup, so a
break in one of these means a fixture needs refreshing, not that the test is wrong.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from funnel.adapters.adzuna import AdzunaAdapter
from funnel.adapters.arbeitnow import ArbeitnowAdapter
from funnel.adapters.remoteok import RemoteOKAdapter
from funnel.adapters.remotive import RemotiveAdapter
from funnel.adapters.teletype import TeletypeAdapter
from funnel.adapters.themuse import TheMuseAdapter
from funnel.adapters.util import looks_remote, strip_canary
from funnel.adapters.weworkremotely import WeWorkRemotelyAdapter
from funnel.matching.filters import passes_hard_filters

FIXTURES = Path(__file__).parent / "fixtures" / "sources"

#: Matches an actual HTML tag, not a literal "<" in prose (e.g. "Ping < 15", "(<4 years)").
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")


def _has_html_tag(text: str) -> bool:
    return _HTML_TAG.search(text) is not None


def _json(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_remoteok_skips_the_metadata_element() -> None:
    jobs = RemoteOKAdapter.parse(_json("remoteok.json"))
    # Fixture holds the legal/metadata element plus two real postings.
    assert len(jobs) == 2
    first = jobs[0]
    assert first.company and first.title
    assert first.is_remote is True
    assert not _has_html_tag(first.description)  # HTML was stripped
    assert first.external_id


def test_remoteok_strips_the_reader_canary() -> None:
    """The "Please mention the word ..." block must never reach the database.

    RemoteOK appends it to the API payload only, and its tag is base64 of the caller's public
    IP. It leaked into four cover-letter drafts before this was stripped, one of which told the
    company "I read the post completely and am READY (#<our IP>)". The fixture keeps the block
    (with a documentation IP) precisely so this stays covered.
    """
    jobs = RemoteOKAdapter.parse(_json("remoteok.json"))
    assert jobs
    for job in jobs:
        assert "mention the word" not in job.description.lower()
        assert "RMjAz" not in job.description  # the base64 IP tag
        assert job.description.endswith("Training & Development")  # real text kept intact


def test_strip_canary_handles_a_truncated_block() -> None:
    """A canary cut short by `clip` still goes, tail and all — it is always appended last."""
    cut = "Real duties.\nPlease mention the word **JOY** and tag RMjAzLjAuMTEzLjc= when app"
    assert strip_canary(cut) == "Real duties."


def test_strip_canary_leaves_an_ordinary_description_alone() -> None:
    body = "We are hiring a backend engineer. Please mention your salary expectations."
    assert strip_canary(body) == body


def test_remotive_ignores_warning_keys() -> None:
    jobs = RemotiveAdapter.parse(_json("remotive.json"))
    assert len(jobs) == 2
    assert all(j.is_remote for j in jobs)
    assert all(j.company and j.title for j in jobs)
    assert all(not _has_html_tag(j.description) for j in jobs)


def test_arbeitnow_reads_the_remote_flag() -> None:
    jobs = ArbeitnowAdapter.parse(_json("arbeitnow.json"))
    assert len(jobs) == 2
    # Fixture is one remote posting and one on-site posting.
    assert {j.is_remote for j in jobs} == {True, False}
    assert all(j.company and j.title and str(j.url) for j in jobs)


def test_weworkremotely_splits_company_from_title() -> None:
    jobs = WeWorkRemotelyAdapter.parse(_text("weworkremotely.rss"))
    assert len(jobs) == 2
    first = jobs[0]
    assert first.company and first.title
    assert ": " not in first.title  # the "Company: " prefix went to `company`
    assert first.is_remote is True
    assert first.location
    assert first.posted_at is not None


def test_teletype_reads_full_body_geo_line_and_sentinel_company() -> None:
    jobs = TeletypeAdapter.parse(_text("teletype.xml"), "testauthor")
    assert len(jobs) == 2
    first = jobs[0]
    assert first.title == "Senior Python Engineer"
    assert first.company == "Teletype (testauthor)"  # blind recruiter post — no employer
    assert "FastAPI" in first.description and not _has_html_tag(first.description)  # full body
    assert first.location == "RELOCATION TO POLAND OR REMOTE FROM EUROPE"  # geo header lifted
    assert first.is_remote is True
    assert str(first.url) == "https://teletype.in/@testauthor/aBc123"  # utm stripped
    assert first.external_id == "aBc123"
    # The geo header feeds the RU/BY hard filter even though the source has no location field.
    russia = jobs[1]
    assert russia.location == "REMOTE RUSSIA"
    assert passes_hard_filters(russia) is False


def test_adzuna_reads_company_and_flags_remote_from_text() -> None:
    jobs = AdzunaAdapter.parse(_json("adzuna.json"))
    assert len(jobs) == 2
    first = jobs[0]
    assert first.company == "Nimbus Labs"
    assert first.title == "Senior Python Backend Developer"  # HTML stripped out of the title
    assert first.is_remote is True  # "remote" in the teaser
    assert first.external_id == "123"
    assert jobs[1].is_remote is False


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Remote-friendly team, work from home whenever you like.", True),
        ("This fully remote W2 contract role offers competitive pay.", True),
        # Row 1342/1348: the words are there, the job is not remote.
        ("Job Location: Chandler, AZ (3 Days onsite, 2 Days work from home) hybrid role", False),
        ("Location: Gdynia / Hybrid (2 days remote)", False),
        ("Malvern, PA (Hybrid 3 days in office and 2 days remote)", False),
        # An outright claim outranks a later mention of office days.
        ("Fully remote. Optional 2 days in the office if you live nearby.", True),
        ("Onsite in Berlin, five days a week.", False),
    ],
)
def test_remote_is_read_in_order_of_specificity(description: str, expected: bool) -> None:
    assert looks_remote("Backend Developer", "Berlin", description) is expected


def test_themuse_reads_full_contents_and_remote_location() -> None:
    jobs = TheMuseAdapter.parse(_json("themuse.json"))
    assert len(jobs) == 2
    first = jobs[0]
    assert first.company == "Nimbus Labs"
    assert "FastAPI" in first.description and not _has_html_tag(first.description)  # full contents
    assert first.is_remote is True  # "Flexible / Remote"
    assert str(first.url).startswith("https://www.themuse.com/")
    assert jobs[1].location == "Berlin, Germany"
    assert jobs[1].is_remote is False


def test_content_hash_is_stable_and_distinct() -> None:
    jobs = RemoteOKAdapter.parse(_json("remoteok.json"))
    hashes = [j.content_hash_for(1) for j in jobs]
    assert len(set(hashes)) == len(hashes)  # distinct postings, distinct hashes
    # Re-parsing yields the same dedup keys (a repeat ingest is a no-op).
    again = RemoteOKAdapter.parse(_json("remoteok.json"))
    assert [j.content_hash_for(1) for j in again] == hashes


@pytest.mark.parametrize(
    "adapter",
    [
        RemoteOKAdapter,
        RemotiveAdapter,
        ArbeitnowAdapter,
        WeWorkRemotelyAdapter,
        TeletypeAdapter,
        AdzunaAdapter,
        TheMuseAdapter,
    ],
)
def test_adapters_are_registered_under_their_name(adapter: type) -> None:
    from funnel import adapters

    assert adapters.registry()[adapter.name] is adapter


def test_arbeitnow_walks_pages_and_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    """The feed rotates fast, so one page is a thin sample and one slice is not the whole board.

    Germany is the relocation destination actually open to the human and this is the feed that
    carries it, so the adapter is configured to see more of it: `pages` deep, once per entry in
    `variants`. Overlap between variants is fine — `_persist` dedups on content_hash.
    """
    import funnel.adapters.arbeitnow as mod

    seen: list[dict[str, object]] = []

    async def fake_get_json(url: str, params: dict[str, object] | None = None) -> object:
        seen.append(dict(params or {}))
        return _json("arbeitnow.json")

    monkeypatch.setattr(mod, "get_json", fake_get_json)
    monkeypatch.setattr(mod, "_PAGE_DELAY", 0)  # the delay is politeness, not behaviour

    jobs = asyncio.run(
        ArbeitnowAdapter(
            {
                "base_url": "https://www.arbeitnow.com/api/job-board-api",
                "pages": 3,
                "variants": [{}, {"visa_sponsorship": "true"}],
            }
        ).fetch()
    )

    assert seen == [
        {"page": 1},
        {"page": 2},
        {"page": 3},
        {"visa_sponsorship": "true", "page": 1},
        {"visa_sponsorship": "true", "page": 2},
        {"visa_sponsorship": "true", "page": 3},
    ]
    assert len(jobs) == 12  # two postings per fixture response, six responses


def test_arbeitnow_defaults_to_one_plain_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """A source seeded before the knobs existed must keep behaving exactly as it did."""
    import funnel.adapters.arbeitnow as mod

    seen: list[dict[str, object]] = []

    async def fake_get_json(url: str, params: dict[str, object] | None = None) -> object:
        seen.append(dict(params or {}))
        return _json("arbeitnow.json")

    monkeypatch.setattr(mod, "get_json", fake_get_json)
    base = {"base_url": "https://www.arbeitnow.com/api/job-board-api"}

    assert len(asyncio.run(ArbeitnowAdapter(base).fetch())) == 2
    assert seen == [{"page": 1}]


def test_arbeitnow_stops_at_the_end_of_a_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty page means the variant is exhausted — do not keep asking for more."""
    import funnel.adapters.arbeitnow as mod

    calls = 0

    async def fake_get_json(url: str, params: dict[str, object] | None = None) -> object:
        nonlocal calls
        calls += 1
        return _json("arbeitnow.json") if calls == 1 else {"data": []}

    monkeypatch.setattr(mod, "get_json", fake_get_json)
    monkeypatch.setattr(mod, "_PAGE_DELAY", 0)
    config = {"base_url": "https://www.arbeitnow.com/api/job-board-api", "pages": 5}

    assert len(asyncio.run(ArbeitnowAdapter(config).fetch())) == 2
    assert calls == 2, "must stop after the first empty page, not walk all five"
