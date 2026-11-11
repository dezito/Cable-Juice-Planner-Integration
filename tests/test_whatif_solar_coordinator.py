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
