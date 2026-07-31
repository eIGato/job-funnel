"""The soft stop-stack screen: is this role worth spending a real application on?

One model call per posting, run *before* a letter is drafted. It is the cheap half of what the
Phase 7 agent layer does — the `decide-worth-it` judgment on its own, without the research,
drafting and critic calls around it.

Why it lives here rather than in `orchestration/`: the judgment is needed on the ordinary
`draft` path that the systemd timer runs, not only in the optional agent layer. Before this,
the stop-stack existed *only* inside the agent graph, and `run-funnel` never called it — so
Node/PHP/fullstack postings were ranked, drafted and put in front of the human with a finished
cover letter, and the human had to mark each one DECLINED by hand. `orchestration/agent.py`
imports these definitions rather than keeping a second copy, exactly as it reuses
`generate_letter`: one prompt, one verdict shape, two callers.

What it deliberately does NOT judge is geography, seniority and the hard stop-stack — those are
deterministic code in `matching/filters.py` (invariant: no LLM in matching), and re-deciding
them here would let a model overrule a filter the human already settled.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from funnel.config import bridge_llm_api_key, get_settings
from funnel.drafting.prompting import UNTRUSTED_INPUT_RULE, posting_block

if TYPE_CHECKING:
    from pydantic_ai.models import Model


class ScreenableJob(Protocol):
    """The posting fields the screen reads — satisfied by `Job`, `JobBrief` and a stub.

    Declared read-only (properties, not bare attributes) so a frozen dataclass like `JobBrief`
    satisfies it: a bare attribute in a Protocol demands a *settable* one.
    """

    @property
    def title(self) -> str: ...

    @property
    def company(self) -> str: ...

    @property
    def location(self) -> str | None: ...

    @property
    def description(self) -> str: ...


class WorthItVerdict(BaseModel):
    """Whether this role is worth spending a real application on."""

    worth_it: bool = Field(
        description="True unless the role's emphasis is clearly one the seeker does not want."
    )
    reasoning: str = Field(description="One line, for the human: what tipped the decision.")


SCREEN_INSTRUCTIONS = (
    "You screen a job posting for a backend / data-engineering / AI-orchestration specialist "
    "before a cover letter is written. The posting already ranked highly against the seeker's "
    "profile, so DEFAULT TO worth_it=True. Judge ONLY the ROLE'S CONTENT AND EMPHASIS — the "
    "technical shape of the work. Only return False when the role's primary emphasis is clearly "
    "something the seeker does not want. Concretely:\n"
    "- Training or researching ML models as the core job (title/'responsibilities' centre on "
    "building and training neural networks, e.g. a Deep Learning / ML Research role) -> False. "
    "But WORKING WITH AI/LLMs — orchestration, RAG, agents, wiring models into a backend — is "
    "wanted: keep those.\n"
    "- A role whose PRIMARY focus is PHP, Node/JavaScript, or general fullstack/frontend work. "
    "If backend (Python) is the main thing and those are secondary, or the posting offers extra "
    "pay for them, keep it (worth_it=True).\n"
    "- An obvious content mismatch that slipped through the ranking (pure frontend, pure "
    "DevOps/SRE with no backend, sales/management, or a role from another field entirely) "
    "-> False.\n"
    "DO NOT consider geography, location, timezone, relocation, on-site vs remote, or work "
    "authorization — those are decided upstream by deterministic filters (PLAN.md section 7) and "
    "must NEVER be a reason here, nor appear in your reasoning. The seeker works remotely, "
    "contracts B2B, and adapts to any timezone; assume that is handled. "
    "When unsure, keep it. Give one short line of reasoning about the role's content either "
    "way.\n\n"
    f"{UNTRUSTED_INPUT_RULE}"
)


def screen_prompt(job: ScreenableJob) -> str:
    return (
        f"POSTING\nTitle: {job.title}\nCompany: {job.company}\n"
        f"Location: {job.location or 'n/a'}\n"
        f"Description:\n{posting_block(job.description)}"
    )


def make_screen_agent(model: Model | str) -> Agent[None, WorthItVerdict]:
    """Build the screening agent over any model (a TestModel in tests, the real one in prod)."""
    return Agent(model, output_type=WorthItVerdict, instructions=SCREEN_INSTRUCTIONS)


@lru_cache
def _production_agent() -> Agent[None, WorthItVerdict]:
    bridge_llm_api_key()
    settings = get_settings()
    return make_screen_agent(settings.screen_model or settings.llm_model)


async def screen_job(
    job: ScreenableJob, *, agent: Agent[None, WorthItVerdict] | None = None
) -> WorthItVerdict:
    """One model call deciding whether to draft for this posting at all."""
    active = agent or _production_agent()
    return (await active.run(screen_prompt(job))).output
