"""Pydantic schemas. The contract between adapters and the pipeline."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, computed_field, field_validator

from funnel.models import SourceKind, compute_content_hash

#: The String(255) columns behind location/external_id (models.py). Boards occasionally send
#: much longer values (WeWorkRemotely packs a country list into `region`), so the contract
#: truncates them here rather than letting a DataError blow up the whole ingest batch. Unlike
#: title/company, an over-long location or id is noise, not a parse bug — trim, don't reject.
_MAX_DB_STRING = 255


class NormalizedJob(BaseModel):
    """What every adapter must return from fetch().

    The pipeline knows only this shape and never the source it came from.
    """

    model_config = ConfigDict(frozen=True)

    url: HttpUrl
    company: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=255)
    description: str = ""
    location: str | None = None
    is_remote: bool = False
    posted_at: datetime | None = None
    external_id: str | None = None

    @field_validator("location", "external_id")
    @classmethod
    def _fit_db_column(cls, value: str | None) -> str | None:
        """Keep DB-bounded strings within their column width; over-long values are noise."""
        if value is not None and len(value) > _MAX_DB_STRING:
            return value[:_MAX_DB_STRING].rstrip()
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_hash(self) -> str:
        """Dedup key: normalized company+title+url, so a repeat ingest is a no-op."""
        return compute_content_hash(self.company, self.title, str(self.url))

    @property
    def embedding_text(self) -> str:
        """The text handed to the embedder."""
        return f"{self.title}\n{self.company}\n{self.description}".strip()


class SourceConfig(BaseModel):
    """Source description used for the registry and seeding."""

    name: str
    kind: SourceKind
    config: dict[str, object] = Field(default_factory=dict)
    enabled: bool = True
