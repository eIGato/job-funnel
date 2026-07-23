"""Cover-letter drafting (Phase 5). Offline: a pydantic-ai TestModel stands in for the LLM,
and the embedding-backed retrieval is monkeypatched, so no network and no model download.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from pydantic_ai.models.test import TestModel

from funnel.drafting import cover_letter
from funnel.drafting.cover_letter import CoverLetterDraft, draft_cover_letter, make_agent

if TYPE_CHECKING:
    import pytest

    from funnel.schemas import NormalizedJob


def test_language_defaults_to_english(job: NormalizedJob) -> None:
    assert cover_letter._detect_language(job) == "en"


def test_language_switches_to_russian_for_a_cyrillic_posting(job: NormalizedJob) -> None:
    ru = job.model_copy(update={"title": "Python-разработчик (Backend)"})
    assert cover_letter._detect_language(ru) == "ru"


def test_profile_bullets_drop_headings_and_short_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    cover_letter._profile_bullets.cache_clear()
    monkeypatch.setattr(
        cover_letter,
        "load_profile_text",
        lambda: "# Backend Developer\nSkills: Python\n- Built payment integrations at scale here",
    )
    bullets = cover_letter._profile_bullets()
    cover_letter._profile_bullets.cache_clear()
    assert any("payment integrations" in b for b in bullets)
    assert not any(b.startswith("#") for b in bullets)  # heading marker stripped


def test_draft_cover_letter_returns_structured_output_offline(
    job: NormalizedJob, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Skip the embedding retrieval entirely; the point here is the generation plumbing.
    monkeypatch.setattr(
        cover_letter, "retrieve_cv_bullets", lambda _job, top_k=5: ["Built payment integrations"]
    )
    agent = make_agent(TestModel())  # no API key, no network
    letter = asyncio.run(draft_cover_letter(job, agent=agent))
    assert isinstance(letter, CoverLetterDraft)
    assert letter.subject and letter.body  # the structured fields are populated


def test_ungrounded_points_flags_invented_claims() -> None:
    """The live 2026-07-23 failure: matched_points listing CV entries that do not exist."""
    bullets = [
        "Python backend developer with FastAPI, PostgreSQL and Kafka",
        "Maintained a crypto derivatives exchange backend under high load",
    ]
    invented = ["Hands-on experience with AI video tools (Kling, Seedance)"]
    assert cover_letter.ungrounded_points(bullets, invented) == invented


def test_ungrounded_points_accepts_paraphrase_of_real_bullets() -> None:
    bullets = ["Python backend developer with FastAPI, PostgreSQL and Kafka"]
    genuine = ["Python backend developer, FastAPI and PostgreSQL"]
    assert cover_letter.ungrounded_points(bullets, genuine) == []
