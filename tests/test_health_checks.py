"""Health checks: the repairs of a car or battery entry for what has gone wrong, removed only once it is fixed."""
from __future__ import annotations

import datetime
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace
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
    ISSUE_BATTERY_FRACTION,
    ISSUE_BLUEPRINT,
    ISSUE_MISSING,
    ISSUE_NOT_FOLLOWING,
    ISSUE_POWER_UNIT,
    ISSUE_PRICE_SCALE,
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
from custom_components.cable_juice_planner.prices.base import CombinedPriceProvider
from custom_components.cable_juice_planner.site import ISSUE_FUSE_EXCEEDED, ISSUE_PHASE_UNITS

from .conftest import PRICE as CAR_PRICE
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


@pytest.mark.parametrize("name", [ISSUE_UNITS, ISSUE_STATUS, ISSUE_NOT_FOLLOWING, ISSUE_POWER_UNIT, ISSUE_PRICE_SCALE])
async def test_an_ignored_repair_stays_ignored_while_the_problem_persists(hass, entry, automations, watchers, recorder, name) -> None:
    answer: dict[str, Any] = {"value": ([(PRICE, "øre/kWh")], True)}
    if name == ISSUE_UNITS:
        hass.states.async_set("sensor.battery", "50", {"unit_of_measurement": "kWh"})
        watcher = make(hass, entry, automations, watchers, battery_entities=["sensor.battery"])
    elif name == ISSUE_STATUS:
        hass.states.async_set(STATUS, "wobbly", timestamp=ago(minutes=11))
        watcher = make(hass, entry, automations, watchers, status_entity=STATUS)
    elif name == ISSUE_POWER_UNIT:
        recorder.numbers = {POWER: KW_HISTORY}
        hass.states.async_set(POWER, "0.8")
        watcher = make(hass, entry, automations, watchers, power_entities=[POWER])
    elif name == ISSUE_PRICE_SCALE:
        watcher = make(hass, entry, automations, watchers, price_problems=lambda: answer["value"])
    else:
        watcher = make(hass, entry, automations, watchers)
    await start(hass, watcher)
    if name == ISSUE_NOT_FOLLOWING:
        for _ in range(NOT_FOLLOWING_EVENTS):
            watcher.record_charger_problem("gave_up")
        await settle(hass)
    assert issue(hass, entry, name), "the problem is raised first"

    ir.async_ignore_issue(hass, DOMAIN, f"{entry.entry_id}_{name}", True)
    # the problem changes (a higher highest value, another price): the repair is updated, the ignore stays
    if name == ISSUE_POWER_UNIT:
        recorder.numbers = {POWER: [*KW_HISTORY, 9.0]}
        rewind_history(watcher, POWER)
    elif name == ISSUE_PRICE_SCALE:
        answer["value"] = ([(PRICE, "ct/kWh")], True)
    await advance(hass, hours=1, seconds=1)  # the hourly check: the problem is still there
    if name == ISSUE_POWER_UNIT:
        assert "≤ 9" in issue(hass, entry, name).translation_placeholders["entities"], "the repair shows the new highest value"
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
# Sensors read in the wrong scale (value_checks): power without a unit, battery levels, price units
# ---------------------------------------------------------------------- #

POWER = "sensor.house_power"
POWER_B = "sensor.house_power_b"
BATTERY = "sensor.house_battery"
PRICE = "sensor.price_kwh"
# ten or more numbers, fractions in kW range: a power without a unit that looks like kW
KW_HISTORY = [0.5, 1.2, 3.4, 0.8, 2.2, 4.2, 1.1, 0.3, 2.5, 3.3]
# a battery level that is a share (0..1)
SHARE_HISTORY = [0.85, 0.9, 0.5, 0.97, 0.12, 0.33, 0.6, 0.7, 0.8, 0.99]


class FakeRecorder:
    """value_checks.async_recorded_numbers: the numbers per entity, counted per call (None: no recorder)."""

    def __init__(self) -> None:
        self.numbers: dict[str, list[float]] | None = {}
        self.calls: list[list[str]] = []

    async def __call__(self, hass: HomeAssistant, entity_ids: list[str], days: int) -> dict[str, list[float]] | None:
        self.calls.append(list(entity_ids))
        if self.numbers is None:
            return None
        return {entity_id: self.numbers.get(entity_id, []) for entity_id in entity_ids}


@pytest.fixture
def recorder(monkeypatch) -> FakeRecorder:
    fake = FakeRecorder()
    monkeypatch.setattr(health_checks, "async_recorded_numbers", fake)
    return fake


def rewind_history(watcher: HealthWatcher, entity_id: str) -> None:
    """The history of the sensor was read a full VALUE_RECHECK ago (the next check reads it again)."""
    watcher._scale_read[entity_id] = dt_util.utcnow() - health_checks.VALUE_RECHECK - datetime.timedelta(minutes=1)


async def test_a_power_without_a_unit_that_looks_like_kilowatts_is_a_repair(hass, entry, automations, watchers, recorder) -> None:
    recorder.numbers = {POWER: KW_HISTORY}
    hass.states.async_set(POWER, "0.8")
    await start(hass, make(hass, entry, automations, watchers, power_entities=[POWER]))

    found = issue(hass, entry, ISSUE_POWER_UNIT)
    assert found is not None and found.severity == ir.IssueSeverity.WARNING
    assert found.translation_placeholders["entities"] == "- sensor.house_power (≤ 4.2)"
    assert found.translation_placeholders["max"] == "50"
    assert found.translation_placeholders["days"] == "3"


@pytest.mark.parametrize(("state", "attributes"), [("60", {}), ("0.8", {"unit_of_measurement": "kW"})])
async def test_a_power_above_fifty_or_with_a_unit_is_no_longer_a_repair(hass, entry, automations, watchers, recorder, state, attributes) -> None:
    recorder.numbers = {POWER: KW_HISTORY}
    hass.states.async_set(POWER, "0.8")
    watcher = await start(hass, make(hass, entry, automations, watchers, power_entities=[POWER]))
    assert issue(hass, entry, ISSUE_POWER_UNIT)

    hass.states.async_set(POWER, state, attributes)
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_POWER_UNIT) is None
    watcher.stop()


async def test_an_unclear_history_or_an_unavailable_sensor_keeps_the_power_repair(hass, entry, automations, watchers, recorder) -> None:
    recorder.numbers = {POWER: KW_HISTORY}
    hass.states.async_set(POWER, "0.8")
    watcher = await start(hass, make(hass, entry, automations, watchers, power_entities=[POWER]))

    # too few numbers now: a verdict that can't be told keeps the previous one
    recorder.numbers = {POWER: [0.5, 0.7]}
    rewind_history(watcher, POWER)
    hass.states.async_set(POWER, "0.4")
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_POWER_UNIT)

    # no recorder at all
    recorder.numbers = None
    rewind_history(watcher, POWER)
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_POWER_UNIT)

    hass.states.async_set(POWER, "unavailable")
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_POWER_UNIT), "unavailable is not a fix"
    watcher.stop()


async def test_the_history_is_read_at_most_once_per_value_recheck_and_in_one_call(hass, entry, automations, watchers, recorder) -> None:
    recorder.numbers = {POWER: KW_HISTORY, POWER_B: KW_HISTORY}
    hass.states.async_set(POWER, "0.8")
    hass.states.async_set(POWER_B, "0.9")
    watcher = await start(hass, make(hass, entry, automations, watchers, power_entities=[POWER, POWER_B]))
    assert recorder.calls == [[POWER, POWER_B]], "both sensors in one read"

    await advance(hass, **SETTLE_ONE_HOUR)
    assert len(recorder.calls) == 1, "the history is not read again within VALUE_RECHECK"

    rewind_history(watcher, POWER)
    await advance(hass, **SETTLE_ONE_HOUR)
    assert recorder.calls[-1] == [POWER]
    assert len(recorder.calls) == 2
    watcher.stop()


async def test_the_power_repair_goes_only_when_every_flagged_sensor_is_fixed(hass, entry, automations, watchers, recorder) -> None:
    recorder.numbers = {POWER: KW_HISTORY, POWER_B: KW_HISTORY}
    hass.states.async_set(POWER, "0.8")
    hass.states.async_set(POWER_B, "0.9")
    watcher = await start(hass, make(hass, entry, automations, watchers, power_entities=[POWER, POWER_B]))
    hass.states.async_set(POWER, "60")
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_POWER_UNIT), "the other sensor still looks like kW"

    hass.states.async_set(POWER_B, "60")
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_POWER_UNIT) is None
    watcher.stop()


async def test_a_battery_level_that_is_a_share_is_a_repair_until_it_reads_percent(hass, entry, automations, watchers, recorder) -> None:
    recorder.numbers = {BATTERY: SHARE_HISTORY}
    hass.states.async_set(BATTERY, "0.85", {"unit_of_measurement": "%"})
    watcher = await start(hass, make(hass, entry, automations, watchers, battery_entities=[BATTERY]))

    found = issue(hass, entry, ISSUE_BATTERY_FRACTION)
    assert found is not None
    assert found.translation_placeholders["entities"] == "- sensor.house_battery (≤ 0.99)"
    assert found.translation_placeholders["days"] == "3"

    hass.states.async_set(BATTERY, "85", {"unit_of_measurement": "%"})
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_BATTERY_FRACTION) is None
    watcher.stop()


async def test_a_battery_level_in_another_unit_is_no_share_repair(hass, entry, automations, watchers, recorder) -> None:
    recorder.numbers = {BATTERY: SHARE_HISTORY}
    hass.states.async_set(BATTERY, "0.85", {"unit_of_measurement": "kWh"})
    await start(hass, make(hass, entry, automations, watchers, battery_entities=[BATTERY]))
    assert issue(hass, entry, ISSUE_BATTERY_FRACTION) is None
    assert recorder.calls == [], "a sensor with another unit is not read"


async def test_a_price_in_a_subunit_is_a_repair_until_its_unit_is_fine(hass, entry, automations, watchers) -> None:
    answer: dict[str, Any] = {"value": ([(PRICE, "øre/kWh")], True)}
    watcher = await start(hass, make(hass, entry, automations, watchers, price_problems=lambda: answer["value"]))
    found = issue(hass, entry, ISSUE_PRICE_SCALE)
    assert found is not None
    assert found.translation_placeholders["entities"] == "- sensor.price_kwh (øre/kWh)"

    answer["value"] = ([], True)
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_PRICE_SCALE) is None
    watcher.stop()


async def test_no_price_provider_or_unjudged_prices_keep_the_price_repair(hass, entry, automations, watchers) -> None:
    create_issue(hass, entry, ISSUE_PRICE_SCALE)
    answer: dict[str, Any] = {"value": None}
    watcher = await start(hass, make(hass, entry, automations, watchers, price_problems=lambda: answer["value"]))
    assert issue(hass, entry, ISSUE_PRICE_SCALE), "no provider yet is not judged"

    answer["value"] = ([], False)
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_PRICE_SCALE), "a price that is not judged yet keeps the repair"
    watcher.stop()


def test_the_price_units_the_provider_judges() -> None:
    states = {
        "sensor.ore": SimpleNamespace(state="0.5", attributes={"unit_of_measurement": "øre/kWh"}),
        "sensor.kr": SimpleNamespace(state="0.5", attributes={"unit_of_measurement": "kr/kWh"}),
        "sensor.mwh": SimpleNamespace(state="1", attributes={"unit_of_measurement": "kr/MWh"}),
        "sensor.gone": SimpleNamespace(state="unavailable", attributes={}),
    }
    entity_ids = {"buy": "sensor.ore", "sell": "sensor.kr", "tariff": "sensor.mwh", "other": "sensor.gone"}
    provider = CombinedPriceProvider({"solar": {"production_price": -1}, "prices": {"entity_ids": entity_ids}}, [])
    problems, judged = provider.price_unit_problems(lambda entity_id: states.get(entity_id))
    assert problems == [("sensor.ore", "øre/kWh"), ("sensor.mwh", "kr/MWh")]
    assert judged is False, "an unavailable price without a unit is not judged"

    # a missing state is not judged either, and a price in kr/kWh is fine
    problems, judged = provider.price_unit_problems(lambda entity_id: states.get(entity_id) if entity_id != "sensor.gone" else None)
    assert [entity for entity, _ in problems] == ["sensor.ore", "sensor.mwh"]
    assert judged is False


# ---------------------------------------------------------------------- #
# The texts: the same keys and placeholders in every language
# ---------------------------------------------------------------------- #

EXPECTED_PLACEHOLDERS = {
    ISSUE_MISSING: {"prefix", "disabled", "missing"},
    ISSUE_UNITS: {"prefix", "entities", "power_units", "energy_units"},
    ISSUE_BLUEPRINT: {"prefix", "automations"},
    ISSUE_STATUS: {"prefix", "values", "minutes", "days"},
    ISSUE_NOT_FOLLOWING: {"prefix", "count", "limit", "days", "gave_up", "no_rule", "latest"},
    ISSUE_POWER_UNIT: {"prefix", "entities", "max", "days"},
    ISSUE_BATTERY_FRACTION: {"prefix", "entities", "days"},
    ISSUE_PRICE_SCALE: {"prefix", "entities"},
    # the shared grid connection's repairs (site.py), not health checks
    ISSUE_PHASE_UNITS: {"entities"},
    ISSUE_FUSE_EXCEEDED: {"phases", "factor", "hours", "days"},
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


# ---------------------------------------------------------------------- #
# The flagged list of the value repairs (store), the restart, the flapping state, a failing recorder, the numbers
# ---------------------------------------------------------------------- #

# whole numbers in W: a sensor they describe can't be judged (unclear) and is never flagged
POWER_WHOLE = "sensor.house_power_whole"
WHOLE_HISTORY = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]


async def test_a_flagged_sensor_fixed_while_another_sensor_was_never_judged_keeps_the_repair(hass, entry, automations, watchers, recorder) -> None:
    """A lost store with the repair open: the list of the flagged sensors is not known, so the whole-number sensor that was
    never judged keeps the repair (on a fresh install the list starts known, see the test above)."""
    create_issue(hass, entry, ISSUE_POWER_UNIT)
    recorder.numbers = {POWER: KW_HISTORY, POWER_WHOLE: WHOLE_HISTORY}
    hass.states.async_set(POWER, "0.8")
    hass.states.async_set(POWER_WHOLE, "30")
    watcher = await start(hass, make(hass, entry, automations, watchers, power_entities=[POWER, POWER_WHOLE]))
    assert issue(hass, entry, ISSUE_POWER_UNIT)

    # the flagged sensor now reads 60 W; the whole-number sensor stays unclear and was never flagged
    hass.states.async_set(POWER, "60")
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_POWER_UNIT), "POWER_WHOLE was never judged: the repair stays"
    watcher.stop()


async def test_a_fresh_install_clears_the_repair_when_the_flagged_sensor_gets_a_unit_though_another_is_never_judged(
    hass, entry, automations, watchers, recorder, storage
) -> None:
    """No store and no repair yet: the list of flagged sensors starts known and empty, so a sensor that is never judged (whole
    numbers under 50 W) does not keep the repair open once the flagged sensor is fixed."""
    recorder.numbers = {POWER: KW_HISTORY, POWER_WHOLE: WHOLE_HISTORY}
    hass.states.async_set(POWER, "0.8")
    hass.states.async_set(POWER_WHOLE, "30")
    watcher = await start(hass, make(hass, entry, automations, watchers, power_entities=[POWER, POWER_WHOLE]))
    assert issue(hass, entry, ISSUE_POWER_UNIT), "POWER looks like kW"

    hass.states.async_set(POWER, "0.8", {"unit_of_measurement": "kW"})
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_POWER_UNIT) is None, "POWER has a unit now: nothing is flagged and the list is known"
    watcher.stop()


async def test_a_flagged_list_is_known_only_once_one_check_judged_every_sensor(hass, entry, automations, watchers, recorder, storage) -> None:
    """The four steps of a lost store (the repair is open, the list is gone): no list is stored; POWER is flagged while POWER_B
    is unavailable, so the list is not known and not saved as known; POWER fixed keeps the repair (POWER_B was never judged);
    POWER_B judged fine removes it."""
    create_issue(hass, entry, ISSUE_POWER_UNIT)
    recorder.numbers = {POWER: KW_HISTORY}
    hass.states.async_set(POWER, "0.8")
    hass.states.async_set(POWER_B, "unavailable")
    watcher = await start(hass, make(hass, entry, automations, watchers, power_entities=[POWER, POWER_B]))
    assert issue(hass, entry, ISSUE_POWER_UNIT), "POWER is flagged"
    assert ISSUE_POWER_UNIT not in watcher._data["scale_flagged"], "one check judged only some sensors: not a known list"
    await settle(hass)
    saved = storage.get(store_key(entry.entry_id), {}).get("data", {}).get("scale_flagged", {})
    assert saved.get(ISSUE_POWER_UNIT) in (None, {POWER: "x"}), "the unknown list is not saved as known"

    hass.states.async_set(POWER, "60")
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_POWER_UNIT), "POWER_B was never judged: the repair stays"

    hass.states.async_set(POWER_B, "60")
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_POWER_UNIT) is None, "every sensor is judged fine"
    watcher.stop()


@pytest.mark.parametrize("stored", [None, {ISSUE_POWER_UNIT: {POWER: "x"}}], ids=["no-store-repair-open", "unreadable-highest-value"])
async def test_a_lost_or_unreadable_list_stays_unknown_until_every_sensor_is_judged(hass, entry, automations, watchers, recorder, storage, stored) -> None:
    create_issue(hass, entry, ISSUE_POWER_UNIT)  # the store is lost or unreadable, the repair is open
    if stored is not None:
        seed(storage, entry, {"scale_flagged": stored})
    recorder.numbers = {POWER: KW_HISTORY}
    hass.states.async_set(POWER, "0.8")
    hass.states.async_set(POWER_B, "unavailable")
    watcher = await start(hass, make(hass, entry, automations, watchers, power_entities=[POWER, POWER_B]))
    assert issue(hass, entry, ISSUE_POWER_UNIT)
    assert ISSUE_POWER_UNIT not in watcher._data["scale_flagged"]
    hass.states.async_set(POWER, "60")
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_POWER_UNIT), "POWER_B was never judged: the repair stays"
    hass.states.async_set(POWER_B, "60")
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_POWER_UNIT) is None
    watcher.stop()


async def test_a_known_list_goes_when_nothing_is_flagged_even_if_a_sensor_turns_unavailable(hass, entry, automations, watchers, recorder) -> None:
    """Once known (every sensor judged in one check), the list stays known: an unavailable sensor does not hold the repair."""
    recorder.numbers = {POWER: KW_HISTORY}
    hass.states.async_set(POWER, "0.8")
    hass.states.async_set(POWER_B, "60")
    watcher = await start(hass, make(hass, entry, automations, watchers, power_entities=[POWER, POWER_B]))
    assert issue(hass, entry, ISSUE_POWER_UNIT)
    assert ISSUE_POWER_UNIT in watcher._data["scale_flagged"], "every sensor was judged in one check: known"

    hass.states.async_set(POWER, "60")
    hass.states.async_set(POWER_B, "unavailable")
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_POWER_UNIT) is None
    watcher.stop()


async def test_without_a_stored_list_an_unclear_sensor_keeps_the_repair(hass, entry, automations, watchers, recorder) -> None:
    """The store is lost: the list of the flagged sensors is not known, so an unclear sensor keeps the repair."""
    create_issue(hass, entry, ISSUE_POWER_UNIT)
    recorder.numbers = {POWER_WHOLE: WHOLE_HISTORY}
    hass.states.async_set(POWER, "60")
    hass.states.async_set(POWER_WHOLE, "30")
    watcher = await start(hass, make(hass, entry, automations, watchers, power_entities=[POWER, POWER_WHOLE]))
    assert issue(hass, entry, ISSUE_POWER_UNIT), "the unclear sensor keeps the repair"
    watcher.stop()


async def test_a_flagged_sensor_stays_flagged_over_a_restart_until_it_is_judged_fine(hass, entry, automations, watchers, recorder, storage) -> None:
    # POWER_WHOLE reaches 80 in its history (judged fine in the first check): the list is known and kept in the store
    recorder.numbers = {POWER: KW_HISTORY, POWER_WHOLE: [*WHOLE_HISTORY, 80.0]}
    hass.states.async_set(POWER, "0.8")
    hass.states.async_set(POWER_WHOLE, "30")
    first = await start(hass, make(hass, entry, automations, watchers, power_entities=[POWER, POWER_WHOLE]))
    assert issue(hass, entry, ISSUE_POWER_UNIT)
    await settle(hass)  # the flagged list is written to the store
    stored = storage[store_key(entry.entry_id)]["data"]["scale_flagged"][ISSUE_POWER_UNIT]
    assert POWER in stored and POWER_WHOLE not in stored, "the flagged list (with its highest value) is what the store keeps"
    first.stop()

    # after the restart the flagged sensor is unavailable: restored from the store, the repair stays
    hass.states.async_set(POWER, "unavailable")
    second = await start(hass, make(hass, entry, automations, watchers, power_entities=[POWER, POWER_WHOLE]))
    assert issue(hass, entry, ISSUE_POWER_UNIT), "restored from the store: an unavailable flagged sensor keeps the repair"

    # the flagged sensor reads 60 W: the list is known from the store, so nothing flagged removes the repair
    hass.states.async_set(POWER, "60")
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_POWER_UNIT) is None, "the stored list is known and nothing is flagged any more"
    second.stop()


async def test_a_stored_flag_of_a_removed_sensor_is_dropped_and_the_repair_goes(hass, entry, automations, watchers, recorder, storage) -> None:
    create_issue(hass, entry, ISSUE_POWER_UNIT)
    seed(storage, entry, {"scale_flagged": {ISSUE_POWER_UNIT: {"sensor.removed_house_power": 4.2}}})
    hass.states.async_set(POWER, "60")  # judged fine now: a value above the limit
    watcher = await start(hass, make(hass, entry, automations, watchers, power_entities=[POWER]))
    assert watcher._data["scale_flagged"][ISSUE_POWER_UNIT] == {}, "the removed sensor is no longer flagged"
    assert issue(hass, entry, ISSUE_POWER_UNIT) is None, "nothing is flagged and the list is known: the repair goes"
    watcher.stop()


async def test_a_stored_value_that_cannot_be_read_keeps_the_repair(hass, entry, automations, watchers, recorder, storage) -> None:
    """A configured sensor whose stored highest value can't be read: the flagged list is not known, so the repair is not taken
    as known and empty (it stays until the sensor is judged)."""
    create_issue(hass, entry, ISSUE_POWER_UNIT)
    seed(storage, entry, {"scale_flagged": {ISSUE_POWER_UNIT: {POWER: "x"}}})
    hass.states.async_set(POWER, "unavailable")  # not judged now
    watcher = await start(hass, make(hass, entry, automations, watchers, power_entities=[POWER]))
    assert issue(hass, entry, ISSUE_POWER_UNIT), "an unknown flagged list keeps the repair"
    assert ISSUE_POWER_UNIT not in watcher._data["scale_flagged"], "the list is not restored as known"
    watcher.stop()


async def test_a_value_that_is_fine_again_is_no_repair_an_hour_later(hass, entry, automations, watchers, recorder) -> None:
    """Raised, then a spike of 1200 (cleared), then 1.5 an hour later: the fine verdict stands, the repair is not raised again."""
    recorder.numbers = {POWER: KW_HISTORY}
    hass.states.async_set(POWER, "0.8")
    watcher = await start(hass, make(hass, entry, automations, watchers, power_entities=[POWER]))
    assert issue(hass, entry, ISSUE_POWER_UNIT)

    recorder.numbers = {POWER: [*KW_HISTORY, 1200.0]}
    hass.states.async_set(POWER, "1200")
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_POWER_UNIT) is None

    hass.states.async_set(POWER, "1.5")
    await advance(hass, **SETTLE_ONE_HOUR)
    assert issue(hass, entry, ISSUE_POWER_UNIT) is None, "a stale True must not raise it again"
    watcher.stop()


async def test_a_failing_recorder_is_logged_once_and_not_read_every_hour(hass, entry, automations, watchers, recorder, monkeypatch, caplog) -> None:
    recorder.numbers = {POWER: KW_HISTORY}
    hass.states.async_set(POWER, "0.8")
    watcher = await start(hass, make(hass, entry, automations, watchers, power_entities=[POWER]))
    assert issue(hass, entry, ISSUE_POWER_UNIT)

    calls: list[list[str]] = []

    async def broken(_hass, entity_ids, _days):
        calls.append(list(entity_ids))
        raise RuntimeError("database is locked")

    monkeypatch.setattr(health_checks, "async_recorded_numbers", broken)
    hass.states.async_set(POWER, "0.5")  # could be kW: its history is read, and the read fails
    rewind_history(watcher, POWER)
    await advance(hass, **SETTLE_ONE_HOUR)
    assert calls == [[POWER]]
    assert issue(hass, entry, ISSUE_POWER_UNIT), "the verdict is kept"

    await advance(hass, **SETTLE_ONE_HOUR)
    assert len(calls) == 1, "the failed read counts as the read: no retry every hour"
    warnings = [record for record in caplog.records if record.levelname == "WARNING" and "value checks" in record.getMessage()]
    assert len(warnings) == 1
    watcher.stop()


async def test_a_price_per_wh_from_the_car_entrys_provider_is_a_repair(hass, loaded_entry) -> None:
    """The price check reads the car's own CombinedPriceProvider (lifecycle._price_unit_problems), not the price entity itself."""
    watcher = loaded_entry.runtime_data.health_watcher
    await watcher.async_check()
    assert issue(hass, loaded_entry, ISSUE_PRICE_SCALE) is None, "kr/kWh is fine"

    state = hass.states.get(CAR_PRICE)
    hass.states.async_set(CAR_PRICE, state.state, {**state.attributes, "unit_of_measurement": "DKK/Wh"})
    await watcher.async_check()
    found = issue(hass, loaded_entry, ISSUE_PRICE_SCALE)
    assert found is not None
    assert found.translation_placeholders["entities"] == f"- {CAR_PRICE} (DKK/Wh)"

    hass.states.async_set(CAR_PRICE, state.state, {**state.attributes, "unit_of_measurement": "kr/kWh"})
    await watcher.async_check()
    assert issue(hass, loaded_entry, ISSUE_PRICE_SCALE) is None, "a provider that judges the price fine removes the repair"


async def test_without_a_price_provider_the_price_repair_is_not_judged(hass, loaded_entry, monkeypatch) -> None:
    watcher = loaded_entry.runtime_data.health_watcher
    state = hass.states.get(CAR_PRICE)
    hass.states.async_set(CAR_PRICE, state.state, {**state.attributes, "unit_of_measurement": "DKK/Wh"})
    await watcher.async_check()
    assert issue(hass, loaded_entry, ISSUE_PRICE_SCALE) is not None

    # no provider (the engine is not there yet): not judged, so the repair stays even though the unit is now fine
    monkeypatch.setattr(type(loaded_entry.runtime_data), "price_provider", property(lambda _self: None))
    hass.states.async_set(CAR_PRICE, state.state, {**state.attributes, "unit_of_measurement": "kr/kWh"})
    await watcher.async_check()
    assert issue(hass, loaded_entry, ISSUE_PRICE_SCALE) is not None, "no provider: not judged"


async def test_the_numbers_of_the_repair_text_follow_the_language(hass, entry, automations, watchers, recorder) -> None:
    hass.config.language = "da"
    recorder.numbers = {POWER: KW_HISTORY}
    hass.states.async_set(POWER, "0.8")
    watcher = await start(hass, make(hass, entry, automations, watchers, power_entities=[POWER]))
    found = issue(hass, entry, ISSUE_POWER_UNIT)
    assert found.translation_placeholders["entities"] == "- sensor.house_power (≤ 4,2)"
    assert found.translation_placeholders["max"] == "50"
    watcher.stop()


async def test_the_highest_value_in_a_repair_text_is_rounded_up_never_below_the_real_one(hass, entry, automations, watchers, recorder) -> None:
    recorder.numbers = {POWER: [*KW_HISTORY, 4.2649]}
    hass.states.async_set(POWER, "0.8")
    watcher = await start(hass, make(hass, entry, automations, watchers, power_entities=[POWER]))
    found = issue(hass, entry, ISSUE_POWER_UNIT)
    assert found.translation_placeholders["entities"] == "- sensor.house_power (≤ 4.27)", "4.2649 shown with 3 digits, rounded up"
    watcher.stop()
