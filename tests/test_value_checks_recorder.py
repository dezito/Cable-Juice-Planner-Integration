"""The recorder part of the value checks (value_checks.async_recorded_numbers) against a real in-memory recorder.

The conftest's autouse fixture starts hass before a recorder can be set up, so this module overrides it and the recorder comes
first. The other tests of the suite fake the recorder (test_value_checks.py, test_health_checks.py).
"""
from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.cable_juice_planner.management.value_checks import async_recorded_numbers


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Overrides the conftest fixture of the same name: the recorder is set up before hass."""


async def test_the_recorded_states_are_read_as_numbers(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.a", "1.5")
    hass.states.async_set("sensor.a", "2.5")
    hass.states.async_set("sensor.a", "unavailable")
    hass.states.async_set("sensor.a", "3")
    hass.states.async_set("sensor.b", "text")
    await async_wait_recording_done(hass)
    assert await async_recorded_numbers(hass, ["sensor.a", "sensor.b"]) == {"sensor.a": [1.5, 2.5, 3.0], "sensor.b": []}
