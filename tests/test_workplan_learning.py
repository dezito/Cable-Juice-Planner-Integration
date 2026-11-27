"""Learned departure and homecoming per weekday (engine/workplan.py)."""
from __future__ import annotations

import datetime
import re
import zoneinfo

import pytest

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.engine import CJPEngine
from custom_components.cable_juice_planner.engine.workplan import (
    LEARN_MIN_AWAY_MINUTES,
    learned_times,
    window_samples,
)

from .test_engine import DAY, OPTIONS, PLUGGED_IN, TEXTS, TZ, run, snapshot

COPENHAGEN = zoneinfo.ZoneInfo("Europe/Copenhagen")
FRIDAYS = [DAY + datetime.timedelta(days=1) - datetime.timedelta(weeks=weeks) for weeks in range(1, 7)]  # before tomorrow's Friday
ALL_WORKDAYS = {0, 1, 2, 3, 4}


def ts(day: datetime.date, hour: int, minute: int = 0, tz: datetime.tzinfo = TZ) -> float:
    return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=tz).timestamp()


def chain(days: list[tuple[datetime.date, tuple[int, int], tuple[int, int]]], tz: datetime.tzinfo = TZ) -> list[dict]:
    """Connection windows of a car that leaves and comes back on each of these days: (day, departure, homecoming)."""
    windows = []
    start = ts(days[0][0] - datetime.timedelta(days=1), 18, 0, tz)
    for day, departure, homecoming in days:
        windows.append({"start": start, "end": ts(day, *departure, tz=tz)})
        start = ts(day, *homecoming, tz=tz)
    windows.append({"start": start, "end": None})
    return windows


def minutes(hour: int, minute: int = 0) -> int:
    return hour * 60 + minute


# --- the samples ----------------------------------------------------------------------------------------------------


def test_every_workday_with_a_return_the_same_day_is_a_sample() -> None:
    days = [(DAY - datetime.timedelta(days=offset), (7, 5), (16, 20)) for offset in range(1, 15) if (DAY - datetime.timedelta(days=offset)).weekday() < 5]
    samples = window_samples(chain(sorted(days)), TZ, DAY, ALL_WORKDAYS, set())
    assert sorted(samples) == [0, 1, 2, 3, 4]
    assert all(row == (minutes(7, 5), minutes(16, 20)) for rows in samples.values() for row in rows)
    assert sum(len(rows) for rows in samples.values()) == len(days)


def test_only_unplugs_between_04_and_12_are_a_departure() -> None:
    day = DAY - datetime.timedelta(days=1)  # a Wednesday
    for hour, minute, counted in ((3, 59, False), (4, 0, True), (11, 59, True), (12, 0, False), (13, 30, False)):
        windows = chain([(day, (hour, minute), (min(hour + 3, 23), minute))])
        assert bool(window_samples(windows, TZ, DAY, ALL_WORKDAYS, set())) is counted, (hour, minute)


def test_only_workdays_of_the_work_plan_count() -> None:
    windows = chain([(DAY - datetime.timedelta(days=1), (7, 0), (16, 0))])  # a Wednesday
    assert window_samples(windows, TZ, DAY, {2}, set())
    assert not window_samples(windows, TZ, DAY, {0, 1, 3, 4}, set())  # Wednesday is no workday


def test_a_day_the_car_did_not_come_back_the_same_day_is_no_sample() -> None:
    day = DAY - datetime.timedelta(days=1)
    next_day = chain([(day, (7, 0), (16, 0))])
    next_day[1]["start"] = ts(day + datetime.timedelta(days=1), 8, 0)  # back the next morning
    assert not window_samples(next_day, TZ, DAY, ALL_WORKDAYS, set())
    still_away = [{"start": ts(day - datetime.timedelta(days=1), 18), "end": ts(day, 7, 0)}]  # no plug-in after it yet
    assert not window_samples(still_away, TZ, DAY, ALL_WORKDAYS, set())


def test_a_re_plug_within_the_minimum_time_away_is_no_departure_and_does_not_fall_through_to_a_later_unplug() -> None:
    """The first unplug of the day is the departure. 05:00 and plugged in again at 05:10 is a re-plug (new rule: under
    LEARN_MIN_AWAY_MINUTES), and the real departure at 07:00 does not replace it."""
    day = DAY - datetime.timedelta(days=1)
    windows = [
        {"start": ts(day - datetime.timedelta(days=1), 18), "end": ts(day, 5, 0)},
        {"start": ts(day, 5, 10), "end": ts(day, 7, 0)},
        {"start": ts(day, 16, 0), "end": None},
    ]
    assert not window_samples(windows, TZ, DAY, ALL_WORKDAYS, set())
    away = LEARN_MIN_AWAY_MINUTES
    exactly = chain([(day, (7, 0), (7, away))])
    assert window_samples(exactly, TZ, DAY, ALL_WORKDAYS, set())
    shorter = chain([(day, (7, 0), (7, away - 1))])
    assert not window_samples(shorter, TZ, DAY, ALL_WORKDAYS, set())


def test_days_of_a_trip_are_not_counted() -> None:
    day = DAY - datetime.timedelta(days=1)
    windows = chain([(day, (7, 0), (16, 0))])
    assert window_samples(windows, TZ, DAY, ALL_WORKDAYS, set())
    assert not window_samples(windows, TZ, DAY, ALL_WORKDAYS, {day})


def test_only_the_last_eight_weeks_and_never_today_count() -> None:
    inside = DAY - datetime.timedelta(weeks=8)  # exactly 8 weeks ago: a Thursday
    outside = DAY - datetime.timedelta(weeks=8, days=1)
    today = DAY
    windows = chain([(outside, (7, 0), (16, 0)), (inside, (7, 0), (16, 0)), (today, (7, 0), (16, 0))])
    samples = window_samples(windows, TZ, DAY, ALL_WORKDAYS, set())
    assert {weekday for weekday in samples} == {inside.weekday()}
    assert sum(len(rows) for rows in samples.values()) == 1


def test_only_measured_windows_with_numbers_are_read() -> None:
    day = DAY - datetime.timedelta(days=1)
    windows = [*chain([(day, (7, 0), (16, 0))]), {"start": "x", "end": 1.0}, {"end": 5.0}, "broken", None]
    assert window_samples(windows, TZ, DAY, ALL_WORKDAYS, set())


def test_the_local_wall_clock_counts_across_a_change_of_summer_time() -> None:
    """07:00 local on both sides of the change to summer time (2026-03-29) is 07:00 in the samples, not 06:00 or 08:00."""
    mondays = [datetime.date(2026, 3, 16), datetime.date(2026, 3, 23), datetime.date(2026, 3, 30), datetime.date(2026, 4, 6)]
    windows = chain([(monday, (7, 0), (16, 0)) for monday in mondays], COPENHAGEN)
    samples = window_samples(windows, COPENHAGEN, datetime.date(2026, 4, 20), ALL_WORKDAYS, set())
    assert samples == {0: [(minutes(7), minutes(16))] * 4}
    # and the autumn change (2026-10-25)
    autumn = chain([(datetime.date(2026, 10, 19), (7, 0), (16, 0)), (datetime.date(2026, 10, 26), (7, 0), (16, 0))], COPENHAGEN)
    assert window_samples(autumn, COPENHAGEN, datetime.date(2026, 11, 2), ALL_WORKDAYS, set()) == {0: [(minutes(7), minutes(16))] * 2}


# --- the learned times ----------------------------------------------------------------------------------------------


def test_the_departure_is_the_25th_percentile_rounded_down_and_the_homecoming_the_75th_rounded_up() -> None:
    samples = {4: [(minutes(7, 5), minutes(16, 10)), (minutes(7, 20), minutes(16, 30)), (minutes(7, 40), minutes(16, 50)), (minutes(8), minutes(17, 20))]}
    friday = learned_times(samples)["friday"]
    # 25 %: 7:05 + 0.75 x 15 min = 7:16.25 -> 7:15; 75 %: 16:50 + 0.25 x 30 min = 16:57.5 -> 17:00
    assert friday == {"departure": datetime.time(7, 15), "homecoming": datetime.time(17, 0), "samples": 4}


@pytest.mark.parametrize(
    ("departures", "expected"),
    [([minutes(7, 15)] * 3, datetime.time(7, 15)), ([minutes(7, 16)] * 3, datetime.time(7, 15)), ([minutes(7, 14)] * 3, datetime.time(7, 0))],
)
def test_the_departure_is_rounded_down_to_15_minutes(departures, expected) -> None:
    assert learned_times({0: [(minute, minutes(16)) for minute in departures]})["monday"]["departure"] == expected


@pytest.mark.parametrize(
    ("homecomings", "expected"),
    [([minutes(16, 15)] * 3, datetime.time(16, 15)), ([minutes(16, 16)] * 3, datetime.time(16, 30)), ([minutes(16, 1)] * 3, datetime.time(16, 15))],
)
def test_the_homecoming_is_rounded_up_to_15_minutes(homecomings, expected) -> None:
    assert learned_times({0: [(minutes(7), minute) for minute in homecomings]})["monday"]["homecoming"] == expected


def test_a_homecoming_rounded_up_past_midnight_stays_in_the_day() -> None:
    assert learned_times({0: [(minutes(7), minutes(23, 50))] * 3})["monday"]["homecoming"] == datetime.time(23, 59)


def test_one_long_day_does_not_move_the_percentiles_much() -> None:
    samples = {0: [(minutes(7), minutes(16))] * 7 + [(minutes(11, 30), minutes(20))]}
    monday = learned_times(samples)["monday"]
    assert (monday["departure"], monday["homecoming"]) == (datetime.time(7, 0), datetime.time(16, 0))


# --- the times the plan uses ----------------------------------------------------------------------------------------


def _engine(windows: list[dict], settings: dict | None = None, **kwargs):
    base = {
        "workplan_charging": True,
        "workday_friday": True,
        "workday_departure_friday": datetime.time(7, 0),
        "workday_homecoming_friday": datetime.time(16, 0),
        **(settings or {}),
    }
    engine, _ = run(12, level=20, settings=base, **PLUGGED_IN, **kwargs)
    engine.economy_db["windows"] = windows
    return engine, base


def _friday_windows(departure: tuple[int, int], homecoming: tuple[int, int], count: int = 3) -> list[dict]:
    return chain([(friday, departure, homecoming) for friday in sorted(FRIDAYS[:count])])


def _effective(engine: CJPEngine, settings: dict, day: str = "friday"):
    now = datetime.datetime.combine(DAY, datetime.time(12, 30))
    holder = {}

    def read() -> None:
        holder["times"] = engine.effective_workday_times(day)

    assert engine.run_job(snapshot(now, level=20, settings=settings, **PLUGGED_IN), read).error is None
    return holder["times"]


def _learned(engine: CJPEngine, settings: dict) -> dict:
    """What the engine has learned, read inside a job like in operation."""
    now = datetime.datetime.combine(DAY, datetime.time(12, 30))
    holder = {}

    def read() -> None:
        holder["learned"] = engine.learned_workplan()

    assert engine.run_job(snapshot(now, level=20, settings=settings, **PLUGGED_IN), read).error is None
    return holder["learned"]


def test_with_learning_on_the_earlier_departure_and_the_later_homecoming_are_used() -> None:
    engine, settings = _engine(_friday_windows((6, 40), (17, 10)))
    assert _effective(engine, {**settings, "learn_departure_time": True}) == (datetime.time(6, 30), datetime.time(17, 15))


def test_a_learned_time_never_moves_the_plan_later_than_entered() -> None:
    engine, settings = _engine(_friday_windows((7, 45), (15, 20)))  # learned later departure, earlier homecoming
    assert _effective(engine, {**settings, "learn_departure_time": True}) == (datetime.time(7, 0), datetime.time(16, 0))


def test_one_side_can_be_learned_while_the_other_stays_entered() -> None:
    engine, settings = _engine(_friday_windows((6, 40), (15, 20)))
    assert _effective(engine, {**settings, "learn_departure_time": True}) == (datetime.time(6, 30), datetime.time(16, 0))


def test_with_learning_off_the_entered_times_are_used_whatever_was_learned() -> None:
    engine, settings = _engine(_friday_windows((6, 40), (17, 10)))
    assert _effective(engine, {**settings, "learn_departure_time": False}) == (datetime.time(7, 0), datetime.time(16, 0))
    assert _learned(engine, settings)["friday"]["samples"] == 3  # but it is still learned, as a suggestion


def test_fewer_than_three_days_of_the_weekday_are_not_used() -> None:
    engine, settings = _engine(_friday_windows((6, 40), (17, 10), count=2))
    assert _effective(engine, {**settings, "learn_departure_time": True}) == (datetime.time(7, 0), datetime.time(16, 0))
    assert _learned(engine, settings)["friday"]["samples"] == 2


def test_a_weekday_that_is_not_a_workday_learns_nothing() -> None:
    engine, settings = _engine(_friday_windows((6, 40), (17, 10)), {"workday_friday": False})
    assert _effective(engine, {**settings, "learn_departure_time": True}, "friday") == (datetime.time(7, 0), datetime.time(16, 0))
    assert "friday" not in _learned(engine, settings)


def test_a_night_shift_keeps_its_entered_times() -> None:
    engine, settings = _engine(
        _friday_windows((6, 40), (17, 10)),
        {"workday_departure_friday": datetime.time(22, 0), "workday_homecoming_friday": datetime.time(6, 0)},
    )
    assert _effective(engine, {**settings, "learn_departure_time": True}) == (datetime.time(22, 0), datetime.time(6, 0))


def test_a_trip_in_the_settings_takes_its_days_out() -> None:
    engine, settings = _engine(_friday_windows((6, 40), (17, 10)))
    trip_day = FRIDAYS[0]
    trip = {
        **settings,
        "learn_departure_time": True,
        "trip_date_time": datetime.datetime.combine(trip_day, datetime.time(6, 0)),
        "trip_homecoming_date_time": datetime.datetime.combine(trip_day, datetime.time(20, 0)),
    }
    assert _effective(engine, trip) == (datetime.time(7, 0), datetime.time(16, 0))  # 2 days left: too few
    assert _learned(engine, trip)["friday"]["samples"] == 2


def test_missing_entered_times_are_not_replaced_by_the_learned_ones() -> None:
    engine, settings = _engine(_friday_windows((6, 40), (17, 10)))
    engine.setting = lambda key, *args, **kwargs: None  # an unreadable setting stays unreadable for the callers
    assert engine.effective_workday_times("friday") == (None, None)


def test_the_learned_times_are_published_as_attributes_of_the_time_entities() -> None:
    engine, settings = _engine(_friday_windows((6, 40), (17, 10)))
    now = datetime.datetime.combine(DAY, datetime.time(12, 30))
    result = engine.run_job(snapshot(now, level=20, settings=settings, **PLUGGED_IN), engine.update_learned_workplan)
    assert result.error is None, result.error
    assert result.attributes["workday_departure_friday"] == {"learned_time": "06:30", "learned_days": 3}
    assert result.attributes["workday_homecoming_friday"] == {"learned_time": "17:15", "learned_days": 3}
    assert result.attributes["workday_departure_monday"] == {"learned_time": None, "learned_days": 0}


def test_the_cache_follows_new_windows_and_the_day() -> None:
    engine, settings = _engine(_friday_windows((6, 40), (17, 10), count=2))
    assert _learned(engine, settings)["friday"]["samples"] == 2
    engine.economy_db["windows"] = _friday_windows((6, 40), (17, 10), count=3)
    assert _learned(engine, settings)["friday"]["samples"] == 3


# --- what the plan does with them -----------------------------------------------------------------------------------


def _planned_friday(engine: CJPEngine, settings: dict) -> tuple[str, str]:
    now = datetime.datetime.combine(DAY, datetime.time(12, 30))
    result = engine.run_job(snapshot(now, level=20, settings=settings, **PLUGGED_IN), engine.recalculate)
    assert result.error is None, result.error
    friday = DAY + datetime.timedelta(days=1)
    rows = [d for d in result.attributes["current_charging_rule"]["distance_forecast"] if d["time"].startswith(str(friday))]
    assert len(rows) == 1
    return rows[0]["time"], rows[0]["end"]


def test_the_plan_leaves_and_returns_at_the_learned_times_with_learning_on() -> None:
    engine, settings = _engine(_friday_windows((6, 40), (17, 10)), {"workday_distance_needed_friday": 80.0})
    friday = DAY + datetime.timedelta(days=1)
    assert _planned_friday(engine, {**settings, "learn_departure_time": False}) == (
        datetime.datetime.combine(friday, datetime.time(7, 0)).isoformat(),
        datetime.datetime.combine(friday, datetime.time(16, 0)).isoformat(),
    )
    assert _planned_friday(engine, {**settings, "learn_departure_time": True}) == (
        datetime.datetime.combine(friday, datetime.time(6, 30)).isoformat(),
        datetime.datetime.combine(friday, datetime.time(17, 15)).isoformat(),
    )


def test_the_plan_is_unchanged_when_learning_is_off_even_with_learned_times() -> None:
    """The regression: with the switch off the plan is the one it was before there was a learned work plan at all."""
    windows = _friday_windows((6, 40), (17, 10))
    with_windows, settings = _engine(windows, {"workday_distance_needed_friday": 80.0})
    without_windows, _ = _engine([], {"workday_distance_needed_friday": 80.0})
    off = {**settings, "learn_departure_time": False}
    assert _planned_friday(with_windows, off) == _planned_friday(without_windows, off)
    now = datetime.datetime.combine(DAY, datetime.time(12, 30))
    plans = [
        sorted(ts for ts in engine.charge_hours if isinstance(ts, datetime.datetime))
        for engine in (with_windows, without_windows)
        if engine.run_job(snapshot(now, level=20, settings=off, **PLUGGED_IN), engine.recalculate).error is None
    ]
    assert plans[0] == plans[1]


def test_the_plan_forecast_and_preheat_read_the_times_only_through_the_helper() -> None:
    """The entered times are read directly only by the helper itself and by the two readers that deliberately keep them:
    the economy analysis of past connections (_next_departure) and the What-if replay of the entered settings.
    Everything else (the plan, the consumption forecast, the preheating) goes through effective_workday_times, so a new reader
    cannot skip it."""
    from pathlib import Path  # noqa: PLC0415

    engine_dir = Path(__file__).resolve().parent.parent / "custom_components" / "cable_juice_planner" / "engine"
    allowed = {"workplan.py", "economy.py", str(Path("whatif") / "engine_part.py")}
    direct = []
    for path in engine_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"""setting\(\s*f?["']workday_(departure|homecoming)_""", text) and str(path.relative_to(engine_dir)) not in allowed:
            direct.append(str(path.relative_to(engine_dir)))
    assert direct == [], direct
    for name in ("planner/planner.py", "energy/forecast.py", "control/preheat.py"):
        assert "effective_workday_times(" in (engine_dir / name).read_text(encoding="utf-8"), name


def _effective_at(engine: CJPEngine, settings: dict, date: datetime.date | None, now: datetime.datetime | None = None, **state):
    now = now or datetime.datetime.combine(DAY, datetime.time(12, 30))
    holder = {}

    def read() -> None:
        holder["times"] = engine.effective_workday_times("thursday", date)

    assert engine.run_job(snapshot(now, level=20, settings=settings, **state), read).error is None
    return holder["times"]


def _thursday_engine(entered: tuple[datetime.time, datetime.time] = (datetime.time(14, 0), datetime.time(18, 0))):
    thursdays = sorted(DAY - datetime.timedelta(weeks=weeks) for weeks in range(1, 5))
    engine, settings = _engine(
        chain([(thursday, (6, 40), (17, 10)) for thursday in thursdays]),
        {"workday_thursday": True, "workday_departure_thursday": entered[0], "workday_homecoming_thursday": entered[1]},
    )
    return engine, {**settings, "learn_departure_time": True}


def _left_today(engine: CJPEngine) -> None:
    """The car was unplugged at 07:00 today: the newest connection window is closed."""
    engine.economy_db["windows"][-1]["end"] = ts(DAY, 7, 0)


def test_today_with_the_car_still_plugged_in_after_the_learned_departure_uses_the_entered_departure() -> None:
    """Learned 06:30, entered 14:00, now 12:30 and a connection window is open (the car is here, plugged in): it did not leave
    at 06:30, so today's work plan is still ahead. Before the learned departure the learned one counts."""
    engine, settings = _thursday_engine()
    assert _effective_at(engine, settings, DAY, **PLUGGED_IN) == (datetime.time(14, 0), datetime.time(18, 0))
    early = datetime.datetime.combine(DAY, datetime.time(5, 30))
    assert _effective_at(engine, settings, DAY, early, **PLUGGED_IN) == (datetime.time(6, 30), datetime.time(18, 0))
    # another day is not today
    assert _effective_at(engine, settings, DAY + datetime.timedelta(days=7), **PLUGGED_IN) == (datetime.time(6, 30), datetime.time(18, 0))


def test_a_car_that_really_left_today_counts_with_the_learned_departure() -> None:
    """The newest connection window is closed (unplugged at 07:00): the learned departure is the one that applies, and the
    planner plans the way back from it."""
    engine, settings = _thursday_engine()
    _left_today(engine)
    assert _effective_at(engine, settings, DAY, status="disconnected", port="off") == (datetime.time(6, 30), datetime.time(18, 0))


@pytest.mark.parametrize("status", ["unavailable", "unknown", "", "disconnected"])
def test_a_missing_or_unknown_status_is_no_sign_that_the_car_left(status: str) -> None:
    """The charger's status entity drops out while the car stands plugged in: only a closed connection window says that the
    car left, so today keeps the entered departure and today's work day is not dropped from the plan."""
    engine, settings = _thursday_engine()
    assert _effective_at(engine, settings, DAY, status=status, port="on") == (datetime.time(14, 0), datetime.time(18, 0))


def test_the_homecoming_is_still_the_later_of_entered_and_learned_today() -> None:
    """Only the departure goes back to the entered one today: an entered homecoming earlier than the learned one stays later."""
    engine, settings = _thursday_engine((datetime.time(14, 0), datetime.time(16, 0)))
    assert _effective_at(engine, settings, DAY, **PLUGGED_IN) == (datetime.time(14, 0), datetime.time(17, 15))


def test_todays_work_plan_is_not_dropped_by_an_earlier_learned_departure() -> None:
    """The planner plans today's work day while now is before the departure it uses. Learned 06:30 would have made it 'past'
    at 12:30 while the car stands plugged in and the user leaves at 14:00."""
    engine, settings = _thursday_engine()
    now = datetime.datetime.combine(DAY, datetime.time(12, 30))
    result = engine.run_job(snapshot(now, level=20, settings={**settings, "workday_distance_needed_thursday": 80.0}, **PLUGGED_IN), engine.recalculate)
    assert result.error is None, result.error
    today = [d for d in result.attributes["current_charging_rule"]["distance_forecast"] if d["time"].startswith(str(DAY))]
    assert [d["time"] for d in today] == [datetime.datetime.combine(DAY, datetime.time(14, 0)).isoformat()]


def test_a_day_with_a_trip_uses_the_entered_times() -> None:
    engine, settings = _thursday_engine()
    trip = {
        **settings,
        "trip_date_time": datetime.datetime.combine(DAY + datetime.timedelta(days=7), datetime.time(6, 0)),
        "trip_homecoming_date_time": datetime.datetime.combine(DAY + datetime.timedelta(days=7), datetime.time(20, 0)),
    }
    away = {"status": "disconnected", "port": "off"}
    assert _effective_at(engine, trip, DAY + datetime.timedelta(days=7), **away) == (datetime.time(14, 0), datetime.time(18, 0))
    assert _effective_at(engine, trip, DAY + datetime.timedelta(days=14), **away) == (datetime.time(6, 30), datetime.time(18, 0))


def _trip(settings: dict, day: datetime.date, start=(6, 0), end=(20, 0)) -> dict:
    return {
        **settings,
        "trip_date_time": datetime.datetime.combine(day, datetime.time(*start)),
        "trip_homecoming_date_time": datetime.datetime.combine(day, datetime.time(*end)),
    }


def test_the_dates_of_a_trip_that_has_started_are_kept_after_the_settings_reset_it() -> None:
    """The trip settings are reset two hours after the departure, before the day can be a sample the next day."""
    engine, settings = _thursday_engine()
    trip_day = DAY - datetime.timedelta(weeks=2)
    now = datetime.datetime.combine(DAY, datetime.time(12, 30))
    first = engine.run_job(snapshot(now, level=20, settings=_trip(settings, trip_day), **PLUGGED_IN), engine.update_learned_workplan)
    assert first.error is None, first.error
    assert "runtime" in first.db_saves
    assert engine.trip_days == {trip_day.isoformat()}
    # the settings are reset: the day is still no sample
    result = engine.run_job(snapshot(now, level=20, settings=settings, **PLUGGED_IN), engine.update_learned_workplan)
    assert result.attributes["workday_departure_thursday"]["learned_days"] == 3
    # kept in the runtime database and read again after a restart
    saved = engine._runtime_state()
    assert saved["trip_days"] == [trip_day.isoformat()]
    restarted = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert restarted.run_job(snapshot(now, level=20, settings=settings, **PLUGGED_IN), restarted.startup, {"runtime": saved}).error is None
    assert restarted.trip_days == {trip_day.isoformat()}


def test_a_trip_that_has_not_started_leaves_nothing_behind() -> None:
    """A planned trip is excluded while it is set, but a trip that is moved, cancelled or typed wrongly is not remembered, and
    never more than the days that have gone."""
    engine, settings = _thursday_engine()
    now = datetime.datetime.combine(DAY, datetime.time(12, 30))
    future = _trip(settings, DAY + datetime.timedelta(days=3))
    assert engine.run_job(snapshot(now, level=20, settings=future, **PLUGGED_IN), engine.update_learned_workplan).error is None
    assert engine.trip_days == set()
    assert _effective_at(engine, future, DAY + datetime.timedelta(days=3), status="disconnected", port="off") == (datetime.time(14, 0), datetime.time(18, 0))
    # a trip set a year ahead (a typing error) is not kept either
    typo = {**future, "trip_homecoming_date_time": datetime.datetime.combine(DAY + datetime.timedelta(days=400), datetime.time(20, 0))}
    assert engine.run_job(snapshot(now, level=20, settings=typo, **PLUGGED_IN), engine.update_learned_workplan).error is None
    assert engine.trip_days == set()
    # a trip that started yesterday and runs on: only the days that have gone
    running = {**future, "trip_date_time": datetime.datetime.combine(DAY - datetime.timedelta(days=2), datetime.time(6, 0)), "trip_homecoming_date_time": typo["trip_homecoming_date_time"]}
    assert engine.run_job(snapshot(now, level=20, settings=running, **PLUGGED_IN), engine.update_learned_workplan).error is None
    assert engine.trip_days == {(DAY - datetime.timedelta(days=offset)).isoformat() for offset in (2, 1, 0)}


def test_changing_a_started_trip_saves_its_dates_in_the_same_job() -> None:
    engine, settings = _thursday_engine()
    now = datetime.datetime.combine(DAY, datetime.time(12, 30))
    trip_day = DAY - datetime.timedelta(days=1)
    result = engine.run_job(snapshot(now, level=20, settings=_trip(settings, trip_day), **PLUGGED_IN), engine.trips_changed, "trip_date_time", datetime.datetime.combine(trip_day, datetime.time(6, 0)))
    assert result.error is None, result.error
    assert engine.trip_days == {trip_day.isoformat()}
    assert "runtime" in result.db_saves


def test_resetting_a_started_trip_keeps_its_dates() -> None:
    """The automatic reset two hours after the departure, and the reset button, run trip_reset."""
    engine, settings = _thursday_engine()
    now = datetime.datetime.combine(DAY, datetime.time(9, 0))
    result = engine.run_job(snapshot(now, level=20, settings=_trip(settings, DAY, (7, 0), (20, 0)), **PLUGGED_IN), engine.trip_reset)
    assert result.error is None, result.error
    assert engine.trip_days == {DAY.isoformat()}


def test_old_trip_dates_are_forgotten() -> None:
    engine, settings = _thursday_engine()
    engine.trip_days = {(DAY - datetime.timedelta(weeks=20)).isoformat(), (DAY - datetime.timedelta(weeks=2)).isoformat()}
    now = datetime.datetime.combine(DAY, datetime.time(12, 30))
    assert engine.run_job(snapshot(now, level=20, settings=settings, **PLUGGED_IN), engine.update_learned_workplan).error is None
    assert engine.trip_days == {(DAY - datetime.timedelta(weeks=2)).isoformat()}


def test_windows_that_are_not_real_numbers_are_left_out() -> None:
    day = DAY - datetime.timedelta(days=1)
    good = chain([(day, (7, 0), (16, 0))])
    for bad in (float("nan"), float("inf"), True):
        windows = [*good, {"start": bad, "end": 5.0}, {"start": 5.0, "end": bad}]
        assert window_samples(windows, TZ, DAY, ALL_WORKDAYS, set()) == {2: [(minutes(7), minutes(16))]}


def test_anything_that_goes_wrong_gives_the_entered_times(caplog: pytest.LogCaptureFixture) -> None:
    engine, settings = _engine(_friday_windows((6, 40), (17, 10)))
    settings = {**settings, "learn_departure_time": True}

    def broken() -> dict:
        raise TypeError("broken")

    engine.learned_workplan = broken
    assert _effective(engine, settings) == (datetime.time(7, 0), datetime.time(16, 0))
    assert "the entered ones are" in caplog.text


def test_the_learned_times_are_calculated_once_per_job() -> None:
    engine, settings = _engine(_friday_windows((6, 40), (17, 10)))
    now = datetime.datetime.combine(DAY, datetime.time(12, 30))
    holder = []

    def read() -> None:
        holder.extend([engine.learned_workplan(), engine.learned_workplan()])
        engine.economy_db["windows"] = []  # a change inside the job is seen by the next one
        holder.append(engine.learned_workplan())

    assert engine.run_job(snapshot(now, level=20, settings=settings, **PLUGGED_IN), read).error is None
    assert holder[0] is holder[1] is holder[2]
    assert _learned(engine, settings) == {}


def test_the_learned_times_are_published_at_start_at_every_hour_end_and_when_the_windows_change() -> None:
    windows = _friday_windows((6, 40), (17, 10))
    now = datetime.datetime.combine(DAY, datetime.time(12, 30))
    settings = {
        "workplan_charging": True, "workday_friday": True,
        "workday_departure_friday": datetime.time(7, 0), "workday_homecoming_friday": datetime.time(16, 0),
    }
    started = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    result = started.run_job(snapshot(now, level=20, settings=settings, **PLUGGED_IN), started.startup, {"economy": {"windows": windows}})
    assert result.error is None, result.error
    assert result.attributes["workday_departure_friday"]["learned_time"] == "06:30", "at the start"

    engine, _ = _engine(windows)
    hour_end = engine.run_job(snapshot(datetime.datetime.combine(DAY, datetime.time(12, 59)), level=20, settings=settings, **PLUGGED_IN), engine.hour_end)
    assert hour_end.error is None, hour_end.error
    assert hour_end.attributes["workday_homecoming_friday"]["learned_time"] == "17:15", "at the hour end"

    # the economy update changes the windows (and drops those older than the charging history): the suggestions follow at once
    changed, _ = _engine([])
    calls = []
    original = changed.update_learned_workplan
    changed.update_learned_workplan = lambda: (calls.append(1), original())[1]
    update = changed.run_job(snapshot(now, level=20, settings=settings, **PLUGGED_IN), changed.economy_update, {})
    assert update.error is None, update.error
    assert calls == [1]


def test_the_plan_is_recalculated_when_the_times_it_uses_change() -> None:
    """The hash that decides whether a plan is calculated again holds the times the plan uses: the switch, and today's departure
    when the car is still here after the learned departure."""
    engine, settings = _thursday_engine()
    before_learned = datetime.datetime.combine(DAY, datetime.time(6, 20))
    after_learned = datetime.datetime.combine(DAY, datetime.time(6, 40))

    def hash_for(now: datetime.datetime, learning: bool):
        holder = {}

        def read() -> None:
            holder["times"] = engine.get_hash_dict()["WORKPLAN_TIMES"]

        run = {**settings, "learn_departure_time": learning}
        assert engine.run_job(snapshot(now, level=20, settings=run, **PLUGGED_IN), read).error is None
        return holder["times"]

    assert hash_for(before_learned, True) != hash_for(before_learned, False)
    assert hash_for(before_learned, True) != hash_for(after_learned, True)  # 06:30 passes: today's departure is the entered one
    assert hash_for(after_learned, False) == hash_for(before_learned, False)  # off: nothing depends on the learned times


def test_a_departure_that_changed_after_it_was_saved_is_not_counted_twice() -> None:
    """Ready at departure: today's saved departure (a learned 06:30 that the car did not keep) is replaced by the later one the
    plan now uses, not kept next to it."""
    engine, _ = _thursday_engine()
    now = datetime.datetime.combine(DAY, datetime.time(6, 45))
    engine.charging_plan = {0: {
        "workday": True, "work_departure": datetime.datetime.combine(DAY, datetime.time(14, 0)),
        "battery_level_before_work_sum": 70.0, "work_battery_level_needed": 20.0,
    }}
    engine.analysis_db["departures"] = [{"date": DAY.isoformat(), "departure": ts(DAY, 6, 30), "target": 70.0, "required": 40.0, "level": 30.0, "met": False, "target_met": False}]
    result = engine.run_job(snapshot(now, level=60, **PLUGGED_IN), engine.analysis_check_departure)
    assert result.error is None, result.error
    assert [entry for entry in engine.analysis_db["departures"] if entry["date"] == DAY.isoformat()] == []
    assert engine.analysis_db["pending_departure"]["departure"] == ts(DAY, 14, 0)


def test_the_switch_defaults_to_off() -> None:
    assert const.SWITCH_SETTINGS["learn_departure_time"]["default"] is False
    assert const.build_config(OPTIONS)  # the options build as before
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snapshot(datetime.datetime.combine(DAY, datetime.time(12, 30))), engine.startup, {}).error is None
    assert engine.learn_departure_time_enabled() is False


# --- the dashboard --------------------------------------------------------------------------------------------------


def _section(language: str = "en") -> list[dict]:
    import yaml  # noqa: PLC0415

    from custom_components.cable_juice_planner.management.dashboard_build import render_dashboard  # noqa: PLC0415

    from .test_dashboard import _own  # noqa: PLC0415

    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    text = render_dashboard(const.build_config({**OPTIONS, "language": language}), "tesla", "entry1", existing)
    assert "@@" not in text
    views = yaml.safe_load(text)["views"]
    return next(
        section["cards"] for section in views[2]["sections"]
        if any(card.get("icon") == "mdi:clock-time-eight-outline" for card in section["cards"] if card.get("type") == "heading")
    )


def test_the_analysis_has_a_section_with_the_entered_the_learned_and_the_used_times() -> None:
    from jinja2 import Environment  # noqa: PLC0415

    cards = _section()
    assert cards[0]["heading"] == "Learned departure and homecoming"
    table = cards[2]["content"]
    assert "learned_time" in table and "learned_days" in table and "switch.cjp_tesla_learn_departure_time" in table
    template = Environment().from_string(table)

    def render(learning: bool, learned_days: int) -> str:
        states = {
            "switch.cjp_tesla_learn_departure_time": "on" if learning else "off",
            "switch.cjp_tesla_workday_friday": "on",
            "switch.cjp_tesla_workplan_charging": "on",
        }
        attributes = {}
        for day in const.WEEKDAYS:
            states[f"time.cjp_tesla_workday_departure_{day}"] = "07:00:00"
            states[f"time.cjp_tesla_workday_homecoming_{day}"] = "16:00:00"
        attributes[("time.cjp_tesla_workday_departure_friday", "learned_time")] = "06:30"
        attributes[("time.cjp_tesla_workday_homecoming_friday", "learned_time")] = "17:15"
        attributes[("time.cjp_tesla_workday_departure_friday", "learned_days")] = learned_days
        return template.render(
            states=lambda entity: states.get(entity, "unknown"),
            state_attr=lambda entity, attribute: attributes.get((entity, attribute)),
            is_state=lambda entity, value: states.get(entity) == value,
        )

    row = lambda text: next(line for line in text.splitlines() if line.startswith("| **Friday**"))  # noqa: E731
    on = render(True, 4)
    assert "is on" in on and "07:00–16:00 | 06:30–17:15 | 06:30–17:15 | 4 |" in row(on)
    off = render(False, 4)
    assert "is off" in off and "07:00–16:00 | 06:30–17:15 | 07:00–16:00 | 4 |" in row(off)  # learned, but not used
    few = render(True, 2)
    assert "07:00–16:00 | 06:30–17:15 | 07:00–16:00 | 2 ⏳ |" in row(few)  # on, but too few days: not used yet
    assert "No days learned yet" in render(True, 0)  # nothing anywhere in the week
    assert "No days learned yet" not in on


def test_the_switch_and_its_help_are_in_the_advanced_settings() -> None:
    import yaml  # noqa: PLC0415

    from custom_components.cable_juice_planner.management.dashboard_build import render_dashboard  # noqa: PLC0415

    from .test_dashboard import _own  # noqa: PLC0415

    existing = _own("tesla") | {"sensor.ev_battery_level", "sensor.charger_status"}
    text = render_dashboard(const.build_config({**OPTIONS, "language": "en"}), "tesla", "entry1", existing)
    assert "state_attr('switch.cjp_tesla_learn_departure_time', 'help')" in text
    assert "switch.cjp_tesla_learn_departure_time" in yaml.safe_dump(yaml.safe_load(text)["views"][1])


def _preheat_at(engine: CJPEngine, settings: dict, now: datetime.datetime, integration: str = "tessie", climate_state: str = "off", **state) -> list[tuple[str, dict]]:
    """The preheat requests preheat_ev() makes at `now` for a car with a climate entity and 20 minutes of lead time."""
    from unittest.mock import patch  # noqa: PLC0415

    fired: list[tuple[str, dict]] = []
    engine.config["ev_car"]["entity_ids"]["climate_entity_id"] = "climate.car"
    settings = {**settings, "preheat_minutes_before": 20.0, "preheat_thursday": True}
    with (
        patch.object(engine, "is_ev_configured", return_value=True),
        patch.object(engine, "workplan_charging_enabled", return_value=True),
        patch.object(engine, "is_entity_available", return_value=True),
        patch.object(engine, "ready_to_charge", return_value=True),
        patch.object(engine, "get_integration", return_value=integration),
        patch.object(engine, "get_state", return_value=climate_state),
        patch.object(engine, "drive_efficiency", side_effect=lambda state: setattr(engine, "preheating", state == "preheat_start") if state in ("preheat_start", "preheat_cancel") else None),
        patch.object(engine, "my_notify"),
        patch.object(engine, "fire_ev_request", side_effect=lambda kind, **data: fired.append((kind, data))),
    ):
        assert engine.run_job(snapshot(now, level=20, settings=settings, **state), engine.preheat_ev).error is None
    return fired


def test_preheating_follows_the_departure_the_plan_uses() -> None:
    """Learned departure 06:30, entered 14:00: with learning on the car is preheated before 06:30, not before 14:00."""
    engine, settings = _thursday_engine()
    early = datetime.datetime.combine(DAY, datetime.time(6, 15))
    assert _preheat_at(engine, settings, early, **PLUGGED_IN) == [("preheat_start", {"departure": "06:30", "defrost": False})]
    assert _preheat_at(engine, settings, datetime.datetime.combine(DAY, datetime.time(13, 45)), **PLUGGED_IN) == [
        ("preheat_start", {"departure": "14:00", "defrost": False})  # it did not leave at 06:30: the entered departure again
    ]


def test_preheating_keeps_the_entered_departure_with_learning_off() -> None:
    engine, settings = _thursday_engine()
    settings = {**settings, "learn_departure_time": False}
    assert _preheat_at(engine, settings, datetime.datetime.combine(DAY, datetime.time(6, 15)), **PLUGGED_IN) == []
    assert _preheat_at(engine, settings, datetime.datetime.combine(DAY, datetime.time(13, 45)), **PLUGGED_IN) == [
        ("preheat_start", {"departure": "14:00", "defrost": False})
    ]


def test_preheating_before_the_learned_departure_needs_enough_learned_days() -> None:
    """Fewer than 3 days of the weekday: the entered departure counts, so nothing is preheated at 06:15."""
    engine, settings = _thursday_engine()
    engine.economy_db["windows"] = engine.economy_db["windows"][-2:]
    assert _preheat_at(engine, settings, datetime.datetime.combine(DAY, datetime.time(6, 15)), **PLUGGED_IN) == []


def test_a_preheating_for_the_learned_departure_is_stopped_when_the_car_did_not_leave_and_started_again_for_the_entered_one() -> None:
    """A car (switch or climate entity) preheated before 06:30 that is still plugged in at 07:35: the stop is judged against 06:30, not
    against the entered 14:00 the plan moved to, so the climate does not run until the afternoon. Then it is preheated for 14:00."""
    engine, settings = _thursday_engine()
    at = lambda hour, minute: datetime.datetime.combine(DAY, datetime.time(hour, minute))  # noqa: E731
    run = lambda now: _preheat_at(engine, settings, now, integration="other", **PLUGGED_IN)  # noqa: E731
    assert run(at(6, 15)) == [("preheat_start", {"departure": "06:30", "defrost": False})]
    assert run(at(6, 20)) == []  # running: no second start
    assert run(at(7, 0)) == []  # not yet: the stop comes 3 x the lead time after the departure
    assert run(at(7, 35)) == [("preheat_stop", {"departure": "06:30"})]
    assert engine.preheating is False
    assert run(at(13, 45)) == [("preheat_start", {"departure": "14:00", "defrost": False})]
    assert run(at(15, 5)) == [("preheat_stop", {"departure": "14:00"})]


def test_a_tessie_car_that_heats_for_the_learned_departure_is_stopped_at_the_same_time() -> None:
    engine, settings = _thursday_engine()
    engine._preheat_departure = datetime.datetime.combine(DAY, datetime.time(6, 30))
    stopped = _preheat_at(engine, settings, datetime.datetime.combine(DAY, datetime.time(7, 35)), climate_state="heat_cool", **PLUGGED_IN)
    assert stopped == [("preheat_stop", {"departure": "06:30"})]
    # after a restart nothing is remembered: today's learned departure stands in for it, the car is still stopped
    engine._preheat_departure = None
    assert _preheat_at(engine, settings, datetime.datetime.combine(DAY, datetime.time(7, 35)), climate_state="heat_cool", **PLUGGED_IN) == [
        ("preheat_stop", {"departure": "06:30"})
    ]
    assert engine._preheat_departure is None
    # a start remembers the learned departure it was for
    assert _preheat_at(engine, settings, datetime.datetime.combine(DAY, datetime.time(6, 15)), **PLUGGED_IN) == [
        ("preheat_start", {"departure": "06:30", "defrost": False})
    ]
    assert engine._preheat_departure == datetime.datetime.combine(DAY, datetime.time(6, 30))


def test_a_missed_stop_window_is_still_corrected_by_the_departure_the_plan_uses() -> None:
    """Preheating for 06:30 whose stop window (07:30-07:40) was missed (the climate entity was unavailable): the next window is the
    entered departure's, 15:00-15:10, and it stops then (as the old code did), also for a switch car."""
    engine, settings = _thursday_engine()
    at = lambda hour, minute: datetime.datetime.combine(DAY, datetime.time(hour, minute))  # noqa: E731
    run = lambda now: _preheat_at(engine, settings, now, integration="other", **PLUGGED_IN)  # noqa: E731
    assert run(at(6, 15)) == [("preheat_start", {"departure": "06:30", "defrost": False})]
    assert run(at(9, 0)) == []  # the window was missed: the preheating is still marked as running
    assert engine.preheating is True
    assert run(at(15, 5)) == [("preheat_stop", {"departure": "14:00"})]
    assert engine.preheating is False


def test_with_learning_off_nothing_is_remembered_and_the_stop_is_the_one_departure_alone() -> None:
    """Exactly as before: a start for a trip and then a work day (switch car): the stop is judged against the departure now."""
    engine, settings = _thursday_engine()
    settings = {**settings, "learn_departure_time": False}
    at = lambda hour, minute: datetime.datetime.combine(DAY, datetime.time(hour, minute))  # noqa: E731
    assert _preheat_at(engine, settings, at(13, 45), integration="other", **PLUGGED_IN) == [("preheat_start", {"departure": "14:00", "defrost": False})]
    assert engine._preheat_departure is None
    # 07:35 is 3 x lead after the learned 06:30 - with learning off there is no such departure to stop for
    engine.preheating = True
    assert _preheat_at(engine, settings, at(7, 35), integration="other", **PLUGGED_IN) == []
    assert engine._preheat_departure is None
