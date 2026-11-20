"""The price entities of the dashboard templates: which configured price entity stands in for which template entity.

Every price integration maps the entities of its own chart (dashboard/prices/<provider>.yaml) in its provider
class (``dashboard_entities()``); this only puts them together.
"""
from __future__ import annotations

from ..const import PRICE_PROVIDER_PRIORITY
from .factory import PROVIDER_CLASSES


def price_dashboard_entities(prices: dict[str, str]) -> dict[str, str]:
    """Template entity id -> configured price entity id ("" = not configured), for the price chart of the dashboard."""
    mapping: dict[str, str] = {}
    has_chart = False
    for name in PRICE_PROVIDER_PRIORITY:
        provider = PROVIDER_CLASSES[name]
        entities = provider.dashboard_entities(prices)
        # one price chart: the first configured provider in priority order has it (Strømligning), the others' charts are removed
        mapping.update(dict.fromkeys(entities, "") if has_chart else entities)
        has_chart = has_chart or bool(prices[provider.buy_key])
    return mapping
