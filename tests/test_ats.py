"""Phase 3.5 D. Slug discovery is what keeps the ATS set self-growing *and* bounded.

The dangerous failure is a false positive: a junk slug is polled on every run forever, so the
"must not match" cases matter more here than the hits.
"""

from __future__ import annotations

import pytest

from funnel.adapters import registry
from funnel.adapters.ats import discover_slugs
from funnel.models import AtsProvider
from funnel.schemas import NormalizedJob


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
    for name in ("greenhouse", "lever", "ashby", "recruitee", "smartrecruiters"):
        assert name in known, f"{name} adapter did not register"


def test_every_provider_has_a_url_pattern() -> None:
    """`discover_slugs` indexes `_PATTERNS` by provider — a missing entry is a KeyError.

    Adding a vendor to the enum without a pattern breaks URL-based discovery for every
    provider at once, because the callers loop over the whole enum.
    """
    from funnel.adapters.ats import _PATTERNS

    for provider in AtsProvider:
        assert provider in _PATTERNS, f"{provider} has no discovery pattern"
        assert discover_slugs(provider, "") == set()


@pytest.mark.parametrize(
    ("provider", "text", "expected"),
    [
        (AtsProvider.RECRUITEE, "https://amperecloud.recruitee.com/o/backend", {"amperecloud"}),
        (AtsProvider.SMARTRECRUITERS, "https://jobs.smartrecruiters.com/Visa/744", {"visa"}),
    ],
)
def test_discover_the_newer_vendors(provider: AtsProvider, text: str, expected: set[str]) -> None:
    assert discover_slugs(provider, text) == expected


@pytest.mark.parametrize(
    ("company", "expected"),
    [
        ("EUROCERT sp. z o.o.", ["eurocert"]),
        ("Amperecloud GmbH", ["amperecloud"]),
        ("Rose International", ["rose"]),
        ("Client Server", ["clientserver", "client-server", "client"]),
        ("Fundacja Szkoła w Chmurze", ["fundacjaszkoawchmurze", "fundacja-szkoa-w-chmurze"]),
        ("ITDS Polska Sp. z o.o.", ["itdspolska", "itds-polska", "itds"]),
        ("   ", []),
        ("Sp. z o.o.", []),
    ],
)
def test_slug_candidates(company: str, expected: list[str]) -> None:
    """The legal form is never part of a slug, and a name yields more than one plausible one."""
    from funnel.adapters.ats import slug_candidates

    assert slug_candidates(company)[: len(expected)] == expected


def test_slug_candidates_are_capped() -> None:
    """Each candidate is a request per provider — three is already speculative."""
    from funnel.adapters.ats import slug_candidates

    assert len(slug_candidates("One Two Three Four Five Six")) <= 3


def _posting(company: str, title: str) -> NormalizedJob:
    return NormalizedJob(url="https://example.com/1", company=company, title=title)


def test_board_confirms_on_the_company_name() -> None:
    from funnel.adapters.ats import board_confirms

    board = [_posting("Amperecloud GmbH", "Praktikant*in Brand & Product Marketing")]
    assert board_confirms("Amperecloud GmbH", "amperecloud", ["Software Engineer"], board) is True


def test_board_confirms_on_a_posting_we_already_hold() -> None:
    """Greenhouse never names the company, so the title is the only handle on those boards."""
    from funnel.adapters.ats import board_confirms

    board = [_posting("launchdarkly", "Backend Engineer, Flag Delivery")]
    titles = ["Backend Engineer, Flag Delivery"]
    assert board_confirms("LaunchDarkly", "launchdarkly", titles, board) is True


def test_a_slug_that_merely_resolves_is_not_a_confirmation() -> None:
    """Regression (2026-08-03): `ashby/clera` returns 301 postings and names nobody.

    "The request returned 200" is the failure mode this guards — a guessed slug that lands on
    somebody else's board looks exactly like a hit.
    """
    from funnel.adapters.ats import board_confirms

    someone_else = [
        _posting("clera", "Content Marketing Manager"),
        _posting("clera", "Head of Growth"),
    ]
    assert board_confirms("Clera", "clera", ["Backend Engineer"], someone_else) is False


def test_confirmation_does_not_fall_for_a_shared_gender_marker() -> None:
    """A loose token-overlap test matched "Account Manager (f/m/d)" to a DevOps role."""
    from funnel.adapters.ats import board_confirms

    board = [_posting("adjoe", "Account Manager (f/m/d)")]
    assert board_confirms("adjoe", "adjoe", ["(Senior) DevOps Engineer (f/m/d)"], board) is False


def test_a_company_name_equal_to_the_guessed_slug_is_not_evidence() -> None:
    """Greenhouse and Lever expose no company name and fall back to `company=slug`.

    Since the slug was derived from the company name, trusting that echo would confirm every
    board that returns anything — which is exactly how a wrong apply link gets written.
    """
    from funnel.adapters.ats import board_confirms

    echo = [_posting("clera", "Content Marketing Manager")]
    assert board_confirms("Clera", "clera", ["Backend Engineer"], echo) is False


def _compiled_probe_candidates(scan: int = 300) -> str:
    """The candidate query as PostgreSQL sees it — no database needed to read it."""
    from sqlalchemy.dialects import postgresql

    from funnel.adapters.ats import probe_candidates

    return str(
        probe_candidates(scan).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_companies_with_no_apply_route_are_probed_first() -> None:
    """For a dead-end posting the employer's own board is the only route to applying at all.

    An ordinary posting already has a working link and the probe merely improves it; a
    geo-blocked or paywalled one has nothing else in the funnel that can help it
    (`matching/apply_route.py`), so it gets the run's limited requests first.
    """
    sql = _compiled_probe_candidates()
    order = sql[sql.rindex("ORDER BY") :]
    assert "apply_blocked DESC" in order
    assert order.index("apply_blocked DESC") < order.index("match_percentile DESC")


def test_the_probe_window_is_still_bounded_by_rank() -> None:
    """The two orderings are not one: blocked-first must reorder the window, not replace it.

    A single `ORDER BY apply_blocked DESC, match_percentile DESC` over the whole table would
    hand the run's whole budget to dead-end postings at rank 900 while a live company at rank 2
    went unprobed. The rank cut has to happen in the subquery, before the reordering.
    """
    sql = _compiled_probe_candidates(scan=300)
    before_cut, _, after_cut = sql.partition("LIMIT 300")
    window_order = before_cut[before_cut.rindex("ORDER BY") :]
    assert "match_percentile DESC" in window_order, "the window is cut by rank"
    assert "apply_blocked" not in window_order, "the apply route must not decide who is in it"
    assert "apply_blocked DESC" in after_cut, "and it orders what survived the cut"


def test_the_probe_window_advances_between_runs() -> None:
    """Regression: the same shape of bug that made `funnel draft` a silent no-op.

    Taking the best-ranked `limit` companies and skipping the already-tried ones afterwards
    reads correctly and does the opposite — a tried company keeps its rank, keeps its slot, and
    the next run probes nothing. The exclusion has to happen before the limit.
    """
    from funnel.adapters.ats import _BY_NAME, _companies_worth_probing

    class _Session:
        def __init__(self, rows: list[tuple[str, str]]) -> None:
            self._rows = rows

        def execute(self, _statement: object) -> _Session:
            return self

        def all(self) -> list[tuple[str, str]]:
            return self._rows

    rows = [(f"Company {n}", f"Backend Engineer {n}") for n in range(10)]
    session = _Session(rows)

    first = _companies_worth_probing(session, 3, set())  # type: ignore[arg-type]
    assert [c for c, _ in first] == ["Company 0", "Company 1", "Company 2"]

    tried = {f"{_BY_NAME}{c}" for c, _ in first}
    second = _companies_worth_probing(session, 3, tried)  # type: ignore[arg-type]
    assert [c for c, _ in second] == ["Company 3", "Company 4", "Company 5"], (
        "the window must move on, not return the same companies for the caller to skip"
    )


def test_a_remembered_miss_is_unique_per_company() -> None:
    """Regression: two Cyrillic company names collided and killed the ingest transaction.

    The miss key was the folded name, and folding transliterates to ASCII — which for "Реактив"
    and "Топ Селекшн" alike leaves nothing. Both wanted the slug `!miss:`, the second violated
    uq_ats_boards_provider_slug, and the IntegrityError took the whole run down.
    """
    from funnel.adapters.ats import _MISS_PREFIX, _miss_slug

    names = ["Реактив", "Топ Селекшн", "РОСГОССТРАХ", "Айдеко"]
    slugs = [_miss_slug(n) for n in names]
    assert len(set(slugs)) == len(names), "every company needs its own miss key"
    assert all(s.startswith(_MISS_PREFIX) and len(s) > len(_MISS_PREFIX) for s in slugs)


def test_a_miss_key_is_stable_across_runs() -> None:
    """It is how the next run knows not to spend the same requests again."""
    from funnel.adapters.ats import _miss_slug

    assert _miss_slug("Реактив") == _miss_slug(" реактив ")
