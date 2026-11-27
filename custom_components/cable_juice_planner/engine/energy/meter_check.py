"""Does the house consumption sensor include this car's charger? Judged from the stored hourly power values.

The planner subtracts the charger from the house consumption (engine/energy/power.py) unless the option
charger_in_house_meter says the charger is on its own meter. When the sensor does not include the charger but the option
says it does, the charger is subtracted from a house that never had it: the house consumption becomes far too low (and
is cut at 0), so the plan sees solar surplus and cheap house hours that are not there. The repair
charger_outside_house_meter (management/health_checks.py) tells the user; this module only gives the verdict.

A house consumption sensor that includes the charger can never read less than the charger: in an hour where the charger
drew at least METER_MIN_EV_W on average, the house (after the home battery corrections, before anything is left out)
must be at least that much too. An hour where it is under METER_SHARE of the charger is a sign that the charger is not
in it; an hour where it is at least METER_OK_SHARE of the charger fits a sensor that includes it, and an hour between
the two is unclear (a large house load can come close to the charger) and points neither way. Hours with solar
production or a charging or discharging home battery are not judged: a sensor that really measures the grid (not the
consumption), or a wrong battery setting, would read low there, which is another problem. Nor are hours where the house
reads 0 (a failed reading, not a measurement).
"""
from __future__ import annotations

import datetime
import math
from collections.abc import Mapping
from typing import Any

# the stored hours that are judged: the last week
METER_CHECK_DAYS = 7
# an hour is judged only when the charger drew at least this much on average (W)
METER_MIN_EV_W = 1000.0
# ...and solar and the home battery's charging and discharging stayed under this (W)
METER_MAX_LOCAL_W = 100.0
# a house under this share of the charger is a sign that the charger is not in the house consumption
METER_SHARE = 0.5
# a house of at least this share of the charger fits a sensor that includes it
METER_OK_SHARE = 0.9
# this many hours under METER_SHARE in METER_CHECK_DAYS raise the repair
METER_FLAG_HOURS = 3
# this many hours of at least METER_OK_SHARE, and no hour under it, remove it (otherwise not judged: the repair stays)
METER_OK_HOURS = 3

_KEYS = (
    "power_consumption", "ev_used_consumption", "solar_production",
    "powerwall_charging_consumption", "powerwall_discharging_consumption",
)


def _hour(stamp: Any) -> datetime.datetime | None:
    if not isinstance(stamp, datetime.datetime):
        return None
    return stamp.replace(minute=0, second=0, microsecond=0)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _rows(database: Mapping[Any, Any], since: datetime.datetime, now: datetime.datetime) -> list[dict[str, float]]:
    """The stored hours since `since` with a number for every key in _KEYS, as {key: W}."""
    rows: list[dict[str, float]] = []
    for data in database.values():
        if not isinstance(data, Mapping):
            continue
        hours: dict[datetime.datetime, dict[str, float]] = {}
        for key in _KEYS:
            for item in data.get(key) or []:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    continue
                hour, value = _hour(item[0]), _number(item[1])
                if hour is None or value is None:
                    continue
                try:
                    if not since <= hour <= now:
                        continue
                except TypeError:  # a naive stamp next to an aware one: not comparable, not judged
                    continue
                hours.setdefault(hour, {})[key] = value
        rows.extend(values for values in hours.values() if len(values) == len(_KEYS))
    return rows


def charger_meter_verdict(database: Mapping[Any, Any], now: datetime.datetime) -> dict[str, Any]:
    """{"verdict": True (the charger looks not to be in the house consumption), False (it is) or None (not judged),
    "hours": the hours that point to it, "judged": the hours judged, "ok": the hours that fit a sensor with the charger,
    "days": METER_CHECK_DAYS, "share": METER_SHARE in percent (the repair's text)}.

    database: power_values_db (hour of day -> key -> [[time, W], ...], newest first)."""
    since = now - datetime.timedelta(days=METER_CHECK_DAYS)
    judged = flagged = ok = 0
    for row in _rows(database, since, now):
        ev = abs(row["ev_used_consumption"])
        if ev < METER_MIN_EV_W or row["power_consumption"] <= 0:
            continue
        if any(row[key] >= METER_MAX_LOCAL_W for key in ("solar_production", "powerwall_charging_consumption", "powerwall_discharging_consumption")):
            continue
        judged += 1
        if row["power_consumption"] < METER_SHARE * ev:
            flagged += 1
        elif row["power_consumption"] >= METER_OK_SHARE * ev:
            ok += 1
    if flagged >= METER_FLAG_HOURS:
        verdict: bool | None = True
    elif flagged == 0 and judged == ok and ok >= METER_OK_HOURS:
        verdict = False
    else:
        verdict = None
    return {"verdict": verdict, "hours": flagged, "judged": judged, "ok": ok, "days": METER_CHECK_DAYS, "share": round(METER_SHARE * 100)}
