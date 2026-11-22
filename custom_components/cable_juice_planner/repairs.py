"""Repair flows: replace an installed blueprint with the version bundled with Cable Juice Planner, and set the grid voltage
that is still the old default to the new one."""
from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant

from .management.blueprint_install import async_update_blueprint
from .management.voltage_issue import VOLTAGE_ISSUE, async_fix_voltage


class BlueprintUpdateFlow(ConfirmRepairFlow):
    def __init__(self, name: str) -> None:
        self._name = name

    async def async_step_confirm(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        if user_input is not None:
            await async_update_blueprint(self.hass, self._name)
        return await super().async_step_confirm(user_input)


class VoltageFixFlow(ConfirmRepairFlow):
    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_confirm(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        if user_input is not None:
            async_fix_voltage(self.hass, self._entry_id)
        return await super().async_step_confirm(user_input)


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None) -> RepairsFlow:
    if data and data.get("kind") == VOLTAGE_ISSUE:
        return VoltageFixFlow(data["entry_id"])
    return BlueprintUpdateFlow(data["file"])
