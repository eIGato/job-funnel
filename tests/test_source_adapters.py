"""Parser tests for the HTTP/RSS source adapters.

These run offline against committed fixtures (real payloads, trimmed): the `parse` seam is
pure, so `fetch()`'s network I/O is never exercised here. Boards change their markup, so a
break in one of these means a fixture needs refreshing, not that the test is wrong.
"""

from __future__ import annotations

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
    hashes = [j.content_hash for j in jobs]
    assert len(set(hashes)) == len(hashes)  # distinct postings, distinct hashes
    # Re-parsing yields the same dedup keys (a repeat ingest is a no-op).
    again = RemoteOKAdapter.parse(_json("remoteok.json"))
    assert [j.content_hash for j in again] == hashes


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
