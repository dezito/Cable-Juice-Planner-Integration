"""What-if Scenario A in the engine: stored data in, published result out, nothing live changed."""
from __future__ import annotations

import datetime

import pytest

from .test_economy import _engine, _ts
from .test_engine import DAY, TZ

LOCAL_START = 18  # plugged in at 18:00
LOCAL_END = 7  # unplugged at 07:00 the next day
# Fill up on: the planned departure of "smart" is 07:00 with it (the work plan's departure otherwise)
PLAN = {"fill_up": True}


def _seed(engine, days=range(1, 6), skip_price_day: int | None = None, public: bool = True, charge: bool = True) -> list[tuple[float, float]]:
    """Five evening-to-morning connections with cheap hours 02-04, a 20 % drive after each and what CJP charged in each:
    20 % in the cheap hours 02-05 (so the car comes home with the same level every day)."""
    windows = []
    prices = engine.economy_db.setdefault("prices", {})
    loss = abs(float(engine.config["charger"]["charging_loss"]))
    kwh = 20.0 / 100.0 * float(engine.config["ev_car"]["battery_size"]) * (1.0 + loss)
    for back in days:
        day = DAY - datetime.timedelta(days=back)
        start, end = _ts(day, LOCAL_START), _ts(day + datetime.timedelta(days=1), LOCAL_END)
        windows.append((start, end))
        window = {"start": start, "end": end}
        if charge:
            # the record stored with the connection when it was settled (engine/whatif/recorded.py): hour by hour at 0.3
            window["record"] = {"kwh": kwh, "percent": 20.0, "local_kwh": 0.0, "cost": kwh * 0.3, "booked_cost": kwh * 0.3}
        engine.economy_db["windows"].append(window)
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
    # the car has 50 % at the first plug-in and 70 % at the last unplug: 20 % charged and 20 % driven in every connection
    result = engine.run_job(snap, engine.what_if_run, start or {"soc": 50.0, "source": "entered", "end_soc": 70.0, "end_source": "recorded_state"})
    assert result.error is None, result.error
    return prepared, result


def test_scenario_a_compares_what_cjp_charged_with_smart_and_asap_and_publishes_it() -> None:
    engine, snap = _engine(PLAN)
    windows = _seed(engine)
    prepared, result = _run(engine, snap)
    assert prepared.value["first_window_start"] == min(start for start, _ in windows)
    assert prepared.value["last_window_end"] == max(end for _, end in windows)  # where the level at the end is read
    r = result.value
    assert r["status"] == "ok" and r["comparable"] is True and r["method"] == "strict"
    assert r["windows"]["total"] == 5 and r["windows"]["replayed"] == 5
    assert r["target"] == {"battery_level": 90.0, "source": "live_setting"}
    assert r["provenance"]["start_soc"] == "entered" and r["provenance"]["end_soc"] == "recorded_state"
    # CJP charged 20 % in the cheap hours every night; smart and asap fill up to the target from the level the car came home with
    assert r["cjp"]["final_soc"] == pytest.approx(70.0) and r["smart"]["final_soc"] == pytest.approx(90.0) and r["asap"]["final_soc"] == pytest.approx(90.0)
    assert r["cjp"]["cost"] < r["smart"]["cost"] < r["asap"]["cost"]
    # the same driving: the recorded 20 % after four connections, and nothing more to balance (the record fits the drives)
    assert r["energy"]["gap_percent"] == pytest.approx(0.0, abs=0.01) and r["energy"]["charged_percent"] == pytest.approx(100.0)
    assert r["energy"]["driven_percent"] == pytest.approx(80.0)
    assert r["coverage"]["drives"] == 4 and r["coverage"]["recorded_windows"] == 5 and r["coverage"]["recorded_left_out"] == 0
    assert r["warnings"] == [] and r["conservation_error_kwh"] < 1e-6
    assert {"recorded_charging", "grid_equivalent_cost", "smart_planned_departure", "energy_balanced_to_cjp"} <= set(r["notes"])
    assert [m["windows"] for m in r["months"]] == [5] and r["months"][0]["cjp_cost"] == pytest.approx(r["cjp"]["cost"], abs=0.01)
    # three differences, the headline one is CJP against charging at once, valued with what the one that holds more energy paid
    assert set(r["differences"]) == {"cjp_asap", "cjp_smart", "smart_asap"} and r["difference"] == r["differences"]["cjp_asap"]
    assert r["differences"]["cjp_asap"]["cost"] < 0 and r["differences"]["cjp_asap"]["adjusted_cost"] is not None
    # the price per kWh of each: CJP and Smart buy in the cheap hours (0.3), charging at once from the plug-in (1.0)
    unit = r["unit_prices"]
    assert unit["cjp"] == pytest.approx(0.3, abs=0.01) and unit["asap"] == pytest.approx(1.0, abs=0.01) and unit["smart"] < unit["asap"]
    assert unit["cjp_kwh"] == pytest.approx(r["cjp"]["grid_kwh"], abs=0.01) and unit["cjp_vs_asap"] < 0
    # the same indication per month (one month here: it is the figure of the whole period)
    month = r["months"][0]
    assert month["cjp_vs_asap_unit"] == pytest.approx(unit["cjp_vs_asap"], abs=0.01) and month["cjp_vs_smart_unit"] == pytest.approx(unit["cjp_vs_smart"], abs=0.01)
    assert month["smart_grid_kwh"] == pytest.approx(r["smart"]["grid_kwh"], abs=0.01) and month["asap_grid_kwh"] == pytest.approx(r["asap"]["grid_kwh"], abs=0.01)
    # published on the sensor and saved for the next restart
    assert result.attributes["what_if"]["result"]["difference"] == r["difference"]
    assert result.states["what_if"] == r["difference"]["adjusted_cost"]
    assert engine.economy_db["what_if"]["computed_at"] == r["computed_at"] and engine.economy_db["what_if_attempt"]["status"] == "ok"


def test_the_three_steps_give_the_same_result_as_one_run() -> None:
    """what_if_run = collect (quick job) + the replay (the coordinator runs it on its own thread) + finish (quick job)."""
    from custom_components.cable_juice_planner.engine.whatif.replay import replay

    engine, snap = _engine(PLAN)
    _seed(engine)
    start = {"soc": 50.0, "source": "entered", "end_soc": 70.0, "end_source": "recorded_state"}
    one = _run(engine, snap, start)[1].value
    collected = engine.run_job(snap, engine.what_if_collect, start)
    assert collected.error is None and not (collected.states or collected.attributes or collected.db_saves)  # nothing is published yet
    prepared = collected.value
    assert prepared["result"] is None and prepared["calculation"].recorded
    result = replay(prepared["calculation"])  # on any thread: it holds copies only
    finished = engine.run_job(snap, engine.what_if_finish, prepared, result)
    assert finished.error is None
    for key in ("cjp", "smart", "asap", "difference", "differences", "energy"):
        assert finished.value[key] == one[key], key
    assert finished.states["what_if"] == one["difference"]["adjusted_cost"]


def test_collect_says_why_there_is_nothing_to_replay() -> None:
    engine, snap = _engine(PLAN)
    _seed(engine)
    engine.analysis_db["drives"].clear()
    prepared = engine.run_job(snap, engine.what_if_collect, {"soc": 50.0, "source": "entered"}).value
    assert prepared["calculation"] is None and prepared["result"].status == "unavailable" and prepared["result"].reason == "no_drives"


def test_the_progress_is_shown_on_the_sensor_while_running_and_never_saved() -> None:
    engine, snap = _engine(PLAN)
    _seed(engine)
    assert engine.run_job(snap, engine.what_if_progress, 3, 9).attributes == {}  # nothing is running: nothing is shown
    engine.run_job(snap, engine.what_if_prepare)  # marks the attempt as running
    saved_before = repr(engine.economy_db["what_if_attempt"])
    result = engine.run_job(snap, engine.what_if_progress, 3, 9)
    assert result.attributes["what_if"]["attempt"]["status"] == "running"
    assert result.attributes["what_if"]["attempt"]["progress"] == {"done": 3, "total": 9}
    assert repr(engine.economy_db["what_if_attempt"]) == saved_before and "economy" not in result.db_saves


def test_without_a_record_of_what_cjp_charged_there_is_nothing_to_compare() -> None:
    engine, snap = _engine(PLAN)
    _seed(engine, charge=False, public=False)  # no charging history at all
    r = _run(engine, snap)[1].value
    assert r["status"] == "unavailable" and r["reason"] == "no_recorded_charging" and r["cjp"] is None and "what_if" not in engine.economy_db


def test_a_connection_without_a_complete_record_ends_the_stretch_and_the_rest_is_counted_as_left_out() -> None:
    engine, snap = _engine(PLAN)
    windows = _seed(engine, public=False)
    # the third newest connection has no stored record (settled before the record existed, or it could not be priced):
    # nothing older can be compared, and the rest is counted
    third_newest = sorted(engine.economy_db["windows"], key=lambda w: w["start"])[2]
    del third_newest["record"]
    prepared, result = _run(engine, snap)
    r = result.value
    assert r["status"] == "ok" and r["windows"]["total"] == 2 and r["coverage"]["recorded_left_out"] == 3
    assert r["coverage"]["recorded_reason"] == "no_stored_record" and "windows_before_recorded_gap_left_out" in r["warnings"]
    assert prepared.value["first_window_start"] == sorted(start for start, _ in windows)[3]  # the level is read at the first one kept


def test_a_connection_that_is_not_settled_yet_is_left_aside_and_does_not_break_the_stretch() -> None:
    engine, snap = _engine(PLAN)
    _seed(engine, public=False)
    newest = max(engine.economy_db["windows"], key=lambda w: w["start"])
    newest["end"] = _ts(DAY, 11)  # unplugged an hour ago: the last hour of its charging may still change
    del newest["record"]
    r = _run(engine, snap, {"soc": 50.0, "source": "entered"})[1].value
    assert r["status"] == "ok" and r["windows"]["total"] == 4 and r["coverage"]["recorded_left_out"] == 0
    assert "windows_before_recorded_gap_left_out" not in r["warnings"]


def test_an_invalid_stored_record_ends_the_stretch() -> None:
    engine, snap = _engine(PLAN)
    _seed(engine, public=False)
    sorted(engine.economy_db["windows"], key=lambda w: w["start"])[2]["record"]["kwh"] = float("nan")
    r = _run(engine, snap)[1].value
    assert r["status"] == "ok" and r["windows"]["total"] == 2 and r["coverage"]["recorded_reason"] == "invalid_record"


def test_cjp_is_priced_with_the_stored_hourly_cost_not_the_price_cjp_booked_for_the_session() -> None:
    engine, snap = _engine(PLAN)
    _seed(engine)
    for window in engine.economy_db["windows"]:
        window["record"]["booked_cost"] = 99.0  # one price for the whole session, and solar at CJP's own value: not comparable
    r = _run(engine, snap)[1].value
    kwh = r["cjp"]["grid_kwh"]
    assert r["cjp"]["cost"] == pytest.approx(kwh * 0.3, abs=0.05)  # every kWh at the hourly price of 02-05
    assert r["recorded_reference"]["cost"] == pytest.approx(5 * 99.0)  # what CJP booked is kept as a labelled reference only


def test_solar_and_home_battery_energy_is_priced_at_the_grid_price_and_the_share_is_said() -> None:
    engine, snap = _engine(PLAN)
    _seed(engine)
    for window in engine.economy_db["windows"]:
        window["record"]["local_kwh"] = window["record"]["kwh"] / 2  # half of what was charged came from solar
    r = _run(engine, snap)[1].value
    assert "recorded_solar_share" in r["warnings"] and r["energy"]["local_kwh"] == pytest.approx(r["energy"]["kwh"] / 2, abs=0.05)
    assert r["cjp"]["cost"] == pytest.approx(r["cjp"]["grid_kwh"] * 0.3, abs=0.05)  # all kWh, none of it free


def test_without_the_level_at_the_end_the_three_are_not_known_to_be_balanced() -> None:
    engine, snap = _engine(PLAN)
    _seed(engine)
    r = _run(engine, snap, {"soc": 50.0, "source": "entered"})[1].value
    assert r["status"] == "ok" and "energy_not_balanced" in r["warnings"] and r["provenance"]["end_soc"] == "unavailable"
    assert r["energy"]["gap_percent"] is None and r["difference"]["cost"] is not None
    assert r["difference"]["adjusted_cost"] is None and r["differences"]["cjp_smart"]["adjusted_cost"] is None
    assert _run(engine, snap, {"soc": 50.0, "source": "entered"})[1].states["what_if"] is None  # no headline figure without it


def test_the_energy_cjp_bought_that_no_drive_explains_is_driving_in_all_three() -> None:
    engine, snap = _engine(PLAN)
    _seed(engine)
    # the car has 50 % at the end where the record and the drives give 70 %: 20 % went on small trips and standby drain
    r = _run(engine, snap, {"soc": 50.0, "source": "entered", "end_soc": 50.0, "end_source": "recorded_state"})[1].value
    assert r["energy"]["gap_percent"] == pytest.approx(20.0)
    assert r["cjp"]["final_soc"] == pytest.approx(50.0)
    assert "energy_not_balanced" not in r["warnings"] and r["status"] == "ok"


def test_the_planned_departure_is_the_work_plan_or_seven_with_fill_up_and_never_after_the_real_unplug() -> None:
    from custom_components.cable_juice_planner.engine.whatif.replay import Window

    thursday = {**PLAN, "workday_thursday": True, "workday_departure_thursday": datetime.time(5, 30)}
    engine, snap = _engine(thursday)
    window = Window(_ts(DAY - datetime.timedelta(days=1), 18), _ts(DAY, 8))  # leaves Thursday 08:00
    deadline = engine.run_job(snap, engine._what_if_deadline, window).value  # noqa: SLF001
    assert deadline == _ts(DAY, 5) + 1800.0  # the work plan's departure on the day the car leaves
    # a workday that is switched off: Fill up is 07:00 (inside this connection), without it the charger is told nothing
    engine, snap = _engine(PLAN)
    assert engine.run_job(snap, engine._what_if_deadline, window).value == _ts(DAY, 7)  # noqa: SLF001
    engine, snap = _engine()
    assert engine.run_job(snap, engine._what_if_deadline, window).value is None  # noqa: SLF001
    # a connection that ends before the departure, or begins after it, has no deadline: the real unplug is the limit
    engine, snap = _engine(PLAN)
    early = Window(_ts(DAY - datetime.timedelta(days=1), 18), _ts(DAY, 6))
    late = Window(_ts(DAY, 8), _ts(DAY, 17))
    assert engine.run_job(snap, engine._what_if_deadline, early).value is None  # noqa: SLF001
    assert engine.run_job(snap, engine._what_if_deadline, late).value is None  # noqa: SLF001


def test_a_year_is_estimated_from_less_and_said_so_and_not_from_too_little() -> None:
    engine, snap = _engine({**PLAN, "what_if_days": 400})
    _seed(engine, days=range(1, 6))
    short = _run(engine, snap)[1].value
    assert short["annual"]["available"] is False and short["annual"]["basis_days"] < 30  # five connections say nothing about a year
    engine, snap = _engine({**PLAN, "what_if_days": 400})
    _seed(engine, days=range(1, 91), public=False)  # three months
    r = _run(engine, snap, {"soc": 50.0, "source": "entered", "end_soc": 70.0, "end_source": "recorded_state"})[1].value
    annual = r["annual"]
    assert annual["available"] is True and annual["method"] == "estimated" and 80 < annual["basis_days"] < 95 and annual["months_covered"] <= 4
    # only the adjusted differences are scaled (the raw costs end at different battery levels), by the days covered
    assert "cjp" not in annual and annual["scale"] == pytest.approx(365.0 / annual["basis_days"], abs=0.01)
    for key, name in (("cjp_vs_asap", "cjp_asap"), ("cjp_vs_smart", "cjp_smart"), ("smart_vs_asap", "smart_asap")):
        assert annual[key] == pytest.approx(r["differences"][name]["adjusted_cost"] * annual["scale"], abs=1.0), key
    assert annual["cjp_vs_asap"] < 0  # CJP charges in the cheap hours, charging at once from the plug-in
    # without the level at the last unplug nothing is adjusted and there is no year
    unbalanced = _run(engine, snap, {"soc": 50.0, "source": "entered"})[1].value["annual"]
    assert unbalanced["available"] is False and unbalanced["reason"] == "not_balanced"
    engine, snap = _engine({**PLAN, "what_if_days": 400})
    _seed(engine, days=range(1, 381), public=False)  # more than a year: the last year as it was
    long = _run(engine, snap, {"soc": 50.0, "source": "entered", "end_soc": 70.0, "end_source": "recorded_state"})[1].value
    assert long["annual"]["method"] == "measured" and long["annual"]["basis_days"] >= 360


def test_a_saved_result_of_an_earlier_model_is_not_shown_under_the_new_labels() -> None:
    engine, snap = _engine(PLAN)
    _seed(engine)
    _run(engine, snap)
    assert engine.run_job(snap, engine.load_what_if).attributes["what_if"]["result"] is not None  # the current model: shown
    engine.economy_db["what_if"]["model_version"] = 5  # the planner replay
    loaded = engine.run_job(snap, engine.load_what_if)
    assert loaded.attributes["what_if"]["result"] is None and "what_if" not in engine.economy_db and "economy" in loaded.db_saves
    engine.economy_db["what_if"] = {"status": "ok"}  # saved before the version existed
    engine.run_job(snap, engine.load_what_if)
    assert "what_if" not in engine.economy_db


def test_scenario_a_changes_nothing_live() -> None:
    engine, snap = _engine(PLAN)
    _seed(engine)
    settings = dict(snap.settings)
    databases = repr(engine._databases())  # noqa: SLF001
    plan_before = repr(getattr(engine, "charging_plan", None))
    _, result = _run(engine, snap)
    assert not result.settings and not getattr(result, "config_updates", None)
    assert snap.settings == settings and repr(getattr(engine, "charging_plan", None)) == plan_before
    assert not (result.events or result.notifications or result.mobile_notifications or result.site)
    assert engine.charger_action == "stop" and engine.charger_stop_request == 0
    assert set(result.db_saves) <= {"economy"} and databases != repr(engine._databases())  # noqa: SLF001  # only its own result is saved


def test_scenario_a_without_a_start_battery_level_is_unavailable_never_defaulted() -> None:
    """CJP's own record can only be followed from a known level: an unknown start is not replaced by a default."""
    engine, snap = _engine(PLAN)
    _seed(engine)
    _, result = _run(engine, snap, {"soc": None, "source": "unavailable"})
    r = result.value
    assert r["status"] == "unavailable" and r["reason"] == "no_start_soc" and r["cjp"] is None
    assert "what_if" not in engine.economy_db


def test_a_failed_calculation_keeps_the_previous_saved_result() -> None:
    engine, snap = _engine(PLAN)
    _seed(engine)
    _, first = _run(engine, snap)
    saved = engine.economy_db["what_if"]
    _, failed = _run(engine, snap, {"soc": 50.0, "source": "entered", "first_window_start": 1.0})  # a connection ended meanwhile
    assert failed.value["status"] == "unavailable" and failed.value["reason"] == "start_level_stale"
    assert engine.economy_db["what_if"] is saved
    assert failed.states["what_if"] == first.value["difference"]["adjusted_cost"]  # the saved result is still shown
    assert failed.attributes["what_if"]["result"]["computed_at"] == first.value["computed_at"]
    assert failed.attributes["what_if"]["attempt"]["status"] == "unavailable"


def _seed_with_an_unpriced_evening():
    """A connection whose evening hour has no price, where CJP did not charge: CJP's own record is complete, but smart and asap
    have no price for that hour (they would have charged in it) and the replay estimates it."""
    engine, snap = _engine(PLAN)
    _seed(engine)
    del engine.economy_db["prices"][_ts(DAY - datetime.timedelta(days=3), 20)]
    return engine, snap


def test_a_connection_with_an_hour_without_a_price_is_estimated_for_smart_and_asap_and_reported() -> None:
    engine, snap = _seed_with_an_unpriced_evening()
    _, result = _run(engine, snap)
    r = result.value
    assert r["status"] == "ok" and r["method"] == "estimated" and r["estimated_windows"] == 1
    assert r["windows"]["replayed"] == r["windows"]["total"] == 5 and r["price_estimate"] is not None
    assert "prices_estimated_windows" in r["warnings"] and r["coverage"]["price_hours_missing"] > 0
    assert "recorded_prices_estimated_lean_cjp" in r["warnings"]  # CJP keeps its real hourly prices there: said to lean its way
    assert r["coverage"]["recorded_left_out"] == 0  # CJP's own record needs no price in that hour


def test_a_connection_settled_since_the_daily_update_is_recorded_when_the_what_if_runs() -> None:
    """The record is made from the charging history as it was recorded (it is combined per day after a week); the daily
    economy update only runs at night, so the What-if makes it for a connection that settled in the meantime."""
    engine, snap = _engine(PLAN)
    _seed(engine, charge=False, public=False)
    kwh = 20.0 / 100.0 * float(engine.config["ev_car"]["battery_size"]) * (1.0 + abs(float(engine.config["charger"]["charging_loss"])))
    for back in range(1, 6):  # 20 % in the cheap hours 02-05 of the morning the car is unplugged
        engine.charging_history_db[datetime.datetime.combine(DAY - datetime.timedelta(days=back - 1), datetime.time(2))] = {
            "kWh": kwh, "kWh_from_grid": kwh, "kWh_from_local_energy": 0.0, "percentage": 20.0, "cost": kwh * 0.3,
            "ended": "05:00", "charging_type": ["planned"],
        }
    assert all("record" not in w for w in engine.economy_db["windows"])
    _, result = _run(engine, snap)
    r = result.value
    assert r["status"] == "ok" and r["windows"]["total"] == 5 and r["coverage"]["recorded_left_out"] == 0
    assert all("record" in w for w in engine.economy_db["windows"])
    assert r["cjp"]["cost"] == pytest.approx(r["cjp"]["grid_kwh"] * 0.3, abs=0.05) and "economy" in result.db_saves


def test_the_months_are_adjusted_for_the_change_of_the_battery_level() -> None:
    engine, snap = _engine(PLAN)
    _seed(engine)
    r = _run(engine, snap)[1].value
    month = r["months"][0]
    # the three end at different levels (CJP 70 %, the others 90 %): the raw costs are not comparable, the adjusted differences are,
    # and the months of a pair add up to the adjusted difference of the whole period
    for pair, name in (("cjp_vs_asap", "cjp_asap"), ("cjp_vs_smart", "cjp_smart"), ("smart_vs_asap", "smart_asap")):
        assert month[f"{pair}_adjusted"] == pytest.approx(r["differences"][name]["adjusted_cost"], abs=0.01), pair
    # without the level at the last unplug nothing is adjusted
    unbalanced = _run(engine, snap, {"soc": 50.0, "source": "entered"})[1].value["months"][0]
    assert unbalanced["cjp_vs_asap_adjusted"] is None and unbalanced["cjp_vs_smart_adjusted"] is None and unbalanced["smart_vs_asap_adjusted"] is None


def test_a_level_from_the_statistics_mean_is_said_to_be_approximate() -> None:
    engine, snap = _engine(PLAN)
    _seed(engine)
    exact = _run(engine, snap)[1].value
    assert "levels_approximate" not in exact["warnings"]
    approx = _run(engine, snap, {"soc": 50.0, "source": "statistics_hour_mean", "end_soc": 70.0, "end_source": "recorded_state"})[1].value
    assert "levels_approximate" in approx["warnings"]


def test_a_level_read_before_a_connection_ended_meanwhile_is_not_used_at_the_end() -> None:
    engine, snap = _engine(PLAN)
    _seed(engine)
    stale = _run(engine, snap, {"soc": 50.0, "source": "entered", "end_soc": 70.0, "end_source": "recorded_state", "last_window_end": 1.0})[1].value
    assert stale["status"] == "unavailable" and stale["reason"] == "start_level_stale"


def test_prices_from_the_price_sensor_history_fill_hours_cjp_did_not_store() -> None:
    engine, snap = _engine(PLAN)
    _seed(engine)
    engine.economy_db["prices_history"] = {ts: 1.0 for ts in engine.economy_db["prices"]}
    for ts in [ts for ts in list(engine.economy_db["prices"]) if ts % 7200 == 0]:
        del engine.economy_db["prices"][ts]  # every other hour was never stored by CJP
    _, result = _run(engine, snap)
    r = result.value
    assert r["status"] == "ok" and r["method"] == "strict" and r["estimated_windows"] == 0
    assert r["coverage"]["price_hours_from_history"] > 0 and "prices_from_price_history" in r["warnings"]
    assert r["provenance"]["prices"] == "measured_partly_unverified"
    # a price CJP stored itself always wins over the history
    engine.economy_db["prices_history"] = {ts: 99.0 for ts in engine.economy_db["prices"]}
    assert _run(engine, snap)[1].value["coverage"]["price_hours_from_history"] == 0


def test_the_what_if_maximum_level_is_the_target_and_never_changes_the_live_setting() -> None:
    engine, snap = _engine(PLAN)
    _seed(engine)
    live = engine.get_max_recommended_charge_limit_battery_level()
    _, result = _run(engine, snapshot_with(snap, {"what_if_max_recommended_battery_level": 60.0}))
    assert result.value["target"] == {"battery_level": 60.0, "source": "override"}
    assert engine.get_max_recommended_charge_limit_battery_level() == live


def snapshot_with(snap, settings: dict):
    from .test_engine import snapshot

    return snapshot(datetime.datetime.combine(DAY, datetime.time(12, 0)), settings={**PLAN, **settings})


def test_day_selection_applies_smart_only_on_the_chosen_weekdays() -> None:
    engine, snap = _engine(PLAN)
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
    assert abs(weekend.value["differences"]["smart_asap"]["cost"]) < abs(everything.value["differences"]["smart_asap"]["cost"])
    # CJP's own record is what it is: the day selection is only about where "smart" is used
    assert weekend.value["cjp"] == everything.value["cjp"]
    assert weekend.value["day_selection"] == "weekend" and len(windows) == 5


def test_the_saved_result_is_published_again_after_a_restart() -> None:
    engine, snap = _engine(PLAN)
    _seed(engine)
    _, result = _run(engine, snap)
    engine.economy_db["what_if_attempt"] = {"status": "running", "reason": None, "at": 1.0}
    restarted = engine.run_job(snap, engine.load_what_if)
    assert restarted.attributes["what_if"]["result"]["computed_at"] == result.value["computed_at"]
    assert restarted.attributes["what_if"]["attempt"]["status"] == "unavailable"
    assert restarted.attributes["what_if"]["attempt"]["reason"] == "interrupted"
    assert restarted.states["what_if"] == result.value["difference"]["adjusted_cost"]


def test_an_open_connection_and_an_empty_history_are_handled() -> None:
    engine, snap = _engine(PLAN)
    engine.economy_db["windows"].append({"start": _ts(DAY, 8), "end": None})
    prepared, result = _run(engine, snap)
    assert prepared.value["first_window_start"] is None
    assert result.value["status"] == "unavailable" and result.value["reason"] == "no_windows"


def test_the_work_plan_selects_its_own_days() -> None:
    engine, snap = _engine({**PLAN, "workday_monday": True, "workday_wednesday": True})
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
    engine, snap = _engine(PLAN)
    _seed(engine)
    engine.analysis_db["drives"].clear()
    _, result = _run(engine, snap)
    assert result.value["status"] == "unavailable" and result.value["reason"] == "no_drives"
    assert result.value["smart"] is None and "what_if" not in engine.economy_db


def test_an_empty_day_selection_is_reported_instead_of_a_zero_difference() -> None:
    engine, snap = _engine(PLAN)
    _seed(engine)
    _, result = _run(engine, snapshot_with(snap, {"what_if_day_selection": "work_plan"}))  # no workday is switched on
    assert result.value["status"] == "unavailable" and result.value["reason"] == "no_days_selected"


def test_a_start_level_read_for_another_first_connection_is_not_used() -> None:
    engine, snap = _engine(PLAN)
    windows = _seed(engine)
    prepared = engine.run_job(snap, engine.what_if_prepare).value
    start = {"soc": 50.0, "source": "recorded_state", "period": prepared["period"], "first_window_start": prepared["first_window_start"]}
    ok = engine.run_job(snap, engine.what_if_run, start).value
    assert ok["status"] == "ok"
    stale = engine.run_job(snap, engine.what_if_run, {**start, "first_window_start": min(w[0] for w in windows) - 7200.0}).value
    assert stale["status"] == "unavailable" and stale["reason"] == "start_level_stale"
    # the period of the prepare is the one that is replayed, even if time has passed
    assert ok["period"]["start"] == prepared["period"][0] and ok["period"]["end"] == prepared["period"][1]


def test_an_estimated_replay_is_published_with_its_label_and_its_period_figure() -> None:
    engine, snap = _seed_with_an_unpriced_evening()
    _, result = _run(engine, snap)
    assert result.value["status"] == "ok" and result.value["difference"] is not None
    assert result.value["difference"]["measured_cost"] is not None
    assert result.states["what_if"] == result.value["difference"]["adjusted_cost"]
    assert result.attributes["what_if"]["result"]["method"] == "estimated"


def test_a_missing_battery_capacity_is_unavailable() -> None:
    engine, snap = _engine(PLAN)
    _seed(engine)
    engine.config["ev_car"]["battery_size"] = None
    _, result = _run(engine, snap)
    assert result.value["status"] == "unavailable" and result.value["reason"] == "no_battery_capacity"


def test_selected_weekdays_follow_the_what_if_day_switches() -> None:
    engine, snap = _engine(PLAN)
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
    engine, snap = _engine(PLAN)
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
    engine, snap = _engine(PLAN)
    first = (_ts(DAY - datetime.timedelta(days=3), 18), _ts(DAY - datetime.timedelta(days=2), 7))
    second = (_ts(DAY - datetime.timedelta(days=2), 18), _ts(DAY - datetime.timedelta(days=1), 7))
    for start, end in (first, second):  # CJP charged a little in each (so the record is complete)
        engine.economy_db["windows"].append({"start": start, "end": end, "record": {"kwh": 5.0, "percent": 5.0, "local_kwh": 0.0, "cost": 1.5, "booked_cost": 1.5}})
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
