"""Helpers for prices whose timestamps mark the start of each interval."""
from __future__ import annotations

import datetime
import logging
import math
from collections.abc import Iterable

_LOGGER = logging.getLogger(__name__)


def naive_timestamp(timestamp: datetime.datetime) -> datetime.datetime:
    """Match the timezone-naive timestamps used by the price providers."""
    return timestamp.replace(tzinfo=None)


# The planner asks for the interval of many timestamps in a row from the same prices (every call gets a fresh copy of the
# price dict), so the latest results are kept, keyed by the prices themselves: the same prices give the same intervals.
_INTERVALS_CACHE: list[tuple[tuple, tuple]] = []
_INTERVALS_CACHE_SIZE = 4


def price_intervals(prices: dict[datetime.datetime, float]) -> list[tuple[datetime.datetime, datetime.datetime, float]]:
    """Return bounded (start, end, price) intervals from ordered price timestamps.

    A price applies until the next timestamp, up to one hour. The final price uses
    the previous interval's duration, or one hour when it is the only price.
    """
    key = tuple(prices.items())
    for cached_key, cached in _INTERVALS_CACHE:
        if cached_key == key:
            return list(cached)
    intervals = _price_intervals(prices)
    _INTERVALS_CACHE.insert(0, (key, tuple(intervals)))
    del _INTERVALS_CACHE[_INTERVALS_CACHE_SIZE:]
    return intervals


def _price_intervals(prices: dict[datetime.datetime, float]) -> list[tuple[datetime.datetime, datetime.datetime, float]]:
    prices = {naive_timestamp(timestamp): price for timestamp, price in prices.items()}
    timestamps = sorted(prices)
    intervals = []
    for index, timestamp in enumerate(timestamps):
        if index + 1 < len(timestamps):
            duration = timestamps[index + 1] - timestamp
            if duration > datetime.timedelta(hours=1):
                duration = timestamps[index] - timestamps[index - 1] if index else datetime.timedelta(hours=1)
        elif index:
            duration = timestamp - timestamps[index - 1]
            if duration > datetime.timedelta(hours=1):
                duration = timestamps[index - 1] - timestamps[index - 2] if index > 1 else datetime.timedelta(hours=1)
        else:
            duration = datetime.timedelta(hours=1)
        hour_end = timestamp.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
        duration = min(max(duration, datetime.timedelta(0)), hour_end - timestamp)
        intervals.append((timestamp, timestamp + duration, prices[timestamp]))
    return intervals


def price_interval_at(
    prices: dict[datetime.datetime, float],
    timestamp: datetime.datetime,
) -> tuple[datetime.datetime, datetime.datetime, float] | None:
    """Return the price interval covering timestamp, if any."""
    timestamp = naive_timestamp(timestamp)
    for start, end, price in price_intervals(prices):
        if start <= timestamp < end:
            return start, end, price
    return None


def provider_intervals(prices: dict[datetime.datetime, float]) -> list[tuple[datetime.datetime, datetime.datetime, float]]:
    """price_intervals of ONE provider's prices, but no price covers more than the provider's own interval that day.

    A provider's interval is the most common gap between its timestamps that day (hourly, every 15 minutes, ...; the
    smaller one on a tie). A row the provider left out (a price that is no number is dropped by the providers) is a
    gap, not a longer interval for the price before it: that price is never made up for the missing time, so the next
    provider's price or the price history fills it. A day with a single price keeps its interval."""
    intervals = price_intervals(prices)
    gaps: dict[datetime.date, dict[datetime.timedelta, int]] = {}
    for (earlier, _end, _price), (later, _later_end, _later_price) in zip(intervals, intervals[1:]):
        gap = later - earlier
        if earlier.date() == later.date() and gap <= datetime.timedelta(hours=1):
            counts = gaps.setdefault(earlier.date(), {})
            counts[gap] = counts.get(gap, 0) + 1
    resolution = {day: min(counts, key=lambda gap: (-counts[gap], gap)) for day, counts in gaps.items()}
    return [
        (start, min(end, start + resolution[start.date()]) if start.date() in resolution else end, price)
        for start, end, price in intervals
    ]


_WARNED_INTERVALS: set[float] = set()


def common_interval_minutes(durations: Iterable[datetime.timedelta], warn: bool = True) -> int:
    """The greatest common divisor, in whole minutes, of price interval lengths that divide an hour.

    The finest interval every provider's prices fit on: 60 and 15 give 15, 60 and 30 give 30, 10 and 15 give 5.
    A length that is not a whole number of minutes that divides an hour (45 minutes, 7 minutes) is left out, with a
    warning once per length. 60 without any usable length."""
    grid = 0
    for duration in durations:
        minutes = duration.total_seconds() / 60
        if minutes < 1 or minutes != int(minutes) or 60 % int(minutes) != 0:
            if warn and minutes not in _WARNED_INTERVALS:
                _WARNED_INTERVALS.add(minutes)
                _LOGGER.warning("Ignoring a price interval of %s minutes: it does not divide an hour", f"{minutes:g}")
            continue
        grid = math.gcd(grid, int(minutes))
    return grid or 60


def price_grid_minutes(prices: dict[datetime.datetime, float]) -> int:
    """The interval, in minutes, the prices are on: the greatest common divisor of the gaps between them (60 for none)."""
    timestamps = sorted(naive_timestamp(timestamp) for timestamp in prices)
    gaps = [later - earlier for earlier, later in zip(timestamps, timestamps[1:]) if later - earlier <= datetime.timedelta(hours=1)]
    return common_interval_minutes(gaps, warn=False) if gaps else 60


def interval_slots(start: datetime.datetime, end: datetime.datetime, minutes: int) -> list[datetime.datetime]:
    """The starts of the `minutes` long slots from start up to (not including) end; start itself when end is not later."""
    step = datetime.timedelta(minutes=minutes)
    slots = [start]
    while slots[-1] + step < end:
        slots.append(slots[-1] + step)
    return slots


def hourly_price_averages(prices: dict[datetime.datetime, float], full_hours_only: bool = False) -> dict[datetime.datetime, float]:
    """Aggregate interval prices into duration-weighted averages for covered hours.

    full_hours_only: leave out an hour whose intervals do not cover all of it.
    """
    totals: dict[datetime.datetime, float] = {}
    durations: dict[datetime.datetime, float] = {}
    for start, end, price in price_intervals(prices):
        cursor = start
        while cursor < end:
            hour = cursor.replace(minute=0, second=0, microsecond=0)
            hour_end = hour + datetime.timedelta(hours=1)
            covered_until = min(end, hour_end)
            duration = (covered_until - cursor).total_seconds()
            totals[hour] = totals.get(hour, 0.0) + price * duration
            durations[hour] = durations.get(hour, 0.0) + duration
            cursor = covered_until
    return {hour: totals[hour] / durations[hour] for hour in totals if not full_hours_only or durations[hour] >= 3600.0 - 1e-6}
