"""The admin's local-time layer: storage stays UTC, the human reads and types his own clock.

Hermetic — the zone is monkeypatched rather than read from `.env`, so these assert the
conversion and not whichever machine runs them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from wtforms import Form

from funnel import admin as admin_module
from funnel.admin import LocalDateTimeField, LocalTimeView, _local_datetime

ZONE = ZoneInfo("Europe/Podgorica")  # +02:00 in summer, +01:00 in winter


@pytest.fixture(autouse=True)
def _fixed_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_module, "admin_zone", lambda: ZONE)


class _Form(Form):
    sent_at = LocalDateTimeField()


class _FormData(dict[str, str]):
    """The slice of the multidict protocol wtforms actually calls."""

    def getlist(self, key: str) -> list[str]:
        return [self[key]] if key in self else []


def _typed(value: str) -> datetime | None:
    data: Any = _Form(formdata=_FormData({"sent_at": value})).sent_at.data
    return data


def test_a_stored_instant_renders_as_the_local_wall_clock() -> None:
    form = _Form(data={"sent_at": datetime(2026, 8, 6, 19, 0, tzinfo=UTC)})
    assert form.sent_at._value() == "2026-08-06 21:00:00"


def test_a_typed_wall_clock_is_stored_as_the_instant_it_names() -> None:
    """The bug in one assertion: 21:00 on the human's watch is 19:00 UTC, not 21:00 UTC."""
    assert _typed("2026-08-06 21:00:00") == datetime(2026, 8, 6, 19, 0, tzinfo=UTC)


def test_the_round_trip_is_lossless() -> None:
    stored = datetime(2026, 8, 6, 19, 0, tzinfo=UTC)
    rendered = _Form(data={"sent_at": stored}).sent_at._value()
    assert _typed(rendered) == stored


def test_the_offset_follows_the_zone_not_a_constant() -> None:
    """Winter is +01:00 there. A hardcoded +02:00 would be wrong for half the year."""
    assert _typed("2026-12-06 21:00:00") == datetime(2026, 12, 6, 20, 0, tzinfo=UTC)


def test_a_naive_stored_value_is_read_as_utc() -> None:
    """As stored: every writer in the pipeline writes UTC, tzinfo or not."""
    form = _Form(data={"sent_at": datetime(2026, 8, 6, 19, 0)})
    assert form.sent_at._value() == "2026-08-06 21:00:00"


def test_the_list_view_spells_out_the_zone() -> None:
    """A bare "21:00" beside a UTC one elsewhere is how this bug started."""
    assert _local_datetime(datetime(2026, 8, 6, 19, 0, tzinfo=UTC)) == "2026-08-06 21:00 CEST"
    assert _local_datetime(datetime(2026, 12, 6, 19, 0, tzinfo=UTC)) == "2026-12-06 20:00 CET"


def test_every_registered_view_speaks_local_time() -> None:
    """One view left out would put two different clocks in front of the same human."""
    assert admin_module.admin.views
    for view in admin_module.admin.views:
        assert isinstance(view, LocalTimeView), f"{type(view).__name__} is not a LocalTimeView"
