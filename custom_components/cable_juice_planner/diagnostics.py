"""Diagnostics (Settings → Devices & services → Cable Juice Planner → ⋮ → Download diagnostics).

For a car: the same anonymised data as the debug export (management/debug_export.py),
without the databases: they can be large, and the debug export has them. For the load
balancing site: its settings and the cars in priority order, anonymised the same way.
"""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .coordinator import CJPCoordinator
from .management.debug_export import Redactor, async_debug_data, jsonable


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    runtime = entry.runtime_data
    if isinstance(runtime, CJPCoordinator):
        data, redactor = await async_debug_data(hass, runtime, dt_util.now(), databases=False)
    else:
        options = {**entry.data, **entry.options}
        cars = [
            other.runtime_data.object_prefix
            for other in hass.config_entries.async_loaded_entries(entry.domain)
            if isinstance(other.runtime_data, CJPCoordinator)
        ]
        data = {"site": True, "options": options, "cars": cars}
        redactor = Redactor("site", options, [])
    return await hass.async_add_executor_job(jsonable, data, redactor)
