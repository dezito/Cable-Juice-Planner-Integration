"""A charger that holds the charging back itself (load balancing, smart start, a schedule, an approval) is not restarted."""
from __future__ import annotations

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

import datetime

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.const import CHARGER_HELD_STATUS, CHARGER_READY_STATUS
from custom_components.cable_juice_planner.engine import CJPEngine

from .conftest import CHARGER_STATUS, async_unload_and_join
from .test_engine import DAY, OPTIONS, TEXTS, _loss_engine, _loss_job, snapshot
from .test_is_charging import INTERVAL, _cron, _entry, _pass

HELD = ["awaiting_smart_start", "Awaiting_Load_Balancing", "awaiting_scheduled_start", "Pending_Approval"]


def test_the_held_states_are_ready_states() -> None:
    assert CHARGER_HELD_STATUS and all(state in CHARGER_READY_STATUS for state in CHARGER_HELD_STATUS)


async def _marked_after(hass: HomeAssistant, status: str) -> tuple[bool, int, bool]:
    """The plan says charge at 16 A and the charger shows this status without power, past the grace: marked as not started?"""
    entry = await _entry(hass)  # plugged in (awaiting_start), the plan says charge
    try:
        coordinator = entry.runtime_data
        engine = coordinator.engine
        hass.states.async_set(CHARGER_STATUS, status)
        await hass.async_block_till_done()
        _pass(engine, 60)
        await _cron(coordinator)
        _pass(engine, INTERVAL)
        await _cron(coordinator)
        return engine.charging_is_beginning, engine.restarting_charger_count, engine.restarting_charger
    finally:
        if entry.state is ConfigEntryState.LOADED:
            await async_unload_and_join(hass, entry)


@pytest.mark.parametrize("status", HELD)
async def test_a_held_charger_is_not_marked_or_restarted(hass: HomeAssistant, status: str) -> None:
    assert await _marked_after(hass, status) == (False, 0, False)


@pytest.mark.parametrize("status", ["awaiting_start", "Connected_Requesting", "Waiting for car demand"])
async def test_a_charger_that_only_waits_for_the_car_is_still_restarted(hass: HomeAssistant, status: str) -> None:
    marked, restarts, restarting = await _marked_after(hass, status)
    assert marked or restarts > 0 or restarting  # marked at the first cron past the grace, stopped for the restart at the next


# --- the other places that read the states ----------------------------------------------------------------------------


def _engine_at(status: str):
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    snap = snapshot(datetime.datetime.combine(DAY, datetime.time(10, 30)), status=status, port="on")
    assert engine.run_job(snap, engine.startup, {}).error is None
    return engine, snap


@pytest.mark.parametrize("status", HELD)
def test_a_held_charger_does_not_end_the_charging_loss_measurement(status: str) -> None:
    """charging -> held is a pause by the charger (load balancing, a schedule), not the end of the charging."""
    engine = _loss_engine()
    _loss_job(engine, 1, "switch", True, level=20, kwh=100)
    assert _loss_job(engine, 5, "status", status, "charging", level=50, kwh=120, status=status).value is None
    assert not engine.charging_loss_charging_completed
    assert _loss_job(engine, 6, "status", "completed", "charging", level=80, kwh=148, status="completed").value == "wait_for_meter"


@pytest.mark.parametrize("status", HELD)
def test_the_charger_test_does_not_start_on_a_held_charger(status: str) -> None:
    engine, snap = _engine_at(status)
    begin = engine.run_job(snap, engine.charger_test_begin).value
    assert (begin["ok"], begin["reason"]) == (False, "not_ready")
    held = engine.run_job(snap, engine.charger_test_hold, "charge", 10, 3, 600).value
    assert held["status"] == "not_ready"


def test_the_charger_test_still_starts_on_a_charger_that_waits_for_the_car() -> None:
    engine, snap = _engine_at("awaiting_start")
    assert engine.run_job(snap, engine.charger_test_begin).value["reason"] is None


@pytest.mark.parametrize(
    ("value", "old_value", "plugged_in"),
    [
        ("awaiting_start", "disconnected", True),
        ("Plugged_In", "Unplugged", True),
        ("charging", "no_ev_connected", True),  # Peblar: from no car straight to charging
        ("Charging", "not_connected", True),  # Tesla Wall Connector
        ("completed", "disconnected", True),
        ("charging", "awaiting_start", False),  # already plugged in
        ("completed", "charging", False),
        ("negotiating", "not_connected", False),  # not a state CJP knows: no plug-in yet
    ],
)
def test_a_plug_in_is_any_state_that_shows_a_car_after_one_that_shows_none(value: str, old_value: str, plugged_in: bool) -> None:
    engine, snap = _engine_at("awaiting_start")
    result = engine.run_job(snap, engine.charger_status_changed, value, old_value)
    assert result.error is None, result.error
    assert (result.value == "wait") is plugged_in
