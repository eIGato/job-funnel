"""Optional agent layer (Phase 7). Do not start this until Phases 1-6 work.

The graph: decide-worth-it -> research-company (web search) -> draft -> critic. It runs
over a handful of top-N postings, not over the stream: the funnel has already squeezed
thousands down to dozens for free.

OPEN QUESTION (PLAN.md section 7): LangGraph, which is a recognizable resume line, or
pydantic-graph, which is coherent with the rest of the stack. Undecided, so neither
dependency is installed yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from funnel.models import Job


async def run_agent(jobs: list[Job]) -> None:
    """Run the agent graph over the top-N postings."""
    raise NotImplementedError("Phase 7: agent layer; choose LangGraph vs pydantic-graph first")
