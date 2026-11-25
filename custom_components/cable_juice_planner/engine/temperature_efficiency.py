"""Driving efficiency at the forecast outdoor temperature (switch temperature_efficiency).

The learned km/kWh stays the base: the newest drives in km_kwh_efficiency_db and the forecast type (EMA, trend or
average) over them. With the switch on, each of those drives is moved to the forecast temperature of the planned drive
along the car's own line km/kWh = a + b * °C, and the same forecast type runs over the moved values. The line is fitted
on the analysis drives (analysis_db["drives"]: unplugging to plugging in, with the mean outdoor temperature while the car
was away), weighted by distance like the line shown under Analysis.

Moving each drive from its own temperature (not the forecast against one mean temperature) keeps the cold from being
counted twice: an EMA of the last cold drives already holds the cold, and moving them to an equally cold day changes
nothing.

The result is never above the learned km/kWh (the cold only ever adds energy to the plan, a warm day keeps the learned
value) and never below MIN_FACTOR of it. Anything missing keeps the learned km/kWh, which is the plan without the switch:
too few drives, too narrow a temperature range, a line that does not fall with the cold, no forecast for the drive, or
too few learned drives with a temperature.
"""
from __future__ import annotations

import datetime
import math

from dateutil import parser as dateutil_parser

MIN_DRIVES = 12  # analysis drives with a temperature before the line is used (the Analysis line shows from 5)
MIN_SPAN = 8.0  # °C between the coldest and the warmest of them
EXTRAPOLATE = 5.0  # °C the line is used beyond the coldest and the warmest drive; further out it stays flat
MIN_FACTOR = 0.5  # the adjusted km/kWh is never below this share of the learned one
MATCH_SECONDS = 600  # a learned km/kWh drive and an analysis drive are the same drive when they ended this close
MIN_MATCHED_SHARE = 0.5  # share of the learned drives that need a temperature before they are moved
DRIVE_HOURS = (6, 22)  # a day's spread driving (fill up, optimal weekly charging): the line is fitted on drives by day
FORECAST_STEP_HOURS = 3  # an hourly forecast row counts for the hours until the next row, at most this many

REASON_TOO_FEW_DRIVES = "too_few_drives"
REASON_TOO_NARROW = "too_narrow"
REASON_NO_COLD_EFFECT = "no_cold_effect"
REASON_IMPLAUSIBLE = "implausible"
REASON_NO_FORECAST = "no_forecast"
REASON_TOO_FEW_MATCHED = "too_few_matched"
REASON_NO_LEARNED = "no_learned"
REASON_FAILED = "failed"  # an unexpected error: logged, and the learned km/kWh is used


def _finite(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def drive_points(drives: list, battery_size: float) -> list[dict]:
    """The analysis drives with a temperature: end (epoch s), temp (°C), km and km/kWh from the battery size."""
    points = []
    size = _finite(battery_size)
    if not size or size <= 0:
        return points
    for drive in drives or []:
        if not isinstance(drive, dict):
            continue
        end, temp, km, used = (_finite(drive.get(key)) for key in ("end", "temp", "km", "used"))
        if None in (end, temp, km, used) or km <= 0 or used <= 0:
            continue
        points.append({"end": end, "temp": temp, "km": km, "km_per_kwh": km / (used / 100 * size)})
    return points


def fit_curve(points: list[dict]) -> tuple[dict | None, str | None]:
    """km/kWh = a + b * °C weighted by distance, with the gates for the plan. (curve, None) or (None, reason)."""
    if len(points) < MIN_DRIVES:
        return None, REASON_TOO_FEW_DRIVES
    temps = [p["temp"] for p in points]
    low, high = min(temps), max(temps)
    if high - low < MIN_SPAN:
        return None, REASON_TOO_NARROW
    w = [p["km"] for p in points]
    sw = sum(w)
    mx = sum(wi * p["temp"] for wi, p in zip(w, points)) / sw
    my = sum(wi * p["km_per_kwh"] for wi, p in zip(w, points)) / sw
    sxx = sum(wi * (p["temp"] - mx) ** 2 for wi, p in zip(w, points))
    if sxx <= 0:
        return None, REASON_TOO_NARROW
    b = sum(wi * (p["temp"] - mx) * (p["km_per_kwh"] - my) for wi, p in zip(w, points)) / sxx
    a = my - b * mx
    if b <= 0:
        return None, REASON_NO_COLD_EFFECT
    if a + b * (low - EXTRAPOLATE) <= 0:
        return None, REASON_IMPLAUSIBLE
    return {"a": a, "b": b, "low": low, "high": high, "drives": len(points)}, None


def curve_value(curve: dict, temp: float) -> float:
    """The line at temp, flat beyond EXTRAPOLATE °C outside the measured temperatures."""
    temp = min(max(temp, curve["low"] - EXTRAPOLATE), curve["high"] + EXTRAPOLATE)
    return curve["a"] + curve["b"] * temp


def local_naive(value, tz: datetime.tzinfo) -> datetime.datetime | None:
    """A forecast or database time as naive local wall clock time in tz (a time with an offset is converted, a naive one is
    already local), None when it is no time."""
    if isinstance(value, str):
        try:
            value = dateutil_parser.isoparse(value)
        except (ValueError, OverflowError):
            return None
    if not isinstance(value, datetime.datetime):
        return None
    if value.tzinfo is not None:
        value = value.astimezone(tz).replace(tzinfo=None)
    return value


def entry_time(item, tz: datetime.tzinfo) -> float | None:
    """Epoch seconds of a km_kwh_efficiency_db entry [time, km/kWh]; None for an old entry without a time."""
    if not isinstance(item, (list, tuple)) or len(item) < 2:
        return None
    moment = item[0]
    if (number := _finite(moment)) is not None:
        return number
    moment = local_naive(moment, tz)
    if moment is None:
        return None
    return moment.replace(tzinfo=tz).timestamp()


def entry_value(item) -> float | None:
    """The km/kWh of a database entry, read like get_list_values (floats only)."""
    if isinstance(item, float):
        return item
    if isinstance(item, (list, tuple)) and len(item) >= 2 and isinstance(item[1], float):
        return item[1]
    return None


def entry_temperatures(entries: list, points: list[dict], tz: datetime.tzinfo) -> list[float | None]:
    """The outdoor temperature of each learned drive: the analysis drive that ended within MATCH_SECONDS of it (both are
    recorded when the car is plugged in again), else None."""
    result = []
    for item in entries or []:
        moment = entry_time(item, tz)
        temp = None
        if moment is not None:
            best = None
            for point in points:
                gap = abs(point["end"] - moment)
                if gap <= MATCH_SECONDS and (best is None or gap < best[0]):
                    best = (gap, point["temp"])
            temp = best[1] if best else None
        result.append(temp)
    return result


def moved_entries(entries: list, temps: list[float | None], curve: dict, target: float) -> list | None:
    """The learned drives moved to target °C along the line (same order and shape, so get_forecast_value reads them).
    A drive without a temperature keeps its value. None when fewer than MIN_MATCHED_SHARE of them have a temperature."""
    values = [entry_value(item) for item in entries or []]
    counted = [value for value in values if value is not None]
    if not counted:
        return None
    matched = sum(1 for value, temp in zip(values, temps) if value is not None and temp is not None)
    if matched < MIN_MATCHED_SHARE * len(counted):
        return None
    at_target = curve_value(curve, target)
    moved = []
    for item, value, temp in zip(entries, values, temps):
        if value is None:
            moved.append(item)
            continue
        if temp is not None:
            at_drive = curve_value(curve, temp)
            value = float(value * at_target / at_drive) if at_drive > 0 else value
        moved.append([item[0], value] if isinstance(item, (list, tuple)) else value)
    return moved


def forecast_temperatures(forecast_dict: dict | None, tz: datetime.tzinfo) -> tuple[dict, dict]:
    """The temperatures of weather.get_forecasts: {local hour: °C} (hourly) and {local date: (low, high)} (daily; the daily
    `temperature` is the day's high, `templow` its low)."""
    hourly: dict[datetime.datetime, float] = {}
    daily: dict[datetime.date, tuple[float | None, float | None]] = {}
    forecast_dict = forecast_dict if isinstance(forecast_dict, dict) else {}
    for row in forecast_dict.get("hourly") or []:
        if not isinstance(row, dict):
            continue
        moment, temp = local_naive(row.get("datetime"), tz), _finite(row.get("temperature"))
        if moment is not None and temp is not None:
            hourly.setdefault(moment.replace(minute=0, second=0, microsecond=0), temp)
    for row in forecast_dict.get("daily") or []:
        if not isinstance(row, dict):
            continue
        moment = local_naive(row.get("datetime"), tz)
        low, high = _finite(row.get("templow")), _finite(row.get("temperature"))
        if moment is not None and (low is not None or high is not None):
            daily.setdefault(moment.date(), (low, high))
    return hourly, daily


def window_temperature(hourly: dict, daily: dict, start: datetime.datetime, end: datetime.datetime | None) -> float | None:
    """The forecast temperature of a drive from start to end (naive local): the mean of the hourly forecast over its hours
    when at least half of them have one, else the middle of the day's low and high, else the day's low. A day with only
    its high is no forecast (the high is warmer than most of the day).

    A row of the hourly forecast counts for the hours until the next row, at most FORECAST_STEP_HOURS (forecasts in 3-hour
    steps). An end at or before the start on the same clock is the next day (a night shift: away 22:00, home 06:00); no
    end is the hour of the start."""
    first = start.replace(minute=0, second=0, microsecond=0)
    if end is None:
        end = first + datetime.timedelta(hours=1)
    while end <= start:
        end += datetime.timedelta(days=1)
    hours = []
    hour = first
    while hour < end:
        hours.append(hour)
        hour += datetime.timedelta(hours=1)
    values = [value for value in (_hour_value(hourly, hour) for hour in hours) if value is not None]
    if values and len(values) * 2 >= len(hours):
        return sum(values) / len(values)
    low, high = daily.get(start.date(), (None, None))
    if low is not None and high is not None:
        return (low + high) / 2
    return low


def _hour_value(hourly: dict, hour: datetime.datetime) -> float | None:
    """The hourly forecast of an hour: its own row, else the latest row up to FORECAST_STEP_HOURS - 1 hours before it."""
    for back in range(FORECAST_STEP_HOURS):
        value = hourly.get(hour - datetime.timedelta(hours=back))
        if value is not None:
            return value
    return None


def day_drive_window(start_of_day: datetime.datetime, now: datetime.datetime) -> tuple[datetime.datetime, datetime.datetime]:
    """(start, end) of a day's spread driving: DRIVE_HOURS of the day, today from now; after them today the whole of
    DRIVE_HOURS (only the temperature of the window is read, and the night would make it colder than the driving)."""
    day = start_of_day.replace(hour=0, minute=0, second=0, microsecond=0)
    first, last = day.replace(hour=DRIVE_HOURS[0]), day.replace(hour=DRIVE_HOURS[1])
    start = max(first, now) if isinstance(now, datetime.datetime) else first
    return (start if start < last else first), last
