"""What-if: what CJP charged before its own records, estimated from the recorder's hourly statistics (solar subtracted)."""
from __future__ import annotations

import datetime

import pytest

from custom_components.cable_juice_planner.engine.whatif.estimated_booking import charging_outside, estimate_booking
from custom_components.cable_juice_planner.engine.whatif.solar import hour_energy

from .test_economy import _engine
from .test_engine import DAY, TZ
from .test_whatif_derived_engine import MEASURED_BACKS, OLDER_BACKS, _evening, _seed as _seed_derived, _statistics
from .test_whatif_estimated_cjp import START, _kwh, _seed

H = 3600.0
T0 = 1_700_000_000 - 1_700_000_000 % 3600  # an exact UTC hour
BATTERY = 60.0
LOSS = 0.1


def hour(n: float) -> float:
    return T0 + n * H


def _estimate(power, *, soc=(20.0, 35.0), span=(0, 2), prices=None, sell=None, production=None, assumed=-1.0):
    prices = prices if prices is not None else {hour(n): 1.0 for n in range(-1, 6)}
    sell = sell if sell is not None else {hour(n): 0.4 for n in range(-1, 6)}
    return estimate_booking(
        hour(span[0]), hour(span[1]), soc[0], soc[1], {hour(n): v for n, v in power.items()}, prices, sell, production or {}, assumed,
        BATTERY, LOSS,
    )


def test_an_hour_is_the_charger_energy_and_the_solar_the_house_leaves_over() -> None:
    assert hour_energy([0.0, 0.0, 5000.0]) == (5.0, 0.0)
    # 3000 W of solar, the house takes 500 W besides the EV: 2.5 kWh of the 5 could be solar
    assert hour_energy([3000.0, 5500.0, 5000.0]) == (5.0, 2.5)
    assert hour_energy([9000.0, 5500.0, 5000.0]) == (5.0, 5.0)  # never more than the EV charged
    assert hour_energy([3000.0, 5500.0, 20.0]) == (0.0, 0.0)  # not a charging hour
    assert hour_energy([3000.0, None, 5000.0]) is None and hour_energy(None) is None and hour_energy([1.0, 2.0]) is None


def test_the_grid_part_is_at_the_grid_price_and_the_solar_part_at_what_cjp_values_it() -> None:
    # two hours of 5 kW: 10 kWh from the charger, the battery level rose 15 % (9 kWh in the battery: 10 / 9 / 1.1 = 1.01)
    grid = _estimate({0: [0.0, 5000.0, 5000.0], 1: [0.0, 5000.0, 5000.0]})
    assert grid == pytest.approx({"kwh": 10.0, "local_kwh": 0.0, "cost": 10.0, "percent": 15.0, "assumed_hours": 0})
    # solar in the first hour: 2.5 kWh of the 5 at the sell price 0.4 (no fixed production price: -1), the rest at the grid price 1.0
    mixed = _estimate({0: [3000.0, 5500.0, 5000.0], 1: [0.0, 5000.0, 5000.0]})
    assert mixed["local_kwh"] == pytest.approx(2.5) and mixed["cost"] == pytest.approx(2.5 * 1.0 + 2.5 * 0.4 + 5.0)
    # a fixed production price of 0.9 that was stored for the hour: the solar is worth 0.9 - 0.4, not the hourly price
    fixed = _estimate({0: [3000.0, 5500.0, 5000.0], 1: [0.0, 5000.0, 5000.0]}, production={hour(0): 0.9})
    assert fixed["cost"] == pytest.approx(2.5 * 1.0 + 2.5 * 0.5 + 5.0) and fixed["assumed_hours"] == 0
    # none stored for the hour: the one set now is used, and counted
    assumed = _estimate({0: [3000.0, 5500.0, 5000.0], 1: [0.0, 5000.0, 5000.0]}, assumed=0.9)
    assert assumed["cost"] == pytest.approx(2.5 * 1.0 + 2.5 * 0.5 + 5.0) and assumed["assumed_hours"] == 1


def test_a_connection_that_starts_in_the_middle_of_an_hour_gets_the_part_of_it_that_lies_inside() -> None:
    result = estimate_booking(
        hour(0.5), hour(2), 20.0, 30.0, {hour(0): [0.0, 5000.0, 5000.0], hour(1): [0.0, 5000.0, 5000.0]}, {hour(n): 1.0 for n in (0, 1)},
        {}, {}, -1.0, BATTERY, LOSS,
    )
    assert result["kwh"] == pytest.approx(2.5 + 5.0)  # half of the first hour and all of the second


def test_nothing_is_made_up_for_what_the_statistics_lack() -> None:
    power = {0: [0.0, 5000.0, 5000.0], 1: [0.0, 5000.0, 5000.0]}
    assert _estimate({0: power[0]}) == "no_power_history"  # an hour without a reading is not an hour without charging
    assert _estimate({0: power[0], 1: [0.0, None, 5000.0]}) == "no_power_history"
    assert _estimate(power, prices={hour(0): 1.0}) == "no_prices"
    solar = {0: [3000.0, 5500.0, 5000.0], 1: power[1]}
    assert _estimate(solar, sell={hour(1): 0.4}) == "no_sell_prices"
    # an hour without charging needs neither a price nor a sell price
    assert _estimate({0: power[0], 1: [3000.0, 500.0, 0.0]}, prices={hour(0): 1.0}, sell={}, soc=(20.0, 28.0))["kwh"] == pytest.approx(5.0)
    assert _estimate(power, soc=(None, 35.0)) == "no_levels" and _estimate(power, soc=(35.0, 20.0)) == "no_levels"


def test_a_connection_whose_energy_and_battery_levels_do_not_agree_is_left_out() -> None:
    power = {0: [0.0, 5000.0, 5000.0], 1: [0.0, 5000.0, 5000.0]}
    assert _estimate(power, soc=(20.0, 70.0)) == "implausible"  # 30 kWh into the battery from 10 kWh at the charger
    assert _estimate(power, soc=(20.0, 24.0)) == "implausible"  # 2.4 kWh into the battery from 10 kWh at the charger
    assert _estimate({0: [0.0, 0.0, 5000.0], 1: [0.0, 0.0, 5000.0]}, soc=(30.0, 30.0)) == "implausible"  # charged, level did not rise
    # a small rise is checked in kWh (the percentage is mostly noise): 0.5 kWh at the charger, +2.9 % = 1.9 kWh needed
    assert _estimate({0: [0.0, 500.0, 500.0], 1: [0.0, 0.0, 0.0]}, soc=(20.0, 22.9)) == "implausible"
    assert _estimate({0: [0.0, 1500.0, 1500.0], 1: [0.0, 0.0, 0.0]}, soc=(20.0, 22.9))["kwh"] == pytest.approx(1.5)
    # the charger gives 25 % less than the battery level rise needs: CJP would be given energy for nothing
    assert _estimate({0: [0.0, 5000.0, 5000.0], 1: [0.0, 5000.0, 5000.0]}, soc=(20.0, 40.0)) == "implausible"


def test_charging_that_no_connection_covers_is_counted_as_outside() -> None:
    power = {hour(n): [0.0, 5000.0, 5000.0] for n in range(0, 6)}
    assert charging_outside(power, [(hour(0), hour(2)), (hour(3), hour(5))], hour(0), hour(6)) == pytest.approx(10.0)
    assert charging_outside(power, [(hour(0), hour(6))], hour(0), hour(6)) == pytest.approx(0.0)
    assert charging_outside(power, [(hour(0.5), hour(2))], hour(0), hour(2)) == pytest.approx(2.5)  # half of the first hour


# ----------------------------------------------------------------------------------------------- the engine


def _power_hours(engine, backs, *, solar: float = 0.0, with_ev: bool = True, skip: int | None = None) -> None:
    """The hourly [solar W, house W, charger W] of the evenings, with the charger running 02-05 (the energy the history of the
    measured evenings holds); skip: one hour of the first evening that has no reading."""
    watt = _kwh(engine) * 1000.0 / 3.0
    table = engine.economy_db.setdefault("power_hours", {})
    for back in backs:
        start, end = _evening(back)
        moment = start
        while moment < end:
            local = datetime.datetime.fromtimestamp(moment, TZ).hour
            charging = with_ev and 2 <= local < 5
            table[moment] = [solar if charging else 0.0, watt if charging else 0.0, watt if charging else 0.0]
            moment += 3600
    if skip is not None:
        start, _ = _evening(skip)
        table.pop(start + 8 * 3600, None)


def _run(engine, snap):
    engine.run_job(snap, engine.store_derived_windows, {"ev_hours": _statistics((*MEASURED_BACKS, *OLDER_BACKS))})
    assert engine.economy_db["derived_windows"]["status"] == "ok"
    prepared = engine.run_job(snap, engine.what_if_prepare)
    assert prepared.error is None, prepared.error
    result = engine.run_job(snap, engine.what_if_run, {**START, "period": prepared.value["period"], "first_window_start": prepared.value["first_window_start"]})
    assert result.error is None, result.error
    return result.value


def _seed_history_from_the_measured_evenings(engine) -> None:
    # (the history reaches back to the noon before the oldest measured evening: the evening before it ends earlier)
    _seed(engine, history_backs=MEASURED_BACKS, marker_back=max(MEASURED_BACKS))


def test_the_estimated_connections_before_the_history_get_what_the_car_charged_from_the_statistics() -> None:
    engine, snap = _engine({"what_if_days": 365, "fill_up": True})
    _seed_history_from_the_measured_evenings(engine)
    _power_hours(engine, OLDER_BACKS)
    r = _run(engine, snap)
    coverage = r["coverage"]
    assert r["status"] == "ok" and coverage["recorded_estimates"] == "used"
    assert coverage["recorded_estimated_booked"] == len(OLDER_BACKS) and coverage["recorded_estimated_booking_refused"] == {}
    assert coverage["recorded_estimated_booked_kwh"] == pytest.approx(len(OLDER_BACKS) * _kwh(engine), abs=0.01 * len(OLDER_BACKS))
    assert coverage["recorded_estimated_windows"] == len(OLDER_BACKS)
    # one cheap night each (0.3 per kWh): the same cost as the evenings CJP booked
    total = len(OLDER_BACKS) + len(MEASURED_BACKS)
    assert r["windows"]["total"] == total
    assert r["cjp"]["cost"] == pytest.approx(total * _kwh(engine) * 0.3, rel=0.01)
    assert sum(m["estimated_connections"] for m in r["months"]) == len(OLDER_BACKS)


def test_without_the_hourly_statistics_the_older_connections_stay_without_a_cjp_side() -> None:
    engine, snap = _engine({"what_if_days": 365, "fill_up": True})
    _seed_history_from_the_measured_evenings(engine)
    r = _run(engine, snap)
    coverage = r["coverage"]
    assert coverage["recorded_estimated_booked"] == 0
    assert coverage["recorded_estimated_booking_refused"] == {"no_power_history": len(OLDER_BACKS)}
    assert r["windows"]["total"] == len(MEASURED_BACKS)  # (the older ones are not compared: nothing is filled in)


def test_one_hour_without_a_reading_leaves_that_connection_out_and_the_others_are_estimated() -> None:
    engine, snap = _engine({"what_if_days": 365, "fill_up": True})
    _seed_history_from_the_measured_evenings(engine)
    _power_hours(engine, OLDER_BACKS, skip=20)
    r = _run(engine, snap)
    coverage = r["coverage"]
    assert coverage["recorded_estimated_booking_refused"] == {"no_power_history": 1}
    assert coverage["recorded_estimated_booked"] == len(OLDER_BACKS) - 1
    assert coverage["recorded_skipped"]["before_history"] >= 1  # a gap: the comparison goes on after it in stretches
    assert r["segments"] >= 2


def test_solar_hours_need_a_sell_price_and_are_valued_like_cjp_values_the_solar() -> None:
    engine, snap = _engine({"what_if_days": 365, "fill_up": True})
    _seed_history_from_the_measured_evenings(engine)
    _power_hours(engine, OLDER_BACKS, solar=1500.0)
    refused = _run(engine, snap)["coverage"]
    assert refused["recorded_estimated_booked"] == 0 and refused["recorded_estimated_booking_refused"] == {"no_sell_prices": len(OLDER_BACKS)}
    # with the sell prices the solar share is subtracted from the grid cost: it is valued at the sell price (0.1), not the grid price
    sell = engine.economy_db.setdefault("sell_prices_history", {})
    for back in OLDER_BACKS:
        start, end = _evening(back)
        moment = start
        while moment < end:
            sell[moment] = 0.1
            moment += 3600
    r = _run(engine, snap)
    coverage = r["coverage"]
    assert coverage["recorded_estimated_booked"] == len(OLDER_BACKS)
    assert coverage["recorded_estimated_booked_local_kwh"] == pytest.approx(len(OLDER_BACKS) * 3 * 1.5, rel=0.01)
    assert r["cjp"]["cost"] < (len(OLDER_BACKS) + len(MEASURED_BACKS)) * _kwh(engine) * 0.3


def test_much_charging_outside_the_connections_gives_no_estimate_at_all() -> None:
    engine, snap = _engine({"what_if_days": 365, "fill_up": True})
    _seed_history_from_the_measured_evenings(engine)
    _power_hours(engine, OLDER_BACKS)
    start, _ = _evening(30)
    for n in range(24):  # a day of charging at the car's other charger, between the evenings
        engine.economy_db["power_hours"][start + (30 + n) * 3600] = [0.0, 7000.0, 7000.0]
    r = _run(engine, snap)
    coverage = r["coverage"]
    assert coverage["recorded_estimated_booked"] == 0
    refused = coverage["recorded_estimated_booking_refused"]
    assert refused.get("outside_charging", 0) >= len(OLDER_BACKS) - 5 and sum(refused.values()) == len(OLDER_BACKS)
    assert coverage["recorded_estimated_booking_outside_kwh"] > 50.0  # (some of the added hours fall inside connections)


def test_the_plan_without_solar_gets_a_cjp_figure_for_the_old_months_only_when_every_connection_has_one() -> None:
    engine, snap = _engine({"what_if_days": 365})
    _seed_derived(engine)
    engine.run_job(snap, engine.store_derived_windows, {"ev_hours": _statistics((*MEASURED_BACKS, *OLDER_BACKS))})
    engine.economy_db["solar_energy_since"] = 0.0
    _power_hours(engine, OLDER_BACKS)
    for back in (3, 2):
        moment = datetime.datetime.combine(DAY - datetime.timedelta(days=back), datetime.time(20, 5))
        engine.charging_history_db[moment] = {
            "kWh": 10.0, "cost": 9.0, "kWh_from_grid": 6.0, "kWh_from_local_energy": 4.0, "solar_kwh_of_local_energy": 4.0,
            "powerwall_kwh_of_local_energy": 0.0, "ended": "21:00", "charging_type": ["planned"],
        }

    def run():
        prepared = engine.run_job(snap, engine.what_if_no_solar_prepare)
        result = engine.run_job(snap, engine.what_if_no_solar_run, {"soc": 33.0, "source": "entered", "period": prepared.value["period"], "first_window_start": prepared.value["first_window_start"]})
        assert result.error is None, result.error
        return result.value

    r = run()
    estimated = r["recorded_estimated"]
    assert estimated["connections"] == len(OLDER_BACKS) and estimated["left_out"] == {}
    old = [m for m in r["months"] if m["recorded_estimated"]]
    assert old and all(m["recorded_kwh"] is not None and m["recorded_cost"] is not None and m["recorded_missing"] == 0 for m in old)
    assert sum(m["recorded_estimated"] for m in r["months"]) == len(OLDER_BACKS)
    # the table (the measured span) is unchanged: the estimate does not enter it
    assert r["recorded"]["sessions"] == 2

    # one older connection without a reading: its month has no figure (a partial month would show a large, false difference)
    engine.economy_db["power_hours"].pop(_evening(20)[0] + 8 * 3600)
    partial = run()
    assert partial["recorded_estimated"]["left_out"] == {"no_power_history": 1}
    gaps = [m for m in partial["months"] if m["recorded_missing"]]
    assert gaps and all(m["recorded_kwh"] is None and m["recorded_cost"] is None for m in gaps)
