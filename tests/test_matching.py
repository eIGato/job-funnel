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
