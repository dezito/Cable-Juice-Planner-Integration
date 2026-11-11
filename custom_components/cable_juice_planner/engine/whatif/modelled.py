"""What-if: connections modelled from the work plan, for the time before CJP and the statistics have any record.

Pure functions, no Home Assistant imports. The work plan holds, per weekday, whether it is a workday, the departure and
the homecoming time and the distance needed. For a period nothing else records (no measured connection, no battery
level statistics) the plan is the only description of when the car was home, so:

- on a workday the car comes home at the homecoming time and stays until the next workday's departure; a stay that is
  longer than the long typical length of the measured connections is cut at its departure end (parked at home is not
  the same as plugged in);
- between two connections the battery use is the distance of the days in the gap (a workday's distance when it leaves
  in the gap, the weekday's distance for a day off that lies in it) times the battery percent per km that the recorded
  drives show.

This is a MODEL of what the plan says today, not a record of what happened: the plan is the plan as it is set now. It is
only used after the same model, run over the period CJP measured, has found the measured connections (the checks of
derive.validate) and its battery use has come out within a factor of the recorded drives. Without enough recorded drives
to know the percent per km, or with fewer than 10 measured connections, nothing is modelled. Nothing is defaulted.
"""
from __future__ import annotations

import datetime
import math
from collections.abc import Mapping, Sequence
from statistics import median
from typing import Any

from .derive import MIN_MEASURED, DerivedWindow, local_time as _local, plan_day as _plan_day, typical_lengths, validate

HOUR = 3600.0
MIN_DRIVES = 10  # recorded drives needed to know the battery percent per km
USE_RATIO = (0.6, 1.6)  # modelled battery use over recorded battery use in the measured period
MIN_USED_PERCENT = 0.05


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def percent_per_km(drives: Sequence[Mapping[str, Any]]) -> float | None:
    """The median battery percent used per km of the recorded drives; None without MIN_DRIVES usable drives."""
    values = [
        d["used"] / d["km"] for d in drives
        if isinstance(d, Mapping) and _number(d.get("km")) and _number(d.get("used")) and d["km"] > 0 and d["used"] > 0
    ]
    return median(values) if len(values) >= MIN_DRIVES else None


def model_windows(
    plan: Mapping[str, Any], tz: datetime.tzinfo, start: float, end: float, long: float
) -> list[tuple[float, float]]:
    """The modelled connections that lie completely inside [start, end)."""
    first = datetime.datetime.fromtimestamp(start, tz).date() - datetime.timedelta(days=1)
    last = datetime.datetime.fromtimestamp(end, tz).date() + datetime.timedelta(days=1)
    windows: list[tuple[float, float]] = []
    date = first
    while date <= last:
        day = _plan_day(plan, date.weekday())
        if day is not None and day.get("workday"):
            nxt, found = date + datetime.timedelta(days=1), None
            for _ in range(7):
                other = _plan_day(plan, nxt.weekday())
                if other is not None and other.get("workday"):
                    found = _local(nxt, other["departure"], tz)
                    break
                nxt += datetime.timedelta(days=1)
            home = _local(date, day["homecoming"], tz)
            if found is not None and found > home:
                begin = max(home, found - long)
                if begin >= start and found <= end and (not windows or begin >= windows[-1][1]):
                    windows.append((begin, found))
        date += datetime.timedelta(days=1)
    return windows


def gap_use(plan: Mapping[str, Any], tz: datetime.tzinfo, begin: float, end: float, per_km: float) -> float:
    """The battery percent used between two connections: the distance of the days in the gap times the percent per km."""
    km = 0.0
    date = datetime.datetime.fromtimestamp(begin, tz).date() - datetime.timedelta(days=1)
    last = datetime.datetime.fromtimestamp(end, tz).date() + datetime.timedelta(days=1)
    while date <= last:
        day = _plan_day(plan, date.weekday())
        if day is not None:
            # a workday counts when the car leaves in the gap, a day off when its noon is in it
            moment = _local(date, day["departure"], tz) if day.get("workday") else _local(date, 12 * 3600, tz)
            if begin <= moment < end:
                km += float(day["km"])
        date += datetime.timedelta(days=1)
    return km * per_km


def _gap_drives(
    plan: Mapping[str, Any], tz: datetime.tzinfo, windows: Sequence[tuple[float, float]], next_start: float | None, per_km: float
) -> list[tuple[float, float]]:
    drives = []
    for index, (_, end) in enumerate(windows):
        nxt = windows[index + 1][0] if index + 1 < len(windows) else next_start
        if nxt is None or nxt <= end:
            continue
        used = gap_use(plan, tz, end, nxt, per_km)
        if used > MIN_USED_PERCENT:
            drives.append(((end + nxt) / 2.0, min(used, 100.0)))
    return drives


def model_connections(
    plan: Mapping[str, Any] | None,
    tz: datetime.tzinfo | None,
    measured: Sequence[tuple[float, float]],
    drives: Sequence[Mapping[str, Any]],
    period_start: float,
    region_end: float,
    next_start: float | None,
) -> dict[str, Any]:
    """The modelled connections in [period_start, region_end) and the battery use between them, checked on the measured period.

    next_start: the start of the first connection after the region (the use in the gap before it is modelled too).
    Returns {"status": "ok" | "unavailable", "reason", "windows": [(start, end)], "drives": [(time, percent)], "validation": {...}}.
    """

    def result(status: str, reason: str | None = None, windows=(), gap_drives=(), validation=None, per_km=None):
        return {
            "status": status, "reason": reason, "windows": [list(w) for w in windows], "drives": [list(d) for d in gap_drives],
            "validation": validation, "percent_per_km": per_km,
        }

    if not isinstance(plan, Mapping) or tz is None:
        return result("unavailable", "no_plan")
    if not any(_plan_day(plan, w) is not None and (plan.get("days") or {}).get(w, {}).get("workday") for w in range(7)):
        return result("unavailable", "no_workdays")
    measured = sorted((float(s), float(e)) for s, e in measured if _number(s) is not None and _number(e) is not None and e > s)
    lengths = typical_lengths(measured)
    if lengths is None:
        return result("unavailable", "too_few_measured")
    per_km = percent_per_km(drives)
    if per_km is None:
        return result("unavailable", "too_few_drives")
    long = lengths[1]
    # the check: the same model over the measured period
    span = (measured[0][0], max(e for _, e in measured))
    over = model_windows(plan, tz, span[0] - 2 * 86400.0, span[1], long)
    validation = validate([DerivedWindow(s, e, 0.0, 0.0) for s, e in over], measured, None)
    # the battery use is compared in the gaps between the MODELLED connections (the ones a replay would use), against the
    # recorded drives in those same gaps: a weekend the model cuts to the long length has days off in its gap, which a
    # comparison in the gaps between the measured connections would never see
    in_gaps = [(e, over[i + 1][0]) for i, (_, e) in enumerate(over[:-1]) if over[i + 1][0] > e]
    modelled_use = sum(gap_use(plan, tz, a, b, per_km) for a, b in in_gaps)
    recorded_use = sum(
        float(d["used"]) for d in drives if isinstance(d, Mapping) and _number(d.get("start")) is not None and _number(d.get("used")) is not None
        and any(a - 900.0 <= d["start"] < b for a, b in in_gaps)
    )
    ratio = modelled_use / recorded_use if recorded_use > 0 else None
    validation = {**validation, "use_ratio": None if ratio is None else round(ratio, 3)}
    if not validation["passed"] or ratio is None or not USE_RATIO[0] <= ratio <= USE_RATIO[1]:
        return result("unavailable", "not_validated", validation=validation, per_km=per_km)
    # the model's battery use is brought to the recorded one over the measured period (it can neither be more nor less
    # than the drives show per km that the plan's distances account for)
    per_km = per_km / ratio
    windows = model_windows(plan, tz, period_start, region_end, long)
    if not windows:
        return result("unavailable", "no_modelled_connections", validation=validation, per_km=per_km)
    return result("ok", None, windows, _gap_drives(plan, tz, windows, next_start, per_km), validation, per_km)


__all__ = ("MIN_MEASURED", "model_connections", "model_windows", "gap_use", "percent_per_km")
