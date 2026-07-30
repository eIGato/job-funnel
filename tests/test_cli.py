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
