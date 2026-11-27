"""Grid prices and the forecast."""
from __future__ import annotations

import datetime
import logging
from copy import deepcopy

from ..const import PRICE_ADDER_DAY_BETWEEN_DIVIDER, price_decimals, price_scale
from ..prices.base import sell_adder_sign
from ..prices.chart_rows import price_chart_rows
from ..prices.intervals import hourly_price_averages, interval_slots, price_grid_minutes
from ..utils.formatting import get_list_values
from ..utils.helpers import (
    average,
    calculate_ema,
    calculate_trend,
    days_between,
    minutes_between,
    reset_time_to_hour,
    reverse_list,
    start_of_day,
)

_LOGGER = logging.getLogger(__name__)


class Pricing:
    """Grid prices and the forecast."""

    def update_grid_prices(self, initial_run=False) -> None:
        try:
            if self.price_provider is None:
                raise ValueError("No supported price provider configured")
            self.price_provider.update_prices()
            self.get_grid_prices(update_prices=True)
            self.get_sell_prices(update_prices=True)
            if not initial_run:
                self.append_kwh_prices()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error in update_grid_prices: %s %s", err, type(err))
            self.persistent_notification(f"Error in update_grid_prices: {err} {type(err)}", f"{self.title} error", f"{self.name}_update_grid_prices_error")
        if self.price_provider is not None:
            # the prices of the plan (real and forecast) for the price chart of the dashboard, after every price update, also one that failed: the
            # chart shows what the plan has now (the cached or offline prices, or none), never the rows of an earlier update
            self.set_attr("current_charging_rule", "price_chart", self.price_chart_rows())

    def get_hour_prices(self, update_prices=False, sell_prices=False) -> dict:
        """Prices per period. Without online prices, offline prices from the price history."""
        now = self.now()
        database = self.sell_price_cache if sell_prices else self.grid_price_cache
        local_database_type = "history_sell" if sell_prices else "history"
        hour_prices: dict = {}
        # forecasts corrected with their measured bias per day ahead (the analysis); a change recalculates the prices
        corrections = self.price_forecast_corrections()
        try:
            if "prices" in database and database.get("using_offline_prices") is False and update_prices is False and database.get("corrections") == corrections:
                hour_prices = dict(database["prices"])
            else:
                provider = self.price_provider
                if provider is None:
                    raise ValueError("No price provider")
                hour_prices = provider.get_sell_prices() if sell_prices else provider.get_buy_prices()
                sources = provider.get_price_sources(sell_prices)
                if not hour_prices:
                    raise ValueError(f"No {'sell' if sell_prices else 'grid'} prices returned from {type(provider).__name__}")
                hour_prices = dict(hour_prices)
                sources = deepcopy(sources)
                if corrections:
                    current_hour = self.current_hour()
                    hour_prices = {
                        timestamp: round(price + corrections.get(days_between(current_hour, timestamp), 0.0), price_decimals(self.config))
                        if str(sources.get(timestamp, "")).endswith(":forecast") else price
                        for timestamp, price in hour_prices.items()
                    }
                # hours of the plan (today + 7 days) the provider has no price for are filled from the price history
                missing_hours = self._database_prices(local_database_type, hour_prices, 8)
                if missing_hours:
                    _LOGGER.debug("No %s price for %s hours, using the price history", "sell" if sell_prices else "grid", len(missing_hours))
                    hour_prices = dict(sorted({**hour_prices, **missing_hours}.items()))
                    sources.update({timestamp: "database" for timestamp in missing_hours})
                database["missing_hours"] = missing_hours
                database["last_update"] = now
                database["prices"] = dict(hour_prices)
                database["sources"] = sources
                database["corrections"] = corrections
                database["using_offline_prices"] = False
        except Exception as err:  # noqa: BLE001
            if "prices" in database and "last_update" in database and minutes_between(database["last_update"], now) <= 120:
                hour_prices = dict(database["prices"])
                _LOGGER.warning("Can't get online %s prices, using last successful: %s", "sell" if sell_prices else "grid", err)
            else:
                _LOGGER.warning("Can't get online %s prices, using database: %s", "sell" if sell_prices else "grid", err)
                database["last_update"] = now
                database["using_offline_prices"] = True
                try:
                    if local_database_type not in self.kwh_avg_prices_db and self.kwh_avg_prices_db:
                        raise KeyError(f"Missing {local_database_type} in KWH_AVG_PRICES_DB")
                    missing_hours = self._database_prices(local_database_type, {}, 7, from_start_of_day=True)
                    if not missing_hours:
                        raise KeyError(f"No prices in {local_database_type} of KWH_AVG_PRICES_DB")
                    hour_prices = dict(missing_hours)
                    database["sources"] = {timestamp: "offline" for timestamp in missing_hours}
                    database["missing_hours"] = missing_hours
                    database["prices"] = dict(hour_prices)
                except Exception as ex:  # noqa: BLE001
                    message = f"Can't get offline prices: {ex} {type(ex)}"
                    _LOGGER.error(message)
                    self.persistent_notification(message, f"{self.title} error", f"{self.name}_get_hour_prices_offline_prices_error")
                    raise ValueError(f"Offline prices error: {ex} {type(ex)}") from ex
        return hour_prices

    def _database_prices(self, history_key: str, prices: dict, days: int, from_start_of_day: bool = False) -> dict:
        """Prices from the price history for the hours from now (or midnight) until the end of days that have none in prices.

        The forecast of the same hour and weekday, plus the same small daily adder as the
        provider forecasts. Hours without any history are left out.
        """
        if len(self.kwh_avg_prices_db) == 0:
            self.load_kwh_prices(None)
        history = self.kwh_avg_prices_db.get(history_key, {})
        current_hour = self.current_hour()
        end = start_of_day(self.now()) + datetime.timedelta(days=days)
        priced_hours = {reset_time_to_hour(timestamp) for timestamp in prices}
        # the price history is hourly: an hour is as many slots as the prices around it have (every slot the same price)
        grid = price_grid_minutes(prices)
        missing = {}
        timestamp = start_of_day(self.now()) if from_start_of_day else current_hour
        while timestamp < end:
            values = history.get(timestamp.hour, {}).get(timestamp.weekday(), [])
            if timestamp not in priced_hours and values:
                adder = days_between(current_hour, timestamp) / PRICE_ADDER_DAY_BETWEEN_DIVIDER * price_scale(self.config)
                if history_key == "history_sell":
                    # a sale at a price from the history is worth less too (see sell_adder_sign)
                    adder *= sell_adder_sign(self.config)
                price = round(self.get_price_forecast_value(values) + adder, price_decimals(self.config))
                for slot in interval_slots(timestamp, timestamp + datetime.timedelta(hours=1), grid):
                    missing[slot] = price
            timestamp += datetime.timedelta(hours=1)
        return missing

    def sell_value(self, price: float, production_price: float | None = None) -> float:
        """What a sold kWh is worth at this hourly sell price.

        With a fixed solar sell price (a production agreement that settles the difference to the
        spot price) it is the fixed price minus the hourly price - negative when the hourly price
        is above the fixed price. Without one (-1) it is the hourly price itself.
        """
        fixed = float(self.config["solar"]["production_price"]) if production_price is None else float(production_price)
        return price if fixed == -1.0 else fixed - price

    def get_grid_prices(self, update_prices=False) -> dict:
        return self.get_hour_prices(update_prices=update_prices)

    def get_sell_prices(self, update_prices=False) -> dict:
        return self.get_hour_prices(update_prices=update_prices, sell_prices=True)

    def price_chart_rows(self) -> list[dict]:
        """The buy and sell prices the plan is made on, per price period for the next 7 days (real and forecast in one list), for the
        price charts of the dashboard. A price that is not there is left out: the chart shows what the plan has, never a favorable default."""
        def prices(sell: bool) -> dict:
            try:
                return self.get_sell_prices() if sell else self.get_grid_prices()
            except Exception as err:  # noqa: BLE001 - a missing sell price (a provider without one) must not stop the buy prices
                _LOGGER.debug("No %s prices for the price chart: %s", "sell" if sell else "buy", err)
                return {}

        return price_chart_rows(
            prices(False), prices(True), self.grid_price_cache.get("sources", {}), start_of_day(self.now()), 7, price_decimals(self.config)
        )

    def get_expensive_hours(self, day=0) -> dict:
        count = 0
        expensive = {}
        day_timestamp = self.now().date() + datetime.timedelta(days=day)
        hourly_prices = hourly_price_averages(self.get_grid_prices())
        for timestamp, price in sorted(hourly_prices.items(), key=lambda kv: (kv[1], kv[0]), reverse=True):
            if timestamp.date() == day_timestamp:
                if count < 4:
                    expensive[timestamp] = price
                else:
                    break
                count += 1
        return expensive

    def get_forecast_type(self) -> str:
        """The selected forecast type. With the switch auto_forecast_type the house consumption and the
        km/kWh use the type that hit each of them best lately (engine/analysis); the fallback prices and
        the solar history always use the selected one."""
        forecast_type = str(self.setting("forecast_type") or "ema").split(" ")[0].lower()
        self.forecast_type = forecast_type if forecast_type in ("average", "ema", "trend") else "ema"
        consumption, drives = self.auto_forecast_types(self.forecast_type)
        self.forecast_type_auto = {"consumption": consumption, "drives": drives}
        self.consumption_forecast_type = consumption or self.forecast_type
        self.drive_forecast_type = drives or self.forecast_type
        return self.forecast_type

    def get_forecast_value(self, data=None, forecast_type: str | None = None, decimals: int = 2) -> float:
        """The forecast of the selected type, or of forecast_type (the forecast accuracy analysis)."""
        forecast_type = forecast_type or self.forecast_type
        if not data or not isinstance(data, list) or len(data) == 0:
            return 0.0
        if forecast_type == "ema":
            return round(calculate_ema(reverse_list(get_list_values(data))), decimals)
        if forecast_type == "average":
            return round(average(get_list_values(data)), decimals)
        if forecast_type == "trend":
            return round(calculate_trend(reverse_list(get_list_values(data))), decimals)
        return 0.0

    def get_price_forecast_value(self, data=None) -> float:
        """get_forecast_value of a list of prices: rounded to the decimals of the price currency (2 for kroner)."""
        return self.get_forecast_value(data, decimals=price_decimals(self.config))
