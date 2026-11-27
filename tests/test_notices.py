"""More push notifications (engine/control/notices.py): each is sent once per event, also across a restart, never from
missing or stale data, and not with its switch off."""
from __future__ import annotations

import datetime

import pytest

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.engine.control import notices

from .test_departure_readiness import VIRTUAL, Car, _at, _plan_day, _rows, _snap, _workday
from .test_engine import DAY, PLUGGED_IN, TEXTS, TZ

OFFLINE = TEXTS["ui.notify_more.title_charger_offline"]
CHEAP = TEXTS["ui.notify_more.title_cheap_power"]
MANUAL = TEXTS["ui.notify_more.title_manual_expensive"]
MONTHLY = TEXTS["ui.notify_more.title_monthly_summary"]
SITE = TEXTS["ui.notify_more.title_site_limit"]
FULL = TEXTS["ui.notify_more.title_battery_full"]
LOSS = TEXTS["ui.notify_more.title_charging_loss"]
OPTION_KEYS = (
    "notify_charger_offline", "notify_cheap_power", "notify_manual_expensive", "notify_monthly_summary",
    "notify_site_limit", "notify_battery_full", "notify_charging_loss",
)
UNAVAILABLE = {"states": {"sensor.charger_status": "unavailable"}}


def _job(car: Car, now: datetime.datetime, func, buffers: dict | None = None, **kwargs) -> list[dict]:
    snap = _snap(now, **{**car.kwargs, **kwargs})
    if buffers is not None:
        snap.buffers = {**snap.buffers, **buffers}
    result = car.engine.run_job(snap, func)
    assert result.error is None, result.error
    car.sent += result.mobile_notifications
    return result.mobile_notifications


def _uri(notice: dict) -> str:
    return notice["data"]["actions"][0]["uri"]


def test_every_notice_is_on_by_default() -> None:
    config = const.build_config(const.default_options())
    assert all(config["notification"][key] is True for key in OPTION_KEYS)


# ---------------------------------------------------------------------- #
# the charger does not answer before a departure
# ---------------------------------------------------------------------- #


def test_charger_offline_before_a_departure_is_sent_once_and_not_after_a_restart() -> None:
    car = Car(_at(5), settings=_workday(datetime.time(9, 0)), level=20, **UNAVAILABLE)
    car.cron(_at(5))
    [notice] = car.titled(OFFLINE)
    departure = TEXTS["ui.notify_departure.today"].format(time="09:00")
    assert notice["message"] == TEXTS["ui.notify_more.charger_offline"].format(minutes=120, departure=departure)
    assert _uri(notice) == "entityId:sensor.charger_status"
    car.cron(_at(5, 35))
    assert len(car.titled(OFFLINE)) == 1
    saved = car.engine._runtime_state()
    assert saved["sent_notices"] == {f"charger_offline work {_at(9, 0).date().isoformat()}": (_at(9, 0) + datetime.timedelta(days=1)).isoformat()}
    restarted = Car(_at(5, 40), databases={"runtime": saved}, settings=_workday(datetime.time(9, 0)), level=20, **UNAVAILABLE)
    restarted.cron(_at(5, 40))
    assert not restarted.titled(OFFLINE)


@pytest.mark.parametrize(("history", "sent"), [
    ([(120, "disconnected"), (40, "unavailable")], True),
    ([(120, "disconnected"), (10, "unavailable")], False),  # a short drop-out
    ([(120, "unavailable"), (20, "disconnected")], False),  # the history's last state is not the state now
    ([], False),  # no history: not known how long
])
def test_charger_offline_needs_30_minutes_without_an_answer(history: list, sent: bool) -> None:
    now = _at(5)
    car = Car(now, settings=_workday(datetime.time(9, 0)), level=20, **UNAVAILABLE)
    _job(car, now, lambda: car.engine.notify_notices(), buffers={"sensor.charger_status": _rows(now, *history)})
    assert len(car.titled(OFFLINE)) == int(sent)


@pytest.mark.parametrize(("options", "settings"), [
    ({}, {}),  # no departure: no charging for one is planned
    ({"notify_charger_offline": False}, _workday(datetime.time(9, 0))),
])
def test_no_charger_offline_without_a_departure_or_with_the_switch_off(options: dict, settings: dict) -> None:
    car = Car(_at(5), options=options, settings=settings, level=20, **UNAVAILABLE)
    car.cron(_at(5))
    assert not car.titled(OFFLINE)


def test_charger_offline_for_an_unknown_status() -> None:
    car = Car(_at(5), settings=_workday(datetime.time(9, 0)), level=20, states={"sensor.charger_status": "unknown"})
    car.cron(_at(5))
    assert len(car.titled(OFFLINE)) == 1


def test_a_departure_that_moves_during_the_day_is_the_same_for_charger_offline() -> None:
    """The learned departure (06:30) gives way to the entered one (07:00): the same work day. A trip is another one."""
    now = _at(5, 0)
    car = Car(now, level=40, **UNAVAILABLE)

    def offline(plan_day: dict, start: datetime.datetime, rule: str = "first_workday_preparation"):
        def job() -> None:
            car.engine.charging_plan = {0: plan_day}
            car.engine.charge_hours = {start: _hour(**{rule: True})}
            car.engine._notice_charger_offline(car.engine.now())

        return job

    _job(car, now, offline(_plan_day(work_goto=_at(6, 30)), _at(5, 0)))
    _job(car, _at(5, 10), offline(_plan_day(work_goto=_at(7, 0)), _at(5, 0)))
    assert len(car.titled(OFFLINE)) == 1
    trip = {**_plan_day(), "trip": True, "trip_goto": _at(15, 0)}
    _job(car, _at(5, 20), offline(trip, _at(12, 0), "trip"))
    assert len(car.titled(OFFLINE)) == 2


def test_no_charger_offline_when_the_charger_answers() -> None:
    car = Car(_at(5), settings=_workday(datetime.time(9, 0)), level=20)
    car.cron(_at(5))
    assert not car.titled(OFFLINE)


# ---------------------------------------------------------------------- #
# cheap charging planned, the car is not plugged in
# ---------------------------------------------------------------------- #


def _hour(**flags) -> dict:
    return {"kWh": 5.0, "battery_level": 7.0, **flags}


def _cheap(car: Car, now: datetime.datetime, hours: dict, **kwargs) -> list[dict]:
    def job() -> None:
        car.engine.charge_hours = hours
        car.engine._notice_cheap_power(car.engine.now())

    return _job(car, now, job, **kwargs)


def _prices(monkeypatch: pytest.MonkeyPatch, car: Car, prices: dict, minutes: int = 60) -> None:
    """The price intervals: <minutes> long from each planned slot, the price by its start."""
    monkeypatch.setattr(car.engine, "_price_interval", lambda moment: (moment + datetime.timedelta(minutes=minutes), prices.get(moment)))


def test_cheap_power_shows_the_window_once_a_day(monkeypatch: pytest.MonkeyPatch) -> None:
    now = _at(4, 30)
    car = Car(now, level=40)
    hours = {_at(5, 0): _hour(half_min_avg_price=True), _at(6, 0): _hour(under_min_avg_price=True), _at(9, 0): _hour(half_min_avg_price=True)}
    _prices(monkeypatch, car, {_at(5, 0): 0.30, _at(6, 0): 0.25, _at(9, 0): 0.10})
    _cheap(car, now, hours, level=40)
    [notice] = car.titled(CHEAP)
    assert notice["message"] == TEXTS["ui.notify_more.cheap_power"].format(start="05:00", end="07:00", price="0.25", valuta=TEXTS["ui.common.valuta"])
    assert _uri(notice) == "entityId:sensor.cjp_test_current_charging_rule"
    _cheap(car, _at(8, 30), hours, level=40)  # the next window the same day
    assert len(car.titled(CHEAP)) == 1


def test_cheap_power_with_15_minute_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    """The plan is per price interval: a slot ends after 15 minutes, and the window is the slots in a row."""
    now = _at(5, 20)
    car = Car(now, level=40)
    hours = {
        _at(5, 0): _hour(half_min_avg_price=True),  # over at 05:15
        _at(5, 15): _hour(half_min_avg_price=True), _at(5, 30): _hour(half_min_avg_price=True), _at(5, 45): _hour(),
    }
    _prices(monkeypatch, car, {_at(5, 0): 0.10, _at(5, 15): 0.30, _at(5, 30): 0.20, _at(5, 45): 0.05}, minutes=15)
    _cheap(car, now, hours, level=40)
    [notice] = car.titled(CHEAP)
    assert notice["message"] == TEXTS["ui.notify_more.cheap_power"].format(start="05:20", end="05:45", price="0.20", valuta=TEXTS["ui.common.valuta"])


def test_no_cheap_power_for_a_15_minute_slot_that_is_over(monkeypatch: pytest.MonkeyPatch) -> None:
    now = _at(5, 50)
    car = Car(now, level=40)
    _prices(monkeypatch, car, {_at(5, 0): 0.10}, minutes=15)
    _cheap(car, now, {_at(5, 0): _hour(half_min_avg_price=True)}, level=40)
    assert not car.titled(CHEAP)


@pytest.mark.parametrize(("now", "hours", "kwargs", "sent"), [
    (_at(5, 20), {_at(5, 0): _hour(half_min_avg_price=True)}, {}, True),  # already started
    (_at(3, 30), {_at(5, 0): _hour(half_min_avg_price=True)}, {}, False),  # not yet
    (_at(4, 30), {_at(5, 0): _hour()}, {}, False),  # not cheap
    (_at(4, 30), {_at(5, 0): {**_hour(half_min_avg_price=True), "kWh": 0.0}}, {}, False),
    (_at(4, 30), {_at(5, 0): _hour(half_min_avg_price=True)}, PLUGGED_IN, False),
    (_at(4, 30), {_at(5, 0): _hour(half_min_avg_price=True)}, {"states": {"device_tracker.ev": "not_home"}}, False),
    # the plug-in reminder of the departure covers it
    (_at(5, 10), {_at(5, 0): _hour(first_workday_preparation=True), _at(6, 0): _hour(half_min_avg_price=True)}, {}, False),
])
def test_cheap_power_only_soon_and_for_a_car_home_and_unplugged(now, hours: dict, kwargs: dict, sent: bool) -> None:
    car = Car(now, level=40, **kwargs)
    notices_sent = _cheap(car, now, hours, level=40, **kwargs)
    assert len([n for n in notices_sent if CHEAP in n["title"]]) == int(sent)


@pytest.mark.parametrize(("price", "flags", "sent"), [
    (-0.2, {}, True),  # a price below 0 without the planner's cheap rule
    (None, {"half_min_avg_price": True}, False),  # the price is not known
])
def test_cheap_power_negative_and_unknown_prices(monkeypatch: pytest.MonkeyPatch, price, flags: dict, sent: bool) -> None:
    now = _at(4, 30)
    car = Car(now, level=40)
    _prices(monkeypatch, car, {_at(5, 0): price})
    _cheap(car, now, {_at(5, 0): _hour(**flags)}, level=40)
    notices_sent = car.titled(CHEAP)
    assert len(notices_sent) == int(sent)
    if sent:
        assert "-0.20" in notices_sent[0]["message"]


@pytest.mark.parametrize(("options", "settings"), [
    (VIRTUAL, {"battery_level": 0.0}),  # a car without its integration is 0 % while unplugged
    ({"notify_cheap_power": False}, {}),
])
def test_no_cheap_power_without_a_level_or_with_the_switch_off(options: dict, settings: dict) -> None:
    now = _at(4, 30)
    car = Car(now, options=options, settings=settings, level=40)
    _cheap(car, now, {_at(5, 0): _hour(half_min_avg_price=True)}, settings=settings, level=40)
    assert not car.titled(CHEAP)


# ---------------------------------------------------------------------- #
# manual charging in an expensive hour
# ---------------------------------------------------------------------- #

MANUAL_ON = {"allow_manual_charging_now": True}


def _manual(car: Car, now: datetime.datetime, **kwargs) -> list[dict]:
    return _job(car, now, lambda: car.engine._notice_manual_expensive(car.engine.now()), **kwargs)


def test_manual_charging_in_an_expensive_hour_once_a_day() -> None:
    now = _at(18)
    car = Car(now, settings=MANUAL_ON, status="charging", port="on")
    _manual(car, now)
    [notice] = car.titled(MANUAL)
    assert _uri(notice) == "entityId:switch.cjp_test_allow_manual_charging_now"
    assert TEXTS["ui.common.valuta"] in notice["message"]
    _manual(car, _at(19))
    assert len(car.titled(MANUAL)) == 1


@pytest.mark.parametrize(("now", "settings", "status", "options"), [
    (_at(12), MANUAL_ON, "charging", {}),  # not an expensive hour
    (_at(18), {}, "charging", {}),  # manual charging off
    (_at(18), MANUAL_ON, "awaiting_start", {}),  # not charging
    (_at(18), MANUAL_ON, "unavailable", {}),
    (_at(18), MANUAL_ON, "charging", {"notify_manual_expensive": False}),
])
def test_no_manual_notice_outside_an_expensive_charging_hour(now, settings: dict, status: str, options: dict) -> None:
    car = Car(now, options=options, settings=settings, status=status, port="on")
    _manual(car, now)
    assert not car.titled(MANUAL)


def test_no_manual_notice_without_a_known_price(monkeypatch: pytest.MonkeyPatch) -> None:
    car = Car(_at(18), settings=MANUAL_ON, status="charging", port="on")
    monkeypatch.setattr(car.engine, "_price_at", lambda moment: None)
    _manual(car, _at(18))
    assert not car.titled(MANUAL)


# ---------------------------------------------------------------------- #
# the month's charging
# ---------------------------------------------------------------------- #

NOVEMBER = datetime.date(2026, 11, 4)


def _history() -> dict:
    return {
        datetime.datetime(2026, 9, 28, 22): {"kWh": 20.0, "cost": 30.0},
        datetime.datetime(2026, 10, 5, 22): {"kWh": 40.0, "cost": 50.0},
        datetime.datetime(2026, 10, 20, 1): {"kWh": 30.0, "cost": 25.0},
    }


def _monthly(car: Car, now: datetime.datetime, history: dict | None = None, windows: list | None = None) -> list[dict]:
    def job() -> None:
        engine = car.engine
        engine.charging_history_db = _history() if history is None else history
        engine.economy_db["windows"] = [
            {**window, "start": engine._economy_ts(window["start"]), "end": engine._economy_ts(window["end"]) if window["end"] else None}
            for window in windows or []
        ]
        engine.publish_economy({})
        engine.notify_monthly_summary()

    return _job(car, now, job)


def _window(day: int, achieved: float = 50.0, daily: float = 70.0) -> dict:
    return {
        "start": datetime.datetime(2026, 10, day, 18), "end": datetime.datetime(2026, 10, day + 1, 6),
        "kwh": 40.0, "achieved": achieved, "average": 60.0, "daily": daily, "lowest": 45.0,
    }


def test_monthly_summary_of_last_month_is_sent_once() -> None:
    now = datetime.datetime.combine(NOVEMBER, datetime.time(12, 30))
    car = Car(now)
    _monthly(car, now, windows=[_window(5)])
    [notice] = car.titled(MONTHLY)
    valuta = TEXTS["ui.common.valuta"]
    assert notice["message"].split("\n") == [
        TEXTS["ui.notify_more.monthly_summary"].format(month=TEXTS["ui.calendar.month_names.october"], kwh=70, cost="75.00", valuta=valuta),
        TEXTS["ui.notify_more.monthly_unit_price"].format(price=f"{75 / 70:.2f}", valuta=valuta),
        TEXTS["ui.notify_more.monthly_saving"].format(amount="20.00", valuta=valuta),
    ]
    assert _uri(notice) == "entityId:sensor.cjp_test_economy"
    _monthly(car, now + datetime.timedelta(days=1), windows=[_window(5)])
    assert len(car.titled(MONTHLY)) == 1
    restarted = Car(now, databases={"runtime": car.engine._runtime_state()})
    _monthly(restarted, now, windows=[_window(5)])
    assert not restarted.titled(MONTHLY)


@pytest.mark.parametrize(("windows", "line"), [
    ([_window(5, achieved=50.0, daily=40.0)], "monthly_extra"),  # dearer than daily charging
    ([], None),  # no compared connections: no saving
])
def test_monthly_summary_saving_line(windows: list, line: str | None) -> None:
    now = datetime.datetime.combine(NOVEMBER, datetime.time(12, 30))
    car = Car(now)
    _monthly(car, now, windows=windows)
    [notice] = car.titled(MONTHLY)
    lines = notice["message"].split("\n")
    if line is None:
        assert len(lines) == 2
    else:
        assert lines[-1] == TEXTS[f"ui.notify_more.{line}"].format(amount="10.00", valuta=TEXTS["ui.common.valuta"])


@pytest.mark.parametrize("history", [
    {datetime.datetime(2026, 10, 5, 22): {"kWh": 40.0, "cost": 50.0}},  # CJP did not see October from its start
    {**_history(), datetime.datetime(2026, 10, 21, 1): {"kWh": 5.0, "cost": float("nan")}},  # a cost not known
    {datetime.datetime(2026, 9, 28, 22): {"kWh": 20.0, "cost": 30.0}},  # no charging in October
])
def test_no_monthly_summary_for_a_month_not_fully_known(history: dict) -> None:
    now = datetime.datetime.combine(NOVEMBER, datetime.time(12, 30))
    car = Car(now)
    _monthly(car, now, history=history)
    assert not car.titled(MONTHLY)


@pytest.mark.parametrize("pending", [
    {"start": datetime.datetime(2026, 10, 31, 20), "end": None},  # still open
    {"start": datetime.datetime(2026, 10, 31, 20), "end": datetime.datetime(2026, 11, 2, 11)},  # closed, not compared yet
])
def test_monthly_summary_waits_a_few_days_for_a_connection_not_compared(pending: dict) -> None:
    second = datetime.datetime(2026, 11, 2, 12, 30)
    car = Car(second)
    _monthly(car, second, windows=[_window(5), pending])
    assert not car.titled(MONTHLY)
    _monthly(car, second + datetime.timedelta(days=2), windows=[_window(5), pending])
    assert len(car.titled(MONTHLY)) == 1


def test_monthly_summary_when_the_connections_begin_before_the_month() -> None:
    """The first charge at home in October, but CJP saw the car's connections since September."""
    now = datetime.datetime.combine(NOVEMBER, datetime.time(12, 30))
    car = Car(now)
    september = {"start": datetime.datetime(2026, 9, 20, 18), "end": datetime.datetime(2026, 9, 21, 6), "kwh": 0.0, "achieved": 0.0}
    _monthly(car, now, history={datetime.datetime(2026, 10, 5, 22): {"kWh": 40.0, "cost": 50.0}}, windows=[september])
    assert len(car.titled(MONTHLY)) == 1


def test_economy_update_sends_the_monthly_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.datetime.combine(NOVEMBER, datetime.time(3, 41))
    car = Car(now)
    calls = []
    monkeypatch.setattr(car.engine, "notify_monthly_summary", lambda: calls.append(car.engine.economy_months))
    _job(car, now, lambda: car.engine.economy_update({}))
    assert calls and isinstance(calls[0], list)


def test_no_monthly_summary_with_the_switch_off() -> None:
    now = datetime.datetime.combine(NOVEMBER, datetime.time(12, 30))
    car = Car(now, options={"notify_monthly_summary": False})
    _monthly(car, now)
    assert not car.titled(MONTHLY)


# ---------------------------------------------------------------------- #
# load balancing
# ---------------------------------------------------------------------- #


CHARGING = {"status": "charging", "port": "on"}
FULL_AMPS = [16, 16, 16]


def _site(car: Car, now: datetime.datetime, limited: list, action: str = "charge", **kwargs) -> list[dict]:
    def job() -> None:
        car.engine.charger_action = action
        car.engine.notify_site_limit(FULL_AMPS, limited, sum(limited) <= 0)

    return _job(car, now, job, **kwargs)


def _kw(amps: int) -> str:
    return f"{amps * 230 / 1000:.1f}"


def test_site_limit_after_10_minutes_once_a_day_each() -> None:
    car = Car(_at(12, 0), **CHARGING)
    _site(car, _at(12, 0), [10, 10, 10])
    _site(car, _at(12, 5), [10, 10, 10])
    assert not car.titled(SITE)  # not yet 10 minutes
    _site(car, _at(12, 10), [10, 10, 10])
    _site(car, _at(12, 20), [8, 8, 8])
    _site(car, _at(12, 30), [0, 0, 0])
    assert len(car.titled(SITE)) == 1  # the stop must hold by itself
    _site(car, _at(12, 40), [0, 0, 0])
    _site(car, _at(12, 50), [0, 0, 0])
    reduced, stopped = car.titled(SITE)
    # the time the limit began, not the end of the wait
    assert reduced["message"] == TEXTS["ui.notify_more.site_reduced"].format(time="12:00", before=_kw(48), after=_kw(30))
    assert stopped["message"] == TEXTS["ui.notify_more.site_stopped"].format(time="12:30", before=_kw(48))
    assert _uri(reduced) == "entityId:sensor.cjp_test_charger_action"
    tomorrow = _at(12, 0, day=DAY + datetime.timedelta(days=1))
    _site(car, tomorrow, [10, 10, 10])
    _site(car, tomorrow + datetime.timedelta(minutes=10), [10, 10, 10])
    assert len(car.titled(SITE)) == 3


def test_a_short_limit_is_no_site_notice() -> None:
    """A kettle: the limit lifts before 10 minutes, and the wait starts again."""
    car = Car(_at(12, 0), **CHARGING)
    _site(car, _at(12, 0), [10, 10, 10])
    _site(car, _at(12, 5), FULL_AMPS)
    _site(car, _at(12, 10), [10, 10, 10])
    assert not car.titled(SITE)
    _site(car, _at(12, 20), [10, 10, 10])
    assert len(car.titled(SITE)) == 1
    # a short stop during the reduction
    _site(car, _at(12, 31), [0, 0, 0])
    _site(car, _at(12, 35), [10, 10, 10])
    _site(car, _at(12, 45), [10, 10, 10])
    assert len(car.titled(SITE)) == 1


def test_site_limit_that_changes_the_phases_is_shown_as_power() -> None:
    car = Car(_at(12, 0), **CHARGING)
    _site(car, _at(12, 0), [16, 0, 0])
    _site(car, _at(12, 10), [16, 0, 0])
    [notice] = car.titled(SITE)
    assert notice["message"] == TEXTS["ui.notify_more.site_reduced"].format(time="12:00", before=_kw(48), after=_kw(16))


def test_a_pause_without_site_decisions_starts_the_wait_again() -> None:
    """Seen once, then no decisions (another car, the charger test): the old sighting is no proof the next day."""
    car = Car(_at(12, 0), **CHARGING)
    _site(car, _at(12, 0), [10, 10, 10])
    tomorrow = _at(12, 0, day=DAY + datetime.timedelta(days=1))
    _site(car, tomorrow, [10, 10, 10])
    assert not car.titled(SITE)  # a kettle the next day
    _site(car, tomorrow + datetime.timedelta(minutes=10), [10, 10, 10])
    [notice] = car.titled(SITE)
    assert notice["message"] == TEXTS["ui.notify_more.site_reduced"].format(time="12:00", before=_kw(48), after=_kw(30))


@pytest.mark.parametrize(("action", "kwargs", "options"), [
    ("stop", CHARGING, {}),  # nothing to limit
    ("charge", {}, {}),  # the car is not plugged in
    ("charge", {"states": {"sensor.charger_status": "unavailable"}}, {}),
    ("charge", CHARGING, {"notify_site_limit": False}),
])
def test_no_site_notice_without_charging_or_with_the_switch_off(action: str, kwargs: dict, options: dict) -> None:
    car = Car(_at(12, 0), options=options, **kwargs)
    _site(car, _at(12, 0), [6, 6, 6], action)
    _site(car, _at(12, 10), [6, 6, 6], action)
    assert not car.titled(SITE)


@pytest.mark.parametrize(("hold", "sent"), [(False, True), (True, False)])  # the charger test holds its own current
def test_site_limit_from_the_load_balancing(monkeypatch: pytest.MonkeyPatch, hold: bool, sent: bool) -> None:
    car = Car(_at(12, 0), **CHARGING)
    monkeypatch.setattr(car.engine, "site_clamp", lambda amps: [6, 6, 6])
    monkeypatch.setattr(car.engine, "site_text", lambda amps, limited: "")
    monkeypatch.setattr(car.engine, "charger_hold_active", lambda: hold)
    car.engine._charger_hold = {"site_stop": False} if hold else None

    def job() -> None:
        car.engine.charger_action = "charge"
        car.engine._apply_site_limit(list(FULL_AMPS), list(FULL_AMPS))

    _job(car, _at(12, 0), job)
    _job(car, _at(12, 10), job)
    assert len(car.titled(SITE)) == int(sent)


# ---------------------------------------------------------------------- #
# the battery stands full
# ---------------------------------------------------------------------- #

LEVEL = "sensor.ev_battery_level"


def _full(car: Car, now: datetime.datetime, *rows, **kwargs) -> list[dict]:
    return _job(car, now, lambda: car.engine._notice_battery_full(car.engine.now()), buffers={LEVEL: _rows(now, *rows)}, **kwargs)


def test_battery_full_for_12_hours_once_per_time_it_is_full() -> None:
    now = _at(12)
    car = Car(now, level=100)
    _full(car, now, (13 * 60, "99"), (8 * 60, "unavailable"), (6 * 60, "100"))
    [notice] = car.titled(FULL)
    assert notice["message"] == TEXTS["ui.notify_more.battery_full"].format(level=100, hours=13)
    assert _uri(notice) == "entityId:number.cjp_test_max_recommended_charge_limit_battery_level"
    _full(car, _at(14), (15 * 60, "99"))
    assert len(car.titled(FULL)) == 1
    # below full: a new time it is full
    _full(car, _at(15), (16 * 60, "99"), (5, "97"), level=97)
    assert "battery_full" not in car.engine.sent_notices
    later = _at(15, day=now.date() + datetime.timedelta(days=1))
    _full(car, later, (13 * 60, "99"))
    assert len(car.titled(FULL)) == 2


def test_battery_full_again_after_a_drop_only_the_history_saw() -> None:
    """Below full while no cron saw it (HA was down, or the level unavailable): the next time it is full is a new one."""
    now = _at(12)
    car = Car(now, level=100)
    _full(car, now, (13 * 60, "100"))
    next_day = now + datetime.timedelta(days=1)
    _full(car, next_day, (37 * 60, "100"))  # still the same time
    assert len(car.titled(FULL)) == 1
    _full(car, next_day, (37 * 60, "100"), (20 * 60, "95"), (13 * 60, "100"))
    assert len(car.titled(FULL)) == 2


def test_battery_full_not_again_after_a_restart_while_still_full() -> None:
    """After a restart the history begins with the state at its window's start: no new time it is full."""
    now = _at(12)
    car = Car(now, level=100)
    _full(car, now, (13 * 60, "100"))
    later = now + datetime.timedelta(days=8)
    _full(car, later, (7 * 24 * 60, "100"))
    assert len(car.titled(FULL)) == 1


@pytest.mark.parametrize(("rows", "settings", "sent"), [
    ([(11 * 60, "99")], {}, False),  # not long enough
    ([(13 * 60, "99"), (5 * 60, "97"), (3 * 60, "100")], {}, False),  # full again 3 hours ago
    ([(13 * 60, "99")], {"full_charge_recommended": 7.0}, False),  # a full charge on purpose: 24 hours
    ([(25 * 60, "99")], {"full_charge_recommended": 7.0}, True),
    ([(13 * 60, "99")], {"full_charge_recommended": None}, False),  # not known: the longer time
    ([], {}, False),  # no history: not known how long
])
def test_battery_full_needs_the_whole_time_at_full(rows: list, settings: dict, sent: bool) -> None:
    now = _at(12)
    car = Car(now, settings=settings, level=100)
    _full(car, now, *rows)
    assert len(car.titled(FULL)) == int(sent)


@pytest.mark.parametrize(("reported", "sent"), [(datetime.timedelta(hours=1), True), (datetime.timedelta(days=2), False), (None, False)])
def test_battery_full_needs_a_recent_car_report(reported, sent: bool) -> None:
    now = _at(12)
    options = {"last_update_entity_id": "sensor.ev_last_update"}
    state = (now.replace(tzinfo=TZ) - reported).isoformat() if reported else "unavailable"
    car = Car(now, options=options, level=100)
    _full(car, now, (13 * 60, "100"), states={"sensor.ev_last_update": state})
    assert len(car.titled(FULL)) == int(sent)


@pytest.mark.parametrize(("options", "level", "rows", "sent"), [
    (VIRTUAL, 100, [(13 * 60, "100")], False),
    ({"notify_battery_full": False}, 100, [(13 * 60, "100")], False),
    # an asleep car is unavailable: its last level counts for a day (battery_level), not longer
    ({}, "unavailable", [(13 * 60, "100"), (12 * 60, "unavailable")], True),
    ({}, "unavailable", [(26 * 60, "100"), (25 * 60, "unavailable")], False),
])
def test_no_battery_full_without_the_car_or_with_the_switch_off(options: dict, level, rows: list, sent: bool) -> None:
    now = _at(12)
    car = Car(now, options=options, level=100)
    _full(car, now, *rows, states={LEVEL: str(level)})
    assert len(car.titled(FULL)) == int(sent)


# ---------------------------------------------------------------------- #
# the charging loss rose
# ---------------------------------------------------------------------- #


def _segment(now: datetime.datetime, days_ago: float, kwh: float, soc: float = 10.0, group: str = "4-7 kW") -> dict:
    start = now.replace(tzinfo=TZ).timestamp() - days_ago * 86400
    return {"start": start, "kwh": kwh, "soc_delta": soc, "group": group, "kw": 5.5, "loss": 0.1, "phases": 3}


def _base(kwh: float = 40.0, soc: float = 50.0, starts: int = 5) -> dict:
    return {"kwh": kwh, "soc": soc, "starts": list(range(starts))}


def _loss(car: Car, now: datetime.datetime, segments: list, months: dict) -> list[dict]:
    def job() -> None:
        car.engine.charging_loss_db = {"segments": segments, "months": months}
        car.engine._notice_charging_loss(car.engine.now())

    return _job(car, now, job)


def test_charging_loss_rise_against_3_to_6_months_back_once_a_month() -> None:
    now = _at(12)  # October: the base is April to July
    car = Car(now)
    segments = [_segment(now, days, 10.0) for days in (2, 9, 20)]
    _loss(car, now, segments, {"2026-06": {"4-7 kW": _base()}})
    [notice] = car.titled(LOSS)
    assert notice["message"] == TEXTS["ui.notify_more.charging_loss"].format(group="4-7 kW", now="1.000", before="0.800", rise=25, days=30)
    assert _uri(notice) == "entityId:sensor.cjp_test_charging_loss_model"
    _loss(car, now + datetime.timedelta(days=3), segments, {"2026-06": {"4-7 kW": _base()}})
    assert len(car.titled(LOSS)) == 1


@pytest.mark.parametrize(("segments", "months"), [
    ([(2, 8.8), (9, 8.8), (20, 8.8)], {"2026-06": {"4-7 kW": _base()}}),  # +10 %
    ([(2, 10.0), (9, 10.0)], {"2026-06": {"4-7 kW": _base()}}),  # too few measurements now
    ([(2, 10.0), (9, 10.0), (40, 10.0)], {"2026-06": {"4-7 kW": _base()}}),  # one older than 30 days
    ([(2, 10.0), (9, 10.0), (20, 10.0)], {"2026-06": {"4-7 kW": _base(starts=4)}}),  # too few back then
    ([(2, 10.0), (9, 10.0), (20, 10.0)], {"2026-08": {"4-7 kW": _base()}}),  # only 2 months back
    ([(2, 10.0), (9, 10.0), (20, 10.0)], {"2026-03": {"4-7 kW": _base()}}),  # 7 months back
])
def test_no_charging_loss_notice_without_a_clear_rise_on_enough_data(segments: list, months: dict) -> None:
    now = _at(12)
    car = Car(now)
    _loss(car, now, [_segment(now, days, kwh) for days, kwh in segments], months)
    assert not car.titled(LOSS)


def test_charging_loss_only_at_4_kw_or_more_and_with_the_switch_on() -> None:
    now = _at(12)
    car = Car(now)
    _loss(car, now, [_segment(now, days, 10.0, group="2,5-4 kW") for days in (2, 9, 20)], {"2026-06": {"2,5-4 kW": _base()}})
    assert not car.titled(LOSS)
    off = Car(now, options={"notify_charging_loss": False})
    _loss(off, now, [_segment(now, days, 10.0) for days in (2, 9, 20)], {"2026-06": {"4-7 kW": _base()}})
    assert not off.titled(LOSS)


def test_no_charging_loss_notice_when_as_high_as_the_same_months_a_year_earlier() -> None:
    now = _at(12)  # October 2026: the year earlier is September and October 2025
    car = Car(now)
    segments = [_segment(now, days, 10.0) for days in (2, 9, 20)]  # 1.000 kWh per 1 %, +25 % against the summer
    winter = {"2026-06": {"4-7 kW": _base()}, "2025-10": {"4-7 kW": _base(kwh=30.0, soc=30.0, starts=3)}, "2025-09": {"4-7 kW": _base(kwh=16.0, soc=20.0, starts=2)}}
    _loss(car, now, segments, winter)  # a year earlier 46 kWh / 50 % = 0.920: only +9 %
    assert not car.titled(LOSS)


def test_charging_loss_rise_against_both_shows_the_year_earlier() -> None:
    now = _at(12)
    car = Car(now)
    segments = [_segment(now, days, 10.0) for days in (2, 9, 20)]
    months = {"2026-06": {"4-7 kW": _base()}, "2025-10": {"4-7 kW": _base(kwh=42.0, soc=50.0)}}  # 0.840: +19 %
    _loss(car, now, segments, months)
    [notice] = car.titled(LOSS)
    assert notice["message"] == (
        TEXTS["ui.notify_more.charging_loss"].format(group="4-7 kW", now="1.000", before="0.800", rise=25, days=30)
        + " " + TEXTS["ui.notify_more.charging_loss_year"].format(year="0.840")
    )


@pytest.mark.parametrize("year", [
    {"2025-10": {"4-7 kW": _base(kwh=50.0, starts=4)}},  # too few measurements a year earlier
    {"2025-10": {"4-7 kW": _base(kwh=30.0, soc=30.0)}},  # too few % a year earlier
    {"2025-08": {"4-7 kW": _base(kwh=50.0)}},  # 14 months back: not the same season
    {"2025-10": {"> 7 kW": _base(kwh=50.0)}},  # another power group
])
def test_charging_loss_without_enough_data_a_year_earlier_uses_3_to_6_months_alone(year: dict) -> None:
    now = _at(12)
    car = Car(now)
    _loss(car, now, [_segment(now, days, 10.0) for days in (2, 9, 20)], {"2026-06": {"4-7 kW": _base()}, **year})
    [notice] = car.titled(LOSS)
    assert notice["message"] == TEXTS["ui.notify_more.charging_loss"].format(group="4-7 kW", now="1.000", before="0.800", rise=25, days=30)


def test_charging_loss_shows_the_group_that_passes_the_year_earlier() -> None:
    """The bigger rise is the season (as high a year earlier); the other group's rise is shown with its own year value."""
    now = _at(12)
    car = Car(now)
    segments = [_segment(now, days, 10.0) for days in (2, 9, 20)] + [_segment(now, days, 9.0, group="> 7 kW") for days in (3, 10, 21)]
    months = {
        "2026-06": {"4-7 kW": _base(kwh=35.0), "> 7 kW": _base(kwh=37.5)},  # 1.000 against 0.700: +43 %, 0.900 against 0.750: +20 %
        "2025-10": {"4-7 kW": _base(kwh=50.0), "> 7 kW": _base(kwh=35.0)},  # a year earlier 1.000 (the season) and 0.700
    }
    _loss(car, now, segments, months)
    [notice] = car.titled(LOSS)
    assert notice["message"] == (
        TEXTS["ui.notify_more.charging_loss"].format(group="> 7 kW", now="0.900", before="0.750", rise=20, days=30)
        + " " + TEXTS["ui.notify_more.charging_loss_year"].format(year="0.700")
    )


@pytest.mark.parametrize(("year_kwh", "sent"), [(42.0, True), (46.0, False)])
def test_charging_loss_year_earlier_across_new_year(year_kwh: float, sent: bool) -> None:
    now = datetime.datetime(2027, 1, 20, 12, 30)  # the base is July to October 2026, a year earlier December and January
    car = Car(now)
    segments = [_segment(now, days, 10.0) for days in (2, 9, 15)]
    months = {"2026-08": {"4-7 kW": _base()}, "2026-01": {"4-7 kW": _base(kwh=year_kwh / 2, soc=25.0, starts=3)}, "2025-12": {"4-7 kW": _base(kwh=year_kwh / 2, soc=25.0, starts=2)}}
    _loss(car, now, segments, months)
    assert len(car.titled(LOSS)) == int(sent)


# ---------------------------------------------------------------------- #
# sent notices and failures
# ---------------------------------------------------------------------- #


def test_sent_notices_are_forgotten_at_their_time_and_junk_is_dropped() -> None:
    now = _at(12)
    past, future = (now - datetime.timedelta(hours=1)).isoformat(), (now + datetime.timedelta(hours=1)).isoformat()
    car = Car(now, databases={"runtime": {"sent_notices": {"old": past, "kept": future, "junk": 3, 4: future}}})
    assert car.engine.sent_notices == {"old": past, "kept": future}

    def job() -> None:
        assert car.engine._new_notice("new", now + datetime.timedelta(days=1))
        assert not car.engine._new_notice("kept", now + datetime.timedelta(days=1))

    _job(car, now, job)
    assert car.engine.sent_notices == {"kept": future, "new": (now + datetime.timedelta(days=1)).isoformat()}
    car.engine.restore_sent_notices(["not", "a", "dict"])
    assert car.engine.sent_notices == {}


def test_a_failing_notice_does_not_stop_the_others_or_the_cron(monkeypatch: pytest.MonkeyPatch) -> None:
    car = Car(_at(5), settings=_workday(datetime.time(9, 0)), level=20, **UNAVAILABLE)

    def broken(now) -> None:
        raise RuntimeError("boom")

    calls = []
    original = car.engine._notice_charging_loss
    monkeypatch.setattr(car.engine, notices.ControlNotices._notice_charger_offline.__name__, broken)
    monkeypatch.setattr(car.engine, "_notice_charging_loss", lambda now: (calls.append(now), original(now)))
    car.cron(_at(5))  # no error
    assert calls  # the last notice still ran
    assert not car.titled(OFFLINE)
