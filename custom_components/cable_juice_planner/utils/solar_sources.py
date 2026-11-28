"""The solar forecast sources: which there are, which are confirmed with real data, and the facts about what the chosen
one delivers. (The same file in both repositories.)"""
from __future__ import annotations

import datetime
import math
from typing import Any

from .helpers import to_local_datetime
from .solar_forecast import KEY_SEMANTICS
from .solcast import SOLCAST_PLATFORM

NO_SOLAR_SOURCE = "none"  # the choice of the flow without a solar forecast; never stored
# the integrations the forecast sensor can be from: the platform of the sensor says which one it is
SOLAR_SOURCES = (SOLCAST_PLATFORM, *KEY_SEMANTICS)
SOLAR_SOURCE_NAMES = {"solcast_solar": "Solcast PV Forecast", "forecast_solar": "Forecast.Solar", "helios_forecast": "Helios Forecast"}
# confirmed only by hand: a source is added when it has been seen working with real data
CONFIRMED_SOLAR_SOURCES = frozenset({SOLCAST_PLATFORM})
# the days listed at most, today first: more than any forecast gives, so a stray far date cannot make an endless list
_MAX_DAYS = 16


def solar_days(rows_by_entity: dict | None, today: datetime.date, tz: datetime.tzinfo | None) -> list[dict[str, Any]]:
    """[{"date", "hours", "kwh"}] from today to the last day with a value, of the forecast's hourly rows (the plan reads the
    same rows, but uses only the hours it has a sell price for).

    hours: the clock hours of the day with a usable value (a finite number >= 0; of two rows for one hour the first, as the
    plan does); kwh: their sum, None for a day without any (never 0). It states facts and judges nothing: a forecast may
    give no value for a dark or partly dark hour, which can't be told from a missing one.
    """
    values: dict[datetime.date, dict[int, float]] = {}
    for rows in (rows_by_entity or {}).values():
        for row in rows or []:
            try:
                moment = to_local_datetime(row["period_start"], tz)
                value = row["pv_estimate"]
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            if not isinstance(moment, datetime.datetime) or isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if math.isfinite(value) and value >= 0:
                values.setdefault(moment.date(), {}).setdefault(moment.hour, float(value))
    last = max((day for day in values if today <= day < today + datetime.timedelta(days=_MAX_DAYS)), default=None)
    if last is None:
        return []
    days = (today + datetime.timedelta(days=offset) for offset in range((last - today).days + 1))
    return [
        {"date": day.isoformat(), "hours": len(values.get(day, {})), "kwh": round(sum(values[day].values()), 1) if values.get(day) else None}
        for day in days
    ]
