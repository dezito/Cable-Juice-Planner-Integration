"""The repair for a grid voltage that is still the default of earlier versions, 240 V (the default is 230 V now).

A voltage of 240 V can't be told from one the user chose (a grid that really has it, a value imported from the PyScript
version), so it is never changed by itself: the repair asks, its fix sets the new default, and an Ignore keeps 240 V.
The voltage counts the charging power of the plan and the solar steps, and turns the charger's measured power into amps for
the load balancing (a voltage set too high counts a car's draw too low).
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from ..const import DEFAULT_CONFIG, DOMAIN

OLD_DEFAULT_VOLTAGE = 240.0
VOLTAGE_ISSUE = "voltage_old_default"
VOLTAGE_OPTION = "power_voltage"


def voltage_issue_id(entry_id: str) -> str:
    return f"{entry_id}_{VOLTAGE_ISSUE}"


def _is_old_default(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and abs(float(value) - OLD_DEFAULT_VOLTAGE) < 0.05


@callback
def async_check_voltage(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Raise the repair while the car entry's voltage option is 240 V, delete it once it is another number.

    An option the entry does not have is the default (230 V): no repair. Re-creating an open issue keeps the user's Ignore."""
    value = entry.options.get(VOLTAGE_OPTION)
    issue_id = voltage_issue_id(entry.entry_id)
    if not _is_old_default(value):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=VOLTAGE_ISSUE,
        translation_placeholders={
            "name": entry.title, "voltage": f"{OLD_DEFAULT_VOLTAGE:g}", "default": f"{DEFAULT_CONFIG['charger']['power_voltage']:g}",
        },
        data={"kind": VOLTAGE_ISSUE, "entry_id": entry.entry_id},
    )


@callback
def async_remove_voltage_issue(hass: HomeAssistant, entry_id: str) -> None:
    ir.async_delete_issue(hass, DOMAIN, voltage_issue_id(entry_id))


@callback
def async_fix_voltage(hass: HomeAssistant, entry_id: str) -> None:
    """The repair's fix: the new default voltage, only while the option still is 240 V (the user may have set another
    voltage meanwhile). The options update reloads the entry, which plans with it."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is not None and _is_old_default(entry.options.get(VOLTAGE_OPTION)):
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, VOLTAGE_OPTION: float(DEFAULT_CONFIG["charger"]["power_voltage"])}
        )
    async_remove_voltage_issue(hass, entry_id)
