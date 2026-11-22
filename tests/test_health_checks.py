"""Health checks: the repairs of a car or battery entry for what has gone wrong, removed only once it is fixed."""
from __future__ import annotations

import datetime
import json
import re
import time
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.automation import EVENT_AUTOMATION_RELOADED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.cable_juice_planner.const import DOMAIN
from custom_components.cable_juice_planner.engine.history.history import _CONVERSION_FACTORS
from custom_components.cable_juice_planner.management import health_checks
from custom_components.cable_juice_planner.management.automation_inputs import blueprint_automation_ids
from custom_components.cable_juice_planner.management.dependency_checks import remove_issues
from custom_components.cable_juice_planner.management.health_checks import (
    HEALTH_ISSUES,
    ISSUE_BLUEPRINT,
    ISSUE_MISSING,
    ISSUE_NOT_FOLLOWING,
    ISSUE_STATUS,
    ISSUE_UNITS,
    NOT_FOLLOWING_EVENTS,
    POWER_UNITS,
    RECHECK_DELAY,
    STATUS_RECHECK_DELAY,
    STATUS_UNKNOWN_DAYS,
    HealthWatcher,
    async_remove_health_store,
    store_key,
)
from custom_components.cable_juice_planner.site import ISSUE_PHASE_UNITS

from .conftest import async_setup, async_unload_and_join, make_entry, make_site_entry, set_source_states

ACTION = "sensor.cjp_bm_charger_action"
OTHER_ACTION = "sensor.cjp_other_charger_action"
AUTOMATION_ID = "1700000000001"
OTHER_ID = "1700000000002"
STATUS = "sensor.test_charger_status"
DISABLED = er.RegistryEntryDisabler.USER
SETTLE_ONE_HOUR = {"hours": 1, "seconds": 1}


@pytest.fixture(autouse=True)
def config_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    """Removing an entry exports a backup into the config folder: keep it out of the shared test config."""
    hass.config.config_dir = str(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def storage(hass_storage: dict[str, Any]) -> dict[str, Any]:
    return hass_storage


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    result = MockConfigEntry(domain=DOMAIN, title="Cable Juice Planner (bm)")
    result.add_to_hass(hass)
    return result


@pytest.fixture
def automations(config_dir: Path) -> Path:
    return config_dir / "automations.yaml"


@pytest.fixture
def watchers():
    """The watchers of a test are stopped at its end (their timers and listeners)."""
    created: list[HealthWatcher] = []
    yield created
    for watcher in created:
        watcher.stop()


def make(hass: HomeAssistant, entry: MockConfigEntry, path: Path, watchers: list[HealthWatcher], **options: Any) -> HealthWatcher:
    values: dict[str, Any] = {
        "entities": [],
        "watched": frozenset(),
        "charger_power_entities": [],
        "power_entities": [],
        "battery_entities": [],
        "energy_entities": [],
        "status_entity": "",
        "action_sensor": ACTION,
        "automations_path": str(path),
        **options,
    }
    watcher = HealthWatcher(hass, entry, "bm", **values)
    watchers.append(watcher)
    return watcher


async def settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)


async def start(hass: HomeAssistant, watcher: HealthWatcher) -> HealthWatcher:
    await watcher.async_load()
    watcher.start()  # Home Assistant runs in the tests: the first check comes at once
    await settle(hass)
    return watcher


async def advance(hass: HomeAssistant, **delta: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + datetime.timedelta(**delta))
    await settle(hass)


def ago(**delta: float) -> float:
    """An epoch timestamp for states that changed some time ago (async_set's timestamp)."""
    return time.time() - datetime.timedelta(**delta).total_seconds()


def issue(hass: HomeAssistant, entry: MockConfigEntry, name: str):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{entry.entry_id}_{name}")


def create_issue(hass: HomeAssistant, entry: MockConfigEntry, name: str) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{entry.entry_id}_{name}",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=name,
        translation_placeholders={"prefix": "bm"},
    )


def seed(storage: dict[str, Any], entry: MockConfigEntry, data: dict[str, Any]) -> None:
    """The state a restart finds in the store (the mocked Store reads hass_storage)."""
    key = store_key(entry.entry_id)
    storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": data}


def write_automations(path: Path, *items: str) -> None:
    path.write_text("".join(items) if items else "[]\n", encoding="utf-8")


def blueprint_item(automation_id: str, sensor: str = ACTION, blueprint: str = "cable_juice_planner/charger_control.yaml") -> str:
    return (
        f"- id: '{automation_id}'\n  alias: Laden\n  use_blueprint:\n    path: {blueprint}\n"
        f"    input:\n      action_sensor: {sensor}\n"
    )


def register_automation(hass: HomeAssistant, automation_id: str = AUTOMATION_ID, state: str = "on", name: str = "Laden") -> str:
    entity_id = er.async_get(hass).async_get_or_create("automation", "automation", automation_id, suggested_object_id="laden").entity_id
    hass.states.async_set(entity_id, state, {"friendly_name": name})
    return entity_id


def placeholders_of(text: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", text))


# ---------------------------------------------------------------------- #
# Entities that are disabled or missing
# ---------------------------------------------------------------------- #


async def test_a_disabled_or_missing_entity_is_a_repair_until_it_is_back(hass, entry, automations, watchers) -> None:
    registry = er.async_get(hass)
    registry.async_get_or_create("sensor", "test", "power", suggested_object_id="charger_power", disabled_by=DISABLED)
    hass.states.async_set("sensor.unavailable_one", "unavailable")  # only unavailable: not listed (the 30 minute notification does)
    watcher = make(hass, entry, automations, watchers, entities=["sensor.charger_power", "sensor.gone", "sensor.unavailable_one", "sensor.watched"], watched=frozenset({"sensor.watched"}))
    await start(hass, watcher)

    found = issue(hass, entry, ISSUE_MISSING)
    assert found is not None and found.translation_key == ISSUE_MISSING and not found.is_fixable
    assert found.severity == ir.IssueSeverity.WARNING
    assert found.translation_placeholders["prefix"] == "bm"
    assert found.translation_placeholders["disabled"] == "- sensor.charger_power"
    assert found.translation_placeholders["missing"] == "- sensor.gone"

    watcher.stop()
    assert issue(hass, entry, ISSUE_MISSING), "stopping keeps the repair"

    # enabled again and found again: the next check after the registry change (a minute later) removes it
    registry.async_update_entity("sensor.charger_power", disabled_by=None)
    hass.states.async_set("sensor.charger_power", "0")
    hass.states.async_set("sensor.gone", "1")
    restarted = make(hass, entry, automations, watchers, entities=["sensor.charger_power", "sensor.gone"])
    await start(hass, restarted)
    assert issue(hass, entry, ISSUE_MISSING) is None


async def test_the_entities_of_a_registry_change_are_checked_a_minute_later(hass, entry, automations, watchers) -> None:
    watcher = await start(hass, make(hass, entry, automations, watchers, entities=["sensor.later"]))
    assert issue(hass, entry, ISSUE_MISSING)
    # registered again (before any state exists: a state would make the registry pick another entity id)
    er.async_get(hass).async_get_or_create("sensor", "test", "later", suggested_object_id="later")
    await settle(hass)
    assert issue(hass, entry, ISSUE_MISSING), "not checked before the integration had time to reload"
    await advance(hass, seconds=RECHECK_DELAY + 1)
    assert issue(hass, entry, ISSUE_MISSING) is None
    watcher.stop()


async def test_nothing_is_judged_before_home_assistant_has_started(hass, entry, automations, watchers) -> None:
    """A repair from before a restart stays until the first check after the start; the watcher doesn't decide before it."""
    create_issue(hass, entry, ISSUE_MISSING)
    watcher = make(hass, entry, automations, watchers, entities=["sensor.present"])
    await watcher.async_load()  # not started
    hass.states.async_set("sensor.present", "1")
    await watcher.async_check()
    assert issue(hass, entry, ISSUE_MISSING), "a check before the start must not remove the repair"


@pytest.mark.parametrize("name", [ISSUE_UNITS, ISSUE_STATUS, ISSUE_NOT_FOLLOWING])
async def test_an_ignored_repair_stays_ignored_while_the_problem_persists(hass, entry, automations, watchers, name) -> None:
    if name == ISSUE_UNITS:
        hass.states.async_set("sensor.battery", "50", {"unit_of_measurement": "kWh"})
        watcher = make(hass, entry, automations, watchers, battery_entities=["sensor.battery"])
    elif name == ISSUE_STATUS:
        hass.states.async_set(STATUS, "wobbly", timestamp=ago(minutes=11))
        watcher = make(hass, entry, automations, watchers, status_entity=STATUS)
    else:
        watcher = make(hass, entry, automations, watchers)
    await start(hass, watcher)
    if name == ISSUE_NOT_FOLLOWING:
        for _ in range(NOT_FOLLOWING_EVENTS):
            watcher.record_charger_problem("gave_up")
        await settle(hass)
    assert issue(hass, entry, name), "the problem is raised first"

    ir.async_ignore_issue(hass, DOMAIN, f"{entry.entry_id}_{name}", True)
    await advance(hass, hours=1, seconds=1)  # the hourly check: the problem is still there
    found = issue(hass, entry, name)
    assert found is not None and found.dismissed_version is not None


async def test_an_attribute_change_of_a_listed_automation_is_no_check(hass, entry, automations, watchers, monkeypatch) -> None:
    write_automations(automations, blueprint_item(AUTOMATION_ID))
    entity_id = register_automation(hass, state="on")
    calls: list[tuple] = []
    original = health_checks.blueprint_automation_ids

    def counted(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(health_checks, "blueprint_automation_ids", counted)
    await start(hass, make(hass, entry, automations, watchers))
    checked = len(calls)
    hass.states.async_set(entity_id, "on", {"friendly_name": "Laden", "last_triggered": "2026-11-21T10:00:00+00:00"})
    await settle(hass)
    assert len(calls) == checked, "last_triggered every run is no reason to read automations.yaml"
    hass.states.async_set(entity_id, "off", {"friendly_name": "Laden"})
    await settle(hass)
    assert len(calls) == checked + 1 and issue(hass, entry, ISSUE_BLUEPRINT) is not None


async def test_an_ignored_repair_stays_ignored_while_it_persists(hass, entry, automations, watchers) -> None:
    watcher = await start(hass, make(hass, entry, automations, watchers, entities=["sensor.gone"]))
    ir.async_ignore_issue(hass, DOMAIN, f"{entry.entry_id}_{ISSUE_MISSING}", True)
    await advance(hass, **SETTLE_ONE_HOUR)
    found = issue(hass, entry, ISSUE_MISSING)
    assert found is not None and found.dismissed_version is not None
    watcher.stop()


# ---------------------------------------------------------------------- #
# Units the code can't read
# ---------------------------------------------------------------------- #


def test_the_power_units_are_the_ones_power_convert_reads_as_watt() -> None:
    assert POWER_UNITS == ("mW", "W", "kW", "MW", "GW")
    assert all("W" in _CONVERSION_FACTORS[unit] for unit in POWER_UNITS)


@pytest.mark.parametrize(("unit", "accepted"), [("mW", True), ("W", True), ("kW", True), ("MW", True), ("GW", True), ("Wh", True), ("kWh", True), ("MWh", True), ("A", False), ("kVA", False)])
def test_a_power_sensor_accepts_the_units_every_reader_reads(unit: str, accepted: bool) -> None:
    """Every reader uses utils/power.py: a unit with an h is read as the unit without it (as in BattMind)."""
    assert health_checks._power_unit_ok(unit) is accepted


@pytest.mark.parametrize(("unit", "accepted"), [("kWh", True), ("Wh", False), ("MWh", False), ("kW", False), ("%", False)])
def test_a_kwh_meter_accepts_only_kwh(unit: str, accepted: bool) -> None:
    """The history and the sessions read the meter raw as kWh; only the charging loss and the battery simulation convert."""
    assert health_checks._energy_unit_ok(unit) is accepted


def _open_charger_blueprint_update(hass: HomeAssistant) -> None:
    ir.async_create_issue(
        hass, DOMAIN, health_checks.CHARGER_BLUEPRINT_UPDATE_ISSUE, is_fixable=True, severity=ir.IssueSeverity.WARNING,
        translation_key="blueprint_update",
    )


@pytest.mark.parametrize(("unit", "accepted"), [("W", True), ("kW", True), ("kWh", True), ("Wh", True), ("mW", True), ("MW", True), ("A", False)])
async def test_the_charger_power_accepts_the_power_units_with_the_bundled_blueprint(hass, entry, automations, watchers, unit, accepted) -> None:
    watcher = make(hass, entry, automations, watchers)
    assert watcher._charger_power_unit_ok(unit) is accepted


@pytest.mark.parametrize(("unit", "accepted"), [("W", True), ("kW", True), ("kWh", False), ("Wh", False), ("mW", False), ("MW", False), ("A", False)])
async def test_the_charger_power_accepts_only_watt_and_kilowatt_with_an_older_blueprint(hass, entry, automations, watchers, unit, accepted) -> None:
    """While the blueprint update repair is open (also ignored), the installed blueprint reads any other unit as W."""
    _open_charger_blueprint_update(hass)
    watcher = make(hass, entry, automations, watchers)
    assert watcher._charger_power_unit_ok(unit) is accepted


async def test_a_unit_the_code_cannot_read_is_a_repair_until_the_sensors_read_right(hass, entry, automations, watchers) -> None:
    hass.states.async_set("sensor.power_amps", "0", {"unit_of_measurement": "A"})
    hass.states.async_set("sensor.power_none", "0")  # no unit: read as W, not flagged
    hass.states.async_set("sensor.power_kwh", "1", {"unit_of_measurement": "kWh"})  # read as kW: not flagged
    hass.states.async_set("sensor.battery", "50", {"unit_of_measurement": "kWh"})
    hass.states.async_set("sensor.meter", "1", {"unit_of_measurement": "kW"})  # a kWh meter in kW: flagged
    # the charger power in kWh while the installed blueprint is older: flagged
    _open_charger_blueprint_update(hass)
    hass.states.async_set("sensor.charger_power", "1", {"unit_of_measurement": "kWh"})
    watcher = await start(
        hass,
        make(
            hass,
            entry,
            automations,
            watchers,
            # the charger power also listed as a power sensor keeps the charger power's rule
            charger_power_entities=["sensor.charger_power"],
            power_entities=["sensor.power_amps", "sensor.power_none", "sensor.power_kwh", "sensor.charger_power"],
            battery_entities=["sensor.battery"],
            energy_entities=["sensor.meter"],
        ),
    )

    found = issue(hass, entry, ISSUE_UNITS)
    assert found is not None
    entities = found.translation_placeholders["entities"]
    assert "- sensor.power_amps (A)" in entities and "- sensor.battery (kWh)" in entities and "- sensor.meter (kW)" in entities
    assert "sensor.power_kwh" not in entities
    assert "- sensor.charger_power (kWh)" in entities
    assert "sensor.power_none" not in entities
    assert found.translation_placeholders["power_units"] == "mW, W, kW, MW, GW"
    assert found.translation_placeholders["energy_units"] == "kWh"

    # the blueprint was updated (the fix deletes its repair and reloads the automations): the charger power in kWh is read
    ir.async_delete_issue(hass, DOMAIN, health_checks.CHARGER_BLUEPRINT_UPDATE_ISSUE)
    hass.states.async_set("sensor.power_amps", "0", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.battery", "50", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.meter", "1", {"unit_of_measurement": "kWh"})
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_UNITS) is None
    watcher.stop()


async def test_a_flagged_sensor_that_is_unavailable_keeps_its_verdict(hass, entry, automations, watchers) -> None:
    hass.states.async_set("sensor.battery", "50", {"unit_of_measurement": "kWh"})
    watcher = await start(hass, make(hass, entry, automations, watchers, battery_entities=["sensor.battery"]))
    assert issue(hass, entry, ISSUE_UNITS)

    hass.states.async_set("sensor.battery", "unavailable", {"unit_of_measurement": "kWh"})
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_UNITS), "unavailable is not a fix"

    hass.states.async_set("sensor.battery", "50", {"unit_of_measurement": "%"})
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_UNITS) is None
    watcher.stop()


async def test_a_sensor_unavailable_since_the_start_is_not_judged_so_the_repair_stays(hass, entry, automations, watchers) -> None:
    hass.states.async_set("sensor.power_ok", "0", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.power_unknown", "unknown")  # never judged: nothing can be said yet
    create_issue(hass, entry, ISSUE_UNITS)
    watcher = await start(hass, make(hass, entry, automations, watchers, power_entities=["sensor.power_ok", "sensor.power_unknown"]))
    assert issue(hass, entry, ISSUE_UNITS), "the repair stays until every sensor has been judged"

    hass.states.async_set("sensor.power_unknown", "0", {"unit_of_measurement": "W"})
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_UNITS) is None
    watcher.stop()


# ---------------------------------------------------------------------- #
# The blueprint automation
# ---------------------------------------------------------------------- #


def test_the_blueprint_automations_of_the_action_sensor_are_listed(tmp_path: Path) -> None:
    path = tmp_path / "automations.yaml"
    path.write_text(
        blueprint_item(AUTOMATION_ID)
        + blueprint_item(OTHER_ID, sensor=OTHER_ACTION)
        + "- alias: no id\n  use_blueprint:\n    path: cable_juice_planner/charger_control.yaml\n    input:\n      action_sensor: " + ACTION + "\n"
        + blueprint_item("1700000000004", blueprint="other/blueprint.yaml")
        + "- id: '1700000000005'\n  alias: no blueprint\n  trigger: []\n",
        encoding="utf-8",
    )
    assert blueprint_automation_ids(str(path), ACTION) == [AUTOMATION_ID]
    assert blueprint_automation_ids(str(path), OTHER_ACTION) == [OTHER_ID]


def test_a_missing_automations_file_lists_nothing(tmp_path: Path) -> None:
    assert blueprint_automation_ids(str(tmp_path / "automations.yaml"), ACTION) == []


@pytest.mark.parametrize("content", ["- id: [unclosed\n", "id: 1\n", "", "just text\n"])
def test_an_automations_file_that_cannot_be_read_is_not_known(tmp_path: Path, content: str) -> None:
    path = tmp_path / "automations.yaml"
    path.write_text(content, encoding="utf-8")
    assert blueprint_automation_ids(str(path), ACTION) is None


def test_an_empty_list_is_no_automation(tmp_path: Path) -> None:
    path = tmp_path / "automations.yaml"
    path.write_text("[]\n", encoding="utf-8")
    assert blueprint_automation_ids(str(path), ACTION) == []


async def test_a_blueprint_automation_that_was_never_on_is_no_repair(hass, entry, automations, watchers) -> None:
    write_automations(automations, blueprint_item(AUTOMATION_ID))
    register_automation(hass, state="off")
    await start(hass, make(hass, entry, automations, watchers))
    assert issue(hass, entry, ISSUE_BLUEPRINT) is None


async def test_an_automation_of_the_user_or_of_another_sensor_is_never_a_repair(hass, entry, automations, watchers) -> None:
    write_automations(automations, blueprint_item(OTHER_ID, sensor=OTHER_ACTION))
    register_automation(hass, OTHER_ID, state="on")
    await start(hass, make(hass, entry, automations, watchers))
    hass.states.async_set("automation.laden", "off", {"friendly_name": "Laden"})
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_BLUEPRINT) is None


async def test_a_blueprint_automation_switched_off_is_a_repair_until_it_is_on_again(hass, entry, automations, watchers) -> None:
    write_automations(automations, blueprint_item(AUTOMATION_ID))
    entity_id = register_automation(hass, state="on")
    await start(hass, make(hass, entry, automations, watchers))
    assert issue(hass, entry, ISSUE_BLUEPRINT) is None  # seen on

    hass.states.async_set(entity_id, "off", {"friendly_name": "Laden"})  # a change of a listed automation checks at once
    await settle(hass)
    found = issue(hass, entry, ISSUE_BLUEPRINT)
    assert found is not None and found.translation_placeholders["automations"] == "- Laden"
    assert found.translation_placeholders["prefix"] == "bm"

    hass.states.async_set(entity_id, "on", {"friendly_name": "Laden"})
    await settle(hass)
    assert issue(hass, entry, ISSUE_BLUEPRINT) is None


async def test_a_blueprint_automation_disabled_in_the_registry_is_a_repair(hass, entry, automations, watchers) -> None:
    """A disabled entity has no state at all: it is off, not unclear, and named by its registry name."""
    write_automations(automations, blueprint_item(AUTOMATION_ID))
    entity_id = register_automation(hass, state="on")
    watcher = await start(hass, make(hass, entry, automations, watchers))
    assert issue(hass, entry, ISSUE_BLUEPRINT) is None  # seen on

    er.async_get(hass).async_update_entity(entity_id, disabled_by=er.RegistryEntryDisabler.USER, name="Ladestyring")
    hass.states.async_remove(entity_id)
    await watcher.async_check()
    found = issue(hass, entry, ISSUE_BLUEPRINT)
    assert found is not None and found.translation_placeholders["automations"] == "- Ladestyring"


async def test_a_blueprint_automation_deleted_from_the_file_is_a_repair(hass, entry, automations, watchers) -> None:
    write_automations(automations, blueprint_item(AUTOMATION_ID))
    register_automation(hass, state="on")
    watcher = await start(hass, make(hass, entry, automations, watchers))

    write_automations(automations)  # the file holds no such automation any more
    hass.bus.async_fire(EVENT_AUTOMATION_RELOADED)  # the reload checks at once
    await settle(hass)
    found = issue(hass, entry, ISSUE_BLUEPRINT)
    assert found is not None and found.translation_placeholders["automations"] == "–"

    write_automations(automations, blueprint_item(AUTOMATION_ID))
    hass.bus.async_fire(EVENT_AUTOMATION_RELOADED)
    await settle(hass)
    assert issue(hass, entry, ISSUE_BLUEPRINT) is None
    watcher.stop()


async def test_a_blueprint_automation_that_cannot_be_judged_leaves_the_repair_as_it_is(hass, entry, automations, watchers) -> None:
    write_automations(automations, blueprint_item(AUTOMATION_ID))
    entity_id = register_automation(hass, state="on")
    watcher = await start(hass, make(hass, entry, automations, watchers))
    hass.states.async_set(entity_id, "off", {"friendly_name": "Laden"})
    await settle(hass)
    assert issue(hass, entry, ISSUE_BLUEPRINT)

    # a second listed automation the entity registry doesn't know (yet): not judged, so nothing changes
    write_automations(automations, blueprint_item(AUTOMATION_ID), blueprint_item(OTHER_ID))
    hass.bus.async_fire(EVENT_AUTOMATION_RELOADED)
    await settle(hass)
    assert issue(hass, entry, ISSUE_BLUEPRINT)

    # the automation is unavailable: not judged either
    hass.states.async_set(entity_id, "unavailable", {"friendly_name": "Laden"})
    write_automations(automations, blueprint_item(AUTOMATION_ID))
    hass.bus.async_fire(EVENT_AUTOMATION_RELOADED)
    await settle(hass)
    assert issue(hass, entry, ISSUE_BLUEPRINT)

    # an automations.yaml that can't be read
    automations.write_text("- id: [unclosed\n", encoding="utf-8")
    hass.bus.async_fire(EVENT_AUTOMATION_RELOADED)
    await settle(hass)
    assert issue(hass, entry, ISSUE_BLUEPRINT)

    # back on: the repair goes
    hass.states.async_set(entity_id, "on", {"friendly_name": "Laden"})
    write_automations(automations, blueprint_item(AUTOMATION_ID))
    hass.bus.async_fire(EVENT_AUTOMATION_RELOADED)
    await settle(hass)
    assert issue(hass, entry, ISSUE_BLUEPRINT) is None
    watcher.stop()


async def test_seen_on_is_kept_over_a_restart(hass, entry, automations, watchers) -> None:
    write_automations(automations, blueprint_item(AUTOMATION_ID))
    entity_id = register_automation(hass, state="on")
    first = await start(hass, make(hass, entry, automations, watchers))
    first.stop()
    hass.states.async_set(entity_id, "off", {"friendly_name": "Laden"})

    second = await start(hass, make(hass, entry, automations, watchers))
    assert issue(hass, entry, ISSUE_BLUEPRINT), "the flag was stored, so the repair is raised after the restart too"
    second.stop()


async def test_a_blueprint_repair_stays_ignored(hass, entry, automations, watchers) -> None:
    write_automations(automations, blueprint_item(AUTOMATION_ID))
    entity_id = register_automation(hass, state="on")
    watcher = await start(hass, make(hass, entry, automations, watchers))
    hass.states.async_set(entity_id, "off", {"friendly_name": "Laden"})
    await settle(hass)
    ir.async_ignore_issue(hass, DOMAIN, f"{entry.entry_id}_{ISSUE_BLUEPRINT}", True)

    await advance(hass, **SETTLE_ONE_HOUR)
    found = issue(hass, entry, ISSUE_BLUEPRINT)
    assert found is not None and found.dismissed_version is not None
    watcher.stop()


# ---------------------------------------------------------------------- #
# The charger status
# ---------------------------------------------------------------------- #


async def test_a_known_status_is_no_repair(hass, entry, automations, watchers) -> None:
    hass.states.async_set(STATUS, "charging", timestamp=ago(days=1))
    await start(hass, make(hass, entry, automations, watchers, status_entity=STATUS))
    assert issue(hass, entry, ISSUE_STATUS) is None


async def test_an_unknown_status_held_for_ten_minutes_is_a_repair(hass, entry, automations, watchers) -> None:
    hass.states.async_set(STATUS, "wobbly", timestamp=ago(minutes=11))
    watcher = await start(hass, make(hass, entry, automations, watchers, status_entity=STATUS))
    found = issue(hass, entry, ISSUE_STATUS)
    assert found is not None
    assert found.translation_placeholders["values"].startswith("- wobbly (")
    assert found.translation_placeholders["minutes"] == "10"
    assert found.translation_placeholders["days"] == str(STATUS_UNKNOWN_DAYS)
    watcher.stop()


async def test_an_unknown_status_not_yet_held_long_enough_is_no_repair(hass, entry, automations, watchers) -> None:
    hass.states.async_set(STATUS, "wobbly", timestamp=ago(minutes=2))
    watcher = await start(hass, make(hass, entry, automations, watchers, status_entity=STATUS))
    assert issue(hass, entry, ISSUE_STATUS) is None
    await advance(hass, seconds=STATUS_RECHECK_DELAY + 1)
    assert issue(hass, entry, ISSUE_STATUS) is None
    watcher.stop()


async def test_a_change_to_an_unknown_status_is_judged_once_it_could_be_held(hass, entry, automations, watchers) -> None:
    hass.states.async_set(STATUS, "charging")
    watcher = await start(hass, make(hass, entry, automations, watchers, status_entity=STATUS))
    hass.states.async_set(STATUS, "wobbly", timestamp=ago(minutes=11))
    await settle(hass)
    assert issue(hass, entry, ISSUE_STATUS) is None, "judged after the delay, not at the change"
    await advance(hass, seconds=STATUS_RECHECK_DELAY + 1)
    assert issue(hass, entry, ISSUE_STATUS) is not None
    watcher.stop()


async def test_a_value_that_comes_and_goes_keeps_its_repair_for_seven_days(hass, entry, automations, watchers, storage) -> None:
    seed(storage, entry, {"blueprint_seen": False, "status_unknown": {"wobbly": dt_util.utcnow().isoformat()}, "charger_events": []})
    hass.states.async_set(STATUS, "charging")
    watcher = await start(hass, make(hass, entry, automations, watchers, status_entity=STATUS))
    assert issue(hass, entry, ISSUE_STATUS), "seen recently: the repair stays although the status is known now"
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_STATUS)
    watcher.stop()


async def test_a_value_not_seen_for_seven_days_takes_its_repair_away(hass, entry, automations, watchers, storage) -> None:
    old = dt_util.utcnow() - datetime.timedelta(days=STATUS_UNKNOWN_DAYS + 1)
    seed(storage, entry, {"blueprint_seen": False, "status_unknown": {"wobbly": old.isoformat()}, "charger_events": []})
    create_issue(hass, entry, ISSUE_STATUS)
    hass.states.async_set(STATUS, "charging")
    watcher = await start(hass, make(hass, entry, automations, watchers, status_entity=STATUS))
    assert issue(hass, entry, ISSUE_STATUS) is None
    watcher.stop()


async def test_an_unavailable_status_leaves_the_repair_as_it_is(hass, entry, automations, watchers) -> None:
    create_issue(hass, entry, ISSUE_STATUS)
    hass.states.async_set(STATUS, "unavailable")
    watcher = await start(hass, make(hass, entry, automations, watchers, status_entity=STATUS))
    assert issue(hass, entry, ISSUE_STATUS)
    watcher.stop()


async def test_without_a_status_entity_the_unknown_values_and_their_repair_are_dropped(hass, entry, automations, watchers, storage) -> None:
    """No status entity any more (the field was cleared): nothing is held, so the repair goes, and so do the values kept for it."""
    seed(storage, entry, {"blueprint_seen": False, "status_unknown": {"wobbly": dt_util.utcnow().isoformat()}, "charger_events": []})
    create_issue(hass, entry, ISSUE_STATUS)
    watcher = await start(hass, make(hass, entry, automations, watchers))  # status_entity ""
    assert issue(hass, entry, ISSUE_STATUS) is None
    await settle(hass)
    assert storage[store_key(entry.entry_id)]["data"]["status_unknown"] == {}
    watcher.stop()


async def test_a_stored_time_without_a_time_zone_is_dropped_and_the_checks_go_on(hass, entry, automations, watchers, storage, caplog) -> None:
    """A naive time can't be compared with the aware ones. It is dropped on load, and the checks that follow don't fail on it."""
    naive = dt_util.utcnow().replace(tzinfo=None).isoformat()
    seed(storage, entry, {"blueprint_seen": False, "status_unknown": {"wobbly": naive}, "charger_events": [{"at": naive, "kind": "gave_up"}] * 3})
    hass.states.async_set(STATUS, "charging")
    watcher = await start(hass, make(hass, entry, automations, watchers, status_entity=STATUS))
    assert issue(hass, entry, ISSUE_NOT_FOLLOWING) is None, "the naive events are not counted"
    assert issue(hass, entry, ISSUE_STATUS) is None, "the naive value is not kept"

    for _ in range(NOT_FOLLOWING_EVENTS):
        watcher.record_charger_problem("no_rule")
    await settle(hass)
    assert issue(hass, entry, ISSUE_NOT_FOLLOWING) is not None, "the events after the start count"
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_NOT_FOLLOWING) is not None
    assert "Health check" not in caplog.text, "no check failed on the stored times"
    watcher.stop()


# ---------------------------------------------------------------------- #
# The charger not following the plan
# ---------------------------------------------------------------------- #


async def test_events_of_the_charger_raise_the_repair_at_three(hass, entry, automations, watchers) -> None:
    watcher = await start(hass, make(hass, entry, automations, watchers))
    watcher.record_charger_problem("gave_up")
    watcher.record_charger_problem("gave_up")
    await settle(hass)
    assert issue(hass, entry, ISSUE_NOT_FOLLOWING) is None, "two events are not enough"

    watcher.record_charger_problem("no_rule")
    await settle(hass)
    found = issue(hass, entry, ISSUE_NOT_FOLLOWING)
    assert found is not None
    assert found.translation_placeholders["count"] == "3"
    assert found.translation_placeholders["limit"] == str(NOT_FOLLOWING_EVENTS)
    assert found.translation_placeholders["gave_up"] == "2"
    assert found.translation_placeholders["no_rule"] == "1"
    assert found.translation_placeholders["latest"].count("- ") == 3
    watcher.stop()


async def test_an_unknown_kind_is_not_an_event(hass, entry, automations, watchers) -> None:
    watcher = await start(hass, make(hass, entry, automations, watchers))
    for _ in range(NOT_FOLLOWING_EVENTS):
        watcher.record_charger_problem("something_else")
    await settle(hass)
    assert issue(hass, entry, ISSUE_NOT_FOLLOWING) is None
    watcher.stop()


async def test_nothing_is_judged_before_the_start_for_the_charger_events(hass, entry, automations, watchers, storage) -> None:
    """Old events are dropped, and the repair goes, only at a check after the start."""
    old = (dt_util.utcnow() - datetime.timedelta(days=8)).isoformat()
    seed(storage, entry, {"blueprint_seen": False, "status_unknown": {}, "charger_events": [{"at": old, "kind": "gave_up"}] * 3})
    create_issue(hass, entry, ISSUE_NOT_FOLLOWING)
    watcher = make(hass, entry, automations, watchers)
    await watcher.async_load()  # not started
    await watcher.async_check()
    assert issue(hass, entry, ISSUE_NOT_FOLLOWING), "the repair stays as it is before the start"


async def test_events_older_than_seven_days_do_not_count(hass, entry, automations, watchers, storage) -> None:
    old = (dt_util.utcnow() - datetime.timedelta(days=8)).isoformat()
    seed(storage, entry, {"blueprint_seen": False, "status_unknown": {}, "charger_events": [{"at": old, "kind": "gave_up"}] * 3})
    watcher = await start(hass, make(hass, entry, automations, watchers))
    assert issue(hass, entry, ISSUE_NOT_FOLLOWING) is None
    await settle(hass)
    assert storage[store_key(entry.entry_id)]["data"]["charger_events"] == [], "the old events are dropped from the store"
    watcher.stop()


async def test_the_repair_goes_when_its_events_have_aged_out(hass, entry, automations, watchers, storage) -> None:
    old = (dt_util.utcnow() - datetime.timedelta(days=8)).isoformat()
    seed(storage, entry, {"blueprint_seen": False, "status_unknown": {}, "charger_events": [{"at": old, "kind": "no_rule"}] * 3})
    create_issue(hass, entry, ISSUE_NOT_FOLLOWING)
    watcher = await start(hass, make(hass, entry, automations, watchers))
    assert issue(hass, entry, ISSUE_NOT_FOLLOWING) is None
    watcher.stop()


async def test_the_events_are_kept_over_a_restart(hass, entry, automations, watchers, storage) -> None:
    watcher = await start(hass, make(hass, entry, automations, watchers))
    for kind in ("gave_up", "no_rule", "gave_up"):
        watcher.record_charger_problem(kind)
    await settle(hass)
    assert [event["kind"] for event in storage[store_key(entry.entry_id)]["data"]["charger_events"]] == ["gave_up", "no_rule", "gave_up"]
    watcher.stop()

    restarted = await start(hass, make(hass, entry, automations, watchers))
    assert issue(hass, entry, ISSUE_NOT_FOLLOWING) is not None
    restarted.stop()


# ---------------------------------------------------------------------- #
# Removal and the entries
# ---------------------------------------------------------------------- #


async def test_a_health_store_that_cannot_be_deleted_does_not_stop_the_backup_of_the_removed_entry(
    hass, loaded_entry, monkeypatch, caplog
) -> None:
    """The store is deleted before the backup is written (async_remove_entry). An error there must not stop the backup."""
    import custom_components.cable_juice_planner as integration  # noqa: PLC0415

    class Unremovable(health_checks.Store):
        async def async_remove(self) -> None:
            raise OSError("the file is locked")

    monkeypatch.setattr(health_checks, "Store", Unremovable)
    exported: list[str] = []
    real_export = integration.async_export

    async def spy(hass_, entry_, reason):
        exported.append(reason)
        await real_export(hass_, entry_, reason)

    monkeypatch.setattr(integration, "async_export", spy)
    await hass.config_entries.async_remove(loaded_entry.entry_id)
    await settle(hass)
    assert exported == ["deleted"], "the backup of the removed entry is still written"
    assert "Could not delete the health state" in caplog.text


async def test_removing_the_entry_deletes_its_repairs_and_its_store(hass, entry, automations, watchers, storage) -> None:
    watcher = await start(hass, make(hass, entry, automations, watchers))
    for _ in range(NOT_FOLLOWING_EVENTS):
        watcher.record_charger_problem("gave_up")
    await settle(hass)
    for name in HEALTH_ISSUES:
        create_issue(hass, entry, name)
    assert store_key(entry.entry_id) in storage

    remove_issues(hass, entry.entry_id)
    await async_remove_health_store(hass, entry.entry_id)
    assert all(issue(hass, entry, name) is None for name in HEALTH_ISSUES)
    assert store_key(entry.entry_id) not in storage


async def test_a_car_entry_has_its_repairs_and_removing_it_removes_them_all(hass, loaded_entry, storage) -> None:
    watcher = loaded_entry.runtime_data.health_watcher
    assert watcher is not None
    for _ in range(NOT_FOLLOWING_EVENTS):
        watcher.record_charger_problem("no_rule")
    await settle(hass)
    assert issue(hass, loaded_entry, ISSUE_NOT_FOLLOWING) and store_key(loaded_entry.entry_id) in storage

    await hass.config_entries.async_remove(loaded_entry.entry_id)
    await settle(hass)
    assert issue(hass, loaded_entry, ISSUE_NOT_FOLLOWING) is None
    assert store_key(loaded_entry.entry_id) not in storage


async def test_an_ignored_repair_stays_ignored_over_a_reload_of_an_entry_without_dependencies(hass, entry, storage) -> None:
    """An entry with no Strømligning or Solcast sensors: its reload must not clear the repairs of the health checks (the
    dependency checks used to remove all of them when they had nothing to watch)."""
    # registered before the states (a state makes the registry pick another entity id)
    er.async_get(hass).async_get_or_create("sensor", "test", "kwh", suggested_object_id="charger_session_energy", disabled_by=DISABLED)
    set_source_states(hass)
    car = make_entry()
    await async_setup(hass, car)
    name = f"{car.entry_id}_{ISSUE_MISSING}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, name), "the disabled kWh meter is a repair"
    ir.async_ignore_issue(hass, DOMAIN, name, True)

    await async_unload_and_join(hass, car)
    assert await hass.config_entries.async_setup(car.entry_id)
    await settle(hass)
    found = ir.async_get(hass).async_get_issue(DOMAIN, name)
    assert found is not None and found.dismissed_version is not None
    await async_unload_and_join(hass, car)


async def test_the_site_entry_gets_no_health_watcher_and_a_car_entry_does(hass, monkeypatch) -> None:
    started: list[HealthWatcher] = []
    monkeypatch.setattr(HealthWatcher, "start", lambda self: started.append(self))
    set_source_states(hass)
    site = make_site_entry()
    site.add_to_hass(hass)
    assert await hass.config_entries.async_setup(site.entry_id)
    await settle(hass)
    assert started == [], "the shared grid connection entry has no health checks"
    await hass.config_entries.async_unload(site.entry_id)

    # the control: a car entry does start one, so the patch above sees what it is meant to see
    car = make_entry()
    await async_setup(hass, car)
    assert len(started) == 1
    await async_unload_and_join(hass, car)


# ---------------------------------------------------------------------- #
# The texts: the same keys and placeholders in every language
# ---------------------------------------------------------------------- #

EXPECTED_PLACEHOLDERS = {
    ISSUE_MISSING: {"prefix", "disabled", "missing"},
    ISSUE_UNITS: {"prefix", "entities", "power_units", "energy_units"},
    ISSUE_BLUEPRINT: {"prefix", "automations"},
    ISSUE_STATUS: {"prefix", "values", "minutes", "days"},
    ISSUE_NOT_FOLLOWING: {"prefix", "count", "limit", "days", "gave_up", "no_rule", "latest"},
    # the shared grid connection's repair (site.py), not a health check
    ISSUE_PHASE_UNITS: {"entities"},
}


def test_every_repair_text_is_in_every_language_with_the_same_placeholders() -> None:
    folder = Path(__file__).resolve().parents[1] / "custom_components" / "cable_juice_planner"
    files = [folder / "strings.json", *sorted((folder / "translations").glob("*.json"))]
    assert {path.name for path in files} >= {"strings.json", "da.json", "de.json", "en.json", "en-GB.json", "pl.json", "uk.json"}
    for path in files:
        issues = json.loads(path.read_text(encoding="utf-8"))["issues"]
        for name, expected in EXPECTED_PLACEHOLDERS.items():
            text = issues[name]
            assert placeholders_of(text["title"]) | placeholders_of(text["description"]) == expected, (path.name, name)
            assert text["title"].strip() and text["description"].strip(), (path.name, name)
            # each text says the repair goes by itself (en, da, de, pl, uk)
            said = ("disappears", "forsvinder", "verschwindet", "znika", "зникне")
            assert any(word in text["description"] for word in said), (path.name, name)
