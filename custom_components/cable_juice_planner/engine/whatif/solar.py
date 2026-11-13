"""What-if Scenario D, first step: the EV's solar energy replaced by grid energy at the same charging times.

Pure (plain dicts in, plain dicts out, no Home Assistant). The question it answers: what would the
energy the EV took from solar have cost if the EV had charged at the same times from the grid instead,
and the solar had been exported? That is the first of the no-solar modes ("no EV use of solar, the
surplus exported"). It is NOT "physically no solar production" and NOT a replanned charging without
solar; both need hourly solar availability, which CJP does not store.

For every finished private charging session with solar energy S (kWh):

- with solar the energy is valued at its opportunity cost, the sale it replaced: S x sell_value(hourly
  sell price), exactly as CJP values it in the plan (never as free);
- without solar the same kWh come from the grid at the hourly buy price minus the refund (the basis CJP
  prices its own grid energy at): S x (buy - refund);
- the solar advantage is the difference (positive = solar was cheaper than the grid).

The grid part of a session is identical in both cases, so it cancels and is not used. The recorded cost
of a session is not used either: it is one snapshot price times the whole session. Both prices are the
time-weighted average over the session span, which assumes the solar energy was spread evenly over it.

A session is priced ONCE, soon after it ended (evaluate_session), and the result is kept in a ledger:
CJP merges its charging history per day after a week, which would turn a session into a day-long span
with pauses, and prices and the refund can change later. The months are summed from that ledger
(summarize).

The home battery is kept as recorded: its energy is neither replaced nor valued, and no whole-house
result is claimed.

Nothing is filled in: a session whose span lacks a stored buy or sell price is left out and counted. A
session recorded before the solar energy was recorded as energy (see engine/energy/solar.py) is left out
too.

The time no priced session covers can be estimated from the recorder's hourly statistics (power_hour_entries).
Hourly means give only an upper bound of the solar energy, so those hours are marked "estimated" and counted
apart (hours, kWh, months), and the result says it holds an upper bound ("upper_bound", source "mixed" or
"power_history").
"""
from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from typing import Any

SOLAR_MODEL_VERSION = 2
HOUR = 3600.0
MIN_SPAN = 60.0  # s : a session shorter than this is priced at the hour it started in

NOTES = (
    "ev_solar_exported_instead",
    "solar_spread_evenly_over_session",
    "home_battery_kept_as_recorded",
    "no_whole_house_result",
    "replanned_without_solar_not_modelled",
    "no_solar_production_not_modelled",
    "recorded_sessions_only",
)

# every reason a session can be left out (all are counted and shown)
LEFT_OUT = (
    "public", "still_charging", "invalid", "without_solar", "before_energy_fix", "missing_buy_price", "missing_sell_price",
    "not_captured",
)


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def sell_value(price: float, production_price: float) -> float:
    """What a sold kWh is worth at this hourly sell price: the one rule of CJP's sell_value() in pricing.py.

    With a fixed solar sell price (a production agreement that settles the difference to the spot
    price) it is the fixed price minus the hourly price; without one (-1) it is the hourly price.
    """
    return price if production_price == -1.0 else production_price - price


def _span_prices(
    start: float, end: float, buy: Mapping[float, float], sell: Mapping[float, float], production: Mapping[float, float], refund: float,
) -> tuple[float, float] | str:
    """(buy price - refund, sell value), both time-weighted over [start, end), or the reason there is none."""
    end = max(end, start + MIN_SPAN)
    hour = start - start % HOUR
    weights = buy_total = sell_total = 0.0
    while hour < end:
        overlap = min(hour + HOUR, end) - max(hour, start)
        if overlap > 0:
            if hour not in buy or not _finite(buy[hour]):
                return "missing_buy_price"
            if hour not in sell or not _finite(sell[hour]) or not _finite(production.get(hour)):
                return "missing_sell_price"
            weights += overlap
            buy_total += overlap * (float(buy[hour]) - refund)
            sell_total += overlap * sell_value(float(sell[hour]), float(production[hour]))
        hour += HOUR
    return buy_total / weights, sell_total / weights


def evaluate_session(
    session: Mapping[str, Any],
    buy: Mapping[float, float],
    sell: Mapping[float, float],
    production: Mapping[float, float],
    refund: float,
    solar_energy_since: float | None,
) -> dict[str, Any]:
    """Price one session. The outcome is one of:

    - "live": still charging, nothing to price yet;
    - "pending": a stored price is missing (it may still arrive; the caller decides when to give up);
    - "left_out" with a reason from LEFT_OUT;
    - "counted" with solar_kwh, grid_cost (S x (buy - refund)), sell_value (S x sell value) and powerwall_kwh.

    session: {"start", "end" (None while charging), "public", "kwh", "local", "solar", "powerwall"}.
    buy: UTC hour -> grid buy price; sell: UTC hour -> raw sell price; production: UTC hour -> the fixed
    production price in effect (-1 = none); refund: what CJP subtracts from the buy price.
    solar_energy_since: sessions that started before it did not record solar as energy (unusable).
    """
    if session.get("public"):
        return {"outcome": "left_out", "reason": "public"}
    start, end = session.get("start"), session.get("end")
    kwh, solar, local = session.get("kwh"), session.get("solar"), session.get("local")
    if not _finite(start):
        return {"outcome": "left_out", "reason": "invalid"}
    if end is None:
        return {"outcome": "live"}
    if not (_finite(end) and _finite(kwh) and _finite(solar) and _finite(local) and kwh >= 0 and solar >= 0 and local >= 0):
        return {"outcome": "left_out", "reason": "invalid"}
    energy = min(solar, local, kwh)  # the solar part of the energy the EV charged can not be more than that
    if energy <= 1e-9:
        return {"outcome": "left_out", "reason": "without_solar"}
    if solar_energy_since is None or start < solar_energy_since:
        return {"outcome": "left_out", "reason": "before_energy_fix"}
    found = _span_prices(float(start), float(end), buy, sell, production, refund)
    if isinstance(found, str):
        return {"outcome": "pending", "reason": found}
    buy_price, sell_price = found
    powerwall = session.get("powerwall")
    return {
        "outcome": "counted",
        "solar_kwh": energy,
        "grid_cost": energy * buy_price,
        "sell_value": energy * sell_price,
        "powerwall_kwh": float(powerwall) if _finite(powerwall) and powerwall > 0 else 0.0,
    }


def summarize(
    entries: Iterable[Mapping[str, Any]], current_month: str, extra_left_out: Mapping[str, int] | None = None,
    hours_left_out: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """The monthly and total solar advantage and what was left out, from priced sessions and estimated hours.

    entries: {"month", "outcome" ("counted" or "left_out"), "reason", and for counted: "solar_kwh", "grid_cost",
    "sell_value", "powerwall_kwh", and "estimated": True for a charging hour estimated from the hourly power
    statistics (power_hour_entries), whose solar energy is an upper bound}. extra_left_out: session counts that are
    not in the ledger (still charging, not captured in time). hours_left_out: the estimated hours that were left out
    (kept apart from the sessions: they count hours).

    Per month and in the total, "sessions" counts the priced sessions and "hours" the estimated hours;
    "estimated_kwh" is the part of "solar_kwh" from estimated hours and "upper_bound" says there is such a part.
    """
    left_out = dict.fromkeys(LEFT_OUT, 0)
    for key, count in (extra_left_out or {}).items():
        left_out[key] = left_out.get(key, 0) + int(count)
    months: dict[str, dict[str, Any]] = {}
    powerwall_kwh = 0.0
    counted = hours = 0
    for entry in entries:
        if entry.get("outcome") != "counted":
            reason = entry.get("reason")
            left_out[reason if reason in left_out else "invalid"] += 1
            continue
        month = months.setdefault(entry["month"], {
            "month": entry["month"], "sessions": 0, "hours": 0, "solar_kwh": 0.0, "estimated_kwh": 0.0, "session_kwh": 0.0,
            "grid_cost": 0.0, "sell_value": 0.0,
        })
        if entry.get("estimated"):
            month["hours"] += 1
            month["estimated_kwh"] += entry["solar_kwh"]
            hours += 1
        else:
            month["sessions"] += 1
            month["session_kwh"] += entry["solar_kwh"]
            counted += 1
        month["solar_kwh"] += entry["solar_kwh"]
        month["grid_cost"] += entry["grid_cost"]
        month["sell_value"] += entry["sell_value"]
        powerwall_kwh += entry.get("powerwall_kwh") or 0.0

    rows = []
    for key in sorted(months):
        month = months[key]
        rows.append({
            "month": key,
            "status": "current" if key >= current_month else "completed",
            "sessions": month["sessions"],
            "hours": month["hours"],
            "solar_kwh": round(month["solar_kwh"], 3),
            "estimated_kwh": round(month["estimated_kwh"], 3),
            "upper_bound": month["hours"] > 0,
            "grid_cost": round(month["grid_cost"], 2),
            "sell_value": round(month["sell_value"], 2),
            "advantage": round(month["grid_cost"] - month["sell_value"], 2),
        })
    completed = [row for row in rows if row["status"] == "completed"]
    total = {
        "months": len(completed),
        "sessions": sum(row["sessions"] for row in completed),
        "hours": sum(row["hours"] for row in completed),
        "solar_kwh": round(sum(row["solar_kwh"] for row in completed), 3),
        "estimated_kwh": round(sum(row["estimated_kwh"] for row in completed), 3),
        "upper_bound": any(row["upper_bound"] for row in completed),
        "grid_cost": round(sum(row["grid_cost"] for row in completed), 2),
        "sell_value": round(sum(row["sell_value"] for row in completed), 2),
        "advantage": round(sum(row["advantage"] for row in completed), 2),
    }
    if counted or hours:
        status, reason = "ok", None
    elif left_out["before_energy_fix"] and not (left_out["missing_buy_price"] or left_out["missing_sell_price"]):
        status, reason = "unavailable", "waiting_for_new_sessions"
    elif left_out["missing_sell_price"] or (hours_left_out or {}).get("missing_sell_price"):
        status, reason = "unavailable", "no_sell_prices"
    elif left_out["missing_buy_price"] or (hours_left_out or {}).get("missing_buy_price"):
        status, reason = "unavailable", "no_buy_prices"
    else:
        status, reason = "unavailable", "no_solar_sessions"
    notes = list(NOTES)
    if hours:
        notes += ["solar_from_power_history", "hourly_solar_upper_bound"]
    return {
        "model_version": SOLAR_MODEL_VERSION,
        "status": status,
        "reason": reason,
        "source": "mixed" if counted and hours else "power_history" if hours else "sessions",
        "months": rows,
        "total": total,
        "sessions_counted": counted,
        "hours_counted": hours,
        "upper_bound": hours > 0,
        # what the recorded sessions priced, and what was estimated from the hourly statistics (an upper bound)
        "ledger": {
            "sessions": counted, "solar_kwh": round(sum(m["session_kwh"] for m in months.values()), 3),
            "months": sum(1 for m in months.values() if m["sessions"]),
        },
        "estimated": {
            "hours": hours, "solar_kwh": round(sum(m["estimated_kwh"] for m in months.values()), 3),
            "months": sum(1 for m in months.values() if m["hours"]),
            "left_out": {key: int(count) for key, count in (hours_left_out or {}).items()},
        },
        "left_out": left_out,
        "powerwall_kwh": round(powerwall_kwh, 3),
        "notes": notes,
    }


def solar_comparison(
    sessions: Iterable[Mapping[str, Any]],
    buy: Mapping[float, float],
    sell: Mapping[float, float],
    production: Mapping[float, float],
    refund: float,
    solar_energy_since: float | None,
    current_month: str,
    month_of: Callable[[float], str],
) -> dict[str, Any]:
    """Price and sum the sessions in one go (a session with a missing price is left out at once).

    The engine prices each session once and keeps the result (see solar_part.py); this is the same
    calculation without the ledger.
    """
    entries: list[dict[str, Any]] = []
    still_charging = 0
    for session in sessions:
        result = evaluate_session(session, buy, sell, production, refund, solar_energy_since)
        if result["outcome"] == "live":
            still_charging += 1
            continue
        if result["outcome"] == "pending":
            result = {"outcome": "left_out", "reason": result["reason"]}
        entries.append({**result, "month": month_of(float(session["start"])) if _finite(session.get("start")) else ""})
    summary = summarize(entries, current_month, {"still_charging": still_charging})
    return {**summary, "solar_energy_since": solar_energy_since}


MIN_EV_KWH = 0.05  # an hour in which the EV charged less than this is not a charging hour


def _overlaps_any(spans: Iterable[tuple[float, float]], start: float, end: float) -> bool:
    """[start, end) shares time with a span [s, e) (a span without length counts as one second)."""
    return any(s < end and start < max(e, s + 1.0) for s, e in spans)


def power_hour_entries(
    power_hours: Mapping[float, Any],
    buy: Mapping[float, float],
    sell: Mapping[float, float],
    production: Mapping[float, float],
    assumed_production: float,
    refund: float,
    last_hour: float,
    month_of: Callable[[float], str],
    covered: Iterable[tuple[float, float]] = (),
) -> tuple[list[dict[str, Any]], dict[str, int], int]:
    """The charging hours of the recorder's hourly statistics, priced like a session: (entries, hours left out, hours
    whose production price was assumed). Each entry is marked "estimated": its solar energy is an UPPER BOUND.

    power_hours: UTC hour -> [solar production W, house consumption W (the EV included), charger power W], the
    hourly means. Like CJP's own formula (local_energy_available) the solar the EV can use in an hour is what the
    production leaves after the rest of the house: max(solar - (house - EV), 0), never more than the EV charged. On
    hourly means that is the most the EV could have taken from solar: a short burst at full power (charger 11 kW for
    20 minutes) and slow charging all hour long have the same mean, and only the slow one can take all of it from the
    solar. A duty-cycle correction would under-estimate solar-following charging, so the hours are kept as an upper
    bound and the recorded sessions are preferred wherever they exist: an hour that shares any time with a span in
    `covered` (the sessions CJP priced or will price) is not used at all, also when only part of it is covered.

    The home battery is not modelled (its energy is not separated). Hours without a buy or sell price are left out and
    counted (as hours); the production price of an hour that has none stored is the one set now (counted as assumed).
    """
    # a session's end is stored to the minute (the last 59 seconds may be cut off): the hour it ends in still belongs
    # to it, so the energy of its last seconds is not estimated a second time
    spans = [(float(s), float(e) + 60.0) for s, e in covered if _finite(s) and _finite(e)]
    left_out = {"missing_buy_price": 0, "missing_sell_price": 0, "covered_by_sessions": 0}
    entries: list[dict[str, Any]] = []
    assumed = 0
    for ts in sorted(power_hours):
        values = power_hours[ts]
        if ts > last_hour or not isinstance(values, (list, tuple)) or len(values) != 3 or not all(_finite(v) for v in values):
            continue
        solar_w, house_w, ev_w = (float(v) for v in values)
        ev_kwh = ev_w / 1000.0
        if ev_kwh < MIN_EV_KWH:
            continue
        if _overlaps_any(spans, ts, ts + HOUR):
            left_out["covered_by_sessions"] += 1  # the recorded session is used for this time instead
            continue
        energy = min(ev_kwh, max(solar_w - max(house_w - ev_w, 0.0), 0.0) / 1000.0)
        if energy <= 1e-9:
            continue  # no solar in this charging hour: nothing to replace
        if not _finite(buy.get(ts)):
            left_out["missing_buy_price"] += 1
            continue
        if not _finite(sell.get(ts)):
            left_out["missing_sell_price"] += 1
            continue
        fixed = production.get(ts)
        if not _finite(fixed):
            fixed, assumed = assumed_production, assumed + 1
        entries.append({
            "month": month_of(ts), "outcome": "counted", "estimated": True, "solar_kwh": energy,
            "grid_cost": energy * (float(buy[ts]) - refund), "sell_value": energy * sell_value(float(sell[ts]), float(fixed)),
            "powerwall_kwh": 0.0,
        })
    return entries, left_out, assumed


def solar_from_power_hours(
    power_hours: Mapping[float, Any],
    buy: Mapping[float, float],
    sell: Mapping[float, float],
    production: Mapping[float, float],
    assumed_production: float,
    refund: float,
    last_hour: float,
    current_month: str,
    month_of: Callable[[float], str],
    sessions: Iterable[Mapping[str, Any]] = (),
    extra_left_out: Mapping[str, int] | None = None,
    also_covered: Iterable[tuple[float, float]] = (),
) -> dict[str, Any]:
    """The comparison from the priced sessions (`sessions`: ledger entries with "start" and "end") and, for the time
    no session covers, the charging hours of the recorder's hourly statistics (see power_hour_entries; their solar
    energy is an upper bound, and the result says how much of it is estimated). Without sessions it is the hours only.
    also_covered: more spans whose hours are left to the sessions (a session still charging or not priced yet).
    """
    sessions = list(sessions)
    covered = [(e.get("start"), e.get("end")) for e in sessions] + list(also_covered)
    hours, left_out, assumed = power_hour_entries(
        power_hours, buy, sell, production, assumed_production, refund, last_hour, month_of, covered,
    )
    summary = summarize([*sessions, *hours], current_month, extra_left_out, left_out)
    summary["assumed_production_hours"] = assumed
    return summary
