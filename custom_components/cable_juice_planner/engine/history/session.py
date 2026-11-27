"""The current and public charging sessions."""
from __future__ import annotations

import datetime
import logging
import math

from ...const import ENTITY_UNAVAILABLE_STATES, EV_PLUGGED_STATES, EV_UNPLUGGED_STATES, normalize_state
from ...utils.formatting import emoji_parse
from ...utils.helpers import to_local_datetime

_LOGGER = logging.getLogger(__name__)

# km (or miles) the odometer may move during a public charging session: rounding and GPS-based odometers
PUBLIC_CHARGING_MAX_ODOMETER_CHANGE = 1.0


class HistorySession:
    """The current and public charging sessions."""

    def reset_current_charging_session(self) -> None:
        self.current_charging_session = {"charging_type": None, "start": None, "start_charger_meter": 0.0, "emoji": "", "data": {}}

    def stop_current_charging_session(self) -> None:
        self.charging_history(None, "")

    def _is_not_home(self) -> bool:
        """Known to be away. No location entity, or an unknown location, is never a public charge:
        the charger's own status can come minutes after the car's port/cable, so it can't tell home from away."""
        location_entity = self.config["ev_car"]["entity_ids"]["location_entity_id"]
        if not self.is_entity_configured(location_entity):
            return False
        current_location = self.get_state(location_entity, error_state="unknown")
        return not (normalize_state(current_location) == "home" or current_location in ENTITY_UNAVAILABLE_STATES)

    def _is_home(self) -> bool:
        """Known to be home (like _is_not_home, a briefly unavailable location is its last known state of the past day).
        A session still open then never ends away from home: the car integration can report the port a moment before
        the location turned home (both in one update), which started it at home."""
        location_entity = self.config["ev_car"]["entity_ids"]["location_entity_id"]
        if not self.is_entity_configured(location_entity):
            return False
        return normalize_state(self.get_state(location_entity, error_state="unknown")) == "home"

    def _odometer(self) -> float | None:
        """The odometer now; None without one or without a number (not the same as not driven).
        Only the live state: a last known value from before a drive would look like a drive."""
        odometer_entity = self.config["ev_car"]["entity_ids"]["odometer_entity_id"]
        if not self.is_entity_configured(odometer_entity) or not self.entity_exists(odometer_entity):
            return None
        state = self.get_state(odometer_entity, try_history=False, error_state=None)
        try:
            value = float(state)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    def _driven_since_start(self, session: dict) -> bool:
        """A car can't drive while it charges: a moved odometer means the session's end was missed.
        Without an odometer reading at the start or now, nothing is known to have moved."""
        start = session.get("start_odometer")
        now = self._odometer()
        if not isinstance(start, (int, float)) or now is None:
            return False
        return abs(now - start) > PUBLIC_CHARGING_MAX_ODOMETER_CHANGE

    def _charger_meter(self) -> float | None:
        value = self.get_state(self.config["charger"]["entity_ids"]["kwh_meter_entity_id"], float_type=True, error_state=None)
        return value if isinstance(value, float) and math.isfinite(value) else None

    def _session_end_time(self, session: dict) -> datetime.datetime:
        """When the charge ended (naive local time like the history keys), not when its cost was entered.
        A session without a usable end time (or one in the future) is booked now, like before."""
        now = self.now()
        ended = session.get("timestamp")
        if isinstance(ended, str):
            try:
                ended = datetime.datetime.fromisoformat(ended)
            except ValueError:
                ended = None
        if not isinstance(ended, datetime.datetime):
            return now
        if ended.tzinfo is not None:
            ended = ended.astimezone(self._snap.tz).replace(tzinfo=None) if self._snap else ended.replace(tzinfo=None)
        if ended > now:
            return now
        while ended in self.charging_history_db:  # never overwrite another entry
            ended += datetime.timedelta(seconds=1)
        return ended

    def _session_odometer(self, session: dict) -> float | None:
        """The odometer of the charge itself; the car doesn't move while charging, so the start counts too.
        A session that recorded none (from before this was stored) is booked without one: the odometer now may be
        days and kilometres later. The next start fills it in from the recorder while it still keeps that time."""
        for key in ("end_odometer", "start_odometer"):
            value = session.get(key)
            if isinstance(value, (int, float)) and math.isfinite(value):
                return float(value)
        if "timestamp" not in session:
            return self._odometer()
        return None

    def _start_public_session(self) -> None:
        session = {"start_battery_level": self.battery_level(real=True, unknown=None)}
        odometer = self._odometer()
        if odometer is not None:
            session["start_odometer"] = odometer
        self.public_charging_sessions.append(session)
        self.mark_dirty("runtime")

    def _set_public_session_done(self, value: str) -> None:
        old = self.sensor_states.get("public_charging_session_done")
        self.set_state("public_charging_session_done", value)
        self.mark_dirty("runtime")
        if old != value:
            # state_trigger_public_charging_session_done
            self.public_charging_session_done_changed(value)

    def finished_public_charging_sessions(self) -> list[dict]:
        """The sessions waiting for their cost; a session still charging (no end level yet) is left out."""
        return [s for s in self.public_charging_sessions if "end_battery_level" in s]

    def _publish_public_sessions(self) -> None:
        self.set_attr("public_charging_session_done", "public_charging_session", [dict(s) for s in self.finished_public_charging_sessions()])

    def public_charging_session(self, entity_state: str):
        """Charge port/cable changed away from home (or the cost was entered: add_to_history)."""
        t = self.t
        sessions = self.public_charging_sessions
        # only the last session can still be open (no end level yet); the finished ones wait for their cost
        is_open = bool(sessions) and "end_battery_level" not in sessions[-1]
        if entity_state in EV_PLUGGED_STATES and self._is_not_home():
            if is_open:
                if not self._driven_since_start(sessions[-1]):
                    return None
                _LOGGER.warning("Public charging session: The car was driven since the open session started at %s%%, starting a new one", sessions[-1]["start_battery_level"])
                sessions.pop(-1)
            self._start_public_session()
        elif is_open and entity_state in EV_PLUGGED_STATES + EV_UNPLUGGED_STATES and self._is_home():
            _LOGGER.warning("Public charging session: The port/cable changed at home while a session started at %s%% was open, dropping it", sessions[-1]["start_battery_level"])
            sessions.pop(-1)
            self.mark_dirty("runtime")
        elif is_open and entity_state in EV_UNPLUGGED_STATES and self._is_not_home():
            if self._driven_since_start(sessions[-1]):
                _LOGGER.warning("Public charging session: The car was driven since the session started at %s%%, its end was missed, not measuring it", sessions[-1]["start_battery_level"])
                sessions.pop(-1)
                self.mark_dirty("runtime")
                if not self.finished_public_charging_sessions():
                    self._set_public_session_done("off")
                return None
            start_level = sessions[-1]["start_battery_level"]
            end_level = self.battery_level(real=True, unknown=None)
            if start_level is None or end_level is None:
                # no level to measure from (car integration down), which is not the same as 0 %
                _LOGGER.warning("Public charging session: No battery level at the %s, not measuring it", "start" if start_level is None else "end")
                sessions.pop(-1)
                self.mark_dirty("runtime")
                if not self.finished_public_charging_sessions():
                    self._set_public_session_done("off")
                return None
            sessions[-1].update({
                "end_battery_level": end_level,
                "added_percentage": round(end_level - start_level, 1),
                "timestamp": self.now(),
            })
            # booked with the charge's own time, odometer and charger meter, also when the cost comes days later
            end_odometer = self._odometer()
            if end_odometer is not None:
                sessions[-1]["end_odometer"] = end_odometer
            charger_meter = self._charger_meter()
            if charger_meter is not None:
                sessions[-1]["charger_meter"] = charger_meter
            self.mark_dirty("runtime")
            if sessions[-1]["added_percentage"] <= 0.0:
                _LOGGER.warning("Public charging session: End battery level %s%% is less than start battery level %s%%", sessions[-1]["end_battery_level"], sessions[-1]["start_battery_level"])
                sessions.pop(-1)
                if not sessions:
                    self._set_public_session_done("off")
                return None
            self._set_public_session_done("on")
            self._publish_public_sessions()
            self.set_setting("public_charging_session_cost", -2.0)
            data = {"actions": [{"action": "URI", "title": t("ui.public_charging_session.action"), "uri": f"entityId:{self.own_entity_id('number', 'public_charging_session_cost')}"}]}
            self.my_notify(
                t(
                    "ui.public_charging_session.message",
                    start_battery_level=sessions[-1]["start_battery_level"],
                    end_battery_level=sessions[-1]["end_battery_level"],
                    added_battery_level=sessions[-1]["added_percentage"],
                ),
                title=f"{self.title} {t('ui.public_charging_session.title')}",
                data=data,
                always=True,
            )
        elif self.get_public_charging_session_done() and self.finished_public_charging_sessions() and entity_state == "add_to_history":
            # the cost is for the oldest finished session; a session still charging stays in the queue
            first = next(i for i, s in enumerate(sessions) if "end_battery_level" in s)
            cost = self.number_state("public_charging_session_cost")
            if not isinstance(cost, float):
                _LOGGER.warning("Public charging session: Invalid cost %s, not adding to history", cost)
                self.set_setting("public_charging_session_cost", -2.0)
                return None
            if cost == 0.0:
                return None
            if cost == -1.0:
                _LOGGER.info("Public charging session cost reset to -1.0, not adding to history")
                sessions.pop(first)
                self.mark_dirty("runtime")
                if not self.finished_public_charging_sessions():
                    self._set_public_session_done("off")
                else:
                    self.set_setting("public_charging_session_cost", -2.0)
                    self._publish_public_sessions()
                return None
            if cost < 0.0:
                self.set_setting("public_charging_session_cost", 0.0)
                return None
            current_session = sessions.pop(first)
            self.mark_dirty("runtime")
            added_kwh = self.percentage_to_kwh(current_session["added_percentage"], include_charging_loss=False)
            timestamp = self._session_end_time(current_session)
            charger_meter = current_session.get("charger_meter")
            if not isinstance(charger_meter, (int, float)):
                charger_meter = float(self.get_state(self.config["charger"]["entity_ids"]["kwh_meter_entity_id"], float_type=True))
            self.charging_history_db[timestamp] = {
                "percentage": round(current_session["added_percentage"], 1),
                "kWh": round(added_kwh, 3),
                "kWh_from_public": round(added_kwh, 3),
                "kWh_from_local_energy": 0.0,
                "solar_kwh_of_local_energy": 0.0,
                "powerwall_kwh_of_local_energy": 0.0,
                "cost": round(cost, 3),
                "public_cost": round(cost, 3),
                "unit": round(cost / added_kwh if added_kwh > 0.0 else 0.0, 3),
                "emoji": emoji_parse({"public_charging": True}),
                "start_charger_meter": charger_meter,
                "end_charger_meter": charger_meter,
                "ended": f"{timestamp.strftime('%H:%M')}",
                "charging_type": "public_charging",
            }
            odometer = self._session_odometer(current_session)
            if odometer:
                self.charging_history_db[timestamp]["odometer"] = int(odometer)
            if not self.finished_public_charging_sessions():
                self._set_public_session_done("off")
            else:
                self.set_setting("public_charging_session_cost", -2.0)
                self._publish_public_sessions()
            _LOGGER.info("Public charging session added to history: %s%% %.2fkWh %.2fkr", current_session["added_percentage"], added_kwh, cost)
            try:
                self.charging_history_combine_and_set()
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Error in public_charging_session: %s %s", err, type(err))
                self.persistent_notification(f"Error in public_charging_session: {err} {type(err)}", f"{self.title} error", f"{self.name}_public_charging_session_error")
        else:
            _LOGGER.warning(
                "Public charging session: Invalid entity_state transition from %s with %s, get_public_charging_session_done()=%s",
                entity_state, sessions, self.get_public_charging_session_done(),
            )
        return None

    def public_charging_session_done_changed(self, value) -> None:
        """state_trigger_public_charging_session_done"""
        if value in ENTITY_UNAVAILABLE_STATES:
            return
        if value == "off":
            # keep a session that is still charging away from home
            self.public_charging_sessions = [s for s in self.public_charging_sessions if "end_battery_level" not in s]
            self.mark_dirty("runtime")
            self.set_setting("public_charging_session_cost", 0.0)
            self.del_attr("public_charging_session_done", "public_charging_session")

    def manual_add_public_charging_session(self, charging_session_start, battery_level_begin=None, battery_level_end=None, battery_level_added=None,
                                           battery_kwh_added=None, total_charging_cost=None, include_charging_loss=False, add_to_history=False) -> dict:
        try:
            if charging_session_start is None:
                raise ValueError('"Charging Session Start" is required.')
            # a start with an offset (e.g. {{ utcnow() }}) is converted to Home Assistant's time zone first
            charging_session_start = to_local_datetime(charging_session_start, self._snap.tz if self._snap else None)
            if not isinstance(charging_session_start, datetime.datetime):
                raise ValueError('"Charging Session Start" must be a datetime object.')
            if battery_level_begin is None and battery_level_end is None and battery_level_added is None and battery_kwh_added is None:
                raise ValueError('Use at least "Battery Level Begin" and "Battery Level End" or "Battery Level Added" or "Battery kWh Added" to calculate the added energy.')
            if battery_kwh_added is not None:
                if not isinstance(battery_kwh_added, (int, float)):
                    raise ValueError('"Battery kWh Added" must be a number.')
                if battery_kwh_added <= 0:
                    raise ValueError('"Battery kWh Added" must be greater than 0.')
                consumption_used = "Battery kWh Added"
                added_percentage = self.kwh_to_percentage(float(battery_kwh_added), include_charging_loss=include_charging_loss)
                added_kwh = float(battery_kwh_added)
            elif battery_level_added is not None:
                if not isinstance(battery_level_added, (int, float)):
                    raise ValueError('"Battery Level Added" must be a number.')
                if battery_level_added <= 0:
                    raise ValueError('"Battery Level Added" must be greater than 0.')
                consumption_used = "Battery Level Added"
                added_percentage = float(battery_level_added)
                added_kwh = self.percentage_to_kwh(added_percentage, include_charging_loss=include_charging_loss)
            else:
                if battery_level_begin is not None and battery_level_end is None:
                    raise ValueError('"Battery Level End" is required when "Battery Level Begin" is provided.')
                if battery_level_begin is None and battery_level_end is not None:
                    raise ValueError('"Battery Level Begin" is required when "Battery Level End" is provided.')
                if not isinstance(battery_level_begin, (int, float)) or not isinstance(battery_level_end, (int, float)):
                    raise ValueError('"Battery Level Begin" and "Battery Level End" must be numbers.')
                if battery_level_end <= battery_level_begin:
                    raise ValueError('"Battery Level End" must be greater than "Battery Level Begin".')
                consumption_used = "Battery Level Begin/End"
                added_percentage = battery_level_end - battery_level_begin
                added_kwh = self.percentage_to_kwh(added_percentage, include_charging_loss=include_charging_loss)
            if total_charging_cost is None:
                raise ValueError('"Total Charging Cost" is required.')
            if not isinstance(total_charging_cost, (int, float)):
                raise ValueError('"Total Charging Cost" must be a number.')
            if total_charging_cost <= 0:
                raise ValueError('"Total Charging Cost" must be greater than 0.')
            total_charging_cost = float(total_charging_cost)
            kwh_cost = total_charging_cost / added_kwh if added_kwh > 0 else 0.0
            data = {
                "Charging Session Start": charging_session_start.isoformat(),
                "Consumption Used": consumption_used,
                "Added Percentage": round(added_percentage, 1),
                "Added kWh": round(added_kwh, 3),
                "kWh Cost": round(kwh_cost, 3),
                "Total Charging Cost": round(total_charging_cost, 3),
            }
            if not add_to_history:
                return {"status": "success", "message": "Public charging session results. Nothing was added to history.", "data": data}
            charger_meter = float(self.get_state(self.config["charger"]["entity_ids"]["kwh_meter_entity_id"], float_type=True))
            self.charging_history_db[charging_session_start] = {
                "percentage": round(added_percentage, 1),
                "kWh": round(added_kwh, 3),
                "kWh_from_public": round(added_kwh, 3),
                "kWh_from_local_energy": 0.0,
                "solar_kwh_of_local_energy": 0.0,
                "powerwall_kwh_of_local_energy": 0.0,
                "cost": round(total_charging_cost, 3),
                "public_cost": round(total_charging_cost, 3),
                "unit": round(kwh_cost, 3),
                "emoji": emoji_parse({"public_charging": True}),
                "start_charger_meter": charger_meter,
                "end_charger_meter": charger_meter,
                "ended": f"{charging_session_start.strftime('%H:%M')}",
                "charging_type": "public_charging",
            }
            self.charging_history_combine_and_set()
            return {"status": "success", "message": "Public charging session added successfully to history.", "data": data}
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error in manual_add_public_charging_session: %s", err)
            return {"status": "error", "message": f"{err}"}
