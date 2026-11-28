"""The solar forecast: which sources exist and are confirmed, the page that asks for it, and the facts the dashboard shows."""
from __future__ import annotations

import datetime
import json
import math
import pathlib
import re
import urllib.parse
import zoneinfo

import pytest
import yaml

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.template import Template

from custom_components.cable_juice_planner.const import build_config, default_options
from custom_components.cable_juice_planner.engine.state import Snapshot
from custom_components.cable_juice_planner.flow.pages import SOLAR_PAGES
from custom_components.cable_juice_planner.management.dashboard_build import render_dashboard
from custom_components.cable_juice_planner.utils.solar_sources import CONFIRMED_SOLAR_SOURCES, SOLAR_SOURCE_NAMES, SOLAR_SOURCES, solar_days

from custom_components.cable_juice_planner.engine.engine import CJPEngine

from .conftest import form_defaults
from .test_dashboard import _own
from .test_engine import OPTIONS, TEXTS

TZ = zoneinfo.ZoneInfo("Europe/Copenhagen")
TODAY = datetime.date(2026, 6, 10)
PACKAGE = pathlib.Path(__file__).parents[1] / "custom_components" / "cable_juice_planner"
SOLCAST = "sensor.solcast_pv_forecast_forecast_today"
FORECAST_SOLAR = "sensor.energy_production_today"
UNKNOWN = "sensor.restored_forecast"


def _row(day: int, hour: int, value) -> dict:
    return {"period_start": datetime.datetime.combine(TODAY + datetime.timedelta(days=day), datetime.time(hour), TZ), "pv_estimate": value}


def test_only_solcast_is_confirmed_and_every_source_has_a_name() -> None:
    assert SOLAR_SOURCES == ("solcast_solar", "forecast_solar", "helios_forecast")
    assert CONFIRMED_SOLAR_SOURCES == {"solcast_solar"}  # by hand only (user decision)
    assert set(SOLAR_SOURCE_NAMES) == set(SOLAR_SOURCES)


def test_the_facts_of_the_forecast_count_only_usable_values_and_never_turn_a_missing_day_into_zero() -> None:
    rows = {
        "a": [
            _row(-1, 12, 5.0),  # the past
            _row(0, 10, 1.0), _row(0, 11, 0.0), _row(0, 12, math.nan), _row(0, 13, -1.0), _row(0, 14, True), _row(0, 15, "2"),
            {"pv_estimate": 3.0}, {"period_start": "no time", "pv_estimate": 3.0},
            {"period_start": "2026-06-10T14:00:00+00:00", "pv_estimate": 2.0},  # 16:00 here
        ],
        "b": [_row(0, 10, 9.0), _row(2, 10, 1.25), _row(40, 10, 1.0)],  # the first row of an hour counts, a stray date makes no list
    }
    assert solar_days(rows, TODAY, TZ) == [
        {"date": "2026-06-10", "hours": 3, "kwh": 3.0},
        {"date": "2026-06-11", "hours": 0, "kwh": None},
        {"date": "2026-06-12", "hours": 1, "kwh": 1.2},
    ]
    assert solar_days({}, TODAY, TZ) == [] and solar_days(None, TODAY, TZ) == []
    assert solar_days({"a": [_row(-1, 12, 5.0)]}, TODAY, TZ) == []


def _register(hass: HomeAssistant, entity_id: str, platform: str) -> None:
    er.async_get(hass).async_get_or_create("sensor", platform, entity_id, suggested_object_id=entity_id.split(".", 1)[1])


async def _solar_pages(hass: HomeAssistant, entry, forecast: str):
    """The options menu's solar pages, with the stored forecast sensor."""
    hass.config_entries.async_update_entry(entry, options={**entry.options, "forecast_entity_id": forecast, "production_entity_id": "sensor.pv"})
    await hass.async_block_till_done(wait_background_tasks=True)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "menu_solar"})
    assert result["step_id"] == "solar_forecast_source", result
    return result


def _field(result, name: str):
    return next(((key, value) for key, value in result["data_schema"].schema.items() if str(key) == name), None)


def _integrations(picker) -> list[str]:
    filters = picker.config.get("filter") or []
    return [item.get("integration") for item in ([filters] if isinstance(filters, dict) else filters)]


def _solar_input(result, **changed) -> dict:
    values = {key: value for key, value in form_defaults(result).items()}
    for key, value in changed.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    return values


async def test_the_page_lists_the_sources_with_the_unconfirmed_ones_marked_and_no_forecast_leaves_the_sensor_out(hass: HomeAssistant, loaded_entry) -> None:
    result = await _solar_pages(hass, loaded_entry, "")
    key, listed = _field(result, "solar_forecast_source")
    assert listed.config["options"] == ["none", "solcast_solar", "forecast_solar_unconfirmed", "helios_forecast_unconfirmed"]
    assert key.default() == "none" and listed.config["translation_key"] == "solar_forecast_sources"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"solar_forecast_source": "none"})
    assert result["step_id"] == "solar" and _field(result, "forecast_entity_id") is None


async def test_the_sensor_picker_offers_the_chosen_source_and_another_source_clears_the_old_sensor(hass: HomeAssistant, loaded_entry) -> None:
    _register(hass, FORECAST_SOLAR, "forecast_solar")
    _register(hass, SOLCAST, "solcast_solar")
    options = hass.config_entries.options
    result = await _solar_pages(hass, loaded_entry, FORECAST_SOLAR)
    assert _field(result, "solar_forecast_source")[0].default() == "forecast_solar_unconfirmed"  # read from the sensor
    result = await options.async_configure(result["flow_id"], {"solar_forecast_source": "forecast_solar_unconfirmed"})
    assert result["step_id"] == "solar_forecast_solar"
    key, picker = _field(result, "forecast_entity_id")
    assert key.description["suggested_value"] == FORECAST_SOLAR and _integrations(picker) == ["forecast_solar"]
    wrong = await options.async_configure(result["flow_id"], _solar_input(result, forecast_entity_id=SOLCAST))
    assert wrong["errors"] == {"forecast_entity_id": "forecast_wrong_source"} and _integrations(_field(wrong, "forecast_entity_id")[1]) == ["forecast_solar"]
    empty = await options.async_configure(wrong["flow_id"], _solar_input(wrong, forecast_entity_id=None))
    # no sensor: back to the choice, from where "no forecast" leads on without one
    assert empty["step_id"] == "solar_forecast_source" and empty["errors"] == {"base": "forecast_sensor_missing"}
    again = await options.async_configure(empty["flow_id"], {"solar_forecast_source": "forecast_solar_unconfirmed"})
    assert again["step_id"] == "solar_forecast_solar"
    accepted = await options.async_configure(again["flow_id"], _solar_input(again, forecast_entity_id=FORECAST_SOLAR))
    assert accepted["step_id"] not in SOLAR_PAGES.values() and not accepted.get("errors"), accepted

    result = await _solar_pages(hass, loaded_entry, FORECAST_SOLAR)
    result = await options.async_configure(result["flow_id"], {"solar_forecast_source": "solcast_solar"})
    key, picker = _field(result, "forecast_entity_id")
    assert not (key.description or {}).get("suggested_value") and _integrations(picker) == ["solcast_solar"]
    assert result["step_id"] == "solar_solcast"


async def test_a_sensor_of_an_unknown_platform_is_never_cleared_or_hidden(hass: HomeAssistant, loaded_entry) -> None:
    """E.g. a restored backup whose forecast integration is not set up yet."""
    options = hass.config_entries.options
    result = await _solar_pages(hass, loaded_entry, UNKNOWN)
    assert _field(result, "solar_forecast_source")[0].default() == "solcast_solar"  # never "none": that would clear it
    result = await options.async_configure(result["flow_id"], {"solar_forecast_source": "solcast_solar"})
    key, picker = _field(result, "forecast_entity_id")
    assert key.description["suggested_value"] == UNKNOWN and _integrations(picker) == ["solcast_solar", "forecast_solar", "helios_forecast"]
    accepted = await options.async_configure(result["flow_id"], _solar_input(result))
    assert accepted["step_id"] not in SOLAR_PAGES.values(), accepted


async def test_a_forecast_still_needs_the_solar_production(hass: HomeAssistant, loaded_entry) -> None:
    _register(hass, SOLCAST, "solcast_solar")
    result = await _solar_pages(hass, loaded_entry, SOLCAST)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"solar_forecast_source": "solcast_solar"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], _solar_input(result, production_entity_id=None))
    assert result["errors"] == {"production_entity_id": "production_required"}


def test_every_language_has_the_page_its_choices_and_errors() -> None:
    for path in [PACKAGE / "strings.json", *(PACKAGE / "translations").glob("*.json")]:
        data = json.loads(path.read_text(encoding="utf-8"))
        labels = data["selector"]["solar_forecast_sources"]["options"]
        assert set(labels) == {"none", *SOLAR_SOURCES, *(f"{source}_unconfirmed" for source in SOLAR_SOURCES)}, path.name
        for source in SOLAR_SOURCES:
            assert labels[source] == SOLAR_SOURCE_NAMES[source] and labels[f"{source}_unconfirmed"] == f"🧪 {SOLAR_SOURCE_NAMES[source]}", path.name
        for flow in ("config", "options"):
            page = data[flow]["step"]["solar_forecast_source"]
            assert page["title"] and "🧪" in page["description"] and page["data"]["solar_forecast_source"], (path.name, flow)
            assert data[flow]["error"]["forecast_wrong_source"] and data[flow]["error"]["forecast_sensor_missing"], (path.name, flow)


def _engine():
    return CJPEngine(build_config(OPTIONS), "cjp_test", TEXTS)


def _checked(source: str | None) -> dict:
    """The attributes one job publishes with this snapshot."""
    snap = Snapshot(now=datetime.datetime.combine(TODAY, datetime.time(9, 30)), tz=TZ)
    snap.solar_source = source
    snap.solcast = {"sensor.x": [_row(0, 10, 1.0), _row(1, 10, 2.0), _row(1, 11, 0.5)]}
    result = _engine().run_job(snap, lambda: None)
    assert result.error is None, result.error
    return result.attributes.get("current_charging_rule", {})


def test_a_job_that_read_a_forecast_publishes_what_the_plan_reads() -> None:
    check = _checked("forecast_solar")["solar_check"]
    assert (check["source"], check["confirmed"]) == ("forecast_solar", False)
    assert check["checked"].startswith("2026-06-10T09:30+02:00")
    assert check["days"] == [{"date": "2026-06-10", "hours": 1, "kwh": 1.0}, {"date": "2026-06-11", "hours": 2, "kwh": 2.5}]
    assert _checked("solcast_solar")["solar_check"]["confirmed"] is True
    assert _checked("")["solar_check"]["confirmed"] is False  # a platform that is not known
    assert "solar_check" not in _checked(None)  # no forecast read in this job
    assert Snapshot.__dataclass_fields__["solar_source"].default is None


def _settings_cards(forecast: str, language: str = "en") -> list[dict]:
    config = build_config({**default_options(), "language": language, "forecast_entity_id": forecast, "production_entity_id": "sensor.pv" if forecast else ""})
    dashboard = yaml.safe_load(render_dashboard(config, "mine", "entry1", _own("mine")))
    view = next(view for view in dashboard["views"] if view["path"] == "settings")
    return [card for part in view["sections"] for card in part["cards"]]


def _solar_card(language: str = "en") -> str:
    found = [card["content"] for card in _settings_cards(SOLCAST, language) if "'solar_check'" in card.get("content", "")]
    assert len(found) == 1, found
    return found[0]


def test_the_section_is_only_there_with_a_forecast() -> None:
    with_forecast = [card.get("heading") for card in _settings_cards(SOLCAST) if card.get("type") == "heading"]
    without = [card.get("heading") for card in _settings_cards("") if card.get("type") == "heading"]
    assert "Solar forecast check" in with_forecast and "Solar forecast check" not in without
    assert [heading for heading in with_forecast if heading != "Solar forecast check"][-2:] == without[-2:]  # the rest stays
    assert not any("solar_check" in card.get("content", "") for card in _settings_cards(""))


@pytest.mark.parametrize("language", ['da', 'de', 'en', 'en-GB', 'pl', 'uk'])
async def test_the_card_renders_in_every_language(hass: HomeAssistant, language: str) -> None:
    card = _solar_card(language)
    names = re.search(r"set sources = (\{.*?\}) %}", card)[1]
    assert yaml.safe_load(names.replace("':", "' :")) == SOLAR_SOURCE_NAMES
    hass.states.async_set("sensor.cjp_mine_current_charging_rule", "x", {"solar_check": {"source": "forecast_solar", "confirmed": False, "checked": "2026-06-10T09:30+02:00",
                                                           "days": [{"date": "2026-06-10", "hours": 1, "kwh": 1.0}]}})
    shown = Template(card, hass).async_render(parse_result=False)
    assert "@@" not in shown and "solar_check_" not in shown and "🧪 Forecast.Solar" in shown, shown


async def test_the_card_shows_the_facts_without_judging_and_the_report_link_holds_no_energy(hass: HomeAssistant) -> None:
    card = _solar_card()
    sensor = "sensor.cjp_mine_current_charging_rule"

    def shown() -> str:
        return Template(card, hass).async_render(parse_result=False).strip()

    def link() -> dict:
        after = shown().split("</details>")[1]
        address = re.search(r"\[Solar Forecast Verification Report \(Confirmed\)\]\((\S+)\)$", after.strip())
        assert address and "(" not in address[1], after
        url = urllib.parse.urlsplit(address[1])
        assert f"{url.scheme}://{url.netloc}{url.path}" == "https://github.com/dezito/Cable-Juice-Planner-Integration/issues/new"
        return dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))

    hass.states.async_set(sensor, "x", {})
    assert "No result." in shown() and "|" not in shown().split("</details>")[0]
    assert (link()["source"], link()["solar_check"], link()["title"]) == ("", "", "Solar Forecast Verification Report (Confirmed)")
    check = {"source": "solcast_solar", "confirmed": True, "checked": "2026-06-10T09:30+02:00",
             "days": [{"date": "2026-06-10", "hours": 18, "kwh": 31.5}, {"date": "2026-06-11", "hours": 0, "kwh": None}]}
    hass.states.async_set(sensor, "x", {"solar_check": check})
    assert "Source: ✅ Solcast PV Forecast" in shown()
    assert """
| Day | Hours with a forecast | kWh |
|:--|:-:|:-:|
| 2026-06-10 | 18 | 31.5 |
| 2026-06-11 | 0 | ➖ |
""" in shown(), shown()
    manifest = json.loads((PACKAGE / "manifest.json").read_text(encoding="utf-8"))
    fields = link()
    assert fields == {
        "template": "solar_forecast_verification.yml",
        "title": "Solar Forecast Verification Report (Confirmed): Solcast PV Forecast",
        "source": "Solcast PV Forecast",
        "version": manifest["version"],
        "ha_version": HA_VERSION,
        "solar_check": fields["solar_check"],
    }
    assert json.loads(fields["solar_check"]) == {
        "checked": check["checked"], "source": "solcast_solar",
        "days": [{"date": "2026-06-10", "hours": 18}, {"date": "2026-06-11", "hours": 0}],
    }
    assert "kwh" not in fields["solar_check"] and "31.5" not in urllib.parse.unquote_plus(shown().split("</details>")[1])
    hass.states.async_set(sensor, "x", {"solar_check": {**check, "days": []}})
    assert "no values for today or later" in shown()


def test_the_issue_form_has_every_field_the_link_fills_in() -> None:
    form = yaml.safe_load((pathlib.Path(__file__).parents[1] / ".github" / "ISSUE_TEMPLATE" / "solar_forecast_verification.yml").read_text(encoding="utf-8"))
    assert form["name"] == "Solar Forecast Verification Report (Confirmed)" and form["title"].startswith(form["name"])
    ids = [item["id"] for item in form["body"] if "id" in item]
    assert len(ids) == len(set(ids)) and {"source", "version", "ha_version", "solar_check"} <= set(ids)


async def test_the_source_of_the_snapshot_is_the_platform_of_the_sensor(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    _register(hass, FORECAST_SOLAR, "forecast_solar")
    solar = coordinator.config["solar"]["entity_ids"]
    stored = solar["forecast_entity_id"]
    try:
        for entity_id, source in ((FORECAST_SOLAR, "forecast_solar"), (UNKNOWN, ""), ("", None)):
            solar["forecast_entity_id"] = entity_id
            assert coordinator._solar_source() == source
    finally:
        solar["forecast_entity_id"] = stored


async def test_a_user_without_a_sensor_of_the_chosen_forecast_is_never_stuck(hass: HomeAssistant, loaded_entry) -> None:
    """The picker of an integration that is not installed is empty: the empty field leads back, and no forecast leads on."""
    options = hass.config_entries.options
    result = await _solar_pages(hass, loaded_entry, "")
    result = await options.async_configure(result["flow_id"], {"solar_forecast_source": "helios_forecast_unconfirmed"})
    assert result["step_id"] == "solar_helios"
    result = await options.async_configure(result["flow_id"], _solar_input(result, forecast_entity_id=None))
    assert result["step_id"] == "solar_forecast_source" and result["errors"] == {"base": "forecast_sensor_missing"}
    result = await options.async_configure(result["flow_id"], {"solar_forecast_source": "none"})
    assert result["step_id"] == "solar" and _field(result, "forecast_entity_id") is None
    result = await options.async_configure(result["flow_id"], form_defaults(result))
    assert result.get("step_id") not in ("solar", "solar_forecast_source") and not result.get("errors"), result


async def test_a_stored_sensor_of_an_integration_that_is_no_source_is_cleared_by_the_choice(hass: HomeAssistant, loaded_entry) -> None:
    """E.g. a template sensor from an import: it never fed the plan, so it is not suggested on a source's page."""
    _register(hass, UNKNOWN, "template")
    result = await _solar_pages(hass, loaded_entry, UNKNOWN)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"solar_forecast_source": "solcast_solar"})
    key, picker = _field(result, "forecast_entity_id")
    assert not (key.description or {}).get("suggested_value") and _integrations(picker) == ["solcast_solar"]


def test_each_forecast_has_a_solar_page_with_only_its_own_texts() -> None:
    assert set(SOLAR_PAGES) == set(SOLAR_SOURCES)
    for path in [PACKAGE / "strings.json", *(PACKAGE / "translations").glob("*.json")]:
        data = json.loads(path.read_text(encoding="utf-8"))
        for flow in ("config", "options"):
            steps = data[flow]["step"]
            # without a forecast: no sensor field and nothing about a forecast integration
            plain = steps["solar"]
            assert "forecast_entity_id" not in plain["data"] and "forecast_entity_id" not in plain.get("data_description", {}), (path.name, flow)
            assert not any(name in plain["description"] for name in SOLAR_SOURCE_NAMES.values()), (path.name, flow)
            for source, step in SOLAR_PAGES.items():
                page = steps[step]
                others = [name for other, name in SOLAR_SOURCE_NAMES.items() if other != source]
                shown = " ".join((page["title"], page["description"], page["data"]["forecast_entity_id"], page["data_description"]["forecast_entity_id"]))
                assert SOLAR_SOURCE_NAMES[source] in page["title"] and SOLAR_SOURCE_NAMES[source] in page["data"]["forecast_entity_id"], (path.name, step)
                assert not any(name in shown for name in others if name not in "Solcast PV Forecast"), (path.name, step, shown)
                assert ("{solcast_problems}" in page["description"]) is (source == "solcast_solar"), (path.name, step)
                assert set(page["data"]) == set(plain["data"]) | {"forecast_entity_id"}, (path.name, step)
            # the Solcast page with nothing missing says so in words, with the same fields
            ready, waiting = steps["solar_solcast_ready"], steps["solar_solcast"]
            assert "✅" in ready["description"] and "⚠️" in waiting["description"] and "{solcast_problems}" not in ready["description"], (path.name, flow)
            assert (ready["title"], ready["data"], ready["data_description"]) == (waiting["title"], waiting["data"], waiting["data_description"]), (path.name, flow)
