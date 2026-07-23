"""Reply classification (Phase 6). The second and last place with an LLM.

Incoming mail is pulled via the Gmail API (`replies.inbox`) and a structured-output model
decides what it is. Deliberately narrow: the model reads one email and returns one enum plus a
confidence. It never decides *which* application the mail belongs to — that is
`replies.match`, deterministic code, because a wrong correlation is unrecoverable in a way a
wrong label is not.

Low confidence is a first-class outcome: the caller records the classification but leaves the
Application status alone, so an uncertain call can never bury a real interview.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from funnel.config import bridge_llm_api_key, get_settings
from funnel.models import ReplyType

if TYPE_CHECKING:
    from pydantic_ai.models import Model

_INSTRUCTIONS = (
    "You classify a single email that a job seeker received after applying for jobs. "
    "Return exactly one reply_type:\n"
    "- 'interview': they want to talk. An invitation to a call, screen or interview, a test "
    "task, or a request for availability, salary expectations or further details all count.\n"
    "- 'rejection': they declined, closed the role, or filled it.\n"
    "- 'no_reply': anything that is not an answer about this candidacy — an automated "
    "acknowledgement of receipt, a newsletter, a job alert, an out-of-office bounce.\n\n"
    "Set confidence honestly. Use a value below 0.7 whenever the email is ambiguous, is in a "
    "language you are unsure of, or could plausibly be two of the classes: a human reviews "
    "everything under that mark, and an over-confident wrong label does far more damage than "
    "an admitted uncertainty. Give one short line of reasoning for that human."
)


class ReplyClassification(BaseModel):
    """Structured output: what this reply actually is."""

    reply_type: ReplyType
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(description="One line explaining why, for human review.")


def make_agent(model: Model | str) -> Agent[None, ReplyClassification]:
    """Build the classifier over any model (a TestModel in tests, the real one in prod)."""
    return Agent(model, output_type=ReplyClassification, instructions=_INSTRUCTIONS)


@lru_cache
def _production_agent() -> Agent[None, ReplyClassification]:
    bridge_llm_api_key()
    return make_agent(get_settings().llm_model)


def _build_prompt(subject: str, from_address: str, body: str) -> str:
    return f"From: {from_address}\nSubject: {subject}\n\n{body or '(empty body)'}"


async def classify_reply(
    subject: str,
    from_address: str,
    body: str,
    *,
    agent: Agent[None, ReplyClassification] | None = None,
) -> ReplyClassification:
    """Classify one incoming email. Reads only; changes nothing by itself."""
    active = agent or _production_agent()
    result = await active.run(_build_prompt(subject, from_address, body))
    return result.output
