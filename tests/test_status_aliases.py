"""The charging and done states of the charger blueprint count for CJP's status entity; the built in states always apply."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from homeassistant.components.automation import EVENT_AUTOMATION_RELOADED
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.cable_juice_planner.management.automation_inputs import status_aliases

from .conftest import CHARGER_STATUS, async_setup, async_unload_and_join, make_entry, set_source_states

ACTION = "sensor.cjp_test_charger_action"
BLUEPRINT = "cable_juice_planner/charger_control.yaml"


def _inputs(**inputs) -> dict:
    return {"action_sensor": ACTION, "charger_status": [CHARGER_STATUS], **inputs}


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        # nothing set: the blueprint's own defaults, which the built in states cover
        ([_inputs()], {}),
        ([_inputs(charging_states=["charging", "on"], completed_states=["completed", "finished"])], {}),
        ([_inputs(completed_states=["Finishing", "Charge_Complete"])], {"finishing": "completed"}),
        ([_inputs(charging_states=["Vehicle_Charging"])], {"vehicle_charging": "charging"}),
        ([_inputs(charging_states=["Vehicle_Charging"], completed_states=["Finishing"])], {"vehicle_charging": "charging", "finishing": "completed"}),
        # the entity as a text (older automations) and in another case
        ([_inputs(charger_status=CHARGER_STATUS, completed_states=["Finishing"])], {"finishing": "completed"}),
        # the blueprint's states are for its own status entities: this entity is not one of them
        ([_inputs(charger_status=["sensor.cars_own_status"], completed_states=["Finishing"])], {}),
        ([_inputs(charger_status=[], completed_states=["Finishing"])], {}),
        # a built in state is never moved to another meaning
        ([_inputs(completed_states=["charging", "Ready", "Disconnected", "Awaiting_Start"])], {}),
        ([_inputs(charging_states=["completed", "Plugged"])], {"plugged": "charging"}),
        # no data and a fault are never charging or done, whatever the user lists
        ([_inputs(completed_states=["Unknown", "unavailable", "ERROR", "None"], charging_states=["error", "UNAVAILABLE"])], {}),
        # a state that is both charging and done is left out; one in only one list stays
        ([_inputs(charging_states=["Busy", "Active"], completed_states=["busy"])], {"active": "charging"}),
        ([_inputs(charging_states=["Busy"]), _inputs(completed_states=["BUSY", "Done"])], {"done": "completed"}),
        # more automations: the ones of this entity are added together
        ([_inputs(completed_states=["Finishing"]), _inputs(charger_status=["sensor.other"], completed_states=["Other"]), _inputs(charging_states=["Run"])],
         {"finishing": "completed", "run": "charging"}),
        # values that are no list of text are ignored
        ([_inputs(charging_states=None, completed_states=[1, None, "", " ", ["x"]])], {}),
        ([_inputs(completed_states="Finishing")], {"finishing": "completed"}),
        ([], {}),
    ],
)
def test_status_aliases(inputs: list[dict], expected: dict) -> None:
    assert status_aliases(inputs, CHARGER_STATUS) == expected


def test_without_a_status_entity_there_are_no_aliases() -> None:
    assert status_aliases([_inputs(completed_states=["Finishing"])], "") == {}


@pytest.fixture(autouse=True)
def config_dir(hass: HomeAssistant, tmp_path: Path) -> None:
    """The default test config folder is shared by all test workers: an automations.yaml written there reaches the others."""
    hass.config.config_dir = str(tmp_path)


def _write_automations(hass: HomeAssistant, *automations: dict) -> None:
    content = yaml.safe_dump([{"id": f"a{index}", "alias": "charger", "use_blueprint": {"path": BLUEPRINT, "input": item}} for index, item in enumerate(automations)])
    Path(hass.config.path("automations.yaml")).write_text(content, encoding="utf-8")


@pytest.fixture
async def aliased_entry(hass: HomeAssistant):
    _write_automations(hass, _inputs(charging_states=["Vehicle_Charging"], completed_states=["Finishing"]))
    set_source_states(hass, charger_status="Finishing")
    entry = make_entry()
    await async_setup(hass, entry)
    yield entry
    if entry.state is ConfigEntryState.LOADED:
        await async_unload_and_join(hass, entry)


async def test_the_states_of_the_blueprint_are_read_at_setup(aliased_entry) -> None:
    coordinator = aliased_entry.runtime_data
    assert coordinator.status_aliases == {"vehicle_charging": "charging", "finishing": "completed"}
    assert coordinator.canonical_status("FINISHING") == "completed"
    assert coordinator.canonical_status("vehicle_charging") == "charging"
    assert coordinator.canonical_status("awaiting_start") == "awaiting_start"  # built in states are not touched
    assert coordinator.canonical_status("Complete") == "Complete"  # it is read as built in (const/base.py) without an alias
    assert coordinator.canonical_status(None) is None


async def test_the_status_reaches_the_engine_as_the_built_in_state(hass: HomeAssistant, aliased_entry) -> None:
    coordinator = aliased_entry.runtime_data
    assert (await coordinator._async_snapshot()).states[CHARGER_STATUS] == "completed"
    hass.states.async_set(CHARGER_STATUS, "Vehicle_Charging")
    assert (await coordinator._async_snapshot()).states[CHARGER_STATUS] == "charging"
    # only the status entity is read this way
    set_source_states(hass, charger_status="Vehicle_Charging")
    other = [entity for entity in coordinator.snapshot_entities if entity != CHARGER_STATUS]
    assert other
    snapshot = await coordinator._async_snapshot()
    assert all(snapshot.states.get(entity) == hass.states.get(entity).state for entity in other if entity in snapshot.states)


async def test_the_history_buffer_and_the_status_change_use_the_built_in_state(hass: HomeAssistant, aliased_entry) -> None:
    coordinator = aliased_entry.runtime_data
    hass.states.async_set(CHARGER_STATUS, "Vehicle_Charging")
    await hass.async_block_till_done()
    assert coordinator._buffers[CHARGER_STATUS][-1][1] == "charging"
    calls: list[tuple] = []

    async def record(job, *args, **kwargs):
        calls.append((job, *args))
        return SimpleNamespace(value=None)

    with patch.object(coordinator, "async_run_job", record):
        coordinator._charger_status_changed("Finishing", "Vehicle_Charging")
        await hass.async_block_till_done(wait_background_tasks=True)
        coordinator._charger_status_changed("Finishing", "completed")  # the same state: nothing happened
        await hass.async_block_till_done(wait_background_tasks=True)
    assert calls == [("charger_status_changed", "completed", "charging")]


async def test_the_states_follow_the_automations_when_they_are_reloaded(hass: HomeAssistant, aliased_entry) -> None:
    coordinator = aliased_entry.runtime_data
    hass.states.async_set(CHARGER_STATUS, "Done_Charging")  # not a known state yet: kept as it is
    await hass.async_block_till_done()
    assert coordinator._buffers[CHARGER_STATUS][-1][1] == "Done_Charging"
    _write_automations(hass, _inputs(completed_states=["Done_Charging"]))
    hass.bus.async_fire(EVENT_AUTOMATION_RELOADED)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.status_aliases == {"done_charging": "completed"}
    assert coordinator._buffers[CHARGER_STATUS][-1][1] == "completed"  # the rows already in the buffer too
    Path(hass.config.path("automations.yaml")).write_text("[]\n", encoding="utf-8")
    hass.bus.async_fire(EVENT_AUTOMATION_RELOADED)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.status_aliases == {}


async def test_without_the_automation_only_the_built_in_states_apply(hass: HomeAssistant) -> None:
    set_source_states(hass, charger_status="Finishing")
    entry = make_entry()
    await async_setup(hass, entry)
    try:
        coordinator = entry.runtime_data
        assert coordinator.status_aliases == {}
        assert coordinator.canonical_status("Finishing") == "Finishing"
        assert (await coordinator._async_snapshot()).states[CHARGER_STATUS] == "Finishing"
    finally:
        await async_unload_and_join(hass, entry)


async def test_whether_the_automation_sets_the_cars_limit_is_read_with_the_states(hass: HomeAssistant, aliased_entry) -> None:
    """The departure notice at plug-in: the car's limit is only reported then when the automation cannot set it."""
    coordinator = aliased_entry.runtime_data
    assert coordinator.car_limit_steps is False
    assert (await coordinator._async_snapshot()).car_limit_steps is False
    _write_automations(hass, _inputs(charging_states=["Vehicle_Charging"], completed_states=["Finishing"], car_limit_actions=[{"action": "number.set_value"}]))
    hass.bus.async_fire(EVENT_AUTOMATION_RELOADED)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.car_limit_steps is True  # the states did not change, the steps did
    assert (await coordinator._async_snapshot()).car_limit_steps is True
    Path(hass.config.path("automations.yaml")).write_text("[]\n", encoding="utf-8")
    hass.bus.async_fire(EVENT_AUTOMATION_RELOADED)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.car_limit_steps is None  # no automation found: not known
