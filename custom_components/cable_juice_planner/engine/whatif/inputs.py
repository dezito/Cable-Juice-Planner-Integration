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

from .derive import used_between
from .modelled import model_connections
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


def _derived_connections(
    derived: Mapping[str, Any] | None, measured: list[Window], period_start: float, period_end: float, tz: datetime.tzinfo | None
) -> tuple[list[Window], list[tuple[float, float]], tuple[float, float] | None, dict[str, Any]]:
    """The connections estimated from the battery level statistics (derive.py) that belong to the period.

    Returns (windows, the net battery use in the gaps between them as (time, percent), the use in the gap before the
    first measured connection, info for the coverage). They only come before the first measured connection and are
    never used when the estimate was not validated against the measured ones.
    """
    info: dict[str, Any] = {"windows_derived": 0, "derived_gaps_without_use": 0, "derived_entries_invalid": 0}
    if not isinstance(derived, Mapping):
        return [], [], None, info
    info["derived_status"] = derived.get("status")
    info["derived_reason"] = derived.get("reason")
    info["derived_validation"] = derived.get("validation")
    if derived.get("status") != "ok":
        return [], [], None, info
    rows: list[tuple[float, float, float, float]] = []
    for entry in derived.get("windows") or []:
        values = tuple(entry.get(key) if isinstance(entry, Mapping) else None for key in ("start", "end", "soc_in", "soc_out"))
        if not all(_finite(v) for v in values) or values[1] <= values[0] or not all(0.0 <= v <= 100.0 for v in values[2:]):
            info["derived_entries_invalid"] += 1
            continue
        start, end, soc_in, soc_out = (float(v) for v in values)  # type: ignore[arg-type]
        if start < period_start or end > period_end or (measured and end > measured[0].start):
            continue
        rows.append((start, end, soc_in, soc_out))
    rows.sort()
    kept: list[tuple[float, float, float, float]] = []
    for row in rows:
        if kept and row[0] < kept[-1][1]:
            info["derived_entries_invalid"] += 1  # overlapping connections cannot both be real
            continue
        kept.append(row)
    anchor = derived.get("anchor")
    if kept and measured and not (
        isinstance(anchor, Mapping) and anchor.get("start") == measured[0].start and _finite(anchor.get("soc_in"))
    ):
        # the battery level at the first measured connection is not known from the statistics (or that connection is not the
        # one the estimate was made for, e.g. an older measured one was left out): the use in the gap before it would be
        # missing, so no estimated connection is used rather than a demand of zero
        info["derived_reason"] = "anchor_mismatch"
        return [], [], None, info
    windows = [
        Window(s, e, datetime.datetime.fromtimestamp(e, tz).weekday() if tz is not None else None, derived=True) for s, e, _, _ in kept
    ]
    gaps: list[tuple[float, float]] = []
    for (_, end, _, out), (start, _, soc_in, _) in zip(kept, kept[1:]):
        used = used_between(out, soc_in)
        if used is None:
            info["derived_gaps_without_use"] += 1
        else:
            gaps.append(((end + start) / 2.0, used))
    final = None
    if kept and measured:
        used = used_between(kept[-1][3], float(anchor["soc_in"]))
        if used is not None:
            final = ((kept[-1][1] + measured[0].start) / 2.0, used)
        else:
            info["derived_gaps_without_use"] += 1
    info["windows_derived"] = len(windows)
    info["derived_first_soc"] = kept[0][2] if kept else None
    return windows, gaps, final, info


def _modelled_connections(
    work_plan: Mapping[str, Any] | None, tz: datetime.tzinfo | None, windows: Iterable[Mapping[str, Any]],
    drives: list[Mapping[str, Any]], period_start: float, derived_info: Mapping[str, Any], derived: Mapping[str, Any] | None,
    real: list[Window],
) -> tuple[list[Window], list[tuple[float, float]], tuple[float, float] | None, dict[str, Any]]:
    """The connections modelled from the work plan (modelled.py) for the part of the period before any record.

    That is before the first estimated or measured connection of the period, and before the battery level statistics begin
    when connections were estimated from them. Returns (windows, the battery use in the gaps between them, the use in the
    gap before the first real connection, info for the coverage).
    """
    info: dict[str, Any] = {"windows_modelled": 0, "modelled_status": None, "modelled_reason": None, "modelled_validation": None}
    if work_plan is None or tz is None or not real:
        return [], [], None, info
    complete = [
        (float(w["start"]), float(w["end"])) for w in windows
        if isinstance(w, Mapping) and _finite(w.get("start")) and _finite(w.get("end")) and w["end"] > w["start"]
    ]
    # never over a time a measured connection exists in, also one that is left out of the replay (outside the period, or
    # before the drive history): the model only fills in what nothing records
    region_end = min(real[0].start, min((s for s, _ in complete), default=real[0].start))
    statistics_from = derived.get("statistics_from") if isinstance(derived, Mapping) else None
    if derived_info.get("windows_derived") and _finite(statistics_from):
        region_end = min(region_end, float(statistics_from))
    if region_end <= period_start:
        return [], [], None, info
    model = model_connections(work_plan, tz, complete, drives, period_start, region_end, real[0].start)
    info.update(modelled_status=model["status"], modelled_reason=model["reason"], modelled_validation=model["validation"])
    if model["status"] != "ok":
        return [], [], None, info
    built = [
        Window(float(s), float(e), datetime.datetime.fromtimestamp(e, tz).weekday(), derived=True, modelled=True)
        for s, e in model["windows"]
    ]
    last_end = built[-1].end
    between = [(float(t), float(u)) for t, u in model["drives"] if t <= last_end]
    final = next(((float(t), float(u)) for t, u in model["drives"] if t > last_end), None)
    info["windows_modelled"] = len(built)
    return built, between, final, info


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
    history_hours: Iterable[float] = (),
    refund: float = 0.0,
    derived: Mapping[str, Any] | None = None,
    work_plan: Mapping[str, Any] | None = None,
) -> tuple[ReplayInput, dict[str, Any]]:
    """The replay input for [period_start, period_end) and what the data covered.

    derived: the connections before CJP's own records, estimated from the battery level statistics (see derive.py).
    When there are some, the battery level at the first of them comes from the same statistics and replaces start_soc.
    work_plan: the work plan as set now (see engine_part.what_if_work_plan). For the time before the first estimated or
    measured connection, and before the statistics begin, the connections and the battery use are modelled from it (see
    modelled.py), when that model has found the measured connections. Their start level is not known: start_soc is used.
    """
    drives = list(drives)
    valid_drives = [d for d in drives if _finite(d.get("start")) and _finite(d.get("used")) and d["used"] >= 0]
    full = drive_history_limit is not None and len(drives) >= drive_history_limit
    oldest = min((d["start"] for d in valid_drives), default=None) if full else None
    windows = list(windows)
    chosen, left_out = select_windows(windows, period_start, period_end, oldest)
    chosen = [Window(w.start, w.end, datetime.datetime.fromtimestamp(w.end, tz).weekday() if tz is not None else None) for w in chosen]
    measured_chosen = chosen
    derived_chosen, derived_gap_drives, derived_final_drive, derived_info = _derived_connections(
        derived, measured_chosen, period_start, period_end, tz
    )
    real = [*derived_chosen, *measured_chosen]
    modelled_chosen, modelled_gap_drives, modelled_final_drive, modelled_info = _modelled_connections(
        work_plan, tz, windows, drives, period_start, derived_info, derived, real
    )
    chosen = [*modelled_chosen, *real]
    estimated = [*modelled_chosen, *derived_chosen]
    if modelled_chosen:
        # nothing records the level at a modelled connection (and an entered or read level belongs to the first measured one,
        # a car that may have been away at the modelled time): the first connection only brings the car to a common level
        start_soc, start_soc_source = None, "unavailable"
    elif derived_chosen:
        # the level at the first estimated connection comes from the statistics too: an entered or read level belongs to the first measured one
        start_soc, start_soc_source = float(derived_info["derived_first_soc"]), "battery_statistics"

    hourly: dict[float, float] = {}
    invalid_prices = 0
    for key, price in prices.items():
        try:
            ts = float(key)
        except (TypeError, ValueError):
            invalid_prices += 1
            continue
        if _finite(ts) and ts % HOUR == 0 and _finite(price):
            hourly[ts] = float(price) - refund  # the basis CJP prices its own grid energy at (the stored prices are gross)
        else:
            invalid_prices += 1

    first = chosen[0].start if chosen else period_start
    last = chosen[-1].end if chosen else period_start
    events: list[Event] = []
    drive_count = drives_outside = 0
    # a recorded drive before the end of the last estimated connection belongs to a gap that is already covered by the
    # net battery use from the statistics: counting both would drive the same trip twice
    estimated_until = estimated[-1].end - GAP_TOLERANCE if estimated else None
    drives_replaced = 0
    for drive in valid_drives:
        if first <= drive["start"] < last:
            if estimated_until is not None and drive["start"] < estimated_until:
                drives_replaced += 1
                continue
            events.append(Event(ts=float(drive["start"]), kind="drive", soc_percent=float(drive["used"])))
            drive_count += 1
        else:
            drives_outside += 1
    # the gap before the first measured connection: a recorded drive in it is better than the net use from the statistics
    if derived_final_drive is not None and not any(estimated_until <= e.ts < measured_chosen[0].start for e in events):  # type: ignore[operator]
        derived_gap_drives = [*derived_gap_drives, derived_final_drive]
    # likewise the gap before the first connection that is not modelled: a recorded drive wins over the modelled use
    if modelled_final_drive is not None and not any(modelled_chosen[-1].end - GAP_TOLERANCE <= e.ts < real[0].start for e in events):
        modelled_gap_drives = [*modelled_gap_drives, modelled_final_drive]
    estimated_drives = [*modelled_gap_drives, *derived_gap_drives]
    events.extend(Event(ts=ts, kind="drive", soc_percent=min(used, 100.0)) for ts, used in estimated_drives)
    drive_count += len(estimated_drives)
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
    from_history_hours = {h for h in history_hours if h in needed and h in hourly}
    legacy = (
        sum(1 for h in needed if h in hourly and h not in from_history_hours and (prices_averaged_since is None or h < prices_averaged_since))
        if needed else 0
    )
    copied = sum(1 for h in dst_copied_hours if h in needed)
    from_history = len(from_history_hours)
    coverage = {
        "windows": len(chosen),
        "windows_measured": len(measured_chosen),
        "windows_derived": derived_info["windows_derived"],
        "derived_plan_bounds": bool(derived.get("plan_bounds")) if isinstance(derived, Mapping) else False,
        "windows_modelled": modelled_info["windows_modelled"],
        "modelled_status": modelled_info["modelled_status"],
        "modelled_reason": modelled_info["modelled_reason"],
        "modelled_validation": modelled_info["modelled_validation"],
        "modelled_drives": len(modelled_gap_drives),
        "derived_status": derived_info.get("derived_status"),
        "derived_reason": derived_info.get("derived_reason"),
        "derived_validation": derived_info.get("derived_validation"),
        "derived_drives": len(derived_gap_drives),
        "derived_gaps_without_use": derived_info["derived_gaps_without_use"],
        "derived_entries_invalid": derived_info["derived_entries_invalid"],
        "drives_replaced_by_derived": drives_replaced,
        "windows_outside_period": left_out["outside"],
        "windows_still_open": left_out["open"],
        "windows_invalid": left_out["invalid"],
        "windows_before_drive_history": left_out["before_drive_history"],
        "price_hours_needed": len(needed),
        "price_hours_covered": covered,
        "price_hours_missing": len(needed) - covered,
        "price_hours_legacy_unverified": legacy,
        "price_hours_dst_copied": copied,
        "price_hours_from_history": from_history,
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
        "windows": (
            "measured" if not estimated else
            "measured_and_modelled_from_work_plan" if modelled_chosen and not derived_chosen else
            "measured_and_estimated_from_battery_statistics" if not modelled_chosen else
            "measured_estimated_from_battery_statistics_and_modelled_from_work_plan"
        ),
        "prices": "measured" if not legacy and not copied and not from_history else "measured_partly_unverified",
        "drives": "measured_accepted_drives_only_net_of_public_charging",
        "public_charges": "recorded_only_counted",
        "start_soc": start_soc_source,
        "loss_model": "configured_or_measured_table",
    }
    warnings = []
    if legacy:
        warnings.append("prices_legacy_unverified")  # may be the last quarter of an hour
    if from_history:
        warnings.append("prices_from_price_history")  # the price sensor's recorded hourly statistics, not CJP's own stored hour
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
    if derived_chosen:
        warnings.append("connections_estimated_from_battery_statistics")  # older connections: when the car was parked, not plugged in
    if modelled_chosen:
        warnings.append("connections_modelled_from_work_plan")  # the plan as set now, not a record of what happened
    if modelled_info.get("modelled_reason") == "not_validated":
        warnings.append("work_plan_model_not_validated")
    if derived_info.get("derived_reason") == "not_validated":
        warnings.append("older_connections_not_validated")  # the method did not find the measured connections: not used
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
