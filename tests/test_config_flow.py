"""Config flow (car/charger instance and shared grid connection) and options flows."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from homeassistant import config_entries
from homeassistant.components.energy.data import async_get_manager
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.cable_juice_planner.const import CONF_CIRCUIT, CONF_CIRCUITS, CONF_ENTITY_PREFIX, CONF_SITE, DOMAIN
from custom_components.cable_juice_planner.const.config import build_config, default_options
from custom_components.cable_juice_planner.flow.pages import SOLAR_PAGES
from custom_components.cable_juice_planner.flow import pages
from custom_components.cable_juice_planner.flow.detection import Detection
from custom_components.cable_juice_planner.management.backup import BACKUP_DIR
from custom_components.cable_juice_planner.utils.formatting import format_charging_rule

from .conftest import (
    CHARGER_OPTIONS,
    CONSUMPTION,
    EV_LEVEL,
    EV_ODOMETER,
    EV_RANGE,
    MOCK_OPTIONS,
    PRICE,
    async_setup,
    form_defaults,
    make_entry,
    make_site_entry,
    register_charger,
    set_source_states,
    providers_input,
)

FIXTURES = Path(__file__).parent / "fixtures"
CHARGER_ENTITIES = {key: value for key, value in CHARGER_OPTIONS.items() if key.endswith("_entity_id")}


async def _start_instance(hass: HomeAssistant, prefix: str = "flow", **extra):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["instance", "site"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "instance"})
    assert result["step_id"] == "instance"
    return await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ENTITY_PREFIX: prefix, "language": "da", **extra})


async def _configure(hass: HomeAssistant, result, step_id: str, user_input: dict):
    if step_id == "solar" and result.get("step_id") == "solar_forecast_source":
        # the page before: the forecast the test is about to choose a sensor of
        source = "solcast_solar" if user_input.get("forecast_entity_id") else "none"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"solar_forecast_source": source})
        if "solar_forecast_source" in user_input:  # made from the page before: the solar page's own defaults, with what the test sets
            user_input = {**form_defaults(result), **{key: value for key, value in user_input.items() if key != "solar_forecast_source"}}
    assert result["type"] is FlowResultType.FORM, result
    # the solar page has a step id of its own for each forecast
    other = (*SOLAR_PAGES.values(), "solar_solcast_ready") if step_id == "solar" else ()
    if step_id in ("prices_stromligning", "prices_stromligning_tariffs"):
        other = (f"{step_id}_ready", f"{step_id}_missing")
    assert result["step_id"] == step_id or result["step_id"] in other, result
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


async def _submit_defaults_until(hass: HomeAssistant, result, step_id: str | None):
    """Press submit on every page until step_id (None: to the end)."""
    while result["type"] is FlowResultType.FORM and result["step_id"] != step_id:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], form_defaults(result))
    return result


async def test_full_flow_creates_entry(hass: HomeAssistant) -> None:
    hass.config.country = "SE"
    result = await _start_instance(hass, dashboard_icon="mdi:car-electric")
    result = await _configure(hass, result, "charger", CHARGER_ENTITIES)
    result = await _configure(hass, result, "charger_specs", {"power_voltage": 230, "charging_phases": 3, "charging_max_amp": 16, "charging_loss": -5})
    # no site entry -> no load balancing page
    result = await _configure(
        hass, result, "ev",
        {"usable_battery_level_entity_id": EV_LEVEL, "estimated_battery_range_entity_id": EV_RANGE, "odometer_entity_id": EV_ODOMETER},
    )
    result = await _configure(hass, result, "ev_specs", {"battery_size": 75, "last_charging_hour_before_departure_buffer": 1})
    result = await _configure(hass, result, "home", {"power_consumption_entity_id": CONSUMPTION})
    result = await _configure(hass, result, "powerwall", form_defaults(result))
    result = await _configure(hass, result, "country", {})
    result = await _configure(hass, result, "price_provider", providers_input("energidataservice"))
    result = await _configure(hass, result, "prices_energidataservice", {"energidataservice_buy_entity_id": PRICE})
    result = await _configure(hass, result, "price_fees", {"refund": 0.0, "sell_fee": 0.03})
    # no production entity -> no solar charging page; the weather page is shown without solar too
    result = await _configure(hass, result, "solar", form_defaults(result))
    result = await _configure(hass, result, "weather", {"outdoor_temp_entity_id": "sensor.outdoor"})
    result = await _configure(hass, result, "notifications", form_defaults(result))
    result = await _configure(hass, result, "advanced", {**form_defaults(result), "dashboard": False})
    result = await _configure(hass, result, "database", form_defaults(result))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Cable Juice Planner (flow)"
    assert result["data"][CONF_ENTITY_PREFIX] == "flow"
    options = result["options"]
    assert options["price_country"] == "SE"  # a new entry: the country of Home Assistant is suggested and stored
    assert options["status_entity_id"] == CHARGER_OPTIONS["status_entity_id"]
    assert options["charging_loss"] == -0.05  # shown in %, stored as a fraction
    assert options["usable_battery_level_entity_id"] == EV_LEVEL
    assert options["dashboard"] is False
    assert options["dashboard_icon"] == "mdi:car-electric"
    assert options["outdoor_temp_entity_id"] == "sensor.outdoor"


async def _to_the_price_pages(hass: HomeAssistant):
    """A new car up to the price provider page (like test_full_flow_creates_entry)."""
    result = await _start_instance(hass)
    result = await _configure(hass, result, "charger", CHARGER_ENTITIES)
    result = await _configure(hass, result, "charger_specs", {"power_voltage": 230, "charging_phases": 3, "charging_max_amp": 16, "charging_loss": -5})
    result = await _configure(
        hass, result, "ev",
        {"usable_battery_level_entity_id": EV_LEVEL, "estimated_battery_range_entity_id": EV_RANGE, "odometer_entity_id": EV_ODOMETER},
    )
    result = await _configure(hass, result, "ev_specs", {"battery_size": 75, "last_charging_hour_before_departure_buffer": 1})
    result = await _configure(hass, result, "home", {"power_consumption_entity_id": CONSUMPTION})
    result = await _configure(hass, result, "powerwall", form_defaults(result))
    result = await _configure(hass, result, "country", {})
    result = await _configure(hass, result, "price_provider", providers_input("energidataservice"))
    return await _configure(hass, result, "prices_energidataservice", {"energidataservice_buy_entity_id": PRICE})


@pytest.mark.parametrize(
    ("unit", "scale", "fee"),
    [("DKK/kWh", 1.0, 0.03), ("EUR/kWh", 0.134, 0.004), ("kr/kWh", 1.0, 0.03), ("XYZ/kWh", None, 0.03), (None, None, 0.03)],
)
async def test_a_new_entry_gets_the_price_scale_of_its_price_sensor(hass: HomeAssistant, unit, scale, fee) -> None:
    """The scale comes from the unit of the price sensor, not from Home Assistant's currency (EUR by default)."""
    hass.states.async_set(PRICE, "0.5", {"unit_of_measurement": unit} if unit else {})
    result = await _to_the_price_pages(hass)
    assert result["step_id"] == "price_fees"
    assert form_defaults(result)["sell_fee"] == fee  # the suggestion is 0.03 kroner in the price currency
    result = await _submit_defaults_until(hass, result, None)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    options = result["options"]
    assert options.get("price_scale", 1.0) == (scale or 1.0)
    assert options["sell_fee"] == fee


async def test_a_sell_fee_the_user_changed_is_kept_when_the_scale_is_found(hass: HomeAssistant) -> None:
    hass.states.async_set(PRICE, "0.5", {"unit_of_measurement": "EUR/kWh"})
    result = await _to_the_price_pages(hass)
    result = await _configure(hass, result, "price_fees", {"refund": 0.0, "sell_fee": 0.05})
    result = await _submit_defaults_until(hass, result, None)
    assert result["options"]["price_scale"] == 0.134 and result["options"]["sell_fee"] == 0.05


async def _to_the_epf_page(hass: HomeAssistant):
    result = await _start_instance(hass)
    result = await _configure(hass, result, "charger", CHARGER_ENTITIES)
    result = await _configure(hass, result, "charger_specs", {"power_voltage": 230, "charging_phases": 3, "charging_max_amp": 16, "charging_loss": -5})
    result = await _configure(
        hass, result, "ev",
        {"usable_battery_level_entity_id": EV_LEVEL, "estimated_battery_range_entity_id": EV_RANGE, "odometer_entity_id": EV_ODOMETER},
    )
    result = await _configure(hass, result, "ev_specs", {"battery_size": 75, "last_charging_hour_before_departure_buffer": 1})
    result = await _configure(hass, result, "home", {"power_consumption_entity_id": CONSUMPTION})
    result = await _configure(hass, result, "powerwall", form_defaults(result))
    result = await _configure(hass, result, "country", {})
    return await _configure(hass, result, "price_provider", providers_input("energypriceforecast"))


async def test_energy_price_forecast_page_refuses_cents_and_a_wholesale_buy_sensor_and_sets_the_scale(hass: HomeAssistant) -> None:
    retail = {"unit_of_measurement": "EUR/kWh", "raw_today": [], "retail_source": "estimate"}
    hass.states.async_set("sensor.epf_retail", "0.2", retail)
    hass.states.async_set("sensor.epf_cents", "20", {**retail, "unit_of_measurement": "ct/kWh"})
    hass.states.async_set("sensor.epf_wholesale", "0.1", {"unit_of_measurement": "EUR/kWh", "raw_today": []})
    hass.states.async_set("sensor.epf_wholesale_2", "0.1", {"unit_of_measurement": "EUR/kWh", "raw_today": []})
    result = await _to_the_epf_page(hass)
    assert result["step_id"] == "prices_energypriceforecast"
    sell = {"energypriceforecast_sell_entity_id": "sensor.epf_wholesale"}
    # the sell sensor is required: a retail price is more than a sale pays, so it is not taken for one
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(result["flow_id"], {"energypriceforecast_buy_entity_id": "sensor.epf_retail"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"energypriceforecast_buy_entity_id": "sensor.epf_cents", **sell})
    assert result["step_id"] == "prices_energypriceforecast" and result["errors"] == {"energypriceforecast_buy_entity_id": "price_unit_subunit"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"energypriceforecast_buy_entity_id": "sensor.epf_wholesale", "energypriceforecast_sell_entity_id": "sensor.epf_wholesale_2"})
    assert result["errors"] == {"energypriceforecast_buy_entity_id": "price_sensor_not_retail"}
    # the retail price is more than a sale pays: it is refused as the sell sensor
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"energypriceforecast_buy_entity_id": "sensor.epf_retail", "energypriceforecast_sell_entity_id": "sensor.epf_retail"}
    )
    assert result["errors"] == {"energypriceforecast_sell_entity_id": "price_sensor_not_wholesale"}
    result = await _configure(
        hass, result, "prices_energypriceforecast", {"energypriceforecast_buy_entity_id": "sensor.epf_retail", "energypriceforecast_sell_entity_id": "sensor.epf_wholesale"}
    )
    assert result["step_id"] == "price_fees" and form_defaults(result)["sell_fee"] == 0.004
    result = await _submit_defaults_until(hass, result, None)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    options = result["options"]
    assert options["price_scale"] == 0.134
    assert options["energypriceforecast_buy_entity_id"] == "sensor.epf_retail" and options["energypriceforecast_sell_entity_id"] == "sensor.epf_wholesale"
    assert options["energidataservice_buy_entity_id"] == ""


async def test_options_flow_finds_the_price_scale_and_keeps_it_without_a_unit(hass: HomeAssistant, loaded_entry) -> None:
    async def save():
        result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "menu_ev"})
        result = await hass.config_entries.options.async_configure(result["flow_id"], form_defaults(result))
        result = await hass.config_entries.options.async_configure(result["flow_id"], form_defaults(result))
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done(wait_background_tasks=True)

    price_entity = loaded_entry.options["energidataservice_buy_entity_id"]
    hass.states.async_set(price_entity, "0.5", {"unit_of_measurement": "GBP/kWh"})
    await save()
    assert loaded_entry.options["price_scale"] == 0.115
    sell_fee = loaded_entry.options["sell_fee"]
    # a sensor without a known unit (not loaded yet at startup): the entry keeps its scale
    hass.states.async_set(price_entity, "unavailable", {})
    await save()
    assert loaded_entry.options["price_scale"] == 0.115
    assert loaded_entry.options["sell_fee"] == sell_fee  # an existing entry's fee is never rescaled


async def test_ev_entities_all_or_nothing(hass: HomeAssistant) -> None:
    result = await _start_instance(hass)
    result = await _configure(hass, result, "charger", CHARGER_ENTITIES)
    result = await _configure(hass, result, "charger_specs", form_defaults(result))
    result = await _configure(hass, result, "ev", {"usable_battery_level_entity_id": EV_LEVEL})
    assert result["step_id"] == "ev"
    assert result["errors"] == {"base": "ev_incomplete"}
    # the battery level simulation needs the car integration
    result = await _configure(hass, result, "ev", {"simulate_battery_level_while_charging": True})
    assert result["step_id"] == "ev"
    assert result["errors"] == {"simulate_battery_level_while_charging": "simulation_needs_ev"}
    # charger only (no car integration) is allowed (the form keeps the switch as entered)
    result = await _configure(hass, result, "ev", {"simulate_battery_level_while_charging": False})
    assert result["step_id"] == "ev_specs"


async def test_solar_charging_min_above_max(hass: HomeAssistant) -> None:
    result = await _start_instance(hass)
    result = await _submit_defaults_until(hass, await _configure(hass, result, "charger", CHARGER_ENTITIES), "ev")
    result = await _configure(hass, result, "ev", {})
    result = await _submit_defaults_until(hass, result, "price_provider")
    result = await _configure(hass, result, "price_provider", providers_input("energidataservice"))
    result = await _configure(hass, result, "prices_energidataservice", {"energidataservice_buy_entity_id": PRICE})
    result = await _configure(hass, result, "price_fees", form_defaults(result))
    result = await _configure(hass, result, "solar", {**form_defaults(result), "production_entity_id": "sensor.solar_production"})
    result = await _configure(
        hass, result, "solar_charging",
        {**form_defaults(result), "charging_single_phase_min_amp": 16, "charging_single_phase_max_amp": 6},
    )
    assert result["errors"] == {"charging_single_phase_min_amp": "min_above_max"}


async def test_reserved_and_duplicate_prefix(hass: HomeAssistant) -> None:
    result = await _start_instance(hass, prefix="site")
    assert result["errors"] == {CONF_ENTITY_PREFIX: "invalid_prefix"}

    make_entry("taken").add_to_hass(hass)
    result = await _start_instance(hass, prefix="taken")
    assert result["errors"] == {CONF_ENTITY_PREFIX: "prefix_in_use"}


async def test_old_prefix_with_cjp_is_normalized(hass: HomeAssistant) -> None:
    result = await _start_instance(hass, prefix="cjp_tesla")
    assert result["step_id"] == "charger"
    assert result["description_placeholders"]["prefix"] == "tesla"


async def test_prefix_taken_by_pyscript(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.cjp_flow_overview", "PyScript")
    result = await _start_instance(hass)
    assert result["errors"] == {CONF_ENTITY_PREFIX: "prefix_entities_exist"}


async def test_site_flow_once(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "site"})
    assert result["step_id"] == "site"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**form_defaults(result), "fuse_l1": 20, "urgent_trip_hours": 6})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_SITE: True}
    assert result["options"]["fuse_l1"] == 20.0
    assert result["options"]["urgent_trip_hours"] == 6
    await hass.async_block_till_done()

    # with a site the menu is skipped and the car flow shows the load balancing page
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "instance"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ENTITY_PREFIX: "second", "language": "da"})
    result = await _configure(hass, result, "charger", CHARGER_ENTITIES)
    result = await _configure(hass, result, "charger_specs", form_defaults(result))
    assert result["step_id"] == "load_balancing"


async def test_import_legacy_pyscript_config(hass: HomeAssistant, tmp_path: Path) -> None:
    """A PyScript ev_config.yaml in the config dir is offered and its values prefill the pages."""
    shutil.copy(FIXTURES / "ev_config.yaml", tmp_path / "ev_config.yaml")
    hass.config.config_dir = str(tmp_path)
    hass.config.country = "DE"
    result = await _start_instance(hass, prefix="ev")
    assert result["step_id"] == "import_legacy"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"import_source": "ev"})
    assert result["step_id"] == "charger"
    assert form_defaults(result)["status_entity_id"] == "sensor.ev_charger_status"
    # the PyScript file still has the old, misspelled key of the charger power sensor
    assert form_defaults(result)["charger_power_entity_id"] == "sensor.ev_charger_power"

    result = await _submit_defaults_until(hass, result, None)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["legacy_prefix"] == "ev"
    assert result["data"]["initial_settings"]["min_daily_battery_level"] == 35.0
    options = result["options"]
    assert options["power_voltage"] == 240.0
    assert options["battery_size"] == 69.0
    assert options["charge_port_door_entity_id"] == "cover.my_car_charge_port_door"
    assert options["notify_list"] == ["notify.mobile_app_phone"]
    assert options["language"] == "da"
    assert options["price_country"] == "DK"  # an import keeps its own: Denmark, not the country of Home Assistant
    # the removed testing mode in the legacy file (testing_mode: true) is ignored, not imported
    assert "testing_mode" not in options
    assert options["charger_power_entity_id"] == "sensor.ev_charger_power"
    assert "power_consumtion_entity_id" not in options


ENERGY_SOLAR = "sensor.energy_solar_power"
ENERGY_BATTERY = "sensor.energy_battery_power"
POWERWALL_LEVEL = "sensor.powerwall_battery_level"


async def _energy_dashboard(hass: HomeAssistant) -> None:
    """An Energy dashboard with one solar and one battery source, each with a plain power sensor in W."""
    hass.config.components.add("energy")
    for entity_id in (ENERGY_SOLAR, ENERGY_BATTERY):
        hass.states.async_set(entity_id, "1000", {"unit_of_measurement": "W", "device_class": "power"})
    manager = await async_get_manager(hass)
    manager.data = {
        "energy_sources": [
            {"type": "solar", "stat_energy_from": "sensor.solar_energy", "stat_rate": ENERGY_SOLAR},
            {
                "type": "battery",
                "stat_energy_from": "sensor.battery_in",
                "stat_energy_to": "sensor.battery_out",
                "stat_rate": ENERGY_BATTERY,
            },
        ],
        "device_consumption": [],
    }


def _suggested(result, key: str):
    """The suggested value of a field of the form (what the frontend shows, not a default)."""
    for marker in result["data_schema"].schema:
        if str(marker) == key:
            return (marker.description or {}).get("suggested_value")
    raise AssertionError(f"{key} is not on the form")


class _DetectionStub:
    """Stands in for the recorder: counts the calls and answers nothing is known."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def __call__(self, hass, consumption, power, level, trusted=None, now=None, ignored_entities=()) -> Detection:
        self.calls.append((consumption, power, level, trusted))
        return Detection()


@pytest.fixture
def detection(monkeypatch) -> _DetectionStub:
    stub = _DetectionStub()
    monkeypatch.setattr(pages, "async_detect", stub)
    return stub


async def _to_powerwall(hass: HomeAssistant, result=None):
    """From the start (or from the given restore page) to the home battery page; the battery power sensor is still only suggested."""
    if result is None:
        result = await _start_instance(hass)
    result = await _configure(hass, result, "charger", CHARGER_ENTITIES)
    result = await _configure(hass, result, "charger_specs", {"power_voltage": 230, "charging_phases": 3, "charging_max_amp": 16, "charging_loss": -5})
    result = await _configure(
        hass, result, "ev",
        {"usable_battery_level_entity_id": EV_LEVEL, "estimated_battery_range_entity_id": EV_RANGE, "odometer_entity_id": EV_ODOMETER},
    )
    result = await _configure(hass, result, "ev_specs", {"battery_size": 75, "last_charging_hour_before_departure_buffer": 1})
    return await _configure(hass, result, "home", {"power_consumption_entity_id": CONSUMPTION})


async def _past_forecast_source(hass: HomeAssistant, result, source: str | None = None):
    """Through the page that asks for the solar forecast (its default when no source is given) to the solar page."""
    assert result["step_id"] == "solar_forecast_source", result
    chosen = {"solar_forecast_source": source} if source else form_defaults(result)
    return await hass.config_entries.flow.async_configure(result["flow_id"], chosen)


async def _to_solar(hass: HomeAssistant, result, source: str | None = None) -> dict:
    """From the home battery page to the solar page, keeping the suggested battery power sensor."""
    result = await _configure(hass, result, "powerwall", {**form_defaults(result), "powerwall_battery_level_entity_id": POWERWALL_LEVEL})
    result = await _configure(hass, result, "detection_review", form_defaults(result))
    result = await _configure(hass, result, "country", {})
    result = await _configure(hass, result, "price_provider", providers_input("energidataservice"))
    result = await _configure(hass, result, "prices_energidataservice", {"energidataservice_buy_entity_id": PRICE})
    result = await _configure(hass, result, "price_fees", {"refund": 0.0, "sell_fee": 0.03})
    return await _past_forecast_source(hass, result, source)


async def test_the_energy_dashboard_pre_fills_the_battery_and_solar_power(hass: HomeAssistant, detection) -> None:
    await _energy_dashboard(hass)
    result = await _to_powerwall(hass)
    assert _suggested(result, "powerwall_watt_flow_entity_id") == ENERGY_BATTERY
    result = await _to_solar(hass, result)
    assert result["step_id"] == "solar"
    assert _suggested(result, "production_entity_id") == ENERGY_SOLAR


async def test_a_pre_filled_solar_sensor_can_be_cleared_and_stays_empty(hass: HomeAssistant, detection) -> None:
    await _energy_dashboard(hass)
    result = await _to_solar(hass, await _to_powerwall(hass))
    assert _suggested(result, "production_entity_id") == ENERGY_SOLAR
    values = form_defaults(result)
    values.pop("production_entity_id")  # the user clears the field: an optional field left out of the submitted form
    result = await hass.config_entries.flow.async_configure(result["flow_id"], values)
    result = await _configure(hass, result, "weather", form_defaults(result))  # shown without solar too
    result = await _configure(hass, result, "notifications", form_defaults(result))
    result = await _configure(hass, result, "advanced", {**form_defaults(result), "dashboard": False})
    result = await _configure(hass, result, "database", form_defaults(result))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"]["production_entity_id"] == ""
    assert result["options"]["powerwall_watt_flow_entity_id"] == ENERGY_BATTERY


async def test_the_battery_sign_is_still_read_from_the_recorder_for_a_pre_filled_sensor(hass: HomeAssistant, detection) -> None:
    await _energy_dashboard(hass)
    await _to_solar(hass, await _to_powerwall(hass))
    # the history is looked at for the pre-filled battery sensor, and nothing is trusted from before (the sign is unknown)
    assert detection.calls == [(CONSUMPTION, ENERGY_BATTERY, POWERWALL_LEVEL, None)]


async def test_the_pyscript_import_wins_over_the_energy_dashboard(hass: HomeAssistant, tmp_path: Path) -> None:
    shutil.copy(FIXTURES / "ev_config.yaml", tmp_path / "ev_config.yaml")
    hass.config.config_dir = str(tmp_path)
    await _energy_dashboard(hass)
    result = await _start_instance(hass, prefix="ev")
    assert result["step_id"] == "import_legacy"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"import_source": "ev"})
    assert result["step_id"] == "charger"
    result = await _submit_defaults_until(hass, result, "powerwall")
    # the imported home battery sensor is empty: the suggestion of the dashboard is not shown
    assert _suggested(result, "powerwall_watt_flow_entity_id") in (None, "")
    result = await _submit_defaults_until(hass, result, None)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"]["production_entity_id"] == "sensor.inverter_input_power"
    assert result["options"]["powerwall_watt_flow_entity_id"] == ""


BACKUP_BATTERY = "sensor.backup_battery_power"


def _write_backup(config_dir: Path, options: dict) -> str:
    """A Cable Juice Planner backup in the backup folder of the test config; returns its file name."""
    folder = config_dir / BACKUP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    backup = {"integration": DOMAIN, "title": "CJP test", "created": "2026-11-01T10:00:00+01:00", "options": options, "databases": {}}
    (folder / "cjp_backup_test.json").write_text(json.dumps(backup), encoding="utf-8")
    return "cjp_backup_test.json"


async def _finish_without_solar(hass: HomeAssistant, result: dict) -> dict:
    """From the solar page to the end, with the solar sensor left out (the user clears the field)."""
    values = form_defaults(result)
    values.pop("production_entity_id", None)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], values)
    result = await _configure(hass, result, "weather", form_defaults(result))  # shown without solar too
    result = await _configure(hass, result, "notifications", form_defaults(result))
    result = await _configure(hass, result, "advanced", {**form_defaults(result), "dashboard": False})
    return await _configure(hass, result, "database", form_defaults(result))


async def test_a_restored_backup_wins_over_the_energy_dashboard(hass: HomeAssistant, tmp_path: Path, detection) -> None:
    """The backup replaces the options after the pre-fill: its empty solar field stays empty, its battery sensor is kept."""
    hass.config.config_dir = str(tmp_path)
    await _energy_dashboard(hass)
    name = _write_backup(tmp_path, {"production_entity_id": "", "powerwall_watt_flow_entity_id": BACKUP_BATTERY})
    result = await _start_instance(hass)
    assert result["step_id"] == "restore_backup"
    result = await _configure(hass, result, "restore_backup", {"backup": name})
    result = await _to_powerwall(hass, result)
    assert _suggested(result, "powerwall_watt_flow_entity_id") == BACKUP_BATTERY
    result = await _to_solar(hass, result)
    assert result["step_id"] == "solar"
    assert _suggested(result, "production_entity_id") in (None, "")
    result = await _finish_without_solar(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["restore_backup"] == name
    assert result["options"]["production_entity_id"] == ""
    assert result["options"]["powerwall_watt_flow_entity_id"] == BACKUP_BATTERY


def test_the_solar_forecast_picker_offers_the_three_integrations() -> None:
    """One filter per integration (the picker offers an entity that matches any of them), still a single sensor picker."""
    from custom_components.cable_juice_planner.flow.schema import SELECTORS

    config = SELECTORS["forecast_entity_id"].config
    assert [item["integration"] for item in config["filter"]] == ["solcast_solar", "forecast_solar", "helios_forecast"]
    assert all(item["domain"] == ["sensor"] for item in config["filter"]) and not config.get("multiple")
    # the selector does not check the value against the filter: an old Solcast option (and a backup or a PyScript
    # import with one) is still valid, as are the new integrations' sensors
    for value in ("sensor.solcast_pv_forecast_forecast_today", "sensor.helios_forecast_power_now", "sensor.energy_production_today"):
        assert SELECTORS["forecast_entity_id"](value) == value
    # the other pickers are built as before
    assert SELECTORS["energidataservice_buy_entity_id"].config["filter"] == [{"domain": ["sensor"], "integration": "energidataservice"}]
    assert SELECTORS["production_entity_id"].config["filter"][0].get("integration") is None


@pytest.mark.parametrize("forecast", ["sensor.helios_forecast_power_now", "sensor.energy_production_today"])
async def test_a_forecast_solar_or_helios_sensor_is_accepted(hass: HomeAssistant, detection, forecast: str) -> None:
    """The solar page takes these sensors and stores the option as it is; a forecast still needs the production sensor."""
    await _energy_dashboard(hass)
    source = "helios_forecast" if "helios" in forecast else "forecast_solar"
    result = await _to_solar(hass, await _to_powerwall(hass), source)
    assert result["step_id"] == SOLAR_PAGES[source]
    values = {**form_defaults(result), "forecast_entity_id": forecast}
    values.pop("production_entity_id", None)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], values)
    assert result["step_id"] == SOLAR_PAGES[source] and result["errors"] == {"production_entity_id": "production_required"}
    hass.states.async_set("sensor.solar_power", "0", {"unit_of_measurement": "W", "device_class": "power"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**values, "production_entity_id": "sensor.solar_power"})
    assert result["step_id"] not in SOLAR_PAGES.values() and not result.get("errors")
    result = await _submit_defaults_until(hass, result, "advanced")
    result = await _configure(hass, result, "advanced", {**form_defaults(result), "dashboard": False})
    result = await _submit_defaults_until(hass, result, None)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"]["forecast_entity_id"] == forecast


async def test_a_sensor_of_another_car_is_still_suggested(hass: HomeAssistant, detection) -> None:
    """The cars of one house share its solar and battery sensors: another car's entry does not hide the suggestion."""
    await _energy_dashboard(hass)
    make_entry("other", {"production_entity_id": ENERGY_SOLAR, "powerwall_watt_flow_entity_id": ENERGY_BATTERY}).add_to_hass(hass)
    result = await _to_powerwall(hass)
    assert _suggested(result, "powerwall_watt_flow_entity_id") == ENERGY_BATTERY
    result = await _to_solar(hass, result)
    assert _suggested(result, "production_entity_id") == ENERGY_SOLAR


async def test_options_flow_never_pre_fills_from_the_energy_dashboard(hass: HomeAssistant, loaded_entry) -> None:
    await _energy_dashboard(hass)
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "menu_solar"})
    assert result["step_id"] == "solar_forecast_source"
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_defaults(result))
    assert result["step_id"] == "solar"
    assert _suggested(result, "production_entity_id") in (None, "")
    while result["type"] is FlowResultType.FORM:
        result = await hass.config_entries.options.async_configure(result["flow_id"], form_defaults(result))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    assert loaded_entry.options["production_entity_id"] == ""
    assert loaded_entry.options["powerwall_watt_flow_entity_id"] == ""

def test_removed_testing_mode_is_ignored() -> None:
    """A testing_mode left in stored options changes nothing: no marker, no config key."""
    config = build_config({**default_options(), "testing_mode": True})
    assert "testing_mode" not in config
    assert "testing_mode" not in default_options()
    assert format_charging_rule("  Charge \n\nSell  ") == "Charge\nSell"
    assert format_charging_rule("Charge", "\n🚧Site") == "Charge\n🚧Site"


async def test_options_flow_changes_battery_size(hass: HomeAssistant, loaded_entry) -> None:
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "menu_ev"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_defaults(result))
    assert result["step_id"] == "ev_specs"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**form_defaults(result), "battery_size": 82.0})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    assert loaded_entry.options["battery_size"] == 82.0
    assert loaded_entry.runtime_data.config["ev_car"]["battery_size"] == 82.0  # reloaded


async def test_options_flow_drops_a_stored_testing_mode(hass: HomeAssistant, loaded_entry) -> None:
    """A removed option that the stored options still hold (old backup) is not saved again."""
    hass.config_entries.async_update_entry(loaded_entry, options={**loaded_entry.options, "testing_mode": True})
    await hass.async_block_till_done(wait_background_tasks=True)
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "menu_ev"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_defaults(result))
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_defaults(result))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "testing_mode" not in result["data"]
    await hass.async_block_till_done(wait_background_tasks=True)
    assert "testing_mode" not in loaded_entry.options


async def test_site_options_flow(hass: HomeAssistant) -> None:
    set_source_states(hass)
    site = make_site_entry()
    await async_setup(hass, site)
    result = await hass.config_entries.options.async_init(site.entry_id)
    assert result["step_id"] == "site"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**form_defaults(result), "fuse_l2": 35})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert site.options["fuse_l2"] == 35.0
    await hass.config_entries.async_unload(site.entry_id)


async def _load_balancing_page(hass: HomeAssistant, entry_id: str):
    result = await hass.config_entries.options.async_init(entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "menu_charger"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_defaults(result))
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_defaults(result))
    assert result["step_id"] == "load_balancing"
    return result


async def test_chargers_on_one_circuit_are_shared_by_the_cars(hass: HomeAssistant) -> None:
    """The circuit is chosen on one car's page and stored once, so the other car shows and changes the same."""
    easee_a = register_charger(hass, "sensor.easee_a_status", "Easee A")
    easee_b = register_charger(hass, "sensor.easee_b_status", "Easee B")
    site = make_site_entry(fuse_l1=25, **{CONF_CIRCUITS: [["other_1", "other_2", "other_3"]]})
    site.add_to_hass(hass)
    car_a = make_entry("tesla", {**MOCK_OPTIONS, "status_entity_id": "sensor.easee_a_status"})
    car_b = make_entry("citroen", {**MOCK_OPTIONS, "status_entity_id": "sensor.easee_b_status"})
    car_a.add_to_hass(hass)
    car_b.add_to_hass(hass)

    result = await _load_balancing_page(hass, car_a.entry_id)
    assert result["description_placeholders"]["circuit"] == "–"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**form_defaults(result), CONF_CIRCUIT: [easee_b]})
    assert result["errors"] == {CONF_CIRCUIT: "circuit_own_charger_missing"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**form_defaults(result), CONF_CIRCUIT: [easee_a, easee_b, "other_1"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # other_1 moved to this circuit; the rest of the old circuit stays
    assert site.options[CONF_CIRCUITS] == [["other_2", "other_3"], [easee_a, easee_b, "other_1"]]
    assert site.options["fuse_l1"] == 25.0
    assert CONF_CIRCUIT not in car_a.options

    result = await _load_balancing_page(hass, car_b.entry_id)
    assert form_defaults(result)[CONF_CIRCUIT] == [easee_a, easee_b, "other_1"]
    assert result["description_placeholders"]["circuit"] == "- Easee A: cjp_tesla\n- Easee B: cjp_citroen\n- other_1: –"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**form_defaults(result), CONF_CIRCUIT: []})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert site.options[CONF_CIRCUITS] == [["other_2", "other_3"]]


async def test_circuit_needs_a_charger_device(hass: HomeAssistant) -> None:
    make_site_entry().add_to_hass(hass)
    car = make_entry("dumb", MOCK_OPTIONS)  # charger entities without a device
    car.add_to_hass(hass)
    result = await _load_balancing_page(hass, car.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**form_defaults(result), CONF_CIRCUIT: ["some_device"]})
    assert result["errors"] == {CONF_CIRCUIT: "circuit_no_charger_device"}


async def test_site_options_flow_keeps_the_circuits(hass: HomeAssistant) -> None:
    site = make_site_entry(**{CONF_CIRCUITS: [["a", "b"]]})
    site.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(site.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**form_defaults(result), "fuse_l2": 35})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert site.options[CONF_CIRCUITS] == [["a", "b"]]
    assert site.options["fuse_l2"] == 35.0


async def test_options_flow_dashboard_icon(hass: HomeAssistant, loaded_entry) -> None:
    async def open_general():
        result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "menu_general"})
        assert result["step_id"] == "general"
        return result

    result = await open_general()
    assert form_defaults(result)["dashboard_icon"] == "mdi:ev-station"

    # not an icon name: rejected, the page is shown again
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**form_defaults(result), "dashboard_icon": "car"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"dashboard_icon": "invalid_icon"}

    result = await hass.config_entries.options.async_configure(result["flow_id"], {**form_defaults(result), "dashboard_icon": " mdi:car-electric "})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    assert loaded_entry.options["dashboard_icon"] == "mdi:car-electric"

    # a cleared field is left out by the frontend (an empty string also counts): both mean the default icon
    result = await open_general()
    assert form_defaults(result)["dashboard_icon"] == "mdi:car-electric"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {k: v for k, v in form_defaults(result).items() if k != "dashboard_icon"})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert loaded_entry.options["dashboard_icon"] == "mdi:ev-station"


async def test_config_flow_rejects_an_invalid_dashboard_icon(hass: HomeAssistant) -> None:
    result = await _start_instance(hass, dashboard_icon="car")
    assert result["step_id"] == "instance"
    assert result["errors"] == {"dashboard_icon": "invalid_icon"}
    # the icon is optional: left out it is the default one
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ENTITY_PREFIX: "flow", "language": "da"})
    assert result["step_id"] == "charger"
