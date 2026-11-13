"""What-if Scenario D through Home Assistant: the sensor exists, is not recorded and starts empty."""
from __future__ import annotations

from unittest.mock import patch

from homeassistant.core import HomeAssistant

from .conftest import OBJECT_PREFIX

SENSOR = f"sensor.{OBJECT_PREFIX}_what_if_solar"


async def test_the_solar_sensor_exists_and_says_why_it_has_no_value(hass: HomeAssistant, loaded_entry) -> None:
    state = hass.states.get(SENSOR)
    assert state is not None
    assert state.state == "unknown"  # never a zero advantage
    result = state.attributes["result"]
    assert result["status"] == "unavailable" and result["reason"] in ("no_solar_configured", "waiting_for_new_sessions", "no_solar_sessions")


async def test_the_economy_update_and_the_hourly_store_both_publish_the_solar_comparison(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    engine = coordinator.engine
    calls: list[str] = []
    original = type(engine).publish_what_if_solar

    def spy(self) -> None:
        calls.append("published")
        original(self)

    with patch.object(type(engine), "publish_what_if_solar", spy):
        result = await coordinator.async_run_job("economy_update", {"prices": {}, "windows": [], "odometer_months": {}})
        assert result.error is None and calls == ["published"]
        result = await coordinator.async_run_job("economy_store_prices")
        assert result.error is None and calls == ["published", "published"]


class _FakeRecorder:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


async def test_the_economy_refresh_reads_the_sell_price_and_the_solar_house_and_charger_power_statistics(hass: HomeAssistant, loaded_entry) -> None:
    from datetime import UTC, datetime

    coordinator = loaded_entry.runtime_data
    seen: dict = {}
    start = datetime(2026, 10, 1, 10, tzinfo=UTC)

    def statistics(hass_, begin, end, ids, period, units, kinds):
        seen[tuple(sorted(ids))] = (period, units, kinds)
        # the first sell sensor has no statistics: the history (and its conversion) is the second one's
        return {entity_id: [] if entity_id == "sensor.sell_first" else [{"start": start, "mean": 1500.0 if "solar" in entity_id else 800.0}] for entity_id in ids}

    provider = coordinator.price_provider
    with patch("homeassistant.components.recorder.statistics.statistics_during_period", side_effect=statistics), patch.object(
        coordinator.engine, "is_solar_configured", return_value=True
    ), patch.dict(coordinator.config["solar"]["entity_ids"], {"production_entity_id": "sensor.solar"}), patch.dict(
        coordinator.config["home"]["entity_ids"], {"power_consumption_entity_id": "sensor.house"}
    ), patch.dict(coordinator.config["charger"]["entity_ids"], {"power_consumtion_entity_id": "sensor.charger"}), patch.object(
        provider, "sell_price_entity_ids", return_value=["sensor.sell_first", "sensor.sell"]
    ):
        data: dict = {}
        await coordinator._async_economy_solar_statistics(_FakeRecorder(), provider, data, datetime(2026, 10, 2, tzinfo=UTC), lambda row: row["start"].timestamp())
    assert data["sell_history"] == {start.timestamp(): 800.0}
    assert data["sell_history_entity_id"] == "sensor.sell"
    assert data["power_hours"] == {start.timestamp(): {"solar": 1500.0, "house": 800.0, "ev": 800.0}}
    # the power is asked for in watts whatever unit the sensors have, hourly means
    assert seen[("sensor.charger", "sensor.house", "sensor.solar")] == ("hour", {"power": "W"}, {"mean"})


async def test_a_failing_statistics_read_never_stops_the_economy_refresh(hass: HomeAssistant, loaded_entry) -> None:
    from datetime import UTC, datetime

    coordinator = loaded_entry.runtime_data
    with patch("homeassistant.components.recorder.statistics.statistics_during_period", side_effect=RuntimeError("no statistics")):
        data: dict = {}
        await coordinator._async_economy_solar_statistics(_FakeRecorder(), coordinator.price_provider, data, datetime(2026, 10, 2, tzinfo=UTC), lambda row: 0.0)
    assert data == {}
