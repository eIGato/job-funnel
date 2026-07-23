"""Optional agent layer (Phase 7). Do not start this until Phases 1-6 work.

The graph: decide-worth-it -> research-company (web search) -> draft -> critic. It runs
over a handful of top-N postings, not over the stream: the funnel has already squeezed
thousands down to dozens for free. Two nodes carry weight beyond nicety:
  - **decide-worth-it** is where the soft stop-stack lives (PLAN.md section 7) — reading the
    whole posting to judge whether PHP/Node/fullstack is the *emphasis*, which the Phase 4a
    regex filters deliberately do not attempt.
  - **critic** is a second LLM pass over the draft; it is the layer that would have caught the
    2026-07-23 fabrication before it reached a human (drafting/cover_letter.py now has a
    deterministic backstop for that, but the critic is the richer check).

Library: **pydantic-graph** (decided 2026-07-24) — coherent with the pydantic/pydantic-ai
stack already in use. Not yet installed; add it with `uv add` when this phase starts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from funnel.models import Job


async def run_agent(jobs: list[Job]) -> None:
    """Run the pydantic-graph agent over the top-N postings (Phase 7)."""
    raise NotImplementedError("Phase 7: agent layer over top-N via pydantic-graph")
