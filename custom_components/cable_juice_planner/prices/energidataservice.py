"""Energi Data Service price provider."""
from __future__ import annotations

import datetime
import logging

from ..utils.helpers import days_between, reset_time_to_hour, to_datetime
from .base import BasePriceProvider, _ZERO_TARIFFS, _naive, is_number, sell_price_parts

_LOGGER = logging.getLogger(__name__)


class EnergiDataServicePriceProvider(BasePriceProvider):
    """https://github.com/MTrab/energidataservice (raw_today / raw_tomorrow / forecast)."""

    source_name = "energidataservice"
    buy_key = "energidataservice_buy_entity_id"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.buy_entity_id = self.entity_ids.get(self.buy_key, "")
        self.sell_entity_id = self.entity_ids.get("energidataservice_sell_entity_id", "") or self.buy_entity_id

    def get_buy_price_entity_id(self) -> str:
        return self.buy_entity_id

    def get_sell_price_entity_id(self) -> str:
        return self.sell_entity_id

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

    @staticmethod
    def _rows(rows):
        for raw in rows or []:
            timestamp = _naive(to_datetime(raw["hour" if "hour" in raw else "time"]))
            price = raw["price"]
            if isinstance(timestamp, datetime.datetime) and is_number(price):
                yield timestamp, price

    def _get_real_prices(self, entity_id) -> dict:
        current_hour = reset_time_to_hour(self.now)
        if not self._entity_exists(entity_id):
            raise ValueError(f"{entity_id} not loaded")
        attr = self._get_entity_attr(entity_id)
        prices = {ts: p for ts, p in self._rows(attr.get("raw_today", [])) if days_between(current_hour, ts) == 0}
        if attr.get("tomorrow_valid"):
            prices.update(self._rows(attr.get("raw_tomorrow", [])))
        return dict(sorted(prices.items()))

    def _get_forecast_prices(self, entity_id, sell=False) -> dict:
        current_hour = reset_time_to_hour(self.now)
        if not self._entity_exists(entity_id):
            raise ValueError(f"{entity_id} not loaded")
        prices = {}
        for timestamp, price in self._rows(self._get_entity_attr(entity_id).get("forecast", [])):
            if days_between(current_hour, timestamp) > 0:
                prices[timestamp] = price + self._forecast_adder(timestamp, sell)
        return dict(sorted(prices.items()))

    def get_tariffs(self, hour, day_of_week, timestamp=None) -> dict:
        try:
            if not self._entity_exists(self.sell_entity_id):
                raise ValueError(f"{self.sell_entity_id} not loaded")
            power_prices_attr = self._get_entity_attr(self.sell_entity_id)
            if "tariffs" not in power_prices_attr:
                return dict(_ZERO_TARIFFS)
            attr = power_prices_attr["tariffs"]
            additional = attr["additional_tariffs"]
            tariffs = attr["tariffs"][str(hour)]
            return {
                "transmissions_nettarif": additional["transmissions_nettarif"],
                "systemtarif": additional["systemtarif"],
                "elafgift": additional["elafgift"],
                "tariffs": tariffs,
                "surcharge": 0.0,
                "tariff_sum": sum([additional["transmissions_nettarif"], additional["systemtarif"], additional["elafgift"], tariffs]),
            }
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("get_tariffs(hour = %s, day_of_week = %s): %s", hour, day_of_week, err)
            return dict(_ZERO_TARIFFS)

    def get_sell_costs(self, hour, day_of_week, timestamp=None) -> dict | None:
        """A raw sell sensor (no tariffs attribute) includes no tariffs, but selling still costs
        Energinet's net and system tariff: those of the buy sensor."""
        if self._get_entity_attr(self.sell_entity_id).get("tariffs"):
            return None
        return eds_sell_costs(self._get_entity_attr(self.buy_entity_id))

    def sell_tariff_day(self) -> tuple[str, dict] | None:
        """Today's tariffs of the sell price, kept per day because Energi Data Service does not record its tariffs
        attribute in the recorder: the recorded sell price of an earlier day can only be converted with this record.

        Only when the sell sensor's prices are today's, so attributes from before midnight are never kept as the new
        day's. A sensor with the tariffs attribute: the tariffs in its price per hour, and its net and system tariff as
        the selling costs. A raw sensor (no tariffs attribute): no tariffs in the price, the selling costs from the buy
        sensor. Nothing is kept unless every value is a number (a missing tariff is never kept as 0)."""
        if not self._entity_exists(self.sell_entity_id):
            return None
        attr = self._get_entity_attr(self.sell_entity_id)
        today = self.now.date()
        if {timestamp.date() for timestamp, _ in self._rows(attr.get("raw_today"))} != {today}:
            return None
        if "tariffs" in attr:
            in_price = eds_tariff_schedule(attr["tariffs"])
            if in_price is None:
                return None
            costs = {key: in_price[key] for key in ("transmissions_nettarif", "systemtarif")}
        else:
            in_price = None
            costs = eds_sell_costs(self._get_entity_attr(self.buy_entity_id)) if self._entity_exists(self.buy_entity_id) else None
            if costs is None or not all(is_number(value) for value in costs.values()):
                return None
        return today.isoformat(), {"tariffs": in_price, "sell_costs": costs, "sell_fee": self.sell_fee}

    def sell_price_from_tariff_day(self, moment, price, day) -> float | None:
        """calculate_sell_price() with the tariffs of a sell_tariff_day() record instead of today's attributes."""
        in_price, costs, fee = day.get("tariffs"), day.get("sell_costs"), day.get("sell_fee")
        if not isinstance(costs, dict) or not is_number(fee) or not all(is_number(costs.get(key)) for key in ("transmissions_nettarif", "systemtarif")):
            return None
        if in_price is None:
            tariff = dict(_ZERO_TARIFFS)  # a raw sell sensor: no tariffs in its price
        elif isinstance(in_price, dict) and isinstance(in_price.get("hours"), dict):
            values = [in_price.get("transmissions_nettarif"), in_price.get("systemtarif"), in_price.get("elafgift"), in_price["hours"].get(str(moment.hour))]
            if not all(is_number(value) for value in values):
                return None
            # the same keys and sum (in the same order) as get_tariffs()
            tariff = {"transmissions_nettarif": values[0], "systemtarif": values[1], "elafgift": values[2], "tariffs": values[3], "surcharge": 0.0, "tariff_sum": sum(values)}
        else:
            return None
        return sell_price_parts(price, tariff, costs, abs(fee))[2]


def eds_tariff_schedule(tariffs) -> dict | None:
    """The tariffs attribute of an Energi Data Service sensor as {net, system tariff, elafgift, hours: {"0".."23"}};
    None unless all of it is there as numbers (the keys get_tariffs() reads)."""
    try:
        additional = tariffs["additional_tariffs"]
        hourly = tariffs["tariffs"]
        schedule = {key: additional[key] for key in ("transmissions_nettarif", "systemtarif", "elafgift")}
        hours = {str(hour): hourly[str(hour)] for hour in range(24)}
    except (KeyError, TypeError):
        return None
    if not all(is_number(value) for value in (*schedule.values(), *hours.values())):
        return None
    return {**{key: float(value) for key, value in schedule.items()}, "hours": {key: float(value) for key, value in hours.items()}}


def eds_sell_costs(attributes: dict | None) -> dict[str, float] | None:
    """Energinet's net and system tariff from an Energi Data Service tariffs attribute, else None."""
    try:
        additional = (attributes or {})["tariffs"]["additional_tariffs"]
        return {
            "transmissions_nettarif": float(additional["transmissions_nettarif"]),
            "systemtarif": float(additional["systemtarif"]),
        }
    except (KeyError, TypeError, ValueError):
        return None
