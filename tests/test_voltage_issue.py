"""The repair for a grid voltage that is still the old default of 240 V (the default is 230 V now)."""
from __future__ import annotations

from pathlib import Path

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir

from custom_components.cable_juice_planner.const import DEFAULT_CONFIG, DOMAIN
from custom_components.cable_juice_planner.engine.state import JobResult
from custom_components.cable_juice_planner.management.voltage_issue import (
    OLD_DEFAULT_VOLTAGE,
    VOLTAGE_ISSUE,
    voltage_issue_id,
)
from custom_components.cable_juice_planner.repairs import BlueprintUpdateFlow, async_create_fix_flow

from .conftest import MOCK_OPTIONS, async_setup, async_unload_and_join, make_entry, make_site_entry, set_source_states


@pytest.fixture(autouse=True)
def config_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    """Removing a car exports a backup into <config>/cable_juice_planner_backups: keep it out of the shared test config."""
    hass.config.config_dir = str(tmp_path)
    return tmp_path


def _issue(hass: HomeAssistant, entry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, voltage_issue_id(entry.entry_id))


async def _fix(hass: HomeAssistant, entry) -> None:
    issue = _issue(hass, entry)
    flow = await async_create_fix_flow(hass, issue.issue_id, issue.data)
    flow.hass, flow.handler, flow.issue_id, flow.flow_id = hass, DOMAIN, issue.issue_id, "test"
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM and result["description_placeholders"] == issue.translation_placeholders
    result = await flow.async_step_confirm({})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)


async def _unload(hass: HomeAssistant, entry) -> None:
    if entry.state is ConfigEntryState.LOADED:
        await async_unload_and_join(hass, entry)


def test_the_default_voltage_is_230_v() -> None:
    assert DEFAULT_CONFIG["charger"]["power_voltage"] == 230.0 and OLD_DEFAULT_VOLTAGE == 240.0


async def test_the_old_default_is_a_repair_that_sets_230_v(hass: HomeAssistant) -> None:
    set_source_states(hass)
    entry = make_entry(options={**MOCK_OPTIONS, "power_voltage": 240.0})
    await async_setup(hass, entry)
    issue = _issue(hass, entry)
    assert issue is not None and issue.is_fixable and issue.translation_key == VOLTAGE_ISSUE
    assert issue.severity is ir.IssueSeverity.WARNING and not issue.is_persistent
    assert issue.translation_placeholders == {"name": entry.title, "voltage": "240", "default": "230"}

    await _fix(hass, entry)
    assert entry.options["power_voltage"] == 230.0
    assert all(value == entry.options[key] for key, value in MOCK_OPTIONS.items() if key != "power_voltage")  # nothing else changed
    assert _issue(hass, entry) is None
    assert entry.state is ConfigEntryState.LOADED and entry.runtime_data.config["charger"]["power_voltage"] == 230.0
    await _unload(hass, entry)


async def test_another_voltage_has_no_repair_and_a_changed_one_removes_it(hass: HomeAssistant) -> None:
    set_source_states(hass)
    entry = make_entry(options={**MOCK_OPTIONS, "power_voltage": 230.0})
    await async_setup(hass, entry)
    assert _issue(hass, entry) is None

    hass.config_entries.async_update_entry(entry, options={**entry.options, "power_voltage": 240.0})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _issue(hass, entry) is not None  # the options reload the entry, which checks again

    # the user sets another voltage in the settings (not through the repair)
    hass.config_entries.async_update_entry(entry, options={**entry.options, "power_voltage": 235.0})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _issue(hass, entry) is None
    await _unload(hass, entry)


async def test_an_ignored_repair_stays_ignored_over_a_reload(hass: HomeAssistant) -> None:
    """A grid that really has 240 V (the UK, for example): the user ignores the repair, and it is not raised again."""
    set_source_states(hass)
    entry = make_entry(options={**MOCK_OPTIONS, "power_voltage": 240.0})
    await async_setup(hass, entry)
    ir.async_ignore_issue(hass, DOMAIN, voltage_issue_id(entry.entry_id), True)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _issue(hass, entry).dismissed_version is not None
    await _unload(hass, entry)


async def test_a_fix_after_the_user_changed_the_voltage_keeps_the_users_voltage(hass: HomeAssistant) -> None:
    set_source_states(hass)
    entry = make_entry(options={**MOCK_OPTIONS, "power_voltage": 240.0})
    await async_setup(hass, entry)
    issue = _issue(hass, entry)
    flow = await async_create_fix_flow(hass, issue.issue_id, issue.data)
    flow.hass, flow.handler, flow.issue_id, flow.flow_id = hass, DOMAIN, issue.issue_id, "test"
    await flow.async_step_init()
    # the settings were changed while the repair was open
    hass.config_entries.async_update_entry(entry, options={**entry.options, "power_voltage": 220.0})
    await hass.async_block_till_done(wait_background_tasks=True)
    await flow.async_step_confirm({})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.options["power_voltage"] == 220.0 and _issue(hass, entry) is None
    await _unload(hass, entry)


async def test_an_unchanged_learned_charging_loss_does_not_swallow_the_fix(hass: HomeAssistant) -> None:
    """The learned charging loss written with the same value changes nothing: the next options change (the fix) still reloads."""
    set_source_states(hass)
    entry = make_entry(options={**MOCK_OPTIONS, "power_voltage": 240.0})
    await async_setup(hass, entry)
    coordinator = entry.runtime_data
    await coordinator._async_publish(JobResult(config_updates={"charging_loss": entry.options["charging_loss"]}), "test")
    assert not coordinator.ignore_next_update

    await _fix(hass, entry)
    assert entry.runtime_data is not coordinator and entry.runtime_data.config["charger"]["power_voltage"] == 230.0
    await _unload(hass, entry)


async def test_a_changed_learned_charging_loss_does_not_reload(hass: HomeAssistant) -> None:
    set_source_states(hass)
    entry = make_entry(options={**MOCK_OPTIONS, "power_voltage": 230.0})
    await async_setup(hass, entry)
    coordinator = entry.runtime_data
    await coordinator._async_publish(JobResult(config_updates={"charging_loss": -0.07}), "test")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.options["charging_loss"] == -0.07 and entry.runtime_data is coordinator and not coordinator.ignore_next_update
    await _unload(hass, entry)


async def test_removing_the_entry_removes_the_repair(hass: HomeAssistant) -> None:
    set_source_states(hass)
    entry = make_entry(options={**MOCK_OPTIONS, "power_voltage": 240.0})
    await async_setup(hass, entry)
    assert _issue(hass, entry) is not None
    coordinator = entry.runtime_data
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    for name in ("_executor", "_what_if_executor"):
        if hasattr(coordinator, name):
            await hass.async_add_executor_job(getattr(coordinator, name).shutdown, True)
    assert _issue(hass, entry) is None


async def test_the_site_has_no_voltage_repair(hass: HomeAssistant) -> None:
    site = make_site_entry()
    await async_setup(hass, site)
    assert _issue(hass, site) is None
    await hass.config_entries.async_unload(site.entry_id)


async def test_the_blueprint_repair_still_gets_its_own_flow(hass: HomeAssistant) -> None:
    flow = await async_create_fix_flow(hass, "blueprint_update_charger_control.yaml", {"file": "charger_control.yaml"})
    assert isinstance(flow, BlueprintUpdateFlow)
