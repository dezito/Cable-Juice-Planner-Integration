"""What-if Scenarios A, B and D: the buttons (read the starting battery level, then run the job)."""
from __future__ import annotations

import datetime
import logging
import math
import time
from functools import partial
from typing import Any

from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from ..const import ENTITY_UNAVAILABLE_STATES
from ..engine.whatif.replay import WhatIfCancelled, replay_segments
from ..utils.helpers import float_or_none

_LOGGER = logging.getLogger(__name__)

# seconds between two progress reports of a running calculation (each one is a job on the worker and an update of the sensor)
WHAT_IF_PROGRESS_INTERVAL = 10.0


class CoordinatorWhatIf:
    """Reads what the engine cannot: the battery level when the car was first plugged in."""

    _what_if_running = False
    _what_if_progress_pending = False

    async def async_run_what_if(self) -> None:
        """button run_what_if (Scenario A). One calculation at a time; the live plan and settings are never touched."""
        await self._async_run_what_if_jobs("what_if")

    async def async_run_what_if_settings(self) -> None:
        """button run_what_if_settings (Scenario B). Shares the one-at-a-time guard with Scenario A."""
        await self._async_run_what_if_jobs("what_if_settings")

    async def async_run_what_if_no_solar(self) -> None:
        """button run_what_if_no_solar (Scenario D). Shares the one-at-a-time guard with the other scenarios."""
        await self._async_run_what_if_jobs("what_if_no_solar")

    async def _async_run_what_if_jobs(self, name: str) -> None:
        """prepare -> read the starting level -> run, for the jobs "<name>_prepare", "<name>_run" and "<name>_fail"."""
        if self._what_if_running:
            return
        self._what_if_running = True
        self._what_if_cancel.clear()  # a stop request (an unload) is only ever for the run that was running when it was made
        try:
            prepared = await self.async_run_job(f"{name}_prepare")
            if prepared.error or not isinstance(prepared.value, dict):
                await self.async_run_job(f"{name}_fail", "internal_error")
                return
            start = await self._async_what_if_start_soc(prepared.value)
            # the run uses the same period and first connection the starting level was read for
            start = {**start, "period": prepared.value["period"], "first_window_start": prepared.value["first_window_start"]}
            if name == "what_if":
                # the level at the start and at the end of every stretch of connections without a gap: the end level balances the
                # energy of the three calculations (it is not a setting: never entered)
                levels = await self._async_what_if_levels(prepared.value, start)
                last = levels[-1] if levels else await self._async_what_if_end_soc(prepared.value)
                start = {
                    **start, "end_soc": last.get("end_soc", last.get("soc")), "end_source": last.get("end_source", last.get("source")),
                    "last_window_end": prepared.value["last_window_end"], "segments": prepared.value.get("segments") or [],
                }
                if levels:
                    start = {**start, "levels": levels, "soc": levels[0]["soc"], "source": levels[0]["source"]}
                await self._async_what_if_planned(start)
                return
            result = await self.async_run_job(f"{name}_run", start)
            if result.error:
                _LOGGER.warning("What-if could not be calculated: %s", result.error)
                await self.async_run_job(f"{name}_fail", "internal_error")
        except Exception:  # noqa: BLE001
            # never leave the attempt "running": the saved result stays and the reason is shown
            _LOGGER.exception("What-if failed")
            if not self._what_if_closed:
                await self.async_run_job(f"{name}_fail", "internal_error")
        finally:
            self._what_if_running = False
            if not self._what_if_closed:  # (unloading keeps it set: the thread may still be on its way to see it)
                self._what_if_cancel.clear()

    async def _async_what_if_stopped(self) -> None:
        """The run was stopped (by an unload: then the worker is gone and there is nothing to publish to)."""
        if not self._what_if_closed:
            await self.async_run_job("what_if_fail", "stopped")

    async def _async_what_if_planned(self, start: dict[str, Any]) -> None:
        """Scenario A: collect (a quick job), replay Smart and charging at once on the what-if thread, publish (a quick job).

        CJP itself is not replayed: its column is the record stored with each connection. The replay runs on its own thread,
        not the worker the charger control uses, and only on copies, so nothing live waits for it or is changed."""
        if self._what_if_stop_requested():  # unloaded while the period and the starting level were being prepared
            await self._async_what_if_stopped()
            return
        collected = await self.async_run_job("what_if_collect", start)
        if collected.error is not None or not isinstance(collected.value, dict):
            _LOGGER.warning("What-if could not be prepared: %s", collected.error)
            await self.async_run_job("what_if_fail", "internal_error")
            return
        prepared = collected.value
        result = prepared["result"]
        if result is None:
            if self._what_if_stop_requested():  # stopped (or unloaded) while the copies were being made
                await self._async_what_if_stopped()
                return
            try:
                result = await self.hass.loop.run_in_executor(
                    self._what_if_executor, partial(replay_segments, prepared["calculations"], self._what_if_window)
                )
            except WhatIfCancelled:
                await self._async_what_if_stopped()
                return
        finished = await self.async_run_job("what_if_finish", prepared, result)
        if finished.error is not None:
            _LOGGER.warning("What-if could not be published: %s", finished.error)
            await self.async_run_job("what_if_fail", "internal_error")

    def _what_if_stop_requested(self) -> bool:
        return self._what_if_cancel.is_set() or self._what_if_closed

    def _what_if_window(self, done: int, total: int) -> None:
        """Called in the what-if thread before each connection: stops when asked to (an unload), and reports the progress."""
        if self._what_if_stop_requested():
            raise WhatIfCancelled
        now = time.monotonic()
        if now - self._what_if_progress_at < WHAT_IF_PROGRESS_INTERVAL:
            return
        self._what_if_progress_at = now
        self.hass.loop.call_soon_threadsafe(self._what_if_report, done, total)

    @callback
    def _what_if_report(self, done: int, total: int) -> None:
        if self._what_if_progress_pending or not self._what_if_running:
            return
        self._what_if_progress_pending = True
        self._background(self._async_what_if_progress(done, total), "what_if_progress")

    async def _async_what_if_progress(self, done: int, total: int) -> None:
        try:
            if self._what_if_running and not self._what_if_closed:
                await self.async_run_job("what_if_progress", done, total)
        finally:
            self._what_if_progress_pending = False

    async def _async_what_if_end_soc(self, prepared: dict[str, Any]) -> dict[str, Any]:
        """The battery level when the last compared connection ended, read from the recorder like the starting level.

        Never defaulted: unknown means the three calculations are not balanced, and the result says so."""
        last = prepared.get("last_window_end")
        soc_id = self.config["ev_car"]["entity_ids"].get("usable_battery_level_entity_id") if self.ev_configured else None
        if last is None or not soc_id or "recorder" not in self.hass.config.components:
            return {"soc": None, "source": "unavailable"}
        try:
            return await self._async_read_soc(soc_id, float(last))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("What-if: could not read the battery level history at the last unplug: %s", err)
            return {"soc": None, "source": "unavailable"}

    async def _async_what_if_levels(self, prepared: dict[str, Any], first: dict[str, Any]) -> list[dict[str, Any]]:
        """The battery level at the start and at the end of every stretch of connections without a gap (a gap leaves the level
        after it unknown, so each stretch has its own). The first start level may be the one entered. Never defaulted."""
        levels = []
        for index, (segment_start, segment_end) in enumerate(prepared.get("segments") or []):
            begin = first if index == 0 else await self._async_what_if_start_soc({"first_window_start": segment_start})
            end = await self._async_what_if_end_soc({"last_window_end": segment_end})
            levels.append({"soc": begin["soc"], "source": begin["source"], "end_soc": end["soc"], "end_source": end["source"]})
        return levels

    async def _async_what_if_start_soc(self, prepared: dict[str, Any]) -> dict[str, Any]:
        """The battery level at the first replayed plug-in: entered by the user, else from the recorder.

        Never defaulted: without a value the replay calibrates its first connection and labels the result an estimate.
        """
        manual = prepared.get("manual_start_soc")
        if manual is not None:
            return {"soc": float(manual), "source": "entered"}
        first = prepared.get("first_window_start")
        soc_id = self.config["ev_car"]["entity_ids"].get("usable_battery_level_entity_id") if self.ev_configured else None
        if first is None or not soc_id or "recorder" not in self.hass.config.components:
            return {"soc": None, "source": "unavailable"}
        try:
            return await self._async_read_soc(soc_id, float(first))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("What-if: could not read the battery level history: %s", err)
            return {"soc": None, "source": "unavailable"}

    async def _async_read_soc(self, soc_id: str, ts: float) -> dict[str, Any]:
        from homeassistant.components.recorder import get_instance, history  # noqa: PLC0415
        from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

        instance = get_instance(self.hass)
        moment = dt_util.utc_from_timestamp(ts)
        # 1. the recorded state at the plug-in itself (only while the recorder still keeps raw history)
        states = await instance.async_add_executor_job(
            partial(
                history.get_significant_states, self.hass, moment, moment + datetime.timedelta(seconds=30), [soc_id], None,
                include_start_time_state=True, significant_changes_only=False, minimal_response=False, no_attributes=True,
            )
        )
        for state in states.get(soc_id, []):
            value = float_or_none(state.state) if state.state not in ENTITY_UNAVAILABLE_STATES else None
            if value is not None and math.isfinite(value) and 0.0 <= value <= 100.0:
                return {"soc": value, "source": "recorded_state"}
        # 2. the hourly long-term statistic: the mean of the hour of the plug-in (an approximation, labelled)
        hour = moment.replace(minute=0, second=0, microsecond=0)
        stats = await instance.async_add_executor_job(
            statistics_during_period, self.hass, hour, hour + datetime.timedelta(hours=1), {soc_id}, "hour", None, {"mean"}
        )
        for row in stats.get(soc_id, []):
            value = row.get("mean")
            if isinstance(value, (int, float)) and math.isfinite(value) and 0.0 <= value <= 100.0:
                return {"soc": float(value), "source": "statistics_hour_mean"}
        return {"soc": None, "source": "unavailable"}
