"""The providers of the spot forecast integrations: wholesale prices with a forecast, in one sensor (Wattcast: two).

These sensors give the exchange price, not a retail price. The buy price is made from it with the VAT and the surcharge of the
user (a fixed amount per kWh for the grid fee, the taxes and the supplier's margin): spot x (1 + VAT) + surcharge. The sell price
is the spot price less the sell fee. Without a VAT and a surcharge there are no prices at all (never a buy price without them,
it would be too low). The recorded history of the sensors is not a retail price either, so it is not used (``records_history``).
"""
from __future__ import annotations

import logging

from ..utils.helpers import days_between, reset_time_to_hour
from .base import BasePriceProvider, _ZERO_TARIFFS, price_timestamp
from .spot_rows import Row, elprognos_rows, spot_forecast_rows, wattcast_rows

_LOGGER = logging.getLogger(__name__)


class SpotPriceProvider(BasePriceProvider):
    """Wholesale prices of one sensor: the published prices are real, the rest of the days after them is the forecast."""

    # the recorded state of the sensor is a wholesale price (or a mean of the forecast): never read as a buy price
    records_history = False
    display_name = ""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        prices_config = config.get("prices", {})
        self.buy_entity_id = self.entity_ids.get(self.buy_key, "")
        self.sell_entity_id = self.buy_entity_id
        self.vat = prices_config.get("spot_vat")
        self.surcharge = prices_config.get("spot_surcharge")
        self._spot: tuple[dict, dict] = ({}, {})
        self._no_published_logged: set[str] = set()

    def get_buy_price_entity_id(self) -> str:
        return self.buy_entity_id

    def get_sell_price_entity_id(self) -> str:
        return ""  # no recorded sell price of its own (see records_history)

    def _read_rows(self) -> list[Row]:
        raise NotImplementedError

    def _local(self, value):
        return price_timestamp(value, self.tz)

    def _read_spot(self) -> tuple[dict, dict]:
        """({start: spot price} published today and tomorrow, {start: spot price} forecast from tomorrow on)."""
        if not self._entity_exists(self.buy_entity_id):
            raise ValueError(f"{self.buy_entity_id} not loaded")
        if self.vat is None or self.surcharge is None:
            raise ValueError("The VAT and the surcharge of the spot prices are not set: no buy price without them")
        if self._wrong_unit(self.buy_entity_id):
            return {}, {}
        current_hour = reset_time_to_hour(self.now)
        rows = sorted(self._read_rows(), key=lambda row: row[0])
        if rows and not any(is_published for _start, _price, is_published in rows):
            # not the price sensor of the integration, or a shape that is not read: no price for today, and no way to tell why
            if self.buy_entity_id not in self._no_published_logged:
                self._no_published_logged.add(self.buy_entity_id)
                _LOGGER.warning("%s has price rows but none of them is published: check that it is the price sensor of the integration", self.buy_entity_id)
        else:
            self._no_published_logged.discard(self.buy_entity_id)
        published: dict = {}
        for start, price, is_published in rows:
            if is_published and 0 <= days_between(current_hour, start) <= 1:
                published.setdefault(start, price)
        published_hours = {reset_time_to_hour(start) for start in published}
        forecast: dict = {}
        for start, price, is_published in rows:
            # from tomorrow on: today's hours without a published price are not planned; a forecast never enters an hour that
            # has a published price (not even a quarter of one, when the two have different periods)
            if not is_published and days_between(current_hour, start) > 0 and reset_time_to_hour(start) not in published_hours:
                forecast.setdefault(start, price)
        return published, forecast

    def update_prices(self) -> None:
        self._spot = self._read_spot()  # raises when the sensor, the VAT or the surcharge is missing: the old prices are kept
        super().update_prices()

    def _buy(self, spot: float) -> float:
        return spot * (1.0 + float(self.vat) / 100.0) + float(self.surcharge)

    def _load_buy_real_prices(self):
        return {start: self._buy(price) for start, price in self._spot[0].items()}

    def _load_buy_forecast_prices(self):
        return {start: self._buy(price) + self._forecast_adder(start) for start, price in self._spot[1].items()}

    def _load_sell_real_prices(self):
        return {start: self.calculate_sell_price(start, price) for start, price in self._spot[0].items()}

    def _load_sell_forecast_prices(self):
        return {start: self.calculate_sell_price(start, price + self._forecast_adder(start, True)) for start, price in self._spot[1].items()}

    def get_tariffs(self, hour, day_of_week, timestamp=None) -> dict:
        """No tariffs in the price, and none to take out of it."""
        return dict(_ZERO_TARIFFS)


class SpotForecastPriceProvider(SpotPriceProvider):
    """cygnusb/ha-spot-forecast (domain spot_forecast): the price sensor of a zone, EUR/kWh without VAT."""

    source_name = "spotforecast"
    display_name = "Spot Forecast"
    buy_key = "spotforecast_buy_entity_id"
    # its zones are DE-LU, NL, DK1, DK2 and NO2; Denmark is left out on purpose: a flat surcharge cannot follow the time-of-day
    # grid tariff there, Energi Data Service and Strømligning are better
    countries = frozenset({"DE", "LU", "NL", "NO"})

    @classmethod
    def dashboard_entities(cls, prices: dict[str, str]) -> dict[str, str]:
        return {"sensor.spot_forecast": prices["spotforecast_buy_entity_id"]}

    def _read_rows(self) -> list[Row]:
        return spot_forecast_rows(self._get_entity_attr(self.buy_entity_id), self._local)


class ElprognosPriceProvider(SpotPriceProvider):
    """AlexAsplund/ha-elprognos (domain elprisprognos): the forecast sensor of a Swedish zone, SEK/kWh without VAT."""

    source_name = "elprognos"
    display_name = "Elprisprognos"
    buy_key = "elprognos_buy_entity_id"
    countries = frozenset({"SE"})

    @classmethod
    def dashboard_entities(cls, prices: dict[str, str]) -> dict[str, str]:
        return {"sensor.elpris_prognos": prices["elprognos_buy_entity_id"]}

    def _read_rows(self) -> list[Row]:
        return elprognos_rows(self._get_entity_attr(self.buy_entity_id), self._local)


class WattcastPriceProvider(SpotPriceProvider):
    """SectorTll/wattcast-homeassistant (domain wattcast): the price sensor and the forecast sensor of a Baltic zone or Finland.

    Its sensors give ct/kWh (euro): the only price sensor in a subunit that is accepted, because the unit is the source's own
    and known, and it is divided by 100 (spot_rows.WATTCAST_DIVISOR); any other unit is a wrong unit and gives no prices."""

    source_name = "wattcast"
    display_name = "Wattcast"
    buy_key = "wattcast_buy_entity_id"
    forecast_key = "wattcast_forecast_entity_id"
    countries = frozenset({"EE", "FI", "LV", "LT"})

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.forecast_entity_id = self.entity_ids.get(self.forecast_key, "")

    @classmethod
    def dashboard_entities(cls, prices: dict[str, str]) -> dict[str, str]:
        return {"sensor.wattcast": prices["wattcast_buy_entity_id"], "sensor.wattcast_forecast": prices["wattcast_forecast_entity_id"]}

    def _wrong_unit(self, entity_id: str) -> bool:
        unit = self._get_entity_attr(entity_id).get("unit_of_measurement")
        if isinstance(unit, str) and unit.strip().lower() == "ct/kwh":
            self._wrong_unit_logged.discard(entity_id)
            return False
        if entity_id not in self._wrong_unit_logged:
            self._wrong_unit_logged.add(entity_id)
            _LOGGER.warning("%s gives its price in %s, not in ct/kWh as Wattcast does: its prices are not used", entity_id, unit)
        return True

    def _read_rows(self) -> list[Row]:
        if not self._entity_exists(self.forecast_entity_id):
            raise ValueError(f"{self.forecast_entity_id} not loaded")
        forecast = self._get_entity_attr(self.forecast_entity_id)
        unit = forecast.get("forecast_unit")  # the forecast sensor says its unit: its p50 is read as ct/kWh
        if isinstance(unit, str) and unit.strip().lower() != "ct/kwh":
            if self.forecast_entity_id not in self._wrong_unit_logged:
                self._wrong_unit_logged.add(self.forecast_entity_id)
                _LOGGER.warning("%s gives its forecast in %s, not in ct/kWh as Wattcast does: its prices are not used", self.forecast_entity_id, unit)
            return []
        self._wrong_unit_logged.discard(self.forecast_entity_id)
        return wattcast_rows(self._get_entity_attr(self.buy_entity_id), forecast, self._local)
