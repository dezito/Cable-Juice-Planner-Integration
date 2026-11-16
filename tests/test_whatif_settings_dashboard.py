"""The What-if Scenario B card rendered by Home Assistant's own template engine with real results."""
from __future__ import annotations

import copy
import functools
import re

import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.management.dashboard_build import render_dashboard

from .test_dashboard import OPTIONS, _cards, _own
from .test_economy import _engine
from .test_whatif_dashboard import assert_decimals
from .test_whatif_engine import _seed, snapshot_with
from .test_whatif_settings_engine import _run

SENSOR = "sensor.cjp_tesla_what_if_settings"
LANGUAGES = ("da", "de", "en", "en-GB", "pl", "uk")


def _view(language: str) -> tuple[dict, str]:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    text = render_dashboard(const.build_config({**OPTIONS, "language": language}), "tesla", "entry1", existing)
    assert "@@" not in text
    return yaml.safe_load(text)["views"][3], text


@functools.cache  # building the whole dashboard once per language: the tests render the card for many attributes
def _content(language: str) -> str:
    view, _ = _view(language)
    return next(c["content"] for c in _cards(view) if c.get("type") == "markdown" and "{% set s = r.settings %}" in c.get("content", ""))


def _attributes() -> dict:
    engine, snap = _engine()
    _seed(engine)
    _, run = _run(engine, snapshot_with(snap, {"what_if_min_daily_battery_level": 50.0}))
    assert run.value["status"] == "ok"
    return run.attributes["what_if_settings"]


async def _render(hass: HomeAssistant, language: str, attributes: dict) -> str:
    content = _content(language)
    hass.states.async_set(re.search(r"states\.(sensor\.\w+)\.attributes", content).group(1), "0", attributes)
    return Template(content, hass).async_render(parse_result=False, strict=True)  # like the frontend: an undefined attribute is an error


def test_the_view_has_a_scenario_b_section_with_its_entities_and_a_read_more() -> None:
    view, text = _view("en")
    entities = {e["entity"] if isinstance(e, dict) else e for c in _cards(view) if c.get("type") == "entities" for e in c["entities"]}
    for entity in (
        "number.cjp_tesla_what_if_min_daily_battery_level", "number.cjp_tesla_what_if_max_recommended_battery_level",
        "button.cjp_tesla_run_what_if_settings", "number.cjp_tesla_what_if_days", "number.cjp_tesla_what_if_start_battery_level",
    ):
        assert entity in entities, entity
    headings = [c["heading"] for c in _cards(view) if c.get("type") == "heading"]
    assert "Reserve and maximum level" in headings
    explain = next(c["content"] for c in _cards(view) if "alternative daily reserve" in c.get("content", ""))
    # the Read more covers what is shown beneath it
    for phrase in (
        # the two settings are named like the entities beneath it
        "What-if alternative daily arrival battery level", "What-if EV max recommended charging limit", "Run what-if settings", "period", "battery level at the first plug-in",
        "not CJP's planner", "uses the current settings", "hindsight", "left out", "no effect", "adjusted difference", "per month", "coverage",
        "assumptions", "only an indication", "price per kWh actually paid", "charging power", "exactly 0 %",
    ):
        assert phrase.lower() in explain.lower(), phrase
    assert "states.sensor.cjp_tesla_what_if_settings.attributes" in text


async def test_a_result_shows_both_settings_the_cost_and_the_difference(hass: HomeAssistant) -> None:
    attributes = _attributes()
    out = await _render(hass, "en", attributes)
    for expected in (
        "Latest result", "Current settings", "Alternative", "Daily arrival battery level", "EV max recommended charging limit", "Adjusted difference",
        "Per month", "Coverage",
        "A simplified model of the CJP planning rule", "not the plan CJP made at the time", "latest connection is left out",
    ):
        assert expected in out, expected
    # no note in the table of where the alternative value came from (the coverage line still says where the start level came from)
    assert not any("(entered)" in line or "(current setting)" in line for line in out.splitlines() if line.startswith("|"))
    for gone in (
        "Energy from the charger", "Charging loss", "Driving that could not be covered", "Lowest battery level", "Final battery level",
        "Homecomings below the current reserve", "Lowest homecoming level",
    ):
        assert f"| {gone}" not in out, gone
    assert "None" not in out and "Undefined" not in out and "@@" not in out
    r = attributes["result"]
    assert str(r["baseline"]["cost"]) in out and str(r["alternative"]["cost"]) in out and str(r["difference"]["adjusted_cost"]) in out
    assert r["months"][0]["month"] in out


async def test_the_table_has_the_energy_charged_by_both_settings_and_the_difference_and_an_old_result_without_it_shows_dashes(hass: HomeAssistant) -> None:
    attributes = _attributes()
    r = attributes["result"]
    row = next(line for line in (await _render(hass, "en", attributes)).splitlines() if line.startswith("| Charged (kWh) |"))
    assert r["baseline"]["kwh"] > 0 and f"{r['baseline']['kwh']:.1f} kWh" in row and f"{r['alternative']['kwh']:.1f} kWh" in row
    assert f"{r['alternative']['kwh'] - r['baseline']['kwh']:.1f} kWh" in row
    old = {**attributes, "result": {**r, **{name: {k: v for k, v in r[name].items() if k != "kwh"} for name in ("baseline", "alternative")}}}
    row = next(line for line in (await _render(hass, "en", old)).splitlines() if line.startswith("| Charged (kWh) |"))
    assert row.count("&ndash;") == 3


async def test_every_language_renders_the_card_without_missing_texts(hass: HomeAssistant) -> None:
    attributes = _attributes()
    for language in LANGUAGES:
        out = await _render(hass, language, attributes)
        assert "@@" not in out and "Undefined" not in out and str(attributes["result"]["alternative"]["cost"]) in out, language
        for code in attributes["result"]["notes"]:
            assert f"- {code}\n" not in out, (language, code)  # a missing text would show the raw code


async def test_not_comparable_shows_the_plain_differences_as_an_indication_and_why(hass: HomeAssistant) -> None:
    attributes = _attributes()
    result = copy.deepcopy(attributes["result"])
    result.update(comparable=False, comparable_reason="unmet_driving_differs", difference=None)
    out = await _render(hass, "en", {**attributes, "result": result})
    assert "only an indication" in out and "cannot cover a different amount of driving" in out and "not a saving" in out
    assert "Adjusted difference" not in out and str(result["baseline"]["cost"]) in out
    assert out.count("The two calculations do not deliver the same") == 1  # the alert only: not repeated under the month table
    # the plain differences are shown, each marked with the approximately-equal sign (the adjusted one never is)
    cost = result["alternative"]["cost"] - result["baseline"]["cost"]
    cost_row = next(line for line in out.splitlines() if line.startswith("| Charging cost"))
    assert f"&asymp; {cost:.2f}" in cost_row.split("|")[-2]
    month = result["months"][0]
    row = next(line for line in out.splitlines() if f"**{month['month']}**" in line)
    assert f"&asymp; {month['alternative_cost'] - month['baseline_cost']:.2f}" in row.split("|")[-2]
    for language in LANGUAGES:
        text = await _render(hass, language, {**attributes, "result": result})
        assert "≈" in text and "@@" not in text and "Undefined" not in text, language


async def test_running_failed_and_empty_states(hass: HomeAssistant) -> None:
    attributes = _attributes()
    running = await _render(hass, "en", {**attributes, "attempt": {"status": "running", "reason": None, "at": 1.0}})
    assert "Calculating..." in running
    failed = await _render(hass, "en", {**attributes, "attempt": {"status": "unavailable", "reason": "no_change", "at": 1.0}})
    assert "could not be completed" in failed and "Nothing to compare" in failed and "Latest result" in failed
    empty = await _render(hass, "en", {"result": None, "attempt": None})
    assert "No calculation yet" in empty and "Latest result" not in empty


async def test_every_new_reason_has_a_text_in_every_language(hass: HomeAssistant) -> None:
    for language in LANGUAGES:
        for code in ("no_change", "invalid_settings", "too_few_windows", "no_start_soc", "no_drives", "start_level_stale"):
            out = await _render(hass, language, {"result": None, "attempt": {"status": "unavailable", "reason": code}})
            assert f": {code}</ha-alert>" not in out, (language, code)


async def test_money_has_two_decimals_and_energy_one_decimal(hass: HomeAssistant) -> None:
    attributes = _attributes()  # one replay for both renders
    assert_decimals(await _render(hass, "en", attributes))
    result = copy.deepcopy(attributes["result"])
    result.update(comparable=False, comparable_reason="unmet_driving_differs", difference=None)
    assert_decimals(await _render(hass, "en", {**attributes, "result": result}))  # the indication has them too
