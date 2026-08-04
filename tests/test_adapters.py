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


def test_strip_html_keeps_text_after_a_bare_ampersand() -> None:
    """Regression (2026-08-03): HTMLParser buffers an unresolved entity until the feed closes.

    RemoteOK served "Patti&amp;More!" as a company name. Unescaped it becomes "Patti&More!",
    the parser held "&More!" back as a possible character reference, and without `close()` the
    whole string came out empty — which fails NormalizedJob's min_length and took the entire
    ingest batch down with it.
    """
    from funnel.adapters.util import strip_html

    assert strip_html("Patti&amp;More!") == "Patti&More!"
    assert strip_html("R&amp;D") == "R&D"
    assert strip_html("Tom &amp; Jerry &amp;") == "Tom & Jerry &"
    assert strip_html("A &amp; B Ltd") == "A & B Ltd"
