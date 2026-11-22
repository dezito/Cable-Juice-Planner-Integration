"""Power units: the rule for the readers of a power sensor (W, kW, mW, MW, GW and their h-variants).

Used by the charger test, charging_without_rule, the charging loss, the What-if statistics, load balancing and the repair.
Exceptions: the engine's history (power_convert) reads an unknown unit as W, as it always did; the config flow's
suggestions (flow/detection.py) have their own stricter table; load balancing reads a phase sensor in an energy unit as
no measurement (with the repair site.ISSUE_PHASE_UNITS) and takes a charger power in an energy unit not from the house
(the fuse: never lower than before).

The factors are the ones power_convert uses (engine/history/history.py _CONVERSION_FACTORS, kept the same by
tests/test_power_units.py; this module doesn't import the engine, which imports it): a unit with an h (Wh, kWh, MWh) is read
as the unit without it, like power_convert and BattMind. A sensor without a unit is read as W. Any other unit is not a power:
the helpers return None, so a reader drops the value instead of guessing its size (the repair of management/health_checks.py
names such a sensor). The blueprint (blueprints/charger_control.yaml) has the same factors in Jinja, kept the same by
tests/test_blueprint_power_units.py; unlike these helpers it still reads an unknown unit as W, as it always did.
"""
from __future__ import annotations

from typing import Any

_FACTORS_TO_W: dict[str, float] = {"mW": 0.001, "W": 1.0, "kW": 1000.0, "MW": 1000000.0, "GW": 1000000000.0}

# the power units, in the order of the table: mW, W, kW, MW, GW
POWER_UNITS: tuple[str, ...] = tuple(_FACTORS_TO_W)

# the units statistics_during_period is asked for when it reads a power sensor. Home Assistant converts a statistic by the
# unit class it was recorded in: a power sensor in W, kW, ... has power statistics (given in W), one in Wh, kWh, ... has
# energy statistics (given in kWh). Without the energy entry they would come in the sensor's current unit.
STATISTICS_POWER_UNITS: dict[str, str] = {"power": "W", "energy": "kWh"}


def power_factor_to_w(unit: Any) -> float | None:
    """The factor that brings a value in this unit to W, or None when the unit is not a power unit.

    No unit (None or "") is W. A unit that is not text (a number, a list) is never read as a size."""
    if unit is None or unit == "":
        return 1.0
    if not isinstance(unit, str):
        return None
    return _FACTORS_TO_W.get(unit.replace("h", ""))


def power_unit_known(unit: Any) -> bool:
    """Whether power_factor_to_w reads the unit (no unit, a power unit or its h-variant)."""
    return power_factor_to_w(unit) is not None


def power_to_w(value: float | None, unit: Any) -> float | None:
    """The value in W, or None when there is no value or the unit is not a power unit."""
    factor = power_factor_to_w(unit)
    if value is None or factor is None:
        return None
    return value * factor


def statistic_power_factor_to_w(statistic_unit: Any) -> float | None:
    """The factor that brings a value of statistics_during_period(..., units=STATISTICS_POWER_UNITS) to W.

    statistic_unit: the unit the statistic was recorded in (get_metadata: "unit_of_measurement"). A power statistic comes
    in W and an energy statistic of a power sensor in an h unit in kWh (read as kW). A statistic without a unit class
    comes in its own unit. None when the values can't be read as a power."""
    from homeassistant.util.unit_conversion import EnergyConverter, PowerConverter  # noqa: PLC0415

    if statistic_unit in PowerConverter.VALID_UNITS:
        return 1.0
    if statistic_unit in EnergyConverter.VALID_UNITS:
        # only the h-variants of the power units are a power (Wh, kWh, MWh, ...); GJ or cal never are
        return power_factor_to_w("kWh") if power_unit_known(statistic_unit) and statistic_unit else None
    return power_factor_to_w(statistic_unit)
