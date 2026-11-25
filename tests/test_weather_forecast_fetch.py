"""The weather forecast of the coordinator's snapshot: also without solar when a weather entity is set (preheating, Adjust
driving efficiency to temperature), never without one; the solar forecast rows stay solar only."""
from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse

from .conftest import MOCK_OPTIONS, async_setup, async_unload_and_join, make_entry, set_source_states

WEATHER = "weather.home"
PRODUCTION = "sensor.solar_production"
HOURLY = [{"datetime": "2026-10-23T05:00:00+00:00", "temperature": -3.0}]


def _weather_service(hass: HomeAssistant) -> list[dict]:
    calls: list[dict] = []

    async def get_forecasts(call: ServiceCall) -> dict:
        calls.append(dict(call.data))
        return {WEATHER: {"forecast": HOURLY}}

    hass.services.async_register("weather", "get_forecasts", get_forecasts, supports_response=SupportsResponse.ONLY)
    hass.states.async_set(WEATHER, "cloudy", {"temperature": 2.0})
    return calls


async def _snapshot(hass: HomeAssistant, options: dict):
    set_source_states(hass)
    hass.states.async_set(PRODUCTION, "0", {"unit_of_measurement": "W", "device_class": "power"})
    entry = make_entry(options={**MOCK_OPTIONS, **options})
    await async_setup(hass, entry)
    try:
        return await entry.runtime_data._async_snapshot(include_forecast=True)  # noqa: SLF001
    finally:
        await async_unload_and_join(hass, entry)


async def test_without_solar_a_weather_entity_gives_the_forecast(hass: HomeAssistant) -> None:
    calls = _weather_service(hass)
    snapshot = await _snapshot(hass, {"production_entity_id": "", "hourly_service_entity_id": WEATHER, "daily_service_entity_id": ""})
    assert snapshot.forecast_dict == {"hourly": HOURLY, "daily": {}}
    assert {"entity_id": WEATHER, "type": "hourly"} in calls
    assert not snapshot.solcast  # the solar forecast rows stay solar only


async def test_without_solar_and_without_a_weather_entity_the_service_is_not_called(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    calls = _weather_service(hass)
    hass.services.async_remove("weather", "get_forecasts")  # not even its missing service is logged
    snapshot = await _snapshot(hass, {"production_entity_id": "", "hourly_service_entity_id": "", "daily_service_entity_id": ""})
    assert snapshot.forecast_dict is None
    assert calls == []
    assert "weather.get_forecasts" not in caplog.text


async def test_with_solar_the_forecast_is_fetched_as_before(hass: HomeAssistant) -> None:
    calls = _weather_service(hass)
    snapshot = await _snapshot(hass, {"production_entity_id": PRODUCTION, "hourly_service_entity_id": WEATHER, "daily_service_entity_id": ""})
    assert snapshot.forecast_dict == {"hourly": HOURLY, "daily": {}}
    assert {"entity_id": WEATHER, "type": "hourly"} in calls


async def test_without_solar_a_daily_forecast_alone_is_fetched(hass: HomeAssistant) -> None:
    calls = _weather_service(hass)
    snapshot = await _snapshot(hass, {"production_entity_id": "", "hourly_service_entity_id": "", "daily_service_entity_id": WEATHER})
    assert snapshot.forecast_dict == {"hourly": {}, "daily": HOURLY}
    assert calls and all(call == {"entity_id": WEATHER, "type": "daily"} for call in calls)  # never the hourly one
