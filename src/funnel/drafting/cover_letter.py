"""Cover letter generation (Phase 5). One of only two places with an LLM.

RAG: the prompt gets the profile bullets retrieved for this specific posting by the same
cosine similarity used for matching, not the whole profile. Retrieval, then augmentation,
then generation.

This module sends nothing. It returns text; the caller stores it in Application.cover_letter
and the human does the sending (invariant 2). The model is reached only through pydantic-ai
(invariant 4), cheap by default (invariant 5); the provider/model is settings.llm_model.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import TYPE_CHECKING

import numpy as np
from pydantic import BaseModel, Field
from pydantic_ai import Agent

from funnel.config import get_settings
from funnel.matching.embed import cosine_similarity, embed_texts
from funnel.matching.profile import load_profile_text, load_writing_style

if TYPE_CHECKING:
    from numpy.typing import NDArray
    from pydantic_ai.models import Model

    from funnel.models import Job

#: A profile line shorter than this is a heading or a label, not a usable bullet.
_MIN_BULLET_CHARS = 30
#: Cyrillic anywhere in the posting -> write in Russian (detected in code, not by the LLM).
_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)  # noqa: RUF001 (Cyrillic is the point)
#: settings.llm_model is "provider:model"; pydantic-ai reads the provider's own env var for the
#: key, so bridge our single LLM_API_KEY onto it. Provider-agnostic apart from this name map.
_PROVIDER_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google-gla": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}

_INSTRUCTIONS = (
    "You draft short, honest cover letters for a job seeker. You are given a posting and a "
    "set of the seeker's real experience bullets already selected as most relevant. Write a "
    "concise letter (roughly 150-220 words) that connects that experience to the posting's "
    "needs. Ground every claim in the provided bullets — never invent employers, titles, "
    "numbers, or skills the bullets do not support. No greeting-name guesses, no placeholders "
    "like [Company]; use the real company name given. Write in the language you are told to. "
    "You produce a draft only; a human reviews and sends it.\n\n"
    "Write like a specific human being, not like a language model:\n"
    "- No cover-letter clichés or resume-speak. Never write 'excited/thrilled to', 'passionate "
    "about', 'proven track record', 'results-driven', 'detail-oriented', 'team player', 'hit "
    "the ground running', 'wear many hats', 'fast-paced environment', 'I believe I would be a "
    "great fit', 'leverage', 'synergy', or their equivalents in any language.\n"
    "- No polished marketing gloss and no throat-clearing opener. Plain, direct, first-person "
    "prose. Be concrete: name the actual thing built or problem solved instead of describing "
    "yourself with adjectives. Open with something specific to this role or company.\n"
    "- Vary sentence length. Avoid the tidy tricolon ('X, Y, and Z'), the 'not only… but also' "
    "construction, and a relentless em-dash rhythm. A little plain or a little blunt is fine.\n"
    "- Don't restate the whole CV. Pick the two or three things that actually matter for this "
    "posting and say them well."
)


class CoverLetterDraft(BaseModel):
    """Structured output from the model."""

    body: str = Field(description="Letter body, without subject line or signature.")
    subject: str = Field(description="Subject line.")
    matched_points: list[str] = Field(
        default_factory=list,
        description="CV points the letter leans on, surfaced for human review.",
    )


@lru_cache
def _profile_bullets() -> tuple[str, ...]:
    """Split the active profile into retrievable bullets (headings and labels dropped)."""
    bullets: list[str] = []
    for line in load_profile_text().splitlines():
        cleaned = line.strip().lstrip("-*•#").strip()
        if len(cleaned) >= _MIN_BULLET_CHARS:
            bullets.append(cleaned)
    return tuple(bullets)


@lru_cache
def _bullet_matrix() -> NDArray[np.float32]:
    """Embed the profile bullets once (passage side of the e5 pair)."""
    return embed_texts(list(_profile_bullets()), is_query=False)


@lru_cache
def _writing_style() -> str:
    """The human's style sample, read once. Empty string when no sample file is present."""
    return load_writing_style()


def _job_query(job: Job) -> str:
    return f"{job.title}\n{job.company}\n{job.description}".strip()


def retrieve_cv_bullets(job: Job, *, top_k: int = 5) -> list[str]:
    """Return the profile bullets most relevant to this posting: the retrieval half of RAG."""
    bullets = _profile_bullets()
    if not bullets:
        return []
    job_vector = embed_texts([_job_query(job)], is_query=True)[0]
    scores = cosine_similarity(_bullet_matrix(), job_vector)
    top = np.argsort(scores)[::-1][:top_k]
    return [bullets[int(i)] for i in top]


def _detect_language(job: Job) -> str:
    """RU when the posting itself is Russian, else the configured default (invariant 4)."""
    if _CYRILLIC.search(f"{job.title} {job.description}"):
        return "ru"
    return get_settings().cover_letter_language


def _build_prompt(job: Job, bullets: list[str], language: str) -> str:
    language_name = "Russian" if language == "ru" else "English"
    experience = "\n".join(f"- {bullet}" for bullet in bullets) or "- (no bullets retrieved)"
    style = _writing_style()
    style_block = (
        "\n\nMY WRITING STYLE — a sample of how I actually write. Match its tone, rhythm and "
        "plainness; do NOT reuse its wording or its facts:\n"
        f"{style}"
        if style
        else ""
    )
    return (
        f"Write the cover letter in {language_name}.\n\n"
        f"POSTING\n"
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Location: {job.location or 'n/a'}\n"
        f"Description:\n{job.description or '(none provided)'}\n\n"
        f"MY RELEVANT EXPERIENCE (use only what fits; invent nothing beyond this):\n{experience}"
        f"{style_block}"
    )


def make_agent(model: Model | str) -> Agent[None, CoverLetterDraft]:
    """Build the drafting agent over any model (a TestModel in tests, the real one in prod)."""
    return Agent(model, output_type=CoverLetterDraft, instructions=_INSTRUCTIONS)


@lru_cache
def _production_agent() -> Agent[None, CoverLetterDraft]:
    settings = get_settings()
    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else ""
    if key:
        provider = settings.llm_model.split(":", 1)[0]
        env_var = _PROVIDER_ENV.get(provider)
        if env_var and not os.environ.get(env_var):
            os.environ[env_var] = key
    return make_agent(settings.llm_model)


async def draft_cover_letter(
    job: Job, *, agent: Agent[None, CoverLetterDraft] | None = None
) -> CoverLetterDraft:
    """Generate a draft. Does not send, and must never learn how."""
    active = agent or _production_agent()
    bullets = retrieve_cv_bullets(job)
    prompt = _build_prompt(job, bullets, _detect_language(job))
    result = await active.run(prompt)
    return result.output
