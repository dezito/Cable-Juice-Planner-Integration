"""The chargers of the cars behind this one in priority, whose consumption is left out of the house consumption."""
from __future__ import annotations

from homeassistant.core import HomeAssistant

from ..const import CONF_SITE, DEFAULT_CONFIG, DOMAIN

# The option with the power sensor of a car's charger. The old name (misspelled) is still read for entries that have
# not been migrated yet. It must never be power_consumption_entity_id: that option is the HOUSE consumption sensor.
CHARGER_POWER_KEYS = ("charger_power_entity_id", "power_consumtion_entity_id")
PRIORITY_KEY = "site_priority"


def charger_power_entity(options: dict) -> str | None:
    """The charger power sensor in the options of a car entry, None when it has none."""
    for key in CHARGER_POWER_KEYS:
        value = options.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def site_priority(options: dict) -> float:
    """The load balancing priority of a car (1 = first), the default when none is stored."""
    try:
        return float(options.get(PRIORITY_KEY))
    except (TypeError, ValueError):
        return float(DEFAULT_CONFIG["site"]["priority"])


def lower_priority_charger_power_entities(hass: HomeAssistant, own_entry_id: str | None, own_charger: str | None, own_priority: float) -> list[str]:
    """The power sensors of the chargers of the cars with a LOWER priority (a higher number) than this car, sorted.

    The car with priority 1 has the first right to the solar surplus, then 2, and so on: a car counts the charging
    of the cars ahead of it as consumption of the house (they take their share first), but not the charging of the cars
    behind it, which only get what is left. Without this the cars would each see the other's charging as free solar.
    Cars with the same priority don't leave each other out, so the default (everyone 1) changes nothing.

    This car's own charger is subtracted on its own (ev_used_consumption), so it is never in the list, not even
    when another car uses the same charger. Read from the options of the config entries, not from the running cars,
    so the answer does not depend on the order they start in. The site entry (no charger) and disabled cars are left out.
    """
    found: set[str] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == own_entry_id or entry.data.get(CONF_SITE) or entry.disabled_by:
            continue
        if site_priority(entry.options) <= own_priority:
            continue
        if (entity_id := charger_power_entity(entry.options)) and entity_id != own_charger:
            found.add(entity_id)
    return sorted(found)
