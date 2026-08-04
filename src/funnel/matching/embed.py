"""Embeddings and cosine similarity (Phase 4b).

fastembed over ONNX, locally, without torch (invariant 3). Weights download once; after
that there is no network and no token spend. Vectors live in Job.embedding as raw float32.
"""

from __future__ import annotations

import warnings
from functools import lru_cache
from typing import TYPE_CHECKING

import numpy as np
from fastembed import TextEmbedding

from funnel.config import get_settings

if TYPE_CHECKING:
    from numpy.typing import NDArray

#: e5 models require query:/passage: prefixes; bge models do not. See CLAUDE.md conventions.
_E5_MARKER = "e5"


@lru_cache
def get_model() -> TextEmbedding:
    """Loaded once per process.

    fastembed >=0.6 switched the e5 family from CLS to MEAN pooling, and warns about it on load.
    Mean pooling is the pooling e5 was *trained* with (the model card averages the last hidden
    state under the attention mask); the old CLS default was incorrect for e5, so this is a fix,
    not a regression — human-accepted 2026-07-24. We take the new behaviour and re-embedded the
    stored vectors for consistency, so the once-per-process warning is expected noise; silence it.
    """
    settings = get_settings()
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*mean pooling.*", category=UserWarning)
        return TextEmbedding(
            model_name=settings.embedding_model,
            cache_dir=str(settings.embedding_cache_dir),
        )


def _needs_e5_prefix() -> bool:
    return _E5_MARKER in get_settings().embedding_model.casefold()


def embed_texts(texts: list[str], *, is_query: bool = False) -> NDArray[np.float32]:
    """Embed texts into an (n, dim) float32 matrix.

    is_query distinguishes the two sides only for e5 models; it is a no-op for bge.

    The batch size is ours, not fastembed's default 256: peak memory is one forward pass,
    and at 256 e5-large's activations are large enough to get the process OOM-killed. See
    `embedding_batch_size` in config.
    """
    if not texts:
        return np.empty((0, 0), dtype=np.float32)
    if _needs_e5_prefix():
        prefix = "query: " if is_query else "passage: "
        texts = [prefix + t for t in texts]
    vectors = get_model().embed(texts, batch_size=get_settings().embedding_batch_size)
    return np.asarray(list(vectors), dtype=np.float32)


def cosine_similarity(
    matrix: NDArray[np.float32], vector: NDArray[np.float32]
) -> NDArray[np.float32]:
    """Cosine similarity of every row in matrix against vector, shape (n,).

    A single matrix pass. At hundreds or thousands of postings this is instant, which is
    why pgvector is unnecessary here (PLAN.md section 6).
    """
    if matrix.size == 0:
        return np.empty((0,), dtype=np.float32)
    matrix_norm = np.linalg.norm(matrix, axis=1)
    vector_norm = np.linalg.norm(vector)
    denominator = matrix_norm * vector_norm
    # A zero vector scores 0 rather than dividing by zero into NaN. `out` is float32, so
    # the result already carries that dtype.
    scores: NDArray[np.float32] = np.divide(
        matrix @ vector,
        denominator,
        out=np.zeros(matrix.shape[0], dtype=np.float32),
        where=denominator > 0,
    )
    return scores


def centered_similarity(
    matrix: NDArray[np.float32], vector: NDArray[np.float32]
) -> NDArray[np.float32]:
    """Cosine similarity against `vector`, with the corpus mean removed from both sides.

    Raw e5 cosine between the profile and *any* job posting lives in a narrow high band: both
    texts are professional-register hiring prose, and that shared component dominates the dot
    product. Measured over the real table (2072 rows, 2026-07-31): min 0.717, median 0.795,
    max 0.856, sd 0.023. A genuine backend posting and a scraped cookie banner sat 0.00001
    apart, both at the 98th percentile — enough to rank with, not enough to separate.

    Subtracting the mean posting vector from both sides removes what every posting shares and
    leaves what distinguishes them. On the same table sd goes 0.023 -> 0.093 and the ordering
    becomes meaningful: "Senior Backend Python Developer" moves from rank 31 to 9 while
    "Mid/Senior AI Cinematic Video Editor" drops from 28 to 167.

    The centre is a property of the whole corpus, so scores are comparable only within one
    call. `match` therefore rescores every stored vector on every run rather than only the new
    ones — cheap, because the vectors are already in the database.
    """
    if matrix.size == 0:
        return np.empty((0,), dtype=np.float32)
    center = matrix.mean(axis=0)
    return cosine_similarity(matrix - center, vector - center)


def percentile_ranks(scores: NDArray[np.float32]) -> NDArray[np.float32]:
    """For each score, the percentage of the population scoring strictly below it, 0-100.

    Centered scores are spread but still abstract (a good match is ~0.23, not ~0.85), and no
    absolute value means anything on its own. The percentile is what the human actually reads
    in the admin: "94% of the shortlist scores below this one". Ties share a percentile. The
    best row lands at (n-1)/n rather than a clean 100 — with n rows nothing can beat all n of
    them, itself included — which at a few thousand postings rounds to 100.0% on display.
    """
    if scores.size == 0:
        return np.empty((0,), dtype=np.float32)
    ordered = np.sort(scores)
    below = np.searchsorted(ordered, scores, side="left")
    return (100.0 * below / scores.size).astype(np.float32)


def to_bytes(vector: NDArray[np.float32]) -> bytes:
    """Serialize a vector for Job.embedding."""
    return np.ascontiguousarray(vector, dtype=np.float32).tobytes()


def from_bytes(blob: bytes) -> NDArray[np.float32]:
    """Deserialize a vector from Job.embedding."""
    return np.frombuffer(blob, dtype=np.float32)
