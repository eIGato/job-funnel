"""Hard filters (Phase 4a). Deterministic code, no LLM, no tokens.

Cheaply discards obvious misses *before* embedding, so only survivors get embedded.

Answered geography rules (PLAN.md section 7):
  - RU / BY work locations: a hard stop.
  - A *remote* posting locked to a geography we cannot satisfy ("US only", "must be authorized
    to work in the UK"): reject — unless it explicitly welcomes a contractor / B2B arrangement,
    which the human can serve through a Georgian entity.
  - On-site / hybrid without explicit sponsorship: KEPT (companies sponsor on request without
    saying so); it merely ranks below remote, and ranking is a sort, not this predicate.
  - Timezone: not filtered at all.
  - Montenegro on-site: deliberately NOT filtered (decided 2026-07-24). The local IT market is
    a fraction of a percent of the input stream, and the human would take a cheap local gig, so
    a hard filter there would cost more in false positives than it could ever save.

Answered seniority / stop-stack (PLAN.md section 7, decided 2026-07-24):
  - Seniority floor is Middle: a posting whose *title* names a level below Middle
    (junior / intern / trainee / entry-level) and no Middle-or-above level is dropped.
  - The only hard stop-stack item is training neural networks *as the primary role*, keyed on
    the title (the title is the "is this the priority?" signal). Working *with* AI/LLMs is
    wanted, not stopped — this targets model-training roles, not AI-orchestration ones. The
    softer preferences (PHP / Node / fullstack as a secondary focus, extra pay) are a judgment
    about a role's *emphasis*, which pure code cannot make well; they are deferred to the
    screening step (`drafting/screen.py`), which every drafting path runs, not forced into a
    regex here.

Junk postings:
  - A row whose title is scraped page furniture ("Job Details", "Couldn't pick up that page") or
    a placeholder ("This is a test job") is not a posting and is dropped. This is only the "not
    a job posting" judgment; deciding that a real posting is the wrong *kind* of job belongs to
    the screening step.
"""

from __future__ import annotations

import re
from html import unescape
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

#: The posting names a citizenship/residency *preference* and then says it accepts everyone:
#: "U.S. Citizens and Green Card Holders highly preferred, all valid work authorizations may
#: apply". `_GEO_LOCKED` sees only the first half and reads a hard lock; this is the second half
#: saying there is none. Kept tight — "all/any" must sit next to the authorization phrase, not
#: merely somewhere in the posting, because "AWS preferred" elsewhere in a body is not consent.
_AUTHORIZATION_OPEN = re.compile(r"\b(?:all|any)\b[\w\s,]{0,30}?work authoriz", re.IGNORECASE)

#: Unconditional stops: clearances a RU citizen cannot obtain.
STOP_PHRASES: frozenset[str] = frozenset({"security clearance"})

#: A level below the Middle floor, read from the TITLE only — level is a title thing, and this
#: keeps "we mentor junior engineers" in a senior role's body from tripping the filter.
_JUNIOR_TITLE = re.compile(
    r"\b(?:junior|jr\.?|intern(?:ship)?|trainee|entry[-\s]?level|new[-\s]?grad(?:uate)?)\b"
    r"|стаж[её]р|младш|джуниор|начинающ",  # noqa: RUF001 (Cyrillic is the point)
    re.IGNORECASE,
)
#: Middle-or-above named in the title. A junior-tagged title that ALSO admits one of these
#: ("Junior/Middle", "Middle/Senior") is a range that reaches the floor, so it is kept.
_MIDDLE_PLUS_TITLE = re.compile(
    r"\b(?:middle|mid[-\s]?level|mid[-\s]?senior|senior|sr\.?|lead|staff|principal|architect)\b"
    r"|миддл|мидл|сеньор|ведущ|старш",
    re.IGNORECASE,
)

#: Training neural networks as the primary role, keyed on the title. Working *with* AI is wanted
#: (the funnel widens toward AI orchestration), so "AI Engineer" is deliberately absent — this
#: targets model-building/training titles, not LLM-application ones.
_ML_TRAINING_TITLE = re.compile(
    r"\b(?:machine[-\s]learning engineer|ml engineer|ml scientist|"
    r"machine[-\s]learning scientist|deep[-\s]learning (?:engineer|scientist))\b",
    re.IGNORECASE,
)
#: ...unless the title marks it as the engineering *around* ML rather than training models. An
#: ML platform / infra / backend role is ordinary backend work and stays.
_ML_ADJACENT_TITLE = re.compile(
    r"platform|infrastructure|\binfra\b|backend|back[-\s]end|devops|mlops|data engineer",
    re.IGNORECASE,
)


#: Titles that are not a role at all: scraped page furniture and placeholder postings. RemoteOK
#: hands these out with an ordinary company name and a plausible teaser body, so nothing
#: downstream can tell them apart — "Job Details" and "Couldn't pick up that page" both embedded
#: at 0.84+ and reached the top of the shortlist, where each cost a drafted letter.
#:
#: Matched against the WHOLE normalized title, never as a substring: a real posting must never
#: be caught here, and "Details" inside "Senior Engineer - Details" is not junk. This list is
#: only for "this is not a job posting"; judging a real posting to be the wrong *kind* of job is
#: the screening step's business (`drafting/screen.py`), not a regex's. Extend as new artifacts
#: show up.
_JUNK_TITLES: frozenset[str] = frozenset(
    {
        "apply now",
        "couldn't pick up that page",
        "create your own role",
        "details",
        "expression of interest",
        "hiring",
        "job",
        "job details",
        "job posting title",
        "job title",
        "join our team",
        "jop posting title",
        "no title",
        "page not found",
        "test job",
        "this is a test job",
        "untitled",
        "vacancy",
        "we are hiring",
    }
)

#: Deliberately NOT paired with a minimum-description rule. The obvious companion filter — drop
#: a posting whose body is too short to use — was measured against the real table and would have
#: caught none of the junk above (those bodies run 371-1503 characters, against a 5th percentile
#: of ~430 for postings that pass). It would only have penalized the terse Telegram postings,
#: which are short *and* real. A filter that cannot be shown to catch the thing it is aimed at
#: does not earn its false positives.


def _normalized_title(title: str) -> str:
    """Fold a title for comparison: entities decoded, whitespace collapsed, case dropped."""
    return " ".join(unescape(title).split()).strip(" -–—:|").casefold()  # noqa: RUF001 (dashes)


def _is_junk(job: _Filterable) -> bool:
    """True when the row is not a real posting at all, only scraped page furniture."""
    return _normalized_title(job.title) in _JUNK_TITLES


def _below_middle(title: str) -> bool:
    return bool(_JUNIOR_TITLE.search(title)) and not _MIDDLE_PLUS_TITLE.search(title)


def _ml_training_primary(title: str) -> bool:
    return bool(_ML_TRAINING_TITLE.search(title)) and not _ML_ADJACENT_TITLE.search(title)


def passes_hard_filters(job: _Filterable) -> bool:
    """True when the posting is worth embedding. Pure: no I/O, no model calls."""
    if _is_junk(job):
        return False
    if _RU_BY_LOCATION.search(job.location or ""):
        return False
    haystack = f"{job.title}\n{job.description}\n{job.location or ''}"
    folded = haystack.casefold()
    if any(phrase in folded for phrase in STOP_PHRASES):
        return False
    # Seniority and the one hard stop-stack item read the title only (its priority signal).
    if _below_middle(job.title) or _ml_training_primary(job.title):
        return False
    # A remote posting locked to a geography we cannot satisfy, with no contractor door and no
    # Europe/worldwide admission, is out.
    return not (
        job.is_remote
        and bool(_GEO_LOCKED.search(haystack))
        and not _CONTRACTOR_OK.search(haystack)
        and not _REGION_OK.search(haystack)
        and not _AUTHORIZATION_OPEN.search(haystack)
    )
