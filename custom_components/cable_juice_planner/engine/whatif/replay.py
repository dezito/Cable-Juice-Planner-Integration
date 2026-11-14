"""Pure, deterministic EV charging replay for the What-if view (Scenario A).

Nothing here touches Home Assistant, the live engine state, a charger or a car. Every
time is UTC epoch seconds and every price is the grid buy price (incl. fees) per kWh of
the whole UTC hour that starts at the key (sub-hour prices must be averaged first).

Two policies are replayed over identical demand, connection windows, physical limits and
starting SOC; the SOC is carried continuously from window to window:

- "smart": charge to the target before unplugging, in the cheapest hours of each
  connection window that are enough to reach it (CJP's daily charging to the maximum
  recommended SOC). The hours are chosen by simulating the real charging physics, so a
  learned SOC taper is respected;
- "asap": charge from plug-in until the same target is reached (the reference).

Scenario A adds a third calculation, CJP's own recorded charging, which is not replayed (see `recorded`).

This is a hindsight replay: the modelled "smart" knows every price of the window. It is not a forecast and not
what CJP would have planned at the time (the settings and forecasts of the past are not stored).

Rules that keep the comparison honest:

- Energy that does not fit becomes unmet target kWh. It is never priced at an average and
  never clipped away silently. If the two policies do not deliver the same (unmet target
  or unmet driving demand differ), no difference is published: "not comparable".
- An hour without a finite price makes its window unsupported. The replay stops there
  (status "partial") instead of filling the price. Nothing after the break is compared,
  because the SOC would no longer be the same for both policies.
- Driving is a withdrawal from the SOC. A drive that needs more than the battery holds
  is reported as unmet driving demand (the SOC stops at 0).
- Public charging is not an event: a drive is the battery level at departure minus the level
  at the next plug-in, so it is already net of any public charge in between (see inputs.py).
- A model that returns a non-finite or negative loss/power is unavailable, not "ok", and a
  failed energy balance makes the replay unavailable.
- Not modelled, and stated in `notes`: house/site limits, solar, standby drain while
  parked, and a departure buffer.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

REPLAY_MODEL_VERSION = 9
EPS = 1e-9
HOUR = 3600.0
SOC_STEP = 10.0  # % : the SOC power model is learned in bands this wide (engine/soc_power.py)
CONSERVATION_TOLERANCE_KWH = 1e-6
COMPARE_TOLERANCE_KWH = 0.01  # unmet energy that may differ before two policies are "not comparable"
MIN_PRICED_SHARE = 0.5  # the share of the connections that must have every price for an estimate (like BattMind's what-if)
GAP_TOLERANCE = 15 * 60.0  # s : a drive recorded this long before the unplug still belongs after the window

NOTES = (
    "hindsight",
    "site_limits_not_replayed",
    "solar_not_modelled",
    "grid_buy_price_only",
    "standby_not_modelled",
    "departure_buffer_not_modelled",
)

# Scenario A with CJP's own recorded charging: three calculations over the same connections, drives and prices.
RECORDED_NOTES = (
    "recorded_charging",
    "booked_cost",
    "smart_planned_departure",
    "smart_hindsight_prices",
    "energy_balanced_to_cjp",
    "site_limits_not_replayed",
)
ENERGY_GAP_TOLERANCE = 0.05  # battery percent: a smaller difference between the recorded energy and the driving is rounding
CLAMP_TOLERANCE = 0.5  # battery percent of the recorded charging that had to be cut at 100 % before it is said

LossFactor = Callable[[float], float]
"""kWh from the charger = kWh into the battery * (1 + factor(kw))."""
PowerCap = Callable[[int, float, float, float], float]
"""(phases, soc_start, soc_end, base_kw) -> the kW the car accepts across that SOC range."""


class WhatIfCancelled(Exception):
    """A running calculation was stopped (the integration is unloaded; raised by ReplayInput.on_window, it passes through replay())."""


def _no_cap(phases: int, soc_start: float, soc_end: float, base_kw: float) -> float:
    return base_kw


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class _ModelError(Exception):
    """A loss or power model returned something that cannot be charged with."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Window:
    """Connected at the charger from start to end (the unplug time is the departure).

    weekday: the local weekday (Monday = 0) of the departure, for the day selection.
    """

    start: float
    end: float
    weekday: int | None = None
    derived: bool = False  # estimated (from the battery level statistics, see derive.py, or modelled from the work plan), not measured
    modelled: bool = False  # of those: modelled from the work plan (see modelled.py)
    deadline: float | None = None
    """The planned departure inside the connection (the work plan, or 07:00 with Fill up): the modelled "smart" charges
    before it, like a charger that is told a departure time and does not know when the car really leaves."""


@dataclass(frozen=True)
class Recorded:
    """What CJP itself charged in one connection (its charging history), for the "CJP" column of scenario A.

    cost: what CJP booked for it: one price per session, with solar and the home battery at CJP's own value. Smart and
    charging at once are grid only, so part of a difference to them can be the solar (local_kwh says how much)."""

    kwh: float  # from the charger: grid + local energy, without public charging
    percent: float  # battery percent charged (CJP's recorded percentage)
    cost: float
    local_kwh: float = 0.0


@dataclass(frozen=True)
class Event:
    """Something that moves the SOC while the car is away from the charger.

    kind "drive" (the only kind): soc_percent is the SOC used (>= 0).
    """

    ts: float
    kind: str
    soc_percent: float = 0.0


@dataclass(frozen=True)
class ReplayInput:
    windows: tuple[Window, ...]
    events: tuple[Event, ...]
    prices: Mapping[float, float]
    battery_kwh: float
    max_kw: float
    phases: int
    start_soc: float | None
    target_soc: float
    loss_factor: LossFactor
    power_cap: PowerCap = _no_cap
    provenance: Mapping[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()  # data the adapter had to leave out or could not verify
    estimated_hours: frozenset[float] = frozenset()  # (internal) hours whose price was estimated, see _with_estimated_prices
    smart_weekdays: frozenset[int] | None = None
    """None: smart on every window. Otherwise only on windows whose departure weekday is in
    the set; the other windows are charged immediately by both policies, so the result differs
    only on the selected days."""
    arrival_reserve: float | None = None
    """Scenario A: the level the car should come home with (CJP's minimum daily level): the goal CJP's recorded charging is judged by."""
    on_window: Callable[[int, int], None] | None = None
    """Called before each connection of the planned policy with (connections done, connections in all). It reports the
    progress and may raise to stop the replay (a stop is not an error of the data, so it is not caught here)."""
    recorded: Mapping[float, Recorded] | None = None
    """Scenario A: what CJP charged in each connection (keyed by the connection's start). With it the replay has three
    calculations: CJP's own recorded charging, "smart" and "asap" (replayed from the level the car comes home with)."""
    end_soc: float | None = None
    """The battery level at the last connection's unplug, read like the starting level. With it the energy CJP bought
    that no recorded drive explains (small trips, standby drain) is added as driving in all three calculations, so they
    deliver the same energy. Without it the three are not balanced and no adjusted difference is given."""


@dataclass
class WindowResult:
    start: float
    end: float
    soc_in: float
    soc_out: float
    charged_kwh: float = 0.0  # into the battery
    grid_kwh: float = 0.0  # from the charger
    cost: float = 0.0
    unmet_target_kwh: float = 0.0  # battery kWh missing to the target at unplug
    drive_shortfall_kwh: float = 0.0  # driving after this window that the battery could not cover
    estimated: bool = False  # a price in this connection was estimated (see _with_estimated_prices)
    calibrated: bool = False  # the level before this connection was unknown: it only brings the car to a common level
    derived: bool = False  # the connection itself was estimated (from the battery level statistics or modelled from the work plan)
    modelled: bool = False  # of those: modelled from the work plan

    @property
    def loss_kwh(self) -> float:
        return self.grid_kwh - self.charged_kwh


@dataclass
class PolicyResult:
    name: str
    windows: list[WindowResult] = field(default_factory=list)
    start_soc: float = 0.0
    final_soc: float = 0.0
    end_soc: float = 0.0
    lowest_soc: float = 0.0
    driven_percent: float = 0.0
    calibrated: bool = False  # the starting level was unknown (see _run)

    @property
    def estimated_cost(self) -> float:
        return sum(w.cost for w in self.windows if w.estimated)

    @property
    def estimated_windows(self) -> int:
        return sum(1 for w in self.windows if w.estimated)

    @property
    def charged_kwh(self) -> float:
        return sum(w.charged_kwh for w in self.windows)

    @property
    def grid_kwh(self) -> float:
        return sum(w.grid_kwh for w in self.windows)

    @property
    def loss_kwh(self) -> float:
        return sum(w.loss_kwh for w in self.windows)

    @property
    def cost(self) -> float:
        return sum(w.cost for w in self.windows)

    @property
    def unmet_target_kwh(self) -> float:
        return sum(w.unmet_target_kwh for w in self.windows)

    @property
    def unmet_driving_kwh(self) -> float:
        return sum(w.drive_shortfall_kwh for w in self.windows)

    @property
    def targets_met(self) -> int:
        return sum(1 for w in self.windows if w.unmet_target_kwh <= 1e-6)


@dataclass
class ReplayResult:
    status: str  # ok | partial | unavailable
    reason: str | None
    model_version: int = REPLAY_MODEL_VERSION
    smart: PolicyResult | None = None
    asap: PolicyResult | None = None
    windows_total: int = 0
    windows_replayed: int = 0
    stopped_at: float | None = None
    average_price: float | None = None
    max_conservation_error_kwh: float = 0.0
    notes: tuple[str, ...] = NOTES
    warnings: tuple[str, ...] = ()
    provenance: Mapping[str, str] = field(default_factory=dict)
    ignored_events: int = 0  # drives that were not replayed (see warnings)
    comparable: bool = False
    comparable_reason: str | None = None
    difference: dict[str, float] | None = None  # smart minus asap; negative cost is better
    method: str = "strict"  # "strict": measured data only; "estimated": some prices or the starting level were estimated
    estimated_windows: int = 0
    price_estimate: float | None = None  # the price used for the hours without one (the average of the priced hours)
    calibrated_start: bool = False
    cjp: PolicyResult | None = None  # scenario A: what CJP itself charged (smart and asap are replayed)
    differences: dict[str, dict[str, float | None]] | None = None  # scenario A: cjp_asap, cjp_smart and smart_asap (first minus second)
    energy: dict[str, float | None] | None = None  # scenario A: the energy balance (gap, charged and driven percent, end level)
    unit_prices: dict[str, Any] | None = None  # scenario A: the average price per kWh of the three and CJP's kWh at the others' price


@dataclass
class _Slot:
    hour: float
    t0: float
    t1: float
    price: float | None

    @property
    def hours(self) -> float:
        return (self.t1 - self.t0) / HOUR


def _slots(window: Window, prices: Mapping[float, float]) -> list[_Slot]:
    slots: list[_Slot] = []
    hour = window.start - window.start % HOUR
    while hour < window.end:
        t0, t1 = max(hour, window.start), min(hour + HOUR, window.end)
        if t1 > t0:
            price = prices.get(hour)
            slots.append(_Slot(hour, t0, t1, float(price) if _finite(price) else None))
        hour += HOUR
    return slots


def _validate(data: ReplayInput) -> str | None:
    if not (_finite(data.battery_kwh) and data.battery_kwh > 0):
        return "no_battery_capacity"
    if not (_finite(data.max_kw) and data.max_kw > 0) or data.phases not in (1, 2, 3):
        return "no_charging_power"
    if not data.windows:
        return "no_windows"  # before the start level: nothing to start a level for
    if data.start_soc is not None and (not _finite(data.start_soc) or not 0.0 <= data.start_soc <= 100.0):
        return "no_start_soc"
    if not _finite(data.target_soc) or not 0.0 < data.target_soc <= 100.0:
        return "invalid_target"
    if data.smart_weekdays is not None and any(not isinstance(d, int) or not 0 <= d <= 6 for d in data.smart_weekdays):
        return "invalid_day_selection"
    if data.smart_weekdays is not None and any(w.weekday is None for w in data.windows):
        return "invalid_day_selection"  # a day selection needs every window's local weekday
    ordered = sorted(data.windows, key=lambda w: w.start)
    for window in ordered:
        if not (_finite(window.start) and _finite(window.end)) or window.end <= window.start:
            return "invalid_window"
    for first, second in zip(ordered, ordered[1:]):
        if second.start < first.end:
            return "overlapping_windows"
    if any(not _finite(key) or key % HOUR != 0 for key in data.prices):
        return "prices_not_hourly"
    for event in data.events:
        if not _finite(event.ts) or event.kind != "drive":
            return "invalid_event"
        if not (_finite(event.soc_percent) and event.soc_percent >= 0):
            return "invalid_event"
    return None


def _with_estimated_prices(data: ReplayInput, windows: list[Window]) -> tuple[ReplayInput, float | None, bool]:
    """Give the hours without a finite price the time-weighted average of the priced hours (the same flat price for
    every policy, so no policy gets energy for free and no hour is cheaper than another). Returns
    (data with the prices filled, that price, whether every connection was fully priced to start with).

    The replay then runs on all connections; the ones that needed an estimate are flagged and counted. The caller
    decides if enough connections had measured prices (MIN_PRICED_SHARE) for the result to be given at all."""
    total = weight = 0.0
    missing: set[float] = set()
    fully_priced = 0
    for window in windows:
        slots = _slots(window, data.prices)
        if slots and all(slot.price is not None for slot in slots):
            fully_priced += 1
        else:
            # a connection with even one hour missing is estimated as a whole: at one flat price smart has nothing
            # to choose between, so the estimate can not make smart look cheaper than immediate charging
            missing.update(slot.hour for slot in slots)
        for slot in slots:
            if slot.price is not None:
                total += slot.price * slot.hours
                weight += slot.hours
    if not missing:
        return data, None, True
    if weight <= 0.0:
        return data, None, False
    fill = total / weight
    filled = {**data.prices, **dict.fromkeys(missing, fill)}
    return replace(data, prices=filled, estimated_hours=frozenset(missing)), fill, fully_priced == len(windows)


def _enough_priced(data: ReplayInput, windows: list[Window]) -> bool:
    """At least MIN_PRICED_SHARE of the connections have every price (measured)."""
    full = sum(1 for w in windows if (s := _slots(w, data.prices)) and all(x.price is not None for x in s))
    return bool(windows) and full / len(windows) >= MIN_PRICED_SHARE


def _loss(data: ReplayInput, kw: float) -> float:
    factor = data.loss_factor(kw)
    if not _finite(factor) or factor < 0.0:
        raise _ModelError("invalid_loss_model")
    return float(factor)


def _cap(data: ReplayInput, soc_start: float, soc_end: float) -> float:
    cap = data.power_cap(data.phases, soc_start, soc_end, data.max_kw)
    if not _finite(cap) or cap < 0.0:
        raise _ModelError("invalid_power_model")
    return min(float(cap), data.max_kw)


def _charge_slot(data: ReplayInput, soc: float, target: float, hours: float) -> tuple[float, float, float]:
    """Charge for `hours` from soc towards target: (new soc, battery kWh, charger kWh).

    The charging power is the learned cap of the SOC band being charged, so a taper is
    followed band by band. A band that accepts no power ends the slot.
    """
    battery_total = grid_total = 0.0
    left = hours
    while left > EPS and soc < target - EPS:
        boundary = min(target, (math.floor(soc / SOC_STEP + EPS) + 1) * SOC_STEP)
        kw = _cap(data, soc, boundary)
        if kw <= 0.0:
            break
        factor = _loss(data, kw)
        to_boundary = (boundary - soc) / 100.0 * data.battery_kwh
        possible = kw * left / (1.0 + factor)
        if possible >= to_boundary - EPS:
            battery = to_boundary
            left -= battery * (1.0 + factor) / kw
            soc = boundary
        else:
            battery = possible
            soc += battery / data.battery_kwh * 100.0
            left = 0.0
        battery_total += battery
        grid_total += battery * (1.0 + factor)
    return soc, battery_total, grid_total


def _simulate(data: ReplayInput, slots: list[_Slot], hours: Mapping[int, float], soc: float, target: float) -> list[tuple[int, float, float]]:
    """Charge chronologically, at most hours[index] in each chosen slot: [(slot index, battery kWh, charger kWh)]."""
    rows = []
    for index, slot in enumerate(slots):
        usable = hours.get(index, 0.0)
        if usable <= EPS:
            continue
        soc, battery, grid = _charge_slot(data, soc, target, min(usable, slot.hours))
        rows.append((index, battery, grid))
        if soc >= target - EPS:
            break
    return rows


def _final_soc(data: ReplayInput, soc: float, rows: list[tuple[int, float, float]]) -> float:
    return soc + sum(battery for _, battery, _ in rows) / data.battery_kwh * 100.0


def _reaches(data: ReplayInput, slots: list[_Slot], hours: Mapping[int, float], soc_in: float, target: float) -> bool:
    return _final_soc(data, soc_in, _simulate(data, slots, hours, soc_in, target)) >= target - 1e-9


def _plan_smart(data: ReplayInput, slots: list[_Slot], soc_in: float, target: float) -> dict[int, float]:
    """The cheapest hours that, simulated with the real physics, reach the target: {slot: hours used}.

    1. Cheapest first (earlier first on a tie) until the chronological simulation reaches the target.
    2. Then the most expensive chosen hour is used as little as possible, then the next, so the
       energy comes from the cheap hours and only the rest from the dearer one.
    If the target cannot be reached at all, every hour is used, which is what charging immediately does.
    """
    if soc_in >= target - EPS:
        return {}
    everything = {i: slot.hours for i, slot in enumerate(slots)}
    if not _reaches(data, slots, everything, soc_in, target):
        return everything  # (unreachable: nothing to choose between)
    chosen: dict[int, float] = {}
    for index in sorted(range(len(slots)), key=lambda i: (slots[i].price, slots[i].t0)):
        chosen[index] = slots[index].hours
        if _reaches(data, slots, chosen, soc_in, target):
            break
    for index in sorted(chosen, key=lambda i: (-slots[i].price, -slots[i].t0)):  # type: ignore[operator]
        full = chosen[index]
        chosen[index] = 0.0
        if _reaches(data, slots, chosen, soc_in, target):
            continue  # not needed at all
        low, high = 0.0, full
        for _ in range(50):  # the least time in this hour that still reaches the target
            middle = (low + high) / 2.0
            chosen[index] = middle
            if _reaches(data, slots, chosen, soc_in, target):
                high = middle
            else:
                low = middle
        chosen[index] = high
    return {index: used for index, used in chosen.items() if used > EPS}


def _apply_events(
    data: ReplayInput, soc: float, events: list[Event], result: PolicyResult, last: WindowResult
) -> float:
    """Apply the drives between two windows. Returns the soc."""
    for event in events:
        used = event.soc_percent
        available = max(soc, 0.0)
        if used > available + EPS:
            last.drive_shortfall_kwh += (used - available) / 100.0 * data.battery_kwh
            soc = 0.0
        else:
            soc -= used
        result.driven_percent += used
        result.lowest_soc = min(result.lowest_soc, soc)
    return soc


def _run(
    data: ReplayInput, name: str, windows: list[Window], targets: list[float] | None = None, next_start: float | None = None,
    calibrate_to: float | None = None,
) -> tuple[PolicyResult, str | None, float | None, int]:
    """Replay one policy. Returns (result, stop reason, stop time, windows replayed).

    The policy named "asap" charges from plug-in; every other policy charges in the cheapest hours.
    targets: the battery level to leave each window with (default: data.target_soc for all).
    next_start: the start of the connection after the last one in `windows`; when given, the drives
    in the gap before it are applied too (and policy.end_soc is the level the car comes back with).
    calibrate_to: used when the starting level is unknown. The first connection then only brings the car to this
    level (nothing is charged or counted in it) and the replay runs normally from the drives after it.
    """
    calibrating = data.start_soc is None
    soc = float(calibrate_to if calibrating else data.start_soc)
    result = PolicyResult(name=name, start_soc=soc, lowest_soc=soc, calibrated=calibrating)
    events = sorted(data.events, key=lambda e: e.ts)
    taken: set[int] = set()  # a drive belongs to one gap even when two gaps overlap (very short connections)
    replayed = 0
    for index, window in enumerate(windows):
        if data.on_window is not None and data.recorded is not None and targets is None and name != "asap":
            data.on_window(index, len(windows))
        slots = _slots(window, data.prices)
        if not slots or any(slot.price is None for slot in slots):
            return _finish(result), "missing_prices", window.start, replayed
        row = WindowResult(
            window.start, window.end, soc, soc,
            estimated=not (calibrating and index == 0) and any(slot.hour in data.estimated_hours for slot in slots),
            calibrated=calibrating and index == 0,
            derived=window.derived,
            modelled=window.modelled,
        )
        target = targets[index] if targets is not None else data.target_soc
        if not row.calibrated:
            immediate = name == "asap" or (data.smart_weekdays is not None and window.weekday not in data.smart_weekdays)
            usable = slots if immediate else _before_deadline(slots, window)
            chosen = {i: slot.hours for i, slot in enumerate(usable)} if immediate else _plan_smart(data, usable, soc, target)
            for slot_index, battery, grid in _simulate(data, usable, chosen, soc, target):
                soc += battery / data.battery_kwh * 100.0
                row.charged_kwh += battery
                row.grid_kwh += grid
                row.cost += grid * usable[slot_index].price  # type: ignore[operator]
            row.soc_out = soc
            shortfall = max(target - soc, 0.0) / 100.0 * data.battery_kwh
            row.unmet_target_kwh = shortfall if shortfall > 1e-6 else 0.0
        result.windows.append(row)
        replayed += 1
        nxt = windows[index + 1].start if index + 1 < len(windows) else next_start
        if nxt is not None:
            between = []
            for position, event in enumerate(events):
                if position not in taken and _in_gap(event, window, nxt):
                    taken.add(position)
                    between.append(event)
            soc = _apply_events(data, soc, between, result, row)
        result.lowest_soc = min(result.lowest_soc, soc)
    return _finish(result, soc), None, None, replayed


def _before_deadline(slots: list[_Slot], window: Window) -> list[_Slot]:
    """The hours a charger that was told the planned departure may use: the ones before it (all of them without a
    departure, or when the car came after it). The real unplug is not known to such a charger."""
    deadline = window.deadline
    if deadline is None or deadline <= window.start:
        return slots
    usable = [slot for slot in slots if slot.t0 < deadline]
    return [replace(slot, t1=min(slot.t1, deadline)) for slot in usable] or slots


def _run_recorded(data: ReplayInput, windows: list[Window], calibrate_to: float | None = None) -> tuple[PolicyResult, float]:
    """What CJP itself charged, connection after connection, with the same drives as the other calculations.

    The level is the recorded start level plus the recorded percent charged minus the drives: nothing is replayed.
    Without a start level (data.start_soc None) the first connection only brings the car to calibrate_to, like in the other
    calculations: nothing CJP charged in it is counted.
    Returns (result, battery percent that had to be cut at 100 %, which says the record does not fit the drives)."""
    assert data.recorded is not None
    calibrating = data.start_soc is None
    assert not calibrating or calibrate_to is not None
    soc = float(calibrate_to if calibrating else data.start_soc)  # type: ignore[arg-type]
    result = PolicyResult(name="cjp", start_soc=soc, lowest_soc=soc, calibrated=calibrating)
    events = sorted(data.events, key=lambda e: e.ts)
    taken: set[int] = set()
    cut = 0.0
    for index, window in enumerate(windows):
        recorded = data.recorded[window.start]
        row = WindowResult(
            window.start, window.end, soc, soc, calibrated=calibrating and index == 0, derived=window.derived, modelled=window.modelled
        )
        if not row.calibrated:
            soc += recorded.percent
            if soc > 100.0:
                cut += soc - 100.0
                soc = 100.0
            row.charged_kwh = recorded.percent / 100.0 * data.battery_kwh
            row.grid_kwh = recorded.kwh
            row.cost = recorded.cost
            row.soc_out = soc
        result.windows.append(row)
        if index + 1 < len(windows):
            between = []
            for position, event in enumerate(events):
                if position not in taken and _in_gap(event, window, windows[index + 1].start):
                    taken.add(position)
                    between.append(event)
            soc = _apply_events(data, soc, between, result, row)
            if data.arrival_reserve is not None:
                missing = max(data.arrival_reserve - soc, 0.0) / 100.0 * data.battery_kwh
                row.unmet_target_kwh = missing if missing > 1e-6 else 0.0
        result.lowest_soc = min(result.lowest_soc, soc)
    return _finish(result, soc), cut


def _in_gap(event: Event, window: Window, next_start: float) -> bool:
    return window.end - GAP_TOLERANCE <= event.ts < next_start


def _finish(result: PolicyResult, end_soc: float | None = None) -> PolicyResult:
    """final_soc: the level at the last unplug. end_soc: the level after the drives that followed it (when applied)."""
    result.final_soc = result.windows[-1].soc_out if result.windows else result.start_soc
    result.end_soc = end_soc if end_soc is not None else result.final_soc
    return result


def _conservation(data: ReplayInput, policy: PolicyResult) -> float:
    """The biggest energy-balance deviation in kWh; non-finite numbers are an infinite deviation."""
    values = [policy.cost, policy.grid_kwh, policy.charged_kwh, policy.final_soc, policy.lowest_soc]
    if not all(math.isfinite(v) for v in values):
        return math.inf
    worst = 0.0
    for row in policy.windows:
        delta = (row.soc_out - row.soc_in) / 100.0 * data.battery_kwh
        worst = max(worst, abs(delta - row.charged_kwh), max(-row.loss_kwh, 0.0))
    # over the whole replay: what was charged - driven (minus what could not be driven)
    driven_kwh = policy.driven_percent / 100.0 * data.battery_kwh - policy.unmet_driving_kwh
    expected = policy.start_soc / 100.0 * data.battery_kwh + policy.charged_kwh - driven_kwh
    if policy.windows:
        # the SOC after the last applied drive (the last unplug when no drive after it was applied)
        actual = policy.end_soc / 100.0 * data.battery_kwh
        worst = max(worst, abs(expected - actual))
    return worst


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
        return _replay(data, ordered, provenance)
    except _ModelError as err:
        return ReplayResult(status="unavailable", reason=err.reason, windows_total=len(ordered), provenance=provenance)


def _replay(data: ReplayInput, ordered: list[Window], provenance: dict[str, str]) -> ReplayResult:
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
    inside = sum(1 for e in data.events if any(w.start <= e.ts < w.end - GAP_TOLERANCE for w in compared))
    estimated_windows = smart.estimated_windows
    calibrated = smart.calibrated
    warnings = (
        *data.warnings, *(("events_inside_windows",) if inside else ()),
        *(("prices_estimated_windows",) if estimated_windows else ()), *(("start_level_unknown_calibrated",) if calibrated else ()),
    )

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


def _difference(
    data: ReplayInput, first: PolicyResult, second: PolicyResult, average: float, price: float | None = None, adjust: bool = True
) -> dict[str, float | None]:
    """first minus second. The energy left in the battery at the end is valued explicitly at `price`
    (default: the period's time-weighted buy price) and the loss actually seen when charging.
    adjust False: no value is put on the end levels (they are not known to be comparable): no adjusted difference."""
    grid, charged = first.grid_kwh + second.grid_kwh, first.charged_kwh + second.charged_kwh
    ratio = grid / charged if charged > EPS else 1.0 + _loss(data, data.max_kw)
    terminal_kwh = (first.final_soc - second.final_soc) / 100.0 * data.battery_kwh
    adjustment = terminal_kwh * ratio * (average if price is None else price)
    return {
        "cost": first.cost - second.cost,
        "measured_cost": (first.cost - first.estimated_cost) - (second.cost - second.estimated_cost),
        "terminal_energy_kwh": terminal_kwh,
        "terminal_adjustment": adjustment if adjust else None,
        "adjusted_cost": first.cost - second.cost - adjustment if adjust else None,
        "grid_kwh": first.grid_kwh - second.grid_kwh,
        "loss_kwh": first.loss_kwh - second.loss_kwh,
    }


def _unit_prices(cjp: PolicyResult, smart: PolicyResult, asap: PolicyResult) -> dict[str, Any]:
    """The average price per kWh from the charger of each calculation, and what CJP's own kWh would have cost at the price per
    kWh of Smart and of charging at once.

    It does not depend on how much energy each bought, so it is the figure that can be compared when the three do not
    deliver the same (one could not cover the driving, or the end levels differ). It says when the energy was bought, not
    how much: a calculation that tops up a lot in the cheap hours has a low price per kWh. A calculation that bought no
    energy has no price (None)."""
    unit = {name: (policy.cost / policy.grid_kwh if policy.grid_kwh > EPS else None) for name, policy in (("cjp", cjp), ("smart", smart), ("asap", asap))}
    normalised = {
        name: (unit[name] * cjp.grid_kwh if unit[name] is not None and cjp.grid_kwh > EPS else None) for name in ("smart", "asap")
    }  # CJP bought nothing: there is nothing to price
    return {
        "cjp": unit["cjp"], "smart": unit["smart"], "asap": unit["asap"], "cjp_kwh": cjp.grid_kwh,
        "cjp_cost": cjp.cost,
        # CJP's kWh at the others' price per kWh, and CJP's actual cost minus that (negative: CJP paid less)
        "smart_at_cjp_kwh": normalised["smart"], "asap_at_cjp_kwh": normalised["asap"],
        "cjp_vs_smart": cjp.cost - normalised["smart"] if normalised["smart"] is not None else None,
        "cjp_vs_asap": cjp.cost - normalised["asap"] if normalised["asap"] is not None else None,
    }


def _energy_gap(data: ReplayInput, windows: list[Window], calibrate_to: float | None = None) -> tuple[float | None, float]:
    """The battery percent CJP charged that the recorded drives and the measured end level do not explain.

    Over a long time the energy CJP buys is the energy the car uses: the drives are only the accepted ones (long and large
    enough), so small trips and standby drain are missing, and the replayed calculations would not need that energy.
    Returns (gap or None when the end level is not known, the percent charged by CJP)."""
    assert data.recorded is not None
    calibrating = data.start_soc is None
    start = float(calibrate_to if calibrating else data.start_soc)  # type: ignore[arg-type]
    charged = sum(data.recorded[w.start].percent for w in (windows[1:] if calibrating else windows))
    if data.end_soc is None:
        return None, charged
    first, _ = _run_recorded(data, windows, calibrate_to)
    # the level the record gives at the last unplug without the 100 % cut: start + charged - the drives that were applied
    raw_end = start + charged - first.driven_percent
    return raw_end - float(data.end_soc), charged


def _replay_recorded(
    data: ReplayInput, ordered: list[Window], provenance: dict[str, str], measured: ReplayInput, price_fill: float | None
) -> ReplayResult:
    """Scenario A: CJP's own recorded charging against "smart" and "asap", replayed from the level the car comes home with."""
    assert data.recorded is not None
    if data.start_soc is None and not any(window.modelled for window in ordered):
        # only connections modelled from the work plan have no level to start from (the first one then brings all three to a
        # common level): for the others an unknown level is not made up
        return ReplayResult(status="unavailable", reason="no_start_soc", windows_total=len(ordered), provenance=provenance)
    calibrate_to = measured.target_soc  # an unknown start level: the first connection brings all three to this level
    if any(window.start not in data.recorded for window in ordered):
        return ReplayResult(status="unavailable", reason="no_recorded_charging", windows_total=len(ordered), provenance=provenance)
    gap, charged = _energy_gap(data, ordered, calibrate_to)
    warnings: list[str] = list(data.warnings)
    energy_gap = gap  # None: the level at the end is not known
    if gap is None:
        warnings.append("energy_not_balanced")
    elif gap > ENERGY_GAP_TOLERANCE and len(ordered) > 1:
        # what CJP bought and no recorded drive explains is driving in all three: spread over the gaps between connections
        share = gap / (len(ordered) - 1)
        data = replace(data, events=(*data.events, *(Event(w.end + 1.0, "drive", share) for w in ordered[:-1])))
    elif gap > ENERGY_GAP_TOLERANCE:
        warnings.append("energy_not_balanced")  # one connection only: there is no gap between connections to put the driving in
    elif gap < -ENERGY_GAP_TOLERANCE:
        warnings.append("recorded_energy_short")  # the car holds more than the record and the drives explain: charging CJP did not record
    smart, smart_stop, smart_at, smart_n = _run(data, "smart", ordered, calibrate_to=calibrate_to)
    asap, asap_stop, asap_at, asap_n = _run(data, "asap", ordered, calibrate_to=calibrate_to)
    count = min(smart_n, asap_n)
    stop, stopped = (smart_stop, smart_at) if smart_n <= asap_n else (asap_stop, asap_at)
    if count == 0:
        return ReplayResult(status="unavailable", reason=stop or "no_windows", windows_total=len(ordered), provenance=provenance)
    if count < len(ordered):
        smart, _, _, _ = _run(data, "smart", ordered[:count], calibrate_to=calibrate_to)
        asap, _, _, _ = _run(data, "asap", ordered[:count], calibrate_to=calibrate_to)
    compared = ordered[:count]
    cjp, cut = _run_recorded(data, compared, calibrate_to)
    # The car was driven: what CJP charged covered the driving by definition. A shortfall in CJP's reconstructed level path
    # (start level + what the history says CJP charged - the drives) is the path, not the car: charging placed in a later
    # connection than it belongs to, or a start level that is too low. It is said, and kept out of what is compared.
    path_short = cjp.unmet_driving_kwh
    for row in cjp.windows:
        row.drive_shortfall_kwh = 0.0
    weighted = [(slot.price, slot.hours) for w in compared for slot in _slots(w, measured.prices) if slot.price is not None]
    hours = sum(h for _, h in weighted)
    average = sum(p * h for p, h in weighted) / hours if hours > 0 else None
    applied = sum(1 for e in data.events if any(_in_gap(e, w, ordered[i + 1].start) for i, w in enumerate(compared[:-1])))
    inside = sum(1 for e in data.events if any(w.start <= e.ts < w.end - GAP_TOLERANCE for w in compared))
    estimated_windows = smart.estimated_windows
    if inside:
        warnings.append("events_inside_windows")
    if estimated_windows:
        warnings.append("prices_estimated_windows")
        # Smart and charging at once get one flat price in such a connection and cannot choose cheap hours, CJP keeps what it booked
        warnings.append("recorded_prices_estimated_lean_cjp")
    if path_short > COMPARE_TOLERANCE_KWH:
        warnings.append("recorded_path_short")
    if cut / max(charged, EPS) * 100.0 > CLAMP_TOLERANCE or cut > CLAMP_TOLERANCE:
        warnings.append("recorded_path_cut")  # the record charged past 100 %: the drives and the record do not fit together
    counted = compared[1:] if cjp.calibrated else compared  # (the connection that only calibrates is not counted)
    local = sum(data.recorded[w.start].local_kwh for w in counted)
    total_kwh = sum(data.recorded[w.start].kwh for w in counted)
    if cjp.calibrated:
        warnings.append("start_level_unknown_calibrated")
    if total_kwh > EPS and local / total_kwh >= 0.05:
        warnings.append("recorded_solar_share")  # CJP's cost values solar / home battery energy at CJP's own value, the others are grid only
    error = max(_conservation(data, smart), _conservation(data, asap))
    if not error <= CONSERVATION_TOLERANCE_KWH:
        return ReplayResult(
            status="unavailable", reason="energy_conservation", windows_total=len(ordered), provenance=provenance,
            max_conservation_error_kwh=error,
        )
    complete = count == len(ordered)
    result = ReplayResult(
        status="ok" if complete else "partial", reason=None if complete else stop, smart=smart, asap=asap, cjp=cjp,
        windows_total=len(ordered), windows_replayed=count, stopped_at=None if complete else stopped, average_price=average,
        max_conservation_error_kwh=error, warnings=tuple(warnings), provenance=provenance,
        ignored_events=len(data.events) - applied, method="estimated" if estimated_windows or cjp.calibrated else "strict",
        estimated_windows=estimated_windows, price_estimate=price_fill, notes=RECORDED_NOTES, calibrated_start=cjp.calibrated,
        energy={
            "gap_percent": energy_gap, "charged_percent": charged, "driven_percent": cjp.driven_percent,
            "end_soc": data.end_soc, "start_soc": cjp.start_soc, "cut_percent": cut,
            "local_kwh": local, "kwh": total_kwh, "path_shortfall_kwh": path_short,
        },
    )
    result.unit_prices = _unit_prices(cjp, smart, asap)
    # the three must deliver the same driving: CJP's own is covered by definition (the car was driven)
    unmet = [0.0, smart.unmet_driving_kwh, asap.unmet_driving_kwh]  # (CJP: the car was driven, whatever the reconstructed path says)
    if max(unmet) - min(unmet) > COMPARE_TOLERANCE_KWH:
        result.comparable_reason = "unmet_driving_differs"
    elif average is None:
        result.comparable_reason = "no_prices"
    else:
        result.comparable = True
        # Without the end level, with charging CJP did not record (the energy is missing: it would be valued at the holder's
        # price, not at what it cost) or with a gap that could not be put anywhere, the energy bought is not known to be the
        # same, and no value is put on the end levels.
        # (a reconstructed level of CJP that had to be held at 0 % ends higher than the car did: its end level is not known to
        # be comparable either)
        balanced = "energy_not_balanced" not in warnings and "recorded_energy_short" not in warnings and path_short <= COMPARE_TOLERANCE_KWH

        def pair(first: PolicyResult, second: PolicyResult) -> dict[str, float | None]:
            holder = first if first.final_soc >= second.final_soc else second
            paid = holder.cost / holder.grid_kwh if holder.grid_kwh > EPS else average
            return _difference(data, first, second, average, paid, adjust=balanced)

        result.differences = {"cjp_asap": pair(cjp, asap), "cjp_smart": pair(cjp, smart), "smart_asap": pair(smart, asap)}
        result.difference = result.differences["cjp_asap"]
    return result


# ---------------------------------------------------------------------------- #
# Scenario B: two sets of CJP settings replayed over the same history
# ---------------------------------------------------------------------------- #
#
# A deliberately small, modelled policy (it is not CJP's planner, only its main planning rule):
# at every plug-in the car is charged, in the cheapest hours
# of the connection, to the level that covers the drive that follows plus the daily homecoming
# reserve, but never above the maximum recommended level (a drive that needs more than that level
# is still covered, as CJP lifts the limit for a trip). The next drive is the recorded one, so this
# is a hindsight replay with perfect knowledge of the demand. It is what the two settings do in
# CJP's planning rule "needed for work + minimum daily level, capped at the maximum"; the planner's
# other rules (cheap-hour top-ups, solar, forced charging below the reserve) are not modelled.

SETTINGS_MODEL_VERSION = 2

SETTINGS_NOTES = (
    "hindsight",
    "modelled_policy",
    "demand_from_recorded_drives",
    "settings_history_not_stored",
    "last_connection_left_out",
    "extra_top_ups_not_modelled",
    "forced_low_battery_charging_not_modelled",
    "site_limits_not_replayed",
    "solar_not_modelled",
    "grid_buy_price_only",
    "standby_not_modelled",
)


@dataclass(frozen=True)
class CjpSettings:
    """The two CJP settings that shape the daily charging level, in battery percent."""

    reserve: float  # min_daily_battery_level: the level the car should still have when it comes home
    maximum: float  # max_recommended_charge_limit_battery_level


def settings_target(need: float, settings: CjpSettings) -> float:
    """The level to leave a connection with: the next drive's need plus the reserve, capped at the maximum.

    A need above the maximum is still covered (the cap never goes below the need).
    """
    return min(100.0, need + settings.reserve, max(settings.maximum, need))


@dataclass
class Readiness:
    """The levels the car came home with, judged against one common reserve for both settings."""

    arrivals: int = 0
    arrivals_below_reserve: int = 0
    lowest_arrival: float | None = None


@dataclass
class SettingsResult:
    status: str  # ok | partial | unavailable
    reason: str | None
    model_version: int = SETTINGS_MODEL_VERSION
    baseline: PolicyResult | None = None
    alternative: PolicyResult | None = None
    baseline_settings: CjpSettings | None = None
    alternative_settings: CjpSettings | None = None
    baseline_readiness: Readiness | None = None
    alternative_readiness: Readiness | None = None
    targets: dict[str, list[float]] = field(default_factory=dict)
    windows_total: int = 0
    windows_replayed: int = 0
    stopped_at: float | None = None
    average_price: float | None = None
    max_conservation_error_kwh: float = 0.0
    notes: tuple[str, ...] = SETTINGS_NOTES
    warnings: tuple[str, ...] = ()
    provenance: Mapping[str, str] = field(default_factory=dict)
    ignored_events: int = 0
    comparable: bool = False
    comparable_reason: str | None = None
    difference: dict[str, float] | None = None  # alternative minus baseline; negative cost is cheaper
    method: str = "strict"
    estimated_windows: int = 0
    price_estimate: float | None = None
    calibrated_start: bool = False


def _validate_settings(baseline: CjpSettings, alternative: CjpSettings) -> str | None:
    for settings in (baseline, alternative):
        if not (_finite(settings.reserve) and 0.0 <= settings.reserve <= 100.0):
            return "invalid_settings"
        if not (_finite(settings.maximum) and 0.0 < settings.maximum <= 100.0):
            return "invalid_settings"
    if baseline == alternative:
        return "no_change"
    return None


def _gap_needs(data: ReplayInput, ordered: list[Window]) -> list[float]:
    """The battery percent the recorded drives after each window (up to the next one) use; the last window has no next one."""
    events = sorted(data.events, key=lambda e: e.ts)
    taken: set[int] = set()  # the same assignment as _run: a drive belongs to one gap
    needs = []
    for index, window in enumerate(ordered[:-1]):
        need = 0.0
        for position, event in enumerate(events):
            if position not in taken and _in_gap(event, window, ordered[index + 1].start):
                taken.add(position)
                if event.kind == "drive":
                    need += event.soc_percent
        needs.append(need)
    return needs


def _readiness(policy: PolicyResult, reserve: float) -> Readiness:
    # the level when the car came back to each following plug-in (the last one after the last applied drive)
    arrivals = [row.soc_in for row in policy.windows[1:]] + [policy.end_soc]
    return Readiness(
        arrivals=len(arrivals),
        arrivals_below_reserve=sum(1 for level in arrivals if level < reserve - 1e-6),
        lowest_arrival=min(arrivals) if arrivals else None,
    )


def replay_settings(data: ReplayInput, baseline: CjpSettings, alternative: CjpSettings) -> SettingsResult:
    """Replay two settings over the same demand. Never raises for bad data: it reports why.

    data.target_soc and data.smart_weekdays are not used (the targets come from the settings).
    """
    ordered = sorted(data.windows, key=lambda w: w.start)
    provenance = dict(data.provenance)
    usable_max = baseline.maximum if _finite(baseline.maximum) and baseline.maximum > 0 else 100.0
    reason = _validate(replace(data, target_soc=usable_max, smart_weekdays=None))
    if reason is None:
        reason = _validate_settings(baseline, alternative)
    if reason is None and len(ordered) < (3 if data.start_soc is None else 2):
        reason = "too_few_windows"  # the demand after the last connection is unknown (and the first one may only calibrate)
    if reason is not None:
        return SettingsResult(status="unavailable", reason=reason, windows_total=len(ordered), provenance=provenance)
    try:
        return _replay_settings(replace(data, smart_weekdays=None), ordered, baseline, alternative, provenance)
    except _ModelError as err:
        return SettingsResult(status="unavailable", reason=err.reason, windows_total=len(ordered), provenance=provenance)


def _replay_settings(
    data: ReplayInput, ordered: list[Window], baseline: CjpSettings, alternative: CjpSettings, provenance: dict[str, str]
) -> SettingsResult:
    needs = _gap_needs(data, ordered)
    candidates = ordered[:-1]  # the last connection's following drive is unknown
    measured = data
    data, price_fill, fully_priced = _with_estimated_prices(data, candidates)
    if not fully_priced and (price_fill is None or not _enough_priced(measured, candidates)):
        return SettingsResult(
            status="unavailable", reason="missing_prices", windows_total=len(ordered), provenance=provenance,
            warnings=measured.warnings,
        )
    base_targets = [settings_target(need, baseline) for need in needs]
    alt_targets = [settings_target(need, alternative) for need in needs]
    # an unknown starting level: the first connection brings both settings to the same level (the current setting's)
    calibrate_to = base_targets[0]
    base, base_stop, base_at, base_n = _run(data, "baseline", candidates, base_targets, ordered[-1].start, calibrate_to)
    alt, alt_stop, alt_at, alt_n = _run(data, "alternative", candidates, alt_targets, ordered[-1].start, calibrate_to)

    count = min(base_n, alt_n)
    stop, stopped = (base_stop, base_at) if base_n <= alt_n else (alt_stop, alt_at)
    if count == 0:
        return SettingsResult(status="unavailable", reason=stop or "no_windows", windows_total=len(ordered), provenance=provenance)
    if count < len(candidates):
        base, _, _, _ = _run(data, "baseline", candidates[:count], base_targets[:count], ordered[count].start, calibrate_to)
        alt, _, _, _ = _run(data, "alternative", candidates[:count], alt_targets[:count], ordered[count].start, calibrate_to)

    compared = ordered[:count]
    weighted = [(slot.price, slot.hours) for w in compared for slot in _slots(w, measured.prices) if slot.price is not None]
    hours = sum(h for _, h in weighted)
    average = sum(p * h for p, h in weighted) / hours if hours > 0 else None

    # the drives of every compared gap are used (the last one as the need only); the rest are not
    used = sum(1 for e in data.events if any(_in_gap(e, w, ordered[i + 1].start) for i, w in enumerate(compared)))
    inside = sum(1 for e in data.events if any(w.start <= e.ts < w.end - GAP_TOLERANCE for w in compared))
    estimated_windows = base.estimated_windows
    calibrated = base.calibrated
    warnings = (
        *data.warnings, *(("events_inside_windows",) if inside else ()),
        *(("prices_estimated_windows",) if estimated_windows else ()), *(("start_level_unknown_calibrated",) if calibrated else ()),
    )

    error = max(_conservation(data, base), _conservation(data, alt))
    complete = count == len(candidates)
    result = SettingsResult(
        status="ok" if complete else "partial",
        reason=None if complete else stop,
        baseline=base,
        alternative=alt,
        baseline_settings=baseline,
        alternative_settings=alternative,
        baseline_readiness=_readiness(base, baseline.reserve),
        alternative_readiness=_readiness(alt, baseline.reserve),
        targets={"baseline": base_targets[:count], "alternative": alt_targets[:count]},
        windows_total=len(ordered),
        windows_replayed=count,
        stopped_at=None if complete else stopped,
        average_price=average,
        max_conservation_error_kwh=error,
        warnings=warnings,
        provenance=provenance,
        ignored_events=len(data.events) - used,
        method="estimated" if estimated_windows or calibrated else "strict",
        estimated_windows=estimated_windows,
        price_estimate=price_fill,
        calibrated_start=calibrated,
    )
    if not error <= CONSERVATION_TOLERANCE_KWH:
        return SettingsResult(
            status="unavailable", reason="energy_conservation", windows_total=len(ordered), provenance=provenance,
            max_conservation_error_kwh=error,
        )

    # the settings aim at different levels by design, so only what must be delivered is compared:
    # a setting that covers less of the driving must not look cheaper for it
    if abs(base.unmet_driving_kwh - alt.unmet_driving_kwh) > COMPARE_TOLERANCE_KWH:
        result.comparable_reason = "unmet_driving_differs"
    elif average is None:
        result.comparable_reason = "no_prices"
    else:
        result.comparable = True
        # The settings differ mainly in the level the car ends with, which was bought once, in the cheap hours.
        # Valuing that energy at the period average would turn the cheap purchase into a gain for the higher
        # reserve, so it is valued at what the policy that holds it actually paid per kWh from the charger.
        holder = alt if alt.final_soc >= base.final_soc else base
        paid = holder.cost / holder.grid_kwh if holder.grid_kwh > EPS else average
        result.difference = _difference(data, alt, base, average, paid)
    return result


# ---------------------------------------------------------------------------- #
# Scenario D: the planned charging with the grid only (no solar), for the period
# ---------------------------------------------------------------------------- #
#
# The same modelled policy as Scenario B for ONE set of settings (the current ones): every connection
# is charged, in the cheapest hours, from the grid to the level the next recorded drive needs plus the
# reserve, capped at the maximum. Solar is not used at all, which is what "no solar" means here: all
# energy is bought at the grid price. Missing prices and an unknown starting level are handled like in
# the other replays (an estimated price, a calibrating first connection).

PLAN_NOTES = (
    "grid_only_no_solar",
    "hindsight",
    "modelled_policy",
    "demand_from_recorded_drives",
    "last_connection_left_out",
    "extra_top_ups_not_modelled",
    "forced_low_battery_charging_not_modelled",
    "site_limits_not_replayed",
    "standby_not_modelled",
)


@dataclass
class PlanResult:
    status: str  # ok | unavailable
    reason: str | None
    model_version: int = SETTINGS_MODEL_VERSION
    plan: PolicyResult | None = None
    settings: CjpSettings | None = None
    readiness: Readiness | None = None
    targets: list[float] = field(default_factory=list)
    windows_total: int = 0
    windows_replayed: int = 0
    average_price: float | None = None
    max_conservation_error_kwh: float = 0.0
    notes: tuple[str, ...] = PLAN_NOTES
    warnings: tuple[str, ...] = ()
    provenance: Mapping[str, str] = field(default_factory=dict)
    method: str = "strict"
    estimated_windows: int = 0
    price_estimate: float | None = None
    calibrated_start: bool = False


def replay_plan(data: ReplayInput, settings: CjpSettings) -> PlanResult:
    """Replay the planned grid-only charging of one set of settings. Never raises for bad data: it reports why."""
    ordered = sorted(data.windows, key=lambda w: w.start)
    provenance = dict(data.provenance)
    usable_max = settings.maximum if _finite(settings.maximum) and settings.maximum > 0 else 100.0
    reason = _validate(replace(data, target_soc=usable_max, smart_weekdays=None))
    if reason is None and not (_finite(settings.reserve) and 0.0 <= settings.reserve <= 100.0 and _finite(settings.maximum) and 0.0 < settings.maximum <= 100.0):
        reason = "invalid_settings"
    if reason is None and len(ordered) < (3 if data.start_soc is None else 2):
        reason = "too_few_windows"
    if reason is not None:
        return PlanResult(status="unavailable", reason=reason, windows_total=len(ordered), provenance=provenance)
    try:
        return _replay_plan(replace(data, smart_weekdays=None), ordered, settings, provenance)
    except _ModelError as err:
        return PlanResult(status="unavailable", reason=err.reason, windows_total=len(ordered), provenance=provenance)


def _replay_plan(data: ReplayInput, ordered: list[Window], settings: CjpSettings, provenance: dict[str, str]) -> PlanResult:
    needs = _gap_needs(data, ordered)
    candidates = ordered[:-1]
    measured = data
    data, price_fill, fully_priced = _with_estimated_prices(data, candidates)
    if not fully_priced and (price_fill is None or not _enough_priced(measured, candidates)):
        return PlanResult(status="unavailable", reason="missing_prices", windows_total=len(ordered), provenance=provenance, warnings=measured.warnings)
    targets = [settings_target(need, settings) for need in needs]
    plan, stop, _, count = _run(data, "planned", candidates, targets, ordered[-1].start, targets[0])
    if count == 0:
        return PlanResult(status="unavailable", reason=stop or "no_windows", windows_total=len(ordered), provenance=provenance)
    compared = ordered[:count]
    weighted = [(slot.price, slot.hours) for w in compared for slot in _slots(w, measured.prices) if slot.price is not None]
    hours = sum(h for _, h in weighted)
    average = sum(p * h for p, h in weighted) / hours if hours > 0 else None
    error = _conservation(data, plan)
    if not error <= CONSERVATION_TOLERANCE_KWH:
        return PlanResult(
            status="unavailable", reason="energy_conservation", windows_total=len(ordered), provenance=provenance, max_conservation_error_kwh=error,
        )
    estimated_windows, calibrated = plan.estimated_windows, plan.calibrated
    inside = sum(1 for e in data.events if any(w.start <= e.ts < w.end - GAP_TOLERANCE for w in compared))
    return PlanResult(
        status="ok",
        reason=None,
        plan=plan,
        settings=settings,
        readiness=_readiness(plan, settings.reserve),
        targets=targets[:count],
        windows_total=len(ordered),
        windows_replayed=count,
        average_price=average,
        max_conservation_error_kwh=error,
        warnings=(
            *data.warnings, *(("events_inside_windows",) if inside else ()),
            *(("prices_estimated_windows",) if estimated_windows else ()), *(("start_level_unknown_calibrated",) if calibrated else ()),
        ),
        provenance=provenance,
        method="estimated" if estimated_windows or calibrated else "strict",
        estimated_windows=estimated_windows,
        price_estimate=price_fill,
        calibrated_start=calibrated,
    )
