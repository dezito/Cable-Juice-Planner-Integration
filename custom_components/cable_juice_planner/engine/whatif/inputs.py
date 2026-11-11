"""Build the replay input from CJP's stored data (pure: plain dicts in, no Home Assistant).

Everything is converted to UTC epoch seconds first. The sources and their quality:

- connection windows: the economy database (plug-in to unplug at home), measured;
- prices: the economy database's hourly buy prices, measured by CJP. Hours stored before
  `prices_averaged_since` were written before quarter-hour prices were averaged and may be
  the last quarter of the hour: they are labelled "legacy_unverified", never silently trusted.
  An hour that CJP copied from the first of two identical local hours (the autumn clock
  change) is flagged too;
- drives: the analysis database. A stored drive is the battery level when the car left minus the
  level at the next plug-in, so it is NET of any public charging in between and already includes
  it: a public charge is therefore not added to the battery level again. Only accepted drives are
  stored (long and large enough), so small trips and standby are missing and the demand is
  understated for both calculations;
- the starting SOC must be given (a recorded statistic or an explicit input): it is never
  defaulted. It is the SOC at the START OF THE FIRST REPLAYED WINDOW (the first plug-in), so the
  caller reads the statistic at that moment, not at the start of the period.

Nothing is filled in. What is missing is counted in the coverage so the result can say it.
"""
from __future__ import annotations

import datetime
import math
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .replay import GAP_TOLERANCE, Event, LossFactor, PowerCap, ReplayInput, Window

HOUR = 3600.0
MANY_WITHOUT_DRIVE = 0.5  # share of the gaps between connections with no recorded drive that earns a warning


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def loss_factor_from_table(points: Mapping[float, float], default: float | None = None) -> LossFactor:
    """A loss factor per kW from a few known points: the nearest kW wins (the lower one on a tie).

    Points that are not finite and non-negative are ignored. With no usable point and no default
    the factor is NaN, which the replay reports as an invalid loss model rather than charging for free.
    """
    usable = sorted((float(kw), float(f)) for kw, f in points.items() if _finite(kw) and _finite(f) and f >= 0.0)

    def factor(kw: float) -> float:
        if not usable:
            return float(default) if default is not None and _finite(default) and default >= 0.0 else math.nan
        return min(usable, key=lambda point: (abs(point[0] - kw), point[0]))[1]

    return factor


def power_cap_from_model(model: Mapping[str, Any] | None, planning_power_cap: Callable[..., float]) -> PowerCap:
    """The learned SOC power model (engine/soc_power.py) as a replay power cap; no model = no cap."""

    def cap(phases: int, soc_start: float, soc_end: float, base_kw: float) -> float:
        if not model:
            return base_kw
        return planning_power_cap(dict(model), phases, soc_start, soc_end, base_kw)

    return cap


def select_windows(
    windows: Iterable[Any], period_start: float, period_end: float, oldest_drive: float | None = None
) -> tuple[list[Window], dict[str, int]]:
    """The completed connections of the period that can be replayed, and what was left out.

    oldest_drive: when the drive history is full, older drives were dropped, so connections that ended
    before the oldest kept drive have an unknown demand and are not replayed.
    """
    chosen: list[Window] = []
    counts = {"outside": 0, "open": 0, "invalid": 0, "before_drive_history": 0}
    for window in sorted((w for w in windows if isinstance(w, Mapping)), key=lambda w: w.get("start") if _finite(w.get("start")) else 0.0):
        start, end = window.get("start"), window.get("end")
        if not _finite(start):
            counts["invalid"] += 1
        elif end is None:
            counts["open"] += 1  # still plugged in: not a complete window
        elif not _finite(end) or end <= start:
            counts["invalid"] += 1
        elif start < period_start or end > period_end:
            counts["outside"] += 1
        elif oldest_drive is not None and end < oldest_drive - GAP_TOLERANCE:
            counts["before_drive_history"] += 1
        else:
            chosen.append(Window(float(start), float(end)))
    return chosen, counts


def build_replay_input(
    *,
    windows: Iterable[Mapping[str, Any]],
    prices: Mapping[Any, Any],
    drives: Iterable[Mapping[str, Any]],
    public_sessions: Iterable[Mapping[str, Any]],
    period_start: float,
    period_end: float,
    battery_kwh: float,
    max_kw: float,
    phases: int,
    start_soc: float | None,
    target_soc: float,
    loss_factor: LossFactor,
    power_cap: PowerCap,
    prices_averaged_since: float | None = None,
    start_soc_source: str = "unavailable",
    tz: datetime.tzinfo | None = None,
    smart_weekdays: frozenset[int] | None = None,
    drive_history_limit: int | None = None,
    dst_copied_hours: Iterable[float] = (),
) -> tuple[ReplayInput, dict[str, Any]]:
    """The replay input for [period_start, period_end) and what the data covered."""
    drives = list(drives)
    valid_drives = [d for d in drives if _finite(d.get("start")) and _finite(d.get("used")) and d["used"] >= 0]
    full = drive_history_limit is not None and len(drives) >= drive_history_limit
    oldest = min((d["start"] for d in valid_drives), default=None) if full else None
    chosen, left_out = select_windows(windows, period_start, period_end, oldest)
    chosen = [Window(w.start, w.end, datetime.datetime.fromtimestamp(w.end, tz).weekday() if tz is not None else None) for w in chosen]

    hourly: dict[float, float] = {}
    invalid_prices = 0
    for key, price in prices.items():
        try:
            ts = float(key)
        except (TypeError, ValueError):
            invalid_prices += 1
            continue
        if _finite(ts) and ts % HOUR == 0 and _finite(price):
            hourly[ts] = float(price)
        else:
            invalid_prices += 1

    first = chosen[0].start if chosen else period_start
    last = chosen[-1].end if chosen else period_start
    events: list[Event] = []
    drive_count = drives_outside = 0
    for drive in valid_drives:
        if first <= drive["start"] < last:
            events.append(Event(ts=float(drive["start"]), kind="drive", soc_percent=float(drive["used"])))
            drive_count += 1
        else:
            drives_outside += 1
    invalid_drives = len(drives) - len(valid_drives)

    # public charges are already inside the net battery use of the drive they belong to; one in a
    # gap without any recorded drive moves the battery level by an unknown amount
    public_sessions = list(public_sessions)
    usable_public = [s for s in public_sessions if _finite(s.get("ts")) and _finite(s.get("kwh")) and s["kwh"] > 0]
    invalid_public = len(public_sessions) - len(usable_public)
    inside = [s for s in usable_public if first <= s["ts"] < last]
    gaps = [(w.end - GAP_TOLERANCE, chosen[i + 1].start) for i, w in enumerate(chosen[:-1])]
    drive_times = [e.ts for e in events]
    drives_in_gaps = sum(1 for t in drive_times if any(lo <= t < hi for lo, hi in gaps))
    gaps_without_drive = sum(1 for lo, hi in gaps if not any(lo <= t < hi for t in drive_times))
    public_without_drive = sum(
        1 for s in inside
        if any(lo <= s["ts"] < hi and not any(lo <= t < hi for t in drive_times) for lo, hi in gaps)
    )

    needed = {h for w in chosen for h in _hours(w)}
    covered = sum(1 for h in needed if h in hourly)
    legacy = (
        sum(1 for h in needed if h in hourly and (prices_averaged_since is None or h < prices_averaged_since))
        if needed else 0
    )
    copied = sum(1 for h in dst_copied_hours if h in needed)
    coverage = {
        "windows": len(chosen),
        "windows_outside_period": left_out["outside"],
        "windows_still_open": left_out["open"],
        "windows_invalid": left_out["invalid"],
        "windows_before_drive_history": left_out["before_drive_history"],
        "price_hours_needed": len(needed),
        "price_hours_covered": covered,
        "price_hours_missing": len(needed) - covered,
        "price_hours_legacy_unverified": legacy,
        "price_hours_dst_copied": copied,
        "price_entries_invalid": invalid_prices,
        "drives": drive_count,
        "drives_in_gaps": drives_in_gaps,
        "drives_invalid": invalid_drives,
        "drives_outside_replay": drives_outside,
        "connection_gaps": len(gaps),
        "connection_gaps_without_drive": gaps_without_drive,
        "public_charges": len(inside),
        "public_charges_without_drive": public_without_drive,
        "public_charges_invalid": invalid_public,
        "first_window": first if chosen else None,
        "last_window": last if chosen else None,
    }
    provenance = {
        "windows": "measured",
        "prices": "measured" if not legacy and not copied else "measured_partly_unverified",
        "drives": "measured_accepted_drives_only_net_of_public_charging",
        "public_charges": "recorded_only_counted",
        "start_soc": start_soc_source,
        "loss_model": "configured_or_measured_table",
    }
    warnings = []
    if legacy:
        warnings.append("prices_legacy_unverified")  # may be the last quarter of an hour
    if copied:
        warnings.append("prices_dst_hour_copied")  # the repeated autumn hour has the first hour's price
    if invalid_prices or invalid_drives or invalid_public or left_out["invalid"]:
        warnings.append("invalid_input_entries_left_out")  # a dropped drive means demand is understated
    if public_without_drive:
        warnings.append("public_charge_without_drive")  # the battery level gain of that charge is unknown
    if gaps and gaps_without_drive / len(gaps) > MANY_WITHOUT_DRIVE:
        warnings.append("many_connections_without_drive")  # the demand is probably understated a lot
    if left_out["before_drive_history"]:
        warnings.append("connections_before_drive_history_left_out")
    data = ReplayInput(
        windows=tuple(chosen),
        events=tuple(events),
        prices=hourly,
        battery_kwh=battery_kwh,
        max_kw=max_kw,
        phases=phases,
        start_soc=start_soc,
        target_soc=target_soc,
        loss_factor=loss_factor,
        power_cap=power_cap,
        provenance=provenance,
        warnings=tuple(warnings),
        smart_weekdays=smart_weekdays,
    )
    return data, coverage


def _hours(window: Window) -> Iterable[float]:
    hour = window.start - window.start % HOUR
    while hour < window.end:
        yield hour
        hour += HOUR
