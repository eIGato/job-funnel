"""Finding the employer's own apply page for a dead-end posting.

Offline: the model is a pydantic-ai `TestModel` with a pinned proposal, and the verification
fetch is monkeypatched. What is under test is the boundary between the two halves — that a
proposed URL is never trusted on its own, and that every way of failing produces a miss rather
than an exception.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from funnel.orchestration import resolve_link
from funnel.orchestration.resolve_link import (
    ApplyLink,
    page_confirms,
    resolve_apply_url,
    resolve_prompt,
)

if TYPE_CHECKING:
    import pytest


@dataclass
class _Posting:
    title: str = "Senior AWS Developer"
    company: str = "RESOURCESOFT, INC."
    location: str | None = "US"


def _agent(url: str | None, reasoning: str = "reason") -> Agent[None, ApplyLink]:
    return Agent(
        TestModel(custom_output_args={"url": url, "reasoning": reasoning}),
        output_type=ApplyLink,
    )


def test_a_page_naming_the_role_confirms() -> None:
    page = "Careers\nSenior AWS Developer\nApply now\nWe are hiring."
    assert page_confirms(page, "Senior AWS Developer") is True


def test_confirmation_survives_the_ways_a_page_restates_a_title() -> None:
    """Both sides fold to letters and digits, so punctuation and spacing cannot break it.

    The page goes through `strip_html` first, exactly as `verify_apply_url` does it — entities
    and tags are that function's job, and folding raw markup would leave `nbsp` sitting in the
    middle of the title.
    """
    from funnel.adapters.util import strip_html

    markup = "<h1>Senior&nbsp;AWS  Developer</h1><p>Apply today</p>"
    assert page_confirms(strip_html(markup), "Senior AWS Developer") is True
    assert page_confirms("Senior AWS Developer (f/m/d)", "Senior AWS Developer") is True
    assert page_confirms("Senior AWS Developer — Remote", "Senior AWS Developer") is True


def test_a_different_role_does_not_confirm() -> None:
    """The whole title must appear, not some of its words.

    A loose token-overlap test confirmed "(Senior) DevOps Engineer (f/m/d)" against "Account
    Manager (f/m/d)" when `ats.board_confirms` tried it — both end in the same gender marker.
    """
    page = "Careers\nAccount Manager (f/m/d)\nSenior something else entirely"
    assert page_confirms(page, "Senior AWS Developer") is False


def test_an_empty_title_never_confirms() -> None:
    """Otherwise the empty string is a substring of every page and confirms everything."""
    assert page_confirms("any page at all", "") is False


def test_a_verified_url_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok(_url: str, _title: str) -> bool:
        return True

    monkeypatch.setattr(resolve_link, "verify_apply_url", _ok)
    result = asyncio.run(resolve_apply_url(_Posting(), agent=_agent("https://acme.example/jobs/1")))
    assert result.url == "https://acme.example/jobs/1"
    assert result.searched is True


def test_an_unverified_url_is_a_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proposed URL is a claim, not a fact — the model invents plausible careers URLs.

    The failure is silent, which is why this is the one behaviour worth pinning: a wrong link
    in the admin costs the human a click and their trust in the column.
    """

    async def _no(_url: str, _title: str) -> bool:
        return False

    monkeypatch.setattr(resolve_link, "verify_apply_url", _no)
    result = asyncio.run(
        resolve_apply_url(_Posting(), agent=_agent("https://acme.example/invented"))
    )
    assert result.url is None
    assert result.searched is True, "a rejected proposal is still a completed search"
    assert "unverified" in result.reasoning
    assert "https://acme.example/invented" in result.reasoning, "show what was rejected"


def test_no_proposal_is_a_completed_search() -> None:
    """Returning null is a correct answer: a staffing agency often posts nowhere else.

    `searched` stays True, so the caller records the miss and never pays for this search again.
    """
    result = asyncio.run(resolve_apply_url(_Posting(), agent=_agent(None, "not found")))
    assert result.url is None
    assert result.searched is True
    assert result.reasoning == "not found"


def test_a_failing_search_is_not_a_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (2026-08-05, first live run): a 400 from the provider retired a real posting.

    The caller writes a miss down permanently, so "we searched and found nothing" and "we could
    not search" must not look alike to it. One dead search must also not take the batch down.
    """

    class _Boom:
        async def run(self, _prompt: str) -> Any:
            raise RuntimeError("upstream is down")

    result = asyncio.run(resolve_apply_url(_Posting(), agent=_Boom()))  # type: ignore[arg-type]
    assert result.url is None
    assert result.searched is False, "an errored attempt must be retried, not written off"
    assert "RuntimeError" in result.reasoning


def test_a_blocked_url_is_never_accepted() -> None:
    """Resolving one dead end to another is worse than useless: it looks resolved."""
    assert asyncio.run(resolve_link.verify_apply_url("https://www.adzuna.com/details/1", "X")) is (
        False
    )


def test_the_prompt_carries_company_title_and_location() -> None:
    prompt = resolve_prompt(_Posting())
    assert "RESOURCESOFT, INC." in prompt
    assert "Senior AWS Developer" in prompt
    assert "US" in prompt
