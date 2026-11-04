"""Builds the price provider for the configured price entities."""
from __future__ import annotations

from ..const import PRICE_PROVIDER_PRIORITY
from .base import BasePriceProvider, CombinedPriceProvider
from .energidataservice import EnergiDataServicePriceProvider
from .stromligning import StromligningPriceProvider

PROVIDER_CLASSES: dict[str, type[BasePriceProvider]] = {
    "stromligning": StromligningPriceProvider,
    "energidataservice": EnergiDataServicePriceProvider,
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
