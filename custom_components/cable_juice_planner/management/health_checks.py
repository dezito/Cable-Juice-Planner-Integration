"""Health checks: repairs for what Cable Juice Planner needs and for what has gone wrong with it (one per car or battery).

The watcher only reads. It never calls a service and never changes a device, the configuration or automations.yaml. A repair
is raised when something is wrong and removed only when it is positively fixed. Whatever can't be judged yet (before Home
Assistant has started, a state that is unavailable or unknown, an automations.yaml that can't be read) leaves the repair as it
is: Home Assistant keeps an "Ignore" while the repair stays, and a repair that is deleted and raised again opens again.

The repairs (the keys of "issues" in strings.json):
- entities_missing: a configured entity is disabled, or neither in the entity registry nor in the states.
- entity_units: a power, battery level or kWh meter sensor has a unit that the code can't read.
- blueprint_automation_lost: the automation made from the charger blueprint was seen running, and is now off or gone.
- charger_status_unknown: the charger's status is a value CJP does not know, and it has been held for a while.
- charger_not_following: the charger repeatedly did not follow the plan (the restarts gave up, or it charged without a rule).
- power_without_unit: a power sensor without a unit whose values look like kW (read as W it is 1000 times too small).
- battery_level_fraction: a battery level sensor whose values look like a share (0..1), not a percentage.
- price_unit_scale: a price sensor in a subunit of its currency (øre, ct), per MWh or per Wh (no price is converted).
- consumption_frozen: the house consumption power sensor has shown the same value for FROZEN_AFTER or longer (the plan uses it for
  the house). Only that sensor is judged: the others (solar, battery flow, charger power) are legitimately 0 for hours.
- charger_outside_house_meter: the house consumption looks not to include this car's charger while the option
  charger_in_house_meter says it does (the charger is then subtracted from a house that never had it). The engine judges it
  from the stored hours (engine/energy/meter_check.py, JobResult.charger_meter); the option set to False removes it.

The three value repairs are judged by management/value_checks.py (the same file as in BattMind): from the state, and from the
history of the last few days, read at most once per VALUE_RECHECK. They are only raised and removed here, never fixed. The
sensors flagged by the two scale repairs (and their highest value) are kept in the health store once their list is known
(every sensor judged in one check, read intact from the store, or no repair and no store yet), so a restart keeps the repair
until they are judged fine.
The price repair asks the price provider (price_problems).

The state that must survive a restart is in a store of its own (not in the backups). Removing the entry deletes the repairs
(management/dependency_checks.py remove_issues) and the store (async_remove_health_store).
"""
from __future__ import annotations

import asyncio
import copy
import datetime
import logging
import math
from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.components.automation import EVENT_AUTOMATION_RELOADED
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    CHARGER_CHARGING_STATUS,
    CHARGER_COMPLETED_STATUS,
    CHARGER_NOT_READY_STATUS,
    CHARGER_READY_STATUS,
    DOMAIN,
    ENTITY_UNAVAILABLE_STATES,
    StateSet,
)
from ..utils.power import POWER_UNITS, power_unit_known
from .automation_inputs import blueprint_automation_ids
from .blueprint_install import ISSUE_PREFIX as BLUEPRINT_UPDATE_PREFIX
from .value_checks import (
    KW_LOOKING_MAX,
    POWER_SCALE_DAYS,
    VALUE_RECHECK,
    async_recorded_numbers,
    battery_fraction_verdict,
    could_be_fraction,
    could_be_kw,
    number_text,
    power_scale_verdict,
)

_LOGGER = logging.getLogger(__name__)

ISSUE_MISSING = "entities_missing"
ISSUE_UNITS = "entity_units"
ISSUE_BLUEPRINT = "blueprint_automation_lost"
ISSUE_STATUS = "charger_status_unknown"
ISSUE_NOT_FOLLOWING = "charger_not_following"
ISSUE_POWER_UNIT = "power_without_unit"
ISSUE_BATTERY_FRACTION = "battery_level_fraction"
ISSUE_PRICE_SCALE = "price_unit_scale"
ISSUE_CONSUMPTION_FROZEN = "consumption_frozen"
ISSUE_CHARGER_OUTSIDE_METER = "charger_outside_house_meter"
HEALTH_ISSUES = (
    ISSUE_MISSING,
    ISSUE_UNITS,
    ISSUE_BLUEPRINT,
    ISSUE_STATUS,
    ISSUE_NOT_FOLLOWING,
    ISSUE_POWER_UNIT,
    ISSUE_BATTERY_FRACTION,
    ISSUE_PRICE_SCALE,
    ISSUE_CONSUMPTION_FROZEN,
    ISSUE_CHARGER_OUTSIDE_METER,
)

STORE_VERSION = 1
NOTHING = "–"

# Home Assistant reloads an integration about 30 seconds after one of its entities was enabled or disabled
RECHECK_DELAY = 60
RECHECK_INTERVAL = datetime.timedelta(hours=1)
# a charger status must be held this long before it counts as unknown: a status can change within seconds while a car is plugged in
STATUS_UNKNOWN_MINUTES = 10
# seconds after a change of the status: the first moment at which a value that changed can have been held that long
STATUS_RECHECK_DELAY = STATUS_UNKNOWN_MINUTES * 60 + 30
# a value that was seen unknown stays in its repair this many days after it was last seen
STATUS_UNKNOWN_DAYS = 7
# this many charger events in NOT_FOLLOWING_DAYS days raise the repair (each event already has its own notification)
NOT_FOLLOWING_EVENTS = 3
NOT_FOLLOWING_DAYS = 7
# the latest events the repair lists
NOT_FOLLOWING_LISTED = 5
CHARGER_EVENTS = ("gave_up", "no_rule")
# the house consumption sensor that has shown the same value this long (its last_changed) is a repair: the plan reads it for the
# house, so a stuck sensor makes the plan wrong. Only that sensor: the solar, battery flow and charger power sensors are 0 for hours
FROZEN_AFTER = datetime.timedelta(hours=6)
# the startup grace of consumption_frozen: for this long after the start nothing is deleted or replaced, and the state listener
# ignores the house sensor (a template can show 0 while Home Assistant starts). It ends at the first check, at or after this time
# from the start, that reads a number (or that finds no sensor configured, or another one)
CONSUMPTION_GRACE = datetime.timedelta(minutes=30)

# the units the code reads. Power: every reader uses utils/power.py (mW, W, kW, MW, GW; a unit with an h such as kWh is read
# as the unit without it, as in BattMind). The charger power too, except while the installed charger blueprint is older than
# the bundled one (its update repair is open): that blueprint reads only W and kW, any other unit as W. Energy: the history
# and the sessions read the kWh meters raw (only the charging loss and engine/battery.py convert Wh and MWh), so only kWh
# passes. Battery levels: %.
OLD_BLUEPRINT_POWER_UNITS = ("W", "kW")
CHARGER_BLUEPRINT_UPDATE_ISSUE = f"{BLUEPRINT_UPDATE_PREFIX}charger_control"
ENERGY_UNIT = "kWh"
BATTERY_UNITS = ("%",)

# the statuses CJP knows (const/base.py), "error" (a restart has its own rules), and the unavailable ones (not a value to judge)
KNOWN_STATUS = StateSet(
    *CHARGER_READY_STATUS, *CHARGER_NOT_READY_STATUS, *CHARGER_COMPLETED_STATUS, *CHARGER_CHARGING_STATUS, "error", *ENTITY_UNAVAILABLE_STATES
)
# the states of an automation that are judged: on (in use), off (disabled)
AUTOMATION_STATES = ("on", "off")


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}.health"


SAVES_KEY = f"{DOMAIN}_health_saves"


def _pending_saves(hass: HomeAssistant, entry_id: str) -> set[asyncio.Task[None]]:
    """The saves of an entry still being written. Kept in hass.data, so that a removal or a reload can wait for them."""
    return hass.data.setdefault(SAVES_KEY, {}).setdefault(entry_id, set())


def _utcnow() -> datetime.datetime:
    """The clock of the consumption_frozen checks (tests move it on: a clock that stands still would never end the startup grace)."""
    return dt_util.utcnow()


async def _async_wait_saves(hass: HomeAssistant, entry_id: str) -> None:
    """Wait for the saves in progress. asyncio.wait does not cancel them when the waiting itself is cancelled (gather would)."""
    pending = list(_pending_saves(hass, entry_id))
    if pending:
        await asyncio.wait(pending)


async def async_remove_health_store(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the store of an entry that was removed (async_remove_entry). A save still in progress is waited for first, so it
    can't write the file again after it was deleted. The entry's saves are forgotten afterwards."""
    await _async_wait_saves(hass, entry_id)
    try:
        await Store(hass, STORE_VERSION, store_key(entry_id), private=True, atomic_writes=True).async_remove()
    finally:
        hass.data.get(SAVES_KEY, {}).pop(entry_id, None)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _shown_highest(value: float) -> float:
    """The highest value rounded UP to the digits number_text shows (3 significant digits, whole numbers from 1000), so the
    "≤" in a repair text is never below the real highest value. The rounding is done here: number_text rounds to nearest."""
    if not value > 0:
        return 0.0
    if value >= 1000:
        return float(math.ceil(round(value, 9)))
    scale = 10 ** (2 - math.floor(math.log10(value)))
    return math.ceil(round(value * scale, 9)) / scale


def _has_unit(unit: str) -> bool:
    """A power sensor with any unit is no value to judge by its size: its unit is judged (entity_units), not this."""
    return bool(unit)


def _has_other_battery_unit(unit: str) -> bool:
    """A battery level in % (or without a unit) is judged by its values; in any other unit it is entity_units' business."""
    return unit not in ("", "%")


def _power_unit_ok(unit: str) -> bool:
    return bool(unit) and power_unit_known(unit)


def _energy_unit_ok(unit: str) -> bool:
    return unit == ENERGY_UNIT


def _battery_unit_ok(unit: str) -> bool:
    return unit in BATTERY_UNITS


def _lines(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) or NOTHING


def _parse(text: Any) -> datetime.datetime | None:
    """An ISO time that has a time zone, or None. A naive time can't be compared with the aware ones: it is dropped."""
    if not isinstance(text, str):
        return None
    parsed = dt_util.parse_datetime(text)
    return parsed if parsed is not None and parsed.tzinfo is not None else None


def _local(moment: datetime.datetime) -> str:
    return dt_util.as_local(moment).strftime("%Y-%m-%d %H:%M")


def _stored_event(event: Any) -> bool:
    return isinstance(event, dict) and event.get("kind") in CHARGER_EVENTS and _parse(event.get("at")) is not None


def _state_number(text: Any) -> float | None:
    """A state as a finite number, or None: unavailable, unknown, text, nan and inf are no value to judge."""
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _frozen_since(stored: dict[str, Any], last_changed: datetime.datetime) -> datetime.datetime:
    """The start of the frozen value: the stored one, unless the sensor's last_changed is older (then that one)."""
    kept = _parse(stored["since"])
    return last_changed if kept is None else min(last_changed, kept)


def _stored_consumption(stored: Any) -> dict[str, Any] | None:
    """The frozen house consumption sensor kept in the store (entity, value, since as an ISO time), or None when it is missing or
    unreadable. A since that can't be read is kept as None: the check then uses the sensor's last_changed instead."""
    if not isinstance(stored, dict):
        return None
    entity, value = stored.get("entity"), stored.get("value")
    if not isinstance(entity, str) or not entity or not isinstance(value, str) or _state_number(value) is None:
        return None
    since = stored.get("since")
    return {"entity": entity, "value": value, "since": since if _parse(since) is not None else None}


class HealthWatcher:
    """Keeps the repairs of one car or battery entry up to date: at start, every hour, after a relevant entity or the charger
    automation changed, and when the engine notes a charger event."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        label: str,
        *,
        entities: list[str],
        watched: frozenset[str],
        charger_power_entities: list[str],
        power_entities: list[str],
        battery_entities: list[str],
        energy_entities: list[str],
        status_entity: str,
        action_sensor: str,
        automations_path: str,
        consumption_entity: str = "",
        charger_in_house_meter: bool = True,
        price_problems: Callable[[], tuple[list[tuple[str, str]], bool] | None] | None = None,
        canonical_status: Callable[[Any], Any] | None = None,
    ) -> None:
        """entities: the configured entities (the ones DependencyWatcher judges, in watched, are left out here).
        charger_power/power/battery/energy_entities: the sensors whose unit is read, by kind; a sensor listed twice is judged by
        the first kind (the charger power's rule, which can be stricter). action_sensor: the charger's action sensor the
        blueprint automation uses. automations_path: the automations.yaml the editor writes (read only). consumption_entity:
        the house consumption power sensor (home.entity_ids.power_consumption_entity_id), judged by _check_consumption_frozen.
        charger_in_house_meter: the option of the same name (False: the charger is on its own meter, so there is no
        charger_outside_house_meter repair)."""
        self.hass = hass
        self.entry = entry
        self.label = label
        self._entities = sorted({entity_id for entity_id in entities if entity_id and entity_id not in watched})
        self._consumption_entity = consumption_entity
        self._charger_in_house_meter = charger_in_house_meter
        self._own_charger_power = any(charger_power_entities)
        # the engine's last verdict whether the house consumption includes the charger (engine/energy/meter_check.py), in
        # memory only: the engine judges it again from its stored hours at every start
        self._meter_verdict: dict[str, Any] | None = None
        # set by _consumption_changed when the house sensor went to another number after the grace (in memory only)
        self._consumption_moved = False
        # the startup grace of the house sensor is over (see _consumption_grace_over): until then the listener counts nothing
        self._consumption_over = False
        # when the watcher started (Home Assistant started): the grace is counted from it
        self._started_at: datetime.datetime | None = None
        scope: dict[str, Callable[[str], bool]] = {}
        for accepted, entity_ids in (
            (self._charger_power_unit_ok, charger_power_entities),
            (_power_unit_ok, power_entities),
            (_battery_unit_ok, battery_entities),
            (_energy_unit_ok, energy_entities),
        ):
            for entity_id in entity_ids:
                if entity_id:
                    scope.setdefault(entity_id, accepted)
        self._unit_scope: list[tuple[str, Callable[[str], bool]]] = list(scope.items())
        self._status_entity = status_entity
        # the states the charger blueprint adds count as known (coordinator/status_states.py); the built in ones always do
        self._canonical_status = canonical_status or (lambda value: value)
        self._action_sensor = action_sensor
        self._automations_path = automations_path
        # the sensors judged by their values (value_checks): the power sensors (the charger's too) and the battery levels
        self._power_ids = list(dict.fromkeys(entity_id for entity_id in (*charger_power_entities, *power_entities) if entity_id))
        self._battery_ids = list(dict.fromkeys(entity_id for entity_id in battery_entities if entity_id))
        # the price provider's unit check: (entity id, unit) of each wrong price entity and whether all were judged; None
        # from it while there is no provider yet (not judged)
        self._price_problems = price_problems
        # the verdict per sensor (True looks wrong, False fine; absent or unchanged when it can't be judged now), when its
        # history was read, and the highest absolute value of that history. The flagged ones (True) are restored from the
        # store at load (self._data["scale_flagged"]); the rest is judged again after a restart
        self._scale_verdicts: dict[str, bool] = {}
        self._scale_read: dict[str, datetime.datetime] = {}
        self._scale_max: dict[str, float] = {}
        self._recorder_failed = False  # a failed history read is logged once (a warning), then at debug level
        self._unsubs: list[CALLBACK_TYPE] = []
        self._later: CALLBACK_TYPE | None = None
        self._status_later: CALLBACK_TYPE | None = None
        self._automation_unsub: CALLBACK_TYPE | None = None
        self._automation_entities: frozenset[str] = frozenset()
        # entity -> (unit, ok) of the last judgement; a sensor that can't be judged now keeps its last one
        self._unit_verdicts: dict[str, tuple[str, bool]] = {}
        # scale_flagged: per value repair the sensors flagged, with their highest value, once the list is known (a key that is
        # there, even {}). A known list goes when nothing is flagged, whatever the unclear sensors say; an unknown one is not stored
        # consumption_frozen: the frozen house consumption sensor (entity, value, since) or None; kept in the store across restarts
        self._data: dict[str, Any] = {
            "blueprint_seen": False,
            "status_unknown": {},
            "charger_events": [],
            "scale_flagged": {},
            "consumption_frozen": None,
        }
        self._ha_started = False
        self._stopped = False
        self._lock = asyncio.Lock()
        self._save_lock = asyncio.Lock()
        self._store = Store(hass, STORE_VERSION, store_key(entry.entry_id), private=True, atomic_writes=True)

    async def async_load(self) -> None:
        """The state kept across restarts. A file that can't be read starts over (the checks judge again)."""
        await _async_wait_saves(self.hass, self.entry.entry_id)  # a save of the entry before a reload must be read back
        try:
            stored = await self._store.async_load()
        except Exception:  # noqa: BLE001 - a broken file must not stop the entry
            _LOGGER.exception("Could not read the health state of %s", self.label)
            return
        if not isinstance(stored, dict):
            return
        status = stored.get("status_unknown")
        events = stored.get("charger_events")
        self._data = {
            "blueprint_seen": stored.get("blueprint_seen") is True,
            "status_unknown": {
                str(value): moment for value, moment in status.items() if _parse(moment) is not None
            } if isinstance(status, dict) else {},
            "charger_events": [event for event in events if _stored_event(event)] if isinstance(events, list) else [],
            "scale_flagged": self._stored_scale_flagged(stored.get("scale_flagged")),
            "consumption_frozen": _stored_consumption(stored.get("consumption_frozen")),
        }
        # the flagged sensors stay flagged until they are judged fine, also after a restart (an unclear one keeps its repair)
        for issue in (ISSUE_POWER_UNIT, ISSUE_BATTERY_FRACTION):
            for entity_id, highest in self._data["scale_flagged"].get(issue, {}).items():
                self._scale_verdicts[entity_id] = True
                self._scale_max[entity_id] = highest

    def _stored_scale_flagged(self, stored: Any) -> dict[str, dict[str, float]]:
        """The flagged sensors of the two value repairs with their highest value. Only sensors that are still configured for the
        repair are kept (a removed one is no longer flagged); a repair whose stored list is readable stays known, even when none
        is left. A configured sensor whose highest value can't be read makes the list of its repair unknown: that repair is left
        out (not restored as known and empty) and stays as it is until the sensors are judged."""
        result: dict[str, dict[str, float]] = {}
        if not isinstance(stored, dict):
            return result
        for issue, entity_ids in ((ISSUE_POWER_UNIT, self._power_ids), (ISSUE_BATTERY_FRACTION, self._battery_ids)):
            entries = stored.get(issue)
            if not isinstance(entries, dict):
                continue
            kept: dict[str, float] = {}
            readable = True
            for entity_id, value in entries.items():
                if entity_id not in entity_ids:
                    continue
                highest = _finite_number(value)
                if highest is None:
                    readable = False
                    break
                kept[str(entity_id)] = highest
            if readable:
                result[issue] = kept
        return result

    @callback
    def start(self) -> None:
        if (unsub := async_at_started(self.hass, self._on_started)) is not None:
            self._unsubs.append(unsub)
        self._unsubs.append(
            self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._registry_changed, event_filter=self._registry_filter)
        )
        self._unsubs.append(self.hass.bus.async_listen(EVENT_AUTOMATION_RELOADED, self._automation_reloaded))
        self._unsubs.append(async_track_time_interval(self.hass, self._on_interval, RECHECK_INTERVAL))
        if self._status_entity:
            self._unsubs.append(async_track_state_change_event(self.hass, [self._status_entity], self._status_changed))
        if self._consumption_entity:
            self._unsubs.append(async_track_state_change_event(self.hass, [self._consumption_entity], self._consumption_changed))

    @callback
    def stop(self) -> None:
        """Stop watching. The repairs stay: an unload also happens for every reload, and a repair the user ignored must not
        come back. The first check after the next start decides about them. Removing the entry deletes them."""
        self._stopped = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._drop_automation_listener()
        if self._later is not None:
            self._later()
            self._later = None
        if self._status_later is not None:
            self._status_later()
            self._status_later = None

    @callback
    def record_charger_problem(self, kind: str) -> None:
        """A charger event the engine noted (JobResult.charger_problems). Kept for NOT_FOLLOWING_DAYS; the repair follows it."""
        if self._stopped or kind not in CHARGER_EVENTS:
            return
        self._data["charger_events"].append({"at": dt_util.utcnow().isoformat(), "kind": kind})
        self._save()
        if self._ha_started:
            self._check_not_following()

    @callback
    def record_charger_meter(self, verdict: dict[str, Any]) -> None:
        """The engine's verdict whether the house consumption includes the charger (JobResult.charger_meter)."""
        if self._stopped or not isinstance(verdict, dict):
            return
        self._meter_verdict = verdict
        if self._ha_started:
            self._check_charger_meter()

    async def async_check(self) -> None:
        """Judge every repair once. The checks are independent: one that fails leaves the others to run."""
        if self._stopped or not self._ha_started:
            return
        async with self._lock:
            for check in (
                self._check_missing,
                self._check_units,
                self._check_price_scale,
                self._check_not_following,
                self._check_status,
                self._check_consumption_frozen,
                self._check_charger_meter,
            ):
                try:
                    check()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Health check %s of %s failed", check.__name__, self.label)
            for scale_check in (self._async_check_power_scale, self._async_check_battery_scale):
                try:
                    await scale_check()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Health check %s of %s failed", scale_check.__name__, self.label)
            try:
                await self._async_check_blueprint()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Health check of the charger automation of %s failed", self.label)

    # ------------------------------------------------------------------ #
    # Triggers (callbacks only: they schedule the checks, they never block)
    # ------------------------------------------------------------------ #

    @callback
    def _schedule(self) -> None:
        if self._stopped or not self._ha_started:
            return
        self.entry.async_create_background_task(self.hass, self.async_check(), name=f"{DOMAIN}_{self.label}_health_checks")

    @callback
    def _on_started(self, _hass: HomeAssistant) -> None:
        self._ha_started = True
        self._started_at = _utcnow()
        self._schedule()

    @callback
    def _on_interval(self, _now: datetime.datetime) -> None:
        self._schedule()

    @callback
    def _registry_filter(self, data: Mapping[str, Any]) -> bool:
        relevant = self._relevant()
        return data.get("entity_id") in relevant or data.get("old_entity_id") in relevant

    @callback
    def _registry_changed(self, _event: Event) -> None:
        """Check a minute later: Home Assistant reloads the integration after a sensor was enabled or disabled."""
        if self._later is not None:
            self._later()
        self._later = async_call_later(self.hass, RECHECK_DELAY, self._check_later)

    @callback
    def _check_later(self, _now: datetime.datetime) -> None:
        self._later = None
        self._schedule()

    @callback
    def _automation_reloaded(self, _event: Event) -> None:
        self._schedule()

    @callback
    def _automation_changed(self, event: Event) -> None:
        """A listed automation switched on or off. An attribute alone (last_triggered, every run of the charger automation)
        is no reason for a check: that would read automations.yaml on every decision change."""
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if old is not None and new is not None and old.state == new.state:
            return
        self._schedule()

    @callback
    def _consumption_changed(self, event: Event) -> None:
        """The house sensor went from one number to another after the startup grace: the next check decides about it, so that a
        value that goes away and comes back between two checks is seen too. Unavailable or text on either side is no answer, so
        it is not counted. During the grace nothing is counted (see _consumption_grace_over)."""
        if not self._consumption_over:
            return
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if old is None or new is None:
            return
        before, after = _state_number(old.state), _state_number(new.state)
        if before is None or after is None or before == after:
            return
        self._consumption_moved = True

    @callback
    def _status_changed(self, _event: Event) -> None:
        """The status is judged again once it could have been held for STATUS_UNKNOWN_MINUTES."""
        if self._status_later is not None:
            self._status_later()
        self._status_later = async_call_later(self.hass, STATUS_RECHECK_DELAY, self._status_check_later)

    @callback
    def _status_check_later(self, _now: datetime.datetime) -> None:
        self._status_later = None
        if not self._stopped and self._ha_started:
            try:
                self._check_status()
            except Exception:  # noqa: BLE001 - like async_check: one failing check is logged, the watcher goes on
                _LOGGER.exception("Health check of the charger status of %s failed", self.label)

    def _relevant(self) -> set[str]:
        entities = {
            *self._entities,
            *(entity_id for entity_id, _ in self._unit_scope),
            self._status_entity,
            self._consumption_entity,
            *self._automation_entities,
        }
        return {entity_id for entity_id in entities if entity_id}

    # ------------------------------------------------------------------ #
    # Repairs
    # ------------------------------------------------------------------ #

    def _issue_raised(self, issue: str) -> bool:
        return ir.async_get(self.hass).async_get_issue(DOMAIN, f"{self.entry.entry_id}_{issue}") is not None

    def _raised_entity(self) -> str | None:
        """The house sensor that the open consumption repair names (its placeholder), or None when there is no repair or it names none."""
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, f"{self.entry.entry_id}_{ISSUE_CONSUMPTION_FROZEN}")
        entity = (issue.translation_placeholders or {}).get("entity") if issue is not None else None
        return entity if isinstance(entity, str) and entity else None

    def _set_issue(self, issue: str, found: bool, placeholders: dict[str, str]) -> None:
        issue_id = f"{self.entry.entry_id}_{issue}"
        if not found:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=issue,
            translation_placeholders={"prefix": self.label, **placeholders},
        )

    def _check_missing(self) -> None:
        """Configured entities that are disabled, or neither in the entity registry nor in the states. An entity that is only
        unavailable is not listed: the planner's notification covers that (Engine.is_entity_available), but only for an entity
        with an entity registry entry, and only once the planner reads it and finds no number for it in the last day."""
        registry = er.async_get(self.hass)
        disabled: list[str] = []
        missing: list[str] = []
        for entity_id in self._entities:
            entry = registry.async_get(entity_id)
            if entry is None:
                if self.hass.states.get(entity_id) is None:
                    missing.append(entity_id)
            elif entry.disabled_by:
                disabled.append(entity_id)
        self._set_issue(ISSUE_MISSING, bool(disabled or missing), {"disabled": _lines(disabled), "missing": _lines(missing)})

    def _charger_power_unit_ok(self, unit: str) -> bool:
        """The power units, but only W and kW while the update repair of the charger blueprint is open (also when it is
        ignored): the installed older blueprint reads any other unit as W."""
        if ir.async_get(self.hass).async_get_issue(DOMAIN, CHARGER_BLUEPRINT_UPDATE_ISSUE) is not None:
            return unit in OLD_BLUEPRINT_POWER_UNITS
        return _power_unit_ok(unit)

    def _check_units(self) -> None:
        """Sensors whose unit the code can't read. A sensor that is unavailable or unknown keeps its last judgement. The repair
        goes only when every sensor has been judged and all are fine, so a sensor unavailable since the start keeps it."""
        verdicts: dict[str, tuple[str, bool]] = {}
        for entity_id, accepted in self._unit_scope:
            state = self.hass.states.get(entity_id)
            if state is None or state.state in ENTITY_UNAVAILABLE_STATES:
                if entity_id in self._unit_verdicts:
                    verdicts[entity_id] = self._unit_verdicts[entity_id]
                continue
            unit = str(state.attributes.get("unit_of_measurement") or "")
            verdicts[entity_id] = (unit, not unit or accepted(unit))
        self._unit_verdicts = verdicts
        wrong = [f"{entity_id} ({unit})" for entity_id, (unit, ok) in verdicts.items() if not ok]
        if wrong:
            self._set_issue(
                ISSUE_UNITS,
                True,
                {
                    "entities": _lines(wrong),
                    "power_units": ", ".join(POWER_UNITS),
                    "energy_units": ENERGY_UNIT,
                },
            )
        elif all(entity_id in verdicts for entity_id, _ in self._unit_scope):
            self._set_issue(ISSUE_UNITS, False, {})

    def _check_status(self) -> None:
        """The charger status when it is a value CJP does not know and has been held for STATUS_UNKNOWN_MINUTES. The values seen
        stay listed for STATUS_UNKNOWN_DAYS after they were last seen, so a value that comes and goes doesn't open and close the
        repair; it goes when no such value is left. Without a status entity (none configured any more) nothing is held, so the
        values kept from before and their repair are dropped: that is a positive answer, not an unknown one."""
        if not self._status_entity:
            if self._data["status_unknown"]:
                self._data["status_unknown"] = {}
                self._save()
            self._set_issue(ISSUE_STATUS, False, {})
            return
        state = self.hass.states.get(self._status_entity)
        if state is None or state.state in ENTITY_UNAVAILABLE_STATES:
            return  # not judged now: the repair stays as it is
        now = dt_util.utcnow()
        before = dict(self._data["status_unknown"])
        if self._canonical_status(state.state) not in KNOWN_STATUS and now - state.last_changed >= datetime.timedelta(minutes=STATUS_UNKNOWN_MINUTES):
            self._data["status_unknown"][state.state] = now.isoformat()
        cutoff = now - datetime.timedelta(days=STATUS_UNKNOWN_DAYS)
        self._data["status_unknown"] = {
            value: moment
            for value, moment in self._data["status_unknown"].items()
            if (parsed := _parse(moment)) is not None and parsed >= cutoff
        }
        if self._data["status_unknown"] != before:
            self._save()
        self._set_issue(
            ISSUE_STATUS,
            bool(self._data["status_unknown"]),
            {
                "values": _lines([f"{value} ({_local(_parse(moment))})" for value, moment in self._data["status_unknown"].items()]),
                "minutes": str(STATUS_UNKNOWN_MINUTES),
                "days": str(STATUS_UNKNOWN_DAYS),
            },
        )

    def _check_not_following(self) -> None:
        """The charger did not follow the plan NOT_FOLLOWING_EVENTS times in NOT_FOLLOWING_DAYS days. Events that are older
        are dropped, so the repair goes by itself when they have aged out."""
        cutoff = dt_util.utcnow() - datetime.timedelta(days=NOT_FOLLOWING_DAYS)
        kept = [event for event in self._data["charger_events"] if _parse(event["at"]) >= cutoff]
        if len(kept) != len(self._data["charger_events"]):
            self._data["charger_events"] = kept
            self._save()
        gave_up = sum(event["kind"] == "gave_up" for event in kept)
        latest = sorted(kept, key=lambda event: _parse(event["at"]), reverse=True)[:NOT_FOLLOWING_LISTED]
        self._set_issue(
            ISSUE_NOT_FOLLOWING,
            len(kept) >= NOT_FOLLOWING_EVENTS,
            {
                "count": str(len(kept)),
                "limit": str(NOT_FOLLOWING_EVENTS),
                "days": str(NOT_FOLLOWING_DAYS),
                "gave_up": str(gave_up),
                "no_rule": str(len(kept) - gave_up),
                "latest": _lines([_local(_parse(event["at"])) for event in latest]),
            },
        )

    def _check_price_scale(self) -> None:
        """Price sensors in a subunit (øre, ct), per MWh or per Wh: no price is converted, so the prices are 100 or 1000 times too
        large, or 1000 times too small (per Wh). Judged by the price provider (no provider yet: not judged, the repair stays).
        Without a wrong one and with all judged it goes; a price entity that is not judged yet keeps it."""
        if self._price_problems is None:
            return
        judged = self._price_problems()
        if judged is None:
            return
        problems, complete = judged
        if problems:
            self._set_issue(ISSUE_PRICE_SCALE, True, {"entities": _lines([f"{entity_id} ({unit})" for entity_id, unit in problems])})
        elif complete:
            self._set_issue(ISSUE_PRICE_SCALE, False, {})

    def _consumption_grace_over(self) -> bool:
        """Whether the startup grace of the house sensor is over. It is over from the first check that is at or after CONSUMPTION_GRACE
        from the start (a check that reads a number, or that finds no sensor configured, or another one), and it stays over."""
        if not self._consumption_over and self._started_at is not None and _utcnow() - self._started_at >= CONSUMPTION_GRACE:
            self._consumption_over = True
        return self._consumption_over

    def _check_consumption_frozen(self) -> None:
        """The house consumption sensor that has shown the same value for FROZEN_AFTER or longer (its last_changed): the plan reads
        it for the house, so a stuck one makes the plan wrong. The frozen value and the time it started are kept in the store: a
        restart resets last_changed, so the repair stays until the value changes.

        During the startup grace (CONSUMPTION_GRACE from the start) nothing is deleted, the stored value and time are not replaced,
        no baseline is taken from a lost store, and the listener counts nothing. An open repair is shown again from its stored
        value (async_create_issue keeps its Ignore); a new repair is raised by the six hours only when no repair is open.

        After the grace a positive answer deletes the repair: a number other than the stored one, the sensor no longer configured,
        another sensor configured, a repair that names another sensor while the stored value is lost, or the listener saw the sensor
        go to another number since the last check. Unavailable, unknown or text keeps the repair as it is. A lost (or unreadable)
        stored value is taken from the first check after the grace that reads a number, when a repair names this sensor, or else
        once the value has been the same for FROZEN_AFTER."""
        entity = self._consumption_entity
        stored = self._data["consumption_frozen"]
        exists = self._issue_raised(ISSUE_CONSUMPTION_FROZEN)
        raised = self._raised_entity()
        if not entity or (stored is not None and stored["entity"] != entity) or (stored is None and raised not in (None, entity)):
            # a positive answer (no house sensor, another one, or a repair that names another one): only after the grace
            if not self._consumption_grace_over():
                return
            self._clear_consumption()
            if not entity:
                return
            stored, exists, raised = None, False, None
        state = self.hass.states.get(entity)
        if state is None or state.state in ENTITY_UNAVAILABLE_STATES:
            return  # not judged now: the repair stays as it is
        value = _state_number(state.state)
        if value is None:
            return  # a text state is no value to judge: the repair stays as it is
        grace = not self._consumption_grace_over()
        now = _utcnow()
        last_changed = state.last_changed
        moved, self._consumption_moved = self._consumption_moved, False
        if stored is not None:
            since = _frozen_since(stored, last_changed)
            differs = value != _state_number(stored["value"])
            if grace:
                # shown from the stored value and time, which are not replaced; a repair that is not open is raised by the six hours only
                if exists or (not differs and now - since >= FROZEN_AFTER):
                    self._set_issue(ISSUE_CONSUMPTION_FROZEN, True, self._consumption_placeholders(state, since, stored["value"]))
                return
            if moved or differs:
                # the value changed, also when it went away and came back between two checks: the frozen start is gone
                self._clear_consumption()
                return
            # the same value: still frozen. The stored start is kept, unless the sensor's last_changed is older (then that one)
            if stored["since"] != since.isoformat():
                self._store_consumption(entity, state, since)
            self._set_issue(ISSUE_CONSUMPTION_FROZEN, True, self._consumption_placeholders(state, since))
            return
        # no stored value: never stored, or lost or unreadable
        if grace:
            # no baseline during the grace: an open repair is kept as it is, and a new one is raised only by the six hours
            if not exists and now - last_changed >= FROZEN_AFTER:
                self._store_and_raise(entity, state, last_changed)
            return
        if raised != entity and now - last_changed < FROZEN_AFTER:
            # not named after this sensor, or without placeholders (a restart loads a repair without them): not before the six hours
            return
        # a repair that names this sensor while its stored value is lost: the value now is the baseline. A value that has been the
        # same for FROZEN_AFTER is raised and kept the same way
        self._store_and_raise(entity, state, last_changed)

    def _check_charger_meter(self) -> None:
        """The house consumption that looks not to include this car's charger (engine/energy/meter_check.py). The option set to
        "on its own meter" is a positive answer and removes the repair; so do no house sensor or no charger power sensor (nothing
        is subtracted then) and the engine's verdict that the house includes it. No verdict yet, or one that can't tell, leaves
        the repair as it is. Another house sensor does not remove it: the stored hours are not reset, so the next verdict would
        raise it again at once (and reset an Ignore); a True updates the sensor it names instead."""
        if not self._charger_in_house_meter or not self._consumption_entity or not self._own_charger_power:
            self._set_issue(ISSUE_CHARGER_OUTSIDE_METER, False, {})
            return
        verdict = self._meter_verdict
        if verdict is None or verdict.get("verdict") is None:
            return
        if verdict["verdict"] is True:
            self._set_issue(
                ISSUE_CHARGER_OUTSIDE_METER,
                True,
                {
                    "hours": str(verdict.get("hours", "")),
                    "days": str(verdict.get("days", "")),
                    "share": str(verdict.get("share", "")),
                    "entity": self._consumption_entity or NOTHING,
                },
            )
        else:
            self._set_issue(ISSUE_CHARGER_OUTSIDE_METER, False, {})

    def _store_and_raise(self, entity: str, state: Any, since: datetime.datetime) -> None:
        self._store_consumption(entity, state, since)
        self._set_issue(ISSUE_CONSUMPTION_FROZEN, True, self._consumption_placeholders(state, since))

    def _clear_consumption(self) -> None:
        """A positive answer: the frozen value is no longer kept, and its repair goes."""
        if self._data["consumption_frozen"] is not None:
            self._data["consumption_frozen"] = None
            self._save()
        self._set_issue(ISSUE_CONSUMPTION_FROZEN, False, {})

    def _store_consumption(self, entity: str, state: Any, since: datetime.datetime) -> None:
        self._data["consumption_frozen"] = {"entity": entity, "value": state.state, "since": since.isoformat()}
        self._save()

    def _consumption_placeholders(self, state: Any, since: datetime.datetime, value: str | None = None) -> dict[str, str]:
        """The repair's texts. value: the stored value, shown instead of the sensor's current state (the repair of a stored value)."""
        unit = str(state.attributes.get("unit_of_measurement") or "")
        shown = number_text(float(state.state if value is None else value), self._language())
        return {"entity": str(state.entity_id), "value": f"{shown} {unit}".strip(), "since": _local(since)}

    async def _async_check_power_scale(self) -> None:
        """Power sensors without a unit whose values look like kW (read as W they are 1000 times too small)."""
        judged = await self._async_judge_scale(self._power_ids, _has_unit, could_be_kw, power_scale_verdict)
        if not self._stopped:
            self._set_scale_issue(
                ISSUE_POWER_UNIT,
                self._power_ids,
                {"max": number_text(KW_LOOKING_MAX, self._language()), "days": str(POWER_SCALE_DAYS)},
                judged,
            )

    async def _async_check_battery_scale(self) -> None:
        """Battery levels in % whose values all lie in 0..1: a share, read as a percentage it is 100 times too small."""
        judged = await self._async_judge_scale(self._battery_ids, _has_other_battery_unit, could_be_fraction, battery_fraction_verdict)
        if not self._stopped:
            self._set_scale_issue(ISSUE_BATTERY_FRACTION, self._battery_ids, {"days": str(POWER_SCALE_DAYS)}, judged)

    def _language(self) -> str | None:
        return self.hass.config.language

    async def _async_judge_scale(
        self,
        entity_ids: list[str],
        has_own_unit: Callable[[str], bool],
        could_be: Callable[[Any], bool | None],
        verdict_of: Callable[[list[float]], bool | None],
    ) -> set[str]:
        """Judges each sensor: by its unit (has_own_unit: False), by its state now (could_be: False), or by its history (True
        when it could be the wrong scale, then verdict_of decides). The history of all such sensors is read in one call, and a
        sensor's history at most once per VALUE_RECHECK (also when the read failed). A state that is unavailable or unknown, or
        a verdict that can't be told, keeps the sensor's previous verdict. Returns the sensors that got a verdict (True or
        False) in this check: the ones whose verdict is held from an earlier check, or was not read now, are not among them."""
        now = dt_util.utcnow()
        judged: set[str] = set()
        current: dict[str, float] = {}
        to_read: list[str] = []
        for entity_id in entity_ids:
            state = self.hass.states.get(entity_id)
            if state is None or state.state in ENTITY_UNAVAILABLE_STATES:
                continue
            if has_own_unit(str(state.attributes.get("unit_of_measurement") or "")):
                self._scale_verdicts[entity_id] = False
                judged.add(entity_id)
                continue
            fits = could_be(state.state)
            if fits is None:
                continue
            if not fits:
                self._scale_verdicts[entity_id] = False
                judged.add(entity_id)
                continue
            current[entity_id] = float(state.state)
            read = self._scale_read.get(entity_id)
            if read is None or now - read >= VALUE_RECHECK:
                to_read.append(entity_id)
        if not to_read:
            return judged
        try:
            numbers = await async_recorded_numbers(self.hass, to_read, POWER_SCALE_DAYS)
        except Exception as err:  # noqa: BLE001 - a failed read keeps the verdicts and is not retried every hour
            if self._recorder_failed:
                _LOGGER.debug("Reading the history for the value checks of %s failed again: %s", self.label, err)
            else:
                _LOGGER.warning("Reading the history for the value checks of %s failed (retried in %s): %s", self.label, VALUE_RECHECK, err)
            self._recorder_failed = True
            for entity_id in to_read:
                self._scale_read[entity_id] = now
            return judged
        if numbers is None or self._stopped:
            return judged  # no recorder: unclear, the verdicts stay
        self._recorder_failed = False
        for entity_id in to_read:
            values = [*numbers.get(entity_id, []), current[entity_id]]
            self._scale_read[entity_id] = now
            verdict = verdict_of(values)
            if verdict is not None:
                self._scale_verdicts[entity_id] = verdict
                self._scale_max[entity_id] = max(abs(value) for value in values)
                judged.add(entity_id)
        return judged

    def _set_scale_issue(self, issue: str, entity_ids: list[str], placeholders: dict[str, str], judged: set[str]) -> None:
        """The repair of a value check. The flagged sensors (True) are shown with their highest value; the list of them is
        known once it was read intact from the store, when this check gave every sensor of the repair a verdict (judged), or
        when there is no store and no repair yet (nothing could have been forgotten: the list starts empty). A known list is
        kept in the store and goes when nothing is flagged. An unknown list keeps its flags in memory only: the repair goes
        when every sensor is judged False, and stays otherwise. A removed sensor is not in entity_ids any more and is dropped
        without making the list unknown."""
        flagged = {entity_id: self._scale_max[entity_id] for entity_id in entity_ids if self._scale_verdicts.get(entity_id) is True}
        # no repair yet: nothing could have been forgotten, so the list starts empty (known)
        newly_known = issue not in self._data["scale_flagged"] and (set(entity_ids) <= judged or not self._issue_raised(issue))
        known = issue in self._data["scale_flagged"] or newly_known
        if flagged:
            found = [f"{entity_id} (≤ {number_text(_shown_highest(value), self._language())})" for entity_id, value in flagged.items()]
            self._set_issue(issue, True, {"entities": _lines(found), **placeholders})
        elif known or all(self._scale_verdicts.get(entity_id) is False for entity_id in entity_ids):
            self._set_issue(issue, False, {})
        else:
            return  # unknown list (store lost, or a sensor not judged in one check): the repair stays as it is
        if known and self._data["scale_flagged"].get(issue) != flagged:
            self._data["scale_flagged"][issue] = flagged
            self._save()

    async def _async_check_blueprint(self) -> None:
        """The automation made from the charger blueprint. Seen on once: then a disabled or deleted one is a repair.

        automations.yaml is read in the executor. An automation that can't be found in the entity registry or has no state yet
        is not judged, and neither is a file that can't be read: the repair stays as it is then."""
        ids = await self.hass.async_add_executor_job(blueprint_automation_ids, self._automations_path, self._action_sensor)
        if self._stopped or ids is None:
            return
        registry = er.async_get(self.hass)
        found: set[str] = set()
        active = False
        unclear = False
        disabled: list[str] = []
        for automation_id in ids:
            entity_id = registry.async_get_entity_id("automation", "automation", automation_id)
            if entity_id is None:
                unclear = True
                continue
            found.add(entity_id)
            entry = registry.async_get(entity_id)
            state = self.hass.states.get(entity_id)
            if entry is not None and entry.disabled_by:
                # disabled in the entity registry: it has no state at all, and it is off
                disabled.append(str(entry.name or entry.original_name or entity_id))
            elif state is None or state.state not in AUTOMATION_STATES:
                unclear = True
            elif state.state == "on":
                active = True
            else:
                disabled.append(str(state.attributes.get("friendly_name") or entity_id))
        self._watch_automations(found)
        if active:
            if not self._data["blueprint_seen"]:
                self._data["blueprint_seen"] = True
                self._save()
            self._set_issue(ISSUE_BLUEPRINT, False, {})
        elif not unclear and self._data["blueprint_seen"]:
            # every automation is off, or none is left in automations.yaml (then NOTHING is shown)
            self._set_issue(ISSUE_BLUEPRINT, True, {"automations": _lines(disabled)})

    # ------------------------------------------------------------------ #
    # Listeners and storage
    # ------------------------------------------------------------------ #

    @callback
    def _watch_automations(self, entity_ids: set[str]) -> None:
        """Listen to the automation entities of the file: a change of one of them checks again. Resubscribed when the set changes."""
        found = frozenset(entity_ids)
        if self._stopped or found == self._automation_entities:
            return
        self._drop_automation_listener()
        self._automation_entities = found
        if found:
            self._automation_unsub = async_track_state_change_event(self.hass, sorted(found), self._automation_changed)

    @callback
    def _drop_automation_listener(self) -> None:
        if self._automation_unsub is not None:
            self._automation_unsub()
            self._automation_unsub = None
        self._automation_entities = frozenset()

    @callback
    def _save(self) -> None:
        """A Home Assistant background task, not one of the entry: an unload cancels the entry's tasks, and a change made just
        before an unload must still be written. No save starts after stop(); a removal waits for the saves in progress."""
        if self._stopped:
            return
        task = self.hass.async_create_background_task(self._async_save(), name=f"{DOMAIN}_{self.label}_health_save")
        pending = _pending_saves(self.hass, self.entry.entry_id)
        pending.add(task)
        task.add_done_callback(pending.discard)

    async def _async_save(self) -> None:
        """Written at once, not delayed: a delayed write could come after the entry was removed and its store deleted."""
        async with self._save_lock:
            try:
                await self._store.async_save(copy.deepcopy(self._data))
            except Exception:  # noqa: BLE001 - a failed write is logged; the next change saves the state again
                _LOGGER.exception("Could not save the health state of %s", self.label)
