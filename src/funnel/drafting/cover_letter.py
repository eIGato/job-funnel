"""Cover letter generation (Phase 5). One of only two places with an LLM.

RAG: the prompt gets the profile bullets retrieved for this specific posting by the same
cosine similarity used for matching, not the whole profile. Retrieval, then augmentation,
then generation.

This module sends nothing. It returns text; the caller stores it in Application.cover_letter
and the human does the sending (invariant 2). The model is reached only through pydantic-ai
(invariant 4), cheap by default (invariant 5); the provider/model is settings.llm_model.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import TYPE_CHECKING

import numpy as np
from pydantic import BaseModel, Field
from pydantic_ai import Agent

from funnel.config import bridge_llm_api_key, get_settings
from funnel.drafting.prompting import UNTRUSTED_INPUT_RULE, posting_block
from funnel.matching.embed import cosine_similarity, embed_texts
from funnel.matching.profile import load_profile_text, load_writing_style
from funnel.models import ApplyChannel

if TYPE_CHECKING:
    from numpy.typing import NDArray
    from pydantic_ai.models import Model

    from funnel.models import Job

#: A profile line shorter than this is a heading or a label, not a usable bullet.
_MIN_BULLET_CHARS = 30
#: Cyrillic anywhere in the posting -> write in Russian (detected in code, not by the LLM).
_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)  # noqa: RUF001 (Cyrillic is the point)
_INSTRUCTIONS = (
    "You draft short, honest cover letters for a job seeker. You are given a posting and a "
    "set of the seeker's real experience bullets already selected as most relevant. Write a "
    "short letter that connects that experience to the posting's needs. Length follows the "
    "style sample rather than a word target — usually 40-120 words, and one or two sentences "
    "for a chat message. Never pad to reach a length. "
    "Ground every claim in the provided bullets — never invent employers, titles, "
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
    "posting and say them well.\n"
    "- Plain does not mean passive. Cut hedges ('maybe we are a fit', 'if you ever have "
    "something'), state the match as fact, and end on one concrete next step. Persuade with "
    "evidence and specifics, never with adjectives.\n"
    "- Carry the posting's own words for its key requirement — that phrase is what a skimming "
    "recruiter and a keyword filter both look for — BUT only when a bullet actually supports "
    "the claim. Naming a requirement is not the same as having met it.\n\n"
    "GROUNDING, which overrides everything above. The bullets are the only facts you have. If "
    "they do not support the posting's core requirement, do NOT claim it: write the shorter, "
    "honest letter about the overlap that does exist, or state plainly that the overlap is "
    "partial. Never restate a requirement from the posting as your own experience, and never "
    "name a tool, platform or domain that is absent from the bullets. `matched_points` must "
    "quote the bullets you actually used, close to verbatim — it is the human's audit trail, "
    "and inventing entries there is the worst thing you can do here. Copy any URL character "
    "for character; never shorten, tidy or reconstruct a link.\n\n"
    "FORMATTING: `body` must contain real newline characters and never be one run-on block. "
    "Greeting on its own line, then one or two short paragraphs, then the closing/next step as "
    "its own last paragraph. Separate paragraphs with a blank line. This holds for every "
    "channel — a long chat message is still broken into greeting, substance, and ask.\n\n"
    f"{UNTRUSTED_INPUT_RULE}"
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


#: A word shorter than this is a preposition or filler; counting it would let any sentence look
#: grounded just by sharing "with" and "the".
_MIN_TOKEN_CHARS = 5
#: Share of a matched_point's words that must also appear in the retrieved bullets for it to
#: count as grounded. Loose enough for paraphrase, tight enough to catch a fabricated claim.
_GROUNDING_RATIO = 0.5


class UngroundedDraftError(RuntimeError):
    """The model claimed experience the retrieved bullets do not support.

    Raised instead of returning the letter. Observed live on 2026-07-23: for a posting whose
    requirements the profile does not meet at all, the model wrote a fluent letter asserting the
    *posting's* requirements as the seeker's experience, and filled `matched_points` with CV
    points that do not exist. A fabricated draft in front of a human is worse than no draft — it
    only has to be believed once to be sent.
    """


def _tokens(text: str) -> set[str]:
    return {w for w in re.split(r"[^\w]+", text.lower()) if len(w) >= _MIN_TOKEN_CHARS}


def ungrounded_points(bullets: list[str], points: list[str]) -> list[str]:
    """The claimed CV points that the retrieved bullets do not actually support.

    Deterministic and cheap: the model is asked to quote what it leaned on, and this checks that
    the quotes came from what it was given. It cannot catch every hallucination in the prose,
    but it catches the failure that matters — inventing the evidence itself.
    """
    vocabulary = _tokens(" ".join(bullets))
    if not vocabulary:
        return []
    offenders = []
    for point in points:
        words = _tokens(point)
        if words and len(words & vocabulary) / len(words) < _GROUNDING_RATIO:
            offenders.append(point)
    return offenders


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


def _build_prompt(job: Job, bullets: list[str], language: str, extra_context: str = "") -> str:
    language_name = "Russian" if language == "ru" else "English"
    experience = "\n".join(f"- {bullet}" for bullet in bullets) or "- (no bullets retrieved)"
    style = _writing_style()
    style_block = (
        "\n\nMY WRITING STYLE — how I actually write to recruiters. Match its tone, rhythm and "
        "length. Its scaffolding (greeting, CV link, closing) is meant to be reused verbatim; "
        "its role-specific claims are NOT — those come from MY RELEVANT EXPERIENCE above:\n"
        f"{style}"
        if style
        else ""
    )
    # Company research and reviewer feedback the agent layer supplies (Phase 7). Empty on the
    # plain `draft` path. Appended last so it can override the opener, and the header inside it
    # (written by the caller) makes clear that research is context to draw on, not experience to
    # claim — the grounding rule in _INSTRUCTIONS still governs what may be asserted.
    context_block = f"\n\n{extra_context}" if extra_context else ""
    # A Job read back from the database always has one; fall back anyway, because FORM is the
    # conservative choice (it is the channel that must never mention an attachment).
    channel = job.apply_channel or ApplyChannel.FORM
    return (
        f"Write the cover letter in {language_name}.\n"
        f"CHANNEL: {channel.value} — follow that channel's rules in MY WRITING "
        f"STYLE below (they decide the length, the greeting, and whether an attached CV may "
        f"be mentioned at all).\n\n"
        f"POSTING\n"
        f"Title: {job.title}\n"
        f"URL: {job.url}\n"
        f"Company: {job.company}\n"
        f"Location: {job.location or 'n/a'}\n"
        f"Description:\n{posting_block(job.description)}\n\n"
        f"MY RELEVANT EXPERIENCE (use only what fits; invent nothing beyond this):\n{experience}"
        f"{style_block}"
        f"{context_block}"
    )


def make_agent(model: Model | str) -> Agent[None, CoverLetterDraft]:
    """Build the drafting agent over any model (a TestModel in tests, the real one in prod)."""
    return Agent(model, output_type=CoverLetterDraft, instructions=_INSTRUCTIONS)


@lru_cache
def _production_agent() -> Agent[None, CoverLetterDraft]:
    bridge_llm_api_key()
    return make_agent(get_settings().llm_model)


async def generate_letter(
    job: Job,
    bullets: list[str],
    *,
    language: str,
    agent: Agent[None, CoverLetterDraft],
    extra_context: str = "",
) -> CoverLetterDraft:
    """Prompt the model over already-retrieved bullets and enforce the grounding backstop.

    The generation core shared by the plain `draft` path (`draft_cover_letter`) and the Phase 7
    agent layer, which passes pre-retrieved bullets, a detected language, its own drafting agent,
    and `extra_context` (company research plus reviewer feedback). Keeping the grounding check
    here means both paths refuse a fabrication the same way.
    """
    prompt = _build_prompt(job, bullets, language, extra_context)
    result = await agent.run(prompt)
    draft = result.output

    # Refuse the draft rather than hand a human a fluent fabrication to review. This fires when
    # the posting is a poor fit: with no relevant bullets to use, the model fills the gap from
    # the posting's own requirements. Note the match score does not catch it — the live case
    # scored 0.841, barely under the best genuine match in the shortlist.
    offenders = ungrounded_points(bullets, draft.matched_points) if bullets else []
    if offenders and len(offenders) * 2 > len(draft.matched_points):
        raise UngroundedDraftError(
            f"{len(offenders)}/{len(draft.matched_points)} claimed CV points are not supported "
            f"by the retrieved bullets, e.g. {offenders[0]!r}. Not drafted."
        )
    return draft


async def draft_cover_letter(
    job: Job, *, agent: Agent[None, CoverLetterDraft] | None = None
) -> CoverLetterDraft:
    """Generate a draft. Does not send, and must never learn how."""
    active = agent or _production_agent()
    bullets = retrieve_cv_bullets(job)
    return await generate_letter(job, bullets, language=_detect_language(job), agent=active)
