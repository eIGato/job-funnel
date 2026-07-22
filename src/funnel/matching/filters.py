"""Hard filters (Phase 4a). Deterministic code, no LLM, no tokens.

Cheaply discards obvious misses *before* embedding, so only survivors get embedded. The rules
here are the ones PLAN.md section 7 records as *answered*; the still-open criteria (a seniority
floor, a stop-stack) are not invented here.

Answered geography rules (PLAN.md section 7):
  - RU / BY work locations: a hard stop.
  - A *remote* posting locked to a geography we cannot satisfy ("US only", "must be authorized
    to work in the UK"): reject — unless it explicitly welcomes a contractor / B2B arrangement,
    which the human can serve through a Georgian entity.
  - On-site / hybrid without explicit sponsorship: KEPT (companies sponsor on request without
    saying so); it merely ranks below remote, and ranking is a sort, not this predicate.
  - Timezone: not filtered at all.
"""

from __future__ import annotations

import re
from typing import Protocol


class _Filterable(Protocol):
    """The posting fields the filters read — satisfied by both NormalizedJob and the Job ORM."""

    title: str
    description: str
    location: str | None
    is_remote: bool


#: RU/BY work locations are a hard stop. Keyed on the *location* only: a remote foreign job that
#: merely names Russia in its description is the main stream, not a reject.
_RU_BY_LOCATION = re.compile(
    r"\b(?:russia|russian federation|moscow|saint[- ]petersburg|st\.? petersburg|belarus|minsk)\b"
    r"|росси|москв|санкт-петербург|петербург|беларус|минск|\bрф\b",  # noqa: RUF001 (Cyrillic is the point)
    re.IGNORECASE,
)

#: An explicit geographic lock we cannot satisfy. Applied to a *remote* posting only — on-site
#: postings normally state an authorization requirement and are kept regardless (sponsor on
#: request). Heuristic seed; extend as real alerts show new phrasings.
_GEO_LOCKED = re.compile(
    r"authorized to work in the|must be authorized to work|"
    r"must be (?:based|located|residing) in|must reside in|"
    r"(?:us|usa|u\.s\.|uk|eu|eea)[-\s]?only\b|only\s+(?:us|usa|uk|eu)\b|"
    r"u\.?s\.? citizens?|citizens? only|citizens? or permanent residents|green card",
    re.IGNORECASE,
)

#: A contractor/B2B welcome overrides the geo lock (the human has a Georgian entity for exactly
#: this — B2B contracts are the natural shape, net ~= gross).
_CONTRACTOR_OK = re.compile(
    r"\b(?:contractor|b2b|c2c|corp[-\s]to[-\s]corp|independent contractor|"
    r"self[-\s]employed|freelanc)",
    re.IGNORECASE,
)

#: A geo lock that still admits Europe (where the human lives — Montenegro/CET) or is globally
#: open is not a reject: it names a region the human *can* satisfy. Without this, a permissive
#: multi-region net like "must be located in the Americas, Europe, or Israel" is a false positive.
_REGION_OK = re.compile(r"europe|\beu\b|\beea\b|\bemea\b|anywhere|world-?wide", re.IGNORECASE)

#: Unconditional stops: clearances a RU citizen cannot obtain. OPEN (PLAN.md section 7): the
#: seniority floor and stop-stack are still to be agreed — this stays a small, honest seed.
STOP_PHRASES: frozenset[str] = frozenset({"security clearance"})


def passes_hard_filters(job: _Filterable) -> bool:
    """True when the posting is worth embedding. Pure: no I/O, no model calls."""
    if _RU_BY_LOCATION.search(job.location or ""):
        return False
    haystack = f"{job.title}\n{job.description}\n{job.location or ''}"
    folded = haystack.casefold()
    if any(phrase in folded for phrase in STOP_PHRASES):
        return False
    # A remote posting locked to a geography we cannot satisfy, with no contractor door and no
    # Europe/worldwide admission, is out.
    return not (
        job.is_remote
        and bool(_GEO_LOCKED.search(haystack))
        and not _CONTRACTOR_OK.search(haystack)
        and not _REGION_OK.search(haystack)
    )
