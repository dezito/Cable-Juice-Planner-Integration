"""What-if Scenario D (first step): the EV's solar energy replaced by grid energy at the same charging times."""
from __future__ import annotations

import datetime

import pytest

from custom_components.cable_juice_planner.engine.whatif.solar import sell_value, solar_comparison

from .test_economy import _engine, _ts
from .test_engine import DAY

H = 3600.0
T0 = 1_700_000_000 - 1_700_000_000 % 3600  # an exact UTC hour


def hour(n: float) -> float:
    return T0 + n * H


def session(start: float, end: float | None, solar: float, *, kwh: float | None = None, **kwargs) -> dict:
    return {
        "start": start, "end": end, "public": False, "kwh": kwh if kwh is not None else solar + 2.0, "local": kwargs.get("local", solar),
        "solar": solar, "powerwall": kwargs.get("powerwall", 0.0), **{k: v for k, v in kwargs.items() if k in ("public",)},
    }


def compare(sessions, buy=None, sell=None, production=None, refund=0.0, since=0.0, month_of=lambda ts: "2026-01", current="2026-02"):
    hours = range(-2, 60)
    buy = buy if buy is not None else {hour(n): 1.0 for n in hours}
    sell = sell if sell is not None else {hour(n): 0.4 for n in hours}
    production = production if production is not None else {hour(n): -1.0 for n in hours}
    return solar_comparison(sessions, buy, sell, production, refund, since, current, month_of)


# --------------------------------------------------------------------------- pure comparison


def test_the_advantage_is_the_grid_cost_of_the_solar_energy_minus_its_sell_value() -> None:
    buy = {hour(10): 1.0, hour(11): 3.0}  # a two hour session: time-weighted 2.0, refund 0.2 -> 1.8
    result = compare([session(hour(10), hour(12), 4.0)], buy=buy, sell={hour(10): 0.4, hour(11): 0.4}, production={hour(10): -1.0, hour(11): -1.0}, refund=0.2)
    assert result["status"] == "ok" and result["sessions_counted"] == 1
    month = result["months"][0]
    assert month["solar_kwh"] == 4.0
    assert month["grid_cost"] == pytest.approx(4.0 * 1.8)
    assert month["sell_value"] == pytest.approx(4.0 * 0.4)
    assert month["advantage"] == pytest.approx(4.0 * (1.8 - 0.4))


def test_solar_is_valued_by_cjps_sell_value_rule_never_as_free() -> None:
    assert sell_value(0.4, -1.0) == 0.4  # no fixed price: the hourly sell price
    assert sell_value(1.0, 0.6) == pytest.approx(-0.4)  # a fixed price settles the difference to the spot price
    # a fixed production price above the hourly price: the solar energy is worth fixed - hourly
    result = compare([session(hour(10), hour(11), 2.0)], sell={hour(10): 0.1}, production={hour(10): 0.8}, buy={hour(10): 1.0})
    assert result["months"][0]["sell_value"] == pytest.approx(2.0 * 0.7)
    # and when the hourly price is above the fixed one the opportunity cost is negative, so the advantage grows
    result = compare([session(hour(10), hour(11), 2.0)], sell={hour(10): 1.0}, production={hour(10): 0.6}, buy={hour(10): 1.0})
    assert result["months"][0]["sell_value"] == pytest.approx(2.0 * -0.4)
    assert result["months"][0]["advantage"] == pytest.approx(2.0 * 1.4)


def test_a_part_hour_session_is_weighted_by_the_time_in_each_hour() -> None:
    buy = {hour(10): 1.0, hour(11): 3.0}
    result = compare([session(hour(10.5), hour(11.5), 1.0)], buy=buy, sell={hour(10): 0.0, hour(11): 0.0}, production={hour(10): -1.0, hour(11): -1.0})
    assert result["months"][0]["grid_cost"] == pytest.approx(2.0)


def test_a_session_with_no_length_is_priced_at_the_hour_it_started_in() -> None:
    result = compare([session(hour(10.2), hour(10.2), 1.0)], buy={hour(10): 5.0}, sell={hour(10): 1.0}, production={hour(10): -1.0})
    assert result["months"][0]["grid_cost"] == pytest.approx(5.0)


def test_a_missing_price_hour_leaves_the_session_out_and_is_counted_never_filled() -> None:
    buy = {hour(10): 1.0}  # hour 11 is missing
    result = compare([session(hour(10), hour(12), 4.0)], buy=buy)
    assert result["status"] == "unavailable" and result["reason"] == "no_buy_prices"
    assert result["left_out"]["missing_buy_price"] == 1 and result["months"] == []
    sell = {hour(10): 0.4}
    result = compare([session(hour(10), hour(12), 4.0)], sell=sell)
    assert result["reason"] == "no_sell_prices" and result["left_out"]["missing_sell_price"] == 1
    # a sell price without its production price is as unusable
    result = compare([session(hour(10), hour(11), 1.0)], production={})
    assert result["left_out"]["missing_sell_price"] == 1
    # one session that can be priced does not pull the unpriced one in
    result = compare([session(hour(10), hour(11), 1.0), session(hour(70), hour(71), 5.0)])
    assert result["sessions_counted"] == 1 and result["left_out"]["missing_buy_price"] == 1
    assert result["months"][0]["solar_kwh"] == 1.0


def test_non_finite_prices_are_missing_prices() -> None:
    result = compare([session(hour(10), hour(11), 1.0)], buy={hour(10): float("nan")})
    assert result["left_out"]["missing_buy_price"] == 1 and result["status"] == "unavailable"


def test_sessions_before_the_energy_fix_are_not_used_and_it_is_said_why() -> None:
    result = compare([session(hour(10), hour(11), 1.0)], since=hour(11))
    assert result["status"] == "unavailable" and result["reason"] == "waiting_for_new_sessions"
    assert result["left_out"]["before_energy_fix"] == 1
    result = compare([session(hour(10), hour(11), 1.0)], since=None)
    assert result["left_out"]["before_energy_fix"] == 1


def test_everything_that_is_not_a_finished_private_session_with_solar_is_left_out_and_counted() -> None:
    sessions = [
        {**session(hour(10), hour(11), 1.0), "public": True},
        session(hour(12), None, 1.0),  # still charging
        session(hour(13), float("nan"), 1.0),  # unreadable end time
        session(hour(14), hour(15), 0.0),  # no solar energy
        {**session(hour(16), hour(17), 1.0), "solar": -1.0},
        session(hour(18), hour(19), 1.0),
    ]
    result = compare(sessions)
    assert result["left_out"] == {
        "public": 1, "still_charging": 1, "invalid": 2, "without_solar": 1, "before_energy_fix": 0, "missing_buy_price": 0, "missing_sell_price": 0,
        "not_captured": 0,
    }
    assert result["sessions_counted"] == 1


def test_the_solar_energy_never_exceeds_what_the_ev_charged_or_took_from_local_energy() -> None:
    assert compare([session(hour(10), hour(11), 5.0, kwh=3.0, local=5.0)])["months"][0]["solar_kwh"] == 3.0
    assert compare([session(hour(10), hour(11), 5.0, kwh=9.0, local=2.0)])["months"][0]["solar_kwh"] == 2.0
    assert compare([session(hour(10), hour(11), 5.0, kwh=9.0, local=0.0)])["left_out"]["without_solar"] == 1


def test_months_are_by_the_start_of_the_session_and_only_completed_months_are_summed() -> None:
    months = {hour(10): "2026-01", hour(30): "2026-02"}
    result = compare(
        [session(hour(10), hour(11), 2.0), session(hour(30), hour(31), 3.0)], month_of=lambda ts: months[ts], current="2026-02",
    )
    assert [(row["month"], row["status"]) for row in result["months"]] == [("2026-01", "completed"), ("2026-02", "current")]
    assert result["total"]["months"] == 1 and result["total"]["solar_kwh"] == 2.0
    assert result["total"]["advantage"] == result["months"][0]["advantage"]  # the current month is shown, not added


def test_the_home_battery_is_kept_as_recorded_and_never_replaced_or_valued() -> None:
    result = compare([session(hour(10), hour(11), 2.0, powerwall=3.0)])
    assert result["powerwall_kwh"] == 3.0
    assert result["months"][0]["solar_kwh"] == 2.0 and result["months"][0]["grid_cost"] == pytest.approx(2.0)
    assert "home_battery_kept_as_recorded" in result["notes"] and "no_whole_house_result" in result["notes"]
    assert "replanned_without_solar_not_modelled" in result["notes"] and "no_solar_production_not_modelled" in result["notes"]


def test_no_sessions_at_all_is_unavailable_not_zero() -> None:
    result = compare([])
    assert result["status"] == "unavailable" and result["reason"] == "no_solar_sessions" and result["total"]["advantage"] == 0.0


# --------------------------------------------------------------------------- engine


def _solar_engine(**options):
    engine, snap = _engine()
    engine.solar_configured = True
    engine.price_provider.refund = options.get("refund", 0.2)
    engine.charging_history_db.clear()  # the test engine starts with an open (current) session of its own
    engine.economy_db["solar_energy_since"] = 0.0
    return engine, snap


def _history(engine, day: datetime.date, hour_: int, ended: str, **values) -> datetime.datetime:
    key = datetime.datetime.combine(day, datetime.time(hour_, 5))
    engine.charging_history_db[key] = {
        "kWh": 6.0, "cost": 1.0, "kWh_from_grid": 2.0, "kWh_from_local_energy": 4.0, "solar_kwh_of_local_energy": 4.0,
        "powerwall_kwh_of_local_energy": 0.0, "ended": ended, "charging_type": ["local_energy"], **values,
    }
    return key


def _prices(engine, day: datetime.date, hours=range(24), buy=1.0, sell=0.4, production=-1.0) -> None:
    for h in hours:
        ts = _ts(day, h)
        engine.economy_db.setdefault("prices", {})[ts] = buy
        engine.economy_db.setdefault("sell_prices", {})[ts] = sell
        engine.economy_db.setdefault("sell_production_price", {})[ts] = production


def _ledger(engine, snap):
    result = engine.run_job(snap, engine.update_solar_ledger)
    assert result.error is None, result.error
    return engine.economy_db.get("solar_ledger", {})


def _publish(engine, snap):
    result = engine.run_job(snap, engine.publish_what_if_solar)
    assert result.error is None, result.error
    return result


def _data(engine, snap) -> dict:
    return _publish(engine, snap).attributes["what_if_solar"]["result"]


RECENT = DAY - datetime.timedelta(days=2)  # inside the window in which a session is priced (the clock of the test is DAY 12:00)


def test_a_session_is_priced_once_when_it_has_ended_and_kept_in_the_ledger() -> None:
    engine, snap = _solar_engine()
    _prices(engine, RECENT)
    _history(engine, RECENT, 11, "12:35")  # 11:05 -> 12:35, 4 kWh of solar
    ledger = _ledger(engine, snap)
    assert len(ledger) == 1
    entry = next(iter(ledger.values()))
    assert entry["outcome"] == "counted" and entry["month"] == RECENT.strftime("%Y-%m")
    assert entry["grid_cost"] == pytest.approx(4.0 * (1.0 - 0.2)) and entry["sell_value"] == pytest.approx(4.0 * 0.4)
    assert entry["start"] == _ts(RECENT, 11) + 300 and entry["end"] == _ts(RECENT, 12) + 35 * 60
    # the card sums the ledger; a month that is not over is shown but not added
    result = _publish(engine, snap)
    data = result.attributes["what_if_solar"]["result"]
    month = data["months"][0]
    assert data["status"] == "ok" and month["status"] == ("current" if RECENT.strftime("%Y-%m") == DAY.strftime("%Y-%m") else "completed")
    assert month["advantage"] == pytest.approx(4.0 * (0.8 - 0.4)) and "recorded_sessions_only" in data["notes"]
    if month["status"] == "current":
        assert result.states["what_if_solar"] is None and data["total"]["months"] == 0


def test_the_state_is_the_advantage_of_the_completed_months() -> None:
    engine, snap = _solar_engine()
    engine.economy_db["solar_ledger"] = {
        "1": {"start": 1.0, "end": 2.0, "month": "2025-01", "outcome": "counted", "solar_kwh": 4.0, "grid_cost": 3.2, "sell_value": 1.6, "powerwall_kwh": 0.0},
        "2": {"start": 3.0, "end": 4.0, "month": "2025-02", "outcome": "counted", "solar_kwh": 2.0, "grid_cost": 2.0, "sell_value": 0.5, "powerwall_kwh": 0.0},
    }
    result = _publish(engine, snap)
    data = result.attributes["what_if_solar"]["result"]
    assert [row["status"] for row in data["months"]] == ["completed", "completed"]
    assert result.states["what_if_solar"] == data["total"]["advantage"] == pytest.approx(1.6 + 1.5)


def test_a_priced_session_is_never_repriced_when_prices_or_the_refund_change_later() -> None:
    engine, snap = _solar_engine()
    _prices(engine, RECENT)
    _history(engine, RECENT, 11, "12:35")
    first = dict(next(iter(_ledger(engine, snap).values())))
    _prices(engine, RECENT, buy=9.0, sell=5.0)
    engine.price_provider.refund = 3.0
    ledger = _ledger(engine, snap)
    assert len(ledger) == 1 and next(iter(ledger.values())) == first


def test_the_session_end_rolls_over_midnight_and_the_price_is_weighted_by_the_minutes() -> None:
    engine, snap = _solar_engine()
    day = DAY - datetime.timedelta(days=3)
    _prices(engine, day)
    _prices(engine, day + datetime.timedelta(days=1), buy=3.0)
    _history(engine, day, 23, "01:10")  # 23:05 -> 01:10 the next day
    sessions = engine.run_job(snap, engine._solar_sessions).value
    assert len(sessions) == 1
    assert sessions[0]["start"] == _ts(day, 23) + 300
    assert sessions[0]["end"] == _ts(day + datetime.timedelta(days=1), 1) + 600
    entry = next(iter(_ledger(engine, snap).values()))
    # 55 min at 1.0 (23:05-24:00), 60 + 10 min at 3.0: (55 x 1.0 + 70 x 3.0) / 125 = 2.12, minus the refund 0.2
    assert entry["grid_cost"] == pytest.approx(4.0 * (2.12 - 0.2))


def test_an_end_time_in_the_same_minute_is_not_a_day_long_session() -> None:
    engine, snap = _solar_engine()
    key = datetime.datetime.combine(RECENT, datetime.time(10, 5, 30))
    engine.charging_history_db[key] = {"kWh": 6.0, "kWh_from_local_energy": 4.0, "solar_kwh_of_local_energy": 4.0, "ended": "10:05", "charging_type": ["local_energy"]}
    session = engine.run_job(snap, engine._solar_sessions).value[0]
    assert session["end"] - session["start"] < 3600


def test_a_still_charging_or_unreadable_session_is_never_counted() -> None:
    engine, snap = _solar_engine()
    _prices(engine, RECENT)
    _history(engine, RECENT, 10, ">")
    _history(engine, RECENT, 12, "soon")
    ledger = _ledger(engine, snap)
    assert [entry["reason"] for entry in ledger.values()] == ["invalid"]  # the open one is not stored: it has not ended
    data = _data(engine, snap)
    assert (data["left_out"]["still_charging"], data["left_out"]["invalid"], data["status"]) == (1, 1, "unavailable")


def test_a_session_whose_prices_are_missing_waits_and_is_then_left_out_never_filled() -> None:
    engine, snap = _solar_engine()
    recent = DAY - datetime.timedelta(days=1)  # ended about a day ago: the missing prices may still arrive
    older = DAY - datetime.timedelta(days=3)  # ended more than 48 hours ago: give up
    _prices(engine, recent, sell=0.4)
    del engine.economy_db["sell_prices"][_ts(recent, 11)]
    _history(engine, recent, 11, "11:55")
    _history(engine, older, 11, "11:55")  # no prices at all for that day
    ledger = _ledger(engine, snap)
    assert len(ledger) == 1
    assert next(iter(ledger.values())) | {"start": 0, "end": 0} == {"start": 0, "end": 0, "month": older.strftime("%Y-%m"), "outcome": "left_out", "reason": "missing_buy_price"}
    # the price turns up in time: the waiting session is priced then
    engine.economy_db["sell_prices"][_ts(recent, 11)] = 0.4
    ledger = _ledger(engine, snap)
    assert sorted(entry["outcome"] for entry in ledger.values()) == ["counted", "left_out"]


def test_a_session_that_was_merged_into_the_daily_history_is_not_counted_twice() -> None:
    engine, snap = _solar_engine()
    _prices(engine, RECENT)
    key = _history(engine, RECENT, 11, "11:55")
    assert len(_ledger(engine, snap)) == 1
    # CJP merges its history per day after a week: the same energy now sits in one longer entry
    del engine.charging_history_db[key]
    _history(engine, RECENT, 9, "17:55", kWh=12.0, kWh_from_local_energy=8.0, solar_kwh_of_local_energy=8.0)
    ledger = _ledger(engine, snap)
    assert len(ledger) == 1 and next(iter(ledger.values()))["solar_kwh"] == 4.0


def test_a_session_older_than_the_pricing_window_that_was_never_priced_is_counted_as_not_captured() -> None:
    engine, snap = _solar_engine()
    old = DAY - datetime.timedelta(days=12)
    _prices(engine, old)
    _history(engine, old, 11, "11:55")
    assert _ledger(engine, snap) == {}  # it is not priced afterwards: its history may have been merged already
    data = _data(engine, snap)
    assert data["left_out"]["not_captured"] == 1 and data["status"] == "unavailable"


def test_public_and_solarless_sessions_are_counted_as_left_out() -> None:
    engine, snap = _solar_engine()
    _prices(engine, RECENT)
    _history(engine, RECENT, 8, "08:50", kWh_from_public=6.0)
    _history(engine, RECENT, 13, "13:50", kWh_from_local_energy=0.0, solar_kwh_of_local_energy=0.0)
    _ledger(engine, snap)
    data = _data(engine, snap)
    assert data["left_out"]["public"] == 1 and data["left_out"]["without_solar"] == 1 and data["sessions_counted"] == 0


def test_sessions_before_the_energy_fix_marker_are_not_used() -> None:
    engine, snap = _solar_engine()
    engine.economy_db["solar_energy_since"] = _ts(DAY, 0)  # later than the session
    _prices(engine, RECENT)
    _history(engine, RECENT, 11, "12:35")
    _ledger(engine, snap)
    data = _data(engine, snap)
    assert data["status"] == "unavailable" and data["reason"] == "waiting_for_new_sessions" and data["left_out"]["before_energy_fix"] == 1


def test_unavailable_without_solar_or_a_price_provider() -> None:
    engine, snap = _engine()
    engine.solar_configured = False
    data = _data(engine, snap)
    assert data["status"] == "unavailable" and data["reason"] == "no_solar_configured"
    assert engine.run_job(snap, engine.update_solar_ledger).error is None and not engine.economy_db.get("solar_ledger")
    engine.solar_configured = True
    engine.price_provider.refund = None
    result = _publish(engine, snap)
    assert result.attributes["what_if_solar"]["result"]["reason"] == "no_price_provider" and result.states["what_if_solar"] is None
    assert engine.run_job(snap, engine.update_solar_ledger).error is None and not engine.economy_db.get("solar_ledger")


def test_the_recorded_cost_is_not_used() -> None:
    engine, snap = _solar_engine()
    _prices(engine, RECENT)
    key = _history(engine, RECENT, 11, "11:55", cost=999.0)
    _ledger(engine, snap)
    cheap = _data(engine, snap)["months"][0]["advantage"]
    engine.charging_history_db[key]["cost"] = 0.0
    assert _data(engine, snap)["months"][0]["advantage"] == cheap


def test_the_ledger_is_trimmed_after_400_days() -> None:
    engine, snap = _solar_engine()
    engine.economy_db["solar_ledger"] = {
        "1": {"start": _ts(DAY - datetime.timedelta(days=401), 10), "end": 1.0, "month": "x", "outcome": "left_out", "reason": "public"},
        "2": {"start": _ts(DAY - datetime.timedelta(days=100), 10), "end": 1.0, "month": "y", "outcome": "left_out", "reason": "public"},
    }
    assert list(_ledger(engine, snap)) == ["2"]


def test_a_failing_calculation_never_breaks_the_job_that_publishes_it() -> None:
    engine, snap = _solar_engine()

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    engine.what_if_solar_result = broken
    result = engine.run_job(snap, engine.publish_what_if_solar)
    assert result.error is None
    assert result.attributes["what_if_solar"]["result"] == {"status": "unavailable", "reason": "internal_error"}
    assert result.states["what_if_solar"] is None


def test_sell_value_is_one_rule_in_the_planner_and_in_the_comparison() -> None:
    engine, snap = _engine()
    for production in (-1.0, 0.0, 0.8):
        for price in (0.4, 1.0, -0.2):
            assert engine.sell_value(price, production) == pytest.approx(sell_value(price, production))
    # without the argument it is the configured production price, as before
    engine.config["solar"]["production_price"] = 0.8
    assert engine.sell_value(0.5) == pytest.approx(0.3)


# --------------------------------------------------------------------------- data collection


def _sell_source(engine, day: datetime.date, hours, *, source="provider:real", price=0.5) -> None:
    prices = {datetime.datetime.combine(day, datetime.time(h)): price for h in hours}
    engine.get_sell_prices = lambda *args, **kwargs: prices  # type: ignore[method-assign]
    engine.sell_price_cache["sources"] = {moment: source for moment in prices}


def test_hourly_sell_prices_are_stored_once_with_the_production_price_in_effect() -> None:
    engine, snap = _solar_engine()
    engine.get_grid_prices = lambda *args, **kwargs: {}  # type: ignore[method-assign]
    engine.config["solar"]["production_price"] = 0.75
    day = DAY - datetime.timedelta(days=1)
    _sell_source(engine, day, range(24))
    assert engine.run_job(snap, engine.economy_store_prices).error is None
    assert engine.economy_db["sell_prices"][_ts(day, 10)] == 0.5
    assert engine.economy_db["sell_production_price"][_ts(day, 10)] == 0.75
    assert engine.economy_db["sell_prices_since"] > 0
    # a later change of the production price never rewrites an hour that was stored
    engine.config["solar"]["production_price"] = -1.0
    _sell_source(engine, day, range(24), price=0.9)
    assert engine.run_job(snap, engine.economy_store_prices).error is None
    assert engine.economy_db["sell_production_price"][_ts(day, 10)] == 0.75 and engine.economy_db["sell_prices"][_ts(day, 10)] == 0.5


def test_estimated_or_forecast_sell_prices_and_future_hours_are_never_stored() -> None:
    engine, snap = _solar_engine()
    engine.get_grid_prices = lambda *args, **kwargs: {}  # type: ignore[method-assign]
    day = DAY - datetime.timedelta(days=1)
    _sell_source(engine, day, range(24), source="database")
    assert engine.run_job(snap, engine.economy_store_prices).error is None
    assert not engine.economy_db.get("sell_prices")
    _sell_source(engine, DAY + datetime.timedelta(days=2), range(24))  # not yet started
    assert engine.run_job(snap, engine.economy_store_prices).error is None
    assert not engine.economy_db.get("sell_prices")


def test_nothing_is_stored_without_solar() -> None:
    engine, snap = _engine()
    engine.solar_configured = False
    engine.get_grid_prices = lambda *args, **kwargs: {}  # type: ignore[method-assign]
    _sell_source(engine, DAY - datetime.timedelta(days=1), range(24))
    assert engine.run_job(snap, engine.economy_store_prices).error is None
    assert not engine.economy_db.get("sell_prices")


def test_a_failing_sell_price_source_never_stops_the_storing_of_the_buy_prices() -> None:
    engine, snap = _solar_engine()
    day = DAY - datetime.timedelta(days=1)
    buy = {datetime.datetime.combine(day, datetime.time(h)): 1.5 for h in range(24)}
    engine.get_grid_prices = lambda *args, **kwargs: buy  # type: ignore[method-assign]
    engine.grid_price_cache["sources"] = {moment: "provider:real" for moment in buy}

    def broken(*args, **kwargs):
        raise ValueError("No sell prices returned")

    engine.get_sell_prices = broken  # type: ignore[method-assign]
    result = engine.run_job(snap, engine.economy_store_prices)
    assert result.error is None
    assert engine.economy_db["prices"][_ts(day, 10)] == 1.5  # the buy prices were stored all the same
    assert not engine.economy_db.get("sell_prices")


def test_sell_prices_are_trimmed_after_400_days() -> None:
    engine, snap = _solar_engine()
    engine.get_grid_prices = lambda *args, **kwargs: {}  # type: ignore[method-assign]
    old = _ts(DAY - datetime.timedelta(days=401), 10)
    engine.economy_db["sell_prices"] = {old: 0.1}
    engine.economy_db["sell_production_price"] = {old: -1.0}
    _sell_source(engine, DAY - datetime.timedelta(days=1), range(24))
    assert engine.run_job(snap, engine.economy_store_prices).error is None
    assert old not in engine.economy_db["sell_prices"] and old not in engine.economy_db["sell_production_price"]


def test_the_hourly_store_prices_the_sessions_and_publishes_the_comparison() -> None:
    engine, snap = _solar_engine()
    day = RECENT
    engine.get_grid_prices = lambda *args, **kwargs: {}  # type: ignore[method-assign]
    _prices(engine, day)
    _history(engine, day, 11, "12:35")
    _sell_source(engine, DAY - datetime.timedelta(days=1), range(24))
    result = engine.run_job(snap, engine.economy_store_prices)
    assert result.error is None
    assert len(engine.economy_db["solar_ledger"]) == 1
    assert result.attributes["what_if_solar"]["result"]["sessions_counted"] == 1


def test_the_energy_fix_marker_is_set_once_at_startup() -> None:
    engine, snap = _engine()
    since = engine.economy_db["solar_energy_since"]
    assert since > 0
    assert engine.run_job(snap, engine.load_economy_db, engine.economy_db).error is None
    assert engine.economy_db["solar_energy_since"] == since  # an existing marker is kept
    # a database without the marker (an older version, a restored backup) gets one: its old sessions are not used
    engine.economy_db.pop("solar_energy_since")
    assert engine.run_job(snap, engine.load_economy_db, engine.economy_db).error is None
    assert engine.economy_db["solar_energy_since"] > 0
    assert engine.run_job(snap, engine.load_economy_db, {"windows": []}).error is None
    assert engine.economy_db["solar_energy_since"] > 0


# --------------------------------------------------------------------------- the energy unit fix


def _local(engine, snap, minutes, ev_kwh, watts):
    engine.solar_configured = True
    engine.local_energy_available = lambda **kwargs: (0.0, *watts)  # type: ignore[method-assign]
    result = engine.run_job(snap, engine.calc_local_energy_kwh, minutes, ev_kwh)
    assert result.error is None, result.error
    return result.value


def test_local_energy_is_power_times_the_period_not_the_power_itself() -> None:
    engine, snap = _engine()
    # 4 kW from local energy for 30 minutes into 3 kWh charged (6 kW): 2 kWh, not 4 kW read as 3 kWh
    assert _local(engine, snap, 30, 3.0, (4000.0, 3000.0, 1000.0)) == (2.0, 1.5, 0.5)
    # 2 hours at 4 kW into 10 kWh: 8 kWh (it used to be 4 and was capped at the 10)
    assert _local(engine, snap, 120, 10.0, (4000.0, 3000.0, 1000.0)) == (8.0, 6.0, 2.0)
    # a full hour is what it always was
    assert _local(engine, snap, 60, 5.0, (4000.0, 3000.0, 1000.0)) == (4.0, 3.0, 1.0)


def test_local_energy_never_exceeds_what_the_ev_used_and_the_sources_are_scaled_down_together() -> None:
    engine, snap = _engine()
    # 8 kW from local energy but the EV only drew 6 kW on average: all three are scaled down, never up
    local, solar, powerwall = _local(engine, snap, 30, 3.0, (8000.0, 6000.0, 2000.0))
    assert local == 3.0 and solar == pytest.approx(2.25) and powerwall == pytest.approx(0.75)
    assert solar + powerwall == pytest.approx(local)


def test_no_solar_configured_is_zero() -> None:
    engine, snap = _engine()
    engine.solar_configured = False
    assert engine.run_job(snap, engine.calc_local_energy_kwh, 60, 5.0).value == (0.0, 0.0, 0.0)


def test_a_period_of_zero_minutes_is_safe_and_never_more_than_the_ev_charged() -> None:
    engine, snap = _engine()
    # the clock of the test is on the hour: the "current hour so far" has no minutes
    result = _local(engine, snap, 60, 0.0, (4000.0, 3000.0, 1000.0))
    assert result == (0.0, 0.0, 0.0)
    engine.solar_configured = True
    engine.local_energy_available = lambda **kwargs: (0.0, 4000.0, 3000.0, 1000.0)  # type: ignore[method-assign]
    value = engine.run_job(snap, engine.calc_local_energy_kwh, 60, 3.0, True).value
    assert all(0.0 <= part <= 3.0 for part in value) and value[0] <= 3.0
