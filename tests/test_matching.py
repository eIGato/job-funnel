"""Hard filters and cosine similarity: the deterministic, free half of the funnel.

These avoid loading the embedding model, so they stay fast and offline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pytest

from funnel.matching.embed import cosine_similarity, from_bytes, to_bytes
from funnel.matching.filters import passes_hard_filters

if TYPE_CHECKING:
    from funnel.schemas import NormalizedJob


def test_clean_job_passes(job: NormalizedJob) -> None:
    assert passes_hard_filters(job) is True


def test_stop_phrase_in_description_rejects(job: NormalizedJob) -> None:
    blocked = job.model_copy(update={"description": "Must hold an active security clearance."})
    assert passes_hard_filters(blocked) is False


def test_stop_phrase_is_case_insensitive(job: NormalizedJob) -> None:
    blocked = job.model_copy(update={"description": "US CITIZENS ONLY"})
    assert passes_hard_filters(blocked) is False


def test_ru_by_location_is_a_hard_stop(job: NormalizedJob) -> None:
    for loc in ("Moscow", "Россия", "Minsk", "Санкт-Петербург"):
        blocked = job.model_copy(update={"location": loc})
        assert passes_hard_filters(blocked) is False, loc


def test_russia_in_description_only_is_not_a_stop(job: NormalizedJob) -> None:
    # The RU/BY stop keys on the work location, not a passing mention in the body.
    fine = job.model_copy(update={"description": "We integrate with a Russia-based payment API."})
    assert passes_hard_filters(fine) is True


def test_remote_geo_locked_posting_is_rejected(job: NormalizedJob) -> None:
    blocked = job.model_copy(
        update={"is_remote": True, "description": "Remote — must be authorized to work in the US."}
    )
    assert passes_hard_filters(blocked) is False


def test_contractor_welcome_overrides_the_geo_lock(job: NormalizedJob) -> None:
    ok = job.model_copy(
        update={"is_remote": True, "description": "US only, but open to B2B contractors worldwide."}
    )
    assert passes_hard_filters(ok) is True


def test_geo_lock_that_admits_europe_is_not_rejected(job: NormalizedJob) -> None:
    # "must be located in the Americas, Europe, or Israel" still admits the human (Montenegro).
    ok = job.model_copy(
        update={
            "is_remote": True,
            "description": "You must be located in the Americas, Europe, or Israel to apply.",
        }
    )
    assert passes_hard_filters(ok) is True


def test_worldwide_remote_geo_phrase_is_not_rejected(job: NormalizedJob) -> None:
    ok = job.model_copy(
        update={
            "is_remote": True,
            "location": "Anywhere in the World",
            "description": "Remote, must be authorized to work — open worldwide.",
        }
    )
    assert passes_hard_filters(ok) is True


def test_onsite_geo_requirement_is_kept_not_rejected(job: NormalizedJob) -> None:
    # On-site postings state an authorization requirement as a matter of course; keep them
    # (they rank below remote via the sort, not via this predicate).
    kept = job.model_copy(
        update={
            "is_remote": False,
            "location": "Berlin",
            "description": "On-site. Must be authorized to work in the EU.",
        }
    )
    assert passes_hard_filters(kept) is True


def test_cosine_of_identical_vectors_is_one() -> None:
    vector = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    scores = cosine_similarity(vector.reshape(1, -1), vector)
    assert scores.shape == (1,)
    # float32 lands a hair under 1.0, so compare with a tolerance.
    assert float(scores[0]) == pytest.approx(1.0, abs=1e-6)


def test_cosine_of_orthogonal_vectors_is_zero() -> None:
    matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    scores = cosine_similarity(matrix, np.array([0.0, 1.0], dtype=np.float32))
    assert abs(float(scores[0])) < 1e-6


def test_cosine_ranks_rows() -> None:
    matrix = np.array([[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]], dtype=np.float32)
    scores = cosine_similarity(matrix, np.array([1.0, 0.0], dtype=np.float32))
    assert scores[0] > scores[1] > scores[2]


def test_zero_vector_scores_zero_instead_of_nan() -> None:
    """A missing embedding must not poison the ranking with NaN."""
    matrix = np.array([[0.0, 0.0]], dtype=np.float32)
    scores = cosine_similarity(matrix, np.array([1.0, 0.0], dtype=np.float32))
    assert not np.isnan(scores).any()
    assert scores[0] == np.float32(0.0)


def test_empty_matrix_is_handled() -> None:
    scores = cosine_similarity(
        np.empty((0, 0), dtype=np.float32), np.array([1.0], dtype=np.float32)
    )
    assert scores.shape == (0,)


def test_embedding_bytes_roundtrip() -> None:
    vector = np.array([0.1, -0.2, 0.3], dtype=np.float32)
    assert np.array_equal(from_bytes(to_bytes(vector)), vector)


def test_junior_title_is_below_the_middle_floor(job: NormalizedJob) -> None:
    for title in (
        "Junior Python Developer",
        "Backend Intern",
        "Trainee Engineer",
        "Младший разработчик",
    ):
        blocked = job.model_copy(update={"title": title})
        assert passes_hard_filters(blocked) is False, title


def test_a_junior_middle_range_is_kept(job: NormalizedJob) -> None:
    # The floor is Middle; a range that reaches it stays.
    ok = job.model_copy(update={"title": "Junior/Middle Python Developer"})
    assert passes_hard_filters(ok) is True


def test_unspecified_and_senior_levels_are_kept(job: NormalizedJob) -> None:
    for title in ("Backend Developer", "Senior Python Engineer", "Lead Backend Engineer"):
        ok = job.model_copy(update={"title": title})
        assert passes_hard_filters(ok) is True, title


def test_mentoring_juniors_in_the_body_does_not_trip_the_floor(job: NormalizedJob) -> None:
    ok = job.model_copy(
        update={"title": "Backend Engineer", "description": "You will mentor junior developers."}
    )
    assert passes_hard_filters(ok) is True


def test_ml_training_role_is_stopped(job: NormalizedJob) -> None:
    for title in ("Machine Learning Engineer", "ML Engineer", "Deep Learning Scientist"):
        blocked = job.model_copy(update={"title": title})
        assert passes_hard_filters(blocked) is False, title


def test_working_with_ai_is_not_stopped(job: NormalizedJob) -> None:
    # Using AI/LLMs is wanted; only training models as the primary role is stopped.
    for title in ("AI Engineer", "Backend Engineer (LLM apps)", "Python Developer, AI tooling"):
        ok = job.model_copy(update={"title": title})
        assert passes_hard_filters(ok) is True, title


def test_ml_platform_engineering_is_kept(job: NormalizedJob) -> None:
    # Engineering around ML is ordinary backend work, not model training.
    ok = job.model_copy(update={"title": "Machine Learning Platform Engineer"})
    assert passes_hard_filters(ok) is True


def test_junk_titles_are_dropped(job: NormalizedJob) -> None:
    """Scraped page furniture reaches us looking like a posting and embeds at 0.84+."""
    for title in (
        "Job Details",
        "Couldn't pick up that page",
        "Jop posting title",
        "This is a test job",
        "  APPLY NOW  ",
        "Untitled",
    ):
        junk = job.model_copy(update={"title": title})
        assert passes_hard_filters(junk) is False, title


def test_junk_match_is_whole_title_not_substring(job: NormalizedJob) -> None:
    """A real role that happens to contain a junk word must survive."""
    for title in ("Senior Engineer - Details", "Backend Developer, Apply Now", "Hiring Manager"):
        real = job.model_copy(update={"title": title})
        assert passes_hard_filters(real) is True, title


def test_html_entities_in_a_junk_title_are_decoded(job: NormalizedJob) -> None:
    junk = job.model_copy(update={"title": "Job&nbsp;Details"})
    assert passes_hard_filters(junk) is False


def test_a_terse_posting_is_kept(job: NormalizedJob) -> None:
    """No minimum-description rule: Telegram postings are short and real (see filters.py)."""
    terse = job.model_copy(update={"description": "Python backend. Remote. Write to @hr."})
    assert passes_hard_filters(terse) is True
