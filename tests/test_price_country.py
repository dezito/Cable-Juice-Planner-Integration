"""The country of the prices (the country page and General): which price providers can be chosen (the price provider page)."""
from __future__ import annotations

import re

import probatio
import pytest

from homeassistant import config_entries
from homeassistant.data_entry_flow import InvalidData
from homeassistant.core import HomeAssistant

from homeassistant.helpers import config_validation as cv

from custom_components.cable_juice_planner.flow.schema import provider_of
from custom_components.cable_juice_planner.const import DOMAIN
from custom_components.cable_juice_planner.const.config import build_config, default_options

from .conftest import CONSUMPTION, EV_LEVEL, EV_ODOMETER, EV_RANGE, form_defaults, providers_input
from .test_config_flow import CHARGER_ENTITIES, _configure, _start_instance


async def _to_country(hass: HomeAssistant, **extra):
    result = await _start_instance(hass, **extra)
    result = await _configure(hass, result, "charger", CHARGER_ENTITIES)
    result = await _configure(hass, result, "charger_specs", {"power_voltage": 230, "charging_phases": 3, "charging_max_amp": 16, "charging_loss": -5})
    result = await _configure(
        hass, result, "ev",
        {"usable_battery_level_entity_id": EV_LEVEL, "estimated_battery_range_entity_id": EV_RANGE, "odometer_entity_id": EV_ODOMETER},
    )
    result = await _configure(hass, result, "ev_specs", {"battery_size": 75, "last_charging_hour_before_departure_buffer": 1})
    result = await _configure(hass, result, "home", {"power_consumption_entity_id": CONSUMPTION})
    return await _configure(hass, result, "powerwall", form_defaults(result))


async def _to_price_provider(hass: HomeAssistant, price_country: str | None = None, **extra):
    """Through the country page (the country of Home Assistant when none is given) to the price provider page."""
    result = await _to_country(hass, **extra)
    return await _configure(hass, result, "country", {"price_country": price_country} if price_country else {})


def _offered(result) -> list[str]:
    """The providers the price provider page lists, section by section."""
    return [provider_of(value) for value in _listed(result)]


def _listed(result) -> list[str]:
    """The values of the price provider page: the provider names, marked where the country has not confirmed them."""
    return [value for part in result["data_schema"].schema.values() for field in part.schema.schema.values() for value in field.config["options"]]


def _sections(result) -> dict[str, list[str]]:
    return {str(key): list(next(iter(part.schema.schema.values())).config["options"]) for key, part in result["data_schema"].schema.items()}

async def _first_page(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "instance"})
    assert result["step_id"] == "instance"
    return result


def _defaults(result) -> dict:
    if result["step_id"] == "price_provider":  # what is ticked, over both sections
        return {"price_providers": [provider_of(value) for part in form_defaults(result).values() for value in part["providers"]]}
    return {str(key): key.default() for key in result["data_schema"].schema if hasattr(key, "default") and callable(key.default)}


async def test_the_country_has_a_page_of_its_own_before_the_providers_and_suggests_the_one_of_home_assistant(hass: HomeAssistant) -> None:
    hass.config.country = "NL"
    assert "price_country" not in {str(key) for key in (await _first_page(hass))["data_schema"].schema}  # the first page has no country
    result = await _to_country(hass)
    assert result["step_id"] == "country" and _defaults(result) == {"price_country": "NL"}
    hass.config.country = None
    assert _defaults(await _to_country(hass)) == {"price_country": "DK"}


async def test_a_new_entry_outside_denmark_starts_with_energy_price_forecast_and_lists_only_the_providers_of_the_country(hass: HomeAssistant) -> None:
    hass.config.country = "DE"
    result = await _to_price_provider(hass)
    assert result["step_id"] == "price_provider"
    assert "price_country" not in {str(key) for key in result["data_schema"].schema}  # the country is not asked here any more
    assert _defaults(result) == {"price_providers": ["energypriceforecast"]}
    assert _offered(result) == ["energidataservice", "energypriceforecast", "spotforecast", "nordpool", "epexspot", "gespot"]
    # the page says which providers the country does not have
    assert result["description_placeholders"]["country"] == "DE"
    assert result["description_placeholders"]["unavailable"] == "Strømligning, Elprisprognos, Wattcast, Octopus Energy"


async def test_a_new_entry_without_a_country_in_home_assistant_starts_in_denmark(hass: HomeAssistant) -> None:
    assert hass.config.country is None
    result = await _to_price_provider(hass)
    assert _defaults(result) == {"price_providers": ["energidataservice"]}
    assert "stromligning" in _offered(result) and "octopus" not in _offered(result)
    assert result["description_placeholders"]["unavailable"] == "Spot Forecast, Elprisprognos, Wattcast, Octopus Energy"


async def test_stromligning_is_not_offered_outside_denmark_and_energi_data_service_is(hass: HomeAssistant) -> None:
    result = await _to_price_provider(hass, price_country="DE")
    flow = hass.config_entries.flow
    assert "stromligning" not in _offered(result)
    with pytest.raises(InvalidData):  # the page only takes what it lists
        await flow.async_configure(result["flow_id"], providers_input("stromligning"))
    accepted = await flow.async_configure(result["flow_id"], providers_input("energidataservice"))
    assert accepted["step_id"] == "prices_energidataservice"
    result = await _to_price_provider(hass, price_country="GB")
    assert "energidataservice" not in _offered(result) and "octopus" in _offered(result)
    assert _defaults(result) == {"price_providers": ["energypriceforecast"]}


def test_the_providers_of_a_country_and_the_forecast_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from custom_components.cable_juice_planner.prices import default_providers, provider_errors, providers_for_country
    from custom_components.cable_juice_planner.prices.factory import PROVIDER_CLASSES

    assert provider_errors("DK", ["stromligning", "energidataservice", "energypriceforecast"]) == {}
    assert provider_errors("SE", ["energidataservice"]) == {} and provider_errors("GB", ["energypriceforecast"]) == {}
    assert provider_errors("SE", ["stromligning"]) == {"base": "price_provider_country"}
    assert provider_errors("GB", ["energidataservice"]) == {"base": "price_provider_country"}
    names = ["energidataservice", "stromligning", "energypriceforecast", "octopus"]
    assert providers_for_country("DK", names) == ["energidataservice", "stromligning", "energypriceforecast"]
    assert providers_for_country("GB", names) == ["energypriceforecast", "octopus"]
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
    assert "stromligning" in _offered(result)  # Denmark, not Germany
    result = await hass.config_entries.options.async_configure(result["flow_id"], providers_input("stromligning", "energidataservice"))
    assert not result.get("errors") and result["step_id"].startswith("prices_"), result


async def test_the_general_page_has_the_country_and_a_provider_the_new_country_does_not_have_leads_to_the_price_pages(hass: HomeAssistant, loaded_entry) -> None:
    hass.config_entries.async_update_entry(loaded_entry, options={**loaded_entry.options, "price_country": "DK", "stromligning_buy_current_price": "sensor.stromligning_price"})
    await hass.async_block_till_done(wait_background_tasks=True)
    options = hass.config_entries.options
    result = await options.async_init(loaded_entry.entry_id)
    result = await options.async_configure(result["flow_id"], {"next_step_id": "menu_general"})
    assert result["step_id"] == "general"
    fields = [str(key) for key in result["data_schema"].schema]
    assert fields.index("price_country") == fields.index("language") + 1
    assert _defaults(result)["price_country"] == "DK"
    # Strømligning is Danish: in Germany the price page follows, and it no longer lists it
    result = await options.async_configure(result["flow_id"], {"language": loaded_entry.options["language"], "price_country": "DE"})
    assert result["step_id"] == "price_provider"
    assert "stromligning" not in _offered(result) and "stromligning" not in _defaults(result)["price_providers"]
    assert result["description_placeholders"]["country"] == "DE" and "Strømligning" in result["description_placeholders"]["unavailable"]


async def test_a_new_country_where_every_chosen_provider_works_is_saved_without_the_price_pages(hass: HomeAssistant, loaded_entry) -> None:
    from homeassistant.data_entry_flow import FlowResultType

    hass.config_entries.async_update_entry(loaded_entry, options={**loaded_entry.options, "price_country": "DK", "stromligning_buy_current_price": ""})
    await hass.async_block_till_done(wait_background_tasks=True)
    options = hass.config_entries.options
    result = await options.async_init(loaded_entry.entry_id)
    result = await options.async_configure(result["flow_id"], {"next_step_id": "menu_general"})
    result = await options.async_configure(result["flow_id"], {"language": loaded_entry.options["language"], "price_country": "SE"})  # Energi Data Service works in Sweden
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["data"]["price_country"] == "SE"


def test_every_provider_has_a_name_for_the_price_page() -> None:
    """The page names the providers a country does not have: a provider without a name would break it in every other country."""
    from custom_components.cable_juice_planner.flow.schema import PRICE_PROVIDER_NAMES, PRICE_PROVIDERS
    from custom_components.cable_juice_planner.prices.factory import PROVIDER_CLASSES

    assert set(PRICE_PROVIDER_NAMES) == set(PRICE_PROVIDERS) == set(PROVIDER_CLASSES)

async def test_saving_the_general_page_without_a_country_change_does_not_lead_to_the_price_pages(hass: HomeAssistant, loaded_entry) -> None:
    from homeassistant.data_entry_flow import FlowResultType

    hass.config_entries.async_update_entry(loaded_entry, options={**loaded_entry.options, "price_country": "GB", "stromligning_buy_current_price": "sensor.stromligning_price"})
    await hass.async_block_till_done(wait_background_tasks=True)
    options = hass.config_entries.options
    result = await options.async_init(loaded_entry.entry_id)
    result = await options.async_configure(result["flow_id"], {"next_step_id": "menu_general"})
    result = await options.async_configure(result["flow_id"], {"language": loaded_entry.options["language"], "price_country": "GB"})  # nothing changed: a running setup is not sent on
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["data"]["stromligning_buy_current_price"] == "sensor.stromligning_price"


async def test_saving_the_general_page_in_a_new_country_ends_with_the_country_stored_and_the_provider_of_the_old_one_cleared(hass: HomeAssistant, loaded_entry) -> None:
    from homeassistant.data_entry_flow import FlowResultType

    hass.config_entries.async_update_entry(loaded_entry, options={**loaded_entry.options, "price_country": "DK", "stromligning_buy_current_price": "sensor.stromligning_price"})
    await hass.async_block_till_done(wait_background_tasks=True)
    options = hass.config_entries.options
    result = await options.async_init(loaded_entry.entry_id)
    result = await options.async_configure(result["flow_id"], {"next_step_id": "menu_general"})
    result = await options.async_configure(result["flow_id"], {"language": loaded_entry.options["language"], "price_country": "DE"})
    assert result["step_id"] == "price_provider"
    result = await options.async_configure(result["flow_id"], providers_input("energidataservice"))
    while result["type"] is FlowResultType.FORM:
        result = await options.async_configure(result["flow_id"], form_defaults(result))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["price_country"] == "DE" and result["data"]["stromligning_buy_current_price"] == ""


async def test_the_providers_are_in_two_sections_and_the_unconfirmed_ones_are_marked(hass: HomeAssistant) -> None:
    result = await _to_price_provider(hass, price_country="DK")
    assert _sections(result) == {
        "with_forecast": ["stromligning", "energidataservice", "energypriceforecast_unconfirmed"],
        "prices_only": ["nordpool_unconfirmed", "epexspot_unconfirmed", "gespot_unconfirmed"],
    }
    # the order the prices are taken in, only with the providers the country has
    assert result["description_placeholders"]["priority"] == "Strømligning → Energi Data Service → Nord Pool → EPEX Spot → GE-Spot → Energy Price Forecast"
    # a provider is confirmed per country: Energi Data Service is not in Sweden
    abroad = await _to_price_provider(hass, price_country="SE")
    assert "energidataservice_unconfirmed" in _sections(abroad)["with_forecast"]
    assert "energidataservice" not in _sections(abroad)["with_forecast"]


async def test_the_form_sent_to_the_browser_has_both_sections_open(hass: HomeAssistant) -> None:
    result = await _to_price_provider(hass, price_country="DK")
    form = probatio.to_field_list(result["data_schema"], custom_serializer=cv.custom_serializer)
    assert [(field["name"], field["type"], field["expanded"]) for field in form] == [("with_forecast", "expandable", True), ("prices_only", "expandable", True)]
    inner = form[0]["schema"][0]
    assert inner["name"] == "providers" and inner["default"] == ["energidataservice"]
    assert inner["selector"]["select"]["translation_key"] == "price_providers"
    assert inner["selector"]["select"]["options"] == ["stromligning", "energidataservice", "energypriceforecast_unconfirmed"]


async def test_a_marked_choice_is_taken_as_the_provider_and_stays_ticked_when_the_page_returns(hass: HomeAssistant) -> None:
    flow = hass.config_entries.flow
    result = await _to_price_provider(hass, price_country="DK")
    # published prices alone have no forecast: the page returns with the marked choice still ticked
    refused = await flow.async_configure(result["flow_id"], {"with_forecast": {"providers": []}, "prices_only": {"providers": ["nordpool_unconfirmed"]}})
    assert refused["errors"] == {"base": "price_provider_no_forecast"} and form_defaults(refused)["prices_only"] == {"providers": ["nordpool_unconfirmed"]}
    accepted = await flow.async_configure(refused["flow_id"], {"with_forecast": {"providers": ["energypriceforecast_unconfirmed"]}, "prices_only": {"providers": []}})
    assert accepted["step_id"] == "prices_energypriceforecast"


def test_every_language_names_both_sections_and_every_provider_with_and_without_the_mark() -> None:
    import json
    import pathlib

    from custom_components.cable_juice_planner.flow.schema import PRICE_PROVIDERS

    package = pathlib.Path(__file__).parents[1] / "custom_components" / "cable_juice_planner"
    for path in [package / "strings.json", *(package / "translations").glob("*.json")]:
        data = json.loads(path.read_text(encoding="utf-8"))
        labels = data["selector"]["price_providers"]["options"]
        assert set(labels) == {*PRICE_PROVIDERS, *(f"{name}_unconfirmed" for name in PRICE_PROVIDERS)}, path.name
        for name in PRICE_PROVIDERS:
            assert labels[f"{name}_unconfirmed"] == f"🧪 {labels[name]}", (path.name, name)
        for flow in ("config", "options"):
            page = data[flow]["step"]["price_provider"]
            assert set(re.findall(r"\{(\w+)\}", page["description"])) == {"priority", "country", "unavailable"}, (path.name, flow)
            assert "🧪" in page["description"], (path.name, flow)
            assert set(page["sections"]) == {"with_forecast", "prices_only"}, (path.name, flow)
            for part in page["sections"].values():
                assert part["name"] and part["description"] and part["data"]["providers"], (path.name, flow)
