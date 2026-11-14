"""What CJP itself charged, from its charging history, laid over the connections (pure: plain dicts in, no Home Assistant).

The charging history holds, for every session, the kWh, the battery percent, the kWh that came from solar or the home
battery, and the cost CJP booked for it (solar and the home battery at CJP's own value, one price per session). Sessions
older than a week are merged per calendar day (engine/history/combine.py): the entry then starts at the first session of
the day and ends at the last one, and the hours in between are lost. A merged day can therefore hold the end of one
connection and the start of the next.

So every entry is laid over the connections by overlap in time: an entry that lies inside one connection gives all of its
figures to it, an entry that reaches into two (a merged day, or a session that runs on past the unplug) is split between
them by the time it overlaps each, and counted as split so that the card can say so. An entry that overlaps no connection
is counted as outside. Public charging inside an entry (kWh_from_public, public_cost) is taken out: the drives already
contain it (a drive is the level at departure minus the level at the next plug-in).

Nothing is filled in: an entry with a missing or invalid figure makes the connections it touches invalid (no record), an
entry that is still running makes them not settled, and a connection that starts before the first entry of the history has
no record (the entries before it may be gone: nothing is known, never a favourable zero or a part of a night).

What the cost is: the price CJP booked, one per session, set when the session ends (or is recalculated in its last hour), so
it can differ from the hourly prices the session was charged at. A booked cost of 0 for grid energy is what CJP writes when
it could not calculate a price: such an entry is invalid, never a free night. After a merge a zero inside a sum cannot be seen.
Entries within the last week are merged per day as well (not only the older ones), and a merged entry ends at the end of its
newest session: when that session runs past midnight the end is read as the same day, so such an entry can be given to the
connection of its first session.
"""
from __future__ import annotations

import datetime
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

TOLERANCE = 600.0  # s: a session may start this long before the plug-in time of its connection
END_TOLERANCE = 60.0  # s: and end this long after the unplug time
MIN_HOME_KWH = 1e-3  # kWh: an entry with less home charging than this is public charging only
MIN_BOOKED_GRID_KWH = 0.1  # kWh: grid energy booked at 0 above this is a missing price


def _number(value: Any, minimum: float | None = 0.0) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if minimum is not None and value < minimum:
        return None
    return float(value)


def history_rows(
    history: Mapping[Any, Any], to_ts: Callable[[datetime.datetime], float], battery_kwh: float | None
) -> list[dict[str, Any]]:
    """The home charging of every entry of the charging history as a plain row: start and end in epoch seconds, kWh,
    battery percent, kWh from local energy and cost, without the public charging inside the entry. A row with a figure that
    is missing or not a finite number is "valid" False, never zero; a row that is only public charging is left out."""
    rows: list[dict[str, Any]] = []
    for key, value in history.items():
        if not isinstance(key, datetime.datetime) or not isinstance(value, Mapping):
            continue
        start = to_ts(key)
        ended = value.get("ended")
        ongoing = ended == ">"
        end = start + 60.0  # an entry without an end time is one minute at its start
        if isinstance(ended, str) and not ongoing:
            try:
                hour, minute = (int(part) for part in ended.split(":")[:2])
                moment = key.replace(hour=hour, minute=minute, second=0, microsecond=0)
                # the keys have seconds: an end in the same minute as the start is that minute, not the next day
                if moment < key.replace(second=0, microsecond=0):
                    moment += datetime.timedelta(days=1)
                end = max(to_ts(moment), start + 60.0)
            except (ValueError, TypeError):
                pass
        kwh, percent, local = _number(value.get("kWh")), _number(value.get("percentage")), _number(value.get("kWh_from_local_energy"))
        cost = _number(value.get("cost"), None)  # a booked cost can be negative (a negative price), never missing
        public_kwh = _number(value.get("kWh_from_public"))
        public_cost = _number(value.get("public_cost"), None)
        valid = None not in (kwh, percent, local, cost, public_kwh if value.get("kWh_from_public") is not None else 0.0)
        if valid and abs(cost or 0.0) < 1e-9 and (kwh or 0.0) - (local or 0.0) - (public_kwh or 0.0) > MIN_BOOKED_GRID_KWH:
            valid = False  # grid energy booked at 0: CJP could not calculate the price, it is not a free night
        if valid and public_kwh:
            # a merged day can hold public charging next to home charging: take it out (its percent from its kWh)
            battery = battery_kwh if battery_kwh and battery_kwh > 0 else None
            if public_cost is None or battery is None or public_kwh > kwh + MIN_HOME_KWH:
                valid = False
            else:
                kwh, percent, cost = max(kwh - public_kwh, 0.0), max(percent - public_kwh / battery * 100.0, 0.0), cost - public_cost  # type: ignore[operator]
                local = min(local, kwh)  # type: ignore[type-var]
        if valid and not ongoing and (kwh or 0.0) < MIN_HOME_KWH and (percent or 0.0) < 0.1 and abs(cost or 0.0) < 0.005:
            continue  # only public charging
        rows.append({
            "ts": start, "end": end, "ongoing": ongoing, "valid": valid,
            "kwh": kwh or 0.0, "percent": percent or 0.0, "local": local or 0.0, "cost": cost if cost is not None else 0.0,
        })
    return rows


def allocate(
    rows: Sequence[Mapping[str, Any]], windows: Sequence[tuple[float, float]], now: float, settle: float
) -> dict[str, Any]:
    """Lay the rows over the connections (start, end) by overlap in time.

    Returns {"records": start -> {kwh, percent, local_kwh, cost}, "reasons": start -> why a connection has none (not_settled,
    invalid_history, before_history), "weights": per row [(start of a connection, its share)], "split": the number of rows
    that went to more than one connection}."""
    oldest = min((row["ts"] for row in rows), default=None)
    touching: dict[float, list[tuple[int, float]]] = {}
    weights: list[list[tuple[float, float]]] = []
    for index, row in enumerate(rows):
        overlaps = [
            (start, min(row["end"], end + END_TOLERANCE) - max(row["ts"], start - TOLERANCE)) for start, end in windows
        ]
        overlaps = [(start, overlap) for start, overlap in overlaps if overlap > 0.0]
        total = sum(overlap for _, overlap in overlaps)
        shares = [(start, overlap / total) for start, overlap in overlaps] if total > 0.0 else []
        weights.append(shares)
        for start, share in shares:
            touching.setdefault(start, []).append((index, share))
    records: dict[float, dict[str, float]] = {}
    reasons: dict[float, str] = {}
    for start, end in windows:
        inside = touching.get(start, [])
        if now - end < settle or any(rows[index]["ongoing"] for index, _ in inside):
            reasons[start] = "not_settled"  # the last session may still change
        elif any(not rows[index]["valid"] for index, _ in inside):
            reasons[start] = "invalid_history"
        elif oldest is None or start - TOLERANCE < oldest:
            # the history does not reach the whole connection (the entries before the first one may be gone): nothing is
            # known, not "charged nothing" and not a part of the night
            reasons[start] = "before_history"
        else:
            records[start] = {
                "kwh": round(sum(rows[i]["kwh"] * share for i, share in inside), 4),
                "percent": round(sum(rows[i]["percent"] * share for i, share in inside), 4),
                "local_kwh": round(sum(rows[i]["local"] * share for i, share in inside), 4),
                "cost": round(sum(rows[i]["cost"] * share for i, share in inside), 4),
            }
    return {"records": records, "reasons": reasons, "weights": weights, "split": sum(1 for shares in weights if len(shares) > 1)}


def outside_kwh(rows: Sequence[Mapping[str, Any]], weights: Sequence[Sequence[tuple[float, float]]], first: float, last: float) -> float:
    """The home kWh of the valid, finished rows that lie between `first` and `last` but overlap no connection: charging
    that no connection of the comparison holds (the connections are the car's time at home, so it was charged elsewhere
    or the connection is missing)."""
    return sum(
        row["kwh"] for row, shares in zip(rows, weights)
        if not shares and row["valid"] and not row["ongoing"] and first - TOLERANCE <= row["ts"] <= last + END_TOLERANCE
    )
