"""The prices the planner uses, per price period, for the price chart of the dashboard (the same file in both repositories)."""
from __future__ import annotations

import bisect
import datetime


def _naive(timestamp: datetime.datetime) -> datetime.datetime:
    return timestamp.replace(tzinfo=None)


def _hour(timestamp: datetime.datetime) -> datetime.datetime:
    return timestamp.replace(minute=0, second=0, microsecond=0)


def _valid(prices: dict, first: datetime.datetime, end: datetime.datetime) -> dict[datetime.datetime, float]:
    return {
        _naive(timestamp): float(price)
        for timestamp, price in prices.items()
        if isinstance(price, (int, float)) and not isinstance(price, bool) and price == price and first <= _naive(timestamp) < end
    }


class _Periods:
    """The price of a period applies to the later periods of the same hour that have none of their own (an hourly price next to 15 minute prices)."""

    def __init__(self, prices: dict[datetime.datetime, float]) -> None:
        self.prices = prices
        self.times = sorted(prices)

    def key_at(self, timestamp: datetime.datetime) -> datetime.datetime | None:
        index = bisect.bisect_right(self.times, timestamp) - 1
        if index >= 0 and _hour(self.times[index]) == _hour(timestamp):
            return self.times[index]
        return None


def price_chart_rows(
    buy_prices: dict,
    sell_prices: dict,
    buy_sources: dict,
    first: datetime.datetime,
    days: int,
    decimals: int,
) -> list[dict]:
    """One row per price period (15 minutes or an hour, of the buy or the sell prices: the finer of the two) from first until `days` days on.

    These are the prices the plan is made on, real and forecast in one list. A period without a buy price in its hour is not drawn
    (the plan does not buy there). sell is None in a period without a sell price (the plan has none either: a sell price from the price
    history is one, and is drawn). forecast is True when the buy price of the period is not a real one (a forecast, the price history or
    the offline prices), after the planner's own source texts ("<provider>:real" is the only real one); a price without a source text
    is not claimed to be a forecast.
    """
    first = _naive(first)
    end = first + datetime.timedelta(days=days)
    buy = _Periods(_valid(buy_prices, first, end))
    sell = _Periods(_valid(sell_prices, first, end))
    sources = {_naive(timestamp): str(source) for timestamp, source in buy_sources.items()}
    rows = []
    for timestamp in sorted(set(buy.times) | set(sell.times)):
        buy_key = buy.key_at(timestamp)
        if buy_key is None:
            continue
        sell_key = sell.key_at(timestamp)
        rows.append(
            {
                "time": timestamp.isoformat(),
                "buy": round(buy.prices[buy_key], decimals),
                "sell": None if sell_key is None else round(sell.prices[sell_key], decimals),
                "forecast": buy_key in sources and not sources[buy_key].endswith(":real"),
            }
        )
    return rows
