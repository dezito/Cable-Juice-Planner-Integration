"""What-if Scenario B in the engine: the current settings against an alternative reserve / maximum level."""
from __future__ import annotations

from .test_economy import _engine
from .test_whatif_engine import _seed, snapshot_with


def _run(engine, snap, start=None):
    prepared = engine.run_job(snap, engine.what_if_settings_prepare)
    assert prepared.error is None, prepared.error
    result = engine.run_job(snap, engine.what_if_settings_run, start or {"soc": 50.0, "source": "entered"})
    assert result.error is None, result.error
    return prepared, result


def test_a_higher_reserve_is_replayed_against_the_current_settings() -> None:
    engine, snap = _engine()
    windows = _seed(engine)
    snap = snapshot_with(snap, {"what_if_min_daily_battery_level": 50.0})
    prepared, result = _run(engine, snap)
    assert prepared.value["first_window_start"] == min(start for start, _ in windows)
    r = result.value
    assert r["status"] == "ok" and r["comparable"] is True
    assert r["settings"] == {
        "baseline": {"reserve": 30.0, "maximum": 90.0},
        "alternative": {"reserve": 50.0, "maximum": 90.0, "sources": {"reserve": "override", "maximum": "live_setting"}},
    }
    assert r["windows"] == {"total": 5, "replayed": 4, "stopped_at": None}  # the last connection's next drive is unknown
    assert r["alternative"]["grid_kwh"] > r["baseline"]["grid_kwh"] and r["difference"]["cost"] > 0
    assert r["alternative"]["final_soc"] > r["baseline"]["final_soc"]
    assert r["difference"]["terminal_energy_kwh"] > 0 and r["difference"]["adjusted_cost"] < r["difference"]["cost"]
    assert r["baseline"]["unmet_driving_kwh"] == 0 and r["alternative"]["unmet_driving_kwh"] == 0
    assert "modelled_policy" in r["notes"] and "settings_history_not_stored" in r["notes"] and r["conservation_error_kwh"] < 1e-6
    assert [m["windows"] for m in r["months"]] == [4] and len(r["rows"]) == 4
    assert r["rows"][0]["baseline_target"] == 50.0 and r["rows"][0]["alternative_target"] == 70.0
    # published on its own sensor and saved separately from Scenario A
    assert result.states["what_if_settings"] == r["difference"]["adjusted_cost"]
    assert result.attributes["what_if_settings"]["result"]["difference"] == r["difference"]
    assert engine.economy_db["what_if_settings"]["computed_at"] == r["computed_at"]
    assert engine.economy_db["what_if_settings_attempt"]["status"] == "ok"
    assert "what_if" not in engine.economy_db


def test_a_lower_maximum_is_an_alternative_too_and_the_live_settings_are_never_changed() -> None:
    engine, snap = _engine()
    _seed(engine)
    live = (engine.get_min_daily_battery_level(), engine.get_max_recommended_charge_limit_battery_level())
    snap = snapshot_with(snap, {"what_if_max_recommended_battery_level": 40.0, "what_if_min_daily_battery_level": 5.0})
    _, result = _run(engine, snap)
    r = result.value
    assert r["settings"]["alternative"]["maximum"] == 40.0 and r["settings"]["alternative"]["sources"] == {"reserve": "override", "maximum": "override"}
    assert r["status"] == "ok"
    assert not result.settings and not getattr(result, "config_updates", None)
    assert (engine.get_min_daily_battery_level(), engine.get_max_recommended_charge_limit_battery_level()) == live


def test_the_same_settings_are_not_a_comparison() -> None:
    engine, snap = _engine()
    _seed(engine)
    _, result = _run(engine, snap)  # both overrides are 0: "the live setting"
    r = result.value
    assert r["status"] == "unavailable" and r["reason"] == "no_change" and r["baseline"] is None
    assert "what_if_settings" not in engine.economy_db
    assert result.states["what_if_settings"] is None
    # an override equal to the live value is the same thing
    _, equal = _run(engine, snapshot_with(snap, {"what_if_min_daily_battery_level": 30.0}))
    assert equal.value["reason"] == "no_change"


def test_a_missing_start_level_calibrates_both_settings_and_a_failed_run_keeps_the_saved_result() -> None:
    engine, snap = _engine()
    _seed(engine)
    snap = snapshot_with(snap, {"what_if_min_daily_battery_level": 50.0})
    _, first = _run(engine, snap)
    saved = engine.economy_db["what_if_settings"]
    _, estimated = _run(engine, snap, {"soc": None, "source": "unavailable"})
    assert estimated.value["status"] == "ok" and estimated.value["calibrated_start"] is True and estimated.value["method"] == "estimated"
    _, failed = _run(engine, snap, {"soc": 50.0, "source": "entered", "first_window_start": 1.0})
    assert failed.value["status"] == "unavailable" and failed.value["reason"] == "start_level_stale"
    assert engine.economy_db["what_if_settings"] is not saved  # the estimated run replaced it, the failed one did not
    assert failed.states["what_if_settings"] == estimated.value["difference"]["adjusted_cost"]
    assert failed.attributes["what_if_settings"]["attempt"]["reason"] == "start_level_stale"
    assert first.value["method"] == "strict"


def test_no_recorded_drive_that_can_be_applied_means_unknown_demand() -> None:
    engine, snap = _engine()
    _seed(engine)
    engine.analysis_db["drives"].clear()
    _, result = _run(engine, snapshot_with(snap, {"what_if_min_daily_battery_level": 50.0}))
    assert result.value["status"] == "unavailable" and result.value["reason"] == "no_drives"


def test_a_connection_without_prices_is_estimated_and_labelled() -> None:
    engine, snap = _engine()
    _seed(engine, skip_price_day=3)
    _, result = _run(engine, snapshot_with(snap, {"what_if_min_daily_battery_level": 50.0}))
    r = result.value
    assert r["status"] == "ok" and r["method"] == "estimated" and r["estimated_windows"] == 1 and r["price_estimate"] is not None
    assert result.states["what_if_settings"] == r["difference"]["adjusted_cost"]


def test_scenario_a_and_b_keep_their_own_results() -> None:
    engine, snap = _engine()
    _seed(engine)
    snap = snapshot_with(snap, {"what_if_min_daily_battery_level": 50.0})
    engine.run_job(snap, engine.what_if_prepare)
    a = engine.run_job(snap, engine.what_if_run, {"soc": 50.0, "source": "entered"})
    _, b = _run(engine, snap)
    assert a.value["status"] == "ok" and b.value["status"] == "ok"
    assert engine.economy_db["what_if"]["computed_at"] == a.value["computed_at"]
    assert engine.economy_db["what_if_settings"]["settings"]["alternative"]["reserve"] == 50.0
    assert "smart" in engine.economy_db["what_if"] and "smart" not in engine.economy_db["what_if_settings"]


def test_a_run_that_was_interrupted_is_shown_as_such_after_a_restart() -> None:
    engine, snap = _engine()
    engine.economy_db["what_if_settings_attempt"] = {"status": "running", "reason": None, "at": 1.0}
    engine.economy_db["what_if_settings"] = "not a result"
    engine.load_what_if_settings()
    assert engine.economy_db["what_if_settings_attempt"]["reason"] == "interrupted"
    assert "what_if_settings" not in engine.economy_db


def test_a_stale_start_level_is_rejected() -> None:
    engine, snap = _engine()
    _seed(engine)
    snap = snapshot_with(snap, {"what_if_min_daily_battery_level": 50.0})
    result = engine.run_job(snap, engine.what_if_settings_run, {"soc": 50.0, "source": "entered", "first_window_start": 1.0})
    assert result.value["reason"] == "start_level_stale"
