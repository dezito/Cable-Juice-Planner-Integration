"""Which actions of the charger blueprint automation have steps: read from automations.yaml, never written.

CJP can not see what an automation does, but an automation made from the blueprint has its inputs in the file the
automation editor writes. The entity of an automation does not help: for a blueprint automation its raw_config is the
substituted configuration, without use_blueprint and its inputs. Automations kept in other files (packages, YAML mode)
are not found, and the test then does not know: it never reads that as "no steps".
"""
from __future__ import annotations

import logging
import os

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import yaml as yaml_util

from ..const import (
    CHARGER_CHARGING_STATUS,
    CHARGER_COMPLETED_STATUS,
    CHARGER_NOT_READY_STATUS,
    CHARGER_READY_STATUS,
    normalize_state,
)

_LOGGER = logging.getLogger(__name__)

BLUEPRINT_PATH = "cable_juice_planner/charger_control.yaml"
ACTION_INPUT = "action_sensor"
STATUS_INPUT = "charger_status"
# the blueprint's lists of states and the built in state each one is read as (the canonical state, const/base.py)
STATE_INPUTS = {"charging_states": CHARGER_CHARGING_STATUS[0], "completed_states": CHARGER_COMPLETED_STATUS[0]}


def automations_using_blueprint(path: str, action_sensor: str) -> list[dict]:
    """The inputs of the automations in the file at path that use the charger blueprint for action_sensor.

    Blocking file I/O. [] when the file is missing or unreadable or holds no such automation."""
    if not os.path.isfile(path):
        return []
    try:
        automations = yaml_util.load_yaml(path)
    except (HomeAssistantError, OSError, ValueError) as err:
        _LOGGER.debug("Could not read %s to see which actions the automation has steps for: %s", path, err)
        return []
    found: list[dict] = []
    for automation in automations if isinstance(automations, list) else []:
        blueprint = automation.get("use_blueprint") if isinstance(automation, dict) else None
        if not isinstance(blueprint, dict) or blueprint.get("path") != BLUEPRINT_PATH or not isinstance(blueprint.get("input"), dict):
            continue
        if blueprint["input"].get(ACTION_INPUT) == action_sensor:
            found.append(blueprint["input"])
    return found


def blueprint_automation_ids(path: str, action_sensor: str) -> list[str] | None:
    """The ids of the automations in the file at path that use the charger blueprint for action_sensor (health checks).

    Blocking file I/O, read only. [] when the file is missing or holds no such automation. None when the file exists but can't
    be read or isn't a list of automations (an empty file included): then it is not known whether the automation is still
    there. An automation without an id can't be found in the entity registry, so it is left out."""
    if not os.path.isfile(path):
        return []
    try:
        automations = yaml_util.load_yaml(path)
    except (HomeAssistantError, OSError, ValueError) as err:
        _LOGGER.debug("Could not read %s to check the charger automation: %s", path, err)
        return None
    if not isinstance(automations, list):
        return None
    ids: list[str] = []
    for automation in automations:
        blueprint = automation.get("use_blueprint") if isinstance(automation, dict) else None
        if not isinstance(blueprint, dict) or blueprint.get("path") != BLUEPRINT_PATH or not isinstance(blueprint.get("input"), dict):
            continue
        if blueprint["input"].get(ACTION_INPUT) == action_sensor and automation.get("id") is not None:
            ids.append(str(automation["id"]))
    return ids


def _listed(value) -> list[str]:
    """The entity ids or states of a blueprint input: a list (the multiple selectors) or one text (older automations)."""
    items = value if isinstance(value, list) else [value]
    return [normalize_state(item) for item in items if isinstance(item, str) and item.strip()]


def status_aliases(inputs: list[dict], status_entity_id: str) -> dict[str, str]:
    """The states the user gave the charger blueprint for CJP's status entity, as {state: built in state}.

    "States that mean charging" and "States that mean done" of an automation that has this entity among its status
    entities are read as charging and completed. The built in states always apply and always win: a state that is built in
    (ready, plugged in ...) is never moved, and a state the automations name as both charging and done is left out (it is
    not known which they mean). "States that mean the charger is not ready" is not used: it also holds states CJP reads as
    plugged in (awaiting_start). An input that is not set is the blueprint's default, which the built in states cover.
    The states are lower case, like every comparison (normalize_state)."""
    entity = normalize_state(status_entity_id) if status_entity_id else ""
    found: dict[str, set[str]] = {}
    for automation in inputs:
        if not entity or entity not in _listed(automation.get(STATUS_INPUT)):
            continue
        for key, built_in in STATE_INPUTS.items():
            for state in _listed(automation.get(key)):
                found.setdefault(state, set()).add(built_in)
    # also what means no data or a fault (unknown, unavailable, error): that is never charging or done, whatever the user lists
    known = (*CHARGER_READY_STATUS, *CHARGER_NOT_READY_STATUS, *CHARGER_COMPLETED_STATUS, *CHARGER_CHARGING_STATUS, "error", "unknown", "unavailable", "none")
    return {state: next(iter(kinds)) for state, kinds in found.items() if len(kinds) == 1 and state not in known}


def has_steps(inputs: list[dict], *keys: str) -> bool | None:
    """Whether any automation of inputs has steps in any of the input keys; None when no automation was found (not known).

    One function of the charger can be done by several inputs of the blueprint, so the caller names all of them (e.g.
    the current is set on the charger, in the car or as the circuit limit)."""
    if not inputs:
        return None
    return any(automation.get(key) for automation in inputs for key in keys)


# the inputs of the blueprint that can do each part of the test
AMPS_INPUTS = ("amps_actions", "car_amps_actions", "circuit_actions")
# a charger that pauses at 0 A is started and stopped by its charging current (the blueprint sets 0 on stop); the car's
# current is never set to 0, so it does neither
START_INPUTS = ("start_actions", "car_start_actions", "amps_actions")
STOP_INPUTS = ("stop_actions", "car_stop_actions", "amps_actions")
# the car's charge limit (the test, and the departure notice at plug-in)
LIMIT_INPUTS = ("car_limit_actions",)
