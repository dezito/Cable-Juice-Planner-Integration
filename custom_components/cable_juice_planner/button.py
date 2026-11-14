"""Cable Juice Planner buttons (replace the input_button helpers)."""
from __future__ import annotations

from homeassistant.components import persistent_notification
from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .management.backup import BACKUP_DIR, async_export
from .const import BUTTON_KEYS, DOMAIN, EV_BUTTON_KEYS, is_ev_configured
from .coordinator import CJPCoordinator
from .management.dashboard_build import async_reset_dashboard
from .management.debug_export import async_export_debug, async_notify_debug
from .entity import CJPEntity

# the entities read the coordinator's data and change only the integration's own settings
PARALLEL_UPDATES = 0

# buttons that no longer exist: removed from the registry at start so no unavailable entity is left behind
RETIRED_BUTTONS = ("stop_what_if",)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: CJPCoordinator = entry.runtime_data
    ent_reg = er.async_get(hass)
    for key in RETIRED_BUTTONS:
        if entity_id := ent_reg.async_get_entity_id("button", DOMAIN, f"{entry.entry_id}_{key}"):
            ent_reg.async_remove(entity_id)
    keys = [*BUTTON_KEYS, *(EV_BUTTON_KEYS if is_ev_configured(coordinator.config) else ())]
    entities = [*(CJPButton(coordinator, "button", key) for key in keys), CJPExportButton(coordinator, "button", "export_data")]
    entities.append(CJPExportDebugButton(coordinator, "button", "export_debug"))
    if coordinator.config.get("dashboard"):
        entities.append(CJPResetDashboardButton(coordinator, "button", "reset_dashboard"))
    async_add_entities(entities)


class CJPButton(CJPEntity, ButtonEntity):
    """enforce_planning (plan now, ignoring the unchanged-input shortcut), trip_reset and
    recalculate_charging_loss (forget the measurements and read the history again)."""

    async def async_press(self) -> None:
        self.coordinator.async_press(self._key)


class CJPExportButton(CJPEntity, ButtonEntity):
    """Write a backup of the databases and settings to cable_juice_planner_backups."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_press(self) -> None:
        name = await async_export(self.hass, self.coordinator.entry)
        persistent_notification.async_create(
            self.hass,
            self.coordinator.t("ui.backup.exported", file=f"{BACKUP_DIR}/{name}"),
            title=f"📟{self.coordinator.title}",
            notification_id=f"{self.coordinator.object_prefix}_backup",
        )


class CJPExportDebugButton(CJPEntity, ButtonEntity):
    """Write the anonymised debug file for a GitHub issue to cable_juice_planner_debug."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_press(self) -> None:
        name = await async_export_debug(self.hass, self.coordinator)
        await async_notify_debug(self.hass, self.coordinator, name)


class CJPResetDashboardButton(CJPEntity, ButtonEntity):
    """Throw away the edits made to the sidebar dashboard in the UI."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_press(self) -> None:
        await async_reset_dashboard(self.hass, self.coordinator.prefix)
