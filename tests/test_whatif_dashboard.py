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


async def test_a_result_is_shown_with_the_three_columns_months_and_assumptions(hass: HomeAssistant) -> None:
    attributes = _result()
    out = await _render(hass, "da", attributes)
    for expected in (
        "Seneste resultat", "CJP (registreret)", "Smart", "Opladning med det samme", "Justeret forskel", "Ladeomkostning", "Mål nået",
        "Pr. måned", "Dækning", "5 / 5", "bagklogskab", "CJP-prisen er det, CJP har bogført", "indtastet", "For få dage", "Energi",
        "batteriniveau ved sidste frakobling",
    ):
        assert expected in out, expected
    assert "None" not in out and "Undefined" not in out and "@@" not in out
    r = attributes["result"]
    assert all(str(r[name]["cost"]) in out for name in ("cjp", "smart", "asap"))
    assert f'{0 - r["difference"]["adjusted_cost"]:.2f}' in out and r["months"][0]["month"] in out
    assert "Kun til reference" not in out and "tilslutninger, " not in out  # the booked cost IS the CJP column now: no separate reference line


async def test_the_table_has_the_three_calculations_the_differences_and_what_targets_met_means(hass: HomeAssistant) -> None:
    attributes = _result()
    r = attributes["result"]
    out = await _render(hass, "en", attributes)
    assert "| CJP (recorded) | Smart | Charging at once |" in out
    for name in ("cjp", "smart", "asap"):
        assert f"{r[name]['cost']} " in out and f"{r[name]['final_soc']} %" in out
    assert "Maximum recommended level" not in out and "Level charged to" in out and "Days with Smart" in out
    # what "targets met" means differs, and the card says so right under the table
    assert "Targets met: for CJP the car came home with at least the daily minimum level" in out
    # the three differences, each with the battery level adjustment
    for label in ("Charging at once &minus; CJP (recorded)", "Smart &minus; CJP (recorded)", "Smart &minus; Charging at once"):
        assert f"**{label}:**" in out, label
    assert f'{0 - r["differences"]["cjp_asap"]["adjusted_cost"]:.2f}' in out and f'{0 - r["differences"]["cjp_smart"]["adjusted_cost"]:.2f}' in out  # money with two decimals, the other minus CJP
    # the energy balance and the levels at both ends
    assert "**Energy:** CJP charged 100.0 %" in out and "battery level at the last unplug 70 %" in out
    # no "None" and no raw "Undefined": the frontend renders strictly


async def test_the_price_per_kwh_is_a_row_and_cjps_kwh_at_the_price_of_the_others_is_a_line(hass: HomeAssistant) -> None:
    attributes = _result()
    unit = attributes["result"]["unit_prices"]
    out = await _render(hass, "en", attributes)
    assert "| Price per kWh | " in out and f"{unit['cjp']:.2f} $/kWh" in out
    row = next(item for item in out.splitlines() if item.startswith("| Price per kWh |"))
    assert row.endswith(f"| {unit['smart'] - unit['cjp']:.2f} $/kWh | {unit['asap'] - unit['cjp']:.2f} $/kWh |")  # each minus CJP: positive, CJP bought cheaper
    line = next(item for item in out.splitlines() if item.startswith("**CJP kWh at the price per kWh of Smart and charging at once"))
    assert f"({unit['cjp_kwh']:.1f} kWh)" in line and f"CJP booked {unit['cjp_cost']:.2f}" in line  # kWh with one decimal
    assert f"Smart vs CJP {0 - unit['cjp_vs_smart']:.2f} $" in line and f"Charging at once vs CJP {0 - unit['cjp_vs_asap']:.2f} $" in line
    assert "The price per kWh says when the energy was bought, not how much" in out
    # a calculation that bought nothing has no price: dashes, never "None"
    none = copy.deepcopy(attributes)
    none["result"]["unit_prices"].update(cjp=None, smart=None, smart_at_cjp_kwh=None, cjp_vs_smart=None)
    out = await _render(hass, "en", none)
    assert "None" not in out and "Undefined" not in out


async def test_the_estimate_per_year_is_labelled_an_estimate_with_its_basis_and_refused_when_too_short(hass: HomeAssistant) -> None:
    attributes = _result()  # five connections: far too few days for a year
    out = await _render(hass, "en", attributes)
    assert "Too few days of connections to estimate a year" in out and "ESTIMATE" not in out
    estimated = copy.deepcopy(attributes)
    estimated["result"]["annual"] = {
        "available": True, "method": "estimated", "basis_days": 90.0, "scale": 4.056, "months_covered": 3,
        "cjp_vs_asap": -490.0, "cjp_vs_smart": -198.0, "smart_vs_asap": -292.0,
    }
    out = await _render(hass, "en", estimated)
    # only the adjusted differences are shown per year: the costs of the three end at different battery levels
    lines = out.splitlines()
    row = next(line for line in lines if line.startswith("| Per year (ESTIMATE)"))
    assert lines[lines.index(row) - 1].startswith("| ")  # a row of the table, not loose text after a blank line
    assert row.endswith("| 198.00 $ | 490.00 $ |")  # two decimals for money, in the columns of the two differences: the other minus CJP, positive when CJP was cheaper
    assert "This is an ESTIMATE" in out and "90.0 days, 3 months" in out  # the basis and the caveat are right there
    measured = copy.deepcopy(estimated)
    measured["result"]["annual"]["method"] = "measured"
    out = await _render(hass, "en", measured)
    assert "| Per year (a year or more of data) |" in out and "This is an ESTIMATE" not in out
    for language in ("da", "de", "pl", "uk", "en-GB"):  # the word that says it is an estimate is in every language
        rendered = await _render(hass, language, estimated)
        assert "ESTIMATE" in rendered or "ESTIMAT" in rendered or "SCHÄTZUNG" in rendered or "SZACUNEK" in rendered or "ОЦІНКА" in rendered, language


async def test_without_the_end_level_the_adjusted_difference_is_dashes_and_the_card_says_why(hass: HomeAssistant) -> None:
    attributes = _result()
    r = attributes["result"]
    for key in ("cjp_asap", "cjp_smart", "smart_asap"):
        r["differences"][key]["adjusted_cost"] = None
        r["differences"][key]["terminal_adjustment"] = None
    r["difference"] = r["differences"]["cjp_asap"]
    r["warnings"] = [*r["warnings"], "energy_not_balanced"]
    r["energy"].update(gap_percent=None, end_soc=None)
    r["provenance"]["end_soc"] = "unavailable"
    out = await _render(hass, "en", attributes)
    assert "None" not in out and "Undefined" not in out and "&ndash;" in out.replace("–", "&ndash;")
    assert "could not be read, so the three are not known to deliver the same energy" in out and "battery level at the last unplug unknown" in out


async def test_the_heading_says_how_many_days_the_data_covers_not_only_the_period_asked_for(hass: HomeAssistant) -> None:
    attributes = _result()
    r = attributes["result"]
    days = round((r["coverage"]["last_window"] - r["coverage"]["first_window"]) / 86400)
    out = await _render(hass, "en", attributes)
    heading = next(line for line in out.splitlines() if line.startswith("**Latest result**"))
    # the period asked for (30 days) and, after it, the dates and days the connections used really cover
    assert f"({r['period']['days']} days)" in heading and "data used:" in heading and f"({days} days)" in heading
    assert heading.index("data used:") > heading.index(f"({r['period']['days']} days)")
    old = copy.deepcopy(attributes)
    old["result"]["coverage"].pop("first_window")
    old["result"]["coverage"].pop("last_window")
    heading = next(line for line in (await _render(hass, "en", old)).splitlines() if line.startswith("**Latest result**"))
    assert "data used:" not in heading


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
    assert "Smart vs CJP" in block and "Charging at once vs CJP" in block


async def test_the_notes_and_the_differences_are_paragraphs_below_the_table_not_table_rows(hass: HomeAssistant) -> None:
    """Directly under the last row of a Markdown table a line is one more row: narrow, in the first column. A blank line
    ends the table, so the lines use the whole width (a phone)."""
    out = await _render(hass, "en", _result())
    lines = out.splitlines()
    table = [i for i, line in enumerate(lines) if line.startswith("| ") and "CJP (recorded)" in line][0]
    last = max(i for i, line in enumerate(lines) if line.startswith("|") and i > table and "Final battery level" in line)
    differences = [i for i, line in enumerate(lines) if line.startswith("**Charging at once &minus; CJP (recorded):**")]
    assert differences and differences[0] > last
    note = next(i for i, line in enumerate(lines) if line.startswith("Targets met:"))
    assert note > last and not lines[note].startswith("|") and lines[note - 1] == ""
    for index in (i for i, line in enumerate(lines) if "&minus;" in line and line.startswith("**")):
        assert lines[index - 1] == "" and not lines[index].startswith("|")


def test_the_charts_of_the_differences_are_hidden_when_the_three_are_not_comparable() -> None:
    """Not comparable: the sensor state is unknown and there is nothing adjusted to draw, so the two charts are not shown at all."""
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    view = yaml.safe_load(render_dashboard(const.build_config({**OPTIONS, "language": "en"}), "tesla", "entry1", existing))["views"][3]
    wrapped = {
        c["card"]["card"]["header"]["title"]: c["conditions"]
        for c in _cards(view)
        if c.get("type") == "conditional" and c["card"].get("type") == "custom:config-template-card"
    }
    assert sorted(wrapped) == [
        "Cumulative saving of CJP per month", "Difference per month: Smart and charging at once against CJP (positive: CJP cheaper)"
    ]
    for conditions in wrapped.values():
        assert conditions == [
            {"condition": "state", "entity": "sensor.cjp_tesla_what_if", "state_not": "unknown"},
            {"condition": "state", "entity": "sensor.cjp_tesla_what_if", "state_not": "unavailable"},
        ]


def test_the_what_if_view_has_monthly_charts_read_from_the_result_sensors() -> None:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    text = render_dashboard(const.build_config({**OPTIONS, "language": "en"}), "tesla", "entry1", existing)
    assert "@@" not in text
    view = yaml.safe_load(text)["views"][3]
    charts = [c for c in _cards(view) if c.get("type") == "custom:apexcharts-card"]
    what_if, settings, no_solar, solar = (f"sensor.cjp_tesla_what_if{suffix}" for suffix in ("", "_settings", "_no_solar", "_solar"))
    # (title, type, series entities, series names) in the order of the view: the scenarios in turn
    assert [(c["header"]["title"], c["all_series_config"]["type"], {s["entity"] for s in c["series"]}, [s["name"] for s in c["series"]]) for c in charts] == [
        ("Cost per month", "column", {what_if}, ["CJP (recorded)", "Smart", "Charging at once"]),
        ("Difference per month: Smart and charging at once against CJP (positive: CJP cheaper)", "column", {what_if}, ["Smart vs CJP", "Charging at once vs CJP"]),
        ("Cumulative saving of CJP per month", "line", {what_if}, ["Saving of CJP vs Charging at once", "Saving of CJP vs Smart"]),
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
    hidden_by = {id(c["card"]): c for c in _cards(view) if c.get("type") == "conditional"}  # the charts that can be hidden
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
        assert ("grid_options" in wrapper or "grid_options" in hidden_by[id(wrapper)]) and "grid_options" not in chart
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
    assert {"cjp_cost", "smart_cost", "asap_cost", "baseline_cost", "alternative_cost", "planned_cost", "recorded_cost"} <= fields


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
    result.update(comparable=False, comparable_reason="unmet_driving_differs", difference=None, differences=None, annual=None)
    result["months"][0].update(cjp_vs_smart_unit=1.234, cjp_vs_asap_unit=None)
    out = await _render(hass, "en", {**attributes, "result": result})
    month_row = next(line for line in out.splitlines() if line.startswith(f"| **{result['months'][0]['month']}** |"))
    assert "| &asymp; -1.23 $ | &ndash; |" in month_row  # per month too: the indication marked, and a dash where there is none
    assert out.count("The three calculations do not deliver the same, so the cost differences marked ≈ are only an indication") == 2  # the warning is under the month table as well
    assert "*The three calculations do not deliver the same" in out
    assert "The three calculations do not deliver the same, so the cost differences marked ≈ are only an indication" in out
    assert "different amount of driving" in out  # and why, right there
    assert "The ≈ values are the CJP kWh at the price per kWh of Smart and of charging at once" in out
    assert "CJP kWh at the price per kWh of Smart and charging at once" in out
    unit = result["unit_prices"]
    lines = out.splitlines()
    cost = next(line for line in lines if line.startswith("| Charging cost |"))
    # the cost differences are the CJP kWh at the price per kWh of each, against what CJP paid: marked as an indication only
    assert cost.endswith(f"| &asymp; {0 - unit['cjp_vs_smart']:.2f} $ | &asymp; {0 - unit['cjp_vs_asap']:.2f} $ |")
    grid = next(line for line in lines if line.startswith("| Energy from the charger |"))
    assert grid.endswith(f"| {result['smart']['grid_kwh'] - result['cjp']['grid_kwh']:.1f} kWh | {result['asap']['grid_kwh'] - result['cjp']['grid_kwh']:.1f} kWh |")
    assert "| Adjusted difference |" not in out and "Per year" not in out  # no adjusted difference and no year without a comparison
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
    assert "Calculating..." in running and "connections</ha-alert>" not in running  # no progress is known yet
    progress = await _render(hass, "en", {**attributes, "attempt": {"status": "running", "reason": None, "at": 1.0, "progress": {"done": 3, "total": 9}}})
    assert "Calculating... &middot; 3 / 9 connections" in progress
    stopped = await _render(hass, "en", {**attributes, "attempt": {"status": "unavailable", "reason": "stopped", "at": 1.0}})
    assert "The calculation was stopped" in stopped and "Latest result" in stopped  # the previous result stays visible
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
    for text in (
        "CJP column: what CJP actually charged", "The CJP cost is what CJP booked", "Smart charges in the cheapest hours before the planned departure",
        "Smart knows every price of the connection in advance (hindsight)", "added as driving in all three",
        "House and site limits are not replayed",
    ):
        assert text in out, text
    assert "planner" not in out.split("<details>")[-1]  # the planner replay is not part of this card


async def test_every_warning_and_reason_has_a_text_in_every_language(hass: HomeAssistant) -> None:
    attributes = _result()
    warnings = ["events_inside_windows", "prices_legacy_unverified", "invalid_input_entries_left_out", "public_charge_without_drive",
                "many_connections_without_drive", "prices_dst_hour_copied", "connections_before_drive_history_left_out",
                "recorded_solar_share", "recorded_days_split", "recorded_sessions_outside_connections", "windows_before_recorded_gap_left_out",
                "recorded_path_short", "recorded_prices_estimated_lean_cjp", "connections_estimated_from_battery_statistics",
                "connections_modelled_from_work_plan", "older_connections_not_validated", "work_plan_model_not_validated",
                "estimated_connections_without_prices_left_out", "start_level_unknown_calibrated"]
    reasons = ["no_battery_capacity", "no_charging_power", "no_start_soc", "invalid_target", "no_windows", "invalid_window",
               "overlapping_windows", "prices_not_hourly", "invalid_event", "invalid_loss_model", "invalid_power_model",
               "energy_conservation", "missing_prices", "invalid_day_selection", "interrupted", "internal_error",
               "no_drives", "no_days_selected", "start_level_stale", "stopped"]
    for language in ("da", "de", "en", "en-GB", "pl", "uk"):
        result = copy.deepcopy(attributes["result"])
        result["warnings"] = warnings
        rendered = await _render(hass, language, {**attributes, "result": result})
        for code in warnings:
            assert f">{code}</ha-alert>" not in rendered, (language, code)  # a missing text would show the raw code
        for code in reasons:
            out = await _render(hass, language, {"result": None, "attempt": {"status": "unavailable", "reason": code}})
            assert f": {code}</ha-alert>" not in out, (language, code)


def _estimated(attributes: dict, **coverage) -> dict:
    result = copy.deepcopy(attributes["result"])
    result["coverage"].update({
        "recorded_estimated_windows": 3, "recorded_measured_from": 1699984800.0, "recorded_estimates": "used",
        "recorded_reason": None, "recorded_stop_at": None, **coverage,
    })
    result["months"][0]["estimated_connections"] = 3
    result["derived"] = {
        "windows": 3, "modelled": 1, "first": 1690048800.0, "last": 1699034400.0, "cjp_cost": 11.5, "smart_cost": 14.25,
        "asap_cost": 20.75, "cjp_kwh": 40.0, "smart_kwh": 41.0, "asap_kwh": 42.0,
    }
    return {**attributes, "result": result}


async def test_estimated_connections_are_marked_with_their_count_the_date_they_stop_and_the_reason_they_stop(hass: HomeAssistant) -> None:
    attributes = _estimated(
        _result(), recorded_reason="before_history", recorded_stop_at=1689012000.0, recorded_left_out=4,
    )
    out = await _render(hass, "en", attributes)
    assert "ESTIMATE: part of the period is connections that were not measured but estimated" in out
    assert "(3 / 5 connections &middot; measured from 2023-11-14)" in out
    assert "The comparison stops here: the charging history of CJP does not reach further back (2023-07-10)" in out
    # the months and the estimated part on their own
    assert "| 5 (3 ESTIMATE) |" in out
    assert "Of this, estimated (battery level statistics or the work plan) (**ESTIMATE**): 3 connections (1 of which modelled from the work plan), 2023-07-22 &ndash; 2023-11-03" in out
    assert re.search(r"CJP \(recorded\) 11\.50 \S+ \(40\.0 kWh\) &middot; Smart 14\.25 \S+ &middot; Charging at once 20\.75 ", out)
    assert "None" not in out and "Undefined" not in out and "@@" not in out
    # without any estimated connection none of it is shown
    plain = await _render(hass, "en", _result())
    assert "ESTIMATE: part of the period" not in plain and "The comparison stops here" not in plain and "ESTIMATE)" not in plain


async def test_estimates_that_are_not_used_say_why(hass: HomeAssistant) -> None:
    base = _result()
    for code, text in (
        ("not_all_measured", "Older estimated connections are not added: a measured connection could not be read"),
        ("no_record", "The charging history of CJP does not reach the estimated connections"),
        ("outside_charging", "The estimated connections are not used: much of what CJP charged lies outside them"),
        ("not_stable", "could not be fitted to the charging history and are not used"),
        ("no_prices", "The estimated connections have no stored prices"),
    ):
        attributes = _estimated(base, recorded_estimates=code, recorded_estimated_windows=0)
        attributes["result"]["derived"] = None
        out = await _render(hass, "en", attributes)
        assert text in out, code
        assert "ESTIMATE: part of the period" not in out
    # nothing could be estimated: the reasons of both methods
    attributes = _estimated(
        base, recorded_estimates="none_available", recorded_estimated_windows=0, derived_reason="no_statistics", modelled_reason="no_plan",
    )
    attributes["result"]["derived"] = None
    out = await _render(hass, "en", attributes)
    assert "No older connections could be estimated (Battery level statistics: the battery level statistics have no data; Work plan: no work plan)" in out
    # one reason only
    attributes = _estimated(base, recorded_estimates="none_available", recorded_estimated_windows=0, modelled_reason="not_validated")
    attributes["result"]["derived"] = None
    assert "No older connections could be estimated (Work plan: the method did not find the measured connections well enough)" in await _render(hass, "en", attributes)


async def test_every_text_of_the_estimates_exists_in_every_language(hass: HomeAssistant) -> None:
    base = _result()
    stops = ("before_history", "invalid_history", "not_settled")
    notes = ("not_all_measured", "none_available", "no_record", "outside_charging", "not_stable", "no_prices")
    reasons = ("no_statistics", "too_few_measured", "not_validated", "no_older_connections", "anchor_mismatch", "no_plan", "no_workdays",
               "too_few_drives", "no_modelled_connections")
    for language in ("da", "de", "en", "en-GB", "pl", "uk"):
        for stop in stops:
            out = await _render(hass, language, _estimated(base, recorded_reason=stop, recorded_stop_at=1689012000.0))
            assert f"{stop} (" not in out, (language, stop)  # a missing text would show the raw code
        for code in notes:
            attributes = _estimated(base, recorded_estimates=code, recorded_estimated_windows=0, derived_reason=reasons[0], modelled_reason=reasons[5])
            attributes["result"]["derived"] = None
            out = await _render(hass, language, attributes)
            assert "@@" not in out and "Undefined" not in out, (language, code)
        for reason in reasons:
            attributes = _estimated(base, recorded_estimates="none_available", recorded_estimated_windows=0, derived_reason=reason, modelled_reason=reason)
            attributes["result"]["derived"] = None
            out = await _render(hass, language, attributes)
            assert f": {reason}" not in out and f"{reason})" not in out, (language, reason)
