"""What-if: connections from before CJP's own records, estimated from the recorder's long-term statistics.

Pure functions, no Home Assistant imports. The connection history CJP measures itself (plug-in to unplug from the
charger status) only reaches back as far as the integration has been running. The hourly statistics of the car's
battery level and the charger power reach back much further, so the older connections are *estimated* from them:

- an hour is "away" when the battery level moved without the charger delivering power, or started lower than the hour
  before ended (the car was driving);
- a run of consecutive hours that are not away and contain home charging is a parked period at the charger;
- the connection is that period, never longer than the long typical length of the connections CJP measured itself
  (parked is not the same as plugged in), anchored at the departure end and always including the observed charging;
  a period with charging far apart is several connections;
- the battery level at the start and end of a connection comes from the statistics, so the drive between two
  connections is the net battery use between them (public charging included, like the measured drives).

The estimate is only used after it has been checked against the connections that were measured: derived over the
measured period it must find most of the real connections, find few that did not exist, not claim much time the car
was not plugged in, and end them at about the right time. (The check shows that the method reproduces the known
connections; it does not prove the estimate of the unknown ones.) Without enough measured connections to check against, or without the statistics, nothing is derived.
No default is ever used for missing data: an hour without a battery level or a power reading ends a parked period.
"""
from __future__ import annotations

import datetime
import math
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

HOUR = 3600.0
CHARGING_WATT = 500.0  # an hour with at least this mean charger power is a charging hour
MOVED_PERCENT = 1.5  # a spread of the battery level within an hour, without charging, is driving (above a 1 % tick or standby drain)
MIN_CONTAINED = 0.8  # share of the time the estimate calls a connection that really was one, in the measured period
MIN_MEASURED = 10  # measured connections needed to check the estimate against (and to take the typical length from)
MIN_RECALL = 0.7  # share of the measured connections with home charging that the estimate finds
MIN_PRECISION = 0.7  # share of the estimated connections (in the measured period) that really existed
MAX_END_ERROR = 2 * HOUR  # median error of the estimated departure time
MIN_USED_PERCENT = 0.05  # a smaller net battery use between two connections counts as no use

REASONS = ("no_statistics", "too_few_measured", "not_validated", "no_older_connections")


@dataclass(frozen=True)
class DerivedWindow:
    start: float
    end: float
    soc_in: float  # the battery level when the connection began
    soc_out: float  # and when it ended

    def as_dict(self) -> dict[str, float]:
        return {"start": self.start, "end": self.end, "soc_in": round(self.soc_in, 1), "soc_out": round(self.soc_out, 1)}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def plan_day(plan: Mapping[str, Any], weekday: int) -> Mapping[str, Any] | None:
    """One weekday of the work plan {"days": {weekday: {"workday", "departure", "homecoming", "km"}}} when it is usable.

    departure and homecoming are seconds after local midnight; km is the distance needed that day.
    """
    day = (plan.get("days") or {}).get(weekday)
    if not isinstance(day, Mapping):
        return None
    km = _number(day.get("km"))
    if km is None or km < 0:
        return None
    if day.get("workday") and (_number(day.get("departure")) is None or _number(day.get("homecoming")) is None):
        return None
    return day


def local_time(date: datetime.date, seconds: float, tz: datetime.tzinfo) -> float:
    """The local time of day (seconds after midnight) on a date as a timestamp."""
    hours, rest = divmod(int(seconds), 3600)
    moment = datetime.datetime.combine(date, datetime.time(hours % 24, rest // 60), tzinfo=tz)
    return moment.timestamp()


def _bounded(start: float, end: float, plan: Mapping[str, Any] | None, tz: datetime.tzinfo | None) -> tuple[float, float]:
    """The connection cut to the work plan: not before the planned homecoming of its first day and not after the planned
    departure of its last one (only a workday has them). The plan can only shorten a connection, never lengthen it."""
    if plan is None or tz is None:
        return start, end
    arrival = plan_day(plan, datetime.datetime.fromtimestamp(start, tz).weekday())
    if arrival is not None and arrival.get("workday"):
        home = local_time(datetime.datetime.fromtimestamp(start, tz).date(), arrival["homecoming"], tz)
        if start < home < end:
            start = home
    leave = plan_day(plan, datetime.datetime.fromtimestamp(end, tz).weekday())
    if leave is not None and leave.get("workday"):
        planned = local_time(datetime.datetime.fromtimestamp(end, tz).date(), leave["departure"], tz)
        if start < planned < end:
            end = planned
    return start, end


def clean_hours(hours: Mapping[Any, Any]) -> dict[float, tuple[float, float, float]]:
    """{hour: (lowest level, highest level, mean charger power in W)} of the hours that have all three, in a valid range.

    The input rows are [mean level, lowest, highest, mean power] (the power can be missing). Hours with a missing or
    impossible value are dropped: an unknown hour must never look like a parked one.
    """
    clean: dict[float, tuple[float, float, float]] = {}
    for key, row in hours.items():
        try:
            ts = float(key)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(ts) or ts % HOUR or not isinstance(row, (list, tuple)) or len(row) < 4:
            continue
        low, high, power = _number(row[1]), _number(row[2]), _number(row[3])
        if low is None or high is None or power is None or not (0.0 <= low <= high <= 100.0) or power < 0.0:
            continue
        clean[ts] = (low, high, power)
    return clean


def typical_lengths(measured: Sequence[tuple[float, float]]) -> tuple[float, float] | None:
    """(short, long) connection length in seconds: the 25th and 75th percentile of the measured connections, in whole hours."""
    lengths = sorted(end - start for start, end in measured if end > start)
    if len(lengths) < MIN_MEASURED:
        return None
    low = lengths[int(0.25 * (len(lengths) - 1))]
    high = lengths[math.ceil(0.75 * (len(lengths) - 1))]
    return max(HOUR, math.floor(low / HOUR) * HOUR), max(HOUR, math.ceil(high / HOUR) * HOUR)


def derive_all(
    hours: Mapping[float, tuple[float, float, float]], lengths: tuple[float, float],
    plan: Mapping[str, Any] | None = None, tz: datetime.tzinfo | None = None,
) -> list[DerivedWindow]:
    """Every parked period with home charging in the statistics, as a connection (cut to the work plan when given)."""
    short, long = lengths
    windows: list[DerivedWindow] = []
    run: list[float] = []

    def close() -> None:
        if run and any(hours[h][2] >= CHARGING_WATT for h in run):
            windows.extend(_windows(run, hours, short, long, plan, tz))
        run.clear()

    previous: tuple[float, float] | None = None  # (hour, highest level) of the hour before
    for hour in sorted(hours):
        low, high, power = hours[hour]
        # away: the level moved with no charging, or it is lower than where the hour before ended (also in an hour that
        # was charging for a while: the car left in it)
        fell = previous is not None and previous[0] == hour - HOUR and previous[1] - low >= MOVED_PERCENT
        away = (power < CHARGING_WATT and high - low >= MOVED_PERCENT) or fell
        # the highest level of an hour is where it ended only when the car did not drive in it
        previous = None if away else (hour, high)
        if away or (run and hour != run[-1] + HOUR):
            close()
        if not away:
            run.append(hour)
    close()
    return windows


def _windows(
    run: list[float], hours: Mapping[float, tuple[float, float, float]], short: float, long: float,
    plan: Mapping[str, Any] | None = None, tz: datetime.tzinfo | None = None,
) -> list[DerivedWindow]:
    """The connections of one parked period: each contains the charging it covers and is never longer than `long`.

    A period with charging far apart (a long stay, or a battery level sensor that stood still for days) is several
    connections, never one long one: the length a smart charger could pick its hours from is bounded. With a work plan
    the connection is also cut to the planned homecoming and departure of its days, as long as it keeps the charging.
    """
    run_end = run[-1] + HOUR
    pending = [h for h in run if hours[h][2] >= CHARGING_WATT]
    windows: list[DerivedWindow] = []
    cursor = run[0]
    while pending:
        first = pending[0]
        # anchored at the departure: the short typical length, but never later than the first observed charging
        start = max(cursor, min(run_end - short, first))
        end = min(run_end, start + long)
        soc_in, soc_out = hours[start][0], hours[end - HOUR][1]
        charged = [h for h in pending if h < end]
        cut_start, cut_end = _bounded(start, end, plan, tz)
        if cut_start > charged[0] or cut_end < charged[-1] + HOUR:
            cut_start, cut_end = start, end  # the plan would cut observed charging off: the plan is not what happened that day
        windows.append(DerivedWindow(cut_start, cut_end, soc_in, soc_out))
        cursor = end
        pending = [h for h in pending if h >= end]
    return windows


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return min(a_end, b_end) - max(a_start, b_start)


def validate(
    derived: Sequence[DerivedWindow], measured: Sequence[tuple[float, float]], hours: Mapping[float, tuple[float, float, float]] | None
) -> dict[str, Any]:
    """The estimate against the measured connections it could be checked on.

    hours: the cleaned statistics. A measured connection can only be found from them when the charger delivered power in
    it; without statistics (a model of the work plan) every measured connection counts.
    """
    span_start, span_end = measured[0][0], max(end for _, end in measured)
    if hours is None:
        eligible = list(measured)
    else:
        charging = sorted(h for h, row in hours.items() if row[2] >= CHARGING_WATT)
        eligible = [(s, e) for s, e in measured if (i := bisect_right(charging, s - HOUR)) < len(charging) and charging[i] < e]
    matched, end_errors = 0, []
    for start, end in eligible:
        best = max(derived, key=lambda d: _overlap(start, end, d.start, d.end), default=None)
        if best is not None and _overlap(start, end, best.start, best.end) > 0:
            matched += 1
            end_errors.append(abs(best.end - end))
    inside = [d for d in derived if d.start >= span_start - HOUR and d.end <= span_end + HOUR]
    real = sum(1 for d in inside if any(_overlap(d.start, d.end, s, e) > 0 for s, e in measured))
    # the safety check: how much of the time the estimate calls a connection really was one (a window that is too
    # long, or put on the wrong hours, claims time the car was not plugged in)
    claimed = sum(d.end - d.start for d in inside)
    inside_measured = sum(max(_overlap(d.start, d.end, s, e), 0.0) for d in inside for s, e in measured)
    contained = inside_measured / claimed if claimed > 0 else 0.0
    end_errors.sort()
    median = end_errors[len(end_errors) // 2] if end_errors else None
    recall = matched / len(eligible) if eligible else 0.0
    precision = real / len(inside) if inside else 0.0
    passed = (
        len(eligible) >= MIN_MEASURED and recall >= MIN_RECALL and precision >= MIN_PRECISION and contained >= MIN_CONTAINED
        and median is not None and median <= MAX_END_ERROR
    )
    return {
        "checked": len(eligible), "found": matched, "estimated": len(inside), "real": real,
        "recall": round(recall, 3), "precision": round(precision, 3), "contained": round(contained, 3),
        "median_end_error_minutes": None if median is None else round(median / 60.0, 1),
        "passed": passed,
    }


def derive_windows(
    raw_hours: Mapping[Any, Any], measured: Sequence[tuple[float, float]],
    plan: Mapping[str, Any] | None = None, tz: datetime.tzinfo | None = None,
) -> dict[str, Any]:
    """The estimated connections older than the first measured one and how well the method matched the measured ones.

    raw_hours: {hour: [mean level, lowest, highest, mean charger power W]} from the long-term statistics.
    measured: (plug-in, unplug) of the completed connections CJP measured itself.
    plan, tz: the work plan (plan_day) and the time zone; the connections are then cut to the planned homecoming and
    departure of their days, when that also passes the check against the measured connections (else without the plan).
    Returns {"status": "ok" | "unavailable", "reason", "windows": [...], "anchor": {...} | None, "validation": {...} | None,
    "plan_bounds": bool, "statistics_from": first hour of the statistics | None}.
    """
    measured = sorted((float(s), float(e)) for s, e in measured if _number(s) is not None and _number(e) is not None and e > s)
    hours = clean_hours(raw_hours)
    statistics_from = min(hours) if hours else None
    bounds = False

    def result(status: str, reason: str | None = None, windows: Sequence[DerivedWindow] = (), anchor: dict | None = None, validation: dict | None = None):
        return {
            "status": status, "reason": reason, "windows": [w.as_dict() for w in windows], "anchor": anchor, "validation": validation,
            "plan_bounds": bounds, "statistics_from": statistics_from,
        }

    if not any(power >= CHARGING_WATT for _, _, power in hours.values()):
        return result("unavailable", "no_statistics")
    lengths = typical_lengths(measured)
    if lengths is None:
        return result("unavailable", "too_few_measured")
    everything = derive_all(hours, lengths)
    validation = validate(everything, measured, hours)
    if plan is not None and tz is not None:
        planned = derive_all(hours, lengths, plan, tz)
        planned_check = validate(planned, measured, hours)
        if planned_check["passed"]:
            everything, validation, bounds = planned, planned_check, True
    if not validation["passed"]:
        return result("unavailable", "not_validated", validation=validation)
    first = measured[0][0]
    older = [w for w in everything if w.end <= first]
    if not older:
        return result("unavailable", "no_older_connections", validation=validation)
    level = hours.get(math.floor(first / HOUR) * HOUR)
    anchor = {"start": first, "soc_in": round(level[0], 1)} if level else None
    return result("ok", None, older, anchor, validation)


def used_between(previous_out: float, next_in: float) -> float | None:
    """The net battery use between two connections; None when it is no use (the level did not fall)."""
    used = previous_out - next_in
    return used if used > MIN_USED_PERCENT else None
