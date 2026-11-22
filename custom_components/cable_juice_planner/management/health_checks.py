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

The state that must survive a restart is in a store of its own (not in the backups). Removing the entry deletes the repairs
(management/dependency_checks.py remove_issues) and the store (async_remove_health_store).
"""
from __future__ import annotations

import asyncio
import copy
import datetime
import logging
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
)
from ..utils.power import POWER_UNITS, power_unit_known
from .automation_inputs import blueprint_automation_ids
from .blueprint_install import ISSUE_PREFIX as BLUEPRINT_UPDATE_PREFIX

_LOGGER = logging.getLogger(__name__)

ISSUE_MISSING = "entities_missing"
ISSUE_UNITS = "entity_units"
ISSUE_BLUEPRINT = "blueprint_automation_lost"
ISSUE_STATUS = "charger_status_unknown"
ISSUE_NOT_FOLLOWING = "charger_not_following"
HEALTH_ISSUES = (ISSUE_MISSING, ISSUE_UNITS, ISSUE_BLUEPRINT, ISSUE_STATUS, ISSUE_NOT_FOLLOWING)

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
KNOWN_STATUS = frozenset(
    (*CHARGER_READY_STATUS, *CHARGER_NOT_READY_STATUS, *CHARGER_COMPLETED_STATUS, *CHARGER_CHARGING_STATUS, "error", *ENTITY_UNAVAILABLE_STATES)
)
# the states of an automation that are judged: on (in use), off (disabled)
AUTOMATION_STATES = ("on", "off")


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}.health"


async def async_remove_health_store(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the store of an entry that was removed (async_remove_entry)."""
    await Store(hass, STORE_VERSION, store_key(entry_id), private=True, atomic_writes=True).async_remove()


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
    ) -> None:
        """entities: the configured entities (the ones DependencyWatcher judges, in watched, are left out here).
        charger_power/power/battery/energy_entities: the sensors whose unit is read, by kind; a sensor listed twice is judged by
        the first kind (the charger power's rule, which can be stricter). action_sensor: the charger's action sensor the
        blueprint automation uses. automations_path: the automations.yaml the editor writes (read only)."""
        self.hass = hass
        self.entry = entry
        self.label = label
        self._entities = sorted({entity_id for entity_id in entities if entity_id and entity_id not in watched})
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
        self._action_sensor = action_sensor
        self._automations_path = automations_path
        self._unsubs: list[CALLBACK_TYPE] = []
        self._later: CALLBACK_TYPE | None = None
        self._status_later: CALLBACK_TYPE | None = None
        self._automation_unsub: CALLBACK_TYPE | None = None
        self._automation_entities: frozenset[str] = frozenset()
        # entity -> (unit, ok) of the last judgement; a sensor that can't be judged now keeps its last one
        self._unit_verdicts: dict[str, tuple[str, bool]] = {}
        self._data: dict[str, Any] = {"blueprint_seen": False, "status_unknown": {}, "charger_events": []}
        self._ha_started = False
        self._stopped = False
        self._lock = asyncio.Lock()
        self._save_lock = asyncio.Lock()
        self._store = Store(hass, STORE_VERSION, store_key(entry.entry_id), private=True, atomic_writes=True)

    async def async_load(self) -> None:
        """The state kept across restarts. A file that can't be read starts over (the checks judge again)."""
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
        }

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

    async def async_check(self) -> None:
        """Judge every repair once. The checks are independent: one that fails leaves the others to run."""
        if self._stopped or not self._ha_started:
            return
        async with self._lock:
            for check in (self._check_missing, self._check_units, self._check_not_following, self._check_status):
                try:
                    check()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Health check %s of %s failed", check.__name__, self.label)
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
        entities = {*self._entities, *(entity_id for entity_id, _ in self._unit_scope), self._status_entity, *self._automation_entities}
        return {entity_id for entity_id in entities if entity_id}

    # ------------------------------------------------------------------ #
    # Repairs
    # ------------------------------------------------------------------ #

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
        unavailable is not listed: the persistent notification after 30 minutes covers it."""
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
        if state.state not in KNOWN_STATUS and now - state.last_changed >= datetime.timedelta(minutes=STATUS_UNKNOWN_MINUTES):
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
        if self._stopped:
            return
        self.entry.async_create_background_task(self.hass, self._async_save(), name=f"{DOMAIN}_{self.label}_health_save")

    async def _async_save(self) -> None:
        """Written at once, not delayed: a delayed write could come after the entry was removed and its store deleted."""
        async with self._save_lock:
            await self._store.async_save(copy.deepcopy(self._data))
