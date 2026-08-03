"""The soft stop-stack screen that runs before any letter is drafted.

Offline: the model is a pydantic-ai `TestModel` with a pinned verdict. What is under test is
the wiring — that `draft` screens at all, that a "no" becomes DECLINED without spending a
drafting call, and that the agent layer and the plain path share one prompt. Model judgment
quality is not testable here and is not the point.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from funnel.drafting.screen import SCREEN_INSTRUCTIONS, WorthItVerdict, screen_job, screen_prompt


@dataclass
class _Posting:
    title: str = "Senior Fullstack Developer (React.js / Node.js)"
    company: str = "Proxify AB"
    location: str | None = "Remote"
    description: str = "You will build React front-ends and Node services."


def _agent(*, worth_it: bool, reasoning: str = "reason") -> Agent[None, WorthItVerdict]:
    return Agent(
        TestModel(custom_output_args={"worth_it": worth_it, "reasoning": reasoning}),
        output_type=WorthItVerdict,
    )


def test_screen_returns_the_models_verdict() -> None:
    verdict = asyncio.run(screen_job(_Posting(), agent=_agent(worth_it=False, reasoning="Node")))
    assert verdict.worth_it is False
    assert verdict.reasoning == "Node"


def test_prompt_carries_the_whole_posting() -> None:
    """The emphasis judgment needs the body, not just the title — that is the whole point."""
    prompt = screen_prompt(_Posting())
    assert "Senior Fullstack Developer" in prompt
    assert "Proxify AB" in prompt
    assert "React front-ends" in prompt


def test_instructions_forbid_geography() -> None:
    """Geography is settled by deterministic filters; the model must not re-litigate it."""
    assert "DO NOT consider geography" in SCREEN_INSTRUCTIONS
    assert "must NEVER be a reason here" in SCREEN_INSTRUCTIONS


def test_agent_layer_shares_this_prompt_and_verdict() -> None:
    """One definition, two callers: the graph must not grow a second copy that drifts."""
    from funnel.drafting.screen import make_screen_agent
    from funnel.orchestration import agent as orch

    assert orch.WorthItVerdict is WorthItVerdict
    assert orch.make_worth_it_agent is make_screen_agent


def test_instructions_forbid_a_stated_geography_requirement_too() -> None:
    """Regression (2026-08-03): a posting that *demands* a geography got one declined for it.

    "Requires Russian or Belarusian citizenship and UTC+3 timezone residency" is the forbidden
    reason written out in full. A blanket "do not consider geography" did not cover the case
    where the posting states the requirement itself.
    """
    assert "citizenship" in SCREEN_INSTRUCTIONS
    assert "even when the posting states such a requirement outright" in SCREEN_INSTRUCTIONS


def test_instructions_close_the_stop_stack_to_other_backend_languages() -> None:
    """Regression (2026-08-03): "Software Engineer GO" was declined for not being Python.

    The stop-stack is PHP, Node and fullstack. Go, Java, C#, Rust and the rest are the work the
    seeker does; "not Python" is not a reason, and treating it as one throws away real hits.
    """
    assert "THAT LIST IS THE WHOLE STOP-STACK" in SCREEN_INSTRUCTIONS
    assert "'Not Python' is NEVER a reason on its own" in SCREEN_INSTRUCTIONS
    for language in ("Go", "Java", "C#", "Rust"):
        assert language in SCREEN_INSTRUCTIONS, language
