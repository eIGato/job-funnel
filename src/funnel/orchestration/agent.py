"""Optional agent layer (Phase 7), over the top-N shortlist only.

The funnel has already squeezed thousands of postings down to dozens for free (deterministic
filters + local embeddings). This layer spends real tokens on a *handful* — the top of the
shortlist a human is about to act on — to do the two things a single generation cannot:

    decide-worth-it  ->  research-company  ->  draft  ->  critic
        (soft            (web search)          (RAG)       (second
         stop-stack)                                        LLM pass)

  - **decide-worth-it** is the soft stop-stack (PLAN.md section 7): reading the *whole* posting
    to judge whether PHP/Node/fullstack is the emphasis or a side note, and whether training
    models is the actual job — judgments the Phase 4a regex filters deliberately do not attempt.
    A "no" records `ApplicationStatus.DECLINED` and never drafts. It is **not** exclusive to this
    layer any more: the same call is `drafting.screen.screen_job`, which the plain `draft` path
    runs too, so a stop-stack posting is declined whether or not the agent layer is used.
  - **research-company** does a short web search so the opener can be specific to the company,
    not generic. It is a nicety: if it fails or finds nothing, the draft still happens.
  - **draft** reuses the exact Phase 5 machinery (`drafting.cover_letter.generate_letter`),
    so the grounding backstop and the anti-cliché rules are identical — it just gets the
    research and any critic feedback as extra context.
  - **critic** is a second LLM pass over the draft; it is the richer version of the
    deterministic grounding check, and can send the draft back once for a revision.

Built on **pydantic-graph** (decided 2026-07-24) — coherent with the pydantic/pydantic-ai
stack already in use. The four model calls all go through pydantic-ai (invariant 4); this
module lives under `orchestration/`, which the invariant test allows alongside `drafting/`
and `replies/`. The agents are injected via `AgentDeps` so tests drive the whole graph with a
`TestModel` and never touch the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.capabilities import WebSearch
from pydantic_graph import BaseNode, End, Graph, GraphBuilder, GraphRunContext, StepContext

from funnel.config import bridge_llm_api_key, get_settings
from funnel.drafting.cover_letter import (
    CoverLetterDraft,
    UngroundedDraftError,
    generate_letter,
    retrieve_cv_bullets,
)
from funnel.drafting.cover_letter import _detect_language as detect_language
from funnel.drafting.cover_letter import (
    make_agent as make_draft_agent,
)
from funnel.drafting.screen import (
    WorthItVerdict,
    make_screen_agent,
    screen_prompt,
)

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from funnel.models import Job


# --------------------------------------------------------------------------------------------
# Structured model outputs
# --------------------------------------------------------------------------------------------


class Critique(BaseModel):
    """critic: the verdict on a drafted letter."""

    approved: bool = Field(description="True if the letter is ready for the human to review.")
    issues: str = Field(
        default="",
        description="If not approved, the concrete problems to fix, one short line each.",
    )


# --------------------------------------------------------------------------------------------
# Graph input / state / deps / output
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class JobBrief:
    """A detached snapshot of a posting: what the graph needs, with no live DB session.

    Carries exactly the attributes `retrieve_cv_bullets` and the drafting prompt read, so a
    `JobBrief` can stand in for a `Job` there by duck typing. Built once, in code, including the
    language decision (invariant 4: detected here, never by the LLM).
    """

    title: str
    company: str
    location: str | None
    url: str
    description: str
    apply_channel: object  # ApplyChannel | None — kept loose so the drafting fallback applies
    language: str

    @classmethod
    def from_job(cls, job: Job) -> JobBrief:
        return cls(
            title=job.title,
            company=job.company,
            location=job.location,
            url=str(job.url),
            description=job.description or "",
            apply_channel=job.apply_channel,
            language=detect_language(job),
        )


@dataclass
class AgentState:
    """Everything the nodes accumulate across the run (research, bullets, revisions)."""

    bullets: list[str] | None = None
    research: str = ""
    reasoning: str = ""
    critiques: list[str] = field(default_factory=list)
    revisions: int = 0
    draft: CoverLetterDraft | None = None


@dataclass
class AgentDeps:
    """The four model calls, injected so tests can swap in a TestModel."""

    worth_it_agent: Agent[None, WorthItVerdict]
    research_agent: Agent[None, str]
    draft_agent: Agent[None, CoverLetterDraft]
    critic_agent: Agent[None, Critique]
    #: How many times the critic may bounce a draft back before the letter is kept as-is.
    max_revisions: int = 1
    #: Web-search research is a nicety; turning it off keeps the graph offline (and tokens down).
    do_research: bool = True


@dataclass
class AgentResult:
    """What the graph returns for one posting."""

    worth_it: bool
    reasoning: str
    draft: CoverLetterDraft | None = None
    research: str = ""
    critique: str = ""
    approved: bool = False
    revisions: int = 0
    #: True when the drafting grounding backstop refused (a fluent fabrication was suppressed).
    refused: bool = False


# --------------------------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------------------------

_RESEARCH_INSTRUCTIONS = (
    "You research a company so a job seeker's cover letter can open with something specific and "
    "true rather than generic. Search the web for the company named. Return 2-4 short factual "
    "sentences a candidate could plausibly reference: what the company builds, its product or "
    "domain, its tech stack if stated, notable recent news. Facts only — no praise, no advice, "
    "no invented detail. If you cannot find the company or are unsure it is the right one, say "
    "so in one line instead of guessing."
)

_CRITIC_INSTRUCTIONS = (
    "You review a drafted cover letter before a human sees it, and you are strict. Approve it "
    "only if ALL of these hold; otherwise list the concrete fixes, one short line each:\n"
    "- GROUNDING: every claim about the seeker's experience is supported by the provided "
    "experience bullets. Nothing from the posting's requirements is restated as the seeker's "
    "own experience; no employer, tool, number or skill appears that the bullets do not carry. "
    "This is the most important check — reject on any ungrounded claim.\n"
    "- The letter answers what THIS posting actually needs, using the two or three things that "
    "matter, not the whole CV.\n"
    "- No cover-letter clichés or resume-speak (excited/thrilled, passionate, proven track "
    "record, results-driven, team player, leverage, synergy, and the like), no marketing gloss, "
    "no throat-clearing opener. Plain, direct, first-person, concrete.\n"
    "- The real company name is used; no [Company] placeholders; any URL is intact.\n"
    "- Length and shape fit the channel (a chat message stays short; a form letter never "
    "mentions an attachment).\n"
    "Do not rewrite the letter yourself — name the problems so the drafter can fix them."
)


def _research_prompt(brief: JobBrief) -> str:
    location = f" (location: {brief.location})" if brief.location else ""
    return f"Company: {brief.company}{location}\nThe role: {brief.title}"


def _critic_prompt(brief: JobBrief, draft: CoverLetterDraft, bullets: list[str]) -> str:
    experience = "\n".join(f"- {b}" for b in bullets) or "- (no bullets retrieved)"
    return (
        f"POSTING\nTitle: {brief.title}\nCompany: {brief.company}\n"
        f"Description:\n{brief.description or '(none provided)'}\n\n"
        f"THE SEEKER'S REAL EXPERIENCE BULLETS (the only facts the letter may claim):\n"
        f"{experience}\n\n"
        f"DRAFT LETTER\nSubject: {draft.subject}\n\n{draft.body}\n\n"
        f"CLAIMED MATCH POINTS (the drafter's own note of what it leaned on):\n"
        + ("\n".join(f"- {p}" for p in draft.matched_points) or "- (none)")
    )


def _extra_context(state: AgentState) -> str:
    """Assemble the research + revision block appended to the drafting prompt."""
    parts: list[str] = []
    if state.research:
        parts.append(
            "COMPANY RESEARCH (context to make the opening specific and accurate — draw on it, "
            "but do NOT claim any of it as your own experience):\n" + state.research
        )
    if state.critiques:
        parts.append(
            "REVISION — a reviewer flagged these issues with your previous draft. Fix them, and "
            "keep every claim grounded in the experience bullets:\n" + state.critiques[-1]
        )
    return "\n\n".join(parts)


# --------------------------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------------------------


@dataclass
class DecideWorthIt(BaseNode[AgentState, AgentDeps, AgentResult]):
    """Soft stop-stack: read the whole posting, keep or decline (PLAN.md section 7)."""

    brief: JobBrief

    async def run(
        self, ctx: GraphRunContext[AgentState, AgentDeps]
    ) -> ResearchCompany | End[AgentResult]:
        verdict = (await ctx.deps.worth_it_agent.run(screen_prompt(self.brief))).output
        ctx.state.reasoning = verdict.reasoning
        if not verdict.worth_it:
            return End(AgentResult(worth_it=False, reasoning=verdict.reasoning))
        return ResearchCompany(self.brief)


@dataclass
class ResearchCompany(BaseNode[AgentState, AgentDeps, AgentResult]):
    """Short web search for company context. A nicety — never sinks the draft."""

    brief: JobBrief

    async def run(self, ctx: GraphRunContext[AgentState, AgentDeps]) -> Draft:
        if ctx.deps.do_research:
            try:
                result = await ctx.deps.research_agent.run(_research_prompt(self.brief))
                ctx.state.research = result.output.strip()
            except Exception:  # research is optional; a failure here must not stop the draft
                ctx.state.research = ""
        return Draft(self.brief)


@dataclass
class Draft(BaseNode[AgentState, AgentDeps, AgentResult]):
    """RAG draft, reusing the Phase 5 core so grounding and style are identical."""

    brief: JobBrief

    async def run(self, ctx: GraphRunContext[AgentState, AgentDeps]) -> Critic | End[AgentResult]:
        if ctx.state.bullets is None:
            ctx.state.bullets = retrieve_cv_bullets(self.brief)  # type: ignore[arg-type]
        try:
            ctx.state.draft = await generate_letter(
                self.brief,  # type: ignore[arg-type]
                ctx.state.bullets,
                language=self.brief.language,
                agent=ctx.deps.draft_agent,
                extra_context=_extra_context(ctx.state),
            )
        except UngroundedDraftError as exc:
            # The deterministic backstop fired: a fluent fabrication was suppressed. Stop here
            # rather than hand the human something to review — same stance as the plain path.
            return End(
                AgentResult(
                    worth_it=True,
                    reasoning=ctx.state.reasoning,
                    research=ctx.state.research,
                    critique=str(exc),
                    refused=True,
                    revisions=ctx.state.revisions,
                )
            )
        return Critic(self.brief)


@dataclass
class Critic(BaseNode[AgentState, AgentDeps, AgentResult]):
    """Second LLM pass. Bounces the draft back up to `max_revisions` times, then keeps it."""

    brief: JobBrief

    async def run(self, ctx: GraphRunContext[AgentState, AgentDeps]) -> Draft | End[AgentResult]:
        assert ctx.state.draft is not None and ctx.state.bullets is not None
        critique = (
            await ctx.deps.critic_agent.run(
                _critic_prompt(self.brief, ctx.state.draft, ctx.state.bullets)
            )
        ).output
        if not critique.approved and ctx.state.revisions < ctx.deps.max_revisions:
            ctx.state.revisions += 1
            ctx.state.critiques.append(critique.issues)
            return Draft(self.brief)
        return End(
            AgentResult(
                worth_it=True,
                reasoning=ctx.state.reasoning,
                draft=ctx.state.draft,
                research=ctx.state.research,
                critique=critique.issues,
                approved=critique.approved,
                revisions=ctx.state.revisions,
            )
        )


# --------------------------------------------------------------------------------------------
# Graph assembly and entry points
# --------------------------------------------------------------------------------------------


def _build_graph() -> Graph[AgentState, AgentDeps, JobBrief, AgentResult]:  # type: ignore[type-arg]
    g = GraphBuilder(
        state_type=AgentState,
        deps_type=AgentDeps,
        input_type=JobBrief,
        output_type=AgentResult,
    )

    @g.step
    async def enter(ctx: StepContext[AgentState, AgentDeps, JobBrief]) -> DecideWorthIt:  # type: ignore[type-arg]
        return DecideWorthIt(ctx.inputs)

    g.add(
        g.node(DecideWorthIt),
        g.node(ResearchCompany),
        g.node(Draft),
        g.node(Critic),
        g.edge_from(g.start_node).to(enter),
    )
    return g.build()


#: Built once; pure structure, no model or key touched until a run.
_GRAPH = _build_graph()


#: The screen the ordinary `draft` path also runs (`drafting/screen.py`) — same prompt, same
#: verdict shape, so the two paths cannot drift into disagreeing about the stop-stack.
make_worth_it_agent = make_screen_agent


def make_research_agent(model: Model | str) -> Agent[None, str]:
    """The research agent, with provider-native web search (local fallback where unsupported)."""
    return Agent(
        model, output_type=str, instructions=_RESEARCH_INSTRUCTIONS, capabilities=[WebSearch()]
    )


def make_critic_agent(model: Model | str) -> Agent[None, Critique]:
    return Agent(model, output_type=Critique, instructions=_CRITIC_INSTRUCTIONS)


def build_agent_deps(*, do_research: bool = True, max_revisions: int = 1) -> AgentDeps:
    """Production deps: the four agents on the configured model, key bridged once."""
    bridge_llm_api_key()
    model = get_settings().llm_model
    return AgentDeps(
        worth_it_agent=make_worth_it_agent(model),
        research_agent=make_research_agent(model),
        draft_agent=make_draft_agent(model),
        critic_agent=make_critic_agent(model),
        max_revisions=max_revisions,
        do_research=do_research,
    )


async def run_agent(brief: JobBrief, deps: AgentDeps) -> AgentResult:
    """Run the decide -> research -> draft -> critic graph over one posting."""
    result = await _GRAPH.run(state=AgentState(), deps=deps, inputs=brief)
    return cast("AgentResult", result)
