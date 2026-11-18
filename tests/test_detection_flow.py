"""The review page of the options flow (home battery): when the suggestions are made, and what they change."""
from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.cable_juice_planner.flow import pages
from custom_components.cable_juice_planner.flow.detection import Detection, Verdict

from .conftest import CONSUMPTION, form_defaults

INVERT = "invert_powerwall_watt_flow_entity_id"
CHARGING = "power_consumption_entity_id_include_powerwall_charging"
DISCHARGING = "power_consumption_entity_id_include_powerwall_discharging"
BATTERY_POWER = "sensor.home_battery_power"
BATTERY_LEVEL = "sensor.home_battery_level"

FOUND = Detection(
    Verdict(True, 2, {"soc_start": 41.2, "soc_end": 63.9, "power_avg": 1840.4}),
    Verdict(False, 1, {"c_charging": 412.0, "c_idle": 389.0, "p_charging": 2100.0}),
    Verdict(None, None, {"c_discharging": 150.0, "c_idle": 389.0, "p_discharging": 900.0}),
)


class Detector:
    """Stands in for the recorder: counts the calls and answers with what the test chose."""

    def __init__(self, answer: Detection = FOUND) -> None:
        self.answer = answer
        self.calls: list[tuple] = []

    async def __call__(self, hass, consumption, power, level, trusted=None, now=None) -> Detection:
        self.calls.append((consumption, power, level, trusted))
        return self.answer


@pytest.fixture
def detector(monkeypatch) -> Detector:
    stand_in = Detector()
    monkeypatch.setattr(pages, "async_detect", stand_in)
    return stand_in


async def _open_home(hass: HomeAssistant, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "menu_home"})
    assert result["step_id"] == "home"
    return await hass.config_entries.options.async_configure(result["flow_id"], form_defaults(result))


async def _save(hass: HomeAssistant, result):
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_defaults(result))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    return result


async def test_a_new_home_battery_is_looked_at_and_the_numbers_are_shown(hass: HomeAssistant, loaded_entry, detector: Detector) -> None:
    hass.states.async_set(BATTERY_POWER, "0", {"unit_of_measurement": "W", "device_class": "power"})
    hass.states.async_set(BATTERY_LEVEL, "50", {"unit_of_measurement": "%", "device_class": "battery"})
    result = await _open_home(hass, loaded_entry)
    assert result["step_id"] == "powerwall"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**form_defaults(result), "powerwall_watt_flow_entity_id": BATTERY_POWER, "powerwall_battery_level_entity_id": BATTERY_LEVEL}
    )

    assert result["step_id"] == "detection_review"
    assert detector.calls == [(CONSUMPTION, BATTERY_POWER, BATTERY_LEVEL, None)]
    placeholders = result["description_placeholders"]
    assert (placeholders["invert_mark"], placeholders["charging_mark"], placeholders["discharging_mark"]) == ("✅", "⬜", "❔")
    assert placeholders["soc_start"] == "41" and placeholders["power_avg"] == "1840" and placeholders["invert_window"] == "2 h"
    defaults = form_defaults(result)
    assert defaults[INVERT] is True and defaults[CHARGING] is False and defaults[DISCHARGING] is False
    await _save(hass, result)
    assert loaded_entry.options[INVERT] is True and "redetect" not in loaded_entry.options


async def test_without_a_home_battery_there_is_nothing_to_suggest(hass: HomeAssistant, loaded_entry, detector: Detector) -> None:
    result = await _open_home(hass, loaded_entry)
    assert result["step_id"] == "powerwall"
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_defaults(result))
    assert result["type"] is FlowResultType.CREATE_ENTRY and detector.calls == []
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_an_unchanged_battery_is_not_looked_at_until_asked(hass: HomeAssistant, loaded_entry, detector: Detector) -> None:
    hass.states.async_set(BATTERY_POWER, "0", {"unit_of_measurement": "W", "device_class": "power"})
    hass.states.async_set(BATTERY_LEVEL, "50", {"unit_of_measurement": "%", "device_class": "battery"})
    hass.config_entries.async_update_entry(
        loaded_entry,
        options={**loaded_entry.options, "powerwall_watt_flow_entity_id": BATTERY_POWER, "powerwall_battery_level_entity_id": BATTERY_LEVEL, INVERT: True, DISCHARGING: True},
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    result = await _open_home(hass, loaded_entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_defaults(result))  # the powerwall page
    assert result["step_id"] == "detection_review" and detector.calls == []
    assert form_defaults(result)[INVERT] is True and form_defaults(result)[DISCHARGING] is True

    # asked: the sign of the stored setting is trusted for the same battery sensor, and what stays unknown keeps its value
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**form_defaults(result), "redetect": True})
    assert detector.calls[-1][3] is True
    defaults = form_defaults(result)
    assert defaults[INVERT] is True and defaults[CHARGING] is False and defaults[DISCHARGING] is True  # discharging: unknown, kept
    await _save(hass, result)
    assert "redetect" not in loaded_entry.options and loaded_entry.options[DISCHARGING] is True
