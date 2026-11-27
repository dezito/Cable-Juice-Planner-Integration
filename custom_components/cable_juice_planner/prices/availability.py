"""Which price providers can be chosen in a country, and whether the choice gives a forecast (the price provider page)."""
from __future__ import annotations

from .factory import PROVIDER_CLASSES

# the country of every entry from before the country existed (Cable Juice Planner and BattMind started in Denmark)
DEFAULT_COUNTRY = "DK"


def default_providers(country: str) -> list[str]:
    """The providers a new entry in the country starts with: Energi Data Service in Denmark (as always), else Energy Price
    Forecast, which is made for Europe."""
    return ["energidataservice"] if country == DEFAULT_COUNTRY else ["energypriceforecast"]


def providers_for_country(country: str, names: list[str]) -> list[str]:
    """The providers of names (in their order) that have prices in the country: the choice of the price provider page."""
    return [name for name in names if PROVIDER_CLASSES[name].countries is None or country in PROVIDER_CLASSES[name].countries]


def provider_errors(country: str, providers: list[str]) -> dict[str, str]:
    """The error of a choice of providers ({} when it is fine): one that has no prices in the country, or none with a forecast.

    Only the price provider page asks (also in the options menu, where an entry from before the country is in Denmark), never
    an unrelated page, so a running setup is not stuck on one. Whether a provider has a forecast is what it is (has_forecast),
    not what its sensor shows now (e.g. Carnot being off must not block an existing setup)."""
    for name in providers:
        countries = PROVIDER_CLASSES[name].countries
        if countries is not None and country not in countries:
            return {"base": "price_provider_country"}
    if providers and not any(PROVIDER_CLASSES[name].has_forecast for name in providers):
        return {"base": "price_provider_no_forecast"}
    return {}
