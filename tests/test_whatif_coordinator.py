"""What-if Scenario A through Home Assistant: the entities, the button and the result sensor."""
from __future__ import annotations

import asyncio
import datetime
import threading
import time
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.cable_juice_planner.engine.whatif.replay import WhatIfCancelled
from custom_components.cable_juice_planner.engine.whatif.replay import replay as real_replay

from .conftest import OBJECT_PREFIX

SENSOR = f"sensor.{OBJECT_PREFIX}_what_if"
BUTTON = f"button.{OBJECT_PREFIX}_run_what_if"
START = {"soc": 55.0, "source": "recorded_state"}
END = {"soc": 70.0, "source": "recorded_state"}  # 55 % + 3 x 15 % charged - 2 x 15 % driven


def _seed(coordinator) -> None:
    """Three connections (evening to morning) with hourly prices, a drive after each and the charging CJP did in each (its history)."""
    engine = coordinator.engine
    now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    prices = engine.economy_db.setdefault("prices", {})
    for back in (2, 3, 4):
        start = (now - datetime.timedelta(days=back)).replace(hour=17)
        end = start + datetime.timedelta(hours=14)
        engine.economy_db["windows"].append({"start": start.timestamp(), "end": end.timestamp()})
        # what CJP charged in it (its charging history): 8 kWh (15 %) in the cheap hours, booked at 0.4
        began = dt_util.as_local(start + datetime.timedelta(hours=9)).replace(tzinfo=None)
        engine.charging_history_db[began] = {
            "kWh": 8.0, "kWh_from_grid": 8.0, "kWh_from_local_energy": 0.0, "percentage": 15.0, "cost": 3.2,
            "ended": (began + datetime.timedelta(hours=1)).strftime("%H:%M"), "charging_type": ["planned"],
        }
        hour = start
        while hour < end:
            prices[hour.timestamp()] = 0.4 if 1 <= hour.hour < 5 else 1.2
            hour += datetime.timedelta(hours=1)
        engine.analysis_db["drives"].append({"start": end.timestamp() + 600, "end": end.timestamp() + 3000, "km": 30.0, "used": 15.0, "temp": None})
    engine.economy_db["prices_averaged_since"] = (now - datetime.timedelta(days=30)).timestamp()
    old = dt_util.as_local(now - datetime.timedelta(days=7)).replace(tzinfo=None)  # a session before the first connection: the history reaches it
    engine.charging_history_db[old] = {
        "kWh": 1.0, "kWh_from_grid": 1.0, "kWh_from_local_energy": 0.0, "percentage": 1.0, "cost": 0.3,
        "ended": (old + datetime.timedelta(minutes=30)).strftime("%H:%M"), "charging_type": ["planned"],
    }


async def _press(hass: HomeAssistant) -> None:
    await hass.services.async_call("button", "press", {"entity_id": BUTTON}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_connection_that_settles_while_the_end_level_is_read_makes_the_run_stale(hass: HomeAssistant, loaded_entry) -> None:
    """The level at the last unplug belongs to the last connection it was read for: another one settling meanwhile refuses the run."""
    coordinator = loaded_entry.runtime_data
    _seed(coordinator)

    async def end_level_while_another_connection_settles(self, prepared):
        now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        start = (now - datetime.timedelta(days=1)).replace(hour=17)
        coordinator.engine.economy_db["windows"].append({"start": start.timestamp(), "end": (start + datetime.timedelta(hours=3)).timestamp()})
        return END

    with patch.object(type(coordinator), "_async_what_if_start_soc", return_value=START), patch.object(
        type(coordinator), "_async_what_if_end_soc", end_level_while_another_connection_settles
    ):
        await _press(hass)
    attempt = hass.states.get(SENSOR).attributes["attempt"]
    assert attempt["status"] == "unavailable" and attempt["reason"] == "start_level_stale"


async def test_the_what_if_entities_exist(hass: HomeAssistant, loaded_entry) -> None:
    for entity_id in (
        f"number.{OBJECT_PREFIX}_what_if_days", f"number.{OBJECT_PREFIX}_what_if_max_recommended_battery_level",
        f"number.{OBJECT_PREFIX}_what_if_start_battery_level", f"select.{OBJECT_PREFIX}_what_if_day_selection", BUTTON, SENSOR,
        *(f"switch.{OBJECT_PREFIX}_what_if_day_{day}" for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")),
    ):
        assert hass.states.get(entity_id) is not None, entity_id
    assert hass.states.get(f"number.{OBJECT_PREFIX}_what_if_days").state == "30.0"
    assert hass.states.get(f"select.{OBJECT_PREFIX}_what_if_day_selection").state == "all"


async def test_the_button_without_a_start_level_gives_no_result_and_says_why(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    _seed(coordinator)
    with patch.object(type(coordinator), "_async_what_if_start_soc", return_value={"soc": None, "source": "unavailable"}):
        await _press(hass)
    attempt = hass.states.get(SENSOR).attributes["attempt"]
    assert attempt["status"] == "unavailable" and attempt["reason"] == "no_start_soc"  # CJP's record can only be followed from a known level


async def test_the_button_reads_the_levels_replays_and_publishes(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    _seed(coordinator)
    settings_before = dict(coordinator.settings)
    with patch.object(type(coordinator), "_async_what_if_start_soc", return_value=START), patch.object(
        type(coordinator), "_async_what_if_end_soc", return_value=END
    ):
        await _press(hass)
    state = hass.states.get(SENSOR)
    result = state.attributes["result"]
    assert result["status"] == "ok" and result["provenance"]["start_soc"] == "recorded_state" and result["provenance"]["end_soc"] == "recorded_state"
    assert result["cjp"]["start_soc"] == 55.0 and result["cjp"]["final_soc"] == 70.0 and result["windows"]["replayed"] == 3
    assert result["cjp"]["cost"] < result["asap"]["cost"]
    assert state.state == str(result["difference"]["adjusted_cost"])
    assert state.attributes["attempt"]["status"] == "ok"
    # the live plan, settings and car are untouched
    assert dict(coordinator.settings) == settings_before


async def test_the_level_at_the_end_is_read_like_the_start_and_never_defaulted(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    assert await coordinator._async_what_if_end_soc({"last_window_end": None}) == {"soc": None, "source": "unavailable"}
    # without a recorder (as here) there is nothing to read: unknown, so the three are not balanced, and the result says so
    assert await coordinator._async_what_if_end_soc({"last_window_end": 1.0}) == {"soc": None, "source": "unavailable"}
    seen: list[float] = []

    async def read(self, soc_id, ts):
        seen.append(ts)
        return {"soc": 64.0, "source": "recorded_state"}

    with patch.object(type(coordinator), "_async_read_soc", read), patch.object(hass.config, "components", {*hass.config.components, "recorder"}):
        assert await coordinator._async_what_if_end_soc({"last_window_end": 1234.0}) == {"soc": 64.0, "source": "recorded_state"}
    assert seen == [1234.0]  # read where the last compared connection ended
    with patch.object(type(coordinator), "_async_read_soc", side_effect=RuntimeError("recorder gone")), patch.object(
        hass.config, "components", {*hass.config.components, "recorder"}
    ):
        assert await coordinator._async_what_if_end_soc({"last_window_end": 1.0}) == {"soc": None, "source": "unavailable"}


async def test_an_entered_start_level_is_used_instead_of_reading_the_history(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    _seed(coordinator)
    await hass.services.async_call(
        "number", "set_value", {"entity_id": f"number.{OBJECT_PREFIX}_what_if_start_battery_level", "value": 60}, blocking=True
    )
    with patch.object(type(coordinator), "_async_read_soc", side_effect=AssertionError("the history must not be read")):
        await _press(hass)
    result = hass.states.get(SENSOR).attributes["result"]
    assert result["provenance"]["start_soc"] == "entered" and result["cjp"]["start_soc"] == 60.0


async def test_the_result_survives_a_failed_second_calculation(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    _seed(coordinator)
    with patch.object(type(coordinator), "_async_what_if_start_soc", return_value={"soc": 55.0, "source": "recorded_state"}):
        await _press(hass)
    first = hass.states.get(SENSOR).attributes["result"]["computed_at"]
    coordinator.engine.analysis_db["drives"].clear()  # the demand is now unknown: the second calculation has no result
    with patch.object(type(coordinator), "_async_what_if_start_soc", return_value={"soc": 55.0, "source": "recorded_state"}):
        await _press(hass)
    attributes = hass.states.get(SENSOR).attributes
    assert attributes["result"]["computed_at"] == first
    assert attributes["attempt"]["status"] == "unavailable" and attributes["attempt"]["reason"] == "no_drives"


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
        (f"number.{OBJECT_PREFIX}_what_if_max_recommended_battery_level", 80),
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


async def _press_without_waiting(hass: HomeAssistant, entity_id: str = BUTTON) -> None:
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)


async def _wait_for(hass: HomeAssistant, condition, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.02)


async def test_the_replay_runs_on_its_own_thread_so_the_live_worker_is_never_held_up(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    _seed(coordinator)
    started, release = threading.Event(), threading.Event()
    threads: list[str] = []

    def blocked_replay(data):
        threads.append(threading.current_thread().name)
        started.set()
        assert release.wait(30)
        return real_replay(data)

    with patch.object(type(coordinator), "_async_what_if_start_soc", return_value=START), patch(
        "custom_components.cable_juice_planner.coordinator.what_if.replay", blocked_replay
    ):
        await _press_without_waiting(hass)
        await _wait_for(hass, started.is_set)
        assert coordinator._what_if_running is True
        # while the replay is busy, a live job (the planning that steers the charger) runs and finishes at once
        live = await asyncio.wait_for(coordinator.async_run_job("recalculate", include_forecast=True), 20)
        assert live.error is None and threads[0] != live.worker_thread
        assert threads[0].startswith(f"{coordinator.object_prefix}_what_if") and live.worker_thread.startswith(f"{coordinator.object_prefix}_worker")
        assert hass.states.get(SENSOR).attributes["attempt"]["status"] == "running"
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(SENSOR).attributes["attempt"]["status"] == "ok" and coordinator._what_if_running is False


async def test_the_progress_is_shown_while_it_runs_and_not_saved(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    _seed(coordinator)
    started, release = threading.Event(), threading.Event()

    def reporting_replay(data):
        data.on_window(2, 7)
        started.set()
        assert release.wait(30)
        return real_replay(data)

    with patch.object(type(coordinator), "_async_what_if_start_soc", return_value=START), patch(
        "custom_components.cable_juice_planner.coordinator.what_if.replay", reporting_replay
    ):
        await _press_without_waiting(hass)
        await _wait_for(hass, lambda: (hass.states.get(SENSOR).attributes["attempt"] or {}).get("progress") is not None)
        attempt = hass.states.get(SENSOR).attributes["attempt"]
        assert attempt["status"] == "running" and attempt["progress"] == {"done": 2, "total": 7}
        assert "progress" not in coordinator.engine.economy_db["what_if_attempt"]  # a figure that changes every few seconds is not stored
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert "progress" not in (hass.states.get(SENSOR).attributes["attempt"] or {})  # the finished attempt has none


async def test_the_stop_button_stops_the_calculation_and_keeps_the_saved_result(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    _seed(coordinator)
    with patch.object(type(coordinator), "_async_what_if_start_soc", return_value=START):
        await _press(hass)
    first = hass.states.get(SENSOR).attributes["result"]["computed_at"]
    started = threading.Event()

    def endless_replay(data):
        started.set()
        for index in range(100000):
            data.on_window(index, 100000)  # raises when the calculation is stopped
            time.sleep(0.005)

    with patch.object(type(coordinator), "_async_what_if_start_soc", return_value=START), patch(
        "custom_components.cable_juice_planner.coordinator.what_if.replay", endless_replay
    ):
        await _press_without_waiting(hass)
        await _wait_for(hass, started.is_set)
        coordinator._what_if_cancel.set()  # a stop request (what an unload makes)
        await hass.async_block_till_done(wait_background_tasks=True)
    attributes = hass.states.get(SENSOR).attributes
    assert attributes["attempt"]["status"] == "unavailable" and attributes["attempt"]["reason"] == "stopped"
    assert attributes["result"]["computed_at"] == first  # the previous result is untouched
    assert coordinator._what_if_running is False and not coordinator._what_if_cancel.is_set()  # ready for the next run
    with patch.object(type(coordinator), "_async_what_if_start_soc", return_value=START):
        await _press(hass)  # and a stop never carries over to the next run
    assert hass.states.get(SENSOR).attributes["attempt"]["status"] == "ok"


async def test_a_stop_requested_while_the_run_is_prepared_still_stops_it_before_the_replay_starts(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    _seed(coordinator)
    replayed: list[bool] = []

    async def start_level_and_stop(self, prepared):
        # the run is still being prepared (the recorder is read): the button is pressed now
        coordinator._what_if_cancel.set()  # a stop request (what an unload makes)
        return START

    with patch.object(type(coordinator), "_async_what_if_start_soc", start_level_and_stop), patch(
        "custom_components.cable_juice_planner.coordinator.what_if.replay", lambda data: replayed.append(True)
    ):
        await _press(hass)
    attempt = hass.states.get(SENSOR).attributes["attempt"]
    assert replayed == [] and attempt["status"] == "unavailable" and attempt["reason"] == "stopped"
    assert coordinator._what_if_running is False and not coordinator._what_if_cancel.is_set()


async def test_a_replay_that_only_looks_after_the_unload_has_finished_still_stops(hass: HomeAssistant, loaded_entry) -> None:
    """Unloading cancels the task that waits for the thread, which used to clear the stop flag: the thread then planned on."""
    coordinator = loaded_entry.runtime_data
    _seed(coordinator)
    started, unloaded = threading.Event(), threading.Event()
    outcome: list[str] = []

    def late_replay(data):
        started.set()
        assert unloaded.wait(30)  # no looking at the flag until the unload is completely done
        try:
            data.on_window(0, 10)
        except WhatIfCancelled:
            outcome.append("stopped")
            raise

    with patch.object(type(coordinator), "_async_what_if_start_soc", return_value=START), patch(
        "custom_components.cable_juice_planner.coordinator.what_if.replay", late_replay
    ):
        await _press_without_waiting(hass)
        await _wait_for(hass, started.is_set)
        await hass.config_entries.async_unload(loaded_entry.entry_id)
        await hass.async_block_till_done()
        unloaded.set()
        await hass.async_add_executor_job(coordinator._what_if_executor.shutdown, True)  # noqa: SLF001
    assert outcome == ["stopped"] and coordinator._what_if_cancel.is_set()


async def test_unloading_stops_a_running_calculation(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    _seed(coordinator)
    started = threading.Event()
    stopped = threading.Event()
    outcome: list[str] = []

    def endless_replay(data):
        started.set()
        try:
            for index in range(100000):
                data.on_window(index, 100000)
                time.sleep(0.005)
        except WhatIfCancelled:
            outcome.append("stopped")
            raise
        finally:
            stopped.set()

    with patch.object(type(coordinator), "_async_what_if_start_soc", return_value=START), patch(
        "custom_components.cable_juice_planner.coordinator.what_if.replay", endless_replay
    ):
        await _press_without_waiting(hass)
        await _wait_for(hass, started.is_set)
        await hass.config_entries.async_unload(loaded_entry.entry_id)
        await hass.async_add_executor_job(stopped.wait, 20)
    assert outcome == ["stopped"]  # the thread did not plan on after the unload
