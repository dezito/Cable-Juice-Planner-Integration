"""The what-if data is kept for as many months as the setup keeps the charging history, so the comparison can reach as far back."""
from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.cable_juice_planner.engine.economy import ECONOMY_PRICE_DAYS, economy_keep_days

from .conftest import OBJECT_PREFIX
from .test_economy import _engine

DAY = 86400.0


def test_the_days_kept_follow_the_months_of_history_of_the_setup_and_never_fall_below_400() -> None:
    def keep(months):
        return economy_keep_days({"database": {"charging_history_db_data_to_save": months}})

    assert keep(12) == 12 * 31 + 31 == 403  # the default
    assert keep(24) == 775 and keep(60) == 1891
    assert keep(1) == keep(3) == ECONOMY_PRICE_DAYS  # fewer months never shorten what the comparisons keep
    for broken in (None, "x", float("nan"), -5, 0):
        assert keep(broken) == ECONOMY_PRICE_DAYS
    assert economy_keep_days({}) == ECONOMY_PRICE_DAYS and economy_keep_days({"database": {}}) == ECONOMY_PRICE_DAYS


def test_the_longest_period_and_the_number_of_drives_kept_follow_it() -> None:
    engine, snap = _engine()
    assert engine.economy_keep_days() == 403 and engine.what_if_max_days() == 403 and engine.drive_history_limit() == 806
    engine.config["database"]["charging_history_db_data_to_save"] = 24
    assert engine.what_if_max_days() == 775 and engine.drive_history_limit() == 1550
    # the period asked for is limited to what is kept
    long = snapshot_days(engine, 5000)
    assert long == 775


@pytest.mark.parametrize("months", [1, 12, 24, 60])
def test_the_same_month_a_year_ago_is_always_in_reach(months: int) -> None:
    """The months set plus one month: from any day of a month the 1st of the same month `months` months ago is kept."""
    import datetime  # noqa: PLC0415

    keep = economy_keep_days({"database": {"charging_history_db_data_to_save": months}})
    first = datetime.date(2024, 1, 1)
    for offset in range(0, 3 * 366, 3):  # every third day of three years, so every day of a month and the leap year are met
        day = first + datetime.timedelta(days=offset)
        total = day.year * 12 + day.month - 1 - months
        month_start = datetime.date(total // 12, total % 12 + 1, 1)
        assert month_start >= day - datetime.timedelta(days=keep), (day, month_start)


def snapshot_days(engine, requested: float) -> int:
    import datetime  # noqa: PLC0415

    from .test_engine import DAY as TODAY, snapshot  # noqa: PLC0415

    snap = snapshot(datetime.datetime.combine(TODAY, datetime.time(12, 0)), settings={"what_if_days": requested})
    result = engine.run_job(snap, engine.what_if_period)
    start, end = result.value
    return round((end - start) / DAY)


@pytest.mark.parametrize(("months", "kept"), [(12, False), (24, True)])
def test_prices_from_the_recorder_are_kept_for_as_long_as_the_history_is(months: int, kept: bool) -> None:
    engine, snap = _engine()
    engine.config["database"]["charging_history_db_data_to_save"] = months
    now = engine.run_job(snap, engine.what_if_period).value[1]
    hour = (now // 3600) * 3600 - 500 * DAY  # 500 days back: older than the 400 days of the default, inside 24 months
    result = engine.run_job(snap, engine.economy_update, {"prices": {hour: 1.25}, "windows": [], "odometer_months": {}})
    assert result.error is None, result.error
    assert (hour in {float(ts) for ts in engine.economy_db.get("prices_history", {})}) is kept


@pytest.mark.parametrize(("months", "expected"), [(12, 806), (24, 900)])
def test_the_drives_are_kept_up_to_two_a_day_for_the_days_kept(months: int, expected: int) -> None:
    engine, snap = _engine()
    engine.config["database"]["charging_history_db_data_to_save"] = months
    drives = [{"start": 1_700_000_000.0 + i * 7200, "end": 1_700_000_000.0 + i * 7200 + 3600, "km": 10.0, "used": 5.0, "temp": None} for i in range(900)]
    result = engine.run_job(snap, engine._add_drives, drives)
    assert result.error is None, result.error
    assert len(engine.analysis_db["drives"]) == expected
    # the newest ones are the ones that are kept
    assert engine.analysis_db["drives"][-1]["start"] == drives[-1]["start"]


async def test_the_period_number_reaches_as_far_back_as_the_history_is_kept(hass: HomeAssistant, loaded_entry) -> None:
    state = hass.states.get(f"number.{OBJECT_PREFIX}_what_if_days")
    assert state.attributes["max"] == 403.0  # the default 12 months kept plus a month
