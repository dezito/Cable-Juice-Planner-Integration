"""The country of the prices: which price providers can be chosen (the price provider page)."""
from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.cable_juice_planner.const.config import build_config, default_options

from .conftest import CONSUMPTION, EV_LEVEL, EV_ODOMETER, EV_RANGE, form_defaults
from .test_config_flow import CHARGER_ENTITIES, _configure, _start_instance


async def _to_price_provider(hass: HomeAssistant):
    result = await _start_instance(hass)
    result = await _configure(hass, result, "charger", CHARGER_ENTITIES)
    result = await _configure(hass, result, "charger_specs", {"power_voltage": 230, "charging_phases": 3, "charging_max_amp": 16, "charging_loss": -5})
    result = await _configure(
        hass, result, "ev",
        {"usable_battery_level_entity_id": EV_LEVEL, "estimated_battery_range_entity_id": EV_RANGE, "odometer_entity_id": EV_ODOMETER},
    )
    result = await _configure(hass, result, "ev_specs", {"battery_size": 75, "last_charging_hour_before_departure_buffer": 1})
    result = await _configure(hass, result, "home", {"power_consumption_entity_id": CONSUMPTION})
    return await _configure(hass, result, "powerwall", form_defaults(result))


def _defaults(result) -> dict:
    return {str(key): key.default() for key in result["data_schema"].schema if hasattr(key, "default") and callable(key.default)}


async def test_a_new_entry_outside_denmark_starts_with_energy_price_forecast_for_the_country_of_home_assistant(hass: HomeAssistant) -> None:
    hass.config.country = "DE"
    result = await _to_price_provider(hass)
    assert result["step_id"] == "price_provider"
    assert _defaults(result) == {"price_country": "DE", "price_providers": ["energypriceforecast"]}


async def test_a_new_entry_without_a_country_in_home_assistant_starts_in_denmark(hass: HomeAssistant) -> None:
    assert hass.config.country is None
    result = await _to_price_provider(hass)
    assert _defaults(result) == {"price_country": "DK", "price_providers": ["energidataservice"]}


async def test_stromligning_is_refused_outside_denmark_and_energi_data_service_is_not(hass: HomeAssistant) -> None:
    result = await _to_price_provider(hass)
    flow = hass.config_entries.flow
    refused = await flow.async_configure(result["flow_id"], {"price_country": "DE", "price_providers": ["stromligning"]})
    assert refused["step_id"] == "price_provider" and refused["errors"] == {"base": "price_provider_country"}
    assert _defaults(refused) == {"price_country": "DE", "price_providers": ["stromligning"]}  # the choice stays on the page
    refused = await flow.async_configure(result["flow_id"], {"price_country": "GB", "price_providers": ["energidataservice"]})
    assert refused["errors"] == {"base": "price_provider_country"}
    accepted = await flow.async_configure(result["flow_id"], {"price_country": "DE", "price_providers": ["energidataservice"]})
    assert accepted["step_id"] == "prices_energidataservice"


def test_the_providers_of_a_country_and_the_forecast_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from custom_components.cable_juice_planner.prices import default_providers, provider_errors
    from custom_components.cable_juice_planner.prices.factory import PROVIDER_CLASSES

    assert provider_errors("DK", ["stromligning", "energidataservice", "energypriceforecast"]) == {}
    assert provider_errors("SE", ["energidataservice"]) == {} and provider_errors("GB", ["energypriceforecast"]) == {}
    assert provider_errors("SE", ["stromligning"]) == {"base": "price_provider_country"}
    assert provider_errors("GB", ["energidataservice"]) == {"base": "price_provider_country"}
    assert default_providers("DK") == ["energidataservice"] and default_providers("FI") == ["energypriceforecast"]
    # no provider with a forecast (a source of the later milestones): refused; one with a forecast next to it is enough
    monkeypatch.setattr(PROVIDER_CLASSES["energypriceforecast"], "has_forecast", False)
    assert provider_errors("DE", ["energypriceforecast"]) == {"base": "price_provider_no_forecast"}
    assert provider_errors("DE", ["energypriceforecast", "energidataservice"]) == {}


def test_an_entry_from_before_the_country_is_danish() -> None:
    assert default_options()["price_country"] == "DK"
    assert build_config(default_options())["prices"]["country"] == "DK"
    stored = {key: value for key, value in default_options().items() if key != "price_country"}
    assert build_config(stored)["prices"]["country"] == "DK"


async def test_the_options_menu_accepts_an_existing_danish_setup_in_a_foreign_home_assistant(hass: HomeAssistant, loaded_entry) -> None:
    """An entry from before the country has none stored: it is in Denmark, not in the country of Home Assistant, so it is never refused."""
    hass.config.country = "DE"
    hass.config_entries.async_update_entry(loaded_entry, options={key: value for key, value in loaded_entry.options.items() if key != "price_country"})
    await hass.async_block_till_done(wait_background_tasks=True)
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "menu_prices"})
    assert result["step_id"] == "price_provider"
    assert _defaults(result)["price_country"] == "DK"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"price_providers": ["stromligning", "energidataservice"]})
    assert not result.get("errors") and result["step_id"].startswith("prices_"), result
