"""Charging economy: achieved price compared with average, daily charging and the lowest possible."""
from __future__ import annotations

import datetime

import pytest

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.engine import CJPEngine
from custom_components.cable_juice_planner.engine.economy import average_price, fill_cheapest

from .test_engine import DAY, OPTIONS, TEXTS, TZ, snapshot

KW = 3 * 16 * 230 / 1000  # 11.04 kW, the test charger


def _ts(day: datetime.date, hour: int) -> float:
    return datetime.datetime.combine(day, datetime.time(hour), tzinfo=TZ).timestamp()


def test_fill_cheapest_takes_the_cheapest_hours_first() -> None:
    prices = {_ts(DAY, h): p for h, p in [(0, 1.0), (1, 0.5), (2, 2.0)]}
    # 15 kWh at 11.04 kW: the 0.5 hour full, the rest at 1.0
    assert fill_cheapest(prices, _ts(DAY, 0), _ts(DAY, 3), 15.0, KW) == pytest.approx(KW * 0.5 + (15 - KW) * 1.0)
    # more than fits in the window: the rest at the average price
    assert fill_cheapest(prices, _ts(DAY, 1), _ts(DAY, 2), 15.0, KW) == pytest.approx(KW * 0.5 + (15 - KW) * 0.5)
    assert average_price(prices, _ts(DAY, 0), _ts(DAY, 3)) == pytest.approx(3.5 / 3)
    assert fill_cheapest({}, 0.0, 3600.0, 1.0, KW) is None


def _engine(settings: dict | None = None) -> tuple[CJPEngine, object]:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    snap = snapshot(datetime.datetime.combine(DAY, datetime.time(12, 0)), settings=settings)
    assert engine.run_job(snap, engine.startup, {}).error is None
    return engine, snap


def test_month_compares_with_daily_and_lowest() -> None:
    """Plugged in Tuesday 17:00 to Thursday 08:00, departure Wednesday 07:00 in the work plan.
    CJP charged 22 kWh in Wednesday night's cheap hours (after the departure, the car stayed home)."""
    tuesday, wednesday = DAY - datetime.timedelta(days=2), DAY - datetime.timedelta(days=1)
    prices = {}
    for day, night in ((tuesday, 1.0), (wednesday, 0.4)):
        for hour in range(24):
            prices[_ts(day, hour)] = night if hour < 6 else 2.0
    engine, snap = _engine({"workplan_charging": True, "workday_wednesday": True, "workday_departure_wednesday": datetime.time(7, 0)})
    # CJP's charging history: two sessions in Wednesday night at 0.4
    wednesday_night = datetime.datetime.combine(wednesday, datetime.time(0))
    engine.charging_history_db.update({
        wednesday_night.replace(hour=1): {"kWh": 11.0, "cost": 4.4, "kWh_from_grid": 11.0},
        wednesday_night.replace(hour=2): {"kWh": 11.0, "cost": 4.4, "kWh_from_grid": 11.0},
    })
    window = (_ts(tuesday, 17), _ts(DAY, 8))
    result = engine.run_job(snap, engine.economy_update, {"prices": prices, "windows": [window], "odometer_months": {DAY.strftime("%Y-%m"): 440.0}})
    assert result.error is None, result.error
    month = result.attributes["economy"]["months"][-1]
    assert month["kwh"] == pytest.approx(22.0)
    assert month["achieved"] == pytest.approx(8.8)
    # daily charging: Wednesday's departure at 07:00, so Wednesday night 00-06 at 0.4 as well
    assert month["daily"] == pytest.approx(22.0 * 0.4)
    assert month["lowest"] == pytest.approx(22.0 * 0.4)
    assert month["average_unit"] > month["achieved_unit"]
    assert month["price_per_km"] == pytest.approx(8.8 / 440)
    assert result.states["economy"] == pytest.approx(0.0)


def test_daily_charging_cannot_wait_for_a_cheaper_night() -> None:
    """Departure Wednesday 07:00: daily charging must charge Tuesday night (1.0). The car stayed
    home on Wednesday and CJP waited for Wednesday night (0.4): saving and score 100 %."""
    tuesday, wednesday = DAY - datetime.timedelta(days=2), DAY - datetime.timedelta(days=1)
    prices = {}
    for day in (tuesday, wednesday, DAY):
        for hour in range(24):
            prices[_ts(day, hour)] = 2.0
    for hour in (22, 23):
        prices[_ts(tuesday, hour)] = 1.0
        prices[_ts(wednesday, hour)] = 0.4
    for hour in range(6):
        prices[_ts(wednesday, hour)] = 1.0
        prices[_ts(DAY, hour)] = 0.4
    engine, snap = _engine({"workplan_charging": True, "workday_wednesday": True, "workday_departure_wednesday": datetime.time(7, 0)})
    engine.charging_history_db[datetime.datetime.combine(wednesday, datetime.time(23))] = {"kWh": 11.0, "cost": 4.4, "kWh_from_grid": 11.0}
    window = (_ts(tuesday, 17), _ts(DAY, 8))
    month = engine.run_job(snap, engine.economy_update, {"prices": prices, "windows": [window]}).attributes["economy"]["months"][-1]
    assert month["daily"] == pytest.approx(11.0 * 1.0)
    assert month["lowest"] == pytest.approx(11.0 * 0.4)
    assert month["saving_daily"] == pytest.approx(11.0 - 4.4)
    assert month["score"] == pytest.approx(1.0)


def test_sources_per_month_and_public_charging() -> None:
    engine, snap = _engine()
    day = datetime.datetime.combine(DAY, datetime.time(2))
    engine.charging_history_db.update({
        day: {"kWh": 10.0, "cost": 5.0, "kWh_from_grid": 6.0, "kWh_from_local_energy": 4.0, "solar_kwh_of_local_energy": 3.0, "powerwall_kwh_of_local_energy": 1.0},
        day.replace(hour=14): {"kWh": 20.0, "cost": 60.0, "kWh_from_public": 20.0, "charging_type": "public_charging"},
    })
    month = engine.run_job(snap, engine.economy_update, {}).attributes["economy"]["months"][-1]
    assert (month["grid"], month["solar"], month["powerwall"], month["public"]) == (6.0, 3.0, 1.0, 20.0)
    assert month["public_cost"] == 60.0 and month["cost"] == 65.0
    # without connection windows there is nothing to compare
    assert month["score"] is None and month["saving_daily"] is None


def test_windows_are_recorded_from_the_charger_status() -> None:
    engine, snap = _engine()
    assert engine.run_job(snap, engine.charger_status_changed, "awaiting_start", "disconnected").error is None
    assert engine.economy_db["windows"][-1]["end"] is None
    assert engine.run_job(snap, engine.charger_status_changed, "disconnected", "completed").error is None
    assert engine.economy_db["windows"][-1]["end"] is not None


def test_own_prices_are_stored_and_used() -> None:
    """hour_end keeps the plan's prices of the passed hours; they win over the price sensor's history."""
    engine, snap = _engine()
    assert engine.run_job(snap, engine.economy_store_prices).error is None
    stored = engine.economy_db["prices"]
    assert stored and max(stored) <= _ts(DAY, 12)
    assert all(ts <= _ts(DAY, 12) for ts in stored)
    yesterday = DAY - datetime.timedelta(days=1)
    engine.charging_history_db[datetime.datetime.combine(DAY, datetime.time(2))] = {"kWh": 11.0, "cost": 11.0, "kWh_from_grid": 11.0}
    window = (_ts(yesterday, 20), _ts(DAY, 8))
    # the sensor history says 9.0, the plan's own prices (from the test prices) win where they exist
    sensor_prices = {_ts(yesterday, h): 9.0 for h in range(20, 24)} | {_ts(DAY, h): 9.0 for h in range(8)}
    month = engine.run_job(snap, engine.economy_update, {"prices": sensor_prices, "windows": [window]}).attributes["economy"]["months"][-1]
    assert month["lowest"] < 11.0 * 9.0


def test_quarter_hour_prices_are_stored_as_hour_averages_and_estimates_are_not_kept() -> None:
    """A stored hour is the average of its intervals (not the last quarter), only when fully
    covered, and never a price that was filled from history or a forecast."""
    engine, snap = _engine()
    at = lambda hour, minute=0: datetime.datetime.combine(DAY, datetime.time(hour, minute))  # noqa: E731
    prices: dict = {}
    sources: dict = {}

    def add(hour: int, values: tuple, source: str, minutes: tuple = (0, 15, 30, 45)) -> None:
        for minute, price in zip(minutes, values):
            prices[at(hour, minute)] = price
            sources[at(hour, minute)] = source

    add(7, (9.0,), "")  # no known source: never kept as a measured price
    add(8, (1.0, 2.0, 3.0, 6.0), "p:real")  # kept as the average
    add(9, (4.0, 4.0, 4.0), "p:real", minutes=(0, 15, 45))  # 09:30 is missing: not stretched over the hole
    add(10, (5.0, 5.0, 5.0, 5.0), "database")  # filled from the price history
    add(11, (7.0, 7.0, 7.0, 7.0), "p:forecast")  # a forecast
    add(12, (2.0, 2.0, 2.0, 2.0), "p:real")  # the hour that is starting/ending when the job runs: kept
    add(13, (1.0,), "p:real")  # not started yet
    engine.get_grid_prices = lambda *args, **kwargs: prices  # type: ignore[method-assign]
    engine.grid_price_cache = {"sources": sources}
    assert engine.run_job(snap, engine.economy_store_prices).error is None
    assert engine.economy_db["prices"] == {
        _ts(DAY, 8): pytest.approx((1.0 + 2.0 + 3.0 + 6.0) / 4),
        _ts(DAY, 12): pytest.approx(2.0),
    }
    assert "prices_averaged_since" in engine.economy_db


def test_score_when_the_window_ends_at_the_departure() -> None:
    """Plugged in in the evening, unplugged at the departure: daily = lowest, the score still shows
    how close CJP came to the cheapest hours, measured from the average."""
    yesterday = DAY - datetime.timedelta(days=1)
    prices = {_ts(yesterday, h): 2.0 for h in range(17, 24)} | {_ts(DAY, h): (0.5 if h < 4 else 1.5) for h in range(7)}
    engine, snap = _engine({"workplan_charging": True, "workday_thursday": True, "workday_departure_thursday": datetime.time(7, 0)})
    # CJP charged 11 kWh at 1.5 (not the cheapest 0.5 hours)
    engine.charging_history_db[datetime.datetime.combine(DAY, datetime.time(5))] = {"kWh": 11.0, "cost": 16.5, "kWh_from_grid": 11.0}
    month = engine.run_job(snap, engine.economy_update, {"prices": prices, "windows": [(_ts(yesterday, 17), _ts(DAY, 7))]}).attributes["economy"]["months"][-1]
    assert month["daily"] == pytest.approx(month["lowest"])
    average = sum(prices.values()) / len(prices)
    assert month["score"] == pytest.approx((average * 11 - 16.5) / (average * 11 - 0.5 * 11), abs=0.001)


def test_daily_without_prices_is_not_free() -> None:
    """Hours without a price after the departure were counted as 0: daily came out below the lowest."""
    yesterday = DAY - datetime.timedelta(days=1)
    # prices only until the departure at 01:00, the car stayed plugged in until 09:00 (no prices after)
    prices = {_ts(yesterday, h): 2.0 for h in range(22, 24)} | {_ts(DAY, 0): 2.0}
    engine, snap = _engine({"workplan_charging": True, "workday_thursday": True, "workday_departure_thursday": datetime.time(1, 0)})
    engine.charging_history_db[datetime.datetime.combine(DAY, datetime.time(3))] = {"kWh": 40.0, "cost": 80.0, "kWh_from_grid": 40.0}
    month = engine.run_job(snap, engine.economy_update, {"prices": prices, "windows": [(_ts(yesterday, 22), _ts(DAY, 9))]}).attributes["economy"]["months"][-1]
    assert month["daily"] >= month["lowest"] > 0
    assert month["daily_unit"] == pytest.approx(2.0)


def test_old_results_are_calculated_again() -> None:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    snap = snapshot(datetime.datetime.combine(DAY, datetime.time(12, 0)))
    old = {"windows": [{"start": 1.0, "end": 2.0, "kwh": 5.0, "achieved": 1.0, "daily": 0.1, "lowest": 0.5, "average": 2.0}]}
    assert engine.run_job(snap, engine.startup, {"economy": old}).error is None
    assert "daily" not in engine.economy_db["windows"][0] and engine.economy_db["version"] == 2

def test_subscription_compares_completed_recorded_months() -> None:
    """The fixed fee uses existing history cost, including local energy, and skips the current month."""
    engine, snap = _engine({"subscription_monthly_price": 100.0})
    previous = datetime.datetime.combine(DAY.replace(day=1) - datetime.timedelta(days=1), datetime.time(2))
    older = previous.replace(day=1) - datetime.timedelta(days=1)
    current = datetime.datetime.combine(DAY, datetime.time(2))
    engine.charging_history_db.update({
        older: {"kWh": 20.0, "cost": 140.0, "kWh_from_grid": 10.0, "kWh_from_local_energy": 10.0, "solar_kwh_of_local_energy": 10.0},
        previous: {"kWh": 10.0, "cost": 80.0, "kWh_from_public": 10.0, "charging_type": "public_charging"},
        current: {"kWh": 10.0, "cost": 200.0, "kWh_from_grid": 10.0},
    })

    attrs = engine.run_job(snap, engine.publish_economy).attributes["economy"]
    months = {row["month"]: row for row in attrs["months"]}
    assert months[older.strftime("%Y-%m")]["subscription_saving"] == 40.0
    assert months[previous.strftime("%Y-%m")]["subscription_saving"] == -20.0
    assert months[current.strftime("%Y-%m")]["subscription_status"] == "current"
    assert months[current.strftime("%Y-%m")]["subscription_saving"] is None
    assert attrs["subscription_total"] == {
        "enabled": True,
        "comparable_months": 2,
        "recorded_cost": 220.0,
        "price": 200.0,
        "saving": 20.0,
    }


def test_subscription_disabled_and_invalid_cost_is_incomplete() -> None:
    previous = datetime.datetime.combine(DAY.replace(day=1) - datetime.timedelta(days=1), datetime.time(2))
    engine, snap = _engine()
    engine.charging_history_db[previous] = {"kWh": 10.0, "cost": 50.0, "kWh_from_grid": 10.0}
    attrs = engine.run_job(snap, engine.publish_economy).attributes["economy"]
    assert attrs["months"][-1]["subscription_status"] == "disabled"
    assert attrs["subscription_total"]["enabled"] is False

    engine, snap = _engine({"subscription_monthly_price": 100.0})
    engine.charging_history_db[previous] = {"kWh": 10.0, "cost": float("nan"), "kWh_from_grid": 10.0}
    start = previous - datetime.timedelta(hours=1)
    end = previous + datetime.timedelta(hours=1)
    prices = {
        _ts(start.date(), start.hour): 1.0,
        _ts(previous.date(), previous.hour): 2.0,
    }
    attrs = engine.run_job(
        snap,
        engine.economy_update,
        {"prices": prices, "windows": [(_ts(start.date(), start.hour), _ts(end.date(), end.hour))]},
    ).attributes["economy"]
    month = next(row for row in attrs["months"] if row["month"] == previous.strftime("%Y-%m"))
    assert month["subscription_status"] == "incomplete"
    assert month["subscription_recorded_cost"] is None
    assert month["cost"] is None
    assert month["achieved_unit"] is None
    assert month["saving_daily"] is None
    assert month["score"] is None
    assert attrs["subscription_total"]["comparable_months"] == 0


def test_an_hour_with_only_its_first_quarter_on_a_quarter_hour_day_is_not_stored() -> None:
    engine, snap = _engine()
    at = lambda hour, minute=0: datetime.datetime.combine(DAY, datetime.time(hour, minute))  # noqa: E731
    prices = {at(8, m): 1.0 + m / 60 for m in (0, 15, 30, 45)}
    prices[at(9)] = 7.0  # only :00 of hour 9, while the day is quarter-hourly
    prices.update({at(10, m): 2.0 for m in (0, 15, 30, 45)})
    sources = {moment: "p:real" for moment in prices}
    engine.get_grid_prices = lambda *args, **kwargs: prices  # type: ignore[method-assign]
    engine.grid_price_cache = {"sources": sources}
    assert engine.run_job(snap, engine.economy_store_prices).error is None
    assert set(engine.economy_db["prices"]) == {_ts(DAY, 8), _ts(DAY, 10)}


def test_the_repeated_autumn_hour_is_stored_for_both_real_hours_and_marked_copied() -> None:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    change = datetime.date(2026, 10, 25)  # clocks go back at 03:00 CEST: 02:00-03:00 happens twice
    snap = snapshot(datetime.datetime.combine(change + datetime.timedelta(days=1), datetime.time(12, 0)))
    assert engine.run_job(snap, engine.startup, {}).error is None
    prices = {datetime.datetime.combine(change, datetime.time(h)): 1.0 + h for h in range(24)}  # one price for the repeated 02:00
    engine.get_grid_prices = lambda *args, **kwargs: prices  # type: ignore[method-assign]
    engine.grid_price_cache = {"sources": {moment: "p:real" for moment in prices}}
    assert engine.run_job(snap, engine.economy_store_prices).error is None
    first = datetime.datetime(2026, 10, 25, 0, tzinfo=datetime.UTC).timestamp()  # 02:00 CEST
    second = first + 3600.0  # 02:00 CET
    stored = engine.economy_db["prices"]
    assert stored[first] == 3.0 and stored[first + 7200.0] == 4.0  # 03:00 CET follows the second 02:00
    # the copy for the second 02:00 is kept apart: the monthly economy comparison never sees it
    assert second not in stored
    assert engine.economy_db["prices_dst_copy"] == {second: 3.0}


def test_the_daily_economy_update_keeps_the_price_sensor_statistics_for_the_what_if_replays() -> None:
    engine, snap = _engine()
    stored_hour, new_hour, old_hour = _ts(DAY, 10), _ts(DAY, 11), _ts(DAY - datetime.timedelta(days=500), 11)
    engine.economy_db["prices"] = {stored_hour: 1.5}
    result = engine.run_job(snap, engine.economy_update, {"prices": {stored_hour: 9.0, new_hour: 2.0, old_hour: 3.0}, "windows": [], "odometer_months": {}})
    assert result.error is None, result.error
    history = engine.economy_db["prices_history"]
    assert history[new_hour] == 2.0  # a price CJP did not store itself is kept
    assert stored_hour not in history  # a stored price always wins and is not duplicated
    assert old_hour not in history  # trimmed after 400 days
    # once CJP stores the hour itself, the history copy goes
    engine.economy_db["prices"][new_hour] = 2.5
    assert engine.run_job(snap, engine.economy_update, {"prices": {new_hour: 2.0}, "windows": [], "odometer_months": {}}).error is None
    assert new_hour not in engine.economy_db["prices_history"]
