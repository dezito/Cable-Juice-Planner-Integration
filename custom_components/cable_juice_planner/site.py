"""Site: the grid connection shared by several Cable Juice Planner instances.

The site config entry holds the main fuse per phase, an optional current (or power)
sensor per phase and how soon a trip makes a car urgent. Every car instance registers
here and asks for its capacity before each job (event loop, no I/O):

* order: urgent cars first (battery under the daily minimum or a trip soon), then the
  fixed priority of the instance, then the prefix
* planned capacity per hour = fuse - margin - forecast house current (hour of day mean
  of the last week, without the cars) - reservations of the cars ahead
* live capacity = fuse - margin - house current now - what the cars ahead draw now (without phase
  sensors from their charger power sensors; without a reading, or just after a raise, what they were given)

Phases are grid phases here; every car maps its charger L1/L2/L3 onto them with its
phase order. When a car ahead changes its plan, the cars behind it are recalculated.
An overloaded phase immediately limits the last charging car: measured by the phase sensors, or
without them the sum of the chargers' power sensors.

Circuits: chargers sharing one circuit limit (e.g. Easee chargers behind a master) are
lists of HA devices in the site options. A car belongs to a circuit through the devices
of its charger entities. Every car of a circuit publishes the circuit limit as the sum of
what the circuit's cars charge with per grid phase, and the first charging car decides
1 or 3 phases for the cars behind it (the circuit limit switches the phases of all).
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import math
import time
from decimal import ROUND_CEILING, Decimal
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    CHARGER_COMPLETED_STATUS,
    CHARGER_NOT_READY_STATUS,
    CIRCUIT_ENTITY_KEYS,
    CONF_CIRCUITS,
    DOMAIN,
    ENTITY_UNAVAILABLE_STATES,
    PHASE_ORDERS,
    SIGNAL_SITE_UPDATE,
    SITE_DEFAULTS,
    SITE_HOURS_AHEAD,
)
from .management.value_checks import number_text
from .utils.helpers import float_or_none
from .utils.power import POWER_UNITS, power_factor_to_w, statistic_power_factor_to_w

if TYPE_CHECKING:
    from .coordinator import CJPCoordinator

_LOGGER = logging.getLogger(__name__)

RECALCULATE_DELAY = 15  # s, the cars behind a changed plan are recalculated after this
OVERLOAD_THROTTLE = 20  # s between two overload reactions for the same car
RAMP_UP = 120  # s after a car ahead got more current, before only its measured draw counts (no phase sensors)
OVERDRAW_SHARE = 0.1  # charger power up to this share above what it was given is the voltage, not an overdraw
# a charger status or power sensor that has not reported for this long frees no current (a lost cloud push keeps the
# last value: a car that starts would not be seen)
STALE_SECONDS = 300
# the house forecast asks the statistics in these. Not energy (unlike STATISTICS_POWER_UNITS): load balancing reads no energy
# unit as a power, and a phase sensor in an energy unit is no measurement (ISSUE_PHASE_UNITS)
FORECAST_UNITS = {"power": "W", "electric_current": "A"}
# the repair for phase sensors in an energy unit (Wh, kWh ...): an energy is no current, it can't be read
ISSUE_PHASE_UNITS = "site_phase_units"
# the repair for a main fuse that the house and the cars exceed for hours (the house forecast's hourly means, fuse_over_lines).
# A warning only: the overload check protects the fuse, the repair points at its setting or at the phase sensors
ISSUE_FUSE_EXCEEDED = "site_fuse_exceeded"
FUSE_OVER_FACTOR = 1.25  # an hourly mean above this times the raw main fuse counts as over it
FUSE_OVER_HOURS = 3  # this many such hours in the forecast's days raise the repair
FUSE_MIN_HOURS = 24  # hours of data per phase before a positive answer (the repair goes)
STALE_CHECK_SECONDS = 30  # how often the overload check runs without a sensor event (a sensor that went stale)
# charger statuses that surely mean "not charging": only then does a car's measured power free current (no phase sensors)
SETTLED_STATUS = (*CHARGER_COMPLETED_STATUS, *CHARGER_NOT_READY_STATUS, "ready_to_charge", "awaiting_start", "awaiting_authorization")
FORECAST_DAYS = 7


def phase_units_issue_id(entry_id: str) -> str:
    return f"{entry_id}_{ISSUE_PHASE_UNITS}"


def fuse_issue_id(entry_id: str) -> str:
    return f"{entry_id}_{ISSUE_FUSE_EXCEEDED}"


def _decimal_mark(text: str, language: str | None) -> str:
    """A number's text with a decimal comma unless the language is English (the same rule as value_checks.number_text)."""
    return text if (language or "en").lower().startswith("en") else text.replace(".", ",")


def _amps(value: float, language: str | None) -> str:
    """A limit or a fuse in amps with up to two decimals, so 31.25 stays 31.25 (number_text keeps three significant digits)."""
    return _decimal_mark(f"{value:.2f}".rstrip("0").rstrip("."), language)


def _highest_mean(value: float, language: str | None) -> str:
    """The highest hourly mean with one decimal, rounded up: the shown maximum is never below the real one, so it can't look equal
    to a limit it is above (20.01 shows 20.1, not 20.0)."""
    return _decimal_mark(str(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_CEILING)), language)


def fuse_over_lines(phases: list[dict[float, float]], fuses: list[float], language: str | None = None) -> tuple[str, ...] | None:
    """The repair's lines for the grid phases whose hourly mean current was above their raw main fuse times FUSE_OVER_FACTOR for
    FUSE_OVER_HOURS hours or more. Empty: none is, and every phase has FUSE_MIN_HOURS hours of data (a positive answer). None:
    can't tell (a phase with no fuse, 0, is not judged, nor is one with too few hours; nothing over is then not a fix).

    phases: per grid phase the hourly mean current in amps (hour timestamp -> amps), the house and the cars both. A line reads
    "- L1: 25 A → 32.0 A (3 h > 31.25 A)": the main fuse, the highest hourly mean, and the hours above the limit (the fuse times
    FUSE_OVER_FACTOR) with that limit. language: the decimal mark of the numbers."""
    lines: list[str] = []
    complete = True
    for index, (series, fuse) in enumerate(zip(phases, fuses)):
        if fuse <= 0:
            complete = False
            continue
        means = list(series.values())
        if len(means) < FUSE_MIN_HOURS:
            complete = False
        limit = fuse * FUSE_OVER_FACTOR
        above = sum(1 for mean in means if mean > limit)
        if above >= FUSE_OVER_HOURS:
            lines.append(
                f"- L{index + 1}: {_amps(fuse, language)} A → {_highest_mean(max(means), language)} A "
                f"({above} h > {_amps(limit, language)} A)"
            )
    if lines:
        return tuple(lines)
    return () if complete else None


def _energy_unit(unit) -> bool:
    """Whether Home Assistant keeps statistics in this unit as energy (Wh, kWh, GJ ...): load balancing never reads them as a
    power."""
    from homeassistant.util.unit_conversion import EnergyConverter  # noqa: PLC0415

    return isinstance(unit, str) and unit in EnergyConverter.VALID_UNITS


def charger_devices(hass: HomeAssistant, entity_ids) -> set[str]:
    """The HA devices of a car's charger entities."""
    ent_reg = er.async_get(hass)
    devices = set()
    for entity_id in entity_ids:
        if entity_id and (entry := ent_reg.async_get(entity_id)) is not None and entry.device_id:
            devices.add(entry.device_id)
    return devices


def circuit_of(circuits, own: set[str]) -> list[str]:
    """The circuit (device ids) with one of the car's own charger devices, else []."""
    for group in circuits or []:
        if own & set(group):
            return list(group)
    return []


def update_circuits(circuits, own: set[str], selected) -> list[list[str]]:
    """The circuits after a car chose ``selected`` as the chargers of its circuit.

    The car's previous circuit is replaced, so a change made from one car applies to all
    cars of the circuit. A device is in one circuit only; a circuit needs two devices.
    """
    selected = list(dict.fromkeys(device for device in selected or [] if device))
    result = []
    for group in circuits or []:
        if own & set(group):
            continue
        group = [device for device in group if device not in selected]
        if len(group) > 1:
            result.append(group)
    if len(selected) > 1:
        result.append(selected)
    return result


def _charger_entities(coordinator: CJPCoordinator) -> list[str]:
    config = getattr(coordinator, "config", None)
    if not config:
        return []
    entity_ids = config["charger"]["entity_ids"]
    return [entity_ids.get(key) or "" for key in CIRCUIT_ENTITY_KEYS]


class CJPSite:
    """Shared capacity of one grid connection."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.config: dict[str, Any] = {**SITE_DEFAULTS, **entry.options}
        self.members: dict[str, CJPCoordinator] = {}
        self.reservations: dict[str, dict[datetime.datetime, list[float]]] = {}  # grid phases
        self.requests: dict[str, list[float]] = {}  # grid phases
        self.urgent: dict[str, bool] = {}
        self.house_forecast: dict[int, list[float]] = {}  # hour of day -> grid phases
        self.house_now: list[float] = [0.0, 0.0, 0.0]
        self._last_house: list[float] = [0.0, 0.0, 0.0]  # the last house current measured by the phase sensors
        self._house_measured = False  # whether _last_house is a measurement (not the start value)
        self._was_metered = False  # the phase sensors measured at the last phase event
        self.available_now: dict[str, list[float]] = {}
        self.circuits: list[list[str]] = [list(group) for group in entry.options.get(CONF_CIRCUITS) or [] if isinstance(group, list)]
        self.devices: dict[str, set[str]] = {}  # entry id -> charger devices
        self._unsubs: list = []
        self._pending: dict[str, Any] = {}
        self._last_overload: dict[str, float] = {}
        self._raised_at: dict[str, float] = {}  # entry id -> monotonic time its requested current was last raised
        self._power_unsub = None
        self._overload_recheck = None
        self._startup_ready = asyncio.Event()
        # (entity id, energy unit) of the phase sensors in the repair; None: not judged yet
        self._phase_units_wrong: tuple[tuple[str, str], ...] | None = None
        # the lines of the main fuse repair (fuse_over_lines): empty = none over, None = not judged yet
        self._fuse_verdict: tuple[str, ...] | None = None
        self._forecast_energy_warned: set[str] = set()  # phase statistics in an energy unit already warned about

    # ------------------------------------------------------------------ #
    # Setup
    # ------------------------------------------------------------------ #

    @property
    def phase_entities(self) -> list[str]:
        return [self.config.get(f"phase_l{i}_entity_id") or "" for i in (1, 2, 3)]

    @property
    def has_phase_sensors(self) -> bool:
        return all(self.phase_entities)

    def fuse(self) -> list[float]:
        margin = float(self.config.get("margin") or 0.0)
        return [max(float(self.config.get(f"fuse_l{i}") or 0.0) - margin, 0.0) for i in (1, 2, 3)]

    async def async_start(self) -> None:
        try:
            if self.has_phase_sensors:
                self._unsubs.append(async_track_state_change_event(self.hass, self.phase_entities, self._phase_changed))
                self._unsubs.append(async_track_time_change(self.hass, self._refresh_forecast, minute=7, second=0))
                await self._async_refresh_forecast()
            self.recalculate_all()
            # last: the repair is only information and must never hold the forecast or the calculation up
            if self.has_phase_sensors:
                self._check_phase_units()
            else:
                # no phase sensors (any more): nothing to judge, a positive answer
                self._set_phase_units_issue(())
                self._set_fuse_issue(())
        finally:
            # Cars starting on the same homeassistant_started event must not
            # calculate against a half-initialized site forecast.
            self._startup_ready.set()

    async def async_wait_started(self) -> None:
        """Wait until the site's initial load-balancing data is ready."""
        await self._startup_ready.wait()

    async def async_stop(self) -> None:
        for unsub in (*self._unsubs, *self._pending.values()):
            unsub()
        self._unsubs.clear()
        self._pending.clear()
        if self._power_unsub is not None:
            self._power_unsub()
            self._power_unsub = None
        if self._overload_recheck is not None:
            self._overload_recheck()
            self._overload_recheck = None
        members = list(self.members.values())
        self.members.clear()
        for coordinator in members:
            coordinator.async_site_recalculate()

    def register(self, coordinator: CJPCoordinator) -> None:
        self.members[coordinator.entry.entry_id] = coordinator
        self.devices[coordinator.entry.entry_id] = charger_devices(self.hass, _charger_entities(coordinator))
        self._track_car_power()
        _LOGGER.info("Site: %s joined (priority %s, phase order %s)", coordinator.prefix, coordinator.site_priority, coordinator.site_phase_order)

    def unregister(self, entry_id: str) -> None:
        self.members.pop(entry_id, None)
        self.devices.pop(entry_id, None)
        self.reservations.pop(entry_id, None)
        self.requests.pop(entry_id, None)
        self.urgent.pop(entry_id, None)
        self._raised_at.pop(entry_id, None)
        self._track_car_power()
        self.recalculate_all()

    # ------------------------------------------------------------------ #
    # Order and phase mapping
    # ------------------------------------------------------------------ #

    def order(self) -> list[str]:
        return sorted(
            self.members,
            key=lambda entry_id: (not self.urgent.get(entry_id, False), self.members[entry_id].site_priority, self.members[entry_id].prefix),
        )

    def ahead(self, entry_id: str) -> list[str]:
        order = self.order()
        return order[: order.index(entry_id)] if entry_id in order else order

    def behind(self, entry_id: str) -> list[str]:
        order = self.order()
        return order[order.index(entry_id) + 1:] if entry_id in order else []

    @staticmethod
    def _order(coordinator: CJPCoordinator) -> tuple[int, int, int]:
        return PHASE_ORDERS.get(coordinator.site_phase_order, PHASE_ORDERS["L1-L2-L3"])

    def to_grid(self, coordinator: CJPCoordinator, charger: list) -> list[float]:
        grid = [0.0, 0.0, 0.0]
        for i, grid_phase in enumerate(self._order(coordinator)):
            grid[grid_phase] += float(charger[i]) if i < len(charger) else 0.0
        return grid

    def to_charger(self, coordinator: CJPCoordinator, grid: list) -> list[float]:
        return [round(float(grid[grid_phase]), 1) for grid_phase in self._order(coordinator)]

    # ------------------------------------------------------------------ #
    # Circuits (chargers sharing one circuit limit)
    # ------------------------------------------------------------------ #

    def circuit_members(self, entry_id: str) -> list[str]:
        """The cars on the circuit of this car (incl. itself), [] without a shared circuit."""
        group = set(circuit_of(self.circuits, self.devices.get(entry_id) or set()))
        if not group:
            return []
        members = [other for other in self.order() if self.devices.get(other, set()) & group]
        return members if len(members) > 1 else []

    def circuit_amps(self, members: list[str]) -> list[int]:
        """Circuit limit per grid phase: the sum of what the circuit's cars charge with, at most the fuse.

        The sum can be above the fuse when a car behind got the current a car ahead doesn't draw: the circuit then
        keeps both within the fuse if the car ahead starts after all.
        """
        total = [0.0, 0.0, 0.0]
        for entry_id in members:
            total = [a + b for a, b in zip(total, self.requests.get(entry_id) or [0.0, 0.0, 0.0])]
        return [min(int(round(value)), int(math.floor(fuse))) for value, fuse in zip(total, self.fuse())]

    def circuit_phases(self, entry_id: str) -> int | None:
        """Phases the circuit allows this car: those of the first charging car ahead on it.

        The circuit limit decides 1 or 3 phases for every charger on it, so a car behind
        cannot charge on other phases than the car ahead. Cars that only charge on one phase
        anyway do not decide it.
        """
        members = self.circuit_members(entry_id)
        for other in self.ahead(entry_id):
            if other not in members or int(self.members[other].config["charger"]["charging_phases"]) <= 1:
                continue
            if phases := sum(1 for value in self.requests.get(other) or [] if value > 0):
                return phases
        return None

    def circuit_patch(self, coordinator: CJPCoordinator) -> bool:
        """Put the circuit's sum on the car's charger action; True when it changed."""
        members = self.circuit_members(coordinator.entry.entry_id)
        if not members:
            return False
        values: dict[str, Any] = {f"circuit_phase_{i}_amps": amps for i, amps in enumerate(self.circuit_amps(members), start=1)}
        values["circuit_chargers"] = [self.members[entry_id].prefix for entry_id in members]
        attributes = coordinator.attributes.setdefault("charger_action", {})
        changed = any(attributes.get(key) != value for key, value in values.items())
        attributes.update(values)
        return changed

    # ------------------------------------------------------------------ #
    # Measurements
    # ------------------------------------------------------------------ #

    def _amps(self, entity_id: str, voltage: float) -> float | None:
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None or (value := float_or_none(state.state)) is None or not math.isfinite(value):
            # nan/inf is no reading either (it would make every capacity nan)
            return None
        unit = state.attributes.get("unit_of_measurement")
        if _energy_unit(unit):
            # an energy (Wh, kWh ...) is no current: no measurement, so the house counts with its last measurement or the
            # forecast, or as the whole budget (the repair ISSUE_PHASE_UNITS says why)
            return None
        if unit == "mA":
            return abs(value) / 1000
        # a power (mW, W, kW, MW, GW) is W on the phase's voltage. Any other unit and no unit are amps as before: a phase
        # sensor protects the fuse and is never read lower than before
        if unit in POWER_UNITS:
            return abs(value) * power_factor_to_w(unit) / voltage
        return abs(value)

    def _measured(self) -> list[float] | None:
        """The phase sensors now; None when one has no number or has not reported within STALE_SECONDS (a meter that hangs
        at a low value would free current, and hide an overload)."""
        voltage = float(self.config.get("voltage") or 230.0)
        values = []
        for entity_id in self.phase_entities:
            state = self.hass.states.get(entity_id) if entity_id else None
            values.append(self._amps(entity_id, voltage) if state is not None and self._fresh(state) else None)
        return None if any(value is None for value in values) else values

    def _car_draw(self, coordinator: CJPCoordinator, fallback: list[float] | None = None) -> list[float]:
        """What the car draws now per grid phase (from its charger power sensor, with phase sensors).

        Without a fresh reading in a power unit (no number, no or another unit, not reported within STALE_SECONDS) fallback counts
        when given: the car's request for a car ahead (a power sensor that hangs at 0 W must not free its current).
        Without fallback (the house) a stale reading counts at most what the car was given per phase: a reading that
        hangs high on a car the plan stopped must not make the house smaller, and a car that charges steadily (a
        sensor that only reports a change) must not count its own charging as the house."""
        charger = coordinator.config["charger"]
        voltage = float(charger["power_voltage"]) or 230.0
        entity_id = self._power_entity(coordinator)
        state = self.hass.states.get(entity_id) if entity_id else None
        watt = self._watt(entity_id)
        fresh = watt is not None and self._fresh(state)
        if not fresh and fallback is not None:
            return list(fallback)
        if watt is None or (not fresh and self._status_says_not_charging(coordinator)):
            # a stale reading of a charger whose fresh status says it doesn't charge (full, paused) is no draw
            return [0.0, 0.0, 0.0]
        request = self.requests.get(coordinator.entry.entry_id) or [0.0, 0.0, 0.0]
        phases = sum(1 for value in request if value > 0) or int(charger["charging_phases"])
        per_phase = watt / voltage / phases
        max_amp = float_or_none(charger.get("charging_max_amp"))
        if fallback is None and max_amp:
            # taken from the house: never more than the charger can draw (its max current and the voltage share). A reading
            # far above it must not make the house smaller and free the fuse's current; for a car ahead a higher reading only
            # counts more (conservative), so it is not capped there
            per_phase = min(per_phase, max_amp * (1 + OVERDRAW_SHARE))
        grid = [0.0, 0.0, 0.0]
        for i, grid_phase in enumerate(self._order(coordinator)):
            if i < phases:
                grid[grid_phase] += per_phase
        return grid if fresh else [min(draw, given) for draw, given in zip(grid, request)]

    def _house(self) -> list[float]:
        """House current per grid phase now, without the cars.

        Without phase sensors 0 A (the fuse is the cars' budget). With phase sensors that measure nothing now (no number,
        not reported within STALE_SECONDS) the house is not 0 A but the higher of its last measured value and the hour's
        forecast (the highest hour of the forecast when this hour has none), per phase: a missing measurement must not
        free the house's current. Without any of them (after a start, before the meter reported) the house counts as the
        whole budget: no current for the cars until the meter reports (user choice)."""
        if not self.has_phase_sensors:
            return [0.0, 0.0, 0.0]
        measured = self._measured()
        if measured is None:
            forecast = self.house_forecast.get(dt_util.now().hour)
            if forecast is None and self.house_forecast:
                forecast = [max(hour[i] for hour in self.house_forecast.values()) for i in range(3)]
            if forecast is None and not self._house_measured:
                return list(self.fuse())
            return [max(last, hour) for last, hour in zip(self._last_house, forecast or [0.0, 0.0, 0.0])]
        cars = [0.0, 0.0, 0.0]
        for coordinator in self.members.values():
            cars = [a + b for a, b in zip(cars, self._car_draw(coordinator))]
        self._last_house = [max(m - c, 0.0) for m, c in zip(measured, cars)]
        self._house_measured = True
        return list(self._last_house)

    # Without phase sensors the chargers' power sensors are the measurement

    def _power_entity(self, coordinator: CJPCoordinator) -> str:
        return coordinator.config["charger"]["entity_ids"].get("charger_power_entity_id") or ""

    def _watt(self, entity_id: str) -> float | None:
        """A charger power reading in W; None without a number, without a unit or with a unit that is not a power unit (never
        guessed: the car then counts with its request). An energy unit (Wh, kWh) is no reading either: it may be a meter picked
        as the power sensor, and load balancing protects the fuse."""
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None or (value := float_or_none(state.state)) is None or not math.isfinite(value):
            return None
        unit = state.attributes.get("unit_of_measurement")
        if unit not in POWER_UNITS:
            return None
        return abs(value) * power_factor_to_w(unit)

    def _worst_case(self, members: list[CJPCoordinator], watt: float, careful: bool = True) -> list[float]:
        """The most the cars on one power sensor can draw per grid phase with this power.

        Not more on a phase than they were given there while the charger keeps to that, but all of the power may be
        on one phase: a car can charge on fewer phases than it was given. Power clearly above all they were given is
        spread over the phases their chargers can use (an overdraw on fewer phases than given is not seen this way). A car drawing without having been given
        current (charging without a rule, which the plan stops) counts with all of it on each of its phases when
        careful (the budget of the cars behind it), else spread over its phases (the overload check, which would
        otherwise limit another car for a draw that is not there).
        """
        request = [0.0, 0.0, 0.0]
        for coordinator in members:
            request = [a + b for a, b in zip(request, self.requests.get(coordinator.entry.entry_id) or [0.0, 0.0, 0.0])]
        amps = watt / (float(members[0].config["charger"]["power_voltage"]) or 230.0)
        total = sum(request)
        if total > 0:
            measured = amps - total * OVERDRAW_SHARE  # up to this share more is the real voltage above the configured one
            if measured <= total:
                return [min(given, amps) if given > 0 else 0.0 for given in request]
            # a charger that takes clearly more than it was given in all (one that did not follow a limit yet): the power is
            # spread over the phases its charger can use (also those it was given nothing on: a charger that did not switch
            # to one phase), never less than it was given on a phase
            phases_of = set()
            for coordinator in members:
                phases = max(int(coordinator.config["charger"]["charging_phases"]), 1)
                phases_of.update(grid_phase for i, grid_phase in enumerate(self._order(coordinator)) if i < phases)
            phases_of.update(i for i, given in enumerate(request) if given > 0)
            spread = measured / len(phases_of)
            return [max(given, spread) if i in phases_of else given for i, given in enumerate(request)]
        grid = [0.0, 0.0, 0.0]
        for coordinator in members:
            phases = max(int(coordinator.config["charger"]["charging_phases"]), 1)
            for i, grid_phase in enumerate(self._order(coordinator)):
                if i < phases:
                    grid[grid_phase] = max(grid[grid_phase], amps if careful else amps / phases)
        return grid

    def _settled(self, coordinator: CJPCoordinator) -> bool:
        """The car's measured power may free its current: past the ramp-up after a raise (the charger needs a moment
        to start), and a charger status that surely is not charging (SETTLED_STATUS). A power sensor that hangs at
        0 W while the car charges must not give its current away, so a status that may also mean charging (on,
        connected, ready …), an unknown one or none counts as charging. Both the status and the power sensor must have
        reported within STALE_SECONDS (a lost cloud push leaves the last values)."""
        if time.monotonic() - self._raised_at.get(coordinator.entry.entry_id, float("-inf")) < RAMP_UP:
            return False
        status_id = coordinator.config["charger"]["entity_ids"].get("status_entity_id") or ""
        state = self.hass.states.get(status_id) if status_id else None
        if state is None or state.state not in SETTLED_STATUS or not self._fresh(state):
            return False
        # the power sensor too: a stale 0 W is no proof that the car doesn't draw
        power = self.hass.states.get(self._power_entity(coordinator)) if self._power_entity(coordinator) else None
        return power is not None and self._fresh(power)

    def _status_says_not_charging(self, coordinator: CJPCoordinator) -> bool:
        """The charger's status reported within STALE_SECONDS and surely means not charging (SETTLED_STATUS)."""
        status_id = coordinator.config["charger"]["entity_ids"].get("status_entity_id") or ""
        state = self.hass.states.get(status_id) if status_id else None
        return state is not None and state.state in SETTLED_STATUS and self._fresh(state)

    @staticmethod
    def _fresh(state) -> bool:
        """The sensor reported (also an unchanged value) within STALE_SECONDS."""
        reported = getattr(state, "last_reported", None) or state.last_updated
        return (dt_util.utcnow() - reported).total_seconds() <= STALE_SECONDS

    def _ahead_draw(self, coordinator: CJPCoordinator) -> list[float]:
        """What a car ahead takes from the budget per grid phase, without phase sensors.

        Its charger power sensor shows what it really draws, so current it was given but doesn't use (a car that
        doesn't start, a full battery) goes to the cars behind. It counts with what it was given without a reading,
        on a power sensor it shares with another car (that shows the other car too, e.g. the asking car), or while not
        :meth:`_settled`.
        """
        request = list(self.requests.get(coordinator.entry.entry_id) or [0.0, 0.0, 0.0])
        entity_id = self._power_entity(coordinator)
        shared = any(self._power_entity(other) == entity_id for other in self.members.values() if other is not coordinator)
        if not entity_id or shared or (watt := self._watt(entity_id)) is None:
            return request
        worst = self._worst_case([coordinator], watt)
        if not self._settled(coordinator):
            return [max(a, b) for a, b in zip(worst, request)]
        return worst

    def _cars_draw(self) -> list[float]:
        """The cars' draw per grid phase from their power sensors (each sensor once), for the overload check.

        Like the budget: a sensor without a reading, or with a car on it that is not :meth:`_settled`, counts with at
        least what its cars were given. So a car whose current went to the car behind and that starts after all (its
        status says charging, or it got more current) is an overload at once, also when its power sensor is slow."""
        total = [0.0, 0.0, 0.0]
        by_entity: dict[str, list[CJPCoordinator]] = {}
        for coordinator in self.members.values():
            by_entity.setdefault(self._power_entity(coordinator), []).append(coordinator)
        for entity_id, members in by_entity.items():
            request = [0.0, 0.0, 0.0]
            for coordinator in members:
                request = [a + b for a, b in zip(request, self.requests.get(coordinator.entry.entry_id) or [0.0, 0.0, 0.0])]
            watt = self._watt(entity_id) if entity_id else None
            if watt is None:
                draw = request
            else:
                draw = self._worst_case(members, watt, careful=False)
                if not all(self._settled(coordinator) for coordinator in members):
                    draw = [max(a, b) for a, b in zip(draw, request)]
            total = [a + b for a, b in zip(total, draw)]
        return total

    def _metered(self) -> bool:
        """The phase sensors measure now. Configured but without a number, the chargers' power sensors are the measurement,
        as without phase sensors, and the house counts with its last measured value or the forecast (_house)."""
        return self.has_phase_sensors and self._measured() is not None

    # ------------------------------------------------------------------ #
    # Capacity for one car (called by the coordinator before each job)
    # ------------------------------------------------------------------ #

    def snapshot_for(self, coordinator: CJPCoordinator, now: datetime.datetime) -> dict:
        entry_id = coordinator.entry.entry_id
        fuse = self.fuse()
        ahead = [self.members[e] for e in self.ahead(entry_id) if e in self.members]
        self.house_now = house = self._house()
        metered = self._metered()
        live = list(fuse)
        for other in ahead:
            # with phase sensors the real draw counts (they measure it), else the charger power sensor (_ahead_draw)
            draw = self._car_draw(other, self.requests.get(other.entry.entry_id) or [0.0, 0.0, 0.0]) if metered else self._ahead_draw(other)
            live = [a - b for a, b in zip(live, draw)]
        live = [max(a - h, 0.0) for a, h in zip(live, house)]
        self.available_now[entry_id] = live
        hour_capacity = {}
        start = now.replace(minute=0, second=0, microsecond=0)
        for offset in range(SITE_HOURS_AHEAD):
            hour = start + datetime.timedelta(hours=offset)
            capacity = [a - h for a, h in zip(fuse, self.house_forecast.get(hour.hour, [0.0, 0.0, 0.0]))]
            for other in ahead:
                reserved = self.reservations.get(other.entry.entry_id, {}).get(hour)
                if reserved:
                    capacity = [a - b for a, b in zip(capacity, reserved)]
            hour_capacity[hour] = self.to_charger(coordinator, [max(a, 0.0) for a in capacity])
        async_dispatcher_send(self.hass, SIGNAL_SITE_UPDATE)
        return {
            "live": self.to_charger(coordinator, live),
            "hour_capacity": hour_capacity,
            "urgent_hours": float(self.config.get("urgent_trip_hours") or 12),
            "circuit_phases": self.circuit_phases(entry_id),
        }

    @callback
    def update_from_result(self, coordinator: CJPCoordinator, site_result: dict) -> None:
        """A car published its plan/request; recalculate the cars behind if it changed."""
        entry_id = coordinator.entry.entry_id
        reservation = {hour: self.to_grid(coordinator, amps) for hour, amps in (site_result.get("reservation") or {}).items()}
        request = self.to_grid(coordinator, site_result.get("request") or [0, 0, 0])
        urgent = bool(site_result.get("urgent"))
        changed_order = self.urgent.get(entry_id) != urgent
        # the live request counts too: a car ahead that draws more (or less) changes what is left
        changed_plan = self.reservations.get(entry_id) != reservation or self.requests.get(entry_id) != request
        raised = any(new > old for new, old in zip(request, self.requests.get(entry_id) or [0.0, 0.0, 0.0]))
        if raised:
            self._raised_at[entry_id] = time.monotonic()
        self.reservations[entry_id] = reservation
        self.requests[entry_id] = request
        self.urgent[entry_id] = urgent
        if raised and not self._metered():
            # a car ahead that gets back the current a car behind took over: the car behind is limited at once, not first
            # when the car ahead's power shows it
            self._check_overload()
        # the other cars of the circuit publish the new sum at once (the master may be one of them)
        for other in self.circuit_members(entry_id):
            if other != entry_id and self.circuit_patch(self.members[other]):
                async_dispatcher_send(self.hass, self.members[other].signal, "charger_action")
        if changed_order:
            _LOGGER.info("Site: %s is %s", coordinator.prefix, "urgent, charging first" if urgent else "no longer urgent")
            # every car incl. this one: its result was calculated with its old place in the order
            self.recalculate_all()
        elif changed_plan:
            for other in self.behind(entry_id):
                self._schedule_recalculate(other)
        async_dispatcher_send(self.hass, SIGNAL_SITE_UPDATE)

    def recalculate_all(self) -> None:
        for entry_id in self.members:
            self._schedule_recalculate(entry_id)

    def _schedule_recalculate(self, entry_id: str) -> None:
        if entry_id in self._pending:
            return

        @callback
        def _run(_now) -> None:
            self._pending.pop(entry_id, None)
            if (coordinator := self.members.get(entry_id)) is not None:
                coordinator.async_site_recalculate()

        self._pending[entry_id] = async_call_later(self.hass, RECALCULATE_DELAY, _run)

    # ------------------------------------------------------------------ #
    # Overload protection
    # ------------------------------------------------------------------ #

    @callback
    def _phase_changed(self, _event: Event) -> None:
        metered = self._metered()
        if metered and not self._was_metered:
            # the meter measures again (or for the first time since the start): the cars were limited by the house's
            # estimate, or got nothing without any house data, until their next calculation
            self._house()
            self.recalculate_all()
        self._was_metered = metered
        self._check_overload()
        # after the overload check: the repair is only information and must never hold the protection up
        self._check_phase_units()

    @callback
    def _check_phase_units(self) -> None:
        """The repair for phase sensors in an energy unit. Raised for every such sensor; removed only when every phase
        sensor has a state that is not unavailable and none is in an energy unit (an unavailable sensor keeps its verdict
        and the unit it was judged in)."""
        before = dict(self._phase_units_wrong or ())
        wrong: list[tuple[str, str]] = []
        judged = True
        for entity_id in self.phase_entities:
            state = self.hass.states.get(entity_id) if entity_id else None
            if state is None or state.state in ENTITY_UNAVAILABLE_STATES:
                if entity_id in before:
                    wrong.append((entity_id, before[entity_id]))
                judged = False
                continue
            unit = state.attributes.get("unit_of_measurement")
            if _energy_unit(unit):
                wrong.append((entity_id, unit))
        if wrong or judged:
            self._set_phase_units_issue(tuple(wrong))

    @callback
    def _set_phase_units_issue(self, wrong: tuple[tuple[str, str], ...]) -> None:
        """wrong: (entity id, energy unit) of each phase sensor in an energy unit; empty removes the repair."""
        if wrong == self._phase_units_wrong:
            return  # unchanged: a phase sensor reports every few seconds
        self._phase_units_wrong = wrong
        issue_id = phase_units_issue_id(self.entry.entry_id)
        if not wrong:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_PHASE_UNITS,
            translation_placeholders={"entities": "\n".join(f"- {entity_id} ({unit})" for entity_id, unit in wrong)},
        )

    def _check_fuse(self, phases: list[dict[float, float]]) -> None:
        """Judges the main fuse repair on the hourly means of the phases (see fuse_over_lines). Not judged: nothing changes."""
        fuses = [float(self.config.get(f"fuse_l{i}") or 0.0) for i in (1, 2, 3)]
        verdict = fuse_over_lines(phases, fuses, self.hass.config.language)
        if verdict is not None:
            self._set_fuse_issue(verdict)

    @callback
    def _set_fuse_issue(self, lines: tuple[str, ...]) -> None:
        """lines: the phases over their fuse (the repair names them); empty removes the repair. Only a change touches the registry."""
        if lines == self._fuse_verdict:
            return
        self._fuse_verdict = lines
        issue_id = fuse_issue_id(self.entry.entry_id)
        if not lines:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_FUSE_EXCEEDED,
            translation_placeholders={
                "phases": "\n".join(lines),
                "factor": number_text(FUSE_OVER_FACTOR, self.hass.config.language),
                "hours": str(FUSE_OVER_HOURS),
                "days": str(FORECAST_DAYS),
            },
        )

    @callback
    def _car_power_changed(self, _event: Event) -> None:
        """A charger's power or status changed: without (working) phase sensors these are the measurement (a car that
        starts to draw, or whose status now says it charges)."""
        if not self._metered():
            self._check_overload()

    @callback
    def _check_overload(self) -> None:
        if self._metered():
            self._limit_on_overload(self._measured())
        else:
            # the house too, when phase sensors are configured but measure nothing now (_house)
            self._limit_on_overload([car + house for car, house in zip(self._cars_draw(), self._house())])

    def _schedule_overload_recheck(self) -> None:
        """Check again once the throttle has passed: a sensor that doesn't change again sends no new event."""
        if self._overload_recheck is not None:
            return

        @callback
        def _run(_now) -> None:
            self._overload_recheck = None
            self._check_overload()

        self._overload_recheck = async_call_later(self.hass, OVERLOAD_THROTTLE + 1, _run)

    def _track_car_power(self) -> None:
        """Follow the members' charger power and status sensors (the measurement when the phase sensors measure nothing)."""
        if self._power_unsub is not None:
            self._power_unsub()
            self._power_unsub = None
        entities = sorted({
            c.config["charger"]["entity_ids"].get(key) or "" for c in self.members.values() for key in ("charger_power_entity_id", "status_entity_id")
        } - {""})
        if entities:
            unsubs = [
                async_track_state_change_event(self.hass, entities, self._car_power_changed),
                # a sensor that goes silent sends no event: its car counts with its request again once it is stale
                # (_settled), so the check also runs on a timer
                async_track_time_interval(self.hass, self._car_power_changed, datetime.timedelta(seconds=STALE_CHECK_SECONDS)),
            ]

            def _unsub() -> None:
                for unsub in unsubs:
                    unsub()

            self._power_unsub = _unsub

    def _limit_on_overload(self, measured: list[float] | None) -> None:
        """A phase above the fuse limits the last charging car at once."""
        if measured is None:
            return
        fuses = [float(self.config.get(f"fuse_l{i}") or 0.0) for i in (1, 2, 3)]
        if not any(m > f for m, f in zip(measured, fuses)):
            return
        # still above the fuse after the reaction (or throttled): check again, while there is a car to limit (the house
        # alone above the fuse is nothing CJP can change)
        if any(sum(self.requests.get(entry_id) or [0]) > 0 for entry_id in self.members):
            self._schedule_overload_recheck()
        for entry_id in reversed(self.order()):
            if sum(self.requests.get(entry_id) or [0]) <= 0:
                continue
            if time.monotonic() - self._last_overload.get(entry_id, 0.0) < OVERLOAD_THROTTLE:
                return
            self._last_overload[entry_id] = time.monotonic()
            coordinator = self.members[entry_id]
            _LOGGER.warning("Site: phase current %s A above the fuse %s A, limiting %s", [round(m, 1) for m in measured], fuses, coordinator.prefix)
            coordinator.async_site_limit()
            return

    # ------------------------------------------------------------------ #
    # House forecast (hour of day mean of the last week, without the cars)
    # ------------------------------------------------------------------ #

    @callback
    def _refresh_forecast(self, _now) -> None:
        self.entry.async_create_background_task(self.hass, self._async_refresh_forecast(), name="cjp_site_forecast")

    async def _async_refresh_forecast(self) -> None:
        if "recorder" not in self.hass.config.components or not self.has_phase_sensors:
            return
        from homeassistant.components.recorder import get_instance  # noqa: PLC0415
        from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period  # noqa: PLC0415

        end = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        start = end - datetime.timedelta(days=FORECAST_DAYS)
        chargers = {e: c.config["charger"]["entity_ids"]["charger_power_entity_id"] for e, c in self.members.items()}
        ids = {*self.phase_entities, *(c for c in chargers.values() if c)}
        try:
            # fixed units: without them HA converts to the state's unit now, which may not be the recorded one (kW read as W
            # would make the forecast, the fallback for a meter that measures nothing, 1000 times too low)
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period, self.hass, start, end, ids, "hour", FORECAST_UNITS, {"mean"}
            )
            metadata = await get_instance(self.hass).async_add_executor_job(partial(get_metadata, self.hass, statistic_ids=ids))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Site: could not load statistics: %s", err)
            return
        voltage = float(self.config.get("voltage") or 230.0)
        units = {statistic_id: meta.get("unit_of_measurement") for statistic_id, (_, meta) in metadata.items()}

        def series(entity_id, to_amps):
            result = {}
            for row in stats.get(entity_id, []):
                ts = row.get("start")
                ts = ts.timestamp() if isinstance(ts, datetime.datetime) else ts
                if ts is not None and row.get("mean") is not None:
                    result[ts] = to_amps(float(row["mean"]))
            return result

        def statistic_unit(entity_id):
            # the unit the statistics were recorded in (a power statistic comes in W, a current in A, see FORECAST_UNITS). The
            # state is only the fallback: right after a restart it may not be there yet
            unit = units.get(entity_id)
            if not unit:
                state = self.hass.states.get(entity_id)
                unit = state.attributes.get("unit_of_measurement") if state else None
            return unit

        phases = []
        for entity_id in self.phase_entities:
            unit = statistic_unit(entity_id)
            if not unit and stats.get(entity_id):
                # W read as A would leave no current for the cars: keep the previous forecast
                _LOGGER.debug("Site: unit of %s unknown, house forecast not updated", entity_id)
                return
            if _energy_unit(unit):
                # an energy is no current (the repair ISSUE_PHASE_UNITS): keep the previous forecast. The statistics keep
                # the unit they were recorded in until the recorder's own repair is fixed, also when the sensor now
                # reports a current, so this is logged where the user sees it (once per sensor, the forecast runs hourly)
                if entity_id in self._forecast_energy_warned:
                    _LOGGER.debug("Site: %s is in the energy unit %s, house forecast not updated", entity_id, unit)
                    return
                self._forecast_energy_warned.add(entity_id)
                _LOGGER.warning(
                    "Site: the statistics of %s are in the energy unit %s, the house forecast is not updated. If the sensor "
                    "now reports a current or a power, fix the recorder's repair for its statistics",
                    entity_id,
                    unit,
                )
                return
            # a power statistic comes in W (FORECAST_UNITS) and is W on the phase's voltage. A current (A, mA) and any other
            # unit are amps as before (the fuse)
            watt = statistic_power_factor_to_w(unit) if unit else None
            factor = watt / voltage if watt is not None else 1.0
            phases.append(series(entity_id, lambda v, f=factor: abs(v) * f))
        # the repair for a main fuse the house and the cars exceed: judged on the phases before the cars are taken off (the fuse
        # carries both). Information only: an error is logged and never holds the forecast up
        try:
            self._check_fuse(phases)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Site: the main fuse check failed")
        cars = []
        for entry_id, entity_id in chargers.items():
            coordinator = self.members[entry_id]
            unit = statistic_unit(entity_id) if entity_id else None
            # a charger's power in W; no unit, an energy unit (a meter picked as the power sensor?) or another unit is no
            # reading: nothing is subtracted, so the house forecast keeps the car's draw (conservative). Never more than the
            # charger can draw per phase, like _car_draw
            watt = statistic_power_factor_to_w(unit) if unit and not _energy_unit(unit) else None
            car_voltage = float(coordinator.config["charger"]["power_voltage"]) or 230.0
            car_phases = int(coordinator.config["charger"]["charging_phases"])
            max_amp = float_or_none(coordinator.config["charger"].get("charging_max_amp"))
            limit = max_amp * (1 + OVERDRAW_SHARE) if max_amp else math.inf
            draw = series(
                entity_id, lambda v, w=watt, cv=car_voltage, cp=car_phases, lim=limit: min(abs(v) * w / cv / cp, lim)
            ) if watt is not None else {}
            cars.append((self._order(coordinator)[:car_phases], draw))
        sums: dict[int, list[float]] = {}
        counts: dict[int, int] = {}
        for ts in set(phases[0]) & set(phases[1]) & set(phases[2]):
            values = [phases[i][ts] for i in range(3)]
            for grid_phases, draw in cars:
                for grid_phase in grid_phases:
                    values[grid_phase] -= draw.get(ts, 0.0)
            hour = dt_util.as_local(datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)).hour
            sums[hour] = [a + max(b, 0.0) for a, b in zip(sums.get(hour, [0.0, 0.0, 0.0]), values)]
            counts[hour] = counts.get(hour, 0) + 1
        self.house_forecast = {hour: [round(v / counts[hour], 2) for v in values] for hour, values in sums.items()}
        _LOGGER.debug("Site: house forecast per hour of day %s", self.house_forecast)
