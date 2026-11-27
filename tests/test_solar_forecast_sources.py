"""The solar forecast sources of the coordinator's snapshot: Solcast's detailedHourly, and the energy platform of
Forecast.Solar and Helios Forecast normalised to the same rows (utils/solar_forecast.py), also through the debug export
and its replay."""
from __future__ import annotations

import asyncio
import datetime
import gzip
import logging
from collections.abc import AsyncGenerator
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockModule, mock_integration, mock_platform

from custom_components.cable_juice_planner.const import DOMAIN
from custom_components.cable_juice_planner.coordinator import data as coordinator_data
from custom_components.cable_juice_planner.management.debug_export import DEBUG_DIR
from custom_components.cable_juice_planner.utils.solcast import SOLCAST_UNIQUE_IDS

from . import debug_replay
from .conftest import MOCK_OPTIONS, OBJECT_PREFIX, async_setup, async_unload_and_join, make_entry, set_source_states

HELIOS_SENSOR = "sensor.helios_forecast_power_now"
FORECAST_SOLAR_SENSOR = "sensor.energy_production_today"
SOLCAST_BASE = "sensor.solcast_pv_forecast_forecast"
PRODUCTION = "sensor.solar_production"


@pytest.fixture
def config_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    hass.config.config_dir = str(tmp_path)
    return tmp_path


def _wh_hours() -> dict[str, float]:
    """Two days of a Helios forecast from two hours ago: Wh per UTC hour, keyed by the start of the hour."""
    start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - datetime.timedelta(hours=2)
    hours = [start + datetime.timedelta(hours=offset) for offset in range(48)]
    return {moment.isoformat(): float(max(0, 4000 - abs(moment.hour - 18) * 600)) for moment in hours}


def _expected_rows(wh_hours: dict[str, float]) -> list[dict]:
    """The rows written out by hand for whole UTC hours keyed by their start: each one is the local hour it falls in (the
    hour that occurs twice when the clocks go back gets both, so 48 UTC hours may give 47 rows)."""
    tz = dt_util.get_default_time_zone()
    wh: dict[datetime.datetime, float] = {}
    for key, value in wh_hours.items():
        local = datetime.datetime.fromisoformat(key).astimezone(tz).replace(tzinfo=None)
        wh[local] = wh.get(local, 0.0) + value
    return [{"period_start": local.replace(tzinfo=tz), "pv_estimate": round(value / 1000, 4)} for local, value in sorted(wh.items())]


def energy_platform(hass: HomeAssistant, domain: str, answer: dict) -> list[str]:
    """A fake energy platform of an integration; answer["value"] is what it returns or raises (changeable by the test),
    "slow" waits longer than any timeout."""
    calls: list[str] = []

    async def async_get_solar_forecast(hass: HomeAssistant, config_entry_id: str):
        calls.append(config_entry_id)
        value = answer["value"]
        if isinstance(value, BaseException):
            raise value
        if value == "slow":
            await asyncio.sleep(10)
        return value

    mock_integration(hass, MockModule(domain))
    mock_platform(hass, f"{domain}.energy", SimpleNamespace(async_get_solar_forecast=async_get_solar_forecast))
    return calls


def helios(hass: HomeAssistant, answer: dict, device_identifier: tuple[str, str] | None = None) -> tuple[MockConfigEntry, list[str]]:
    entry = MockConfigEntry(domain="helios_forecast", title="Helios", state=ConfigEntryState.LOADED)
    entry.add_to_hass(hass)
    device_id = None
    if device_identifier:
        device_id = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={device_identifier}).id
    registered = er.async_get(hass).async_get_or_create(
        "sensor", "helios_forecast", f"{entry.entry_id}_power_now", config_entry=entry, device_id=device_id,
        suggested_object_id="helios_forecast_power_now",
    )
    assert registered.entity_id == HELIOS_SENSOR
    hass.states.async_set(HELIOS_SENSOR, "1200", {"unit_of_measurement": "W", "device_class": "power"})
    return entry, energy_platform(hass, "helios_forecast", answer)


def forecast_solar_plane(hass: HomeAssistant, name: str) -> tuple[MockConfigEntry, str]:
    plane = MockConfigEntry(domain="forecast_solar", title=name, state=ConfigEntryState.LOADED)
    plane.add_to_hass(hass)
    sensor = er.async_get(hass).async_get_or_create(
        "sensor", "forecast_solar", f"{plane.entry_id}_energy_production_today", config_entry=plane,
        suggested_object_id="energy_production_today",
    ).entity_id
    hass.states.async_set(sensor, "12.5", {"unit_of_measurement": "kWh"})
    return plane, sensor


async def _setup(hass: HomeAssistant, forecast_entity: str) -> MockConfigEntry:
    set_source_states(hass)
    hass.states.async_set(PRODUCTION, "0", {"unit_of_measurement": "W", "device_class": "power"})
    entry = make_entry(options={**MOCK_OPTIONS, "production_entity_id": PRODUCTION, "forecast_entity_id": forecast_entity})
    await async_setup(hass, entry)
    return entry


@pytest.fixture
async def unload(hass: HomeAssistant) -> AsyncGenerator[list[MockConfigEntry]]:
    entries: list[MockConfigEntry] = []
    yield entries
    for entry in entries:
        if entry.state is ConfigEntryState.LOADED:
            await async_unload_and_join(hass, entry)


async def _snapshot_rows(entry: MockConfigEntry) -> dict:
    return (await entry.runtime_data._async_snapshot(include_forecast=True)).solcast  # noqa: SLF001


async def test_solcast_rows_are_taken_as_they_are(hass: HomeAssistant, unload) -> None:
    """Regression: the Solcast path is unchanged, and the energy platform is not asked for a Solcast sensor."""
    tz = dt_util.get_default_time_zone()
    today = dt_util.now().date()
    attributes = {}
    for day, (ending, unique_id) in enumerate(SOLCAST_UNIQUE_IDS.items()):
        entity_id = er.async_get(hass).async_get_or_create("sensor", "solcast_solar", unique_id, suggested_object_id=f"solcast_pv_forecast_forecast_{ending}").entity_id
        date = today + datetime.timedelta(days=day)
        rows = [{"period_start": datetime.datetime.combine(date, datetime.time(hour), tzinfo=tz), "pv_estimate": hour / 10, "pv_estimate10": 0.0} for hour in range(24)]
        hass.states.async_set(entity_id, "12.3", {"detailedHourly": rows, "unit_of_measurement": "kWh"})
        attributes[entity_id] = rows
    calls = energy_platform(hass, "solcast_solar", {"value": {"wh_hours": _wh_hours()}})
    unload.append(entry := await _setup(hass, f"{SOLCAST_BASE}_today"))

    solcast = await _snapshot_rows(entry)
    assert solcast == attributes
    assert solcast[f"{SOLCAST_BASE}_today"][0] is hass.states.get(f"{SOLCAST_BASE}_today").attributes["detailedHourly"][0]
    assert calls == []


async def test_helios_rows_come_from_the_energy_platform(hass: HomeAssistant, unload, caplog: pytest.LogCaptureFixture) -> None:
    wh_hours = _wh_hours()
    answer = {"value": {"wh_hours": wh_hours}}
    helios_entry, calls = helios(hass, answer)
    unload.append(entry := await _setup(hass, HELIOS_SENSOR))
    coordinator = entry.runtime_data

    expected = _expected_rows(wh_hours)
    assert 47 <= len(expected) <= 48 and expected[0]["pv_estimate"] == wh_hours[min(wh_hours)] / 1000
    assert await _snapshot_rows(entry) == {HELIOS_SENSOR: expected}
    assert calls and set(calls) == {helios_entry.entry_id}
    # the plan reads them
    result = await coordinator.async_run_job("cron", include_forecast=True)
    assert result.error is None
    assert any(day["home"]["total_forecast"] for day in coordinator.engine.local_energy_prediction_db["solar_prediction"].values())

    # no answer: no rows (the weather and the history are used), a warning once until the reason changes
    answer["value"] = None
    caplog.clear()
    with caplog.at_level(logging.INFO):
        for _ in range(2):
            assert await _snapshot_rows(entry) == {}
        result = await coordinator.async_run_job("cron", include_forecast=True)
        assert result.error is None
        warnings = [record for record in caplog.records if record.getMessage().startswith("No solar forecast from helios_forecast")]
        assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
        assert helios_entry.entry_id not in caplog.text
        answer["value"] = {"wh_hours": wh_hours}
        assert await _snapshot_rows(entry) == {HELIOS_SENSOR: expected}
        assert "The solar forecast from helios_forecast is available again" in caplog.text


@pytest.mark.parametrize("failure", [RuntimeError("boom"), "slow"])
async def test_a_failing_or_slow_forecast_gives_no_rows_and_the_plan_still_runs(
    hass: HomeAssistant, unload, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, failure
) -> None:
    monkeypatch.setattr(coordinator_data, "async_solar_forecast_rows", partial(coordinator_data.async_solar_forecast_rows, timeout=0.05))
    helios(hass, {"value": failure})
    unload.append(entry := await _setup(hass, HELIOS_SENSOR))  # the start plans once already
    assert await _snapshot_rows(entry) == {}
    result = await entry.runtime_data.async_run_job("cron", include_forecast=True)
    assert result.error is None
    reason = "no answer within 0.05 seconds" if failure == "slow" else "the forecast failed (RuntimeError)"
    warnings = [record for record in caplog.records if record.getMessage().startswith("No solar forecast from helios_forecast")]
    assert len(warnings) == 1 and f"({reason})" in warnings[0].getMessage()


async def test_the_integration_is_the_platform_of_the_sensor_not_of_its_device(hass: HomeAssistant, unload) -> None:
    """The coordinator's platforms take the domain of the device's first identifier: a Helios device with another
    identifier domain must not hide the forecast (the energy platform is looked up by the sensor's platform)."""
    wh_hours = _wh_hours()
    helios(hass, {"value": {"wh_hours": wh_hours}}, device_identifier=("helios", "site"))
    unload.append(entry := await _setup(hass, HELIOS_SENSOR))
    assert entry.runtime_data._platforms[HELIOS_SENSOR] == "helios"  # noqa: SLF001
    assert await _snapshot_rows(entry) == {HELIOS_SENSOR: _expected_rows(wh_hours)}


async def test_forecast_solar_rows_count_each_value_for_the_hour_before_its_key(hass: HomeAssistant, unload) -> None:
    """Forecast.Solar keys the end of each period: the value at 10:00 is the energy of 09:00-10:00 (literal rows)."""
    tz = dt_util.get_default_time_zone()
    tomorrow = dt_util.now().date() + datetime.timedelta(days=1)

    def at(hour: int) -> datetime.datetime:
        return datetime.datetime.combine(tomorrow, datetime.time(hour), tzinfo=tz)

    plane, sensor = forecast_solar_plane(hass, "Roof")
    assert sensor == FORECAST_SOLAR_SENSOR
    wh_hours = {at(9).isoformat(): 0, at(10).isoformat(): 1000, at(11).isoformat(): 2500, at(12).isoformat(): 1500, at(13).isoformat(): 500}
    calls = energy_platform(hass, "forecast_solar", {"value": {"wh_hours": wh_hours}})
    unload.append(entry := await _setup(hass, sensor))

    assert await _snapshot_rows(entry) == {
        sensor: [
            {"period_start": at(9), "pv_estimate": 1.0},
            {"period_start": at(10), "pv_estimate": 2.5},
            {"period_start": at(11), "pv_estimate": 1.5},
            {"period_start": at(12), "pv_estimate": 0.5},
        ]
    }
    assert calls and set(calls) == {plane.entry_id}


async def test_the_roof_planes_of_the_energy_dashboard_are_added_up(hass: HomeAssistant, unload) -> None:
    """Forecast.Solar has one config entry per roof plane: the planes listed with the selected one as the forecast of a
    solar source in the Energy dashboard are summed, only for the hours every plane covers."""
    tz = dt_util.get_default_time_zone()
    tomorrow = dt_util.now().date() + datetime.timedelta(days=1)

    def at(hour: int) -> str:
        return datetime.datetime.combine(tomorrow, datetime.time(hour), tzinfo=tz).isoformat()

    east, sensor = forecast_solar_plane(hass, "East")
    west = MockConfigEntry(domain="forecast_solar", title="West", state=ConfigEntryState.LOADED)
    west.add_to_hass(hass)
    east_data = {at(hour): 1000 for hour in range(8, 14)}
    west_data = {at(hour): 500 for hour in range(8, 12)}
    answers = {east.entry_id: {"wh_hours": east_data}, west.entry_id: {"wh_hours": west_data}}
    calls: list[str] = []

    async def async_get_solar_forecast(hass: HomeAssistant, config_entry_id: str):
        calls.append(config_entry_id)
        return answers[config_entry_id]

    mock_integration(hass, MockModule("forecast_solar"))
    mock_platform(hass, "forecast_solar.energy", SimpleNamespace(async_get_solar_forecast=async_get_solar_forecast))
    hass.config.components.add("energy")
    prefs = {"energy_sources": [{"type": "solar", "stat_energy_from": "sensor.solar_energy", "config_entry_solar_forecast": [east.entry_id, west.entry_id]}]}
    unload.append(entry := await _setup(hass, sensor))
    with patch("homeassistant.components.energy.data.async_get_manager", return_value=SimpleNamespace(data=prefs)):
        rows = (await _snapshot_rows(entry))[sensor]
    assert set(calls) == {east.entry_id, west.entry_id}
    # the east plane covers 08:00-13:00, the west plane only 08:00-11:00: three hours of both
    assert [(row["period_start"].hour, row["pv_estimate"]) for row in rows] == [(8, 1.5), (9, 1.5), (10, 1.5)]


async def test_a_sensor_of_another_integration_gives_no_rows(hass: HomeAssistant, unload, caplog: pytest.LogCaptureFixture) -> None:
    """Only the integrations with known key semantics are read: another energy platform could key its hours otherwise.
    Nothing is logged for it (it is no forecast source of this integration)."""
    other = MockConfigEntry(domain="open_meteo_solar_forecast", state=ConfigEntryState.LOADED)
    other.add_to_hass(hass)
    er.async_get(hass).async_get_or_create("sensor", "open_meteo_solar_forecast", "x", config_entry=other, suggested_object_id="other_forecast")
    hass.states.async_set("sensor.other_forecast", "1.0")
    calls = energy_platform(hass, "open_meteo_solar_forecast", {"value": {"wh_hours": _wh_hours()}})
    unload.append(entry := await _setup(hass, "sensor.other_forecast"))
    assert await _snapshot_rows(entry) == {}
    assert calls == [] and "No solar forecast" not in caplog.text


@pytest.mark.parametrize("source", ["helios", "forecast_solar"])
async def test_the_debug_export_replays_the_normalised_rows(hass: HomeAssistant, config_dir: Path, unload, source: str) -> None:
    """There is no attribute to export for these sources: the snapshot keeps the rows under the configured sensor
    (named after its option by the Redactor, like the option itself), and the replay plans with them."""
    wh_hours = _wh_hours()
    if source == "helios":
        forecast_entry, _ = helios(hass, {"value": {"wh_hours": wh_hours}})
        sensor = HELIOS_SENSOR
        expected = _expected_rows(wh_hours)
    else:
        forecast_entry, sensor = forecast_solar_plane(hass, "Roof")
        energy_platform(hass, "forecast_solar", {"value": {"wh_hours": wh_hours}})
        expected = None  # the same keys read as period ends: the first one has no start
    unload.append(entry := await _setup(hass, sensor))
    coordinator = entry.runtime_data
    await coordinator.async_run_job("cron", include_forecast=True)
    recorded = coordinator.data["current_charging_rule"]

    response = await hass.services.async_call(DOMAIN, "export_debug", {}, blocking=True, return_response=True)
    path = config_dir / response["files"][OBJECT_PREFIX]
    assert path.parent.name == DEBUG_DIR
    with gzip.open(path, "rt", encoding="utf-8") as file:
        text = file.read()
    assert sensor not in text and forecast_entry.entry_id not in text
    export = debug_replay.load_export(str(path))
    redacted = export["config"]["solar"]["entity_ids"]["forecast_entity_id"]
    assert redacted != sensor
    solcast = export["planning_job"]["snapshot"]["solcast"]
    assert list(solcast) == [redacted]
    assert set(solcast[redacted][0]) == {"period_start", "pv_estimate"}
    if expected is not None:
        assert len(solcast[redacted]) == len(expected)

    snapshot = debug_replay.build_snapshot(export["planning_job"])
    assert snapshot.solcast[redacted][0]["period_start"].tzinfo is not None
    engine = debug_replay.build_engine(export)
    forecast = await hass.async_add_executor_job(engine.run_job, snapshot, engine.get_solar_kwh_forecast)
    assert forecast.error is None and forecast.value, "the replay reads the exported rows"
    engine = debug_replay.build_engine(export)
    engine.sensor_states.pop("current_charging_rule", None)
    result = await hass.async_add_executor_job(debug_replay.replay, export, None, engine)
    assert result.error is None
    assert engine.sensor_states["current_charging_rule"] == recorded
