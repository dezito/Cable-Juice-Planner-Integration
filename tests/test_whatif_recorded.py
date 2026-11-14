"""What CJP charged in a connection: the charging history laid over the connections (engine/whatif/recorded.py)."""
from __future__ import annotations

import datetime

import pytest

from custom_components.cable_juice_planner.engine.whatif.recorded import allocate, history_rows, outside_kwh

BASE = datetime.datetime(2026, 3, 2)
HOUR = 3600.0
BATTERY = 75.0


def _to_ts(moment: datetime.datetime) -> float:
    return moment.replace(tzinfo=datetime.UTC).timestamp()


NOW = _to_ts(datetime.datetime(2030, 1, 1))  # long after everything below


def _at(day: int, hour: int, minute: int = 0) -> datetime.datetime:
    return BASE + datetime.timedelta(days=day, hours=hour, minutes=minute)


def _entry(day: int, hour: int, ended: str, **fields) -> dict:
    return {
        _at(day, hour): {
            "kWh": 8.0, "kWh_from_grid": 8.0, "kWh_from_local_energy": 0.0, "percentage": 15.0, "cost": 3.0, "ended": ended,
            "charging_type": ["planned"], **fields,
        }
    }


def _older() -> dict:
    """A session long before the connections of a test: the history reaches back past them."""
    return _entry(-8, 12, "13:00")


def _window(day: int, start_hour: int = 18, end_hour: int = 7) -> tuple[float, float]:
    """Evening of `day` to the morning of the next day."""
    return _to_ts(_at(day, start_hour)), _to_ts(_at(day + 1, end_hour))


def _rows(history: dict, battery: float | None = BATTERY) -> list[dict]:
    return history_rows(history, _to_ts, battery)


def test_a_session_inside_one_connection_gives_all_of_its_figures_to_it() -> None:
    window = _window(0)
    result = allocate(_rows({**_older(), **_entry(1, 2, "04:00")}), [window], NOW, 7200.0)
    assert result["records"][window[0]] == {"kwh": 8.0, "percent": 15.0, "local_kwh": 0.0, "cost": 3.0}
    assert result["split"] == 0 and result["reasons"] == {}


def test_a_night_without_a_session_is_charged_nothing_only_where_the_history_reaches_it() -> None:
    rows = _rows(_entry(1, 2, "04:00"))  # the history starts at the morning of day 1
    after, before = _window(1), _window(-3)
    result = allocate(rows, [before, after], NOW, 7200.0)
    assert result["records"][after[0]] == {"kwh": 0.0, "percent": 0.0, "local_kwh": 0.0, "cost": 0.0}
    assert result["reasons"] == {before[0]: "before_history"}  # never a favourable zero
    assert allocate([], [after], NOW, 7200.0)["reasons"] == {after[0]: "before_history"}  # no history at all: nothing is known


def test_a_merged_day_that_reaches_into_two_connections_is_split_by_the_time_it_overlaps_each() -> None:
    first, second = _window(0), _window(1)
    # the merged day of the 2nd: the morning session of the first connection (02:00) and the evening session of the second (23:30)
    rows = _rows({**_older(), **_entry(1, 2, "23:30", **{"kWh": 16.0, "percentage": 30.0, "cost": 6.0})})
    result = allocate(rows, [first, second], NOW, 7200.0)
    one, two = result["records"][first[0]], result["records"][second[0]]
    assert one["kwh"] + two["kwh"] == pytest.approx(16.0) and one["cost"] + two["cost"] == pytest.approx(6.0)
    assert one["kwh"] > 0 and two["kwh"] > 0 and result["split"] == 1
    # 02:00-07:01 in the first connection (the end tolerance), 17:50-23:30 in the second (the start tolerance)
    assert one["kwh"] == pytest.approx(16.0 * (5 * 3600 + 60) / ((5 * 3600 + 60) + (5 * 3600 + 40 * 60)), rel=1e-3)


def test_public_charging_in_an_entry_is_taken_out_and_a_public_only_entry_is_left_out() -> None:
    merged = _entry(1, 2, "05:00", **{"kWh": 18.0, "percentage": 15.0 + 10.0 / BATTERY * 100.0, "cost": 11.0, "kWh_from_public": 10.0, "public_cost": 8.0})
    [row] = _rows(merged)
    assert row["kwh"] == pytest.approx(8.0) and row["cost"] == pytest.approx(3.0) and row["percent"] == pytest.approx(15.0)
    public_only = _entry(1, 2, "05:00", **{"kWh_from_grid": 0.0, "kWh_from_public": 8.0, "public_cost": 3.0, "percentage": 8.0 / BATTERY * 100.0, "charging_type": "public_charging"})
    assert _rows(public_only) == []
    assert not _rows(_entry(1, 2, "05:00", **{"kWh_from_public": 8.0}))[0]["valid"]  # public kWh without public cost: unknown
    assert not _rows(merged, battery=None)[0]["valid"]  # and without the battery size its percent is unknown


def test_a_missing_or_invalid_figure_makes_the_connection_invalid_not_zero() -> None:
    window = _window(0)
    history = _entry(1, 2, "04:00")
    del history[_at(1, 2)]["cost"]
    assert allocate(_rows(history), [window], NOW, 7200.0)["reasons"] == {window[0]: "invalid_history"}
    for figure in ("kWh", "percentage", "kWh_from_local_energy"):
        bad = _entry(1, 2, "04:00", **{figure: float("nan")})
        assert allocate(_rows(bad), [window], NOW, 7200.0)["reasons"] == {window[0]: "invalid_history"}, figure
    negative = _entry(1, 2, "04:00", percentage=-1.0)
    assert allocate(_rows(negative), [window], NOW, 7200.0)["reasons"] == {window[0]: "invalid_history"}
    # a booked cost may be negative (a negative price), a figure that is not a number may not
    assert allocate(_rows({**_older(), **_entry(1, 2, "04:00", cost=-1.5)}), [window], NOW, 7200.0)["records"][window[0]]["cost"] == -1.5
    assert not _rows(_entry(1, 2, "04:00", kWh="8"))[0]["valid"] and not _rows(_entry(1, 2, "04:00", kWh=True))[0]["valid"]


def test_a_session_that_is_still_running_and_a_connection_that_just_ended_are_not_settled() -> None:
    window = _window(0)
    assert allocate(_rows(_entry(1, 2, ">")), [window], NOW, 7200.0)["reasons"] == {window[0]: "not_settled"}
    assert allocate(_rows(_entry(1, 2, "04:00")), [window], window[1] + 3600.0, 7200.0)["reasons"] == {window[0]: "not_settled"}
    assert window[0] in allocate(_rows({**_older(), **_entry(1, 2, "04:00")}), [window], window[1] + 3 * 3600.0, 7200.0)["records"]


def test_charging_that_no_connection_holds_is_counted() -> None:
    window = _window(0)
    rows = _rows({**_entry(1, 2, "04:00"), **_entry(1, 12, "13:00", **{"kWh": 6.0})})  # the second one is at noon, the car away
    result = allocate(rows, [window], NOW, 7200.0)
    assert outside_kwh(rows, result["weights"], window[0], window[1] + 86400.0) == pytest.approx(6.0)
    assert outside_kwh(rows, result["weights"], window[0], window[1]) == pytest.approx(0.0)  # noon is after this stretch


def test_the_end_time_of_an_entry_is_read_with_the_seconds_of_its_start_and_across_midnight() -> None:
    key = BASE.replace(hour=2, minute=0, second=30)
    [row] = _rows({key: {"kWh": 1.0, "percentage": 1.0, "kWh_from_local_energy": 0.0, "cost": 0.1, "ended": "02:00"}})
    assert row["end"] - row["ts"] == 60.0  # the same minute: one minute, not the next day
    late = BASE.replace(hour=23, minute=30)
    [row] = _rows({late: {"kWh": 1.0, "percentage": 1.0, "kWh_from_local_energy": 0.0, "cost": 0.1, "ended": "01:00"}})
    assert row["end"] - row["ts"] == 90 * 60.0
    [row] = _rows({late: {"kWh": 1.0, "percentage": 1.0, "kWh_from_local_energy": 0.0, "cost": 0.1}})
    assert row["end"] - row["ts"] == 60.0  # no end time


def test_a_connection_that_starts_before_the_first_entry_has_no_record() -> None:
    straddling = _window(0)  # 18:00 on day 0 to 07:00 on day 1; the first entry is at 02:00 on day 1: its evening may be gone
    rows = _rows(_entry(1, 2, "04:00"))
    assert allocate(rows, [straddling], NOW, 7200.0)["reasons"] == {straddling[0]: "before_history"}


def test_grid_energy_booked_at_zero_is_invalid_but_solar_at_zero_is_not() -> None:
    assert not _rows(_entry(1, 2, "04:00", cost=0.0))[0]["valid"]  # CJP writes 0 when it could not calculate the price
    assert _rows(_entry(1, 2, "04:00", cost=0.0, **{"kWh": 8.0, "kWh_from_grid": 0.0, "kWh_from_local_energy": 8.0}))[0]["valid"]
    assert _rows(_entry(1, 2, "04:00", cost=0.0, **{"kWh": 0.05, "kWh_from_grid": 0.05}))[0]["valid"]  # a trickle: nothing to price


def test_a_row_over_three_connections_is_split_in_three_and_nothing_is_lost() -> None:
    windows = [_window(-1), _window(0), _window(1)]
    wide = _rows({**_older(), _at(0, 1): {"kWh": 30.0, "kWh_from_grid": 30.0, "kWh_from_local_energy": 0.0, "percentage": 45.0, "cost": 9.0, "ended": "20:00", "charging_type": ["planned"]}})
    wide[-1]["end"] = _to_ts(_at(2, 20))  # a merged entry that was read as running into the third connection (the newest row)
    result = allocate(wide, windows, NOW, 7200.0)
    assert len([r for r in result["records"].values() if r["kwh"] > 0]) == 3 and result["split"] == 1
    assert sum(r["kwh"] for r in result["records"].values()) == pytest.approx(30.0, abs=1e-3)


def test_an_entry_that_is_still_running_makes_only_its_own_connections_not_settled() -> None:
    first, second = _window(0), _window(2)
    rows = _rows({**_older(), **_entry(3, 2, ">"), **_entry(1, 2, "04:00")})
    result = allocate(rows, [first, second], NOW, 7200.0)
    assert first[0] in result["records"] and result["reasons"] == {second[0]: "not_settled"}


def test_the_edges_of_an_estimated_connection_are_known_to_about_two_hours() -> None:
    """The connection estimated from the battery statistics runs 18:00-07:00; a session at 15:30-16:30 lies 1.5 h before it."""
    window = _window(0)
    rows = _rows({**_older(), **_entry(0, 15, "16:30")})
    measured = allocate(rows, [window], NOW, 7200.0)
    estimated = allocate(rows, [window], NOW, 7200.0, estimated={window[0]})
    assert window[0] in measured["records"] and measured["records"][window[0]]["kwh"] == 0.0  # not inside: the connection has nothing
    assert estimated["records"][window[0]]["kwh"] == 8.0  # inside the estimate's wider edge: it is CJP's charging of that connection
    assert outside_kwh(rows, measured["weights"], window[0], window[1]) == 0.0  # (the session starts before the period asked for)
    far = _rows({**_older(), **_entry(0, 10, "11:00")})
    assert allocate(far, [window], NOW, 7200.0, estimated={window[0]})["records"][window[0]]["kwh"] == 0.0  # eight hours before: no
    # the history must reach the wider edge too: the first entry two hours before the start is not enough to know the night
    near_start = _rows(_entry(0, 17, "17:30"))
    assert window[0] not in allocate(near_start, [window], NOW, 7200.0, estimated={window[0]})["records"]
    assert allocate(near_start, [window], NOW, 7200.0, estimated={window[0]})["reasons"][window[0]] == "before_history"


def test_the_room_at_the_edge_of_an_estimated_connection_never_reaches_into_its_neighbour() -> None:
    """An estimated connection ends right before the first measured one: a session just after that start belongs to the measured one."""
    estimated, measured = _window(0, 18, 7), (_to_ts(_at(1, 7, 30)), _to_ts(_at(1, 15)))
    rows = _rows({**_older(), **_entry(1, 8, "09:00")})  # inside the measured connection, within two hours of the estimated one's end
    result = allocate(rows, [estimated, measured], NOW, 7200.0, estimated={estimated[0]})
    assert result["records"][measured[0]]["kwh"] == 8.0 and result["records"][estimated[0]]["kwh"] == 0.0 and result["split"] == 0

