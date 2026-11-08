"""Strømligning price provider."""
from __future__ import annotations

import datetime
import logging

from ..utils.helpers import to_datetime
from .base import BasePriceProvider, _naive

_LOGGER = logging.getLogger(__name__)


class StromligningPriceProvider(BasePriceProvider):
    """Strømligning ('prices' attribute with start/price, tariffs as separate sensors)."""

    source_name = "stromligning"
    buy_key = "stromligning_buy_current_price"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        get = self.entity_ids.get
        self.buy_current_price = get(self.buy_key, "")
        self.buy_tomorrow_available = get("stromligning_buy_tomorrow_available", "")
        self.buy_forecasts = get("stromligning_buy_forecasts", "")
        self.sell_current_price = get("stromligning_sell_current_price", "")
        self.sell_tomorrow_available = get("stromligning_sell_tomorrow_available", "")
        self.sell_forecasts = get("stromligning_sell_forecasts", "")
        self.sell_distribution = get("stromligning_sell_distribution", "")
        self.sell_electricity_tax = get("stromligning_sell_electricity_tax", "")
        self.sell_nettariff = get("stromligning_sell_nettariff", "")
        self.sell_systemtariff = get("stromligning_sell_systemtariff", "")
        self.sell_surcharge = get("stromligning_sell_surcharge", "")
        self._distribution_loaded = False
        self._distribution_prices: dict = {}

    def update_prices(self) -> None:
        self._distribution_loaded = False
        self._distribution_prices = {}
        super().update_prices()

    def get_buy_price_entity_id(self) -> str:
        return self.buy_current_price

    def _load_buy_real_prices(self):
        return self._get_real_prices(self.buy_current_price, self.buy_tomorrow_available)

    def _load_buy_forecast_prices(self):
        return self._get_forecast_prices(self.buy_forecasts, self.buy_tomorrow_available)

    def _load_sell_real_prices(self):
        prices = self._get_real_prices(self.sell_current_price, self.sell_tomorrow_available)
        for timestamp, price in prices.items():
            prices[timestamp] = self.calculate_sell_price(timestamp, price)
        return prices

    def _load_sell_forecast_prices(self):
        prices = self._get_forecast_prices(self.sell_forecasts, self.sell_tomorrow_available, sell=True)
        for timestamp, price in prices.items():
            prices[timestamp] = self.calculate_sell_price(timestamp, price)
        return prices

    def _get_real_prices(self, current_entity_id, tomorrow_entity_id) -> dict:
        prices: dict = {}
        self._add_prices_from_entity(prices, current_entity_id, forecast=False)
        if self._entity_exists(tomorrow_entity_id) and not self._get_entity_attr(tomorrow_entity_id).get("forecast_data", False):
            self._add_prices_from_entity(prices, tomorrow_entity_id, forecast=False)
        return dict(sorted(prices.items()))

    def _get_forecast_prices(self, forecast_entity_id, tomorrow_entity_id=None, sell=False) -> dict:
        prices: dict = {}
        if self._entity_exists(tomorrow_entity_id) and self._get_entity_attr(tomorrow_entity_id).get("forecast_data", False):
            self._add_prices_from_entity(prices, tomorrow_entity_id, forecast=True, sell=sell)
        self._add_prices_from_entity(prices, forecast_entity_id, forecast=True, sell=sell)
        return dict(sorted(prices.items()))

    def _add_prices_from_entity(self, prices: dict, entity_id, forecast=False, sell=False) -> None:
        if not entity_id:
            return
        if not self._entity_exists(entity_id):
            raise ValueError(f"{entity_id} not loaded")
        attr = self._get_entity_attr(entity_id)
        if "prices" not in attr:
            raise ValueError(f"prices not in {entity_id} attributes")
        for raw in attr["prices"]:
            timestamp = _naive(to_datetime(raw.get("start")))
            price = raw.get("price")
            if not isinstance(timestamp, datetime.datetime) or not isinstance(price, (int, float)):
                continue
            if timestamp in prices:
                continue
            if forecast:
                price += self._forecast_adder(timestamp, sell)
            prices[timestamp] = price

    def get_tariffs(self, hour, day_of_week, timestamp=None) -> dict:
        if timestamp is None:
            timestamp = self.now.replace(hour=hour, minute=0, second=0, microsecond=0)
        timestamp = _naive(timestamp)
        # Same keys as Energi Data Service: "tariffs" is the grid company's tariff of the period
        # (distribution), "transmissions_nettarif" Energinet's net tariff.
        values = {
            "transmissions_nettarif": self._get_entity_state(self.sell_nettariff),
            "systemtarif": self._get_entity_state(self.sell_systemtariff),
            "elafgift": self._get_entity_state(self.sell_electricity_tax),
            "tariffs": self._get_distribution_price(timestamp),
            "surcharge": self._get_entity_state(self.sell_surcharge),
        }
        return {**values, "tariff_sum": sum(values.values())}

    def _get_distribution_price(self, timestamp) -> float:
        if not self._distribution_loaded:
            self._distribution_loaded = True
            self._distribution_prices = {}
            if self._entity_exists(self.sell_distribution):
                for raw in self._get_entity_attr(self.sell_distribution).get("prices", []):
                    price_timestamp = _naive(to_datetime(raw.get("start")))
                    price = raw.get("price")
                    if isinstance(price_timestamp, datetime.datetime) and isinstance(price, (int, float)):
                        self._distribution_prices[price_timestamp] = price
        result = self.get_price_at_timestamp(self._distribution_prices, timestamp)
        if result is None and self._distribution_prices:
            # The sensor only has today's tariffs, but tomorrow's and the forecast prices include
            # them too. The tariff follows the time of day: use the latest known day's period
            # (0 would make the sell price too high; it still is 0 without the sensor or before its first row).
            latest = max(self._distribution_prices).date()
            result = self.get_price_at_timestamp(self._distribution_prices, datetime.datetime.combine(latest, timestamp.time()))
        return result["price"] if result is not None else 0.0

    def _get_entity_state(self, entity_id) -> float:
        if not entity_id or not self._entity_exists(entity_id):
            return 0.0
        value = self._get_cached_entity_state(entity_id)
        return value if isinstance(value, (int, float)) else 0.0
