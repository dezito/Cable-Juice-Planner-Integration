"""The providers of the integrations that give published prices (today and tomorrow) and no forecast: EPEX Spot, GE-Spot, Octopus Energy
(UK) and Nord Pool (HACS). None of them looks beyond tomorrow, so they work together with a provider that has a forecast.

Their prices are the user's own: the sensors already have the VAT, the surcharge and the fees the user set up in them in the price, so
the buy price is what they say (no VAT or surcharge of ours). The sell price is the wholesale price of a second sensor (EPEX Spot's
Market Price, Nord Pool's sensor without VAT) or a field of the same sensor (GE-Spot's raw_value), less the sell fee; Octopus has the
export rates of its export tariff, and without them a sale is worth nothing (0, less the sell fee), never the buy price. A sell price
that is not a market price (GE-Spot's raw_value from Strømligning or Amber) is not used.
"""
from __future__ import annotations

import logging

from ..utils.helpers import days_between, reset_time_to_hour
from .base import _ZERO_TARIFFS, BasePriceProvider, price_timestamp
from .energypriceforecast import EnergyPriceForecastPriceProvider
from .published_rows import OCTOPUS_MAX_PLAUSIBLE, Row, epex_rows, gespot_raw_is_spot, gespot_rows, octopus_rows

_LOGGER = logging.getLogger(__name__)


class PublishedPriceProvider(BasePriceProvider):
    """Published prices of today and tomorrow from one or two sensors; no forecast."""

    has_forecast = False
    sell_key = ""
    # a sale is worth nothing in every hour (the combined provider takes the sell prices of the other providers away): Octopus without export
    sells_nothing = False

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.buy_entity_id = self.entity_ids.get(self.buy_key, "")
        self.sell_entity_id = self.entity_ids.get(self.sell_key, "") if self.sell_key else ""
        self._logged: set[str] = set()

    def get_buy_price_entity_id(self) -> str:
        return self.buy_entity_id

    def get_sell_price_entity_id(self) -> str:
        return self.sell_entity_id

    def get_tariffs(self, hour, day_of_week, timestamp=None) -> dict:
        """No tariffs in the price, and none to take out of it."""
        return dict(_ZERO_TARIFFS)

    def _local(self, value):
        return price_timestamp(value, self.tz)

    def _warn_once(self, key: str, message: str, *args) -> None:
        if key not in self._logged:
            self._logged.add(key)
            _LOGGER.warning(message, *args)

    def _rows_of(self, entity_id: str, parse) -> list[Row]:
        """The rows of a sensor; raises when it is not loaded (the old prices are kept) and gives none for a unit that is refused."""
        if not self._entity_exists(entity_id):
            raise ValueError(f"{entity_id} not loaded")
        if self._wrong_unit(entity_id):
            return []
        return parse(self._get_entity_attr(entity_id))

    def _buy_rows(self) -> list[Row]:
        raise NotImplementedError

    def _sell_rows(self) -> list[Row]:
        raise NotImplementedError

    def _published(self, rows: list[Row]) -> dict:
        """Today's and tomorrow's rows, the first row of a start winning (the repeated hour when the clocks go back is one start in
        local time, like the other providers); yesterday's rows (a source keeps them) are not prices."""
        current_hour = reset_time_to_hour(self.now)
        prices: dict = {}
        for start, price in sorted(rows, key=lambda row: row[0]):
            if 0 <= days_between(current_hour, start) <= 1:
                prices.setdefault(start, price)
        return prices

    def _load_buy_real_prices(self):
        return self._published(self._buy_rows())

    def _load_sell_real_prices(self):
        return {start: self.calculate_sell_price(start, price) for start, price in self._published(self._sell_rows()).items()}


class EpexSpotPriceProvider(PublishedPriceProvider):
    """mampfes/ha_epex_spot (domain epex_spot): the Total Price sensor is the buy price (the user's surcharge and tax are in it), the
    Market Price sensor the wholesale price (the sell price). Both are €/kWh with a ``data`` attribute."""

    source_name = "epexspot"
    buy_key = "epexspot_buy_entity_id"
    sell_key = "epexspot_sell_entity_id"

    @classmethod
    def dashboard_entities(cls, prices: dict[str, str]) -> dict[str, str]:
        # the chart draws the plan's prices and only watches the buy sensor of the provider (it is gone or unavailable: the restart button)
        return {"sensor.epex_spot_total_price": prices["epexspot_buy_entity_id"]}

    def _buy_rows(self) -> list[Row]:
        return self._rows_of(self.buy_entity_id, lambda attributes: epex_rows(attributes, self._local))

    def _sell_rows(self) -> list[Row]:
        # no sell sensor (the page requires one): no sell price, never the buy price
        return self._rows_of(self.sell_entity_id, lambda attributes: epex_rows(attributes, self._local)) if self.sell_entity_id else []


class GeSpotPriceProvider(PublishedPriceProvider):
    """enoch85/ge-spot (domain ge_spot): one sensor with ``value`` (the price with VAT, tariff and tax: the buy price) and ``raw_value``
    (the market price: the sell price) in every row. It has no sensor of its own with the sell price, so there is no recorded sell
    history; raw_value is only a market price from a source that has one (not Strømligning or Amber)."""

    source_name = "gespot"
    buy_key = "gespot_buy_entity_id"

    @classmethod
    def dashboard_entities(cls, prices: dict[str, str]) -> dict[str, str]:
        # the chart draws the plan's prices and only watches the buy sensor of the provider (it is gone or unavailable: the restart button)
        return {"sensor.gespot_current_price": prices["gespot_buy_entity_id"]}

    def _buy_rows(self) -> list[Row]:
        return self._rows_of(self.buy_entity_id, lambda attributes: gespot_rows(attributes, self._local, "value"))

    def _sell_rows(self) -> list[Row]:
        def parse(attributes) -> list[Row]:
            if not gespot_raw_is_spot(attributes):
                self._warn_once("raw", "%s has no market price in raw_value from %s: no sell price from it", self.buy_entity_id, attributes.get("data_source"))
                return []
            self._logged.discard("raw")
            return gespot_rows(attributes, self._local, "raw_value")

        return self._rows_of(self.buy_entity_id, parse)

    def get_sell_price_entity_id(self) -> str:
        return ""


class OctopusPriceProvider(PublishedPriceProvider):
    """BottlecapDave/HomeAssistant-OctopusEnergy (domain octopus_energy), the United Kingdom: the event entities of the current and the
    next day hold the rates (pounds per kWh with VAT, 30 minutes); the export tariff has its own pair. The state of an event entity is
    the time of its last event, so there is no recorded price history (records_history). Without export rates a sale is worth 0."""

    source_name = "octopus"
    buy_key = "octopus_buy_entity_id"
    sell_key = "octopus_sell_entity_id"
    countries = frozenset({"GB"})
    records_history = False

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.buy_next_entity_id = self.entity_ids.get("octopus_buy_next_day_entity_id", "")
        self.sell_next_entity_id = self.entity_ids.get("octopus_sell_next_day_entity_id", "")
        self.sells_nothing = not (self.sell_entity_id or self.sell_next_entity_id)

    @classmethod
    def dashboard_entities(cls, prices: dict[str, str]) -> dict[str, str]:
        # the chart draws the plan's prices and only watches the buy sensor of the provider (it is gone or unavailable: the restart button)
        return {"event.octopus_current_day_rates": prices["octopus_buy_entity_id"]}

    def _day(self, entity_id: str, required: bool) -> list[Row]:
        if not entity_id:
            return []
        if not self._entity_exists(entity_id):
            if required:
                raise ValueError(f"{entity_id} not loaded")
            return []
        attributes = self._get_entity_attr(entity_id)
        rows = octopus_rows(attributes, self._local)
        if not rows and any(isinstance(row, dict) and isinstance(row.get("value_inc_vat"), (int, float)) and row["value_inc_vat"] > OCTOPUS_MAX_PLAUSIBLE for row in attributes.get("rates") or []):
            self._warn_once(entity_id, "%s has rates above %s per kWh: they are pence, not pounds, so its prices are not used", entity_id, OCTOPUS_MAX_PLAUSIBLE)
        return rows

    def _buy_rows(self) -> list[Row]:
        return [*self._day(self.buy_entity_id, True), *self._day(self.buy_next_entity_id, False)]

    def _sell_rows(self) -> list[Row]:
        if self.sell_entity_id or self.sell_next_entity_id:
            return [*self._day(self.sell_entity_id, bool(self.sell_entity_id)), *self._day(self.sell_next_entity_id, False)]  # a configured export entity that is gone is an error
        return [(start, 0.0) for start, _price in self._buy_rows()]  # no export tariff: a sale earns nothing


class NordpoolPriceProvider(EnergyPriceForecastPriceProvider):
    """custom-components/nordpool (HACS, domain nordpool): raw_today / raw_tomorrow rows {start, end, value} like Energy Price Forecast,
    in the currency per kWh with the VAT and the additional costs the user set in it. They are not market prices, so the sell price is
    the market price of a second sensor of the integration (without VAT and costs), required on the page. No forecast. A price per MWh
    or Wh is refused (price_unit_error), never converted."""

    source_name = "nordpool"
    buy_key = "nordpool_buy_entity_id"
    has_forecast = False
    # its regions (the HACS integration has no Luxembourg and no Poland)
    countries = frozenset({"AT", "BE", "DE", "DK", "EE", "FI", "FR", "LT", "LV", "NL", "NO", "SE"})

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.sell_entity_id = self.entity_ids.get("nordpool_sell_entity_id", "")  # no sell sensor, no sell price: never the buy price

    @classmethod
    def dashboard_entities(cls, prices: dict[str, str]) -> dict[str, str]:
        # the chart draws the plan's prices and only watches the buy sensor of the provider (it is gone or unavailable: the restart button)
        return {"sensor.nordpool": prices["nordpool_buy_entity_id"]}

    def _divisor(self, entity_id) -> float:
        return 1.0

    def _load_buy_forecast_prices(self):
        return {}

    def _load_sell_real_prices(self):
        return super()._load_sell_real_prices() if self.sell_entity_id else {}

    def _load_sell_forecast_prices(self):
        return {}
