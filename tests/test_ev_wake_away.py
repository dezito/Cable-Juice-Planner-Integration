"""The car is never woken while it is away from home, and charger and cable states are matched without regard to case.

Issue 143: the car was in another zone, the cable was unplugged and the charger (ESPHome Tesla BLE) reported "Complete".
That status was not known, so ready_to_charge() skipped its "car not at home" check and woke the car every 15 minutes.
"""
from __future__ import annotations

import datetime

import pytest

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.const import (
    CHARGER_CHARGING_STATUS,
    CHARGER_COMPLETED_STATUS,
    CHARGER_NOT_READY_STATUS,
    CHARGER_READY_STATUS,
    EV_PLUGGED_STATES,
    EV_UNPLUGGED_STATES,
    StateSet,
)
from custom_components.cable_juice_planner.engine import CJPEngine

from .test_engine import DAY, OPTIONS, TEXTS, snapshot

CABLE = "binary_sensor.ev_cable"
CAR_OPTIONS = {**OPTIONS, "charge_cable_entity_id": CABLE}


def _ready_to_charge(*, location: str, cable: str, status: str = "disconnected", port: str = "off"):
    engine = CJPEngine(const.build_config(CAR_OPTIONS), "cjp_test", TEXTS)
    snap = snapshot(datetime.datetime.combine(DAY, datetime.time(10, 30)), status=status, port=port)
    snap.states["device_tracker.ev"] = location
    snap.states[CABLE] = cable
    result = engine.run_job(snap, engine.ready_to_charge)
    assert result.error is None, result.error
    wakes = [event for event in result.events if event[0] == "wake_up"]
    return result, wakes


@pytest.mark.parametrize("status", ["Complete", "complete", "Completed", "Charging", "Ready", "Stopped", "NoPower"])
def test_away_with_the_cable_unplugged_does_not_wake_the_car(status: str) -> None:
    result, wakes = _ready_to_charge(location="Systematic", cable="off", status=status)
    assert result.value is None
    assert not wakes
    rule = result.states["current_charging_rule"]
    assert rule.startswith("⛔")
    assert TEXTS["ui.ready_to_charge.cable_connected_not_to_ev"] not in rule


@pytest.mark.parametrize("cable", ["unavailable", "unknown"])
def test_away_with_an_unavailable_cable_does_not_wake_the_car(cable: str) -> None:
    result, wakes = _ready_to_charge(location="not_home", cable=cable, status="Complete")
    assert result.value is None
    assert not wakes


def test_away_with_the_port_closed_does_not_wake_the_car() -> None:
    result, wakes = _ready_to_charge(location="Systematic", cable="on", status="Complete", port="off")
    assert result.value is None
    assert not wakes


def test_away_says_so_not_that_the_cable_is_missing() -> None:
    """A status CJP does not know and an unplugged cable: the car is not at home, the cable is not "connected"."""
    result, _ = _ready_to_charge(location="Systematic", cable="off", status="Stopped")
    assert TEXTS["ui.ready_to_charge.ev_not_home"] in result.states["current_charging_rule"]
    assert TEXTS["ui.ready_to_charge.cable_connected_ev_not_home"] not in result.states["current_charging_rule"]


def test_away_with_the_cable_plugged_in_says_the_cable_is_connected() -> None:
    result, wakes = _ready_to_charge(location="Systematic", cable="on", status="awaiting_start")
    assert result.value is None
    assert not wakes
    assert TEXTS["ui.ready_to_charge.cable_connected_ev_not_home"] in result.states["current_charging_rule"]


def test_home_with_the_cable_unplugged_still_wakes_the_car() -> None:
    result, wakes = _ready_to_charge(location="home", cable="off", status="Complete")
    assert result.value is True
    assert wakes
    assert TEXTS["ui.ready_to_charge.cable_connected_not_to_ev"] in result.states["current_charging_rule"]


def test_home_with_an_unavailable_cable_still_wakes_the_car() -> None:
    _, wakes = _ready_to_charge(location="home", cable="unavailable", status="awaiting_start")
    assert wakes


@pytest.mark.parametrize(
    ("states", "state", "expected"),
    [
        (CHARGER_COMPLETED_STATUS, "Complete", True),
        (CHARGER_COMPLETED_STATUS, "COMPLETED", True),
        (CHARGER_COMPLETED_STATUS, " finished ", True),
        (CHARGER_COMPLETED_STATUS, "charge_complete", True),
        (CHARGER_COMPLETED_STATUS, "charging", False),
        (CHARGER_CHARGING_STATUS, "Charging", True),
        (CHARGER_READY_STATUS, "Awaiting_Start", True),
        (CHARGER_NOT_READY_STATUS, "Disconnected", True),
        (EV_PLUGGED_STATES, "On", True),
        (EV_UNPLUGGED_STATES, "Off", True),
        (EV_PLUGGED_STATES, "Off", False),
        # the states of the charger integrations (const/base.py)
        (CHARGER_READY_STATUS, "Plugged_In", True),  # Ohme
        (CHARGER_READY_STATUS, "Pending_Approval", True),  # Ohme
        (CHARGER_READY_STATUS, "connected_requesting", True),  # Zaptec
        (CHARGER_READY_STATUS, "Awaiting_Smart_Start", True),  # Easee
        (CHARGER_READY_STATUS, "Waiting for car demand", True),  # Wallbox
        (CHARGER_READY_STATUS, "EV Connected", True),  # myenergi
        (CHARGER_NOT_READY_STATUS, "Unplugged", True),  # Ohme
        (CHARGER_NOT_READY_STATUS, "not_connected", True),  # OpenEVSE, Tesla Wall Connector
        (CHARGER_NOT_READY_STATUS, "no_ev_connected", True),  # Peblar
        (CHARGER_NOT_READY_STATUS, "EV Disconnected", True),  # myenergi
        (CHARGER_COMPLETED_STATUS, "charging_finished", True),  # Tesla Wall Connector
        (CHARGER_COMPLETED_STATUS, "Connected_Finished", True),  # Zaptec
        (CHARGER_CHARGING_STATUS, "connected_charging", True),  # Zaptec
        (CHARGER_CHARGING_STATUS, "Charging_Reduced", True),  # Tesla Wall Connector
        (CHARGER_CHARGING_STATUS, "Boosting", True),  # myenergi
        # one state is one thing: no state is in two sets
        (CHARGER_READY_STATUS, "charging", False),
        (CHARGER_NOT_READY_STATUS, "plugged_in", False),
    ],
)
def test_status_states_ignore_case(states: StateSet, state: str, expected: bool) -> None:
    assert (state in states) is expected


@pytest.mark.parametrize("state", [None, 0, 1.5, "", "unknown"])
def test_status_states_never_match_a_missing_value(state) -> None:
    for states in (CHARGER_READY_STATUS, CHARGER_NOT_READY_STATUS, CHARGER_COMPLETED_STATUS, CHARGER_CHARGING_STATUS, EV_PLUGGED_STATES, EV_UNPLUGGED_STATES):
        assert state not in states


@pytest.mark.parametrize(
    "state",
    # may mean either, or a fault or no data: no class (CJP then does not know the status, which is the honest answer)
    ["paused", "Suspended", "sleeping", "scheduled", "idle", "Occupied", "Preparing", "SuspendedEV", "Finishing", "Available", "error", "Faulted", "Discharging", "locked", "waiting"],
)
def test_a_state_that_can_mean_either_is_in_no_set(state: str) -> None:
    for states in (CHARGER_READY_STATUS, CHARGER_NOT_READY_STATUS, CHARGER_COMPLETED_STATUS, CHARGER_CHARGING_STATUS):
        assert state not in states


def test_no_state_is_in_two_charger_sets() -> None:
    sets = (CHARGER_READY_STATUS, CHARGER_NOT_READY_STATUS, CHARGER_COMPLETED_STATUS, CHARGER_CHARGING_STATUS)
    every = [state for states in sets for state in states]
    assert len(every) == len(set(every))


def test_status_states_survive_a_copy() -> None:
    import copy
    import pickle

    for states in (CHARGER_COMPLETED_STATUS, EV_PLUGGED_STATES):
        for copied in (copy.copy(states), copy.deepcopy(states), pickle.loads(pickle.dumps(states))):
            assert isinstance(copied, StateSet) and tuple(copied) == tuple(states)
            assert states[0].upper() in copied


@pytest.mark.parametrize("location", ["home", "Home", "HOME"])
def test_home_is_home_whatever_the_case(location: str) -> None:
    engine = CJPEngine(const.build_config(CAR_OPTIONS), "cjp_test", TEXTS)
    snap = snapshot(datetime.datetime.combine(DAY, datetime.time(10, 30)), status="awaiting_start")
    snap.states["device_tracker.ev"] = location
    for job, expected in ((engine.is_ev_home, True), (engine._is_home, True), (engine._is_not_home, False)):
        result = engine.run_job(snap, job)
        assert result.error is None and result.value is expected, job.__name__


def test_another_zone_is_not_home_for_the_planner_and_the_public_charging() -> None:
    engine = CJPEngine(const.build_config(CAR_OPTIONS), "cjp_test", TEXTS)
    snap = snapshot(datetime.datetime.combine(DAY, datetime.time(10, 30)), status="awaiting_start")
    snap.states["device_tracker.ev"] = "Systematic"
    for job, expected in ((engine.is_ev_home, False), (engine._is_home, False), (engine._is_not_home, True)):
        assert engine.run_job(snap, job).value is expected, job.__name__


def test_away_without_a_status_entity_does_not_say_the_cable_is_connected() -> None:
    """Without a status entity the status reads "connected"; that is no sign of a plugged in cable."""
    options = {**CAR_OPTIONS, "status_entity_id": ""}
    engine = CJPEngine(const.build_config(options), "cjp_test", TEXTS)
    snap = snapshot(datetime.datetime.combine(DAY, datetime.time(10, 30)))
    snap.states.pop("sensor.charger_status")
    snap.states["device_tracker.ev"] = "Systematic"
    snap.states[CABLE] = "off"
    result = engine.run_job(snap, engine.ready_to_charge)
    assert result.error is None and result.value is None
    assert TEXTS["ui.ready_to_charge.ev_not_home"] in result.states["current_charging_rule"]
    assert TEXTS["ui.ready_to_charge.cable_connected_ev_not_home"] not in result.states["current_charging_rule"]
    assert not [event for event in result.events if event[0] == "wake_up"]


def test_status_states_keep_the_rule_when_combined() -> None:
    combined = CHARGER_READY_STATUS + CHARGER_CHARGING_STATUS + CHARGER_COMPLETED_STATUS
    assert isinstance(combined, StateSet)
    assert "Complete" in combined
    assert "DISCONNECTED" not in combined
    assert isinstance(StateSet("A") + ("b",), StateSet)
    assert isinstance(("b",) + StateSet("A"), StateSet)
    assert tuple(StateSet("Complete", " Charging ")) == ("complete", "charging")
