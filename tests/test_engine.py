"""Cable Juice Planner's algorithm without Home Assistant: the worker jobs on a hand made Snapshot.

The scenarios set the time of day, the car's battery level, the charger status and the
dashboard settings directly, so a whole day can be checked in milliseconds.
"""
from __future__ import annotations

import datetime
import json
import math
import zoneinfo
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from astral import Observer

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.utils.codec import decode
from custom_components.cable_juice_planner.engine.control.preheat import preheat_lead_time
from custom_components.cable_juice_planner.engine import CJPEngine, Snapshot
from custom_components.cable_juice_planner.engine.planner.charge_plan import ChargePlan
from custom_components.cable_juice_planner.engine.planner.planner import Planner
from custom_components.cable_juice_planner.engine.pricing import Pricing
from custom_components.cable_juice_planner.engine.control.schedule import ControlSchedule

TZ = zoneinfo.ZoneInfo("Europe/Copenhagen")
ASTRAL = Observer(57.02, 8.68, 0)
DAY = datetime.date(2026, 10, 22)  # a Thursday
EXPENSIVE_HOURS = set(range(17, 21))

OPTIONS = {
    **const.default_options(),
    "status_entity_id": "sensor.charger_status",
    "power_consumtion_entity_id": "sensor.charger_power",
    "kwh_meter_entity_id": "sensor.charger_kwh",
    "lifetime_kwh_meter_entity_id": "sensor.charger_lifetime_kwh",
    "usable_battery_level_entity_id": "sensor.ev_battery_level",
    "estimated_battery_range_entity_id": "sensor.ev_range",
    "odometer_entity_id": "sensor.ev_odometer",
    "charge_port_door_entity_id": "binary_sensor.ev_charge_port",
    "charging_limit_entity_id": "number.ev_charge_limit",
    "location_entity_id": "device_tracker.ev",
    "power_consumption_entity_id": "sensor.consumption",
    "energidataservice_buy_entity_id": "sensor.price",
    "power_voltage": 230.0,
    "charging_phases": 3.0,
    "charging_max_amp": 16.0,
    "battery_size": 75.0,
}
MAX_WATT = 230 * 3 * 16


@pytest.mark.parametrize(("thawing", "expected"), [(False, 15), (True, 30)])
def test_preheat_lead_time_doubles_for_frost(thawing: bool, expected: int) -> None:
    assert preheat_lead_time(15, thawing) == expected


def _texts() -> dict[str, str]:
    path = Path(__file__).parents[1] / "custom_components" / "cable_juice_planner" / "texts" / "da.json"
    texts: dict[str, str] = {}

    def flatten(data: dict, prefix: str = "") -> None:
        for key, value in data.items():
            if isinstance(value, dict):
                flatten(value, f"{prefix}{key}.")
            else:
                texts[f"{prefix}{key}"] = value

    flatten(json.loads(path.read_text(encoding="utf-8")))
    return texts


TEXTS = _texts()


def default_settings() -> dict:
    """The values of the setting entities right after the first setup (car configured, no solar)."""
    settings: dict = {key: info["default"] for key, info in const.SWITCH_SETTINGS.items() if info.get("requires") is None}
    settings.update(
        {key: float(info["default"]) for key, info in const.NUMBER_SETTINGS.items() if info.get("requires") in (None, "ev", "public_charging")}
    )
    settings.update({key: info["default"] for key, info in const.SELECT_SETTINGS.items()})
    settings.update({key: datetime.time.fromisoformat(info["default"]) for key, info in const.TIME_SETTINGS.items()})
    settings.update(dict.fromkeys(const.DATETIME_SETTINGS))
    return settings


def _price_rows(day: datetime.date, base: float) -> list[dict]:
    rows = []
    for hour in range(24):
        price = base + (1.5 if hour in EXPENSIVE_HOURS else 0.6 if 7 <= hour <= 16 else 0.0) + hour / 100
        rows.append({"hour": datetime.datetime.combine(day, datetime.time(hour), tzinfo=TZ).isoformat(), "price": round(price, 3)})
    return rows


def snapshot(
    now: datetime.datetime,
    *,
    level: float = 40,
    status: str = "disconnected",
    port: str = "off",
    power: float = 0,
    settings: dict | None = None,
    site: dict | None = None,
) -> Snapshot:
    tomorrow = now.date() + datetime.timedelta(days=1)
    price = {
        "raw_today": _price_rows(now.date(), 1.0),
        "raw_tomorrow": _price_rows(tomorrow, 0.8),
        "tomorrow_valid": True,
        "forecast": _price_rows(tomorrow + datetime.timedelta(days=1), 0.9) + _price_rows(tomorrow + datetime.timedelta(days=2), 1.1),
    }
    states = {
        "sensor.charger_status": status,
        "sensor.charger_power": str(power),
        "sensor.charger_kwh": "0",
        "sensor.charger_lifetime_kwh": "1000",
        "sensor.ev_battery_level": str(level),
        "sensor.ev_range": str(level * 4.5),
        "sensor.ev_odometer": "25000",
        "binary_sensor.ev_charge_port": port,
        "number.ev_charge_limit": "80",
        "device_tracker.ev": "home",
        "sensor.consumption": "650",
        "sensor.price": "1.2",
    }
    return Snapshot(
        now=now,
        tz=TZ,
        settings={**default_settings(), **(settings or {})},
        states=states,
        attributes={"sensor.price": price},
        units={"sensor.charger_power": "W", "sensor.consumption": "W"},
        price_data={"attributes": {"sensor.price": price}, "values": {}},
        astral=ASTRAL,
        site=site,
    )


def run(hour: int, databases: dict | None = None, **kwargs) -> tuple[CJPEngine, object]:
    """Start an engine and plan once at <hour>:30 on DAY."""
    now = datetime.datetime.combine(DAY, datetime.time(hour, 30))
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    started = engine.run_job(snapshot(now, **kwargs), engine.startup, databases or {})
    assert started.error is None, started.error
    result = engine.run_job(snapshot(now, **kwargs), engine.charge_if_needed, True)
    assert result.error is None, result.error
    return engine, result


def charge_hours(engine: CJPEngine) -> list[datetime.datetime]:
    return sorted(ts for ts in engine.charge_hours if isinstance(ts, datetime.datetime))


PLUGGED_IN = {"status": "awaiting_start", "port": "on"}


def test_no_charging_mode_charges_at_max() -> None:
    """ev.py's fallback: every charging mode off -> charge like a normal charger."""
    engine, result = run(3)
    assert engine.charger_action == "charge"
    assert result.states["charging_watts"] == MAX_WATT
    assert result.states["current_charging_rule"].startswith("⛔")
    assert result.attributes["charger_action"]["mode"] == "normal"


def test_unplugged_stops() -> None:
    engine, result = run(3, settings={"fill_up": True})
    assert engine.charger_action == "stop"
    assert result.states["charger_phase_1_amps"] == 0


@pytest.mark.parametrize("hour", [3, 12, 18])
def test_fill_up_only_charges_in_planned_hours(hour: int) -> None:
    engine, result = run(hour, settings={"fill_up": True}, **PLUGGED_IN)
    hours = charge_hours(engine)
    assert hours, "fill up from 40 % should plan charging hours"
    assert not [ts for ts in hours if ts.hour in EXPENSIVE_HOURS]
    in_plan = any(ts.date() == DAY and ts.hour == hour for ts in hours)
    assert (engine.charger_action == "charge") is in_plan


def test_fill_up_plans_the_cheapest_night() -> None:
    """Tomorrow's night (base 0.8) is cheaper than the rest of tonight (base 1.0)."""
    engine, _ = run(3, settings={"fill_up": True}, **PLUGGED_IN)
    first = charge_hours(engine)[0]
    assert first.date() == DAY + datetime.timedelta(days=1)
    assert first.hour < 7


def test_low_battery_charges_unless_expensive() -> None:
    settings = {"fill_up": True, "forced_charging_daily_battery_level": True}
    engine, result = run(12, level=10, settings=settings, **PLUGGED_IN)
    assert engine.charger_action == "charge"
    assert result.states["charging_limit"] == pytest.approx(90.0)

    engine, _ = run(18, level=10, settings=settings, **PLUGGED_IN)
    assert engine.charger_action == "stop"


def test_charging_without_rule_asks_for_stop_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """The car/charger starts by itself when plugged in while the action already is stop.

    The action doesn't change, so the charger automation would never run: each forced stop
    counts stop_request up, which changes the decision the blueprint triggers on.
    """
    from custom_components.cable_juice_planner.engine.history.history import History
    from custom_components.cable_juice_planner.sensor import charger_decision

    monkeypatch.setattr(History, "get_average_value", lambda self, *args, **kwargs: 3000.0)
    kwargs = {"level": 80, "settings": {"fill_up": True}, "status": "charging", "port": "on", "power": 3000}
    engine, result = run(18, **kwargs)
    assert engine.charger_action == "stop"
    now = datetime.datetime.combine(DAY, datetime.time(18, 35))
    requests = [result.attributes["charger_action"]["stop_request"]]
    for _ in range(3):
        result = engine.run_job(snapshot(now, **kwargs), engine.charge_if_needed, True)
        assert result.error is None, result.error
        requests.append(result.attributes["charger_action"]["stop_request"])

    assert engine.charger_action == "stop"
    # first only counted (startup + the first plan), then a forced stop on every run while it charges
    assert requests == [0, 1, 2, 3]
    attributes = result.attributes["charger_action"]
    assert charger_decision("stop", attributes).endswith(", stop request 3")

    # not charging any more: nothing more is asked for
    kwargs = {**kwargs, "status": "awaiting_start", "power": 0}
    monkeypatch.setattr(History, "get_average_value", lambda self, *args, **kwargs: 0.0)
    result = engine.run_job(snapshot(now, **kwargs), engine.charge_if_needed, True)
    assert result.error is None, result.error
    assert engine.charger_stop_request == 3


def _no_rule_engine(monkeypatch: pytest.MonkeyPatch, start: datetime.datetime) -> tuple[CJPEngine, dict]:
    """An engine whose charger gives 3000 W (now and on average)."""
    from custom_components.cable_juice_planner.engine.history.history import History

    monkeypatch.setattr(History, "get_average_value", lambda self, *args, **kwargs: 3000.0)
    kwargs = {"level": 80, "status": "charging", "port": "on", "power": 3000}
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snapshot(start, **kwargs), engine.startup, {}).error is None
    return engine, kwargs


def _check_no_rule(engine: CJPEngine, at: datetime.datetime, kwargs: dict) -> bool:
    result = engine.run_job(snapshot(at, **kwargs), engine.charging_without_rule)
    assert result.error is None, result.error
    return result.value


@pytest.mark.parametrize("minute", [0, 2, 4])
def test_charging_without_rule_is_seen_at_the_start_of_the_hour(monkeypatch: pytest.MonkeyPatch, minute: int) -> None:
    """No blind minutes after the hour: the car that started by itself is counted at once."""
    start = datetime.datetime.combine(DAY, datetime.time(12, minute))
    engine, kwargs = _no_rule_engine(monkeypatch, start)
    engine.charging_no_rule_count = 0
    assert _check_no_rule(engine, start, kwargs) is False
    assert engine.charging_no_rule_count == 1


def test_charging_just_after_a_stop_is_not_without_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """A planned hour ended: the charger still gives power until the stop has reached it."""
    stopped = datetime.datetime.combine(DAY, datetime.time(12, 0))
    engine, kwargs = _no_rule_engine(monkeypatch, stopped - datetime.timedelta(hours=1))
    engine.run_job(snapshot(stopped - datetime.timedelta(hours=1), **kwargs), engine.start_charging)
    engine.run_job(snapshot(stopped, **kwargs), engine.stop_charging)
    assert engine.charger_stopped_at == stopped
    engine.charging_no_rule_count = 0

    # within the average's window (5 min): not counted
    for minutes in (0, 2, 4):
        assert _check_no_rule(engine, stopped + datetime.timedelta(minutes=minutes), kwargs) is False
    assert engine.charging_no_rule_count == 0

    # still charging after the window: it is
    assert _check_no_rule(engine, stopped + datetime.timedelta(minutes=5), kwargs) is False
    assert engine.charging_no_rule_count == 1

    # a stop that was already stop doesn't start a new window
    engine.run_job(snapshot(stopped + datetime.timedelta(minutes=6), **kwargs), engine.stop_charging)
    assert engine.charger_stopped_at == stopped


def test_stop_before_the_clock_went_back_does_not_hide_charging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local time: after the clock went back an hour, a stop at 02:58 lies "in the future"."""
    stopped = datetime.datetime.combine(DAY, datetime.time(2, 58))
    engine, kwargs = _no_rule_engine(monkeypatch, stopped)
    engine.charger_action = "charge"
    engine.run_job(snapshot(stopped, **kwargs), engine.stop_charging)
    engine.charging_no_rule_count = 0
    assert _check_no_rule(engine, datetime.datetime.combine(DAY, datetime.time(2, 10)), kwargs) is False
    assert engine.charging_no_rule_count == 1


def test_charger_decision_without_stop_request_is_unchanged() -> None:
    from custom_components.cable_juice_planner.sensor import charger_decision

    attributes = {"mode": "normal", "charger_enabled": True, "phase_1_amps": 0, "charging_limit": 80, "stop_request": 0}
    assert charger_decision("stop", attributes) == "stop, enabled, mode normal, 0/0/0 A, circuit 0/0/0 A, limit 80 %"


def test_manual_charging() -> None:
    engine, result = run(12, settings={"allow_manual_charging_now": True}, **PLUGGED_IN)
    assert engine.charger_action == "charge"
    assert result.states["charging_watts"] == MAX_WATT


def test_workday_departure_plans_before_departure() -> None:
    """Workplan: Friday 07:00 departure with 150 km needs charging before Friday morning."""
    settings = {
        "workplan_charging": True,
        "workday_friday": True,
        "workday_distance_needed_friday": 150.0,
    }
    engine, result = run(12, level=20, settings=settings, **PLUGGED_IN)
    friday_morning = datetime.datetime.combine(DAY + datetime.timedelta(days=1), datetime.time(7))
    assert [ts for ts in charge_hours(engine) if ts < friday_morning]
    # the dashboard chart: the 150 km on Friday, each planned charging with its end
    plan = result.attributes["current_charging_rule"]
    friday = [d for d in plan["distance_forecast"] if d["time"].startswith(str(DAY + datetime.timedelta(days=1)))]
    assert [d["km"] for d in friday] == [150]
    assert all(p["end"] > p["time"] for p in plan["charge_plan"])
    # BattMind reads the cost of each planned hour (its "Elbiler i perioden" table)
    assert sum(p["cost"] for p in plan["charge_plan"]) == pytest.approx(engine.charge_hours["total_cost"], abs=0.05)


def _forecast(result) -> dict[datetime.datetime, float]:
    flow = result.attributes["current_charging_rule"]["battery_forecast"]
    return {datetime.datetime.fromisoformat(p["time"]): p["battery_level"] for p in flow}


def _assert_drive_halves(flow: dict, goto: datetime.datetime, homecoming: datetime.datetime, used: float) -> None:
    """Half the driving on the way out, the level stays while away, the other half on the way home."""
    half_hour = datetime.timedelta(minutes=30)
    assert flow[goto] - flow[goto + half_hour] == pytest.approx(used / 2, abs=0.2)
    assert flow[homecoming - half_hour] == pytest.approx(flow[goto + half_hour], abs=0.1)
    assert flow[homecoming - half_hour] - flow[homecoming] == pytest.approx(used / 2, abs=0.2)


def test_battery_forecast_follows_the_plan_hour_by_hour() -> None:
    """The chart: a point every hour, charging up before the departure and the driving split in two."""
    settings = {
        "workplan_charging": True,
        "workday_friday": True,
        "workday_distance_needed_friday": 150.0,
    }
    engine, result = run(12, level=20, settings=settings, **PLUGGED_IN)
    flow = _forecast(result)
    times = sorted(flow)
    assert times[0] == datetime.datetime.combine(DAY, datetime.time(12, 30))
    assert flow[times[0]] == 20.0
    assert all(later - earlier <= datetime.timedelta(hours=1) for earlier, later in zip(times, times[1:]))
    friday = engine.charging_plan[1]
    # charged up to the planner's level before work, then the 150 km half out and half home
    assert flow[friday["work_goto"]] == pytest.approx(friday["battery_level_before_work_sum"], abs=0.5)
    _assert_drive_halves(flow, friday["work_goto"], friday["work_homecoming"], friday["work_battery_level_needed"])
    plan = result.attributes["current_charging_rule"]
    # the bars start and end at the plan's times: a point at each charging start/end and drive
    for entry in plan["charge_plan"] + plan["distance_forecast"]:
        for key in ("time", "end"):
            time = datetime.datetime.fromisoformat(entry[key])
            assert time < times[0] or time in flow
    assert [(d["time"], d["end"]) for d in plan["distance_forecast"]] == [(friday["work_goto"].isoformat(), friday["work_homecoming"].isoformat())]


TRIP_GOTO = datetime.datetime.combine(DAY + datetime.timedelta(days=1), datetime.time(9))
TRIP_HOMECOMING = datetime.datetime.combine(DAY + datetime.timedelta(days=1), datetime.time(18))


@pytest.mark.parametrize("trip", [{"trip_range_needed": 300.0}, {"trip_charge_procent": 80.0}])
def test_battery_forecast_shows_charging_for_a_trip_tomorrow(trip: dict) -> None:
    settings = {"trip_date_time": TRIP_GOTO, "trip_homecoming_date_time": TRIP_HOMECOMING, **trip}
    engine, result = run(12, level=30, settings=settings, **PLUGGED_IN)
    flow = _forecast(result)
    plan_day = engine.charging_plan[1]
    assert plan_day["trip"]
    used = plan_day["trip_battery_level_needed"] + plan_day["trip_battery_level_above_max"] - engine.get_min_trip_battery_level()
    assert used > 0.0
    # charged up to the trip before leaving, not only at the end of the day
    assert flow[TRIP_GOTO] == pytest.approx(min(plan_day["trip_battery_level_needed"] + plan_day["trip_battery_level_above_max"], 100.0), abs=1.0)
    assert flow[TRIP_GOTO] > 30.0
    _assert_drive_halves(flow, TRIP_GOTO, TRIP_HOMECOMING, used)
    # the bar: the trip's distance, also for a trip set as a battery level
    km = trip.get("trip_range_needed") or round(engine.calc_battery_level_to_distance(used), 0)
    assert result.attributes["current_charging_rule"]["distance_forecast"] == [{"time": TRIP_GOTO.isoformat(), "end": TRIP_HOMECOMING.isoformat(), "km": km}]


@pytest.mark.parametrize("fill_up", [True, False])
@pytest.mark.parametrize("trip_day", [1, 3])
def test_planned_driving_shows_every_active_rule(fill_up: bool, trip_day: int) -> None:
    """Every active rule, added up on the same day: fill up's typical daily distance over each day
    (today only the part that is left), the work plan and the trip (on a day off or a workday)."""
    trip_goto = TRIP_GOTO + datetime.timedelta(days=trip_day - 1)
    trip_homecoming = TRIP_HOMECOMING + datetime.timedelta(days=trip_day - 1)
    settings = {
        "trip_date_time": trip_goto, "trip_homecoming_date_time": trip_homecoming, "trip_range_needed": 300.0,
        "typical_daily_distance": 40.0, "fill_up": fill_up,
        "workplan_charging": True, "workday_saturday": True, "workday_distance_needed_saturday": 100.0,
        "workday_sunday": True, "workday_distance_needed_sunday": 60.0,
    }
    engine, result = run(12, level=30, settings=settings, **PLUGGED_IN)
    now = datetime.datetime.combine(DAY, datetime.time(12, 30))
    today = engine.charging_plan[0]
    part = (today["end_of_day"] - now) / (today["end_of_day"] - today["start_of_day"])
    expected = []
    for day in range(8):  # Thursday to Thursday, Saturday and Sunday are workdays
        plan_day = engine.charging_plan[day]
        if fill_up:
            expected.append({"time": max(plan_day["start_of_day"], now).isoformat(), "end": plan_day["end_of_day"].isoformat(), "km": round(40.0 * (part if day == 0 else 1.0), 0)})
        if day in (2, 3):
            expected.append({"time": plan_day["work_goto"].isoformat(), "end": plan_day["work_homecoming"].isoformat(), "km": 100.0 if day == 2 else 60.0})
        if day == trip_day:
            expected.append({"time": trip_goto.isoformat(), "end": trip_homecoming.isoformat(), "km": 300.0})
    assert result.attributes["current_charging_rule"]["distance_forecast"] == expected
    # the battery level drives today's part before tonight's charging
    flow = _forecast(result)
    assert flow[engine.charging_plan[1]["start_of_day"]] == pytest.approx(30.0 - (engine.calc_distance_to_battery_level(40.0) * part if fill_up else 0.0), abs=0.1)


@pytest.mark.parametrize("hour", [6, 12])
def test_battery_forecast_away_on_a_workday_only_drives_home(hour: int) -> None:
    """Away from home: the way out is already in the battery level, the rest comes on the way home."""
    now = datetime.datetime.combine(DAY, datetime.time(hour, 30))
    settings = {"workplan_charging": True, "workday_thursday": True, "workday_distance_needed_thursday": 100.0}
    snap = snapshot(now, level=60, settings=settings)
    snap.states["device_tracker.ev"] = "not_home"
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, {}).error is None
    result = engine.run_job(snap, engine.charge_if_needed, True)
    assert result.error is None
    flow = _forecast(result)
    today = engine.charging_plan[0]
    homecoming = today["work_homecoming"]
    assert flow[homecoming - datetime.timedelta(minutes=30)] == 60.0
    assert flow[homecoming] == pytest.approx(60.0 - today["work_battery_level_needed"], abs=0.1)


def test_site_live_capacity_limits_amps() -> None:
    """Load balancing: only 10 A per phase left on the grid connection."""
    site = {"live": [10.0, 10.0, 10.0], "hour_capacity": {}, "urgent_hours": 12.0}
    engine, result = run(12, settings={"allow_manual_charging_now": True}, site=site, **PLUGGED_IN)
    assert engine.charger_action == "charge"
    assert [result.states[f"charger_phase_{i}_amps"] for i in (1, 2, 3)] == [10, 10, 10]
    assert result.site["request"] == [10, 10, 10]


def test_site_without_capacity_waits() -> None:
    site = {"live": [0.0, 0.0, 0.0], "hour_capacity": {}, "urgent_hours": 12.0}
    _, result = run(12, settings={"allow_manual_charging_now": True}, site=site, **PLUGGED_IN)
    assert [result.states[f"charger_phase_{i}_amps"] for i in (1, 2, 3)] == [0, 0, 0]


def test_site_urgent_when_under_daily_minimum() -> None:
    site = {"live": [16.0, 16.0, 16.0], "hour_capacity": {}, "urgent_hours": 12.0}
    _, result = run(12, level=10, settings={"fill_up": True}, site=site, **PLUGGED_IN)
    assert result.site["urgent"] is True
    _, result = run(12, level=60, settings={"fill_up": True}, site=site, **PLUGGED_IN)
    assert result.site["urgent"] is False


def test_hour_end_records_power_values_without_solar() -> None:
    """ev.py only recorded them with solar + Solcast; now the house consumption entity is enough."""
    now = datetime.datetime.combine(DAY, datetime.time(12, 59))
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snapshot(now), engine.startup, {}).error is None
    hour_end = snapshot(now)
    # the house used 650 W the whole last two hours (recorder buffer: timestamp, state)
    two_hours_ago = (now - datetime.timedelta(hours=2)).replace(tzinfo=TZ).timestamp()
    hour_end.buffers = {"sensor.consumption": [(two_hours_ago, "650")]}
    result = engine.run_job(hour_end, engine.hour_end)
    assert result.error is None, result.error
    assert "power_values" in result.db_saves
    hour = decode(result.db_saves["power_values"])[12]
    assert hour["power_consumption"][0] == [now, 650.0]
    assert hour["solar_production"][0][1] == 0.0


def test_hour_end_does_not_subtract_battery_charging_twice() -> None:
    """The consumption sensor without battery charging: the charging is not taken off the house again."""
    now = datetime.datetime.combine(DAY, datetime.time(12, 59))
    options = {**OPTIONS, "powerwall_watt_flow_entity_id": "sensor.battery_flow", "invert_powerwall_watt_flow_entity_id": True}
    engine = CJPEngine(const.build_config(options), "cjp_test", TEXTS)
    assert engine.run_job(snapshot(now), engine.startup, {}).error is None
    hour_end = snapshot(now)
    two_hours_ago = (now - datetime.timedelta(hours=2)).replace(tzinfo=TZ).timestamp()
    hour_end.buffers = {"sensor.consumption": [(two_hours_ago, "1000")], "sensor.battery_flow": [(two_hours_ago, "500")]}
    result = engine.run_job(hour_end, engine.hour_end)
    assert result.error is None, result.error
    hour = decode(result.db_saves["power_values"])[12]
    assert hour["powerwall_charging_consumption"][0][1] == 500.0
    assert hour["power_consumption_without_all_exclusion"][0][1] == hour["power_consumption_without_ignored_ev"][0][1]
    assert hour["power_consumption_without_all_exclusion"][0][1] > 0.0


def _home_battery_charging(monkeypatch: pytest.MonkeyPatch, hour: int, solar: float) -> tuple[str, float]:
    """The home battery charges 7277 W below the car's battery level limit, with `solar` W production and 600 W house."""
    from custom_components.cable_juice_planner.engine.energy.power import POWER_VALUE_KEYS

    now = datetime.datetime.combine(DAY, datetime.time(hour, 30))
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snapshot(now), engine.startup, {}).error is None
    house, charging = 600.0, 7277.0
    values = dict.fromkeys(POWER_VALUE_KEYS, 0.0)
    values.update(
        power_consumption=house,
        power_consumption_without_ignored=house,
        power_consumption_without_ignored_ev=house,
        power_consumption_without_all_exclusion=house,
        powerwall_charging_consumption=charging,
        solar_production=solar,
    )
    patches = {
        "is_solar_configured": lambda: True,
        "is_powerwall_configured": lambda: True,
        "power_values": lambda **_: dict(values),
        "local_energy_available": lambda **_: max(solar - house, 0.0),
        "local_energy_prediction": lambda **_: {}.keys(),  # no hour where the battery is expected to charge from solar
        "get_powerwall_discharge_above_needed": lambda: False,
        "get_powerwall_battery_level": lambda: 63.0,
        "get_ev_charge_after_powerwall_battery_level": lambda: 90.0,
        "powerwall_max_charging_power": lambda **_: charging,
        "ready_to_charge": lambda: True,
        "current_hour_in_charge_hours": lambda: False,
    }
    for name, func in patches.items():
        monkeypatch.setattr(engine, name, func)
    result = engine.run_job(snapshot(now), engine.max_local_energy_available_remaining_period)
    assert result.error is None, result.error
    latest = engine.max_local_energy_available_history[max(engine.max_local_energy_available_history)]
    return engine.powerwall_charging_text, latest["5. powerwall_force_power"]


def test_home_battery_charging_without_solar_surplus_is_not_reported_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grid charging of the home battery (no solar surplus): nothing to give the car, so not "stopped"."""
    text, force_power = _home_battery_charging(monkeypatch, 0, solar=0.0)
    assert text == TEXTS["ui.max_local_energy_available_remaining_period.powerwall_charging"].format(powerwall_charging_consumption=7277)
    assert force_power == 0.0


def test_home_battery_charging_with_solar_surplus_is_reported_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Solar surplus that charges the home battery is moved to the car - at any time of day."""
    for hour in (12, 0):  # depends on the surplus, not on the clock
        text, force_power = _home_battery_charging(monkeypatch, hour, solar=3000.0)
        assert text == TEXTS["ui.max_local_energy_available_remaining_period.powerwall_force_stop"].format(powerwall_charging_consumption=7277)
        assert force_power == 2400.0  # min(solar surplus 3000 - 600, battery charging 7277)


def test_hour_end_without_consumption_entity_records_nothing() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(12, 59))
    engine = CJPEngine(const.build_config({**OPTIONS, "power_consumption_entity_id": ""}), "cjp_test", TEXTS)
    assert engine.run_job(snapshot(now), engine.startup, {}).error is None
    result = engine.run_job(snapshot(now), engine.hour_end)
    assert result.error is None, result.error
    assert "power_values" not in result.db_saves


def test_existing_power_values_are_loaded_without_solar() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))
    stamp = now - datetime.timedelta(days=1)
    database = {"version": 1.0, 12: {"power_consumption": [[stamp, 900.0]]}}
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snapshot(now), engine.startup, {"power_values": database}).error is None
    assert engine.power_values_db[12]["power_consumption"] == [[stamp, 900.0]]


def test_startup_saves_every_database() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    result = engine.run_job(snapshot(now), engine.startup, {})
    assert set(result.db_saves) == set(const.DB_NAMES)
    assert decode(result.db_saves["runtime"])["charging_loss"] == OPTIONS["charging_loss"]


def test_startup_migrates_price_history_before_provider_is_available(caplog: pytest.LogCaptureFixture) -> None:
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    result = engine.run_job(snapshot(now), engine.startup, {"kwh_avg_prices": {}})
    assert result.error is None, result.error
    assert "Can't append KWH avg prices" not in caplog.text


def _loss_job(engine: CJPEngine, hour: int, source: str, *args, level: float, kwh: float, status: str = "charging"):
    now = datetime.datetime.combine(DAY, datetime.time(hour, 0))
    snap = snapshot(now, level=level, status=status, port="on", settings={"calculate_charging_loss": True})
    snap.states["sensor.charger_kwh"] = str(kwh)
    result = engine.run_job(snap, engine.calculate_charging_loss, source, *args)
    assert result.error is None, result.error
    return result


def _loss_engine() -> CJPEngine:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snapshot(datetime.datetime.combine(DAY, datetime.time(1, 0))), engine.startup, {}).error is None
    return engine


@pytest.mark.parametrize("final_source", ["kwh_meter", "timeout"])
def test_charging_loss_measurement(final_source: str) -> None:
    """20% -> 80% of 75 kWh = 45 kWh in the car, 50 kWh from the charger: 11.1% loss."""
    engine = _loss_engine()
    _loss_job(engine, 1, "switch", True, level=20, kwh=100)
    assert _loss_job(engine, 6, "status", "completed", "charging", level=80, kwh=148, status="completed").value == "wait_for_meter"
    # the slow meter reports the rest later (or the coordinator's timeout reads it)
    args = ("150", "148") if final_source == "kwh_meter" else ()
    result = _loss_job(engine, 6, final_source, *args, level=80, kwh=150, status="completed")
    assert result.config_updates["charging_loss"] == pytest.approx(-0.1111, abs=1e-4)
    assert result.settings["calculate_charging_loss"] is False
    # the stored loss converts the car's 45 kWh back into the 50 kWh from the charger
    assert engine.percentage_to_kwh(60, include_charging_loss=True) == pytest.approx(50, abs=0.01)


def test_charging_loss_resumed_charging_keeps_measuring() -> None:
    engine = _loss_engine()
    _loss_job(engine, 1, "switch", True, level=20, kwh=100)
    _loss_job(engine, 5, "status", "completed", "charging", level=79, kwh=147, status="completed")
    _loss_job(engine, 5, "status", "charging", "completed", level=79, kwh=147)
    # a meter update while charging again does not finish the measurement
    assert "charging_loss" not in _loss_job(engine, 5, "kwh_meter", "148", "147", level=80, kwh=148).config_updates
    assert engine.charging_loss_car_begin_battery_level == 20


def test_charging_loss_needs_enough_charged() -> None:
    engine = _loss_engine()
    _loss_job(engine, 1, "switch", True, level=60, kwh=100)
    _loss_job(engine, 3, "status", "completed", "charging", level=80, kwh=117, status="completed")
    result = _loss_job(engine, 3, "timeout", level=80, kwh=117, status="completed")
    assert "charging_loss" not in result.config_updates
    assert result.settings["calculate_charging_loss"] is False
    assert engine.charging_loss_charger_begin_kwh == 0.0


def test_charging_loss_charges_to_the_car_limit() -> None:
    """A car limit above the recommended max is published, not the daily range level."""
    now = datetime.datetime.combine(DAY, datetime.time(12, 30))
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    snap = snapshot(now, settings={"calculate_charging_loss": True}, **PLUGGED_IN)
    snap.states["number.ev_charge_limit"] = "100"
    assert engine.run_job(snap, engine.startup, {}).error is None
    result = engine.run_job(snap, engine.charge_if_needed, True)
    assert result.error is None, result.error
    assert engine.charger_action == "charge"
    assert result.states["charging_limit"] == 100


def test_cheap_grid_charging_alone_charges_to_the_cheap_levels() -> None:
    """Only "charge on cheap power": cheap hours up to the very/ultra cheap level, nothing else."""
    settings = {"cheap_grid_charging": True, "very_cheap_grid_charging_max_battery_level": 70.0, "ultra_cheap_grid_charging_max_battery_level": 80.0}
    engine, result = run(12, settings=settings, **PLUGGED_IN)
    hours = charge_hours(engine)
    assert hours, "cheap hours should be planned from 40 %"
    assert not [ts for ts in hours if ts.hour in EXPENSIVE_HOURS]
    for ts in hours:
        hour = engine.charge_hours[ts]
        assert "under_min_avg_price" in hour or "half_min_avg_price" in hour, (ts, hour)
        assert "fill_up" not in hour
    # no hour is ultra cheap in the test prices: exactly up to the very cheap level, also
    # with the hours just after midnight (they once went uncounted and overcharged)
    assert 40 + engine.charge_hours["total_procent"] == pytest.approx(70, abs=0.5)
    assert not result.states["current_charging_rule"].startswith("⛔")


def test_cheap_rules_do_not_stick_to_later_hours() -> None:
    """The price rules of one hour used to leak into every later fill up hour."""
    engine, _ = run(3, settings={"fill_up": True}, **PLUGGED_IN)
    prices = sorted(engine.get_grid_prices().values())[: engine.config["database"]["kwh_avg_prices_db_data_to_save"]]
    very_cheap = round(sum(prices) / len(prices), 3)
    for ts in charge_hours(engine):
        hour = engine.charge_hours[ts]
        assert ("under_min_avg_price" in hour or "half_min_avg_price" in hour) == (round(hour["Price"], 3) <= very_cheap), (ts, hour)


def _distance_engine(daily_km: dict[int, float], weeks: int = 4) -> CJPEngine:
    """An engine with odometer readings: daily_km per weekday (0 = monday) for <weeks> weeks up to DAY."""
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    odometer = 20000.0
    first = DAY - datetime.timedelta(weeks=weeks)
    for offset in range((DAY - first).days + 1):
        date = first + datetime.timedelta(days=offset)
        odometer += daily_km.get(date.weekday(), 0.0) if offset else 0.0
        engine.daily_odometer[date.isoformat()] = odometer
    return engine


def _distance_run(engine: CJPEngine, settings: dict | None = None):
    result = engine.run_job(snapshot(datetime.datetime.combine(DAY, datetime.time(12, 0)), settings=settings), engine.update_learned_distances)
    assert result.error is None, result.error
    return result


def test_learned_distance_is_only_suggested_by_default() -> None:
    engine = _distance_engine({0: 42, 1: 42, 2: 42, 3: 42, 4: 120, 5: 10, 6: 0})
    result = _distance_run(engine)
    assert result.attributes["workday_distance_needed_monday"]["learned_distance"] == 45
    assert result.attributes["workday_distance_needed_friday"]["learned_distance"] == 120
    assert result.attributes["workday_distance_needed_sunday"]["learned_distance"] == 0
    # 75th percentile of all days as the buffered typical day
    assert result.attributes["typical_daily_distance"]["learned_distance"] == 45
    assert not result.settings


def test_learned_distance_never_changes_the_numbers() -> None:
    engine = _distance_engine({0: 42, 1: 42, 2: 42, 3: 42, 4: 120, 5: 10, 6: 0})
    result = _distance_run(engine, {"learn_driving_distance": True})
    assert not result.settings
    assert engine.learned_distances["workday_distance_needed_friday"] == 120
    assert engine.learned_distances["typical_daily_distance"] == 45


def test_learned_distance_median_ignores_one_long_trip() -> None:
    engine = _distance_engine({0: 30})
    # one monday with a 400 km trip
    monday = min(date for date in engine.daily_odometer if datetime.date.fromisoformat(date).weekday() == 0 and date > (DAY - datetime.timedelta(weeks=4)).isoformat())
    for date in engine.daily_odometer:
        if date >= monday:
            engine.daily_odometer[date] += 370
    result = _distance_run(engine)
    assert result.attributes["workday_distance_needed_monday"]["learned_distance"] == 30


def test_learned_distance_needs_enough_days() -> None:
    engine = _distance_engine({0: 42}, weeks=1)
    result = _distance_run(engine, {"learn_driving_distance": True})
    assert result.attributes["workday_distance_needed_monday"]["learned_days"] == 1
    assert engine.learned_distances == {}


@pytest.mark.parametrize(("learn", "entered", "expected"), [(True, 50.0, 120), (True, 150.0, 150), (False, 50.0, 50)])
def test_plan_uses_the_higher_of_entered_and_learned_distance(learn: bool, entered: float, expected: float) -> None:
    """Regression: the chart showed the entered km with learning on; a higher entered day still counts."""
    settings = {"workplan_charging": True, "workday_friday": True, "workday_distance_needed_friday": entered}
    engine, _ = run(12, level=20, settings=settings, **PLUGGED_IN)
    engine.daily_odometer = _distance_engine({4: 120}).daily_odometer
    now = datetime.datetime.combine(DAY, datetime.time(12, 30))
    assert engine.run_job(snapshot(now, level=20, settings=settings, **PLUGGED_IN), engine.update_learned_distances).error is None
    # turning the switch on recalculates the plan
    result = engine.run_job(snapshot(now, level=20, settings={**settings, "learn_driving_distance": learn}, **PLUGGED_IN), engine.recalculate)
    assert result.error is None, result.error
    assert "workday_distance_needed_friday" not in result.settings
    friday = [d for d in result.attributes["current_charging_rule"]["distance_forecast"] if d["time"].startswith(str(DAY + datetime.timedelta(days=1)))]
    assert [d["km"] for d in friday] == [expected]


def test_daily_odometer_is_recorded_at_the_end_of_the_day() -> None:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    result = engine.run_job(snapshot(datetime.datetime.combine(DAY, datetime.time(23, 59))), engine.hour_end)
    assert result.error is None, result.error
    assert engine.daily_odometer == {DAY.isoformat(): 25000.0}
    assert "runtime" in result.db_saves


def test_distance_backfill_fills_only_the_missing_days() -> None:
    """Evenings CJP did not read itself come from the recorder; its own readings win, and today
    (a partial day), days older than the learning window and bad values are left out."""
    recorded = _distance_engine({0: 42, 1: 42, 2: 42, 3: 42, 4: 120, 5: 10, 6: 0}).daily_odometer
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    own_day = (DAY - datetime.timedelta(days=3)).isoformat()
    engine.daily_odometer = {own_day: recorded[own_day]}
    readings = {
        **recorded,
        own_day: recorded[own_day] + 999,  # the own reading wins
        DAY.isoformat(): recorded[DAY.isoformat()] - 10,  # today is left to hour_end
        (DAY - datetime.timedelta(weeks=9)).isoformat(): 1.0,  # older than the learning window
        (DAY - datetime.timedelta(weeks=5)).isoformat(): "unavailable",
        (DAY - datetime.timedelta(weeks=6)).isoformat(): float("nan"),
        "not a date": 1.0,
    }
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))
    result = engine.run_job(snapshot(now), engine.distance_backfill, readings)
    assert result.error is None, result.error
    assert engine.daily_odometer == {date: value for date, value in recorded.items() if date != DAY.isoformat()}
    assert "runtime" in result.db_saves
    # 28 evenings up to yesterday give 27 days
    assert result.attributes["typical_daily_distance"]["learned_days"] == 27
    assert result.attributes["workday_distance_needed_friday"]["learned_distance"] == 120

    # nothing new: nothing saved
    result = engine.run_job(snapshot(now), engine.distance_backfill, readings)
    assert result.error is None, result.error
    assert "runtime" not in result.db_saves


def test_fill_up_fills_to_max_in_the_cheapest_night_and_keeps_the_floor() -> None:
    """40 %: tonight only the floor would be needed, the cheap night tomorrow fills up to 90 %."""
    engine, _ = run(3, settings={"fill_up": True}, **PLUGGED_IN)
    plan = engine.charging_plan
    tomorrow_night = [ts for ts in charge_hours(engine) if ts.date() == DAY + datetime.timedelta(days=1) and ts.hour < 7]
    assert tomorrow_night
    # day 1 reaches the max recommended level before its driving
    assert plan[1]["battery_level_at_midnight_sum"] + engine.charging_plan[1]["offday_battery_level_needed"] == pytest.approx(90, abs=0.5)
    # never below the daily minimum at midnight (the test has prices for 4 days)
    for day in range(4):
        assert plan[day]["battery_level_at_midnight_sum"] >= 30 - 0.5, day


def test_fill_up_follows_the_prices_not_the_clock() -> None:
    """Solar days: tomorrow's cheapest hours are at noon, not at night."""
    now = datetime.datetime.combine(DAY, datetime.time(3, 30))
    snap = snapshot(now, settings={"fill_up": True}, **PLUGGED_IN)
    price = snap.attributes["sensor.price"]
    for row in price["raw_tomorrow"]:
        hour = datetime.datetime.fromisoformat(row["hour"]).hour
        row["price"] = round((0.3 if 10 <= hour <= 15 else 1.2) + hour / 100, 3)
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, {}).error is None
    assert engine.run_job(snap, engine.charge_if_needed, True).error is None
    hours = charge_hours(engine)
    today = [ts for ts in hours if ts.date() == DAY]
    tomorrow = [ts for ts in hours if ts.date() == DAY + datetime.timedelta(days=1)]
    # today only the floor: 40 % does not cover today's 16.7 % driving + the 30 % minimum
    assert sum(engine.charge_hours[ts]["battery_level"] for ts in today) == pytest.approx(46.67 - 40, abs=0.5)
    # filling up waits for tomorrow's cheap noon
    assert tomorrow and all(10 <= ts.hour <= 15 for ts in tomorrow), tomorrow


def test_full_charge_recommended_charges_to_100() -> None:
    """Every 7 days, last full charge 10 days ago: tomorrow's plan reaches 100 %, above the max recommended 90 %."""
    last = datetime.datetime.combine(DAY - datetime.timedelta(days=10), datetime.time(8, 0))
    engine, _ = run(12, {"runtime": {"sensors": {"last_full_charge": last}}}, settings={"full_charge_recommended": 7.0}, **PLUGGED_IN)
    hours = charge_hours(engine)
    assert hours and all("battery_health" in engine.charge_hours[ts] for ts in hours)
    assert 40 + engine.charge_hours["total_procent"] == pytest.approx(100, abs=0.5)


def test_full_charge_not_needed_after_a_recent_one() -> None:
    last = datetime.datetime.combine(DAY - datetime.timedelta(days=2), datetime.time(8, 0))
    engine, _ = run(12, {"runtime": {"sensors": {"last_full_charge": last}}}, settings={"full_charge_recommended": 7.0}, **PLUGGED_IN)
    assert not [ts for ts in charge_hours(engine) if "battery_health" in engine.charge_hours[ts]]


def test_full_charge_is_registered_at_100() -> None:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    result = engine.run_job(snapshot(datetime.datetime.combine(DAY, datetime.time(6, 0)), level=100), engine.is_battery_fully_charged)
    assert result.error is None
    assert "last_full_charge" in result.states
    assert "runtime" in result.db_saves
    assert "last_full_charge" not in result.settings


def test_full_charge_at_99_requires_30_minutes_stopped() -> None:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    started = datetime.datetime.combine(DAY, datetime.time(6, 0))
    first = engine.run_job(snapshot(started, level=99, status="awaiting_start"), engine.is_battery_fully_charged)
    assert first.error is None
    assert "last_full_charge" not in first.states
    assert engine.full_charge_99_since == started
    assert decode(first.db_saves["runtime"])["full_charge_99_since"] == started

    after_29_minutes = started + datetime.timedelta(minutes=29)
    waiting = engine.run_job(snapshot(after_29_minutes, level=99, status="awaiting_start"), engine.is_battery_fully_charged)
    assert waiting.error is None
    assert "last_full_charge" not in waiting.states

    after_30_minutes = started + datetime.timedelta(minutes=30)
    completed = engine.run_job(snapshot(after_30_minutes, level=99, status="awaiting_start"), engine.is_battery_fully_charged)
    assert completed.error is None
    assert completed.states["last_full_charge"] == after_30_minutes
    assert engine.full_charge_99_since is None


def test_full_charge_at_99_does_not_start_wait_while_charging() -> None:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    engine.full_charge_99_since = datetime.datetime.combine(DAY, datetime.time(5, 45))
    result = engine.run_job(
        snapshot(datetime.datetime.combine(DAY, datetime.time(6, 0)), level=99, status="charging"),
        engine.is_battery_fully_charged,
    )
    assert result.error is None
    assert "last_full_charge" not in result.states
    assert engine.full_charge_99_since is None


def test_full_charge_at_99_is_not_retried_during_wait() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(12, 30))
    since = now - datetime.timedelta(minutes=15)
    last = now - datetime.timedelta(days=10)
    engine, _ = run(
        12,
        {
            "runtime": {
                "sensors": {"last_full_charge": last},
                "full_charge_99_since": since,
            }
        },
        level=99,
        status="awaiting_start",
        settings={"full_charge_recommended": 7.0},
    )
    assert engine.full_charge_99_since == since
    assert not [ts for ts in charge_hours(engine) if "battery_health" in engine.charge_hours[ts]]
    assert engine.charger_action == "stop"


@pytest.mark.parametrize(("location", "hour", "expected_km"), [("not_home", 12, 50.0), ("not_home", 6, 50.0), ("home", 6, 100.0), ("home", 12, None)])
def test_away_on_a_workday_counts_half_the_distance(location: str, hour: int, expected_km: float | None) -> None:
    """Thursday 07:00-16:00, 100 km: away from home only the way back is left for today."""
    now = datetime.datetime.combine(DAY, datetime.time(hour, 30))
    settings = {"workplan_charging": True, "workday_thursday": True, "workday_distance_needed_thursday": 100.0}
    snap = snapshot(now, level=60, settings=settings)
    snap.states["device_tracker.ev"] = location
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, {}).error is None
    assert engine.run_job(snap, engine.charge_if_needed, True).error is None
    today = engine.charging_plan[0]
    if expected_km is None:
        # home again after the departure time: no driving left today (as before)
        assert not today["workday"]
    else:
        assert today["workday"]
        assert today["work_range_needed"] == expected_km
        # the day's driving is taken from the level before work (incl. charging planned before the departure)
        assert today["battery_level_after_work_sum"] == pytest.approx(today["battery_level_before_work_sum"] - today["work_battery_level_needed"], abs=0.1)


def test_virtual_car_reports_the_drive_after_unplugging_from_completed() -> None:
    """Without a car integration: unplugged long after charging completed, plugged in again and the level entered."""
    options = {**OPTIONS, **{key: "" for key in ("usable_battery_level_entity_id", "estimated_battery_range_entity_id", "odometer_entity_id", "charge_port_door_entity_id", "charging_limit_entity_id", "location_entity_id")}}
    options["battery_size"] = 9.9
    engine = CJPEngine(const.build_config(options), "cjp_test", TEXTS)

    def at(hour: int) -> datetime.datetime:
        return datetime.datetime.combine(DAY, datetime.time(hour, 0))

    def snap(hour: int, status: str, level: float, history: list[tuple[int, str]]) -> Snapshot:
        result = snapshot(at(hour), status=status, settings={"battery_level": level, "estimated_total_range": 60.0})
        result.buffers = {"sensor.charger_status": [(at(h).replace(tzinfo=TZ).timestamp(), value) for h, value in history]}
        return result

    def drive_notifications(result) -> list[dict]:
        return [n for n in result.mobile_notifications if TEXTS["ui.drive_efficiency.title"] in n["title"]]

    # first drive, unplugged while the charger still waited for the plan: 90 % -> 70 %
    history = [(0, "awaiting_start")]
    assert engine.run_job(snap(1, "awaiting_start", 90.0, history), engine.startup, {}).error is None
    assert engine.run_job(snap(1, "disconnected", 90.0, history), engine.charger_status_changed, "disconnected", "awaiting_start").error is None
    assert drive_notifications(engine.run_job(snap(2, "awaiting_start", 70.0, history), engine.emulated_battery_level_changed, 70.0, 0.0))
    assert engine.last_drive_efficiency_data["reported"] is True

    # charged to 80 %, completed at 05:00 and unplugged at 07:00: the level before the drive is still saved
    history = [(2, "awaiting_start"), (4, "charging"), (5, "completed")]
    assert engine.run_job(snap(7, "disconnected", 80.0, history), engine.charger_status_changed, "disconnected", "completed").error is None
    assert engine.sensor_states["drive_efficiency_last_battery_level"] == 80.0

    plugged = engine.run_job(snap(16, "awaiting_start", 50.0, history), engine.emulated_battery_level_changed, 50.0, 0.0)
    assert plugged.error is None
    assert drive_notifications(plugged)
    assert engine.last_drive_efficiency_data["usedBattery"] == 30.0
    assert engine.last_drive_efficiency_data["distance"] == 18.0

    # a level above the saved one is no drive
    history = [(16, "awaiting_start")]
    assert engine.run_job(snap(17, "disconnected", 50.0, history), engine.charger_status_changed, "disconnected", "awaiting_start").error is None
    higher = engine.run_job(snap(18, "awaiting_start", 60.0, history), engine.emulated_battery_level_changed, 60.0, 0.0)
    assert higher.error is None
    assert not drive_notifications(higher)


SIMULATION_OPTIONS = {**OPTIONS, "simulate_battery_level_while_charging": True}
# 7.5 kWh from the charger with the default 5 % charging loss in a 75 kWh battery
SIMULATED_PER_7_5_KWH = 7.5 / 1.05 / 75 * 100


def _simulated(engine: CJPEngine, hour: float, level: float, meter: float, status: str = "charging", entered: float | None = None):
    """Publish the simulated battery level (number.<prefix>_battery_level) with the car at level and the meter at meter kWh."""
    now = datetime.datetime.combine(DAY, datetime.time(int(hour), int(hour % 1 * 60)))
    snap = snapshot(now, level=level, status=status, settings={"battery_level": entered} if entered is not None else None)
    snap.states["sensor.charger_kwh"] = str(meter)
    result = engine.run_job(snap, engine.publish_simulated_battery_level)
    assert result.error is None, result.error
    return result


def test_simulated_battery_level_rises_with_the_meter_and_follows_the_car() -> None:
    engine = CJPEngine(const.build_config(SIMULATION_OPTIONS), "cjp_test", TEXTS)
    assert _simulated(engine, 10, 40, 0).settings["battery_level"] == 40
    result = _simulated(engine, 10.5, 40, 7.5)
    assert result.settings["battery_level"] == pytest.approx(40 + SIMULATED_PER_7_5_KWH, abs=0.05)
    attributes = result.attributes["battery_level"]
    assert attributes["real_battery_level"] == 40
    assert attributes["simulating"] is True
    assert attributes["kwh_since_car_update"] == 7.5
    # the car reports: corrected to its level, rising from there
    assert _simulated(engine, 11, 48, 7.5).settings["battery_level"] == 48
    assert _simulated(engine, 11.5, 48, 15).settings["battery_level"] == pytest.approx(48 + SIMULATED_PER_7_5_KWH, abs=0.05)
    # 1 % under the car's charging limit (80 %): the car confirms the end
    assert _simulated(engine, 13, 48, 60).settings["battery_level"] == 79
    # planning gets the simulated level, history the car's own
    snap = snapshot(datetime.datetime.combine(DAY, datetime.time(13, 5)), level=48, status="charging")
    snap.states["sensor.charger_kwh"] = "60"
    assert engine.run_job(snap, engine.battery_level).value == 79
    assert engine.run_job(snap, engine.battery_level, True).value == 48


def test_simulated_battery_level_entered_by_the_user() -> None:
    """Plugged in with the car's level 30 minutes behind: the entered level is simulated from until the car reports."""
    engine = CJPEngine(const.build_config(SIMULATION_OPTIONS), "cjp_test", TEXTS)
    assert _simulated(engine, 16, 60, 0, status="awaiting_start").settings["battery_level"] == 60
    entered = _simulated(engine, 16.1, 60, 0, status="awaiting_start", entered=50)
    assert entered.settings["battery_level"] == 50  # under the car's (stale) level is allowed
    assert entered.attributes["battery_level"]["entered"] is True
    assert _simulated(engine, 17, 60, 7.5, entered=50).settings["battery_level"] == pytest.approx(50 + SIMULATED_PER_7_5_KWH, abs=0.05)
    reported = _simulated(engine, 17.5, 62, 7.5)
    assert reported.settings["battery_level"] == 62
    assert reported.attributes["battery_level"]["entered"] is False


def test_simulated_battery_level_ignores_the_meter_without_the_car() -> None:
    engine = CJPEngine(const.build_config(SIMULATION_OPTIONS), "cjp_test", TEXTS)
    _simulated(engine, 10, 40, 0)
    level = _simulated(engine, 10.5, 40, 7.5).settings["battery_level"]
    # unplugged: another car may use the charger, the level is kept
    assert _simulated(engine, 11, 40, 20, status="disconnected").settings["battery_level"] == level
    # plugged in again: counts from the meter then, a meter reset is no drop either
    assert _simulated(engine, 12, 40, 27.5).settings["battery_level"] == pytest.approx(level + SIMULATED_PER_7_5_KWH, abs=0.05)
    assert _simulated(engine, 13, 40, 0).settings["battery_level"] == pytest.approx(level + SIMULATED_PER_7_5_KWH, abs=0.05)


def test_simulated_battery_level_off_uses_the_car() -> None:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    result = _simulated(engine, 10, 40, 0)
    assert "battery_level" not in result.settings
    snap = snapshot(datetime.datetime.combine(DAY, datetime.time(11, 0)), level=40, status="charging")
    snap.states["sensor.charger_kwh"] = "30"
    assert engine.run_job(snap, engine.battery_level).value == 40


def test_simulated_battery_level_asks_at_plug_in() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(16, 30))
    snap = snapshot(now, level=55, **PLUGGED_IN)
    engine = CJPEngine(const.build_config(SIMULATION_OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, {}).error is None
    result = engine.run_job(snap, engine.charger_status_changed, "awaiting_start", "disconnected")
    assert result.error is None, result.error
    asks = [n for n in result.mobile_notifications if TEXTS["ui.notify_check_battery_level.title"] in n["title"]]
    assert len(asks) == 1
    assert "55 %" in asks[0]["message"]
    assert asks[0]["data"]["actions"][0]["uri"] == "entityId:number.cjp_test_battery_level"


def test_plug_in_under_daily_level_notifies_once() -> None:
    """The charger status and the car's charge port both report the plug-in: one notification."""
    now = datetime.datetime.combine(DAY, datetime.time(16, 30))
    snap = snapshot(now, level=20, **PLUGGED_IN)
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, {}).error is None

    def daily_level_notifications(result) -> list[dict]:
        assert result.error is None, result.error
        return [n for n in result.mobile_notifications if TEXTS["ui.notify_battery_under_daily_battery_level.title"] in n["title"]]

    assert len(daily_level_notifications(engine.run_job(snap, engine.charger_connected, "awaiting_start"))) == 1
    assert not daily_level_notifications(engine.run_job(snap, engine.ev_port_changed, "on", "off"))


@pytest.mark.parametrize(
    ("status", "status_history", "cable", "cable_history", "home"),
    [
        ("awaiting_start", [], None, [], True),
        ("disconnected", [(10, "completed")], None, [], True),  # unplugged 2 hours after charging completed
        ("disconnected", [(11, "charging"), (11.95, "disconnected")], None, [], True),  # unplugged 3 minutes ago
        ("disconnected", [(10, "charging"), (11, "disconnected")], None, [], False),
        ("unavailable", [], None, [], False),
        ("disconnected", [(10, "disconnected")], "on", [], True),  # the cable says plugged in
        ("disconnected", [(10, "disconnected")], "off", [(11, "on"), (11.95, "off")], True),
        ("disconnected", [(10, "disconnected")], "off", [(10, "off")], False),
        (None, [], "off", [(10, "off")], False),  # cable only
        (None, [], "on", [], True),
        (None, [], None, [], True),  # nothing to go by
    ],
)
def test_without_location_home_follows_charger_status_and_cable(status, status_history, cable, cable_history, home: bool) -> None:
    options = {**OPTIONS, "location_entity_id": "", "status_entity_id": "sensor.charger_status" if status else "", "cable_connected_entity_id": "binary_sensor.charger_cable" if cable else ""}
    engine = CJPEngine(const.build_config(options), "cjp_test", TEXTS)
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))

    def rows(history: list[tuple[float, str]]) -> list[tuple[float, str]]:
        return [(now.replace(tzinfo=TZ).timestamp() - (12 - hour) * 3600, value) for hour, value in history]

    snap = snapshot(now, status=status or "disconnected")
    snap.states.pop("device_tracker.ev")
    if status is None:
        snap.states.pop("sensor.charger_status")
    if cable:
        snap.states["binary_sensor.charger_cable"] = cable
    snap.buffers = {"sensor.charger_status": rows(status_history), "binary_sensor.charger_cable": rows(cable_history)}
    result = engine.run_job(snap, engine.is_ev_home)
    assert result.error is None
    assert result.value is home


def _price_history() -> dict:
    """Price history where the price tells the weekday: 2.<weekday> in every hour."""
    history = {hour: {dow: [2.0 + dow / 10] * 3 for dow in range(7)} for hour in range(24)}
    return {"history": history, "history_sell": deepcopy(history), "max": [3.0], "mean": [2.0], "min": [1.0], "version": 2.0}


def test_missing_hours_are_filled_from_the_price_history() -> None:
    """Only today's prices are published: the rest of the plan (today + 7 days) uses the history of that weekday."""
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))
    snap = snapshot(now)
    snap.price_data["attributes"]["sensor.price"].update(raw_tomorrow=[], tomorrow_valid=False, forecast=[])
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, {"kwh_avg_prices": _price_history()}).error is None
    assert engine.run_job(snap, engine.charge_if_needed, True).error is None

    prices = engine.grid_price_cache["prices"]
    sources = engine.grid_price_cache["sources"]
    assert prices[now.replace(hour=17)] == pytest.approx(1.0 + 1.5 + 0.17)  # published price kept
    assert sources[now.replace(hour=17)] == "energidataservice:real"
    for days in (1, 4, 7):
        timestamp = now.replace(hour=17) + datetime.timedelta(days=days)
        assert sources[timestamp] == "database"
        assert prices[timestamp] == pytest.approx(round(2.0 + timestamp.weekday() / 10 + days / 30, 2))
    assert max(prices) == datetime.datetime.combine(DAY + datetime.timedelta(days=7), datetime.time(23))
    assert engine.grid_price_cache["using_offline_prices"] is False


def _short_of_hours_engine(departure: datetime.time, missing_hours: tuple[int, ...] = (), databases: dict | None = None) -> CJPEngine:
    """Work plan whose need (150 km) cannot be met before departure: the planner then uses every hour until departure.

    Planned at 03:30 on DAY; missing_hours are left out of today's published prices."""
    now = datetime.datetime.combine(DAY, datetime.time(3, 30))
    settings = {
        "workplan_charging": True,
        "workday_thursday": True,
        "workday_departure_thursday": departure,
        "workday_distance_needed_thursday": 150.0,
    }
    snap = snapshot(now, level=20, settings=settings, **PLUGGED_IN)
    attributes = snap.price_data["attributes"]["sensor.price"]
    attributes["raw_today"] = [row for row in attributes["raw_today"] if int(row["hour"][11:13]) not in missing_hours]
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, databases or {}).error is None
    assert engine.run_job(snap, engine.charge_if_needed, True).error is None
    return engine


def test_an_hour_the_price_integrations_lack_is_planned_at_its_price_from_the_history() -> None:
    """With a stored price history the missing hour has a price (the same hour of the weekday), so it is planned like any other."""
    engine = _short_of_hours_engine(datetime.time(8, 0), missing_hours=(5,), databases={"kwh_avg_prices": _price_history()})
    timestamp = datetime.datetime.combine(DAY, datetime.time(5))
    assert engine.grid_price_cache["sources"][timestamp] == "database"
    assert timestamp in charge_hours(engine)
    assert engine.charge_hours[timestamp]["Price"] == pytest.approx(engine.grid_price_cache["prices"][timestamp], abs=0.011)
    assert engine.charge_hours[timestamp]["Price"] == pytest.approx(2.0 + DAY.weekday() / 10, abs=0.011)


@pytest.mark.parametrize("departure", [datetime.time(8, 0), datetime.time(7, 30)])
def test_hours_added_when_charging_falls_short_use_their_own_price(departure: datetime.time, caplog: pytest.LogCaptureFixture) -> None:
    """The last hours before departure are added without a price search: each keeps the price of its hour, not a price left over from the search.

    With a departure at 07:30 those hours are planned at :30 (the fallback does not align to clock hours),
    so the price must be looked up by the hour they are in. Do not assert those :30 slots themselves."""
    engine = _short_of_hours_engine(departure)
    prices = engine.grid_price_cache["prices"]
    hours = charge_hours(engine)
    assert hours
    # the planner reached the "add every hour until departure" step
    assert "Not enought charging planned" in caplog.text
    for timestamp in hours:
        price = prices[timestamp.replace(minute=0)]
        data = engine.charge_hours[timestamp]
        assert data["Price"] == pytest.approx(price, abs=0.011), timestamp
        assert data["Cost"] == pytest.approx(data["kWh"] * price, abs=0.02), timestamp


def test_an_hour_without_a_price_is_skipped_and_its_neighbours_keep_their_price() -> None:
    """One published hour is missing (no price database): it is not planned, the hours around it are."""
    engine = _short_of_hours_engine(datetime.time(8, 0), missing_hours=(5,))
    prices = engine.grid_price_cache["prices"]
    hours = charge_hours(engine)
    assert datetime.datetime.combine(DAY, datetime.time(5)) not in prices
    assert datetime.datetime.combine(DAY, datetime.time(5)) not in hours
    for hour in (4, 6, 7):
        timestamp = datetime.datetime.combine(DAY, datetime.time(hour))
        assert timestamp in hours
        assert engine.charge_hours[timestamp]["Price"] == pytest.approx(prices[timestamp], abs=0.011), timestamp


def test_no_hour_is_planned_without_a_price() -> None:
    """Only published price rows are planned; absent intervals are never priced as another hour."""
    now = datetime.datetime.combine(DAY, datetime.time(21, 30))
    snap = snapshot(now, **PLUGGED_IN, settings={"workplan_charging": True, "workday_friday": True})
    rows = snap.price_data["attributes"]["sensor.price"]["raw_today"]
    snap.price_data["attributes"]["sensor.price"].update(
        raw_today=[row for row in rows if int(row["hour"][11:13]) < 3], raw_tomorrow=[], tomorrow_valid=False, forecast=[]
    )
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, {}).error is None
    assert engine.run_job(snap, engine.charge_if_needed, True).error is None
    prices = engine.grid_price_cache["prices"]
    assert not [timestamp for timestamp in charge_hours(engine) if timestamp not in prices]
    assert not charge_hours(engine)


def test_price_period_capacity_tracks_each_interval_and_current_time() -> None:
    start = datetime.datetime.combine(DAY, datetime.time(12))
    prices = {
        start: 1.0,
        start + datetime.timedelta(minutes=15): 2.0,
        start + datetime.timedelta(minutes=45): 3.0,
        start + datetime.timedelta(hours=1): 4.0,
    }
    engine = SimpleNamespace(
        get_grid_prices=lambda: prices,
        now=lambda: start - datetime.timedelta(hours=1),
        hour_kwh_capacity=lambda _timestamp: MAX_WATT / 1000,
    )
    engine.price_period_end = lambda timestamp: ChargePlan.price_period_end(engine, timestamp)
    engine.charge_plan_start = lambda timestamp: ChargePlan.charge_plan_start(engine, timestamp)
    engine.period_charge_capacity = lambda timestamp, battery_level=None: ChargePlan.period_charge_capacity(engine, timestamp, battery_level)

    assert ChargePlan.period_kwh_capacity(engine, start) == pytest.approx(MAX_WATT / 1000 * 0.25)
    assert ChargePlan.period_kwh_capacity(engine, start + datetime.timedelta(minutes=15)) == pytest.approx(MAX_WATT / 1000 * 0.5)

    engine.now = lambda: start + datetime.timedelta(minutes=30)
    assert ChargePlan.period_kwh_capacity(engine, start + datetime.timedelta(minutes=15)) == pytest.approx(MAX_WATT / 1000 * 0.25)
    assert ChargePlan.charge_plan_start(engine, start + datetime.timedelta(minutes=15)) == start + datetime.timedelta(minutes=30)
    assert ChargePlan.charge_plan_end(engine, start + datetime.timedelta(minutes=15), MAX_WATT / 1000 * 0.25) == start + datetime.timedelta(minutes=45)
    engine.now = lambda: start + datetime.timedelta(minutes=50)
    assert ChargePlan.period_kwh_capacity(engine, start + datetime.timedelta(minutes=15)) == 0.0


def test_charge_overview_merges_only_contiguous_actual_charging_periods() -> None:
    start = datetime.datetime.combine(DAY, datetime.time(12))
    timestamps = [start + datetime.timedelta(minutes=15 * index) for index in range(3)]
    now = start + datetime.timedelta(minutes=7)
    ends = {
        timestamps[0]: timestamps[1],
        timestamps[1]: timestamps[2],
        timestamps[2]: timestamps[2] + datetime.timedelta(minutes=15),
    }
    planner = SimpleNamespace(
        charge_plan_start=lambda timestamp: max(timestamp, now),
        charge_plan_end=lambda timestamp, _kwh: ends[timestamp],
    )
    periods = [
        (timestamp, {"kWh": 1.0, "Cost": index + 1.0, "Price": index + 1.0, "battery_level": 5.0})
        for index, timestamp in enumerate(timestamps)
    ]

    merged, has_combined = Planner.merge_charge_intervals(planner, periods)

    assert has_combined
    assert len(merged) == 1
    assert merged[0]["start"] == now
    assert merged[0]["end"] == ends[timestamps[-1]]
    assert merged[0]["unit"] == pytest.approx(2.0)

    ends[timestamps[0]] = start + datetime.timedelta(minutes=10)
    separated, has_combined = Planner.merge_charge_intervals(planner, periods[:2])
    assert has_combined is False
    assert len(separated) == 2
    assert separated[0]["start"] == now


def test_current_charging_slot_follows_the_active_price_interval() -> None:
    start = datetime.datetime.combine(DAY, datetime.time(12))
    prices = {
        start: 1.0,
        start + datetime.timedelta(minutes=15): 2.0,
        start + datetime.timedelta(minutes=45): 3.0,
    }
    active = start + datetime.timedelta(minutes=15)
    engine = SimpleNamespace(
        get_grid_prices=lambda: prices,
        now=lambda: start + datetime.timedelta(minutes=20),
        charge_hours={active: {"kWh": 1.0}},
    )

    assert ControlSchedule.current_hour_in_charge_hours(engine) == active


def test_expensive_hours_and_cheap_average_remain_hour_based() -> None:
    prices = {
        datetime.datetime.combine(DAY, datetime.time(hour, minute)): float(hour)
        for hour in range(24)
        for minute in (0, 15, 30, 45)
    }
    engine = SimpleNamespace(
        now=lambda: datetime.datetime.combine(DAY, datetime.time(11, 30)),
        get_grid_prices=lambda: prices,
        config={"database": {"kwh_avg_prices_db_data_to_save": 15}},
    )

    expensive = Pricing.get_expensive_hours(engine)
    assert len(expensive) == 4
    assert list(expensive) == [datetime.datetime.combine(DAY, datetime.time(hour)) for hour in (23, 22, 21, 20)]
    assert ChargePlan.cheap_price_check(engine, [], prices, 5.0) == [True, True]

    mixed_interval_prices = {
        datetime.datetime.combine(DAY, datetime.time(hour, minute)): float(hour)
        for hour in range(10)
        for minute in (0, 15, 30, 45)
    }
    mixed_interval_prices.update(
        {datetime.datetime.combine(DAY, datetime.time(hour)): float(hour) for hour in range(10, 24)}
    )
    assert ChargePlan.fill_up_price_limit(engine, [], mixed_interval_prices) == pytest.approx(6.0)


def test_scheduled_planner_books_quarter_hour_price_periods() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(12))
    settings = {
        "workplan_charging": True,
        "workday_friday": True,
        "workday_distance_needed_friday": 150.0,
    }
    snap = snapshot(now, level=10, settings=settings, **PLUGGED_IN)
    rows = snap.price_data["attributes"]["sensor.price"]["raw_today"]
    snap.price_data["attributes"]["sensor.price"]["raw_today"] = [
        row for row in rows if int(row["hour"][11:13]) != 12
    ] + [
        {"hour": (now + datetime.timedelta(minutes=minute)).replace(tzinfo=TZ).isoformat(), "price": 0.1 + minute / 100}
        for minute in (0, 15, 30, 45)
    ]
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, {}).error is None
    assert engine.run_job(snap, engine.charge_if_needed, True).error is None

    planned_periods = [timestamp for timestamp in charge_hours(engine) if timestamp.hour == 12]
    assert planned_periods
    assert any(timestamp.minute for timestamp in planned_periods)
    assert all(
        engine.charge_hours[timestamp]["kWh"] <= engine.hour_kwh_capacity(timestamp) * 0.25 + 0.01
        for timestamp in planned_periods
    )


def _workday_engine(
    now_time: datetime.time,
    *,
    buffer: int = 2,
    level: float = 25.0,
    km: float = 60.0,
    departure: datetime.time = datetime.time(7, 0),
    prices: dict[int, float] | None = None,
    settings: dict | None = None,
) -> tuple[CJPEngine, object]:
    """Work plan on DAY (a Thursday), planned at `now_time`, car plugged in at home.

    prices: price per hour of DAY for the hours given, every other hour costs 2.0."""
    now = datetime.datetime.combine(DAY, now_time)
    settings = {
        "workplan_charging": True,
        "workday_thursday": True,
        "workday_departure_thursday": departure,
        "workday_distance_needed_thursday": km,
        **(settings or {}),
    }
    snap = snapshot(now, level=level, settings=settings, **PLUGGED_IN)
    if prices is not None:
        snap.price_data["attributes"]["sensor.price"]["raw_today"] = [
            {"hour": datetime.datetime.combine(DAY, datetime.time(hour), tzinfo=TZ).isoformat(), "price": prices.get(hour, 2.0)} for hour in range(24)
        ]
    engine = CJPEngine(const.build_config({**OPTIONS, "last_charging_hour_before_departure_buffer": float(buffer)}), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, {}).error is None
    result = engine.run_job(snap, engine.charge_if_needed, True)
    assert result.error is None, result.error
    return engine, result


def test_negative_prices_obey_max_recommended_level() -> None:
    engine, _ = _workday_engine(
        datetime.time(1, 30),
        level=25,
        km=300,
        prices={2: -1.0, 3: -0.9, 4: -0.8, 5: -0.7, 6: -0.6},
    )
    work_hours = [engine.charge_hours[timestamp] for timestamp in charge_hours(engine) if engine.charge_hours[timestamp].get("first_workday_preparation")]
    assert engine.charging_plan[0]["work_battery_level_needed"] + engine.config["ev_car"]["min_daily_battery_level"] > 90
    assert sum(engine.charging_plan[0]["battery_level_before_work"]) == pytest.approx(90, abs=0.1)
    assert max(
        sum(engine.charging_plan[0][key])
        for key in ("battery_level_before_work", "battery_level_after_work", "battery_level_at_midnight")
    ) <= 90.1
    assert sum(hour["battery_level"] for hour in work_hours) == pytest.approx(65, abs=0.1)
    assert sum(hour["kWh"] for hour in work_hours) <= engine.percentage_to_kwh(65, include_charging_loss=True) + 0.1
    assert engine.charging_plan[0]["work_total_cost"] == pytest.approx(sum(hour["Cost"] for hour in work_hours), abs=0.05)
    assert engine.charge_hours["total_cost"] == pytest.approx(
        sum(engine.charge_hours[timestamp]["Cost"] for timestamp in charge_hours(engine)), abs=0.05
    )


def test_negative_prices_do_not_overcharge_when_workday_need_is_below_limit() -> None:
    engine, _ = _workday_engine(
        datetime.time(1, 30),
        level=25,
        km=50,
        prices={2: -1.0, 3: -0.9, 4: -0.8, 5: -0.7, 6: -0.6},
    )
    target = engine.charging_plan[0]["work_battery_level_needed"] + engine.config["ev_car"]["min_daily_battery_level"]
    assert target < 90
    level = sum(engine.charging_plan[0]["battery_level_before_work"])
    assert target <= level <= 90.1


def test_workday_need_above_100_percent_never_plans_past_battery_capacity() -> None:
    engine, _ = _workday_engine(
        datetime.time(1, 30),
        level=25,
        km=500,
        departure=datetime.time(10),
        prices={2: -1.0, 3: -0.9, 4: -0.8, 5: -0.7, 6: -0.6, 7: -0.5, 8: -0.4, 9: -0.3},
        settings={"max_recommended_charge_limit_battery_level": 100.0},
    )
    work_hours = [engine.charge_hours[timestamp] for timestamp in charge_hours(engine) if engine.charge_hours[timestamp].get("first_workday_preparation")]
    assert engine.charging_plan[0]["work_battery_level_needed"] + engine.config["ev_car"]["min_daily_battery_level"] > 100
    assert sum(engine.charging_plan[0]["battery_level_before_work"]) <= 100.1
    assert sum(hour["battery_level"] for hour in work_hours) <= 75.1
    assert sum(hour["kWh"] for hour in work_hours) <= engine.percentage_to_kwh(75, include_charging_loss=True) + 0.1
    assert engine.charge_hours["total_cost"] == pytest.approx(
        sum(engine.charge_hours[timestamp]["Cost"] for timestamp in charge_hours(engine)), abs=0.05
    )


@pytest.mark.parametrize(
    ("work_need", "trip_need", "expected_work_cost", "expected_trip_cost"),
    [(100.0, 100.0, -30.0, 0.0), (40.0, 20.0, -20.0, -10.0)],
)
def test_scheduled_cost_split_uses_only_capacity_available_to_each_rule(
    monkeypatch: pytest.MonkeyPatch,
    work_need: float,
    trip_need: float,
    expected_work_cost: float,
    expected_trip_cost: float,
) -> None:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    monkeypatch.setattr(engine, "remove_solar_prediction_from_charge_hours", lambda *args, **kwargs: None)
    monkeypatch.setattr(engine, "add_charging_session_to_day", lambda *args, **kwargs: "battery_level_before_work")
    monkeypatch.setattr(engine, "add_charging_to_days", lambda *args, **kwargs: None)
    ctx = SimpleNamespace(
        charging_plan={
            0: {
                "trip_battery_level_needed": trip_need,
                "trip_battery_level_above_max": 0.0,
                "work_battery_level_needed": work_need,
                "workday": True,
                "rules": ["trip", "first_workday_preparation"],
                "trip_total_cost": 0.0,
                "work_total_cost": 0.0,
            }
        },
        energy_prediction_db={},
    )

    engine.scheduled_charging_added(
        ctx, 0, 0, datetime.datetime.combine(DAY, datetime.time(2)), "battery_level_before_work", 1.0, -30.0
    )

    assert ctx.charging_plan[0]["work_total_cost"] == pytest.approx(expected_work_cost)
    assert ctx.charging_plan[0]["trip_total_cost"] == pytest.approx(expected_trip_cost)


@pytest.mark.parametrize("buffer", [0, 1, 2, 3])
def test_charging_is_done_the_set_number_of_hours_before_departure(buffer: int) -> None:
    """Departure 07:00: the last hour that is charged ends `buffer` hours before it, also when a later hour is cheaper."""
    engine, _ = _workday_engine(datetime.time(1, 30), buffer=buffer, prices={6: 0.1, 5: 0.2, 4: 0.3})
    departure = datetime.datetime.combine(DAY, datetime.time(7))
    # the hours planned for this departure (the evening's charging to the daily level has its own hours)
    hours = [timestamp for timestamp in charge_hours(engine) if engine.charge_hours[timestamp].get("first_workday_preparation")]
    assert hours
    assert max(hours) + datetime.timedelta(hours=1) <= departure - datetime.timedelta(hours=buffer)
    # the cheapest hour that is still allowed is used
    cheapest_allowed = datetime.datetime.combine(DAY, datetime.time(6 - buffer)) if buffer <= 2 else None
    assert cheapest_allowed is None or cheapest_allowed in hours


@pytest.mark.parametrize(
    ("now_time", "action"),
    [(datetime.time(7, 30), "charge"), (datetime.time(8, 30), "charge"), (datetime.time(9, 30), "stop")],
    ids=["30 min after departure", "90 min after departure", "150 min after departure"],
)
def test_charging_continues_up_to_2_hours_after_departure_when_the_target_is_not_reached(now_time: datetime.time, action: str) -> None:
    """The car is still plugged in at home after its departure and is below what the day needs: it keeps charging for 2 hours."""
    engine, _ = _workday_engine(now_time, level=20, km=150)
    assert engine.charger_action == action


@pytest.mark.parametrize("hour", [7, 8], ids=["30 min after", "90 min after"])
def test_trip_charging_continues_up_to_2_hours_after_departure(hour: int) -> None:
    """Trip departure 07:00 today, the car is still plugged in at home and below the trip's target (run() plans at :30).

    The trip is reset 2 hours after the departure, so there is no later case."""
    settings = {
        "trip_date_time": datetime.datetime.combine(DAY, datetime.time(7)),
        "trip_homecoming_date_time": datetime.datetime.combine(DAY, datetime.time(18)),
        "trip_charge_procent": 80.0,
    }
    engine, _ = run(hour, level=30, settings=settings, **PLUGGED_IN)
    assert engine.charger_action == "charge"


def test_charging_does_not_stop_in_the_departure_minute() -> None:
    """At 07:00 sharp (the cron runs at whole minutes) the car is still plugged in and below its level: it keeps charging."""
    for now_time in (datetime.time(6, 55), datetime.time(7, 0), datetime.time(7, 5)):
        engine, _ = _workday_engine(now_time, level=20, km=150)
        assert engine.charger_action == "charge", now_time


def test_a_trip_above_the_max_level_is_never_charged_in_hours_that_have_passed() -> None:
    """Trip at 09:00 to 100 % with the max level at 60 %, planned at 06:30 (inside the 2 hour buffer): only hours from now on."""
    settings = {
        "trip_date_time": datetime.datetime.combine(DAY, datetime.time(9)),
        "trip_homecoming_date_time": datetime.datetime.combine(DAY, datetime.time(18)),
        "trip_charge_procent": 100.0,
        "max_recommended_charge_limit_battery_level": 60.0,
    }
    engine, _ = run(6, level=40, settings=settings, **PLUGGED_IN)
    trip_hours = [timestamp for timestamp in charge_hours(engine) if engine.charge_hours[timestamp].get("trip")]
    assert trip_hours
    assert min(trip_hours) >= datetime.datetime.combine(DAY, datetime.time(6)), trip_hours
    # the hours inside the buffer are used, up to the departure hour
    assert max(trip_hours) < datetime.datetime.combine(DAY, datetime.time(9)), trip_hours


def test_a_short_workday_gives_a_shorter_extension_after_the_departure() -> None:
    """Departure 07:00, homecoming 08:30: the extension stops before the homecoming, it is not dropped."""
    settings = {"workday_homecoming_thursday": datetime.time(8, 30)}
    for now_time in (datetime.time(7, 30), datetime.time(8, 15)):
        engine, _ = _workday_engine(now_time, level=20, km=150, settings=settings)
        assert engine.charger_action == "charge", now_time
        plan = engine.charging_plan[0]
        assert plan["work_goto"] < plan["work_homecoming"]


def test_a_trip_after_work_is_not_charged_while_the_car_is_at_work() -> None:
    """Work 07:00-16:00 and a trip at 17:00 to 100 % (max level 60 %), planned at 05:30: no trip hour while the car is away."""
    settings = {
        "workplan_charging": True,
        "workday_thursday": True,
        "trip_date_time": datetime.datetime.combine(DAY, datetime.time(17)),
        "trip_homecoming_date_time": datetime.datetime.combine(DAY, datetime.time(21)),
        "trip_charge_procent": 100.0,
        "max_recommended_charge_limit_battery_level": 60.0,
    }
    engine, _ = run(5, level=40, settings=settings, **PLUGGED_IN)
    trip_hours = [timestamp for timestamp in charge_hours(engine) if engine.charge_hours[timestamp].get("trip")]
    assert trip_hours
    at_work = [timestamp for timestamp in trip_hours if datetime.datetime.combine(DAY, datetime.time(7)) <= timestamp < datetime.datetime.combine(DAY, datetime.time(16))]
    assert not at_work, at_work


def test_the_analysis_saves_one_departure_as_set_while_the_car_is_charged_after_it() -> None:
    """The plan's goto moves up to 2 hours later for a car still plugged in; the analysis saves the set departure once, not a later one too."""
    settings = {"workplan_charging": True, "workday_thursday": True, "workday_distance_needed_thursday": 150.0}
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    for index, now_time in enumerate((datetime.time(6, 55), datetime.time(7, 5), datetime.time(9, 0))):
        snap = snapshot(datetime.datetime.combine(DAY, now_time), level=20, settings=settings, **PLUGGED_IN)
        if index == 0:
            assert engine.run_job(snap, engine.startup, {}).error is None
        assert engine.run_job(snap, engine.charge_if_needed, True).error is None
        assert engine.run_job(snap, engine.analysis_check_departure).error is None
    departures = engine.analysis_db["departures"]
    assert len(departures) == 1
    assert departures[0]["departure"] == datetime.datetime.combine(DAY, datetime.time(7)).replace(tzinfo=TZ).timestamp()


def test_no_charging_after_departure_when_the_target_is_reached() -> None:
    engine, _ = _workday_engine(datetime.time(7, 30), level=90, km=60)
    assert engine.charger_action == "stop"
    assert not charge_hours(engine)


@pytest.mark.parametrize("departure", [datetime.time(7, 0), datetime.time(7, 30)])
def test_hours_added_when_charging_falls_short_stay_within_the_hour_capacity(departure: datetime.time) -> None:
    """Also with a departure at 07:30 an hour is booked once: no slots at :30 on top of the whole hours (up to 16.5 kWh in one hour)."""
    engine = _short_of_hours_engine(departure)
    hours = [timestamp for timestamp in charge_hours(engine) if timestamp.date() == DAY]
    assert hours
    assert not [timestamp for timestamp in hours if timestamp.minute], hours
    assert max(engine.charge_hours[timestamp]["kWh"] for timestamp in hours) <= MAX_WATT / 1000 + 0.01


def _cheap_power_engine(tomorrow: dict[int, float], very: float = 40.0, ultra: float = 90.0) -> CJPEngine:
    """Only 'Charge on cheap power' is on; today costs 1.0 all day, tomorrow `tomorrow` (hour -> price, other hours 1.0)."""
    now = datetime.datetime.combine(DAY, datetime.time(12, 30))
    settings = {
        "cheap_grid_charging": True,
        "very_cheap_grid_charging_max_battery_level": very,
        "ultra_cheap_grid_charging_max_battery_level": ultra,
        "max_recommended_charge_limit_battery_level": 100.0,
    }
    snap = snapshot(now, level=10, settings=settings, **PLUGGED_IN)
    attributes = snap.price_data["attributes"]["sensor.price"]
    next_day = DAY + datetime.timedelta(days=1)
    attributes["raw_today"] = [{"hour": datetime.datetime.combine(DAY, datetime.time(hour), tzinfo=TZ).isoformat(), "price": 1.0} for hour in range(24)]
    attributes["raw_tomorrow"] = [
        {"hour": datetime.datetime.combine(next_day, datetime.time(hour), tzinfo=TZ).isoformat(), "price": tomorrow.get(hour, 1.0)} for hour in range(24)
    ]
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, {}).error is None
    assert engine.run_job(snap, engine.charge_if_needed, True).error is None
    return engine


def test_ultra_cheap_is_cheaper_than_very_cheap_also_with_a_negative_average() -> None:
    """The 15 lowest prices average -2.0 (paid to charge). An hour at that average is very cheap, not ultra cheap:
    ultra cheap is 25 % under the average (-2.5), where 75 % of -2.0 (-1.5) would be above it."""
    engine = _cheap_power_engine({hour: -2.0 for hour in range(15)})
    hours = charge_hours(engine)
    assert hours
    assert all(engine.charge_hours[timestamp].get("under_min_avg_price") for timestamp in hours)
    assert not [timestamp for timestamp in hours if engine.charge_hours[timestamp].get("half_min_avg_price")]
    # so the plan only charges to the very cheap level (40 %), not to the ultra cheap level (90 %)
    assert 10 + sum(engine.charge_hours[timestamp]["battery_level"] for timestamp in hours) == pytest.approx(40.0, abs=1.5)


def test_ultra_cheap_hours_with_a_negative_average_are_25_percent_under_it() -> None:
    """2 hours at -5.0, 8 at -3.0, 5 at -0.5: the average is -2.43, ultra cheap starts at -3.04.
    The -5.0 hours are ultra cheap, the -3.0 hours only very cheap (with 75 % of the average, -1.83, they were ultra cheap too)
    and the -0.5 hours are not cheap at all. Very cheap power charges to 40 %, ultra cheap to 90 %."""
    engine = _cheap_power_engine({**{hour: -5.0 for hour in range(2)}, **{hour: -3.0 for hour in range(2, 10)}, **{hour: -0.5 for hour in range(10, 15)}})
    tomorrow = DAY + datetime.timedelta(days=1)
    hours = {timestamp: engine.charge_hours[timestamp] for timestamp in charge_hours(engine) if timestamp.date() == tomorrow}
    by_price = {price: [data for data in hours.values() if data["Price"] == price] for price in (-5.0, -3.0, -0.5)}
    assert by_price[-5.0] and all(data.get("half_min_avg_price") for data in by_price[-5.0])
    assert by_price[-3.0] and all(data.get("under_min_avg_price") and not data.get("half_min_avg_price") for data in by_price[-3.0])
    assert not by_price[-0.5]
    # 10 % + the two ultra cheap hours (28 %) + a little of the very cheap ones: the very cheap level
    assert 10 + sum(data["battery_level"] for data in hours.values()) == pytest.approx(40.0, abs=1.5)


def test_the_ultra_cheap_limit_keeps_the_75_percent_for_a_positive_average() -> None:
    from custom_components.cable_juice_planner.utils.helpers import ultra_cheap_limit

    assert ultra_cheap_limit(2.0) == 2.0 * 0.75
    assert ultra_cheap_limit(0.0) == 0.0
    assert ultra_cheap_limit(-2.0) == pytest.approx(-2.5)


def test_the_low_forecast_price_of_ultra_cheap_follows_the_same_limit() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(12, 30))
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    snap = snapshot(now, **PLUGGED_IN)
    engine.run_job(snap, engine.startup, {})
    for minimum, ultra in ((2.0, 1.5), (-2.0, -2.5)):
        engine.kwh_avg_prices_db = {"min": [minimum]}
        result = engine.run_job(snap, engine.set_low_forecast_price)
        assert result.error is None, result.error
        assert result.attributes["charge_ultra_cheap_battery_level"]["low_forecast_price"] == pytest.approx(ultra, abs=0.01)


def _battery_level_with(state: str, history: list[tuple[float, str]], real: bool = False) -> float:
    """The planning level (real: the car's own level for measurements) when the car sensor says `state` and has had `history` (hours ago, state) before."""
    now = datetime.datetime.combine(DAY, datetime.time(12, 30))
    snap = snapshot(now, **PLUGGED_IN)
    snap.states["sensor.ev_battery_level"] = state
    snap.buffers = {
        "sensor.ev_battery_level": sorted(((now - datetime.timedelta(hours=hours)).replace(tzinfo=TZ).timestamp(), value) for hours, value in history)
    }
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    result = engine.run_job(snap, engine.battery_level, real)
    assert result.error is None, result.error
    return result.value


def test_the_cars_own_level_for_measurements_still_looks_a_day_back() -> None:
    """Only the level the plan starts from has the 6 hour limit for a broken value; drive and charging measurements keep the last value of the day."""
    assert _battery_level_with("unavailable", [(8, "55"), (7, "unavailable")], real=True) == pytest.approx(55.0)
    assert _battery_level_with("nan", [(8, "55"), (7, "nan")], real=True) == pytest.approx(55.0)
    assert _battery_level_with("nan", [(30, "55"), (29, "nan")], real=True) == 0.0


@pytest.mark.parametrize("state", ["nan", "inf", "-inf", "abc"])
def test_a_broken_battery_level_is_the_last_known_one_for_6_hours(state: str) -> None:
    # 55 % until 3 hours ago, then the sensor gave something that is not a number
    assert _battery_level_with(state, [(7, "55"), (3, state)]) == pytest.approx(55.0)
    # a level that has not changed since before the window counts too (55 % from 20 hours ago, broken since 4 hours ago)
    assert _battery_level_with(state, [(20, "55"), (4, state), (1, state)]) == pytest.approx(55.0)


@pytest.mark.parametrize("state", ["nan", "inf", "-inf", "abc"])
def test_a_broken_battery_level_for_more_than_6_hours_starts_from_0(state: str) -> None:
    assert _battery_level_with(state, [(8, "55"), (7, state)]) == 0.0
    assert _battery_level_with(state, []) == 0.0
    # a number that was recorded as nan/inf is not a level either
    assert _battery_level_with(state, [(2, "nan"), (1, "inf")]) == 0.0


@pytest.mark.parametrize("state", ["unavailable", "unknown"])
def test_an_unavailable_battery_level_is_the_last_known_one_for_a_day(state: str) -> None:
    """A car that is asleep is unavailable for hours: its last level counts for a day (as before), then the plan starts from 0 %."""
    assert _battery_level_with(state, [(8, "55"), (7, state)]) == pytest.approx(55.0)
    assert _battery_level_with(state, [(23, "55"), (22, state)]) == pytest.approx(55.0)
    assert _battery_level_with(state, [(30, "55"), (29, state)]) == 0.0
    assert _battery_level_with(state, []) == 0.0
    assert _battery_level_with(state, [(2, "nan"), (1, "inf")]) == 0.0


def test_a_good_battery_level_is_used_as_it_is() -> None:
    assert _battery_level_with("62.5", [(3, "40"), (1, "55")]) == pytest.approx(62.5)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"])
def test_prices_that_are_not_finite_numbers_are_never_planned(bad: float) -> None:
    """nan/inf rows are dropped like missing hours (no price history here): not planned at all, and no nan/inf in the plan."""
    now = datetime.datetime.combine(DAY, datetime.time(12, 30))
    snap = snapshot(now, **PLUGGED_IN, settings={"fill_up": True})
    attributes = snap.price_data["attributes"]["sensor.price"]
    for row in attributes["raw_tomorrow"]:
        if int(row["hour"][11:13]) in (1, 2, 3):
            row["price"] = bad
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, {}).error is None
    assert engine.run_job(snap, engine.charge_if_needed, True).error is None
    tomorrow = DAY + datetime.timedelta(days=1)
    assert not [hour for hour in (1, 2, 3) if datetime.datetime.combine(tomorrow, datetime.time(hour)) in engine.grid_price_cache["prices"]]
    hours = charge_hours(engine)
    assert hours
    for timestamp in hours:
        assert all(math.isfinite(engine.charge_hours[timestamp][key]) for key in ("Price", "Cost", "kWh")), timestamp
    assert math.isfinite(engine.charge_hours["total_cost"])


def test_offline_prices_use_the_same_weekday() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))
    snap = snapshot(now)
    snap.price_data = {"attributes": {}, "values": {}}
    snap.attributes = {}
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    engine.run_job(snap, engine.startup, {"kwh_avg_prices": _price_history()})
    engine.run_job(snap, engine.get_grid_prices, True)
    prices = engine.grid_price_cache["prices"]
    assert engine.grid_price_cache["using_offline_prices"] is True
    for days in range(7):
        timestamp = now + datetime.timedelta(days=days)
        assert prices[timestamp] == pytest.approx(round(2.0 + timestamp.weekday() / 10 + days / 30, 2))


def test_price_history_never_stores_the_filled_hours() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))
    snap = snapshot(now)
    rows = snap.price_data["attributes"]["sensor.price"]["raw_today"]
    snap.price_data["attributes"]["sensor.price"]["raw_today"] = [row for row in rows if row["hour"][11:13] not in ("14", "15", "16")]
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, {"kwh_avg_prices": _price_history()}).error is None
    assert engine.run_job(snap, engine.update_grid_prices).error is None
    assert engine.grid_price_cache["sources"][now.replace(hour=15)] == "database"
    # 21 real hours: the day is not stored, so the filled hours never become history
    assert engine.kwh_avg_prices_db["history"][15][DAY.weekday()] == _price_history()["history"][15][DAY.weekday()]


def test_price_history_averages_mixed_intervals_by_covered_duration() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(12))
    snap = snapshot(now)
    rows = snap.price_data["attributes"]["sensor.price"]["raw_today"]
    snap.price_data["attributes"]["sensor.price"]["raw_today"] = [
        row for row in rows if row["hour"][11:13] != "12"
    ] + [
        {"hour": (now + datetime.timedelta(minutes=minute)).replace(tzinfo=TZ).isoformat(), "price": price}
        for minute, price in ((0, 0.1), (15, 0.2), (45, 0.4))
    ]
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, {"kwh_avg_prices": _price_history()}).error is None
    assert engine.run_job(snap, engine.update_grid_prices).error is None

    assert engine.kwh_avg_prices_db["history"][12][DAY.weekday()][0] == pytest.approx(0.225)


def _assert_price_history_repaired(engine: CJPEngine, good: dict) -> None:
    today, other = DAY.weekday(), (DAY.weekday() + 1) % 7
    for name in ("history", "history_sell"):
        history = engine.kwh_avg_prices_db[name]
        assert all(isinstance(history[h][d], list) for h in range(24) for d in range(7))
        assert history[15][other] == good[name][15][other]
    # today's prices are stored again, in front of the old ones
    assert engine.kwh_avg_prices_db["history"][15][today][1:] == good["history"][15][today]
    assert engine.kwh_avg_prices_db["last_save"].date() == DAY


def test_price_history_nested_twice_is_repaired(caplog: pytest.LogCaptureFixture) -> None:
    """A version 2.0 database that an earlier migration wrapped once more ({hour: {weekday: {weekday: [...]}}})."""
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))
    snap = snapshot(now)
    snap.price_data["attributes"]["sensor.price"].update(raw_tomorrow=[], tomorrow_valid=False, forecast=[])
    good = _price_history()
    broken = deepcopy(good)
    for name in ("history", "history_sell"):
        broken[name] = {h: {d: deepcopy(good[name][h]) for d in range(7)} for h in range(24)}
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    started = engine.run_job(snap, engine.startup, {"kwh_avg_prices": broken})
    assert started.error is None, started.error
    assert "kwh_avg_prices" in started.db_saves
    assert engine.run_job(snap, engine.update_grid_prices).error is None
    assert "Can't append KWH avg prices" not in caplog.text
    _assert_price_history_repaired(engine, good)

    # the hours without a provider price get the history of that weekday, not 0 + the daily adder
    # (not day 7: today's weekday now starts with today's price)
    assert engine.run_job(snap, engine.charge_if_needed, True).error is None
    prices = engine.grid_price_cache["prices"]
    for days in (1, 6):
        timestamp = now.replace(hour=17) + datetime.timedelta(days=days)
        assert engine.grid_price_cache["sources"][timestamp] == "database"
        assert prices[timestamp] == pytest.approx(round(2.0 + timestamp.weekday() / 10 + days / 30, 2))


def test_price_history_version_1_is_transformed() -> None:
    """Version 1.0 had one list per hour; every weekday starts with that list."""
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))
    good = _price_history()
    old = {**deepcopy(good), "version": 1.0}
    for name in ("history", "history_sell"):
        old[name] = {h: [2.5] * 3 for h in range(24)}
        for h in range(24):
            good[name][h] = {d: [2.5] * 3 for d in range(7)}
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snapshot(now), engine.startup, {"kwh_avg_prices": old}).error is None
    assert engine.run_job(snapshot(now), engine.update_grid_prices).error is None
    _assert_price_history_repaired(engine, good)


def test_failed_price_append_leaves_the_database_unchanged(caplog: pytest.LogCaptureFixture) -> None:
    """max/mean/min are not changed when storing the day fails halfway (they used to gain today's value every hour)."""
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.run_job(snapshot(now), engine.startup, {"kwh_avg_prices": _price_history()}).error is None
    engine.kwh_avg_prices_db.pop("last_save", None)
    engine.kwh_avg_prices_db["history"][15] = None
    before = deepcopy(engine.kwh_avg_prices_db)
    result = engine.run_job(snapshot(now), engine.update_grid_prices)
    assert result.error is None
    assert "Can't append KWH avg prices" in caplog.text
    assert engine.kwh_avg_prices_db == before
    assert "kwh_avg_prices" not in result.db_saves


@pytest.mark.parametrize(("fixed", "sign"), [(-1.0, -1.0), (1.0, 1.0)], ids=["hourly sell price", "fixed solar sell price"])
def test_history_sell_prices_get_the_adder_that_makes_a_sale_worth_less(fixed: float, sign: float) -> None:
    """Hours filled from the price history get the same daily adder as the provider forecasts: a buy price goes
    up, a sell price down (with a fixed solar sell price a sold kWh is worth fixed - hourly, so it goes up)."""
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))
    snap = snapshot(now)
    engine = CJPEngine(const.build_config({**OPTIONS, "solar_sell_fixed_price": fixed}), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, {"kwh_avg_prices": _price_history()}).error is None
    target = datetime.datetime.combine(DAY + datetime.timedelta(days=2), datetime.time(12))  # a Saturday: 2.5 in the history
    buy = engine.run_job(snap, engine._database_prices, "history", {}, 4)
    sell = engine.run_job(snap, engine._database_prices, "history_sell", {}, 4)
    assert buy.error is None and sell.error is None, (buy.error, sell.error)
    assert buy.value[target] == pytest.approx(2.5 + 2 / 30, abs=0.006)
    assert sell.value[target] == pytest.approx(2.5 + sign * 2 / 30, abs=0.006)


def test_sell_value_with_fixed_solar_sell_price() -> None:
    """A fixed price settles the difference to the hourly price: fixed - hourly, also negative."""
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.sell_value(5.0) == pytest.approx(5.0)
    engine = CJPEngine(const.build_config({**OPTIONS, "solar_sell_fixed_price": 1.0}), "cjp_test", TEXTS)
    assert engine.sell_value(5.0) == pytest.approx(-4.0)
    assert engine.sell_value(0.5) == pytest.approx(0.5)


@pytest.mark.parametrize("fixed", [-1.0, 1.0])
def test_solar_sell_price_with_fixed_price(fixed: float) -> None:
    """The solar kWh is worth what selling it would earn: the hourly sell price, or fixed - hourly."""
    options = {**OPTIONS, "production_entity_id": "sensor.solar", "solar_sell_fixed_price": fixed}
    now = datetime.datetime.combine(DAY, datetime.time(18, 0))
    snap = snapshot(now)
    snap.states["sensor.solar"] = "0"
    engine = CJPEngine(const.build_config(options), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, {}).error is None
    result = engine.run_job(snap, engine.get_solar_sell_price)
    assert result.error is None, result.error
    hourly = engine.price_provider.get_sell_price(now)["price"]
    assert result.value == pytest.approx(hourly if fixed == -1.0 else 1.0 - hourly, abs=0.001)
    if fixed != -1.0:
        assert result.value < 0.0  # 18:00 sells for more than the fixed price


def test_solcast_alone_when_there_is_no_solar_history() -> None:
    """A new installation has no solar history: Solcast alone, not averaged with an unknown 0 (0.75 x Solcast)."""
    options = {**OPTIONS, "production_entity_id": "sensor.solar", "forecast_entity_id": "sensor.solcast_today"}
    now = datetime.datetime.combine(DAY, datetime.time(3, 30))
    noon = datetime.datetime.combine(DAY + datetime.timedelta(days=2), datetime.time(12))  # a Saturday, not a workday
    snap = snapshot(now)
    snap.states["sensor.solar"] = "0"
    daily = [{"datetime": datetime.datetime.combine(DAY + datetime.timedelta(days=day), datetime.time(0)).isoformat(), "condition": "sunny"} for day in range(8)]
    snap.forecast_dict = {"hourly": [{"datetime": noon.isoformat(), "condition": "sunny"}], "daily": daily}
    engine = CJPEngine(const.build_config(options), "cjp_test", TEXTS)
    assert engine.run_job(snap, engine.startup, {}).error is None
    engine.get_solar_kwh_forecast = lambda: {noon: (2.0, 0.5)}
    result = engine.run_job(snap, engine.local_energy_prediction)
    assert result.error is None, result.error
    assert engine.local_energy_prediction_db["solar_prediction_timestamps"][noon] == pytest.approx(2.0)


def _public_job(engine: CJPEngine, minute: int, func, *args, level: float, port: str, cost: float = 0.0, location: str = "not_home", location_history: list | None = None):
    now = datetime.datetime.combine(DAY, datetime.time(12, minute))
    snap = snapshot(now, level=level, port=port, settings={"public_charging_session_cost": cost})
    snap.states["device_tracker.ev"] = location
    if location_history is not None:
        # (minutes before now, state)
        snap.buffers = {"device_tracker.ev": [(now.replace(tzinfo=TZ).timestamp() - ago * 60, value) for ago, value in location_history]}
    result = engine.run_job(snap, func, *args)
    assert result.error is None, result.error
    return result


def test_public_charging_session_still_charging_is_not_in_the_queue() -> None:
    """A second public charge starts before the first one's cost is entered: only finished charges wait for a cost."""
    engine = _loss_engine()
    queue = lambda: engine.sensor_attrs.get("public_charging_session_done", {}).get("public_charging_session")  # noqa: E731
    _public_job(engine, 0, engine.public_charging_session, "on", level=30, port="on")
    _public_job(engine, 30, engine.public_charging_session, "off", level=70, port="off")
    assert engine.sensor_states["public_charging_session_done"] == "on"
    # plugged in again away from home, the first charge's cost not entered yet
    _public_job(engine, 40, engine.public_charging_session, "on", level=65, port="on")
    assert len(engine.public_charging_sessions) == 2
    assert [s["added_percentage"] for s in queue()] == [40]
    # a restart while charging: the queue still leaves out the charge in progress
    restarted = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    runtime = {"public_charging_session_done": "on", "public_charging_session": deepcopy(engine.public_charging_sessions)}
    assert _public_job(restarted, 41, restarted.startup, {"runtime": runtime}, level=66, port="on")
    assert [s["added_percentage"] for s in restarted.sensor_attrs["public_charging_session_done"]["public_charging_session"]] == [40]
    assert restarted.sensor_states["public_charging_session_done"] == "on"
    # entering the first cost adds it and keeps the charge in progress
    _public_job(engine, 45, engine.add_public_charging_session, level=65, port="on", cost=120.0)
    assert engine.sensor_states["public_charging_session_done"] == "off"
    assert engine.public_charging_sessions == [{"start_battery_level": 65.0}]
    assert queue() is None
    public = [row for row in engine.charging_history_db.values() if row.get("public_cost")]
    assert [(row["percentage"], row["public_cost"]) for row in public] == [(40.0, 120.0)]
    # the second charge ends and waits for its cost
    _public_job(engine, 50, engine.public_charging_session, "off", level=90, port="off")
    assert engine.sensor_states["public_charging_session_done"] == "on"
    assert [s["added_percentage"] for s in queue()] == [25]


@pytest.mark.parametrize(
    ("location_entity", "location", "location_history", "detected"),
    [
        ("device_tracker.ev", "not_home", None, True),
        ("device_tracker.ev", "Work", None, True),  # another zone is away too
        ("device_tracker.ev", "home", None, False),
        ("", "not_home", None, False),  # no location entity: the charger can't tell, its status comes minutes after the car's port
        ("device_tracker.missing", "not_home", None, False),  # the configured entity is gone from Home Assistant
        ("device_tracker.ev", "unknown", None, False),
        ("device_tracker.ev", "unavailable", [], False),
        ("device_tracker.ev", "unavailable", [(60, "home")], False),
        ("device_tracker.ev", "unavailable", [(60, "not_home")], True),  # the last known location
    ],
)
def test_public_charging_session_needs_a_known_location(location_entity: str, location: str, location_history: list | None, detected: bool) -> None:
    """Only a car known to be away gets a public charge; no or an unknown location never starts or ends one."""
    engine = CJPEngine(const.build_config({**OPTIONS, "location_entity_id": location_entity}), "cjp_test", TEXTS)
    assert engine.run_job(snapshot(datetime.datetime.combine(DAY, datetime.time(1, 0))), engine.startup, {}).error is None
    _public_job(engine, 0, engine.public_charging_session, "on", level=30, port="on", location=location, location_history=location_history)
    assert bool(engine.public_charging_sessions) is detected
    _public_job(engine, 30, engine.public_charging_session, "off", level=70, port="off", location=location, location_history=location_history)
    assert (engine.sensor_states.get("public_charging_session_done") == "on") is detected
    assert bool(engine.sensor_attrs.get("public_charging_session_done", {}).get("public_charging_session")) is detected


def test_no_public_charging_without_a_location() -> None:
    """The location removed: a waiting session isn't restored (no entity to enter its cost), the port starts none."""
    engine = CJPEngine(const.build_config({**OPTIONS, "location_entity_id": ""}), "cjp_test", TEXTS)
    waiting = [{"start_battery_level": 30.0, "end_battery_level": 70.0, "added_percentage": 40.0, "timestamp": datetime.datetime.combine(DAY, datetime.time(0, 30))}]
    runtime = {"public_charging_session_done": "on", "public_charging_session": waiting}
    assert engine.run_job(snapshot(datetime.datetime.combine(DAY, datetime.time(1, 0))), engine.startup, {"runtime": runtime}).error is None
    assert engine.public_charging_sessions == []
    assert not engine.get_public_charging_session_done()
    assert "public_charging_session_done" not in engine.sensor_states
    _public_job(engine, 0, engine.ev_port_changed, "on", "off", level=30, port="on")
    _public_job(engine, 30, engine.ev_port_changed, "off", "on", level=70, port="off")
    assert engine.public_charging_sessions == []
    assert "public_charging_session_done" not in engine.sensor_states


SOLAR_OPTIONS = {**OPTIONS, "production_entity_id": "sensor.solar", "allow_grid_charging_above_solar_available": -100.0}


def _amps_rows(table: str) -> list[tuple[int, int, int, int]]:
    import re

    return [tuple(int(v) for v in m.groups()) for m in re.finditer(r"^\| (\d+) W \| (\d+) A \| (\d) \| (\d+) W \|$", table, re.MULTILINE)]


def _check_table_against_schedule(engine: CJPEngine, limit: float, rows: list[tuple[int, int, int, int]]) -> None:
    """Every row is where calc_charging_amps (called with the inverter limit, like the schedule) switches to it."""
    previous_watt = 0
    for start, amps, _phase, watt in rows:
        above = engine.calc_charging_amps(start - 100.0 + 1, max_allowed=limit)
        below = engine.calc_charging_amps(start - 100.0 - 1, max_allowed=limit)
        assert (above[0], above[3]) == (amps, watt)
        assert below[3] == previous_watt
        previous_watt = watt
    # the last row takes every larger surplus: no step above it is ever chosen
    assert engine.calc_charging_amps(100000.0, max_allowed=limit)[3] == previous_watt


def test_charging_amps_table_follows_the_solar_steps() -> None:
    """The dashboard table starts each step where calc_charging_amps switches to it (surplus + allowed difference)."""
    engine = CJPEngine(const.build_config({**SOLAR_OPTIONS, "inverter_discharging_power_limit": 20000.0}), "cjp_test", TEXTS)
    table = engine.charging_amps_table()
    assert table is not None
    assert "-100 W" in table.splitlines()[0]
    rows = _amps_rows(table)
    # 1 phase 6-16 A on 230 V, then 3 phases from the first step above what 1 phase gives (3680 W)
    assert rows[0] == (1455, 6, 1, 1380)
    assert (4010, 6, 3, 4140) in rows
    assert rows[-1] == (10795, 16, 3, 11040)
    assert "| – | 0 A | – | 0 W |" in table
    _check_table_against_schedule(engine, 20000.0, rows)


def test_charging_amps_table_stops_at_the_inverter_limit() -> None:
    """The schedule passes the inverter limit as max_allowed, so steps above it can never be chosen."""
    engine = CJPEngine(const.build_config(SOLAR_OPTIONS), "cjp_test", TEXTS)
    limit = engine.config["solar"]["inverter_discharging_power_limit"]
    assert limit == 5000.0
    rows = _amps_rows(engine.charging_amps_table())
    assert rows[-1] == (4585, 7, 3, 4830)  # 8 A on 3 phases (5520 W) is above the limit
    assert all(watt <= limit for *_rest, watt in rows)
    _check_table_against_schedule(engine, limit, rows)


def test_charging_amps_table_with_an_inverter_limit_below_the_first_step() -> None:
    """calc_charging_amps raises a limit below the first step to the 0 A step: no charging, only the 0 A row."""
    engine = CJPEngine(const.build_config({**SOLAR_OPTIONS, "inverter_discharging_power_limit": 1000.0}), "cjp_test", TEXTS)
    table = engine.charging_amps_table()
    assert _amps_rows(table) == []
    assert "| – | 0 A | – | 0 W |" in table
    assert engine.calc_charging_amps(100000.0, max_allowed=1000.0)[3] == 0.0


def test_charging_amps_table_uses_the_allowed_difference() -> None:
    engine = CJPEngine(const.build_config({**SOLAR_OPTIONS, "allow_grid_charging_above_solar_available": 0.0}), "cjp_test", TEXTS)
    table = engine.charging_amps_table()
    assert _amps_rows(table)[0] == (1355, 6, 1, 1380)
    assert "0 W" in table.splitlines()[0]


def test_charging_amps_without_solar_only_in_the_startup_log() -> None:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    assert engine.charging_amps_table() is None
    assert engine.charging_amps_report() == f"Max charging: 16.0A 3.0 phase = {float(MAX_WATT)}W"
    result = engine.run_job(snapshot(datetime.datetime.combine(DAY, datetime.time(1, 0))), engine.startup, {})
    assert result.error is None
    assert "charging_amps_table" not in result.states


def test_startup_publishes_the_charging_amps_table_with_solar() -> None:
    engine = CJPEngine(const.build_config(SOLAR_OPTIONS), "cjp_test", TEXTS)
    snap = snapshot(datetime.datetime.combine(DAY, datetime.time(1, 0)))
    snap.states["sensor.solar"] = "0"
    result = engine.run_job(snap, engine.startup, {})
    assert result.error is None
    assert result.states["charging_amps_table"] == engine.charging_amps_table()
    assert result.value["amps_report"] == engine.charging_amps_table()
