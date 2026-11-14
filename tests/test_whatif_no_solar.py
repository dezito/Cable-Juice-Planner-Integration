"""What-if Scenario D without solar: the charging planned from the grid only over the selected period."""
from __future__ import annotations

import datetime
import re
from dataclasses import replace
from unittest.mock import patch

import pytest
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.engine.whatif.replay import CjpSettings, replay_plan
from custom_components.cable_juice_planner.management.dashboard_build import render_dashboard

from .conftest import OBJECT_PREFIX
from .test_dashboard import OPTIONS, _cards, _own
from .test_economy import _engine, _ts
from .test_engine import DAY
from .test_whatif_coordinator import _seed as _seed_coordinator
from .test_whatif_engine import _seed as _seed_connections
from .test_whatif_engine import snapshot_with


def _seed(engine, **kwargs):
    """The connections, prices and drives, without the charging CJP did (scenario A's record): this scenario has its own sessions."""
    return _seed_connections(engine, charge=False, **kwargs)
from .test_whatif_replay import LOSS, flat, hour, make
from .test_whatif_settings_replay import three_days

LIVE = CjpSettings(reserve=30.0, maximum=90.0)
LANGUAGES = ("da", "de", "en", "en-GB", "pl", "uk")
SENSOR = f"sensor.{OBJECT_PREFIX}_what_if_no_solar"
BUTTON = f"button.{OBJECT_PREFIX}_run_what_if_no_solar"


# --------------------------------------------------------------------------- replay


def test_the_plan_buys_all_energy_from_the_grid_in_the_cheapest_hours_to_the_need_plus_the_reserve() -> None:
    prices = {n: (0.2 if 2 <= n % 24 <= 4 else 1.0) for n in range(60)}
    result = replay_plan(three_days(prices), LIVE)
    assert result.status == "ok" and result.method == "strict" and "grid_only_no_solar" in result.notes
    assert result.windows_replayed == 2 and len(result.targets) == 2
    plan = result.plan
    assert plan.grid_kwh == pytest.approx(plan.charged_kwh * (1.0 + LOSS))  # the loss is on the charger side
    assert plan.cost / plan.grid_kwh < result.average_price  # the cheap hours were used
    assert result.max_conservation_error_kwh < 1e-6
    assert result.readiness.arrivals == 2 and result.readiness.arrivals_below_reserve == 0


def test_the_plan_estimates_missing_prices_and_calibrates_an_unknown_start_and_says_so() -> None:
    prices = {i: 1.0 for i in range(60)}
    for i in range(20, 32):
        del prices[i]
    result = replay_plan(replace(three_days(prices), start_soc=None), LIVE)
    assert result.status == "ok" and result.method == "estimated" and result.estimated_windows == 1 and result.calibrated_start
    assert result.price_estimate == pytest.approx(1.0) and {"prices_estimated_windows", "start_level_unknown_calibrated"} <= set(result.warnings)
    assert result.plan.windows[0].calibrated and result.plan.windows[0].cost == 0.0


def test_the_plan_needs_two_connections_and_half_of_them_priced_and_valid_settings() -> None:
    assert replay_plan(make([(0, 8)], flat(10), start=50.0), LIVE).reason == "too_few_windows"
    assert replay_plan(three_days({}), LIVE).reason == "missing_prices"
    assert replay_plan(three_days(), CjpSettings(-1.0, 90.0)).reason == "invalid_settings"
    assert replay_plan(make([], flat(2), start=50.0), LIVE).reason == "no_windows"


# --------------------------------------------------------------------------- engine


def _run(engine, snap, start=None):
    prepared = engine.run_job(snap, engine.what_if_no_solar_prepare)
    assert prepared.error is None, prepared.error
    result = engine.run_job(snap, engine.what_if_no_solar_run, start or {"soc": 50.0, "source": "entered"})
    assert result.error is None, result.error
    return prepared, result


def _recorded(engine, solar: float = 4.0, cost: float = 9.0) -> None:
    """Two private charging sessions with solar energy inside the seeded connections (recorded after the solar energy
    was stored as energy)."""
    engine.economy_db["solar_energy_since"] = 0.0
    for back in (3, 2):
        day = DAY - datetime.timedelta(days=back)
        engine.charging_history_db[datetime.datetime.combine(day, datetime.time(20, 5))] = {
            "kWh": 10.0, "cost": cost, "kWh_from_grid": 10.0 - solar, "kWh_from_local_energy": solar, "solar_kwh_of_local_energy": solar,
            "powerwall_kwh_of_local_energy": 0.0, "ended": "21:00", "charging_type": ["planned"],
        }


def test_the_plan_replays_the_period_and_compares_it_with_the_recorded_charging() -> None:
    engine, snap = _engine()
    _seed(engine, public=False)
    _recorded(engine)
    _, result = _run(engine, snap)
    r = result.value
    assert r["status"] == "ok" and r["method"] == "strict" and r["settings"] == {"reserve": 30.0, "maximum": 90.0}
    assert r["windows"] == {"total": 5, "replayed": 4, "derived": 0} and r["plan"]["unit"] is not None
    recorded = r["recorded"]
    assert recorded["sessions"] == 2 and recorded["kwh"] == 20.0 and recorded["cost"] == 18.0 and recorded["solar_kwh"] == 8.0
    estimate = r["estimate"]
    # the recorded energy at the price per kWh the planned charging paid, against what was recorded:
    # difference vs plan = recorded cost - that (positive: the recorded charging cost more), not a solar value
    assert set(estimate) == {"cost_at_plan_price", "recorded_cost", "difference_vs_plan", "kwh", "unit"}
    assert estimate["cost_at_plan_price"] == pytest.approx(20.0 * r["plan_measured"]["unit"], abs=0.01)
    assert estimate["difference_vs_plan"] == pytest.approx(18.0 - estimate["cost_at_plan_price"], abs=0.011)
    assert result.states["what_if_no_solar"] == estimate["difference_vs_plan"]
    assert "grid_only_no_solar" in r["notes"] and engine.economy_db["what_if_no_solar"]["computed_at"] == r["computed_at"]
    assert r["months"] and r["months"][0]["recorded_kwh"] > 0
    assert "what_if" not in engine.economy_db and "what_if_settings" not in engine.economy_db  # the other scenarios keep their own


def test_nothing_live_is_changed_and_a_missing_start_level_is_calibrated_not_defaulted() -> None:
    engine, snap = _engine()
    _seed(engine, public=False)
    live = (engine.get_min_daily_battery_level(), engine.get_max_recommended_charge_limit_battery_level())
    _, result = _run(engine, snap, {"soc": None, "source": "unavailable"})
    assert result.value["status"] == "ok" and result.value["calibrated_start"] and "start_level_unknown_calibrated" in result.value["warnings"]
    assert not result.settings and not getattr(result, "config_updates", None)
    assert (engine.get_min_daily_battery_level(), engine.get_max_recommended_charge_limit_battery_level()) == live


def test_without_recorded_private_charging_there_is_no_estimate_but_the_plan_is_shown() -> None:
    engine, snap = _engine()
    _seed(engine, public=False)
    _, result = _run(engine, snap)
    r = result.value
    assert r["status"] == "ok" and r["estimate"] is None and r["recorded"]["sessions"] == 0 and r["plan"]["cost"] is not None
    assert result.states["what_if_no_solar"] is None


def test_an_invalid_recorded_cost_gives_no_estimate() -> None:
    engine, snap = _engine()
    _seed(engine, public=False)
    _recorded(engine, cost=float("nan"))
    r = _run(engine, snap)[1].value
    assert r["recorded"]["cost"] is None and r["estimate"] is None


def test_a_failed_run_keeps_the_previous_result_and_an_interrupted_one_is_marked() -> None:
    engine, snap = _engine()
    _seed(engine, public=False)
    _recorded(engine)
    _, first = _run(engine, snap)
    saved = engine.economy_db["what_if_no_solar"]
    _, failed = _run(engine, snap, {"soc": 50.0, "source": "entered", "first_window_start": 1.0})
    assert failed.value["reason"] == "start_level_stale" and engine.economy_db["what_if_no_solar"] is saved
    assert failed.states["what_if_no_solar"] == first.value["estimate"]["difference_vs_plan"]
    engine.economy_db["what_if_no_solar_attempt"] = {"status": "running", "reason": None, "at": 1.0}
    engine.load_what_if_no_solar()
    assert engine.economy_db["what_if_no_solar_attempt"]["reason"] == "interrupted"


def test_no_recorded_drive_means_unknown_demand() -> None:
    engine, snap = _engine()
    _seed(engine, public=False)
    engine.analysis_db["drives"].clear()
    assert _run(engine, snap)[1].value["reason"] == "no_drives"


# --------------------------------------------------------------------------- dashboard


def _content(language: str) -> str:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    text = render_dashboard(const.build_config({**OPTIONS, "language": language}), "tesla", "entry1", existing)
    assert "@@" not in text
    view = yaml.safe_load(text)["views"][3]
    return next(c["content"] for c in _cards(view) if c.get("type") == "markdown" and "states.sensor." in c.get("content", "") and "what_if_no_solar" in c["content"])


def _attributes() -> dict:
    engine, snap = _engine()
    _seed(engine, public=False)
    _recorded(engine)
    _, result = _run(engine, snapshot_with(snap, {}), {"soc": None, "source": "unavailable"})
    assert result.value["status"] == "ok"
    return result.attributes["what_if_no_solar"]


async def _render(hass: HomeAssistant, language: str, attributes: dict) -> str:
    content = _content(language)
    hass.states.async_set(re.search(r"states\.(sensor\.\w+)\.attributes", content).group(1), "0", attributes)
    return Template(content, hass).async_render(parse_result=False, strict=True)  # like the frontend: an undefined attribute is an error


async def test_the_card_shows_the_plan_the_recorded_charging_the_estimate_and_the_warnings(hass: HomeAssistant) -> None:
    attributes = _attributes()
    out = await _render(hass, "en", attributes)
    for expected in (
        "Latest result", "Current settings, from the grid (no solar)", "Recorded (with solar)", "Difference (without solar - recorded)", "Price per kWh", "Of which solar energy", "Estimate for the recorded energy",
        "Difference vs plan (recorded - at the plan's price)", "This is not the value of the solar energy", "Per month", "Coverage", "Estimate: part of the data was missing", "first connection is only used to bring the car",
        "All energy is bought from the grid", "Assumptions and what is not included", "Homecomings below the current reserve",
    ):
        assert expected in out, expected
    assert "None" not in out and "Undefined" not in out and "@@" not in out
    r = attributes["result"]
    assert str(r["plan"]["cost"]) in out and str(r["estimate"]["difference_vs_plan"]) in out and str(r["recorded"]["cost"]) in out
    assert "Solar advantage" not in out


async def test_a_result_saved_before_the_difference_and_measured_span_fields_still_renders(hass: HomeAssistant) -> None:
    attributes = _attributes()
    old = {**attributes, "result": {k: v for k, v in attributes["result"].items() if k != "plan_measured"}}
    for language in ("da", "en"):
        out = await _render(hass, language, old)
        assert "None" not in out and "Undefined" not in out and str(old["result"]["plan"]["cost"]) in out, language


async def test_every_language_renders_without_missing_texts_and_every_warning_and_reason_is_translated(hass: HomeAssistant) -> None:
    attributes = _attributes()
    warnings = ["prices_estimated_windows", "start_level_unknown_calibrated", "prices_from_price_history", "events_inside_windows"]
    for language in LANGUAGES:
        result = {**attributes["result"], "warnings": warnings}
        out = await _render(hass, language, {**attributes, "result": result})
        assert "@@" not in out and "Undefined" not in out and str(result["plan"]["cost"]) in out, language
        for code in warnings:
            assert f">{code}</ha-alert>" not in out, (language, code)
        for code in result["notes"]:
            assert f"- {code}\n" not in out, (language, code)
        for code in ("missing_prices", "too_few_windows", "no_drives", "start_level_stale", "no_change"):
            failed = await _render(hass, language, {"result": None, "attempt": {"status": "unavailable", "reason": code}})
            assert f": {code}</ha-alert>" not in failed, (language, code)


async def test_without_a_recorded_comparison_the_card_says_so(hass: HomeAssistant) -> None:
    attributes = _attributes()
    result = {**attributes["result"], "estimate": None, "recorded": {"sessions": 0, "kwh": 0.0, "cost": None, "solar_kwh": 0.0, "unit": None}}
    out = await _render(hass, "en", {**attributes, "result": result})
    assert "No recorded private charging in the period to compare with." in out and "Difference vs plan" not in out


async def test_a_result_saved_with_the_older_estimate_keys_still_renders_and_is_migrated_on_load(hass: HomeAssistant) -> None:
    attributes = _attributes()
    old_estimate = {"cost_without_solar": 25.0, "recorded_cost": 18.0, "solar_advantage": 7.0, "kwh": 20.0, "unit": 1.25}
    old = {**attributes, "result": {**attributes["result"], "estimate": old_estimate}}
    for language in ("da", "en"):
        out = await _render(hass, language, old)  # an unmigrated result: the line is left out, never an undefined value
        assert "Undefined" not in out and "None" not in out and str(old["result"]["plan"]["cost"]) in out, language
    engine, snap = _engine()
    engine.economy_db["what_if_no_solar"] = {**attributes["result"], "estimate": dict(old_estimate)}
    result = engine.run_job(snap, engine.load_what_if_no_solar)
    assert result.error is None, result.error
    migrated = engine.economy_db["what_if_no_solar"]["estimate"]
    assert migrated == {"cost_at_plan_price": 25.0, "recorded_cost": 18.0, "difference_vs_plan": -7.0, "kwh": 20.0, "unit": 1.25}
    assert result.states["what_if_no_solar"] == -7.0  # recorded minus the plan's price: the opposite sign of the old number
    engine.economy_db["what_if_no_solar"] = {**attributes["result"], "estimate": {"cost_without_solar": None, "recorded_cost": 18.0}}
    result = engine.run_job(snap, engine.load_what_if_no_solar)
    assert engine.economy_db["what_if_no_solar"]["estimate"] is None and result.states["what_if_no_solar"] is None


async def test_running_and_empty_states(hass: HomeAssistant) -> None:
    out = await _render(hass, "en", {"result": None, "attempt": {"status": "running", "reason": None, "at": 1.0}})
    assert "Calculating..." in out
    assert "No calculation yet" in await _render(hass, "en", {"result": None, "attempt": None})


def test_the_read_more_covers_what_the_section_shows() -> None:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    view = yaml.safe_load(render_dashboard(const.build_config({**OPTIONS, "language": "en"}), "tesla", "entry1", existing))["views"][3]
    explain = next(c["content"] for c in _cards(view) if "Takes the solar charging out completely" in c.get("content", ""))
    assert "what the solar was worth" not in explain and "value of the solar energy." not in explain
    for phrase in (
        "period", "battery level at the first plug-in", "Run what-if without solar", "nothing changes in the plan", "planned grid-only charging", "price per kWh",
        "estimate", "table per month", "price sensor history", "estimated price", "calibrates", "fewer than half", "latest connection is left out",
        "fold-out", "result sensor", "house limits", "standby", "difference vs plan", "positive number means the recorded charging cost more",
        "not the value of the solar energy", "charging times and price choices", "after the solar energy was stored as energy",
    ):
        assert phrase.lower() in explain.lower(), phrase


# --------------------------------------------------------------------------- coordinator


async def test_the_entities_exist_and_the_button_runs_the_plan_without_touching_the_other_results(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    assert hass.states.get(SENSOR) is not None and hass.states.get(BUTTON) is not None
    _seed_coordinator(coordinator)
    with patch.object(type(coordinator), "_async_what_if_start_soc", return_value={"soc": 55.0, "source": "recorded_state"}):
        await hass.services.async_call("button", "press", {"entity_id": BUTTON}, blocking=True)
        await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get(SENSOR)
    assert state.attributes["attempt"]["status"] == "ok" and state.attributes["result"]["status"] == "ok"
    assert state.attributes["result"]["plan"]["start_soc"] == 55.0
    assert hass.states.get(f"sensor.{OBJECT_PREFIX}_what_if").attributes.get("attempt") is None
    assert hass.states.get(f"sensor.{OBJECT_PREFIX}_what_if_settings").attributes.get("attempt") is None


def test_without_recorded_solar_energy_there_is_no_estimate_and_the_home_battery_is_shown_not_replaced() -> None:
    engine, snap = _engine()
    _seed(engine, public=False)
    _recorded(engine, solar=0.0)
    r = _run(engine, snap)[1].value
    assert r["recorded"]["sessions"] == 2 and r["recorded"]["solar_kwh"] == 0.0 and r["estimate"] is None
    _recorded(engine, solar=4.0)
    for entry in engine.charging_history_db.values():
        if "powerwall_kwh_of_local_energy" in entry and entry["kWh"] == 10.0:
            entry["powerwall_kwh_of_local_energy"] = 1.5
    r = _run(engine, snap)[1].value
    assert r["recorded"]["powerwall_kwh"] == 3.0 and r["estimate"] is not None


def test_sessions_before_the_solar_energy_marker_keep_their_energy_but_not_their_solar_part() -> None:
    """Sessions that started before the solar energy was stored as energy have an unusable solar part: it neither
    counts in the recorded solar energy nor opens the estimate line, but their energy and cost are still recorded."""
    engine, snap = _engine()
    _seed(engine, public=False)
    _recorded(engine)
    # between the two sessions (3 and 2 days back, 20:05): only the later one counts its solar energy
    engine.economy_db["solar_energy_since"] = _ts(DAY - datetime.timedelta(days=2), 0)
    r = _run(engine, snap)[1].value
    assert r["recorded"]["sessions"] == 2 and r["recorded"]["kwh"] == 20.0 and r["recorded"]["cost"] == 18.0
    assert r["recorded"]["solar_kwh"] == 4.0 and r["estimate"] is not None
    # after both: no usable solar energy, so no estimate line, although the energy and cost are still shown
    engine.economy_db["solar_energy_since"] = _ts(DAY, 0)
    r = _run(engine, snap)[1].value
    assert r["recorded"]["sessions"] == 2 and r["recorded"]["kwh"] == 20.0 and r["recorded"]["cost"] == 18.0
    assert r["recorded"]["solar_kwh"] == 0.0 and r["estimate"] is None


def test_the_difference_vs_plan_is_recorded_minus_the_plan_price_and_not_a_solar_value() -> None:
    """The difference vs plan is the recorded cost minus the recorded energy at the plan's price per kWh, so it follows
    the recorded cost one to one: it holds when and at which price the energy was bought, not only what the solar was
    worth (e.g. 10 of 100 kWh from solar valued 0.5 and 90 kWh bought at 2.5, 230 in all, against a plan price of 1.5
    gives +80, although the solar was worth 20)."""
    engine, snap = _engine()
    _seed(engine, public=False)
    _recorded(engine)
    r = _run(engine, snap)[1].value
    unit = r["plan_measured"]["unit"]
    estimate = r["estimate"]
    assert estimate["unit"] == unit and estimate["kwh"] == 20.0
    assert estimate["difference_vs_plan"] == pytest.approx(estimate["recorded_cost"] - estimate["cost_at_plan_price"], abs=0.011)
    # the sign follows the recorded cost: a dearer recorded charging gives a larger difference vs plan
    _recorded(engine, cost=30.0)
    dearer = _run(engine, snap)[1].value["estimate"]
    assert dearer["difference_vs_plan"] == pytest.approx(estimate["difference_vs_plan"] + 2 * (30.0 - 9.0), abs=0.02)


def test_the_plan_uses_the_prices_net_of_the_refund_like_the_recorded_cost() -> None:
    engine, snap = _engine()
    _seed(engine, public=False)
    gross = _run(engine, snap)[1].value["plan"]
    engine.price_provider.refund = 0.25
    net = _run(engine, snap)[1].value["plan"]
    assert net["unit"] == pytest.approx(gross["unit"] - 0.25, abs=0.01) and net["grid_kwh"] == gross["grid_kwh"]
    engine.price_provider.refund = 0.0


def test_the_recorded_span_starts_after_a_calibrating_first_connection() -> None:
    engine, snap = _engine()
    windows = _seed(engine, public=False)
    first_day = DAY - datetime.timedelta(days=5)
    engine.charging_history_db[datetime.datetime.combine(first_day, datetime.time(19, 5))] = {
        "kWh": 7.0, "cost": 5.0, "kWh_from_grid": 3.0, "kWh_from_local_energy": 4.0, "solar_kwh_of_local_energy": 4.0,
        "powerwall_kwh_of_local_energy": 0.0, "ended": "20:00", "charging_type": ["planned"],
    }
    assert windows
    r = _run(engine, snap, {"soc": None, "source": "unavailable"})[1].value
    assert r["calibrated_start"] and r["recorded"]["sessions"] == 0  # the session in the calibrating connection is not compared
