"""Configuration via Pydantic Settings. The only place that reads the environment."""

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
    cover_letter_language: Literal["en", "ru"] = Field(
        default="en",
        description="OPEN QUESTION (PLAN.md section 7): cover letter language.",
    )

    # --- Gmail: board alerts. We do not scrape LinkedIn. ---
    gmail_credentials_path: Path = Field(default=Path("secrets/gmail_credentials.json"))
    gmail_token_path: Path = Field(default=Path("secrets/gmail_token.json"))

    # --- Matching ---
    match_top_k: int = Field(default=25, ge=1, description="Shortlist size after ranking.")
    match_score_threshold: float = Field(default=0.0, ge=-1.0, le=1.0)

    # --- Admin ---
    admin_host: str = Field(default="127.0.0.1")
    admin_port: int = Field(default=8000)


@lru_cache
def get_settings() -> Settings:
    """Cached singleton. Call this instead of constructing Settings() directly."""
    return Settings()
