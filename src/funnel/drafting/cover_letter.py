"""Cover letter generation (Phase 5). One of only two places with an LLM.

RAG: the prompt gets the CV bullets retrieved for this specific posting by the same
cosine similarity, not the whole CV. Retrieval, then augmentation, then generation.

This module sends nothing. It returns text, the caller stores it in
Application.cover_letter, and the human does the sending (invariant 2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from funnel.models import Job


class CoverLetterDraft(BaseModel):
    """Structured output from the model."""

    body: str = Field(description="Letter body, without subject line or signature.")
    subject: str = Field(description="Subject line.")
    matched_points: list[str] = Field(
        default_factory=list,
        description="CV points the letter leans on, surfaced for human review.",
    )


def retrieve_cv_bullets(job: Job, *, top_k: int = 5) -> list[str]:
    """Return the CV bullets most relevant to this posting: the retrieval half of RAG."""
    # TODO Phase 5:
    #   1. Load settings.cv_path and split it into bullets; cache their embeddings.
    #      OPEN QUESTION (PLAN.md section 7): CV path, format and splitting rule.
    #   2. embed_texts(bullets) once, then cosine_similarity against the job vector.
    #   3. Return the top_k.
    raise NotImplementedError("Phase 5: CV bullet retrieval")


async def draft_cover_letter(job: Job) -> CoverLetterDraft:
    """Generate a draft. Does not send, and must never learn how."""
    # TODO Phase 5:
    #   Agent(settings.llm_model, output_type=CoverLetterDraft, instructions=...) with
    #   retrieve_cv_bullets(job) in the prompt. Cheap model by default (invariant 4).
    #   Language comes from settings.cover_letter_language.
    raise NotImplementedError("Phase 5: generation via pydantic-ai")
