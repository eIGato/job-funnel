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

from funnel.adapters.arbeitnow import ArbeitnowAdapter
from funnel.adapters.remoteok import RemoteOKAdapter
from funnel.adapters.remotive import RemotiveAdapter
from funnel.adapters.weworkremotely import WeWorkRemotelyAdapter

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


def test_content_hash_is_stable_and_distinct() -> None:
    jobs = RemoteOKAdapter.parse(_json("remoteok.json"))
    hashes = [j.content_hash for j in jobs]
    assert len(set(hashes)) == len(hashes)  # distinct postings, distinct hashes
    # Re-parsing yields the same dedup keys (a repeat ingest is a no-op).
    again = RemoteOKAdapter.parse(_json("remoteok.json"))
    assert [j.content_hash for j in again] == hashes


@pytest.mark.parametrize(
    "adapter",
    [RemoteOKAdapter, RemotiveAdapter, ArbeitnowAdapter, WeWorkRemotelyAdapter],
)
def test_adapters_are_registered_under_their_name(adapter: type) -> None:
    from funnel import adapters

    assert adapters.registry()[adapter.name] is adapter
