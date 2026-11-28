"""The self test of the price providers: does each one give the prices the plan needs? (The same file in both repositories.)

Every provider is judged on its OWN prices, never on the combined ones: the next provider, the price history and the offline
prices fill the gaps of a provider there. The test only reads; no price and no plan depends on it. A result is True, False or
None, and None means "can't be judged" (an unavailable sensor, tomorrow's prices before they must be there), never "fine".
A passed self test does not make a provider confirmed: that is the provider class's confirmed_countries.
"""
from __future__ import annotations

import datetime
import math
from collections.abc import Callable

from .intervals import common_interval_minutes, interval_slots, provider_intervals

# local hour from which the next day's prices must be there: day-ahead prices come in the afternoon (Octopus Energy's last)
PUBLISHED_BY_HOUR = 20
# a provider's market day can end an hour before the local day does (Octopus Energy's rates end at 23:00)
MARKET_DAY_EDGE = datetime.timedelta(hours=1)


def local_slots(day: datetime.date, tz: datetime.tzinfo | None, minutes: int) -> list[datetime.datetime]:
    """The price slots of a local day as naive local time: no skipped hour when the clocks go forward, and the repeated
    hour when they go back once (it is one price key)."""
    step = datetime.timedelta(minutes=minutes)
    start = datetime.datetime.combine(day, datetime.time())
    end = start + datetime.timedelta(days=1)
    if tz is None:
        return interval_slots(start, end, minutes)
    moment = start.replace(tzinfo=tz).astimezone(datetime.timezone.utc)
    last = end.replace(tzinfo=tz).astimezone(datetime.timezone.utc)
    slots: dict[datetime.datetime, None] = {}
    while moment < last:
        slots[moment.astimezone(tz).replace(tzinfo=None)] = None
        moment += step
    return list(slots)


def missing_slots(prices: dict, day: datetime.date, tz: datetime.tzinfo | None) -> list[datetime.datetime]:
    """The slots of the day without a price, on the provider's own interval that day (the whole day when it has none)."""
    intervals = [interval for interval in provider_intervals(prices) if interval[0].date() == day]
    grid = common_interval_minutes((end - start for start, end, _price in intervals), warn=False)
    covered = {
        slot
        for start, end, price in intervals
        if isinstance(price, (int, float)) and not isinstance(price, bool) and math.isfinite(price)
        for slot in interval_slots(start, end, grid)
    }
    return [slot for slot in local_slots(day, tz, grid) if slot not in covered]


def day_check(prices: dict, day: datetime.date, now: datetime.datetime, tz: datetime.tzinfo | None) -> bool | None:
    """Whether the prices of today or tomorrow are complete; None while the missing ones need not be there yet.

    Today only the slots from the current hour count: the plan needs no hour that is over, and a provider whose market day
    starts after local midnight may drop today's first hour once it is over."""
    missing = missing_slots(prices, day, tz)
    if day == now.date():
        this_hour = now.replace(minute=0, second=0, microsecond=0)
        missing = [slot for slot in missing if slot >= this_hour]
    if not missing:
        return True
    edge = datetime.datetime.combine(day, datetime.time()) + datetime.timedelta(days=1) - MARKET_DAY_EDGE
    only_the_end = all(slot >= edge for slot in missing)
    published = now.hour >= PUBLISHED_BY_HOUR
    if day == now.date():
        return None if only_the_end and not published else False
    if not published:
        return None
    return only_the_end


def forecast_days(prices: dict, today: datetime.date) -> int:
    """How many days after today the forecast reaches (0 without one)."""
    return max([0, *((timestamp.date() - today).days for timestamp in prices)])


def overall(*results: bool | None) -> bool | None:
    """False when any check failed, True only when every one of them passed."""
    if any(result is False for result in results):
        return False
    return True if results and all(result is True for result in results) else None


def sensor_keys(source_name: str, buy_key: str) -> tuple[str, str]:
    """The config keys of the buy and the sell price sensor of a provider (a provider without a sell sensor never has the key set)."""
    return buy_key, "stromligning_sell_current_price" if source_name == "stromligning" else f"{source_name}_sell_entity_id"


def check_provider(
    provider,
    country: str,
    now: datetime.datetime,
    tz: datetime.tzinfo | None,
    entity_ids: dict[str, str],
    attributes: dict[str, dict],
    unavailable: set[str],
    update_failed: bool,
    unit_error: Callable[[str, object], bool],
) -> dict:
    """The self test of one provider: {"confirmed", "ok", "unit", "today", "tomorrow", "sell_today", "sell_tomorrow", "forecast_days"}.

    now: the naive local time the prices were read. unavailable: the price entities that are missing, unavailable or unknown.
    update_failed: the last update of the provider raised, so the prices it holds are old ones and are not judged as its prices
    of now. unit_error(config key, unit): whether the prices of that sensor are read wrongly. Without sell prices ok is never
    True, unless the provider says a sale is worth nothing (sells_nothing). The result names no entity.
    """
    result: dict = {
        "confirmed": country in provider.confirmed_countries,
        "ok": None,
        "unit": None,
        "today": None,
        "tomorrow": None,
        "sell_today": None,
        "sell_tomorrow": None,
        "forecast_days": None,
    }
    buy_key, sell_key = sensor_keys(provider.source_name, provider.buy_key)
    sensors = [(key, entity_ids[key]) for key in (buy_key, sell_key) if entity_ids.get(key)]
    if not sensors or any(entity_id in unavailable or entity_id not in attributes for _key, entity_id in sensors):
        return result
    result["unit"] = not any(unit_error(key, (attributes[entity_id] or {}).get("unit_of_measurement")) for key, entity_id in sensors)
    sells_nothing = getattr(provider, "sells_nothing", False)
    if update_failed:
        result["today"] = False
    else:
        today = now.date()
        tomorrow = today + datetime.timedelta(days=1)
        result["today"] = day_check(provider.buy_real_prices, today, now, tz)
        result["tomorrow"] = day_check(provider.buy_real_prices, tomorrow, now, tz)
        # a chosen sell sensor without a price fails; without sell prices and without a sell sensor there is nothing to judge
        if not sells_nothing and (provider.sell_real_prices or entity_ids.get(sell_key)):
            result["sell_today"] = day_check(provider.sell_real_prices, today, now, tz)
            result["sell_tomorrow"] = day_check(provider.sell_real_prices, tomorrow, now, tz)
        if provider.has_forecast:
            result["forecast_days"] = forecast_days(provider.buy_forecast_prices, today)
    # the provider's other sensors (tomorrow's, the forecast's, the tariffs): while one has no state, a missing price may
    # be that sensor's, so nothing is called failed because of it
    others = [
        entity_id
        for key, entity_id in entity_ids.items()
        if entity_id and key.startswith(f"{provider.source_name}_") and key not in (buy_key, sell_key)
    ]
    if any(entity_id in unavailable or entity_id not in attributes for entity_id in others):
        for key in ("today", "tomorrow", "sell_today", "sell_tomorrow"):
            if result[key] is False:
                result[key] = None
    sells = () if sells_nothing else (result["sell_today"], result["sell_tomorrow"])
    result["ok"] = overall(result["unit"], result["today"], result["tomorrow"], *sells)
    return result
