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
    ), patch.dict(coordinator.config["charger"]["entity_ids"], {"charger_power_entity_id": "sensor.power"}):
        data: dict = {}
        end = datetime(2026, 10, 2, tzinfo=UTC)
        await coordinator._async_economy_ev_statistics(_FakeRecorder(), data, end, _stat_ts)
    # a level row without all three values is no hour; a power row without a level row is no hour either
    assert data["ev_hours"] == {start.timestamp(): [61.0, 60.0, 62.0, 7000.0]}
    begin, finish, ids, period, units, kinds = seen["call"]
    assert (finish - begin).days == 403 and finish == end  # as far back as the economy keeps prices (12 months of history)
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
    ), patch.dict(coordinator.config["charger"]["entity_ids"], {"charger_power_entity_id": "sensor.power"}):
        data = {}
        await coordinator._async_economy_ev_statistics(_FakeRecorder(), data, end, _stat_ts)
    assert data == {}  # the stored estimate stays


async def test_the_hours_the_statistics_lack_are_filled_from_the_recorder_states_in_watts_and_percent(hass: HomeAssistant, loaded_entry) -> None:
    """Like BattMind's what-if: an entity without statistics (no state_class) still gives its hours from the recorder states."""
    from datetime import timedelta
    from homeassistant.core import State  # noqa: PLC0415

    coordinator = loaded_entry.runtime_data
    end = datetime(2026, 10, 2, tzinfo=UTC)
    hour = datetime(2026, 10, 1, 10, tzinfo=UTC)

    def state(entity, value, moment):
        return State(entity, str(value), last_changed=moment, last_updated=moment)

    states = {
        "sensor.level": [state("sensor.level", 40.0, hour - timedelta(hours=1)), state("sensor.level", 50.0, hour + timedelta(minutes=30)),
                         state("sensor.level", 60.0, hour + timedelta(hours=1, minutes=10))],
        # a charger that reports in kW: 7 kW all the time
        "sensor.power": [state("sensor.power", 7.0, hour - timedelta(hours=1))],
    }

    def raw(hass_, begin, stop, ids, *args, **kwargs):
        return {entity: [s for s in states[entity]] for entity in ids if entity in states}

    coordinator.ev_configured = True
    hass.states.async_set("sensor.power", "7", {"unit_of_measurement": "kW"})
    with patch("homeassistant.components.recorder.statistics.statistics_during_period", return_value={}), patch(
        "homeassistant.components.recorder.history.get_significant_states", side_effect=raw
    ), patch.dict(coordinator.config["ev_car"]["entity_ids"], {"usable_battery_level_entity_id": "sensor.level"}), patch.dict(
        coordinator.config["charger"]["entity_ids"], {"charger_power_entity_id": "sensor.power"}
    ):
        data: dict = {}
        await coordinator._async_economy_ev_statistics(_FakeRecorder(), data, end, _stat_ts, keep_days=30)
    h = hour.timestamp()
    # the hour of 10:00: 40 % for half an hour, then 50 %: mean 45, lowest 40, highest 50; the power in W (7 kW)
    assert data["ev_hours"][h] == [45.0, 40.0, 50.0, 7000.0]
    assert data["ev_hours"][h - 3600.0] == [40.0, 40.0, 40.0, 7000.0]  # an hour that is all 40 %
    assert h + 7200.0 in data["ev_hours"] and data["ev_hours"][h + 7200.0][:3] == [60.0, 60.0, 60.0]
