"""The adapter interface and its registry.

A new source is a new BaseAdapter subclass plus @register. The pipeline resolves an
adapter through the registry and never branches on a specific source.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from funnel.models import Source
    from funnel.schemas import NormalizedJob


class BaseAdapter(ABC):
    """A source of job postings.

    fetch() is async because adapters do network I/O through httpx. The synchronous core
    bridges it with asyncio.run() at the CLI boundary.
    """

    #: Registry key for this adapter. Matches Source.name.
    name: ClassVar[str]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    @abstractmethod
    async def fetch(self) -> list[NormalizedJob]:
        """Pull postings from the source and normalize them.

        Do not deduplicate here; ingest handles that via content_hash.
        """


_REGISTRY: dict[str, type[BaseAdapter]] = {}


def register(cls: type[BaseAdapter]) -> type[BaseAdapter]:
    """Decorator that puts an adapter into the registry under its .name."""
    if not getattr(cls, "name", None):
        raise ValueError(f"{cls.__name__} must declare a ClassVar name")
    if cls.name in _REGISTRY:
        raise ValueError(f"Adapter {cls.name!r} is already registered")
    _REGISTRY[cls.name] = cls
    return cls


def get_adapter(source: Source) -> BaseAdapter:
    """Build the adapter for a Source row."""
    try:
        cls = _REGISTRY[source.name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise LookupError(f"No adapter named {source.name!r}. Known adapters: {known}") from None
    return cls(source.config)


def registry() -> dict[str, type[BaseAdapter]]:
    return dict(_REGISTRY)


__all__ = ["BaseAdapter", "get_adapter", "register", "registry"]
