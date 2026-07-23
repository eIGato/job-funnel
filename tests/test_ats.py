"""Phase 3.5 D. Slug discovery is what keeps the ATS set self-growing *and* bounded.

The dangerous failure is a false positive: a junk slug is polled on every run forever, so the
"must not match" cases matter more here than the hits.
"""

from __future__ import annotations

import pytest

from funnel.adapters import registry
from funnel.adapters.ats import discover_slugs
from funnel.models import AtsProvider


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Apply at https://boards.greenhouse.io/acme/jobs/123", {"acme"}),
        ("https://job-boards.greenhouse.io/acmecorp", {"acmecorp"}),
        ("see boards.eu.greenhouse.io/euco/jobs/9", {"euco"}),
        ("https://acme.greenhouse.io/", {"acme"}),
        ("nothing to see here", set()),
    ],
)
def test_discover_greenhouse(text: str, expected: set[str]) -> None:
    assert discover_slugs(AtsProvider.GREENHOUSE, text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://jobs.lever.co/someco/abc-123", {"someco"}),
        ("https://jobs.eu.lever.co/euco", {"euco"}),
        ("https://jobs.ashbyhq.com/other", set()),  # wrong provider
    ],
)
def test_discover_lever(text: str, expected: set[str]) -> None:
    assert discover_slugs(AtsProvider.LEVER, text) == expected


def test_discover_ashby() -> None:
    assert discover_slugs(AtsProvider.ASHBY, "https://jobs.ashbyhq.com/wonder/x") == {"wonder"}


def test_slugs_are_lowercased_and_deduped() -> None:
    text = "boards.greenhouse.io/Acme and boards.greenhouse.io/acme again"
    assert discover_slugs(AtsProvider.GREENHOUSE, text) == {"acme"}


@pytest.mark.parametrize(
    "text",
    [
        "https://boards.greenhouse.io/embed/job_board?for=acme",  # 'embed' is not a company
        "https://boards.greenhouse.io/jobs",
        "https://jobs.lever.co/apply",
    ],
)
def test_structural_path_segments_are_not_slugs(text: str) -> None:
    """A greedy pattern would record these and then poll them on every run, forever."""
    for provider in AtsProvider:
        assert not discover_slugs(provider, text) - {"acme"}


def test_discovery_scans_free_text_not_just_urls() -> None:
    """The link usually sits inside an aggregator's description, not in Job.url."""
    description = "Great role. To apply, go to https://jobs.lever.co/coolstartup/1 today."
    assert discover_slugs(AtsProvider.LEVER, description) == {"coolstartup"}


def test_ats_adapters_are_registered() -> None:
    known = registry()
    for name in ("greenhouse", "lever", "ashby"):
        assert name in known, f"{name} adapter did not register"
