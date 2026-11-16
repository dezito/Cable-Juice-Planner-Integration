"""What-if: the charts of the view are hidden when they have no points to draw."""
from __future__ import annotations

import yaml

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.engine.whatif.charts import (
    points_cjp_against_smart,
    points_no_solar,
    points_settings,
    points_solar,
    points_subscription,
)
from custom_components.cable_juice_planner.management.dashboard_build import render_dashboard

from .test_dashboard import OPTIONS, _own
from .test_economy import _engine
from .test_whatif_engine import _run, _seed


def test_scenario_a_counts_the_months_each_chart_draws() -> None:
    months = [
        {"cjp_cost": 1.0, "smart_cost": 2.0, "asap_cost": 3.0, "cjp_vs_smart_adjusted": 0.5, "cjp_vs_asap_adjusted": None},
        {"cjp_cost": 1.0, "smart_cost": 2.0, "asap_cost": 3.0, "cjp_vs_smart_adjusted": None, "cjp_vs_asap_adjusted": None},
        {"cjp_cost": None, "smart_cost": None, "asap_cost": None},
    ]
    assert points_cjp_against_smart({"comparable": True, "months": months}) == {"chart_cost_points": 2, "chart_difference_points": 1}
    # the differences need the three to be comparable
    assert points_cjp_against_smart({"comparable": False, "months": months}) == {"chart_cost_points": 2, "chart_difference_points": 0}
    assert points_cjp_against_smart(None) == {"chart_cost_points": 0, "chart_difference_points": 0}
    assert points_cjp_against_smart({"comparable": True, "months": "nothing"}) == {"chart_cost_points": 0, "chart_difference_points": 0}


def test_the_other_scenarios_count_what_their_charts_draw() -> None:
    settings = {"comparable": True, "months": [{"baseline_cost": 1.0, "alternative_cost": 2.0}, {"baseline_cost": 1.0}]}
    assert points_settings(settings) == {"chart_cost_points": 2, "chart_difference_points": 1}
    assert points_settings({**settings, "comparable": False})["chart_difference_points"] == 0
    no_solar = {"months": [
        {"planned_cost": 5.0, "planned_kwh": 10.0, "recorded_cost": None, "recorded_kwh": 0.0},
        {"planned_cost": 5.0, "planned_kwh": 0.0, "recorded_cost": 4.0, "recorded_kwh": 8.0},
        {"planned_cost": None, "planned_kwh": None, "recorded_cost": None, "recorded_kwh": 0.0},
    ]}
    assert points_no_solar(no_solar) == {"chart_cost_points": 2, "chart_unit_points": 2, "chart_energy_points": 2}
    solar = {"months": [{"grid_cost": 3.0, "sell_value": 1.0, "status": "completed", "advantage": 2.0}, {"grid_cost": 1.0, "sell_value": 1.0, "status": "current", "advantage": 0.0}]}
    assert points_solar(solar) == {"chart_value_points": 2, "chart_cumulative_points": 1}
    assert points_solar({"status": "unavailable"}) == {"chart_value_points": 0, "chart_cumulative_points": 0}
    assert points_subscription([{"subscription_status": "recorded", "subscription_recorded_cost": 10.0}, {"subscription_status": "current"}]) == 1


def test_the_sensor_publishes_the_points_of_its_charts() -> None:
    engine, snap = _engine()
    _seed(engine)
    _, run = _run(engine, snap)
    attributes = run.attributes["what_if"]
    assert attributes["chart_cost_points"] > 0 and attributes["chart_difference_points"] > 0
    assert attributes["chart_cost_points"] == len(attributes["result"]["months"])
    engine.economy_db.pop("what_if", None)  # no result: no chart
    empty = engine.run_job(snap, engine.load_what_if).attributes["what_if"]
    assert empty["chart_cost_points"] == 0 and empty["chart_difference_points"] == 0


def _walk(node, wrappers=()):
    """(chart, the config-template cards around it) for every apexcharts card."""
    if isinstance(node, dict):
        if node.get("type") == "custom:apexcharts-card":
            yield node, wrappers
        inner = wrappers + (node,) if node.get("type") == "custom:config-template-card" else wrappers
        for value in node.values():
            yield from _walk(value, inner)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value, wrappers)


def test_every_chart_of_the_view_is_hidden_when_it_has_no_points() -> None:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    view = yaml.safe_load(render_dashboard(const.build_config({**OPTIONS, "language": "en"}), "tesla", "entry1", existing))["views"][3]
    charts = list(_walk(view))
    assert len(charts) == 11
    for chart, wrappers in charts:
        title = chart["header"]["title"]
        assert len(wrappers) == 1, title
        wrapper = wrappers[0]
        assert wrapper["card"]["type"] == "conditional" and wrapper["card"]["card"] is chart, title
        entity = wrapper["entities"][0]
        assert {s["entity"] for s in chart["series"]} == {entity}, title  # the chart draws what its points count
        assert wrapper["variables"]["points"].endswith("_points) : 0"), title
        # only documented conditions of the conditional card: a state that is never reached when there are no points
        assert "numeric_state" not in wrapper["card"]["conditions"] and "attribute:" not in wrapper["card"]["conditions"], title
