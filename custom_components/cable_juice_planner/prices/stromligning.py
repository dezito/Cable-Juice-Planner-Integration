"""Strømligning price provider."""
from __future__ import annotations

import datetime
import logging

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..const import FLOW_STEPS
from ..utils.helpers import to_datetime
from .base import BasePriceProvider, _naive, is_number

_LOGGER = logging.getLogger(__name__)


def _derive(entity_id: str, old: str, new: str) -> str:
    """The entity of the same device: the configured one with `old` in its id replaced by `new` ("" when it has no `old`)."""
    return entity_id.replace(old, new) if old in entity_id else ""


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

    @classmethod
    def dashboard_entities(cls, prices: dict[str, str]) -> dict[str, str]:
        buy, sell = prices["stromligning_buy_current_price"], prices["stromligning_sell_current_price"]
        return {
            "sensor.stromligning_current_price_vat": buy,
            "sensor.stromligning_forecasts_vat": prices["stromligning_buy_forecasts"],
            "binary_sensor.stromligning_tomorrow_available_vat": prices["stromligning_buy_tomorrow_available"],
            "sensor.stromligning_nettariff_vat": _derive(buy, "current_price", "nettariff"),
            "sensor.stromligning_systemtariff_vat": _derive(buy, "current_price", "systemtariff"),
            "sensor.stromligning_15min_spotprice_ex_vat": _derive(sell, "current_price", "spotprice"),
            "sensor.stromligning_15min_forecasts_ex_vat": prices["stromligning_sell_forecasts"],
            "binary_sensor.stromligning_15min_tomorrow_available_ex_vat": prices["stromligning_sell_tomorrow_available"],
        }

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
            if not isinstance(timestamp, datetime.datetime) or not is_number(price):
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

# ---------------------------------------------------------------------------------------------------------------------
# Flow time and repairs: no provider exists yet, so these read the options / the registry themselves - here, in prices/
# ---------------------------------------------------------------------------------------------------------------------

STROMLIGNING_PLATFORM = "stromligning"

# the Strømligning sensors that can be selected on the price pages (the start of their unique ids)
STROMLIGNING_UNIQUE_ID_PREFIXES = (
    "current_price",
    "tomorrow_available",
    "forecasts",
    "distribution",
    "electricity_tax",
    "nettariff",
    "systemtariff",
    "surcharge",
)


def stromligning_entities_to_watch(prices: dict[str, str]) -> list[str]:
    """The configured Strømligning sensors that count: the tariff sensors only with a sell sensor (their page is hidden without it)."""
    tariffs = FLOW_STEPS["prices_stromligning_tariffs"] if not prices.get("stromligning_sell_current_price") else []
    return [entity_id for key, entity_id in prices.items() if key.startswith("stromligning_") and entity_id and key not in tariffs]


def stromligning_problems(hass: HomeAssistant, entity_ids: list[str]) -> dict[str, list[str]]:
    """Which of the configured Strømligning sensors are disabled, and which the integration doesn't provide.

    A sensor with no state, or only the state Home Assistant restores for an entity nobody provides, is not provided
    (e.g. the price forecasts when they are off in the options of Strømligning). That is only a finding while the
    Strømligning entry that owns the sensor runs: during an outage every sensor is unavailable, which is not a setting.
    """
    registry = er.async_get(hass)
    disabled: list[str] = []
    not_provided: list[str] = []
    for entity_id in dict.fromkeys(entity_ids):
        entry = registry.async_get(entity_id)
        if entry is not None and entry.disabled_by:
            disabled.append(entity_id)
            continue
        state = hass.states.get(entity_id)
        if (state is None or state.attributes.get("restored")) and _integration_running(hass, entry):
            not_provided.append(entity_id)
    return {"disabled": disabled, "not_provided": not_provided}


def _integration_running(hass: HomeAssistant, entry: er.RegistryEntry | None) -> bool:
    """Whether the Strømligning entry that owns the sensor runs (there can be one for buying and one for selling).

    Without a registry entry (the sensor was never created) any running Strømligning entry will do.
    """
    owner = hass.config_entries.async_get_entry(entry.config_entry_id) if entry is not None and entry.config_entry_id else None
    if owner is not None:
        return owner.state is ConfigEntryState.LOADED
    return any(candidate.state is ConfigEntryState.LOADED for candidate in hass.config_entries.async_entries(STROMLIGNING_PLATFORM))


def has_stromligning_sell_prices(options: dict) -> bool:
    """Whether the options (the flat options of a config entry) have a Strømligning sell sensor, so the tariff page is needed."""
    return bool(options.get("stromligning_sell_current_price"))
