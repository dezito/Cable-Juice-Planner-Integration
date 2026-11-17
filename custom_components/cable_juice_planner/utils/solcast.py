"""Names of the Solcast forecast sensors Cable Juice Planner reads."""
from __future__ import annotations

SOLCAST_PLATFORM = "solcast_solar"
SOLCAST_ENDINGS = ["today", "tomorrow", "day_3", "day_4", "day_5", "day_6", "day_7"]
# the unique ids Solcast gives its forecast sensors (the registry finds them even if the user renamed them)
SOLCAST_UNIQUE_IDS = {
    "today": "total_kwh_forecast_today",
    "tomorrow": "total_kwh_forecast_tomorrow",
    **{f"day_{day}": f"total_kwh_forecast_d{day}" for day in range(3, 8)},
}


def strip_forecast_suffix(entity_id: str) -> str:
    """The part of a Solcast forecast entity id before _today / _tomorrow / _day_N."""
    parts = entity_id.split("_")
    if parts[-1] in SOLCAST_ENDINGS:
        return "_".join(parts[:-1])
    if len(parts) >= 2 and parts[-2] == "day" and parts[-1].isdigit():
        return "_".join(parts[:-2])
    return entity_id


def solcast_endings_for_days(days: int) -> list[str]:
    """The forecast sensors a plan of this many days (today included) needs: today, tomorrow and day_3 .. day_<days>."""
    return [ending for ending in SOLCAST_ENDINGS if ending in ("today", "tomorrow") or int(ending.removeprefix("day_")) <= days]
