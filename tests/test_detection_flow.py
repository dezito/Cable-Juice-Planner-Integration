"""The review page of the options flow (home battery): when the suggestions are made, and what they change."""
from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.cable_juice_planner.const import CONF_ENTITY_PREFIX, DOMAIN
from custom_components.cable_juice_planner.flow import pages
from custom_components.cable_juice_planner.flow.detection import Detection, Verdict
from custom_components.cable_juice_planner.utils.chargers import PRIORITY_KEY

from .conftest import (
    CHARGER_POWER,
    CONSUMPTION,
    EV_LEVEL,
    EV_ODOMETER,
    EV_RANGE,
    PRICE,
    async_unload_and_join,
    form_defaults,
    make_entry,
)
from .test_config_flow import CHARGER_ENTITIES, FIXTURES, _configure, _start_instance

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
        self.left_out: list[list[str]] = []

    async def __call__(self, hass, consumption, power, level, trusted=None, now=None, ignored_entities=()) -> Detection:
        self.calls.append((consumption, power, level, trusted))
        self.left_out.append(list(ignored_entities))
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


# --- the config flow of a new car, an import, a restored backup --------------------------------------------------------


def _battery_states(hass: HomeAssistant) -> None:
    """Present, so the entry made at the end of a flow does not wait two minutes for a missing sensor."""
    hass.states.async_set(BATTERY_POWER, "0", {"unit_of_measurement": "W", "device_class": "power"})
    hass.states.async_set(BATTERY_LEVEL, "50", {"unit_of_measurement": "%", "device_class": "battery"})


async def _new_car_to_the_battery_page(hass: HomeAssistant):
    result = await _start_instance(hass)
    result = await _configure(hass, result, "charger", CHARGER_ENTITIES)
    result = await _configure(hass, result, "charger_specs", {"power_voltage": 230, "charging_phases": 3, "charging_max_amp": 16, "charging_loss": -5})
    result = await _configure(
        hass, result, "ev", {"usable_battery_level_entity_id": EV_LEVEL, "estimated_battery_range_entity_id": EV_RANGE, "odometer_entity_id": EV_ODOMETER}
    )
    result = await _configure(hass, result, "ev_specs", {"battery_size": 75, "last_charging_hour_before_departure_buffer": 1})
    result = await _configure(hass, result, "home", {"power_consumption_entity_id": CONSUMPTION, "ignore_consumption_from_entity_ids": ["sensor.pool_pump"]})
    assert result["step_id"] == "powerwall"
    return result


async def _to_the_end(hass: HomeAssistant, result):
    while result["type"] is FlowResultType.FORM:
        answer = form_defaults(result)
        if result["step_id"] == "prices_energidataservice":
            answer["energidataservice_buy_entity_id"] = PRICE
        if result["step_id"] == "advanced":
            answer["dashboard"] = False
        result = await hass.config_entries.flow.async_configure(result["flow_id"], answer)
    return result


async def test_a_new_car_with_a_home_battery_gets_the_suggestions_and_the_left_out_consumers_are_given(hass: HomeAssistant, detector: Detector) -> None:
    _battery_states(hass)
    result = await _new_car_to_the_battery_page(hass)
    result = await _configure(hass, result, "powerwall", {"powerwall_watt_flow_entity_id": BATTERY_POWER, "powerwall_battery_level_entity_id": BATTERY_LEVEL})

    assert result["step_id"] == "detection_review"
    assert detector.calls == [(CONSUMPTION, BATTERY_POWER, BATTERY_LEVEL, None)]
    assert detector.left_out == [["sensor.pool_pump", CHARGER_POWER]]  # what the planner leaves out of the house consumption: the ignored and this charger
    result = await _to_the_end(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][INVERT] is True and result["options"][CHARGING] is False
    assert "redetect" not in result["options"]


async def test_a_power_sensor_without_a_level_sensor_is_not_looked_at(hass: HomeAssistant, detector: Detector) -> None:
    _battery_states(hass)
    result = await _new_car_to_the_battery_page(hass)
    result = await _configure(hass, result, "powerwall", {"powerwall_watt_flow_entity_id": BATTERY_POWER})

    assert result["step_id"] == "detection_review" and detector.calls == []
    assert {value for key, value in result["description_placeholders"].items() if key.endswith("mark")} == {"❔"}
    assert not result.get("errors")
    result = await _to_the_end(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["options"][INVERT] is False


async def test_without_a_recorder_the_page_says_so_and_keeps_the_values(hass: HomeAssistant) -> None:
    """The real lookup (not the stand-in) in a Home Assistant without a recorder."""
    _battery_states(hass)
    hass.states.async_set(CONSUMPTION, "0", {"unit_of_measurement": "W", "device_class": "power"})
    result = await _new_car_to_the_battery_page(hass)
    result = await _configure(hass, result, "powerwall", {"powerwall_watt_flow_entity_id": BATTERY_POWER, "powerwall_battery_level_entity_id": BATTERY_LEVEL})

    assert result["step_id"] == "detection_review"
    assert result["errors"] == {"base": "detection_recorder"}
    assert (form_defaults(result)[INVERT], form_defaults(result)[CHARGING], form_defaults(result)[DISCHARGING]) == (False, False, False)
    result = await _to_the_end(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_a_failed_lookup_says_why_and_is_looked_at_again_next_time(hass: HomeAssistant, detector: Detector) -> None:
    detector.answer = Detection(reason="timeout")
    result = await _new_car_to_the_battery_page(hass)
    result = await _configure(hass, result, "powerwall", {"powerwall_watt_flow_entity_id": BATTERY_POWER, "powerwall_battery_level_entity_id": BATTERY_LEVEL})

    assert result["errors"] == {"base": "detection_timeout"}
    assert hass.config_entries.flow._progress[result["flow_id"]]._detections == {}
    detector.answer = FOUND
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**form_defaults(result), "redetect": True})
    assert not result.get("errors") and form_defaults(result)[INVERT] is True
    assert len(hass.config_entries.flow._progress[result["flow_id"]]._detections) == 1
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_an_import_from_pyscript_keeps_its_battery_settings_and_looks_at_nothing(hass: HomeAssistant, detector: Detector, tmp_path: Path) -> None:
    _battery_states(hass)
    text = (FIXTURES / "ev_config.yaml").read_bytes().decode("utf-8")
    for old, new in (
        ('powerwall_battery_level_entity_id: ""', f"powerwall_battery_level_entity_id: {BATTERY_LEVEL}"),
        ('powerwall_watt_flow_entity_id: ""', f"powerwall_watt_flow_entity_id: {BATTERY_POWER}"),
        ("invert_powerwall_watt_flow_entity_id: false", "invert_powerwall_watt_flow_entity_id: true"),
    ):
        assert text.count(old) == 1
        text = text.replace(old, new)
    (tmp_path / "ev_config.yaml").write_bytes(text.encode("utf-8"))
    hass.config.config_dir = str(tmp_path)

    result = await _start_instance(hass, prefix="ev")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"import_source": "ev"})
    while result["step_id"] != "detection_review":
        result = await hass.config_entries.flow.async_configure(result["flow_id"], form_defaults(result))

    assert detector.calls == []
    assert form_defaults(result)[INVERT] is True
    result = await _to_the_end(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["options"][INVERT] is True and detector.calls == []


async def test_a_restored_backup_keeps_its_battery_settings_and_looks_at_nothing(hass: HomeAssistant, loaded_entry, detector: Detector, tmp_path: Path) -> None:
    hass.config.config_dir = str(tmp_path)
    for entity_id in (BATTERY_POWER, BATTERY_LEVEL):
        hass.states.async_set(entity_id, "0", {"unit_of_measurement": "W" if "power" in entity_id else "%"})
    hass.config_entries.async_update_entry(
        loaded_entry,
        options={**loaded_entry.options, "powerwall_watt_flow_entity_id": BATTERY_POWER, "powerwall_battery_level_entity_id": BATTERY_LEVEL, INVERT: True, DISCHARGING: True},
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = loaded_entry.runtime_data
    await hass.config_entries.async_remove(loaded_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_add_executor_job(coordinator._executor.shutdown, True)
    backups = [path.name for path in tmp_path.rglob("*_deleted.json")]
    assert len(backups) == 1

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "instance"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ENTITY_PREFIX: "restored", "language": "da"})
    assert result["step_id"] == "restore_backup"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"backup": backups[0]})
    while result["step_id"] != "detection_review":
        result = await hass.config_entries.flow.async_configure(result["flow_id"], form_defaults(result))

    assert detector.calls == []
    assert form_defaults(result)[INVERT] is True and form_defaults(result)[DISCHARGING] is True
    result = await _to_the_end(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["options"][INVERT] is True
    await hass.async_block_till_done(wait_background_tasks=True)
    entry = result["result"]
    assert entry.state is ConfigEntryState.LOADED
    await async_unload_and_join(hass, entry)


async def test_the_chargers_of_the_cars_behind_are_given_to_the_detector(hass: HomeAssistant, loaded_entry, detector: Detector) -> None:
    """A car leaves out the charging of the cars BEHIND it (a higher priority number) and its own charger; the cars ahead stay in the house."""
    make_entry("zoe", {**loaded_entry.options, "charger_power_entity_id": "sensor.zoe_power", PRIORITY_KEY: 2}).add_to_hass(hass)
    hass.states.async_set(BATTERY_POWER, "0", {"unit_of_measurement": "W", "device_class": "power"})
    hass.states.async_set(BATTERY_LEVEL, "50", {"unit_of_measurement": "%", "device_class": "battery"})
    result = await _open_home(hass, loaded_entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**form_defaults(result), "powerwall_watt_flow_entity_id": BATTERY_POWER, "powerwall_battery_level_entity_id": BATTERY_LEVEL}
    )
    assert result["step_id"] == "detection_review"

    assert detector.left_out[-1] == [CHARGER_POWER, "sensor.zoe_power"]
    hass.config_entries.options.async_abort(result["flow_id"])


@pytest.mark.parametrize(
    ("other_priority", "switch"),
    [
        (2, False),  # the planner does not leave the other chargers out when the option is off
        (1, True),  # the same priority: nobody leaves anybody out
        (0.5, True),  # a car AHEAD of this one is not left out, only the cars behind it
    ],
)
async def test_only_the_chargers_the_planner_leaves_out_are_given(hass: HomeAssistant, loaded_entry, detector: Detector, other_priority, switch) -> None:
    make_entry("zoe", {**loaded_entry.options, "charger_power_entity_id": "sensor.zoe_power", PRIORITY_KEY: other_priority}).add_to_hass(hass)
    hass.states.async_set(BATTERY_POWER, "0", {"unit_of_measurement": "W", "device_class": "power"})
    hass.states.async_set(BATTERY_LEVEL, "50", {"unit_of_measurement": "%", "device_class": "battery"})
    hass.config_entries.async_update_entry(loaded_entry, options={**loaded_entry.options, "ignore_cjp_chargers": switch})
    await hass.async_block_till_done(wait_background_tasks=True)
    result = await _open_home(hass, loaded_entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**form_defaults(result), "powerwall_watt_flow_entity_id": BATTERY_POWER, "powerwall_battery_level_entity_id": BATTERY_LEVEL}
    )

    assert detector.left_out[-1] == [CHARGER_POWER]  # only this car's own charger
    hass.config_entries.options.async_abort(result["flow_id"])


async def test_chargers_on_their_own_meter_are_not_given_to_the_detector(hass: HomeAssistant, loaded_entry, detector: Detector) -> None:
    """The house sensor does not include them, so there is nothing to leave out of what it reads: neither this car's own
    charger nor one of a car behind."""
    other = make_entry("zoe", {**loaded_entry.options, "charger_power_entity_id": "sensor.zoe_power", PRIORITY_KEY: 2, "charger_in_house_meter": False})
    other.add_to_hass(hass)
    hass.states.async_set(BATTERY_POWER, "0", {"unit_of_measurement": "W", "device_class": "power"})
    hass.states.async_set(BATTERY_LEVEL, "50", {"unit_of_measurement": "%", "device_class": "battery"})
    hass.config_entries.async_update_entry(loaded_entry, options={**loaded_entry.options, "charger_in_house_meter": False})
    await hass.async_block_till_done(wait_background_tasks=True)
    result = await _open_home(hass, loaded_entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**form_defaults(result), "powerwall_watt_flow_entity_id": BATTERY_POWER, "powerwall_battery_level_entity_id": BATTERY_LEVEL}
    )
    assert result["step_id"] == "detection_review"

    assert detector.left_out[-1] == []
    hass.config_entries.options.async_abort(result["flow_id"])


async def test_the_trusted_sign_is_the_stored_one_not_what_the_form_says_now(hass: HomeAssistant, loaded_entry, detector: Detector) -> None:
    hass.states.async_set(BATTERY_POWER, "0", {"unit_of_measurement": "W", "device_class": "power"})
    hass.states.async_set(BATTERY_LEVEL, "50", {"unit_of_measurement": "%", "device_class": "battery"})
    hass.config_entries.async_update_entry(
        loaded_entry, options={**loaded_entry.options, "powerwall_watt_flow_entity_id": BATTERY_POWER, "powerwall_battery_level_entity_id": BATTERY_LEVEL, INVERT: True}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    result = await _open_home(hass, loaded_entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_defaults(result))
    assert result["step_id"] == "detection_review"

    # the user flips the sign by hand and asks again: that guess is not what the history is read with
    await hass.config_entries.options.async_configure(result["flow_id"], {**form_defaults(result), INVERT: False, "redetect": True})

    assert detector.calls[-1][3] is True
    hass.config_entries.options.async_abort(result["flow_id"])


@pytest.mark.parametrize("reason", ["unit", "data", "recorder", "error"])
async def test_every_reason_is_said_on_the_page_in_the_options_flow(hass: HomeAssistant, loaded_entry, detector: Detector, reason: str) -> None:
    detector.answer = Detection(reason=reason)
    hass.states.async_set(BATTERY_POWER, "0", {"unit_of_measurement": "W", "device_class": "power"})
    hass.states.async_set(BATTERY_LEVEL, "50", {"unit_of_measurement": "%", "device_class": "battery"})
    result = await _open_home(hass, loaded_entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**form_defaults(result), "powerwall_watt_flow_entity_id": BATTERY_POWER, "powerwall_battery_level_entity_id": BATTERY_LEVEL}
    )

    assert result["step_id"] == "detection_review" and result["errors"] == {"base": f"detection_{reason}"}
    hass.config_entries.options.async_abort(result["flow_id"])
