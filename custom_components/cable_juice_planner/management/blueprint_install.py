"""Install the bundled automation blueprints into config/blueprints/automation/<domain>/.

Like Home Assistant's own blueprints they are copied at startup when they are missing,
so they show up in the blueprint list without importing a URL. An existing file is never
overwritten on its own: when it differs from the bundled version (a HACS update or the
user's own changes) a repair issue lets the user replace it. The files are compared the
way Home Assistant writes a blueprint (validated and dumped again), so re-importing it
from source_url is not a change.

Inputs that changed from one entity to several still accept a single entity id (the blueprint
turns it into a list), but the automation editor shows a single entity id in a multiple entity
selector as empty, so saving the automation there would drop it. The user's automation
configuration is never written by the integration: automations.yaml is only read, and a
repair lists the automations so the user can select the entity again in the editor.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from homeassistant.components.automation import EVENT_AUTOMATION_RELOADED
from homeassistant.components.blueprint import BLUEPRINT_SCHEMA, Blueprint
from homeassistant.config import AUTOMATION_CONFIG_PATH
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import yaml as yaml_util

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SOURCE = Path(__file__).parent.parent / "blueprints"
ISSUE_PREFIX = "blueprint_update_"
SINGLE_ENTITY_ISSUE = "blueprint_single_entity_inputs"
# inputs that took one entity in earlier versions and take several now
LIST_INPUTS = {"charger_control.yaml": ("charger_status", "power_sensor")}


def _normalized(text: str) -> str:
    """The blueprint as Home Assistant saves it when it is re-imported."""
    try:
        data = yaml_util.parse_yaml(text)
        return Blueprint(data, expected_domain="automation", schema=BLUEPRINT_SCHEMA).yaml()
    except HomeAssistantError:
        return text


def _target(hass: HomeAssistant) -> Path:
    return Path(hass.config.path("blueprints", "automation", DOMAIN))


def _issue_id(name: str) -> str:
    return f"{ISSUE_PREFIX}{Path(name).stem}"


def _single_entity_automations(path: str, inputs_by_path: dict[str, tuple[str, ...]]) -> list[str]:
    """Automations with a single entity id in a LIST_INPUTS input. Only reads the file.

    automations.yaml is the file the automation editor edits, so it holds the automations
    with the problem. The automation entities do not help: for a blueprint automation their
    raw_config is the substituted configuration, without use_blueprint and its inputs.
    """
    if not os.path.isfile(path):
        return []
    try:
        automations = yaml_util.load_yaml(path)
    except HomeAssistantError as err:
        _LOGGER.debug("Could not read %s to check the blueprint inputs: %s", path, err)
        return []
    found: list[str] = []
    for automation in automations if isinstance(automations, list) else []:
        blueprint = automation.get("use_blueprint") if isinstance(automation, dict) else None
        if not isinstance(blueprint, dict) or not isinstance(blueprint.get("input"), dict):
            continue
        inputs = blueprint["input"]
        if any(isinstance(inputs.get(key), str) and inputs[key] for key in inputs_by_path.get(blueprint.get("path"), ())):
            found.append(str(automation.get("alias") or automation.get("id") or blueprint["path"]))
    return found


async def _async_check_single_entity_inputs(hass: HomeAssistant) -> None:
    """Raise a repair for automations with a single entity id in a LIST_INPUTS input (read only)."""
    issues = ir.async_get(hass)
    # while the old blueprint is installed (its update issue is open) the input takes one entity
    inputs_by_path = {
        f"{DOMAIN}/{name}": keys
        for name, keys in LIST_INPUTS.items()
        if issues.async_get_issue(DOMAIN, _issue_id(name)) is None
    }
    found = (
        await hass.async_add_executor_job(_single_entity_automations, hass.config.path(AUTOMATION_CONFIG_PATH), inputs_by_path)
        if inputs_by_path
        else []
    )
    if not found:
        ir.async_delete_issue(hass, DOMAIN, SINGLE_ENTITY_ISSUE)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        SINGLE_ENTITY_ISSUE,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=SINGLE_ENTITY_ISSUE,
        translation_placeholders={"automations": "\n".join(f"- {name}" for name in sorted(found))},
    )


@callback
def _async_track_single_entity_inputs(hass: HomeAssistant) -> None:
    """Check once Home Assistant has started and after each automation reload (the editor saved)."""

    async def _started(_hass: HomeAssistant) -> None:
        await _async_check_single_entity_inputs(hass)

    async def _reloaded(_event: Event) -> None:
        await _async_check_single_entity_inputs(hass)

    async_at_started(hass, _started)
    hass.bus.async_listen(EVENT_AUTOMATION_RELOADED, _reloaded)


async def _async_reload(hass: HomeAssistant) -> None:
    # the blueprint integration caches the blueprints it has read, and automations that are
    # already set up still use the old version - reload them like Home Assistant does after
    # a re-import
    domain_blueprints = hass.data.get("blueprint", {}).get("automation")
    if domain_blueprints is not None:
        await domain_blueprints.async_reset_cache()
        if hass.services.has_service("automation", "reload"):
            await hass.services.async_call("automation", "reload", blocking=False)


async def async_install_blueprints(hass: HomeAssistant) -> None:
    target = _target(hass)

    def _install() -> tuple[list[str], list[str], dict[str, str]]:
        sources = sorted(SOURCE.glob("*.yaml"))
        installed: list[str] = []
        outdated: dict[str, str] = {}
        for source in sources:
            content = source.read_text(encoding="utf-8")
            destination = target / source.name
            if not destination.exists():
                target.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
                installed.append(source.name)
                _LOGGER.info("Installed blueprint %s", destination)
            elif _normalized(destination.read_text(encoding="utf-8")) != _normalized(content):
                outdated[source.name] = yaml_util.parse_yaml(content)["blueprint"]["name"]
        return [source.name for source in sources], installed, outdated

    # earlier versions kept the digests of the installed files here
    await Store(hass, 1, f"{DOMAIN}.blueprints").async_remove()
    try:
        # the folder is listed in the executor too: scandir blocks the event loop
        names, installed, outdated = await hass.async_add_executor_job(_install)
    except (OSError, HomeAssistantError) as err:
        _LOGGER.error("Could not install the Cable Juice Planner blueprints: %s", err)
        return
    for name in names:
        if name not in outdated:
            ir.async_delete_issue(hass, DOMAIN, _issue_id(name))
    for name, title in outdated.items():
        ir.async_create_issue(
            hass,
            DOMAIN,
            _issue_id(name),
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="blueprint_update",
            translation_placeholders={"name": title, "path": f"blueprints/automation/{DOMAIN}/{name}"},
            data={"file": name},
        )
    _async_track_single_entity_inputs(hass)
    if installed:
        await _async_reload(hass)


async def async_update_blueprint(hass: HomeAssistant, name: str) -> None:
    """Replace the installed blueprint with the bundled version (repair fix flow)."""
    source = SOURCE / name
    destination = _target(hass) / name

    def _update() -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    await hass.async_add_executor_job(_update)
    _LOGGER.info("Updated blueprint %s", destination)
    # before the reload: the single entity check after it skips blueprints with this issue
    ir.async_delete_issue(hass, DOMAIN, _issue_id(name))
    await _async_reload(hass)
