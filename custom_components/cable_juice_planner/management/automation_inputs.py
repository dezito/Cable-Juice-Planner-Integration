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

_LOGGER = logging.getLogger(__name__)

BLUEPRINT_PATH = "cable_juice_planner/charger_control.yaml"
ACTION_INPUT = "action_sensor"


def automations_using_blueprint(path: str, action_sensor: str) -> list[dict]:
    """The inputs of the automations in the file at path that use the charger blueprint for action_sensor.

    Blocking file I/O. [] when the file is missing or unreadable or holds no such automation."""
    if not os.path.isfile(path):
        return []
    try:
        automations = yaml_util.load_yaml(path)
    except (HomeAssistantError, OSError) as err:
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
