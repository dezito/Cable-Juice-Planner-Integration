"""The charger control blueprint reads its power sensors in the units of utils/power.py (its Jinja power_factors table)."""
from __future__ import annotations

from typing import Any

import pytest

from homeassistant.components.blueprint import BLUEPRINT_SCHEMA, Blueprint
from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template
from homeassistant.util import yaml as yaml_util

from custom_components.cable_juice_planner.management.blueprint_install import SOURCE
from custom_components.cable_juice_planner.utils.power import POWER_UNITS, power_factor_to_w

BLUEPRINT = SOURCE / "charger_control.yaml"
ENTITY = "sensor.test_power"
THRESHOLD = 300


@pytest.fixture(scope="module")
def parsed() -> dict[str, Any]:
    return yaml_util.parse_yaml(BLUEPRINT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def factors(parsed: dict[str, Any]) -> dict[str, Any]:
    return parsed["variables"]["power_factors"]


def _strings(node: Any):
    if isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)
    elif isinstance(node, str):
        yield node


@pytest.fixture(scope="module")
def power_templates(parsed: dict[str, Any]) -> list[str]:
    """The three templates that read the power sensor: already_charging and the two waits in the automation."""
    # the blueprint shares some actions through YAML anchors, so a template can be reached more than once
    found = [text for text in _strings(parsed) if "states(e)" in text and "unit_of_measurement" in text]
    return list(dict.fromkeys(found))


def test_the_blueprint_is_valid(parsed: dict[str, Any]) -> None:
    Blueprint(parsed, expected_domain="automation", schema=BLUEPRINT_SCHEMA)


def test_the_blueprint_table_is_the_shared_power_table(factors: dict[str, Any]) -> None:
    assert set(factors) == set(POWER_UNITS)
    for unit in POWER_UNITS:
        assert factors[unit] == power_factor_to_w(unit)


def test_all_three_power_readers_use_the_table(power_templates: list[str]) -> None:
    assert len(power_templates) == 3
    assert all("power_factors.get(" in text and "replace('h', '')" in text for text in power_templates)
    assert all("== 'kW'" not in text for text in power_templates)


async def _evaluate(hass: HomeAssistant, template: str, factors: dict[str, Any], state: str, unit: str | None) -> bool:
    attributes = {} if unit is None else {"unit_of_measurement": unit}
    hass.states.async_set(ENTITY, state, attributes)
    context = {
        "power_entities": [ENTITY],
        "status_entities": [],
        "power_threshold": THRESHOLD,
        "charging_states": ["charging"],
        "completed_states": ["done"],
        "not_ready_states": ["not_ready"],
        "power_factors": factors,
    }
    # parse_result=False: the text, so that "True" and "False" are compared as the blueprint writes them
    return Template(template, hass).async_render(context, parse_result=False) == "True"


# (state, unit, charging, not charging) with the threshold 300 W
CASES = [
    ("0.5", "kW", True, False),
    ("0.1", "kW", False, True),
    ("7.4", "kWh", True, False),  # read as kW: 7400 W (before, kWh was read as W: 7.4 W)
    ("0.1", "kWh", False, True),  # 100 W
    ("0.0005", "MW", True, False),  # 500 W
    ("0.0001", "MW", False, True),  # 100 W
    ("500000", "mW", True, False),  # 500 W
    ("250000", "mW", False, True),  # 250 W
    ("0.000001", "GW", True, False),  # 1000 W
    ("400", "W", True, False),
    ("200", "W", False, True),
    ("400", None, True, False),  # no unit is W
    ("400", "A", True, False),  # an unknown unit is read as W, as before (the repair flags the sensor)
    ("unavailable", "kW", False, False),  # not a number: no value
]


@pytest.mark.parametrize(("state", "unit", "charging", "not_charging"), CASES)
async def test_the_power_readers_follow_the_unit(
    hass: HomeAssistant,
    power_templates: list[str],
    factors: dict[str, Any],
    state: str,
    unit: str | None,
    charging: bool,
    not_charging: bool,
) -> None:
    on_templates = [text for text in power_templates if "ns.off" not in text]
    off_templates = [text for text in power_templates if "ns.off" in text]
    assert len(on_templates) == 2 and len(off_templates) == 1
    for template in on_templates:
        assert await _evaluate(hass, template, factors, state, unit) is charging
    for template in off_templates:
        assert await _evaluate(hass, template, factors, state, unit) is not_charging
