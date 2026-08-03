"""The command wiring itself, with the work stubbed out.

`run-funnel` is what the systemd timer calls, so a break here is a break of the whole
scheduled pipeline — and it is the one path that no per-command test exercises.
"""

from __future__ import annotations

import inspect
from typing import Any

import typer
from typer.testing import CliRunner

from funnel import cli

runner = CliRunner()


def test_run_funnel_calls_the_three_stages_in_order(monkeypatch: Any) -> None:
    calls: list[str] = []
    for stage in ("ingest", "match", "draft"):
        monkeypatch.setattr(cli, stage, lambda *_a, _s=stage, **_kw: calls.append(_s))

    result = runner.invoke(cli.app, ["run-funnel"])

    assert result.exit_code == 0, result.output
    assert calls == ["ingest", "match", "draft"]


def test_run_funnel_hands_draft_a_real_limit(monkeypatch: Any) -> None:
    """Regression: `ctx.invoke(draft)` used to leave `limit` as Typer's OptionInfo sentinel.

    Typer substitutes the value inside an OptionInfo only while parsing a command line; calling
    the function from another command skips that, and the sentinel travelled all the way into
    SQLAlchemy's `.limit()`, killing every timer run after `match`.
    """
    seen: list[object] = []
    # The stand-in must carry the *real* command's default, or it quietly supplies a sane None
    # of its own and the test passes against the very bug it exists to catch.
    sentinel = inspect.signature(cli.draft).parameters["limit"].default
    assert isinstance(sentinel, typer.models.OptionInfo), "the trap this test guards is gone"

    monkeypatch.setattr(cli, "ingest", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli, "match", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli, "draft", lambda limit=sentinel, **_kw: seen.append(limit))

    result = runner.invoke(cli.app, ["run-funnel"])

    assert result.exit_code == 0, result.output
    assert seen == [None]
    assert not isinstance(seen[0], typer.models.OptionInfo)


def test_run_funnel_hands_draft_a_real_screen_flag(monkeypatch: Any) -> None:
    """Same OptionInfo trap as `limit`, on the flag that decides whether the screen runs.

    A leaked sentinel here is truthy, so the screen would appear to work while ignoring
    `settings.draft_screen` entirely.
    """
    seen: list[object] = []
    sentinel = inspect.signature(cli.draft).parameters["screen"].default
    assert isinstance(sentinel, typer.models.OptionInfo), "the trap this test guards is gone"

    monkeypatch.setattr(cli, "ingest", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli, "match", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli, "draft", lambda screen=sentinel, **_kw: seen.append(screen))

    result = runner.invoke(cli.app, ["run-funnel"])

    assert result.exit_code == 0, result.output
    assert seen == [None]
    assert not isinstance(seen[0], typer.models.OptionInfo)


def _compiled_shortlist(top_n: int = 25, floor: float = 90.0, remote_bonus: float = 0.02) -> str:
    """The shortlist query as PostgreSQL sees it — no database needed to read it."""
    from sqlalchemy.dialects import postgresql

    return str(
        cli.shortlist_select(top_n=top_n, floor=floor, remote_bonus=remote_bonus).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_shortlist_excludes_decided_roles_before_the_limit() -> None:
    """Regression (2026-08-03): `draft` had gone permanently silent.

    The exclusion used to run in Python over an already-limited result set, so a decided
    posting kept its rank and its slot forever. Once all 25 slots held a decided row the
    command drafted nothing, on every run, for good — 2853 postings had produced 49 shortlist
    entries. The NOT EXISTS must sit in the WHERE, ahead of the LIMIT, so deciding a posting
    frees its slot for the next one.
    """
    sql = _compiled_shortlist()
    where, _, after = sql.partition("LIMIT")
    assert "NOT (EXISTS" in where, "decided roles must be excluded in the WHERE clause"
    assert "applications" in where, "the exclusion must consult the applications table"
    assert "EXISTS" not in after, "the exclusion must not trail the LIMIT"


def test_shortlist_keeps_a_role_whose_only_application_is_shortlisted() -> None:
    """`shortlisted` means nothing was written yet — the role is still owed a letter."""
    assert "status != 'SHORTLISTED'" in _compiled_shortlist()


def test_shortlist_matches_a_role_case_and_whitespace_insensitively() -> None:
    """One role, however many rows carry it: ' Acme ' and 'acme' are the same company."""
    sql = _compiled_shortlist()
    assert sql.count("lower(trim(") >= 4, "company and title fold on both sides of the join"


def test_shortlist_honours_the_percentile_floor_and_size() -> None:
    sql = _compiled_shortlist(top_n=7, floor=95.0)
    assert "match_percentile >= 95.0" in sql
    assert "LIMIT 7" in sql


def test_shortlist_orders_on_score_not_on_a_remote_partition() -> None:
    """Regression (2026-08-03): `ORDER BY is_remote DESC` was a partition, not a preference.

    Every remote row outranked every on-site one whatever the scores, and with 893 remote rows
    the best-matching posting in the database sat at rank 894 while a 3D artist made the top
    25. Remote is a bonus on the score now, so the two pools interleave on merit.
    """
    sql = _compiled_shortlist()
    order_by = sql.partition("ORDER BY")[2]
    assert "is_remote DESC" not in order_by, "remote must not be a sort key of its own"
    assert "match_score" in order_by and "CASE WHEN" in order_by


def test_remote_bonus_reaches_the_ordering() -> None:
    assert "0.05" in _compiled_shortlist(remote_bonus=0.05).partition("ORDER BY")[2]


def test_a_zero_remote_bonus_sorts_on_merit_alone() -> None:
    """The escape hatch: 0 means the shortlist ignores remoteness entirely."""
    order_by = _compiled_shortlist(remote_bonus=0.0).partition("ORDER BY")[2]
    assert "is_remote" not in order_by or "0.0" in order_by
