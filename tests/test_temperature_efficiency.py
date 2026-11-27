"""The plan's km/kWh at the forecast outdoor temperature (engine/temperature_efficiency.py, switch temperature_efficiency)."""
from __future__ import annotations

import datetime
import re
from pathlib import Path

import pytest

from custom_components.cable_juice_planner.engine import temperature_efficiency as te

from .test_engine import DAY, PLUGGED_IN, TZ, run, snapshot

BATTERY = 75.0  # OPTIONS battery_size
FRIDAY = DAY + datetime.timedelta(days=1)
NOW = datetime.datetime.combine(DAY, datetime.time(12, 30))


def line(temp: float) -> float:
    """The test car: 4.0 km/kWh at 0 °C, 0.1 more per degree."""
    return 4.0 + 0.1 * temp


def drive(end: datetime.datetime, temp: float | None, km: float = 30.0, km_per_kwh: float | None = None) -> dict:
    km_per_kwh = km_per_kwh if km_per_kwh is not None else line(temp or 0.0)
    used = km / km_per_kwh / BATTERY * 100
    end_ts = end.replace(tzinfo=TZ).timestamp()
    return {"start": end_ts - 3600, "end": end_ts, "km": km, "used": round(used, 4), "temp": temp}


def history(temps: list[float], car=line) -> tuple[list[dict], list]:
    """Analysis drives on the evenings before DAY at these temperatures, and the learned km/kWh drives (newest first) that
    were recorded at the same moments (km_kwh_efficiency_db: [time, km/kWh])."""
    drives, entries = [], []
    for days_ago, temp in enumerate(reversed(temps), start=1):
        end = datetime.datetime.combine(DAY - datetime.timedelta(days=days_ago), datetime.time(18, 0))
        drives.append(drive(end, temp, km_per_kwh=car(temp)))
        entries.append([end, float(car(temp))])
    return sorted(drives, key=lambda d: d["end"]), entries


def points(temps: list[float]) -> list[dict]:
    return te.drive_points(history(temps)[0], BATTERY)


SPREAD = [-5.0, -2.0, 0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 15.0, 15.0]  # 12 drives over 20 °C


# --- the line ------------------------------------------------------------------------------------------------------


def test_the_line_is_the_cars_own_km_per_kwh_against_the_temperature() -> None:
    curve, reason = te.fit_curve(points(SPREAD))
    assert reason is None
    assert curve["b"] == pytest.approx(0.1, abs=1e-3)
    assert curve["a"] == pytest.approx(4.0, abs=1e-3)
    assert (curve["low"], curve["high"], curve["drives"]) == (-5.0, 15.0, 12)


def test_fewer_than_twelve_drives_keep_the_learned_value() -> None:
    assert te.fit_curve(points(SPREAD[:-1])) == (None, te.REASON_TOO_FEW_DRIVES)


def test_drives_over_less_than_eight_degrees_keep_the_learned_value() -> None:
    assert te.fit_curve(points([5.0 + i * 0.5 for i in range(14)])) == (None, te.REASON_TOO_NARROW)  # 5..11.5 °C


def test_a_line_that_does_not_fall_with_the_cold_is_not_used() -> None:
    drives = [drive(datetime.datetime(2026, 10, 1, 18) + datetime.timedelta(days=i), temp, km_per_kwh=5.0 - 0.05 * temp) for i, temp in enumerate(SPREAD)]
    assert te.fit_curve(te.drive_points(drives, BATTERY)) == (None, te.REASON_NO_COLD_EFFECT)


def test_a_line_that_reaches_zero_in_the_cold_is_not_used() -> None:
    drives = [drive(datetime.datetime(2026, 10, 1, 18) + datetime.timedelta(days=i), temp, km_per_kwh=0.5 + 0.4 * temp) for i, temp in enumerate([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0])]
    assert te.fit_curve(te.drive_points(drives, BATTERY)) == (None, te.REASON_IMPLAUSIBLE)


def test_drives_without_a_temperature_or_numbers_are_left_out() -> None:
    drives, _ = history(SPREAD)
    broken = [{**drives[0], "temp": None}, {**drives[1], "km": "x"}, {**drives[2], "used": 0}, {**drives[3], "temp": float("nan")}, "x"]
    assert len(te.drive_points([*broken, *drives[4:]], BATTERY)) == len(drives) - 4
    assert te.drive_points(drives, 0) == []


def test_the_line_stays_flat_beyond_five_degrees_outside_the_drives() -> None:
    curve, _ = te.fit_curve(points(SPREAD))
    assert te.curve_value(curve, -10.0) == pytest.approx(line(-10.0), abs=1e-3)
    assert te.curve_value(curve, -25.0) == pytest.approx(line(-10.0), abs=1e-3)
    assert te.curve_value(curve, 40.0) == pytest.approx(line(20.0), abs=1e-3)


# --- moving the learned drives -------------------------------------------------------------------------------------


def test_each_learned_drive_moves_from_its_own_temperature_to_the_forecast() -> None:
    curve, _ = te.fit_curve(points(SPREAD))
    entries = [["a", 5.0], ["b", 4.0]]
    moved = te.moved_entries(entries, [10.0, 0.0], curve, -5.0)
    assert [item[0] for item in moved] == ["a", "b"]
    assert moved[0][1] == pytest.approx(5.0 * line(-5) / line(10), abs=1e-3)
    assert moved[1][1] == pytest.approx(4.0 * line(-5) / line(0), abs=1e-3)


def test_drives_that_already_were_cold_are_not_made_colder_again() -> None:
    """The EMA of the last cold drives already holds the cold: moving them to an equally cold day changes nothing."""
    curve, _ = te.fit_curve(points(SPREAD))
    moved = te.moved_entries([["a", line(-5)], ["b", line(-5)]], [-5.0, -5.0], curve, -5.0)
    assert [item[1] for item in moved] == pytest.approx([line(-5), line(-5)])


def test_a_drive_without_a_temperature_keeps_its_value_and_too_few_with_one_move_nothing() -> None:
    curve, _ = te.fit_curve(points(SPREAD))
    moved = te.moved_entries([["a", 5.0], ["b", 4.5], 4.2], [10.0, 10.0, None], curve, 0.0)
    assert moved[2] == 4.2 and isinstance(moved[0][1], float)
    assert te.moved_entries([["a", 5.0], ["b", 4.5], 4.2], [10.0, None, None], curve, 0.0) is None
    assert te.moved_entries([], [], curve, 0.0) is None


def test_the_learned_drives_get_the_temperature_of_the_analysis_drive_that_ended_at_the_same_time() -> None:
    drives, entries = history([0.0, 5.0, 10.0])
    pts = te.drive_points(drives, BATTERY)
    later = [[entries[0][0] + datetime.timedelta(seconds=te.MATCH_SECONDS + 1), 5.0]]
    as_text = [[entries[1][0].isoformat(), 4.5]]
    aware = [[entries[2][0].replace(tzinfo=TZ).astimezone(datetime.UTC), 4.0]]
    assert te.entry_temperatures(entries, pts, TZ) == [10.0, 5.0, 0.0]
    assert te.entry_temperatures(later + as_text + aware + [4.2, ["x", 4.1]], pts, TZ) == [None, 5.0, 0.0, None, None]


# --- the forecast --------------------------------------------------------------------------------------------------


def test_forecast_times_with_an_offset_are_converted_to_local_time() -> None:
    hourly, daily = te.forecast_temperatures(
        {
            "hourly": [{"datetime": "2026-10-23T05:00:00+00:00", "temperature": -3}, {"datetime": "2026-10-23T06:00:00+00:00", "temperature": "x"}],
            "daily": [{"datetime": "2026-10-22T22:00:00+00:00", "temperature": 8, "templow": -4}],
        },
        TZ,
    )
    assert hourly == {datetime.datetime(2026, 10, 23, 7, 0): -3.0}  # CEST, UTC+2
    assert daily == {datetime.date(2026, 10, 23): (-4.0, 8.0)}
    assert te.forecast_temperatures(None, TZ) == ({}, {})


def test_the_drive_temperature_is_the_mean_of_its_hours() -> None:
    hourly = {datetime.datetime(2026, 10, 23, hour): float(hour - 10) for hour in range(7, 16)}
    start, end = datetime.datetime(2026, 10, 23, 7, 0), datetime.datetime(2026, 10, 23, 16, 0)
    assert te.window_temperature(hourly, {}, start, end) == pytest.approx(sum(range(-3, 6)) / 9)


def test_without_enough_hourly_forecast_the_middle_of_the_days_low_and_high_counts() -> None:
    hourly = {datetime.datetime(2026, 10, 23, 7): 20.0}  # 1 of 9 hours
    start, end = datetime.datetime(2026, 10, 23, 7, 0), datetime.datetime(2026, 10, 23, 16, 0)
    assert te.window_temperature(hourly, {datetime.date(2026, 10, 23): (-4.0, 8.0)}, start, end) == 2.0
    assert te.window_temperature({}, {datetime.date(2026, 10, 23): (-4.0, None)}, start, end) == -4.0


def test_a_day_with_only_its_high_is_no_forecast() -> None:
    """The daily `temperature` is the day's high: warmer than the drive, so it would plan too little."""
    start = datetime.datetime(2026, 10, 23, 7, 0)
    assert te.window_temperature({}, {datetime.date(2026, 10, 23): (None, 8.0)}, start, None) is None
    assert te.window_temperature({}, {}, start, start) is None


def test_a_drive_over_the_night_summer_time_ends_is_read_by_its_wall_clock_hours() -> None:
    hourly = {datetime.datetime(2026, 10, 25, hour): -1.0 for hour in range(0, 6)}
    assert te.window_temperature(hourly, {}, datetime.datetime(2026, 10, 25, 1, 30), datetime.datetime(2026, 10, 25, 4, 0)) == -1.0


def test_a_night_shift_is_read_until_the_next_morning() -> None:
    hourly = {datetime.datetime(2026, 10, 23, 22): 10.0, **{datetime.datetime(2026, 10, 24, hour): -5.0 for hour in range(0, 6)}}
    hourly[datetime.datetime(2026, 10, 23, 23)] = -5.0
    temp = te.window_temperature(hourly, {}, datetime.datetime(2026, 10, 23, 22, 0), datetime.datetime(2026, 10, 23, 6, 0))
    assert temp == pytest.approx((10.0 - 5.0 * 7) / 8)


def test_a_forecast_in_three_hour_steps_covers_the_hours_between() -> None:
    hourly = {datetime.datetime(2026, 10, 23, hour): float(hour) for hour in (6, 9, 12, 15)}
    start, end = datetime.datetime(2026, 10, 23, 7, 0), datetime.datetime(2026, 10, 23, 16, 0)
    assert te.window_temperature(hourly, {}, start, end) == pytest.approx((6 * 2 + 9 * 3 + 12 * 3 + 15) / 9)
    assert te.window_temperature({datetime.datetime(2026, 10, 23, 3): 1.0}, {}, start, end) is None  # too old for 07:00


def test_half_of_the_hours_is_enough() -> None:
    hourly = {datetime.datetime(2026, 10, 23, hour): -2.0 for hour in (7, 8)}
    start = datetime.datetime(2026, 10, 23, 7, 0)
    assert te.window_temperature(hourly, {}, start, start + datetime.timedelta(hours=8)) == -2.0  # 07-10 (lookback): 4 of 8 hours
    assert te.window_temperature(hourly, {}, start, start + datetime.timedelta(hours=9)) is None  # 4 of 9


def test_a_days_spread_driving_is_its_daytime_hours() -> None:
    day = datetime.datetime(2026, 10, 23)
    assert te.day_drive_window(day, datetime.datetime(2026, 10, 22, 12)) == (day.replace(hour=6), day.replace(hour=22))
    assert te.day_drive_window(day, day.replace(hour=13, minute=20)) == (day.replace(hour=13, minute=20), day.replace(hour=22))
    assert te.day_drive_window(day, day.replace(hour=23)) == (day.replace(hour=6), day.replace(hour=22))


# --- the plan ------------------------------------------------------------------------------------------------------


WORK = {
    "workplan_charging": True,
    "workday_friday": True,
    "workday_departure_friday": datetime.time(7, 0),
    "workday_homecoming_friday": datetime.time(16, 0),
    "workday_distance_needed_friday": 80.0,
}


def _forecast(temp: float | None) -> dict:
    if temp is None:
        return {"hourly": [], "daily": []}
    return {"hourly": [{"datetime": datetime.datetime.combine(FRIDAY, datetime.time(hour), tzinfo=TZ).isoformat(), "temperature": temp} for hour in range(24)], "daily": []}


def _plan(temps: list[float], forecast_temp: float | None, *, switch: bool = True, entries=None, car=line, settings=WORK):
    """Plan Friday's work (80 km, or the given settings) after drives at these temperatures; returns (engine, result)."""
    engine, _ = run(12, level=20, settings=settings, **PLUGGED_IN)
    drives, learned = history(temps, car)
    engine.analysis_db["drives"] = drives
    engine.km_kwh_efficiency_db = entries if entries is not None else learned[:10]
    snap = snapshot(NOW, level=20, settings={**settings, "temperature_efficiency": switch}, **PLUGGED_IN)
    snap.forecast_dict = _forecast(forecast_temp)
    result = engine.run_job(snap, engine.recalculate)
    assert result.error is None, result.error
    return engine, result


def _friday_level(engine) -> float:
    return engine.charging_plan[1]["work_battery_level_needed"]


WARM_HISTORY = [-5.0, -2.0, 0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0]  # the newest 10 at 10 °C


def test_with_the_switch_off_the_plan_is_the_learned_km_per_kwh() -> None:
    engine, result = _plan(WARM_HISTORY, -5.0, switch=False)
    learned = 80 / (line(10) * BATTERY / 100)
    assert _friday_level(engine) == pytest.approx(learned, abs=0.01)
    assert result.attributes["km_per_kwh"]["temperature_adjustment"] == {"enabled": False, "reason": None, "drives": None, "per_degree": None, "drives_used": []}


def test_a_cold_forecast_raises_the_battery_level_the_work_day_needs() -> None:
    engine, result = _plan(WARM_HISTORY, -5.0)
    assert _friday_level(engine) == pytest.approx(80 / (line(-5) * BATTERY / 100), abs=0.05)
    adjustment = result.attributes["km_per_kwh"]["temperature_adjustment"]
    assert adjustment["enabled"] is True and adjustment["reason"] is None and adjustment["drives"] == len(WARM_HISTORY)
    friday = next(d for d in adjustment["drives_used"] if d["start"].startswith(f"{FRIDAY}T07:00"))
    assert friday == {"start": f"{FRIDAY}T07:00:00", "end": f"{FRIDAY}T16:00:00", "temp": -5.0, "learned": 5.0, "km_per_kwh": 3.5, "reason": None}


def test_a_warm_forecast_never_lowers_the_charging() -> None:
    off, _ = _plan(WARM_HISTORY, 25.0, switch=False)
    on, _ = _plan(WARM_HISTORY, 25.0)
    assert _friday_level(on) == _friday_level(off)


def test_the_adjusted_km_per_kwh_is_never_below_half_the_learned() -> None:
    steep = lambda temp: 2.2 + 0.2 * temp  # noqa: E731  # 0.2 km/kWh at -10 °C, 4.2 at 10 °C
    engine, result = _plan(WARM_HISTORY, -15.0, car=steep)
    assert _friday_level(engine) == pytest.approx(80 / (steep(10) * 0.5 * BATTERY / 100), abs=0.05)
    friday = next(d for d in result.attributes["km_per_kwh"]["temperature_adjustment"]["drives_used"] if d["start"].startswith(f"{FRIDAY}T07:00"))
    assert friday["km_per_kwh"] == pytest.approx(steep(10) * 0.5, abs=0.01)


def test_without_a_forecast_the_plan_keeps_the_learned_km_per_kwh() -> None:
    off, _ = _plan(WARM_HISTORY, None, switch=False)
    on, result = _plan(WARM_HISTORY, None)
    assert _friday_level(on) == _friday_level(off)
    friday = next(d for d in result.attributes["km_per_kwh"]["temperature_adjustment"]["drives_used"] if d["start"].startswith(f"{FRIDAY}T07:00"))
    assert (friday["km_per_kwh"], friday["reason"]) == (None, te.REASON_NO_FORECAST)


def test_too_few_drives_keep_the_learned_km_per_kwh_and_say_why() -> None:
    off, _ = _plan(WARM_HISTORY[-11:], -5.0, switch=False)
    on, result = _plan(WARM_HISTORY[-11:], -5.0)
    assert _friday_level(on) == _friday_level(off)
    assert result.attributes["km_per_kwh"]["temperature_adjustment"]["reason"] == te.REASON_TOO_FEW_DRIVES


def test_learned_drives_without_a_temperature_keep_the_learned_km_per_kwh() -> None:
    old = [5.0] * 10  # entries from before the times were stored
    off, _ = _plan(WARM_HISTORY, -5.0, switch=False, entries=old)
    on, result = _plan(WARM_HISTORY, -5.0, entries=old)
    assert _friday_level(on) == _friday_level(off)
    friday = next(d for d in result.attributes["km_per_kwh"]["temperature_adjustment"]["drives_used"] if d["start"].startswith(f"{FRIDAY}T07:00"))
    assert friday["reason"] == te.REASON_TOO_FEW_MATCHED


def test_an_error_in_the_adjustment_keeps_the_learned_km_per_kwh(monkeypatch: pytest.MonkeyPatch) -> None:
    off, _ = _plan(WARM_HISTORY, -5.0, switch=False)

    def broken(*_args, **_kwargs):
        raise ValueError("broken forecast")

    monkeypatch.setattr(te, "window_temperature", broken)
    on, result = _plan(WARM_HISTORY, -5.0)
    assert _friday_level(on) == _friday_level(off)
    friday = next(d for d in result.attributes["km_per_kwh"]["temperature_adjustment"]["drives_used"] if d["start"].startswith(f"{FRIDAY}T07:00"))
    assert (friday["km_per_kwh"], friday["reason"]) == (None, te.REASON_FAILED)


def test_a_night_shift_reads_the_forecast_until_the_next_morning() -> None:
    night = {**WORK, "workday_departure_friday": datetime.time(22, 0), "workday_homecoming_friday": datetime.time(6, 0)}
    engine, _ = run(12, level=20, settings=night, **PLUGGED_IN)
    drives, learned = history(WARM_HISTORY)
    engine.analysis_db["drives"] = drives
    engine.km_kwh_efficiency_db = learned[:10]
    snap = snapshot(NOW, level=20, settings={**night, "temperature_efficiency": True}, **PLUGGED_IN)
    hours = [datetime.datetime.combine(FRIDAY, datetime.time(0)) + datetime.timedelta(hours=h) for h in range(48)]
    snap.forecast_dict = {"hourly": [{"datetime": h.replace(tzinfo=TZ).isoformat(), "temperature": 10.0 if h.hour == 22 and h.date() == FRIDAY else -5.0} for h in hours], "daily": []}
    result = engine.run_job(snap, engine.recalculate)
    assert result.error is None, result.error
    friday = next(d for d in result.attributes["km_per_kwh"]["temperature_adjustment"]["drives_used"] if d["start"].startswith(f"{FRIDAY}T22:00"))
    assert friday["end"] == f"{FRIDAY + datetime.timedelta(days=1)}T06:00:00"
    assert friday["temp"] == pytest.approx((10.0 - 5.0 * 7) / 8, abs=0.05)
    assert friday["km_per_kwh"] < friday["learned"]


def _used(result, start: str) -> dict:
    return next(d for d in result.attributes["km_per_kwh"]["temperature_adjustment"]["drives_used"] if d["start"] == start)


FILL_UP = {"fill_up": True, "typical_daily_distance": 40.0}


def test_optimal_weekly_charging_plans_the_day_at_its_daytime_temperature() -> None:
    off, off_result = _plan(WARM_HISTORY, -5.0, switch=False, settings=FILL_UP)
    on, result = _plan(WARM_HISTORY, -5.0, settings=FILL_UP)
    assert off.charging_plan[1]["offday_battery_level_needed"] == pytest.approx(40 / (line(10) * BATTERY / 100), abs=0.01)
    assert on.charging_plan[1]["offday_battery_level_needed"] == pytest.approx(40 / (line(-5) * BATTERY / 100), abs=0.01)
    assert _used(result, f"{FRIDAY}T06:00:00") == {"start": f"{FRIDAY}T06:00:00", "end": f"{FRIDAY}T22:00:00", "temp": -5.0, "learned": 5.0, "km_per_kwh": 3.5, "reason": None}
    # the chart spreads the same km over the whole day; only the battery % of them follows the temperature
    friday = [d for d in result.attributes["current_charging_rule"]["distance_forecast"] if d["time"].startswith(f"{FRIDAY}T00:00")]
    assert friday == [d for d in off_result.attributes["current_charging_rule"]["distance_forecast"] if d["time"].startswith(f"{FRIDAY}T00:00")]
    assert friday[0]["km"] == 40.0


TRIP = {
    "trip_date_time": datetime.datetime.combine(FRIDAY, datetime.time(9)),
    "trip_homecoming_date_time": datetime.datetime.combine(FRIDAY, datetime.time(18)),
    "trip_range_needed": 100.0,
}


def test_a_trip_in_km_is_planned_at_its_forecast_temperature() -> None:
    off, _ = _plan(WARM_HISTORY, -5.0, switch=False, settings=TRIP)
    on, result = _plan(WARM_HISTORY, -5.0, settings=TRIP)
    extra = 100 / (line(-5) * BATTERY / 100) - 100 / (line(10) * BATTERY / 100)  # each rounded up to a whole %
    assert on.charging_plan[1]["trip_battery_level_needed"] - off.charging_plan[1]["trip_battery_level_needed"] == pytest.approx(extra, abs=1.0)
    assert _used(result, f"{FRIDAY}T09:00:00")["end"] == f"{FRIDAY}T18:00:00"
    assert _used(result, f"{FRIDAY}T09:00:00")["km_per_kwh"] == 3.5


def test_a_trip_without_homecoming_is_read_until_the_end_of_its_day() -> None:
    """A trip is only planned with a homecoming (public_charging.py); the window still has an end without one."""
    from custom_components.cable_juice_planner.engine.planner.charge_plan import ChargePlan  # noqa: PLC0415

    goto = datetime.datetime.combine(FRIDAY, datetime.time(9))
    assert ChargePlan.trip_drive_window({"trip_goto": goto, "trip_homecoming": None}) == (goto, datetime.datetime.combine(FRIDAY, datetime.time(23, 59, 59)))


def test_the_measurements_and_the_range_keep_the_learned_km_per_kwh() -> None:
    engine, _ = _plan(WARM_HISTORY, -5.0)
    snap = snapshot(NOW, level=20, settings={**WORK, "temperature_efficiency": True}, **PLUGGED_IN)
    snap.forecast_dict = _forecast(-5.0)
    holder = {}

    def read() -> None:
        holder["plain"] = engine.distance_per_percentage()
        holder["planned"] = engine.distance_per_percentage_for(datetime.datetime.combine(FRIDAY, datetime.time(7)), datetime.datetime.combine(FRIDAY, datetime.time(16)))

    assert engine.run_job(snap, read).error is None
    assert holder["plain"] == pytest.approx(line(10) * BATTERY / 100, abs=0.01)
    assert holder["planned"] == pytest.approx(line(-5) * BATTERY / 100, abs=0.01)


def test_switching_it_on_or_off_replans() -> None:
    engine, _ = _plan(WARM_HISTORY, -5.0)
    hashes = {}
    for state in (True, False):
        snap = snapshot(NOW, level=20, settings={**WORK, "temperature_efficiency": state}, **PLUGGED_IN)

        def read(state=state) -> None:
            hashes[state] = engine.make_hashable_snapshot(engine.get_hash_dict()["SETTINGS"])

        assert engine.run_job(snap, read).error is None
    assert hashes[True] != hashes[False]


def test_only_the_planner_reads_the_temperature_adjusted_km_per_kwh() -> None:
    """The measurements (drive efficiency, the km/kWh database, the estimated range), the startup info and the What-if
    keep the learned km/kWh; a new reader of the adjusted one has to be added here on purpose."""
    engine_dir = Path(__file__).resolve().parent.parent / "custom_components" / "cable_juice_planner" / "engine"
    pattern = re.compile(r"distance_per_percentage_for\(|calc_\w+_for\(|temperature_km_kwh\(|\bdrive=")
    readers = sorted(path.relative_to(engine_dir).as_posix() for path in engine_dir.rglob("*.py") if pattern.search(path.read_text(encoding="utf-8")))
    assert readers == ["battery.py", "planner/charge_plan.py", "planner/future/alternate.py", "planner/planner.py"]


# --- the dashboard -------------------------------------------------------------------------------------------------


def _plan_card(language: str = "en") -> str:
    import yaml  # noqa: PLC0415

    from custom_components.cable_juice_planner import const  # noqa: PLC0415
    from custom_components.cable_juice_planner.management.dashboard_build import render_dashboard  # noqa: PLC0415

    from .test_dashboard import _own  # noqa: PLC0415
    from .test_engine import OPTIONS  # noqa: PLC0415

    text = render_dashboard(const.build_config({**OPTIONS, "language": language}), "tesla", "entry1", _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"})
    assert "@@" not in text, language
    found = [card["content"] for card in _all_cards(yaml.safe_load(text)) if card.get("type") == "markdown" and "temperature_adjustment" in card.get("content", "")]
    assert len(found) == 1, language
    return found[0]


def _all_cards(node):
    if isinstance(node, dict):
        if "type" in node:
            yield node
        for value in node.values():
            yield from _all_cards(value)
    elif isinstance(node, list):
        for value in node:
            yield from _all_cards(value)


def _render_plan(card: str, switch: str, adjustment: dict | None) -> str:
    from types import SimpleNamespace  # noqa: PLC0415

    from jinja2 import Environment  # noqa: PLC0415

    states = SimpleNamespace(sensor=SimpleNamespace(cjp_tesla_analysis=SimpleNamespace(attributes={"temperature_fit": None, "drives": []})))
    return Environment().from_string(card).render(
        states=states,
        is_state=lambda entity, value: entity == "switch.cjp_tesla_temperature_efficiency" and switch == value,
        state_attr=lambda entity, attribute: adjustment if (entity, attribute) == ("sensor.cjp_tesla_km_per_kwh", "temperature_adjustment") else None,
    )


def test_the_dashboard_shows_what_the_plan_used_for_each_drive() -> None:
    card = _plan_card()
    used = {
        "enabled": True, "reason": None, "drives": 18, "per_degree": 0.1,
        "drives_used": [
            {"start": "2026-10-23T07:00:00", "end": "2026-10-23T16:00:00", "temp": -5.0, "learned": 5.0, "km_per_kwh": 3.5, "reason": None},
            {"start": "2026-10-24T06:00:00", "end": "2026-10-24T22:00:00", "temp": None, "learned": 5.0, "km_per_kwh": None, "reason": "no_forecast"},
        ],
    }
    text = _render_plan(card, "on", used)
    assert "| 23/10 07:00–16:00 | -5.0 °C | 5.0 | **3.5** |" in text
    assert "| 24/10 06:00–22:00 | – | 5.0 | 5.0 (" in text
    trip = {**used, "drives_used": [{**used["drives_used"][0], "end": "2026-10-25T18:00:00"}]}
    assert "| 23/10 07:00–25/10 18:00 |" in _render_plan(card, "on", trip)
    midnight = {**used, "drives_used": [{**used["drives_used"][0], "end": "2026-10-25T00:00:00"}]}
    assert "| 23/10 07:00–25/10 00:00 |" in _render_plan(card, "on", midnight)
    none = _render_plan(card, "on", {**used, "drives_used": []})
    assert "no planned drives" in none and "| 23/10" not in none
    assert "no_forecast" not in text  # the reason is shown in the user's language
    waiting = _render_plan(card, "on", {**used, "reason": "too_few_drives", "drives": 7, "drives_used": []})
    assert "(7)" in waiting and "too_few_drives" not in waiting
    assert "| 23/10" not in _render_plan(card, "off", used)


@pytest.mark.parametrize("language", ["da", "de", "en", "en-GB", "pl", "uk"])
def test_every_language_renders_the_plan_card(language: str) -> None:
    card = _plan_card(language)
    for reason in ("too_few_drives", "too_narrow", "no_cold_effect", "implausible", "no_forecast", "too_few_matched", "no_learned", "failed"):
        text = _render_plan(card, "on", {"enabled": True, "reason": reason, "drives": 3, "drives_used": []})
        assert reason not in text, (language, reason)
