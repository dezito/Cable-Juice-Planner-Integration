"""The states of a window are held until the next change (engine/history/history.py), not drawn as lines between rows.

A sensor writes a row only when its state changes. A charger or battery power that jumps between 0 and
several kW with minutes between the rows got ramps over the quiet time: the energy of a window was far
too high (a battery that blinked for 90 seconds in an hour counted as 2.3 kWh instead of 0.13 kWh).
"""
from __future__ import annotations

import datetime
import zoneinfo

import pytest

from custom_components.cable_juice_planner.engine.history.history import History

TZ = zoneinfo.ZoneInfo("Europe/Copenhagen")
DAY = datetime.date(2026, 11, 18)
NOW = datetime.datetime.combine(DAY, datetime.time(12, 0))
NOW_TS = NOW.replace(tzinfo=TZ).timestamp()


def at(hour: int, minute: int = 0, second: int = 0) -> float:
    return datetime.datetime.combine(DAY, datetime.time(hour, minute, second), tzinfo=TZ).timestamp()


def naive(hour: int, minute: int = 0, second: int = 0) -> datetime.datetime:
    return datetime.datetime.combine(DAY, datetime.time(hour, minute, second))


def history(rows, statistics=None, unit="W", entity_id="sensor.power") -> History:
    return History({entity_id: rows}, statistics or {}, {entity_id: unit}, TZ, NOW)


def values(rows, start=naive(5), end=naive(6), **kwargs) -> list[float]:
    return history(rows, **kwargs).get_values("sensor.power", start, end, float_type=True, convert_to="W", error_state=[])


def mean(numbers: list[float]) -> float:
    return sum(numbers) / len(numbers)


# the rows of a Huawei battery on 100 % (power in W as text, positive = charging)
FULL_BATTERY = [
    (at(4, 58, 42), "2641.0"), (at(4, 59, 13), "49.0"), (at(4, 59, 44), "-539.0"),
    (at(5, 0, 15), "0.0"),
    (at(5, 5, 25), "4998.0"), (at(5, 5, 56), "0.0"),
    (at(5, 35, 24), "5000.0"), (at(5, 35, 55), "0.0"),
    (at(5, 55, 35), "5000.0"), (at(5, 56, 6), "0.0"),
]


def test_a_full_battery_that_blinks_does_not_charge_for_an_hour() -> None:
    numbers = values(FULL_BATTERY, end=naive(5, 59, 59))
    assert len(numbers) == 100
    charging = mean([max(number, 0.0) for number in numbers])
    assert charging == pytest.approx(130, abs=100)  # three bursts of 30 s at 5 kW; lines between the rows gave 2300 W
    assert charging < 300


def test_a_state_is_held_until_the_next_row() -> None:
    numbers = values([(at(4), "100"), (at(5, 30), "0")])
    assert set(numbers) == {100.0, 0.0}  # every value is a real state, nothing in between
    assert mean(numbers) == pytest.approx(50, abs=1.5)


def test_a_short_burst_after_a_quiet_stretch_is_not_a_ramp() -> None:
    numbers = values([(at(5), "0"), (at(5, 30), "11000"), (at(5, 30, 30), "0")])
    assert mean(numbers) < 250  # 92 W exactly, 0 or 110 W sampled (a point every 36 s); a line from the 0 to the 11 kW row gave 5500 W


def test_the_state_at_the_start_of_the_window_counts() -> None:
    assert values([(at(3), "700")]) == [700.0] * 100


def test_a_window_before_the_first_row_holds_the_first_value_back() -> None:
    assert values([(at(5, 30), "400")]) == [400.0] * 100


def test_the_last_value_is_the_newest_state() -> None:
    """The newest state, also when it changed minutes ago (get_last_value reads the rows, get_values ends with the state now)."""
    rows = [(at(10), "55"), (at(11, 56), "60")]
    engine = history(rows)
    assert engine.get_last_value("sensor.power", float_type=True) == 60.0
    assert engine.get_values("sensor.power", naive(11), naive(12), float_type=True)[-1] == 60.0


def test_the_unit_is_converted_to_watt() -> None:
    numbers = values([(at(4), "2")], unit="kW")
    assert numbers == [2000.0] * 100


def test_the_last_value_of_a_sensor_that_is_unavailable_now_is_the_last_number_it_reported() -> None:
    """The way get_last_value is used (battery level, kWh meter): the sensor is lost now, a number came minutes before."""
    rows = [(at(4), "50"), (at(11, 54), "55"), (at(11, 57), "unavailable")]  # NOW is 12:00
    assert history(rows).get_last_value("sensor.power", float_type=True) == 55.0
    # a number that was reported once, long before the window: it still is the last value
    rows = [(NOW_TS - 3 * 86400, "71"), (at(11, 57), "unknown")]
    assert history(rows).get_last_value("sensor.power", float_type=True) == 71.0
    # and none at all: the error state
    engine = history([(at(11, 57), "unavailable")])
    assert engine.get_last_value("sensor.power", float_type=True, error_state=None, warn=False) is None


def test_a_number_reported_between_two_unavailable_states_is_the_last_value() -> None:
    """The case a sampled version missed: the number lasted three minutes, no sample point fell in it."""
    rows = [(at(4), "unavailable"), (at(11, 54), "55"), (at(11, 57), "unavailable")]
    assert history(rows).get_last_value("sensor.power", float_type=True) == 55.0
    assert history([(at(11, 57), "unavailable")]).get_last_value("sensor.power", float_type=True, error_state=None, warn=False) is None
    assert history([(at(12, 30), "5")]).get_last_value("sensor.power", float_type=True, error_state=None, warn=False) is None  # only in the future


def test_the_last_value_of_a_mixed_series_is_the_newest_row() -> None:
    assert history([(at(10), "abc"), (at(11), "55")]).get_last_value("sensor.power") == 55.0
    assert history([(at(10), "55"), (at(11), "abc")]).get_last_value("sensor.power") == "abc"
    assert history([(at(10), "55"), (at(11), "abc")]).get_last_value("sensor.power", float_type=True) == 55.0


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "unavailable", "unknown"])
def test_the_last_value_is_not_a_number_that_is_no_measurement(bad: str) -> None:
    rows = [(at(10), "42"), (at(11), bad)]
    assert history(rows).get_last_value("sensor.power", float_type=True) == 42.0


def test_the_last_value_of_a_text_sensor_and_the_unit() -> None:
    assert history([(at(9), "charging"), (at(11, 30), "unavailable")], entity_id="sensor.status").get_last_value("sensor.status") == "charging"
    assert history([(at(9), "charging")], entity_id="sensor.status").get_last_value("sensor.status", float_type=True, error_state=None, warn=False) is None
    assert history([(at(11), "2")], unit="kW").get_last_value("sensor.power", float_type=True, convert_to="W") == 2000.0


def test_a_text_state_in_a_series_of_numbers_ends_the_hold_and_stays_a_text_state() -> None:
    numbers = values([(at(4), "100"), (at(5, 30), "abc"), (at(5, 45), "200")])
    assert set(numbers) == {100.0, 200.0}
    assert numbers.count(100.0) == pytest.approx(50, abs=2)
    assert numbers.count(200.0) == pytest.approx(25, abs=2)
    states = history([(at(4), "100"), (at(5, 30), "abc")]).get_values("sensor.power", naive(5), naive(6), include_timestamps=True)
    assert "abc" in states.values()


def test_a_row_without_a_value_is_a_gap() -> None:
    numbers = values([(at(4), "100"), (at(5, 30), None), (at(5, 45), "200")])
    assert set(numbers) == {100.0, 200.0}
    assert numbers.count(100.0) == pytest.approx(50, abs=2)


def test_gaps_and_the_hourly_statistics_are_averaged_together() -> None:
    """The average is over the known time of the states (50 values here, a gap of half an hour) and the 100 values of the statistics."""
    stats = {"sensor.power": [(at(5), 400.0)]}
    rows = [(at(4), "100"), (at(5, 30), "unavailable")]  # 100 W for the first half hour, then no value
    average = history(rows, stats).get_average_value("sensor.power", naive(5), naive(6), convert_to="W")
    assert average == pytest.approx((100 * 50 + 400 * 100) / 150, rel=0.03)


def test_the_average_is_the_time_weighted_mean() -> None:
    rows = [(at(4), "0"), (at(5, 45), "1000")]
    assert history(rows).get_average_value("sensor.power", naive(5), naive(6), convert_to="W") == pytest.approx(250, abs=8)


@pytest.mark.parametrize("gap", ["unavailable", "unknown", "nan", "inf"])
def test_a_sensor_that_is_lost_does_not_keep_its_last_value(gap: str) -> None:
    """5 kW, then lost for 20 minutes, then 100 W: the lost time gives no value (it is not 5 kW for 20 more minutes)."""
    numbers = values([(at(4, 59), "5000"), (at(5, 10), gap), (at(5, 30), "100")])
    assert set(numbers) == {5000.0, 100.0}
    assert numbers.count(5000.0) == pytest.approx(100 * 10 / 60, abs=2)
    assert numbers.count(100.0) == pytest.approx(100 * 30 / 60, abs=2)
    assert mean(numbers) == pytest.approx((5000 * 10 + 100 * 30) / 40, rel=0.05)  # only the known time


def test_a_window_that_starts_while_the_sensor_is_lost_has_no_values_until_it_is_back() -> None:
    numbers = values([(at(4), "unavailable"), (at(5, 30), "100")])
    assert set(numbers) == {100.0}
    assert len(numbers) == pytest.approx(50, abs=2)


def test_a_sensor_that_was_never_a_number_gives_the_error_state() -> None:
    engine = history([(at(4), "unavailable"), (at(5), "unknown")])
    assert engine.get_values("sensor.power", naive(5), naive(6), float_type=True, error_state=[]) == []
    assert engine.get_average_value("sensor.power", naive(5), naive(6), error_state=-1.0) == -1.0


def test_status_sensors_keep_their_text_states_with_their_own_timestamps() -> None:
    rows = [(at(4), "disconnected"), (at(5, 20), "charging"), (at(5, 40), "completed")]
    states = history(rows, entity_id="sensor.status").get_values("sensor.status", naive(5), naive(6), include_timestamps=True)
    assert list(states.values()) == ["disconnected", "charging", "completed"]
    assert list(states)[1] == naive(5, 20)


def test_the_hourly_statistics_are_still_interpolated_as_before() -> None:
    """Only the states are held; the long term statistics of the hours are not part of this change."""
    stats = {"sensor.power": [(at(5), 100.0), (at(6), 300.0)]}
    numbers = list(history([], stats).get_longterm_values("sensor.power", naive(5), naive(7)))
    assert len(numbers) == 100
    assert numbers[0] == 100.0
    assert 100.0 < numbers[25] < 300.0  # a line between the two hours
    assert numbers[-1] == 300.0
