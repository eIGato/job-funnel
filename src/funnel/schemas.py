"""Pydantic schemas. The contract between adapters and the pipeline."""

from __future__ import annotations

import hashlib
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, computed_field

from funnel.models import SourceKind


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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_hash(self) -> str:
        """Dedup key: normalized company+title+url, so a repeat ingest is a no-op."""
        raw = "|".join(
            (
                self.company.strip().casefold(),
                self.title.strip().casefold(),
                str(self.url).strip().casefold(),
            )
        )
        return hashlib.sha256(raw.encode()).hexdigest()

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
