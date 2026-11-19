"""The price helpers that live in prices/ because they read the price options and the Strømligning platform: flow time, repairs, dashboard."""
from __future__ import annotations

import re

import pytest

from custom_components.cable_juice_planner.management.dashboard_build import TEMPLATE_DIR
from custom_components.cable_juice_planner.prices import (
    has_stromligning_sell_prices,
    price_dashboard_entities,
    providers_from_options,
)
from custom_components.cable_juice_planner.prices.factory import PROVIDER_CLASSES

BUY = "stromligning_buy_current_price"
PRICE_KEYS = [
    "energidataservice_buy_entity_id",
    "energidataservice_sell_entity_id",
    BUY,
    "stromligning_buy_tomorrow_available",
    "stromligning_buy_forecasts",
    "stromligning_sell_current_price", "stromligning_sell_tomorrow_available", "stromligning_sell_forecasts",
]


def test_the_providers_of_the_options_follow_their_buy_sensors() -> None:
    assert providers_from_options({}) == ["energidataservice"]  # nothing chosen yet: the flow starts with Energi Data Service
    assert providers_from_options({BUY: "sensor.sl_current_price_vat"}) == ["stromligning"]
    both = {BUY: "sensor.sl", "energidataservice_buy_entity_id": "sensor.eds"}
    assert providers_from_options(both) == ["energidataservice", "stromligning"]
    assert providers_from_options({"stromligning_sell_current_price": "sensor.sl_sell"}) == ["energidataservice"]  # a sell sensor alone is no provider


def test_the_tariff_page_is_for_a_stromligning_sell_sensor() -> None:
    assert has_stromligning_sell_prices({"stromligning_sell_current_price": "sensor.sl_sell"}) is True
    assert has_stromligning_sell_prices({"stromligning_sell_current_price": ""}) is False
    assert has_stromligning_sell_prices({}) is False


def test_the_dashboard_has_one_price_chart_and_stromligning_is_preferred() -> None:
    prices = dict.fromkeys(PRICE_KEYS, "")
    prices.update({"energidataservice_buy_entity_id": "sensor.eds", "energidataservice_sell_entity_id": "sensor.eds_sell"})
    mapping = price_dashboard_entities(prices)
    assert mapping["sensor.energi_data_service"] == "sensor.eds" and mapping["sensor.energi_data_service_salg"] == "sensor.eds_sell"
    assert mapping["sensor.stromligning_current_price_vat"] == ""

    prices.update({BUY: "sensor.sl_current_price_vat", **{"stromligning_sell_current_price": "sensor.sl_15min_current_price_ex_vat"}})
    mapping = price_dashboard_entities(prices)
    assert mapping["sensor.stromligning_current_price_vat"] == "sensor.sl_current_price_vat"
    assert mapping["sensor.stromligning_nettariff_vat"] == "sensor.sl_nettariff_vat"
    assert mapping["sensor.stromligning_15min_spotprice_ex_vat"] == "sensor.sl_15min_spotprice_ex_vat"
    assert mapping["sensor.energi_data_service"] == "" and mapping["sensor.energi_data_service_salg"] == ""  # the Energi Data Service chart is removed


def test_the_dashboard_price_chart_with_only_stromligning_or_none() -> None:
    prices = dict.fromkeys(PRICE_KEYS, "")
    assert set(price_dashboard_entities(prices).values()) == {""}
    prices[BUY] = "sensor.sl_current_price_vat"
    mapping = price_dashboard_entities(prices)
    assert mapping["sensor.stromligning_current_price_vat"] == "sensor.sl_current_price_vat"
    assert mapping["sensor.energi_data_service"] == "" and mapping["sensor.energi_data_service_salg"] == ""


@pytest.mark.parametrize(("provider", "prefix"), [("energidataservice", "energi_data_service"), ("stromligning", "stromligning")])
def test_a_provider_maps_exactly_the_external_entities_of_its_chart_file(provider: str, prefix: str) -> None:
    """dashboard/prices/<provider>.yaml and the provider's dashboard_entities() stay together."""
    mapped = set(PROVIDER_CLASSES[provider].dashboard_entities(dict.fromkeys(PRICE_KEYS, "")))
    chart = (TEMPLATE_DIR / "prices" / f"{provider}.yaml").read_text(encoding="utf-8")
    assert mapped == set(re.findall(rf"\b(?:sensor|binary_sensor)\.{prefix}\w*", chart))
