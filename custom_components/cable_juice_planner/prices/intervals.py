"""Helpers for prices whose timestamps mark the start of each interval."""
from __future__ import annotations

import datetime


def naive_timestamp(timestamp: datetime.datetime) -> datetime.datetime:
    """Match the timezone-naive timestamps used by the price providers."""
    return timestamp.replace(tzinfo=None)


def price_intervals(prices: dict[datetime.datetime, float]) -> list[tuple[datetime.datetime, datetime.datetime, float]]:
    """Return bounded (start, end, price) intervals from ordered price timestamps.

    A price applies until the next timestamp, up to one hour. The final price uses
    the previous interval's duration, or one hour when it is the only price.
    """
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
