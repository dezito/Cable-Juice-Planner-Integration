"""Shared price helpers, the base provider and the combined provider."""
from __future__ import annotations

import datetime
import logging
import math
from collections.abc import Callable, Mapping
from typing import Any

from ..const import PRICE_ADDER_DAY_BETWEEN_DIVIDER
from ..utils.helpers import days_between, float_or_none, reset_time_to_hour

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


def is_number(value) -> bool:
    """A usable price: a finite number (not None, text, a bool, NaN or infinity)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class BasePriceProvider:
    source_name = "base"
    # config key of the buy price entity
    buy_key = ""

    def __init__(self, config: dict) -> None:
        prices_config = config.get("prices", {})
        self.entity_ids = dict(prices_config.get("entity_ids", {}))
        self.refund = abs(prices_config.get("refund", 0.0))
        self.sell_fee = abs(prices_config.get("sell_fee", 0.0))
        self.sell_adder_sign = sell_adder_sign(config)
        self.buy_real_prices: dict = {}
        self.buy_forecast_prices: dict = {}
        self.sell_real_prices: dict = {}
        self.sell_forecast_prices: dict = {}
        self.sell_price_details: dict = {}
        self.shared_data: dict = {"attributes": {}, "values": {}, "version": 0}
        self.now = datetime.datetime.now().replace(microsecond=0)

    @staticmethod
    def get_price_at_timestamp(prices: dict, timestamp):
        """Latest price at or before timestamp on the same day."""
        if not prices:
            return None
        timestamp = _naive(timestamp)
        best = None
        for price_timestamp in prices:
            price_timestamp = _naive(price_timestamp)
            if price_timestamp.date() == timestamp.date() and price_timestamp <= timestamp:
                if best is None or price_timestamp > best:
                    best = price_timestamp
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

    def _forecast_adder(self, timestamp, sell: bool = False) -> float:
        """Forecast prices get a small penalty per day ahead to prefer known prices.

        A forecast buy price is dearer, and a sale at a forecast sell price is worth less (sell_adder_sign)."""
        adder = days_between(reset_time_to_hour(self.now), timestamp) / PRICE_ADDER_DAY_BETWEEN_DIVIDER
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
        raw_price = price - tariff["tariff_sum"]
        sell_tariffs = sum((self.sell_fee, costs["transmissions_nettarif"], costs["systemtarif"]))
        sell_price = round(raw_price - sell_tariffs, 2)
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

    @staticmethod
    def _periods_in_hour(prices: dict, timestamp) -> int:
        hour_start = _naive(timestamp).replace(minute=0, second=0, microsecond=0)
        hour_end = hour_start + datetime.timedelta(hours=1)
        return sum(1 for price_timestamp in prices if hour_start <= _naive(price_timestamp) < hour_end)


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

    def buy_price_entity_ids(self) -> list[str]:
        """The buy price entities of the providers, in priority order."""
        return [entity_id for entity_id in (p.get_buy_price_entity_id() for p in self.providers) if entity_id]

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

    def sell_price_entity_ids(self) -> list[str]:
        """The sell price entities of the providers that have one, in priority order."""
        return list(dict.fromkeys(entity_id for entity_id in (p.get_sell_price_entity_id() for p in self.providers) if entity_id))

    def combine_history(self, history: Mapping[str, Mapping]) -> dict:
        """Recorded buy prices {entity_id: {timestamp: price}} -> {timestamp: price}: per
        timestamp the price of the first provider that has one."""
        combined: dict = {}
        for entity_id in self.buy_price_entity_ids():
            for timestamp, price in (history.get(entity_id) or {}).items():
                combined.setdefault(timestamp, price)
        return combined

    # prices (worker) ----------------------------------------------------- #

    def set_now(self, now: datetime.datetime) -> None:
        self.now = now
        for provider in self.providers:
            provider.now = now

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
        self.buy_prices, self.buy_real_prices, self.buy_price_sources, _ = self._combine("buy")
        self.sell_prices, self.sell_real_prices, self.sell_price_sources, self.sell_price_details = self._combine("sell")
        self.last_update = self.now
        self.processed_version = self.shared_data["version"]

    def _combine(self, kind: str) -> tuple[dict, dict, dict, dict]:
        prices: dict = {}
        real_prices: dict = {}
        sources: dict = {}
        details: dict = {}
        sell = kind == "sell"
        for provider in self.providers:
            try:
                for timestamp, price in getattr(provider, f"get_{kind}_real_prices")().items():
                    timestamp = _naive(timestamp)
                    if not is_number(price):
                        continue  # NaN/infinity is no price: the hour gets another provider's price, else the price history's, else none
                    if timestamp not in prices:
                        price = round(price, 2)
                        prices[timestamp] = price
                        real_prices[timestamp] = price
                        sources[timestamp] = f"{provider.source_name}:real"
                        if sell and timestamp in provider.sell_price_details:
                            details[timestamp] = provider.sell_price_details[timestamp].copy()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Can't get real prices from %s: %s %s", provider.source_name, err, type(err))
        for provider in self.providers:
            try:
                for timestamp, price in getattr(provider, f"get_{kind}_forecast_prices")().items():
                    timestamp = _naive(timestamp)
                    if not is_number(price):
                        continue
                    if timestamp not in prices:
                        prices[timestamp] = round(price, 2)
                        sources[timestamp] = f"{provider.source_name}:forecast"
                        if sell and timestamp in provider.sell_price_details:
                            details[timestamp] = provider.sell_price_details[timestamp].copy()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Can't get forecast prices from %s: %s %s", provider.source_name, err, type(err))
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
            timestamp: price if adder else price - sign * days_between(current_hour, timestamp) / PRICE_ADDER_DAY_BETWEEN_DIVIDER
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
        result = self.get_price_at_timestamp(self.buy_real_prices, timestamp)
        if result is None:
            raise ValueError(f"No real buy price available for {timestamp}")
        return result["price"]

    def get_sell_price(self, timestamp=None) -> dict:
        self._sync_prices()
        timestamp = _naive(timestamp if timestamp is not None else self.now)
        result = self.get_price_at_timestamp(self.sell_prices, timestamp)
        if result is None:
            raise ValueError(f"No sell price available for {timestamp}")
        price_timestamp = result["price_timestamp"]
        details = self.sell_price_details.get(price_timestamp)
        if details is None:
            return {"price": result["price"], "details": {"price_timestamp": price_timestamp, "solar_sell_price": result["price"]}}
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
