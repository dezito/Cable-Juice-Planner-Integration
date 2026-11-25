"""Times with an offset are read in local time: the weather forecast in UTC (solar surplus, preheating) and the car's
last update (a timestamp sensor's state is UTC) are compared with CJP's local naive time."""
from __future__ import annotations

import datetime

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.engine import CJPEngine
from custom_components.cable_juice_planner.utils.helpers import to_datetime

from .test_engine import DAY, OPTIONS, TEXTS, TZ, snapshot

NOW = datetime.datetime.combine(DAY, datetime.time(12, 30))  # CEST, UTC+2
LAST_UPDATE = "sensor.ev_last_update"


def test_a_string_with_an_offset_is_converted_to_the_local_time() -> None:
    assert to_datetime("2026-10-22T10:00:00+00:00", TZ) == datetime.datetime(2026, 10, 22, 12, 0)
    assert to_datetime("2026-10-22T12:00:00", TZ) == datetime.datetime(2026, 10, 22, 12, 0)  # no offset: as it is
    assert to_datetime("2026-10-22T10:00:00+00:00") == datetime.datetime(2026, 10, 22, 10, 0)  # without tz: unchanged
    assert to_datetime("unknown", TZ) == "unknown"
    moment = datetime.datetime(2026, 10, 22, 10, 0)
    assert to_datetime(moment, TZ) is moment  # not a string: unchanged, as before


def test_a_forecast_in_utc_is_found_by_the_local_hour_and_date() -> None:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    snap = snapshot(NOW)
    snap.forecast_dict = {
        "hourly": [{"datetime": "2026-10-22T10:00:00+00:00", "condition": "sunny"}, {"datetime": "2026-10-22T11:00:00+00:00", "condition": "rainy"}],
        "daily": [{"datetime": "2026-10-22T22:00:00+00:00", "condition": "snowy"}],  # midnight of the 23rd in Copenhagen
    }
    found = {}

    def read() -> None:
        forecast = engine.get_forecast_dict()
        found["noon"] = engine.get_forecast(forecast, datetime.datetime(2026, 10, 22, 12, 0))
        found["13"] = engine.get_forecast(forecast, datetime.datetime(2026, 10, 22, 13, 0))
        found["day"] = engine.get_forecast({"daily": forecast["daily"]}, datetime.datetime(2026, 10, 23, 0, 0))

    assert engine.run_job(snap, read).error is None
    assert (found["noon"]["condition"], found["13"]["condition"], found["day"]["condition"]) == ("sunny", "rainy", "snowy")


def test_winter_time_and_the_night_the_clocks_go_back() -> None:
    assert to_datetime("2026-11-25T11:00:00+00:00", TZ) == datetime.datetime(2026, 11, 25, 12, 0)  # CET, UTC+1
    # 25 October: 00:00Z and 01:00Z are both 02:00 local; the first row (summer time) is kept, like the price keys
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    snap = snapshot(NOW)
    snap.forecast_dict = {"hourly": [{"datetime": "2026-10-25T00:00:00+00:00", "condition": "sunny"}, {"datetime": "2026-10-25T01:00:00+00:00", "condition": "rainy"}]}
    found = {}

    def read() -> None:
        found["02"] = engine.get_forecast(engine.get_forecast_dict(), datetime.datetime(2026, 10, 25, 2, 0))

    assert engine.run_job(snap, read).error is None
    assert found["02"]["condition"] == "sunny"


def _wakes(last_update: str) -> list:
    engine = CJPEngine(const.build_config({**OPTIONS, "last_update_entity_id": LAST_UPDATE}), "cjp_test", TEXTS)
    snap = snapshot(NOW)
    snap.states[LAST_UPDATE] = last_update
    result = engine.run_job(snap, engine.wake_up_ev)
    assert result.error is None, result.error
    return [event for event in result.events if event[0] == "wake_up"]


def test_a_car_updated_minutes_ago_in_utc_is_not_woken() -> None:
    assert not _wakes("2026-10-22T10:25:00+00:00")  # 12:25 local, five minutes ago


def test_a_car_not_updated_for_half_an_hour_is_woken() -> None:
    assert _wakes("2026-10-22T10:00:00+00:00")  # 12:00 local
