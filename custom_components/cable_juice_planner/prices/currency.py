"""The currency of the price sensors, and from it prices.price_scale (see const/base.py); the checks of a chosen price sensor."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..const import CURRENCY_PRICE_SCALE, CURRENCY_UNIT_ALIASES, PRICE_PROVIDER_KEYS, PRICE_PROVIDER_PRIORITY
from .base import WATTCAST_BUY_KEY, _is_subunit_code, is_price_per_mwh, is_price_subunit, price_unit_divisor, price_unit_error  # noqa: F401 (the converter of the providers and the history, the unit checks)
from .base import price_timestamp
from .factory import PROVIDER_CLASSES
from .spot_rows import elprognos_rows, spot_forecast_rows, wattcast_rows


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
    "spotforecast_buy_entity_id",
    "elprognos_buy_entity_id",
    "wattcast_buy_entity_id",
)


def is_forecast_only(state: Any) -> bool:
    """An Energy Price Forecast sensor that is loaded but has no published prices today or tomorrow, only a forecast: a
    market where it publishes none (today then has no price before the present; its own price source gives one), or
    published prices that are late. A sensor that is not loaded (None, unavailable, unknown) is not judged."""
    if state is None or getattr(state, "state", None) in ("unavailable", "unknown"):
        return False
    attributes = getattr(state, "attributes", {})
    return not attributes.get("raw_today") and not attributes.get("raw_tomorrow") and bool(attributes.get("raw_forecast"))


def _unit_of(key: str, state: Any) -> Any:
    """The unit a price sensor's prices are in, for the currency: Wattcast's ct/kWh is euro (the one sensor in a subunit that is
    accepted, see price_unit_error), the others have the unit they say."""
    unit = getattr(state, "attributes", {}).get("unit_of_measurement") if state is not None else None
    if key == WATTCAST_BUY_KEY and isinstance(unit, str) and unit.strip().lower() == "ct/kwh":
        return "EUR/kWh"
    return unit


def forecast_only_sensors(hass: HomeAssistant, entity_ids: list[str]) -> dict[str, bool | None]:
    """For each Energy Price Forecast sensor whether it has only a forecast (is_forecast_only), or None while it can't be
    judged: unavailable or unknown (Energy Price Forecast starting, down, reloading), or loaded without any price rows
    yet. A sensor that Energy Price Forecast doesn't provide counts as without the problem, so it can't hold a repair:
    one with no state (removed or renamed together with its registry entry), and one with only the state Home Assistant
    restores for a registry entry nobody provides, while the Energy Price Forecast entry that owns it runs."""
    registry = er.async_get(hass)
    result: dict[str, bool | None] = {}
    for entity_id in entity_ids:
        state = hass.states.get(entity_id)
        if state is None or (state.attributes.get("restored") and _energypriceforecast_running(hass, registry.async_get(entity_id))):
            result[entity_id] = False
            continue
        loaded = state.state not in ("unavailable", "unknown")
        has_rows = any(state.attributes.get(key) for key in ("raw_today", "raw_tomorrow", "raw_forecast"))
        result[entity_id] = is_forecast_only(state) if loaded and has_rows else None
    return result


def _energypriceforecast_running(hass: HomeAssistant, entry: er.RegistryEntry | None) -> bool:
    """Whether the Energy Price Forecast entry that owns the sensor runs (without a registry entry: any of them)."""
    owner = hass.config_entries.async_get_entry(entry.config_entry_id) if entry is not None and entry.config_entry_id else None
    if owner is not None:
        return owner.state is ConfigEntryState.LOADED
    return any(candidate.state is ConfigEntryState.LOADED for candidate in hass.config_entries.async_entries("energypriceforecast"))


def energypriceforecast_entities(prices: Mapping[str, Any]) -> list[str]:
    """The Energy Price Forecast sensors of a setup (buy, then sell; once each)."""
    return list(dict.fromkeys(prices.get(key) for key in ("energypriceforecast_buy_entity_id", "energypriceforecast_sell_entity_id") if prices.get(key)))


def price_unit_sensors(prices: Mapping[str, Any]) -> dict[str, str]:
    """{entity_id: config key} of the configured price sensors whose unit is judged (PRICE_SENSOR_KEYS)."""
    return {prices[key]: key for key in PRICE_SENSOR_KEYS if prices.get(key)}


def wrong_unit_sensors(get_state: Callable[[str], Any], sensors: Mapping[str, str]) -> dict[str, str]:
    """{entity_id: unit} of the price sensors whose unit makes their prices 100 or 1000 times too large (price_unit_error).
    The unit attribute stays while a sensor is down, so it is judged then too; a sensor that is not there has none."""
    found: dict[str, str] = {}
    for entity_id, key in sensors.items():
        state = get_state(entity_id)
        unit = getattr(state, "attributes", {}).get("unit_of_measurement") if state is not None else None
        if price_unit_error(key, unit):
            found[entity_id] = str(unit)
    return found


def spot_without_published(key: str, values: Mapping[str, Any], get_state: Callable[[str], Any]) -> bool:
    """A spot forecast sensor that is loaded and has price rows but none of them published (spot_rows): not the price sensor of
    the integration, or attributes that differ from what the provider reads. It would give no price for today and so no real
    price at all. A sensor that is not loaded, has no rows yet, or (Wattcast) has no loaded forecast sensor is not judged."""
    state = get_state(values[key]) if values.get(key) else None
    if state is None or getattr(state, "state", None) in ("unavailable", "unknown"):
        return False
    attributes = getattr(state, "attributes", {})

    def local(value):
        return price_timestamp(value, None)

    if key == "spotforecast_buy_entity_id":
        rows = spot_forecast_rows(attributes, local)
    elif key == "elprognos_buy_entity_id":
        rows = elprognos_rows(attributes, local)
    elif key == WATTCAST_BUY_KEY:
        forecast = get_state(values["wattcast_forecast_entity_id"]) if values.get("wattcast_forecast_entity_id") else None
        if forecast is None or getattr(forecast, "state", None) in ("unavailable", "unknown"):
            return False
        rows = wattcast_rows(attributes, getattr(forecast, "attributes", {}), local)
    else:
        return False
    return bool(rows) and not any(is_published for _start, _price, is_published in rows)


def price_sensor_errors(values: Mapping[str, Any], get_state: Callable[[str], Any]) -> dict[str, str]:
    """Errors of the price sensors a price page has just been given (field -> translation key), for a sensor that is loaded.

    A sensor whose unit is a subunit of the currency (ct/kWh, p/kWh) is refused (so is a price per MWh of Energi Data Service): its prices are 100 or 1000 times too large. The buy
    sensor of Energy Price Forecast must be its retail price (a wholesale sensor as the buy price would plan on prices
    without the supplier's margin, taxes and network charge) and its sell sensor the wholesale one, not the same sensor
    (a retail price as the sell price is more than a sale pays). A sensor of Energy Price Forecast with only a forecast and
    no published prices today or tomorrow is refused too. A sensor that is not loaded is not judged."""
    errors: dict[str, str] = {}
    for key in PRICE_SENSOR_KEYS:
        state = get_state(values[key]) if values.get(key) else None
        if state is None:
            continue
        attributes = getattr(state, "attributes", {})
        unit = attributes.get("unit_of_measurement")
        loaded = getattr(state, "state", None) not in ("unavailable", "unknown")
        # a price per MWh is converted for Energy Price Forecast (price_unit_divisor); Energi Data Service's own price type is kWh
        if price_unit_error(key, unit):
            errors[key] = "price_unit_subunit"  # judged also while the sensor is down: it keeps its unit attribute
        elif loaded and key == "energypriceforecast_buy_entity_id" and isinstance(attributes.get("raw_today"), list) and "retail_source" not in attributes:
            errors[key] = "price_sensor_not_retail"
        elif key == "energypriceforecast_sell_entity_id" and (
            values.get(key) == values.get("energypriceforecast_buy_entity_id")
            or (loaded and isinstance(attributes.get("raw_today"), list) and "retail_source" in attributes)
        ):
            errors[key] = "price_sensor_not_wholesale"  # the retail price is more than a sale pays: the sell sensor is the wholesale one
        elif key.startswith("energypriceforecast_") and is_forecast_only(state):
            # a market where Energy Price Forecast publishes no prices (the charging history, the price of the energy in the
            # battery and the price history need today's), or prices that are late: the text says both
            errors[key] = "price_sensor_forecast_only"
        elif loaded and spot_without_published(key, values, get_state):
            # a spot forecast sensor whose rows are none of them published: not the sensor of the integration (or a shape it does not read)
            errors[key] = "price_sensor_no_published"
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
            currency = currency_of_unit(_unit_of(key, state)) if state is not None else None
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
            scale = price_scale_of_unit(_unit_of(key, state)) if state is not None else None
            if scale is not None:
                return scale
    return None
