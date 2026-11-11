"""What-if older connections: the coordinator reads the battery level and charger power statistics."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from homeassistant.core import HomeAssistant


class _FakeRecorder:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _stat_ts(row):
    return row["start"].timestamp()


async def test_the_battery_level_and_the_charger_power_are_read_as_hourly_statistics(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    seen: dict = {}
    start = datetime(2026, 10, 1, 10, tzinfo=UTC)

    def statistics(hass_, begin, end, ids, period, units, kinds):
        seen["call"] = (begin, end, set(ids), period, units, kinds)
        return {
            "sensor.level": [{"start": start, "mean": 61.0, "min": 60.0, "max": 62.0}, {"start": datetime(2026, 10, 1, 11, tzinfo=UTC), "mean": 62.0, "min": None, "max": 62.0}],
            "sensor.power": [{"start": start, "mean": -7000.0}, {"start": datetime(2026, 10, 1, 11, tzinfo=UTC), "mean": 5.0}, {"start": datetime(2026, 10, 1, 12, tzinfo=UTC), "mean": 1.0}],
        }

    coordinator.ev_configured = True
    with patch("homeassistant.components.recorder.statistics.statistics_during_period", side_effect=statistics), patch.dict(
        coordinator.config["ev_car"]["entity_ids"], {"usable_battery_level_entity_id": "sensor.level"}
    ), patch.dict(coordinator.config["charger"]["entity_ids"], {"power_consumtion_entity_id": "sensor.power"}):
        data: dict = {}
        end = datetime(2026, 10, 2, tzinfo=UTC)
        await coordinator._async_economy_ev_statistics(_FakeRecorder(), data, end, _stat_ts)
    # a level row without all three values is no hour; a power row without a level row is no hour either
    assert data["ev_hours"] == {start.timestamp(): [61.0, 60.0, 62.0, 7000.0]}
    begin, finish, ids, period, units, kinds = seen["call"]
    assert (finish - begin).days == 400 and finish == end  # as far back as the economy keeps prices
    assert ids == {"sensor.level", "sensor.power"} and period == "hour" and units == {"power": "W"} and kinds == {"mean", "min", "max"}


async def test_without_the_entities_nothing_is_estimated_and_a_failing_read_leaves_the_key_out(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    end = datetime(2026, 10, 2, tzinfo=UTC)
    coordinator.ev_configured = False
    data: dict = {}
    await coordinator._async_economy_ev_statistics(_FakeRecorder(), data, end, _stat_ts)
    assert data == {"ev_hours": {}}
    coordinator.ev_configured = True
    with patch("homeassistant.components.recorder.statistics.statistics_during_period", side_effect=RuntimeError("no statistics")), patch.dict(
        coordinator.config["ev_car"]["entity_ids"], {"usable_battery_level_entity_id": "sensor.level"}
    ), patch.dict(coordinator.config["charger"]["entity_ids"], {"power_consumtion_entity_id": "sensor.power"}):
        data = {}
        await coordinator._async_economy_ev_statistics(_FakeRecorder(), data, end, _stat_ts)
    assert data == {}  # the stored estimate stays
