"""The chargers of the cars with a lower priority are left out of the house consumption (utils/chargers.py)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigEntryChange, ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cable_juice_planner.const import DOMAIN, build_config, upgrade_options
from custom_components.cable_juice_planner.coordinator import CJPCoordinator
from custom_components.cable_juice_planner.engine import CJPEngine
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
