"""What CJP charged in a connection: the record made from the charging history when the connection is settled."""
from __future__ import annotations

import datetime

import pytest

from custom_components.cable_juice_planner.engine.whatif.recorded import RECORD_BACKFILL_DAYS, raw_sessions, window_record

from .test_economy import _engine, _ts
from .test_engine import DAY

BASE = datetime.datetime(2026, 3, 2)
HOUR = 3600.0


def _to_ts(moment: datetime.datetime) -> float:
    return moment.replace(tzinfo=datetime.UTC).timestamp()


def _session(hour: int, ended: str | None = "04:00", **fields) -> dict:
    return {
        BASE.replace(hour=hour): {
            "kWh": 8.0, "kWh_from_grid": 8.0, "kWh_from_local_energy": 0.0, "percentage": 15.0, "cost": 3.0, "ended": ended,
            "charging_type": ["planned"], **fields,
        }
    }


def _prices(**by_hour: float) -> dict[float, float]:
    return {_to_ts(BASE.replace(hour=int(hour[1:]))): price for hour, price in by_hour.items()}


def _record(history: dict, prices: dict, refund: float = 0.0):
    rows = raw_sessions(history, _to_ts)
    return window_record(rows, _to_ts(BASE.replace(hour=0)), _to_ts(BASE.replace(hour=7)), prices, refund)


def test_the_kwh_are_spread_over_the_session_and_priced_hour_by_hour() -> None:
    record, reason = _record(_session(2), _prices(h2=0.2, h3=0.4), refund=0.1)  # 8 kWh over 02:00-04:00 = 4 kWh in each hour
    assert reason is None
    assert record["kwh"] == 8.0 and record["percent"] == 15.0 and record["local_kwh"] == 0.0 and record["booked_cost"] == 3.0
    assert record["cost"] == pytest.approx(4.0 * 0.1 + 4.0 * 0.3)  # the refund is taken off the hourly price, like the replay does


def test_an_hour_without_a_price_is_never_filled_in() -> None:
    assert _record(_session(2), _prices(h2=0.2)) == (None, "unpriced_hours")
    assert _record(_session(2), {}) == (None, "unpriced_hours")


def test_a_session_that_is_still_running_is_not_settled() -> None:
    assert _record(_session(2, ended=">"), _prices(h2=0.2, h3=0.2)) == (None, "not_settled")


def test_a_session_with_a_missing_or_invalid_figure_makes_the_record_invalid() -> None:
    history = _session(2)
    del history[BASE.replace(hour=2)]["kWh"]
    assert _record(history, _prices(h2=0.2, h3=0.2)) == (None, "invalid_record")
    assert _record(_session(2, percentage=float("nan")), _prices(h2=0.2, h3=0.2)) == (None, "invalid_record")
    assert _record(_session(2, percentage=-1.0), _prices(h2=0.2, h3=0.2)) == (None, "invalid_record")


def test_public_charging_is_not_part_of_the_record_and_a_missing_booked_cost_is_not_zero() -> None:
    history = {**_session(2), **_session(5, ended="05:30", kWh_from_public=6.0, charging_type="public_charging")}
    record, reason = _record(history, _prices(h2=0.2, h3=0.2))
    assert reason is None and record["kwh"] == 8.0
    history = _session(2, cost=None)
    assert _record(history, _prices(h2=0.2, h3=0.2))[0]["booked_cost"] is None


def test_the_end_time_of_a_session_is_read_with_the_seconds_of_its_start_and_across_midnight() -> None:
    key = BASE.replace(hour=2, minute=0, second=30)
    [row] = raw_sessions({key: {"kWh": 1.0, "percentage": 1.0, "kWh_from_local_energy": 0.0, "ended": "02:00"}}, _to_ts)
    assert row["end"] - row["ts"] == 60.0  # the same minute: one minute, not the next day
    late = BASE.replace(hour=23, minute=30)
    [row] = raw_sessions({late: {"kWh": 1.0, "percentage": 1.0, "kWh_from_local_energy": 0.0, "ended": "01:00"}}, _to_ts)
    assert row["end"] - row["ts"] == 90 * 60.0
    [row] = raw_sessions({late: {"kWh": 1.0, "percentage": 1.0, "kWh_from_local_energy": 0.0}}, _to_ts)
    assert row["end"] - row["ts"] == 60.0  # no end time


def test_a_figure_that_is_not_a_number_is_invalid_not_zero() -> None:
    [row] = raw_sessions({BASE.replace(hour=2): {"kWh": "8", "percentage": 15.0, "kWh_from_local_energy": 0.0, "ended": "03:00"}}, _to_ts)
    assert row["valid"] is False
    [row] = raw_sessions({BASE.replace(hour=2): {"kWh": True, "percentage": 15.0, "kWh_from_local_energy": 0.0, "ended": "03:00"}}, _to_ts)
    assert row["valid"] is False


# -- stored with the connection when it is settled (engine/economy.py) ----------------------------------------------------------


def _store(engine, snap, windows, prices, now):
    result = engine.run_job(snap, engine._economy_store_records, windows, prices, now)  # noqa: SLF001
    assert result.error is None, result.error


def _connection(back: int) -> dict:
    return {"start": _ts(DAY - datetime.timedelta(days=back), 18), "end": _ts(DAY - datetime.timedelta(days=back - 1), 7)}


def _charged(engine, back: int, ended: str = "04:00") -> None:
    day = DAY - datetime.timedelta(days=back - 1)
    engine.charging_history_db[datetime.datetime.combine(day, datetime.time(2))] = {
        "kWh": 8.0, "kWh_from_grid": 8.0, "kWh_from_local_energy": 0.0, "percentage": 15.0, "cost": 3.0, "ended": ended, "charging_type": ["planned"],
    }


def _hours(back: int, price: float = 0.4) -> dict[float, float]:
    day = DAY - datetime.timedelta(days=back - 1)
    return {_ts(day, hour): price for hour in range(0, 7)}


def test_a_settled_connection_gets_its_record_once() -> None:
    engine, snap = _engine()
    windows = [_connection(2)]
    _charged(engine, 2)
    _store(engine, snap, windows, _hours(2), _ts(DAY, 12))
    record = windows[0]["record"]
    assert record["kwh"] == 8.0 and record["cost"] == pytest.approx(8.0 * 0.4)
    windows[0]["record"]["cost"] = 123.0  # the stored record is never made again
    _store(engine, snap, windows, _hours(2, 9.0), _ts(DAY, 12))
    assert windows[0]["record"]["cost"] == 123.0


def test_a_connection_that_is_not_settled_yet_gets_no_record() -> None:
    engine, snap = _engine()
    windows = [_connection(1)]
    _charged(engine, 1)
    _store(engine, snap, windows, _hours(1), windows[0]["end"] + 3600)  # unplugged an hour ago
    assert "record" not in windows[0]
    _store(engine, snap, windows, _hours(1), windows[0]["end"] + 3 * 3600)
    assert "record" in windows[0]


def test_a_connection_that_could_not_be_priced_is_tried_again_until_the_history_is_combined() -> None:
    engine, snap = _engine()
    windows = [_connection(2)]
    _charged(engine, 2)
    _store(engine, snap, windows, {}, _ts(DAY, 12))
    assert "record" not in windows[0]  # the price statistics are read once a day: tried again
    _store(engine, snap, windows, _hours(2), _ts(DAY, 12))
    assert "record" in windows[0]
    # older than the days the sessions are kept apart: never recorded from the combined history
    old = [_connection(RECORD_BACKFILL_DAYS + 3)]
    _charged(engine, RECORD_BACKFILL_DAYS + 3)
    _store(engine, snap, old, _hours(RECORD_BACKFILL_DAYS + 3), _ts(DAY, 12))
    assert "record" not in old[0]


def test_a_night_without_a_session_is_a_record_of_nothing_only_where_the_history_reaches_back_to_it() -> None:
    engine, snap = _engine()
    windows = [_connection(2)]
    _store(engine, snap, windows, _hours(2), _ts(DAY, 12))
    assert "record" not in windows[0]  # no history at all: nothing is known, not a record of zero
    _charged(engine, 5)  # the history starts three nights earlier
    _store(engine, snap, windows, _hours(2), _ts(DAY, 12))
    assert windows[0]["record"]["kwh"] == 0.0 and windows[0]["record"]["cost"] == 0.0
    before = [_connection(6)]
    _store(engine, snap, before, _hours(6), _ts(DAY, 12))
    assert "record" not in before[0]  # ended before the first session of the history


def test_a_session_still_running_in_the_connection_gives_no_record_yet() -> None:
    engine, snap = _engine()
    windows = [_connection(2)]
    _charged(engine, 2, ended=">")
    _store(engine, snap, windows, _hours(2), _ts(DAY, 12))
    assert "record" not in windows[0]
