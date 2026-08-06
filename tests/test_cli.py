"""The command wiring itself, with the work stubbed out.

`run-funnel` is what the systemd timer calls, so a break here is a break of the whole
scheduled pipeline — and it is the one path that no per-command test exercises.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
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
    assert "is_remote DESC" not in sql, "remote must not be a sort key of its own"
    assert "match_score + CASE WHEN jobs.is_remote THEN 0.02 ELSE 0.0 END" in sql


def test_remote_bonus_reaches_the_ordering() -> None:
    assert "THEN 0.05 ELSE" in _compiled_shortlist(remote_bonus=0.05)


def test_a_zero_remote_bonus_sorts_on_merit_alone() -> None:
    """The escape hatch: 0 means the shortlist ignores remoteness entirely."""
    assert "THEN 0.0 ELSE 0.0" in _compiled_shortlist(remote_bonus=0.0)


def test_shortlist_collapses_twins_before_the_limit() -> None:
    """Regression (2026-08-03): duplicate rows of one role ate the shortlist.

    The shortlist opened with EuroCert five times, Rose International four and STAFIDE twice —
    twelve of 25 slots for six roles. Each row is a genuine posting with its own id (a board
    lists one role once per city), so ingest cannot merge them; the shortlist has to.
    """
    sql = _compiled_shortlist()
    before_limit, _, after = sql.partition("LIMIT")
    assert "row_number() OVER" in before_limit
    assert "PARTITION BY lower(trim(" in before_limit
    assert "twin_rank = 1" in before_limit
    assert "row_number" not in after


def _window(sql: str, label: str) -> str:
    """The OVER (...) clause of the window function labelled `label`."""
    before = sql.partition(f") AS {label}")[0]
    return before.rpartition("row_number() OVER (")[2]


def test_shortlist_keeps_the_best_scoring_row_of_a_role() -> None:
    """Which twin represents the role matters: it also picks the best per-city variant."""
    window = _window(_compiled_shortlist(), "twin_rank")
    assert "PARTITION BY lower(trim(" in window
    assert "match_score" in window.partition("ORDER BY")[2]
    assert window.rstrip().endswith("DESC"), f"twins must be ranked best-first, got {window!r}"


def test_run_funnel_hands_draft_a_real_job_id(monkeypatch: Any) -> None:
    """The same OptionInfo trap as `limit`, on the option added for hand-fed postings.

    A leaked sentinel is not None, so `draft` would take the single-posting branch and try to
    `session.get(Job, <OptionInfo>)` on every scheduled run.
    """
    seen: list[object] = []
    sentinel = inspect.signature(cli.draft).parameters["job_id"].default
    assert isinstance(sentinel, typer.models.OptionInfo), "the trap this test guards is gone"

    monkeypatch.setattr(cli, "ingest", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli, "match", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli, "draft", lambda job_id=sentinel, **_kw: seen.append(job_id))

    result = runner.invoke(cli.app, ["run-funnel"])

    assert result.exit_code == 0, result.output
    assert seen == [None]
    assert not isinstance(seen[0], typer.models.OptionInfo)


def test_draft_by_job_id_never_touches_the_shortlist(monkeypatch: Any) -> None:
    """`--job` is the hand-fed path: it works on the named row, whatever its rank or status."""
    monkeypatch.setattr(
        cli, "shortlist_select", lambda **_kw: pytest.fail("--job must not query the shortlist")
    )
    monkeypatch.setattr(cli, "_screen_and_draft", lambda *_a, **_kw: "drafted")

    result = runner.invoke(cli.app, ["draft", "--job", "999999"])

    # No such row in an empty test database, so the command reports that and exits non-zero —
    # what matters is that it got there without building the shortlist query.
    assert "999999" in result.output or result.exit_code != 0


def test_shortlist_caps_how_many_slots_one_company_holds() -> None:
    """Regression (2026-08-03): the first ATS board took 14 of 25 slots.

    An ATS board arrives as a whole careers page, not as a posting, so one employer's twelfth
    role was outranking every other company's best — frontend, engineering manager and data
    scientist roles among them, each costing a screening call.
    """
    sql = _compiled_shortlist()
    before_limit, _, after = sql.partition("LIMIT")
    window = _window(sql, "company_rank")
    assert "PARTITION BY" in window and ".company" in window
    assert "rank_score DESC" in window, "a company's own roles rank by score"
    assert "company_rank <= 3" in before_limit
    assert "company_rank" not in after


def test_the_shortlist_skips_postings_with_no_apply_route() -> None:
    """A link the human cannot apply through must not hold a slot (2026-08-05).

    Adzuna's US and CA sites answer 403 from where the human lives and RemoteOK's apply button
    is behind its paid tier — 131 of the ~640 rows above the floor, a fifth of the shortlist
    spent on letters that could not be sent. Excluded in the WHERE like every other selection
    rule, so the slot goes to the next posting instead of being lost.
    """
    sql = _compiled_shortlist()
    where, _, _ = sql.partition("LIMIT")
    assert "jobs.apply_blocked IS false" in where


def test_the_shortlist_skips_postings_with_nothing_to_write_from() -> None:
    """An empty or one-line body must not hold a slot (2026-08-06).

    Gmail alerts carry a subject line and a link, no posting: 124 of the 554 rows above the
    floor had an empty body and 71 more were a single short line, so 36% of every shortlist
    bought a screening call and a letter written from a title. The admin's per-row button is
    the path for these now — the human pastes the real description in and draws the letter from
    there. In the WHERE like every other selection rule, so the slot goes to the next posting.
    """
    sql = _compiled_shortlist()
    where, _, after = sql.partition("LIMIT")
    assert "length(trim(jobs.description)) >= 300" in where
    assert "length(trim(" not in after


def test_the_body_floor_does_not_reject_a_one_paragraph_teaser() -> None:
    """A length floor, not a newline test: Adzuna's 500-character teaser is one paragraph.

    Salary, requirements and stack, all real — the literal reading of "one line" would have
    dropped 68 of those along with the junk.
    """
    assert cli.MIN_DRAFTABLE_BODY < 426, "Adzuna's shortest teaser is 426 characters"


def test_the_company_cap_is_applied_after_twins_collapse() -> None:
    """Order matters: five rows of one role would otherwise spend the whole allowance."""
    sql = _compiled_shortlist()
    twin_at = sql.index("twin_rank = 1")
    cap_at = sql.index("company_rank <= 3")
    assert twin_at < cap_at, "twins must collapse before the per-company cap counts roles"


def test_the_company_cap_is_configurable() -> None:
    from sqlalchemy.dialects import postgresql

    sql = str(
        cli.shortlist_select(top_n=25, floor=90.0, remote_bonus=0.02, per_company=1).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "company_rank <= 1" in sql
