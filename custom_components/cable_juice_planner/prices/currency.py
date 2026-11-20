"""The currency of the price sensors, and from it prices.price_scale (see const/base.py)."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..const import CURRENCY_PRICE_SCALE, CURRENCY_UNIT_ALIASES, PRICE_PROVIDER_KEYS, PRICE_PROVIDER_PRIORITY
from .factory import PROVIDER_CLASSES


def price_scale_of_unit(unit: Any) -> float | None:
    """The scale of a price unit ('DKK/kWh', 'kr/kWh', '€/kWh', 'EUR/kWh'), None when it is not a known currency per kWh.

    Anything else (MWh, cents, an unknown currency, no unit) is None: it is not guessed, the entry keeps the scale it has."""
    if not isinstance(unit, str) or "/" not in unit:
        return None
    currency, per = (part.strip() for part in unit.rsplit("/", 1))
    if per.lower() != "kwh":
        return None
    code = currency.upper()
    return CURRENCY_PRICE_SCALE.get(CURRENCY_UNIT_ALIASES.get(code, code))


def detect_price_scale(options: Mapping[str, Any], get_state: Callable[[str], Any]) -> float | None:
    """The scale of the first price sensor with a known unit, else None: per provider in priority order its buy sensor first,
    then its other sensors (the sell sensor, the tariffs); a sensor without a unit (a binary sensor, not loaded) is skipped.

    The unit of the sensor says what the prices are in: Home Assistant's own currency setting does not (it is EUR by
    default, also for a Danish installation with prices in kroner)."""
    for name in PRICE_PROVIDER_PRIORITY:
        provider = PROVIDER_CLASSES[name]
        for key in (provider.buy_key, *(key for key in PRICE_PROVIDER_KEYS[name] if key != provider.buy_key)):
            entity_id = options.get(key) if key else None
            state = get_state(entity_id) if entity_id else None
            scale = price_scale_of_unit(getattr(state, "attributes", {}).get("unit_of_measurement")) if state is not None else None
            if scale is not None:
                return scale
    return None
