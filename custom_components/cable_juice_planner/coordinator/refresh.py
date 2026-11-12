"""Charging loss, economy, analysis and learned driving distance refresh."""
from __future__ import annotations

import datetime
import logging
from functools import partial
from typing import Any

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import callback
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import DistanceConverter

from ..const import ENTITY_UNAVAILABLE_STATES
from ..engine.distance import DISTANCE_WEEKS
from ..engine.economy import economy_keep_days
from ..engine.loss import LOSS_GROUP_SIZE, as_series, resample_steps
from ..engine.soc_power import SOC_POWER_HISTORY_DAYS
from ..utils.helpers import float_or_none
from .constants import CHARGING_LOSS_STATISTICS_CHUNK_DAYS, CHARGING_LOSS_STATISTICS_DAYS, CONNECTED_STATUS

_LOGGER = logging.getLogger(__name__)


class CoordinatorRefresh:
    """Reads long-term statistics for the charging loss, economy, analysis and learned driving distance."""

    @callback
    def _charging_loss_hourly(self, _now) -> None:
        self._background(self._async_charging_loss_refresh(), "charging_loss_history")

    async def _async_charging_loss_refresh(self, backfill: bool = False) -> None:
        """Find charging loss segments in the recorder: the last day every hour, at the first start
        the whole raw history and, before it, the hourly long-term statistics."""
        if not self.ev_configured or "recorder" not in self.hass.config.components:
            return
        async with self._charging_loss_lock:
            await self._async_charging_loss_read(backfill)

    async def _async_charging_loss_read(self, backfill: bool) -> None:
        from homeassistant.components.recorder import get_instance, history  # noqa: PLC0415
        from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period  # noqa: PLC0415

        instance = get_instance(self.hass)
        charger = self.config["charger"]["entity_ids"]
        soc_id = self.config["ev_car"]["entity_ids"]["usable_battery_level_entity_id"]
        meter_id = charger["kwh_meter_entity_id"]
        power_id = charger["power_consumtion_entity_id"]
        if not (soc_id and meter_id and power_id):
            return
        phase_ids = [f"sensor.{self.object_prefix}_charger_phase_{i}_amps" for i in (1, 2, 3)]
        end = dt_util.utcnow()
        raw_start = end - datetime.timedelta(days=getattr(instance, "keep_days", 10) if backfill else 1.1)
        ids = [soc_id, meter_id, *phase_ids]

        def raw_history(entity_ids):
            return instance.async_add_executor_job(
                partial(
                    history.get_significant_states, self.hass, raw_start, end, entity_ids, None,
                    include_start_time_state=True, significant_changes_only=False, minimal_response=False, no_attributes=True,
                )
            )

        try:
            metadata = await instance.async_add_executor_job(partial(get_metadata, self.hass, statistic_ids={*ids, power_id}))
            states = await raw_history(ids)
            power_stats = await instance.async_add_executor_job(
                statistics_during_period, self.hass, raw_start, end, {power_id}, "5minute", None, {"mean", "min", "max"}
            )
            if not power_stats.get(power_id):
                # no statistics for the power sensor (no state_class): its raw readings, which can be many
                states.update(await raw_history([power_id]))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Charging loss: could not load the history: %s", err)
            return

        def unit(entity_id):
            if entity_id in metadata:
                return metadata[entity_id][1].get("unit_of_measurement")
            state = self.hass.states.get(entity_id)
            return state.attributes.get("unit_of_measurement") if state else None

        power_factor = 1000.0 if unit(power_id) == "kW" else 1.0
        meter_factor = {"Wh": 0.001, "MWh": 1000.0}.get(unit(meter_id), 1.0)

        def series(entity_id, factor=1.0):
            rows = [(state.last_changed.timestamp(), state.state) for state in states.get(entity_id, [])]
            return [(ts, value * factor) for ts, value in as_series(rows)]

        def stat_ts(row):
            start = row.get("start")
            return start.timestamp() if isinstance(start, datetime.datetime) else float(start)

        power_rows = [
            (stat_ts(row), row["mean"] * power_factor, row["min"] * power_factor, row["max"] * power_factor)
            for row in power_stats.get(power_id, []) if None not in (row.get("mean"), row.get("min"), row.get("max"))
        ]
        if not power_rows:
            # no statistics for the power sensor: the raw readings in 5 minute steps
            power_rows = resample_steps(series(power_id, power_factor), raw_start.timestamp(), end.timestamp())
        data: dict[str, Any] = {
            "soc": series(soc_id),
            "meter": series(meter_id, meter_factor),
            "power": power_rows,
            "phases": [series(entity_id) for entity_id in phase_ids],
        }

        _LOGGER.debug("Charging loss: history %s battery level, %s meter, %s power rows", len(data["soc"]), len(data["meter"]), len(data["power"]))
        groups = (await self.async_run_job("charging_loss_update", {**data, "backfill": False})).value or {}
        if not backfill:
            return

        # older charging from the hourly statistics, 30 days at a time, only as far back as a
        # group still misses measurements (a full group keeps its newest anyway) and as the charging
        # power by battery level keeps its statistics
        chunk_end = raw_start
        statistics_ok = True
        for _ in range(CHARGING_LOSS_STATISTICS_DAYS // CHARGING_LOSS_STATISTICS_CHUNK_DAYS):
            soc_power_done = (end - chunk_end).days >= SOC_POWER_HISTORY_DAYS
            if groups and all(count >= LOSS_GROUP_SIZE for count in groups.values()) and soc_power_done:
                break
            chunk_start = chunk_end - datetime.timedelta(days=CHARGING_LOSS_STATISTICS_CHUNK_DAYS)
            try:
                hourly = await instance.async_add_executor_job(
                    statistics_during_period, self.hass, chunk_start, chunk_end, {soc_id, meter_id, power_id}, "hour", None, {"mean", "min", "max", "change"}
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Charging loss: could not load the statistics: %s", err)
                statistics_ok = False
                break
            if not hourly.get(power_id):
                break  # before the statistics of the power sensor began
            hours: dict[float, dict] = {}
            for row in hourly.get(power_id, []):
                if row.get("mean") is not None:
                    hours.setdefault(stat_ts(row), {})
                    hours[stat_ts(row)].update(power_mean=row["mean"] * power_factor, power_min=(row.get("min") or 0.0) * power_factor, power_max=(row.get("max") or 0.0) * power_factor)
            for row in hourly.get(meter_id, []):
                if row.get("change") is not None and stat_ts(row) in hours:
                    hours[stat_ts(row)]["kwh"] = row["change"] * meter_factor
            for row in hourly.get(soc_id, []):
                if row.get("mean") is not None and stat_ts(row) in hours:
                    hours[stat_ts(row)]["soc"] = row["mean"]
            _LOGGER.debug("Charging loss: statistics %s - %s, %s hours", chunk_start.date(), chunk_end.date(), len(hours))
            groups = (await self.async_run_job("charging_loss_update", {"hours": [{"start": ts, **values} for ts, values in hours.items()]})).value or groups
            chunk_end = chunk_start
        await self.async_run_job("charging_loss_update", {"backfill": True, "soc_power_statistics_done": statistics_ok})
        _LOGGER.info("Charging loss: history read back to %s, measurements per group %s", chunk_end.date(), groups)

    @callback
    def _economy_daily(self, _now) -> None:
        self._background(self._async_economy_refresh(), "economy")

    async def _async_economy_refresh(self) -> None:
        """Hourly prices and the odometer per month from the long-term statistics; the first
        time also the connection windows of the last days from the charger's status."""
        if "recorder" not in self.hass.config.components:
            return
        from homeassistant.components.recorder import get_instance, history  # noqa: PLC0415
        from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

        instance = get_instance(self.hass)
        # the buy price history of every price provider, combined by the provider (priority per hour)
        provider = self.price_provider
        price_ids = provider.buy_price_entity_ids() if provider else []
        odometer_id = self.config["ev_car"]["entity_ids"]["odometer_entity_id"] if self.ev_configured else ""
        status_id = self.config["charger"]["entity_ids"]["status_entity_id"]
        end = dt_util.utcnow()
        keep_days = getattr(instance, "keep_days", 10)
        backfill = not self.engine.economy_db.get("backfilled")
        data: dict[str, Any] = {}

        def stat_ts(row):
            start = row.get("start")
            return start.timestamp() if isinstance(start, datetime.datetime) else float(start)

        try:
            if price_ids:
                # long-term statistics are not purged with the raw history: as far back as the economy keeps prices
                stats = await instance.async_add_executor_job(
                    statistics_during_period, self.hass, end - datetime.timedelta(days=economy_keep_days(self.config)), end, set(price_ids), "hour", None, {"mean"}
                )
                recorded = {}
                for price_id in price_ids:
                    hourly = {stat_ts(row): row["mean"] for row in stats.get(price_id, []) if row.get("mean") is not None}
                    if not hourly:
                        # no long-term statistics (no state_class): the raw history, the price changes every hour
                        start = end - datetime.timedelta(days=keep_days)
                        raw = await instance.async_add_executor_job(
                            partial(
                                history.get_significant_states, self.hass, start, end, [price_id], None,
                                include_start_time_state=True, significant_changes_only=False, minimal_response=False, no_attributes=True,
                            )
                        )
                        series = as_series((state.last_changed.timestamp(), state.state) for state in raw.get(price_id, []))
                        hourly = {row[0]: row[1] for row in resample_steps(series, start.timestamp(), end.timestamp(), 3600.0)}
                    recorded[price_id] = hourly
                data["prices"] = provider.combine_history(recorded)
            await self._async_economy_solar_statistics(instance, provider, data, end, stat_ts)
            await self._async_economy_ev_statistics(instance, data, end, stat_ts)
            if odometer_id:
                months = await instance.async_add_executor_job(
                    statistics_during_period, self.hass, end - datetime.timedelta(days=economy_keep_days(self.config)), end, {odometer_id}, "month", None, {"change"}
                )
                data["odometer_months"] = {
                    dt_util.as_local(dt_util.utc_from_timestamp(stat_ts(row))).strftime("%Y-%m"): row["change"]
                    for row in months.get(odometer_id, []) if row.get("change") is not None
                }
            if backfill and status_id:
                states = await instance.async_add_executor_job(
                    partial(
                        history.get_significant_states, self.hass, end - datetime.timedelta(days=keep_days), end, [status_id], None,
                        include_start_time_state=True, significant_changes_only=False, minimal_response=False, no_attributes=True,
                    )
                )
                windows, start = [], None
                for state in states.get(status_id, []):
                    connected = state.state in CONNECTED_STATUS
                    if connected and start is None:
                        start = state.last_changed.timestamp()
                    elif not connected and start is not None and state.state not in ENTITY_UNAVAILABLE_STATES:
                        windows.append((start, state.last_changed.timestamp()))
                        start = None
                data["windows"] = windows
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Economy: could not load the statistics: %s", err)
            return
        _LOGGER.debug("Economy: %s hourly prices, %s odometer months, %s windows", len(data.get("prices", {})), len(data.get("odometer_months", {})), len(data.get("windows", [])))
        await self.async_run_job("economy_update", data)
        if backfill:
            await self.async_run_job("economy_mark_backfilled")

    async def _async_economy_solar_statistics(self, instance, provider, data: dict[str, Any], end: datetime.datetime, stat_ts) -> None:
        """The What-if solar comparison: the hourly sell price and the hourly solar production, house consumption and
        charger power (W) from the long-term statistics, so it does not have to wait for CJP's own records.
        Missing entities or statistics just leave the keys out (a failure here must never stop the rest of the update)."""
        from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

        start = end - datetime.timedelta(days=economy_keep_days(self.config))
        try:
            sell_ids = provider.sell_price_entity_ids() if provider else []
            if sell_ids:
                stats = await instance.async_add_executor_job(
                    statistics_during_period, self.hass, start, end, set(sell_ids), "hour", None, {"mean"}
                )
                for sell_id in sell_ids:
                    hourly = {stat_ts(row): row["mean"] for row in stats.get(sell_id, []) if row.get("mean") is not None}
                    if hourly:
                        data["sell_history"] = hourly  # the first provider that has a history
                        break
            solar_id = self.config["solar"]["entity_ids"].get("production_entity_id") if self.engine.is_solar_configured() else ""
            house_id = self.config["home"]["entity_ids"].get("power_consumption_entity_id")
            ev_id = self.config["charger"]["entity_ids"].get("power_consumtion_entity_id")
            if solar_id and house_id and ev_id:
                stats = await instance.async_add_executor_job(
                    statistics_during_period, self.hass, start, end, {solar_id, house_id, ev_id}, "hour", {"power": "W"}, {"mean"}
                )
                hours: dict[float, dict[str, float]] = {}
                for key, entity_id in (("solar", solar_id), ("house", house_id), ("ev", ev_id)):
                    for row in stats.get(entity_id, []):
                        if row.get("mean") is not None:
                            hours.setdefault(stat_ts(row), {})[key] = abs(float(row["mean"]))
                if hours:
                    data["power_hours"] = hours
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Economy: could not load the solar statistics: %s", err)

    async def _async_economy_ev_statistics(self, instance, data: dict[str, Any], end: datetime.datetime, stat_ts) -> None:
        """The What-if older connections: the hourly battery level (mean, lowest, highest) and the charger power (W) from
        the long-term statistics, as far back as the economy keeps its prices. Without the entities, or without statistics
        for them, "ev_hours" is empty (nothing is estimated); when the statistics cannot be read the key is left out
        and what was stored stays."""
        from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

        soc_id = self.config["ev_car"]["entity_ids"].get("usable_battery_level_entity_id") if self.ev_configured else ""
        power_id = self.config["charger"]["entity_ids"].get("power_consumtion_entity_id")
        if not soc_id or not power_id:
            data["ev_hours"] = {}
            return
        try:
            stats = await instance.async_add_executor_job(
                statistics_during_period, self.hass, end - datetime.timedelta(days=economy_keep_days(self.config)), end, {soc_id, power_id},
                "hour", {"power": "W"}, {"mean", "min", "max"},
            )
            hours: dict[float, list[float | None]] = {}
            for row in stats.get(soc_id, []):
                if None not in (row.get("mean"), row.get("min"), row.get("max")):
                    hours[stat_ts(row)] = [row["mean"], row["min"], row["max"], None]
            for row in stats.get(power_id, []):
                ts = stat_ts(row)
                if ts in hours and row.get("mean") is not None:
                    hours[ts][3] = abs(float(row["mean"]))
            data["ev_hours"] = hours
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Economy: could not load the battery level statistics: %s", err)

    @callback
    def _analysis_daily(self, _now) -> None:
        self._background(self._async_analysis_refresh(), "analysis")

    async def _async_analysis_refresh(self) -> None:
        """Hourly outdoor temperatures for the drives; the first time also the drives of the
        recorder's raw history (unplugging to plugging in, odometer and battery level)."""
        if not self.ev_configured or "recorder" not in self.hass.config.components:
            return
        from homeassistant.components.recorder import get_instance, history  # noqa: PLC0415
        from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

        instance = get_instance(self.hass)
        temp_id = self.config["forecast"]["entity_ids"]["outdoor_temp_entity_id"]
        ev = self.config["ev_car"]["entity_ids"]
        status_id = self.config["charger"]["entity_ids"]["status_entity_id"]
        end = dt_util.utcnow()
        keep_days = getattr(instance, "keep_days", 10)
        backfill = not self.engine.analysis_backfilled()
        data: dict[str, Any] = {"backfill": backfill}
        try:
            if temp_id:
                stats = await instance.async_add_executor_job(
                    statistics_during_period, self.hass, end - datetime.timedelta(days=keep_days + 5), end, {temp_id}, "hour", None, {"mean"}
                )
                data["temps"] = {
                    (row["start"].timestamp() if isinstance(row["start"], datetime.datetime) else float(row["start"])): row["mean"]
                    for row in stats.get(temp_id, []) if row.get("mean") is not None
                }
            if backfill and status_id and ev["odometer_entity_id"] and ev["usable_battery_level_entity_id"]:
                ids = [status_id, ev["odometer_entity_id"], ev["usable_battery_level_entity_id"]]
                states = await instance.async_add_executor_job(
                    partial(
                        history.get_significant_states, self.hass, end - datetime.timedelta(days=keep_days), end, ids, None,
                        include_start_time_state=True, significant_changes_only=False, minimal_response=False, no_attributes=True,
                    )
                )
                data["status"] = [
                    (state.last_changed.timestamp(), state.state in CONNECTED_STATUS)
                    for state in states.get(status_id, []) if state.state not in ENTITY_UNAVAILABLE_STATES
                ]
                data["odometer"] = as_series((state.last_changed.timestamp(), state.state) for state in states.get(ev["odometer_entity_id"], []))
                data["soc"] = as_series((state.last_changed.timestamp(), state.state) for state in states.get(ev["usable_battery_level_entity_id"], []))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Analysis: could not load the history: %s", err)
            return
        _LOGGER.debug("Analysis: %s hourly temperatures, %s status changes", len(data.get("temps", {})), len(data.get("status", [])))
        await self.async_run_job("analysis_update", data)

    @callback
    def _distance_daily(self, _now) -> None:
        self._background(self._async_distance_refresh(), "learned_distance")

    async def _async_distance_refresh(self) -> None:
        """The odometer at the end of each day of the last weeks, for the evenings the engine did
        not read it itself (learned driving distance): the daily long-term statistics (the last
        state, the max for a measurement sensor), without statistics the raw history."""
        if not self.ev_configured or "recorder" not in self.hass.config.components:
            return
        odometer_id = self.config["ev_car"]["entity_ids"]["odometer_entity_id"]
        if not odometer_id:
            return
        from homeassistant.components.recorder import get_instance, history  # noqa: PLC0415
        from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

        instance = get_instance(self.hass)
        today = dt_util.start_of_local_day()
        start = dt_util.start_of_local_day(today.date() - datetime.timedelta(weeks=DISTANCE_WEEKS, days=1))
        # the statistics in the unit the engine reads the odometer in
        state = self.hass.states.get(odometer_id)
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) if state else None
        units = {"distance": unit} if unit in DistanceConverter.VALID_UNITS else None
        readings: dict[str, float] = {}
        try:
            stats = await instance.async_add_executor_job(
                statistics_during_period, self.hass, start, today, {odometer_id}, "day", units, {"state", "max"}
            )
            for row in stats.get(odometer_id, []):
                value = float_or_none(row.get("state") if row.get("state") is not None else row.get("max"))
                begin = row.get("start")
                if value is None or begin is None:
                    continue
                begin = begin if isinstance(begin, datetime.datetime) else dt_util.utc_from_timestamp(float(begin))
                readings[dt_util.as_local(begin).date().isoformat()] = value
            if not readings:
                # no long-term statistics (no state_class): the last state of each day in the raw history
                raw_start = max(start, dt_util.utcnow() - datetime.timedelta(days=getattr(instance, "keep_days", 10)))
                raw = await instance.async_add_executor_job(
                    partial(
                        history.get_significant_states, self.hass, raw_start, today, [odometer_id], None,
                        include_start_time_state=True, significant_changes_only=False, minimal_response=False, no_attributes=True,
                    )
                )
                rows = sorted((state.last_changed, state.state) for state in raw.get(odometer_id, []))
                index, last = 0, None
                day = dt_util.as_local(raw_start).date()
                while day < today.date():
                    day_end = dt_util.start_of_local_day(day + datetime.timedelta(days=1))
                    while index < len(rows) and rows[index][0] < day_end:
                        last = rows[index][1]
                        index += 1
                    # unavailable at the end of the day: no reading, like the engine's own
                    if (value := float_or_none(last)) is not None:
                        readings[day.isoformat()] = value
                    day += datetime.timedelta(days=1)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Learned driving distance: could not load the odometer history: %s", err)
            return
        _LOGGER.debug("Learned driving distance: %s days of odometer history", len(readings))
        if readings:
            await self.async_run_job("distance_backfill", readings)

    async def async_recalculate_charging_loss(self) -> None:
        """button recalculate_charging_loss: forget the measurements and read the whole history again."""
        await self.async_run_job("charging_loss_reset")
        await self._async_charging_loss_refresh(backfill=True)
