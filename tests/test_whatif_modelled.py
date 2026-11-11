"""What-if connections modelled from the work plan, and the work plan as a bound for the estimated connections."""
from __future__ import annotations

import datetime
import zoneinfo

import pytest

from custom_components.cable_juice_planner.engine.whatif.derive import derive_windows
from custom_components.cable_juice_planner.engine.whatif.inputs import build_replay_input
from custom_components.cable_juice_planner.engine.whatif.modelled import gap_use, model_connections, model_windows, percent_per_km
from custom_components.cable_juice_planner.engine.whatif.replay import replay

TZ = zoneinfo.ZoneInfo("Europe/Copenhagen")
HOUR = 3600.0
MONDAY = datetime.date(2026, 3, 2)  # a Monday, before the clocks change


def local(date: datetime.date, hour: float) -> float:
    return datetime.datetime.combine(date, datetime.time(int(hour), int(hour % 1 * 60)), tzinfo=TZ).timestamp()


def plan(departure: float = 7.0, homecoming: float = 16.0, workdays=(0, 1, 2, 3, 4), km: float = 30.0, day_off_km: float = 10.0) -> dict:
    return {"days": {
        weekday: {"workday": weekday in workdays, "departure": int(departure * 3600), "homecoming": int(homecoming * 3600), "km": km if weekday in workdays else day_off_km}
        for weekday in range(7)
    }}


def week(first_monday: datetime.date, weeks: int, home: float = 16.5, leave: float = 6.8):
    """What CJP measured: home on the workdays in the evening until the next workday's departure (the weekend in one go),
    and a recorded drive of 9 % (30 km) at every workday's departure."""
    windows, drives = [], []
    for w in range(weeks):
        monday = first_monday + datetime.timedelta(days=7 * w)
        for d in range(5):
            date = monday + datetime.timedelta(days=d)
            nxt = date + datetime.timedelta(days=1 if d < 4 else 3)
            windows.append((local(date, home), local(nxt, leave)))
            drives.append({"start": local(nxt, 7.0), "end": local(nxt, 8.0), "km": 30.0, "used": 9.0})
    return windows, drives


def test_the_plan_gives_a_connection_per_workday_evening_and_one_for_the_weekend_cut_to_the_long_typical_length() -> None:
    windows = model_windows(plan(), TZ, local(MONDAY, 0), local(MONDAY + datetime.timedelta(days=14), 0), 15 * HOUR)
    starts = [datetime.datetime.fromtimestamp(s, TZ) for s, _ in windows]
    ends = [datetime.datetime.fromtimestamp(e, TZ) for _, e in windows]
    assert [(s.weekday(), s.hour) for s in starts[:5]] == [(0, 16), (1, 16), (2, 16), (3, 16), (6, 16)]  # Friday's: only the last 15 h of the weekend
    assert all(e.hour == 7 for e in ends) and [e.weekday() for e in ends[:5]] == [1, 2, 3, 4, 0]
    # nothing that is not completely inside the region
    assert all(local(MONDAY, 0) <= s and e <= local(MONDAY + datetime.timedelta(days=14), 0) for s, e in windows)
    # a plan without a workday has no connections
    assert model_windows(plan(workdays=()), TZ, local(MONDAY, 0), local(MONDAY + datetime.timedelta(days=14), 0), 15 * HOUR) == []


def test_the_battery_use_between_connections_is_the_distance_of_the_days_in_the_gap() -> None:
    per_km = 0.3
    # a workday leaves at 07:00: its distance is in the gap from 06:50 to 16:30
    assert gap_use(plan(), TZ, local(MONDAY, 6.8), local(MONDAY, 16.5), per_km) == pytest.approx(9.0)
    # Friday's departure, Saturday and Sunday (a day off counts when its noon is in the gap) until the weekend connection
    friday = MONDAY + datetime.timedelta(days=4)
    assert gap_use(plan(), TZ, local(friday, 6.8), local(friday + datetime.timedelta(days=2), 16.0), per_km) == pytest.approx((30 + 10 + 10) * per_km)
    assert gap_use(plan(), TZ, local(MONDAY, 17.0), local(MONDAY, 18.0), per_km) == 0.0


def test_the_percent_per_km_comes_from_the_recorded_drives_and_needs_enough_of_them() -> None:
    _, drives = week(MONDAY, 3)
    assert percent_per_km(drives) == pytest.approx(0.3)
    assert percent_per_km(drives[:5]) is None
    assert percent_per_km([{"km": 0.0, "used": 5.0}] * 20 + [{"km": float("nan"), "used": 5.0}] * 5) is None


def test_the_model_is_checked_against_the_measured_connections_before_it_is_used() -> None:
    measured, drives = week(MONDAY + datetime.timedelta(days=28), 6)
    before = local(MONDAY, 0)
    ok = model_connections(plan(), TZ, measured, drives, before, measured[0][0], measured[0][0])
    assert ok["status"] == "ok" and ok["validation"]["passed"] and ok["validation"]["use_ratio"] == pytest.approx(1.0, abs=0.1)
    assert len(ok["windows"]) >= 18  # four weeks of workday evenings (the Friday one is the weekend)
    assert all(window[1] <= measured[0][0] for window in ok["windows"])
    assert {round(u, 1) for _, u in ok["drives"]} <= {9.0, 15.0}
    # a plan that says something else than what was measured is not used (the car leaves at noon, not at 07:00)
    wrong = model_connections(plan(departure=12.0), TZ, measured, drives, before, measured[0][0], measured[0][0])
    assert wrong["status"] == "unavailable" and wrong["reason"] == "not_validated" and wrong["windows"] == []
    # the battery use that does not match the recorded drives is not used either
    heavy = model_connections(plan(km=90.0), TZ, measured, drives, before, measured[0][0], measured[0][0])
    assert heavy["reason"] == "not_validated" and heavy["validation"]["use_ratio"] > 1.6


def test_nothing_is_modelled_without_a_plan_enough_measured_connections_or_recorded_drives() -> None:
    measured, drives = week(MONDAY + datetime.timedelta(days=28), 6)
    start = local(MONDAY, 0)
    assert model_connections(None, TZ, measured, drives, start, measured[0][0], None)["reason"] == "no_plan"
    assert model_connections(plan(workdays=()), TZ, measured, drives, start, measured[0][0], None)["reason"] == "no_workdays"
    assert model_connections(plan(), TZ, measured[:5], drives, start, measured[0][0], None)["reason"] == "too_few_measured"
    assert model_connections(plan(), TZ, measured, drives[:5], start, measured[0][0], None)["reason"] == "too_few_drives"


# --------------------------------------------------------------------------- in the replay input


def build(windows, drives, **overrides):
    first = min(w[0] for w in windows)
    base = first - first % HOUR - 40 * 24 * HOUR
    kwargs = dict(
        windows=[{"start": s, "end": e} for s, e in windows],
        prices={base + i * HOUR: 1.0 + (i % 24) / 24 for i in range(40 * 24 + 60 * 24)},
        drives=drives, public_sessions=[],
        period_start=first - 21 * 24 * HOUR, period_end=max(w[1] for w in windows) + HOUR,
        battery_kwh=60.0, max_kw=10.0, phases=3, start_soc=None, target_soc=80.0,
        loss_factor=lambda kw: 0.1, power_cap=lambda p, a, b, kw: kw,
        prices_averaged_since=first - 100 * 24 * HOUR, start_soc_source="unavailable", tz=TZ,
        work_plan=plan(),
    )
    kwargs.update(overrides)
    return build_replay_input(**kwargs)


def test_the_time_before_the_first_record_is_modelled_from_the_plan_and_labelled() -> None:
    windows, drives = week(MONDAY + datetime.timedelta(days=28), 6)
    data, coverage = build(windows, drives)
    modelled = [w for w in data.windows if w.modelled]
    assert modelled and all(w.derived for w in modelled) and not any(w.modelled for w in data.windows[len(modelled):])
    assert coverage["windows_modelled"] == len(modelled) and coverage["modelled_status"] == "ok" and coverage["modelled_drives"] > 0
    assert "connections_modelled_from_work_plan" in data.warnings
    assert data.provenance["windows"] == "measured_and_modelled_from_work_plan"
    # the level at the first modelled connection is unknown: the entered one (here none) is what the replay gets, never one made up
    assert data.start_soc is None and data.provenance["start_soc"] == "unavailable"
    result = replay(data)
    assert result.status == "ok", result.reason
    assert result.calibrated_start
    assert [w.modelled for w in result.smart.windows][: len(modelled)] == [True] * len(modelled)


def test_a_model_that_does_not_match_the_measured_connections_leaves_the_measured_ones_alone() -> None:
    windows, drives = week(MONDAY + datetime.timedelta(days=28), 6)
    data, coverage = build(windows, drives, work_plan=plan(departure=12.0))
    assert not any(w.derived for w in data.windows) and coverage["modelled_reason"] == "not_validated"
    assert "work_plan_model_not_validated" in data.warnings and "connections_modelled_from_work_plan" not in data.warnings
    plain, plain_coverage = build(windows, drives, work_plan=None)
    assert [(w.start, w.end) for w in plain.windows] == [(w.start, w.end) for w in data.windows] and plain_coverage["modelled_status"] is None


def test_the_plan_cuts_the_estimated_connections_to_the_planned_homecoming_and_departure() -> None:
    from .test_whatif_derive import at, history, measured as measured_days  # noqa: PLC0415

    utc = datetime.UTC
    every_day = plan(departure=6.5, homecoming=17.5, workdays=range(7))
    rows = history(0, 40)
    bounded = derive_windows(rows, measured_days(20, 40), every_day, utc)
    free = derive_windows(rows, measured_days(20, 40))
    assert free["status"] == "ok" and free["plan_bounds"] is False and free["windows"][0]["start"] == at(0, 17)
    # the statistics say 17:00-07:00; the plan says home at 17:30 and gone at 06:30: the connection is cut to the plan
    assert bounded["status"] == "ok" and bounded["plan_bounds"] is True
    assert (bounded["windows"][0]["start"], bounded["windows"][0]["end"]) == (at(0, 17.5), at(1, 6.5))
    # levels still come from the hours of the statistics, and the first hour of the statistics is reported
    assert bounded["windows"][0]["soc_in"] == 60.0 and bounded["statistics_from"] == min(rows)
    # a plan that would cut off the observed charging (home at 02:00) does not: the connection is left as observed
    late = derive_windows(rows, measured_days(20, 40), plan(departure=6.5, homecoming=2.0, workdays=range(7)), utc)
    assert late["windows"][0]["start"] == at(0, 17)
