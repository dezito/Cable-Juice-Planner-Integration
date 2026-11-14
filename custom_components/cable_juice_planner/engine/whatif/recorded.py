"""What CJP itself charged in one connection, from its charging history (pure: plain dicts in, no Home Assistant).

The charging history is combined later: sessions older than a week are merged per day (the first start, the last end, one
type), so the hour in which the energy was charged is lost. The record of a connection is therefore made when the connection
is settled, from the sessions as they were recorded, and stored with the connection (economy database, window["record"]).
Only a connection that was settled within RECORD_BACKFILL_DAYS of the first run of a version with this record can be
recorded from the history (older ones are already combined): the others have no record and are never given one.

The kWh of a session (from the grid and from solar or the home battery, without public charging) are spread evenly over the
session's own start..end and priced per hour at the hourly buy price: one price per session, as CJP books it, and solar at
CJP's own value, would not be on the same footing as the calculations it is compared with. Never filled in: an hour in
which CJP charged and that has no price makes the connection unrecorded (None).
"""
from __future__ import annotations

import datetime
import math
from collections.abc import Callable, Iterable, Mapping
from typing import Any

HOUR = 3600.0
RECORD_BACKFILL_DAYS = 6  # sessions of the last 6 days are not yet combined per day (they are from the 8th day)
RECORD_FIELDS = ("kwh", "percent", "local_kwh", "cost", "booked_cost")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def raw_sessions(history: Mapping[Any, Any], to_ts: Callable[[datetime.datetime], float]) -> list[dict[str, Any]]:
    """The sessions of the charging history as plain rows: start and end in epoch seconds, kWh, battery percent, local
    energy, booked cost, public and ongoing. A row with a figure that is missing or not a finite number is "invalid"."""
    rows: list[dict[str, Any]] = []
    for key, value in history.items():
        if not isinstance(key, datetime.datetime) or not isinstance(value, Mapping):
            continue
        start = to_ts(key)
        ended = value.get("ended")
        ongoing = ended == ">"
        end = start + 60.0  # a session without an end time is one minute at its start
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
        figures = {name: _number(value.get(source)) for name, source in (("kwh", "kWh"), ("percent", "percentage"), ("local", "kWh_from_local_energy"))}
        cost = value.get("cost")
        rows.append({
            "ts": start, "end": end, "ongoing": ongoing,
            "public": "public_charging" in str(value.get("charging_type", "")) or bool(value.get("kWh_from_public")),
            # a missing figure is not a zero: the session is invalid
            "valid": all(number is not None for number in figures.values()),
            "cost": float(cost) if _number(cost) is not None else None,
            **{name: number if number is not None else 0.0 for name, number in figures.items()},
        })
    return rows


def window_record(
    sessions: Iterable[Mapping[str, Any]], start: float, end: float, prices: Mapping[float, float], refund: float = 0.0
) -> tuple[dict[str, float] | None, str | None]:
    """(the record of the connection, or None and why). prices: hourly buy price per UTC hour start (gross, the refund is
    taken off like the replay does)."""
    inside = [s for s in sessions if start - 600 <= s["ts"] <= end and not s["public"]]
    if any(s["ongoing"] for s in inside):
        return None, "not_settled"
    if any(not s["valid"] for s in inside):
        return None, "invalid_record"
    cost = 0.0
    for session in inside:
        span = max(session["end"] - session["ts"], 1.0)
        hour = session["ts"] - session["ts"] % HOUR
        while hour < session["end"]:
            overlap = (min(hour + HOUR, session["end"]) - max(hour, session["ts"])) / span
            if overlap > 0:
                price = prices.get(hour)
                if price is None or not math.isfinite(price):
                    return None, "unpriced_hours"  # an hour CJP charged in has no price: never filled in
                cost += session["kwh"] * overlap * (price - refund)
            hour += HOUR
    booked = [s["cost"] for s in inside]
    return {
        "kwh": round(sum(s["kwh"] for s in inside), 4), "percent": round(sum(s["percent"] for s in inside), 4),
        "local_kwh": round(sum(s["local"] for s in inside), 4), "cost": round(cost, 4),
        "booked_cost": round(sum(booked), 4) if all(c is not None for c in booked) else None,
    }, None
