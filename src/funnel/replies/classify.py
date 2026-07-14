"""Reply classification (Phase 6). The second and last place with an LLM.

Incoming mail is pulled via the Gmail API and a structured-output model sets reply_type.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from funnel.models import ReplyType


class ReplyClassification(BaseModel):
    """Structured output: what this reply actually is."""

    reply_type: ReplyType
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(description="One line explaining why, for human review.")


async def classify_reply(email_body: str) -> ReplyClassification:
    """Classify an incoming email."""
    # TODO Phase 6:
    #   Agent(settings.llm_model, output_type=ReplyClassification).
    #   On low confidence, leave it for the human rather than guessing.
    raise NotImplementedError("Phase 6: reply classifier via pydantic-ai")
