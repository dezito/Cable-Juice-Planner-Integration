"""What-if Scenario A through Home Assistant: the entities, the button and the result sensor."""
from __future__ import annotations

import datetime
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .conftest import OBJECT_PREFIX

SENSOR = f"sensor.{OBJECT_PREFIX}_what_if"
BUTTON = f"button.{OBJECT_PREFIX}_run_what_if"


def _seed(coordinator) -> None:
    """Three connections (evening to morning) with hourly prices and a drive after each."""
    engine = coordinator.engine
    now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    prices = engine.economy_db.setdefault("prices", {})
    for back in (2, 3, 4):
        start = (now - datetime.timedelta(days=back)).replace(hour=17)
        end = start + datetime.timedelta(hours=14)
        engine.economy_db["windows"].append({"start": start.timestamp(), "end": end.timestamp()})
        hour = start
        while hour < end:
            prices[hour.timestamp()] = 0.4 if 1 <= hour.hour < 5 else 1.2
            hour += datetime.timedelta(hours=1)
        engine.analysis_db["drives"].append({"start": end.timestamp() + 600, "end": end.timestamp() + 3000, "km": 30.0, "used": 15.0, "temp": None})
    engine.economy_db["prices_averaged_since"] = (now - datetime.timedelta(days=30)).timestamp()


async def _press(hass: HomeAssistant) -> None:
    await hass.services.async_call("button", "press", {"entity_id": BUTTON}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_the_what_if_entities_exist(hass: HomeAssistant, loaded_entry) -> None:
    for entity_id in (
        f"number.{OBJECT_PREFIX}_what_if_days", f"number.{OBJECT_PREFIX}_what_if_target_battery_level",
        f"number.{OBJECT_PREFIX}_what_if_start_battery_level", f"select.{OBJECT_PREFIX}_what_if_day_selection", BUTTON, SENSOR,
        *(f"switch.{OBJECT_PREFIX}_what_if_day_{day}" for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")),
    ):
        assert hass.states.get(entity_id) is not None, entity_id
    assert hass.states.get(f"number.{OBJECT_PREFIX}_what_if_days").state == "30.0"
    assert hass.states.get(f"select.{OBJECT_PREFIX}_what_if_day_selection").state == "all"


async def test_the_button_without_a_start_level_is_unavailable_and_never_defaults(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    _seed(coordinator)
    with patch.object(type(coordinator), "_async_what_if_start_soc", return_value={"soc": None, "source": "unavailable"}):
        await _press(hass)
    attributes = hass.states.get(SENSOR).attributes
    assert attributes["attempt"]["status"] == "unavailable" and attributes["attempt"]["reason"] == "no_start_soc"
    assert attributes["result"] is None and hass.states.get(SENSOR).state == "unknown"


async def test_the_button_reads_the_level_replays_and_publishes(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    _seed(coordinator)
    settings_before = dict(coordinator.settings)
    with patch.object(type(coordinator), "_async_what_if_start_soc", return_value={"soc": 55.0, "source": "recorded_state"}):
        await _press(hass)
    state = hass.states.get(SENSOR)
    result = state.attributes["result"]
    assert result["status"] == "ok" and result["provenance"]["start_soc"] == "recorded_state"
    assert result["smart"]["start_soc"] == 55.0 and result["windows"]["replayed"] == 3
    assert result["smart"]["cost"] < result["asap"]["cost"]
    assert state.state == str(result["difference"]["adjusted_cost"])
    assert state.attributes["attempt"]["status"] == "ok"
    # the live plan, settings and car are untouched
    assert dict(coordinator.settings) == settings_before


async def test_an_entered_start_level_is_used_instead_of_reading_the_history(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    _seed(coordinator)
    await hass.services.async_call(
        "number", "set_value", {"entity_id": f"number.{OBJECT_PREFIX}_what_if_start_battery_level", "value": 60}, blocking=True
    )
    with patch.object(type(coordinator), "_async_read_soc", side_effect=AssertionError("the history must not be read")):
        await _press(hass)
    result = hass.states.get(SENSOR).attributes["result"]
    assert result["provenance"]["start_soc"] == "entered" and result["smart"]["start_soc"] == 60.0


async def test_the_result_survives_a_failed_second_calculation(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    _seed(coordinator)
    with patch.object(type(coordinator), "_async_what_if_start_soc", return_value={"soc": 55.0, "source": "recorded_state"}):
        await _press(hass)
    first = hass.states.get(SENSOR).attributes["result"]["computed_at"]
    with patch.object(type(coordinator), "_async_what_if_start_soc", return_value={"soc": None, "source": "unavailable"}):
        await _press(hass)
    attributes = hass.states.get(SENSOR).attributes
    assert attributes["result"]["computed_at"] == first
    assert attributes["attempt"]["status"] == "unavailable"


async def test_the_start_level_comes_from_the_user_first_and_is_never_invented(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    assert await coordinator._async_what_if_start_soc({"manual_start_soc": 42.0, "first_window_start": 1.0}) == {"soc": 42.0, "source": "entered"}
    # without a recorder (as here) or a first connection there is nothing to read: unknown, not a default
    assert await coordinator._async_what_if_start_soc({"manual_start_soc": None, "first_window_start": 1.0}) == {"soc": None, "source": "unavailable"}
    assert await coordinator._async_what_if_start_soc({"manual_start_soc": None, "first_window_start": None}) == {"soc": None, "source": "unavailable"}
    with patch.object(type(coordinator), "_async_read_soc", side_effect=RuntimeError("recorder gone")), patch.object(
        hass.config, "components", {*hass.config.components, "recorder"}
    ):
        assert await coordinator._async_what_if_start_soc({"manual_start_soc": None, "first_window_start": 1.0}) == {"soc": None, "source": "unavailable"}


class _FakeRecorder:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _State:
    def __init__(self, state: str) -> None:
        self.state = state


async def test_the_recorder_read_prefers_the_recorded_state_then_the_hourly_mean(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    soc_id = "sensor.ev_battery_level"
    start = datetime.datetime(2026, 10, 22, 18, 20, tzinfo=datetime.UTC).timestamp()
    seen: dict = {}

    def significant(hass_, begin, end, ids, *args, **kwargs):
        seen["begin"], seen["ids"] = begin, ids
        return {soc_id: [_State("unavailable"), _State("63.5")]}

    def statistics(hass_, begin, end, ids, period, types, kinds):
        seen["stats"] = (begin, end, period, kinds)
        return {soc_id: [{"mean": 48.2}]}

    with patch("homeassistant.components.recorder.get_instance", return_value=_FakeRecorder()), patch(
        "homeassistant.components.recorder.history.get_significant_states", side_effect=significant
    ), patch("homeassistant.components.recorder.statistics.statistics_during_period", side_effect=statistics):
        # 1. the state recorded at the plug-in (unavailable readings are skipped)
        assert await coordinator._async_read_soc(soc_id, start) == {"soc": 63.5, "source": "recorded_state"}
        assert seen["begin"] == datetime.datetime.fromtimestamp(start, datetime.UTC) and seen["ids"] == [soc_id]
    with patch("homeassistant.components.recorder.get_instance", return_value=_FakeRecorder()), patch(
        "homeassistant.components.recorder.history.get_significant_states", return_value={soc_id: [_State("unknown"), _State("140")]}
    ), patch("homeassistant.components.recorder.statistics.statistics_during_period", side_effect=statistics):
        # 2. no usable raw state (purged, unknown or out of range): the hour of the plug-in, labelled approximate
        assert await coordinator._async_read_soc(soc_id, start) == {"soc": 48.2, "source": "statistics_hour_mean"}
        assert seen["stats"][0] == datetime.datetime(2026, 10, 22, 18, tzinfo=datetime.UTC) and seen["stats"][2] == "hour"
    with patch("homeassistant.components.recorder.get_instance", return_value=_FakeRecorder()), patch(
        "homeassistant.components.recorder.history.get_significant_states", return_value={}
    ), patch("homeassistant.components.recorder.statistics.statistics_during_period", return_value={soc_id: [{"mean": None}]}):
        # 3. nothing at all: unknown, never a default
        assert await coordinator._async_read_soc(soc_id, start) == {"soc": None, "source": "unavailable"}


async def test_changing_a_what_if_setting_does_not_recalculate_the_live_plan(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    for entity_id, value in (
        (f"number.{OBJECT_PREFIX}_what_if_days", 90),
        (f"number.{OBJECT_PREFIX}_what_if_target_battery_level", 80),
        (f"number.{OBJECT_PREFIX}_what_if_start_battery_level", 50),
    ):
        await hass.services.async_call("number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True)
    await hass.services.async_call(
        "select", "select_option", {"entity_id": f"select.{OBJECT_PREFIX}_what_if_day_selection", "option": "weekend"}, blocking=True
    )
    assert coordinator.settings["what_if_days"] == 90 and coordinator.settings["what_if_day_selection"] == "weekend"
    await hass.services.async_call("switch", "turn_on", {"entity_id": f"switch.{OBJECT_PREFIX}_what_if_day_friday"}, blocking=True)
    assert coordinator.settings["what_if_day_friday"] is True
    assert coordinator._settings_debounce is None  # an ordinary setting would have scheduled a recalculation
    await hass.services.async_call(
        "number", "set_value", {"entity_id": f"number.{OBJECT_PREFIX}_min_daily_battery_level", "value": 45}, blocking=True
    )
    assert coordinator._settings_debounce is not None


async def test_a_second_press_while_running_is_ignored_and_a_crash_never_leaves_it_running(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    _seed(coordinator)
    coordinator._what_if_running = True
    with patch.object(type(coordinator), "_async_what_if_start_soc", side_effect=AssertionError("must not run twice")):
        await _press(hass)
    assert hass.states.get(SENSOR).attributes["attempt"] is None
    coordinator._what_if_running = False
    with patch.object(type(coordinator), "_async_what_if_start_soc", side_effect=RuntimeError("boom")):
        await _press(hass)
    attempt = hass.states.get(SENSOR).attributes["attempt"]
    assert attempt["status"] == "unavailable" and attempt["reason"] == "internal_error"  # not stuck on "running"
    assert coordinator._what_if_running is False
