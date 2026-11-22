"""The charger's power sensor in its own unit (mW, W, kW, MW, GW and the h-variants; no unit = W) for the readers of the
control: the detector of charging without a rule, the idle check of the restart, and the charger test's readings.

A sensor in kW (7.4) is 7400 W. Before, the detector read the live value raw, so a kW charger never looked like charging,
and a mW sensor (300 mW of standby) read as 300 W. A unit that is no power (A, VA, %) and an unavailable sensor that has lost
its unit give no reading: they never trigger, and a size is never guessed.
"""
from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.coordinator.charger_test import CoordinatorChargerTest
from custom_components.cable_juice_planner.engine import CJPEngine

from .test_engine import DAY, OPTIONS, TEXTS, TZ, snapshot

CHARGER = "sensor.charger_power"
MINUTE = datetime.timedelta(minutes=1)
CRON = datetime.timedelta(minutes=5)  # the cron of the test engine: the window of charging_without_rule is 5 minutes
START = datetime.datetime.combine(DAY, datetime.time(12, 0))


def _ts(moment: datetime.datetime) -> float:
    return moment.replace(tzinfo=TZ).timestamp()


def _snap(at: datetime.datetime, *, state: str, unit: str | None, history=None, settings: dict | None = None):
    snap = snapshot(at, status="charging", port="on", level=80, settings=settings)
    snap.states[CHARGER] = state
    if unit is None:
        snap.units.pop(CHARGER, None)  # HA drops the unit of a sensor that is unavailable
    else:
        snap.units[CHARGER] = unit
    rows = history(at) if history is not None else [(at - 30 * MINUTE, state)]
    snap.buffers = {CHARGER: sorted((_ts(moment), value) for moment, value in rows)}
    return snap


def _started() -> CJPEngine:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    result = engine.run_job(snapshot(START, status="charging", port="on", level=80), engine.startup, {})
    assert result.error is None, result.error
    return engine


def _run(engine: CJPEngine, job, moments, *, state: str, unit: str | None, history=None, settings: dict | None = None, args=()):
    results = []
    for at in moments:
        result = engine.run_job(_snap(at, state=state, unit=unit, history=history, settings=settings), job, *args)
        assert result.error is None, result.error
        results.append(result)
    return results


def _crons(count: int, start: datetime.datetime = START) -> list[datetime.datetime]:
    return [start + CRON * index for index in range(count)]


# --- the detector fires for every unit that is a power -------------------------------------------------------------------


@pytest.mark.parametrize(("state", "unit"), [("7.4", "kW"), ("7400", "W"), ("7.4", "kWh"), ("7400000", "mW"), ("0.0074", "MW"), ("7400", None)])
def test_a_charger_that_charges_without_a_rule_is_stopped_in_any_power_unit(state: str, unit: str | None) -> None:
    engine = _started()
    results = _run(engine, engine.charging_without_rule, _crons(5), state=state, unit=unit)

    # counted from the second cron on, stopped (forced from the third), notified and noted on the fifth
    assert [result.value for result in results] == [False, False, False, False, True]
    assert results[-1].charger_problems == ["no_rule"]
    assert engine.charger_action == "stop"
    assert engine.charger_stop_request >= 1


MINUTES = [START + MINUTE * index for index in range(3)]  # the crons inside one average window


# --- a charger that draws power while CJP has no rule, and it is legitimate: not stopped --------------------------------


def test_a_kw_charger_just_stopped_is_given_its_window() -> None:
    """A stop reached the charger a minute ago: the charger still gives power and the average holds the charging."""
    engine = _started()
    engine.charger_stopped_at = START - MINUTE
    results = _run(engine, engine.charging_without_rule, MINUTES, state="7.4", unit="kW")
    assert [result.value for result in results] == [False, False, False]
    assert engine.charging_no_rule_count == 0
    assert engine.charger_stop_request == 0

    # the window is over: counted again
    _run(engine, engine.charging_without_rule, [START + CRON], state="7.4", unit="kW")
    assert engine.charging_no_rule_count == 1


def test_a_kw_charger_released_from_the_charger_test_is_given_its_window() -> None:
    """The test of the charger automation ends and restores the plan's action: the charger still draws the test's power.
    Within the average's window nothing is counted; after it, the detector works again."""
    engine = _started()
    engine._charger_test_released_at = START - 2 * MINUTE
    results = _run(engine, engine.charging_without_rule, MINUTES, state="7.4", unit="kW")
    assert [result.value for result in results] == [False, False, False]
    assert engine.charging_no_rule_count == 0

    _run(engine, engine.charging_without_rule, [START + CRON], state="7.4", unit="kW")
    assert engine.charging_no_rule_count == 1


def test_a_kw_charger_is_not_stopped_while_the_fail_safe_after_the_restarts_runs() -> None:
    engine = _started()
    engine._charger_restart_gave_up = True
    before = engine.charger_action
    results = _run(engine, engine.charging_without_rule, _crons(6), state="7.4", unit="kW")
    assert all(result.value is False for result in results)
    assert engine.charging_no_rule_count == 0
    assert engine.charger_action == before
    assert engine.charger_stop_request == 0


def test_a_kw_charger_under_the_charger_test_hold_is_not_touched() -> None:
    """The test holds the charger: the plan is not calculated, so the detector is not reached at all."""
    engine = _started()
    engine._charger_hold = {"kind": "charge", "amps": [6, 6, 6], "until": START + datetime.timedelta(hours=1), "since": START, "site_stop": False}
    before = engine.charger_action
    results = _run(engine, engine.charge_if_needed, _crons(6), state="7.4", unit="kW", args=(True,))
    assert all(result.error is None for result in results)
    assert engine.charging_no_rule_count == 0
    assert engine.charger_stop_request == 0
    assert engine.charger_action == before


def test_a_kw_charger_on_manual_charging_is_not_counted_as_without_a_rule() -> None:
    """Manual charging is a rule of its own: the plan's branch charges, the detector is not reached."""
    engine = _started()
    results = _run(
        engine, engine.charge_if_needed, _crons(6), state="7.4", unit="kW", settings={"allow_manual_charging_now": True}, args=(True,),
    )
    assert all(result.error is None for result in results)
    assert engine.charger_action == "charge"
    assert engine.charging_no_rule_count == 0
    assert engine.charger_stop_request == 0


# --- an mW sensor, a unit that is no power, a sensor that lost its unit: no false alarm -----------------------------------


def test_a_milliwatt_standby_is_no_charging_though_its_number_is_above_the_voltage() -> None:
    """The car has just stopped: the window still holds 7.4 kW, now the charger stands by at 300 mW (= 0.3 W).
    Read raw, the 300 is above the voltage (230) and the detector would count it."""
    engine = _started()
    history = lambda at: [(at - 4 * MINUTE, "7400000"), (at - MINUTE, "300")]  # noqa: E731
    results = _run(engine, engine.charging_without_rule, _crons(6), state="300", unit="mW", history=history)
    assert all(result.value is False for result in results)
    assert engine.charging_no_rule_count == 0
    assert engine.charger_stop_request == 0


@pytest.mark.parametrize("unit", ["A", "kVA", "%"])
def test_a_value_in_a_unit_that_is_no_power_is_no_reading(unit: str) -> None:
    engine = _started()
    results = _run(engine, engine.charging_without_rule, _crons(6), state="7000", unit=unit)
    assert all(result.value is False for result in results)
    assert engine.charging_no_rule_count == 0
    assert engine.charger_stop_request == 0


def test_an_unavailable_sensor_that_lost_its_unit_is_no_reading() -> None:
    """Its last value was 7400000 (mW): without the unit it can't be read, so it does not trigger."""
    engine = _started()
    history = lambda at: [(at - 10 * MINUTE, "7400000")]  # noqa: E731
    results = _run(engine, engine.charging_without_rule, _crons(6), state="unavailable", unit=None, history=history)
    assert all(result.value is False for result in results)
    assert engine.charging_no_rule_count == 0


def test_an_unavailable_kw_sensor_reads_its_last_value_in_kw() -> None:
    """The unit is kept while the sensor is unavailable: the last value (7.4) is 7.4 kW, and it counts."""
    engine = _started()
    history = lambda at: [(at - 10 * MINUTE, "7.4")]  # noqa: E731
    results = _run(engine, engine.charging_without_rule, _crons(5), state="unavailable", unit="kW", history=history)
    assert [result.value for result in results] == [False, False, False, False, True]


# --- the idle check of the restart reads the unit too ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "unit", "expected"),
    [("0", "kW", True), ("7.4", "kW", False), ("300", "mW", True), ("7400000", "mW", False), ("7000", "A", None), ("7000", "VA", None)],
)
def test_the_idle_check_of_the_restart_reads_the_unit(state: str, unit: str, expected: bool | None) -> None:
    engine = _started()
    result = engine.run_job(_snap(START, state=state, unit=unit), engine._charger_power_idle)
    assert result.error is None, result.error
    assert result.value is expected


# --- the charger test's reading of the power (coordinator) ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "unit", "expected"),
    [
        ("7.4", "kW", 7400.0), ("7400000", "mW", 7400.0), ("0.0074", "MW", 7400.0), ("7.4", "kWh", 7400.0),
        ("-7.4", "kW", 7400.0), ("7400", "W", 7400.0), ("7400", None, 7400.0), ("7000", "A", None), ("7000", "%", None),
    ],
)
async def test_the_charger_test_reads_the_power_in_watt_or_not_at_all(hass: HomeAssistant, state: str, unit: str | None, expected: float | None) -> None:
    attributes = {} if unit is None else {"unit_of_measurement": unit}
    hass.states.async_set(CHARGER, state, attributes)
    value = CoordinatorChargerTest._charger_power(SimpleNamespace(hass=hass), CHARGER)
    if expected is None:
        assert value is None
    else:
        assert value == pytest.approx(expected)
