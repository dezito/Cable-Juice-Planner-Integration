"""The shared power unit rule (utils/power.py)."""
from __future__ import annotations

import pytest

from custom_components.cable_juice_planner.engine.history.history import _CONVERSION_FACTORS, power_convert
from custom_components.cable_juice_planner.utils.power import (
    POWER_UNITS,
    STATISTICS_POWER_UNITS,
    power_factor_to_w,
    power_to_w,
    power_unit_known,
    statistic_power_factor_to_w,
)


def test_the_power_units_are_the_table_of_power_convert() -> None:
    assert POWER_UNITS == ("mW", "W", "kW", "MW", "GW")
    assert POWER_UNITS == tuple(unit for unit, factors in _CONVERSION_FACTORS.items() if "W" in factors)
    for unit in POWER_UNITS:
        assert power_factor_to_w(unit) == pytest.approx(_CONVERSION_FACTORS[unit]["W"])


@pytest.mark.parametrize(
    ("unit", "factor"),
    [
        ("mW", 0.001), ("W", 1.0), ("kW", 1000.0), ("MW", 1e6), ("GW", 1e9),
        ("mWh", 0.001), ("Wh", 1.0), ("kWh", 1000.0), ("MWh", 1e6), ("GWh", 1e9),
        (None, 1.0), ("", 1.0),
        ("A", None), ("kVA", None), ("%", None), ("km", None), ("TW", None), ("BTU/h", None), (5, None), (["kW"], None),
    ],
)
def test_the_factor_to_watt(unit, factor) -> None:
    assert power_factor_to_w(unit) == factor
    assert power_unit_known(unit) is (factor is not None)


@pytest.mark.parametrize("unit", ["mW", "W", "kW", "MW", "GW", "mWh", "Wh", "kWh", "MWh"])
def test_the_factor_reads_like_power_convert(unit) -> None:
    """Every reader gets the same W as the history (power_convert) for the units both read."""
    assert power_to_w(7.4, unit) == pytest.approx(power_convert(7.4, unit, "W"))


def test_no_value_or_an_unknown_unit_gives_no_watt() -> None:
    assert power_to_w(None, "kW") is None
    assert power_to_w(3.0, "A") is None
    assert power_to_w(-2.5, "kW") == -2500.0


@pytest.mark.parametrize(
    ("statistic_unit", "factor"),
    [
        # power statistics come in W (units={"power": "W"})
        ("W", 1.0), ("kW", 1.0), ("mW", 1.0), ("MW", 1.0), ("GW", 1.0), ("BTU/h", 1.0),
        # a power sensor in an h unit has energy statistics; they come in kWh, read as kW
        ("Wh", 1000.0), ("kWh", 1000.0), ("MWh", 1000.0),
        # an energy unit that is no power, or no unit class at all
        ("GJ", None), ("A", None),
        (None, 1.0),
    ],
)
def test_the_statistic_factor_to_watt(statistic_unit, factor) -> None:
    assert statistic_power_factor_to_w(statistic_unit) == factor


def test_the_statistics_are_asked_for_in_watt_and_kilowatt_hours() -> None:
    assert STATISTICS_POWER_UNITS == {"power": "W", "energy": "kWh"}
    assert all("W" in _CONVERSION_FACTORS[unit] for unit in POWER_UNITS)
