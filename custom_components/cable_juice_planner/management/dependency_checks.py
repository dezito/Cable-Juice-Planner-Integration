"""Repairs for the Strømligning and Solcast sensors Cable Juice Planner uses but Home Assistant has disabled or the integration doesn't give.

Both integrations switch some of their sensors off by default (all Strømligning *_ex_vat sensors, the Solcast
forecast sensors of day 3 and on), and a disabled sensor has no state, so Cable Juice Planner simply doesn't see it: a missing sell
tariff counts as 0 (a too high sell price) and a missing Solcast day is planned from the weather and the stored solar
history instead. The repair names the sensors and goes away by itself when they are fixed. Nothing is changed in the
other integrations.
"""
from __future__ import annotations

import datetime
import logging
from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.start import async_at_started

from ..const import DOMAIN
from ..prices import stromligning_problems
from ..utils.solcast import SOLCAST_PLATFORM, SOLCAST_UNIQUE_IDS, strip_forecast_suffix
from .health_checks import HEALTH_ISSUES

_LOGGER = logging.getLogger(__name__)

ISSUE_STROMLIGNING = "stromligning_entities"
ISSUE_SOLCAST = "solcast_entities"
# Home Assistant reloads an integration about 30 seconds after one of its entities was enabled or disabled
RECHECK_DELAY = 60
RECHECK_INTERVAL = datetime.timedelta(hours=1)
NOTHING = "–"
# the list in a flow page: the first sensors, then how many more there are
FLOW_LIST_LIMIT = 12
def solcast_problems(hass: HomeAssistant, forecast_entity: str, endings: list[str]) -> dict[str, list[str]] | None:
    """Which of the Solcast forecast sensors a plan needs are disabled, named differently or without hourly detail.

    Found by their unique id, so a renamed sensor is still found: Cable Juice Planner reads <base>_today, <base>_tomorrow and
    <base>_day_N of the selected sensor, so a sensor under another name is as good as missing. None when the
    forecast sensor isn't a Solcast one.
    """
    registry = er.async_get(hass)
    selected = registry.async_get(forecast_entity) if forecast_entity else None
    if selected is not None:
        if selected.platform != SOLCAST_PLATFORM:
            return None
    elif not forecast_entity or not forecast_entity.endswith(tuple(f"_{ending}" for ending in SOLCAST_UNIQUE_IDS)):
        return None
    elif registry.async_get_entity_id("sensor", SOLCAST_PLATFORM, SOLCAST_UNIQUE_IDS["today"]) is None:
        return None
    # (a selected sensor that is no longer in the registry, e.g. renamed, is judged by the Solcast sensors that are)
    base = strip_forecast_suffix(forecast_entity)
    disabled: list[str] = []
    renamed: list[str] = []
    no_detail: list[str] = []
    for ending in endings:
        expected = f"{base}_{ending}"
        entity_id = registry.async_get_entity_id("sensor", SOLCAST_PLATFORM, SOLCAST_UNIQUE_IDS[ending])
        entry = registry.async_get(entity_id) if entity_id else None
        if entry is None:
            renamed.append(expected)
        elif entry.disabled_by:
            disabled.append(entity_id)
        elif entity_id != expected:
            renamed.append(f"{entity_id} → {expected}")
        elif (state := hass.states.get(entity_id)) is not None and state.state != "unavailable" and "detailedHourly" not in state.attributes:
            no_detail.append(entity_id)
    return {"disabled": disabled, "renamed": renamed, "no_detail": no_detail}


def disabled_sensors(hass: HomeAssistant, platform: str, unique_id_prefix: str | tuple[str, ...] = "") -> str:
    """The disabled sensors of an integration as text for a config flow page (the entity picker can't offer them).

    unique_id_prefix keeps the list to the sensors that can be used (Strømligning has about 20 per entry); its unique
    ids start with the key of the sensor.
    """
    registry = er.async_get(hass)
    found = sorted(
        entry.entity_id
        for entry in registry.entities.values()
        if entry.platform == platform and entry.disabled_by and entry.unique_id.startswith(unique_id_prefix)
    )
    if not found:
        return NOTHING
    shown = ", ".join(found[:FLOW_LIST_LIMIT])
    return f"{shown} … (+{len(found) - FLOW_LIST_LIMIT})" if len(found) > FLOW_LIST_LIMIT else shown


def _lines(entity_ids: list[str]) -> str:
    return "\n".join(f"- {entity_id}" for entity_id in entity_ids) or NOTHING


@callback
def remove_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Delete all repairs of a config entry (it was removed): these two and the ones of the health checks."""
    for issue in (ISSUE_STROMLIGNING, ISSUE_SOLCAST, *HEALTH_ISSUES):
        ir.async_delete_issue(hass, DOMAIN, f"{entry_id}_{issue}")


@callback
def _remove_dependency_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Only the two repairs of this watcher: a reload of an entry with nothing to watch keeps the health checks' repairs."""
    for issue in (ISSUE_STROMLIGNING, ISSUE_SOLCAST):
        ir.async_delete_issue(hass, DOMAIN, f"{entry_id}_{issue}")


class DependencyWatcher:
    """Keeps the two repairs of one config entry up to date: at start, every hour and after a watched sensor changed."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        label: str,
        stromligning_entities: list[str],
        forecast_entity: str,
        solcast_endings: list[str],
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.label = label
        self.stromligning_entities = list(stromligning_entities)
        self.forecast_entity = forecast_entity
        self.solcast_endings = list(solcast_endings)
        self._unsubs: list[CALLBACK_TYPE] = []
        self._later: CALLBACK_TYPE | None = None
        base = strip_forecast_suffix(forecast_entity) if forecast_entity else ""
        self._watched = {*self.stromligning_entities, *(f"{base}_{ending}" for ending in self.solcast_endings if base)}

    def _issue_id(self, issue: str) -> str:
        return f"{self.entry.entry_id}_{issue}"

    @property
    def watched_entities(self) -> frozenset[str]:
        """The sensors this watcher judges: the health checks leave them out (they are repaired here)."""
        return frozenset(self._watched)

    @callback
    def start(self) -> None:
        if not self.stromligning_entities and not self.forecast_entity:
            _remove_dependency_issues(self.hass, self.entry.entry_id)  # nothing to watch any more: no repair about it either
            return
        if (unsub := async_at_started(self.hass, self._started)) is not None:
            self._unsubs.append(unsub)
        self._unsubs.append(self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._registry_changed, event_filter=self._registry_filter))
        self._unsubs.append(async_track_time_interval(self.hass, self._interval, RECHECK_INTERVAL))

    @callback
    def stop(self) -> None:
        """Stop watching. The repairs stay (see remove_issues): an unload also happens for every reload, and a repair
        the user chose to ignore must not come back each time. The first check after the start decides about them."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._later is not None:
            self._later()
            self._later = None

    @callback
    def _registry_filter(self, data: Mapping[str, Any]) -> bool:
        return data.get("entity_id") in self._watched or data.get("old_entity_id") in self._watched or self._is_solcast_entity(data.get("entity_id"))

    @callback
    def _is_solcast_entity(self, entity_id: str | None) -> bool:
        """A forecast sensor of Solcast, whatever its name (it may have just been renamed or enabled)."""
        if not entity_id or not self.forecast_entity:
            return False
        entry = er.async_get(self.hass).async_get(entity_id)
        return entry is not None and entry.platform == SOLCAST_PLATFORM and entry.unique_id in SOLCAST_UNIQUE_IDS.values()

    @callback
    def _started(self, _hass: HomeAssistant) -> None:
        self.check()

    @callback
    def _interval(self, _now: datetime.datetime) -> None:
        self.check()

    @callback
    def _registry_changed(self, _event: Event) -> None:
        """Check a minute later: Home Assistant reloads the integration after a sensor was enabled or disabled."""
        if self._later is not None:
            self._later()
        self._later = async_call_later(self.hass, RECHECK_DELAY, self._check_later)

    @callback
    def _check_later(self, _now: datetime.datetime) -> None:
        self._later = None
        self.check()

    @callback
    def check(self) -> None:
        self._check_stromligning()
        self._check_solcast()

    def _set_issue(self, issue: str, problems: dict[str, list[str]], placeholders: Callable[[dict[str, list[str]]], dict[str, str]]) -> None:
        issue_id = self._issue_id(issue)
        if not any(problems.values()):
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=issue,
            translation_placeholders={"prefix": self.label, **placeholders(problems)},
        )

    def _check_stromligning(self) -> None:
        if not self.stromligning_entities:
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(ISSUE_STROMLIGNING))
            return
        problems = stromligning_problems(self.hass, self.stromligning_entities)
        self._set_issue(
            ISSUE_STROMLIGNING, problems, lambda found: {"disabled": _lines(found["disabled"]), "not_provided": _lines(found["not_provided"])}
        )

    def _check_solcast(self) -> None:
        problems = solcast_problems(self.hass, self.forecast_entity, self.solcast_endings)
        if problems is None:
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(ISSUE_SOLCAST))
            return
        self._set_issue(
            ISSUE_SOLCAST,
            problems,
            lambda found: {"disabled": _lines(found["disabled"]), "renamed": _lines(found["renamed"]), "no_detail": _lines(found["no_detail"])},
        )
