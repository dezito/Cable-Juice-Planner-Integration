from __future__ import annotations

import datetime
import zoneinfo

import pytest

from custom_components.cable_juice_planner.prices.intervals import hourly_price_averages, price_interval_at, price_intervals


def test_price_intervals_support_mixed_durations_within_an_hour() -> None:
    start = datetime.datetime(2026, 10, 22)
    prices = {
        start: 10.0,
        start + datetime.timedelta(minutes=15): 20.0,
        start + datetime.timedelta(minutes=45): 30.0,
        start + datetime.timedelta(hours=1): 40.0,
    }

    assert price_intervals(prices) == [
        (start, start + datetime.timedelta(minutes=15), 10.0),
        (start + datetime.timedelta(minutes=15), start + datetime.timedelta(minutes=45), 20.0),
        (start + datetime.timedelta(minutes=45), start + datetime.timedelta(hours=1), 30.0),
        (start + datetime.timedelta(hours=1), start + datetime.timedelta(hours=1, minutes=15), 40.0),
    ]


def test_partial_hour_prices_are_aggregated_without_dropping_the_hour() -> None:
    start = datetime.datetime(2026, 10, 22)
    prices = {
        start: 10.0,
        start + datetime.timedelta(minutes=15): 20.0,
        start + datetime.timedelta(minutes=45): 30.0,
        start + datetime.timedelta(hours=2): 40.0,
    }

    hourly = hourly_price_averages(prices)

    assert hourly[start] == pytest.approx(20.0)
    assert start + datetime.timedelta(hours=1) not in hourly
    assert hourly[start + datetime.timedelta(hours=2)] == pytest.approx(40.0)


def test_last_interval_uses_previous_duration_and_long_gaps_are_bounded() -> None:
    start = datetime.datetime(2026, 10, 22)
    prices = {
        start: 1.0,
        start + datetime.timedelta(minutes=15): 2.0,
        start + datetime.timedelta(hours=2): 3.0,
    }

    assert price_interval_at(prices, start + datetime.timedelta(minutes=30)) is None
    assert price_interval_at(prices, start + datetime.timedelta(hours=2, minutes=10)) == (
        start + datetime.timedelta(hours=2),
        start + datetime.timedelta(hours=2, minutes=15),
        3.0,
    )


def test_price_interval_lookup_matches_provider_naive_timestamps() -> None:
    start = datetime.datetime(2026, 10, 22)
    prices = {start: 1.0, start + datetime.timedelta(minutes=15): 2.0}

    assert price_interval_at(prices, start.replace(tzinfo=zoneinfo.ZoneInfo("Europe/Copenhagen")) + datetime.timedelta(minutes=20)) == (
        start + datetime.timedelta(minutes=15),
        start + datetime.timedelta(minutes=30),
        2.0,
    )


def test_full_hours_only_leaves_out_partly_covered_hours() -> None:
    start = datetime.datetime(2026, 10, 22, 8)
    prices = {
        start: 1.0, start + datetime.timedelta(minutes=15): 2.0, start + datetime.timedelta(minutes=30): 3.0,
        start + datetime.timedelta(minutes=45): 4.0,
        start + datetime.timedelta(hours=1): 5.0, start + datetime.timedelta(hours=1, minutes=15): 6.0,
        start + datetime.timedelta(hours=3): 7.0,
    }
    assert set(hourly_price_averages(prices)) >= {start, start + datetime.timedelta(hours=1)}
    full = hourly_price_averages(prices, full_hours_only=True)
    assert full == {start: pytest.approx(2.5)}
