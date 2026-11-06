"""Cable Juice Planner binary sensor: a public charging session waits for its cost."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import CJPCoordinator
from .entity import CJPUpdatingEntity, requirement_met

# the entities read the coordinator's data and change only the integration's own settings
PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: CJPCoordinator = entry.runtime_data
    key = "public_charging_session_done"
    if requirement_met(coordinator, "public_charging"):
        async_add_entities([CJPPublicChargingSessionDone(coordinator, "binary_sensor", key)])
        return
    # no car location: no public charging, no unavailable entity left behind
    ent_reg = er.async_get(hass)
    if entity_id := ent_reg.async_get_entity_id("binary_sensor", DOMAIN, f"{entry.entry_id}_{key}"):
        ent_reg.async_remove(entity_id)


class CJPPublicChargingSessionDone(CJPUpdatingEntity, BinarySensorEntity):
    """On when a public charging session ended and its cost must be entered."""

    _unrecorded_attributes = frozenset({MATCH_ALL})

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.data.get(self._key)
        return None if value is None else value == "on"
