"""The currency of the price sensors, and from it prices.price_scale (see const/base.py); the checks of a chosen price sensor."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..const import CURRENCY_PRICE_SCALE, CURRENCY_UNIT_ALIASES, PRICE_PROVIDER_KEYS, PRICE_PROVIDER_PRIORITY
from .base import is_price_per_mwh, price_unit_divisor  # noqa: F401 (price_unit_divisor: the converter of the providers and the history)
from .factory import PROVIDER_CLASSES


def price_scale_of_unit(unit: Any) -> float | None:
    """The scale of a price unit ('DKK/kWh', 'kr/kWh', '€/kWh', 'EUR/kWh'), None when it is not a known currency per kWh.

    A price per MWh has the scale of its currency too (price_unit_divisor() converts the prices). Anything else (cents, an
    unknown currency, no unit) is None: it is not guessed, the entry keeps the scale it has."""
    if not isinstance(unit, str) or "/" not in unit:
        return None
    currency, per = (part.strip() for part in unit.rsplit("/", 1))
    if per.lower() not in ("kwh", "mwh"):
        return None
    if _is_subunit_code(currency):
        return None  # "GBp" is pence, not pounds: a currency code with a lower-case last letter is a subunit, never the currency
    return CURRENCY_PRICE_SCALE.get(_currency_code(currency))


def _currency_code(currency: str) -> str:
    code = currency.upper()
    return CURRENCY_UNIT_ALIASES.get(code, code)


def currency_of_unit(unit: Any) -> str | None:
    """The currency of a price per kWh ('EUR/kWh' -> 'EUR', 'kr/kWh' -> 'DKK'), also one that is not in the scale table (it is
    only named, not guessed), also for a price per MWh; None for a subunit, another quantity or text that is no unit."""
    if not isinstance(unit, str) or "/" not in unit or is_price_subunit(unit):
        return None
    currency, per = (part.strip() for part in unit.rsplit("/", 1))
    if per.lower() not in ("kwh", "mwh") or not currency or " " in currency or _is_subunit_code(currency):
        return None
    return _currency_code(currency)


# the price sensors a price page asks for: a price is in the currency per kWh, never in its subunit (cents, pence)
PRICE_SENSOR_KEYS = (
    "energidataservice_buy_entity_id",
    "energidataservice_sell_entity_id",
    "energypriceforecast_buy_entity_id",
    "energypriceforecast_sell_entity_id",
)
_SUBUNITS = {
    "c", "ct", "cts", "cent", "cents", "eurocent", "eurocents", "c€", "¢", "p", "pence", "penny", "gbx",
    "øre", "ore", "öre", "snt", "gr", "grosz", "groszy", "rp", "rappen", "bani", "st", "stotinki",
}


def _is_subunit_code(currency: str) -> bool:
    """GBp, EUr...: an ISO code of the table with a lower-case last letter (the currency code spelled as its subunit)."""
    return len(currency) > 1 and currency[:-1].isupper() and currency[-1].islower() and currency.upper() in CURRENCY_PRICE_SCALE


def is_price_subunit(unit: Any) -> bool:
    """A price per kWh in a subunit of the currency (ct/kWh, p/kWh, GBp/kWh, EURct/kWh): 100 times the price in the currency.

    price_scale_of_unit() is None for these (they are not a currency), this tells that apart from a unit it just does not know."""
    if not isinstance(unit, str) or "/" not in unit:
        return False
    currency, per = (part.strip() for part in unit.rsplit("/", 1))
    if per.lower() not in ("kwh", "mwh") or not currency:
        return False
    token = currency.lower()
    return token in _SUBUNITS or token.endswith(("ct", "cent", "cents")) or _is_subunit_code(currency)


def price_sensor_errors(values: Mapping[str, Any], get_state: Callable[[str], Any]) -> dict[str, str]:
    """Errors of the price sensors a price page has just been given (field -> translation key), for a sensor that is loaded.

    A sensor whose unit is a subunit of the currency (ct/kWh, p/kWh) is refused (so is a price per MWh of Energi Data Service): its prices are 100 or 1000 times too large. The buy
    sensor of Energy Price Forecast must be its retail price (a wholesale sensor as the buy price would plan on prices
    without the supplier's margin, taxes and network charge) and its sell sensor the wholesale one, not the same sensor
    (a retail price as the sell price is more than a sale pays). A sensor that is not loaded is not judged."""
    errors: dict[str, str] = {}
    for key in PRICE_SENSOR_KEYS:
        state = get_state(values[key]) if values.get(key) else None
        if state is None:
            continue
        attributes = getattr(state, "attributes", {})
        unit = attributes.get("unit_of_measurement")
        loaded = getattr(state, "state", None) not in ("unavailable", "unknown")
        # a price per MWh is converted for Energy Price Forecast (price_unit_divisor); Energi Data Service's own price type is kWh
        if is_price_subunit(unit) or (is_price_per_mwh(unit) and key.startswith("energidataservice_")):
            errors[key] = "price_unit_subunit"  # judged also while the sensor is down: it keeps its unit attribute
        elif loaded and key == "energypriceforecast_buy_entity_id" and isinstance(attributes.get("raw_today"), list) and "retail_source" not in attributes:
            errors[key] = "price_sensor_not_retail"
        elif key == "energypriceforecast_sell_entity_id" and (
            values.get(key) == values.get("energypriceforecast_buy_entity_id")
            or (loaded and isinstance(attributes.get("raw_today"), list) and "retail_source" in attributes)
        ):
            errors[key] = "price_sensor_not_wholesale"  # the retail price is more than a sale pays: the sell sensor is the wholesale one
    return errors


def price_currency_errors(options: Mapping[str, Any], page_keys: Any, get_state: Callable[[str], Any]) -> dict[str, str]:
    """Price sensors in more than one currency: one currency per setup, so a second provider is a backup of the first only
    within it (a price in another currency would be planned as if it were kroner). The sensors of this page that are in
    a currency that is not the same as every other price sensor's are refused (compared by currency, also one that is not in
    the scale table); sensors without a known unit are not judged.

    options: the options with this page's values in them."""
    currencies: dict[str, str] = {}
    for keys in PRICE_PROVIDER_KEYS.values():
        for key in keys:
            entity_id = options.get(key)
            state = get_state(entity_id) if entity_id else None
            currency = currency_of_unit(getattr(state, "attributes", {}).get("unit_of_measurement")) if state is not None else None
            if currency is not None:
                currencies[key] = currency
    if len(set(currencies.values())) < 2:
        return {}
    return {key: "price_currency_mismatch" for key in page_keys if key in currencies}


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
