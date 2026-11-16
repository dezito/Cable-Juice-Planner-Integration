"""What-if: how many points each chart of the dashboard has (the dashboard hides a chart that has none).

Pure (plain dicts in, plain ints out). The numbers are published as flat attributes of the result sensors, because the
frontend's conditional card can only look at a top-level attribute. Each count follows the data generator of its chart in
dashboard/cable_juice_planner.yaml: a month is a point when the chart would draw it.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _months(result: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    months = (result or {}).get("months")
    return [month for month in months if isinstance(month, Mapping)] if isinstance(months, list) else []


def _count(months: Iterable[Mapping[str, Any]], *keys: str) -> int:
    """The months in which at least one of the values is a number."""
    return sum(1 for month in months if any(_finite(month.get(key)) for key in keys))


def points_cjp_against_smart(result: Mapping[str, Any] | None) -> dict[str, int]:
    """Scenario A: the cost per month of the three, and the differences (which need the three to be comparable)."""
    months = _months(result)
    comparable = bool((result or {}).get("comparable"))
    return {
        "chart_cost_points": _count(months, "cjp_cost", "smart_cost", "asap_cost"),
        "chart_difference_points": _count(months, "cjp_vs_smart_adjusted", "cjp_vs_asap_adjusted") if comparable else 0,
    }


def points_settings(result: Mapping[str, Any] | None) -> dict[str, int]:
    """Scenario B: the cost per month of both settings, and the cumulative difference (which needs them comparable)."""
    months = _months(result)
    comparable = bool((result or {}).get("comparable"))
    return {
        "chart_cost_points": _count(months, "baseline_cost", "alternative_cost"),
        "chart_difference_points": sum(
            1 for month in months if _finite(month.get("alternative_cost")) and _finite(month.get("baseline_cost"))
        ) if comparable else 0,
    }


def points_no_solar(result: Mapping[str, Any] | None) -> dict[str, int]:
    """Without solar: the cost, the price per kWh and the energy per month, planned and recorded."""
    months = _months(result)
    price = sum(
        1 for month in months
        if (_finite(month.get("planned_kwh")) and month["planned_kwh"] > 0 and _finite(month.get("planned_cost")))
        or (_finite(month.get("recorded_cost")) and _finite(month.get("recorded_kwh")) and month["recorded_kwh"] > 0)
    )
    energy = sum(
        1 for month in months
        if _finite(month.get("planned_kwh")) or (_finite(month.get("recorded_kwh")) and month["recorded_kwh"] > 0)
    )
    return {"chart_cost_points": _count(months, "planned_cost", "recorded_cost"), "chart_unit_points": price, "chart_energy_points": energy}


def points_solar(result: Mapping[str, Any] | None) -> dict[str, int]:
    """The EV's solar energy replaced by grid energy: the two values per month, and the cumulative advantage (completed months)."""
    months = _months(result)
    return {
        "chart_value_points": _count(months, "grid_cost", "sell_value"),
        "chart_cumulative_points": sum(1 for month in months if month.get("status") == "completed" and _finite(month.get("advantage"))),
    }


def points_subscription(months: Iterable[Mapping[str, Any]]) -> int:
    """The fixed monthly price: the completed months with a recorded cost."""
    return sum(
        1 for month in months
        if month.get("subscription_status") == "recorded" and _finite(month.get("subscription_recorded_cost"))
    )
