"""Suggestions of the solar and battery power sensors from the Energy dashboard (flow/energy_prefs.py)."""
from __future__ import annotations

import logging
from typing import Any

import pytest

from homeassistant.components.energy.data import async_get_manager
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cable_juice_planner.const import DOMAIN
from custom_components.cable_juice_planner.flow.energy_prefs import BATTERY_POWER, SOLAR_POWER, async_energy_suggestions

OPTION_KEYS = {SOLAR_POWER: "production_entity_id", BATTERY_POWER: "powerwall_watt_flow_entity_id"}
SOLAR = "sensor.energy_solar_power"
BATTERY = "sensor.energy_battery_power"
GENERATED = "sensor.battery_power_generated"
BAT_IN = "sensor.bat_in"  # the sensors of an inverted or a two-sensor battery in the Energy dashboard
BAT_OUT = "sensor.bat_out"
INVERTED = {"stat_rate_inverted": BAT_IN}
TWO_SENSORS = {"stat_rate_from": BAT_OUT, "stat_rate_to": BAT_IN}


async def _suggest(hass: HomeAssistant, sources: Any, **kwargs: Any) -> dict[str, str]:
    """Set the Energy dashboard to the sources (plain dicts on the real manager) and ask for suggestions."""
    hass.config.components.add("energy")
    manager = await async_get_manager(hass)
    manager.data = {"energy_sources": sources, "device_consumption": [], "device_consumption_water": []}
    return await async_energy_suggestions(hass, DOMAIN, OPTION_KEYS, **kwargs)


async def _save_like_the_dashboard(hass: HomeAssistant, monkeypatch, sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Save the sources through Home Assistant's energy manager, as the Energy dashboard does; returns what was stored."""
    hass.config.components.add("energy")
    manager = await async_get_manager(hass)
    # the storage write is delayed by Home Assistant; no timer is left running in the test
    monkeypatch.setattr(manager._store, "async_delay_save", lambda *args, **kwargs: None)
    await manager.async_update({"energy_sources": sources})
    return manager.data


def _sensors(hass: HomeAssistant, *entity_ids: str, unit: str = "W") -> None:
    for entity_id in entity_ids:
        hass.states.async_set(entity_id, "1000", {"unit_of_measurement": unit, "device_class": "power"})


def _solar(stat_rate: Any = SOLAR) -> dict[str, Any]:
    source: dict[str, Any] = {"type": "solar", "stat_energy_from": "sensor.solar_energy"}
    if stat_rate is not None:
        source["stat_rate"] = stat_rate
    return source


def _battery(**extra: Any) -> dict[str, Any]:
    return {"type": "battery", "stat_energy_from": "sensor.battery_in", "stat_energy_to": "sensor.battery_out", **extra}


async def test_plain_stat_rate_of_solar_and_battery(hass: HomeAssistant) -> None:
    _sensors(hass, SOLAR, BATTERY)
    result = await _suggest(hass, [_solar(), _battery(stat_rate=BATTERY)])
    assert result == {SOLAR_POWER: SOLAR, BATTERY_POWER: BATTERY}


async def test_power_config_with_a_plain_stat_rate(hass: HomeAssistant) -> None:
    _sensors(hass, SOLAR, BATTERY)
    result = await _suggest(hass, [_solar(), _battery(power_config={"stat_rate": BATTERY}, stat_rate=BATTERY)])
    assert result[BATTERY_POWER] == BATTERY


@pytest.mark.parametrize("power_config", [INVERTED, TWO_SENSORS])
async def test_inverted_or_two_sensors_use_the_generated_sensor_when_it_exists(hass: HomeAssistant, power_config) -> None:
    _sensors(hass, SOLAR, GENERATED)
    result = await _suggest(hass, [_solar(), _battery(power_config=power_config, stat_rate=GENERATED)])
    assert result == {SOLAR_POWER: SOLAR, BATTERY_POWER: GENERATED}


@pytest.mark.parametrize("power_config", [INVERTED, TWO_SENSORS])
async def test_inverted_or_two_sensors_without_the_generated_sensor_give_no_battery(hass: HomeAssistant, power_config) -> None:
    _sensors(hass, SOLAR, BATTERY)  # the input sensors exist, the generated one does not
    result = await _suggest(hass, [_solar(), _battery(power_config=power_config, stat_rate=GENERATED)])
    assert result == {SOLAR_POWER: SOLAR}


async def test_sources_without_a_power_sensor_give_nothing(hass: HomeAssistant) -> None:
    _sensors(hass, SOLAR, BATTERY)
    result = await _suggest(hass, [_solar(None), _battery(power_config=INVERTED)])
    assert result == {}  # neither source has a stat_rate: the battery's generated sensor is missing too


async def test_two_solar_sources_give_no_solar_suggestion(hass: HomeAssistant) -> None:
    _sensors(hass, SOLAR, "sensor.second_solar_power", BATTERY)
    result = await _suggest(hass, [_solar(), _solar("sensor.second_solar_power"), _battery(stat_rate=BATTERY)])
    assert result == {BATTERY_POWER: BATTERY}


async def test_two_battery_sources_give_no_battery_suggestion(hass: HomeAssistant) -> None:
    _sensors(hass, SOLAR, BATTERY)
    result = await _suggest(hass, [_solar(), _battery(stat_rate=BATTERY), _battery(stat_rate=BATTERY)])
    assert result == {SOLAR_POWER: SOLAR}


async def test_a_statistic_id_is_not_an_entity(hass: HomeAssistant) -> None:
    _sensors(hass, BATTERY)
    result = await _suggest(hass, [_solar("tibber:solar_power"), _battery(stat_rate=BATTERY)])
    assert result == {BATTERY_POWER: BATTERY}


async def test_an_entity_that_does_not_exist_is_not_suggested(hass: HomeAssistant) -> None:
    _sensors(hass, BATTERY)  # SOLAR has no state
    result = await _suggest(hass, [_solar(), _battery(stat_rate=BATTERY)])
    assert result == {BATTERY_POWER: BATTERY}


@pytest.mark.parametrize("unit", ["kWh", "%", None])
async def test_a_sensor_with_a_unit_that_is_not_a_power_is_not_suggested(hass: HomeAssistant, unit: str | None) -> None:
    _sensors(hass, BATTERY)
    hass.states.async_set(SOLAR, "1", {} if unit is None else {"unit_of_measurement": unit})
    result = await _suggest(hass, [_solar(), _battery(stat_rate=BATTERY)])
    assert result == {BATTERY_POWER: BATTERY}


async def test_kilowatt_is_a_power_unit(hass: HomeAssistant) -> None:
    _sensors(hass, SOLAR, BATTERY, unit="kW")
    result = await _suggest(hass, [_solar(), _battery(stat_rate=BATTERY)])
    assert result == {SOLAR_POWER: SOLAR, BATTERY_POWER: BATTERY}


async def test_an_entity_used_by_another_entry_is_not_suggested(hass: HomeAssistant) -> None:
    """BattMind: one battery per entry, so a sensor another entry uses is left out (skip_used defaults to True)."""
    _sensors(hass, SOLAR, BATTERY)
    other = MockConfigEntry(domain=DOMAIN, title="Other", data={}, options={"powerwall_watt_flow_entity_id": BATTERY})
    other.add_to_hass(hass)
    result = await _suggest(hass, [_solar(), _battery(stat_rate=BATTERY)])
    assert result == {SOLAR_POWER: SOLAR}


async def test_an_entity_used_by_another_entry_is_suggested_when_skip_used_is_false(hass: HomeAssistant) -> None:
    """Cable Juice Planner: the cars of one house share its sensors, so the other entry does not hide the suggestion."""
    _sensors(hass, SOLAR, BATTERY)
    other = MockConfigEntry(domain=DOMAIN, title="Other", data={}, options={"powerwall_watt_flow_entity_id": BATTERY})
    other.add_to_hass(hass)
    result = await _suggest(hass, [_solar(), _battery(stat_rate=BATTERY)], skip_used=False)
    assert result == {SOLAR_POWER: SOLAR, BATTERY_POWER: BATTERY}


async def test_the_same_sensor_used_for_another_field_is_still_suggested(hass: HomeAssistant) -> None:
    _sensors(hass, SOLAR)
    other = MockConfigEntry(domain=DOMAIN, title="Other", data={}, options={"powerwall_watt_flow_entity_id": SOLAR})
    other.add_to_hass(hass)
    result = await _suggest(hass, [_solar()])
    assert result == {SOLAR_POWER: SOLAR}


async def test_no_energy_dashboard_gives_nothing(hass: HomeAssistant) -> None:
    _sensors(hass, SOLAR, BATTERY)
    assert await async_energy_suggestions(hass, DOMAIN, OPTION_KEYS) == {}  # energy is not set up


async def test_manager_without_data_gives_nothing(hass: HomeAssistant) -> None:
    hass.config.components.add("energy")
    manager = await async_get_manager(hass)
    manager.data = None
    assert await async_energy_suggestions(hass, DOMAIN, OPTION_KEYS) == {}


async def test_no_sources_gives_nothing(hass: HomeAssistant) -> None:
    """No sources at all is a normal state, not an error: it is read as an empty list."""
    _sensors(hass, SOLAR)
    assert await _suggest(hass, None) == {}
    assert await _suggest(hass, []) == {}


@pytest.mark.parametrize("sources", [{"type": "solar", "stat_rate": SOLAR}, "solar", 5])
async def test_energy_sources_that_is_not_a_list_gives_nothing(hass: HomeAssistant, sources: Any) -> None:
    _sensors(hass, SOLAR)
    assert await _suggest(hass, sources) == {}


@pytest.mark.parametrize("source", ["not a source", None, 5])
async def test_a_source_that_is_not_a_mapping_gives_nothing(hass: HomeAssistant, source: Any) -> None:
    _sensors(hass, SOLAR)
    assert await _suggest(hass, [source, _solar()]) == {}


async def test_a_stat_rate_that_is_not_a_string_gives_no_suggestion_for_its_field(hass: HomeAssistant) -> None:
    _sensors(hass, SOLAR, BATTERY)
    assert await _suggest(hass, [_solar(123), _battery(stat_rate=BATTERY)]) == {BATTERY_POWER: BATTERY}
    assert await _suggest(hass, [_solar(), _battery(stat_rate=["x"])]) == {SOLAR_POWER: SOLAR}
    assert await _suggest(hass, [_solar(), _battery(power_config={"stat_rate": 5})]) == {SOLAR_POWER: SOLAR}


@pytest.mark.parametrize("power_config", ["sensor.battery_power", ["stat_rate"], None, 5])
async def test_a_power_config_that_is_not_a_mapping_gives_no_battery_suggestion(hass: HomeAssistant, power_config: Any) -> None:
    _sensors(hass, SOLAR, BATTERY)
    # the top-level stat_rate is a sensor that exists, but it is not used when power_config has an unexpected shape
    result = await _suggest(hass, [_solar(), _battery(power_config=power_config, stat_rate=BATTERY)])
    assert result == {SOLAR_POWER: SOLAR}


@pytest.mark.parametrize(
    "power_config",
    [
        {},
        {"stat_rate_from": BAT_OUT},
        {"stat_rate_to": BAT_IN},
        {"unknown": BATTERY},
        {"stat_rate_inverted": None},
        {"stat_rate_inverted": 5},
        {"stat_rate_from": 5, "stat_rate_to": BAT_IN},
        {"stat_rate_from": BAT_OUT, "stat_rate_to": None},
    ],
)
async def test_a_power_config_that_is_not_usable_gives_no_battery_suggestion(hass: HomeAssistant, power_config: dict) -> None:
    _sensors(hass, SOLAR, BATTERY)
    result = await _suggest(hass, [_solar(), _battery(power_config=power_config, stat_rate=BATTERY)])
    assert result == {SOLAR_POWER: SOLAR}


async def test_an_error_while_reading_gives_nothing(hass: HomeAssistant, monkeypatch, caplog) -> None:
    async def broken(hass: HomeAssistant):
        raise RuntimeError("storage is gone")

    monkeypatch.setattr("homeassistant.components.energy.data.async_get_manager", broken)
    hass.config.components.add("energy")
    caplog.set_level(logging.DEBUG)
    assert await async_energy_suggestions(hass, DOMAIN, OPTION_KEYS) == {}
    assert "Could not read the Energy dashboard" in caplog.text


async def test_manager_data_that_is_not_a_mapping_raises_and_gives_nothing(hass: HomeAssistant, caplog) -> None:
    hass.config.components.add("energy")
    manager = await async_get_manager(hass)
    manager.data = ["not", "a", "mapping"]  # .get() raises AttributeError inside the suggestion code
    caplog.set_level(logging.DEBUG)
    assert await async_energy_suggestions(hass, DOMAIN, OPTION_KEYS) == {}
    assert "Could not read the Energy dashboard" in caplog.text


# Home Assistant's own contract: the sources are saved through the energy manager, which generates the battery's stat_rate
# for an inverted or a two-sensor setup. The generated name is read from the saved data, not written out here.


@pytest.mark.parametrize("power_config", [INVERTED, TWO_SENSORS], ids=["inverted", "two_sensors"])
async def test_the_sensor_home_assistant_generates_is_suggested_when_it_exists(hass: HomeAssistant, monkeypatch, power_config) -> None:
    saved = await _save_like_the_dashboard(hass, monkeypatch, [_solar(), _battery(power_config=power_config)])
    generated = saved["energy_sources"][1]["stat_rate"]
    assert isinstance(generated, str) and generated.startswith("sensor.")  # Home Assistant generated a sensor id
    _sensors(hass, SOLAR, generated)
    assert await async_energy_suggestions(hass, DOMAIN, OPTION_KEYS) == {SOLAR_POWER: SOLAR, BATTERY_POWER: generated}


@pytest.mark.parametrize("power_config", [INVERTED, TWO_SENSORS], ids=["inverted", "two_sensors"])
async def test_the_generated_sensor_is_not_suggested_when_it_does_not_exist(hass: HomeAssistant, monkeypatch, power_config) -> None:
    saved = await _save_like_the_dashboard(hass, monkeypatch, [_solar(), _battery(power_config=power_config)])
    generated = saved["energy_sources"][1]["stat_rate"]
    assert generated not in (BAT_IN, BAT_OUT)
    _sensors(hass, SOLAR, BAT_IN, BAT_OUT)  # the input sensors exist, the generated one does not
    assert await async_energy_suggestions(hass, DOMAIN, OPTION_KEYS) == {SOLAR_POWER: SOLAR}


async def test_the_generated_sensor_must_have_a_power_unit(hass: HomeAssistant, monkeypatch) -> None:
    saved = await _save_like_the_dashboard(hass, monkeypatch, [_solar(), _battery(power_config=INVERTED)])
    generated = saved["energy_sources"][1]["stat_rate"]
    _sensors(hass, SOLAR)
    hass.states.async_set(generated, "1", {"unit_of_measurement": "kWh"})
    assert await async_energy_suggestions(hass, DOMAIN, OPTION_KEYS) == {SOLAR_POWER: SOLAR}
