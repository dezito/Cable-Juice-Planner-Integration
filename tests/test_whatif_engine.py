"""What-if Scenario A in the engine: stored data in, published result out, nothing live changed."""
from __future__ import annotations

import datetime

import pytest

from .test_economy import _engine, _ts
from .test_engine import DAY, TZ

LOCAL_START = 18  # plugged in at 18:00
LOCAL_END = 7  # unplugged at 07:00 the next day


def _seed(engine, days=range(1, 6), skip_price_day: int | None = None, public: bool = True) -> list[tuple[float, float]]:
    """Five evening-to-morning connections with cheap hours 02-04 and a 20 % drive after each."""
    windows = []
    prices = engine.economy_db.setdefault("prices", {})
    for back in days:
        day = DAY - datetime.timedelta(days=back)
        start, end = _ts(day, LOCAL_START), _ts(day + datetime.timedelta(days=1), LOCAL_END)
        windows.append((start, end))
        engine.economy_db["windows"].append({"start": start, "end": end})
        hour = start
        while hour < end:
            local = datetime.datetime.fromtimestamp(hour, TZ).hour
            if back != skip_price_day or hour < start + 3 * 3600:
                prices[hour] = 0.3 if 2 <= local < 5 else 1.0
            hour += 3600
        engine.analysis_db["drives"].append({"start": end + 300, "end": end + 3600, "km": 40.0, "used": 20.0, "temp": None})
    if public:
        public_day = DAY - datetime.timedelta(days=3)
        engine.charging_history_db[datetime.datetime.combine(public_day, datetime.time(12))] = {
            "kWh": 4.0, "cost": 3.0, "kWh_from_public": 4.0, "charging_type": "public_charging",
        }
    engine.economy_db["prices_averaged_since"] = _ts(DAY - datetime.timedelta(days=40), 0)
    return windows


def _run(engine, snap, start=None):
    prepared = engine.run_job(snap, engine.what_if_prepare)
    assert prepared.error is None, prepared.error
    result = engine.run_job(snap, engine.what_if_run, start or {"soc": 50.0, "source": "entered"})
    assert result.error is None, result.error
    return prepared, result


def test_scenario_a_replays_the_stored_history_and_publishes_it() -> None:
    engine, snap = _engine()
    windows = _seed(engine)
    prepared, result = _run(engine, snap)
    assert prepared.value["first_window_start"] == min(start for start, _ in windows)
    r = result.value
    assert r["status"] == "ok" and r["comparable"] is True
    assert r["windows"]["total"] == 5 and r["windows"]["replayed"] == 5
    assert r["target"] == {"battery_level": 90.0, "source": "live_setting"}
    assert r["provenance"]["start_soc"] == "entered"
    assert r["smart"]["cost"] < r["asap"]["cost"] and r["difference"]["cost"] < 0
    assert r["smart"]["final_soc"] == pytest.approx(r["asap"]["final_soc"])
    assert r["smart"]["targets_met"] == 5 and r["smart"]["unmet_target_kwh"] == 0
    assert r["coverage"]["drives"] == 4 and r["coverage"]["public_charges"] == 1  # the drive after the last window is outside
    assert r["warnings"] == []  # the public charge lies in a gap with a recorded drive
    assert r["coverage"]["public_charges_without_drive"] == 0
    assert "hindsight" in r["notes"] and r["conservation_error_kwh"] < 1e-6
    assert [m["windows"] for m in r["months"]] == [5]
    assert r["recorded_reference"]["sessions"] == 1 and r["recorded_reference"]["cost"] == 3.0
    # published on the sensor and saved for the next restart
    assert result.attributes["what_if"]["result"]["difference"] == r["difference"]
    assert result.states["what_if"] == r["difference"]["adjusted_cost"]
    assert engine.economy_db["what_if"]["computed_at"] == r["computed_at"]
    assert engine.economy_db["what_if_attempt"]["status"] == "ok"


def test_scenario_a_changes_nothing_live() -> None:
    engine, snap = _engine()
    _seed(engine)
    settings = dict(snap.settings)
    plan_before = repr(getattr(engine, "charging_plan", None))
    _, result = _run(engine, snap)
    assert not result.settings and not getattr(result, "config_updates", None)
    assert snap.settings == settings
    assert repr(getattr(engine, "charging_plan", None)) == plan_before


def test_scenario_a_needs_a_start_battery_level() -> None:
    engine, snap = _engine()
    _seed(engine)
    _, result = _run(engine, snap, {"soc": None, "source": "unavailable"})
    r = result.value
    assert r["status"] == "unavailable" and r["reason"] == "no_start_soc" and r["smart"] is None
    # never defaulted, and nothing was saved
    assert "what_if" not in engine.economy_db
    assert result.states["what_if"] is None and result.attributes["what_if"]["attempt"]["reason"] == "no_start_soc"


def test_a_failed_calculation_keeps_the_previous_saved_result() -> None:
    engine, snap = _engine()
    _seed(engine)
    _, first = _run(engine, snap)
    saved = engine.economy_db["what_if"]
    _, failed = _run(engine, snap, {"soc": None, "source": "unavailable"})
    assert failed.value["status"] == "unavailable"
    assert engine.economy_db["what_if"] is saved
    assert failed.states["what_if"] == first.value["difference"]["adjusted_cost"]  # the saved result is still shown
    assert failed.attributes["what_if"]["result"]["computed_at"] == first.value["computed_at"]
    assert failed.attributes["what_if"]["attempt"]["status"] == "unavailable"


def test_a_missing_price_stops_the_replay_and_is_reported() -> None:
    engine, snap = _engine()
    _seed(engine, skip_price_day=3)
    _, result = _run(engine, snap)
    r = result.value
    assert r["status"] == "partial" and r["reason"] == "missing_prices"
    assert 0 < r["windows"]["replayed"] < r["windows"]["total"] and r["windows"]["stopped_at"] is not None
    assert r["coverage"]["price_hours_missing"] > 0


def test_a_temporary_target_never_changes_the_live_setting() -> None:
    engine, snap = _engine()
    _seed(engine)
    live = engine.get_max_recommended_charge_limit_battery_level()
    _, result = _run(engine, snapshot_with(snap, {"what_if_target_battery_level": 60.0}))
    assert result.value["target"] == {"battery_level": 60.0, "source": "override"}
    assert engine.get_max_recommended_charge_limit_battery_level() == live


def snapshot_with(snap, settings: dict):
    from .test_engine import snapshot

    return snapshot(datetime.datetime.combine(DAY, datetime.time(12, 0)), settings=settings)


def test_day_selection_applies_smart_only_on_the_chosen_weekdays() -> None:
    engine, snap = _engine()
    windows = _seed(engine)
    _, everything = _run(engine, snap)
    _, weekend = _run(engine, snapshot_with(snap, {"what_if_day_selection": "weekend"}))
    chosen = {"all": everything.value, "weekend": weekend.value}
    for r in chosen.values():
        assert r["status"] == "ok"
    for row in weekend.value["rows"]:
        weekday = datetime.datetime.fromtimestamp(row["end"], TZ).weekday()
        if weekday < 5:  # not a selected day: both charge immediately
            assert row["smart_cost"] == pytest.approx(row["asap_cost"])
    # the unplug days are Fri to Tue: Saturday and Sunday mornings are the only smart ones
    assert len([row for row in weekend.value["rows"] if row["smart_cost"] < row["asap_cost"]]) <= 2
    assert abs(weekend.value["difference"]["cost"]) < abs(everything.value["difference"]["cost"])
    assert weekend.value["day_selection"] == "weekend" and len(windows) == 5


def test_the_saved_result_is_published_again_after_a_restart() -> None:
    engine, snap = _engine()
    _seed(engine)
    _, result = _run(engine, snap)
    engine.economy_db["what_if_attempt"] = {"status": "running", "reason": None, "at": 1.0}
    restarted = engine.run_job(snap, engine.load_what_if)
    assert restarted.attributes["what_if"]["result"]["computed_at"] == result.value["computed_at"]
    assert restarted.attributes["what_if"]["attempt"]["status"] == "unavailable"
    assert restarted.attributes["what_if"]["attempt"]["reason"] == "interrupted"
    assert restarted.states["what_if"] == result.value["difference"]["adjusted_cost"]


def test_an_open_connection_and_an_empty_history_are_handled() -> None:
    engine, snap = _engine()
    engine.economy_db["windows"].append({"start": _ts(DAY, 8), "end": None})
    prepared, result = _run(engine, snap)
    assert prepared.value["first_window_start"] is None
    assert result.value["status"] == "unavailable" and result.value["reason"] == "no_windows"


def test_the_work_plan_selects_its_own_days() -> None:
    engine, snap = _engine({"workday_monday": True, "workday_wednesday": True})
    _seed(engine)
    selected = snapshot_with(snap, {"what_if_day_selection": "work_plan", "workday_monday": True, "workday_wednesday": True})
    assert engine.run_job(selected, engine.what_if_smart_weekdays).value == frozenset({0, 2})
    _, result = _run(engine, selected)
    assert result.value["status"] == "ok" and result.value["day_selection"] == "work_plan"
    for row in result.value["rows"]:
        if datetime.datetime.fromtimestamp(row["end"], TZ).weekday() not in (0, 2):
            assert row["smart_cost"] == pytest.approx(row["asap_cost"])


def test_the_day_selections() -> None:
    for selection, expected in (("all", None), ("monday_friday", frozenset(range(5))), ("weekend", frozenset({5, 6})), ("selected", frozenset())):
        engine, snap = _engine({"what_if_day_selection": selection})
        assert engine.run_job(snap, engine.what_if_smart_weekdays).value == expected, selection


def test_without_any_recorded_drive_the_demand_is_unknown_not_zero() -> None:
    engine, snap = _engine()
    _seed(engine)
    engine.analysis_db["drives"].clear()
    _, result = _run(engine, snap)
    assert result.value["status"] == "unavailable" and result.value["reason"] == "no_drives"
    assert result.value["smart"] is None and "what_if" not in engine.economy_db


def test_an_empty_day_selection_is_reported_instead_of_a_zero_difference() -> None:
    engine, snap = _engine()
    _seed(engine)
    _, result = _run(engine, snapshot_with(snap, {"what_if_day_selection": "work_plan"}))  # no workday is switched on
    assert result.value["status"] == "unavailable" and result.value["reason"] == "no_days_selected"


def test_a_start_level_read_for_another_first_connection_is_not_used() -> None:
    engine, snap = _engine()
    windows = _seed(engine)
    prepared = engine.run_job(snap, engine.what_if_prepare).value
    start = {"soc": 50.0, "source": "recorded_state", "period": prepared["period"], "first_window_start": prepared["first_window_start"]}
    ok = engine.run_job(snap, engine.what_if_run, start).value
    assert ok["status"] == "ok"
    stale = engine.run_job(snap, engine.what_if_run, {**start, "first_window_start": min(w[0] for w in windows) - 7200.0}).value
    assert stale["status"] == "unavailable" and stale["reason"] == "start_level_stale"
    # the period of the prepare is the one that is replayed, even if time has passed
    assert ok["period"]["start"] == prepared["period"][0] and ok["period"]["end"] == prepared["period"][1]


def test_a_stopped_replay_publishes_its_figures_but_no_period_state() -> None:
    engine, snap = _engine()
    _seed(engine, skip_price_day=3)
    _, result = _run(engine, snap)
    assert result.value["status"] == "partial" and result.value["difference"] is not None
    assert result.states["what_if"] is None  # a whole-period number would be misleading
    assert result.attributes["what_if"]["result"]["status"] == "partial"


def test_a_missing_battery_capacity_is_unavailable() -> None:
    engine, snap = _engine()
    _seed(engine)
    engine.config["ev_car"]["battery_size"] = None
    _, result = _run(engine, snap)
    assert result.value["status"] == "unavailable" and result.value["reason"] == "no_battery_capacity"


def test_selected_weekdays_follow_the_what_if_day_switches() -> None:
    engine, snap = _engine()
    windows = _seed(engine)
    selected = snapshot_with(snap, {"what_if_day_selection": "selected", "what_if_day_tuesday": True, "what_if_day_sunday": True})
    assert engine.run_job(selected, engine.what_if_smart_weekdays).value == frozenset({1, 6})
    _, result = _run(engine, selected)
    assert result.value["status"] == "ok" and result.value["day_selection"] == "selected"
    for row in result.value["rows"]:
        if datetime.datetime.fromtimestamp(row["end"], TZ).weekday() not in (1, 6):
            assert row["smart_cost"] == pytest.approx(row["asap_cost"])
    assert len(windows) == 5


def test_the_repeated_autumn_hour_reaches_the_replay_flagged_and_never_beats_a_real_price() -> None:
    engine, snap = _engine()
    windows = _seed(engine)
    start, end = windows[0]
    hour_ts = start + 3600.0
    # a copy for an hour of the first connection, plus a real stored price for another hour that also has a copy
    stored = engine.economy_db["prices"]
    engine.economy_db["prices_dst_copy"] = {hour_ts: 9.99, start + 7200.0: 9.99}
    del stored[hour_ts]
    _, result = _run(engine, snap)
    r = result.value
    assert r["coverage"]["price_hours_dst_copied"] == 1  # only the hour without a real price counts as copied
    assert "prices_dst_hour_copied" in r["warnings"] and r["provenance"]["prices"] == "measured_partly_unverified"
    assert stored[start + 7200.0] != 9.99  # the real price was not replaced by its copy


def test_a_drive_that_can_not_be_applied_does_not_count_as_recorded_demand() -> None:
    """Two connections whose only drive starts well inside the first one: nothing can be replayed as demand."""
    engine, snap = _engine()
    first = (_ts(DAY - datetime.timedelta(days=3), 18), _ts(DAY - datetime.timedelta(days=2), 7))
    second = (_ts(DAY - datetime.timedelta(days=2), 18), _ts(DAY - datetime.timedelta(days=1), 7))
    for start, end in (first, second):
        engine.economy_db["windows"].append({"start": start, "end": end})
        hour = start
        while hour < end:
            engine.economy_db.setdefault("prices", {})[hour] = 1.0
            hour += 3600
    engine.analysis_db["drives"].append({"start": first[1] - 4 * 3600, "end": first[1], "km": 30.0, "used": 20.0, "temp": None})
    _, result = _run(engine, snap)
    assert result.value["coverage"]["drives"] == 1 and result.value["coverage"]["drives_in_gaps"] == 0
    assert result.value["status"] == "unavailable" and result.value["reason"] == "no_drives"


def test_the_autumn_hour_copy_is_added_for_prices_stored_before_it_existed() -> None:
    import datetime as dt

    from custom_components.cable_juice_planner import const
    from custom_components.cable_juice_planner.engine import CJPEngine

    from .test_economy import OPTIONS, TEXTS
    from .test_engine import snapshot

    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    snap = snapshot(dt.datetime(2026, 11, 10, 12, 0))
    assert engine.run_job(snap, engine.startup, {}).error is None
    first = dt.datetime(2026, 10, 25, 0, tzinfo=dt.UTC).timestamp()  # 02:00 CEST, the first of the two 02:00
    engine.economy_db["prices"] = {first - 3600.0: 1.0, first: 3.0, first + 7200.0: 4.0}  # no copy: stored by an older version
    engine.get_grid_prices = lambda *args, **kwargs: {}  # type: ignore[method-assign]
    assert engine.run_job(snap, engine.economy_store_prices).error is None
    assert engine.economy_db["prices_dst_copy"] == {first + 3600.0: 3.0}
    assert first + 3600.0 not in engine.economy_db["prices"]
