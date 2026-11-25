"""Push notifications before a departure (engine/control/readiness.py): the car will not be ready, or it is not plugged in
for the charging planned for the departure. Each departure gets each notice once, also across a restart."""
from __future__ import annotations

import datetime
import math
from types import SimpleNamespace

import pytest

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.engine import CJPEngine
from custom_components.cable_juice_planner.engine.control import readiness

from .test_engine import DAY, OPTIONS, PLUGGED_IN, TEXTS, TZ, snapshot

NOT_READY = TEXTS["ui.notify_departure.title_not_ready"]
PLUG_IN = TEXTS["ui.notify_departure.title_plug_in"]
VIRTUAL = {"usable_battery_level_entity_id": "", "estimated_battery_range_entity_id": "", "odometer_entity_id": ""}


def _at(hour: int, minute: int = 30, day: datetime.date = DAY) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(hour, minute))


def _workday(departure: datetime.time, km: float = 150.0) -> dict:
    return {"workplan_charging": True, "workday_thursday": True, "workday_departure_thursday": departure, "workday_distance_needed_thursday": km}


def _snap(now: datetime.datetime, states: dict | None = None, **kwargs):
    snap = snapshot(now, **kwargs)
    snap.states.update(states or {})
    # the charger status has been as it is for two hours (the plug-in reminder looks at the last minutes)
    snap.buffers = {"sensor.charger_status": _rows(now, (120, snap.states["sensor.charger_status"])), **(getattr(snap, "buffers", None) or {})}
    return snap


class Car:
    """An engine started at <now>; cron() runs charge_trigger like the coordinator's cron."""

    def __init__(self, now: datetime.datetime, options: dict | None = None, databases: dict | None = None, **kwargs) -> None:
        self.engine = CJPEngine(const.build_config({**OPTIONS, **(options or {})}), "cjp_test", TEXTS)
        self.kwargs = kwargs
        started = self.engine.run_job(_snap(now, **kwargs), self.engine.startup, databases or {})
        assert started.error is None, started.error
        self.sent = list(started.mobile_notifications)

    def cron(self, now: datetime.datetime, **kwargs) -> list[dict]:
        result = self.engine.run_job(_snap(now, **{**self.kwargs, **kwargs}), self.engine.charge_trigger)
        assert result.error is None, result.error
        self.sent += result.mobile_notifications
        return result.mobile_notifications

    def run(self, now: datetime.datetime, func, *args, **kwargs):
        result = self.engine.run_job(_snap(now, **{**self.kwargs, **kwargs}), func, *args)
        assert result.error is None, result.error
        self.sent += result.mobile_notifications
        return result

    def titled(self, title: str) -> list[dict]:
        return [n for n in self.sent if title in n["title"]]


# ---------------------------------------------------------------------- #
# the planner records the shortfall
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize(("departure", "short"), [(datetime.time(5, 0), True), (datetime.time(8, 0), False)])
def test_the_planner_records_a_departure_it_cannot_reach(departure: datetime.time, short: bool) -> None:
    car = Car(_at(3), settings=_workday(departure), level=20, **PLUGGED_IN)
    car.cron(_at(3))
    shortfall = car.engine.charging_plan[0]["departure_shortfall"]
    if not short:
        assert shortfall is None
        return
    assert shortfall["departure"] == shortfall["set_departure"] == datetime.datetime.combine(DAY, departure)
    assert shortfall["reason"] == readiness.REASON_HOURS
    assert shortfall["missing"] >= readiness.MIN_SHORTFALL_PERCENT
    assert all(car.engine.charging_plan[day]["departure_shortfall"] is None for day in range(1, 8))


def test_a_trip_above_what_the_battery_holds_is_the_limit() -> None:
    trip = {"trip_date_time": _at(14, 0), "trip_homecoming_date_time": _at(20, 0), "trip_range_needed": 600.0}
    car = Car(_at(12), settings=trip, level=20, **PLUGGED_IN)
    car.cron(_at(12))
    shortfall = car.engine.charging_plan[0]["departure_shortfall"]
    assert shortfall["reason"] == readiness.REASON_LIMIT
    [notice] = car.titled(NOT_READY)
    assert shortfall["limit"] == 100
    assert TEXTS["ui.notify_departure.reason_limit"].format(limit=100) in notice["message"]
    # no limit left to raise at 100 %: the action shows the plan
    assert notice["data"]["actions"][0]["uri"] == "entityId:sensor.cjp_test_current_charging_rule"


# ---------------------------------------------------------------------- #
# not ready: once per departure, also after a restart
# ---------------------------------------------------------------------- #


def test_not_ready_is_sent_once_and_not_again_after_a_restart() -> None:
    car = Car(_at(3), settings=_workday(datetime.time(5, 0)), level=20, **PLUGGED_IN)
    car.cron(_at(3))
    [notice] = car.titled(NOT_READY)
    assert TEXTS["ui.notify_departure.today"].format(time="05:00") in notice["message"]
    missing = round(car.engine.charging_plan[0]["departure_shortfall"]["missing"])
    assert notice["message"].startswith(TEXTS["ui.notify_departure.not_ready"].format(departure=TEXTS["ui.notify_departure.today"].format(time="05:00"), missing=missing))
    assert TEXTS["ui.notify_departure.reason_hours"] in notice["message"]
    assert notice["data"]["actions"][0]["uri"] == "entityId:sensor.cjp_test_current_charging_rule"
    assert not car.titled(PLUG_IN)  # plugged in
    car.cron(_at(3, 35))
    car.cron(_at(4, 0))
    assert len(car.titled(NOT_READY)) == 1
    saved = car.engine._runtime_state()
    assert saved["departure_notices"] == {f"not_ready work {DAY.isoformat()}": _at(5, 0).isoformat()}

    restarted = Car(_at(4, 5), databases={"runtime": saved}, settings=_workday(datetime.time(5, 0)), level=20, **PLUGGED_IN)
    restarted.cron(_at(4, 5))
    assert not restarted.titled(NOT_READY)


def test_no_notice_when_the_setting_is_off() -> None:
    car = Car(_at(5), options={"departure_readiness": False}, settings=_workday(datetime.time(9, 0)), level=20)
    car.cron(_at(5))
    assert not car.titled(NOT_READY) and not car.titled(PLUG_IN)
    assert const.build_config(const.default_options())["notification"]["departure_readiness"] is True


def _shortfall(departure: datetime.datetime, missing: float = 10.0, reason: str = readiness.REASON_HOURS) -> dict:
    return {"departure": departure, "set_departure": departure, "kind": readiness.WORK, "missing": missing, "reason": reason, "limit": 90}


def _plan_day(shortfall: dict | None = None, work_goto: datetime.datetime | None = None) -> dict:
    return {"workday": work_goto is not None, "work_goto": work_goto, "work_departure": work_goto, "trip": False, "trip_goto": None, "departure_shortfall": shortfall}


def _notify(car: Car, now: datetime.datetime, plan: dict, hours: dict | None = None, **kwargs) -> None:
    def job() -> None:
        car.engine.charging_plan = plan
        car.engine.charge_hours = hours or {}
        car.engine.notify_departure_readiness()

    car.run(now, job, **kwargs)


@pytest.mark.parametrize(
    ("hours_ahead", "missing", "sent"),
    [
        (11.5, 10.0, True),
        (23.5, 10.0, True),  # the day before: in time to act
        (25.0, 10.0, False),  # further ahead than the horizon: prices and the plan will still move
        (-0.5, 10.0, False),  # the departure has passed
        (11.5, 1.5, False),  # rounding or the cold weather margin
    ],
)
def test_not_ready_only_for_a_near_departure_and_a_real_shortfall(hours_ahead: float, missing: float, sent: bool) -> None:
    now = _at(18)
    car = Car(now, level=40)
    departure = now + datetime.timedelta(hours=hours_ahead)
    _notify(car, now, {0: _plan_day(), 1: _plan_day(_shortfall(departure, missing))}, level=40)
    notices = car.titled(NOT_READY)
    assert len(notices) == int(sent)
    if sent:
        assert TEXTS["ui.notify_departure.tomorrow"].format(time=departure.strftime("%H:%M")) in notices[0]["message"]


def test_a_departure_that_moves_during_the_day_is_not_new() -> None:
    """The learned departure (06:30) gives way to the entered one (07:00) for a car still plugged in: the same departure."""
    now = _at(6, 0)
    car = Car(now, level=40)
    _notify(car, now, {0: _plan_day(_shortfall(_at(6, 30)))}, level=40)
    _notify(car, _at(6, 35), {0: _plan_day(_shortfall(_at(7, 0)))}, level=40)
    assert len(car.titled(NOT_READY)) == 1
    # a trip the same day is another departure
    _notify(car, _at(6, 40), {0: _plan_day({**_shortfall(_at(15, 0)), "kind": readiness.TRIP})}, level=40)
    assert len(car.titled(NOT_READY)) == 2
    # and a second trip the same day is another one again
    _notify(car, _at(6, 45), {0: _plan_day({**_shortfall(_at(18, 0)), "kind": readiness.TRIP})}, level=40)
    assert len(car.titled(NOT_READY)) == 3


@pytest.mark.parametrize(("options", "settings", "level", "sent"), [
    (VIRTUAL, {"battery_level": 0.0}, 40, False),  # a car without its integration is 0 % while unplugged
    (VIRTUAL, {"battery_level": 55.0}, 40, True),
    ({}, {}, "unavailable", False),  # the plan counts an unknown level as 0 %
    ({}, {}, "40", True),
])
def test_no_notice_without_a_real_battery_level(options: dict, settings: dict, level, sent: bool) -> None:
    now = _at(18)
    car = Car(now, options=options, settings=settings)
    _notify(car, now, {0: _plan_day(_shortfall(now + datetime.timedelta(hours=3)))}, settings=settings, states={"sensor.ev_battery_level": str(level)})
    assert len(car.titled(NOT_READY)) == int(sent)


def test_sent_notices_are_forgotten_a_day_after_their_departure() -> None:
    now = _at(12)
    old, recent = (now - datetime.timedelta(days=2)).isoformat(), (now - datetime.timedelta(hours=3)).isoformat()
    car = Car(now, level=40, databases={"runtime": {"departure_notices": {f"not_ready {old}": old, f"plug_in {recent}": recent, "junk": 3, "bad": "x"}}})
    assert car.engine.departure_notices == {f"not_ready {old}": old, f"plug_in {recent}": recent, "bad": "x"}
    _notify(car, now, {}, level=40)
    assert car.engine.departure_notices == {f"plug_in {recent}": recent}
    car.engine.restore_departure_notices(["not", "a", "dict"])
    assert car.engine.departure_notices == {}


def test_a_failure_does_not_stop_the_cron(monkeypatch: pytest.MonkeyPatch) -> None:
    car = Car(_at(12), level=40)

    def broken() -> None:
        raise RuntimeError("boom")

    calls = []
    original = car.engine.set_estimated_range
    monkeypatch.setattr(car.engine, "_notify_departure_readiness", broken)
    monkeypatch.setattr(car.engine, "set_estimated_range", lambda *args, **kwargs: (calls.append(True), original(*args, **kwargs)))
    car.cron(_at(12, 35))  # no error
    assert calls  # the steps after the notice still ran


# ---------------------------------------------------------------------- #
# plug in
# ---------------------------------------------------------------------- #


def test_plug_in_reminder_when_the_planned_charging_starts_and_the_car_is_unplugged() -> None:
    car = Car(_at(5), settings=_workday(datetime.time(9, 0)), level=20)
    car.cron(_at(5))
    [notice] = car.titled(PLUG_IN)
    assert notice["message"] == TEXTS["ui.notify_departure.plug_in"].format(departure=TEXTS["ui.notify_departure.today"].format(time="09:00"), start="05:30")
    car.cron(_at(5, 35))
    car.cron(_at(6, 0))
    assert len(car.titled(PLUG_IN)) == 1


@pytest.mark.parametrize("kwargs", [
    PLUGGED_IN,
    {"states": {"device_tracker.ev": "not_home"}},
    {"states": {"sensor.charger_status": "unavailable"}},  # unknown is no unplug
])
def test_no_plug_in_reminder_when_plugged_in_away_or_unknown(kwargs: dict) -> None:
    car = Car(_at(5), settings=_workday(datetime.time(9, 0)), level=20, **kwargs)
    car.cron(_at(5))
    assert not car.titled(PLUG_IN)


@pytest.mark.parametrize(("start_in", "rule", "sent"), [
    (30, "first_workday_preparation", True),
    (-30, "trip", True),  # started half an hour ago
    (90, "first_workday_preparation", False),  # not yet
    (30, "half_min_avg_price", False),  # a cheap top-up, not charging the departure needs
])
def test_plug_in_only_for_charging_the_departure_needs_that_starts_soon(start_in: int, rule: str, sent: bool) -> None:
    now = _at(5, 0)
    car = Car(now, level=40)
    start = now + datetime.timedelta(minutes=start_in)
    hours = {start: {"kWh": 5.0, "battery_level": 7.0, rule: True}}
    _notify(car, now, {0: _plan_day(work_goto=_at(9, 0))}, hours, level=40)
    assert len(car.titled(PLUG_IN)) == int(sent)


def _rows(now: datetime.datetime, *rows: tuple[int, str]) -> list[tuple[float, str]]:
    """History rows <minutes> before now."""
    return [(now.replace(tzinfo=TZ).timestamp() - minutes * 60, value) for minutes, value in rows]


@pytest.mark.parametrize(("status", "history", "sent"), [
    ("disconnected", [(120, "disconnected")], True),
    ("unavailable", [(60, "disconnected")], False),  # the history's last state is not the state now
    ("disconnected", [(10, "awaiting_start"), (5, "disconnected")], False),  # just unplugged, maybe to leave early
    ("disconnected", [], False),  # no history at all: unknown
])
def test_plug_in_reminder_needs_the_car_unplugged_now_and_for_a_while(status: str, history: list, sent: bool) -> None:
    now = _at(5)
    car = Car(now, settings=_workday(datetime.time(9, 0)), level=20)
    snap = _snap(now, settings=_workday(datetime.time(9, 0)), level=20, states={"sensor.charger_status": status})
    snap.buffers = {"sensor.charger_status": _rows(now, *history)}
    result = car.engine.run_job(snap, car.engine.charge_trigger)
    assert result.error is None, result.error
    assert len([n for n in result.mobile_notifications if PLUG_IN in n["title"]]) == int(sent)


def test_plug_in_reminder_is_not_sent_again_after_a_restart() -> None:
    car = Car(_at(5), settings=_workday(datetime.time(9, 0)), level=20)
    car.cron(_at(5))
    assert len(car.titled(PLUG_IN)) == 1
    restarted = Car(_at(5, 10), databases={"runtime": car.engine._runtime_state()}, settings=_workday(datetime.time(9, 0)), level=20)
    restarted.cron(_at(5, 10))
    assert not restarted.titled(PLUG_IN)


def test_a_work_day_above_the_charge_limit_is_the_limit() -> None:
    car = Car(_at(3), settings=_workday(datetime.time(8, 0), km=400.0), level=20, **PLUGGED_IN)
    car.cron(_at(3))
    shortfall = car.engine.charging_plan[0]["departure_shortfall"]
    assert shortfall["reason"] == readiness.REASON_LIMIT and shortfall["limit"] == 90
    [notice] = car.titled(NOT_READY)
    assert notice["data"]["actions"][0]["uri"] == "entityId:number.cjp_test_max_recommended_charge_limit_battery_level"


def _record(car: Car, now: datetime.datetime, plan_day: dict, departure: datetime.datetime, grid_prices: dict | None = None) -> dict | None:
    hours = {now.replace(minute=0) + datetime.timedelta(hours=i): 1.0 for i in range(24)}

    def record() -> None:
        ctx = SimpleNamespace(charging_plan={0: plan_day}, current_hour=now.replace(minute=0), grid_prices=hours if grid_prices is None else grid_prices)
        car.engine.record_departure_shortfall(ctx, 0, departure, 10.0)

    car.run(now, record)
    return plan_day["departure_shortfall"]


def _work_and_trip(before_work: float, trip_needed: float = 60.0, trip_goto: datetime.datetime | None = None) -> dict:
    trip_goto = trip_goto or _at(17, 0)
    return {
        "workday": True, "trip": True, "work_goto": _at(7, 0), "work_departure": _at(7, 0), "work_homecoming": _at(15, 0),
        "work_battery_level_needed": 20.0, "trip_goto": trip_goto, "trip_departure": trip_goto, "trip_battery_level_needed": trip_needed,
        "trip_battery_level_above_max": 0.0, "battery_level_before_work": [before_work], "departure_shortfall": None,
    }


@pytest.mark.parametrize(("before_work", "kind", "departure"), [
    (60.0, readiness.TRIP, _at(17, 0)),  # the work day is covered (20 % + the daily minimum 30 %): the trip after it is short
    (40.0, readiness.WORK, _at(7, 0)),
])
def test_a_trip_after_the_work_day_is_the_departure_that_is_short(before_work: float, kind: str, departure: datetime.datetime) -> None:
    car = Car(_at(5), level=40)
    shortfall = _record(car, _at(5), _work_and_trip(before_work), _at(7, 0))
    assert (shortfall["kind"], shortfall["set_departure"], shortfall["reason"]) == (kind, departure, readiness.REASON_HOURS)


@pytest.mark.parametrize(("trip_goto", "reason"), [
    (_at(17, 0), readiness.REASON_HOURS),  # after the homecoming: charged for in between, the needs never add up
    (_at(12, 0), readiness.REASON_LIMIT),  # during the work day: one charge for both
])
def test_work_and_a_trip_add_up_only_when_one_charge_must_cover_both(trip_goto: datetime.datetime, reason: str) -> None:
    """Each need alone is under 90 %, together 20 + 30 (daily minimum) + 70 - 30 = 90 %."""
    car = Car(_at(5), level=40)
    shortfall = _record(car, _at(5), _work_and_trip(40.0, trip_needed=70.0, trip_goto=trip_goto), _at(7, 0))
    assert shortfall["reason"] == reason


def test_no_shortfall_while_an_hour_before_the_departure_has_no_price() -> None:
    """Hours without a price are not planned: "too few hours" would be the missing prices, not a fact."""
    now = _at(3)
    car = Car(now, settings=_workday(datetime.time(5, 0)), level=20)
    def plan_day() -> dict:
        return {"workday": True, "trip": False, "work_goto": _at(5, 0), "work_departure": _at(5, 0), "work_battery_level_needed": 40.0,
                "trip_battery_level_needed": 0.0, "battery_level_before_work": [20.0], "departure_shortfall": None}

    assert _record(car, now, plan_day(), _at(5, 0), {_at(3, 0): 1.0}) is None
    shortfall = _record(car, now, plan_day(), _at(5, 0), {_at(3, 0): 1.0, _at(4, 0): 1.0})
    assert (shortfall["kind"], shortfall["reason"]) == (readiness.WORK, readiness.REASON_HOURS)


# ---------------------------------------------------------------------- #
# while the plan charges: the car does not follow it
# ---------------------------------------------------------------------- #

NEEDED = 80  # the work day's 50 % + the daily minimum 30 %


def _charging_day(departure: datetime.datetime) -> dict:
    return {
        "workday": True, "work_goto": departure, "work_departure": departure, "work_homecoming": departure + datetime.timedelta(hours=8),
        "work_battery_level_needed": 50.0, "trip": False, "trip_goto": None, "trip_battery_level_needed": 0.0, "departure_shortfall": None,
    }


CAR_TIME = {"last_update_entity_id": "sensor.ev_last_update"}  # the car's own time of its last report
NOW = "now"


def _charge(
    car: Car, now: datetime.datetime, *, departure: datetime.datetime | None = None, published: float | None = 85.0, level: float = 40,
    status: str = "awaiting_start", power: float = 0.0, history: list | None = None, states: dict | None = None, setup=None,
    settings: dict | None = None, plan: dict | None = None, level_changed: float | None = 1.0, car_time=NOW, level_rows: list | None = None,
) -> list[dict]:
    """A cron while the plan charges the slot of now for the work departure at 08:00 (unless departure is given).

    level_changed: minutes since the car's level last changed in the history (None: no change kept). car_time: the car's
    own time of its last report, for a car configured with CAR_TIME (NOW, a time, or None for unavailable)."""
    states = dict(states or {})
    if car_time is not None:
        states.setdefault("sensor.ev_last_update", (now if car_time == NOW else car_time).replace(tzinfo=TZ).isoformat())
    else:
        states.setdefault("sensor.ev_last_update", "unavailable")
    snap = _snap(now, settings={**_workday(datetime.time(8, 0)), **(settings or {})}, level=level, status=status, port="on", power=power, states=states)
    if level_rows is None:
        level_rows = [(level_changed + 60, str(level - 1)), (level_changed, str(level))] if level_changed is not None else [(120, str(level))]
    snap.buffers = {
        "sensor.charger_status": _rows(now, *(history or [(120, snap.states["sensor.charger_status"])])),
        "sensor.charger_power": _rows(now, (120, snap.states["sensor.charger_power"])),
        "sensor.ev_battery_level": _rows(now, *level_rows),
    }

    def job() -> None:
        engine = car.engine
        engine.charging_plan = plan or {0: _charging_day(departure or _at(8, 0))}
        engine.charge_hours = {now.replace(minute=0): {"kWh": 5.0, "battery_level": 7.0, "first_workday_preparation": True}}
        engine.charger_action = "charge"
        engine.sensor_states["charging_limit"] = published
        if setup is not None:
            setup(engine)
        engine.notify_departure_readiness()

    result = car.engine.run_job(snap, job)
    assert result.error is None, result.error
    car.sent += result.mobile_notifications
    return [n for n in result.mobile_notifications if NOT_READY in n["title"]]


LOW = {"number.ev_charge_limit": "60"}


def test_the_cars_own_limit_below_the_need_is_reported_while_the_plan_charges() -> None:
    """The automation did not set the limit CJP asks for: the car will stop at 60 %. The car reported after CJP asked."""
    car = Car(_at(5, 0), options=CAR_TIME, level=40)
    assert not _charge(car, _at(5, 0), states=LOW)  # it may still be setting it
    [notice] = _charge(car, _at(5, 10), states=LOW)
    today = TEXTS["ui.notify_departure.today"].format(time="08:00")
    assert notice["message"] == TEXTS["ui.notify_departure.car_limit"].format(departure=today, limit=60, needed=NEEDED)
    assert notice["data"]["actions"][0]["uri"] == "entityId:number.ev_charge_limit"
    assert not _charge(car, _at(5, 20), states=LOW)  # once
    assert f"car_limit work {DAY.isoformat()}" in car.engine._runtime_state()["departure_notices"]


def test_without_the_cars_own_time_the_car_limit_must_hold_an_hour() -> None:
    """A cloud integration may show the car's limit late: without the car's time an hour, in planned slots in a row."""
    car = Car(_at(5, 0), level=40)
    for minute in (0, 10, 30, 50):
        assert not _charge(car, _at(5, minute), states=LOW)
    assert _charge(car, _at(6, 0), states=LOW)


def _set(**values):
    def setup(engine) -> None:
        for key, value in values.items():
            setattr(engine, key, value)
    return setup


@pytest.mark.parametrize("kwargs", [
    {"states": {"number.ev_charge_limit": "85"}},  # the car took it
    {"states": {"number.ev_charge_limit": "unavailable"}},  # not known: no notice
    {"states": LOW, "published": None},  # CJP has not asked for a limit yet
    {"states": LOW, "published": 55.0},  # nor for more than the car has
    {"states": LOW, "setup": _set(charger_action="stop")},  # the plan does not charge now
    {"states": LOW, "setup": _set(restarting_charger_count=1)},  # the restart notifies itself
    {"states": LOW, "setup": _set(_charger_restart_gave_up=True)},
    {"states": LOW, "setup": _set(_charger_test_released_at=_at(4, 30))},  # the test set the car's limit itself
    {"states": LOW, "settings": {"allow_manual_charging_now": True}},  # not the plan
    {"states": LOW, "settings": {"calculate_charging_loss": True}},
    {"states": LOW, "settings": {"workplan_charging": False}},  # no charging modes
    {"states": LOW, "level": 79},  # about at the need already
    {"states": {"number.ev_charge_limit": "79"}},  # 1 % under the need: a car with steps of 5 or 10 %
    # the car last reported before CJP asked for its limit: a cloud integration shows the old one
    {"states": LOW, "car_time": _at(4, 0)},
    {"states": LOW, "car_time": None},  # the car's time is not known: an hour (the test above)
])
def test_no_car_limit_notice_when_the_car_follows_or_it_is_not_known(kwargs: dict) -> None:
    car = Car(_at(5, 0), options=CAR_TIME, level=40)
    assert not _charge(car, _at(5, 0), **kwargs) + _charge(car, _at(5, 10), **kwargs) + _charge(car, _at(5, 20), **kwargs)


def test_a_car_that_stops_below_the_need_is_reported() -> None:
    car = Car(_at(5, 0), level=40)
    kwargs = {"status": "completed", "states": {"number.ev_charge_limit": "90"}}
    assert not _charge(car, _at(5, 0), **kwargs)
    [notice] = _charge(car, _at(5, 10), **kwargs)
    today = TEXTS["ui.notify_departure.today"].format(time="08:00")
    assert notice["message"] == TEXTS["ui.notify_departure.car_stopped"].format(departure=today, level=40, needed=NEEDED)
    assert notice["data"]["actions"][0]["uri"] == "entityId:sensor.cjp_test_current_charging_rule"


def test_a_car_that_stops_at_its_own_limit_is_the_car_limit_once() -> None:
    car = Car(_at(5, 0), level=60)
    kwargs = {"status": "completed", "level": 59.5, "states": LOW}
    for minute in (0, 10, 20):
        _charge(car, _at(5, minute), **kwargs)
    [notice] = car.titled(NOT_READY)
    assert notice["message"].startswith(TEXTS["ui.notify_departure.car_limit"].split("{limit}")[0].format(departure=TEXTS["ui.notify_departure.today"].format(time="08:00")))


@pytest.mark.parametrize(("options", "kwargs"), [
    # the level last changed before the stop (a cloud car reports every 30-60 minutes): maybe it got there after all
    ({}, {"level_changed": 30.0}),
    ({}, {"level_changed": None}),  # it has not changed in the history kept: its age is not known
    ({}, {"states": {"sensor.ev_battery_level": "unavailable"}}),  # the plan then uses the last level from the history
    (CAR_TIME, {"car_time": _at(4, 30)}),  # the car's own time is from before the stop
    (CAR_TIME, {"car_time": None, "level_changed": 30.0}),
])
def test_no_stopped_notice_from_a_level_older_than_the_stop(options: dict, kwargs: dict) -> None:
    """A cloud integration writes its cached level again on every poll: only the car's time or a new value counts."""
    car = Car(_at(5, 0), options=options, level=40)
    stopped = {"status": "completed", **kwargs, "states": {"number.ev_charge_limit": "90", **kwargs.get("states", {})}}
    for minute in (0, 10, 20):
        assert not _charge(car, _at(5, minute), **stopped)


def test_a_stop_is_reported_once_the_car_reports_after_it() -> None:
    car = Car(_at(5, 0), level=40)
    kwargs = {"status": "completed", "states": {"number.ev_charge_limit": "90"}}
    assert not _charge(car, _at(5, 0), level_changed=30.0, **kwargs)
    assert not _charge(car, _at(5, 10), level_changed=40.0, **kwargs)  # still the old one
    assert _charge(car, _at(5, 15), level_changed=1.0, **kwargs)


def test_no_stopped_notice_for_a_car_without_its_integration() -> None:
    """Its level is CJP's own estimate: a car that says done is more likely full."""
    settings = {"battery_level": 40.0}
    car = Car(_at(5, 0), options=VIRTUAL, settings=settings)
    for minute in (0, 10, 20):
        assert not _charge(car, _at(5, minute), status="completed", settings=settings)


@pytest.mark.parametrize(("departure", "sent"), [(_at(8, 0), True), (_at(20, 0), False)])
def test_slow_charging_is_reported_for_a_car_without_its_integration(departure: datetime.datetime, sent: bool) -> None:
    """Its level is counted from the charger meter every cron: 3 kW from 40 % misses 80 % at 08:00, not at 20:00."""
    settings = {"battery_level": 40.0}
    car = Car(_at(5, 0), options=VIRTUAL, settings=settings)
    kwargs = {"status": "charging", "power": 3000.0, "settings": settings, "departure": departure}
    assert not _charge(car, _at(5, 0), **kwargs)
    notices = _charge(car, _at(5, 10), **kwargs)
    assert len(notices) == int(sent)
    if sent:
        assert "3.0 kW" in notices[0]["message"]


@pytest.mark.parametrize(("base", "level", "sent"), [(0.0, 40.0, False), (30.0, 40.0, True), (0.0, 55.0, True)])
def test_a_virtual_level_counted_up_from_zero_is_no_real_level(base: float, level: float, sent: bool) -> None:
    """Plugged in without entering the level: it is 0 % (unplugged) plus what was charged since, not the car's level. An
    entered level (the base of the count, or a level that is not the counted one) is real."""
    settings = {"battery_level": level}
    car = Car(_at(5, 0), options=VIRTUAL, settings=settings)

    def counted(engine) -> None:
        engine.virtual_charge_anchor = {"meter": 120.0, "level": 40.0, "base": base}

    kwargs = {"status": "charging", "power": 3000.0, "settings": settings, "departure": _at(8, 0), "setup": counted, "level": level}
    assert not _charge(car, _at(5, 0), **kwargs)
    assert len(_charge(car, _at(5, 10), **kwargs)) == int(sent)


def test_a_slow_notice_never_comes_from_an_old_level() -> None:
    """Last changed two hours ago at 40 %: counted up at the planned power it may be near the need already."""
    car = Car(_at(5, 0), level=40)
    kwargs = {"status": "charging", "power": 3000.0, "level_changed": 120.0}
    assert not _charge(car, _at(5, 0), **kwargs) + _charge(car, _at(5, 10), **kwargs)
    kwargs["level_changed"] = None  # its age not known
    assert not _charge(car, _at(5, 20), **kwargs) + _charge(car, _at(5, 30), **kwargs)


def test_load_balancing_is_no_slow_car() -> None:
    """The site allows 4 A now: 3 kW is what the plan asks for."""
    car = Car(_at(5, 0), level=40)
    kwargs = {"status": "charging", "power": 3000.0, "setup": _set(current_charging_amps=[4.0, 4.0, 4.0])}
    assert not _charge(car, _at(5, 0), **kwargs) + _charge(car, _at(5, 10), **kwargs)


def test_no_check_against_the_next_departure_after_one_that_is_moved_later() -> None:
    """Today's 05:00 is moved to 07:00 for the car still plugged in: the charging now is for it, not for tomorrow."""
    car = Car(_at(5, 0), options=CAR_TIME, level=40)
    today = {**_charging_day(_at(7, 0)), "work_departure": _at(5, 0)}
    tomorrow = _charging_day(_at(8, 0, DAY + datetime.timedelta(days=1)))
    kwargs = {"states": LOW, "plan": {0: today, 1: tomorrow}}
    assert not _charge(car, _at(5, 10), **kwargs) + _charge(car, _at(5, 20), **kwargs) + _charge(car, _at(5, 30), **kwargs)


@pytest.mark.parametrize(("trip_goto", "kind", "needed"), [
    (_at(17, 0), readiness.WORK, 50.0),  # a trip after the homecoming: each on its own
    (_at(17, 0), readiness.TRIP, 70.0),
    (_at(12, 0), readiness.WORK, 90.0),  # during the work day: one charge for both, 20 + 30 + 70 - 30
])
def test_the_level_a_departure_needs(trip_goto: datetime.datetime, kind: str, needed: float) -> None:
    car = Car(_at(5, 0), level=40)
    plan_day = _work_and_trip(40.0, trip_needed=70.0, trip_goto=trip_goto)
    assert car.run(_at(5, 0), car.engine._needed_level, plan_day, kind).value == pytest.approx(needed)


def test_a_car_that_stops_at_the_limit_the_plan_asks_for_now_is_not_stopped() -> None:
    """This slot asks for 50 %: a later one asks for more."""
    car = Car(_at(5, 0), level=49)
    kwargs = {"status": "completed", "level": 49, "published": 50.0, "states": {"number.ev_charge_limit": "50"}}
    assert not _charge(car, _at(5, 0), **kwargs) + _charge(car, _at(5, 10), **kwargs)


def test_charging_so_slow_that_the_departure_is_missed_is_reported() -> None:
    """3 kW instead of 11 kW: 40 % to 80 % takes far more than the three hours left."""
    car = Car(_at(5, 0), level=40)
    kwargs = {"status": "charging", "power": 3000.0}
    assert not _charge(car, _at(5, 0), **kwargs)
    [notice] = _charge(car, _at(5, 10), **kwargs)
    today = TEXTS["ui.notify_departure.today"].format(time="08:00")
    prefix = TEXTS["ui.notify_departure.slow"].split("{missing}")[0].format(departure=today, actual="3.0", planned="11.0")
    assert notice["message"].startswith(prefix)
    assert not _charge(car, _at(5, 20), **kwargs)


@pytest.mark.parametrize("kwargs", [
    {"status": "charging", "power": 3000.0, "departure": _at(20, 0)},  # slower, but the hours up to the departure are enough
    {"status": "charging", "power": 10000.0},  # about the planned power
    {"status": "charging", "power": 3000.0, "history": [(120, "awaiting_start"), (5, "charging")]},  # just started
    {"status": "charging", "power": 3000.0, "states": {"sensor.charger_power": "unavailable"}},  # the power is not known
    {"status": "awaiting_start", "power": 3000.0},  # not charging: the restart of the charger handles that
])
def test_no_slow_notice_in_time_at_the_planned_power_or_not_known(kwargs: dict) -> None:
    car = Car(_at(5, 0), level=40)
    assert not _charge(car, _at(5, 0), **kwargs) + _charge(car, _at(5, 10), **kwargs)


def test_a_charging_problem_must_hold_and_starts_over_when_it_goes_away() -> None:
    car = Car(_at(5, 0), options=CAR_TIME, level=40)
    _charge(car, _at(5, 0), states=LOW)
    _charge(car, _at(5, 5), states={"number.ev_charge_limit": "85"})  # set after all
    assert not _charge(car, _at(5, 10), states=LOW)  # low again: the wait starts over
    assert _charge(car, _at(5, 20), states=LOW)


@pytest.mark.parametrize("kwargs", [
    {"states": {"sensor.ev_battery_level": "nan"}},  # a broken value now: the plan uses an old one from the history
    # a broken value between two equal ones is no new report
    {"level_rows": [(90, "40"), (3, "nan"), (1, "40")]},
    {"level_rows": [(90, "40"), (3, "inf"), (1, "40")]},
])
def test_a_broken_level_is_no_new_report(kwargs: dict) -> None:
    car = Car(_at(5, 0), level=40)
    stopped = {"status": "completed", **kwargs, "states": {"number.ev_charge_limit": "90", **kwargs.get("states", {})}}
    for minute in (0, 10, 20):
        assert not _charge(car, _at(5, minute), **stopped)


@pytest.mark.parametrize("car_time", [
    _at(7, 0),  # the car's clock ahead, or a UTC time read as local west of UTC
    "2026-10-22T05:00:00-05:00",  # an offset: 11:00 here, in the future
])
def test_a_car_time_in_the_future_is_not_known(car_time) -> None:
    car = Car(_at(5, 0), options=CAR_TIME, level=40)
    states = {**LOW, "sensor.ev_last_update": car_time if isinstance(car_time, str) else car_time.replace(tzinfo=TZ).isoformat()}
    assert not _charge(car, _at(5, 0), states=states) + _charge(car, _at(5, 10), states=states) + _charge(car, _at(5, 20), states=states)


def test_the_waits_start_over_after_the_setting_was_off() -> None:
    car = Car(_at(5, 0), options=CAR_TIME, level=40)
    _charge(car, _at(5, 0), states=LOW)
    car.engine.config["notification"]["departure_readiness"] = False
    _charge(car, _at(5, 5), states=LOW)
    car.engine.config["notification"]["departure_readiness"] = True
    assert not _charge(car, _at(5, 10), states=LOW)  # the wait starts over
    assert _charge(car, _at(5, 20), states=LOW)


def test_the_waits_start_over_outside_a_planned_slot() -> None:
    """The automation sends the limit again for a new charge: the car gets the time again."""
    car = Car(_at(5, 0), options=CAR_TIME, level=40)
    _charge(car, _at(5, 0), states=LOW)
    _charge(car, _at(5, 5), states=LOW, setup=_set(charger_action="stop"))
    assert not _charge(car, _at(5, 10), states=LOW)
    assert _charge(car, _at(5, 20), states=LOW)


# ---------------------------------------------------------------------- #
# the car's limit at plug-in, when the charger automation can't set it
# ---------------------------------------------------------------------- #

TOMORROW = DAY + datetime.timedelta(days=1)
NO_STEPS = TEXTS["ui.notify_departure.no_limit_steps"]


def _plugged_in(
    car: Car, now: datetime.datetime, *, steps: bool | None = False, status: str = "awaiting_start", states: dict | None = None,
    car_time=NOW, level: float = 40, hours: dict | None = None, settings: dict | None = None, setup=None,
) -> list[dict]:
    """A cron in the evening: plugged in, the charging for tomorrow's 07:00 is planned from 02:00, the plan does not charge now."""
    states = {**LOW, **(states or {})}
    states.setdefault("sensor.ev_last_update", (now if car_time == NOW else car_time).replace(tzinfo=TZ).isoformat())
    snap = _snap(now, settings={**_workday(datetime.time(8, 0)), **(settings or {})}, level=level, status=status, port="on", states=states)
    snap.car_limit_steps = steps

    def job() -> None:
        engine = car.engine
        engine.charging_plan = {0: _plan_day(), 1: _charging_day(_at(7, 0, TOMORROW))}
        engine.charge_hours = hours if hours is not None else {_at(2, 0, TOMORROW): {"kWh": 11.0, "battery_level": 14.0, "second_workday_preparation": True}}
        engine.charger_action = "stop"
        if setup is not None:
            setup(engine)
        engine.notify_departure_readiness()

    result = car.engine.run_job(snap, job)
    assert result.error is None, result.error
    car.sent += result.mobile_notifications
    return [n for n in result.mobile_notifications if NOT_READY in n["title"]]


def test_the_cars_limit_is_reported_at_plug_in_when_the_automation_cannot_set_it() -> None:
    """Not first at 02:10 in the night: in the evening, as soon as the car reported its limit after it was seen."""
    car = Car(_at(18, 0), options=CAR_TIME, level=40)
    assert not _plugged_in(car, _at(18, 0))
    [notice] = _plugged_in(car, _at(18, 10))
    tomorrow = TEXTS["ui.notify_departure.tomorrow"].format(time="07:00")
    assert notice["message"] == f"{TEXTS['ui.notify_departure.car_limit'].format(departure=tomorrow, limit=60, needed=NEEDED)}\n{NO_STEPS}"
    assert notice["data"]["actions"][0]["uri"] == "entityId:number.ev_charge_limit"
    assert not _plugged_in(car, _at(18, 20))
    # the planned charging in the night does not say it again
    assert not _charge(car, _at(2, 0, TOMORROW), departure=_at(7, 0, TOMORROW), states=LOW)
    assert not _charge(car, _at(2, 10, TOMORROW), departure=_at(7, 0, TOMORROW), states=LOW)


@pytest.mark.parametrize("kwargs", [
    {"steps": True},  # the automation sets it when the charging starts (the check while charging)
    {"steps": None},  # no automation found in automations.yaml (packages, YAML mode): not known
    {"car_time": _at(17, 0)},  # the car last reported before it was seen: its limit may be old
    {"status": "unavailable"},  # not known whether it is plugged in
    {"status": "disconnected"},  # not plugged in (the plug-in reminder's job)
    {"states": {"device_tracker.ev": "not_home"}},
    {"states": {"number.ev_charge_limit": "85"}},
    {"level": 79},  # about at the need already
    {"hours": {_at(2, 0, TOMORROW): {"kWh": 11.0, "battery_level": 14.0, "half_min_avg_price": True}}},  # a cheap top-up only
    {"setup": _set(restarting_charger_count=1)},  # not the plan's charging (the restart notifies itself)
    {"states": {"number.ev_charge_limit": "70"}, "settings": {"max_recommended_charge_limit_battery_level": 70.0}},  # the plan's own limit
])
def test_no_plug_in_limit_notice_when_the_automation_sets_it_or_it_is_not_known(kwargs: dict) -> None:
    car = Car(_at(18, 0), options=CAR_TIME, level=40)
    assert not _plugged_in(car, _at(18, 0), **kwargs) + _plugged_in(car, _at(18, 10), **kwargs) + _plugged_in(car, _at(18, 20), **kwargs)


def test_without_the_cars_own_time_the_plug_in_notice_waits_an_hour() -> None:
    car = Car(_at(18, 0), level=40)
    for minute in (0, 10, 30, 50):
        assert not _plugged_in(car, _at(18, minute))
    assert _plugged_in(car, _at(19, 0))


def test_the_plug_in_notice_needs_at_most_what_the_plan_charges_to() -> None:
    """A need of 80 % with the max recommended charge limit at 70 %: the plan charges to 70 %, so a car limit of 70 % is
    enough (the planner's own not ready notice says that the need is above the limit), and one of 60 % is short of 70 %."""
    settings = {"max_recommended_charge_limit_battery_level": 70.0}
    car = Car(_at(18, 0), options=CAR_TIME, level=40)
    _plugged_in(car, _at(18, 0), settings=settings)
    [notice] = _plugged_in(car, _at(18, 10), settings=settings)
    tomorrow = TEXTS["ui.notify_departure.tomorrow"].format(time="07:00")
    assert notice["message"].startswith(TEXTS["ui.notify_departure.car_limit"].format(departure=tomorrow, limit=60, needed=70))


def test_the_charging_check_names_the_missing_automation_step() -> None:
    car = Car(_at(5, 0), options=CAR_TIME, level=40)

    def no_steps(engine) -> None:
        engine._snap.car_limit_steps = False

    _charge(car, _at(5, 0), states=LOW)
    [notice] = _charge(car, _at(5, 10), states=LOW, setup=no_steps)
    assert notice["message"].endswith(f"\n{NO_STEPS}")


# ---------------------------------------------------------------------- #
# the virtual EV's level follows the charger meter
# ---------------------------------------------------------------------- #


def _emulate(car: Car, now: datetime.datetime, meter: float, level: float, *, session_start: float = 100.0, units: dict | None = None) -> float:
    """One cron of charging at 3 kW: the virtual level after it (the setting as HA would hold it)."""
    settings = {"battery_level": level}
    snap = _snap(now, settings=settings, status="charging", port="on", power=3000.0, states={"sensor.charger_kwh": str(meter)})
    snap.buffers = {"sensor.charger_power": _rows(now, (120, "3000.0")), "sensor.charger_status": _rows(now, (120, "charging"))}
    snap.units.update(units or {})

    def job() -> None:
        session = car.engine.current_charging_session
        if session.get("start_charger_meter") != session_start or not session.get("start"):
            # CJP's session ends every hour: a new one starts at the meter then
            car.engine.current_charging_session = {"charging_type": "planned", "start": now, "start_charger_meter": session_start, "emoji": "", "data": {}}
        car.engine.charging_power_to_emulated_battery_level()

    result = car.engine.run_job(snap, job)
    assert result.error is None, result.error
    return float(result.settings.get("battery_level", level))


def _percent(car: Car, kwh: float) -> float:
    return car.run(_at(5, 0), lambda: car.engine.kwh_to_percentage(kwh, include_charging_loss=True), settings={"battery_level": 40.0}).value


def test_a_slow_charge_raises_the_virtual_level_instead_of_being_rounded_away() -> None:
    """0.25 kWh a cron is under 1 %: before, each cron rounded it to 0 and the level stayed at 40 %. Across CJP's hourly
    session too. The count starts at the meter of the first cron: 23 crons of 0.25 kWh."""
    car = Car(_at(5, 0), options=VIRTUAL, settings={"battery_level": 40.0})
    level, meter = 40.0, 100.0
    for cron in range(1, 25):  # two hours, a new session after the first
        meter += 0.25
        level = _emulate(car, _at(5, 0) + datetime.timedelta(minutes=5 * cron), meter, level, session_start=100.0 if cron <= 12 else 103.0)
    assert level == 40.0 + math.floor(_percent(car, 5.75) + 1e-9)
    assert level > 44.0
    assert car.engine.virtual_charge_anchor["base"] == 40.0


def test_the_virtual_level_count_starts_again_after_an_unplug_or_an_entered_level() -> None:
    car = Car(_at(5, 0), options=VIRTUAL, settings={"battery_level": 40.0})
    # the first count starts at the meter now, not at the session's start: those kWh may be another car's or counted
    assert _emulate(car, _at(5, 5), 105.0, 40.0) == 40.0
    assert car.engine.virtual_charge_anchor == {"meter": 105.0, "level": 40.0, "base": 40.0}
    level = _emulate(car, _at(5, 10), 110.0, 40.0)
    assert level == 40.0 + math.floor(_percent(car, 5.0))
    assert car.engine.virtual_charge_anchor["base"] == 40.0
    # the user enters 50 %: what was charged before is in it
    assert _emulate(car, _at(5, 15), 110.2, 50.0) == 50.0
    assert car.engine.virtual_charge_anchor == {"meter": 110.2, "level": 50.0, "base": 50.0}
    car.run(_at(5, 20), car.engine.virtual_charge_unplugged, settings={"battery_level": 50.0})
    assert car.engine.virtual_charge_anchor == {"meter": None, "level": 50.0, "base": 50.0}
    # a meter that went back (a meter per session) starts again from it
    _emulate(car, _at(5, 25), 3.0, 50.0)
    assert car.engine.virtual_charge_anchor == {"meter": 3.0, "level": 50.0, "base": 50.0}
    assert _emulate(car, _at(5, 30), 1.0, 50.0) == 50.0
    assert car.engine.virtual_charge_anchor["meter"] == 1.0


@pytest.mark.parametrize("route", ["meter_back", "plugged_in_again"])
def test_a_level_counted_up_from_zero_stays_no_entered_level(route: str) -> None:
    """Plugged in without entering a level: counted from 0 %. A new meter (a meter per session) or an unplug and plug in
    before the cron set the level to 0 % starts the count from the meter again, but the level is still not entered: no
    notices. Entering a level makes it real."""
    car = Car(_at(5, 0), options=VIRTUAL, settings={"battery_level": 0.0})
    _emulate(car, _at(5, 5), 100.0, 0.0)
    level = _emulate(car, _at(5, 10), 110.0, 0.0)
    assert level > 5.0 and car.engine.virtual_charge_anchor["base"] == 0.0
    if route == "meter_back":
        _emulate(car, _at(5, 15), 2.0, level)
    else:
        car.run(_at(5, 12), car.engine.virtual_charge_unplugged, settings={"battery_level": level})
        _emulate(car, _at(5, 15), 112.0, level)
    assert car.engine.virtual_charge_anchor["base"] == 0.0
    counted = car.engine.virtual_charge_anchor

    def keep(engine) -> None:
        engine.virtual_charge_anchor = dict(counted)

    kwargs = {"status": "charging", "power": 3000.0, "departure": _at(8, 0), "setup": keep}
    for minute in (20, 30, 40):
        assert not _charge(car, _at(5, minute), settings={"battery_level": level}, level=level, **kwargs)
    # entered: the same level is now the car's
    _emulate(car, _at(5, 45), 112.5, 30.0)
    assert car.engine.virtual_charge_anchor["base"] == 30.0


@pytest.mark.parametrize("meter", ["unavailable", "unknown", "abc", "inf", "nan"])
def test_an_unknown_meter_changes_neither_the_virtual_level_nor_the_count(meter: str) -> None:
    car = Car(_at(5, 0), options=VIRTUAL, settings={"battery_level": 40.0})
    _emulate(car, _at(5, 5), 100.0, 40.0)
    anchor = dict(car.engine.virtual_charge_anchor)
    assert _emulate(car, _at(5, 10), meter, 40.0) == 40.0
    assert car.engine.virtual_charge_anchor == anchor


def test_the_virtual_level_stops_below_completed_and_drops_the_rest() -> None:
    settings = {"battery_level": 40.0}
    car = Car(_at(5, 0), options=VIRTUAL, settings=settings)
    completed = car.run(_at(5, 0), car.engine.get_completed_battery_level, settings=settings).value
    _emulate(car, _at(5, 5), 100.0, 40.0)
    assert _emulate(car, _at(5, 10), 200.0, 40.0) == completed - 1.0
    assert car.engine.virtual_charge_anchor == {"meter": 200.0, "level": completed - 1.0, "base": 40.0}


@pytest.mark.parametrize(("unit", "scale"), [("Wh", 1000.0), ("MWh", 0.001)])
def test_the_virtual_level_count_survives_a_restart_and_reads_the_meters_unit(unit: str, scale: float) -> None:
    units = {"sensor.charger_kwh": unit}
    car = Car(_at(5, 0), options=VIRTUAL, settings={"battery_level": 40.0})
    _emulate(car, _at(5, 5), 100.0 * scale, 40.0, session_start=99.0 * scale, units=units)
    _emulate(car, _at(5, 10), 100.2 * scale, 40.0, session_start=99.0 * scale, units=units)  # 0.2 kWh: under 1 %
    saved = car.engine._runtime_state()
    assert saved["virtual_charge_anchor"] == pytest.approx({"meter": 100.0, "level": 40.0, "base": 40.0})
    restarted = Car(_at(5, 15), options=VIRTUAL, settings={"battery_level": 40.0}, databases={"runtime": saved})
    assert restarted.engine.virtual_charge_anchor == saved["virtual_charge_anchor"]
    level = _emulate(restarted, _at(5, 20), 102.0 * scale, 40.0, session_start=101.0 * scale, units=units)
    assert level == 40.0 + math.floor(_percent(restarted, 2.0) + 1e-9)


def test_the_level_entered_after_an_unplug_is_entered_also_when_it_was_the_counted_one() -> None:
    """Unplugged, the cron sets the level to 0 % and the count with it: entering the old counted level is entering it."""
    settings = {"battery_level": 15.0}
    car = Car(_at(5, 0), options=VIRTUAL, settings=settings)
    car.engine.virtual_charge_anchor = {"meter": None, "level": 15.0, "base": 0.0}
    result = car.run(_at(5, 5), car.engine.ready_to_charge, settings=settings, status="disconnected", port="off")
    assert result.settings.get("battery_level") == 0.0, (result.settings, result.value)
    assert car.engine.virtual_charge_anchor == {"meter": None, "level": 0.0, "base": 0.0}
    _emulate(car, _at(5, 10), 120.0, 15.0)
    assert car.engine.virtual_charge_anchor == {"meter": 120.0, "level": 15.0, "base": 15.0}


def test_an_old_entered_level_does_not_make_the_next_count_from_zero_real() -> None:
    """Entered 50 % yesterday, unplugged (0 %), plugged in today without entering a level: counted from 0 %, no notices."""
    settings = {"battery_level": 50.0}
    car = Car(_at(5, 0), options=VIRTUAL, settings=settings)
    car.engine.virtual_charge_anchor = {"meter": 100.0, "level": 50.0, "base": 50.0}
    car.run(_at(5, 0), car.engine.ready_to_charge, settings=settings, status="disconnected", port="off")
    assert car.engine.virtual_charge_anchor == {"meter": None, "level": 0.0, "base": 0.0}
    _emulate(car, _at(5, 5), 120.0, 0.0)
    level = _emulate(car, _at(5, 10), 130.0, 0.0)
    assert level > 5.0 and car.engine.virtual_charge_anchor["base"] == 0.0
    counted = car.engine.virtual_charge_anchor

    def keep(engine) -> None:
        engine.virtual_charge_anchor = dict(counted)

    for minute in (20, 30, 40):
        assert not _charge(car, _at(5, minute), status="charging", power=3000.0, departure=_at(8, 0), setup=keep, settings={"battery_level": level}, level=level)


def test_an_unplugged_count_is_restored() -> None:
    saved = {"meter": None, "level": 12.0, "base": 0.0}
    car = Car(_at(5, 0), options=VIRTUAL, settings={"battery_level": 12.0}, databases={"runtime": {"virtual_charge_anchor": saved}})
    assert car.engine.virtual_charge_anchor == saved


@pytest.mark.parametrize("anchor", [
    {"meter": float("nan"), "level": 40.0, "base": 40.0},
    {"meter": 100.0, "level": float("inf"), "base": 40.0},
    {"meter": 100.0, "level": 40.0},  # no base
    {"meter": True, "level": 40.0, "base": 40.0},
    {"meter": 100.0, "level": None, "base": 40.0},  # only the meter may be None (unplugged)
    "100",
])
def test_a_broken_saved_count_is_not_restored(anchor) -> None:
    car = Car(_at(5, 0), options=VIRTUAL, settings={"battery_level": 40.0}, databases={"runtime": {"virtual_charge_anchor": anchor}})
    assert car.engine.virtual_charge_anchor is None
