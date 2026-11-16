"""What-if: what CJP charged in the connections from before its own records, estimated from the recorder's hourly statistics.

Pure (plain dicts in, plain dicts out, no Home Assistant). CJP's charging history only reaches back as far as the integration
has been running; the connections before that are estimated from the battery level statistics (derive.py), so the "CJP" side of
the comparison would be empty there. The recorder also has the hourly mean of the charger power, the solar production and the
house consumption (economy_db["power_hours"], the same hours Scenario D uses), which gives what the car took in a connection:

- the energy from the charger: the charger power of each hour of the connection, the part of the hour that lies inside it;
- the solar share of it: what the production left after the rest of the house (solar.hour_energy, one rule for every
  scenario), valued the way CJP values it (sell_value: the sale it replaced, never free);
- the grid share at the hourly grid price the replay uses (net of the refund).

It is an ESTIMATE and says so, in two directions. Hourly means cannot tell a short burst of charging from slow charging, so
the solar share is an upper bound and the cost a lower bound: it leans CJP's way. The home battery is not separated and counts
as grid energy: that leans the other way. A connection is only estimated when every hour it needs has a reading, a price and
(for an hour with solar) a sell price, when the battery levels at its ends are known and agree with the charger energy, and
nothing is ever filled in: a connection that cannot be estimated is left out and counted by the reason.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

from .solar import HOUR, MIN_EV_KWH, hour_energy, sell_value

# the charger energy over what the battery level rise needs from the charger (the loss the replay uses): the statistics lag and
# average, so only a connection whose two figures are apart is refused. Smart and charging at once pay for the battery rise, CJP
# for the charger energy: a band that is too wide would give CJP energy for nothing
RATIO_MIN = 0.9
RATIO_MAX = 1.3
MIN_PERCENT_FOR_RATIO = 3.0  # under this the energy is checked in kWh instead (the percentage is mostly noise)
SMALL_KWH_TOLERANCE = 1.0  # kWh the charger and the battery levels may differ by in a small rise


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _hours(start: float, end: float) -> Iterable[tuple[float, float]]:
    """(the hour, the share of it that lies inside [start, end)) for every hour the span touches."""
    hour = start - start % HOUR
    while hour < end:
        overlap = min(hour + HOUR, end) - max(hour, start)
        if overlap > 0:
            yield hour, overlap / HOUR
        hour += HOUR


def estimate_booking(
    start: float,
    end: float,
    soc_in: float | None,
    soc_out: float | None,
    power_hours: Mapping[float, Any],
    prices: Mapping[float, float],
    sell: Mapping[float, float],
    production: Mapping[float, float],
    assumed_production: float,
    battery_kwh: float,
    loss: float,
) -> dict[str, Any] | str:
    """What CJP charged in the connection [start, end): {"kwh", "local_kwh", "cost", "percent", "assumed_hours"} (kWh from the
    charger, of which solar, what it cost with solar at CJP's own value, the battery percent it added, the hours whose fixed
    production price was assumed), or why there is no estimate: "no_levels", "no_power_history", "no_prices", "no_sell_prices"
    or "implausible".

    prices: the hourly grid price net of the refund (the replay's). loss: the charging loss factor the replay uses."""
    if not (_finite(soc_in) and _finite(soc_out)) or soc_out < soc_in:
        return "no_levels"
    kwh = local = cost = 0.0
    assumed = 0
    for hour, share in _hours(start, end):
        energy = hour_energy(power_hours.get(hour))
        if energy is None:
            return "no_power_history"  # an hour without readings is not an hour without charging
        ev_kwh, solar_kwh = energy[0] * share, energy[1] * share
        if ev_kwh <= 1e-9:
            continue
        price = prices.get(hour)
        if not _finite(price):
            return "no_prices"
        solar_value = 0.0
        if solar_kwh > 1e-9:
            hourly = sell.get(hour)
            if not _finite(hourly):
                return "no_sell_prices"
            fixed = production.get(hour)
            if not _finite(fixed):
                fixed, assumed = assumed_production, assumed + 1
            solar_value = solar_kwh * sell_value(float(hourly), float(fixed))
        kwh += ev_kwh
        local += solar_kwh
        cost += (ev_kwh - solar_kwh) * float(price) + solar_value
    percent = float(soc_out) - float(soc_in)
    expected = percent / 100.0 * battery_kwh * (1.0 + loss)  # what the charger gives for that rise in the battery
    if percent >= MIN_PERCENT_FOR_RATIO:
        if not RATIO_MIN <= kwh / expected <= RATIO_MAX:
            return "implausible"
    elif abs(kwh - expected) > SMALL_KWH_TOLERANCE:
        return "implausible"  # (also: the charger gave energy and the battery level did not rise)
    return {"kwh": kwh, "local_kwh": local, "cost": cost, "percent": percent, "assumed_hours": assumed}


def charging_outside(power_hours: Mapping[float, Any], spans: Iterable[tuple[float, float]], first: float, last: float) -> float:
    """The charger kWh between `first` and `last` that no span covers (an hour a span shares with the connection counts for the
    part that lies outside): charging the estimate would miss, so the CJP side would look cheaper than it was."""
    spans = sorted(spans)
    total = 0.0
    for hour, _share in _hours(first, last):
        energy = hour_energy(power_hours.get(hour))
        if energy is None or energy[0] < MIN_EV_KWH:
            continue
        low, high = max(hour, first), min(hour + HOUR, last)  # the part of the hour between first and last
        covered = sum(max(min(high, e) - max(low, s), 0.0) for s, e in spans)
        total += energy[0] * max((high - low) - min(covered, high - low), 0.0) / HOUR
    return total
