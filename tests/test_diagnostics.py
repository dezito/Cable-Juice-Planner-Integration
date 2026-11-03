"""Diagnostics download (diagnostics.py): the anonymised debug data of a car, the settings of a site."""
from __future__ import annotations

import json

from homeassistant.core import HomeAssistant

from custom_components.cable_juice_planner.const import DOMAIN
from custom_components.cable_juice_planner.diagnostics import async_get_config_entry_diagnostics

from .conftest import CONSUMPTION, OBJECT_PREFIX, async_setup, async_unload_and_join, make_site_entry


async def test_car_diagnostics_are_anonymised(hass: HomeAssistant, loaded_entry) -> None:
    hass.config.location_name = "Sommerhuset Bakkebo"
    hass.states.async_set("person.thomas", "home", {"friendly_name": "Thomas Sloth"})

    data = await async_get_config_entry_diagnostics(hass, loaded_entry)
    text = json.dumps(data)

    assert data["integration"] == DOMAIN and data["prefix"] == OBJECT_PREFIX
    assert data["jobs"]
    assert data["databases"] == "<only in the debug export>"
    for secret in (CONSUMPTION, "Thomas", "Sloth", "Bakkebo"):
        assert secret not in text, secret


async def test_site_diagnostics(hass: HomeAssistant, loaded_entry) -> None:
    site = make_site_entry(fuse_l1=25)
    await async_setup(hass, site)

    data = await async_get_config_entry_diagnostics(hass, site)

    assert data["site"] is True
    assert data["options"]["fuse_l1"] == 25
    assert data["cars"] == [OBJECT_PREFIX]
    json.dumps(data)
    await async_unload_and_join(hass, site)
