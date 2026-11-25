"""The rows of the sensors of the integrations that give published prices (today and tomorrow) and no forecast, as (start, price).

Every parser takes the attributes of its sensor and a ``to_local`` function (an ISO timestamp, or a datetime object, with any offset ->
naive local time, like ``price_timestamp``), and returns rows of (start as naive local time, the price per kWh in the currency of the
sensor). A row that is not a dict, has no timestamp or no finite number is skipped. Nothing here knows Home Assistant, so the module is
the same in Cable Juice Planner and BattMind.

The shapes are read from the sources of the integrations (the file is named at each parser); they have not been checked against a
running installation, and the tests use rows built from them:

* ``epex_spot`` (mampfes/ha_epex_spot): attribute ``data``, rows ``{start_time, end_time, price_per_kwh}`` (ISO, local time with offset).
* ``ge_spot`` (enoch85/ge-spot): attributes ``today_interval_prices`` and ``tomorrow_interval_prices``, rows ``{time, value, raw_value}``
  (``time`` is a datetime object in Home Assistant, an ISO string once serialized), ``tomorrow_valid`` and ``data_source``.
* ``octopus_energy`` (BottlecapDave/HomeAssistant-OctopusEnergy): attribute ``rates`` of the event entities of the current and the next
  day, rows ``{start, end, value_inc_vat, ...}`` in pounds (its code divides the pence of the API by 100).

(Nord Pool, the HACS integration, has the rows of Energy Price Forecast: ``raw_today`` / ``raw_tomorrow`` with ``{start, end, value}``,
read by that provider.)
"""
from __future__ import annotations

import datetime
import math
from collections.abc import Callable, Mapping
from typing import Any

Row = tuple[datetime.datetime, float]
ToLocal = Callable[[Any], Any]

# GE-Spot's raw_value is the market spot price, except from sources that only have a price with grid fees and taxes in it
GESPOT_NOT_SPOT = ("stromligning", "strømligning", "amber")
# a rate above this (pounds per kWh) is a price in pence: Agile is capped at 1 pound, so nothing real comes near it
OCTOPUS_MAX_PLAUSIBLE = 5.0


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _dicts(rows: Any) -> list[dict]:
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def epex_rows(attributes: Mapping[str, Any], to_local: ToLocal) -> list[Row]:
    """mampfes/ha_epex_spot: the ``data`` rows of the Market Price sensor (the exchange price) or of the Total Price sensor (the price
    with the user's surcharge and tax), €/kWh. The rows are the market data of the source, also yesterday's."""
    rows: list[Row] = []
    for row in _dicts(attributes.get("data")):
        start, price = to_local(row.get("start_time")), _number(row.get("price_per_kwh"))
        if isinstance(start, datetime.datetime) and price is not None:
            rows.append((start, price))
    return rows


def gespot_rows(attributes: Mapping[str, Any], to_local: ToLocal, field: str) -> list[Row]:
    """enoch85/ge-spot: the rows of today and, when ``tomorrow_valid`` is true (at least 80 % of its intervals), of tomorrow.

    ``field`` is ``value`` (the price with VAT, tariff and tax: the buy price) or ``raw_value`` (the market price)."""
    names = ["today_interval_prices"]
    if attributes.get("tomorrow_valid") is True:
        names.append("tomorrow_interval_prices")
    rows: list[Row] = []
    for name in names:
        for row in _dicts(attributes.get(name)):
            start, price = to_local(row.get("time")), _number(row.get(field))
            if isinstance(start, datetime.datetime) and price is not None:
                rows.append((start, price))
    return rows


def gespot_raw_is_spot(attributes: Mapping[str, Any]) -> bool:
    """Whether ``raw_value`` is the market price: not from a source whose only price has grid fees and taxes in it (Strømligning,
    Amber), and a known source. The source can change during the day (the integration falls back to the next one)."""
    source = str(attributes.get("data_source") or "").lower()
    return bool(source) and not any(name in source for name in GESPOT_NOT_SPOT)


def octopus_rows(attributes: Mapping[str, Any], to_local: ToLocal) -> list[Row]:
    """BottlecapDave/HomeAssistant-OctopusEnergy: the ``rates`` of an event entity of a day (import or export), pounds per kWh with VAT.

    A rate above OCTOPUS_MAX_PLAUSIBLE makes the whole list unusable (it is pence: nothing is guessed)."""
    rows: list[Row] = []
    for row in _dicts(attributes.get("rates")):
        start, price = to_local(row.get("start")), _number(row.get("value_inc_vat"))
        if isinstance(start, datetime.datetime) and price is not None:
            rows.append((start, price))
    if any(price > OCTOPUS_MAX_PLAUSIBLE for _start, price in rows):
        return []
    return rows
