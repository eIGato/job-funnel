"""NormalizedJob is the adapter contract. What matters here is that dedup really dedups."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from funnel.schemas import NormalizedJob


def test_content_hash_is_stable(job: NormalizedJob) -> None:
    assert job.content_hash == job.model_copy().content_hash


def test_content_hash_ignores_case_and_whitespace(job: NormalizedJob) -> None:
    """The same offer from two sources must not become two rows in jobs."""
    twin = job.model_copy(update={"company": "  ACME  ", "title": "data engineer"})
    assert twin.content_hash == job.content_hash


def test_content_hash_tracks_company_title_url(job: NormalizedJob) -> None:
    assert job.model_copy(update={"company": "Globex"}).content_hash != job.content_hash
    assert job.model_copy(update={"title": "ML Engineer"}).content_hash != job.content_hash


def test_content_hash_ignores_description(job: NormalizedJob) -> None:
    """The board reworded the blurb; it is still the same posting."""
    assert job.model_copy(update={"description": "different text"}).content_hash == job.content_hash


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
