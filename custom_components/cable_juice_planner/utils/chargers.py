"""The chargers of the cars behind this one in priority, whose consumption is left out of the house consumption."""
from __future__ import annotations

from homeassistant.core import HomeAssistant

from ..const import CONF_SITE, DEFAULT_CONFIG, DOMAIN

# The option with the power sensor of a car's charger. The old name (misspelled) is still read for entries that have
# not been migrated yet. It must never be power_consumption_entity_id: that option is the HOUSE consumption sensor.
CHARGER_POWER_KEYS = ("charger_power_entity_id", "power_consumtion_entity_id")
PRIORITY_KEY = "site_priority"
# whether the house consumption sensor includes the car's charger (default True). BattMind reads the same option
CHARGER_IN_HOUSE_METER_KEY = "charger_in_house_meter"


def charger_in_house_meter(options: dict) -> bool:
    """Whether the house consumption sensor includes this car's charger: only an explicit False says it does not."""
    return options.get(CHARGER_IN_HOUSE_METER_KEY, True) is not False


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


def lower_priority_charger_power_entities(
    hass: HomeAssistant, own_entry_id: str | None, own_charger: str | None, own_priority: float, *, include_own_meter: bool = False
) -> list[str]:
    """The power sensors of the chargers of the cars with a LOWER priority (a higher number) than this car, sorted.

    The car with priority 1 has the first right to the solar surplus, then 2, and so on: a car counts the charging
    of the cars ahead of it as consumption of the house (they take their share first), but not the charging of the cars
    behind it, which only get what is left. Without this the cars would each see the other's charging as free solar.
    Cars with the same priority don't leave each other out, so the default (everyone 1) changes nothing.

    This car's own charger is subtracted on its own (ev_used_consumption), so it is never in the list, not even
    when another car uses the same charger. Read from the options of the config entries, not from the running cars,
    so the answer does not depend on the order they start in. The site entry (no charger) and disabled cars are left out,
    and so are cars whose charger is on its own meter (charger_in_house_meter False) unless include_own_meter: the house
    consumption does not include them, so there is nothing to subtract, but they are still chargers.
    """
    found: set[str] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == own_entry_id or entry.data.get(CONF_SITE) or entry.disabled_by:
            continue
        if site_priority(entry.options) <= own_priority:
            continue
        if not include_own_meter and not charger_in_house_meter(entry.options):
            continue  # on its own meter: the house consumption does not include it, so there is nothing to subtract
        if (entity_id := charger_power_entity(entry.options)) and entity_id != own_charger:
            found.add(entity_id)
    return sorted(found)


def _max_watt(options: dict) -> float:
    """The most a car's charger can draw (W): its phases x max current x voltage, the defaults for what is not stored."""
    charger = DEFAULT_CONFIG["charger"]
    values = []
    for key in ("charging_phases", "charging_max_amp", "power_voltage"):
        try:
            value = float(options.get(key))
        except (TypeError, ValueError):
            value = float(charger[key])
        values.append(value if value > 0 else float(charger[key]))
    return round(values[0] * values[1] * values[2], 2)


def own_meter_chargers_ahead(hass: HomeAssistant, own_entry_id: str | None, own_charger: str | None, own_priority: float) -> dict[str, float]:
    """The chargers on their own meter (charger_in_house_meter False) of the cars AHEAD of this one or with the same
    priority: power sensor -> the most that charger can draw (W), sorted by sensor.

    The house consumption sensor does not include them, but with one meter this car would see their charging as house
    consumption (they take their share of the solar surplus first), so it is added to the house. The cars behind are not:
    with one meter their charging would be subtracted anyway. The maximum is what counts while the sensor has no number
    (the safe side: less solar for this car). This car's own charger (also when another car shares it) is never in it.
    """
    found: dict[str, float] = {}
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == own_entry_id or entry.data.get(CONF_SITE) or entry.disabled_by:
            continue
        if site_priority(entry.options) > own_priority or charger_in_house_meter(entry.options):
            continue
        if (entity_id := charger_power_entity(entry.options)) and entity_id != own_charger:
            found[entity_id] = max(found.get(entity_id, 0.0), _max_watt(entry.options))
    return dict(sorted(found.items()))
