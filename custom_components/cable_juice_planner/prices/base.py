"""Shared price helpers, the base provider and the combined provider."""
from __future__ import annotations

import datetime
import logging
import math
from collections.abc import Callable, Mapping
from typing import Any

from ..const import CURRENCY_PRICE_SCALE, ENTITY_UNAVAILABLE_STATES, PRICE_ADDER_DAY_BETWEEN_DIVIDER, price_decimals, price_scale
from ..management.value_checks import is_price_per_mwh, is_price_per_wh, is_price_subunit, price_unit_wrong
from ..utils.helpers import days_between, float_or_none, reset_time_to_hour, to_local_datetime
from .intervals import common_interval_minutes, interval_slots, provider_intervals

_LOGGER = logging.getLogger(__name__)


TARIFF_STATE_KEYS = (
    "stromligning_sell_electricity_tax",
    "stromligning_sell_nettariff",
    "stromligning_sell_systemtariff",
    "stromligning_sell_surcharge",
)

PRICE_SNAPSHOT_SECONDS = 60 * 60 - 5

_ZERO_TARIFFS = {
    "transmissions_nettarif": 0.0,
    "systemtarif": 0.0,
    "elafgift": 0.0,
    "tariffs": 0.0,
    "surcharge": 0.0,
    "tariff_sum": 0.0,
}


# hass.states.get (or anything returning an object with .state and .attributes, or None)
GetState = Callable[[str], Any]


def sell_adder_sign(config: dict) -> float:
    """+1 or -1: how the forecast adder moves a SELL price so that a sale is worth less.

    A sold kWh is worth the hourly sell price, so a forecast sell price gets the adder subtracted (-1). With a
    fixed solar sell price (production_price != -1) it is worth fixed price minus the hourly price, so the
    hourly price has to be raised (+1) to make the sale worth less.
    """
    try:
        return 1.0 if float(config["solar"]["production_price"]) != -1.0 else -1.0
    except (TypeError, ValueError):
        # not a number: like the default (-1, no fixed price), so the buy prices still work
        return -1.0


def _naive(timestamp):
    return timestamp.replace(tzinfo=None) if isinstance(timestamp, datetime.datetime) else timestamp


def price_timestamp(value, tz: datetime.tzinfo | None = None):
    """The start of a price row from an entity attribute (ISO string or datetime) as naive local time.

    An aware timestamp, with any offset (UTC from Nord Pool, EPEX, ...), is converted to tz (Home Assistant's
    time zone) before the offset is dropped; a naive one is taken as local time already. Without a tz the offset
    is just dropped. Anything that is no timestamp is returned unchanged (the providers skip such rows).
    """
    return to_local_datetime(value, tz)


def is_number(value) -> bool:
    """A usable price: a finite number (not None, text, a bool, NaN or infinity)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def price_unit_divisor(unit) -> float:
    """What the numbers of a price sensor are divided by to get the price per kWh: 1000 for a price per MWh, else 1."""
    return 1000.0 if is_price_per_mwh(unit) else 1.0


def _is_subunit_code(currency: str) -> bool:
    """GBp, EUr...: an ISO code of the table with a lower-case last letter (the currency code spelled as its subunit)."""
    return len(currency) > 1 and currency[:-1].isupper() and currency[-1].islower() and currency.upper() in CURRENCY_PRICE_SCALE


WATTCAST_BUY_KEY = "wattcast_buy_entity_id"
WATTCAST_FORECAST_KEY = "wattcast_forecast_entity_id"


def price_unit_error(key: str, unit: Any) -> bool:
    """A price sensor whose unit makes its prices 100 or 1000 times off: a price in a subunit of the currency (ct/kWh,
    p/kWh) on any price key, a price per MWh or per Wh on an Energi Data Service, Spot Forecast, Elprisprognos, EPEX Spot, GE-Spot, Octopus or Nord Pool key (Energy Price Forecast converts its own
    price per MWh, see price_unit_divisor(); a price per Wh is wrong there too)."""
    if key == WATTCAST_BUY_KEY:
        # Wattcast gives ct/kWh (euro) and nothing else: that unit is its own and known, any other is wrong
        return isinstance(unit, str) and bool(unit.strip()) and unit.strip().lower() != "ct/kwh"
    if key.startswith("energypriceforecast_"):
        return is_price_subunit(unit) or is_price_per_wh(unit)  # it converts a price per MWh itself, never one per Wh
    return is_price_subunit(unit) or (key.startswith(("energidataservice_", "spotforecast_", "elprognos_", "epexspot_", "gespot_", "octopus_", "nordpool_")) and (is_price_per_mwh(unit) or is_price_per_wh(unit)))


def sell_price_parts(price: float, tariff: Mapping, costs: Mapping, sell_fee: float, decimals: int = 2) -> tuple[float, float, float]:
    """The sell price formula (see calculate_sell_price): (price without the tariffs in it, selling costs, sell price).

    decimals: what the sell price is rounded to (price_decimals of the config)."""
    raw_price = price - tariff["tariff_sum"]
    sell_tariffs = sum((sell_fee, costs["transmissions_nettarif"], costs["systemtarif"]))
    return raw_price, sell_tariffs, round(raw_price - sell_tariffs, decimals)


class BasePriceProvider:
    source_name = "base"
    # config key of the buy price entity
    buy_key = ""
    # the countries (ISO 3166-1 alpha-2) the provider gives prices in (None: any, it has its own list), and whether it gives a forecast
    countries: frozenset[str] | None = None
    has_forecast = True
    # whether the recorded state of its sensor is a buy price (the economy and the price history read it); the wholesale sensors are not
    records_history = True

    def __init__(self, config: dict) -> None:
        prices_config = config.get("prices", {})
        self.entity_ids = dict(prices_config.get("entity_ids", {}))
        self.refund = abs(prices_config.get("refund", 0.0))
        self.sell_fee = abs(prices_config.get("sell_fee", 0.0))
        self.sell_adder_sign = sell_adder_sign(config)
        # the money limits are for kroner: prices in another currency scale them (const/base.py price_scale)
        self.price_scale = price_scale(config)
        self.decimals = price_decimals(config)
        self.buy_real_prices: dict = {}
        self.buy_forecast_prices: dict = {}
        self.sell_real_prices: dict = {}
        self.sell_forecast_prices: dict = {}
        self.sell_price_details: dict = {}
        self.shared_data: dict = {"attributes": {}, "values": {}, "version": 0}
        self.now = datetime.datetime.now().replace(microsecond=0)
        # Home Assistant's time zone (set_now): the offset of a price row is converted to it, see price_timestamp()
        self.tz: datetime.tzinfo | None = None
        self._wrong_unit_logged: set[str] = set()

    @staticmethod
    def get_price_at_timestamp(prices: dict, timestamp):
        """Latest price at or before timestamp on the same day."""
        if not prices:
            return None
        timestamp = _naive(timestamp)
        best = None
        for start in prices:
            start = _naive(start)
            if start.date() == timestamp.date() and start <= timestamp:
                if best is None or start > best:
                    best = start
        if best is None:
            return None
        return {"price_timestamp": best, "price": prices[best]}

    def update_prices(self) -> None:
        old_details = self.sell_price_details
        self.sell_price_details = {}
        try:
            buy_real = self._load_buy_real_prices()
            buy_forecast = self._load_buy_forecast_prices()
            sell_real = self._load_sell_real_prices()
            sell_forecast = self._load_sell_forecast_prices()
        except Exception:
            self.sell_price_details = old_details
            raise
        self.buy_real_prices = buy_real
        self.buy_forecast_prices = buy_forecast
        self.sell_real_prices = sell_real
        self.sell_forecast_prices = sell_forecast

    def _entity_exists(self, entity_id) -> bool:
        return bool(entity_id) and entity_id in self.shared_data["attributes"]

    def _get_entity_attr(self, entity_id) -> dict:
        return self.shared_data["attributes"].get(entity_id, {})

    def _wrong_unit(self, entity_id: str) -> bool:
        """A price sensor whose unit makes its prices 100 or 1000 times too large (price_unit_error) gives no prices at all, so
        the next provider or the stored prices fill in, never a price that is too high. The price page refuses such a sensor;
        this is for one whose unit changed later."""
        unit = self._get_entity_attr(entity_id).get("unit_of_measurement")
        if not price_unit_error(self.buy_key, unit):
            self._wrong_unit_logged.discard(entity_id)
            return False
        if entity_id not in self._wrong_unit_logged:
            self._wrong_unit_logged.add(entity_id)
            _LOGGER.warning("%s gives its price in %s, not per kWh in the currency: its prices are not used", entity_id, unit)
        return True

    def _get_cached_entity_state(self, entity_id):
        return self.shared_data["values"].get(entity_id, 0.0)

    def get_buy_real_prices(self):
        return self.buy_real_prices

    def get_buy_forecast_prices(self):
        return self.buy_forecast_prices

    def get_sell_real_prices(self):
        return self.sell_real_prices

    def get_sell_forecast_prices(self):
        return self.sell_forecast_prices

    def get_buy_price_entity_id(self) -> str:
        return ""

    def get_sell_price_entity_id(self) -> str:
        """The entity whose recorded history is the provider's sell price (empty when there is no single one)."""
        return ""

    def _load_buy_real_prices(self):
        raise NotImplementedError

    def _load_buy_forecast_prices(self):
        return {}

    def _load_sell_real_prices(self):
        raise NotImplementedError

    def _load_sell_forecast_prices(self):
        return {}

    def get_tariffs(self, hour, day_of_week, timestamp=None) -> dict:
        raise NotImplementedError

    @classmethod
    def dashboard_entities(cls, prices: dict[str, str]) -> dict[str, str]:
        """Template entity id -> configured entity id ("" = not configured) of this provider's price chart.

        The chart is dashboard/prices/<source_name>.yaml; every external entity it uses is mapped here."""
        return {}

    def _forecast_adder(self, timestamp, sell: bool = False) -> float:
        """Forecast prices get a small penalty per day ahead to prefer known prices.

        A forecast buy price is dearer, and a sale at a forecast sell price is worth less (sell_adder_sign)."""
        adder = days_between(reset_time_to_hour(self.now), timestamp) / PRICE_ADDER_DAY_BETWEEN_DIVIDER * self.price_scale
        return adder * self.sell_adder_sign if sell else adder

    def get_sell_costs(self, hour, day_of_week, timestamp=None) -> dict | None:
        """The net and system tariff paid when selling, None = those of get_tariffs()
        (a raw sell price includes no tariffs, but selling still costs these)."""
        return None

    def calculate_sell_price(self, timestamp, price) -> float:
        """Sell price: every tariff, tax and surcharge in the price is subtracted (leaving the spot
        price), and then what selling a kWh costs: the sell fee plus Energinet's net and system tariff."""
        timestamp = _naive(timestamp)
        tariff = self.get_tariffs(timestamp.hour, timestamp.weekday(), timestamp=timestamp)
        sell_costs = self.get_sell_costs(timestamp.hour, timestamp.weekday(), timestamp=timestamp)
        costs = tariff if sell_costs is None else sell_costs
        raw_price, sell_tariffs, sell_price = sell_price_parts(price, tariff, costs, self.sell_fee, self.decimals)
        self.sell_price_details[timestamp] = {
            "price": price,
            "price_timestamp": timestamp,
            "transmissions_nettarif": tariff["transmissions_nettarif"],
            "systemtarif": tariff["systemtarif"],
            "elafgift": tariff["elafgift"],
            "tariffs": tariff["tariffs"],
            "surcharge": tariff["surcharge"],
            "tariff_sum": tariff["tariff_sum"],
            "raw_price": raw_price,
            "sell_fee": self.sell_fee,
            "sell_transmissions_nettarif": costs["transmissions_nettarif"],
            "sell_systemtarif": costs["systemtarif"],
            "sell_tariffs": sell_tariffs,
            "solar_sell_price": sell_price,
        }
        return sell_price

    def sell_tariff_day(self) -> tuple[str, dict] | None:
        """(local date, record) of the tariffs the sell price of today is converted with, to keep for converting the
        recorded sell price of this day later (sell_price_from_tariff_day). None when today's tariffs are not known
        in full: a provider without a recorded sell entity, or one whose tariffs could be another day's."""
        return None

    def sell_price_from_tariff_day(self, moment: datetime.datetime, price: float, day: Mapping) -> float | None:
        """The sell price of a recorded price at the local moment, with the tariffs kept for its day
        (sell_tariff_day()); the same formula as calculate_sell_price(). None when the record can't convert it."""
        return None

    @staticmethod
    def _periods_in_hour(prices: dict, timestamp) -> int:
        hour_start = _naive(timestamp).replace(minute=0, second=0, microsecond=0)
        hour_end = hour_start + datetime.timedelta(hours=1)
        return sum(1 for start in prices if hour_start <= _naive(start) < hour_end)


class CombinedPriceProvider(BasePriceProvider):
    """Every price of the integration, from the configured providers in PRICE_PROVIDER_PRIORITY
    order (Strømligning before Energi Data Service).

    For each timestamp the first provider with a price wins, and real prices always win over
    forecasts. The rest of the integration only asks this class: the coordinator which
    entities to read (snapshot, recorded history), the engine the prices.

    The Home Assistant side methods only read the configuration, so the coordinator can call
    them on the event loop while the worker updates the prices.
    """

    source_name = "combined"

    def __init__(self, config: dict, providers: list[BasePriceProvider]) -> None:
        super().__init__(config)
        self.providers = providers
        self.processed_version = -1
        self.buy_prices: dict = {}
        self.sell_prices: dict = {}
        self.buy_price_sources: dict = {}
        self.sell_price_sources: dict = {}
        self.last_update = None
        for provider in providers:
            provider.shared_data = self.shared_data

    # Home Assistant side ------------------------------------------------- #

    def price_entity_ids(self) -> list[str]:
        """Every configured price entity (each once)."""
        return list(dict.fromkeys(entity_id for entity_id in self.entity_ids.values() if entity_id))

    def price_unit_problems(self, get_state: GetState) -> tuple[list[tuple[str, str]], bool]:
        """The configured price entities in a unit the prices are not read in: (entity id, unit) of each, and whether every entity
        was judged. A state that is missing, or unavailable or unknown without a unit, is not judged.

        A price sensor is judged by the rules of the key it is configured under (price_unit_error: Energy Price Forecast converts its
        own price per MWh, Wattcast gives ct/kWh); any other entity of a price integration (Strømligning's and Octopus Energy's sensors,
        the tariffs) by its unit alone (price_unit_wrong). Wattcast's forecast sensor has a unit of its own that its provider checks."""
        from .currency import PRICE_SENSOR_KEYS  # noqa: PLC0415 (currency.py imports this module)

        def wrong(key: str, unit: Any) -> bool:
            if key in PRICE_SENSOR_KEYS:
                return price_unit_error(key, unit)
            return price_unit_wrong(unit)

        keys_of: dict[str, list[str]] = {}
        for key, entity_id in self.entity_ids.items():
            if entity_id:
                keys_of.setdefault(entity_id, []).append(key)
        problems: list[tuple[str, str]] = []
        judged = True
        for entity_id, keys in keys_of.items():
            if keys == [WATTCAST_FORECAST_KEY]:
                continue  # its provider checks its unit: it neither names a repair nor keeps one open
            state = get_state(entity_id)
            if state is None:
                judged = False
                continue
            unit = state.attributes.get("unit_of_measurement")
            if unit is None and state.state in ENTITY_UNAVAILABLE_STATES:
                judged = False
                continue
            if any(wrong(key, unit) for key in keys):
                problems.append((entity_id, unit))
        return problems, judged

    def buy_price_entity_ids(self) -> list[str]:
        """The buy price entities of the providers, in priority order."""
        return [entity_id for entity_id in (p.get_buy_price_entity_id() for p in self.providers if p.records_history) if entity_id]

    def snapshot(self, get_state: GetState) -> dict:
        """The price entities for set_snapshot(): {"attributes": {...}, "values": {...}}."""
        attributes: dict[str, dict] = {}
        values: dict[str, float] = {}
        tariff_entities = {self.entity_ids.get(key) for key in TARIFF_STATE_KEYS} - {None, ""}
        for entity_id in self.price_entity_ids():
            state = get_state(entity_id)
            if state is None:
                continue
            attributes[entity_id] = dict(state.attributes)
            if entity_id in tariff_entities:
                value = float_or_none(state.state)
                values[entity_id] = value if value is not None else 0.0
        return {"attributes": attributes, "values": values}

    def price_unit_divisors(self, get_state: GetState) -> dict[str, float | None]:
        """{entity_id: divisor} of the Energy Price Forecast, Energi Data Service, EPEX Spot, GE-Spot and Nord Pool sensors whose recorded history (the statistics
        and states of the sensor itself) is not a price per kWh as it is: 1000.0 for a price per MWh of Energy Price Forecast, and
        None where the history is left out, never read as kWh: an Energy Price Forecast sensor that is not in Home Assistant (its
        unit is not known) and a sensor whose unit makes its prices 100 or 1000 times too large (price_unit_error: ct/kWh, or per
        MWh for Energi Data Service). The unit is the sensor's unit now; the recorder keeps no unit for the states, so a sensor
        that changed its unit has its old history read in the new one."""
        divisors: dict[str, float | None] = {}
        for provider in self.providers:
            source = getattr(provider, "source_name", "")
            if source not in ("energypriceforecast", "energidataservice", "epexspot", "gespot", "nordpool"):
                continue
            for entity_id in (provider.buy_entity_id, provider.sell_entity_id):
                if not entity_id:
                    continue
                state = get_state(entity_id)
                if state is None:
                    if source == "energypriceforecast":
                        divisors[entity_id] = None
                    continue
                unit = getattr(state, "attributes", {}).get("unit_of_measurement")
                if price_unit_error(provider.buy_key, unit):
                    divisors[entity_id] = None
                elif source == "energypriceforecast" and (divisor := price_unit_divisor(unit)) != 1.0:
                    divisors[entity_id] = divisor
        return divisors

    def sell_price_entity_ids(self) -> list[str]:
        """The sell price entities of the providers that have one, in priority order."""
        return list(dict.fromkeys(entity_id for entity_id in (p.get_sell_price_entity_id() for p in self.providers if p.records_history) if entity_id))

    def combine_history(self, history: Mapping[str, Mapping]) -> dict:
        """Recorded buy prices {entity_id: {timestamp: price}} -> {timestamp: price}: per
        timestamp the price of the first provider that has one."""
        combined: dict = {}
        for entity_id in self.buy_price_entity_ids():
            for timestamp, price in (history.get(entity_id) or {}).items():
                combined.setdefault(timestamp, price)
        return combined

    # prices (worker) ----------------------------------------------------- #

    def set_now(self, now: datetime.datetime, tz: datetime.tzinfo | None = None) -> None:
        """now: local time without tzinfo; tz: Home Assistant's time zone (None = drop the offset of a price row as it is)."""
        self.now = now
        self.tz = tz
        for provider in self.providers:
            provider.now = now
            provider.tz = tz

    def set_snapshot(self, price_data: dict) -> None:
        """New HA snapshot (replaces _update_shared_price_data)."""
        self.shared_data["attributes"] = dict(price_data.get("attributes", {}))
        self.shared_data["values"] = dict(price_data.get("values", {}))
        self.shared_data["version"] += 1

    def update_prices(self, force=False) -> None:
        self._sync_prices()

    def _sync_prices(self) -> None:
        if self.processed_version == self.shared_data["version"] or self.shared_data["version"] == 0:
            return
        for provider in self.providers:
            try:
                provider.update_prices()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Can't update prices from %s: %s %s", provider.source_name, err, type(err))
        self.buy_prices, self.buy_real_prices, self.buy_price_sources, _ = self._combine_or_empty("buy")
        self.sell_prices, self.sell_real_prices, self.sell_price_sources, self.sell_price_details = self._combine_or_empty("sell")
        self._apply_sells_nothing()
        self.last_update = self.now
        self.processed_version = self.shared_data["version"]

    def prices_loaded(self) -> bool:
        """Whether a price snapshot has arrived (before it, no prices is just the start; after it, no online prices is a problem)."""
        return self.shared_data["version"] != 0

    def _combine_or_empty(self, kind: str) -> tuple[dict, dict, dict, dict]:
        """_combine, but no prices at all (every sensor of the kind is down or in the wrong unit) is an empty result, not an error:
        the prices of the other kind stay, and the engine falls back to the cached prices and the price history (get_hour_prices)."""
        try:
            return self._combine(kind)
        except ValueError as err:
            _LOGGER.warning("No %s prices from the configured price providers: %s", kind, err)
            return {}, {}, {}, {}

    def _apply_sells_nothing(self) -> None:
        """A provider that says a sale is worth nothing (Octopus without an export tariff) makes it so in every hour of the buy prices: the sell
        prices of a provider with a forecast, for the days after tomorrow, are not a price this user can get."""
        provider = next((p for p in self.providers if getattr(p, "sells_nothing", False)), None)
        if provider is None or not self.buy_prices:
            return
        self.sell_prices = {timestamp: round(provider.calculate_sell_price(timestamp, 0.0), self.decimals) for timestamp in self.buy_prices}
        self.sell_real_prices = {timestamp: self.sell_prices[timestamp] for timestamp in self.buy_real_prices}
        self.sell_price_sources = {timestamp: f"{provider.source_name}:{'real' if timestamp in self.buy_real_prices else 'forecast'}" for timestamp in self.buy_prices}
        self.sell_price_details = {timestamp: provider.sell_price_details[timestamp].copy() for timestamp in self.buy_prices if timestamp in provider.sell_price_details}

    def _combine(self, kind: str) -> tuple[dict, dict, dict, dict]:
        """The prices of every provider on one common interval (the finest all of them fit on).

        A provider's price covers the interval to its next timestamp (an hourly price the whole hour) and is
        repeated over every slot of it. Per slot the first provider with a real price wins, then the first with a
        forecast, so one hour never gets a falsely cheap quarter from an hourly price next to another provider's
        quarters. A price that is no finite number covers its slots without a price, so another provider fills them."""
        prices: dict = {}
        real_prices: dict = {}
        sources: dict = {}
        details: dict = {}
        sell = kind == "sell"
        layers = []
        for provider in self.providers:
            entries: dict = {}
            real_starts: set = set()
            for label in ("forecast", "real"):
                try:
                    for timestamp, price in getattr(provider, f"get_{kind}_{label}_prices")().items():
                        timestamp = _naive(timestamp)
                        if label == "real" and not is_number(price) and is_number(entries.get(timestamp)):
                            continue  # a real price that is no number leaves the provider's forecast for it, as before
                        entries[timestamp] = price
                        if label == "real":
                            real_starts.add(timestamp)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Can't get %s prices from %s: %s %s", label, provider.source_name, err, type(err))
            try:
                layers.append((provider, provider_intervals(entries), real_starts))
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Can't read the %s prices of %s: %s %s", kind, provider.source_name, err, type(err))
        grid = common_interval_minutes(end - start for _provider, intervals, _real in layers for start, end, _price in intervals)
        for want_real in (True, False):
            label = "real" if want_real else "forecast"
            for provider, intervals, real_starts in layers:
                for start, end, price in intervals:
                    if (start in real_starts) != want_real or not is_number(price):
                        continue  # NaN/infinity is no price: the slots get another provider's price, else the price history's, else none
                    price = round(price, self.decimals)
                    for slot in interval_slots(start, end, grid):
                        if slot in prices:
                            continue
                        prices[slot] = price
                        if want_real:
                            real_prices[slot] = price
                        sources[slot] = f"{provider.source_name}:{label}"
                        if sell and start in provider.sell_price_details:
                            details[slot] = provider.sell_price_details[start].copy()
        if not prices:
            raise ValueError("No prices available from configured price providers")
        return (
            dict(sorted(prices.items())),
            dict(sorted(real_prices.items())),
            dict(sorted(sources.items())),
            dict(sorted(details.items())),
        )

    def get_buy_prices(self) -> dict:
        self._sync_prices()
        return self.buy_prices

    def get_sell_prices(self) -> dict:
        self._sync_prices()
        return self.sell_prices

    def get_buy_real_prices(self) -> dict:
        self._sync_prices()
        return self.buy_real_prices

    def get_sell_real_prices(self) -> dict:
        self._sync_prices()
        return self.sell_real_prices

    def get_buy_forecast_prices(self, adder: bool = True) -> dict:
        """The forecast buy prices; adder=False without the penalty per day ahead the providers
        add to prefer known prices."""
        self._sync_prices()
        return self._forecast(self.buy_prices, self.buy_price_sources, adder, 1.0)

    def get_sell_forecast_prices(self, adder: bool = True) -> dict:
        """The forecast sell prices; adder=False without the penalty (which makes a sale worth less)."""
        self._sync_prices()
        return self._forecast(self.sell_prices, self.sell_price_sources, adder, self.sell_adder_sign)

    def _forecast(self, prices: dict, sources: dict, adder: bool, sign: float) -> dict:
        current_hour = reset_time_to_hour(self.now)
        return {
            timestamp: price if adder else price - sign * days_between(current_hour, timestamp) / PRICE_ADDER_DAY_BETWEEN_DIVIDER * self.price_scale
            for timestamp, price in prices.items()
            if str(sources.get(timestamp, "")).endswith(":forecast")
        }

    def get_price_sources(self, sell_prices: bool = False) -> dict:
        """{timestamp: "<provider>:real" or "<provider>:forecast"}. Shared - do not modify."""
        self._sync_prices()
        return self.sell_price_sources if sell_prices else self.buy_price_sources

    def price_source(self, timestamp, sell_prices: bool = False) -> str:
        """The source ("<provider>:real", "<provider>:forecast" or "") of the price at exactly timestamp."""
        return self.get_price_sources(sell_prices).get(_naive(timestamp), "")

    def is_forecast(self, timestamp, sell_prices: bool = False) -> bool:
        return self.price_source(timestamp, sell_prices).endswith(":forecast")

    def get_buy_price(self, timestamp=None) -> float:
        self._sync_prices()
        timestamp = _naive(timestamp if timestamp is not None else self.now)
        # the real price, else the forecast of the hour (e.g. after midnight before the next price update): a missing price
        # would book the charging at 0
        result = self.get_price_at_timestamp(self.buy_prices, timestamp)
        if result is None:
            raise ValueError(f"No buy price available for {timestamp}")
        return result["price"]

    def get_sell_price(self, timestamp=None) -> dict:
        self._sync_prices()
        timestamp = _naive(timestamp if timestamp is not None else self.now)
        result = self.get_price_at_timestamp(self.sell_prices, timestamp)
        if result is None:
            raise ValueError(f"No sell price available for {timestamp}")
        start = result["price_timestamp"]
        details = self.sell_price_details.get(start)
        if details is None:
            return {"price": result["price"], "details": {"price_timestamp": start, "solar_sell_price": result["price"]}}
        return {"price": result["price"], "details": details.copy()}

    def buy_prices_available(self) -> bool:
        self._sync_prices()
        return bool(self.buy_prices)

    def get_buy_price_entity_id(self) -> str:
        return ", ".join(provider.get_buy_price_entity_id() for provider in self.providers if provider.get_buy_price_entity_id())

    def get_tariffs(self, hour, day_of_week, timestamp=None) -> dict:
        self._sync_prices()
        if timestamp is None:
            timestamp = self.now.replace(hour=hour, minute=0, second=0, microsecond=0)
        result = self.get_price_at_timestamp(self.sell_price_details, _naive(timestamp))
        if result is not None:
            details = result["price"]
            return {key: details.get(key, 0.0) for key in _ZERO_TARIFFS}
        return dict(_ZERO_TARIFFS)

    def sell_tariff_days(self) -> dict[str, tuple[str, dict]]:
        """{sell entity: (local date, record)}: today's tariffs of the sell price of every provider with a recorded
        sell entity that knows them in full (see BasePriceProvider.sell_tariff_day)."""
        days: dict[str, tuple[str, dict]] = {}
        for provider in self.providers:
            entity_id = provider.get_sell_price_entity_id()
            if not entity_id or entity_id in days:
                continue
            try:
                day = provider.sell_tariff_day()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Can't read today's sell tariffs from %s: %s %s", provider.source_name, err, type(err))
                continue
            if day is not None:
                days[entity_id] = day
        return days

    def convert_recorded_sell_price(self, entity_id: str, moment: datetime.datetime, price: float, day: Any) -> float | None:
        """A recorded price of the sell entity at the local moment as the sell price, with the tariffs kept for its
        day (a record of sell_tariff_days()). None without such a record: never with another day's tariffs or none."""
        provider = next((p for p in self.providers if p.get_sell_price_entity_id() == entity_id), None) if entity_id else None
        if provider is None or not isinstance(day, Mapping) or not is_number(price):
            return None
        try:
            value = provider.sell_price_from_tariff_day(_naive(moment), price, day)
        except Exception:  # noqa: BLE001
            return None
        return value if is_number(value) else None

    def get_buy_periods_in_hour(self, timestamp) -> int:
        self._sync_prices()
        return self._periods_in_hour(self.buy_real_prices, timestamp) or 1

    def get_sell_periods_in_hour(self, timestamp) -> int:
        self._sync_prices()
        return self._periods_in_hour(self.sell_real_prices, timestamp) or 1

    def get_current_price_timestamp(self, timestamp=None, sell_prices: bool = False):
        self._sync_prices()
        timestamp = _naive(timestamp if timestamp is not None else self.now)
        result = self.get_price_at_timestamp(self.sell_real_prices if sell_prices else self.buy_real_prices, timestamp)
        return None if result is None else result["price_timestamp"]
