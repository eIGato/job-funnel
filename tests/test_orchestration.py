"""Phase 7 agent layer over the top-N shortlist.

Offline: every one of the four model calls is a pydantic-ai `TestModel` with a fixed output,
injected through `AgentDeps`, and the embedding-backed bullet retrieval is monkeypatched. So
the whole `decide -> research -> draft -> critic` graph runs with no network and no model
download — the point here is the routing and the persistence contract, not model quality.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from funnel.orchestration import agent as orch
from funnel.orchestration.agent import (
    AgentDeps,
    CoverLetterDraft,
    Critique,
    JobBrief,
    WorthItVerdict,
    run_agent,
)

if TYPE_CHECKING:
    import pytest

    from funnel.schemas import NormalizedJob

_BULLETS = ["Built ETL pipelines and payment integrations at scale here"]


def _deps(
    *,
    worth_it: bool = True,
    approved: bool = True,
    issues: str = "",
    matched_points: list[str] | None = None,
    max_revisions: int = 1,
) -> AgentDeps:
    """Four TestModels with pinned outputs; research is off so the graph stays offline."""
    draft_args = {
        "body": "Hi.\n\nI built ETL pipelines and payment integrations.\n\nLet's talk.",
        "subject": "Data Engineer",
        "matched_points": matched_points if matched_points is not None else _BULLETS,
    }
    return AgentDeps(
        worth_it_agent=Agent(
            TestModel(custom_output_args={"worth_it": worth_it, "reasoning": "reason"}),
            output_type=WorthItVerdict,
        ),
        research_agent=Agent(
            TestModel(custom_output_text="Acme builds ETL tools."), output_type=str
        ),
        draft_agent=Agent(TestModel(custom_output_args=draft_args), output_type=CoverLetterDraft),
        critic_agent=Agent(
            TestModel(custom_output_args={"approved": approved, "issues": issues}),
            output_type=Critique,
        ),
        max_revisions=max_revisions,
        do_research=False,
    )


def _brief(job: NormalizedJob) -> JobBrief:
    return JobBrief.from_job(job)


def test_from_job_detects_language(job: NormalizedJob) -> None:
    assert _brief(job).language == "en"
    ru = job.model_copy(update={"title": "Инженер данных"})
    assert JobBrief.from_job(ru).language == "ru"


def test_decline_stops_before_drafting(job: NormalizedJob, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orch, "retrieve_cv_bullets", lambda _j, top_k=5: _BULLETS)
    result = asyncio.run(run_agent(_brief(job), _deps(worth_it=False)))
    assert result.worth_it is False
    assert result.draft is None
    assert result.reasoning == "reason"


def test_worth_it_produces_a_grounded_draft(
    job: NormalizedJob, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orch, "retrieve_cv_bullets", lambda _j, top_k=5: _BULLETS)
    result = asyncio.run(run_agent(_brief(job), _deps(approved=True)))
    assert result.worth_it is True
    assert result.approved is True
    assert result.revisions == 0
    assert result.draft is not None
    assert result.draft.subject and result.draft.body


def test_critic_bounces_the_draft_back_once(
    job: NormalizedJob, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orch, "retrieve_cv_bullets", lambda _j, top_k=5: _BULLETS)
    result = asyncio.run(
        run_agent(_brief(job), _deps(approved=False, issues="too generic", max_revisions=1))
    )
    # One revision, then the loop stops (max_revisions=1) and the draft is kept, unapproved.
    assert result.revisions == 1
    assert result.approved is False
    assert result.critique == "too generic"
    assert result.draft is not None


def test_ungrounded_draft_is_refused(job: NormalizedJob, monkeypatch: pytest.MonkeyPatch) -> None:
    """The grounding backstop from Phase 5 still governs the agent's draft node."""
    monkeypatch.setattr(orch, "retrieve_cv_bullets", lambda _j, top_k=5: _BULLETS)
    result = asyncio.run(
        run_agent(
            _brief(job),
            _deps(matched_points=["Trained deep neural networks on large GPU clusters"]),
        )
    )
    assert result.worth_it is True
    assert result.draft is None
    assert result.refused is True
    assert result.critique  # carries the backstop's explanation


def test_research_flows_into_the_draft_prompt(
    job: NormalizedJob, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With research on, the research text reaches generate_letter as extra_context."""
    monkeypatch.setattr(orch, "retrieve_cv_bullets", lambda _j, top_k=5: _BULLETS)
    seen: dict[str, str] = {}

    async def _capture(job_arg, bullets, *, language, agent, extra_context=""):  # type: ignore[no-untyped-def]
        seen["extra_context"] = extra_context
        return CoverLetterDraft(body="b", subject="s", matched_points=_BULLETS)

    monkeypatch.setattr(orch, "generate_letter", _capture)
    deps = _deps()
    deps.do_research = True
    deps.research_agent = Agent(
        TestModel(custom_output_text="Acme builds ETL tools."), output_type=str
    )
    asyncio.run(run_agent(_brief(job), deps))
    assert "Acme builds ETL tools." in seen["extra_context"]
