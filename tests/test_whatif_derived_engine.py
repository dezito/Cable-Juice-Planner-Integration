"""What-if with connections older than CJP's own records, estimated from the battery level and charger power statistics."""
from __future__ import annotations

import datetime

import pytest

from .test_economy import _engine, _ts
from .test_engine import DAY, TZ

MEASURED_BACKS = range(1, 16)  # the connections CJP measured itself: 1..15 days back
OLDER_BACKS = range(16, 41)  # the older ones that only the statistics know


def _evening(back: int) -> tuple[float, float]:
    day = DAY - datetime.timedelta(days=back)
    return _ts(day, 18), _ts(day + datetime.timedelta(days=1), 7)


def _statistics(backs, charging: bool = True) -> dict[float, list]:
    """The hourly statistics [mean level, lowest, highest, mean charger power] of the evenings and nights: arrival in the hour
    before 18:00 (the level falls 20 %), parked and charging 02-05, the drive away in the hour of 07:00, parked at work."""
    rows: dict[float, list] = {}
    for back in backs:
        start, end = _evening(back)
        rows[start - 3600] = [50.0, 40.0, 60.0, 0.0]
        hour = start
        while hour < end:
            local = datetime.datetime.fromtimestamp(hour, TZ).hour
            if charging and 2 <= local < 5:  # the level rises 20 % in the three hours
                low, high = 60.0 + (local - 2) * 20.0 / 3, 60.0 + (local - 1) * 20.0 / 3
                rows[hour] = [(low + high) / 2, low, high, 7000.0]
            else:
                level = 80.0 if charging and 5 <= local < 7 else 60.0
                rows[hour] = [level, level, level, 0.0]
            hour += 3600
        rows[end] = [79.0, 78.0, 80.0, 0.0]
        for work in range(1, 10):
            rows.setdefault(end + work * 3600, [78.0, 78.0, 78.0, 0.0])
    return rows


def _seed(engine) -> None:
    prices = engine.economy_db.setdefault("prices", {})
    for back in (*MEASURED_BACKS, *OLDER_BACKS):
        start, end = _evening(back)
        if back in MEASURED_BACKS:
            engine.economy_db["windows"].append({"start": start, "end": end})
            engine.analysis_db["drives"].append({"start": end + 300, "end": end + 3600, "km": 40.0, "used": 20.0, "temp": None})
        hour = start
        while hour < end:
            local = datetime.datetime.fromtimestamp(hour, TZ).hour
            prices[hour] = (0.3 if 2 <= local < 5 else 1.0) * (3.0 if back in OLDER_BACKS else 1.0)  # an older, dearer period
            hour += 3600
    engine.economy_db["prices_averaged_since"] = _ts(DAY - datetime.timedelta(days=60), 0)


def test_the_statistics_become_older_connections_after_being_checked_against_the_measured_ones() -> None:
    engine, snap = _engine({"what_if_days": 365})
    _seed(engine)
    stored = engine.run_job(snap, engine.store_derived_windows, {"ev_hours": _statistics((*MEASURED_BACKS, *OLDER_BACKS))})
    assert stored.error is None, stored.error
    derived = engine.economy_db["derived_windows"]
    assert derived["status"] == "ok" and derived["validation"]["passed"] and derived["validation"]["recall"] == 1.0
    assert len(derived["windows"]) == len(OLDER_BACKS)
    prepared = engine.run_job(snap, engine.what_if_prepare)
    result = engine.run_job(snap, engine.what_if_run, {"soc": 33.0, "source": "entered", "period": prepared.value["period"], "first_window_start": prepared.value["first_window_start"]})
    assert result.error is None, result.error
    r = result.value
    assert r["status"] == "ok", r
    assert r["windows"]["total"] == len(MEASURED_BACKS) + len(OLDER_BACKS)
    assert r["coverage"]["windows_derived"] == len(OLDER_BACKS) and r["coverage"]["windows_measured"] == len(MEASURED_BACKS)
    # the level at the first estimated connection comes from the statistics, not from the entered one
    assert r["provenance"]["start_soc"] == "battery_statistics" and r["smart"]["start_soc"] == 60.0
    assert "connections_estimated_from_battery_statistics" in r["warnings"]
    assert r["derived"]["windows"] == len(OLDER_BACKS)
    assert r["derived"]["difference"] < 0 < r["derived"]["asap_cost"] and r["derived"]["smart_cost"] < r["derived"]["asap_cost"]
    # the whole figure is the sum of the estimated part and the measured part, which stays apart
    assert sum(m["derived_windows"] for m in r["months"]) == len(OLDER_BACKS)
    assert r["difference"]["cost"] == pytest.approx(r["smart"]["cost"] - r["asap"]["cost"], abs=1e-2)
    assert r["smart"]["targets_met"] == r["windows"]["total"]


def test_the_plan_without_solar_values_the_recorded_energy_at_the_price_of_the_measured_span_only() -> None:
    engine, snap = _engine({"what_if_days": 365})
    _seed(engine)
    engine.run_job(snap, engine.store_derived_windows, {"ev_hours": _statistics((*MEASURED_BACKS, *OLDER_BACKS))})
    for back in (3, 2):  # recorded private charging with solar energy inside measured connections
        day = DAY - datetime.timedelta(days=back)
        engine.charging_history_db[datetime.datetime.combine(day, datetime.time(20, 5))] = {
            "kWh": 10.0, "cost": 9.0, "kWh_from_grid": 6.0, "kWh_from_local_energy": 4.0, "solar_kwh_of_local_energy": 4.0,
            "powerwall_kwh_of_local_energy": 0.0, "ended": "21:00", "charging_type": ["planned"],
        }
    prepared = engine.run_job(snap, engine.what_if_no_solar_prepare)
    result = engine.run_job(snap, engine.what_if_no_solar_run, {"soc": 33.0, "source": "entered", "period": prepared.value["period"], "first_window_start": prepared.value["first_window_start"]})
    assert result.error is None, result.error
    r = result.value
    assert r["status"] == "ok", r
    assert r["windows"]["derived"] == len(OLDER_BACKS) and r["windows"]["total"] == len(OLDER_BACKS) + len(MEASURED_BACKS)
    assert r["recorded"]["sessions"] == 2 and r["estimate"] is not None
    # the older period is three times dearer: valuing the recorded energy at the price of the whole replay would inflate the solar advantage
    assert r["estimate"]["unit"] < r["plan"]["unit"]
    # the table compares the recorded charging with the plan over the same (measured) span
    assert r["plan_measured"]["unit"] == r["estimate"]["unit"] and r["plan_measured"]["cost"] < r["plan"]["cost"]
    assert all(m["recorded_cost"] is None for m in r["months"] if m["recorded_kwh"] == 0)  # no recorded sessions: unknown, not 0


def test_statistics_that_do_not_match_the_measured_connections_give_no_older_connections() -> None:
    engine, snap = _engine({"what_if_days": 365})
    _seed(engine)
    # the charger power in the statistics is never above the threshold in the measured period: the method cannot be checked
    quiet = _statistics(OLDER_BACKS) | _statistics(MEASURED_BACKS, charging=False)
    engine.run_job(snap, engine.store_derived_windows, {"ev_hours": quiet})
    assert engine.economy_db["derived_windows"]["status"] == "unavailable" and engine.economy_db["derived_windows"]["reason"] == "not_validated"
    prepared = engine.run_job(snap, engine.what_if_prepare)
    result = engine.run_job(snap, engine.what_if_run, {"soc": 50.0, "source": "entered", "period": prepared.value["period"], "first_window_start": prepared.value["first_window_start"]})
    r = result.value
    assert r["status"] == "ok" and r["windows"]["total"] == len(MEASURED_BACKS) and r["derived"] is None
    assert r["provenance"]["start_soc"] == "entered" and "older_connections_not_validated" in r["warnings"]


def test_a_failed_statistics_read_keeps_what_was_stored_and_empty_statistics_clear_it() -> None:
    engine, snap = _engine({"what_if_days": 365})
    _seed(engine)
    engine.run_job(snap, engine.store_derived_windows, {"ev_hours": _statistics((*MEASURED_BACKS, *OLDER_BACKS))})
    stored = engine.economy_db["derived_windows"]
    engine.run_job(snap, engine.store_derived_windows, {})  # the read failed: no "ev_hours" key
    assert engine.economy_db["derived_windows"] is stored
    engine.run_job(snap, engine.store_derived_windows, {"ev_hours": {}})  # the entities have no statistics (any more)
    assert engine.economy_db["derived_windows"]["status"] == "unavailable" and engine.economy_db["derived_windows"]["reason"] == "no_statistics"


def test_the_work_plan_is_read_per_weekday_as_it_is_set_now() -> None:
    engine, snap = _engine({
        "workday_monday": True, "workday_departure_monday": datetime.time(7, 30), "workday_homecoming_monday": datetime.time(16, 15),
        "workday_distance_needed_monday": 42.0, "workday_tuesday": False,
    })
    result = engine.run_job(snap, engine.what_if_work_plan)
    assert result.error is None, result.error
    days = result.value["days"]
    assert sorted(days) == list(range(7))
    assert days[0]["workday"] is True and days[0]["departure"] == 7 * 3600 + 30 * 60 and days[0]["homecoming"] == 16 * 3600 + 15 * 60
    assert days[0]["km"] > 0 and days[1]["workday"] is False


def test_a_damaged_stored_estimate_is_dropped_when_the_database_is_loaded() -> None:
    engine, snap = _engine()
    assert engine.run_job(snap, engine.load_economy_db, {"windows": [], "derived_windows": [1, 2, 3]}).error is None
    assert "derived_windows" not in engine.economy_db
