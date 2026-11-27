"""Suggest the solar and battery power sensors from the Energy dashboard of Home Assistant (nothing is saved here).

The dashboard's sources are read through the energy manager API only; its storage file is never opened. A suggestion is
made only when the answer is unambiguous: exactly one source of the type, a power sensor that exists in Home Assistant
and has a unit the integration reads as a power (the same rule as flow/detection.py), and - with ``skip_used`` - no
other config entry of the same integration already uses that sensor for the same field. Anything else gives no
suggestion. An error or an unexpected shape of the sources (not a list, a source that is not a mapping) gives no
suggestions at all; an unexpected value of one source only gives no suggestion for its own field.

Battery power: a plain ``stat_rate`` in ``power_config`` is used as it is. For an inverted or a two-sensor setup the
top-level ``stat_rate`` (the sensor Home Assistant generates for it) is used, and only when it exists. A ``power_config``
that is not a mapping, or a mapping with none of these keys (or with a key whose value is not a string), gives no battery
suggestion. The sign of the battery power is never taken from the dashboard: the caller reads it from the recorder.

BattMind and Cable Juice Planner have the same file; change both together.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant

from .detection import unit_factor_to_w

_LOGGER = logging.getLogger(__name__)

SOLAR_POWER = "solar_power"  # the solar source's power sensor
BATTERY_POWER = "battery_power"  # the battery source's power sensor


def _power_sensor(hass: HomeAssistant, entity_id: Any) -> bool:
    """A sensor of Home Assistant whose unit is a power unit (kWh and units without a unit are not)."""
    if not isinstance(entity_id, str) or not entity_id.startswith("sensor."):
        return False  # a statistic id such as "tibber:power" is not an entity
    state = hass.states.get(entity_id)
    if state is None:
        return False
    unit = state.attributes.get("unit_of_measurement")
    return isinstance(unit, str) and unit_factor_to_w(unit) is not None


def _sources_of(data: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    """The sources of one type; a source that is not a mapping is an unexpected shape and raises."""
    sources = data.get("energy_sources") or []
    if not isinstance(sources, list) or not all(isinstance(source, Mapping) for source in sources):
        raise ValueError("unexpected energy_sources")
    return [source for source in sources if source.get("type") == kind]


def _battery_sensor(battery: Mapping[str, Any]) -> Any:
    """The power sensor of a battery source, or None when its power configuration is not one this reads."""
    if "power_config" not in battery:
        return battery.get("stat_rate")  # a plain stat_rate of the source
    config = battery["power_config"]
    if not isinstance(config, Mapping):
        return None  # an unexpected shape: no suggestion, and not the top-level stat_rate
    if "stat_rate" in config:
        return config["stat_rate"]
    inverted = isinstance(config.get("stat_rate_inverted"), str)
    two_sensors = isinstance(config.get("stat_rate_from"), str) and isinstance(config.get("stat_rate_to"), str)
    if inverted or two_sensors:
        return battery.get("stat_rate")  # the sensor Home Assistant generates for an inverted or two-sensor setup
    return None  # none of the keys, or one of them is not a string


def _candidates(data: Mapping[str, Any]) -> dict[str, Any]:
    """The power sensor of the one solar source and of the one battery source, before the checks of the states."""
    found: dict[str, Any] = {}
    solar = _sources_of(data, "solar")
    if len(solar) == 1:
        found[SOLAR_POWER] = solar[0].get("stat_rate")
    batteries = _sources_of(data, "battery")
    if len(batteries) == 1:
        found[BATTERY_POWER] = _battery_sensor(batteries[0])
    return found


async def async_energy_suggestions(
    hass: HomeAssistant, domain: str, option_keys: Mapping[str, str], *, skip_used: bool = True
) -> dict[str, str]:
    """The suggested power sensors, by SOLAR_POWER / BATTERY_POWER; only the keys that have a suggestion are returned.

    domain: the integration whose config entries are checked. option_keys: the option that each suggestion key fills in
    that integration's entries; a key missing here gets no suggestion. skip_used: a sensor that another config entry of
    the domain already uses for the same field is not suggested (True), or is suggested anyway (False).
    """
    if "energy" not in hass.config.components:
        return {}
    try:
        from homeassistant.components.energy.data import async_get_manager  # noqa: PLC0415

        manager = await async_get_manager(hass)
        if manager.data is None:
            return {}
        found = _candidates(manager.data)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not read the Energy dashboard for the suggestions: %s", err)
        return {}

    entries = hass.config_entries.async_entries(domain)
    suggestions: dict[str, str] = {}
    for key, entity_id in found.items():
        option_key = option_keys.get(key)
        if option_key is None or not _power_sensor(hass, entity_id):
            continue
        if skip_used and any(entry.options.get(option_key) == entity_id for entry in entries):
            continue  # another config entry already uses this sensor for this field
        suggestions[key] = entity_id
    return suggestions
