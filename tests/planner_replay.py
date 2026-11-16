"""The planner replay of what-if (Smart and charging at once from a start level, without CJP's recorded charging).

The dashboard never asks for it any more (Scenario A always has CJP's charging history), but it exercises the same
primitives (slots, the charging simulation, the events, the loss and power cap, estimated prices) and is kept here as a test
oracle for them. `replay()` of the integration is the recorded comparison only; this one also runs a ReplayInput without
`recorded`.
"""
from __future__ import annotations

from custom_components.cable_juice_planner.engine.whatif.replay import (  # noqa: F401
    COMPARE_TOLERANCE_KWH,
    CONSERVATION_TOLERANCE_KWH,
    ReplayInput,
    ReplayResult,
    Window,
    _conservation,
    _difference,
    _enough_priced,
    _in_gap,
    _ModelError,
    _replay_recorded,
    _run,
    _slots,
    _validate,
    _with_estimated_prices,
)

NOTES = (
    "hindsight",
    "site_limits_not_replayed",
    "solar_not_modelled",
    "grid_buy_price_only",
    "standby_not_modelled",
    "departure_buffer_not_modelled",
)


def replay(data: ReplayInput) -> ReplayResult:
    """Replay both policies over the same demand. Never raises for bad data: it reports why."""
    reason = _validate(data)
    ordered = sorted(data.windows, key=lambda w: w.start)
    provenance = dict(data.provenance)
    if reason is None and data.start_soc is None and len(ordered) < 2:
        reason = "too_few_windows"  # the only connection would be the one that calibrates: nothing is replayed
    if reason is not None:
        return ReplayResult(status="unavailable", reason=reason, windows_total=len(ordered), provenance=provenance)
    try:
        return _planner_replay(data, ordered, provenance)
    except _ModelError as err:
        return ReplayResult(status="unavailable", reason=err.reason, windows_total=len(ordered), provenance=provenance)


def _planner_replay(data: ReplayInput, ordered: list[Window], provenance: dict[str, str]) -> ReplayResult:
    measured = data  # the average price and the "enough measured prices" rule use only what was really priced
    data, price_fill, fully_priced = _with_estimated_prices(data, ordered)
    if not fully_priced and (price_fill is None or not _enough_priced(measured, ordered)):
        return ReplayResult(
            status="unavailable", reason="missing_prices", windows_total=len(ordered), provenance=provenance,
            warnings=measured.warnings,
        )
    if data.recorded is not None:
        return _replay_recorded(data, ordered, provenance, measured, price_fill)
    calibrate_to = measured.target_soc
    smart, smart_stop, smart_at, smart_n = _run(data, "smart", ordered, calibrate_to=calibrate_to)
    asap, asap_stop, asap_at, asap_n = _run(data, "asap", ordered, calibrate_to=calibrate_to)

    # both policies must cover the same windows or the comparison would not be like for like
    count = min(smart_n, asap_n)
    stop, stopped = (smart_stop, smart_at) if smart_n <= asap_n else (asap_stop, asap_at)
    if count == 0:
        return ReplayResult(
            status="unavailable", reason=stop or "no_windows", windows_total=len(ordered), provenance=provenance,
        )
    if count < len(ordered):
        # run again over exactly the compared windows: no drive after the
        # last one counts towards the lowest SOC, the energy balance or the unmet demand
        smart, _, _, _ = _run(data, "smart", ordered[:count], calibrate_to=calibrate_to)
        asap, _, _, _ = _run(data, "asap", ordered[:count], calibrate_to=calibrate_to)

    compared = ordered[:count]
    weighted = [(slot.price, slot.hours) for w in compared for slot in _slots(w, measured.prices) if slot.price is not None]
    hours = sum(h for _, h in weighted)
    average = sum(p * h for p, h in weighted) / hours if hours > 0 else None

    applied = sum(1 for e in data.events if any(_in_gap(e, w, ordered[i + 1].start) for i, w in enumerate(compared[:-1])))
    estimated_windows = smart.estimated_windows
    calibrated = smart.calibrated
    warnings = (*data.warnings, *(("start_level_unknown_calibrated",) if calibrated else ()))

    error = max(_conservation(data, smart), _conservation(data, asap))
    complete = count == len(ordered)
    result = ReplayResult(
        status="ok" if complete else "partial",
        reason=None if complete else stop,
        smart=smart,
        asap=asap,
        windows_total=len(ordered),
        windows_replayed=count,
        stopped_at=None if complete else stopped,
        average_price=average,
        max_conservation_error_kwh=error,
        warnings=warnings,
        provenance=provenance,
        ignored_events=len(data.events) - applied,
        method="estimated" if estimated_windows or calibrated else "strict",
        estimated_windows=estimated_windows,
        price_estimate=price_fill,
        calibrated_start=calibrated,
        notes=NOTES,
    )
    if not error <= CONSERVATION_TOLERANCE_KWH:
        return ReplayResult(
            status="unavailable", reason="energy_conservation", windows_total=len(ordered), provenance=provenance,
            max_conservation_error_kwh=error,
        )

    # an infeasible policy must not win by delivering less
    if abs(smart.unmet_target_kwh - asap.unmet_target_kwh) > COMPARE_TOLERANCE_KWH:
        result.comparable_reason = "unmet_target_differs"
    elif abs(smart.unmet_driving_kwh - asap.unmet_driving_kwh) > COMPARE_TOLERANCE_KWH:
        result.comparable_reason = "unmet_driving_differs"
    elif average is None:
        result.comparable_reason = "no_prices"
    else:
        result.comparable = True
        result.difference = _difference(data, smart, asap, average)
    return result
