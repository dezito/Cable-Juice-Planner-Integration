"""The prices of the plan per price period for the price charts of the dashboard (price_chart_rows, the same module in both repositories)."""
from __future__ import annotations

import datetime

import pytest

from custom_components.cable_juice_planner.prices.chart_rows import price_chart_rows

DAY = datetime.datetime(2026, 10, 22)


def _periods(first: datetime.datetime, count: int, value: float, step: int = 60) -> dict:
    return {first + datetime.timedelta(minutes=step * index): value + index for index in range(count)}


def test_one_row_per_price_with_the_buy_price_the_sell_price_and_whether_it_is_a_forecast() -> None:
    buy = _periods(DAY, 4, 1.0)
    sell = _periods(DAY, 4, 0.5)
    sources = {DAY + datetime.timedelta(hours=hour): ("p:real" if hour < 2 else "p:forecast") for hour in range(4)}
    rows = price_chart_rows(buy, sell, sources, DAY, 7, 3)
    assert rows == [
        {"time": "2026-10-22T00:00:00", "buy": 1.0, "sell": 0.5, "forecast": False},
        {"time": "2026-10-22T01:00:00", "buy": 2.0, "sell": 1.5, "forecast": False},
        {"time": "2026-10-22T02:00:00", "buy": 3.0, "sell": 2.5, "forecast": True},
        {"time": "2026-10-22T03:00:00", "buy": 4.0, "sell": 3.5, "forecast": True},
    ]


def test_the_price_history_and_the_offline_prices_are_not_real_prices() -> None:
    sources = {DAY: "database", DAY + datetime.timedelta(hours=1): "offline", DAY + datetime.timedelta(hours=2): "p:real"}
    rows = price_chart_rows(_periods(DAY, 3, 1.0), {}, sources, DAY, 7, 2)
    assert [row["forecast"] for row in rows] == [True, True, False]


def test_a_price_without_a_source_text_is_not_claimed_to_be_a_forecast() -> None:
    assert [row["forecast"] for row in price_chart_rows(_periods(DAY, 2, 1.0), {}, {}, DAY, 7, 2)] == [False, False]


def test_a_missing_sell_price_is_none_never_the_buy_price_or_zero() -> None:
    rows = price_chart_rows(_periods(DAY, 3, 1.0), {DAY + datetime.timedelta(hours=1): 0.4}, {}, DAY, 7, 2)
    assert [row["sell"] for row in rows] == [None, 0.4, None]
    assert [row["sell"] for row in price_chart_rows(_periods(DAY, 2, 1.0), {}, {}, DAY, 7, 2)] == [None, None]


def test_prices_of_15_minutes_are_a_row_each() -> None:
    buy = _periods(DAY, 8, 0.0, 15)
    rows = price_chart_rows(buy, buy, {}, DAY, 7, 3)
    assert [(row["time"][11:16], row["buy"], row["sell"]) for row in rows] == [(f"0{index // 4}:{index % 4 * 15:02d}", float(index), float(index)) for index in range(8)]


def test_a_sell_price_covers_the_later_periods_of_its_hour_only() -> None:
    """An hourly sell price with 15 minute buy prices: the four quarters have it, the next hour has none (never the last hour's price)."""
    buy = _periods(DAY, 8, 1.0, 15)
    rows = price_chart_rows(buy, {DAY: 0.5}, {}, DAY, 7, 2)
    assert [row["sell"] for row in rows] == [0.5, 0.5, 0.5, 0.5, None, None, None, None]


def test_an_hourly_buy_price_with_15_minute_sell_prices_has_a_row_for_every_sell_period() -> None:
    """Strømligning: hourly buy prices next to 15 minute sell prices. The plan uses every sell price, so the chart shows every one of them."""
    buy = _periods(DAY, 2, 1.0)
    sell = _periods(DAY, 8, 0.0, 15)
    sources = {DAY: "p:real", DAY + datetime.timedelta(hours=1): "p:forecast"}
    rows = price_chart_rows(buy, sell, sources, DAY, 7, 2)
    assert [(row["time"][11:16], row["buy"], row["sell"], row["forecast"]) for row in rows] == [
        ("00:00", 1.0, 0.0, False), ("00:15", 1.0, 1.0, False), ("00:30", 1.0, 2.0, False), ("00:45", 1.0, 3.0, False),
        ("01:00", 2.0, 4.0, True), ("01:15", 2.0, 5.0, True), ("01:30", 2.0, 6.0, True), ("01:45", 2.0, 7.0, True),
    ]


def test_a_sell_period_in_an_hour_without_a_buy_price_is_not_drawn() -> None:
    assert price_chart_rows({DAY: 1.0}, {DAY + datetime.timedelta(hours=1, minutes=15): 0.5}, {}, DAY, 7, 2) == [
        {"time": "2026-10-22T00:00:00", "buy": 1.0, "sell": None, "forecast": False}
    ]


def test_the_forecast_is_told_per_period() -> None:
    sources = {DAY: "p:real", DAY + datetime.timedelta(minutes=30): "p:forecast"}
    assert [row["forecast"] for row in price_chart_rows(_periods(DAY, 2, 1.0, 30), {}, sources, DAY, 7, 2)] == [False, True]


def test_only_the_days_asked_for_from_the_start_and_no_period_without_a_buy_price() -> None:
    buy = _periods(DAY - datetime.timedelta(hours=2), 24 * 9, 1.0)
    rows = price_chart_rows(buy, buy, {}, DAY, 7, 2)
    assert rows[0]["time"] == "2026-10-22T00:00:00" and rows[-1]["time"] == "2026-10-28T23:00:00" and len(rows) == 24 * 7
    # a period with a sell price only is not drawn: the chart is made of the periods the plan buys at
    assert price_chart_rows({}, {DAY: 1.0}, {}, DAY, 7, 2) == []
    assert price_chart_rows({}, {}, {}, DAY, 7, 2) == []


@pytest.mark.parametrize("bad", [None, "1.0", True, float("nan")])
def test_a_price_that_is_not_a_number_is_left_out(bad) -> None:
    rows = price_chart_rows({DAY: bad, DAY + datetime.timedelta(hours=1): 2.0}, {DAY: bad}, {}, DAY, 7, 2)
    assert rows == [{"time": "2026-10-22T01:00:00", "buy": 2.0, "sell": None, "forecast": False}]


def test_timestamps_with_a_time_zone_are_read_as_the_local_time_they_show() -> None:
    zone = datetime.timezone(datetime.timedelta(hours=2))
    rows = price_chart_rows({datetime.datetime(2026, 10, 22, 5, tzinfo=zone): 1.0}, {}, {}, DAY, 7, 2)
    assert [row["time"] for row in rows] == ["2026-10-22T05:00:00"]


def test_the_rounding_follows_the_decimals_of_the_currency() -> None:
    assert price_chart_rows({DAY: 0.123456}, {DAY: 0.987654}, {}, DAY, 7, 3) == [{"time": "2026-10-22T00:00:00", "buy": 0.123, "sell": 0.988, "forecast": False}]
