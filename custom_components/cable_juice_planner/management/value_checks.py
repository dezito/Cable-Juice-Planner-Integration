"""Sanity checks of sensor values: readings that are in another scale than the code reads them in.

The same file is in BattMind and Cable Juice Planner; change both together (tests/test_value_checks.py in each).

The checks only judge; the health watcher raises the repairs (never a fix: a blueprint can read the same sensor itself, the
charger power in both, so the only fix that is right everywhere is at the sensor). Each verdict is True (looks wrong), False (positively fine) or
None (can't tell: too little history, no recorder). None must never clear a repair.

- A power sensor without a unit is read as W. When its values over POWER_SCALE_DAYS never reach KW_LOOKING_MAX and some
  of them are fractions, it is most likely in kW: read as W it is 1000 times too small.
- A battery level is read in %. When every value over the days lies in 0..1 and some are fractions, it is a share, not a
  percentage: 0.85 is read as 0.85 %.
- A price sensor in a subunit of its currency (øre, ct: Energi Data Service's "show prices in cent"), per MWh or per Wh is
  in another scale than the currency per kWh (100 or 1000 times too large, or 1000 times too small): no price is converted.
  Judged by its unit only, never by the size of the prices.
"""
from __future__ import annotations

import datetime
import math
from collections.abc import Iterable
from functools import partial
from typing import Any

from homeassistant.const import COMPRESSED_STATE_STATE
from homeassistant.core import HomeAssistant

# the days of states a sensor's values are judged on (read once per VALUE_RECHECK, only for a sensor whose state now could
# be in the wrong scale)
POWER_SCALE_DAYS = 3
VALUE_RECHECK = datetime.timedelta(hours=6)
# fewer numbers than this in the days can't tell anything
VALUE_MIN_SAMPLES = 10
# a power in W of a house, a battery, the sun or a charger is above this at some time in POWER_SCALE_DAYS; a power in kW never
KW_LOOKING_MAX = 50.0
# the units a price is in when it is 100 times (a subunit) the price in its currency
_PRICE_SUBUNITS = frozenset(
    {
        "c", "ct", "cts", "cent", "cents", "eurocent", "eurocents", "c€", "¢", "p", "pence", "penny", "gbx",
        "øre", "ore", "öre", "snt", "gr", "grosz", "groszy", "rp", "rappen", "bani", "st", "stotinki",
    }
)


def _fraction(value: float) -> bool:
    return not math.isclose(value, round(value), abs_tol=1e-9)


def power_scale_verdict(values: Iterable[float]) -> bool | None:
    """Whether a power sensor without a unit looks like kW: True, False (it reached KW_LOOKING_MAX, so it is W), None (too
    few numbers, or nothing that tells: all zero, or only whole numbers)."""
    numbers = [abs(value) for value in values if math.isfinite(value)]
    if any(value > KW_LOOKING_MAX for value in numbers):
        return False
    if len(numbers) < VALUE_MIN_SAMPLES or len(set(numbers)) < 2:
        return None
    return True if any(value > 0 and _fraction(value) for value in numbers) else None


def battery_fraction_verdict(values: Iterable[float]) -> bool | None:
    """Whether a battery level is a share 0..1 instead of a percentage: True, False (a value above 1, so it is in %), None
    (too few numbers, no change, or only 0 and 1)."""
    numbers = [value for value in values if math.isfinite(value)]
    if any(value > 1 for value in numbers):
        return False
    if len(numbers) < VALUE_MIN_SAMPLES or len(set(numbers)) < 2 or any(value < 0 for value in numbers):
        return None
    return True if any(0 < value < 1 and _fraction(value) for value in numbers) else None


def could_be_kw(state_value: Any) -> bool | None:
    """The state of a power sensor without a unit now: False when it is above KW_LOOKING_MAX (so in W), True when it could be
    kW (the history decides), None when it is no number (not judged)."""
    value = _number(state_value)
    if value is None:
        return None
    return abs(value) <= KW_LOOKING_MAX


def could_be_fraction(state_value: Any) -> bool | None:
    """The state of a battery level now: False above 1 (so in %), True in 0..1 (the history decides), None when it is no number
    or below 0 (not judged)."""
    value = _number(state_value)
    if value is None or value < 0:
        return None
    return value <= 1


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def is_price_subunit(unit: Any) -> bool:
    """A price per Wh, kWh or MWh in a subunit of its currency (øre/kWh, ct/kWh, GBp/kWh): 100 times the price in the currency."""
    if not isinstance(unit, str) or "/" not in unit:
        return False
    currency, per = (part.strip() for part in unit.rsplit("/", 1))
    if per.lower() not in ("wh", "kwh", "mwh") or not currency:
        return False
    token = currency.lower()
    subunit_code = len(currency) > 1 and currency[:-1].isupper() and currency[-1].islower()  # GBp, EURct is caught below
    return token in _PRICE_SUBUNITS or token.endswith(("ct", "cent", "cents")) or (subunit_code and len(currency) == 3)


def is_price_per_mwh(unit: Any) -> bool:
    return _price_per(unit) == "mwh"


def is_price_per_wh(unit: Any) -> bool:
    return _price_per(unit) == "wh"


def _price_per(unit: Any) -> str | None:
    return unit.rsplit("/", 1)[1].strip().lower() if isinstance(unit, str) and "/" in unit else None


def price_unit_wrong(unit: Any) -> bool:
    """A price unit the prices are not read in: a subunit (100 times), per MWh (1000 times) or per Wh (1/1000). No unit, or
    any other, passes: it is not guessed."""
    return is_price_subunit(unit) or is_price_per_mwh(unit) or is_price_per_wh(unit)


def number_text(value: float, language: str | None) -> str:
    """A value for a repair text: 3 significant digits (38.24 -> 38.2, 0.857 -> 0.857), whole numbers from 1000, with a
    decimal comma unless the language is English."""
    text = f"{value:.3g}" if abs(value) < 1000 else f"{value:.0f}"
    return text if (language or "en").lower().startswith("en") else text.replace(".", ",")


async def async_recorded_numbers(hass: HomeAssistant, entity_ids: list[str], days: int = POWER_SCALE_DAYS) -> dict[str, list[float]] | None:
    """The numeric states of the entities over the last days, None without a recorder. The states are read and turned into
    numbers in the recorder's executor (days of a sensor that reports every second are many rows). A state that is no
    number is left out."""
    if not entity_ids or "recorder" not in hass.config.components:
        return None
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415
    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    end = dt_util.utcnow()
    return await get_instance(hass).async_add_executor_job(
        partial(_recorded_numbers, hass, end - datetime.timedelta(days=days), end, list(entity_ids))
    )


def _recorded_numbers(hass: HomeAssistant, start: datetime.datetime, end: datetime.datetime, entity_ids: list[str]) -> dict[str, list[float]]:
    """In the recorder's executor: the compressed states (what the frontend graphs read, a plain dict per change) as numbers."""
    from homeassistant.components.recorder import history  # noqa: PLC0415

    states = history.get_significant_states(
        hass,
        start,
        end,
        entity_ids,
        None,
        include_start_time_state=True,
        significant_changes_only=False,
        minimal_response=True,
        no_attributes=True,
        compressed_state_format=True,
    )
    result: dict[str, list[float]] = {}
    for entity_id in entity_ids:
        numbers = []
        for state in states.get(entity_id, []):
            if (value := _number(state.get(COMPRESSED_STATE_STATE) if isinstance(state, dict) else None)) is not None:
                numbers.append(value)
        result[entity_id] = numbers
    return result
