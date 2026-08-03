"""Configuration via Pydantic Settings. The only place that reads the environment."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database ---
    database_url: PostgresDsn = Field(
        default=PostgresDsn("postgresql+psycopg://funnel:funnel@localhost:5432/funnel"),
        description="Connection string. Host is `db` inside compose, `localhost` otherwise.",
    )

    # --- Embeddings: local, fastembed/ONNX, zero tokens ---
    embedding_model: str = Field(
        default="intfloat/multilingual-e5-large",
        description=(
            "Multilingual e5 (letters are EN, but RU postings must embed sensibly). e5 models "
            "require query:/passage: prefixes — handled in matching/embed.py. The decided "
            "e5-small is not in fastembed 0.8.0; e5-large is the same family (human confirmed "
            "2026-07-22). See the conventions section of CLAUDE.md."
        ),
    )
    embedding_cache_dir: Path = Field(
        default=Path(".cache/fastembed"),
        description="Where fastembed stores downloaded ONNX weights.",
    )
    embedding_batch_size: int = Field(
        default=16,
        gt=0,
        description=(
            "Postings per ONNX forward pass. fastembed defaults to 256, which on e5-large "
            "(24 layers, 1024 hidden, 512 tokens) allocates activations by the gigabyte and "
            "got the run OOM-killed at ~13GB on a 24GB laptop. 16 keeps the peak under a "
            "gigabyte; the throughput difference is small because the CPU saturates anyway."
        ),
    )

    # --- Profile (one active profile; multi-profile shelved, see PLAN.md section 4) ---
    profiles_dir: Path = Field(
        default=Path("data/profiles"),
        description="Gitignored. Holds _experience.md (shared) plus one header per role.",
    )
    active_profile: str = Field(
        default="backend",
        description="The role header file (<name>.md) matched against, minus the .md suffix.",
    )

    # --- Source APIs (aggregators). Keys are secrets, so they live here, never in Source.config
    #     (invariant 7); non-secret params like countries/query stay in Source.config. ---
    adzuna_app_id: str | None = Field(default=None)
    adzuna_app_key: SecretStr | None = Field(default=None)
    themuse_api_key: SecretStr | None = Field(
        default=None, description="Optional; The Muse works keyless at a lower rate limit."
    )

    # --- LLM: confined to drafting/ and replies/ ---
    llm_model: str = Field(
        default="anthropic:claude-sonnet-5",
        description=(
            "pydantic-ai model string (provider:model). Human-confirmed decision 2026-07-23: "
            "Sonnet 5 over the cheaper Haiku for cover-letter quality (~2x cost, accepted) — "
            "the 'explicit decision' invariant 4 asks for. Still not a frontier model."
        ),
    )
    llm_api_key: SecretStr | None = Field(default=None)
    draft_screen: bool = Field(
        default=True,
        description=(
            "Run the soft stop-stack screen (drafting/screen.py) before drafting: one cheap "
            "call that declines PHP/Node/fullstack-first roles and obvious content mismatches "
            "instead of writing a letter the human then marks DECLINED by hand."
        ),
    )
    screen_model: str | None = Field(
        default=None,
        description=(
            "Model for that screen; falls back to llm_model. The screen is a coarse binary "
            "judgment, not prose, so a cheaper model than the one chosen for letter quality is "
            "the obvious setting (e.g. anthropic:claude-haiku-4-5-20251001) — left unset "
            "because picking it is the human's call (invariant 8)."
        ),
    )
    cover_letter_language: Literal["en", "ru"] = Field(
        default="en",
        description="OPEN QUESTION (PLAN.md section 7): cover letter language.",
    )
    reply_confidence_threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description=(
            "Below this, a classified reply is recorded but the Application status is left "
            "alone. A wrong auto-status is worse than no status: it hides a real interview."
        ),
    )
    reply_lookback_days: int = Field(
        default=14, ge=1, description="How far back `funnel check-replies` scans the mailbox."
    )

    # --- Gmail: board alerts. We do not scrape LinkedIn. ---
    gmail_credentials_path: Path = Field(default=Path("secrets/gmail_credentials.json"))
    gmail_token_path: Path = Field(default=Path("secrets/gmail_token.json"))

    # --- Matching ---
    match_top_k: int = Field(default=25, ge=1, description="Shortlist size after ranking.")
    #: A floor, not a selector. `match_top_k` already decides how many postings get drafted for;
    #: this decides when there are simply not that many worth drafting for, so a thin week ends
    #: with three letters instead of twenty-five padded out with whatever ranked highest.
    #:
    #: Expressed as a percentile rather than a score on purpose. A centered score is only
    #: meaningful against the corpus it was centered on, and that corpus grows with every
    #: ingest — an absolute floor would quietly drift. A percentile means the same thing in
    #: every run. Measured 2026-07-31 over 2061 scored rows: rank 25 sits at 97.6, rank 100 at
    #: 91.0 ("Senior Vue Developer" — arguable but a real backend-adjacent role), rank 206 at
    #: 81.3 ("Virtual Assistant" — not). 90 lands between the last defensible row and the first
    #: indefensible one, and is non-binding at today's volume: it only bites when the pool is thin.
    match_percentile_threshold: float = Field(
        default=90.0,
        ge=0.0,
        le=100.0,
        description="Minimum match percentile a posting needs before it is drafted for.",
    )
    #: Added to a remote posting's score when the shortlist is ordered. A preference, not a
    #: filter — `matching/filters.py` deliberately keeps on-site postings ("it merely ranks
    #: below remote, and ranking is a sort, not this predicate"), and sorting by `is_remote`
    #: first turned that sort into a partition: 893 remote rows stood ahead of 1718 on-site
    #: ones, so the best-matching posting in the database sat at rank 894 while a 3D artist
    #: made the top 25 (measured 2026-08-03).
    #:
    #: 0.02 is about a quarter of the score spread (sd 0.09 centered, and the two pools score
    #: alike: remote mean 0.025, on-site -0.016). At that size remote wins a tie and a small
    #: deficit, and a clearly better on-site role still outranks a mediocre remote one — the
    #: top 25 came out 13 remote / 12 on-site. Raise it to lean harder on remote; 0 sorts on
    #: merit alone.
    remote_bonus: float = Field(
        default=0.02,
        ge=0.0,
        description="Score bonus for a remote posting when ordering the shortlist.",
    )

    # --- Admin ---
    admin_host: str = Field(default="127.0.0.1")
    admin_port: int = Field(default=8000)


@lru_cache
def get_settings() -> Settings:
    """Cached singleton. Call this instead of constructing Settings() directly."""
    return Settings()


#: `llm_model` is "provider:model"; each provider SDK reads its own env var for the key, so
#: our single LLM_API_KEY has to be bridged onto it. Provider-agnostic apart from this map.
_PROVIDER_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google-gla": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}


def bridge_llm_api_key() -> None:
    """Copy LLM_API_KEY onto the env var the configured provider expects.

    Lives here rather than next to the agents because both LLM call sites (`drafting/` and
    `replies/`) need it, and duplicating the map would let the two drift. It deliberately
    does not import pydantic-ai: this is environment plumbing, not a model call, which is
    what keeps it outside the invariant-4 boundary.
    """
    settings = get_settings()
    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else ""
    if not key:
        return
    env_var = _PROVIDER_ENV.get(settings.llm_model.split(":", 1)[0])
    if env_var and not os.environ.get(env_var):
        os.environ[env_var] = key
