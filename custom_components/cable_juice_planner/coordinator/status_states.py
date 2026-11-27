"""The charger's status as CJP reads it: the states the user gave the charger blueprint count as charging or done too.

Every status value that enters CJP (the snapshot, the history buffers, the state change of the status entity, the recorder's
history, the repairs and the load balancing) goes through canonical_status(), so the engine and the rest only know the built in
states (const/base.py). The built in states always apply; the blueprint can only add states (management/automation_inputs.py
status_aliases). The file with the automations is only read, in the executor.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.automation import EVENT_AUTOMATION_RELOADED
from homeassistant.config import AUTOMATION_CONFIG_PATH
from homeassistant.core import Event, callback

from ..const import normalize_state
from ..management.automation_inputs import LIMIT_INPUTS, automations_using_blueprint, has_steps, status_aliases

_LOGGER = logging.getLogger(__name__)


class CoordinatorStatusStates:
    """The extra charging and done states of the charger blueprint for the status entity."""

    def _init_status_states(self) -> None:
        self.status_aliases: dict[str, str] = {}
        # whether the charger automation has steps that set the car's charge limit (None: no automation found, not known);
        # read with the states, passed to the engine in the snapshot (the departure notice at plug-in)
        self.car_limit_steps: bool | None = None

    def canonical_status(self, value: Any) -> Any:
        """The status as CJP reads it: a state the blueprint gave as charging or done is the built in one, anything else is
        returned as it is (also a value that is no text)."""
        if not self.status_aliases or not isinstance(value, str):
            return value
        return self.status_aliases.get(normalize_state(value), value)

    def _canonical_state(self, entity_id: str, value: Any) -> Any:
        """The state of an entity as CJP reads it: only the charger's status entity is changed (canonical_status)."""
        if entity_id and entity_id == self.config["charger"]["entity_ids"]["status_entity_id"]:
            return self.canonical_status(value)
        return value

    async def _async_load_status_aliases(self) -> bool:
        """Read the automations and keep the states of the blueprint. True when they changed."""
        status_entity = self.config["charger"]["entity_ids"]["status_entity_id"]
        found = await self.hass.async_add_executor_job(
            automations_using_blueprint, self.hass.config.path(AUTOMATION_CONFIG_PATH), f"sensor.{self.object_prefix}_charger_action"
        )
        # before the comparison below: a reload that only changes these steps is kept too
        self.car_limit_steps = has_steps(found, *LIMIT_INPUTS)
        aliases: dict[str, str] = status_aliases(found, status_entity) if status_entity else {}
        if aliases == self.status_aliases:
            return False
        self.status_aliases = aliases
        # the rows already in the history buffer take the added states too (a state taken away is read as before until the
        # rows age out: the original value is not kept)
        rows = self._buffers.get(status_entity)
        if rows:
            self._buffers[status_entity] = [(moment, self.canonical_status(value)) for moment, value in rows]
        if aliases:
            _LOGGER.info("The charger blueprint adds states for %s: %s", status_entity, aliases)
        return True

    def _start_status_aliases(self) -> None:
        """Read them again each time the automations are reloaded (the editor saves them); the first read is made in async_setup."""

        async def _reload() -> None:
            try:
                if await self._async_load_status_aliases() and self.started:
                    await self.async_run_job("recalculate", include_forecast=True)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Could not read the states of the charger blueprint", exc_info=True)

        @callback
        def _on_reloaded(_event: Event) -> None:
            self._background(_reload(), "status_aliases")

        self._unsubs.append(self.hass.bus.async_listen(EVENT_AUTOMATION_RELOADED, _on_reloaded))
