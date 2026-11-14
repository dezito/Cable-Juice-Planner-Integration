"""What-if Scenario A over connections that are estimates: CJP's charging history is laid over the connections estimated from the
battery statistics (and modelled from the work plan) before the measured ones, marked as estimates, as far back as the history
reaches; the reason and the date where it stops are kept."""
from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

from custom_components.cable_juice_planner.const import WEEKDAYS

from .test_economy import _engine, _ts
from .test_engine import DAY, TZ
from .test_whatif_derived_engine import MEASURED_BACKS, OLDER_BACKS, _evening, _statistics

START = {"soc": 33.0, "source": "entered", "end_soc": 80.0, "end_source": "recorded_state"}


def _kwh(engine) -> float:
    return 20.0 / 100.0 * float(engine.config["ev_car"]["battery_size"]) * (1.0 + abs(float(engine.config["charger"]["charging_loss"])))


def _session(engine, back: int, **over) -> None:
    """What CJP charged in the connection `back` days back: 20 % at 02:00-05:00, booked at 0.3 per kWh."""
    day = DAY - datetime.timedelta(days=back)
    kwh = _kwh(engine)
    engine.charging_history_db[datetime.datetime.combine(day + datetime.timedelta(days=1), datetime.time(2))] = {
        "kWh": kwh, "kWh_from_grid": kwh, "kWh_from_local_energy": 0.0, "percentage": 20.0, "cost": kwh * 0.3, "ended": "05:00",
        "charging_type": ["planned"], **over,
    }


def _seed(engine, history_backs=(*MEASURED_BACKS, *OLDER_BACKS), marker_back: int | None = max(OLDER_BACKS) + 1, days: int = 60):
    """15 measured evenings, 25 older ones that only the statistics know, cheap hours 02-04 in all of them, and the charging
    history of CJP for the given connections plus (marker_back) one older session that shows how far the history reaches back."""
    prices = engine.economy_db.setdefault("prices", {})
    for back in (*MEASURED_BACKS, *OLDER_BACKS):
        start, end = _evening(back)
        if back in MEASURED_BACKS:
            engine.economy_db["windows"].append({"start": start, "end": end})
            engine.analysis_db["drives"].append({"start": end + 300, "end": end + 3600, "km": 40.0, "used": 20.0, "temp": None})
        hour = start
        while hour < end:
            local = datetime.datetime.fromtimestamp(hour, TZ).hour
            prices[hour] = 0.3 if 2 <= local < 5 else 1.0
            hour += 3600
    engine.economy_db["prices_averaged_since"] = _ts(DAY - datetime.timedelta(days=days), 0)
    for back in history_backs:
        _session(engine, back)
    if marker_back is not None:
        engine.charging_history_db[datetime.datetime.combine(DAY - datetime.timedelta(days=marker_back), datetime.time(12))] = {
            "kWh": 1.0, "kWh_from_grid": 1.0, "kWh_from_local_energy": 0.0, "percentage": 1.0, "cost": 0.3, "ended": "12:30",
            "charging_type": ["planned"],
        }


def _run(engine, snap, start=START):
    engine.run_job(snap, engine.store_derived_windows, {"ev_hours": _statistics((*MEASURED_BACKS, *OLDER_BACKS))})
    assert engine.economy_db["derived_windows"]["status"] == "ok"
    prepared = engine.run_job(snap, engine.what_if_prepare)
    assert prepared.error is None, prepared.error
    result = engine.run_job(snap, engine.what_if_run, {**start, "period": prepared.value["period"], "first_window_start": prepared.value["first_window_start"]})
    assert result.error is None, result.error
    return prepared.value, result.value


def test_the_history_is_laid_over_the_estimated_connections_before_the_measured_ones_and_they_are_marked() -> None:
    engine, snap = _engine({"what_if_days": 365, "fill_up": True})
    _seed(engine)
    prepared, r = _run(engine, snap)
    assert r["status"] == "ok" and r["comparable"] is True, r
    coverage = r["coverage"]
    assert coverage["recorded_estimates"] == "used" and coverage["recorded_estimated_windows"] == coverage["windows_derived"] == len(OLDER_BACKS)
    assert coverage["windows_measured"] == len(MEASURED_BACKS) and coverage["recorded_windows"] == r["windows"]["total"] == len(OLDER_BACKS) + len(MEASURED_BACKS)
    assert coverage["recorded_measured_from"] == _evening(max(MEASURED_BACKS))[0]
    assert coverage["recorded_reason"] is None and coverage["recorded_left_out"] == 0
    assert prepared["first_window_start"] == coverage["first_window"] == _evening(max(OLDER_BACKS))[0]
    # the level at the first estimated connection is the statistics', not the entered one (that belongs to a measured connection)
    assert r["provenance"]["start_soc"] == "battery_statistics" and r["provenance"]["windows"] == "measured_and_estimated_from_battery_statistics"
    assert "connections_estimated_from_battery_statistics" in r["warnings"]
    # every connection is CJP's 20 % at 0.3: the estimated ones are not left without what CJP charged
    assert r["cjp"]["cost"] == pytest.approx((len(OLDER_BACKS) + len(MEASURED_BACKS)) * _kwh(engine) * 0.3, abs=0.01)
    # marked: rows, months and the estimated part on its own
    assert [row["estimated"] for row in r["rows"]].count(True) == len(OLDER_BACKS) and all(row["modelled"] is False for row in r["rows"])
    assert sum(m["estimated_connections"] for m in r["months"]) == len(OLDER_BACKS)
    part = r["derived"]
    assert part["windows"] == len(OLDER_BACKS) and part["modelled"] == 0 and part["cjp_cost"] == pytest.approx(len(OLDER_BACKS) * _kwh(engine) * 0.3, abs=0.01)
    assert part["last"] <= coverage["recorded_measured_from"]


def test_a_year_made_of_estimated_connections_is_still_an_estimate() -> None:
    engine, snap = _engine({"what_if_days": 365, "fill_up": True})
    _seed(engine)
    _, r = _run(engine, snap)
    assert r["annual"] is not None, (r["status"], r["windows"], r["comparable"], r["comparable_reason"], r["coverage"]["first_window"], r["coverage"]["last_window"])
    assert r["annual"]["available"] is True and r["annual"]["basis_days"] < 360 and r["annual"]["method"] == "estimated"
    # at a year or more the figure is the last year ("measured") only when none of the connections is an estimate
    result = SimpleNamespace(
        cjp=object(), comparable=True, windows_replayed=5, windows_total=5,
        differences={key: {"adjusted_cost": 10.0} for key in ("cjp_asap", "cjp_smart", "smart_asap")},
    )
    year = {"first_window": 86400.0, "last_window": 401 * 86400.0}
    assert engine._what_if_annual(result, year, {}, False)["method"] == "measured"
    assert engine._what_if_annual(result, year, {}, True)["method"] == "estimated"


def test_without_the_charging_history_the_estimated_connections_stop_where_it_stops_and_the_date_and_reason_are_kept() -> None:
    engine, snap = _engine({"what_if_days": 365, "fill_up": True})
    # the history reaches back to the connection 20 days back only: the older estimated ones are "before the history"
    _seed(engine, history_backs=(*MEASURED_BACKS, *range(16, 21)), marker_back=20)
    _, r = _run(engine, snap)
    coverage = r["coverage"]
    assert r["status"] == "ok" and coverage["recorded_estimates"] == "used"
    assert coverage["recorded_reason"] == "before_history" and coverage["recorded_left_out"] > 0
    assert 0 < coverage["recorded_estimated_windows"] < len(OLDER_BACKS)
    assert coverage["recorded_stop_at"] is not None and coverage["recorded_stop_at"] < coverage["first_window"]
    assert "windows_before_recorded_gap_left_out" in r["warnings"]
    # the stretch is unbroken: the connections of the stretch are every one after the stop
    assert coverage["first_window"] > coverage["recorded_stop_at"]


def test_a_history_that_reaches_no_estimated_connection_keeps_only_the_measured_ones_and_says_why() -> None:
    engine, snap = _engine({"what_if_days": 365, "fill_up": True})
    _seed(engine, history_backs=MEASURED_BACKS, marker_back=max(MEASURED_BACKS))  # the history starts at noon of the oldest measured day
    _, r = _run(engine, snap)
    coverage = r["coverage"]
    assert r["status"] == "ok" and r["windows"]["total"] == len(MEASURED_BACKS) and r["derived"] is None
    assert coverage["recorded_estimates"] == "no_record" and coverage["recorded_estimated_windows"] == 0
    assert coverage["recorded_reason"] == "before_history" and coverage["recorded_stop_at"] is not None
    assert coverage["recorded_stop_at"] < coverage["first_window"] and r["provenance"]["start_soc"] == "entered"


def test_a_connection_in_the_middle_that_cannot_be_read_is_a_gap_and_the_estimates_before_it_are_still_compared() -> None:
    engine, snap = _engine({"what_if_days": 365, "fill_up": True})
    _seed(engine)
    _session(engine, 8, cost=None)  # a measured connection in the middle has a missing cost
    _, r = _run(engine, snap)
    coverage = r["coverage"]
    assert r["status"] == "ok" and coverage["recorded_estimates"] == "used" and coverage["recorded_estimated_windows"] == len(OLDER_BACKS)
    # one gap: two stretches, the older one with the estimated connections and the measured ones to the gap, the newer one after it
    assert coverage["recorded_skipped"] == {"invalid_history": 1} and r["segments"] == 2
    assert [s["windows"] for s in coverage["recorded_segments"]] == [len(OLDER_BACKS) + 7, 7]
    assert r["windows"]["total"] == len(OLDER_BACKS) + len(MEASURED_BACKS) - 1 and coverage["recorded_reason"] is None
    assert "recorded_segments" in r["warnings"] and "windows_before_recorded_gap_left_out" in r["warnings"]


def test_much_charging_outside_the_estimated_connections_means_they_are_not_used() -> None:
    """That energy would be missing from CJP's column: CJP would look cheaper than it was."""
    engine, snap = _engine({"what_if_days": 365, "fill_up": True})
    _seed(engine)
    for back in OLDER_BACKS:  # charging at noon, when no estimated connection exists
        day = DAY - datetime.timedelta(days=back)
        engine.charging_history_db[datetime.datetime.combine(day, datetime.time(12, 5))] = {
            "kWh": 8.0, "kWh_from_grid": 8.0, "kWh_from_local_energy": 0.0, "percentage": 10.0, "cost": 2.4, "ended": "12:40",
            "charging_type": ["planned"],
        }
    _, r = _run(engine, snap)
    coverage = r["coverage"]
    assert r["status"] == "ok" and coverage["recorded_estimates"] == "outside_charging" and coverage["recorded_estimated_windows"] == 0
    assert coverage["recorded_estimated_outside_kwh"] > 100.0 and coverage["windows_derived"] == 0 and r["derived"] is None


def test_the_statistics_without_a_validated_estimate_say_so_and_the_measured_part_is_unchanged() -> None:
    engine, snap = _engine({"what_if_days": 365, "fill_up": True})
    _seed(engine)
    engine.run_job(snap, engine.store_derived_windows, {"ev_hours": {}})  # no statistics
    prepared = engine.run_job(snap, engine.what_if_prepare)
    result = engine.run_job(snap, engine.what_if_run, {**START, "period": prepared.value["period"], "first_window_start": prepared.value["first_window_start"]})
    r = result.value
    assert r["status"] == "ok" and r["windows"]["total"] == len(MEASURED_BACKS)
    assert r["coverage"]["recorded_estimates"] == "none_available" and r["coverage"]["derived_reason"] == "no_statistics"
    assert r["provenance"]["start_soc"] == "entered" and r["derived"] is None


def _work_plan() -> dict:
    settings: dict = {"what_if_days": 40, "fill_up": True}
    for day in WEEKDAYS:
        settings.update({
            f"workday_{day}": True, f"workday_departure_{day}": datetime.time(7, 0), f"workday_homecoming_{day}": datetime.time(18, 0),
            f"workday_distance_needed_{day}": 40.0,
        })
    return settings


def test_connections_modelled_from_the_work_plan_have_no_start_level_so_the_first_one_calibrates_all_three() -> None:
    engine, snap = _engine(_work_plan())
    _seed(engine, history_backs=(*MEASURED_BACKS, *range(16, 31)), marker_back=31, days=60)
    # every evening up to 30 days back is a measured or a modelled connection; there are no statistics
    prices = engine.economy_db["prices"]
    for back in range(16, 31):
        start, end = _evening(back)
        hour = start
        while hour < end:
            prices[hour] = 0.3 if 2 <= datetime.datetime.fromtimestamp(hour, TZ).hour < 5 else 1.0
            hour += 3600
    prepared = engine.run_job(snap, engine.what_if_prepare)
    assert prepared.error is None, prepared.error
    result = engine.run_job(snap, engine.what_if_run, {**START, "period": prepared.value["period"], "first_window_start": prepared.value["first_window_start"]})
    assert result.error is None, result.error
    r = result.value
    assert r["status"] == "ok", r
    coverage = r["coverage"]
    assert coverage["modelled_status"] == "ok" and coverage["windows_modelled"] > 0 and coverage["recorded_estimates"] == "used"
    # the modelled connections further back than any price was stored are gaps in the data, not the end of the comparison
    assert 0 < coverage["recorded_estimated_windows"] <= coverage["windows_modelled"] and r["derived"]["modelled"] == coverage["recorded_estimated_windows"]
    assert sum(coverage["recorded_skipped"].values()) == coverage["windows_modelled"] - coverage["recorded_estimated_windows"]
    assert "connections_modelled_from_work_plan" in r["warnings"] and "start_level_unknown_calibrated" in r["warnings"]
    assert r["provenance"]["start_soc"] == "unavailable" and r["calibrated_start"] is True and r["method"] == "estimated"
    # the first connection only brings all three to the same level: nothing CJP charged in it is counted anywhere
    total = r["windows"]["total"]
    assert r["energy"]["charged_percent"] == pytest.approx(20.0 * (total - 1)) and r["energy"]["start_soc"] == r["target"]["battery_level"]
    assert r["cjp"]["cost"] == pytest.approx((total - 1) * _kwh(engine) * 0.3, abs=0.01)
    assert r["rows"][0]["estimated"] is True and r["rows"][0]["modelled"] is True and r["rows"][-1]["modelled"] is False
    assert r["annual"] is None or r["annual"]["method"] == "estimated"


def test_a_measured_connection_that_is_not_compared_blocks_the_model_over_its_time() -> None:
    """A measured connection before the period is a time that is recorded: nothing is modelled over it, as in the settings comparison."""
    engine, snap = _engine(_work_plan())
    _seed(engine, history_backs=(*MEASURED_BACKS, *range(16, 31)), marker_back=31, days=60)
    old_start, old_end = _evening(100)
    engine.economy_db["windows"].insert(0, {"start": old_start, "end": old_end})  # outside the 40 days
    prepared = engine.run_job(snap, engine.what_if_prepare)
    result = engine.run_job(snap, engine.what_if_run, {**START, "period": prepared.value["period"], "first_window_start": prepared.value["first_window_start"]})
    r = result.value
    assert r["status"] == "ok" and r["coverage"]["windows_modelled"] == 0 and r["coverage"]["recorded_estimates"] == "none_available"
    assert r["windows"]["total"] == len(MEASURED_BACKS)


def test_estimated_connections_without_a_stored_price_say_that_and_not_that_nothing_exists() -> None:
    engine, snap = _engine({"what_if_days": 365, "fill_up": True})
    _seed(engine)
    for back in OLDER_BACKS:  # no price is made up for a connection that is itself an estimate
        start, end = _evening(back)
        for hour in range(int(start), int(end) + 1, 3600):
            engine.economy_db["prices"].pop(float(hour), None)
    _, r = _run(engine, snap)
    assert r["status"] == "ok" and r["coverage"]["recorded_estimates"] == "no_prices" and r["windows"]["total"] == len(MEASURED_BACKS)
    # and where the prices end: how many estimated connections that left out, and when the newest one without a price ends
    assert r["coverage"]["estimated_unpriced_left_out"] > 0 and r["coverage"]["estimated_unpriced_until"] is not None


def test_one_estimated_connection_without_a_price_does_not_stop_the_older_ones() -> None:
    """The comparison goes as far back as the period asked for: a small gap in the prices is a gap, not the end."""
    engine, snap = _engine({"what_if_days": 365, "fill_up": True})
    _seed(engine)
    start, end = _evening(30)  # in the middle of the estimated connections
    engine.economy_db["prices"].pop(float(start + 3600 * 3), None)
    _, r = _run(engine, snap)
    coverage = r["coverage"]
    assert r["status"] == "ok" and coverage["recorded_estimates"] == "used"
    assert coverage["recorded_skipped"] == {"no_prices": 1} and r["segments"] == 2
    assert coverage["recorded_estimated_windows"] == len(OLDER_BACKS) - 1  # all the others, also the ones before the gap
    assert [s["windows"] for s in coverage["recorded_segments"]] == [10, 14 + len(MEASURED_BACKS)]  # 40..31 back, then 29..16 and the measured
    assert coverage["first_window"] == _evening(max(OLDER_BACKS))[0]
    assert "estimated_connections_without_prices_left_out" in r["warnings"]


def test_a_stretch_that_ends_with_an_estimated_connection_has_the_level_of_the_statistics_at_its_end() -> None:
    """The unplug time of an estimated connection is an estimate: a level read there would belong to the drive after it."""
    engine, snap = _engine({"what_if_days": 365, "fill_up": True})
    _seed(engine)
    _session(engine, max(MEASURED_BACKS), cost=None)  # the oldest measured connection cannot be read: the estimates end there
    engine.run_job(snap, engine.store_derived_windows, {"ev_hours": _statistics((*MEASURED_BACKS, *OLDER_BACKS))})
    prepared = engine.run_job(snap, engine.what_if_prepare)
    level = {"soc": 33.0, "source": "entered", "end_soc": 80.0, "end_source": "recorded_state"}
    collected = engine.run_job(snap, engine.what_if_collect, {**level, "levels": [level, level], "period": prepared.value["period"], "segments": prepared.value["segments"]}).value
    first, second = collected["calculations"]
    assert first.windows[-1].derived and first.provenance["end_soc"] == "battery_statistics" and first.end_soc is not None
    assert first.provenance["start_soc"] == "battery_statistics" and not second.windows[0].derived and second.provenance["start_soc"] == "entered"
    assert second.provenance["end_soc"] == "recorded_state"
