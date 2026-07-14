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
        default="BAAI/bge-small-en-v1.5",
        description=(
            "Multilingual alternative: intfloat/multilingual-e5-small, which requires "
            "query:/passage: prefixes. See the conventions section of CLAUDE.md."
        ),
    )
    embedding_cache_dir: Path = Field(
        default=Path(".cache/fastembed"),
        description="Where fastembed stores downloaded ONNX weights.",
    )

    # --- Profile ---
    cv_path: Path = Field(
        default=Path("data/cv.md"),
        description="OPEN QUESTION (PLAN.md section 7): the real path and format of the CV.",
    )

    # --- LLM: confined to drafting/ and replies/ ---
    llm_model: str = Field(
        default="anthropic:claude-haiku-4-5",
        description=(
            "pydantic-ai model string (provider:model). Cheap by default; a frontier model "
            "only on an explicit decision. OPEN QUESTION (PLAN.md section 7): the provider."
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
