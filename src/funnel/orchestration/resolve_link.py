"""Find the employer's own apply page for a posting whose link is a dead end (2026-08-05).

`matching/apply_route.py` decides that a link leads nowhere the human can apply — a site that
answers 403 from where he lives, an apply button behind a paywall. This is the other half: the
attempt to find a link that *does* work, by doing automatically what the human does by hand —
search for the company plus the title, and apply on the employer's own site.

**Two halves, and the split is the whole design.** A model with web search *proposes* a URL; a
plain HTTP fetch *confirms* it. A proposed URL is a claim, not a fact — the model can invent a
plausible careers URL as readily as it can find a real one, and the failure is silent: a wrong
link in the admin costs the human a click and their trust in the column, forever. So a URL is
only written to the database once the page has been fetched and found to name the posting. Same
rule, and for the same reason, as `adapters/ats.board_confirms`: a slug that merely resolves is
not evidence of anything.

**This is the only LLM call outside `drafting/` and `replies/` that is not part of the agent
graph.** It lives here because `orchestration/` is already inside the invariant-4 boundary
(`tests/test_invariants.py`) — no boundary moves for this. It is deliberately NOT in
`matching/`, which is and stays deterministic.

**It runs on a handful of rows, on demand.** Only postings that would otherwise hold a shortlist
slot are worth a search: measured 2026-08-05, that was 9 rows of a 25-slot shortlist, not the
131 dead ends in the table. At $10/1000 searches that is cents per run — but `funnel
resolve-links` stays a separate command rather than a `run-funnel` stage, because the timer runs
three times a day and nothing that spends money joins it without the human saying so.

A miss is recorded (`Job.apply_resolved_at` with no `apply_url`) so the same row is never
searched twice. That is what keeps this bounded as the table grows, and it is the same shape as
the `!miss:` rows in `adapters/ats.py`. **A search that fails is not a miss** — see `LinkResult`:
recording one would retire a real posting on a transient provider error, and did.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Protocol

import httpx
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.capabilities import WebSearch

from funnel.adapters.util import USER_AGENT, strip_html
from funnel.config import bridge_llm_api_key, get_settings
from funnel.matching.apply_route import is_blocked

if TYPE_CHECKING:
    from pydantic_ai.models import Model

#: How much of a fetched page to read when confirming. The title appears in the first screenful
#: of any real posting; reading megabytes to look for it only invites a memory surprise.
_MAX_PAGE_CHARS = 200_000
_TIMEOUT = httpx.Timeout(20.0)


class ResolvableJob(Protocol):
    """The posting fields the resolver reads — satisfied by `Job` and by a stub."""

    @property
    def title(self) -> str: ...

    @property
    def company(self) -> str: ...

    @property
    def location(self) -> str | None: ...


class ApplyLink(BaseModel):
    """A proposed direct link to a posting. `url` is None when the search found nothing."""

    url: str | None = Field(
        default=None,
        description="The employer's own posting/apply page, or null if none was found.",
    )
    reasoning: str = Field(description="One line, for the human: what was found, or why nothing.")


RESOLVE_INSTRUCTIONS = (
    "You find the direct application page for one job posting. The seeker found this posting on "
    "an aggregator whose link does not work for them, so they need the page on the EMPLOYER'S "
    "own site or on the applicant tracking system the employer uses (Greenhouse, Lever, Ashby, "
    "Recruitee, SmartRecruiters, Workable, Workday, Teamtailor, and the like).\n"
    "Search the web for the company name together with the job title. Return the URL of the "
    "page for THIS SPECIFIC ROLE at THIS SPECIFIC COMPANY.\n"
    "- Never return a link on the aggregator the seeker already has, nor on any other job "
    "aggregator or search site (adzuna, remoteok, indeed, glassdoor, linkedin, ziprecruiter, "
    "talent.com, jooble, google jobs). Those are what they are trying to get away from.\n"
    "- Never return a link to a company's generic careers index, a job-search results page, or "
    "the company home page. It must be the page for this one role.\n"
    "- Do not guess or construct a URL from a pattern. Return only a URL that appeared in your "
    "search results. If you did not find the specific posting, return url=null — that is a "
    "correct and useful answer, and inventing a plausible link is much worse than finding none.\n"
    "A posting may simply be gone, or may only ever have existed on the aggregator (staffing "
    "agencies often post nowhere else). Returning null for those is expected."
)


def resolve_prompt(job: ResolvableJob) -> str:
    return (
        f"Company: {job.company}\nJob title: {job.title}\n"
        f"Location: {job.location or 'not stated'}\n\n"
        "Find the direct application page for this role."
    )


def make_resolver_agent(model: Model | str) -> Agent[None, ApplyLink]:
    """Build the resolver over any model (a TestModel in tests, the real one in prod)."""
    return Agent(
        model,
        output_type=ApplyLink,
        instructions=RESOLVE_INSTRUCTIONS,
        capabilities=[WebSearch()],
    )


@lru_cache
def _production_agent() -> Agent[None, ApplyLink]:
    """The configured resolver. `resolve_model` exists because not every model can search.

    This is the only call in the funnel that depends on the provider's server-side web-search
    loop, and carrying that loop is a real capability difference rather than a quality one:
    `claude-haiku-4-5` fails every attempt with a 400 ("web_search tool use ... without a
    corresponding web_search_tool_result block") while `claude-sonnet-5` completes it — both
    measured on the first live run, 2026-08-05. So a cheap model chosen for drafting must not
    silently decide whether links can be resolved at all.
    """
    bridge_llm_api_key()
    settings = get_settings()
    return make_resolver_agent(settings.resolve_model or settings.llm_model)


def _fold(text: str) -> str:
    """Reduce text to lowercase ASCII letters and digits, for substring comparison.

    Folding both sides is what makes the title test survive the ways a page restates a title:
    "Senior AWS Developer" against "Senior&nbsp;AWS Developer</h1>", a non-breaking hyphen, or
    an em dash. It also makes the test strict in the direction that matters — the words must
    still appear in the same order.
    """
    stripped = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", stripped.lower())


def page_confirms(page: str, title: str) -> bool:
    """True when a fetched page really carries this posting. Pure; no network.

    The whole folded title must appear in the folded page. Deliberately all-or-nothing rather
    than a token-overlap score: a loose match confirmed "(Senior) DevOps Engineer (f/m/d)"
    against "Account Manager (f/m/d)" when `adapters/ats.board_confirms` tried it, because both
    end in the same gender marker. A false negative costs one unresolved row; a false positive
    puts a wrong link in front of the human.
    """
    folded_title = _fold(title)
    return bool(folded_title) and folded_title in _fold(page)


async def verify_apply_url(url: str, title: str) -> bool:
    """Fetch a proposed URL and check it names this posting. The half the model cannot do.

    Rejects a link that is itself a dead end — the model can propose another Adzuna page as
    readily as anything else, and resolving one blocked link to another is worse than useless
    because it looks resolved.
    """
    if is_blocked(url):
        return False
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=_TIMEOUT, follow_redirects=True
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            body = response.text[:_MAX_PAGE_CHARS]
    except Exception:
        # A 404, a timeout, a TLS failure, a hallucinated host that does not resolve — every
        # one of them means the same thing here: this is not a link we can hand the human.
        return False
    # Rechecked after redirects: a company careers page that 302s onto an aggregator is exactly
    # the case the pre-fetch check cannot see.
    if is_blocked(str(response.url)):
        return False
    return page_confirms(strip_html(body), title)


@dataclass(frozen=True)
class LinkResult:
    """What one resolution attempt produced.

    `searched` is the field that earns this class its existence. "We searched and there is no
    employer page" and "we could not search" look identical to a caller reading only `url`, and
    conflating them is expensive in exactly one direction: the caller records a miss
    permanently, so a posting whose search merely *errored* is written off forever. That is not
    hypothetical — the first live run retired a real posting on a 400 from the provider
    (2026-08-05), which is why the distinction lives in the type and not in a comment.
    """

    url: str | None
    reasoning: str
    #: False only when the attempt never reached a verdict: the model call itself failed.
    searched: bool = True


async def resolve_apply_url(
    job: ResolvableJob, *, agent: Agent[None, ApplyLink] | None = None
) -> LinkResult:
    """Search for one posting's direct apply page and verify what comes back.

    Never raises: `resolve-links` walks a batch, and one dead search must not take the run down
    with it. A failure comes back as `searched=False` instead, which is the caller's signal to
    leave the posting for next time rather than record a miss against it.
    """
    active = agent or _production_agent()
    try:
        proposed = (await active.run(resolve_prompt(job))).output
    except Exception as error:
        return LinkResult(None, f"search failed: {type(error).__name__}", searched=False)
    if not proposed.url:
        return LinkResult(None, proposed.reasoning or "no direct posting found")
    if not await verify_apply_url(proposed.url, job.title):
        return LinkResult(None, f"unverified ({proposed.url}): {proposed.reasoning}")
    return LinkResult(proposed.url, proposed.reasoning)
