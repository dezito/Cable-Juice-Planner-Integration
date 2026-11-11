"""What-if Scenario B through Home Assistant: the entities, the button and the result sensor."""
from __future__ import annotations

from unittest.mock import patch

from homeassistant.core import HomeAssistant

from .conftest import OBJECT_PREFIX
from .test_whatif_coordinator import _seed

SENSOR = f"sensor.{OBJECT_PREFIX}_what_if_settings"
BUTTON = f"button.{OBJECT_PREFIX}_run_what_if_settings"
RESERVE = f"number.{OBJECT_PREFIX}_what_if_min_daily_battery_level"
MAXIMUM = f"number.{OBJECT_PREFIX}_what_if_max_recommended_battery_level"


async def _press(hass: HomeAssistant, button: str = BUTTON) -> None:
    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_the_scenario_b_entities_exist(hass: HomeAssistant, loaded_entry) -> None:
    for entity_id in (RESERVE, MAXIMUM, BUTTON, SENSOR):
        assert hass.states.get(entity_id) is not None, entity_id
    assert hass.states.get(RESERVE).state == "0.0" and hass.states.get(MAXIMUM).state == "0.0"  # 0 = the live setting


async def test_the_button_replays_the_alternative_and_publishes_it(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    _seed(coordinator)
    await hass.services.async_call("number", "set_value", {"entity_id": RESERVE, "value": 55}, blocking=True)
    live = (coordinator.settings.get("min_daily_battery_level"), coordinator.settings.get("max_recommended_charge_limit_battery_level"))
    with patch.object(type(coordinator), "_async_what_if_start_soc", return_value={"soc": 55.0, "source": "recorded_state"}):
        await _press(hass)
    state = hass.states.get(SENSOR)
    result = state.attributes["result"]
    assert result["status"] == "ok" and result["provenance"]["start_soc"] == "recorded_state"
    assert result["settings"]["alternative"]["reserve"] == 55.0 and result["windows"]["replayed"] == 2
    assert result["alternative"]["grid_kwh"] > result["baseline"]["grid_kwh"]
    assert state.state == str(result["difference"]["adjusted_cost"])
    assert state.attributes["attempt"]["status"] == "ok"
    # Scenario A's sensor was not touched and the live settings are unchanged
    assert hass.states.get(f"sensor.{OBJECT_PREFIX}_what_if").attributes.get("attempt") is None
    assert (coordinator.settings.get("min_daily_battery_level"), coordinator.settings.get("max_recommended_charge_limit_battery_level")) == live


async def test_without_a_start_level_both_settings_are_calibrated_to_one_level(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    _seed(coordinator)
    await hass.services.async_call("number", "set_value", {"entity_id": MAXIMUM, "value": 60}, blocking=True)
    with patch.object(type(coordinator), "_async_what_if_start_soc", return_value={"soc": None, "source": "unavailable"}):
        await _press(hass)
    attributes = hass.states.get(SENSOR).attributes
    assert attributes["attempt"]["status"] == "ok" and attributes["result"]["calibrated_start"] is True


async def test_both_overrides_at_zero_compare_nothing(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    _seed(coordinator)
    with patch.object(type(coordinator), "_async_what_if_start_soc", return_value={"soc": 55.0, "source": "entered"}):
        await _press(hass)
    attributes = hass.states.get(SENSOR).attributes
    assert attributes["attempt"]["status"] == "unavailable" and attributes["attempt"]["reason"] == "no_change"
    assert attributes["result"] is None


async def test_the_two_scenarios_share_one_running_guard_and_a_crash_never_leaves_it_running(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    _seed(coordinator)
    coordinator._what_if_running = True  # Scenario A is running
    with patch.object(type(coordinator), "_async_what_if_start_soc", side_effect=AssertionError("must not run twice")):
        await _press(hass)
    assert hass.states.get(SENSOR).attributes["attempt"] is None
    coordinator._what_if_running = False
    with patch.object(type(coordinator), "_async_what_if_start_soc", side_effect=RuntimeError("boom")):
        await _press(hass)
    attempt = hass.states.get(SENSOR).attributes["attempt"]
    assert attempt["status"] == "unavailable" and attempt["reason"] == "internal_error"
    assert coordinator._what_if_running is False


async def test_changing_a_scenario_b_setting_does_not_recalculate_the_live_plan(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    for entity_id, value in ((RESERVE, 40), (MAXIMUM, 70)):
        await hass.services.async_call("number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True)
    assert coordinator.settings["what_if_min_daily_battery_level"] == 40
    assert coordinator._settings_debounce is None
