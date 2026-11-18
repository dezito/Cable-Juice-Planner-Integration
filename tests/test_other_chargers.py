"""The chargers of the cars with a lower priority are left out of the house consumption (utils/chargers.py)."""
from __future__ import annotations

import datetime
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigEntryChange, ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cable_juice_planner.const import DOMAIN, build_config, upgrade_options
from custom_components.cable_juice_planner.coordinator import CJPCoordinator
from custom_components.cable_juice_planner.engine import CJPEngine, Snapshot
from custom_components.cable_juice_planner.utils.chargers import lower_priority_charger_power_entities as lower_priority_chargers
from custom_components.cable_juice_planner.utils.helpers import unique_entity_ids

from .conftest import CHARGER_POWER, MOCK_OPTIONS, async_unload_and_join, form_defaults, make_entry, make_site_entry, set_source_states
from .test_engine import OPTIONS, TEXTS

OWN = "own-entry-id"


@pytest.fixture(autouse=True)
def config_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    """Removing a car exports a backup into <config>/cable_juice_planner_backups: keep it out of the shared test config."""
    hass.config.config_dir = str(tmp_path)
    return tmp_path


def car(hass: HomeAssistant, charger: str | None, prefix: str = "zoe", announce: bool = False, priority: int = 2, **kwargs) -> MockConfigEntry:
    """Another car's config entry, by default behind the car under test (priority 1).

    announce=True sends the signal a real new entry sends (add_to_hass does not).
    """
    options = {**MOCK_OPTIONS, "charger_power_entity_id": charger or "", "site_priority": priority}
    entry = make_entry(prefix, options)
    if kwargs:
        entry = MockConfigEntry(domain=DOMAIN, version=2, title=entry.title, data=dict(entry.data), options=options, **kwargs)
    entry.add_to_hass(hass)
    if announce:
        hass.config_entries._async_dispatch(ConfigEntryChange.ADDED, entry)
    return entry


def test_unique_entity_ids_keeps_the_order_and_drops_empty_values() -> None:
    assert unique_entity_ids(["sensor.a", "", None, "sensor.b", "sensor.a"]) == ["sensor.a", "sensor.b"]
    assert unique_entity_ids(None) == []


async def test_chargers_of_the_cars_behind_this_one_only(hass: HomeAssistant) -> None:
    """Not this car (by entry), not its own charger (also when another car shares it), no site, disabled or empty ones."""
    own = car(hass, CHARGER_POWER, "own", priority=1)
    car(hass, "sensor.zoe_power", "zoe", priority=2)
    car(hass, "sensor.tesla_power", "tesla", priority=3)
    car(hass, "sensor.zoe_power", "zoe2", priority=2)  # the same charger twice
    car(hass, CHARGER_POWER, "shared", priority=2)  # shares the charger of this car
    car(hass, "sensor.old_power", "old", disabled_by=ConfigEntryDisabler.USER)
    car(hass, "", "none")
    make_site_entry().add_to_hass(hass)
    MockConfigEntry(domain="other", options={"charger_power_entity_id": "sensor.other", "site_priority": 5}).add_to_hass(hass)

    assert lower_priority_chargers(hass, own.entry_id, CHARGER_POWER, 1) == ["sensor.tesla_power", "sensor.zoe_power"]
    # a new car that is being set up has no entry yet
    assert lower_priority_chargers(hass, None, "sensor.zoe_power", 1) == [CHARGER_POWER, "sensor.tesla_power"]


async def test_the_first_priority_has_the_first_right_to_the_solar_surplus(hass: HomeAssistant) -> None:
    """Car 1 leaves out the chargers of cars 2 and 3, car 2 only car 3's, car 3 none: the surplus is shared out in order."""
    chargers = {1: "sensor.car1_power", 2: "sensor.car2_power", 3: "sensor.car3_power"}
    entries = {priority: car(hass, charger, f"car{priority}", priority=priority) for priority, charger in chargers.items()}

    def seen_by(priority: int) -> list[str]:
        return lower_priority_chargers(hass, entries[priority].entry_id, chargers[priority], priority)

    assert seen_by(1) == ["sensor.car2_power", "sensor.car3_power"]
    assert seen_by(2) == ["sensor.car3_power"]
    assert seen_by(3) == []


async def test_cars_with_the_same_priority_do_not_leave_each_other_out(hass: HomeAssistant) -> None:
    """The default (everyone priority 1) changes nothing: each car still counts the others' charging as consumption."""
    entries = [car(hass, f"sensor.car{number}_power", f"car{number}", priority=1) for number in (1, 2)]
    assert lower_priority_chargers(hass, entries[0].entry_id, "sensor.car1_power", 1) == []
    assert lower_priority_chargers(hass, entries[1].entry_id, "sensor.car2_power", 1) == []


def test_upgrading_the_options_keeps_the_charger_when_the_new_option_is_empty() -> None:
    """The form defaults hold an empty new option next to the not yet migrated old one: the old value is the charger."""
    assert upgrade_options({"power_consumtion_entity_id": "sensor.charger", "charger_power_entity_id": ""}) == {"charger_power_entity_id": "sensor.charger"}
    assert upgrade_options({"power_consumtion_entity_id": "sensor.old", "charger_power_entity_id": "sensor.new"}) == {"charger_power_entity_id": "sensor.new"}


async def test_a_missing_priority_counts_as_the_default(hass: HomeAssistant) -> None:
    """A car stored without a priority has the default 1, like the one in the options."""
    entry = car(hass, "sensor.zoe_power", "zoe")
    hass.config_entries.async_update_entry(entry, options={"charger_power_entity_id": "sensor.zoe_power"})
    assert lower_priority_chargers(hass, OWN, None, 0.5) == ["sensor.zoe_power"]
    assert lower_priority_chargers(hass, OWN, None, 1) == []


async def test_the_house_consumption_sensor_is_never_a_charger(hass: HomeAssistant) -> None:
    """power_consumption_entity_id is the HOUSE sensor of a car; only the (misspelled) charger option counts."""
    car(hass, None, "zoe")  # a car without a charger but with the house sensor of MOCK_OPTIONS
    assert MOCK_OPTIONS["power_consumption_entity_id"] == "sensor.house_consumption"
    assert lower_priority_chargers(hass, OWN, None, 1) == []


async def test_the_chargers_are_added_once_and_not_required(hass: HomeAssistant, mock_entry) -> None:
    car(hass, "sensor.zoe_power")
    car(hass, "sensor.tesla_power", "tesla")
    mock_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_entry, options={**MOCK_OPTIONS, "ignore_consumption_from_entity_ids": ["sensor.zoe_power", "sensor.pool", "sensor.pool"]})

    coordinator = CJPCoordinator(hass, mock_entry)

    # the user's sensors first, then the chargers they did not list, nothing twice, never this car's own charger
    assert coordinator.config["home"]["entity_ids"]["ignore_consumption_from_entity_ids"] == ["sensor.zoe_power", "sensor.pool", "sensor.tesla_power"]
    assert coordinator.auto_ignored_entities == ["sensor.tesla_power"]
    # every other car's charger (listed or found, whatever its priority) is checked for a hung reading, never this car's own
    assert coordinator.config["home"]["charger_entity_ids"] == ["sensor.tesla_power", "sensor.zoe_power"]
    # a car that is not set up yet must not hold this one up, but its history is kept and its unit read
    assert "sensor.tesla_power" not in coordinator.configured_entities
    assert {"sensor.tesla_power", "sensor.zoe_power"} <= set(coordinator.buffered_entities) & set(coordinator.power_entities)
    assert "sensor.tesla_power" in coordinator.snapshot_entities
    assert {"sensor.zoe_power", "sensor.pool", CHARGER_POWER} <= set(coordinator.configured_entities)
    await hass.async_add_executor_job(coordinator._executor.shutdown, True)
    await hass.async_add_executor_job(coordinator._what_if_executor.shutdown, True)


async def test_switched_off_adds_no_charger(hass: HomeAssistant, mock_entry) -> None:
    car(hass, "sensor.zoe_power")
    mock_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_entry, options={**MOCK_OPTIONS, "ignore_cjp_chargers": False, "ignore_consumption_from_entity_ids": ["sensor.pool"]})

    coordinator = CJPCoordinator(hass, mock_entry)

    assert coordinator.config["home"]["entity_ids"]["ignore_consumption_from_entity_ids"] == ["sensor.pool"]
    assert coordinator.auto_ignored_entities == [] and coordinator.lower_priority_chargers == []
    assert coordinator.config["home"]["charger_entity_ids"] == ["sensor.zoe_power"]  # still a charger, so still checked if the user lists it
    await hass.async_add_executor_job(coordinator._executor.shutdown, True)
    await hass.async_add_executor_job(coordinator._what_if_executor.shutdown, True)


def test_an_entry_without_the_option_ignores_the_chargers() -> None:
    """Entries stored before the option existed (and the PyScript import) get it on."""
    assert build_config({})["home"]["ignore_cjp_chargers"] is True
    assert build_config({"ignore_cjp_chargers": None})["home"]["ignore_cjp_chargers"] is True
    assert build_config({"ignore_cjp_chargers": False})["home"]["ignore_cjp_chargers"] is False


def test_a_sensor_listed_twice_is_subtracted_once() -> None:
    def ignored(listed: list[str]) -> float:
        engine = CJPEngine(build_config({**OPTIONS, "ignore_consumption_from_entity_ids": listed}), "cjp_test", TEXTS)
        engine._history = SimpleNamespace(get_average_value=lambda entity_id, *args, **kwargs: {"sensor.charger": 700.0, "sensor.pool": 100.0}[entity_id])
        return engine.power_from_ignored(None, None)

    assert ignored(["sensor.charger"]) == 700.0
    assert ignored(["sensor.charger", "sensor.pool", "sensor.charger", ""]) == 800.0


async def _settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_changed_set_of_chargers_reloads_the_car_once(hass: HomeAssistant, loaded_entry) -> None:
    """A car behind this one added, given another charger or priority, or removed reloads this car; nothing else does."""
    coordinators = [loaded_entry.runtime_data]

    async def changed() -> bool:
        await _settle(hass)
        if loaded_entry.runtime_data is coordinators[-1]:
            return False
        for name in ("_executor", "_what_if_executor"):
            await hass.async_add_executor_job(getattr(coordinators[-1], name).shutdown, True)
        coordinators.append(loaded_entry.runtime_data)
        assert loaded_entry.state is ConfigEntryState.LOADED
        return True

    zoe = car(hass, "sensor.zoe_power", announce=True)
    assert await changed()
    assert coordinators[-1].auto_ignored_entities == ["sensor.zoe_power"]

    # a title change, the same options again, this car's own change, a site entry and another integration: nothing to do
    hass.config_entries.async_update_entry(zoe, title="Zoe")
    hass.config_entries.async_update_entry(zoe, options={**zoe.options})
    site = make_site_entry()
    site.add_to_hass(hass)
    hass.config_entries._async_dispatch(ConfigEntryChange.ADDED, site)
    other = MockConfigEntry(domain="other", options={"charger_power_entity_id": "sensor.other"})
    other.add_to_hass(hass)
    hass.config_entries._async_dispatch(ConfigEntryChange.ADDED, other)
    assert not await changed()

    # a charger that is the same as this car's own one is no change either
    shared = car(hass, CHARGER_POWER, "shared", announce=True)
    assert not await changed()
    await hass.config_entries.async_remove(shared.entry_id)
    assert not await changed()

    # a second car: one reload, and a state change of the new entry does not start another
    tesla = car(hass, "sensor.tesla_power", "tesla", announce=True)
    assert await changed()
    assert coordinators[-1].auto_ignored_entities == ["sensor.tesla_power", "sensor.zoe_power"]
    hass.config_entries.async_update_entry(tesla, title="Tesla")
    assert not await changed()

    hass.config_entries.async_update_entry(zoe, options={**zoe.options, "charger_power_entity_id": "sensor.zoe_wallbox"})
    assert await changed()
    assert coordinators[-1].auto_ignored_entities == ["sensor.tesla_power", "sensor.zoe_wallbox"]

    # a car that gets the same priority as this one (1) is no longer behind it; one that gets a lower one is again
    hass.config_entries.async_update_entry(zoe, options={**zoe.options, "site_priority": 1})
    assert await changed()
    assert coordinators[-1].auto_ignored_entities == ["sensor.tesla_power"]
    hass.config_entries.async_update_entry(zoe, options={**zoe.options, "site_priority": 4})
    assert await changed()
    assert coordinators[-1].auto_ignored_entities == ["sensor.tesla_power", "sensor.zoe_wallbox"]
    # a car ahead of this one (a priority that is not behind it) changes nothing, whatever it is set to
    hass.config_entries.async_update_entry(tesla, options={**tesla.options, "site_priority": 1})
    assert await changed()
    hass.config_entries.async_update_entry(tesla, options={**tesla.options, "charger_power_entity_id": "sensor.tesla_wallbox"})
    assert not await changed()
    hass.config_entries.async_update_entry(tesla, options={**tesla.options, "site_priority": 2})
    assert await changed()

    await hass.config_entries.async_remove(tesla.entry_id)
    assert await changed()
    assert coordinators[-1].auto_ignored_entities == ["sensor.zoe_wallbox"]


async def test_cars_do_not_reload_each_other(hass: HomeAssistant) -> None:
    """Cars 1 and 2 are set up. Car 3 (priority 3) is behind both: they reload once each. Car 4 (priority 1) is behind
    none of them: no reload at all, and nothing reloads by itself afterwards."""
    first = make_entry("first", {**MOCK_OPTIONS, "charger_power_entity_id": "sensor.first_power", "site_priority": 1})
    second = make_entry("second", {**MOCK_OPTIONS, "charger_power_entity_id": "sensor.second_power", "site_priority": 2})
    set_source_states(hass)
    # present, so the start does not wait two minutes for a missing entity
    for charger in ("sensor.first_power", "sensor.second_power", "sensor.third_power", "sensor.fourth_power"):
        hass.states.async_set(charger, "0", {"unit_of_measurement": "W", "device_class": "power"})
    for entry in (first, second):
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(first.entry_id)  # sets up every entry of the integration
    await _settle(hass)
    # the car with the first right leaves out the one behind it, the one behind it nothing
    assert first.runtime_data.auto_ignored_entities == ["sensor.second_power"]
    assert second.runtime_data.auto_ignored_entities == []
    before = {entry.entry_id: entry.runtime_data for entry in (first, second)}

    third = car(hass, "sensor.third_power", "third", announce=True, priority=3)
    await _settle(hass)
    await _settle(hass)
    for entry in (first, second):
        assert entry.runtime_data is not before[entry.entry_id], "reloaded for the car behind it"
        for name in ("_executor", "_what_if_executor"):
            await hass.async_add_executor_job(getattr(before[entry.entry_id], name).shutdown, True)
    assert first.runtime_data.auto_ignored_entities == ["sensor.second_power", "sensor.third_power"]
    assert second.runtime_data.auto_ignored_entities == ["sensor.third_power"]

    # a car with priority 1 is behind none of them: no reload, now or later
    reloaded = {entry.entry_id: entry.runtime_data for entry in (first, second)}
    car(hass, "sensor.fourth_power", "fourth", announce=True, priority=1)
    await _settle(hass)
    await _settle(hass)
    assert all(entry.runtime_data is reloaded[entry.entry_id] for entry in (first, second))
    for entry in (first, second):
        await async_unload_and_join(hass, entry)
    assert third.state is not ConfigEntryState.LOADED


async def test_the_home_page_shows_the_chargers_and_the_switch(hass: HomeAssistant, loaded_entry) -> None:
    car(hass, "sensor.zoe_power")
    old_coordinator = loaded_entry.runtime_data
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "menu_home"})
    assert result["step_id"] == "home"
    assert result["description_placeholders"]["cjp_chargers"] == "sensor.zoe_power"
    assert form_defaults(result)["ignore_cjp_chargers"] is True
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**form_defaults(result), "ignore_cjp_chargers": False})
    assert result["step_id"] == "powerwall"
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_defaults(result))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert loaded_entry.options["ignore_cjp_chargers"] is False
    await _settle(hass)
    for name in ("_executor", "_what_if_executor"):
        await hass.async_add_executor_job(getattr(old_coordinator, name).shutdown, True)
    assert DOMAIN == loaded_entry.domain


async def test_a_charger_of_a_car_behind_is_subtracted_with_its_unit(hass: HomeAssistant, mock_entry) -> None:
    car(hass, "sensor.zoe_power")
    hass.states.async_set("sensor.zoe_power", "7.0", {"unit_of_measurement": "kW", "device_class": "power"})
    set_source_states(hass)
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await _settle(hass)

    coordinator = mock_entry.runtime_data
    snapshot = await coordinator._async_snapshot()
    assert snapshot.units["sensor.zoe_power"] == "kW"
    assert coordinator.engine.config["home"]["entity_ids"]["ignore_consumption_from_entity_ids"] == ["sensor.zoe_power"]
    await async_unload_and_join(hass, mock_entry)


# --- what the planner counts as house consumption, in watts ------------------------------------------------------------

SOLAR = "sensor.solar_production"
CAR_A = "sensor.charger_power"  # the charger of the car under test (OPTIONS)
CAR_B = "sensor.car_b_power"
NOW = datetime.datetime(2026, 6, 1, 12, 0)


def engine_with(
    readings: dict[str, float],
    *,
    ignored: list[str] | None = None,
    own: str = CAR_A,
    ages: dict[str, float] | None = None,
    now_reads: dict[str, float] | None = None,
) -> CJPEngine:
    """An engine whose recorder answers with these watts (the average of the window), with solar, leaving out these sensors.

    now_reads: what the sensors read at this moment, when that is not the same as the average of the window."""
    options = {**OPTIONS, "production_entity_id": SOLAR, "charger_power_entity_id": own, "ignore_consumption_from_entity_ids": ignored or []}
    engine = CJPEngine(build_config(options), "cjp_test", TEXTS)
    engine.config["home"]["charger_entity_ids"] = [CAR_B, STUCK]  # what the coordinator sets from the cars behind this one
    engine._history = SimpleNamespace(
        get_average_value=lambda entity_id, *args, **kwargs: readings[entity_id],
        get_values=lambda *args, **kwargs: [0.0],
    )
    states = {entity_id: str(watts) for entity_id, watts in {**readings, **(now_reads or {})}.items()}
    engine._snap = Snapshot(now=NOW, tz=None, states=states, report_ages=ages or {})
    return engine


def surplus(engine: CJPEngine) -> float:
    """The solar surplus a car can charge from: what solar gives after the house, without the car's own charging."""
    return engine.local_energy_available(period=60, without_all_exclusion=True, solar_only=True)


def test_the_first_priority_has_the_whole_surplus_and_the_next_gets_what_is_left() -> None:
    """House 600 W, car A (priority 1) charges 3 kW, car B (priority 2) 2 kW, solar 5 kW; the house sensor has both chargers in it."""
    readings = {"sensor.consumption": 5600.0, SOLAR: 5000.0, CAR_A: 3000.0, CAR_B: 2000.0}

    # A leaves out B (behind it) and its own charging: it is offered everything but the house
    car_a = engine_with(readings, ignored=[CAR_B], own=CAR_A)
    assert car_a.power_values(period=60)["power_consumption_without_all_exclusion"] == 600.0
    assert surplus(car_a) == 4400.0
    # B leaves out nothing behind it, only its own 2 kW: A's 3 kW is consumption of the house, so only 1.4 kW is left for B
    car_b = engine_with(readings, ignored=[], own=CAR_B)
    assert car_b.power_values(period=60)["power_consumption_without_all_exclusion"] == 3600.0
    assert surplus(car_b) == 1400.0


def test_cars_with_the_same_priority_leave_nothing_out_and_each_counts_the_other_as_house() -> None:
    readings = {"sensor.consumption": 5600.0, SOLAR: 5000.0, CAR_A: 3000.0, CAR_B: 2000.0}

    assert surplus(engine_with(readings, ignored=[], own=CAR_A)) == 2400.0  # 5000 - (5600 - 3000)
    assert surplus(engine_with(readings, ignored=[], own=CAR_B)) == 1400.0  # 5000 - (5600 - 2000)


def test_a_charger_listed_twice_is_left_out_once() -> None:
    readings = {"sensor.consumption": 5600.0, SOLAR: 5000.0, CAR_A: 3000.0, CAR_B: 2000.0}

    assert surplus(engine_with(readings, ignored=[CAR_B, "", CAR_B], own=CAR_A)) == 4400.0
    # not 2 x 2000 off the house, which would add to the surplus
    assert engine_with(readings, ignored=[CAR_B, CAR_B], own=CAR_A).power_from_ignored(None, None) == 2000.0


def test_a_consumer_that_reads_more_than_the_house_does_not_add_to_the_surplus() -> None:
    """The house sensor says 1 kW, the left-out charger 4 kW (a wrong or stuck sensor): not -3 kW of house."""
    readings = {"sensor.consumption": 1000.0, SOLAR: 2000.0, CAR_A: 0.0, CAR_B: 4000.0}
    engine = engine_with(readings, ignored=[CAR_B])

    assert engine.power_values(period=60)["power_consumption_without_ignored"] == -3000.0  # as measured, the stored value is kept
    assert engine.local_energy_available(period=60, solar_only=True) == 2000.0  # never more than solar gives
    assert engine.local_energy_available(period=60) == 2000.0  # not solar + 3000 W of "negative house"
    assert surplus(engine) == 2000.0


# --- a left-out sensor that has stopped reporting ----------------------------------------------------------------------

STUCK = "sensor.stuck_charger"
FRESH_S, STALE_S = 60.0, 31 * 60.0
WARNING = "has not reported for over 30 minutes"


def test_a_stuck_consumer_is_not_subtracted_and_one_warning_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Stuck at 11 kW without being unavailable: subtracting it for ever would take the house consumption to 0."""
    readings = {"sensor.consumption": 11500.0, SOLAR: 0.0, CAR_A: 0.0, STUCK: 11000.0}
    engine = engine_with(readings, ignored=[STUCK], ages={STUCK: STALE_S})

    with caplog.at_level(logging.WARNING):
        assert engine.power_from_ignored(None, None) == 0.0
        assert engine.power_from_ignored(None, None) == 0.0
        values = engine.power_values(period=60)
    assert values["ignored_consumption"] == 0.0 and values["power_consumption_without_ignored"] == 11500.0
    assert sum(1 for record in caplog.records if WARNING in record.getMessage() and STUCK in record.getMessage()) == 1


def test_a_steady_charge_that_keeps_reporting_is_subtracted() -> None:
    """The value has not changed for hours (steady 11 kW) but the charger reports: last_reported is fresh, so it is not stuck."""
    readings = {"sensor.consumption": 11500.0, SOLAR: 0.0, CAR_A: 0.0, STUCK: 11000.0}

    assert engine_with(readings, ignored=[STUCK], ages={STUCK: FRESH_S}).power_from_ignored(None, None) == 11000.0
    assert engine_with(readings, ignored=[STUCK], ages={STUCK: 30 * 60.0}).power_from_ignored(None, None) == 11000.0  # not over the limit yet


def test_no_report_time_counts_as_fresh_and_a_stuck_zero_changes_nothing(caplog: pytest.LogCaptureFixture) -> None:
    readings = {"sensor.consumption": 11500.0, SOLAR: 0.0, CAR_A: 0.0, STUCK: 11000.0}
    assert engine_with(readings, ignored=[STUCK], ages={}).power_from_ignored(None, None) == 11000.0  # no last_reported known

    readings[STUCK] = 0.0
    with caplog.at_level(logging.WARNING):
        assert engine_with(readings, ignored=[STUCK], ages={STUCK: STALE_S}).power_from_ignored(None, None) == 0.0
    assert not [record for record in caplog.records if WARNING in record.getMessage()]


def test_the_hours_since_it_got_stuck_are_corrected_too_and_the_hours_before_are_as_recorded() -> None:
    """A charger hangs at 700 W three hours ago (stuck since 2.5 hours ago): the hours that are recomputed until they are cached must not keep subtracting it."""
    readings = {"sensor.consumption": 1500.0, SOLAR: 0.0, CAR_A: 0.0, STUCK: 700.0}
    engine = engine_with(readings, ignored=[STUCK], ages={STUCK: 3 * 3600.0})
    hour = datetime.timedelta(hours=1)

    assert engine.power_from_ignored(NOW - 70 * hour / 60, NOW - 10 * hour / 60) == 0.0  # the last hour but one
    assert engine.power_from_ignored(NOW - 3 * hour, NOW - 2 * hour) == 0.0  # ended 2 h ago, after it got stuck
    assert engine.power_from_ignored(NOW - 5 * hour, NOW - 4 * hour) == 700.0  # ended before: as recorded


def test_a_listed_sensor_that_is_not_a_charger_is_always_subtracted(caplog: pytest.LogCaptureFixture) -> None:
    """A plug or a template sensor of a steady load may report only when its value changes: it is not 'stuck' after 30 minutes."""
    readings = {"sensor.consumption": 1500.0, SOLAR: 0.0, CAR_A: 0.0, "sensor.pool_pump": 700.0}
    engine = engine_with(readings, ignored=["sensor.pool_pump"], ages={"sensor.pool_pump": 3 * 3600.0})

    with caplog.at_level(logging.WARNING):
        assert engine.power_from_ignored(None, None) == 700.0
    assert not [record for record in caplog.records if WARNING in record.getMessage()]


def test_a_charger_that_stopped_and_reads_zero_is_still_subtracted_for_what_it_charged_in_the_window() -> None:
    """It charged 11 kW until 20 minutes ago, reads 0 W now and does not write an unchanged 0 again: it is not stuck."""
    readings = {"sensor.consumption": 11500.0, SOLAR: 0.0, CAR_A: 0.0, STUCK: 3667.0}  # the average of the last hour
    engine = engine_with(readings, ignored=[STUCK], ages={STUCK: STALE_S}, now_reads={STUCK: 0.0})

    assert engine.power_from_ignored(None, None) == 3667.0


def test_the_window_given_the_other_way_round_is_still_the_live_one(caplog: pytest.LogCaptureFixture) -> None:
    """local_energy_available(timeFrom=0, timeTo=X) gives from=now and to=X minutes ago: the solar charging decision reads it."""
    readings = {"sensor.consumption": 11500.0, SOLAR: 5000.0, CAR_A: 0.0, STUCK: 11000.0}
    stuck = engine_with(readings, ignored=[STUCK], ages={STUCK: STALE_S})
    fresh = engine_with(readings, ignored=[STUCK], ages={STUCK: FRESH_S})

    with caplog.at_level(logging.WARNING):
        assert stuck.local_energy_available(timeFrom=0, timeTo=10, without_all_exclusion=True, solar_only=True) == 0.0  # 11.5 kW house, 5 kW solar
    assert fresh.local_energy_available(timeFrom=0, timeTo=10, without_all_exclusion=True, solar_only=True) == 4500.0  # 500 W house
    assert [record for record in caplog.records if WARNING in record.getMessage()]


def test_history_windows_are_not_changed_by_a_stuck_consumer(caplog: pytest.LogCaptureFixture) -> None:
    """Only the live window is corrected: an hour that was recorded with the charger working keeps its value."""
    readings = {"sensor.consumption": 11500.0, SOLAR: 0.0, CAR_A: 0.0, STUCK: 11000.0}
    engine = engine_with(readings, ignored=[STUCK], ages={STUCK: STALE_S})

    with caplog.at_level(logging.WARNING):
        assert engine.power_from_ignored(NOW - datetime.timedelta(hours=2), NOW - datetime.timedelta(hours=1)) == 11000.0
    assert not [record for record in caplog.records if WARNING in record.getMessage()]


def test_a_consumer_that_reports_again_is_subtracted_and_warned_about_again_when_it_sticks_again(caplog: pytest.LogCaptureFixture) -> None:
    readings = {"sensor.consumption": 11500.0, SOLAR: 0.0, CAR_A: 0.0, STUCK: 11000.0}
    engine = engine_with(readings, ignored=[STUCK], ages={STUCK: STALE_S})

    def watts(age: float) -> float:
        engine._snap = Snapshot(now=NOW, tz=None, states={STUCK: "11000.0"}, report_ages={STUCK: age})
        return engine.power_from_ignored(None, None)

    with caplog.at_level(logging.WARNING):
        assert watts(STALE_S) == 0.0
        assert watts(FRESH_S) == 11000.0  # it reports again
        assert watts(STALE_S) == 0.0  # and sticks again
    assert sum(1 for record in caplog.records if WARNING in record.getMessage()) == 2


async def test_the_snapshot_has_the_time_since_each_sensor_reported(hass: HomeAssistant, mock_entry) -> None:
    car(hass, "sensor.zoe_power")
    hass.states.async_set("sensor.zoe_power", "7.0", {"unit_of_measurement": "kW", "device_class": "power"})
    set_source_states(hass)
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await _settle(hass)

    snapshot = await mock_entry.runtime_data._async_snapshot()

    assert 0.0 <= snapshot.report_ages["sensor.zoe_power"] < 60.0
    await async_unload_and_join(hass, mock_entry)
