"""What-if: connections older than CJP's own records, estimated from the battery level and charger power statistics."""
from __future__ import annotations

import pytest

from custom_components.cable_juice_planner.engine.whatif.derive import (
    HOUR,
    clean_hours,
    derive_windows,
    typical_lengths,
    used_between,
)

DAY = 86400.0
BASE = 1_700_000_000 - (1_700_000_000 % DAY)  # a day boundary


def at(day: int, hour: int = 0) -> float:
    return BASE + day * DAY + hour * HOUR


def charge_row(index: int, count: int, level: float = 60.0) -> list:
    """The charging hour `index` of `count`: the level rises 20 % in all, so each hour starts where the one before ended."""
    low, high = level + 20.0 * index / count, level + 20.0 * (index + 1) / count
    return [(low + high) / 2, low, high, 7000.0]


def statistics_day(day: int, *, plug: int = 17, leave: int = 7, charge: tuple[int, ...] = (1, 2, 3), level: float = 60.0) -> dict[float, list]:
    """The statistics of one evening, night and day: the car is parked and (some hours of the night) charging from `plug`
    until the drive away in the hour `leave`, parked at work (no charger) until the drive home, which is the hour before
    `plug` of the next day. Rows are [mean level, lowest, highest, mean charger power in W]."""
    rows: dict[float, list] = {}
    charged = {at(day + 1, h) for h in charge}
    after = level + 20 if charged else level
    for hour in range(int(at(day, plug)), int(at(day + 1, leave)), int(HOUR)):
        if hour in charged:
            rows[float(hour)] = charge_row(sorted(charged).index(hour), len(charged), level)
        elif charged and hour > min(charged):
            rows[float(hour)] = [after, after, after, 0.0]
        else:
            rows[float(hour)] = [level, level, level, 0.0]
    rows[at(day + 1, leave)] = [after - 1, after - 2, after, 0.0]  # driving away: the level moved, no charging
    for h in range(leave + 1, plug - 1):  # parked at work, no charger
        rows[at(day + 1, h)] = [after - 2, after - 2, after - 2, 0.0]
    rows[at(day + 1, plug - 1)] = [after - 12, after - 22, after - 2, 0.0]  # the drive home: -20 %
    return rows


def history(first_day: int, last_day: int, **kwargs) -> dict[float, list]:
    rows: dict[float, list] = {}
    for day in range(first_day, last_day + 1):
        rows.update(statistics_day(day, **kwargs))
    return rows


def measured(first_day: int, last_day: int) -> list[tuple[float, float]]:
    return [(at(day, 17), at(day + 1, 7)) for day in range(first_day, last_day + 1)]


def test_connections_before_the_measured_ones_are_found_and_checked_against_the_measured_ones() -> None:
    result = derive_windows(history(0, 40), measured(20, 40))
    assert result["status"] == "ok", result
    validation = result["validation"]
    assert validation["passed"] and validation["recall"] == 1.0 and validation["precision"] == 1.0 and validation["contained"] == 1.0
    assert validation["median_end_error_minutes"] == 0.0 and validation["checked"] == 21
    windows = result["windows"]
    # the connections of the evenings of days 0..19: the last one ends on day 20 at 07:00, before the first measured one
    assert [w["start"] for w in windows] == [at(day, 17) for day in range(20)]
    assert all(w["end"] <= at(20, 17) for w in windows)
    assert windows[0]["end"] - windows[0]["start"] == 14 * HOUR and windows[0]["soc_in"] == 60.0 and windows[0]["soc_out"] == 80.0
    assert result["anchor"] == {"start": at(20, 17), "soc_in": 60.0}


def test_a_parked_period_of_days_is_cut_to_the_typical_length_around_the_observed_charging() -> None:
    rows = history(0, 40)
    # a weekend at home: parked and steady from Friday evening to Monday morning, charged only in the first night
    for hour in range(int(at(5, 17)), int(at(8, 7)), int(HOUR)):
        charging = at(6, 1) <= hour < at(6, 4)
        rows[float(hour)] = charge_row(int((hour - at(6, 1)) // HOUR), 3) if charging else [80.0 if hour >= at(6, 4) else 60.0] * 3 + [0.0]
    result = derive_windows(rows, measured(20, 40))
    assert result["status"] == "ok", result
    weekend = [w for w in result["windows"] if at(5, 17) <= w["start"] < at(8, 7)]
    # not the 62 hours the car stood there: from the first charging hour, as long as a typical connection (14 h)
    assert [(w["start"], w["end"]) for w in weekend] == [(at(6, 1), at(6, 15))]


def test_a_long_stay_with_charging_far_apart_is_several_connections_none_longer_than_the_long_typical_one() -> None:
    rows = history(0, 40)
    # the level sensor stood still for days (or the car stayed home) with charging in the first and the third night
    for hour in range(int(at(5, 17)), int(at(8, 7)), int(HOUR)):
        charging = at(6, 1) <= hour < at(6, 4) or at(8, 1) <= hour < at(8, 4)
        rows[float(hour)] = [60.0, 60.0, 60.0, 7000.0 if charging else 0.0]  # the sensor never moves
    windows = [w for w in derive_windows(rows, measured(20, 40))["windows"] if at(5, 17) <= w["start"] < at(8, 7)]
    assert [(w["start"], w["end"]) for w in windows] == [(at(6, 1), at(6, 15)), (at(7, 17), at(8, 7))]
    assert all(w["end"] - w["start"] <= 14 * HOUR for w in windows)


def test_charging_that_runs_into_the_departure_hour_does_not_keep_the_car_parked_through_the_day() -> None:
    rows = history(0, 40)
    # the car charged until it left at 07:30: the hour of the departure has charger power but the level fell below where the hour before ended
    rows[at(4, 7)] = [74.0, 72.0, 80.0, 4000.0]
    windows = {w["start"]: w for w in derive_windows(rows, measured(20, 40))["windows"]}
    assert windows[at(3, 17)]["end"] == at(4, 7)


def test_a_one_percent_tick_in_a_parked_hour_is_not_a_drive() -> None:
    rows = history(0, 40)
    rows[at(3, 20)] = [59.5, 59.0, 60.0, 0.0]  # a whole-percent sensor that ticked down once (standby drain)
    windows = {w["start"]: w for w in derive_windows(rows, measured(20, 40))["windows"]}
    assert at(3, 17) in windows and windows[at(3, 17)]["end"] == at(4, 7)


def test_an_estimate_that_claims_time_the_car_was_not_plugged_in_is_not_used() -> None:
    # half of the measured connections are short, so the typical length is short and the estimate starts at the first
    # charging hour: that is inside the long measured connections but outside the short ones
    short = [(at(day + 1, 3), at(day + 1, 6)) for day in range(31, 42)]
    result = derive_windows(history(0, 45), [*measured(20, 29), *short])
    assert result["validation"]["contained"] < 0.8 and result["validation"]["recall"] == 1.0
    assert result["status"] == "unavailable" and result["reason"] == "not_validated"


def test_an_hour_without_a_reading_or_with_an_impossible_one_ends_the_parked_period() -> None:
    rows = history(0, 40)
    del rows[at(3, 22)]
    rows[at(4, 22)] = [60.0, 60.0, 250.0, 0.0]
    starts = [w["start"] for w in derive_windows(rows, measured(20, 40))["windows"]]
    # the parked period only begins after the unknown hour: the evening before it is not part of the connection
    assert at(3, 17) not in starts and at(4, 17) not in starts
    assert at(3, 23) in starts and at(4, 23) in starts and at(2, 17) in starts and at(5, 17) in starts


def test_nothing_is_derived_without_statistics_or_with_too_few_measured_connections() -> None:
    assert derive_windows({}, measured(20, 40))["reason"] == "no_statistics"
    assert derive_windows(history(0, 40), measured(30, 35))["reason"] == "too_few_measured"
    assert derive_windows(history(0, 40), [])["status"] == "unavailable"


def test_an_estimate_that_does_not_match_the_measured_connections_is_not_used() -> None:
    # the measured connections are at other times than the statistics suggest (or the power is not the charger's)
    wrong = [(at(day, 9), at(day, 12)) for day in range(20, 41)]
    result = derive_windows(history(0, 40), wrong)
    assert result["status"] == "unavailable" and result["reason"] == "not_validated" and result["windows"] == []
    assert result["validation"]["passed"] is False
    # charging that is found in the measured period, but the departure is off by more than two hours
    shifted = [(at(day, 17), at(day + 1, 11)) for day in range(20, 41)]
    late = derive_windows(history(0, 40), shifted)
    assert late["status"] == "unavailable" and late["reason"] == "not_validated" and late["validation"]["median_end_error_minutes"] == 240.0
    # no charging in the measured period: nothing to check the method with
    quiet = history(0, 19)
    quiet.update(history(20, 40, charge=()))
    assert derive_windows(quiet, measured(20, 40))["status"] == "unavailable"


def test_no_older_connections_is_reported_not_invented() -> None:
    result = derive_windows(history(20, 40), measured(20, 40))
    assert result["status"] == "unavailable" and result["reason"] == "no_older_connections"


def test_hours_are_cleaned_and_the_typical_length_is_a_whole_number_of_hours() -> None:
    cleaned = clean_hours({0.0: [60, 60, 60, 0], 1800.0: [60, 60, 60, 0], HOUR: [60, 60, 60, None], 2 * HOUR: [60, 70, 65, 0]})
    assert cleaned == {0.0: (60.0, 60.0, 0.0)}
    assert typical_lengths([(0.0, 3600.0)] * 5) is None
    assert typical_lengths([(0.0, 14.5 * HOUR)] * 12) == (14 * HOUR, 15 * HOUR)


def test_the_net_use_between_two_connections_is_only_a_fall_of_the_level() -> None:
    assert used_between(80.0, 60.0) == pytest.approx(20.0)
    assert used_between(80.0, 80.0) is None and used_between(60.0, 80.0) is None
