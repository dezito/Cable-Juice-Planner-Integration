"""Energy Price Forecast EU price provider (a price and its forecast for most of Europe, also outside Denmark)."""
from __future__ import annotations

import datetime

from ..utils.helpers import days_between, reset_time_to_hour
from .base import BasePriceProvider, _ZERO_TARIFFS, is_number, price_timestamp, price_unit_divisor


class EnergyPriceForecastPriceProvider(BasePriceProvider):
    """The Energy Price Forecast integration (domain energypriceforecast): raw_today / raw_tomorrow are published prices, raw_forecast is the forecast.

    Every row is {"start", "end", "value"}: the start is an ISO timestamp with an offset (UTC with a "Z" is normal) that is
    converted to Home Assistant's time zone, and a row is an hour or a quarter of an hour. The buy sensor is the retail
    price sensor (the price a kWh costs the household). The config flow requires a sell sensor (the wholesale price: the
    retail price is higher than what a sale pays). An entry without one (a restored backup or an import) takes the buy
    sensor's price, which is higher than a sale pays. The price has no tariffs attribute, so no
    tariff is taken out of the sell price: it is the price minus the sell fee.
    """

    source_name = "energypriceforecast"
    buy_key = "energypriceforecast_buy_entity_id"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.buy_entity_id = self.entity_ids.get(self.buy_key, "")
        self.sell_entity_id = self.entity_ids.get("energypriceforecast_sell_entity_id", "") or self.buy_entity_id

    def get_buy_price_entity_id(self) -> str:
        return self.buy_entity_id

    def get_sell_price_entity_id(self) -> str:
        return self.sell_entity_id

    @classmethod
    def dashboard_entities(cls, prices: dict[str, str]) -> dict[str, str]:
        return {
            "sensor.energy_price_forecast": prices["energypriceforecast_buy_entity_id"],
            "sensor.energy_price_forecast_sell": prices["energypriceforecast_sell_entity_id"],
        }

    def _load_buy_real_prices(self):
        return self._get_real_prices(self.buy_entity_id)

    def _load_buy_forecast_prices(self):
        return self._get_forecast_prices(self.buy_entity_id)

    def _load_sell_real_prices(self):
        prices = self._get_real_prices(self.sell_entity_id)
        for timestamp, price in prices.items():
            prices[timestamp] = self.calculate_sell_price(timestamp, price)
        return prices

    def _load_sell_forecast_prices(self):
        prices = self._get_forecast_prices(self.sell_entity_id, sell=True)
        for timestamp, price in prices.items():
            prices[timestamp] = self.calculate_sell_price(timestamp, price)
        return prices

    def _divisor(self, entity_id) -> float:
        """1000 for a sensor that gives the price per MWh (its prices are converted to kWh), else 1."""
        return price_unit_divisor(self._get_entity_attr(entity_id).get("unit_of_measurement"))

    def _rows(self, rows, divisor: float = 1.0):
        """(start as naive local time, price per kWh) of every row with a timestamp and a number; the first row of a start wins
        (the repeated hour when the clocks go back is one start in local time, like the other providers)."""
        seen = set()
        for raw in rows if isinstance(rows, list) else []:
            if not isinstance(raw, dict):
                continue
            timestamp = price_timestamp(raw.get("start"), self.tz)
            price = raw.get("value")
            if isinstance(timestamp, datetime.datetime) and is_number(price) and timestamp not in seen:
                seen.add(timestamp)
                yield timestamp, price / divisor

    def _get_real_prices(self, entity_id) -> dict:
        current_hour = reset_time_to_hour(self.now)
        if not self._entity_exists(entity_id):
            raise ValueError(f"{entity_id} not loaded")
        return dict(sorted(self._published(entity_id, current_hour).items()))

    def _published(self, entity_id, current_hour) -> dict:
        """The published prices of today and tomorrow, from both lists: the integration may split the days by its own
        market day or roll the lists over after midnight, so a row is judged by its own local date, not by its list
        (today's rows can still be in raw_tomorrow, and an hour of tomorrow in raw_today). The first row of a start wins."""
        attr = self._get_entity_attr(entity_id)
        prices: dict = {}
        for attribute in ("raw_today", "raw_tomorrow"):
            for timestamp, price in self._rows(attr.get(attribute), self._divisor(entity_id)):
                if 0 <= days_between(current_hour, timestamp) <= 1:
                    prices.setdefault(timestamp, price)
        return prices

    def _get_forecast_prices(self, entity_id, sell=False) -> dict:
        current_hour = reset_time_to_hour(self.now)
        if not self._entity_exists(entity_id):
            raise ValueError(f"{entity_id} not loaded")
        real = self._published(entity_id, current_hour)
        published_hours = {reset_time_to_hour(timestamp) for timestamp in real}
        prices = {}
        for timestamp, price in self._rows(self._get_entity_attr(entity_id).get("raw_forecast"), self._divisor(entity_id)):
            # from tomorrow on: a market without published prices (today only a forecast, no price before the present) is
            # refused on the price page; the own price source of Energy Price Forecast gives it published prices. A
            # forecast never enters an hour with a published price (not even a quarter of a published hour, when the two
            # lists have different periods)
            if days_between(current_hour, timestamp) > 0 and reset_time_to_hour(timestamp) not in published_hours:
                prices[timestamp] = price + self._forecast_adder(timestamp, sell)
        return dict(sorted(prices.items()))

    def get_tariffs(self, hour, day_of_week, timestamp=None) -> dict:
        """No tariffs in the price, and none to take out of it."""
        return dict(_ZERO_TARIFFS)
