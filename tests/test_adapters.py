"""The adapter registry: the seam that keeps the pipeline ignorant of specific sources."""

from __future__ import annotations

import pytest

from funnel import adapters
from funnel.models import Source, SourceKind


def test_shipped_adapters_are_registered() -> None:
    registry = adapters.registry()
    assert "gmail-alerts" in registry
    assert "remotive" in registry


def test_get_adapter_resolves_by_source_name() -> None:
    source = Source(name="remotive", kind=SourceKind.API, config={"base_url": "https://x.test"})
    adapter = adapters.get_adapter(source)
    assert adapter.name == "remotive"
    assert adapter.config == {"base_url": "https://x.test"}


def test_get_adapter_reports_unknown_source() -> None:
    source = Source(name="does-not-exist", kind=SourceKind.API, config={})
    with pytest.raises(LookupError, match="does-not-exist"):
        adapters.get_adapter(source)


def test_register_rejects_duplicate_name() -> None:
    class Duplicate(adapters.BaseAdapter):
        name = "remotive"

        async def fetch(self) -> list:  # type: ignore[type-arg]
            return []

    with pytest.raises(ValueError, match="already registered"):
        adapters.register(Duplicate)


def test_register_requires_a_name() -> None:
    class Nameless(adapters.BaseAdapter):
        async def fetch(self) -> list:  # type: ignore[type-arg]
            return []

    with pytest.raises(ValueError, match="ClassVar name"):
        adapters.register(Nameless)
