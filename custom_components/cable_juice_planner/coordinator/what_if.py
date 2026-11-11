"""What-if Scenario A: button run_what_if (reads the starting battery level, then runs the job)."""
from __future__ import annotations

import datetime
import logging
import math
from functools import partial
from typing import Any

from homeassistant.util import dt as dt_util

from ..const import ENTITY_UNAVAILABLE_STATES
from ..utils.helpers import float_or_none

_LOGGER = logging.getLogger(__name__)


class CoordinatorWhatIf:
    """Reads what the engine cannot: the battery level when the car was first plugged in."""

    _what_if_running = False

    async def async_run_what_if(self) -> None:
        """button run_what_if. One calculation at a time; the live plan and settings are never touched."""
        if self._what_if_running:
            return
        self._what_if_running = True
        try:
            prepared = await self.async_run_job("what_if_prepare")
            if prepared.error or not isinstance(prepared.value, dict):
                await self.async_run_job("what_if_fail", "internal_error")
                return
            start = await self._async_what_if_start_soc(prepared.value)
            # the run uses the same period and first connection the starting level was read for
            start = {**start, "period": prepared.value["period"], "first_window_start": prepared.value["first_window_start"]}
            result = await self.async_run_job("what_if_run", start)
            if result.error:
                _LOGGER.warning("What-if could not be calculated: %s", result.error)
                await self.async_run_job("what_if_fail", "internal_error")
        except Exception:  # noqa: BLE001
            # never leave the attempt "running": the saved result stays and the reason is shown
            _LOGGER.exception("What-if failed")
            await self.async_run_job("what_if_fail", "internal_error")
        finally:
            self._what_if_running = False

    async def _async_what_if_start_soc(self, prepared: dict[str, Any]) -> dict[str, Any]:
        """The battery level at the first replayed plug-in: entered by the user, else from the recorder.

        Never defaulted: without a value the engine reports "no_start_soc".
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
