"""Load balancing frees a car's current on a charger status that surely means not charging, however the status is spelt.

SETTLED_STATUS (site.py) is safety sensitive: a status that may also mean charging, an unknown one or none counts as
charging. The states are compared without regard to case, and a state the charger blueprint gives as done counts too.
"""
from __future__ import annotations

import pytest

from homeassistant.core import HomeAssistant

from .test_site import STATUS_A, _live_b, _make_stale, _power_site, _ramped, _result

FREED = [24.0, 24.0, 24.0]
KEPT = [8.0, 8.0, 8.0]


async def _a_idle(hass: HomeAssistant, status: str, **cars_a):
    """a was given 16 A and draws 0 W (past the ramp-up) with this charger status: what b gets."""
    site, cars = await _power_site(hass)
    for name, value in cars_a.items():
        setattr(cars["a"], name, value)
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    _ramped(site, "a")
    hass.states.async_set(STATUS_A, status)
    return site, cars


@pytest.mark.parametrize(
    "status",
    ["completed", "Complete", "COMPLETED", "charge_complete", "Finished", "charging_finished", "Unplugged", "No_EV_Connected", "Disconnected"],
)
async def test_a_status_that_surely_means_not_charging_frees_the_current_in_any_case(hass: HomeAssistant, status: str) -> None:
    site, cars = await _a_idle(hass, status)
    assert _live_b(site, cars) == FREED
    await site.async_stop()


@pytest.mark.parametrize(
    "status",
    ["Charging", "connected_charging", "charging_reduced", "Boosting", "On", "Connected", "Ready", "paused", "suspended", "sleeping", "idle", "occupied", "Error", "Unknown", "unavailable", "Connected_Finished"],
)
async def test_a_status_that_may_mean_charging_frees_nothing(hass: HomeAssistant, status: str) -> None:
    site, cars = await _a_idle(hass, status)
    assert _live_b(site, cars) == KEPT
    await site.async_stop()


async def test_a_state_the_blueprint_gives_as_done_frees_the_current(hass: HomeAssistant) -> None:
    aliases = {"finishing": "completed"}
    site, cars = await _a_idle(hass, "Finishing", canonical_status=lambda value: aliases.get(str(value).strip().lower(), value))
    assert _live_b(site, cars) == FREED
    await site.async_stop()


async def test_the_same_state_frees_nothing_without_the_blueprint(hass: HomeAssistant) -> None:
    site, cars = await _a_idle(hass, "Finishing")
    assert _live_b(site, cars) == KEPT
    await site.async_stop()


async def test_a_done_status_that_has_not_reported_frees_nothing(hass: HomeAssistant) -> None:
    """A lost cloud push keeps the last status: a stale Complete is no proof that the car does not charge."""
    site, cars = await _a_idle(hass, "Complete")
    assert _live_b(site, cars) == FREED
    _make_stale(hass, STATUS_A)
    assert _live_b(site, cars) == KEPT
    await site.async_stop()
