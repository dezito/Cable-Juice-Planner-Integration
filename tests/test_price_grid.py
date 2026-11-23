"""Providers with different price intervals are merged on one common interval (same rule as BattMind)."""
from __future__ import annotations

import datetime
import logging

import pytest

from custom_components.cable_juice_planner.const import build_config, default_options
from custom_components.cable_juice_planner.prices import create_price_provider
from custom_components.cable_juice_planner.prices.intervals import common_interval_minutes, price_grid_minutes

DAY = datetime.date(2026, 11, 22)
NEXT_DAY = DAY + datetime.timedelta(days=1)


def _at(day: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(hour, minute))


def _series(day: datetime.date, minutes: int, price, hours=range(24)) -> dict:
    """{timestamp: price} every `minutes` minutes of the hours; price(hour, minute)."""
    return {
        _at(day, hour, minute): price(hour, minute)
        for hour in hours
        for minute in range(0, 60, minutes)
    }


class _Stub:
    def __init__(self, name: str, real: dict, forecast: dict | None = None, sell_details: dict | None = None) -> None:
        self.source_name = name
        self._real = real
        self._forecast = forecast or {}
        self.sell_price_details = sell_details or {}

    def get_buy_real_prices(self):
        return self._real

    def get_buy_forecast_prices(self):
        return self._forecast

    def get_sell_real_prices(self):
        return self._real

    def get_sell_forecast_prices(self):
        return self._forecast


def _combined(*providers: _Stub):
    provider = create_price_provider(build_config({
        **default_options(),
        "energidataservice_buy_entity_id": "sensor.eds",
    }))
    provider.providers = list(providers)
    return provider


def _combine(*providers: _Stub, kind: str = "buy"):
    return _combined(*providers)._combine(kind)


def test_an_hourly_price_covers_every_quarter_of_its_hour_before_a_lower_provider() -> None:
    """The reported case: Strømligning hourly (:00 only) next to Energi Data Service every 15 minutes."""
    hourly = _Stub("stromligning", _series(DAY, 60, lambda h, m: 2.0 + h / 100, hours=range(12)))
    quarters = _Stub("energidataservice", _series(DAY, 15, lambda h, m: 2.5 + h / 100))
    prices, real, sources, _ = _combine(hourly, quarters)

    assert len(prices) == 96 and real == prices
    for hour in range(12):
        for minute in (0, 15, 30, 45):
            assert prices[_at(DAY, hour, minute)] == pytest.approx(2.0 + hour / 100)
            assert sources[_at(DAY, hour, minute)] == "stromligning:real"
    for hour in range(12, 24):
        for minute in (0, 15, 30, 45):
            assert prices[_at(DAY, hour, minute)] == pytest.approx(2.5 + hour / 100)
            assert sources[_at(DAY, hour, minute)] == "energidataservice:real"


def test_hourly_providers_alone_keep_one_price_per_hour() -> None:
    first = _Stub("stromligning", _series(DAY, 60, lambda h, m: 1.0, hours=range(10)))
    second = _Stub("energidataservice", _series(DAY, 60, lambda h, m: 3.0))
    prices, _real, sources, _ = _combine(first, second)
    assert len(prices) == 24 and all(timestamp.minute == 0 for timestamp in prices)
    assert sources[_at(DAY, 9)] == "stromligning:real" and sources[_at(DAY, 10)] == "energidataservice:real"


def test_forecast_hours_of_an_hourly_provider_get_every_slot_of_the_grid() -> None:
    hourly = _Stub("stromligning", {}, _series(NEXT_DAY, 60, lambda h, m: 2.0))
    quarters = _Stub("energidataservice", _series(DAY, 15, lambda h, m: 3.0))
    prices, real, sources, _ = _combine(hourly, quarters)
    assert sum(1 for timestamp in prices if timestamp.date() == NEXT_DAY) == 96
    assert all(sources[timestamp] == "stromligning:forecast" for timestamp in prices if timestamp.date() == NEXT_DAY)
    assert all(timestamp.date() == DAY for timestamp in real)


def test_a_real_price_of_a_lower_provider_beats_a_forecast_of_a_higher_one() -> None:
    hourly = _Stub("stromligning", {}, _series(DAY, 60, lambda h, m: 1.0))
    quarters = _Stub("energidataservice", _series(DAY, 15, lambda h, m: 3.0))
    prices, _real, sources, _ = _combine(hourly, quarters)
    assert set(sources.values()) == {"energidataservice:real"} and set(prices.values()) == {3.0}


@pytest.mark.parametrize(
    ("minutes", "grid"),
    [((60, 15), 15), ((60, 30), 30), ((60, 10), 10), ((10, 15), 5), ((60, 30, 10), 10), ((30, 20), 10), ((60,), 60)],
)
def test_the_grid_is_the_greatest_common_divisor_of_the_provider_intervals(minutes, grid) -> None:
    providers = [
        _Stub(f"provider{index}", _series(DAY, step, lambda h, m, index=index: 1.0 + index))
        for index, step in enumerate(minutes)
    ]
    prices, _real, sources, _ = _combine(*providers)
    assert len(prices) == 24 * 60 // grid
    assert {timestamp.minute % grid for timestamp in prices} == {0}
    assert set(sources.values()) == {"provider0:real"}
    assert price_grid_minutes(prices) == grid


def test_a_ten_and_a_fifteen_minute_provider_meet_on_five_minutes() -> None:
    ten = _Stub("stromligning", _series(DAY, 10, lambda h, m: 1.0, hours=range(6)))
    fifteen = _Stub("energidataservice", _series(DAY, 15, lambda h, m: 2.0))
    prices, _real, sources, _ = _combine(ten, fifteen)
    assert len(prices) == 288
    assert prices[_at(DAY, 5, 5)] == 1.0 and prices[_at(DAY, 5, 50)] == 1.0
    assert sources[_at(DAY, 6, 5)] == "energidataservice:real" and prices[_at(DAY, 6, 10)] == 2.0


def test_three_providers_each_fill_the_slots_the_higher_ones_do_not_cover() -> None:
    hourly = _Stub("stromligning", _series(DAY, 60, lambda h, m: 1.0, hours=range(8)))
    half = _Stub("middle", _series(DAY, 30, lambda h, m: 2.0, hours=range(4, 16)))
    quarters = _Stub("energidataservice", _series(DAY, 15, lambda h, m: 3.0))
    prices, _real, sources, _ = _combine(hourly, half, quarters)
    assert len(prices) == 96
    assert {sources[_at(DAY, 7, minute)] for minute in (0, 15, 30, 45)} == {"stromligning:real"}
    assert {sources[_at(DAY, 8, minute)] for minute in (0, 15, 30, 45)} == {"middle:real"}
    assert {sources[_at(DAY, 16, minute)] for minute in (0, 15, 30, 45)} == {"energidataservice:real"}


def test_an_unusable_quarter_is_filled_by_the_next_provider_and_never_stretches_its_neighbour() -> None:
    quarters = _series(DAY, 15, lambda h, m: 1.0, hours=range(6))
    quarters[_at(DAY, 2, 15)] = float("nan")
    first = _Stub("stromligning", quarters)
    second = _Stub("energidataservice", _series(DAY, 60, lambda h, m: 9.0))
    prices, _real, sources, _ = _combine(first, second)
    assert prices[_at(DAY, 2, 0)] == 1.0 and prices[_at(DAY, 2, 30)] == 1.0
    assert prices[_at(DAY, 2, 15)] == 9.0 and sources[_at(DAY, 2, 15)] == "energidataservice:real"


def test_an_interval_that_does_not_divide_the_hour_is_ignored_with_a_warning_once(caplog) -> None:
    from custom_components.cable_juice_planner.prices import intervals

    intervals._WARNED_INTERVALS.clear()
    odd = {_at(DAY, 0, 0): 1.0, _at(DAY, 0, 45): 1.1, _at(DAY, 1, 30): 1.2, _at(DAY, 2, 15): 1.3}
    hourly = _Stub("energidataservice", _series(DAY, 60, lambda h, m: 2.0, hours=range(4)))
    with caplog.at_level(logging.WARNING):
        prices, _real, _sources, _ = _combine(_Stub("stromligning", odd), hourly)
        _combine(_Stub("stromligning", odd), hourly)
    # 45 minutes does not divide an hour, 15 and 30 do: the grid is 15 minutes
    assert price_grid_minutes(prices) == 15
    assert len([record for record in caplog.records if "interval of 45" in record.getMessage()]) == 1


def test_a_row_the_provider_left_out_is_a_gap_and_never_a_longer_price() -> None:
    """The providers drop a price that is no number, so it is a missing row: the price before it must not cover it."""
    quarters = _series(DAY, 15, lambda h, m: 1.0, hours=range(6))
    del quarters[_at(DAY, 2, 15)]
    del quarters[_at(DAY, 4, 15)], quarters[_at(DAY, 4, 30)]
    first = _Stub("stromligning", quarters)
    second = _Stub("energidataservice", _series(DAY, 15, lambda h, m: 9.0))
    prices, real, sources, _ = _combine(first, second)
    for slot in (_at(DAY, 2, 15), _at(DAY, 4, 15), _at(DAY, 4, 30)):
        assert prices[slot] == 9.0 and sources[slot] == "energidataservice:real"
    assert prices[_at(DAY, 2, 0)] == 1.0 and prices[_at(DAY, 2, 30)] == 1.0 and prices[_at(DAY, 4, 45)] == 1.0
    alone, alone_real, _sources, _ = _combine(first)
    assert _at(DAY, 2, 15) not in alone and _at(DAY, 4, 15) not in alone and _at(DAY, 4, 30) not in alone
    assert alone_real == alone and len(alone) == 6 * 4 - 3


def test_an_hourly_day_and_a_quarter_day_of_one_provider_keep_their_own_interval() -> None:
    mixed = {**_series(DAY, 60, lambda h, m: 1.0), **_series(NEXT_DAY, 15, lambda h, m: 2.0)}
    del mixed[_at(DAY, 5)]
    below = _Stub("energidataservice", _series(DAY, 15, lambda h, m: 9.0), _series(NEXT_DAY, 15, lambda h, m: 9.0))
    prices, _real, sources, _ = _combine(_Stub("stromligning", mixed), below)
    # the hourly day is hourly: its prices cover the whole hour, the lower provider only fills the missing 05:00 hour
    for minute in (0, 15, 30, 45):
        assert prices[_at(DAY, 4, minute)] == 1.0 and sources[_at(DAY, 4, minute)] == "stromligning:real"
        assert prices[_at(DAY, 5, minute)] == 9.0 and sources[_at(DAY, 5, minute)] == "energidataservice:real"
    # the quarter day keeps its quarters
    assert {prices[_at(NEXT_DAY, 3, minute)] for minute in (0, 15, 30, 45)} == {2.0}
    assert len([timestamp for timestamp in prices if timestamp.date() == NEXT_DAY]) == 96


def test_a_tie_between_two_gaps_takes_the_smaller_and_a_day_with_one_price_keeps_its_hour() -> None:
    from custom_components.cable_juice_planner.prices.intervals import provider_intervals

    half = datetime.timedelta(minutes=30)
    # gaps 30, 30, 60: 30 is the most common, so no price covers more than 30 minutes that day
    tie = {_at(DAY, 0): 1.0, _at(DAY, 0, 30): 1.0, _at(DAY, 1): 1.0, _at(DAY, 2): 1.0}
    assert [end - start for start, end, _price in provider_intervals(tie)] == [half, half, half, half]
    # gaps 30, 60, 60: 60 is the most common
    even = {_at(DAY, 0): 1.0, _at(DAY, 0, 30): 1.0, _at(DAY, 1, 30): 1.0, _at(DAY, 2, 30): 1.0}
    assert provider_intervals(even)[0][1] == _at(DAY, 0, 30)
    # gaps 15, 45, 15: two quarters and one 45 minute gap, the quarter is the most common
    tied = {_at(DAY, 0): 1.0, _at(DAY, 0, 15): 1.0, _at(DAY, 1): 1.0, _at(DAY, 1, 15): 1.0}
    assert provider_intervals(tied)[0][1] == _at(DAY, 0, 15)
    assert provider_intervals({_at(DAY, 7): 1.0}) == [(_at(DAY, 7), _at(DAY, 8), 1.0)]


def test_a_provider_that_cannot_be_read_is_skipped_and_the_others_are_used() -> None:
    broken = _Stub("stromligning", {"not a time": 1.0})
    prices, _real, sources, _ = _combine(broken, _Stub("energidataservice", _series(DAY, 60, lambda h, m: 3.0)))
    assert len(prices) == 24 and set(sources.values()) == {"energidataservice:real"}


def test_a_real_provider_drops_a_price_that_is_no_number_and_it_is_missing_from_the_combined_prices() -> None:
    """Through the real provider class: 10:15 is NaN in a 15 minute feed, so the combined prices have no 10:15."""
    from custom_components.cable_juice_planner.prices import create_price_provider as create

    provider = create(build_config({**default_options(), "energidataservice_buy_entity_id": "sensor.eds"}))
    rows = [
        {"hour": (datetime.datetime.combine(DAY, datetime.time(0)) + datetime.timedelta(minutes=15 * index)).isoformat() + "+01:00",
         "price": float("nan") if index == 41 else 2.0}
        for index in range(96)
    ]
    provider.set_now(_at(DAY, 9))
    provider.set_snapshot({"attributes": {"sensor.eds": {"raw_today": rows}}, "values": {}})
    real = provider.get_buy_real_prices()
    assert _at(DAY, 10, 15) not in real and _at(DAY, 10) in real and _at(DAY, 10, 30) in real and len(real) == 95
    # through the combined prices (the ones the planner reads): 10:15 is not made up from 10:00
    combined = provider.get_buy_prices()
    assert _at(DAY, 10, 15) not in combined and len(combined) == 95


def test_the_sell_price_details_follow_every_slot() -> None:
    details = {_at(DAY, hour): {"price": 1.0 + hour, "price_timestamp": _at(DAY, hour), "solar_sell_price": 0.5} for hour in range(24)}
    hourly = _Stub("stromligning", _series(DAY, 60, lambda h, m: 1.0), sell_details=details)
    quarters = _Stub("energidataservice", _series(DAY, 15, lambda h, m: 3.0))
    prices, _real, _sources, sell_details = _combine(hourly, quarters, kind="sell")
    assert len(prices) == 96
    for minute in (0, 15, 30, 45):
        assert sell_details[_at(DAY, 7, minute)]["price"] == 8.0
    sell_details[_at(DAY, 7, 15)]["price"] = 99.0
    assert details[_at(DAY, 7)]["price"] == 8.0


def test_interval_helpers() -> None:
    assert common_interval_minutes([datetime.timedelta(minutes=60), datetime.timedelta(minutes=15)]) == 15
    assert common_interval_minutes([]) == 60
    assert price_grid_minutes({}) == 60
    assert price_grid_minutes(_series(DAY, 30, lambda h, m: 1.0)) == 30


def test_the_interval_cache_follows_the_prices() -> None:
    """The latest intervals are kept per prices: a changed set of prices never gets the intervals of an older one."""
    from custom_components.cable_juice_planner.prices.intervals import price_interval_at, price_intervals

    hourly = _series(DAY, 60, lambda h, m: 1.0, hours=range(4))
    first = price_intervals(hourly)
    assert price_intervals(dict(hourly)) == first
    first.clear()
    assert len(price_intervals(hourly)) == 4
    quarters = _series(DAY, 15, lambda h, m: 2.0, hours=range(4))
    assert price_interval_at(hourly, _at(DAY, 1, 20)) == (_at(DAY, 1), _at(DAY, 2), 1.0)
    assert price_interval_at(quarters, _at(DAY, 1, 20)) == (_at(DAY, 1, 15), _at(DAY, 1, 30), 2.0)
    hourly[_at(DAY, 1)] = 5.0
    assert price_interval_at(hourly, _at(DAY, 1, 20)) == (_at(DAY, 1), _at(DAY, 2), 5.0)


def test_hours_from_the_price_history_get_every_slot_of_the_grid() -> None:
    """The price history is hourly: an hour it fills in is as many slots as the prices around it."""
    from types import SimpleNamespace

    from custom_components.cable_juice_planner.engine.pricing import Pricing

    now = _at(DAY, 10, 20)
    history = {hour: {weekday: [1.0 + hour] for weekday in range(7)} for hour in range(24)}
    engine = SimpleNamespace(
        kwh_avg_prices_db={"history": history},
        now=lambda: now,
        current_hour=lambda: _at(DAY, 10),
        get_forecast_value=lambda values: values[-1],
        config={"solar": {"production_price": -1.0}},
    )
    known = {**_series(DAY, 15, lambda h, m: 2.0, hours=range(0, 12)), **_series(DAY, 15, lambda h, m: 2.0, hours=range(14, 24))}
    missing = Pricing._database_prices(engine, "history", known, 1)
    assert sorted(missing) == [_at(DAY, 12, minute) for minute in (0, 15, 30, 45)] + [_at(DAY, 13, minute) for minute in (0, 15, 30, 45)]
    assert {missing[_at(DAY, 12, minute)] for minute in (0, 15, 30, 45)} == {13.0}
    hourly_known = {**_series(DAY, 60, lambda h, m: 2.0, hours=range(0, 12)), **_series(DAY, 60, lambda h, m: 2.0, hours=range(14, 24))}
    assert sorted(Pricing._database_prices(engine, "history", hourly_known, 1)) == [_at(DAY, 12), _at(DAY, 13)]
    assert sorted(Pricing._database_prices(engine, "history", {}, 1))[:2] == [_at(DAY, 10), _at(DAY, 11)]
