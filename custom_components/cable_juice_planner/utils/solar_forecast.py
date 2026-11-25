"""Hourly solar forecast rows from the energy platform of Forecast.Solar and Helios Forecast.

The same file is in BattMind and Cable Juice Planner: change both together, with tests/test_solar_forecast.py.

An integration that gives the Energy dashboard of Home Assistant a solar forecast has an ``energy`` platform with
``async_get_solar_forecast(hass, config_entry_id)``, which returns ``{"wh_hours": {ISO timestamp: Wh}}`` or None.
What a timestamp means differs per integration (KEY_SEMANTICS), so only the integrations listed there are read:

* ``start``: the value is the energy of the period that starts at the key (Helios Forecast: whole UTC hours). A key
  covers [key, key + step), step being the smallest gap between two keys; a longer gap is not covered. An hour the
  source itself summed from only part of its points looks whole here and can't be told apart (it is too low).
* ``end``: the value is the energy of the period that ends at the key, (previous key, key] (Forecast.Solar: the
  sunrise, the whole hours and the sunset; the night is one period from the sunset to the next sunrise, as Home
  Assistant leaves out the 0 at midnight). The first key has no known start, so its value is not used. A period
  longer than the usual gap between the keys (a key is missing) is not covered when it has energy; one with 0 Wh (the
  night) is. A key that can't be read leaves the periods next to it unknown, so such data gives no rows at all.

Keys outside ``window`` (the async helper: a day back to 16 days ahead) are left out, as if the data began or ended
there.

The energy of a period is spread evenly over the time it covers and summed per clock hour of the given time zone: on
the day the clocks go back the hour that occurs twice gets both, the hour skipped when they go forward gets nothing.
Only an hour the source covers completely gets a row, so a missing hour never becomes 0: a value that is not a finite
number >= 0 (or two values for one moment) leaves its period uncovered, and an hour at either end of the forecast
that is covered only in part is left out. With more than one config entry (Forecast.Solar has one per roof plane) the
energy is summed, and only for the hours that every entry covers.

A row is ``{"period_start": <aware datetime of the local hour>, "pv_estimate": <kWh in that hour>}``, the shape of the
``detailedHourly`` rows of Solcast (kW averaged over a whole hour is the kWh of the hour).
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import math
from collections.abc import Iterable, Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import IntegrationNotFound, async_get_integration

_LOGGER = logging.getLogger(__name__)

# integration (the platform of the selected sensor) -> what a key of its wh_hours means
KEY_SEMANTICS: dict[str, str] = {"forecast_solar": "end", "helios_forecast": "start"}
# seconds for the answers of all config entries together (the caller may hold up other work meanwhile)
FETCH_TIMEOUT = 10
# the keys async_solar_forecast_rows reads, from now: longer than any plan (7 days) and any forecast seen (7 days)
READ_BACK = datetime.timedelta(days=1)
READ_AHEAD = datetime.timedelta(days=16)

# every UTC offset and every change of the clocks is a multiple of 15 minutes, so no slot holds two local hours
_SLOT = datetime.timedelta(minutes=15)
# more than a local hour can last (two hours when the clocks go back), so every slot of a covered hour is counted
_MARGIN = datetime.timedelta(hours=3)
_EPOCH = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)


def _instant(key: Any) -> datetime.datetime | None:
    """The moment of a key in UTC, None for a key that is no time or has no time zone."""
    if isinstance(key, datetime.datetime):
        moment = key
    elif isinstance(key, str):
        try:
            moment = datetime.datetime.fromisoformat(key)
        except ValueError:
            return None
    else:
        return None
    try:
        if moment.tzinfo is None or moment.utcoffset() is None:
            return None
        return moment.astimezone(datetime.UTC)
    except (OverflowError, ValueError):
        return None  # a moment that can't be shown in UTC (next to datetime.min or max)


def _energy(value: Any) -> float | None:
    """Wh as a float, None for anything that is not a finite number >= 0 (True is no number here)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


Period = tuple[datetime.datetime, datetime.datetime, float | None]
Window = tuple[datetime.datetime, datetime.datetime]


def _usual_gap(gaps: list[datetime.timedelta]) -> datetime.timedelta:
    """The most common gap between the keys (of two as common, the longer one)."""
    counts: dict[datetime.timedelta, int] = {}
    for gap in gaps:
        counts[gap] = counts.get(gap, 0) + 1
    return max(counts, key=lambda gap: (counts[gap], gap))


def _periods(wh_hours: Mapping[Any, Any], semantics: str, window: Window | None = None) -> list[Period]:
    """(start, end, Wh or None when the value can't be used) of every period of one config entry."""
    values: dict[datetime.datetime, list[Any]] = {}
    for key, value in wh_hours.items():
        moment = _instant(key)
        if moment is None:
            if semantics == "end":
                return []  # the period that ends there and the one after it are unknown
            continue
        if window is not None and not window[0] <= moment <= window[1]:
            continue  # as if the data began or ended at the window
        values.setdefault(moment, []).append(value)
    keys = sorted(values)

    def energy(moment: datetime.datetime) -> float | None:
        found = values[moment]
        return _energy(found[0]) if len(found) == 1 else None  # two values for one moment contradict each other

    if semantics == "start":
        gaps = [later - earlier for earlier, later in zip(keys, keys[1:])]
        if not gaps:
            return []  # one key: the length of its period is unknown
        step = min(gaps)
        return [(moment, moment + step, energy(moment)) for moment in keys]
    if not keys:
        return []
    usual = _usual_gap([later - earlier for earlier, later in zip(keys, keys[1:])] or [datetime.timedelta(0)])
    periods: list[Period] = []
    for begin, end in zip(keys, keys[1:]):
        wh = energy(end)
        if wh and end - begin > usual:
            wh = None  # a key is missing: the energy of this value covers only its own (unknown) part of the period
        periods.append((begin, end, wh))
    return periods


def _local_hours(periods: list[Period], tz: datetime.tzinfo) -> dict[datetime.datetime, float]:
    """Wh per local clock hour (naive), only for the hours the periods cover completely."""
    first = min(start for start, _, _ in periods) - _MARGIN
    last = max(end for _, end, _ in periods) + _MARGIN
    base = first - (first - _EPOCH) % _SLOT
    count = math.ceil((last - base) / _SLOT)
    covered = [0.0] * count
    energy = [0.0] * count
    unusable = [False] * count
    for start, end, wh in periods:
        length = (end - start).total_seconds()
        index = (start - base) // _SLOT
        while length > 0 and index < count:
            slot = base + index * _SLOT
            if slot >= end:
                break
            overlap = (min(end, slot + _SLOT) - max(start, slot)).total_seconds()
            if overlap > 0:
                if wh is None:
                    unusable[index] = True
                else:
                    covered[index] += overlap
                    energy[index] += wh * overlap / length
            index += 1
    full = _SLOT.total_seconds() - 1e-6
    hours: dict[datetime.datetime, list] = {}  # local hour -> [Wh, complete]
    for index in range(count):
        local = (base + index * _SLOT).astimezone(tz)
        item = hours.setdefault(local.replace(minute=0, second=0, microsecond=0, tzinfo=None), [0.0, True])
        item[0] += energy[index]
        if unusable[index] or covered[index] < full:
            item[1] = False
    return {hour: wh for hour, (wh, complete) in hours.items() if complete}


def rows_from_wh_hours(
    wh_hours_list: Iterable[Mapping[Any, Any] | None], semantics: str, tz: datetime.tzinfo, window: Window | None = None
) -> list[dict[str, Any]]:
    """The hourly rows of the wh_hours of one or more config entries (see the module docstring), oldest first.

    semantics: "start" or "end" (KEY_SEMANTICS); tz: the time zone of Home Assistant; window: (first, last) aware moment
    of the keys to read, None for every key (the work grows with the time between the first and the last key).
    Pure: no Home Assistant objects.
    """
    if semantics not in ("start", "end"):
        raise ValueError(f"Unknown key semantics {semantics!r}")
    total: dict[datetime.datetime, float] | None = None
    for wh_hours in wh_hours_list:
        periods = _periods(wh_hours, semantics, window) if isinstance(wh_hours, Mapping) else []
        try:
            hours = _local_hours(periods, tz) if periods else {}
        except OverflowError:
            hours = {}  # a key next to datetime.min or max
        total = hours if total is None else {hour: total[hour] + wh for hour, wh in hours.items() if hour in total}
        if not total:
            return []
    return [
        {"period_start": hour.replace(tzinfo=tz), "pv_estimate": round(wh / 1000.0, 4)}
        for hour, wh in sorted((total or {}).items())
    ]


async def _async_entry_ids(hass: HomeAssistant, domain: str, selected: str) -> tuple[list[str], str | None]:
    """(the config entries to sum, problem): the selected one plus the other entries of the integration that the Energy
    dashboard lists in a solar source together with it (Forecast.Solar: one entry per roof plane).

    An entry the user disabled is left out. Any other entry that is not loaded, or an Energy dashboard that can't be
    read, gives no entries: summing only some planes would give half a forecast.
    """
    listed = [selected]
    if "energy" in hass.config.components:
        try:
            from homeassistant.components.energy.data import async_get_manager  # noqa: PLC0415

            manager = await async_get_manager(hass)
            sources = (manager.data or {}).get("energy_sources") or []
            for source in sources:
                if not isinstance(source, Mapping) or source.get("type") != "solar":
                    continue
                ids = source.get("config_entry_solar_forecast")
                if isinstance(ids, list) and selected in ids:
                    listed += [entry_id for entry_id in ids if isinstance(entry_id, str) and entry_id not in listed]
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not read the solar forecasts of the Energy dashboard: %s", err)
            return [], "the Energy dashboard could not be read"
    entry_ids = []
    for entry_id in listed:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != domain or entry.disabled_by is not None:
            if entry_id == selected:
                return [], "the config entry of the sensor is missing or disabled"
            continue
        if entry.state is not ConfigEntryState.LOADED:
            return [], "a config entry of the forecast is not loaded"
        entry_ids.append(entry_id)
    return entry_ids, None


async def _async_forecast_function(hass: HomeAssistant, domain: str) -> Any:
    """async_get_solar_forecast of the integration's energy platform, None when it has none."""
    try:
        integration = await async_get_integration(hass, domain)
        platform = await integration.async_get_platform("energy")
    except (IntegrationNotFound, ImportError) as err:
        _LOGGER.debug("%s has no energy platform: %s", domain, err)
        return None
    function = getattr(platform, "async_get_solar_forecast", None)
    return function if callable(function) else None


async def async_solar_forecast_rows(
    hass: HomeAssistant, entity_id: str, tz: datetime.tzinfo, timeout: float = FETCH_TIMEOUT
) -> tuple[list[dict[str, Any]], str | None]:
    """(rows, problem) of the forecast integration of a sensor (any sensor of its config entry), on the event loop.

    problem is None when there are rows, else a short text for the log (no names or ids). The integration is the
    platform of the sensor in the entity registry and must be in KEY_SEMANTICS.
    """
    entry = er.async_get(hass).async_get(entity_id) if entity_id else None
    if entry is None or entry.platform not in KEY_SEMANTICS:
        return [], "the sensor is not from an integration with a known solar forecast"
    if not entry.config_entry_id:
        return [], "the sensor has no config entry"
    domain = entry.platform
    entry_ids, problem = await _async_entry_ids(hass, domain, entry.config_entry_id)
    if problem:
        return [], problem
    fetch = await _async_forecast_function(hass, domain)
    if fetch is None:
        return [], "the integration has no solar forecast for the Energy dashboard"
    forecasts = []
    try:
        async with asyncio.timeout(timeout):
            for entry_id in entry_ids:
                answer = await fetch(hass, entry_id)
                wh_hours = answer.get("wh_hours") if isinstance(answer, Mapping) else None
                if not isinstance(wh_hours, Mapping):
                    return [], "a config entry has no forecast"
                forecasts.append(wh_hours)
    except TimeoutError:
        return [], f"no answer within {timeout:g} seconds"
    except Exception as err:  # noqa: BLE001 - the code of another integration
        _LOGGER.debug("The solar forecast of %s failed", domain, exc_info=True)
        return [], f"the forecast failed ({type(err).__name__})"
    now = datetime.datetime.now(datetime.UTC)
    try:
        rows = rows_from_wh_hours(forecasts, KEY_SEMANTICS[domain], tz, (now - READ_BACK, now + READ_AHEAD))
    except Exception as err:  # noqa: BLE001 - a plan must never fail on the data of another integration
        _LOGGER.debug("The solar forecast of %s could not be read", domain, exc_info=True)
        return [], f"the forecast could not be read ({type(err).__name__})"
    return rows, None if rows else "the forecast covers no whole hour"
