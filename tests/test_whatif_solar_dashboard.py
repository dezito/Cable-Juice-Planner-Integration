"""The What-if Scenario D card rendered by Home Assistant's own template engine."""
from __future__ import annotations

import re

import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.management.dashboard_build import render_dashboard

from .test_dashboard import OPTIONS, _cards, _own
from .test_whatif_solar import compare, hour, session

LANGUAGES = ("da", "de", "en", "en-GB", "pl", "uk")
SOLAR_CARD = "{% set left = {"


def _view(language: str) -> dict:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    text = render_dashboard(const.build_config({**OPTIONS, "language": language}), "tesla", "entry1", existing)
    assert "@@" not in text
    return yaml.safe_load(text)["views"][3]


def _content(language: str) -> str:
    return next(c["content"] for c in _cards(_view(language)) if c.get("type") == "markdown" and SOLAR_CARD in c.get("content", ""))


def _result(**kwargs) -> dict:
    months = {hour(10): "2026-01", hour(30): "2026-02"}
    sessions = [session(hour(10), hour(11), 2.0, powerwall=1.5), session(hour(30), hour(31), 3.0), {**session(hour(40), hour(41), 1.0), "public": True}]
    result = compare(sessions, month_of=lambda ts: months.get(ts, "2026-02"), current="2026-02", since=hour(-5), **kwargs)
    return {**result, "computed_at": hour(50), "sell_prices_since": hour(0)}


async def _render(hass: HomeAssistant, language: str, result: dict | None) -> str:
    content = _content(language)
    hass.states.async_set(re.search(r"states\.(sensor\.\w+)\.attributes", content).group(1), "0", {"result": result})
    return Template(content, hass).async_render(parse_result=False, strict=True)  # like the frontend: an undefined attribute is an error


def test_the_view_has_a_scenario_d_section_that_asks_for_nothing_and_reads_its_own_sensor() -> None:
    view = _view("en")
    headings = [c["heading"] for c in _cards(view) if c.get("type") == "heading"]
    assert "EV solar energy replaced by grid energy" in headings
    content = _content("en")
    assert "what_if_solar.attributes" in content
    explain = next(c["content"] for c in _cards(view) if "the same kWh are priced twice" in c.get("content", ""))
    # the Read more covers every element of the card
    for phrase in (
        "never changes the plan", "estimate", "refund", "never treated as free", "positive means", "per month", "completed months",
        "not added", "evenly", "recorded cost", "left out", "public charging", "still charging", "unreadable", "no solar energy",
        "recorded before", "missing stored buy or sell price", "home battery", "since when", "no price is ever filled in",
        "existing history gives no result", "not physically no solar production", "replanned", "no whole-house result",
        "priced once", "not priced in time", "fold-out with the assumptions", "marked incomplete",
        "no recorded session covers", "upper bound", "asterisk", "recorded sessions and what was estimated", "left out for a missing price",
        "sessions, charging hours, or both",
    ):
        assert phrase.lower() in explain.lower(), phrase


async def test_a_result_shows_months_the_total_what_was_left_out_and_the_assumptions(hass: HomeAssistant) -> None:
    result = _result()
    out = await _render(hass, "en", result)
    for expected in (
        "Month", "Sessions", "Solar energy", "At the grid price", "Sale it replaced", "Solar advantage", "2026-01", "2026-02",
        "Completed months (1)", "The current month is incomplete", "Left out", "public charging (1)", "Home battery energy in these sessions",
        "1.5 kWh", "Solar energy usable since", "Sell prices stored since", "Assumptions and what is not included",
        "Charging replanned without solar is not modelled", "No whole-house result is claimed",
    ):
        assert expected in out, expected
    assert "None" not in out and "Undefined" not in out and "@@" not in out
    january = result["months"][0]
    assert str(january["advantage"]) in out and str(result["total"]["advantage"]) in out
    # the current month is shown but its advantage is not added
    assert result["total"]["advantage"] == january["advantage"]


async def test_every_language_renders_the_card_without_missing_texts(hass: HomeAssistant) -> None:
    result = _result()
    for language in LANGUAGES:
        out = await _render(hass, language, result)
        assert "@@" not in out and "Undefined" not in out and str(result["total"]["advantage"]) in out, language
        for code in result["notes"]:
            assert f"- {code}\n" not in out, (language, code)
        for code in result["left_out"]:
            assert f"{code} (" not in out, (language, code)


async def test_every_reason_has_a_text_in_every_language_and_an_unknown_one_is_shown_as_it_is(hass: HomeAssistant) -> None:
    for language in LANGUAGES:
        for code in ("no_solar_configured", "no_price_provider", "waiting_for_new_sessions", "no_sell_prices", "no_buy_prices", "no_solar_sessions", "internal_error"):
            out = await _render(hass, language, {"status": "unavailable", "reason": code})
            assert code not in out, (language, code)
    out = await _render(hass, "en", {"status": "unavailable", "reason": "something_new"})
    assert "something_new" in out


async def test_waiting_for_new_sessions_says_so_and_lists_what_was_left_out(hass: HomeAssistant) -> None:
    result = compare([session(hour(10), hour(11), 1.0)], since=hour(20))
    assert result["reason"] == "waiting_for_new_sessions"
    out = await _render(hass, "en", {**result, "solar_energy_since": hour(20)})
    assert "this fills as new solar sessions are recorded" in out
    assert "recorded before solar energy was stored as energy (1)" in out
    assert "Month" not in out and "Completed months" not in out  # nothing is invented while there is no result


def _mixed_result() -> dict:
    """A ledger session and two charging hours that no session covers, one of them without a buy price."""
    from custom_components.cable_juice_planner.engine.whatif.solar import solar_from_power_hours

    entry = {"start": hour(10), "end": hour(11), "month": "2026-01", "outcome": "counted", "solar_kwh": 1.5, "grid_cost": 1.5, "sell_value": 0.6, "powerwall_kwh": 0.0}
    power = {hour(10): [6000.0, 3000.0, 2000.0], hour(20): [6000.0, 3000.0, 2000.0], hour(21): [6000.0, 3000.0, 2000.0]}
    result = solar_from_power_hours(
        power, {hour(10): 1.0, hour(20): 1.0}, dict.fromkeys(power, 0.4), {}, -1.0, 0.0, hour(100), "2026-02", lambda ts: "2026-01", [entry],
    )
    assert result["source"] == "mixed" and result["upper_bound"]
    return {**result, "computed_at": hour(50), "sell_prices_since": hour(0), "solar_energy_since": hour(-5)}


async def test_estimated_hours_are_marked_as_an_upper_bound_with_the_split_and_the_hours_left_out(hass: HomeAssistant) -> None:
    result = _mixed_result()
    out = await _render(hass, "en", result)
    for expected in (
        "Sessions / Charging hours", "| 1 / 1 |", "3.5 kWh&#42;", "&#42; Includes charging hours that no recorded session covers",
        "upper bound", "From recorded sessions:", "Sessions 1", "1.5 kWh", "Estimated from hourly averages (upper bound):",
        "Charging hours 1", "2.0 kWh", "Charging hours left out:", "missing buy price (1)",
        "Hours that no recorded session covers are estimated from the hourly averages",
    ):
        assert expected in out, expected
    assert "covered_by_sessions" not in out and "None" not in out and "Undefined" not in out
    assert str(result["total"]["advantage"]) in out
    # only the sessions: no mark, no warning
    plain = await _render(hass, "en", _result())
    assert "&#42;" not in plain and "upper bound" not in plain


async def test_every_language_marks_the_estimated_hours_without_missing_texts(hass: HomeAssistant) -> None:
    result = _mixed_result()
    for language in LANGUAGES:
        out = await _render(hass, language, result)
        assert "@@" not in out and "Undefined" not in out and "&#42;" in out, language
        assert "hourly_solar_upper_bound" not in out and "missing_buy_price (" not in out, language


async def test_no_result_at_all_shows_that_nothing_has_been_recorded_yet(hass: HomeAssistant) -> None:
    out = await _render(hass, "en", None)
    assert "No finished charging session with solar energy has been recorded" in out and "Press" not in out
