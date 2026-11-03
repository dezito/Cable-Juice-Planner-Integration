"""Bundled blueprints are installed when missing and updated through a repair issue."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from homeassistant.components.automation import EVENT_AUTOMATION_RELOADED
from homeassistant.components.blueprint import BLUEPRINT_SCHEMA, Blueprint
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import yaml as yaml_util

from custom_components.cable_juice_planner.management.blueprint_install import (
    SINGLE_ENTITY_ISSUE,
    SOURCE,
    async_install_blueprints,
)
from custom_components.cable_juice_planner.const import DOMAIN
from custom_components.cable_juice_planner.repairs import async_create_fix_flow


@pytest.fixture(autouse=True)
def config_dir(hass: HomeAssistant, tmp_path: Path) -> None:
    """The default test config folder is shared, keep the blueprints out of it."""
    hass.config.config_dir = str(tmp_path)


@pytest.fixture
def reloads(hass: HomeAssistant) -> tuple[AsyncMock, list[ServiceCall]]:
    domain_blueprints = AsyncMock()
    hass.data["blueprint"] = {"automation": domain_blueprints}
    calls: list[ServiceCall] = []
    hass.services.async_register("automation", "reload", calls.append)
    return domain_blueprints, calls


def _bundled() -> dict[str, str]:
    return {source.name: source.read_text(encoding="utf-8") for source in SOURCE.glob("*.yaml")}


def _old(content: str) -> str:
    """A previous version of the blueprint: same file with another description."""
    data = yaml_util.parse_yaml(content)
    data["blueprint"]["description"] = "old version"
    return yaml_util.dump(data)


def _reimported(content: str) -> str:
    """The file as Home Assistant writes it when the blueprint is re-imported from source_url."""
    return Blueprint(yaml_util.parse_yaml(content), expected_domain="automation", schema=BLUEPRINT_SCHEMA).yaml()


def _target(hass: HomeAssistant) -> Path:
    return Path(hass.config.path("blueprints", "automation", DOMAIN))


def _write(hass: HomeAssistant, files: dict[str, str]) -> None:
    _target(hass).mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (_target(hass) / name).write_text(content, encoding="utf-8")


def _files(hass: HomeAssistant) -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in _target(hass).iterdir()}


def _issues(hass: HomeAssistant) -> dict[str, ir.IssueEntry]:
    return {issue_id: issue for (domain, issue_id), issue in ir.async_get(hass).issues.items() if domain == DOMAIN}


async def test_fresh_install(hass: HomeAssistant, reloads: tuple[AsyncMock, list[ServiceCall]]) -> None:
    await async_install_blueprints(hass)
    await hass.async_block_till_done()
    assert _files(hass) == _bundled()
    assert not _issues(hass)
    reloads[0].async_reset_cache.assert_awaited_once()
    assert len(reloads[1]) == 1


@pytest.mark.parametrize("variant", [lambda content: content, _reimported])
async def test_current_version_is_left_alone(
    hass: HomeAssistant, reloads: tuple[AsyncMock, list[ServiceCall]], variant: Any
) -> None:
    """Also when it was re-imported, which rewrites the file in Home Assistant's format."""
    files = {name: variant(content) for name, content in _bundled().items()}
    _write(hass, files)
    await async_install_blueprints(hass)
    await hass.async_block_till_done()
    assert _files(hass) == files
    assert not _issues(hass)
    reloads[0].async_reset_cache.assert_not_awaited()


async def test_other_version_creates_repair(hass: HomeAssistant, reloads: tuple[AsyncMock, list[ServiceCall]]) -> None:
    old = {name: _old(content) for name, content in _bundled().items()}
    _write(hass, old)
    await async_install_blueprints(hass)
    await hass.async_block_till_done()

    assert _files(hass) == old
    issues = _issues(hass)
    assert len(issues) == len(old)
    for issue_id, issue in issues.items():
        name = issue.data["file"]
        assert issue.is_fixable
        assert issue.translation_key == "blueprint_update"
        assert issue.translation_placeholders == {
            "name": yaml_util.parse_yaml(_bundled()[name])["blueprint"]["name"],
            "path": f"blueprints/automation/{DOMAIN}/{name}",
        }

        flow = await async_create_fix_flow(hass, issue_id, issue.data)
        flow.hass = hass
        flow.handler = DOMAIN
        flow.issue_id = issue_id
        flow.flow_id = "test"
        result = await flow.async_step_init()
        assert result["type"] is FlowResultType.FORM
        assert result["description_placeholders"] == issue.translation_placeholders
        result = await flow.async_step_confirm({})
        assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert _files(hass) == _bundled()
    assert len(reloads[1]) == len(old)

    # the next start finds the bundled version and removes a leftover issue
    ir.async_create_issue(hass, DOMAIN, next(iter(issues)), is_fixable=True, severity=ir.IssueSeverity.WARNING, translation_key="blueprint_update")
    await async_install_blueprints(hass)
    assert not _issues(hass)


async def test_old_digest_store_is_removed(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    hass_storage[f"{DOMAIN}.blueprints"] = {"version": 1, "minor_version": 1, "key": f"{DOMAIN}.blueprints", "data": {}}
    await async_install_blueprints(hass)
    assert f"{DOMAIN}.blueprints" not in hass_storage


def _automation(alias: str, blueprint: str, charger_status: Any, power_sensor: Any = None) -> dict[str, Any]:
    """An automation as the automation editor saves it in automations.yaml."""
    inputs = {"action_sensor": "sensor.cjp_tesla_charger_action", "charger_status": charger_status}
    if power_sensor is not None:
        inputs["power_sensor"] = power_sensor
    return {"id": alias.replace(" ", "_"), "alias": alias, "description": "", "use_blueprint": {"path": blueprint, "input": inputs}}


def _write_automations(hass: HomeAssistant, automations: list[dict[str, Any]]) -> str:
    content = yaml_util.dump(automations)
    Path(hass.config.path("automations.yaml")).write_text(content, encoding="utf-8")
    return content


async def test_single_entity_inputs_raise_repair(hass: HomeAssistant, reloads: tuple[AsyncMock, list[ServiceCall]]) -> None:
    """The editor shows a single entity id in a multiple entity selector as empty: a repair lists the automations.

    automations.yaml is only read, never written."""
    _write(hass, _bundled())
    content = _write_automations(
        hass,
        [
            _automation("charger control", f"{DOMAIN}/charger_control.yaml", "sensor.bil_lader_status", ["sensor.bil_lader_power"]),
            _automation("power only", f"{DOMAIN}/charger_control.yaml", [], "sensor.bil_lader_power"),
            _automation("already lists", f"{DOMAIN}/charger_control.yaml", ["sensor.a"], ["sensor.b"]),
            _automation("empty", f"{DOMAIN}/charger_control.yaml", "", ""),
            # another blueprint with an input of the same name is left alone
            _automation("other", "other/blueprint.yaml", "sensor.other"),
            {"id": "plain", "alias": "plain", "triggers": [], "actions": []},
        ],
    )
    await async_install_blueprints(hass)
    await hass.async_block_till_done()

    issue = _issues(hass)[SINGLE_ENTITY_ISSUE]
    assert not issue.is_fixable
    assert issue.translation_key == SINGLE_ENTITY_ISSUE
    assert issue.translation_placeholders == {"automations": "- charger control\n- power only"}
    assert Path(hass.config.path("automations.yaml")).read_text(encoding="utf-8") == content
    assert not reloads[1]

    # the user selected the entities again in the editor: it saves lists and reloads the automations
    _write_automations(hass, [_automation("charger control", f"{DOMAIN}/charger_control.yaml", ["sensor.bil_lader_status"], [])])
    hass.bus.async_fire(EVENT_AUTOMATION_RELOADED)
    await hass.async_block_till_done()
    assert SINGLE_ENTITY_ISSUE not in _issues(hass)


async def test_no_single_entity_repair_while_old_blueprint_is_installed(
    hass: HomeAssistant, reloads: tuple[AsyncMock, list[ServiceCall]]
) -> None:
    """The old blueprint takes one entity there; after the update the repair points at the automations."""
    _write(hass, {name: _old(content) for name, content in _bundled().items()})
    _write_automations(hass, [_automation("charger control", f"{DOMAIN}/charger_control.yaml", "sensor.bil_lader_status")])
    await async_install_blueprints(hass)
    await hass.async_block_till_done()
    assert SINGLE_ENTITY_ISSUE not in _issues(hass)

    issue_id, issue = next((issue_id, issue) for issue_id, issue in _issues(hass).items() if issue.data and issue.data["file"] == "charger_control.yaml")
    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    flow.handler = DOMAIN
    flow.issue_id = issue_id
    flow.flow_id = "test"
    await flow.async_step_confirm({})
    # the reload after the update ends with this event
    hass.bus.async_fire(EVENT_AUTOMATION_RELOADED)
    await hass.async_block_till_done()
    assert _issues(hass)[SINGLE_ENTITY_ISSUE].translation_placeholders == {"automations": "- charger control"}


@pytest.mark.parametrize("content", [None, "", "- alias: [broken\n", "not: a list\n", "- id: secret\n  alias: !secret name\n"])
async def test_missing_or_unreadable_automations_give_no_repair(
    hass: HomeAssistant, reloads: tuple[AsyncMock, list[ServiceCall]], content: str | None
) -> None:
    if content is not None:
        Path(hass.config.path("automations.yaml")).write_text(content, encoding="utf-8")
    await async_install_blueprints(hass)
    await hass.async_block_till_done()
    assert _files(hass) == _bundled()
    assert SINGLE_ENTITY_ISSUE not in _issues(hass)
