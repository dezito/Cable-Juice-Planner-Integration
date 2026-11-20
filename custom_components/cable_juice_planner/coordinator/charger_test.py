"""The test of the user's own charger automation: running it step by step and the published result.

A switch starts the test (and stops it). One current after the other is published on the charger action sensor, the
charger's power sensor is read, and the result of every step is published as markdown on the charger_test sensor. The
runner is a background task; the engine holds the current (engine/control/charger_test.py), so a runner that dies can
not leave the charger at a forced current: the hold ends by itself. Nothing but the published action and amps is changed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.config import AUTOMATION_CONFIG_PATH
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from ..const import CHARGER_COMPLETED_STATUS, CHARGER_NOT_READY_STATUS, ENTITY_UNAVAILABLE_STATES
from ..engine.control.charger_test import (
    HOLD_MARGIN_SECONDS,
    MOVED_W,
    SKIP_REASONS,
    STOP_REASONS,
    STOPPED_W,
    build_charger_test_markdown,
    car_phases_of,
    judge_power,
    next_amps,
    overdraw_w,
    power_is_capped,
)
from ..management.automation_inputs import AMPS_INPUTS, START_INPUTS, STOP_INPUTS, automations_using_blueprint, has_steps

_LOGGER = logging.getLogger(__name__)

# seconds
ATTEMPT_TIMEOUT = 150  # the longest one current is given to show in the power
SETTLE_SECONDS = 20  # after the current was published, before the readings count
SAMPLE_SECONDS = 10  # between readings
READINGS_NEEDED = 3  # readings (30 s) that are averaged
PASSES_NEEDED = 2  # averages in a row that must agree
STALE_SECONDS = 180  # a power sensor that has not reported for this long is not trusted
OVERDRAW_AFTER_SECONDS = 60  # a slow charger may still be coming down from the step before until then
# a charger without a way to set the current only has to start: this many amps of power
STARTED_AMPS = 5


class CoordinatorChargerTest:
    """Run, stop and publish the test of the user's charger automation."""

    def _init_charger_test(self) -> None:
        # automation: "found" when the automation made from the blueprint was found in automations.yaml, else "unknown"
        self.charger_test_state: dict[str, Any] = {
            "status": "idle", "steps": [], "reason": None, "finished": None, "markdown": "", "automation": None, "amps_control": None,
            "car_phases": None,
        }
        self._charger_test_task: asyncio.Task | None = None

    @callback
    def async_restore_charger_test(self, attributes: dict[str, Any]) -> None:
        """The result of the last test, from before the restart. A test that was running did not finish."""
        steps, status = attributes.get("steps"), attributes.get("status")
        if status not in ("done", "stopped", "failed") or not isinstance(steps, list):
            return
        number = (int, float, type(None))
        if not all(
            isinstance(step, dict) and step.get("name") in ("max", "min", "single_max", "single_min", "stop")
            and step.get("status") in ("ok", "adjusted", "fail", "skip")
            and (step.get("reason") in SKIP_REASONS if step.get("status") == "skip" else step.get("reason") in (None, "overdraw"))
            and all(isinstance(step.get(key), number) for key in ("amps", "configured_amps", "watt", "expected_w", "phases", "judged_phases"))
            and isinstance(step.get("attempts") or [], list)
            and all(isinstance(attempt, dict) and isinstance(attempt.get("amps"), number) and isinstance(attempt.get("watt"), number)
                    for attempt in step.get("attempts") or [])
            for step in steps
        ):
            return
        car_phases, automation, amps_control = attributes.get("car_phases"), attributes.get("automation"), attributes.get("amps_control")
        reason = attributes.get("reason")
        self.charger_test_state = {
            "status": status, "steps": steps, "reason": reason if reason in STOP_REASONS else None, "finished": attributes.get("finished"),
            "markdown": "", "automation": automation if automation in ("found", "unknown") else None,
            "amps_control": amps_control if isinstance(amps_control, bool) else None,
            "car_phases": car_phases if isinstance(car_phases, int) and 0 < car_phases < 3 else None,
        }
        self.charger_test_state["markdown"] = build_charger_test_markdown(self.t, self.charger_test_state)

    @callback
    def async_run_charger_test(self) -> None:
        """Start the test. Nothing happens while one runs or before CJP has started."""
        if self.charger_test_state["status"] == "running" or not self.started or self.engine is None:
            return
        self._publish_charger_test(status="running", steps=[], reason=None, finished=None, automation=None, amps_control=None, car_phases=None)
        self._charger_test_task = task = self.entry.async_create_background_task(
            self.hass, self._async_charger_test(), name=f"{self.object_prefix}_charger_test"
        )
        task.add_done_callback(self._charger_test_ended)

    @callback
    def _charger_test_ended(self, _task: asyncio.Task) -> None:
        """A task that was stopped before it ran never gets to publish its end: the status must not stay on running."""
        if self.charger_test_state["status"] == "running":
            self._publish_charger_test(status="stopped", reason="stopped", finished=dt_util.now().isoformat(timespec="seconds"))

    @callback
    def async_stop_charger_test(self) -> None:
        """Stop the test: the step that runs ends, and the plan's own action and amps are published again."""
        if self._charger_test_task and not self._charger_test_task.done():
            self._charger_test_task.cancel()

    async def async_unload_charger_test(self) -> None:
        """Called when the entry is unloaded: stop the test and put the plan back before the worker is shut down."""
        task = self._charger_test_task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _publish_charger_test(self, **changes: Any) -> None:
        state = {**self.charger_test_state, **changes}
        state["markdown"] = build_charger_test_markdown(self.t, state)
        self.charger_test_state = state
        async_dispatcher_send(self.hass, self.signal, "charger_test")

    # ------------------------------------------------------------------ #
    # The runner
    # ------------------------------------------------------------------ #

    async def _async_charger_test(self) -> None:
        steps: list[dict[str, Any]] = []
        status, reason = "done", None
        stop_step = {"name": "stop", "phases": 0, "amps": 0, "expected_w": 0}
        planned: list[dict] = []  # every step the test would have run, for the ones that were not reached
        charging_at: tuple[int, int] | None = None  # (amps, phases) of the last current that worked
        try:
            begin = (await self.async_run_job("charger_test_begin")).value
            if not begin or not begin["ok"]:
                status, reason = "failed", (begin or {}).get("reason") or "internal_error"
                return
            found = await self.hass.async_add_executor_job(
                automations_using_blueprint, self.hass.config.path(AUTOMATION_CONFIG_PATH), f"sensor.{self.object_prefix}_charger_action"
            )
            # the current can be set when the automation has steps for it; without the automation in the file it is assumed
            # when the plan steps by amps (solar), and not otherwise
            amps_known = has_steps(found, *AMPS_INPUTS)
            amps_control = begin["solar_steps"] if amps_known is None else amps_known
            self._publish_charger_test(automation="found" if found else "unknown", amps_control=amps_control)
            plan = [step for step in begin["steps"] if amps_control or step["name"] == "max"]
            planned = [*plan, stop_step]
            # a car that already charges proves no start: the first step that charges says so
            before = self._charger_power(begin["power_entity"])
            already_charging = before is not None and before >= STARTED_AMPS * begin["voltage"]
            context: dict[str, Any] = {"car_phases": None}
            for step in plan:
                if has_steps(found, *START_INPUTS) is False:
                    steps.append(self._skipped(step, "no_steps"))
                    self._publish_charger_test(steps=list(steps))
                    continue
                record, stop = await self._async_charge_step(step, begin, amps_control, context)
                if already_charging and record["status"] in ("ok", "adjusted"):
                    record, already_charging = {**record, "status": "adjusted", "already_charging": True}, False
                steps.append(record)
                self._publish_charger_test(steps=list(steps), car_phases=context["car_phases"])
                if record["status"] in ("ok", "adjusted"):
                    charging_at = (record["amps"], record["phases"])
                if stop:
                    status, reason = "stopped", stop
                    break
            else:
                if has_steps(found, *STOP_INPUTS) is False:
                    steps.append(self._skipped(stop_step, "no_steps"))
                else:
                    record, stop = await self._async_stop_step(stop_step, begin, charging_at)
                    steps.append(record)
                    if stop:
                        status, reason = "stopped", stop
                self._publish_charger_test(steps=list(steps))
        except asyncio.CancelledError:
            status, reason = "stopped", "stopped"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("The charger test failed")
            status, reason = "failed", "internal_error"
        finally:
            await self._async_charger_test_release()
            if status == "stopped":  # the steps that were not run are shown, not left out
                steps.extend(self._skipped(step, "not_reached") for step in planned[len(steps):])
            self._publish_charger_test(status=status, steps=steps, reason=reason, finished=dt_util.now().isoformat(timespec="seconds"))

    async def _async_charger_test_release(self) -> None:
        """Put the plan's own action and amps back. Also run when the test is stopped or the entry is unloaded."""
        try:
            # with the forecast, like every planning run
            await asyncio.shield(self.async_run_job("charger_test_release", include_forecast=True))
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            _LOGGER.warning("The charger test could not put the plan back; it ends by itself", exc_info=True)

    @staticmethod
    def _skipped(step: dict, reason: str, **extra: Any) -> dict[str, Any]:
        return {
            "name": step["name"], "status": "skip", "reason": reason, "phases": step["phases"], "configured_amps": step["amps"],
            "amps": step["amps"], "expected_w": step.get("expected_w", 0), "attempts": [], **extra,
        }

    def _charger_status(self) -> str:
        entity_id = self.config["charger"]["entity_ids"]["status_entity_id"]
        state = self.hass.states.get(entity_id) if entity_id else None
        return state.state if state else "unavailable"

    def _charger_power(self, entity_id: str) -> float | None:
        """The power of the charger now in W; None when the sensor has no number or has not reported lately."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ENTITY_UNAVAILABLE_STATES:
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        if value != value or abs(value) == float("inf"):
            return None
        if (dt_util.utcnow() - state.last_reported).total_seconds() > STALE_SECONDS:
            return None
        return abs(value) * (1000.0 if state.attributes.get("unit_of_measurement") == "kW" else 1.0)

    def _ended_by_the_charger(self) -> str | None:
        """Why the test can not go on: the cable was pulled out or the car is full."""
        status = self._charger_status()
        if status in CHARGER_NOT_READY_STATUS:
            return "not_connected"
        if status in CHARGER_COMPLETED_STATUS:
            return "completed"
        return None

    async def _async_wait_for_power(self, entity_id: str, check, over_w: float | None = None) -> tuple[str, float | None, int]:
        """Read the power until check(mean) holds twice in a row, or the time is up.

        Returns ("ok" | "fail" | "no_data" | "ended" | "over", the last mean in W, seconds). The first SETTLE_SECONDS do not
        count, and a reading the sensor did not have (lost, stale) starts the averaging again. "over": every reading of the
        average above over_w after OVERDRAW_AFTER_SECONDS (the charger takes more than it was given, and it is not a charger
        that is still coming down from the step before)."""
        started = time.monotonic()
        readings: list[float] = []
        passes = 0
        last: float | None = None
        while time.monotonic() - started < ATTEMPT_TIMEOUT:
            await asyncio.sleep(SAMPLE_SECONDS)
            elapsed = time.monotonic() - started
            if self._ended_by_the_charger():
                return "ended", last, round(elapsed)
            value = self._charger_power(entity_id)
            if value is None:
                readings, passes, last = [], 0, None
                continue
            if elapsed < SETTLE_SECONDS:
                continue
            readings = [*readings, value][-READINGS_NEEDED:]
            if len(readings) < READINGS_NEEDED:
                continue
            last = sum(readings) / len(readings)
            if over_w is not None and elapsed >= OVERDRAW_AFTER_SECONDS and min(readings) > over_w:
                return "over", last, round(elapsed)
            passes = passes + 1 if check(last) else 0
            if passes >= PASSES_NEEDED:
                return "ok", last, round(elapsed)
        return ("no_data" if last is None else "fail"), last, round(time.monotonic() - started)

    async def _async_charge_step(
        self, step: dict, begin: dict, amps_control: bool, context: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        """One current: published, read, and (with a way to set the current) searched when it fails.

        The search goes two amps at a time towards the middle of the group, then one amp back towards the step (see
        next_amps), so the result is the current closest to the step that works. The maximum is the test that charging works
        at all: when it gives (almost) no power, the rest is not tried (the stop reason no_charging).
        * Power clearly above the published amps ends the test at once (overdraw): the load balancing counts on them.
        * A car that charges on fewer phases is found on the first attempt that matches them (context["car_phases"]), and
          from then on every step is judged on those phases.
        * Power that does not rise with the amps is the car's or the charger's limit (limited), not a setting to change."""
        voltage = begin["voltage"]
        amps, attempts, seconds = step["amps"], [], 0
        threshold = STARTED_AMPS * voltage
        record = {"name": step["name"], "reason": None, "phases": step["phases"], "configured_amps": step["amps"], "attempts": attempts}
        while True:
            held = (await self.async_run_job(
                "charger_test_hold", "charge", amps, step["phases"], ATTEMPT_TIMEOUT + HOLD_MARGIN_SECONDS, include_forecast=True
            )).value
            if held["status"] != "ok":
                return self._skipped(step, "not_ready", attempts=attempts), self._ended_by_the_charger() or "not_connected"
            if held["published"] != held["requested"]:
                # the load balancing gives this car less than the step asks for: never pushed past the fuse
                return self._skipped(step, "site_limited", attempts=attempts), None
            phases = min(step["phases"], context["car_phases"] or step["phases"])
            expected = amps * phases * voltage
            check = (lambda mean, e=expected: judge_power(e, mean)) if amps_control else (lambda mean: mean >= threshold)
            verdict, mean, taken = await self._async_wait_for_power(begin["power_entity"], check, overdraw_w(amps * step["phases"] * voltage))
            seconds += taken
            if verdict == "ended":
                return self._skipped(step, "not_ready", attempts=attempts), self._ended_by_the_charger()
            if verdict == "no_data":
                return self._skipped(step, "no_data", attempts=attempts), None
            if verdict == "over":
                attempts.append({"amps": amps, "watt": round(mean or 0), "ok": False})
                record.update(reason="overdraw", amps=amps, watt=round(mean or 0), expected_w=amps * step["phases"] * voltage, seconds=seconds)
                return {**record, "status": "fail"}, "overdraw"
            if (await self.async_run_job("charger_test_published")).value not in (None, held["requested"]):
                # the load balancing limited the car during the step: the power says nothing about the automation
                return self._skipped(step, "site_limited", attempts=attempts), None
            ok = verdict == "ok"
            if not ok and amps_control and context["car_phases"] is None and mean is not None and (found := car_phases_of(amps, phases, voltage, mean)):
                context["car_phases"] = phases = found
                ok = True
            attempts.append({"amps": amps, "watt": round(mean or 0), "ok": ok})
            working = [attempt for attempt in attempts if attempt["ok"]]
            # a maximum that gives (almost) no power does not charge at all: another current will not help
            dead = step["name"] == "max" and not working and (mean or 0) < threshold
            following = None if dead or not amps_control else next_amps(step, attempts)
            if following is not None:
                amps = following
                continue
            record["seconds"] = seconds
            if phases != step["phases"]:
                record["judged_phases"] = phases
            if working:
                best = (min if step["search"] > 0 else max)(working, key=lambda attempt: attempt["amps"])
                limited = amps_control and power_is_capped(step["search"], attempts, best, threshold)
                record.update(amps=best["amps"], watt=best["watt"], expected_w=best["amps"] * phases * voltage if amps_control else 0, limited=limited)
                return {**record, "status": "ok" if best["amps"] == step["amps"] else "adjusted"}, None
            if amps_control and power_is_capped(step["search"], attempts, None, threshold):
                lowest = min(attempts, key=lambda attempt: attempt["amps"])
                watt = round(sum(attempt["watt"] for attempt in attempts) / len(attempts))
                record.update(amps=lowest["amps"], watt=watt, expected_w=lowest["amps"] * phases * voltage, limited=True)
                return {**record, "status": "adjusted"}, None
            record.update(amps=step["amps"], watt=attempts[0]["watt"], expected_w=step["amps"] * phases * voltage if amps_control else 0)
            return {**record, "status": "fail"}, "no_charging" if dead else None

    async def _async_stop_step(self, step: dict, begin: dict, charging_at: tuple[int, int] | None) -> tuple[dict[str, Any], str | None]:
        """Stop: the charger must be charging just before, or standing still proves nothing.

        The last step may have ended on a current the charger did not take, so the last current that worked is published
        again and the stop is only tried when the power shows that the charger charges."""
        if charging_at is None:
            return self._skipped(step, "no_baseline"), None
        amps, phases = charging_at
        held = (await self.async_run_job("charger_test_hold", "charge", amps, phases, ATTEMPT_TIMEOUT + HOLD_MARGIN_SECONDS, include_forecast=True)).value
        if held["status"] != "ok":
            return self._skipped(step, "not_ready"), self._ended_by_the_charger() or "not_connected"
        if held["published"] != held["requested"]:
            return self._skipped(step, "site_limited"), None
        verdict, mean, taken = await self._async_wait_for_power(
            begin["power_entity"], lambda value: value >= MOVED_W, overdraw_w(amps * phases * begin["voltage"])
        )
        if verdict == "ended":
            return self._skipped(step, "not_ready"), self._ended_by_the_charger()
        if verdict == "over":
            record = {
                "name": "stop", "reason": "overdraw", "phases": phases, "configured_amps": 0, "amps": amps, "watt": round(mean or 0),
                "expected_w": amps * phases * begin["voltage"], "attempts": [], "seconds": taken,
            }
            return {**record, "status": "fail"}, "overdraw"
        if verdict != "ok":
            return self._skipped(step, "no_data" if verdict == "no_data" else "no_baseline"), None
        held = (await self.async_run_job("charger_test_hold", "stop", 0, 0, ATTEMPT_TIMEOUT + HOLD_MARGIN_SECONDS, include_forecast=True)).value
        if held["status"] != "ok":
            return self._skipped(step, "not_ready"), self._ended_by_the_charger() or "not_connected"
        verdict, mean, taken = await self._async_wait_for_power(begin["power_entity"], lambda value: value <= STOPPED_W)
        if verdict == "ended":
            return self._skipped(step, "not_ready"), self._ended_by_the_charger()
        if verdict == "no_data":
            return self._skipped(step, "no_data"), None
        record = {
            "name": "stop", "reason": None, "phases": 0, "configured_amps": 0, "amps": 0, "watt": round(mean or 0), "expected_w": 0,
            "attempts": [], "seconds": taken,
        }
        return {**record, "status": "ok" if verdict == "ok" else "fail"}, None
