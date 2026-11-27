"""The rows of the sensors of the spot forecast integrations, as (start, spot price, published) for the providers.

Every parser takes the attributes of its sensor and a ``to_local`` function (an ISO timestamp with any offset -> naive local
time, like ``price_timestamp``), and returns rows of (start as naive local time, the WHOLESALE price per kWh in the currency
without VAT, grid fees and margin, whether it is a published market price and not a forecast). A row that is not a dict, has no
timestamp or no finite number is skipped. Nothing here knows Home Assistant, so the module is the same in Cable Juice Planner
and BattMind.

The shapes are read from the sources of the integrations (the file is named at each parser); they have not been checked against
a running installation, and the tests use rows built from them:

* ``spot_forecast`` (cygnusb/ha-spot-forecast): attribute ``prices``, rows ``{time, price, source: market|forecast, ...}``.
* ``elprisprognos`` (AlexAsplund/ha-elprognos): attribute ``forecast``, rows ``{start, value, spot, forecast: bool}``.
* ``wattcast`` (SectorTll/wattcast-homeassistant): ``raw_today`` / ``raw_tomorrow`` rows ``{start, value}`` of the price sensor,
  and on the forecast sensor ``known_until`` and ``forecast`` rows ``{start, p50, ...}``, all in ct/kWh.
"""
from __future__ import annotations

import datetime
import math
from collections.abc import Callable, Mapping
from typing import Any

Row = tuple[datetime.datetime, float, bool]
ToLocal = Callable[[Any], Any]

# Wattcast gives ct/kWh (its README: "ct/kWh without VAT, network fees or supplier margin"); its prices are in euro
WATTCAST_DIVISOR = 100.0


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _dicts(rows: Any) -> list[dict]:
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _hour(start: datetime.datetime) -> datetime.datetime:
    return start.replace(minute=0, second=0, microsecond=0)


def spot_forecast_rows(attributes: Mapping[str, Any], to_local: ToLocal) -> list[Row]:
    """cygnusb/ha-spot-forecast, ``sensor.spot_forecast_<zone>_price``: the ``prices`` rows, EUR/kWh without VAT.

    ``source`` is "market" for a published day-ahead price (today's slots from midnight and tomorrow's once published) and
    "forecast" for the model; anything else is not taken for a published price."""
    rows: list[Row] = []
    for row in _dicts(attributes.get("prices")):
        start, price = to_local(row.get("time")), _number(row.get("price"))
        if isinstance(start, datetime.datetime) and price is not None:
            rows.append((start, price, row.get("source") == "market"))
    return rows


def elprognos_rows(attributes: Mapping[str, Any], to_local: ToLocal) -> list[Row]:
    """AlexAsplund/ha-elprognos, ``sensor.elpris_prognos_<zone>``: the ``forecast`` rows, SEK/kWh.

    ``spot`` is the price without VAT (``value`` has VAT and the user's own additional costs added, so it is not used: the
    basis is ours). ``forecast`` false is a published price (elprisetjustnu.se), true is the model; only an explicit false is
    taken for a published price."""
    rows: list[Row] = []
    for row in _dicts(attributes.get("forecast")):
        start, price = to_local(row.get("start")), _number(row.get("spot"))
        if isinstance(start, datetime.datetime) and price is not None:
            rows.append((start, price, row.get("forecast") is False))
    return rows


def wattcast_rows(attributes: Mapping[str, Any], forecast_attributes: Mapping[str, Any], to_local: ToLocal) -> list[Row]:
    """SectorTll/wattcast-homeassistant: the price sensor's ``raw_today`` / ``raw_tomorrow`` rows (ct/kWh without VAT) and the
    forecast sensor's ``forecast`` rows (``p50``, ct/kWh).

    The price sensor's rows hold the published price where there is one and the forecast's p50 where there is not, with one
    flag for the whole day, so a row is published only when it starts before ``known_until`` of the forecast sensor. Without
    a ``known_until`` nothing is published. The forecast rows give the days after those the price sensor has, an hour that
    the price sensor has is never taken twice."""
    known_until = to_local(forecast_attributes.get("known_until"))
    known = known_until if isinstance(known_until, datetime.datetime) else None
    rows: list[Row] = []
    covered: set[datetime.datetime] = set()
    for attribute in ("raw_today", "raw_tomorrow"):
        for row in _dicts(attributes.get(attribute)):
            start, value = to_local(row.get("start")), _number(row.get("value"))
            if isinstance(start, datetime.datetime) and value is not None:
                rows.append((start, value / WATTCAST_DIVISOR, known is not None and start < known))
                covered.add(_hour(start))
    for row in _dicts(forecast_attributes.get("forecast")):
        start, value = to_local(row.get("start")), _number(row.get("p50"))
        if isinstance(start, datetime.datetime) and value is not None and _hour(start) not in covered:
            rows.append((start, value / WATTCAST_DIVISOR, False))
    return rows
