"""NormalizedJob is the adapter contract. What matters here is that dedup really dedups."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from funnel.schemas import NormalizedJob


def test_content_hash_is_stable(job: NormalizedJob) -> None:
    assert job.content_hash_for(1) == job.model_copy().content_hash_for(1)


def test_content_hash_ignores_case_and_whitespace(job: NormalizedJob) -> None:
    """A board that recases its own fields must not create a second row."""
    twin = job.model_copy(update={"company": "  ACME  ", "title": "data engineer"})
    assert twin.content_hash_for(1) == job.content_hash_for(1)


def test_content_hash_tracks_company_title_url(job: NormalizedJob) -> None:
    assert job.model_copy(update={"company": "Globex"}).content_hash_for(1) != job.content_hash_for(
        1
    )
    assert job.model_copy(update={"title": "ML Engineer"}).content_hash_for(
        1
    ) != job.content_hash_for(1)


def test_content_hash_ignores_description(job: NormalizedJob) -> None:
    """The board reworded the blurb; it is still the same posting."""
    twin = job.model_copy(update={"description": "different text"})
    assert twin.content_hash_for(1) == job.content_hash_for(1)


def test_content_hash_is_scoped_by_source(job: NormalizedJob) -> None:
    """Two boards numbering their ids from 1 must not collide into one row."""
    numbered = job.model_copy(update={"external_id": "1"})
    assert numbered.content_hash_for(1) != numbered.content_hash_for(2)


def test_external_id_beats_a_volatile_url(job: NormalizedJob) -> None:
    """The Adzuna bug: a fresh per-request `se=` token must not mint a new posting."""
    first = job.model_copy(update={"url": "https://b.example/ad/55?se=AAA", "external_id": "55"})
    second = job.model_copy(update={"url": "https://b.example/ad/55?se=BBB", "external_id": "55"})
    assert first.content_hash_for(3) == second.content_hash_for(3)


def test_tracking_params_are_stripped_without_an_external_id(job: NormalizedJob) -> None:
    """Same posting, no id from the board: the URL still has to normalize to one key."""
    first = job.model_copy(update={"url": "https://b.example/ad/55?utm_source=x&se=AAA"})
    second = job.model_copy(update={"url": "https://b.example/ad/55/"})
    assert first.content_hash_for(3) == second.content_hash_for(3)


def test_rejects_empty_company() -> None:
    with pytest.raises(ValidationError):
        NormalizedJob(url="https://example.com/j/1", company="", title="Dev")


def test_embedding_text_includes_title_and_company(job: NormalizedJob) -> None:
    text = job.embedding_text
    assert "Data Engineer" in text
    assert "Acme" in text


def test_long_location_and_external_id_are_truncated_to_the_column() -> None:
    """A board can pack a whole country list into `location`; it must still fit varchar(255)."""
    job = NormalizedJob(
        url="https://example.com/j/1",
        company="Acme",
        title="Dev",
        location="Anywhere in the World, " + ", ".join(["Country"] * 200),
        external_id="x" * 500,
    )
    assert job.location is not None and len(job.location) <= 255
    assert job.external_id is not None and len(job.external_id) <= 255
    assert job.location.startswith("Anywhere in the World")
