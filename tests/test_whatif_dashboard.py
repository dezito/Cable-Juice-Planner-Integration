"""The What-if Scenario A card rendered by Home Assistant's own template engine with real results."""
from __future__ import annotations

import copy
import re

import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.management.dashboard_build import render_dashboard

from .test_dashboard import OPTIONS, _cards, _own
from .test_economy import _engine
from .test_whatif_engine import _run, _seed


def _card(language: str) -> tuple[str, str]:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    text = render_dashboard(const.build_config({**OPTIONS, "language": language}), "tesla", "entry1", existing)
    assert "@@" not in text
    view = yaml.safe_load(text)["views"][3]
    content = next(c["content"] for c in _cards(view) if c.get("type") == "markdown" and "{% set r = a.result %}" in c.get("content", ""))
    sensor = re.search(r"states\.(sensor\.\w+)\.attributes", content).group(1)
    return content, sensor


def _result() -> dict:
    engine, snap = _engine()
    _seed(engine)
    _, run = _run(engine, snap)
    assert run.value["status"] == "ok"
    return run.attributes["what_if"]


async def _render(hass: HomeAssistant, language: str, attributes: dict) -> str:
    content, sensor = _card(language)
    hass.states.async_set(sensor, "0", attributes)
    return Template(content, hass).async_render(parse_result=False)


async def test_a_result_is_shown_with_both_columns_months_and_assumptions(hass: HomeAssistant) -> None:
    attributes = _result()
    out = await _render(hass, "da", attributes)
    for expected in (
        "Seneste resultat", "Smart", "Opladning med det samme", "Justeret forskel", "Ladeomkostning", "Mål nået", "Pr. måned",
        "Dækning", "5 / 5", "Bagklogskab", "Sol er ikke modelleret", "indtastet",
    ):
        assert expected in out, expected
    assert "None" not in out and "Undefined" not in out and "@@" not in out
    r = attributes["result"]
    assert str(r["smart"]["cost"]) in out and str(r["asap"]["cost"]) in out
    assert str(r["difference"]["adjusted_cost"]) in out
    assert r["months"][0]["month"] in out
    assert "Kun til reference" in out and "1 sessioner" in out  # CJP's own recorded cost: a labelled reference only


async def test_every_language_renders_the_card_without_missing_texts(hass: HomeAssistant) -> None:
    attributes = _result()
    for language in ("da", "de", "en", "en-GB", "pl", "uk"):
        out = await _render(hass, language, attributes)
        assert "@@" not in out and "Undefined" not in out and str(attributes["result"]["smart"]["cost"]) in out, language


async def test_not_comparable_shows_no_difference_and_why(hass: HomeAssistant) -> None:
    attributes = _result()
    result = copy.deepcopy(attributes["result"])
    result.update(comparable=False, comparable_reason="unmet_target_differs", difference=None)
    out = await _render(hass, "en", {**attributes, "result": result})
    assert "No difference is shown" in out and "misses the target by a different amount" in out
    assert "Adjusted difference" not in out
    assert str(result["smart"]["cost"]) in out


async def test_a_stopped_replay_says_where_and_why(hass: HomeAssistant) -> None:
    attributes = _result()
    result = copy.deepcopy(attributes["result"])
    result.update(status="partial", reason="missing_prices")
    result["windows"] = {"total": 9, "replayed": 5, "stopped_at": result["period"]["end"] - 86400}
    out = await _render(hass, "en", {**attributes, "result": result})
    assert "The replay stopped early at" in out and "An hour has no stored price" in out and "5 / 9" in out


async def test_running_failed_and_empty_states(hass: HomeAssistant) -> None:
    attributes = _result()
    running = await _render(hass, "en", {**attributes, "attempt": {"status": "running", "reason": None, "at": 1.0}})
    assert "Calculating..." in running
    failed = await _render(
        hass, "en", {**attributes, "attempt": {"status": "unavailable", "reason": "no_start_soc", "at": 1.0}}
    )
    assert "could not be completed" in failed and "enter it in the field above" in failed
    assert "Latest result" in failed  # the previous successful result stays visible
    empty = await _render(hass, "en", {"result": None, "attempt": None})
    assert "No calculation yet" in empty and "Latest result" not in empty
    unknown_reason = await _render(hass, "en", {"result": None, "attempt": {"status": "unavailable", "reason": "something_new"}})
    assert "something_new" in unknown_reason  # an unknown code is shown as it is, not hidden


async def test_what_if_card_assumptions_list_everything_not_modelled(hass: HomeAssistant) -> None:
    out = await _render(hass, "en", _result())
    for text in ("Hindsight", "House and site limits are not replayed", "Solar is not modelled", "Standby drain", "departure buffer"):
        assert text in out, text


async def test_every_warning_and_reason_has_a_text_in_every_language(hass: HomeAssistant) -> None:
    attributes = _result()
    warnings = ["events_inside_windows", "prices_legacy_unverified", "invalid_input_entries_left_out", "public_charge_without_drive",
                "many_connections_without_drive", "prices_dst_hour_copied", "connections_before_drive_history_left_out"]
    reasons = ["no_battery_capacity", "no_charging_power", "no_start_soc", "invalid_target", "no_windows", "invalid_window",
               "overlapping_windows", "prices_not_hourly", "invalid_event", "invalid_loss_model", "invalid_power_model",
               "energy_conservation", "missing_prices", "public_overfill", "invalid_day_selection", "interrupted", "internal_error",
               "no_drives", "no_days_selected", "start_level_stale"]
    for language in ("da", "de", "en", "en-GB", "pl", "uk"):
        result = copy.deepcopy(attributes["result"])
        result["warnings"] = warnings
        rendered = await _render(hass, language, {**attributes, "result": result})
        for code in warnings:
            assert f">{code}</ha-alert>" not in rendered, (language, code)  # a missing text would show the raw code
        for code in reasons:
            out = await _render(hass, language, {"result": None, "attempt": {"status": "unavailable", "reason": code}})
            assert f": {code}</ha-alert>" not in out, (language, code)
