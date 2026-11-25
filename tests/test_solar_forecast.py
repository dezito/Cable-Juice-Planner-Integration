"""Hourly solar forecast rows from the energy platform of Forecast.Solar and Helios Forecast (utils/solar_forecast.py).

The module and this test are the same files in BattMind and Cable Juice Planner: change both together. The module is
loaded from its file, so this file needs no integration name.
"""
from __future__ import annotations

import asyncio
import datetime
import importlib.util
import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockModule, mock_integration, mock_platform


def _load_module():
    [path] = sorted((Path(__file__).resolve().parents[1] / "custom_components").rglob("solar_forecast.py"))
    spec = importlib.util.spec_from_file_location("solar_forecast_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


solar_forecast = _load_module()
rows_from_wh_hours = solar_forecast.rows_from_wh_hours

CPH = ZoneInfo("Europe/Copenhagen")
KOLKATA = ZoneInfo("Asia/Kolkata")
UTC = datetime.UTC


def utc(day: datetime.date, hour: int, minute: int = 0) -> str:
    return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=UTC).isoformat()


def local(day: datetime.date, hour: int, minute: int = 0, tz=CPH) -> str:
    return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=tz).isoformat()


def by_hour(rows: list[dict]) -> dict[datetime.datetime, float]:
    """Naive local hour -> kWh, as the planner reads the rows (it drops the time zone of period_start)."""
    result = {}
    for row in rows:
        assert row["period_start"].tzinfo is not None
        hour = row["period_start"].replace(tzinfo=None)
        assert hour not in result, "one row per local hour"
        result[hour] = row["pv_estimate"]
    return result


def hour(day: datetime.date, value: int) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(value))


SUMMER = datetime.date(2026, 6, 10)
NEXT = SUMMER + datetime.timedelta(days=1)


def test_the_key_semantics_are_known_only_for_the_two_integrations() -> None:
    assert solar_forecast.KEY_SEMANTICS == {"forecast_solar": "end", "helios_forecast": "start"}
    with pytest.raises(ValueError):
        rows_from_wh_hours([{utc(SUMMER, 10): 1.0}], "middle", CPH)


def test_helios_keys_start_their_utc_hour() -> None:
    wh_hours = {utc(SUMMER, 10): 1500.0, utc(SUMMER, 11): 2500, utc(SUMMER, 12): 500.0}
    rows = rows_from_wh_hours([wh_hours], "start", CPH)
    # CEST is UTC+2: the UTC hour 10 is the local hour 12, and kWh in an hour is the average kW
    assert by_hour(rows) == {hour(SUMMER, 12): 1.5, hour(SUMMER, 13): 2.5, hour(SUMMER, 14): 0.5}
    assert rows[0]["period_start"] == datetime.datetime.combine(SUMMER, datetime.time(12), tzinfo=CPH)
    assert [row["period_start"] for row in rows] == sorted(row["period_start"] for row in rows)


FORECAST_SOLAR_DAYS = {
    # the sunrise, the whole hours and the sunset; Home Assistant drops the 0 at midnight
    local(SUMMER, 4, 30): 0,
    local(SUMMER, 5): 100,
    local(SUMMER, 6): 600,
    local(SUMMER, 7): 1200,
    **{local(SUMMER, hour_): 50 * hour_ for hour_ in range(8, 22)},
    local(SUMMER, 21, 45): 90,
    local(NEXT, 4, 30): 0,
    local(NEXT, 5): 120,
    local(NEXT, 6): 700,
}


def test_forecast_solar_keys_end_their_period() -> None:
    found = by_hour(rows_from_wh_hours([FORECAST_SOLAR_DAYS], "end", CPH))
    # the sunrise has no known start: its hour is covered only from 04:30 and gets no row
    assert hour(SUMMER, 4) not in found
    # the energy lands in the hour BEFORE its key (read as start keys, 05:00 would get the 100 Wh of the 05:00 key)
    assert found[hour(SUMMER, 5)] == 0.6
    assert found[hour(SUMMER, 6)] == 1.2
    assert found[hour(SUMMER, 7)] == 0.4
    assert found[hour(SUMMER, 20)] == 1.05
    # the sunset hour: 21:00-21:45 from the sunset key, 21:45-22:00 from the night (the next sunrise, 0)
    assert found[hour(SUMMER, 21)] == 0.09
    assert all(found[hour(SUMMER, 22) + datetime.timedelta(hours=offset)] == 0 for offset in range(6))
    # the next sunrise hour is covered by the night and the 05:00 key
    assert found[hour(NEXT, 4)] == 0.12
    assert found[hour(NEXT, 5)] == 0.7
    # nothing after the last key
    assert max(found) == hour(NEXT, 5)


def test_a_missing_end_key_leaves_its_hours_out() -> None:
    """Without the 12:00 key the 13:00 value covers (11:00, 13:00] in the keys, but only its own hour in fact: spreading
    it would halve two hours. A long period without energy (the night) is a real 0."""
    data = {local(SUMMER, 9): 0, local(SUMMER, 10): 1000, local(SUMMER, 11): 2000, local(SUMMER, 13): 3000, local(SUMMER, 14): 500}
    assert by_hour(rows_from_wh_hours([data], "end", CPH)) == {hour(SUMMER, 9): 1.0, hour(SUMMER, 10): 2.0, hour(SUMMER, 13): 0.5}
    night = {local(SUMMER, 20): 100, local(SUMMER, 21): 50, local(NEXT, 5): 0, local(NEXT, 6): 80}
    found = by_hour(rows_from_wh_hours([night], "end", CPH))
    assert found[hour(SUMMER, 20)] == 0.05 and found[hour(NEXT, 2)] == 0 and found[hour(NEXT, 5)] == 0.08


def test_keys_outside_the_window_and_extreme_keys_are_left_out() -> None:
    data = {
        "1970-01-01T00:00:00+00:00": 100,
        "0001-01-01T00:00:00+00:00": 100,
        "9999-12-31T23:00:00-12:00": 100,  # not a moment in UTC
        utc(SUMMER, 10): 1000,
        utc(SUMMER, 11): 2000,
        utc(SUMMER, 12): 3000,
    }
    window = (datetime.datetime.combine(SUMMER, datetime.time(0), tzinfo=UTC), datetime.datetime.combine(NEXT, datetime.time(0), tzinfo=UTC))
    assert by_hour(rows_from_wh_hours([data], "start", CPH, window)) == {hour(SUMMER, 12): 1.0, hour(SUMMER, 13): 2.0, hour(SUMMER, 14): 3.0}
    # without a window a key next to datetime.min can't be read with its margin: no rows, never an exception
    assert rows_from_wh_hours([{"0001-01-01T00:00:00+00:00": 1, "0001-01-01T01:00:00+00:00": 1}], "start", CPH) == []


def test_forecast_solar_read_as_start_keys_would_put_the_energy_an_hour_late() -> None:
    """The distinction matters: the same data read with the other semantics moves every value one hour."""
    data = {local(SUMMER, 9): 0, local(SUMMER, 10): 1000, local(SUMMER, 11): 3000, local(SUMMER, 12): 0}
    end = by_hour(rows_from_wh_hours([data], "end", CPH))
    start = by_hour(rows_from_wh_hours([data], "start", CPH))
    assert end == {hour(SUMMER, 9): 1.0, hour(SUMMER, 10): 3.0, hour(SUMMER, 11): 0.0}
    assert start[hour(SUMMER, 10)] == 1.0 and start[hour(SUMMER, 11)] == 3.0


def test_the_last_period_of_a_day_that_ends_mid_hour_is_dropped() -> None:
    data = {local(SUMMER, 19): 0, local(SUMMER, 20): 400, local(SUMMER, 21): 200, local(SUMMER, 21, 30): 50}
    assert by_hour(rows_from_wh_hours([data], "end", CPH)) == {hour(SUMMER, 19): 0.4, hour(SUMMER, 20): 0.2}


@pytest.mark.parametrize("semantics", ["start", "end"])
def test_quarter_hours_are_summed_per_hour(semantics: str) -> None:
    keys = [utc(SUMMER, 10, minute) for minute in (0, 15, 30, 45)] + [utc(SUMMER, 11)]
    data = dict(zip(keys, [100, 200, 300, 400, 500]))
    found = by_hour(rows_from_wh_hours([data], semantics, CPH))
    if semantics == "start":
        # every key covers its own quarter: 10:00-11:15 UTC, so only the UTC hour 10 (local 12) is whole
        assert found == {hour(SUMMER, 12): 1.0}
    else:
        # the first key only starts the first quarter: (10:00, 11:00] UTC = 200 + 300 + 400 + 500
        assert found == {hour(SUMMER, 12): 1.4}


def test_the_clocks_going_back_sum_both_hours() -> None:
    day = datetime.date(2026, 10, 25)  # 03:00 CEST -> 02:00 CET at 01:00 UTC
    eve = day - datetime.timedelta(days=1)
    data = {utc(eve, 23): 100, utc(day, 0): 200, utc(day, 1): 300, utc(day, 2): 400, utc(day, 3): 500}
    rows = rows_from_wh_hours([data], "start", CPH)
    # 00:00 UTC is 02:00 CEST and 01:00 UTC is 02:00 CET: one local hour with both, never overwritten
    assert by_hour(rows) == {hour(day, 1): 0.1, hour(day, 2): 0.5, hour(day, 3): 0.4, hour(day, 4): 0.5}
    assert rows[1]["period_start"].utcoffset() == datetime.timedelta(hours=2)  # the first 02:00
    # end keys (Forecast.Solar): the periods ending 01:00 and 02:00 UTC are both 02:00 local
    assert by_hour(rows_from_wh_hours([data], "end", CPH)) == {hour(day, 1): 0.2, hour(day, 2): 0.7, hour(day, 3): 0.5}


def test_the_clocks_going_forward_leave_out_the_hour_that_does_not_exist() -> None:
    day = datetime.date(2026, 3, 29)  # 02:00 CET -> 03:00 CEST at 01:00 UTC
    data = {utc(day, 0): 100, utc(day, 1): 200, utc(day, 2): 300}
    assert by_hour(rows_from_wh_hours([data], "start", CPH)) == {hour(day, 1): 0.1, hour(day, 3): 0.2, hour(day, 4): 0.3}
    end = {utc(day, 0): 0, utc(day, 1): 100, utc(day, 2): 200}
    assert by_hour(rows_from_wh_hours([end], "end", CPH)) == {hour(day, 1): 0.1, hour(day, 3): 0.2}


def test_a_half_hour_time_zone_splits_the_utc_hours() -> None:
    """India is UTC+5:30: a local hour is half of two UTC hours, and the halves at the ends are no whole hour."""
    data = {utc(SUMMER, 4): 1000, utc(SUMMER, 5): 3000}
    found = by_hour(rows_from_wh_hours([data], "start", KOLKATA))
    assert found == {hour(SUMMER, 10): 2.0}


def test_values_that_are_no_energy_leave_their_hour_out() -> None:
    data = {
        utc(SUMMER, 8): 100,
        utc(SUMMER, 9): math.nan,
        utc(SUMMER, 10): -5,
        utc(SUMMER, 11): None,
        utc(SUMMER, 12): "200",
        utc(SUMMER, 13): True,
        utc(SUMMER, 14): math.inf,
        utc(SUMMER, 15): 300,
        "2026-06-10T16:00:00": 400,  # no time zone
        "not a time": 500,
        utc(SUMMER, 17): 0,
    }
    found = by_hour(rows_from_wh_hours([data], "start", CPH))
    assert found == {hour(SUMMER, 10): 0.1, hour(SUMMER, 17): 0.3, hour(SUMMER, 19): 0.0}


def test_an_unreadable_end_key_hides_every_period() -> None:
    """With end keys a period starts at the key before it: a key that can't be read leaves the periods unknown."""
    data = {local(SUMMER, 9): 0, local(SUMMER, 10): 100, "2026-06-10T11:00:00": 200, local(SUMMER, 12): 300}
    assert rows_from_wh_hours([data], "end", CPH) == []
    data = {local(SUMMER, 9): 0, local(SUMMER, 10): 100, local(SUMMER, 11): math.nan, local(SUMMER, 12): 300}
    assert by_hour(rows_from_wh_hours([data], "end", CPH)) == {hour(SUMMER, 9): 0.1, hour(SUMMER, 11): 0.3}


def test_two_keys_for_the_same_moment_leave_their_hour_out() -> None:
    data = {utc(SUMMER, 10): 100, local(SUMMER, 12): 999, utc(SUMMER, 11): 200}
    assert by_hour(rows_from_wh_hours([data], "start", CPH)) == {hour(SUMMER, 13): 0.2}


def test_a_gap_in_the_keys_is_not_covered() -> None:
    data = {utc(SUMMER, 10): 100, utc(SUMMER, 11): 200, utc(SUMMER, 14): 400}
    assert by_hour(rows_from_wh_hours([data], "start", CPH)) == {hour(SUMMER, 12): 0.1, hour(SUMMER, 13): 0.2, hour(SUMMER, 16): 0.4}


def test_only_the_hours_the_forecast_covers() -> None:
    """A free Forecast.Solar plan covers about two days of a seven day plan: no row (not 0) for the days after."""
    data = {utc(SUMMER + datetime.timedelta(days=day), hour_): 100 for day in range(2) for hour_ in range(24)}
    found = by_hour(rows_from_wh_hours([data], "start", CPH))
    assert len(found) == 48
    assert max(found) == hour(SUMMER + datetime.timedelta(days=2), 1)
    assert not any(key.date() >= SUMMER + datetime.timedelta(days=3) for key in found)


def test_two_entries_are_summed_only_where_both_cover_the_hour() -> None:
    """One config entry per roof plane: an hour that one plane does not cover would be half a forecast."""
    east = {local(SUMMER, hour_): 100 * hour_ for hour_ in range(6, 15)}
    west = {local(SUMMER, hour_): 10 * hour_ for hour_ in range(6, 12)}
    found = by_hour(rows_from_wh_hours([east, west], "end", CPH))
    assert found == {hour(SUMMER, hour_): round((100 + 10) * (hour_ + 1) / 1000, 4) for hour_ in range(6, 11)}


@pytest.mark.parametrize("data", [[], [{}], [None], [{utc(SUMMER, 10): 100}], [{utc(SUMMER, 10): 100}, {}]])
def test_no_data_gives_no_rows(data) -> None:
    assert rows_from_wh_hours(data, "start", CPH) == []
    assert rows_from_wh_hours(data, "end", CPH) == []


# ---------------------------------------------------------------------------------------------------------------- #
# Reading the energy platform
# ---------------------------------------------------------------------------------------------------------------- #

HELIOS = "helios_forecast"
FORECAST_SOLAR = "forecast_solar"
# the helper reads only the keys from a day back to 16 days ahead of now
SOON = datetime.datetime.now(CPH).date() + datetime.timedelta(days=1)


def local_hour_of(day: datetime.date, utc_hour: int) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(utc_hour), tzinfo=UTC).astimezone(CPH).replace(tzinfo=None)


def forecast_entry(hass: HomeAssistant, domain: str, sensor: str, state: ConfigEntryState = ConfigEntryState.LOADED) -> tuple[MockConfigEntry, str]:
    entry = MockConfigEntry(domain=domain, title=domain)
    entry.add_to_hass(hass)
    entry.mock_state(hass, state)
    entity = er.async_get(hass).async_get_or_create("sensor", domain, f"{entry.entry_id}_{sensor}", config_entry=entry, suggested_object_id=f"{domain}_{sensor}")
    return entry, entity.entity_id


def energy_platform(hass: HomeAssistant, domain: str, forecasts: dict) -> list[str]:
    """A fake energy platform that answers from forecasts (entry id -> answer, or an exception to raise)."""
    calls: list[str] = []

    async def async_get_solar_forecast(hass: HomeAssistant, config_entry_id: str):
        calls.append(config_entry_id)
        answer = forecasts.get(config_entry_id)
        if isinstance(answer, BaseException):
            raise answer
        if answer == "slow":
            await asyncio.sleep(10)
        return answer

    mock_integration(hass, MockModule(domain))
    mock_platform(hass, f"{domain}.energy", SimpleNamespace(async_get_solar_forecast=async_get_solar_forecast))
    return calls


async def test_reads_the_energy_platform_of_the_sensors_config_entry(hass: HomeAssistant) -> None:
    entry, entity_id = forecast_entry(hass, HELIOS, "power_now")
    # a key far in the past (or future) is left out: it would only make the normalising slow
    wh_hours = {utc(SOON, 10): 1500.0, utc(SOON, 11): 500.0, "1970-01-01T00:00:00+00:00": 9999.0}
    energy_platform(hass, HELIOS, {entry.entry_id: {"wh_hours": wh_hours}})
    rows, problem = await solar_forecast.async_solar_forecast_rows(hass, entity_id, CPH)
    assert problem is None
    assert by_hour(rows) == {local_hour_of(SOON, 10): 1.5, local_hour_of(SOON, 11): 0.5}
    # nothing in the window: no rows
    energy_platform(hass, HELIOS, {entry.entry_id: {"wh_hours": {utc(SUMMER, 10): 1.0, utc(SUMMER, 11): 1.0}}})
    rows, problem = await solar_forecast.async_solar_forecast_rows(hass, entity_id, CPH)
    assert rows == [] and problem


@pytest.mark.parametrize("answer", [None, {}, {"wh_hours": None}, {"wh_hours": {}}, RuntimeError("boom"), "slow"])
async def test_no_answer_gives_no_rows(hass: HomeAssistant, answer) -> None:
    entry, entity_id = forecast_entry(hass, HELIOS, "power_now")
    energy_platform(hass, HELIOS, {entry.entry_id: answer})
    rows, problem = await solar_forecast.async_solar_forecast_rows(hass, entity_id, CPH, timeout=0.05)
    assert rows == [] and problem


async def test_an_entry_that_is_not_loaded_is_not_asked(hass: HomeAssistant) -> None:
    entry, entity_id = forecast_entry(hass, HELIOS, "power_now", ConfigEntryState.SETUP_RETRY)
    calls = energy_platform(hass, HELIOS, {entry.entry_id: {"wh_hours": {utc(SOON, 10): 1.0, utc(SOON, 11): 1.0}}})
    rows, problem = await solar_forecast.async_solar_forecast_rows(hass, entity_id, CPH)
    assert rows == [] and problem and calls == []


async def test_an_integration_without_energy_platform_or_another_integration(hass: HomeAssistant) -> None:
    entry, entity_id = forecast_entry(hass, HELIOS, "power_now")
    mock_integration(hass, MockModule(HELIOS))  # no energy platform: importing it fails
    rows, problem = await solar_forecast.async_solar_forecast_rows(hass, entity_id, CPH)
    assert rows == [] and problem
    _, other_id = forecast_entry(hass, "solcast_solar", "total_kwh_forecast_today")
    rows, problem = await solar_forecast.async_solar_forecast_rows(hass, other_id, CPH)
    assert rows == [] and problem
    rows, problem = await solar_forecast.async_solar_forecast_rows(hass, "sensor.not_registered", CPH)
    assert rows == [] and problem


def energy_prefs(*forecast_lists: list[str] | None):
    data = {"energy_sources": [{"type": "solar", "stat_energy_from": f"sensor.solar_{index}", "config_entry_solar_forecast": ids} for index, ids in enumerate(forecast_lists)]}
    return patch("homeassistant.components.energy.data.async_get_manager", return_value=SimpleNamespace(data=data))


async def test_every_plane_of_the_energy_dashboard_source_is_summed(hass: HomeAssistant) -> None:
    east, entity_id = forecast_entry(hass, FORECAST_SOLAR, "energy_production_today")
    west, _ = forecast_entry(hass, FORECAST_SOLAR, "energy_production_today_2")
    other, _ = forecast_entry(hass, FORECAST_SOLAR, "energy_production_today_3")  # another source: not summed
    helios, _ = forecast_entry(hass, HELIOS, "power_now")  # another integration in the list: not summed
    east_data = {local(SOON, hour_): 100 for hour_ in range(6, 15)}
    west_data = {local(SOON, hour_): 10 for hour_ in range(6, 12)}
    calls = energy_platform(hass, FORECAST_SOLAR, {east.entry_id: {"wh_hours": east_data}, west.entry_id: {"wh_hours": west_data}, other.entry_id: {"wh_hours": east_data}})
    hass.config.components.add("energy")
    with energy_prefs([helios.entry_id, east.entry_id, west.entry_id], [other.entry_id]):
        rows, problem = await solar_forecast.async_solar_forecast_rows(hass, entity_id, CPH)
    assert problem is None and calls == [east.entry_id, west.entry_id]
    assert by_hour(rows) == {hour(SOON, hour_): 0.11 for hour_ in range(6, 11)}

    # a plane that is not loaded (enabled, but its setup failed) would give half a forecast: none at all
    calls.clear()
    west.mock_state(hass, ConfigEntryState.SETUP_ERROR)
    with energy_prefs([east.entry_id, west.entry_id]):
        rows, problem = await solar_forecast.async_solar_forecast_rows(hass, entity_id, CPH)
    assert rows == [] and problem and calls == []
    # a plane the user disabled is left out
    off = MockConfigEntry(domain=FORECAST_SOLAR, title="off", disabled_by=ConfigEntryDisabler.USER)
    off.add_to_hass(hass)
    with energy_prefs([east.entry_id, off.entry_id]):
        rows, problem = await solar_forecast.async_solar_forecast_rows(hass, entity_id, CPH)
    assert problem is None and calls == [east.entry_id]
    assert by_hour(rows)[hour(SOON, 6)] == 0.1


async def test_without_the_energy_dashboard_only_the_selected_entry(hass: HomeAssistant) -> None:
    east, entity_id = forecast_entry(hass, FORECAST_SOLAR, "energy_production_today")
    west, _ = forecast_entry(hass, FORECAST_SOLAR, "energy_production_today_2")
    data = {local(SOON, hour_): 100 for hour_ in range(6, 9)}
    calls = energy_platform(hass, FORECAST_SOLAR, {east.entry_id: {"wh_hours": data}, west.entry_id: {"wh_hours": data}})
    rows, problem = await solar_forecast.async_solar_forecast_rows(hass, entity_id, CPH)
    assert problem is None and calls == [east.entry_id] and len(rows) == 2
    # the selected entry is not in the dashboard's lists: also only that one
    hass.config.components.add("energy")
    calls.clear()
    with energy_prefs([west.entry_id], None):
        rows, problem = await solar_forecast.async_solar_forecast_rows(hass, entity_id, CPH)
    assert problem is None and calls == [east.entry_id]
    # an Energy dashboard that can't be read: the other planes are unknown, so no forecast
    with patch("homeassistant.components.energy.data.async_get_manager", side_effect=RuntimeError("broken")):
        assert (await solar_forecast.async_solar_forecast_rows(hass, entity_id, CPH))[0] == []
