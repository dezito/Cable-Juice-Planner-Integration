"""The restart of a charger that does not start charging when the plan says charge (is_charging).

The integration only publishes the action: the restart is a stop at 0 A (or a disable with an enabled entity) kept for a
cron interval, and the plan's charge after that, so the user's automation sees both. The tests run on the real clock and
let time pass by moving the moments the restart remembers back (_pass).
"""
from __future__ import annotations

import datetime
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.cable_juice_planner.engine.control.schedule import CHARGER_MAX_RESTARTS, ControlSchedule

from .conftest import CHARGER_POWER, CHARGER_STATUS, MOCK_OPTIONS, MOCK_OPTIONS_NO_EV, async_setup, async_unload_and_join, make_entry, set_source_states

ACTION = "sensor.cjp_test_charger_action"
ENABLED = "switch.charger_enabled"
INTERVAL = 5  # the default cron interval
MOMENTS = ("_charger_restart_enabled_seen_at", "_charger_restart_waiting_since", "_charger_test_released_at", "_charger_restart_marked_at", "_charger_restart_stopped_at", "_charger_restart_started_at")


async def _entry(hass: HomeAssistant, options: dict | None = None):
    set_source_states(hass, charger_status="awaiting_start")  # plugged in, the plan says charge, the car does not
    entry = make_entry(options=options)
    await async_setup(hass, entry)
    # a reading in the history (no recorder in the tests): without an average the power can't tell, and there is no restart.
    # A minute old: the snapshot's now is whole seconds, a reading from this second would come after it
    entry.runtime_data._buffers.setdefault(CHARGER_POWER, []).insert(0, (time.time() - 60, "0"))
    await entry.runtime_data.async_run_job("cron", include_forecast=True)  # first seen waiting: the grace counts from here
    return entry


@pytest.fixture
async def waiting_entry(hass: HomeAssistant):
    entry = await _entry(hass)
    yield entry
    if entry.state is ConfigEntryState.LOADED:
        await async_unload_and_join(hass, entry)


@pytest.fixture
async def enabled_entry(hass: HomeAssistant):
    hass.states.async_set(ENABLED, "on")
    entry = await _entry(hass, {**MOCK_OPTIONS, "enabled_entity_id": ENABLED})
    yield entry
    if entry.state is ConfigEntryState.LOADED:
        await async_unload_and_join(hass, entry)


@pytest.fixture
def notified(waiting_entry, monkeypatch) -> list[dict]:
    calls: list[dict] = []
    engine = waiting_entry.runtime_data.engine
    monkeypatch.setattr(engine, "my_notify", lambda message="", title="", data=None, **kw: calls.append({"message": message, "data": data}))
    return calls


def _published(hass: HomeAssistant) -> tuple:
    state = hass.states.get(ACTION)
    return state.state, state.attributes["charger_enabled"], tuple(state.attributes[f"phase_{i}_amps"] for i in (1, 2, 3))


def _pass(engine, minutes: float, keep_row: bool = True) -> None:
    """Let minutes pass: the session's start (and its row in the history) and the moments of the restart move back."""
    delta = datetime.timedelta(minutes=minutes)
    session = engine.current_charging_session
    if session["start"] is not None:
        row = engine.charging_history_db.pop(session["start"], None)
        session["start"] -= delta
        if keep_row and row is not None:
            engine.charging_history_db[session["start"]] = row
    for name in MOMENTS:
        if getattr(engine, name) is not None:
            setattr(engine, name, getattr(engine, name) - delta)


async def _cron(coordinator) -> None:
    await coordinator.async_run_job("cron", include_forecast=True)


async def _restart(coordinator, after: float = 2 * INTERVAL + 1) -> None:
    """From a charge the charger does not start on: past the grace, marked, stopped, started again."""
    engine = coordinator.engine
    count = engine.restarting_charger_count
    _pass(engine, after)
    await _cron(coordinator)
    assert engine.charging_is_beginning, "not marked after the grace"
    _pass(engine, INTERVAL)
    await _cron(coordinator)
    assert engine.restarting_charger and engine.restarting_charger_count == count + 1
    _pass(engine, INTERVAL)
    await _cron(coordinator)
    assert not engine.restarting_charger


async def test_a_charger_that_does_not_start_is_stopped_for_an_interval_and_started_again(hass, waiting_entry, notified) -> None:
    coordinator = waiting_entry.runtime_data
    engine = coordinator.engine
    assert _published(hass) == ("charge", True, (16, 16, 16))
    _pass(engine, 60)
    await _cron(coordinator)  # marked: it gets one more cron interval
    assert engine.charging_is_beginning and engine.restarting_charger_count == 0
    await _cron(coordinator)  # another run within the interval (e.g. a setting changed) does not stop it yet
    assert engine.restarting_charger_count == 0 and _published(hass)[0] == "charge"
    _pass(engine, INTERVAL)
    await _cron(coordinator)  # the restart: a stop at 0 A, like the plan's stop
    assert engine.restarting_charger and engine.restarting_charger_count == 1
    assert _published(hass) == ("stop", True, (0, 0, 0))
    assert engine.last_charging_rule_text == engine._charger_restart_rule != "", "the reason shown is not the restart's"
    assert len(notified) == 1 and not notified[0]["data"]
    await coordinator.async_run_job("recalculate", include_forecast=True)  # a calculation in between does not undo it
    await _cron(coordinator)  # and neither does a run within the interval
    assert _published(hass) == ("stop", True, (0, 0, 0))
    _pass(engine, INTERVAL)
    await _cron(coordinator)  # started again by the plan
    assert not engine.restarting_charger and _published(hass) == ("charge", True, (16, 16, 16))
    hass.states.async_set(CHARGER_STATUS, "charging")
    hass.states.async_set(CHARGER_POWER, "11000", {"unit_of_measurement": "W", "device_class": "power"})
    await _cron(coordinator)
    assert engine.restarting_charger_count == 0 and not engine.charging_is_beginning
    assert len(notified) == 2  # the restart, and charging again


async def test_after_a_restart_the_charger_gets_the_grace_again(hass, waiting_entry) -> None:
    """With a short cron interval a charger that is slow to report charging is not stopped again right away."""
    coordinator = waiting_entry.runtime_data
    engine = coordinator.engine
    await _restart(coordinator, after=60)
    for _ in range(2):  # 5 and 10 minutes after the start: within two intervals
        _pass(engine, INTERVAL)
        await _cron(coordinator)
        assert not engine.charging_is_beginning and not engine.restarting_charger
    _pass(engine, INTERVAL)
    await _cron(coordinator)
    assert engine.charging_is_beginning


async def test_it_gives_up_after_the_last_restart_also_across_the_end_of_the_hour(hass, waiting_entry, notified) -> None:
    coordinator = waiting_entry.runtime_data
    engine = coordinator.engine
    for attempt in range(CHARGER_MAX_RESTARTS):
        await _restart(coordinator, after=60 if attempt == 0 else 2 * INTERVAL + 1)
    _pass(engine, 2 * INTERVAL + 1)
    await _cron(coordinator)
    _pass(engine, INTERVAL)
    await _cron(coordinator)  # no fourth stop: it gives up
    assert engine._charger_restart_gave_up and engine.restarting_charger_count == CHARGER_MAX_RESTARTS
    assert _published(hass)[0] == "charge"
    alarms = [n for n in notified if n["data"]]
    assert len(alarms) == 1 and alarms[0]["data"]["channel"] == "alarm_stream"
    assert len(notified) == CHARGER_MAX_RESTARTS + 1
    await coordinator.async_run_job("hour_end", include_forecast=True)  # the session ends every hour, the plan goes on
    for _ in range(4):
        _pass(engine, 60)
        await _cron(coordinator)
        assert engine._charger_restart_gave_up and not engine.restarting_charger
        assert _published(hass)[0] == "charge"
        # why nothing happens, and the fail-safe, also in the plan's later charges; not "restarting the charger"
        assert engine.last_charging_rule_text.startswith(f"{engine._charger_restart_rule}\n")
        assert "gave up" in engine._charger_restart_rule.lower() or "giver op" in engine._charger_restart_rule.lower()
    assert len(notified) == CHARGER_MAX_RESTARTS + 1
    hass.states.async_set(CHARGER_STATUS, "disconnected")  # unplugged: it starts over
    await _cron(coordinator)
    assert not engine._charger_restart_gave_up and engine.restarting_charger_count == 0


async def test_a_give_up_is_handed_to_the_health_checks_once(hass, waiting_entry, notified) -> None:
    """The give-up is one charger event for the repair of the health checks (engine JobResult -> coordinator -> watcher)."""
    coordinator = waiting_entry.runtime_data
    engine = coordinator.engine
    noted: list[str] = []
    coordinator.health_watcher.record_charger_problem = noted.append
    for attempt in range(CHARGER_MAX_RESTARTS):
        await _restart(coordinator, after=60 if attempt == 0 else 2 * INTERVAL + 1)
    _pass(engine, 2 * INTERVAL + 1)
    await _cron(coordinator)
    _pass(engine, INTERVAL)
    await _cron(coordinator)  # gives up
    assert engine._charger_restart_gave_up and noted == ["gave_up"]
    _pass(engine, 60)
    await _cron(coordinator)  # the fail-safe goes on: not a new event
    assert noted == ["gave_up"]


async def test_a_failing_health_watcher_does_not_stop_the_alarm_of_the_give_up(hass, waiting_entry, notified) -> None:
    """The events go to the health watcher before the job's notifications: an error there must not drop the alarm."""
    coordinator = waiting_entry.runtime_data
    engine = coordinator.engine

    def broken(kind: str) -> None:
        raise RuntimeError("the repair store is broken")

    coordinator.health_watcher.record_charger_problem = broken
    for attempt in range(CHARGER_MAX_RESTARTS):
        await _restart(coordinator, after=60 if attempt == 0 else 2 * INTERVAL + 1)
    _pass(engine, 2 * INTERVAL + 1)
    await _cron(coordinator)
    _pass(engine, INTERVAL)
    await _cron(coordinator)  # gives up
    assert engine._charger_restart_gave_up
    alarms = [n for n in notified if n["data"]]
    assert len(alarms) == 1 and alarms[0]["data"]["channel"] == "alarm_stream"


async def test_a_charger_with_an_enabled_entity_is_disabled_for_an_interval(hass, enabled_entry) -> None:
    coordinator = enabled_entry.runtime_data
    engine = coordinator.engine
    _pass(engine, 60)
    await _cron(coordinator)
    _pass(engine, INTERVAL)
    await _cron(coordinator)
    assert engine.restarting_charger_count == 1
    assert _published(hass) == ("charge", False, (16, 16, 16))  # disabled, the action and the amps are the plan's
    hass.states.async_set(ENABLED, "off")  # the automation disabled it
    await coordinator.async_run_job("recalculate", include_forecast=True)  # ready_to_charge does not enable it again
    await _cron(coordinator)
    assert _published(hass) == ("charge", False, (16, 16, 16))
    _pass(engine, INTERVAL)
    await _cron(coordinator)
    assert _published(hass) == ("charge", True, (16, 16, 16))


async def test_a_status_stuck_at_ready_does_not_stop_a_charging_car(hass, waiting_entry) -> None:
    coordinator = waiting_entry.runtime_data
    engine = coordinator.engine
    hass.states.async_set(CHARGER_POWER, "11000", {"unit_of_measurement": "W", "device_class": "power"})
    _pass(engine, 60)
    for _ in range(4):
        await _cron(coordinator)
        _pass(engine, INTERVAL)
    assert not engine.charging_is_beginning and engine.restarting_charger_count == 0
    assert _published(hass)[0] == "charge"
    await _cron(coordinator)  # charging: the grace counts again from when it stops
    assert engine._charger_restart_waiting_since is None


async def test_no_restart_when_the_power_cant_be_read(hass, waiting_entry) -> None:
    coordinator = waiting_entry.runtime_data
    engine = coordinator.engine
    hass.states.async_set(CHARGER_POWER, "unavailable")
    _pass(engine, 60)
    for _ in range(4):
        await _cron(coordinator)
        _pass(engine, INTERVAL)
    assert not engine.charging_is_beginning and engine.restarting_charger_count == 0


async def test_no_restart_within_two_cron_intervals_of_the_session_start(hass, waiting_entry) -> None:
    coordinator = waiting_entry.runtime_data
    engine = coordinator.engine
    _pass(engine, 2 * INTERVAL - 1)
    for _ in range(3):
        await _cron(coordinator)
    assert not engine.charging_is_beginning and engine.restarting_charger_count == 0
    assert _published(hass)[0] == "charge"


async def test_no_restart_right_after_the_test_of_the_charger_automation(hass, waiting_entry) -> None:
    """The test may end on a stop: the charger gets the same time to start again as at a new session."""
    coordinator = waiting_entry.runtime_data
    engine = coordinator.engine
    _pass(engine, 60)
    await _cron(coordinator)
    assert engine.charging_is_beginning  # marked before the test
    await coordinator.async_run_job("charger_test_hold", "stop", 0, 0, 600)
    await coordinator.async_run_job("charger_test_release", include_forecast=True)
    assert not engine.charging_is_beginning and engine._charger_test_released_at is not None
    for _ in range(3):
        await _cron(coordinator)
    assert not engine.charging_is_beginning and engine.restarting_charger_count == 0
    assert _published(hass)[0] == "charge"
    _pass(engine, 2 * INTERVAL + 1)  # the grace is over: the check comes back
    await _cron(coordinator)
    assert engine.charging_is_beginning


async def test_a_test_during_a_restart_ends_the_restart(hass, waiting_entry) -> None:
    coordinator = waiting_entry.runtime_data
    engine = coordinator.engine
    _pass(engine, 60)
    await _cron(coordinator)
    _pass(engine, INTERVAL)
    await _cron(coordinator)
    assert engine.restarting_charger and _published(hass)[0] == "stop"
    await coordinator.async_run_job("charger_test_hold", "charge", 6, 3, 600)
    assert not engine.restarting_charger and engine.restarting_charger_count == 1  # a test does not reset the restarts made
    await coordinator.async_run_job("charger_test_release", include_forecast=True)
    assert _published(hass) == ("charge", True, (16, 16, 16))  # the plan's charge, no stop left from the restart
    await _cron(coordinator)
    assert not engine.charging_is_beginning and not engine.restarting_charger


async def test_a_session_without_its_row_in_the_history_is_restarted_without_failing(hass, waiting_entry) -> None:
    coordinator = waiting_entry.runtime_data
    engine = coordinator.engine
    _pass(engine, 60, keep_row=False)
    assert engine.charging_history_db.get(engine.current_charging_session["start"]) is None
    await _restart(coordinator, after=0)
    _pass(engine, 2 * INTERVAL + 1, keep_row=False)
    await _cron(coordinator)
    _pass(engine, INTERVAL, keep_row=False)
    await _cron(coordinator)  # the second restart marks the history row
    assert engine.restarting_charger_count == 2 and _published(hass)[0] == "stop"
    hass.states.async_set(CHARGER_STATUS, "charging")
    hass.states.async_set(CHARGER_POWER, "11000", {"unit_of_measurement": "W", "device_class": "power"})
    await _cron(coordinator)  # "charging again" also reads the row
    assert engine.restarting_charger_count == 0


def test_the_minutes_since_a_moment_count_the_real_time_across_a_change_of_the_clock() -> None:
    tz = ZoneInfo("Europe/Copenhagen")

    def since(now: datetime.datetime, moment, zone=tz) -> float | None:
        fake = SimpleNamespace(now=lambda: now, _snap=SimpleNamespace(tz=zone) if zone else None)
        return ControlSchedule._minutes_since(fake, moment)

    # the clock jumps from 02:00 to 03:00: 01:50 to 03:10 is 20 minutes, not 80
    assert since(datetime.datetime(2026, 3, 29, 3, 10), datetime.datetime(2026, 3, 29, 1, 50)) == 20
    # and back from 03:00 to 02:00: 02:50 (summer time) to 02:10 (winter time) is 20 minutes
    assert since(datetime.datetime(2026, 10, 25, 2, 10, fold=1), datetime.datetime(2026, 10, 25, 2, 50)) == 20
    assert since(datetime.datetime(2026, 6, 1, 12, 0), datetime.datetime(2026, 6, 1, 11, 30)) == 30
    assert since(datetime.datetime(2026, 6, 1, 12, 0), datetime.datetime(2026, 6, 1, 11, 30), zone=None) == 30
    assert since(datetime.datetime(2026, 6, 1, 12, 0), "not a time") is None


async def test_unplugging_between_two_crons_ends_a_give_up(hass, waiting_entry, notified) -> None:
    coordinator = waiting_entry.runtime_data
    engine = coordinator.engine
    engine.restarting_charger_count, engine._charger_restart_gave_up = CHARGER_MAX_RESTARTS, True
    # the status job (the plug-in flow after it waits for the car), unplugged and in again before the next cron
    await coordinator.async_run_job("charger_status_changed", "disconnected", "awaiting_start")
    await coordinator.async_run_job("charger_status_changed", "awaiting_start", "disconnected")
    assert not engine._charger_restart_gave_up and engine.restarting_charger_count == 0


async def test_charging_status_without_power_keeps_the_restarts_made(hass, waiting_entry) -> None:
    coordinator = waiting_entry.runtime_data
    engine = coordinator.engine
    await _restart(coordinator, after=60)
    hass.states.async_set(CHARGER_STATUS, "charging")  # the status says charging, the power says no
    await _cron(coordinator)
    assert engine.restarting_charger_count == 1
    hass.states.async_set(CHARGER_POWER, "11000", {"unit_of_measurement": "W", "device_class": "power"})
    await _cron(coordinator)  # now it charges
    assert engine.restarting_charger_count == 0


async def test_a_disabled_charger_that_reports_off_is_not_an_unplug(hass, enabled_entry, monkeypatch) -> None:
    """The restart disables the charger, and some report off (or disconnected) then: no unplug and plug-in, which would end
    the economy window and turn the manual and forced charging off."""
    coordinator = enabled_entry.runtime_data
    engine = coordinator.engine
    calls: list[str] = []
    monkeypatch.setattr(engine, "economy_unplugged", lambda: calls.append("unplugged"))
    monkeypatch.setattr(engine, "economy_plugged_in", lambda: calls.append("plugged_in"))
    switches = {key: engine.switch_state(key) for key in ("allow_manual_charging_now", "allow_manual_charging_solar", "forced_charging_daily_battery_level")}
    _pass(engine, 60)
    await _cron(coordinator)
    _pass(engine, INTERVAL)
    await _cron(coordinator)
    assert engine.restarting_charger and _published(hass)[1] is False
    for value, old in (("off", "awaiting_start"), ("awaiting_start", "off")):
        assert (await coordinator.async_run_job("charger_status_changed", value, old)).value is None
    assert calls == [] and engine.restarting_charger_count == 1
    assert {key: engine.switch_state(key) for key in switches} == switches
    hass.states.async_set(CHARGER_STATUS, "off")  # still off at the next cron, within the pause: no unplug either
    await _cron(coordinator)
    assert engine.restarting_charger_count == 1


async def test_an_unplug_while_the_restart_has_the_charger_disabled_is_handled_when_it_has_settled(hass, enabled_entry, monkeypatch, caplog) -> None:
    coordinator = enabled_entry.runtime_data
    engine = coordinator.engine
    calls: list[str] = []
    monkeypatch.setattr(engine, "economy_unplugged", lambda: calls.append("unplugged"))
    _pass(engine, 60)
    await _cron(coordinator)
    _pass(engine, INTERVAL)
    await _cron(coordinator)
    assert engine.restarting_charger
    hass.states.async_set(CHARGER_STATUS, "disconnected")  # the car leaves during the pause
    await coordinator.async_run_job("charger_status_changed", "disconnected", "awaiting_start")
    assert calls == [] and engine._charger_restart_unplug_pending
    _pass(engine, INTERVAL)
    await _cron(coordinator)  # enabled again
    _pass(engine, INTERVAL)
    await _cron(coordinator)  # the switch seen on: a status read from a cloud may come late, not yet
    _pass(engine, INTERVAL)
    await _cron(coordinator)
    assert calls == []
    _pass(engine, INTERVAL)
    await _cron(coordinator)  # settled (10 minutes, the switch on) and still disconnected: it was unplugged
    assert calls == ["unplugged"] and not engine._charger_restart_unplug_pending
    assert engine.restarting_charger_count == 0
    assert _published(hass)[1] is True  # enabled again (the action is the plan's: the fixture charges with CJP deactivated)
    assert "job cron failed" not in caplog.text


async def test_a_disabled_charger_that_reports_off_keeps_the_battery_level_without_a_car_integration(hass) -> None:
    """Without a car integration an unplug sets the battery level to 0 % (it is asked for at the plug-in): not for a charger
    the restart disabled, there is no plug-in that would ask."""
    hass.states.async_set(ENABLED, "on")
    entry = await _entry(hass, {**MOCK_OPTIONS_NO_EV, "enabled_entity_id": ENABLED})
    try:
        coordinator = entry.runtime_data
        engine = coordinator.engine
        await coordinator.async_run_job("set_setting", "battery_level", 40.0)
        assert float(coordinator.settings.get("battery_level")) == 40.0
        _pass(engine, 60)
        await _cron(coordinator)
        _pass(engine, INTERVAL)
        await _cron(coordinator)
        if not engine.restarting_charger:
            pytest.skip("the plan does not charge without a car integration here")
        hass.states.async_set(CHARGER_STATUS, "off")
        await coordinator.async_run_job("charger_status_changed", "off", "awaiting_start")
        # what the plan and the status job ask (the fixture's plan runs with CJP deactivated and doesn't)
        await coordinator.async_run_job("ready_to_charge")
        assert float(coordinator.settings.get("battery_level")) == 40.0
    finally:
        await async_unload_and_join(hass, entry)


async def test_a_charger_slow_to_report_ready_after_the_restart_is_not_unplugged(hass, enabled_entry, monkeypatch) -> None:
    """A charger whose switch and status come back late: no unplug and plug-in, the restarts made are kept (else every
    round starts the count over and it never gives up)."""
    coordinator = enabled_entry.runtime_data
    engine = coordinator.engine
    calls: list[str] = []
    monkeypatch.setattr(engine, "economy_unplugged", lambda: calls.append("unplugged"))
    monkeypatch.setattr(engine, "economy_plugged_in", lambda: calls.append("plugged_in"))
    _pass(engine, 60)
    await _cron(coordinator)
    _pass(engine, INTERVAL)
    await _cron(coordinator)
    assert engine.restarting_charger
    hass.states.async_set(ENABLED, "off")
    hass.states.async_set(CHARGER_STATUS, "off")
    await coordinator.async_run_job("charger_status_changed", "off", "awaiting_start")
    for _ in range(4):  # enabled again by CJP, but the switch and the status stay off for 20 minutes
        _pass(engine, INTERVAL)
        await _cron(coordinator)
    assert calls == [] and engine.restarting_charger_count == 1
    hass.states.async_set(ENABLED, "on")
    hass.states.async_set(CHARGER_STATUS, "awaiting_start")
    await coordinator.async_run_job("charger_status_changed", "awaiting_start", "off")
    assert calls == [] and engine.restarting_charger_count == 1
    assert _published(hass)[1] is True


async def test_an_unplug_and_plug_in_during_the_pause_without_an_enabled_entity_starts_over(hass, waiting_entry) -> None:
    coordinator = waiting_entry.runtime_data
    engine = coordinator.engine
    _pass(engine, 60)
    await _cron(coordinator)
    _pass(engine, INTERVAL)
    await _cron(coordinator)
    assert engine.restarting_charger and engine.restarting_charger_count == 1
    await coordinator.async_run_job("charger_status_changed", "disconnected", "awaiting_start")
    await coordinator.async_run_job("charger_status_changed", "awaiting_start", "disconnected")
    assert engine.restarting_charger_count == 0 and not engine.restarting_charger


async def test_after_giving_up_it_charges_at_full_power_as_a_fail_safe_until_it_charges(hass, waiting_entry, monkeypatch) -> None:
    coordinator = waiting_entry.runtime_data
    engine = coordinator.engine
    for attempt in range(CHARGER_MAX_RESTARTS):
        await _restart(coordinator, after=60 if attempt == 0 else 2 * INTERVAL + 1)
    _pass(engine, 2 * INTERVAL + 1)
    await _cron(coordinator)
    _pass(engine, INTERVAL)
    await _cron(coordinator)
    assert engine._charger_restart_gave_up
    # a plan that doesn't charge now (no charge hour): the fail-safe charges anyway
    monkeypatch.setattr(engine, "no_charging_modes_active", lambda: False)
    await coordinator.async_run_job("recalculate", include_forecast=True)
    assert _published(hass) == ("charge", True, (16, 16, 16))
    fail_safe = engine.t("ui.is_charging.fail_safe_max", watt=int(48 * engine.config["charger"]["power_voltage"]))
    assert engine.last_charging_rule_text.endswith(fail_safe)
    await coordinator.async_run_job("hour_end", include_forecast=True)
    _pass(engine, 60)
    await _cron(coordinator)  # also after the hour shift
    assert _published(hass)[0] == "charge" and engine._charger_restart_gave_up
    hass.states.async_set(CHARGER_STATUS, "charging")
    hass.states.async_set(CHARGER_POWER, "11000", {"unit_of_measurement": "W", "device_class": "power"})
    await _cron(coordinator)  # it charges: the plan decides again, and it doesn't charge now
    assert not engine._charger_restart_gave_up
    assert _published(hass)[0] == "stop"


async def test_no_fail_safe_when_unplugged_after_giving_up(hass, waiting_entry, monkeypatch) -> None:
    coordinator = waiting_entry.runtime_data
    engine = coordinator.engine
    monkeypatch.setattr(engine, "no_charging_modes_active", lambda: False)
    engine.restarting_charger_count, engine._charger_restart_gave_up = CHARGER_MAX_RESTARTS, True
    hass.states.async_set(CHARGER_STATUS, "disconnected")
    await _cron(coordinator)
    assert not engine._charger_restart_gave_up and _published(hass)[0] == "stop"


async def _give_up(coordinator) -> None:
    engine = coordinator.engine
    for attempt in range(CHARGER_MAX_RESTARTS):
        await _restart(coordinator, after=60 if attempt == 0 else 2 * INTERVAL + 1)
    _pass(engine, 2 * INTERVAL + 1)
    await _cron(coordinator)
    _pass(engine, INTERVAL)
    await _cron(coordinator)
    assert engine._charger_restart_gave_up


async def test_power_that_shows_charging_ends_the_fail_safe_with_a_status_that_stays_ready(hass, waiting_entry, monkeypatch) -> None:
    """A status entity that only says on/connected, or hangs at ready: the power ends the fail-safe, else it charges at full
    power in every hour."""
    coordinator = waiting_entry.runtime_data
    engine = coordinator.engine
    await _give_up(coordinator)
    monkeypatch.setattr(engine, "no_charging_modes_active", lambda: False)
    hass.states.async_set(CHARGER_POWER, "11000", {"unit_of_measurement": "W", "device_class": "power"})
    await _cron(coordinator)  # the status still awaiting_start
    assert not engine._charger_restart_gave_up and engine.restarting_charger_count == 0
    assert _published(hass)[0] == "stop"  # the plan decides again


async def test_the_fail_safe_keeps_one_charging_session(hass, waiting_entry, monkeypatch) -> None:
    coordinator = waiting_entry.runtime_data
    engine = coordinator.engine
    await _give_up(coordinator)
    monkeypatch.setattr(engine, "no_charging_modes_active", lambda: False)  # a plan that doesn't charge now
    await coordinator.async_run_job("recalculate", include_forecast=True)
    start = engine.current_charging_session["start"]
    assert start is not None
    for _ in range(3):
        await _cron(coordinator)
        assert engine.current_charging_session["start"] == start  # not stopped and started again every run
    assert _published(hass)[0] == "charge" and engine.charging_no_rule_count == 0
