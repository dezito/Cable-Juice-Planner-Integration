"""Load balancing: several cars behind one main fuse (site config entry)."""
from __future__ import annotations

import asyncio
import datetime
import time
from unittest.mock import patch
from types import SimpleNamespace

import pytest

from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.helpers.dispatcher import async_dispatcher_connect

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.const import CONF_CIRCUITS, DOMAIN, SITE_SENSORS
from custom_components.cable_juice_planner.engine import CJPEngine
from custom_components.cable_juice_planner.site import (
    OVERLOAD_THROTTLE,
    RAMP_UP,
    STALE_CHECK_SECONDS,
    STALE_SECONDS,
    CJPSite,
    update_circuits,
)

from .conftest import (
    CHARGER_STATUS,
    MOCK_OPTIONS,
    async_advance,
    async_setup,
    async_unload_and_join,
    make_entry,
    make_site_entry,
    register_charger,
    set_source_states,
)

EV_B_LEVEL = "sensor.ev_b_battery_level"
CHARGER_B_STATUS = "sensor.charger_b_status"


def _amps(hass: HomeAssistant, prefix: str) -> list[int]:
    return [int(hass.states.get(f"sensor.cjp_{prefix}_charger_phase_{i}_amps").state) for i in (1, 2, 3)]


async def _settle(hass: HomeAssistant) -> None:
    """Let the site recalculate the cars behind a changed plan (15 s delay) until nothing changes."""
    for _ in range(4):
        await async_advance(hass, 16)
        await hass.async_block_till_done(wait_background_tasks=True)


async def _setup_two_cars(
    hass: HomeAssistant, b_level: float = 60, circuits: list | None = None
) -> tuple[MockConfigEntry, MockConfigEntry, MockConfigEntry]:
    set_source_states(hass)
    hass.states.async_set(EV_B_LEVEL, str(b_level), {"unit_of_measurement": "%", "device_class": "battery"})
    hass.states.async_set(CHARGER_B_STATUS, "disconnected")
    site = make_site_entry(fuse_l1=25, fuse_l2=25, fuse_l3=25, margin=1, **({CONF_CIRCUITS: circuits} if circuits else {}))
    await async_setup(hass, site)
    car_a = make_entry("a", {**MOCK_OPTIONS, "site_priority": 1})
    car_b = make_entry("b", {**MOCK_OPTIONS, "site_priority": 2, "usable_battery_level_entity_id": EV_B_LEVEL, "status_entity_id": CHARGER_B_STATUS})
    await async_setup(hass, car_a)
    await async_setup(hass, car_b)
    await _settle(hass)
    return site, car_a, car_b


async def _unload(hass: HomeAssistant, *entries: MockConfigEntry) -> None:
    for entry in entries:
        await async_unload_and_join(hass, entry)


async def test_site_sensors(hass: HomeAssistant) -> None:
    set_source_states(hass)
    site = make_site_entry()
    await async_setup(hass, site)
    for key in SITE_SENSORS:
        assert hass.states.get(f"sensor.cjp_{key}") is not None, key
    # fuse 25 A - 1 A margin, no house sensors, no cars
    assert float(hass.states.get("sensor.cjp_site_available_l1").state) == 24.0
    await hass.config_entries.async_unload(site.entry_id)


async def test_cars_register_with_the_site(hass: HomeAssistant) -> None:
    site, car_a, car_b = await _setup_two_cars(hass)
    members = hass.data[DOMAIN]["site"].members
    assert set(members) == {car_a.entry_id, car_b.entry_id}
    assert hass.data[DOMAIN]["site"].order() == [car_a.entry_id, car_b.entry_id]
    await _unload(hass, car_a, car_b, site)


async def test_lower_priority_gets_what_is_left(hass: HomeAssistant) -> None:
    """Both cars want 3 x 16 A (no charging modes = max), the fuse allows 24 A per phase."""
    site, car_a, car_b = await _setup_two_cars(hass)
    assert _amps(hass, "a") == [16, 16, 16]
    assert _amps(hass, "b") == [8, 8, 8]
    assert float(hass.states.get("sensor.cjp_site_available_l1").state) == 0.0
    await _unload(hass, car_a, car_b, site)


async def test_urgent_car_overtakes(hass: HomeAssistant) -> None:
    """Car b under its daily minimum (30 %) charges first despite the lower priority."""
    site, car_a, car_b = await _setup_two_cars(hass, b_level=10)
    assert hass.data[DOMAIN]["site"].order() == [car_b.entry_id, car_a.entry_id]
    assert _amps(hass, "b") == [16, 16, 16]
    assert _amps(hass, "a") == [8, 8, 8]
    await _unload(hass, car_a, car_b, site)


async def test_fuse_budget_respected_after_cron(hass: HomeAssistant) -> None:
    """Both cars recalculate in the same cron minute; the sum must stay within 24 A per phase."""
    site, car_a, car_b = await _setup_two_cars(hass, b_level=10)
    for cron in range(1, 4):
        # the cron trigger is rescheduled from the fired time, so each run fires 5 min later
        await async_advance(hass, cron * 5 * 60)
        await _settle(hass)
        total = [a + b for a, b in zip(_amps(hass, "a"), _amps(hass, "b"))]
        assert max(total) <= 24, (cron, total)
    assert _amps(hass, "b") == [16, 16, 16]
    await _unload(hass, car_a, car_b, site)


async def test_unloading_the_site_releases_the_limit(hass: HomeAssistant) -> None:
    site, car_a, car_b = await _setup_two_cars(hass)
    await hass.config_entries.async_unload(site.entry_id)
    await _settle(hass)
    await hass.services.async_call(DOMAIN, "enforce_planning", {"config_entry_id": car_b.entry_id}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _amps(hass, "b") == [16, 16, 16]
    await _unload(hass, car_a, car_b)


def _circuit(hass: HomeAssistant, prefix: str) -> tuple[list[int], list[str] | None]:
    attributes = hass.states.get(f"sensor.cjp_{prefix}_charger_action").attributes
    amps = [attributes[f"circuit_phase_{i}_amps"] for i in (1, 2, 3)]
    # the blueprint triggers on the decision: it must carry the circuit sum too
    assert f"circuit {'/'.join(map(str, amps))} A" in attributes["decision"]
    return amps, attributes.get("circuit_chargers")


async def test_cars_on_one_circuit_publish_the_sum(hass: HomeAssistant) -> None:
    """Two chargers behind one circuit (Easee master): both cars publish the sum, so the master's
    automation sets the circuit for both (16 A + 8 A per phase)."""
    easee_a = register_charger(hass, CHARGER_STATUS, "Easee A")
    easee_b = register_charger(hass, CHARGER_B_STATUS, "Easee B")
    site, car_a, car_b = await _setup_two_cars(hass, circuits=[[easee_a, easee_b]])
    assert _amps(hass, "a") == [16, 16, 16]
    assert _amps(hass, "b") == [8, 8, 8]
    assert _circuit(hass, "a") == ([24, 24, 24], ["a", "b"])
    assert _circuit(hass, "b") == ([24, 24, 24], ["a", "b"])
    cars = hass.states.get("sensor.cjp_site_available_l1").attributes["cars"]
    assert [car["circuit"] for car in cars] == [["a", "b"], ["a", "b"]]
    await _unload(hass, car_a, car_b, site)


async def test_without_a_circuit_each_car_keeps_its_own_limit(hass: HomeAssistant) -> None:
    site, car_a, car_b = await _setup_two_cars(hass)
    assert _circuit(hass, "a") == ([16, 16, 16], None)
    assert _circuit(hass, "b") == ([8, 8, 8], None)
    await _unload(hass, car_a, car_b, site)


# --------------------------------------------------------------------------- #
# CJPSite logic with stand-in cars (deterministic, no worker jobs)
# --------------------------------------------------------------------------- #


class FakeCar:
    def __init__(self, entry_id: str, priority: int, phases: int = 3) -> None:
        self.entry = SimpleNamespace(entry_id=entry_id)
        self.prefix = entry_id
        self.site_priority = priority
        self.site_phase_order = "L1-L2-L3"
        self.config = {"charger": {"charging_phases": phases, "entity_ids": {}}}
        self.attributes: dict = {}
        self.signal = f"test_site_car_{entry_id}"
        self.recalculated = 0

    def async_site_recalculate(self) -> None:
        self.recalculated += 1

    def async_site_limit(self) -> None:
        self.limited = getattr(self, "limited", 0) + 1


@pytest.fixture
async def fake_site(hass: HomeAssistant):
    site = CJPSite(hass, make_site_entry(fuse_l1=25, fuse_l2=25, fuse_l3=25, margin=1))
    cars = {"a": FakeCar("a", 1), "b": FakeCar("b", 2)}
    for car in cars.values():
        site.register(car)
    yield site, cars
    await site.async_stop()


async def test_site_order_by_priority_then_urgency(fake_site) -> None:
    site, cars = fake_site
    assert site.order() == ["a", "b"]
    site.update_from_result(cars["b"], {"reservation": {}, "request": [8, 8, 8], "urgent": True})
    assert site.order() == ["b", "a"]
    assert site.behind("b") == ["a"]


async def test_site_signals_when_initial_calculation_is_ready(fake_site) -> None:
    site, _cars = fake_site
    waiter = asyncio.create_task(site.async_wait_started())
    await asyncio.sleep(0)
    assert not waiter.done()

    await site.async_start()
    await waiter
    assert waiter.done()


async def test_site_capacity_for_the_car_behind(fake_site) -> None:
    site, cars = fake_site
    site.update_from_result(cars["a"], {"reservation": {}, "request": [16, 16, 16], "urgent": False})
    now = dt_util.now().replace(tzinfo=None)
    assert site.snapshot_for(cars["b"], now)["live"] == [8.0, 8.0, 8.0]
    assert site.snapshot_for(cars["a"], now)["live"] == [24.0, 24.0, 24.0]


async def test_changed_plan_recalculates_the_cars_behind(fake_site) -> None:
    site, cars = fake_site
    hour = dt_util.now().replace(tzinfo=None, minute=0, second=0, microsecond=0)
    site.update_from_result(cars["a"], {"reservation": {hour: [16, 16, 16]}, "request": [0, 0, 0], "urgent": False})
    assert "b" in site._pending


async def test_car_turning_urgent_is_recalculated_itself(fake_site) -> None:
    site, cars = fake_site
    site.update_from_result(cars["b"], {"reservation": {}, "request": [8, 8, 8], "urgent": True})
    assert "b" in site._pending


async def test_changed_request_recalculates_the_cars_behind(fake_site) -> None:
    site, cars = fake_site
    site.update_from_result(cars["a"], {"reservation": {}, "request": [8, 8, 8], "urgent": False})
    for cancel in site._pending.values():
        cancel()
    site._pending.clear()
    site.update_from_result(cars["a"], {"reservation": {}, "request": [16, 16, 16], "urgent": False})
    assert "b" in site._pending


async def test_unchanged_result_recalculates_nobody(fake_site) -> None:
    """Recalculations stop when the results stop changing (no ping-pong between the cars)."""
    site, cars = fake_site
    result = {"reservation": {}, "request": [16, 16, 16], "urgent": False}
    site.update_from_result(cars["a"], result)
    for cancel in site._pending.values():
        cancel()
    site._pending.clear()
    site.update_from_result(cars["a"], dict(result))
    assert site._pending == {}


# --------------------------------------------------------------------------- #
# Current a car ahead was given but doesn't draw goes to the cars behind
# --------------------------------------------------------------------------- #

POWER_A = "sensor.fake_power_a"
POWER_B = "sensor.fake_power_b"
STATUS_A = "sensor.fake_status_a"
STATUS_B = "sensor.fake_status_b"
WATT_16A = str(3 * 16 * 230)
W = {"unit_of_measurement": "W"}


def _powered(car: FakeCar, power: str, status: str) -> FakeCar:
    car.config["charger"].update({"power_voltage": 230.0, "entity_ids": {"charger_power_entity_id": power, "status_entity_id": status}})
    return car


async def _power_site(hass: HomeAssistant, power_b: str = POWER_B, **site_options):
    for entity_id in (POWER_A, POWER_B):
        hass.states.async_set(entity_id, "0", W)
    for entity_id in (STATUS_A, STATUS_B):
        hass.states.async_set(entity_id, "ready_to_charge")
    site = CJPSite(hass, make_site_entry(fuse_l1=25, fuse_l2=25, fuse_l3=25, margin=1, **site_options))
    cars = {"a": _powered(FakeCar("a", 1), POWER_A, STATUS_A), "b": _powered(FakeCar("b", 2), power_b, STATUS_B)}
    for car in cars.values():
        site.register(car)
    return site, cars


def _ramped(site: CJPSite, entry_id: str) -> None:
    """The car ahead got its current longer ago than the ramp-up."""
    site._raised_at[entry_id] -= RAMP_UP + 1


def _live_b(site: CJPSite, cars) -> list[float]:
    return site.snapshot_for(cars["b"], dt_util.now().replace(tzinfo=None))["live"]


async def test_unused_current_goes_to_the_car_behind_without_phase_sensors(hass: HomeAssistant) -> None:
    """A fails to start (0 W at 16 A) on a site without phase sensors: after the ramp-up, b gets the fuse."""
    site, cars = await _power_site(hass)
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    assert _live_b(site, cars) == [8.0, 8.0, 8.0]  # a is still starting
    _ramped(site, "a")
    assert _live_b(site, cars) == [24.0, 24.0, 24.0]
    site.update_from_result(cars["a"], _result([16, 16, 16]))  # unchanged: no new ramp-up
    assert _live_b(site, cars) == [24.0, 24.0, 24.0]
    # a raise starts a new ramp-up: the new request counts again
    site.update_from_result(cars["a"], _result([10, 10, 10]))
    _ramped(site, "a")
    assert _live_b(site, cars) == [24.0, 24.0, 24.0]
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    assert _live_b(site, cars) == [8.0, 8.0, 8.0]
    await site.async_stop()


@pytest.mark.parametrize(
    ("watt", "unit", "expected"),
    [
        # 16 A may all be on one phase (a car charging on fewer phases than given): nothing is freed
        (3680, "W", [8.0, 8.0, 8.0]),
        # 3 x 10 A could be 16 A on each phase (the charger keeps to 16 A): nothing is freed either
        (6900, "W", [8.0, 8.0, 8.0]),
        # 10 A in all: at most 10 A on a phase
        (2300, "W", [14.0, 14.0, 14.0]),
        (2.3, "kW", [14.0, 14.0, 14.0]),
        # a unit that is not W/kW is never guessed: what a was given counts
        (2300, None, [8.0, 8.0, 8.0]),
        (2300, "A", [8.0, 8.0, 8.0]),
    ],
)
async def test_measured_power_frees_only_what_no_phase_can_draw(hass: HomeAssistant, watt, unit, expected) -> None:
    site, cars = await _power_site(hass)
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    _ramped(site, "a")
    hass.states.async_set(POWER_A, str(watt), {"unit_of_measurement": unit} if unit else {})
    assert _live_b(site, cars) == expected
    await site.async_stop()


async def test_one_phase_car_ahead_frees_only_its_phase(hass: HomeAssistant) -> None:
    """a was given 16 A on its L1 (grid L2 with its phase order) and draws nothing: grid L1/L3 were free anyway."""
    site, cars = await _power_site(hass)
    cars["a"].site_phase_order = "L2-L3-L1"
    site.update_from_result(cars["a"], _result([16, 0, 0]))
    assert _live_b(site, cars) == [24.0, 8.0, 24.0]
    _ramped(site, "a")
    assert _live_b(site, cars) == [24.0, 24.0, 24.0]
    await site.async_stop()


@pytest.mark.parametrize("state", ["unavailable", "unknown", "nan", "inf"])
async def test_missing_power_counts_as_the_request_without_phase_sensors(hass: HomeAssistant, state) -> None:
    """No reading from a's charger is not 0 A: b gets only what a's request leaves."""
    site, cars = await _power_site(hass)
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    _ramped(site, "a")
    hass.states.async_set(POWER_A, state, W)
    assert _live_b(site, cars) == [8.0, 8.0, 8.0]
    await site.async_stop()


@pytest.mark.parametrize("status", ["charging", "unavailable", "unknown", None, "on", "connected", "ready", "error"])
async def test_power_frees_nothing_while_the_charger_says_charging(hass: HomeAssistant, status) -> None:
    """A power sensor that hangs at 0 W while the charger charges must not give a's current away; nor without a
    known status."""
    site, cars = await _power_site(hass)
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    _ramped(site, "a")
    if status is None:
        hass.states.async_remove(STATUS_A)
    else:
        hass.states.async_set(STATUS_A, status)
    assert _live_b(site, cars) == [8.0, 8.0, 8.0]
    hass.states.async_set(STATUS_A, "completed")  # a full battery frees it
    assert _live_b(site, cars) == [24.0, 24.0, 24.0]
    await site.async_stop()


async def test_car_without_power_sensor_counts_as_the_request(hass: HomeAssistant) -> None:
    """a has no power sensor (its status says ready, past the ramp-up): what it was given counts."""
    site, cars = await _power_site(hass)
    cars["a"].config["charger"]["entity_ids"]["charger_power_entity_id"] = ""
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    _ramped(site, "a")
    assert _live_b(site, cars) == [8.0, 8.0, 8.0]
    await site.async_stop()


def _make_stale(hass: HomeAssistant, entity_id: str) -> None:
    """The sensor's last report was longer ago than STALE_SECONDS (same value)."""
    state = hass.states.get(entity_id)
    hass.states.async_remove(entity_id)
    hass.states.async_set(entity_id, state.state, state.attributes, timestamp=time.time() - STALE_SECONDS - 1)


@pytest.mark.parametrize("stale", [STATUS_A, POWER_A])
async def test_a_sensor_that_has_not_reported_frees_nothing(hass: HomeAssistant, stale) -> None:
    """A lost cloud push keeps the last status and 0 W: a sensor without a report for STALE_SECONDS gives a's current
    no longer away; a report of the unchanged value (no new state, only last_reported) frees it again."""
    site, cars = await _power_site(hass)
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    _ramped(site, "a")
    assert _live_b(site, cars) == [24.0, 24.0, 24.0]
    _make_stale(hass, stale)
    assert _live_b(site, cars) == [8.0, 8.0, 8.0]
    before = hass.states.get(stale)
    hass.states.async_set(stale, before.state, before.attributes)
    assert hass.states.get(stale).last_updated == before.last_updated  # only reported, not updated
    assert _live_b(site, cars) == [24.0, 24.0, 24.0]
    await site.async_stop()


async def test_a_sensor_going_silent_limits_the_car_behind_without_an_event(hass: HomeAssistant) -> None:
    """b took a's unused current; a's sensors stop reporting (no event at all): the timed check counts a with its request
    again and limits b."""
    site, cars = await _b_took_a_current(hass)
    with patch("custom_components.cable_juice_planner.site.STALE_SECONDS", -1):  # every report is now too old
        await async_advance(hass, STALE_CHECK_SECONDS + 1)
        await hass.async_block_till_done()
    assert cars["b"].limited == 1
    await site.async_stop()


async def test_shared_power_sensor_counts_as_the_request(hass: HomeAssistant) -> None:
    """a and b on one power sensor (one charger, two cars): it shows b's own charging, so a counts as given."""
    site, cars = await _power_site(hass, power_b=POWER_A)
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    _ramped(site, "a")
    assert _live_b(site, cars) == [8.0, 8.0, 8.0]
    # the overload check counts the sensor once: 10 A on it against the 24 A given on it. Counted per car it would be
    # min(16, 10) + min(8, 10) = 18 A
    site.update_from_result(cars["b"], _result([8, 8, 8]))
    _ramped(site, "b")
    hass.states.async_set(POWER_A, str(3 * 10 * 230 / 3), W)
    await hass.async_block_till_done()
    assert getattr(cars["b"], "limited", 0) == 0
    assert site._cars_draw() == [10.0, 10.0, 10.0]
    await site.async_stop()


async def test_two_cars_ahead_on_one_power_sensor_count_as_given(hass: HomeAssistant) -> None:
    """a (nothing given) and b (16 A, charging) share one charger's sensor; c behind gets what b's 16 A leave, not 0 A
    (a judged alone against b's power would take 48 A)."""
    site, cars = await _power_site(hass, power_b=POWER_A)
    cars["c"] = _powered(FakeCar("c", 3), "sensor.fake_power_c", "sensor.fake_status_c")
    hass.states.async_set("sensor.fake_power_c", "0", W)
    hass.states.async_set("sensor.fake_status_c", "ready_to_charge")
    site.register(cars["c"])
    site.update_from_result(cars["b"], _result([16, 16, 16]))
    _ramped(site, "b")
    hass.states.async_set(POWER_A, WATT_16A, W)
    await hass.async_block_till_done()
    assert site.snapshot_for(cars["c"], dt_util.now().replace(tzinfo=None))["live"] == [8.0, 8.0, 8.0]
    await site.async_stop()


async def test_a_charger_taking_more_than_it_was_given_is_an_overload(hass: HomeAssistant) -> None:
    """b was limited to 8 A, but its charger still takes 16 A (the automation did not follow yet): with a's 16 A that
    is 32 A, not hidden behind the 8 A b was given."""
    site, cars = await _power_site(hass)
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    site.update_from_result(cars["b"], _result([8, 8, 8]))
    hass.states.async_set(POWER_A, WATT_16A, W)
    hass.states.async_set(POWER_B, WATT_16A, W)
    await hass.async_block_till_done()
    # b's 48 A less the 10 % voltage margin of its 24 A, on its three phases: 15.2 A each, plus a's 16 A
    assert site._cars_draw() == pytest.approx([31.2, 31.2, 31.2])
    assert cars["b"].limited >= 1
    await site.async_stop()


async def test_an_overdraw_on_more_phases_than_given_is_spread_over_the_chargers_phases(hass: HomeAssistant) -> None:
    """a was given 16 A on one phase, but its charger did not switch and takes 3 x 16 A: counted 16/15.5/15.5 A (what
    it really takes), not 48 A on L1 (a false overload of a car alone) and not 0 A on L2/L3."""
    site, cars = await _power_site(hass)
    site.update_from_result(cars["a"], _result([16, 0, 0]))
    hass.states.async_set(POWER_A, WATT_16A, W)
    await hass.async_block_till_done()
    assert site._cars_draw() == pytest.approx([16.0, 46.4 / 3, 46.4 / 3])
    assert getattr(cars["a"], "limited", 0) == 0 and site._overload_recheck is None
    _ramped(site, "a")
    assert _live_b(site, cars) == [8.0, 8.5, 8.5]  # the budget sees it too (24 - 15.5 A, to the charger's half amps)
    await site.async_stop()


async def test_a_higher_real_voltage_is_no_overdraw(hass: HomeAssistant) -> None:
    """24 A at 240 V reads as 25 A at the configured 230 V: still what it was given, no limit and no rechecks."""
    site, cars = await _power_site(hass)
    site.update_from_result(cars["b"], _result([24, 24, 24]))
    hass.states.async_set(POWER_B, str(3 * 24 * 240), W)
    await hass.async_block_till_done()
    assert site._cars_draw() == [24.0, 24.0, 24.0]
    assert getattr(cars["b"], "limited", 0) == 0 and site._overload_recheck is None
    await site.async_stop()


async def test_unequal_requests_on_one_power_sensor_are_counted_exactly(hass: HomeAssistant) -> None:
    """a 3 x 10 A and b 1 x 14 A on one sensor, both drawing what they were given: 24/10/10 A, nothing extra."""
    site, cars = await _power_site(hass, power_b=POWER_A)
    site.update_from_result(cars["a"], _result([10, 10, 10]))
    site.update_from_result(cars["b"], _result([14, 0, 0]))
    hass.states.async_set(POWER_A, str((30 + 14) * 230), W)
    await hass.async_block_till_done()
    assert site._cars_draw() == [24.0, 10.0, 10.0]
    await site.async_stop()


async def test_car_ahead_starting_to_draw_limits_the_last_car_without_phase_sensors(hass: HomeAssistant) -> None:
    """b took a's unused current; a starts to draw: b is limited at once, at most every OVERLOAD_THROTTLE, and checked
    again after it while still too much (no new sensor event)."""
    site, cars = await _power_site(hass)
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    _ramped(site, "a")
    site.update_from_result(cars["b"], _result([16, 16, 16]))
    hass.states.async_set(POWER_B, WATT_16A, W)
    await hass.async_block_till_done()
    assert getattr(cars["b"], "limited", 0) == 0  # 16 A on 25 A
    assert site._overload_recheck is None
    hass.states.async_set(POWER_A, WATT_16A, W)
    await hass.async_block_till_done()
    assert cars["b"].limited == 1
    assert getattr(cars["a"], "limited", 0) == 0
    hass.states.async_set(POWER_A, str(3 * 15 * 230), W)
    await hass.async_block_till_done()
    assert cars["b"].limited == 1  # throttled
    site._last_overload["b"] -= OVERLOAD_THROTTLE
    await async_advance(hass, OVERLOAD_THROTTLE + 2)
    await hass.async_block_till_done()
    assert cars["b"].limited == 2  # the recheck, without a new event
    # within the fuse again: no more rechecks
    hass.states.async_set(POWER_B, str(3 * 8 * 230), W)
    site.update_from_result(cars["b"], _result([8, 8, 8]))
    site._last_overload["b"] -= OVERLOAD_THROTTLE
    await async_advance(hass, OVERLOAD_THROTTLE + 2)
    await hass.async_block_till_done()
    assert cars["b"].limited == 2
    assert site._overload_recheck is None
    await site.async_stop()


async def test_power_sensors_are_followed_as_cars_join_and_leave(hass: HomeAssistant) -> None:
    """The power and status sensors that are followed change with the members."""
    site, cars = await _power_site(hass)
    checks: list[int] = []
    site._check_overload = lambda: checks.append(1)  # what a followed sensor's event runs

    async def _events(*entities: str) -> int:
        checks.clear()
        for entity_id in entities:
            hass.states.async_set(entity_id, str(float(hass.states.get(entity_id).state or 0) + 1) if entity_id.startswith("sensor.fake_power") else "charging")
        await hass.async_block_till_done()
        return len(checks)

    assert await _events(POWER_A, STATUS_B) == 2
    site.unregister("a")
    assert await _events(POWER_A, STATUS_A) == 0  # no longer a member: not followed
    assert await _events(POWER_B) == 1
    site.register(cars["a"])
    assert await _events(POWER_A) == 1
    await site.async_stop()
    assert site._power_unsub is None
    assert await _events(POWER_A, POWER_B) == 0


async def test_unused_current_goes_to_the_car_behind_with_phase_sensors(hass: HomeAssistant) -> None:
    """With phase sensors the measured draw counts already (also right after a raise)."""
    phases = {f"phase_l{i}_entity_id": f"sensor.grid_l{i}" for i in (1, 2, 3)}
    for entity_id in phases.values():
        hass.states.async_set(entity_id, "0", {"unit_of_measurement": "A"})
    site, cars = await _power_site(hass, **phases)
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    assert _live_b(site, cars) == [24.0, 24.0, 24.0]
    # the phase sensors are the measurement: a charger power sensor is no overload
    site.update_from_result(cars["b"], _result([16, 16, 16]))
    hass.states.async_set(POWER_A, str(3 * 30 * 230), W)
    hass.states.async_set(STATUS_A, "charging")
    await hass.async_block_till_done()
    assert getattr(cars["b"], "limited", 0) == 0
    await site.async_stop()


@pytest.mark.parametrize("state", ["nan", "unavailable"])
async def test_phase_sensor_without_a_number_falls_back_to_the_power_sensors(hass: HomeAssistant, state) -> None:
    """A phase sensor that measures nothing is not 0 A: the chargers' power sensors are the measurement then (the house
    counts with its last measured 0 A)."""
    phases = {f"phase_l{i}_entity_id": f"sensor.grid_l{i}" for i in (1, 2, 3)}
    for entity_id in phases.values():
        hass.states.async_set(entity_id, "0", {"unit_of_measurement": "A"})
    site, cars = await _power_site(hass, **phases)
    assert _live_b(site, cars) == [24.0, 24.0, 24.0]  # measured once
    hass.states.async_set("sensor.grid_l2", state, {"unit_of_measurement": "A"})
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    assert _live_b(site, cars) == [8.0, 8.0, 8.0]  # a is still starting
    _ramped(site, "a")
    assert _live_b(site, cars) == [24.0, 24.0, 24.0]
    site.update_from_result(cars["b"], _result([16, 16, 16]))
    hass.states.async_set(STATUS_A, "charging")
    await hass.async_block_till_done()
    assert cars["b"].limited == 1
    await site.async_stop()


async def test_a_phase_sensor_that_has_not_reported_falls_back_to_the_power_sensors(hass: HomeAssistant) -> None:
    """A meter that hangs (a lost connection keeps its last, low value) measures nothing: the chargers' power sensors are
    the measurement then, carefully."""
    phases = {f"phase_l{i}_entity_id": f"sensor.grid_l{i}" for i in (1, 2, 3)}
    for entity_id in phases.values():
        hass.states.async_set(entity_id, "0", {"unit_of_measurement": "A"})
    site, cars = await _power_site(hass, **phases)
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    assert site._metered() and _live_b(site, cars) == [24.0, 24.0, 24.0]
    _make_stale(hass, "sensor.grid_l3")
    assert not site._metered()
    assert _live_b(site, cars) == [8.0, 8.0, 8.0]  # a is still starting: its request counts
    await site.async_stop()


async def _phase_site(hass: HomeAssistant, amps: float):
    phases = {f"phase_l{i}_entity_id": f"sensor.grid_l{i}" for i in (1, 2, 3)}
    for entity_id in phases.values():
        hass.states.async_set(entity_id, str(amps), {"unit_of_measurement": "A"})
    return await _power_site(hass, **phases)


def _live_a(site: CJPSite, cars) -> list[float]:
    return site.snapshot_for(cars["a"], dt_util.now().replace(tzinfo=None))["live"]


async def test_a_stale_charger_power_reading_counts_at_most_the_request_from_the_house(hass: HomeAssistant) -> None:
    """The meter says 16 A on each phase and a's power sensor hangs at 3 x 16 A (it only reports a change)."""
    site, cars = await _phase_site(hass, 16)
    hass.states.async_set(STATUS_A, "charging")
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    hass.states.async_set(POWER_A, WATT_16A, W)
    assert _live_a(site, cars) == [24.0, 24.0, 24.0]  # fresh: the 16 A measured are a's own charging
    _make_stale(hass, POWER_A)
    # a still has its 16 A and says charging: it may well charge steadily, so its own charging is not the house
    assert _live_a(site, cars) == [24.0, 24.0, 24.0]
    # a's fresh status says it is full: a reading that hangs high must not hide the house's 16 A
    hass.states.async_set(STATUS_A, "completed")
    assert _live_a(site, cars) == [8.0, 8.0, 8.0]
    hass.states.async_set(STATUS_A, "charging")
    # the plan stopped a: neither
    site.update_from_result(cars["a"], _result([0, 0, 0]))
    assert _live_a(site, cars) == [8.0, 8.0, 8.0]
    await site.async_stop()


async def test_without_any_house_data_the_cars_get_no_current_until_the_meter_reports(hass: HomeAssistant) -> None:
    """After a start the meter has not reported and there is no forecast: no current for the cars (user choice), and
    with a forecast that lacks this hour its highest hour counts."""
    phases = {f"phase_l{i}_entity_id": f"sensor.grid_l{i}" for i in (1, 2, 3)}
    for entity_id in phases.values():
        hass.states.async_set(entity_id, "unavailable", {"unit_of_measurement": "A"})
    site, cars = await _power_site(hass, **phases)
    assert _live_a(site, cars) == [0.0, 0.0, 0.0]
    other = (dt_util.now().hour + 1) % 24
    site.house_forecast = {other: [9.0, 2.0, 3.0], (other + 1) % 24: [1.0, 4.0, 3.0]}
    assert _live_a(site, cars) == [15.0, 20.0, 21.0]
    recalculated: list[str] = []
    site.recalculate_all = lambda: recalculated.append("all")
    for entity_id in phases.values():
        hass.states.async_set(entity_id, "5", {"unit_of_measurement": "A"})
        site._phase_changed(None)  # what the phase sensors' events run (the site is not started here)
    site._phase_changed(None)
    assert recalculated == ["all"]  # once, when the meter measures again: the cars don't wait for their next run
    assert _live_a(site, cars) == [19.0, 19.0, 19.0]  # its value counts
    await site.async_stop()


@pytest.mark.parametrize("reading", ["stale", "unavailable", "amps"])
async def test_a_car_ahead_without_a_fresh_power_reading_counts_its_request_with_phase_sensors(hass: HomeAssistant, reading) -> None:
    """The meter says nothing is drawn, a was given 16 A and charges (its status says so) but its power sensor shows no
    fresh W/kW: b gets what a's 16 A leave, not the whole fuse."""
    site, cars = await _phase_site(hass, 0)
    hass.states.async_set(STATUS_A, "charging")
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    if reading == "stale":
        _make_stale(hass, POWER_A)
    elif reading == "unavailable":
        hass.states.async_set(POWER_A, "unavailable", W)
    else:
        hass.states.async_set(POWER_A, "16", {"unit_of_measurement": "A"})
    assert _live_b(site, cars) == [8.0, 8.0, 8.0]
    await site.async_stop()


async def test_a_power_reading_in_another_unit_is_not_taken_from_the_house(hass: HomeAssistant) -> None:
    """a's power sensor reports amps (never guessed as watts): the meter's 16 A stay the house's."""
    site, cars = await _phase_site(hass, 16)
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    hass.states.async_set(POWER_A, "11040", {"unit_of_measurement": "A"})
    assert _live_a(site, cars) == [8.0, 8.0, 8.0]
    await site.async_stop()


async def test_a_meter_that_measures_nothing_keeps_the_house(hass: HomeAssistant) -> None:
    """The house used 10 A; the meter stops reporting: the house counts with its last 10 A (or the hour's forecast when
    that is higher), in the budget and in the overload check, never 0 A."""
    site, cars = await _phase_site(hass, 10)
    assert _live_a(site, cars) == [14.0, 14.0, 14.0]
    for i in (1, 2, 3):
        _make_stale(hass, f"sensor.grid_l{i}")
    assert not site._metered()
    assert _live_a(site, cars) == [14.0, 14.0, 14.0]
    site.house_forecast = {hour: [12.0, 5.0, 5.0] for hour in range(24)}
    assert _live_a(site, cars) == [12.0, 14.0, 14.0]
    # a charges its 14 A (fresh power): with the house's 12/10/10 A that is over the fuse on L1, and the check sees it
    site.update_from_result(cars["a"], _result([14, 14, 14]))
    hass.states.async_set(POWER_A, str(3 * 14 * 230), W)
    await hass.async_block_till_done()
    assert cars["a"].limited >= 1
    await site.async_stop()


async def _b_took_a_current(hass: HomeAssistant):
    """a was given 10 A but draws nothing; b took it over and draws 16 A."""
    site, cars = await _power_site(hass)
    site.update_from_result(cars["a"], _result([10, 10, 10]))
    _ramped(site, "a")
    site.update_from_result(cars["b"], _result([16, 16, 16]))
    hass.states.async_set(POWER_B, WATT_16A, W)
    await hass.async_block_till_done()
    assert getattr(cars["b"], "limited", 0) == 0
    return site, cars


async def test_car_ahead_whose_status_says_charging_limits_the_last_car(hass: HomeAssistant) -> None:
    """a's power sensor is slow; its status says charging: b is limited at once."""
    site, cars = await _b_took_a_current(hass)
    hass.states.async_set(STATUS_A, "charging")
    await hass.async_block_till_done()
    assert cars["b"].limited == 1
    assert getattr(cars["a"], "limited", 0) == 0
    await site.async_stop()


async def test_car_ahead_getting_more_current_limits_the_last_car(hass: HomeAssistant) -> None:
    """a gets more current: what it was given counts at once (it starts), so b is limited without a sensor event."""
    site, cars = await _b_took_a_current(hass)
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    assert cars["b"].limited == 1
    assert getattr(cars["a"], "limited", 0) == 0
    await site.async_stop()


async def test_car_drawing_without_request_is_no_false_overload(hass: HomeAssistant) -> None:
    """a charges 16 A in all without a rule (one phase or three, unknown): spread over its phases for the overload
    check, so b is not limited for a draw that may not be there."""
    site, cars = await _power_site(hass)
    site.update_from_result(cars["b"], _result([16, 16, 16]))
    hass.states.async_set(POWER_B, WATT_16A, W)
    hass.states.async_set(POWER_A, str(16 * 230), W)
    await hass.async_block_till_done()
    assert getattr(cars["b"], "limited", 0) == 0
    assert site._overload_recheck is None
    await site.async_stop()


async def test_overload_without_a_car_to_limit_is_not_checked_again(hass: HomeAssistant) -> None:
    """Above the fuse but no car has current to give back: no endless rechecks."""
    site, cars = await _power_site(hass)
    hass.states.async_set(POWER_A, str(3 * 30 * 230), W)
    await hass.async_block_till_done()
    assert site._cars_draw() == [30.0, 30.0, 30.0]
    assert site._overload_recheck is None
    assert getattr(cars["a"], "limited", 0) == 0
    assert getattr(cars["b"], "limited", 0) == 0
    await site.async_stop()


async def test_circuit_sum_is_at_most_the_fuse(circuit_site) -> None:
    """b got the 16 A a doesn't draw: the circuit limit stays at the fuse, so it holds both if a starts."""
    site, cars = circuit_site
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    site.update_from_result(cars["b"], _result([16, 16, 16]))
    assert site.circuit_amps(["a", "b"]) == [24, 24, 24]


# --------------------------------------------------------------------------- #
# Shared circuit (chargers behind one circuit limit, e.g. an Easee master)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("circuits", "own", "selected", "expected"),
    [
        # the car's circuit is replaced by the new choice
        ([["a", "b"], ["c", "d"]], {"a"}, ["a", "e"], [["c", "d"], ["a", "e"]]),
        # a charger moved from another circuit leaves it (a charger is on one circuit only)
        ([["c", "d", "e"]], {"a"}, ["a", "c"], [["d", "e"], ["a", "c"]]),
        ([["c", "d"]], {"a"}, ["a", "c"], [["a", "c"]]),
        # cleared from one car: gone for every car of it
        ([["a", "b"]], {"b"}, [], []),
        # a circuit needs two chargers
        ([], {"a"}, ["a"], []),
        # a car without a circuit changes nothing
        ([["c", "d"]], {"a"}, [], [["c", "d"]]),
    ],
)
def test_update_circuits(circuits, own, selected, expected) -> None:
    assert update_circuits(circuits, own, selected) == expected


@pytest.fixture
async def circuit_site(hass: HomeAssistant):
    """Cars a and b share a circuit, c has its own charger."""
    site = CJPSite(hass, make_site_entry(fuse_l1=25, fuse_l2=25, fuse_l3=25, margin=1, **{CONF_CIRCUITS: [["easee_a", "easee_b"]]}))
    cars = {"a": FakeCar("a", 1), "b": FakeCar("b", 2), "c": FakeCar("c", 3)}
    for car in cars.values():
        site.register(car)
    site.devices.update({"a": {"easee_a"}, "b": {"easee_b", "shelly_b"}, "c": {"easee_c"}})
    yield site, cars
    await site.async_stop()


def _result(request: list) -> dict:
    return {"reservation": {}, "request": request, "urgent": False}


async def test_circuit_sum_reaches_every_car_of_it(hass: HomeAssistant, circuit_site) -> None:
    site, cars = circuit_site
    signals = []

    @callback
    def _signal(key) -> None:
        signals.append(key)

    async_dispatcher_connect(hass, cars["a"].signal, _signal)
    cars["b"].site_phase_order = "L2-L3-L1"  # b's L1 is grid L2
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    site.update_from_result(cars["b"], _result([6, 0, 0]))
    # car a was patched when b changed, so the master's automation sees it at once
    assert signals == ["charger_action"]
    assert [cars["a"].attributes["charger_action"][f"circuit_phase_{i}_amps"] for i in (1, 2, 3)] == [16, 22, 16]
    assert cars["a"].attributes["charger_action"]["circuit_chargers"] == ["a", "b"]
    assert site.circuit_patch(cars["b"]) is True
    assert cars["b"].attributes["charger_action"]["circuit_phase_2_amps"] == 22
    assert site.circuit_patch(cars["b"]) is False  # unchanged
    assert site.circuit_members("c") == []
    assert site.circuit_patch(cars["c"]) is False
    assert cars["c"].attributes == {}


async def test_circuit_is_zero_when_nobody_on_it_charges(circuit_site) -> None:
    site, cars = circuit_site
    site.update_from_result(cars["a"], _result([0, 0, 0]))
    site.update_from_result(cars["b"], _result([0, 0, 0]))
    site.update_from_result(cars["c"], _result([16, 16, 16]))
    site.circuit_patch(cars["a"])
    assert [cars["a"].attributes["charger_action"][f"circuit_phase_{i}_amps"] for i in (1, 2, 3)] == [0, 0, 0]


async def test_circuit_with_one_loaded_car_is_no_circuit(circuit_site) -> None:
    site, cars = circuit_site
    site.unregister("b")
    assert site.circuit_members("a") == []


async def test_first_charging_car_on_the_circuit_decides_the_phases(circuit_site) -> None:
    site, cars = circuit_site
    now = dt_util.now().replace(tzinfo=None)
    assert site.snapshot_for(cars["b"], now)["circuit_phases"] is None  # nobody ahead charges
    site.update_from_result(cars["a"], _result([6, 0, 0]))
    assert site.snapshot_for(cars["b"], now)["circuit_phases"] == 1
    assert site.circuit_phases("a") is None  # nobody ahead of a
    assert site.circuit_phases("c") is None  # not on the circuit
    site.update_from_result(cars["a"], _result([16, 16, 16]))
    assert site.circuit_phases("b") == 3
    # a charger that only charges on one phase is not switched by the circuit
    cars["a"].config["charger"]["charging_phases"] = 1
    site.update_from_result(cars["a"], _result([16, 0, 0]))
    assert site.circuit_phases("b") is None


def _clamp_engine(live: list, circuit_phases: int | None, charging_phases: int = 3) -> CJPEngine:
    options = {
        **const.default_options(),
        "charging_phases": float(charging_phases),
        "charging_max_amp": 16.0,
        "power_voltage": 230.0,
        "charging_single_phase_min_amp": 6.0,
        "charging_single_phase_max_amp": 16.0,
        "charging_three_phase_min_amp": 6.0,
    }
    engine = CJPEngine(const.build_config(options), "cjp_test", {})
    engine._snap = SimpleNamespace(site={"live": live, "circuit_phases": circuit_phases})
    return engine


@pytest.mark.parametrize(
    ("requested", "circuit_phases", "charging_phases", "expected"),
    [
        ([16, 16, 16], None, 3, [16, 16, 16]),
        # the car ahead charges on 1 phase: only 1 phase for this car
        ([16, 16, 16], 1, 3, [16, 0, 0]),
        # 1 phase solar while the car ahead charges on 3 phases: 3 x 6 A is more than asked, so wait
        ([6, 0, 0], 3, 3, [0, 0, 0]),
        ([10, 10, 10], 3, 3, [10, 10, 10]),
        # a 1 phase charger is not switched by the circuit
        ([16, 0, 0], 3, 1, [16, 0, 0]),
    ],
)
def test_site_clamp_keeps_to_the_circuit_phases(requested, circuit_phases, charging_phases, expected) -> None:
    engine = _clamp_engine([24.0, 24.0, 24.0], circuit_phases, charging_phases)
    assert engine.site_clamp(requested) == expected


@pytest.mark.parametrize(
    ("meta_unit", "expected"),
    # HA converts power to W and current to A (the units asked for), whatever the recorded or the state's unit
    [("W", 1150 / 230), ("kW", 1150 / 230), ("A", 1150), ("mA", 1150), (None, None)],
)
async def test_house_forecast_uses_the_recorded_unit(hass: HomeAssistant, meta_unit, expected) -> None:
    """Right after a restart the phase sensors have no state yet: the unit comes from the statistics.
    Watts read as amps (the bug) left no current for the cars."""
    from unittest.mock import MagicMock  # noqa: PLC0415

    phases = {f"phase_l{i}_entity_id": f"sensor.grid_l{i}" for i in (1, 2, 3)}
    site = CJPSite(hass, make_site_entry(fuse_l1=25, fuse_l2=25, fuse_l3=25, margin=1, **phases))
    site.house_forecast = {3: [1.0, 1.0, 1.0]}
    hass.config.components.add("recorder")
    start = dt_util.utcnow().replace(hour=3, minute=0, second=0, microsecond=0) - datetime.timedelta(days=1)
    stats = {entity_id: [{"start": start, "mean": 1150.0}] for entity_id in phases.values()}
    metadata = {entity_id: (1, {"unit_of_measurement": meta_unit}) for entity_id in phases.values()} if meta_unit else {}
    recorder = SimpleNamespace(async_add_executor_job=hass.async_add_executor_job)
    during = MagicMock(return_value=stats)
    with (
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        patch("homeassistant.components.recorder.statistics.statistics_during_period", during),
        patch("homeassistant.components.recorder.statistics.get_metadata", return_value=metadata),
    ):
        await site._async_refresh_forecast()
    assert during.call_args.args[5] == {"power": "W", "electric_current": "A"}
    hour = dt_util.as_local(start).hour
    if expected is None:
        # unknown unit: the previous forecast is kept
        assert site.house_forecast == {3: [1.0, 1.0, 1.0]}
    else:
        assert site.house_forecast[hour] == [round(expected, 2)] * 3
