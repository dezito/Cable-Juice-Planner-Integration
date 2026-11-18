"""Suggest three settings from the recorder while the config flow is open (nothing is saved or decided here).

* the sign of the battery power (does a positive value mean charging?),
* whether the house consumption sensor includes the power that charges the battery,
* whether it includes the part of the load the battery covers when it discharges.

The sign is read from the battery level: it rises while the power has one sign. That needs an unbroken stretch of 1, 2
or 3 hours in which the level moves.

The other two are read from the *switch-overs*: the moment the battery goes from idle to charging (or discharging), or
back. The consumption is compared in the 10 minutes before and the 10 minutes after the switch-over and the step is set
against the step in battery power. A sensor of the total load of the house does not step (the battery covers a load that
is there anyway); a sensor derived from grid and solar steps up by the charging power and down by the discharging power.
Because only the minutes around a switch-over are compared, a load that differs between the evening and the night, or an
EV charging in the same hours, does not count, and a battery that sells to the grid does not look like a sensor that
excludes the discharging. A switch-over takes about 25 minutes of unbroken history, but one is not enough: at least
MIN_EDGES of the latest ones must agree (and nearly all of what is looked at), otherwise the setting stays unknown.

How far back it looks: the history is read a day at a time, newest first, for up to LOOKBACK_DAYS (the recorder may keep
less), until everything is decided. The stretches and switch-overs used are the latest the history has that are clear,
so a few weeks back is fine and a long unbroken period is not required. What the history cannot decide stays unknown, and
the caller keeps the current value: unknown is never turned into a favourable default. A sensor that never changes, or
whose unit is unknown, decides nothing.

The same file is in Cable Juice Planner; keep them in sync.
"""
from __future__ import annotations

import asyncio
import bisect
import datetime
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import partial

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

Rows = list[tuple[float, float | None]]
Bins = list[tuple[float, float, float] | None]

BIN_SECONDS = 300
WINDOW_HOURS = (1, 2, 3)  # the lengths of the unbroken stretches tried for the sign, the shortest first
STEP_BINS = 6  # a stretch is tried every half hour
LOOKBACK_DAYS = 21  # how far back the history is searched
CHUNK_HOURS = 24  # the history is read this much at a time, newest first, and reading stops when everything is decided
IDLE_W = 100.0  # below this the battery is idle
ACTIVE_W = 300.0  # from this the battery charges or discharges
MIN_FILLED = 3  # of the 5 minutes of a bin that must have a value
MIN_SOC_MOVE = 2.0  # percentage points the level must move while the battery flows before the sign is decided
AGREE = 0.8  # share of the level movement that must agree with the sign
STEADY = 0.3  # the two bins of a battery that flows may differ by this share of their mean
MIN_EDGES = 4  # switch-overs that must agree before an include setting is decided
MAX_EDGES = 8  # the latest switch-overs looked at; if they do not agree it stays unknown
EDGE_AGREE = 0.8  # share of the switch-overs looked at that must agree
STEP_FOLLOWS = 0.6  # a step of at least this share of the battery power: the sensor follows the battery (grid and solar)
STEP_FLAT = 0.25  # a step of at most this share: the sensor does not follow the battery (total load)
NO_EXPORT = 0.8  # a load that covers the battery is at least this share of its power; below that the battery sells to the grid
SHOWN_FROM_HOURS = 6  # how far back an answer reaches before the page says so
LOAD_TIMEOUT = 10  # seconds for one read of the recorder
TOTAL_TIMEOUT = 25  # seconds for all reads together; what has been read by then is used
_POWER_FACTORS_TO_W = {"mW": 0.001, "W": 1.0, "kW": 1000.0, "MW": 1000000.0, "GW": 1000000000.0}
NOTHING = "–"


@dataclass(frozen=True)
class Verdict:
    """True / False when the history decides the setting, None when it cannot."""

    value: bool | None = None
    window: int | None = None  # the sign: hours of the unbroken stretch; the include settings: the number of switch-overs
    numbers: Mapping[str, float | None] = field(default_factory=dict)
    age: float | None = None  # hours between now and the oldest history the answer rests on


@dataclass(frozen=True)
class Detection:
    invert: Verdict = Verdict()
    include_charging: Verdict = Verdict()
    include_discharging: Verdict = Verdict()
    reason: str | None = None  # why nothing could be looked at (no recorder, no data ...)


def unit_factor_to_w(unit: str | None) -> float | None:
    """The factor from the unit of a power sensor to W; None for a unit that is not a power (then the sensor decides nothing)."""
    return _POWER_FACTORS_TO_W.get(unit or "")


# ---------------------------------------------------------------------------------------------------------------------
# Pure part: series -> bins -> verdicts
# ---------------------------------------------------------------------------------------------------------------------


def _grid(rows: Rows, start: float, minutes: int) -> list[float | None]:
    """The value of every minute from start: the last value at or before it (a state stays until the next one)."""
    stamps = [row[0] for row in rows]
    values: list[float | None] = []
    for minute in range(minutes):
        moment = start + minute * 60 + 59  # the value the minute ended with
        index = bisect.bisect_right(stamps, moment) - 1
        value = rows[index][1] if index >= 0 else None
        values.append(value if value is not None and math.isfinite(value) else None)
    return values


def _bins(consumption: Rows, power: Rows, level: Rows, end: float, hours: int) -> Bins:
    """(consumption W, battery power W, battery level %) per 5 minutes, None where one of the three is not known."""
    minutes = hours * 60
    start = end - minutes * 60
    grids = [_grid(rows, start, minutes) for rows in (consumption, power, level)]
    per_bin = BIN_SECONDS // 60
    result: list[tuple[float, float, float] | None] = []
    for first in range(0, minutes - per_bin + 1, per_bin):
        means = []
        for grid in grids:
            known = [value for value in grid[first : first + per_bin] if value is not None]
            means.append(sum(known) / len(known) if len(known) >= MIN_FILLED else None)
        result.append((means[0], means[1], means[2]) if None not in means else None)  # type: ignore[arg-type]
    return result


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _invert(bins: Bins) -> Verdict:
    """Does a rise of the level go with positive power? The level movement between two bins against the power between them."""
    valid = [item for item in bins if item is not None]
    numbers: dict[str, float | None] = {
        "soc_start": valid[0][2] if valid else None,
        "soc_end": valid[-1][2] if valid else None,
        "power_avg": _mean([item[1] for item in valid]),
    }
    moved = agreeing = 0.0
    for before, after in zip(bins, bins[1:]):
        if before is None or after is None:
            continue
        power = (before[1] + after[1]) / 2
        change = after[2] - before[2]
        if abs(power) < ACTIVE_W or change == 0:
            continue
        moved += abs(change)
        if (change > 0) == (power > 0):
            agreeing += abs(change)
    if moved < MIN_SOC_MOVE:
        return Verdict(None, None, numbers)
    share = agreeing / moved
    if share >= AGREE:
        return Verdict(True, None, numbers)  # positive power = charging
    if share <= 1 - AGREE:
        return Verdict(False, None, numbers)
    return Verdict(None, None, numbers)


def _has_numbers(verdict: Verdict) -> bool:
    return any(number is not None for number in verdict.numbers.values())


def _merge(old: Verdict, new: Verdict) -> Verdict:
    """What an older part of the history found, behind what the newer part found."""
    if old.value is not None:
        return old
    if new.value is not None:
        return new
    return old if _has_numbers(old) else new


def decide_invert(bins: Bins, limit: int | None = None) -> Verdict:
    """The sign from the latest unbroken stretch of 1, 2 or 3 hours that decides it.

    Stretches that end every half hour are tried from now backwards, the shortest first at each end. limit is for a search
    that already covered the newer bins: only stretches that start before bin number limit are tried.
    """
    per_hour = 3600 // BIN_SECONDS
    total = len(bins)
    verdict = Verdict()
    for end in range(total, 0, -STEP_BINS):
        for hours in WINDOW_HOURS:
            first = end - hours * per_hour
            if first < 0 or (limit is not None and first >= limit):
                continue
            found = _invert(bins[first:end])
            if found.value is not None:
                return Verdict(found.value, hours, found.numbers, (total - end) * BIN_SECONDS / 3600)
            if not _has_numbers(verdict):
                verdict = Verdict(None, None, found.numbers)
    return verdict


@dataclass(frozen=True)
class _Edge:
    first: int  # the first bin of the five the switch-over was read from
    c_active: float  # consumption in the two bins where the battery flows
    c_idle: float  # consumption in the two bins where it is idle
    flow: float  # the battery power there, W
    step: float  # (c_active - c_idle) / flow


def _kind(item: tuple[float, float, float] | None, positive_is_charging: bool, charging: bool) -> tuple[str, float, float] | None:
    """('idle' | 'active' | 'other', consumption, power of the direction looked at) for a bin."""
    if item is None:
        return None
    consumption, power, _level = item
    charge = max(power if positive_is_charging else -power, 0.0)
    discharge = max(-power if positive_is_charging else power, 0.0)
    flow, other = (charge, discharge) if charging else (discharge, charge)
    if charge < IDLE_W and discharge < IDLE_W:
        return "idle", consumption, flow
    if flow >= ACTIVE_W and other < IDLE_W:
        return "active", consumption, flow
    return "other", consumption, flow


def _edges(bins: Bins, positive_is_charging: bool, charging: bool) -> list[_Edge]:
    """The clear switch-overs between idle and charging (or discharging), the newest first.

    Five bins make one: two bins of one kind, one in between (the switch-over itself, however it falls in the bins) and
    two bins of the other kind. The bins that flow must be steady and the consumption must have moved at all (a sensor
    that stands still decides nothing).
    """
    kinds = [_kind(item, positive_is_charging, charging) for item in bins]
    edges: list[_Edge] = []
    first = len(bins) - 5
    while first >= 0:
        window = kinds[first : first + 5]
        pair_a = [window[0], window[1]]
        pair_b = [window[3], window[4]]
        accepted = None
        if None not in pair_a and None not in pair_b:
            kinds_a = {item[0] for item in pair_a}  # type: ignore[index]
            kinds_b = {item[0] for item in pair_b}  # type: ignore[index]
            if kinds_a == {"idle"} and kinds_b == {"active"}:
                accepted = (pair_b, pair_a)
            elif kinds_a == {"active"} and kinds_b == {"idle"}:
                accepted = (pair_a, pair_b)
        if accepted is not None:
            active, idle = accepted
            flows = [item[2] for item in active]  # type: ignore[index]
            c_active = sum(item[1] for item in active) / 2  # type: ignore[index]
            c_idle = sum(item[1] for item in idle) / 2  # type: ignore[index]
            flow = sum(flows) / 2 - sum(item[2] for item in idle) / 2  # type: ignore[index]
            values = [item[1] for item in active + idle]  # type: ignore[index]
            if flow >= ACTIVE_W and abs(flows[0] - flows[1]) <= STEADY * (sum(flows) / 2) and max(values) - min(values) > 0.5:
                edges.append(_Edge(first, c_active, c_idle, flow, (c_active - c_idle) / flow))
                first -= 5
                continue
        first -= 1
    return edges


def _says_included(edge: _Edge, charging: bool) -> bool | None:
    """Does the step in consumption at this switch-over say that the sensor includes the battery power?"""
    # a sensor from grid and solar steps up by the charging power and down by the discharging power; the total load does not step
    step = edge.step if charging else -edge.step
    covered = edge.c_active >= NO_EXPORT * edge.flow
    if step >= STEP_FOLLOWS:
        # follows the battery: includes the charging (then it is never below the charging power), excludes the discharging
        return (True if covered else None) if charging else False
    if step <= STEP_FLAT:
        # does not follow it: excludes the charging, includes the discharging - unless the battery sells to the grid or the
        # load follows the battery (a sensor from grid and solar then also stays flat), which a load below the battery power shows
        return False if charging else (True if covered else None)
    return None


def decide_include(bins: Bins, positive_is_charging: bool | None, charging: bool) -> Verdict:
    """Does the consumption include the charging (charging=True) or the discharging (False)? From the latest switch-overs.

    True means the sensor includes the power of the battery in that direction (a sensor of the total load includes the
    discharging and excludes the charging; a sensor derived from grid and solar the other way round).
    """
    if positive_is_charging is None:
        return Verdict()
    edges = _edges(bins, positive_is_charging, charging)[:MAX_EDGES]
    if not edges:
        return Verdict()
    key = "charging" if charging else "discharging"

    def shown(used: list[_Edge]) -> dict[str, float | None]:
        return {
            f"c_{key}": _mean([edge.c_active for edge in used]),
            "c_idle": _mean([edge.c_idle for edge in used]),
            f"p_{key}": _mean([edge.flow for edge in used]),
        }

    says = [_says_included(edge, charging) for edge in edges]
    for count in range(MIN_EDGES, len(edges) + 1):
        for value in (True, False):
            if sum(1 for item in says[:count] if item is value) >= EDGE_AGREE * count:
                return Verdict(value, count, shown(edges[:count]), (len(bins) - edges[count - 1].first) * BIN_SECONDS / 3600)
    return Verdict(None, None, shown(edges))


def decide(bins: Bins, trusted_positive_is_charging: bool | None = None) -> Detection:
    """The three verdicts from 5 minute bins (the last one is now).

    The sign of the battery power for the two include settings is the one the history decides; only when it cannot is
    trusted_positive_is_charging used (the setting as it is now, when the battery power sensor has not been changed).
    Without any sign the two include settings stay unknown.
    """
    invert = decide_invert(bins)
    sign = invert.value if invert.value is not None else trusted_positive_is_charging
    return Detection(invert, decide_include(bins, sign, True), decide_include(bins, sign, False))


def detect(
    consumption: Rows,
    power: Rows,
    level: Rows,
    end: float,
    trusted_positive_is_charging: bool | None = None,
    hours: int = 3,
) -> Detection:
    """The three verdicts from series in W and %, covering the hours before the timestamp end (UTC seconds)."""
    return decide(_bins(consumption, power, level, end, hours), trusted_positive_is_charging)


# ---------------------------------------------------------------------------------------------------------------------
# Loading part: the recorder
# ---------------------------------------------------------------------------------------------------------------------


async def async_load_rows(hass: HomeAssistant, entity_ids: list[str], start: datetime.datetime, end: datetime.datetime) -> dict[str, Rows] | None:
    """The states of the entities between start and end as (timestamp, number or None); None without a recorder."""
    if "recorder" not in hass.config.components:
        return None
    from homeassistant.components.recorder import get_instance, history  # noqa: PLC0415

    states = await get_instance(hass).async_add_executor_job(
        partial(
            history.get_significant_states,
            hass,
            start,
            end,
            entity_ids,
            None,
            include_start_time_state=True,
            significant_changes_only=False,
            minimal_response=False,
            no_attributes=True,
        )
    )
    result: dict[str, Rows] = {}
    for entity_id in entity_ids:
        rows: Rows = []
        for state in states.get(entity_id, []):
            try:
                value: float | None = float(state.state)
            except (TypeError, ValueError):
                value = None
            rows.append((state.last_updated.timestamp(), value))
        result[entity_id] = rows
    return result


def _factor(hass: HomeAssistant, entity_id: str) -> float | None:
    state = hass.states.get(entity_id)
    return unit_factor_to_w(state.attributes.get("unit_of_measurement") if state else None)


def _read_day(
    loaded: dict[str, Rows],
    entities: list[str],
    factors: tuple[float, float],
    chunk_end: float,
    bins: Bins,
    invert: Verdict,
    day: int,
) -> tuple[Bins, Verdict, Verdict | None, Verdict | None]:
    """Add a day of rows (older than the bins so far) and decide what can be decided; the include verdicts when the sign is known."""
    consumption, power, level = (loaded[entity_id] for entity_id in entities)
    scaled = [
        [(stamp, value * factor if value is not None else None) for stamp, value in rows]
        for rows, factor in ((consumption, factors[0]), (power, factors[1]))
    ]
    new_bins = _bins(scaled[0], scaled[1], list(level), chunk_end, CHUNK_HOURS)
    bins = new_bins + bins
    if invert.value is None:
        invert = _merge(invert, decide_invert(bins, len(new_bins) if day else None))
    if invert.value is None:
        return bins, invert, None, None
    # the include settings are read from all the switch-overs found so far: the newest ones stay the same
    return bins, invert, decide_include(bins, invert.value, True), decide_include(bins, invert.value, False)


async def async_detect(
    hass: HomeAssistant,
    consumption_entity: str,
    power_entity: str,
    level_entity: str,
    trusted_positive_is_charging: bool | None = None,
    now: datetime.datetime | None = None,
) -> Detection:
    """Look at the history of the three sensors, a day at a time from now backwards, until every setting is decided.

    Never raises: no recorder, a timeout, an unknown unit or too little data is 'unknown'. Reading stops at LOOKBACK_DAYS,
    at the end of the history the recorder has, or when TOTAL_TIMEOUT has passed (what was read by then is used).
    """
    if not (consumption_entity and power_entity and level_entity):
        return Detection(reason="entities")
    consumption_factor, power_factor = _factor(hass, consumption_entity), _factor(hass, power_entity)
    if consumption_factor is None or power_factor is None:
        return Detection(reason="unit")  # a power in an unknown unit would be compared in the wrong size
    entities = [consumption_entity, power_entity, level_entity]
    end = now or datetime.datetime.now(datetime.timezone.utc)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TOTAL_TIMEOUT
    invert = Verdict()
    bins: Bins = []
    for day in range(LOOKBACK_DAYS * 24 // CHUNK_HOURS):
        chunk_end = end - datetime.timedelta(hours=CHUNK_HOURS * day)
        chunk_start = chunk_end - datetime.timedelta(hours=CHUNK_HOURS)
        try:
            async with asyncio.timeout(max(0.001, min(LOAD_TIMEOUT, deadline - loop.time()))):
                loaded = await async_load_rows(hass, entities, chunk_start, chunk_end)
        except TimeoutError:
            _LOGGER.debug("Reading the history for the suggestions took longer than %s s", LOAD_TIMEOUT)
            if day == 0:
                return Detection(reason="timeout")
            break
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not read the history for the suggestions: %s", err)
            if day == 0:
                return Detection(reason="error")
            break
        if loaded is None:
            return Detection(reason="recorder")
        if not any(loaded[entity_id] for entity_id in entities):
            break  # nothing older is kept
        try:
            # the arithmetic of a day of history is done outside the event loop
            bins, invert, charging, discharging = await asyncio.to_thread(
                _read_day, loaded, entities, (consumption_factor, power_factor), chunk_end.timestamp(), bins, invert, day
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not read the history for the suggestions: %s", err)
            return Detection(reason="error")
        if charging is not None and discharging is not None and charging.value is not None and discharging.value is not None:
            return Detection(invert, charging, discharging)
        if loop.time() >= deadline:
            break
        await asyncio.sleep(0)  # let the event loop breathe between days

    # the history decided no sign (or not all the settings): the sign of the setting as it is now stands in for it
    sign = invert.value if invert.value is not None else trusted_positive_is_charging
    try:
        found = await asyncio.to_thread(lambda: Detection(invert, decide_include(bins, sign, True), decide_include(bins, sign, False)))
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not read the history for the suggestions: %s", err)
        return Detection(reason="error")
    verdicts = (found.invert, found.include_charging, found.include_discharging)
    if all(verdict.value is None and all(number is None for number in verdict.numbers.values()) for verdict in verdicts):
        return Detection(reason="data")
    return found


# ---------------------------------------------------------------------------------------------------------------------
# For the page: a mark and the numbers per setting
# ---------------------------------------------------------------------------------------------------------------------

MARK_ON = "✅"
MARK_OFF = "⬜"
MARK_UNKNOWN = "❔"


def _number(value: float | None) -> str:
    return NOTHING if value is None else f"{value:.0f}"


def _mark(verdict: Verdict) -> str:
    return MARK_UNKNOWN if verdict.value is None else (MARK_ON if verdict.value else MARK_OFF)


def _window(verdict: Verdict, switch_overs: bool) -> str:
    """What the answer rests on, and how far back it reaches when that is not just now: '2 h', 'n=4, -9 h', '2 h, -9 d'."""
    if verdict.window is None:
        return NOTHING
    text = f"n={verdict.window}" if switch_overs else f"{verdict.window} h"
    if verdict.age is not None and verdict.age >= SHOWN_FROM_HOURS:
        text += f", -{round(verdict.age)} h" if verdict.age < 48 else f", -{round(verdict.age / 24)} d"
    return text


def evidence_placeholders(detection: Detection | None) -> dict[str, str]:
    """The placeholders of the review page: language neutral marks and numbers (the words are in the texts)."""
    detection = detection or Detection()
    invert, charging, discharging = detection.invert, detection.include_charging, detection.include_discharging
    return {
        "invert_mark": _mark(invert),
        "invert_window": _window(invert, False),
        "soc_start": _number(invert.numbers.get("soc_start")),
        "soc_end": _number(invert.numbers.get("soc_end")),
        "power_avg": _number(invert.numbers.get("power_avg")),
        "charging_mark": _mark(charging),
        "charging_window": _window(charging, True),
        "c_charging": _number(charging.numbers.get("c_charging")),
        "c_idle_charging": _number(charging.numbers.get("c_idle")),
        "p_charging": _number(charging.numbers.get("p_charging")),
        "discharging_mark": _mark(discharging),
        "discharging_window": _window(discharging, True),
        "c_discharging": _number(discharging.numbers.get("c_discharging")),
        "c_idle_discharging": _number(discharging.numbers.get("c_idle")),
        "p_discharging": _number(discharging.numbers.get("p_discharging")),
    }


def verdict_values(detection: Detection) -> dict[str, bool]:
    """The settings the history decided, by option key (the undecided ones are left out: they keep their value)."""
    decided = {
        "invert_powerwall_watt_flow_entity_id": detection.invert.value,
        "power_consumption_entity_id_include_powerwall_charging": detection.include_charging.value,
        "power_consumption_entity_id_include_powerwall_discharging": detection.include_discharging.value,
    }
    return {key: value for key, value in decided.items() if value is not None}
