"""The generated dashboard: entities mapped to the instance, missing cards removed, texts translated."""
from __future__ import annotations

from types import SimpleNamespace

import yaml
from jinja2 import Environment
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.core import HomeAssistant

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.management.dashboard_build import render_dashboard
from custom_components.cable_juice_planner.entity import help_values
from custom_components.cable_juice_planner.utils.formatting import (
    format_analysis_help_text,
    format_help_text,
    load_texts,
)

from .conftest import (
    MOCK_OPTIONS,
    OBJECT_PREFIX,
    async_setup,
    async_unload_and_join,
    make_entry,
    set_source_states,
)
from .test_engine import OPTIONS


def _own(prefix: str) -> set[str]:
    own = const.object_prefix(prefix)
    ids = {f"switch.{own}_{key}" for key, info in const.SWITCH_SETTINGS.items() if info.get("requires") in (None, "ev")}
    ids |= {f"number.{own}_{key}" for key, info in const.NUMBER_SETTINGS.items() if info.get("requires") in (None, "ev", "public_charging")}
    ids |= {f"sensor.{own}_{key}" for key, info in const.SENSOR_DEFS.items() if info[3] in (None, "ev")}
    ids |= {f"time.{own}_{key}" for key in const.TIME_SETTINGS} | {f"datetime.{own}_{key}" for key in const.DATETIME_SETTINGS}
    ids |= {f"select.{own}_{key}" for key in const.SELECT_SETTINGS} | {f"button.{own}_{key}" for key in (*const.BUTTON_KEYS, *const.EV_BUTTON_KEYS)}
    return ids


def _cards(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _cards(value)
    elif isinstance(node, list):
        for item in node:
            yield from _cards(item)


def test_dashboard_for_a_car_without_solar() -> None:
    config = const.build_config({**OPTIONS, "language": "da"})
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status", "sensor.price"}
    text = render_dashboard(config, "tesla", "entry1", existing)
    assert "@@" not in text
    assert "cjp_tesla_fill_up" in text and "switch.cjp_fill_up" not in text
    assert "sensor.ev_battery_level" in text
    dashboard = yaml.safe_load(text)
    assert [view["title"] for view in dashboard["views"]] == ["Oversigt", "Indstillinger", "Analyse", "What-if"]
    headings = [card["heading"] for card in _cards(dashboard) if card.get("type") == "heading"]
    # no solar, no virtual car: their sections are gone with their headings
    assert "Sol og husbatteri" not in headings and "Virtuel elbil" not in headings
    assert "Arbejdsplan" in headings and "Billig strøm" in headings
    # the overview markdown is shown as the integration writes it
    contents = [card.get("content") for card in _cards(dashboard) if card.get("type") == "markdown"]
    assert "{{ state_attr('sensor.cjp_tesla_overview', 'overview') }}" in contents
    # the debug info badge depends on the owner's input_boolean, so it is removed without it
    assert "cable_juice_planner.debug_info" not in text
    with_debug = render_dashboard(config, "tesla", "entry1", existing | {"input_boolean.dezito_show_debug_button"})
    assert "config_entry_id: entry1" in with_debug


def test_currency_follows_home_assistant() -> None:
    texts = load_texts("en", "DKK")
    assert (texts["ui.common.valuta"], texts["ui.common.valuta_kwh"], texts["ui.common.valuta_distance"]) == ("kr", "kr/kWh", "kr/mi")
    assert load_texts("da", "EUR")["ui.common.valuta_distance"] == "€/km"
    assert "{alternative_cost}€ {alternative_per_kwh}€/kWh" in load_texts("da", "EUR")["ui.cheap_grid_charge_hours.departure_plan.estimated_daily_charging"]
    config = const.build_config({**OPTIONS, "language": "da"})
    text = render_dashboard(config, "tesla", "entry1", _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status", "sensor.price"}, "EUR")
    assert "€/kWh" in text and "kr/kWh" not in text and "{valuta" not in text


def test_sell_price_chart_subtracts_the_configured_sell_fee() -> None:
    """The chart's sell price is spot - (net tariff + system tariff + sell fee), the fee being the planner's."""
    options = {
        **OPTIONS,
        "language": "da",
        "sell_fee": 0.07,
        "stromligning_buy_current_price": "sensor.stromligning_current_price_vat",
        "stromligning_sell_current_price": "sensor.stromligning_15min_current_price_ex_vat",
    }
    existing = _own("tesla") | {
        "sensor.ev_battery_level",
        "sensor.charger_status",
        "sensor.stromligning_current_price_vat",
        "sensor.stromligning_nettariff_vat",
        "sensor.stromligning_systemtariff_vat",
        "sensor.stromligning_15min_spotprice_ex_vat",
    }
    text = render_dashboard(const.build_config(options), "tesla", "entry1", existing)
    assert "const sellFee = Number('0.07') || 0;" in text
    assert "0.025" not in text and "@@sell_fee_value@@" not in text
    assert "spotprice - sellCosts" in text


def test_distance_follows_home_assistant() -> None:
    metric = load_texts("en", "GBP", "km")
    assert (metric["ui.common.distance_type"], metric["ui.common.distance_kwh"], metric["ui.common.valuta_distance"]) == ("km", "km/kWh", "£/km")
    assert "{distance" not in metric["dashboard.forecast_avg_km"] and "km/kWh" in metric["dashboard.forecast_avg_km"]
    assert load_texts("da", "DKK", "mi")["ui.common.wh_distance"] == "Wh/mi"
    assert load_texts("uk", None, "km")["ui.common.distance_type"] == "км"
    config = const.build_config({**OPTIONS, "language": "da"})
    text = render_dashboard(config, "tesla", "entry1", _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status", "sensor.price"}, "DKK", "mi")
    assert "mi/kWh" in text and "km/kWh" not in text and "{distance" not in text


def test_every_language_has_every_text() -> None:
    """A key missing in a language (and in English, the fallback) is shown as the raw key."""
    import json
    from pathlib import Path

    def flat(data: dict, prefix: str = "") -> set[str]:
        keys: set[str] = set()
        for key, value in data.items():
            keys |= flat(value, f"{prefix}{key}.") if isinstance(value, dict) else {f"{prefix}{key}"}
        return keys

    folder = Path(__file__).parents[1] / "custom_components" / "cable_juice_planner" / "texts"
    keys = {path.stem: flat(json.loads(path.read_text(encoding="utf-8"))) for path in folder.glob("*.json")}
    every = set().union(*keys.values())
    assert {language: sorted(every - found) for language, found in keys.items() if every - found} == {}


def test_dashboard_layout() -> None:
    config = const.build_config({**OPTIONS, "language": "da"})
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status", "binary_sensor.cjp_tesla_public_charging_session_done"}
    overview, settings, *_ = yaml.safe_load(render_dashboard(config, "tesla", "entry1", existing))["views"]
    headings = lambda view: [card["heading"] for card in _cards(view) if card.get("type") == "heading"]  # noqa: E731
    # the charging history above the overview, like the old dashboard
    contents = [card.get("content") for card in _cards(overview) if card.get("type") == "markdown"]
    history = contents.index("{{ state_attr('sensor.cjp_tesla_charging_history', 'history') }}")
    assert contents[history + 1] == "{{ state_attr('sensor.cjp_tesla_overview', 'overview') }}"
    # no rule twice in the settings: the modes with their own section are only there
    settings_ids = [row["entity"] for card in _cards(settings) if card.get("type") == "entities" for row in card["entities"] if "entity" in row]
    # manual and forced charging are only on the overview
    assert not {"switch.cjp_tesla_allow_manual_charging_now", "switch.cjp_tesla_forced_charging_daily_battery_level"} & set(settings_ids)
    assert settings_ids.count("switch.cjp_tesla_workplan_charging") == 1
    assert settings_ids.count("switch.cjp_tesla_fill_up") == 1
    # manual charging also on the overview, with a description
    overview_ids = [row["entity"] for card in _cards(overview) if card.get("type") == "entities" for row in card["entities"] if "entity" in row]
    assert {"switch.cjp_tesla_allow_manual_charging_now", "switch.cjp_tesla_allow_manual_charging_solar"} <= set(overview_ids)
    assert "Manuel ladning" in headings(overview)
    # status above the trip, manual charging below the trip, before the emoji description
    trip_section = next(section for section in overview["sections"] if "Manuel ladning" in headings(section))
    assert headings(trip_section)[:2] == ["Status", "Tur"]
    assert "emoji_description" in trip_section["cards"][-1].get("content", "")
    # the battery chart: the planned km per day, not the battery level history
    chart = next(card for card in _cards(overview) if card.get("type") == "custom:apexcharts-card")
    assert [series.get("yaxis_id") for series in chart["series"]] == ["distance", "level", "charge"]
    for series in chart["series"]:
        generator = series["data_generator"]
        assert "const flow = entity.attributes.battery_forecast || [];" in generator
        assert "return flow.map(" in generator
    assert "sum + Number(d.km)" in chart["series"][0]["data_generator"]
    # public charging only while a session waits for its cost
    conditional = next(card for card in _cards(overview) if card.get("type") == "conditional" and "Offentlig ladning" in headings(card))
    assert conditional["conditions"] == [{"condition": "state", "entity": "binary_sensor.cjp_tesla_public_charging_session_done", "state": "on"}]
    # the queue: how many are waiting and which session the cost is for
    queue = next(card["content"] for card in _cards(conditional) if card.get("type") == "markdown" and "public_charging_session'" in card.get("content", ""))
    assert "sessions | length > 1" in queue and "sessions[0].start_battery_level" in queue
    assert "@@" not in queue


def test_public_charging_only_with_a_location() -> None:
    """Without the car's location the public charging card goes, also while the old entities are still registered."""
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status", "binary_sensor.cjp_tesla_public_charging_session_done"}
    with_location = render_dashboard(const.build_config({**OPTIONS, "language": "da"}), "tesla", "entry1", existing)
    without = render_dashboard(const.build_config({**OPTIONS, "language": "da", "location_entity_id": ""}), "tesla", "entry1", existing)
    for entity_id in ("binary_sensor.cjp_tesla_public_charging_session_done", "number.cjp_tesla_public_charging_session_cost"):
        assert entity_id in with_location
        assert entity_id not in without
    overview = yaml.safe_load(without)["views"][0]
    assert "Offentlig ladning" not in [card.get("heading") for card in _cards(overview)]


def test_preheat_only_with_a_climate_entity() -> None:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status", "climate.tesla_climate"}
    without = render_dashboard(const.build_config({**OPTIONS, "language": "da"}), "tesla", "entry1", existing)
    assert "preheat" not in without
    with_climate = render_dashboard(const.build_config({**OPTIONS, "language": "da", "climate_entity_id": "climate.tesla_climate"}), "tesla", "entry1", existing)
    for entity_id in ("switch.cjp_tesla_trip_preheat", "switch.cjp_tesla_preheat_monday", "number.cjp_tesla_preheat_minutes_before"):
        assert entity_id in with_climate


def test_min_charge_limit_only_with_a_charge_limit_entity() -> None:
    """Without "Car charge limit" the min. charge limit is hidden; the max. recommended limit stays (the plan uses it)."""
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status", "number.ev_charge_limit"}
    with_limit = render_dashboard(const.build_config({**OPTIONS, "language": "da"}), "tesla", "entry1", existing)
    assert "number.cjp_tesla_min_charge_limit_battery_level" in with_limit
    without = render_dashboard(const.build_config({**OPTIONS, "language": "da", "charging_limit_entity_id": ""}), "tesla", "entry1", existing)
    assert "number.cjp_tesla_min_charge_limit_battery_level" not in without
    assert "entity: number.cjp_tesla_max_recommended_charge_limit_battery_level" in without
    # the section's read more still finds its help
    assert "state_attr('number.cjp_tesla_max_recommended_charge_limit_battery_level', 'help')" in without


async def test_help_leaves_out_the_min_charge_limit_without_its_entity(hass: HomeAssistant) -> None:
    from custom_components.cable_juice_planner.entity import CJPEntity

    def help_text(charging_limit: str) -> str:
        config = const.build_config({**OPTIONS, "language": "da", "charging_limit_entity_id": charging_limit})
        coordinator = SimpleNamespace(
            hass=hass, object_prefix="cjp_tesla", data={}, settings={}, config=config,
            texts=load_texts("da"), entry=SimpleNamespace(entry_id="entry1"), prefix="tesla", car_name=None,
        )
        return CJPEntity(coordinator, "number", "min_daily_battery_level").help_attributes()["help"]

    assert "Minimum ladegrænse" in help_text("number.ev_charge_limit")
    without = help_text("")
    assert "Minimum ladegrænse" not in without and "Maks. anbefalet ladegrænse" in without and "\n\n\n" not in without


def test_forced_charging_shown_when_relevant() -> None:
    """Only while the battery level is under the daily minimum."""
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    overview = yaml.safe_load(render_dashboard(const.build_config({**OPTIONS, "language": "da"}), "tesla", "entry1", existing))["views"][0]
    card = next(c for c in _cards(overview) if c.get("type") == "conditional" and "forced_charging_daily_battery_level" in yaml.dump(c["card"]))
    branches = card["conditions"][0]["conditions"]
    # the car's battery level; the virtual car's branch is removed with its entity (an empty "and" would always be true)
    assert len(branches) == 1
    assert [c["entity"] for c in branches[0]["conditions"]] == ["sensor.ev_battery_level"] * 2
    assert branches[0]["conditions"][0]["below"] == "number.cjp_tesla_min_daily_battery_level"


def test_simulated_battery_level_on_the_dashboard() -> None:
    """With the simulation the virtual battery level is the simulated one: next to the car's, in the history and its own settings section."""
    config = const.build_config({**OPTIONS, "language": "da", "simulate_battery_level_while_charging": True})
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status", "number.cjp_tesla_battery_level"}
    dashboard = yaml.safe_load(render_dashboard(config, "tesla", "entry1", existing))
    overview, settings, *_ = dashboard["views"]
    status = next(card for card in _cards(overview) if card.get("type") == "entities" and any(row.get("entity") == "sensor.ev_battery_level" for row in card["entities"]))
    assert [row["entity"] for row in status["entities"]][:2] == ["sensor.ev_battery_level", "number.cjp_tesla_battery_level"]
    graph = next(card for card in _cards(dashboard) if card.get("type") == "history-graph" and any(row.get("entity") == "sensor.ev_battery_level" for row in card["entities"]))
    assert "number.cjp_tesla_battery_level" in [row["entity"] for row in graph["entities"]]
    # forced charging follows the level the plan uses
    forced = next(c for c in _cards(overview) if c.get("type") == "conditional" and "forced_charging_daily_battery_level" in yaml.dump(c["card"]))
    assert {c["entity"] for branch in forced["conditions"][0]["conditions"] for c in branch["conditions"]} == {"number.cjp_tesla_battery_level"}
    # the virtual car section is named after the simulation, with its read more (only the battery level is left in it)
    headings = [card["heading"] for card in _cards(settings) if card.get("type") == "heading"]
    assert "Simuleret batteriniveau" in headings and "Virtuel elbil" not in headings
    contents = [card["content"] for card in _cards(settings) if card.get("type") == "markdown"]
    assert any("state_attr('number.cjp_tesla_battery_level', 'help')" in content for content in contents)


def test_settings_read_more_includes_each_help_group() -> None:
    options = {
        **OPTIONS,
        "language": "da",
        "production_entity_id": "sensor.solar",
        "powerwall_battery_level_entity_id": "sensor.powerwall_battery",
    }
    existing = _own("tesla") | {
        "sensor.ev_battery_level",
        "sensor.charger_status",
        "sensor.solar",
        "sensor.powerwall_battery",
        "switch.cjp_tesla_solar_charging",
        "switch.cjp_tesla_powerwall_discharge_above_needed",
        "number.cjp_tesla_ev_charge_after_powerwall_battery_level",
    }
    settings = yaml.safe_load(render_dashboard(const.build_config(options), "tesla", "entry1", existing))["views"][1]
    contents = [card["content"] for card in _cards(settings) if card.get("type") == "markdown"]
    cheap_help = next(content for content in contents if "limits_help" in content)
    solar_help = next(content for content in contents if "{{ solar_help }}" in content)
    powerwall_help = next(content for content in contents if "{{ powerwall_help }}" in content)

    assert "state_attr('switch.cjp_tesla_cheap_grid_charging', 'help')" in cheap_help
    assert "state_attr('sensor.cjp_tesla_charge_very_cheap_battery_level', 'help')" in cheap_help
    assert "{{ ns.limits_help }}" in cheap_help and "{{ ns.targets_help }}" in cheap_help
    learn_help = next(content for content in contents if "learn_help" in content)
    assert "state_attr('switch.cjp_tesla_learn_driving_distance', 'help')" in learn_help
    assert "ns.learn_help" in learn_help
    # solar and the home battery each have their own section and their own read more
    assert "state_attr('switch.cjp_tesla_solar_charging', 'help')" in solar_help
    assert "powerwall" not in solar_help
    assert "state_attr('number.cjp_tesla_ev_charge_after_powerwall_battery_level', 'help')" in powerwall_help
    assert "solar_charging" not in powerwall_help


def test_learned_distance_switch_is_under_advanced_settings() -> None:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    switch = "switch.cjp_tesla_learn_driving_distance"

    for language in ("da", "de", "en", "en-GB", "pl", "uk"):
        sections = _settings_sections({**OPTIONS, "language": language}, existing)
        workplan = next(ids for ids in sections.values() if "switch.cjp_tesla_workplan_charging" in ids)
        advanced = next(ids for ids in sections.values() if "select.cjp_tesla_forecast_type" in ids)

        assert switch not in workplan, language
        assert switch in advanced, language
        assert "switch.cjp_tesla_learn_soc_charge_power" in advanced, language
        text = render_dashboard(const.build_config({**OPTIONS, "language": language}), "tesla", "entry1", existing)
        assert "@@" not in text, language
        settings = yaml.safe_load(text)["views"][1]
        rows = next(
            card["entities"] for card in _cards(settings)
            if card.get("type") == "entities"
            and any(row.get("entity") == "select.cjp_tesla_forecast_type" for row in card["entities"])
        )
        actual_order = [
            row.get("entity", ("section", row.get("label")))
            for row in rows
        ]
        assert actual_order == [
            switch,
            ("section", None),
            "switch.cjp_tesla_auto_forecast_type",
            "select.cjp_tesla_forecast_type",
            ("section", None),
            "switch.cjp_tesla_price_forecast_correction",
            ("section", None),
            "switch.cjp_tesla_learn_soc_charge_power",
        ], language
        assert "learned_distance" not in load_texts(language, "GBP", "km")["entity_help.workplan"], language

        read_more = next(content for content in _cards(settings) if content.get("type") == "markdown" and "learn_help" in content.get("content", ""))["content"]
        assert "state_attr('switch.cjp_tesla_learn_soc_charge_power', 'help')" in read_more, language
        assert "ns.soc_power_help" in read_more, language
        attributes = {
            ("switch.cjp_tesla_learn_driving_distance", "help"): "learn-help",
            ("switch.cjp_tesla_auto_forecast_type", "help"): "forecast-help",
            ("select.cjp_tesla_forecast_type", "description"): "forecast-type-description",
            ("switch.cjp_tesla_price_forecast_correction", "help"): "price-help",
        }
        rendered_read_more = Environment().from_string(read_more).render(
            state_attr=lambda entity, attribute: attributes.get((entity, attribute))
        )
        ordered_sections = (
            "learn-help",
            "---",
            "forecast-help",
            "forecast-type-description",
            "---",
            "price-help",
        )
        section_positions = []
        cursor = 0
        for section in ordered_sections:
            position = rendered_read_more.index(section, cursor)
            section_positions.append(position)
            cursor = position + len(section)
        assert section_positions == sorted(section_positions), language
        assert rendered_read_more.count("---") == 2, language


def _settings_sections(options: dict, existing: set[str]) -> dict[str, list[str]]:
    """Section heading -> entity ids of the Settings view."""
    settings = yaml.safe_load(render_dashboard(const.build_config(options), "tesla", "entry1", existing))["views"][1]
    sections = {}
    for grid in (card for card in _cards(settings) if card.get("type") == "grid"):
        heading = None
        for card in grid["cards"]:
            if card.get("type") == "heading":
                heading = card["heading"]
            elif card.get("type") == "entities":
                sections.setdefault(heading, []).extend(row if isinstance(row, str) else row["entity"] for row in card["entities"] if isinstance(row, str) or "entity" in row)
    return sections


def test_home_battery_settings_have_their_own_section() -> None:
    options = {
        **OPTIONS,
        "language": "en",
        "production_entity_id": "sensor.solar",
        "powerwall_battery_level_entity_id": "sensor.powerwall_battery",
    }
    existing = _own("tesla") | {
        "sensor.ev_battery_level", "sensor.charger_status", "sensor.solar", "sensor.powerwall_battery",
        "switch.cjp_tesla_solar_charging", "sensor.cjp_tesla_solar_over_production_current_hour",
        "switch.cjp_tesla_powerwall_discharge_above_needed",
        "number.cjp_tesla_ev_charge_after_powerwall_battery_level",
    }
    sections = _settings_sections(options, existing)
    assert sections["Solar"] == ["switch.cjp_tesla_solar_charging", "sensor.cjp_tesla_solar_over_production_current_hour"]
    assert sections["Home battery"] == [
        "number.cjp_tesla_ev_charge_after_powerwall_battery_level",
        "switch.cjp_tesla_powerwall_discharge_above_needed",
    ]


def test_no_home_battery_section_without_a_home_battery() -> None:
    """Without a configured home battery its entities do not exist: neither the section nor its read more is left."""
    options = {**OPTIONS, "language": "en", "production_entity_id": "sensor.solar"}
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status", "sensor.solar", "switch.cjp_tesla_solar_charging"}
    settings = yaml.safe_load(render_dashboard(const.build_config(options), "tesla", "entry1", existing))["views"][1]
    sections = _settings_sections(options, existing)
    assert "Solar" in sections and "Home battery" not in sections
    assert "powerwall" not in yaml.safe_dump(settings)


def test_solar_read_more_has_the_table_style_of_overview_and_analysis() -> None:
    """The solar steps table in "Read more" gets the same card-mod table style as the tables on Overview and Analysis."""
    options = {**OPTIONS, "language": "da", "production_entity_id": "sensor.solar"}
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status", "sensor.solar", "switch.cjp_tesla_solar_charging"}
    views = yaml.safe_load(render_dashboard(const.build_config(options), "tesla", "entry1", existing))["views"]
    cards = [card for view in views for card in _cards(view) if card.get("type") == "markdown"]
    solar = next(card for card in cards if "solar_help" in card["content"])
    overview = next(card for card in cards if "sensor.cjp_tesla_overview" in card["content"])
    assert solar["card_mod"] == overview["card_mod"]
    assert "table, th, td" in solar["card_mod"]["style"]["ha-markdown$"]


def test_entity_help_uses_spaced_bold_italic_labels() -> None:
    text = format_help_text(
        "Group overview.\n• Charging: Uses surplus.\n  • Current target: Follows house consumption."
    )

    assert text == (
        "Group overview.\n\n"
        "***Charging***\nUses surplus.\n\n"
        "***Current target***\nFollows house consumption."
    )
    assert format_help_text("Overview. **First metric**: one. **Second metric**: two.") == (
        "Overview.\n\n***First metric***\none.\n\n***Second metric***\ntwo."
    )
    assert format_analysis_help_text("*First point. Second point.*") == "First point.\nSecond point."
    assert format_analysis_help_text(
        "*Hver aften kl. 23:59 gemmes planen. Gns.: gennemsnitspris. Ved bilen med kravet: dagens kørsel.*"
    ) == (
        "Hver aften kl. 23:59 gemmes planen.\n"
        "**Gns.**: gennemsnitspris.\n"
        "Ved bilen med kravet:\n"
        "Dagens kørsel."
    )
    economy = format_analysis_help_text(
        "*Gns.: gennemsnitspris. Daglig: billigst frem til afgang. Laveste: billigste timer. "
        "Score: tæt på laveste. kr/km: al ladning inkl. offentlig divideret med kørte km. "
        "Sammenligningen findes kun i kendte perioder.*"
    )
    assert economy == (
        "**Gns.**: gennemsnitspris.\n"
        "**Daglig**: billigst frem til afgang.\n"
        "**Laveste**: billigste timer.\n"
        "**Score**: tæt på laveste.\n"
        "**kr/km**: al ladning inkl. offentlig divideret med kørte km.\n"
        "Sammenligningen findes kun i kendte perioder."
    )
    assert format_analysis_help_text("Ready; next point.") == "Ready;\nNext point."
    assert load_texts("da")["dashboard.economy_explain"].startswith("**Gns.**:")
    assert "***Solcelleproduktion til rådighed***\n" in load_texts("da")["entity_help.solar_charging"]
    assert "***Batteriniveau og ladegrænse***\n" in load_texts("da")["dashboard.history_explain"]


def test_entity_help_uses_current_number_states(hass: HomeAssistant) -> None:
    hass.states.async_set("number.cjp_tesla_min_daily_battery_level", "37.0")
    hass.states.async_set("number.cjp_tesla_typical_daily_distance", "62.5")
    coordinator = SimpleNamespace(
        hass=hass,
        object_prefix="cjp_tesla",
        data={"min_daily_battery_level": 30.0},
        settings={"typical_daily_distance": 50.0},
    )

    rendered = (
        "{min_daily_battery_level} % / {typical_daily_distance} km"
        .format_map(help_values(coordinator))
    )

    assert rendered == "37 % / 62.5 km"


async def test_entity_help_updates_when_number_state_changes(hass: HomeAssistant, loaded_entry) -> None:
    entity_id = "number.cjp_test_min_daily_battery_level"
    help_entity_id = "switch.cjp_test_fill_up"

    for value in (41, 47):
        await hass.services.async_call(
            "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
        )
        await hass.async_block_till_done()
        assert f"{value} %" in hass.states.get(help_entity_id).attributes["help"]


def test_analysis_sections_include_controls_for_their_graphs() -> None:
    options = {**OPTIONS, "language": "da"}
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status", "sensor.eds"}
    dashboard = yaml.safe_load(render_dashboard(const.build_config(options), "tesla", "entry1", existing))
    sections = dashboard["views"][2]["sections"]

    def section_entities(title: str) -> set[str]:
        section = next(
            section
            for section in sections
            if any(card.get("heading") == title for card in _cards(section))
        )
        return {node["entity"] for node in _cards(section) if isinstance(node.get("entity"), str)}

    assert "switch.cjp_tesla_workplan_charging" not in section_entities("Klar til afgang")
    assert "switch.cjp_tesla_price_forecast_correction" in section_entities("Prisprognose")
    assert "select.cjp_tesla_forecast_type" in section_entities("Prognosens træfsikkerhed")
    assert "switch.cjp_tesla_auto_forecast_type" in section_entities("Prognosens træfsikkerhed")
    assert "switch.cjp_tesla_calculate_charging_loss" not in section_entities("Ladetab pr. effekt")
    assert "switch.cjp_tesla_use_measured_charging_loss" in section_entities("Ladetab pr. effekt")
    assert "button.cjp_tesla_recalculate_charging_loss" in section_entities("Ladetab pr. effekt")


def _price_charts(options: dict) -> list[str]:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status", "sensor.eds", "sensor.sl_price"}
    overview = yaml.safe_load(render_dashboard(const.build_config({**OPTIONS, "language": "da", **options}), "tesla", "entry1", existing))["views"][0]
    return [s["entity"] for c in _cards(overview) if c.get("type") == "custom:apexcharts-card" for s in c["series"][:1] if s.get("yaxis_id") == "price"]


def test_one_price_chart_stromligning_preferred() -> None:
    assert _price_charts({"energidataservice_buy_entity_id": "sensor.eds"}) == ["sensor.eds"]
    both = _price_charts({"energidataservice_buy_entity_id": "sensor.eds", "stromligning_buy_current_price": "sensor.sl_price"})
    assert both == ["sensor.sl_price"]


def test_price_chart_tooltip_series_share_one_timeline() -> None:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status", "sensor.eds", "sensor.sl_price"}
    for options in (
        {"energidataservice_buy_entity_id": "sensor.eds"},
        {"stromligning_buy_current_price": "sensor.sl_price"},
    ):
        overview = yaml.safe_load(render_dashboard(const.build_config({**OPTIONS, "language": "en", **options}), "tesla", "entry1", existing))["views"][0]
        chart = next(card for card in _cards(overview) if card.get("type") == "custom:apexcharts-card" and card.get("series", [{}])[0].get("yaxis_id") == "price")
        assert chart["apex_config"]["tooltip"]["enabledOnSeries"] == [0, 1, 2, 4]
        assert chart["apex_config"]["tooltip"]["followCursor"] is True
        assert chart["apex_config"]["tooltip"]["fixed"]["enabled"] is False
        custom = chart["apex_config"]["tooltip"]["custom"]
        assert "const indices = [0, 1, 2, 4];" in custom
        assert custom.strip().endswith("}")
        for index in (0, 1, 2, 4):
            generator = chart["series"][index]["data_generator"]
            assert "const times = [...new Set(" in generator
            assert "return times.map(time =>" in generator
        buy, _sell, solar = chart["series"][:3]
        assert "const hourKey = value =>" in buy["data_generator"]
        assert "hourlyValues" in buy["data_generator"]
        assert "return times.map(time => [time, solarByHour.get(hourKey(time)) || 0]);" in solar["data_generator"]
        planned = chart["series"][4]
        assert "const times = [...new Set(" in planned["data_generator"]
        assert "return times.map(time =>" in planned["data_generator"]
        assert planned["curve"] == "stepline"
        assert "data.push([start, 0]" not in planned["data_generator"]


def test_every_chart_has_a_shared_non_intersecting_tooltip() -> None:
    config = const.build_config({**OPTIONS, "language": "en"})
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status", "sensor.price"}
    content = yaml.safe_load(render_dashboard(config, "tesla", "entry1", existing))
    charts = [card for card in _cards(content) if card.get("type") == "custom:apexcharts-card"]
    assert charts
    for chart in charts:
        tooltip = chart["apex_config"]["tooltip"]
        assert tooltip["shared"] is True
        assert tooltip["intersect"] is False


def test_price_forecast_chart_series_share_the_same_hours() -> None:
    config = const.build_config({**OPTIONS, "language": "en"})
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    content = yaml.safe_load(render_dashboard(config, "tesla", "entry1", existing))
    chart = next(card for card in _cards(content) if card.get("graph_span") == "6d")
    assert len(chart["series"]) == 9
    for series in chart["series"]:
        generator = series["data_generator"]
        assert "const hours = ((entity.attributes.price_forecast || {}).hours || []);" in generator
        assert "return hours.map(h =>" in generator
        assert ".filter(" not in generator


def test_capacity_chart_series_share_the_same_months() -> None:
    config = const.build_config({**OPTIONS, "language": "en"})
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    content = yaml.safe_load(render_dashboard(config, "tesla", "entry1", existing))
    chart = next(card for card in _cards(content) if card.get("graph_span") == "1095d")
    assert [series["name"] for series in chart["series"]] == ["> 7 kW", "4-7 kW", "2,5-4 kW", "< 2,5 kW"]
    for series in chart["series"]:
        generator = series["data_generator"]
        assert "const months = [...new Set(rows.map(m => m.month))].sort();" in generator
        assert "return months.map(month =>" in generator
        assert "values.has(month) ? values.get(month) : null" in generator


def test_charging_loss_section_on_analysis() -> None:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    text = render_dashboard(const.build_config({**OPTIONS, "language": "da"}), "tesla", "entry1", existing)
    assert "@@" not in text
    analysis = yaml.safe_load(text)["views"][2]
    assert "Ladetab pr. effekt" in [c["heading"] for c in _cards(analysis) if c.get("type") == "heading"]
    charts = [c for c in _cards(analysis) if c.get("type") == "custom:apexcharts-card" and c.get("graph_span") == "12h"]
    # the loss by charging power (measurements and model) and what it is made of (fixed use + percentage loss)
    assert len(charts) == 2 and all(s["entity"] == "sensor.cjp_tesla_charging_loss_model" for c in charts for s in c["series"])
    assert [c["header"]["title"] for c in charts] == ["Tab mod ladeeffekt", "Hvad tabet består af"]
    assert charts[1]["stacked"] is True and [s["name"] for s in charts[1]["series"]] == ["Proportionalt resttab", "Fast forbrug"]
    buttons = [row["entity"] for c in _cards(analysis) if c.get("type") == "entities" for row in c["entities"]]
    assert "button.cjp_tesla_recalculate_charging_loss" in buttons
    assert "switch.cjp_tesla_calculate_charging_loss" not in buttons
    analysis_help = "\n".join(
        card.get("content", "") for card in _cards(analysis) if card.get("type") == "markdown"
    )
    assert "Brug målt ladetab" not in analysis_help
    # without a car integration there is nothing to measure: the section is gone
    no_ev_entities = _own("tesla") - {
        "sensor.cjp_tesla_charging_loss_model",
        "sensor.cjp_tesla_soc_charging_power_model",
        "button.cjp_tesla_recalculate_charging_loss",
        "switch.cjp_tesla_use_measured_charging_loss",
        "switch.cjp_tesla_learn_soc_charge_power",
    }
    no_ev_config = const.build_config({**OPTIONS, "language": "da", "usable_battery_level_entity_id": ""})
    no_ev = yaml.safe_load(render_dashboard(no_ev_config, "tesla", "entry1", no_ev_entities))
    assert "Ladetab pr. effekt" not in [c.get("heading") for c in _cards(no_ev["views"][2])]
    assert "Ladeeffekt efter SOC" not in [c.get("heading") for c in _cards(no_ev["views"][2])]


def test_soc_power_section_on_analysis() -> None:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    text = render_dashboard(const.build_config({**OPTIONS, "language": "da"}), "tesla", "entry1", existing)
    analysis = yaml.safe_load(text)["views"][2]
    section = next(
        section for section in analysis["sections"]
        if any(card.get("heading") == "Ladeeffekt efter SOC" for card in section["cards"])
    )
    explanation, switch, table, chart = section["cards"][1:]

    assert any(e["entity"] == "switch.cjp_learn_soc_charge_power" or e["entity"].endswith("learn_soc_charge_power") for e in switch["entities"])

    assert "Grafen og tabellen" in explanation["content"]
    assert "styrer ikke ladeboksen" in explanation["content"]
    assert "row.sample_sessions" in table["content"]
    assert "row.sessions" in table["content"]
    assert "row.cap_kw" in table["content"]
    assert [series["name"] for series in chart["series"]] == ["1 fase", "3 faser"]
    assert " %'" in chart["apex_config"]["xaxis"]["labels"]["formatter"]
    assert " %'" in chart["apex_config"]["tooltip"]["x"]["formatter"]


def test_economy_section_on_analysis() -> None:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    text = render_dashboard(const.build_config({**OPTIONS, "language": "da"}), "tesla", "entry1", existing)
    assert "@@" not in text
    analysis = yaml.safe_load(text)["views"][2]
    headings = [c["heading"] for c in _cards(analysis) if c.get("type") == "heading"]
    assert headings[0] == "Økonomi"
    charts = [c for c in _cards(analysis) if c.get("type") == "custom:apexcharts-card" and c["series"][0]["entity"] == "sensor.cjp_tesla_economy"]
    assert len(charts) == 2
    # the simulated subscription comparison lives on the What-if view, not under Economy
    analysis_text = yaml.safe_dump(analysis, allow_unicode=True)
    assert "subscription_monthly_price" not in analysis_text and "subscription_total" not in analysis_text
    assert "abonnementet ville have været billigere" not in analysis_text


def test_what_if_view_holds_the_subscription_comparison() -> None:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    text = render_dashboard(const.build_config({**OPTIONS, "language": "da"}), "tesla", "entry1", existing)
    assert "@@" not in text
    view = yaml.safe_load(text)["views"][3]
    assert view["path"] == "what-if" and view["title"] == "What-if" and view["icon"] == "mdi:flask-outline"
    headings = [c["heading"] for c in _cards(view) if c.get("type") == "heading"]
    assert headings == ["Daglig smart opladning til det maksimalt anbefalede niveau", "Reserve og maksimalt niveau", "Bilens solenergi erstattet af nettet", "Fast månedlig ladepris"]
    entities = [row["entity"] for c in _cards(view) if c.get("type") == "entities" for row in c["entities"]]
    assert entities == [
        "number.cjp_tesla_what_if_days", "number.cjp_tesla_what_if_target_battery_level",
        "number.cjp_tesla_what_if_start_battery_level", "select.cjp_tesla_what_if_day_selection",
        *(f"switch.cjp_tesla_what_if_day_{day}" for day in const.WEEKDAYS),
        "button.cjp_tesla_run_what_if",
        "number.cjp_tesla_what_if_days", "number.cjp_tesla_what_if_start_battery_level",
        "number.cjp_tesla_what_if_min_daily_battery_level", "number.cjp_tesla_what_if_max_recommended_battery_level",
        "button.cjp_tesla_run_what_if_settings", "number.cjp_tesla_subscription_monthly_price",
    ]
    markdown = "\n".join(c.get("content", "") for c in _cards(view) if c.get("type") == "markdown")
    # Read more covers the simulated nature, the price field, completed months and the recorded-history limit
    assert "ændrer aldrig den aktuelle plan" in markdown
    assert "0 slår det fra" in markdown and "0 slår sammenligningen fra" in markdown
    assert "Kun afsluttede måneder sammenlignes" in markdown
    assert "manglende historik kan gøre månedsomkostningen for lav" in markdown
    assert "indtast ikke hele prisen på flere biler" in markdown
    # the table
    assert "Fast månedlig ladepris" in markdown
    assert "En positiv forskel betyder, at abonnementet ville have været billigere." in markdown
    assert "Kun sessioner registreret i CJP indgår" in markdown
    assert "subscription_total" in markdown and "subscription_saving" in markdown
    assert "Indtast en fast månedlig ladepris ovenfor" in markdown


def test_what_if_subscription_template_branches() -> None:
    """Run the What-if table template for: off, on without months, current month only, completed months."""
    import re
    from types import SimpleNamespace

    from jinja2 import Environment

    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    view = yaml.safe_load(render_dashboard(const.build_config({**OPTIONS, "language": "da"}), "tesla", "entry1", existing))["views"][3]
    template = next(c["content"] for c in _cards(view) if c.get("type") == "markdown" and "subscription_total" in c.get("content", ""))
    sensor = re.search(r"states\.sensor\.(\w+)\.attributes", template).group(1)

    def render(attributes: dict) -> str:
        states = SimpleNamespace(sensor=SimpleNamespace(**{sensor: SimpleNamespace(attributes=attributes)}))
        return Environment().from_string(template).render(states=states)

    off = {"subscription_total": {"enabled": False}, "months": []}
    assert "Indtast en fast månedlig ladepris ovenfor" in render(off)
    assert "Indtast en fast månedlig ladepris ovenfor" in render({})  # attributes missing
    on_empty = render({"subscription_total": {"enabled": True, "comparable_months": 0}, "months": []})
    assert "ingen registrerede ladeforløb" in on_empty and "Indtast en fast" not in on_empty

    current_only = render({
        "subscription_total": {"enabled": True, "comparable_months": 0},
        "months": [{"month": "2026-11", "cost": 12.4, "subscription_status": "current"}],
    })
    assert "Indeværende måned er ikke afsluttet" in current_only and "Sammenlignelige måneder" not in current_only

    full = render({
        "subscription_total": {"enabled": True, "comparable_months": 1, "recorded_cost": 150.0, "price": 100.0, "saving": 50.0},
        "months": [
            {"month": "2026-11", "cost": 12.4, "subscription_status": "current"},
            {"month": "2026-10", "cost": 150.0, "subscription_status": "recorded", "subscription_recorded_cost": 150.0, "subscription_price": 100.0, "subscription_saving": 50.0},
            {"month": "2026-09", "cost": None, "subscription_status": "incomplete"},
        ],
    })
    assert re.search(r"\| \*\*2026-10\*\* \| 150(\.0)? kr \| 100(\.0)? kr \| 50(\.0)? kr \|", full)
    assert "Ufuldstændig ladehistorik" in full
    assert "Sammenlignelige måneder (1):" in full


def test_analysis_sections() -> None:
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    text = render_dashboard(const.build_config({**OPTIONS, "language": "da"}), "tesla", "entry1", existing)
    assert "@@" not in text
    headings = [c["heading"] for c in _cards(yaml.safe_load(text)["views"][2]) if c.get("type") == "heading"]
    for heading in ("Ladeeffekt efter SOC", "Kørsel og temperatur", "Klar til afgang", "Lært kørselsafstand", "Planlagt mod faktisk ladet", "Batterikapacitet"):
        assert heading in headings


def test_learned_distance_section_shows_weekday_names() -> None:
    """Entered against learned distance per weekday name (not dates), plus the typical daily distance."""
    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    analysis = yaml.safe_load(render_dashboard(const.build_config({**OPTIONS, "language": "da"}), "tesla", "entry1", existing))["views"][2]
    section = next(s for s in analysis["sections"] if any(c.get("heading") == "Lært kørselsafstand" for c in s["cards"]))
    table = section["cards"][2]["content"]
    for day in ("Mandag", "Tirsdag", "Onsdag", "Torsdag", "Fredag", "Lørdag", "Søndag", "Typisk daglig afstand"):
        assert day in table
    assert "number.cjp_tesla_workday_distance_needed_" in table and "number.cjp_tesla_typical_daily_distance" in table
    assert "switch.cjp_tesla_learn_driving_distance" in table and "'learned_distance'" in table
    assert "Indtastet (km)" in table and "Lært (km)" in table
    assert "**Dage**: antal målte dage" in section["cards"][1]["content"]


def test_analysis_sections_without_a_car() -> None:
    """The price and house consumption forecasts and the plan need no car; the drives and departures do."""
    no_ev_entities = {f"sensor.cjp_tesla_{key}" for key, info in const.SENSOR_DEFS.items() if info[3] == "ev"}
    existing = (_own("tesla") - no_ev_entities) | {"sensor.charger_status", "sensor.eds"}
    config = const.build_config({**OPTIONS, "language": "da", "usable_battery_level_entity_id": "", "energidataservice_buy_entity_id": "sensor.eds"})
    analysis = yaml.safe_load(render_dashboard(config, "tesla", "entry1", existing))["views"][2]
    headings = [c["heading"] for c in _cards(analysis) if c.get("type") == "heading"]
    for heading in ("Prisprognose", "Prognosens træfsikkerhed", "Planlagt mod faktisk ladet"):
        assert heading in headings
    assert "Kørsel og temperatur" not in headings and "Klar til afgang" not in headings and "Lært kørselsafstand" not in headings
    contents = [c.get("content", "") for c in _cards(analysis) if c.get("type") == "markdown"]
    assert any("attributes.forecast or {}" in content and "Husets forbrug pr. time i kWh" in content for content in contents)


async def test_sidebar_dashboard_can_be_edited_and_reset(hass: HomeAssistant, hass_ws_client, tmp_path) -> None:
    hass.config.config_dir = str(tmp_path)
    set_source_states(hass)
    entry = make_entry(options={**MOCK_OPTIONS, "dashboard": True})
    await async_setup(hass, entry)
    client = await hass_ws_client(hass)

    async def command(message: dict) -> dict:
        await client.send_json_auto_id({"url_path": "cjp-test", **message})
        response = await client.receive_json()
        assert response["success"], response
        return response["result"]

    generated = await command({"type": "lovelace/config"})
    assert generated["views"]
    # storage mode: the frontend offers the edit UI and the raw configuration editor
    panel = hass.data["frontend_panels"]["cjp-test"]
    assert panel.config == {"mode": "storage"}

    await command({"type": "lovelace/config/save", "config": {"views": [{"title": "Mine"}]}})
    assert await command({"type": "lovelace/config"}) == {"views": [{"title": "Mine"}]}

    # the edits survive a reload, the generated dashboard is not shown over them
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert await command({"type": "lovelace/config"}) == {"views": [{"title": "Mine"}]}

    await hass.services.async_call("button", "press", {"entity_id": f"button.{OBJECT_PREFIX}_reset_dashboard"}, blocking=True)
    assert await command({"type": "lovelace/config"}) == generated
    await async_unload_and_join(hass, entry)


async def test_sidebar_icon_follows_the_dashboard_icon_option(hass: HomeAssistant, tmp_path) -> None:
    hass.config.config_dir = str(tmp_path)
    set_source_states(hass)
    entry = make_entry(options={**MOCK_OPTIONS, "dashboard": True})
    await async_setup(hass, entry)
    # no choice yet: the default icon
    assert hass.data["frontend_panels"]["cjp-test"].sidebar_icon == "mdi:ev-station"

    hass.config_entries.async_update_entry(entry, options={**entry.options, "dashboard_icon": "mdi:car-electric"})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.data["frontend_panels"]["cjp-test"].sidebar_icon == "mdi:car-electric"
    assert hass.data[LOVELACE_DATA].dashboards["cjp-test"].config["icon"] == "mdi:car-electric"

    # a stored value that is not an icon name falls back to the default instead of breaking the sidebar
    hass.config_entries.async_update_entry(entry, options={**entry.options, "dashboard_icon": "not an icon"})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.data["frontend_panels"]["cjp-test"].sidebar_icon == "mdi:ev-station"
    await async_unload_and_join(hass, entry)
