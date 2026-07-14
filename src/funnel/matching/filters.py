"""Hard filters (Phase 4a). Deterministic code, no LLM, no tokens.

Cheaply discards obvious misses *before* embedding, so only survivors get embedded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from funnel.schemas import NormalizedJob

#: OPEN QUESTION (PLAN.md section 7): the real criteria are timezone, seniority floor and
#: stop-stack. This is a placeholder seed, not an agreed list. Confirm with the human.
STOP_PHRASES: frozenset[str] = frozenset(
    {
        "security clearance",
        "us citizens only",
        "onsite only",
    }
)


def passes_hard_filters(job: NormalizedJob) -> bool:
    """True when the posting is worth embedding.

    Keep this predicate pure and testable: no I/O, no model calls.

    TODO Phase 4a, once the criteria are agreed: remote and timezone window, seniority
    floor derived from the title, required stack.
    """
    haystack = f"{job.title} {job.description} {job.location or ''}".casefold()
    return all(phrase not in haystack for phrase in STOP_PHRASES)
