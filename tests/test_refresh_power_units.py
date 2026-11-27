"""Power sensors in any unit (mW, W, kW, MW, GW, their h-variants; no unit = W) read by the charging loss and the What-if
readers of coordinator/refresh.py. The statistics are asked for STATISTICS_POWER_UNITS (W, or kWh for an energy statistic) and
converted by the unit they were recorded in; raw states by the unit they have now. A unit that is not a power gives no value."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from custom_components.cable_juice_planner.coordinator.refresh import current_power_factor, current_unit, statistic_factor
from custom_components.cable_juice_planner.utils.power import STATISTICS_POWER_UNITS, statistic_power_factor_to_w

SOC = "sensor.cjp_test_battery_level"
POWER = "sensor.cjp_test_charger_power"
METER = "sensor.cjp_test_charger_meter"
SOLAR = "sensor.cjp_test_solar"
HOUSE = "sensor.cjp_test_house"
T = datetime(2026, 10, 1, 10, tzinfo=UTC)  # the hour the statistics are about
END = datetime(2026, 10, 2, tzinfo=UTC)

# what Home Assistant gives for a stored value when STATISTICS_POWER_UNITS is asked for: a power in W, an energy in kWh
_POWER_TO_W = {"mW": 0.001, "W": 1.0, "kW": 1000.0, "MW": 1_000_000.0, "GW": 1_000_000_000.0}
_ENERGY_TO_KWH = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}

# (unit the sensor is recorded in and has now, the value it has there, the same reading in W)
POWER_CASES = [
    ("kW", 7.0, 7000.0),
    ("kWh", 7.4, 7400.0),
    ("MW", 0.007, 7000.0),
    ("MWh", 0.0074, 7400.0),
    ("W", 7000.0, 7000.0),
    (None, 7000.0, 7000.0),
]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request):
    """Replaces the conftest fixture of the same name for this file. The conftest one sets up hass first, and the recorder
    refuses to start after hass (recorder_db_url asserts that), so a test that asks for recorder_mock gets the recorder first."""
    if "recorder_mock" in request.fixturenames:
        request.getfixturevalue("recorder_mock")
    request.getfixturevalue("enable_custom_integrations")
    yield


class _FakeRecorder:
    keep_days = 10

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _requested(stored_unit, value, requested):
    """A stored value in the requested units, as Home Assistant converts it: a unit class gets the requested unit, others stay."""
    if requested != STATISTICS_POWER_UNITS:
        return value
    if stored_unit in _POWER_TO_W:
        return value * _POWER_TO_W[stored_unit]
    if stored_unit in _ENERGY_TO_KWH:
        return value * _ENERGY_TO_KWH[stored_unit]
    return value  # no unit class (no unit, %, A): unchanged


def _metadata_fake(units: dict):
    """get_metadata for the entities in units (entity -> the unit it was recorded in); an entity not in units has none."""

    def get_metadata(hass, *, statistic_ids=None, **_):
        return {
            entity_id: (number, {"statistic_id": entity_id, "unit_of_measurement": unit})
            for number, (entity_id, unit) in enumerate(units.items(), 1) if entity_id in (statistic_ids or ())
        }

    return get_metadata


def _statistics_fake(units: dict, rows: dict, seen: list):
    """statistics_during_period: rows[period][entity] are the stored rows; each period answers once (like the statistics
    of a day the recorder does not have again). Every request is appended to seen."""
    asked: set[str] = set()

    def statistics_during_period(hass, begin, end, ids, period, requested, kinds):
        seen.append({"period": period, "units": requested, "kinds": set(kinds), "ids": set(ids)})
        if period in asked:
            return {}
        asked.add(period)
        source = rows.get(period, {})
        result = {}
        for entity_id in ids:
            if not source.get(entity_id):
                continue
            stored = units.get(entity_id)
            result[entity_id] = [
                {key: (_requested(stored, value, requested) if key in ("mean", "min", "max", "change") and value is not None else value)
                 for key, value in row.items()}
                for row in source[entity_id]
            ]
        return result

    return statistics_during_period


def _history_fake(raw: dict):
    def get_significant_states(hass, begin, end, ids, *args, **kwargs):
        return {entity_id: raw[entity_id] for entity_id in ids if entity_id in raw}

    return get_significant_states


def _stat_ts(row):
    return row["start"].timestamp()


async def _read_charging_loss(loaded_entry, *, units: dict, rows: dict | None = None, raw: dict | None = None, backfill: bool = False):
    """Runs the charging loss read against fake recorder data. Returns the payloads sent to the charging loss job and the requests."""
    coordinator = loaded_entry.runtime_data
    seen: list = []
    job = AsyncMock(return_value=SimpleNamespace(value={}))
    with (
        patch("homeassistant.components.recorder.get_instance", return_value=_FakeRecorder()),
        patch("homeassistant.components.recorder.statistics.get_metadata", side_effect=_metadata_fake(units)),
        patch("homeassistant.components.recorder.statistics.statistics_during_period", side_effect=_statistics_fake(units, rows or {}, seen)),
        patch("homeassistant.components.recorder.history.get_significant_states", side_effect=_history_fake(raw or {})),
        patch.object(coordinator, "async_run_job", job),
        patch.dict(coordinator.config["ev_car"]["entity_ids"], {"usable_battery_level_entity_id": SOC}),
        patch.dict(coordinator.config["charger"]["entity_ids"], {"kwh_meter_entity_id": METER, "charger_power_entity_id": POWER}),
    ):
        await coordinator._async_charging_loss_read(backfill)
    return [call.args[1] for call in job.await_args_list], seen


def _set_state(hass: HomeAssistant, entity_id: str, state: str, unit: str | None) -> None:
    attributes = {} if unit is None else {"unit_of_measurement": unit}
    hass.states.async_set(entity_id, state, attributes)


@pytest.mark.parametrize(("unit", "stored", "watts"), POWER_CASES)
async def test_the_charging_loss_statistics_are_read_in_watts_whatever_the_unit(hass: HomeAssistant, loaded_entry, unit, stored, watts) -> None:
    _set_state(hass, POWER, str(stored), unit)
    rows = {"5minute": {POWER: [{"start": T, "mean": stored, "min": stored, "max": stored}]}}
    payloads, seen = await _read_charging_loss(loaded_entry, units={POWER: unit}, rows=rows)
    assert seen[0]["units"] == STATISTICS_POWER_UNITS
    assert payloads[0]["power"] == [(T.timestamp(), pytest.approx(watts), pytest.approx(watts), pytest.approx(watts))]


@pytest.mark.parametrize(("unit", "stored", "watts"), POWER_CASES)
async def test_the_raw_charging_loss_states_are_read_in_the_unit_they_have_now(hass: HomeAssistant, loaded_entry, unit, stored, watts) -> None:
    """No statistics for the sensor (no metadata): its raw states, converted by the unit the sensor has now."""
    now = dt_util.utcnow()
    _set_state(hass, POWER, str(stored), unit)
    raw = {POWER: [State(POWER, str(stored), last_changed=now - timedelta(hours=2))]}
    payloads, _ = await _read_charging_loss(loaded_entry, units={}, raw=raw)
    values = [row[1] for row in payloads[0]["power"] if row[1] is not None]
    assert values and all(value == pytest.approx(watts) for value in values)


async def test_a_power_statistic_in_an_unknown_unit_gives_no_power_rows(hass: HomeAssistant, loaded_entry) -> None:
    now = dt_util.utcnow()
    _set_state(hass, POWER, "7", "A")
    rows = {"5minute": {POWER: [{"start": T, "mean": 7.0, "min": 7.0, "max": 7.0}]}}
    raw = {POWER: [State(POWER, "7", last_changed=now - timedelta(hours=2))]}
    payloads, _ = await _read_charging_loss(loaded_entry, units={POWER: "A"}, rows=rows, raw=raw)
    # neither the statistic (A is not a power) nor the raw state (A) gives a power: no row with a value, never 7 W
    assert [row for row in payloads[0]["power"] if row[1] is not None] == []


async def test_a_power_without_metadata_is_read_raw_in_the_unit_it_has_now(hass: HomeAssistant, loaded_entry) -> None:
    now = dt_util.utcnow()
    _set_state(hass, POWER, "7", "kW")
    rows = {"5minute": {POWER: [{"start": T, "mean": 7.0, "min": 7.0, "max": 7.0}]}}
    raw = {POWER: [State(POWER, "7", last_changed=now - timedelta(hours=2))]}
    payloads, _ = await _read_charging_loss(loaded_entry, units={}, rows=rows, raw=raw)
    values = {row[1] for row in payloads[0]["power"] if row[1] is not None}
    assert values == {7000.0}  # the raw state in kW, not the statistic that has no unit to be read by


async def test_the_meter_and_its_raw_states_keep_their_units(hass: HomeAssistant, loaded_entry) -> None:
    now = dt_util.utcnow()
    _set_state(hass, METER, "2000", "Wh")
    raw = {METER: [State(METER, "2000", last_changed=now - timedelta(hours=2))]}
    payloads, _ = await _read_charging_loss(loaded_entry, units={METER: "Wh"}, raw=raw)
    values = [value for _, value in payloads[0]["meter"]]
    assert values and all(value == pytest.approx(2.0) for value in values)  # 2000 Wh is 2 kWh


@pytest.mark.parametrize(("unit", "stored", "watts"), POWER_CASES)
async def test_the_hourly_statistics_are_read_in_watts_and_kwh_and_the_backfill_is_marked_done(hass: HomeAssistant, loaded_entry, unit, stored, watts) -> None:
    _set_state(hass, POWER, str(stored), unit)
    rows = {
        "5minute": {POWER: [{"start": T, "mean": stored, "min": stored, "max": stored}]},
        "hour": {
            POWER: [{"start": T, "mean": stored, "min": stored * 0.8, "max": stored * 1.2}],
            METER: [{"start": T, "change": 2000.0}],  # Wh: an energy statistic comes in kWh
            SOC: [{"start": T, "mean": 50.0}],
        },
    }
    payloads, seen = await _read_charging_loss(loaded_entry, units={POWER: unit, METER: "Wh", SOC: "%"}, rows=rows, backfill=True)
    assert all(request["units"] == STATISTICS_POWER_UNITS for request in seen if request["period"] in ("5minute", "hour"))
    hours = [payload for payload in payloads if "hours" in payload]
    assert len(hours) == 1
    row = {entry["start"]: entry for entry in hours[0]["hours"]}[T.timestamp()]
    assert row["power_mean"] == pytest.approx(watts)
    assert row["power_min"] == pytest.approx(watts * 0.8)
    assert row["power_max"] == pytest.approx(watts * 1.2)
    assert row["kwh"] == pytest.approx(2.0)
    assert row["soc"] == 50.0
    assert payloads[-1] == {"backfill": True, "soc_power_statistics_done": True}


async def test_an_hourly_power_in_an_unknown_unit_reads_nothing_older_and_is_done(hass: HomeAssistant, loaded_entry) -> None:
    _set_state(hass, POWER, "7", "A")
    rows = {"hour": {POWER: [{"start": T, "mean": 7.0, "min": 7.0, "max": 7.0}]}}
    payloads, _ = await _read_charging_loss(loaded_entry, units={POWER: "A"}, rows=rows, backfill=True)
    assert not any("hours" in payload for payload in payloads)
    # done as without statistics: a retry would read the whole raw history again at every start
    assert payloads[-1] == {"backfill": True, "soc_power_statistics_done": True}


async def test_a_power_sensor_without_statistics_has_nothing_older_and_the_backfill_is_done(hass: HomeAssistant, loaded_entry) -> None:
    _set_state(hass, POWER, "7", "kW")
    payloads, _ = await _read_charging_loss(loaded_entry, units={}, backfill=True)
    assert not any("hours" in payload for payload in payloads)
    assert payloads[-1] == {"backfill": True, "soc_power_statistics_done": True}  # as before the statistics began: not retried


async def _read_ev(loaded_entry, hass: HomeAssistant, *, units: dict, rows: dict, state_unit: str | None):
    coordinator = loaded_entry.runtime_data
    coordinator.ev_configured = True
    _set_state(hass, POWER, "7", state_unit)
    seen: list = []
    data: dict = {}
    with (
        patch("homeassistant.components.recorder.statistics.get_metadata", side_effect=_metadata_fake(units)),
        patch("homeassistant.components.recorder.statistics.statistics_during_period", side_effect=_statistics_fake(units, rows, seen)),
        patch("homeassistant.components.recorder.history.get_significant_states", side_effect=_history_fake({})),
        patch.dict(coordinator.config["ev_car"]["entity_ids"], {"usable_battery_level_entity_id": SOC}),
        patch.dict(coordinator.config["charger"]["entity_ids"], {"charger_power_entity_id": POWER}),
    ):
        await coordinator._async_economy_ev_statistics(_FakeRecorder(), data, END, _stat_ts, keep_days=10)
    return data, seen


@pytest.mark.parametrize(("unit", "stored", "watts"), POWER_CASES)
async def test_the_what_if_older_connections_read_the_charger_power_in_watts(hass: HomeAssistant, loaded_entry, unit, stored, watts) -> None:
    rows = {"hour": {
        SOC: [{"start": T, "mean": 61.0, "min": 60.0, "max": 62.0}],
        POWER: [{"start": T, "mean": stored, "min": stored, "max": stored}],
    }}
    data, seen = await _read_ev(loaded_entry, hass, units={SOC: "%", POWER: unit}, rows=rows, state_unit=unit)
    assert seen[0]["units"] == STATISTICS_POWER_UNITS
    # the battery level has no unit class: it is read as it is, in percent
    assert data["ev_hours"][T.timestamp()] == [61.0, 60.0, 62.0, pytest.approx(watts)]


@pytest.mark.parametrize(("units", "state_unit"), [({SOC: "%", POWER: "A"}, "A"), ({SOC: "%"}, "kW")])
async def test_the_what_if_older_connections_leave_out_a_charger_power_without_a_known_unit(hass: HomeAssistant, loaded_entry, units, state_unit) -> None:
    rows = {"hour": {
        SOC: [{"start": T, "mean": 61.0, "min": 60.0, "max": 62.0}],
        POWER: [{"start": T, "mean": 7.0, "min": 7.0, "max": 7.0}],
    }}
    data, _ = await _read_ev(loaded_entry, hass, units=units, rows=rows, state_unit=state_unit)
    assert data["ev_hours"][T.timestamp()] == [61.0, 60.0, 62.0, None]  # no power, never a guessed one


async def test_the_what_if_solar_statistics_are_read_in_watts_per_unit(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    provider = coordinator.price_provider
    units = {SOLAR: "kWh", HOUSE: "MW", POWER: "W"}
    _set_state(hass, SOLAR, "1.5", "kWh")
    _set_state(hass, HOUSE, "0.0008", "MW")
    _set_state(hass, POWER, "800", "W")
    rows = {"hour": {SOLAR: [{"start": T, "mean": 1.5}], HOUSE: [{"start": T, "mean": 0.0008}], POWER: [{"start": T, "mean": 800.0}]}}
    seen: list = []
    with (
        patch("homeassistant.components.recorder.statistics.get_metadata", side_effect=_metadata_fake(units)),
        patch("homeassistant.components.recorder.statistics.statistics_during_period", side_effect=_statistics_fake(units, rows, seen)),
        patch("homeassistant.components.recorder.history.get_significant_states", side_effect=_history_fake({})),
        patch.object(coordinator.engine, "is_solar_configured", return_value=True),
        patch.dict(coordinator.config["solar"]["entity_ids"], {"production_entity_id": SOLAR}),
        patch.dict(coordinator.config["home"]["entity_ids"], {"power_consumption_entity_id": HOUSE}),
        patch.dict(coordinator.config["charger"]["entity_ids"], {"charger_power_entity_id": POWER}),
        patch.object(provider, "sell_price_entity_ids", return_value=[]),
    ):
        data: dict = {}
        await coordinator._async_economy_solar_statistics(_FakeRecorder(), provider, data, END, _stat_ts)
    assert seen[0]["units"] == STATISTICS_POWER_UNITS
    assert data["power_hours"] == {T.timestamp(): {"solar": pytest.approx(1500.0), "house": pytest.approx(800.0), "ev": pytest.approx(800.0)}}


async def test_the_what_if_solar_house_in_an_unknown_unit_is_left_out(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    provider = coordinator.price_provider
    units = {SOLAR: "kWh", HOUSE: "A", POWER: "W"}
    _set_state(hass, SOLAR, "1.5", "kWh")
    _set_state(hass, HOUSE, "0.8", "A")
    _set_state(hass, POWER, "800", "W")
    rows = {"hour": {SOLAR: [{"start": T, "mean": 1.5}], HOUSE: [{"start": T, "mean": 0.8}], POWER: [{"start": T, "mean": 800.0}]}}
    with (
        patch("homeassistant.components.recorder.statistics.get_metadata", side_effect=_metadata_fake(units)),
        patch("homeassistant.components.recorder.statistics.statistics_during_period", side_effect=_statistics_fake(units, rows, [])),
        patch("homeassistant.components.recorder.history.get_significant_states", side_effect=_history_fake({})),
        patch.object(coordinator.engine, "is_solar_configured", return_value=True),
        patch.dict(coordinator.config["solar"]["entity_ids"], {"production_entity_id": SOLAR}),
        patch.dict(coordinator.config["home"]["entity_ids"], {"power_consumption_entity_id": HOUSE}),
        patch.dict(coordinator.config["charger"]["entity_ids"], {"charger_power_entity_id": POWER}),
        patch.object(provider, "sell_price_entity_ids", return_value=[]),
    ):
        data: dict = {}
        await coordinator._async_economy_solar_statistics(_FakeRecorder(), provider, data, END, _stat_ts)
    assert data["power_hours"] == {T.timestamp(): {"solar": pytest.approx(1500.0), "ev": pytest.approx(800.0)}}


async def test_a_live_power_state_converts_to_watts_or_gives_nothing(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    # an h-unit is read as the unit without the h (7.4 kWh is 7400 W; Wh is read as W, like power_convert)
    for state, unit, watts_per_unit in (("7", "kW", 1000.0), ("0.007", "MW", 1_000_000.0), ("7.4", "kWh", 1000.0), ("7", "W", 1.0), ("7", "Wh", 1.0)):
        _set_state(hass, POWER, state, unit)
        assert coordinator._power_to_watt(POWER)(1.0) == pytest.approx(watts_per_unit), unit
    _set_state(hass, POWER, "7", "A")
    assert coordinator._power_to_watt(POWER) is None
    _set_state(hass, POWER, "unavailable", "kW")
    assert coordinator._power_to_watt(POWER)(1.0) == pytest.approx(1000.0)  # HA keeps the unit on an unavailable state
    _set_state(hass, POWER, "unavailable", None)
    assert coordinator._power_to_watt(POWER) is None  # no unit while unavailable and nothing recorded: not known
    assert coordinator._power_to_watt("sensor.cjp_test_missing") is None


class _States:
    def __init__(self, states: dict):
        self._states = states

    def get(self, entity_id):
        return self._states.get(entity_id)


def test_the_unit_helpers_fall_back_only_to_a_known_unit() -> None:
    kw, unavailable, unitless, gone, missing = (
        "sensor.cjp_test_kw", "sensor.cjp_test_unavailable", "sensor.cjp_test_unitless", "sensor.cjp_test_gone", "sensor.cjp_test_missing",
    )
    hass = SimpleNamespace(states=_States({
        kw: State(kw, "7", {"unit_of_measurement": "kW"}),
        unavailable: State(unavailable, "unavailable", {}),
        unitless: State(unitless, "7", {}),
    }))
    metadata = {unavailable: (1, {"unit_of_measurement": "kWh"}), gone: (2, {"unit_of_measurement": "kW"})}
    assert current_power_factor(hass, kw) == 1000.0
    assert current_power_factor(hass, unitless) == 1.0  # no unit attribute: W
    assert current_unit(hass, unavailable, metadata) == (True, "kWh")  # the recorded unit when the state gives none
    assert current_power_factor(hass, unavailable) is None
    assert current_power_factor(hass, gone, metadata) == 1000.0  # no state now: the unit it was recorded in
    assert current_power_factor(hass, missing) is None
    assert statistic_factor({}, kw) is None  # no metadata: never a guess
    assert statistic_factor({kw: (1, {"unit_of_measurement": None})}, kw) == 1.0
    assert statistic_factor({kw: (1, {"unit_of_measurement": "A"})}, kw) is None
    assert statistic_factor({kw: (1, {"unit_of_measurement": "GJ"})}, kw) is None
    assert statistic_power_factor_to_w("MWh") == 1000.0  # an energy statistic is kWh: 1 kWh is 1000 W


def test_home_assistant_converts_a_statistic_the_way_the_factor_assumes() -> None:
    """Home Assistant's own converter, which statistics_during_period applies with STATISTICS_POWER_UNITS, without the recorder
    database: the value of a power statistic comes in W, of an energy statistic in kWh, and the factor then brings it to W."""
    from homeassistant.components.recorder.statistics import _get_statistic_to_display_unit_converter  # noqa: PLC0415

    cases = [
        ("power", "kW", 7.0, 7000.0),
        ("power", "W", 7000.0, 7000.0),
        ("power", "MW", 0.007, 7000.0),
        ("energy", "kWh", 7.4, 7400.0),
        ("energy", "Wh", 7400.0, 7400.0),
        ("energy", "MWh", 0.0074, 7400.0),
    ]
    for unit_class, unit, value, watts in cases:
        convert = _get_statistic_to_display_unit_converter(unit_class, unit, unit, STATISTICS_POWER_UNITS)
        converted = convert(value) if convert is not None else value
        assert converted * statistic_power_factor_to_w(unit) == pytest.approx(watts), unit
    # the battery level has no unit class: Home Assistant leaves its values as they are (no factor is used for it)
    level = _get_statistic_to_display_unit_converter(None, "%", "%", STATISTICS_POWER_UNITS)
    assert (level(61.0) if level is not None else 61.0) == 61.0


async def test_the_what_if_statistics_of_a_kwh_and_a_kw_charger_are_read_in_watts_on_the_real_recorder(
    recorder_mock, hass: HomeAssistant, loaded_entry
) -> None:
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415
    from homeassistant.components.recorder.models import StatisticMeanType  # noqa: PLC0415
    from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
        async_import_statistics,
        get_metadata,
        statistics_during_period,
    )
    from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done  # noqa: PLC0415

    kwh_charger = "sensor.cjp_test_kwh_charger"
    kw_charger = "sensor.cjp_test_kw_charger"
    level = "sensor.cjp_test_level"
    now = dt_util.utcnow()
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=5)
    hours = [start + timedelta(hours=offset) for offset in range(3)]
    for statistic_id, unit, unit_class, value in (
        (kwh_charger, "kWh", "energy", 7.4),
        (kw_charger, "kW", "power", 7.0),
        (level, "%", None, 61.0),
    ):
        async_import_statistics(
            hass,
            {
                "source": "recorder", "statistic_id": statistic_id, "name": None, "unit_of_measurement": unit,
                "unit_class": unit_class, "mean_type": StatisticMeanType.ARITHMETIC, "has_sum": False,
            },
            [{"start": hour, "mean": value, "min": value, "max": value} for hour in hours],
        )
    await async_wait_recording_done(hass)

    instance = get_instance(hass)
    coordinator = loaded_entry.runtime_data
    coordinator.ev_configured = True
    ids = {kwh_charger, kw_charger}

    # the statistics as the What-if asks for them: the power in W (kWh for an energy statistic), the level in percent
    stats = await instance.async_add_executor_job(statistics_during_period, hass, start, now, ids, "hour", STATISTICS_POWER_UNITS, {"mean"})
    metadata = await instance.async_add_executor_job(partial(get_metadata, hass, statistic_ids=ids))
    assert [row["mean"] for row in stats[kwh_charger]] == [pytest.approx(7.4)] * 3
    assert statistic_factor(metadata, kwh_charger) == pytest.approx(1000.0)
    assert statistic_factor(metadata, kw_charger) == pytest.approx(1.0)
    assert [row["mean"] * statistic_factor(metadata, kwh_charger) for row in stats[kwh_charger]] == [pytest.approx(7400.0)] * 3
    assert [row["mean"] * statistic_factor(metadata, kw_charger) for row in stats[kw_charger]] == [pytest.approx(7000.0)] * 3

    # and the What-if's own read of the older connections, on the real recorder
    for charger, state, unit, watts in ((kwh_charger, "7.4", "kWh", 7400.0), (kw_charger, "7", "kW", 7000.0)):
        hass.states.async_set(charger, state, {"unit_of_measurement": unit})
        data: dict = {}
        with patch.dict(coordinator.config["ev_car"]["entity_ids"], {"usable_battery_level_entity_id": level}), patch.dict(
            coordinator.config["charger"]["entity_ids"], {"charger_power_entity_id": charger}
        ):
            # the real recorder gives the start as a float (or a datetime): the same reading as the coordinator's own
            await coordinator._async_economy_ev_statistics(
                instance, data, now, lambda row: row["start"].timestamp() if isinstance(row["start"], datetime) else float(row["start"]), keep_days=10,
            )
        for hour in hours:
            assert data["ev_hours"][hour.timestamp()] == [61.0, 61.0, 61.0, pytest.approx(watts)]
