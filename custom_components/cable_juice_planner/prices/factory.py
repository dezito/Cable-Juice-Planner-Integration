"""Builds the price provider for the configured price entities."""
from __future__ import annotations

from ..const import PRICE_PROVIDER_PRIORITY
from .base import BasePriceProvider, CombinedPriceProvider
from .energidataservice import EnergiDataServicePriceProvider
from .energypriceforecast import EnergyPriceForecastPriceProvider
from .publishedprices import EpexSpotPriceProvider, GeSpotPriceProvider, NordpoolPriceProvider, OctopusPriceProvider
from .spotprices import ElprognosPriceProvider, SpotForecastPriceProvider, WattcastPriceProvider
from .stromligning import StromligningPriceProvider

PROVIDER_CLASSES: dict[str, type[BasePriceProvider]] = {
    "stromligning": StromligningPriceProvider,
    "energidataservice": EnergiDataServicePriceProvider,
    "energypriceforecast": EnergyPriceForecastPriceProvider,
    "spotforecast": SpotForecastPriceProvider,
    "elprognos": ElprognosPriceProvider,
    "wattcast": WattcastPriceProvider,
    "epexspot": EpexSpotPriceProvider,
    "gespot": GeSpotPriceProvider,
    "octopus": OctopusPriceProvider,
    "nordpool": NordpoolPriceProvider,
}


def create_price_provider(config: dict) -> CombinedPriceProvider:
    """The combined provider of the configured providers, in PRICE_PROVIDER_PRIORITY order."""
    entity_ids = config.get("prices", {}).get("entity_ids", {})
    providers: list[BasePriceProvider] = [
        provider_class(config)
        for provider_class in (PROVIDER_CLASSES[name] for name in PRICE_PROVIDER_PRIORITY)
        if entity_ids.get(provider_class.buy_key)
    ]
    if not providers:
        raise ValueError("No supported price provider configured")
    return CombinedPriceProvider(config, providers)


FLOW_PROVIDER_ORDER = ("energidataservice", "stromligning", "energypriceforecast", "spotforecast", "elprognos", "wattcast", "epexspot", "gespot", "octopus", "nordpool")


def providers_from_options(options: dict) -> list[str]:
    """The providers the flat options of a config entry have a buy sensor for (flow time, no provider exists yet).

    Energi Data Service when there is none, as the first page of the flow suggests.
    """
    found = [name for name in FLOW_PROVIDER_ORDER if options.get(PROVIDER_CLASSES[name].buy_key)]
    return found or ["energidataservice"]
