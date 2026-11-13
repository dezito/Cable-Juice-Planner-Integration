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
    return Template(content, hass).async_render(parse_result=False, strict=True)  # like the frontend: an undefined attribute is an error


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


async def test_the_table_has_a_current_settings_column_and_the_settings_rows(hass: HomeAssistant) -> None:
    attributes = _result()
    r = attributes["result"]
    out = await _render(hass, "en", attributes)
    # the third column replays the current reserve and maximum; the rows show what was used and what is set now
    # the current settings come first, as the reference; smart and immediate charging each have a difference against it
    assert "| Current settings | Smart | Immediate charging | Smart vs current settings | Immediate vs current settings |" in out
    assert str(r["current"]["cost"]) in out and str(r["current"]["lowest_soc"]) in out
    same = r["current"]["same"]
    assert f"{round(same['smart_cost'] - same['current_cost'], 2)} " in out and f"{round(same['asap_cost'] - same['current_cost'], 2)} " in out
    # the same per month, and the smart - immediate difference (with the battery level adjustment) below the table
    month = r["months"][0]
    assert month["current_cost"] is not None and str(r["difference"]["adjusted_cost"]) in out
    assert f"{round(month['same_smart_cost'] - month['current_cost'], 2)} " in out
    assert "Target battery level" in out and "Smart charging days" in out and "Daily arrival battery level" in out
    assert f"{round(r['settings']['reserve'])} %" in out and "one connection less" in out
    # a saved result from before the column existed (or a replay that could not be made) shows dashes, never "None"
    old = copy.deepcopy(attributes)
    old["result"]["current"] = None
    old["result"]["settings"] = None
    out = await _render(hass, "en", old)
    assert "None" not in out and "Undefined" not in out and "one connection less" not in out and "&ndash;" in out.replace("–", "&ndash;")


async def test_the_heading_says_how_many_days_the_data_covers_not_only_the_period_asked_for(hass: HomeAssistant) -> None:
    attributes = _result()
    r = attributes["result"]
    days = round((r["coverage"]["last_window"] - r["coverage"]["first_window"]) / 86400)
    out = await _render(hass, "en", attributes)
    heading = next(line for line in out.splitlines() if line.startswith("**Latest result**"))
    # the period asked for (30 days) and, after it, the dates and days the connections used really cover
    assert f"({r['period']['days']} days)" in heading and "data used:" in heading and f"({days} days)" in heading
    assert heading.index("data used:") > heading.index(f"({r['period']['days']} days)")
    # a result from before the field existed has no data span: the period alone
    old = copy.deepcopy(attributes)
    old["result"]["coverage"].pop("first_window")
    old["result"]["coverage"].pop("last_window")
    heading = next(line for line in (await _render(hass, "en", old)).splitlines() if line.startswith("**Latest result**"))
    assert "data used:" not in heading


async def test_a_result_saved_before_the_current_settings_column_still_renders(hass: HomeAssistant) -> None:
    """The result is saved: after an update the card first shows the one from the earlier version, without the new fields
    (current, settings, derived, the month fields of the current settings). The frontend renders strictly."""
    attributes = _result()
    old = copy.deepcopy(attributes)
    for key in ("current", "current_reason", "settings", "derived"):
        del old["result"][key]
    for month in old["result"]["months"]:
        for key in ("current_cost", "same_windows", "same_smart_cost", "same_asap_cost", "derived_windows"):
            month.pop(key, None)
    for language in ("da", "en"):
        out = await _render(hass, language, old)
        assert "None" not in out and "Undefined" not in out and str(old["result"]["smart"]["cost"]) in out, language
        assert "&ndash;" in out.replace("–", "&ndash;")  # the current settings column is shown with dashes


async def test_the_month_rows_form_one_markdown_table_without_blank_lines_between_them(hass: HomeAssistant) -> None:
    attributes = _result()
    out = await _render(hass, "en", attributes)
    rows = [line for line in out.splitlines() if re.match(r"\| \*\*\d{4}-\d{2}\*\*", line)]
    assert rows and len(rows) == len(attributes["result"]["months"])
    start = out.index(rows[0])
    block = out[out.rindex("\n\n", 0, start) + 2: out.index(rows[-1]) + len(rows[-1])]
    # header, separator and every row follow each other directly (a blank line would end the table)
    assert "\n\n" not in block and block.count("\n") == len(rows) + 1
    assert all(line.count("|") == block.splitlines()[0].count("|") for line in block.splitlines())
    # the month that holds the last connection has one connection less for the current settings: marked with an asterisk
    last = next(m for m in attributes["result"]["months"] if m["same_windows"] != m["windows"])
    assert any(line.startswith(f"| **{last['month']}**") and "*" in line.split("**", 2)[2] for line in rows)


async def test_the_note_and_the_smart_minus_immediate_line_are_paragraphs_below_the_table_not_table_rows(hass: HomeAssistant) -> None:
    """Directly under the last row of a Markdown table a line is one more row: narrow, in the first column. A blank line
    ends the table, so the two lines use the whole width (a phone)."""
    out = await _render(hass, "en", _result())
    lines = out.splitlines()
    table = [i for i, line in enumerate(lines) if line.startswith("| ") and "Current settings" in line][0]
    last = max(i for i, line in enumerate(lines) if line.startswith("|") and i > table and "Final battery level" in line)
    assert lines[last + 1] == ""  # the table ends
    note = next(i for i, line in enumerate(lines) if "one connection less" in line)
    difference = next(i for i, line in enumerate(lines) if line.startswith("**Smart &minus; Immediate charging:**"))
    assert note > last and difference > note and not lines[note].startswith("|") and not lines[difference].startswith("|")
    assert lines[note - 1] == "" and lines[difference - 1] == ""


def test_the_what_if_view_has_monthly_charts_read_from_the_result_sensors() -> None:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    text = render_dashboard(const.build_config({**OPTIONS, "language": "en"}), "tesla", "entry1", existing)
    assert "@@" not in text
    view = yaml.safe_load(text)["views"][3]
    charts = [c for c in _cards(view) if c.get("type") == "custom:apexcharts-card"]
    what_if, settings, no_solar, solar = (f"sensor.cjp_tesla_what_if{suffix}" for suffix in ("", "_settings", "_no_solar", "_solar"))
    # (title, type, series entities, series names) in the order of the view: the scenarios in turn
    assert [(c["header"]["title"], c["all_series_config"]["type"], {s["entity"] for s in c["series"]}, [s["name"] for s in c["series"]]) for c in charts] == [
        ("Cost per month", "column", {what_if}, ["Current settings", "Smart", "Immediate charging"]),
        ("Difference against the current settings per month", "column", {what_if}, ["Smart vs current settings", "Immediate vs current settings"]),
        ("Cumulative saving of smart against immediate charging per month", "line", {what_if}, ["Saving"]),
        ("Current and alternative settings: cost per month", "column", {settings}, ["Current settings", "Alternative"]),
        ("Cumulative difference per month (alternative - current)", "line", {settings}, ["Difference"]),
        ("Without solar and recorded: cost per month", "column", {no_solar}, ["Current settings, from the grid (no solar)", "Recorded (with solar)"]),
        ("Price per kWh per month", "line", {no_solar}, ["Current settings, from the grid (no solar)", "Recorded (with solar)"]),
        ("Energy per month", "column", {no_solar}, ["Current settings, from the grid (no solar)", "Recorded (with solar)"]),
        ("Solar energy: grid cost against sale value per month", "column", {solar}, ["At the grid price", "Sale it replaced"]),
        ("Cumulative solar advantage (completed months)", "line", {solar}, ["Solar advantage"]),
        ("Recorded cost against the fixed price per month", "line", {"sensor.cjp_tesla_economy"}, ["Recorded cost", "Subscription"]),
    ]
    wrappers = [c for c in _cards(view) if c.get("type") == "custom:config-template-card" and c["card"].get("type") == "custom:apexcharts-card"]
    assert [w["card"] for w in wrappers] == charts
    for wrapper in wrappers:
        # the time axis follows the months of the result of the chart's own sensor, from the first month to the end of this one
        chart = wrapper["card"]
        entity = chart["series"][0]["entity"]
        script = wrapper["variables"]["span_days"]
        assert wrapper["entities"] == [entity] and {s["entity"] for s in chart["series"]} == {entity}
        assert chart["graph_span"] == '${vars.span_days + "d"}' and chart["span"] == {"end": "month"}
        assert f"states['{entity}']" in script and "(s && s.attributes) || {}" in script and "return 62" in script and "Math.max(" in script
        assert ("a.result" in script) == (entity != "sensor.cjp_tesla_economy")
        assert script.count("{") == script.count("}") and script.count("(") == script.count(")") and script.count("[") == script.count("]")
        assert "grid_options" in wrapper and "grid_options" not in chart
    for chart in charts:
        # a chart reads the saved result of its sensor and shows nothing without one; never a point without a value
        assert chart["now"] == {"show": False}
        for series in chart["series"]:
            code = series["data_generator"]
            assert "entity.attributes" in code and ("getTime()" in code or "* 1000" in code)
            assert ".filter(" in code and "Number.isFinite" in code
            assert code.count("{") == code.count("}") and code.count("(") == code.count(")") and code.count("[") == code.count("]")
    # a chart for each month field the result sensors publish
    fields = {name for c in charts for s in c["series"] for name in re.findall(r"m\.(\w+_cost)", s["data_generator"])}
    assert {"current_cost", "smart_cost", "asap_cost", "same_smart_cost", "same_asap_cost", "baseline_cost", "alternative_cost", "planned_cost", "recorded_cost"} <= fields


def test_a_chart_whose_sensor_is_missing_is_removed_with_its_wrapper() -> None:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    existing -= {"sensor.cjp_tesla_what_if_solar", "sensor.cjp_tesla_economy"}
    text = render_dashboard(const.build_config({**OPTIONS, "language": "en"}), "tesla", "entry1", existing)
    view = yaml.safe_load(text)["views"][3]
    wrappers = [c for c in _cards(view) if c.get("type") == "custom:config-template-card" and c["card"].get("type") == "custom:apexcharts-card"]
    charts = [c for c in _cards(view) if c.get("type") == "custom:apexcharts-card"]
    # no wrapper is left without its chart, no chart without its wrapper, and none reads a sensor that does not exist
    assert wrappers and [w["card"] for w in wrappers] == charts
    assert not {w["entities"][0] for w in wrappers} & {"sensor.cjp_tesla_what_if_solar", "sensor.cjp_tesla_economy"}
    assert "what_if_solar" not in "".join(w["variables"]["span_days"] for w in wrappers)


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


async def test_a_saved_result_from_when_public_charges_were_replayed_still_renders(hass: HomeAssistant) -> None:
    # public charges are no replay events any more; a stored result may still hold the old stop reason and public_kwh
    attributes = _result()
    result = copy.deepcopy(attributes["result"])
    result.update(status="partial", reason="public_overfill")
    result["windows"] = {"total": 9, "replayed": 5, "stopped_at": result["period"]["end"] - 86400}
    result["smart"]["public_kwh"] = 6.0
    result["asap"]["public_kwh"] = 6.0
    for language in ("da", "en"):
        out = await _render(hass, language, {**attributes, "result": result})
        assert "public_overfill" in out and "5 / 9" in out and "Undefined" not in out, language  # an unknown code is shown as it is


async def test_running_failed_and_empty_states(hass: HomeAssistant) -> None:
    attributes = _result()
    running = await _render(hass, "en", {**attributes, "attempt": {"status": "running", "reason": None, "at": 1.0}})
    assert "Calculating..." in running
    failed = await _render(
        hass, "en", {**attributes, "attempt": {"status": "unavailable", "reason": "no_start_soc", "at": 1.0}}
    )
    assert "could not be completed" in failed and "must be between 0 and 100 %" in failed
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
               "energy_conservation", "missing_prices", "invalid_day_selection", "interrupted", "internal_error",
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
