"""Deciding whether to charge right now."""
from __future__ import annotations

import datetime
import logging

from ...const import (
    CHARGER_CHARGING_STATUS,
    CHARGER_COMPLETED_STATUS,
    CHARGER_NOT_READY_STATUS,
    CHARGER_READY_STATUS,
    CHARGING_ALLOWED_AFTER_GOTO_TIME,
    EV_PLUGGED_STATES,
    POWERWALL_DISCHARGING_TRIGGER,
)
from ...utils.formatting import emoji_parse
from ...utils.helpers import date_to_string, minutes_between, reset_datetime, round_up
from ...prices.intervals import naive_timestamp, price_interval_at
from ..history.history import power_convert

_LOGGER = logging.getLogger(__name__)

# restarts of a charger that does not charge before is_charging gives up
CHARGER_MAX_RESTARTS = 3


class ControlSchedule:
    """Deciding whether to charge right now."""

    def ev_power_connected(self) -> bool:
        if not self.is_ev_configured():
            return True
        ids = self.config["ev_car"]["entity_ids"]
        if not self.is_entity_configured(ids["charge_cable_entity_id"]) and not self.is_entity_configured(ids["charge_port_door_entity_id"]):
            return True
        return self.get_state(ids["charge_cable_entity_id"]) in EV_PLUGGED_STATES or self.get_state(ids["charge_port_door_entity_id"]) in EV_PLUGGED_STATES

    def ready_to_charge(self):  # noqa: C901
        charger = self.config["charger"]["entity_ids"]

        def entity_unavailable(entity_id):
            return True if self.is_entity_configured(entity_id) and not self.is_entity_available(entity_id) else None

        if entity_unavailable(charger["status_entity_id"]) or entity_unavailable(charger["enabled_entity_id"]):
            return None

        charger_status = self.get_state(charger["status_entity_id"], error_state="connected")
        charger_enabled = self.get_state(charger["enabled_entity_id"], error_state="off") if self.is_entity_configured(charger["enabled_entity_id"]) else "on"
        if charger_enabled != "on" and not self.restarting_charger:
            # not while is_charging has disabled it for a restart: it enables it again itself a cron interval later
            _LOGGER.warning("Charger was off, turning it on")
            self.start_charging(["enabled"])

        if charger_status in CHARGER_NOT_READY_STATUS:
            _LOGGER.debug("Charger cable disconnected")
            self.set_charging_rule(self.t("ui.ready_to_charge.cable_disconnected"))
            # not for a charger the restart (is_charging) disabled: it may report off, and it was not unplugged
            if not self.is_ev_configured() and float(self.number_state("battery_level", error_state=0.0)) != 0.0 and not self._charger_restart_disabling():
                _LOGGER.info("Reseting battery level to 0.0")
                self.set_setting("battery_level", 0.0)
            return None
        if self.manual_charging_enabled() or self.manual_charging_solar_enabled():
            return True
        if self.is_ev_configured():
            ids = self.config["ev_car"]["entity_ids"]
            ev_charger_connector = self.get_state(ids["charge_cable_entity_id"], error_state="on") if self.is_entity_configured(ids["charge_cable_entity_id"]) else "not_configured"
            if (ev_charger_connector in EV_PLUGGED_STATES or charger_status in CHARGER_READY_STATUS + CHARGER_CHARGING_STATUS + CHARGER_COMPLETED_STATUS) and not self.is_ev_home():
                _LOGGER.debug("To long away from home")
                self.set_charging_rule(f"⛔{self.t('ui.ready_to_charge.cable_connected_ev_not_home')}")
                return None
            if ev_charger_connector not in EV_PLUGGED_STATES:
                _LOGGER.debug("Charger cable is Disconnected")
                self.set_charging_rule(f"⚠️{self.t('ui.ready_to_charge.cable_connected_not_to_ev')}")
                self.wake_up_ev()
                return True
            if not self.ev_power_connected():
                _LOGGER.debug("Chargeport not open")
                self.set_charging_rule(f"⚠️{self.t('ui.ready_to_charge.charge_port_closed')}")
                self.wake_up_ev()
                return True
            if entity_unavailable(ids["charge_port_door_entity_id"]) or entity_unavailable(ids["charge_cable_entity_id"]):
                _LOGGER.debug("Chargeport or charge cable entity not available")
                self.set_charging_rule(f"⛔{self.t('ui.ready_to_charge.ev_unavailable')}")
                self.wake_up_ev()
                return None
        return True

    def _minutes_since(self, moment) -> float | None:
        """Minutes from moment (naive local, like now()) to now, across a change of the clock (the time zone of the
        snapshot); None without a datetime."""
        if not isinstance(moment, datetime.datetime):
            return None
        now = self.now()
        tz = self._snap.tz if self._snap is not None else None
        if tz is None:
            return (now.replace(tzinfo=None) - moment.replace(tzinfo=None)).total_seconds() / 60
        return (now.replace(tzinfo=tz).timestamp() - moment.replace(tzinfo=tz).timestamp()) / 60

    def _charging_grace(self) -> bool:
        """Whether a charger that does not charge yet is still given time: within two cron intervals of when it was
        first seen waiting with a current to charge at (_charger_restart_waiting_since: a new plan window, a site that
        gives current again, power that stopped), of the end of the test of the charger automation, or of the last
        restart's start (the charger and the car take a while to start on the action). Not the charging session's start:
        it ends every hour. Whole minutes: the crons of one interval are not a second apart. No time to count from keeps
        the grace (no restart on missing data)."""
        grace = self.config["cron_interval"] * 2
        moments = (self._charger_restart_waiting_since, self._charger_test_released_at, self._charger_restart_started_at)
        for index, moment in enumerate(moments):
            if moment is None and index > 0:
                continue
            minutes = self._minutes_since(moment)
            if minutes is None or round(minutes) <= grace:
                return True
        return False

    def _waited_an_interval(self, moment) -> bool:
        """Whether a whole cron interval has passed since moment (not a datetime: no)."""
        minutes = self._minutes_since(moment)
        return minutes is not None and round(minutes) >= self.config["cron_interval"]

    def _charger_power_idle(self) -> bool | None:
        """Whether the charger's power shows that it does not charge, now and on average (the window of
        charging_without_rule). None when it can't be told (no power entity, unavailable): the status alone is no reason
        to stop a charger, a status that is stuck at ready while the car charges would stop it. The state now, not the
        last value before it became unavailable, and an average from the history: without one it can't be told."""
        entity_id = self.config["charger"]["entity_ids"]["charger_power_entity_id"]
        if not self.is_entity_configured(entity_id):
            return None
        power = self.get_state(entity_id, float_type=True, try_history=False, error_state=None)
        if power is None:
            return None
        power = power_convert(power, self.history.units.get(entity_id), "W")
        minutes = max(5, int(round_up(self.config["cron_interval"] / 2)))
        now = self.now()
        average = self.history.get_average_value(entity_id, now - datetime.timedelta(minutes=minutes), now, convert_to="W", error_state=None)
        if average is None:
            return None
        voltage = self.config["charger"]["power_voltage"]
        return abs(float(power)) <= voltage and abs(float(average)) <= voltage

    def _charger_restart_reset(self, full: bool) -> None:
        """Forget the mark and when it was first seen waiting (with full the restarts made and the give-up too: it
        charged, or the cable was unplugged). Never leaves the charger disabled."""
        self.charging_is_beginning = False
        self._charger_restart_marked_at = None
        self._charger_restart_waiting_since = None
        if self.restarting_charger:
            self.restarting_charger = False
            self.start_charging(["enabled"])
        if full:
            if self.restarting_charger_count > 0 or self._charger_restart_gave_up:
                self.start_charging(["enabled"])
            self.restarting_charger_count = 0
            self._charger_restart_gave_up = False
            self._charger_restart_started_at = None
            self._charger_restart_stopped_at = None
            self._charger_restart_enabled_seen_at = None
            self._charger_restart_rule = ""

    def _charger_restart_disabling(self) -> bool:
        """With an enabled entity, from the restart's disable until the entity is seen on again and two cron intervals, at
        least 10 minutes, have passed since: some chargers report off or disconnected while disabled, and a status read
        from a cloud comes late. Not an unplug in that time. Without an enabled entity the restart is a stop at 0 A, and
        off/disconnected is an unplug."""
        enabled_entity = self.config["charger"]["entity_ids"]["enabled_entity_id"]
        if not self.is_entity_configured(enabled_entity):
            return False
        if self.restarting_charger:
            return True
        started = self._charger_restart_started_at
        if started is None or not self._charger_restart_within_an_hour():
            return False
        if self.get_state(enabled_entity, try_history=False, error_state="off") != "on":
            self._charger_restart_enabled_seen_at = None
            return self._charger_restart_within_an_hour()
        if self._charger_restart_enabled_seen_at is None:
            self._charger_restart_enabled_seen_at = self.now()
        minutes = self._minutes_since(self._charger_restart_enabled_seen_at)
        return minutes is None or round(minutes) < max(2 * self.config["cron_interval"], 10)

    def _charger_restart_within_an_hour(self) -> bool:
        """The charger is not enabled again yet after the restart: still the restart's, for at most an hour (an automation
        that never enables it again is not kept from an unplug for ever)."""
        minutes = self._minutes_since(self._charger_restart_started_at)
        return minutes is None or round(minutes) < 60

    def charger_restart_unplugged(self) -> None:
        """The cable was unplugged (charger_status_changed): the restarts made and a give-up are forgotten, also when the
        cable is in again before the next cron."""
        if not self._charger_restart_disabling():
            self._charger_restart_unplug_pending = False
            self._charger_restart_reset(full=True)

    def _charger_restart_history_emoji(self, emoji: str, replace: str | None = None) -> None:
        """Mark the session's row in the charging history. The row may be gone (an unfinished row is removed)."""
        start = self.current_charging_session["start"]
        entry = self.charging_history_db.get(start) if start else None
        if entry is None:
            return
        if replace is not None and replace in entry["emoji"]:
            entry["emoji"] = entry["emoji"].replace(replace, emoji)
        elif emoji not in entry["emoji"]:
            entry["emoji"] = f"{entry['emoji']} {emoji}".strip()
        else:
            return
        try:
            self.charging_history_combine_and_set()
        except Exception as err:  # noqa: BLE001 - the stop, the start and the notification must not be skipped for it
            _LOGGER.error("Error marking the charging history for a restart: %s %s", err, type(err))

    def is_charging(self):  # noqa: C901
        """Check that the charger charges when the plan says charge, else stop and start it (restart sequence).

        A charger that reports ready (or error) while the plan charges, after the grace (_charging_grace), with a power
        that shows it does not charge, is marked (charging_is_beginning). A whole cron interval later, still so, it is
        stopped (restarting_charger: the stop at 0 A, or the disable with an enabled entity, is what is published, see
        _charge_if_needed and ready_to_charge), and a whole cron interval after that it is enabled again and the plan
        starts it. After MAX restarts it gives up (one notification on the alarm channel), also across the session
        that ends every hour, a stop of the plan or a test of the charger automation, until it charges (status, and power
        when it can be read), is done or the cable is unplugged."""
        t = self.t
        charger = self.config["charger"]["entity_ids"]
        restarts = CHARGER_MAX_RESTARTS
        charger_status = self.get_state(charger["status_entity_id"], error_state="unavailable")
        emoji_charging_problem = f"{emoji_parse({'charging_problem': True})}"
        emoji_charging_error = f"{emoji_parse({'charging_error': True})}"

        if self._charger_restart_unplug_pending and not self._charger_restart_disabling():
            # an off/disconnected status seen while the restart had the charger disabled (charger_status_changed): still
            # so when it has settled, it was unplugged
            self._charger_restart_unplug_pending = False
            if charger_status in CHARGER_NOT_READY_STATUS:
                _LOGGER.info("Charger status is %s after the restart of the charger: unplugged", charger_status)
                self.charger_unplugged(charger_status)

        charging_by_power = self._charger_restart_gave_up and self._charger_power_idle() is False
        if charger_status in CHARGER_COMPLETED_STATUS or charger_status in CHARGER_CHARGING_STATUS or charging_by_power:
            # after giving up also the power: a status entity that only says on/connected, or hangs, would keep the
            # fail-safe charging at full power in every hour
            if charger_status in CHARGER_CHARGING_STATUS and self._charger_power_idle():
                # "charging" without power (a car that refuses it): not charging again, the restarts made are kept
                self._charger_restart_reset(full=False)
                return True
            if self.restarting_charger_count > 0 or self._charger_restart_gave_up:
                _LOGGER.info("Ev is charging as it should again")
                if self.restarting_charger_count >= 2 or self._charger_restart_gave_up:
                    self._charger_restart_history_emoji(emoji_charging_problem, replace=emoji_charging_error)
                self._charger_restart_gave_up = False  # the plan's rule is shown again
                self.my_notify(t("ui.is_charging.charging_again"), title=f"{self.title} {t('ui.is_charging.charging_title')}", always=True, persistent_notification=True)
            self._charger_restart_reset(full=True)
            return True if charger_status in CHARGER_CHARGING_STATUS else None

        if self.restarting_charger:
            # stopped for the restart: the stop is kept for a whole cron interval, so the automation acts on it. Then the
            # charger is enabled again and the plan (charge_if_needed) publishes its own action
            if not self._waited_an_interval(self._charger_restart_stopped_at):
                return None
            _LOGGER.warning("Restarting charger (attempt %s): starting it again", self.restarting_charger_count)
            self.restarting_charger = False
            self._charger_restart_started_at = self.now()
            self.start_charging(["enabled"])
            return None

        if charger_status in CHARGER_NOT_READY_STATUS and not self._charger_restart_disabling():
            # unplugged: start over (also after the plan stopped). Not a status of a charger the restart just disabled
            self._charger_restart_reset(full=True)
            return None
        if self.charger_action != "charge":
            # the plan (or the site) stopped: the restarts made and a give-up are kept for its next charge
            self._charger_restart_reset(full=False)
            return None
        if not self.ready_to_charge():
            self._charger_restart_reset(full=False)
            return None
        if not self.current_charging_session["start"] or self._charger_restart_gave_up:
            # the session ends every hour (hour_end) while the plan goes on: the mark and the restarts made are kept
            return None

        if charger_status == "error":
            problem = t("ui.is_charging.charger_error")
        elif charger_status in CHARGER_READY_STATUS and sum(self.current_charging_amps) > 0:
            problem = t("ui.is_charging.charger_restart")
        else:
            self._charger_restart_reset(full=False)  # e.g. the site gives no current: nothing to start
            return None
        if self._charger_restart_waiting_since is None:
            self._charger_restart_waiting_since = self.now()
        if self._charging_grace():
            return None
        idle = self._charger_power_idle()
        if not idle:
            if idle is None:
                _LOGGER.debug("Charger reports %s, but its power can't tell whether it charges: no restart", charger_status)
            self._charger_restart_reset(full=False)
            return None
        if not self.charging_is_beginning:
            self.charging_is_beginning = True
            self._charger_restart_marked_at = self.now()
            return None
        if not self._waited_an_interval(self._charger_restart_marked_at):
            return None

        message = f"⛔{t('ui.is_charging.charging_failed')}\n{problem}"
        self.charging_is_beginning = False
        self._charger_restart_marked_at = None
        if self.restarting_charger_count >= restarts:
            _LOGGER.warning("Charging has not started after %s restarts: giving up until it charges or is unplugged", restarts)
            # the reason, not "restarting the charger": it doesn't any more
            reason = f"{problem}\n" if charger_status == "error" else ""
            # shown while the plan charges (_charge_if_needed), also in the plan's later charges
            self._charger_restart_rule = f"⛔{t('ui.is_charging.charging_failed')}\n{reason}{t('ui.is_charging.gave_up', count=restarts)}"
            self.set_charging_rule(self._charger_restart_rule)
            self._charger_restart_gave_up = True
            self._charger_restart_history_emoji(emoji_charging_error, replace=emoji_charging_problem)
            self.my_notify(
                f"{t('ui.is_charging.ev_not_charging')}:\n{reason}{t('ui.is_charging.gave_up', count=restarts)}",
                title=f"{self.title} {t('ui.is_charging.fail_title')}",
                data={"ttl": 0, "priority": "high", "channel": "alarm_stream"}, always=True, persistent_notification=True,
            )
            return None

        self.restarting_charger = True
        self.restarting_charger_count += 1
        self._charger_restart_stopped_at = self.now()
        self._charger_restart_enabled_seen_at = None
        if self.restarting_charger_count >= 2:
            self._charger_restart_history_emoji(emoji_charging_problem)
        if self.is_entity_configured(charger["enabled_entity_id"]):
            _LOGGER.warning("Restarting charger (attempt %s): disabling the charger for now", self.restarting_charger_count)
            restarting = t("ui.is_charging.charger_restart_count", count=self.restarting_charger_count)
            self.stop_charging(["enabled"])
        else:
            _LOGGER.warning("Restarting charging (attempt %s): stopping charging for now", self.restarting_charger_count)
            restarting = t("ui.is_charging.ev_restart_count", count=self.restarting_charger_count)
            self.stop_charging()
        self._charger_restart_rule = f"{message}\n{restarting}"
        self.set_charging_rule(self._charger_restart_rule)
        self.my_notify(f"{t('ui.is_charging.ev_not_charging')}:\n{problem}\n{restarting}", title=f"{self.title} {t('ui.is_charging.fail_title')}", always=True, persistent_notification=True)
        return None

    def charging_without_rule(self) -> bool | None:
        if self._charger_restart_gave_up:
            # the fail-safe asks for charging (_charge_if_needed): it is not charging without a rule
            self.charging_no_rule_count = 0
            return False
        minutes = max(5, int(round_up(self.config["cron_interval"] / 2)))
        now = self.now()
        past = now - datetime.timedelta(minutes=minutes)
        # just stopped (e.g. a planned hour ended): the charger may still give power until the
        # stop has reached it, and the average still holds the allowed charging (local time:
        # a stop "in the future" after the clock went back an hour doesn't count)
        if self.charger_stopped_at is not None and past < self.charger_stopped_at <= now:
            return False
        entity_id = self.config["charger"]["entity_ids"]["charger_power_entity_id"]
        power = abs(float(self.get_state(entity_id, float_type=True, error_state=0.0)))
        power_avg = round(abs(float(self.history.get_average_value(entity_id, past, now, convert_to="W", error_state=0.0))), 3)
        trigger_count = 3
        voltage = self.config["charger"]["power_voltage"]
        if power > voltage and power_avg > voltage:
            if self.charging_no_rule_count > 0:
                self.stop_charging(force=self.charging_no_rule_count >= 2)
            if self.charging_no_rule_count > trigger_count:
                if not self.current_charging_session["start"]:
                    self.charging_history({"Price": self.get_current_hour_price(), "Cost": 0.0, "kWh": 0.0, "battery_level": 0.0, "no_rule": True}, "no_rule")
                self.set_charging_rule(f"{emoji_parse({'no_rule': True})}{self.t('ui.charging_without_rule.charging_no_rule', watt=int(power_avg))}")
                _LOGGER.warning("Charging without rule for %s power: %sW, power_avg: %sW %s times", entity_id, power, power_avg, self.charging_no_rule_count)
                if self.charging_no_rule_count == trigger_count + 1:
                    unavailable_entities = self.get_all_unavailable_entities()
                    if unavailable_entities:
                        lines = [f"\n\n{self.t('ui.charging_without_rule.entities_not_available')}:", *(f"\n- {entity}" for entity in unavailable_entities)]
                        self.my_notify(
                            f"{self.t('ui.charging_without_rule.check_options')}{''.join(lines)}",
                            title=f"{self.title} {self.t('ui.charging_without_rule.ev_charging_no_rule')}",
                            always=True,
                            persistent_notification=True,
                            persistent_notification_id=f"{self.name}_charging_without_rule",
                        )
                integration = self.get_integration(entity_id)
                if integration:
                    self.persistent_notification(
                        f"⛔{self.t('ui.charging_without_rule.restart_integration', entity_id=entity_id, integration=integration.capitalize())}",
                        title=f"{self.title} {self.t('ui.charging_without_rule.ev_charging_no_rule')}",
                        notification_id=f"{self.name}_charging_without_rule_{entity_id}",
                    )
                return True
            self.charging_no_rule_count += 1
        else:
            self.charging_no_rule_count = 0
        return False

    def other_ev_connected(self) -> bool:
        charger_status = self.get_state(self.config["charger"]["entity_ids"]["status_entity_id"], error_state="disconnected")
        if charger_status not in CHARGER_READY_STATUS + CHARGER_CHARGING_STATUS + CHARGER_COMPLETED_STATUS:
            return False
        for entity_id in self.config["charger"]["entity_ids"]["other_ev_using_this_charger_entity_ids"]:
            if self.get_state(entity_id, try_history=False, error_state="off") in EV_PLUGGED_STATES:
                return True
        return False

    def current_hour_in_charge_hours(self):
        now = naive_timestamp(self.now())
        interval = price_interval_at(self.get_grid_prices(), now)
        if interval is None:
            return False
        candidates = [
            timestamp
            for timestamp in self.charge_hours
            if isinstance(timestamp, datetime.datetime) and interval[0] <= timestamp < interval[1]
        ]
        if not candidates:
            return False
        upcoming = [timestamp for timestamp in candidates if timestamp > now]
        return min(upcoming) if upcoming else max(candidates)

    def charge_if_needed(self, force_recalculate=False):
        """Plan and set the action entities, then publish the mode and the heartbeat.

        Attributes of sensor.<prefix>_charger_action for the user's automations:
        mode    normal / error (error: charge with max amps, like ev.py)
        updated time of the last calculation; an automation can fall back to charging
                when it gets old (the integration stopped or hangs)
        """
        if self.charger_hold_active():
            # the test of the automation holds the current: nothing is planned or published until it ends
            self._charger_test_publish()
            return self.charger_action
        self.charger_mode = "normal"
        try:
            self.is_battery_fully_charged()
            return self._charge_if_needed(force_recalculate)
        finally:
            self.publish_site()
            self.set_attr("charger_action", "mode", self.charger_mode)
            self.set_attr("charger_action", "updated", self.now().replace(tzinfo=self._snap.tz).isoformat() if self._snap else None)

    def _charge_if_needed(self, force_recalculate=False):  # noqa: C901
        t = self.t
        config = self.config
        max_amps = self._max_amps
        self.rule_set_this_run = False
        try:
            charging_rule = None
            trip_date_time = self.get_trip_date_time() if self.get_trip_date_time() != reset_datetime() else reset_datetime()
            if self.is_trip_planned():
                if trip_date_time != reset_datetime() and minutes_between(self.now(), trip_date_time, error_value=0) < CHARGING_ALLOWED_AFTER_GOTO_TIME:
                    _LOGGER.info("Trip date %s exceeded by an hour. Reseting trip settings", trip_date_time)
                    self.trip_reset()

            # ev.py ran these two as parallel tasks; the short one usually finished first
            inverter_watt, inverter_watt_solar_only = self.max_local_energy_available_remaining_period()
            self.cheap_grid_charge_hours(force_recalculate)

            if self.other_ev_connected():
                self.set_charging_rule(f"⛔{t('ui.charge_if_needed.other_ev_connected')}")
                return None

            inverter_amps = self.calc_charging_amps(inverter_watt, max_allowed=config["solar"]["inverter_discharging_power_limit"])[:-1]

            powerwall_discharge_watt = 0.0
            if self.is_powerwall_configured():
                discharge_above_needed = self.get_powerwall_discharge_above_needed()
                powerwall_battery_level = self.get_powerwall_battery_level()
                powerwall_reserved_battery_level = self.get_ev_charge_after_powerwall_battery_level()
                powerwall_discharging_consumption = self.power_values(period=max(config["cron_interval"] * 2, 20))["powerwall_discharging_consumption"]
                powerwall_discharge_watt = powerwall_discharging_consumption
                if discharge_above_needed and powerwall_battery_level > powerwall_reserved_battery_level + 1.0 and powerwall_discharging_consumption < POWERWALL_DISCHARGING_TRIGGER:
                    powerwall_discharge_watt = config["solar"]["powerwall_discharging_power"]

            current_hour = self.current_hour()
            current_price = self.get_current_hour_price()
            charging_limit = min(self.range_to_battery_level(), self.get_max_recommended_charge_limit_battery_level())
            circuit_amps = inverter_amps if sum(inverter_amps) > 0.0 else max_amps()
            charger_amps = [0.0, 0.0, 0.0]
            charge_hour = self.current_hour_in_charge_hours()
            solar_using_grid_price = False
            also_cheap_power = ""

            if sum(inverter_amps) != 0.0:
                charging_limit = self.get_max_recommended_charge_limit_battery_level()
                if self.is_solar_configured():
                    solar_using_grid_price = float(config["solar"]["production_price"]) == -1.0
                if charge_hour:
                    hour_data = self.charge_hours[charge_hour]
                    circuit_amps = max_amps()
                    charger_amps = max_amps()
                    if "half_min_avg_price" in hour_data:
                        ultra = self.get_ultra_cheap_grid_charging_max_battery_level()
                        charging_limit = charging_limit if charging_limit > ultra else ultra
                        also_cheap_power = " + Ultra cheap power"
                    elif "under_min_avg_price" in hour_data:
                        very = self.get_very_cheap_grid_charging_max_battery_level()
                        charging_limit = charging_limit if charging_limit > very else very
                        also_cheap_power = " + Cheap power"
                    else:
                        charging_limit = round_up(self.battery_level() + hour_data["battery_level"])
                        also_cheap_power = " + Grid Charging not enough solar production"
                    charging_limit = min(charging_limit, self.get_max_recommended_charge_limit_battery_level())

            solar = self.is_solar_production_available(inverter_watt_solar_only)
            powerwall = self.is_powerwall_discharging(powerwall_discharge_watt)
            # the fail-safe after the restarts gave up (is_charging): charge at full power whatever the plan says. A status
            # of "charging" without power counts too (else it starts and stops every cron)
            charger_status = self.get_state(config["charger"]["entity_ids"]["status_entity_id"], error_state="unavailable")
            fail_safe = self._charger_restart_gave_up and charger_status in CHARGER_READY_STATUS + CHARGER_CHARGING_STATUS + ("error",)

            if self.no_charging_modes_active():
                _LOGGER.debug("No charging modes active, setting amps to max")
                charging_limit = self.get_max_recommended_charge_limit_battery_level()
                circuit_amps = max_amps()
                charger_amps = max_amps()
                charging_rule = f"⛔{t('ui.charge_if_needed.no_charging_modes_active', watt=int(sum(charger_amps) * config['charger']['power_voltage']))}"
                self.charging_history({"Price": self.get_solar_sell_price() if sum(inverter_amps) != 0.0 else current_price, "Cost": 0.0, "kWh": 0.0, "battery_level": 0.0, "manual": True, "solar": solar}, "deactivate")
            elif self.is_calculating_charging_loss():
                limit_entity = config["ev_car"]["entity_ids"]["charging_limit_entity_id"]
                if self.is_ev_configured() and self.is_entity_configured(limit_entity):
                    completed_battery_level = float(self.get_state(limit_entity, float_type=True, error_state=100.0))
                else:
                    completed_battery_level = self.get_completed_battery_level()
                _LOGGER.info("Calculating charging loss %s%%", completed_battery_level)
                if completed_battery_level < self.get_max_recommended_charge_limit_battery_level():
                    completed_battery_level = self.get_max_recommended_charge_limit_battery_level()
                # the car must charge to the limit the measurement expects
                charging_limit = completed_battery_level
                charging_rule = f"{emoji_parse({'charging_loss': True})}{t('ui.charge_if_needed.charging_loss', percentage=completed_battery_level)}"
                circuit_amps = max_amps()
                charger_amps = max_amps()
                battery = round(completed_battery_level - self.battery_level(), 1)
                kwh = round(self.percentage_to_kwh(battery, include_charging_loss=True))
                cost = round(current_price * kwh, 2)
                self.charging_history({"Price": current_price, "Cost": cost, "kWh": kwh, "battery_level": battery, "charging_loss": True, "solar": solar, "powerwall": powerwall}, "charging_loss")
            elif self.manual_charging_enabled() or self.manual_charging_solar_enabled():
                charging_limit = self.get_max_recommended_charge_limit_battery_level()
                voltage = config["charger"]["power_voltage"]
                if not self.manual_charging_solar_enabled():
                    _LOGGER.debug("Manual charging")
                    circuit_amps = max_amps()
                    charger_amps = max_amps()
                    charging_rule = f"{emoji_parse({'manual': True})}{t('ui.charge_if_needed.manual_charging', watt=int(sum(charger_amps) * voltage))}"
                else:
                    _LOGGER.debug("Manual charging solar only")
                    charger_amps = self.calc_charging_amps(inverter_watt_solar_only, max_allowed=config["solar"]["inverter_discharging_power_limit"])[:-1]
                    circuit_amps = charger_amps
                    charging_rule = f"{emoji_parse({'manual': True, 'solar': True})}{t('ui.charge_if_needed.manual_charging_solar', watt=int(sum(charger_amps) * voltage))}"
                if sum(charger_amps) > 0.0:
                    self.charging_history({"Price": self.get_solar_sell_price() if sum(inverter_amps) != 0.0 else current_price, "Cost": 0.0, "kWh": 0.0, "battery_level": 0.0, "manual": True, "solar": solar}, "manual")
                else:
                    self.stop_current_charging_session()
            elif self.ready_to_charge():
                if charge_hour:
                    hour_data = self.charge_hours[charge_hour]
                    hour_data["solar"] = self.is_solar_production_available(inverter_watt_solar_only)
                    hour_data["powerwall"] = self.is_powerwall_discharging(powerwall_discharge_watt)
                    self.charging_history(hour_data, "planned")
                    battery_level_plus_charge = self.battery_level() + hour_data["battery_level"]
                    charging_limit = min(round_up(max(battery_level_plus_charge, self.charge_hours["max_charging_level_today"], self.range_to_battery_level())), 100.0)
                    circuit_amps = max_amps()
                    charger_amps = max_amps()
                    emoji = emoji_parse(hour_data)
                    charging_rule = t("ui.charge_if_needed.planned_charging", emoji=emoji)
                    _LOGGER.info(
                        "Charging because of %s %s%s. %sx%samps (%skWh)",
                        emoji, hour_data["Price"], t("ui.common.valuta"), int(config["charger"]["charging_phases"]), hour_data["ChargingAmps"], self.max_kwh_charging,
                    )
                elif self.switch_state("forced_charging_daily_battery_level", error_state="on") == "on" and self.battery_level() < self.get_min_daily_battery_level():
                    if current_hour in self.charge_hours["expensive_hours"]:
                        charging_rule = f"{emoji_parse({'low_battery': True})}{t('ui.charge_if_needed.low_battery_but_expensive')}"
                        _LOGGER.info("Battery under <%s%%, but power is expensive: %s", self.get_min_daily_battery_level(), date_to_string(self.charge_hours["expensive_hours"], format="%H:%M"))
                    else:
                        battery = round(self.get_min_daily_battery_level() - self.battery_level(), 1)
                        kwh = round(self.percentage_to_kwh(battery, include_charging_loss=True))
                        cost = round(current_price * kwh, 2)
                        self.charging_history({"Price": current_price, "Cost": cost, "kWh": kwh, "battery_level": battery, "low_battery": True, "solar": solar, "powerwall": powerwall}, "low_battery")
                        charging_limit = self.get_min_daily_battery_level()
                        circuit_amps = max_amps()
                        charger_amps = max_amps()
                        charging_rule = f"{emoji_parse({'low_battery': True})}{t('ui.charge_if_needed.low_battery', percentage=self.get_min_daily_battery_level())}"
                        _LOGGER.info("Charging because of under <%s%%", self.get_min_daily_battery_level())
                elif self.solar_charging_enabled() and sum(inverter_amps) != 0.0:
                    if self.battery_level() < self.get_max_recommended_charge_limit_battery_level():
                        if config["solar"]["enable_selling_during_expensive_hours"] and solar_using_grid_price and current_hour in self.charge_hours["expensive_hours"] and solar:
                            charging_rule = t("ui.charge_if_needed.solar_production_but_expensive", price=round(self.get_solar_sell_price(), 2))
                            _LOGGER.info("Ignoring solar overproduction, because of expensive hour")
                            inverter_amps = [0.0, 0.0, 0.0]
                            charger_amps = inverter_amps
                        else:
                            if charge_hour:
                                hour_data = self.charge_hours[charge_hour]
                                hour_data["solar"] = solar
                                hour_data["powerwall"] = powerwall
                                self.charging_history(hour_data, "planned")
                            else:
                                self.charging_history({"Price": self.get_solar_sell_price(), "Cost": 0.0, "kWh": 0.0, "battery_level": 0.0, "solar": solar, "powerwall": powerwall}, "local_energy")
                            solar_string = f"{emoji_parse({'solar': True})}{t('ui.charge_if_needed.solar')}" if solar else ""
                            powerwall_string = f"{emoji_parse({'powerwall': True})}{t('ui.charge_if_needed.powerwall')}" if powerwall else ""
                            local_energy_string = " &".join([solar_string, powerwall_string]) if solar and powerwall else solar_string or powerwall_string
                            charger_amps = inverter_amps
                            charging_rule = t("ui.charge_if_needed.local_energy_charging", local_energy=local_energy_string, watt=int(sum(charger_amps) * config["charger"]["power_voltage"]))
                            _LOGGER.debug("EV solar/powerwall charging at max %s%s", charger_amps, also_cheap_power)
                    else:
                        charging_rule = f"{emoji_parse({'solar': True})}{t('ui.charge_if_needed.local_energy_charging_not_needed', percentage=self.get_max_recommended_charge_limit_battery_level())}"
                        _LOGGER.debug("EV solar charging not needed, battery over %s%%", self.get_max_recommended_charge_limit_battery_level())
                else:
                    if not self.charging_without_rule() and not fail_safe:
                        self.stop_current_charging_session()
                        _LOGGER.debug("No rules for charging")
                        charging_rule = t("ui.charge_if_needed.not_charging")
            else:
                if not self.charging_without_rule() and not fail_safe:
                    self.stop_current_charging_session()
                    _LOGGER.debug("EV not ready to charge")

            if self.fill_up_charging_enabled():
                charging_limit = max(charging_limit, self.get_max_recommended_charge_limit_battery_level())

            if fail_safe:
                # fail-safe: the restarts (is_charging) did not get the charger to charge on the plan. Charge at full power,
                # like an error in the plan, whatever the plan says, until it charges, is done or is unplugged (is_charging
                # ends the give-up then, and the plan decides again). The site still limits the current. Never below the
                # plan's limit (a trip may need more than the recommended level)
                charging_limit = max(charging_limit, self.get_max_recommended_charge_limit_battery_level())
                circuit_amps = max_amps()
                charger_amps = max_amps()
                if not self.current_charging_session["start"]:
                    self.charging_history({"Price": current_price, "Cost": 0.0, "kWh": 0.0, "battery_level": 0.0, "error": True}, "error")
                fail_safe = t("ui.is_charging.fail_safe_max", watt=int(sum(charger_amps) * config["charger"]["power_voltage"]))
                charging_rule = f"{self._charger_restart_rule}\n{fail_safe}" if self._charger_restart_rule else fail_safe

            if self.restarting_charger and sum(charger_amps) > 0.0:
                # the restart (is_charging) stopped or disabled the charger for this cron: starting it again here would
                # undo it in the same publish (the automation only sees the last action). The next cron starts it. A stop
                # is published like the plan's, at 0 A: a charger that is set by its current alone starts on the amps
                if self.charger_action == "stop":
                    charger_amps = [0.0, 0.0, 0.0]
                # the reason is the restart's, not the plan's charge
                charging_rule = self._charger_restart_rule or charging_rule
            elif sum(charger_amps) > 0.0:
                self.publish_charging_limit(self.verify_charge_limit(charging_limit))
                self.start_charging()
            else:
                self.stop_charging()
            if charging_rule:
                self.set_charging_rule(charging_rule)
            elif not self.rule_set_this_run:
                # no branch set a rule this run, so never keep the previous one (stale emoji)
                self.set_charging_rule(t("ui.charge_if_needed.not_charging"))
            self.set_charger_phase_limits(circuit_amps, charger_amps)
        except Exception as err:  # noqa: BLE001
            self.charger_mode = "error"
            message = f"Error running charge_if_needed(), setting charger and car to max: {err} {type(err)}"
            _LOGGER.exception(message)
            self.persistent_notification(
                f"Error running charge_if_needed(), setting charger and car to max\nTrying to restart script to fix error in {self.error_count}/3: {err} {type(err)}",
                title=f"{self.title} error",
                notification_id=f"{self.name}_charge_if_needed_restart_count_error",
            )
            if self.error_count == 3:
                _LOGGER.error("Reloading the integration to maybe fix error!!!")
                self.persistent_notification("Reloading the integration to maybe fix error!!!", title=f"{self.title} error", notification_id=f"{self.name}_charge_if_needed_restart_script")
                if self._out is not None:
                    self._out.request_reload = True
            self.error_count += 1
            self.publish_charging_limit(self.verify_charge_limit(self.get_max_recommended_charge_limit_battery_level()))
            self.start_charging()
            self.charging_history({"Price": 0.0, "Cost": 0.0, "kWh": 0.0, "battery_level": 0.0, "error": True}, "error")
            self.set_charging_rule(f"{emoji_parse({'error': True})}{t('ui.charge_if_needed.error_charging_max')}")
            self.set_charger_phase_limits(max_amps(), max_amps())
        return self.charger_action
