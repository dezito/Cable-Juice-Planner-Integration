"""Power measurements and the power database."""
from __future__ import annotations

import datetime
import logging
import math
from copy import deepcopy

from ...utils.helpers import average, get_specific_values, unique_entity_ids
from ...utils.power import power_to_w
from .meter_check import charger_meter_verdict

_LOGGER = logging.getLogger(__name__)


POWER_VALUE_KEYS = (
    "power_consumption",
    "ignored_consumption",
    "powerwall_charging_consumption",
    "powerwall_discharging_consumption",
    "ev_used_consumption",
    "solar_production",
    "power_consumption_without_ignored",
    "power_consumption_without_ignored_ev",
    "power_consumption_without_all_exclusion",
)


# A left-out consumer (an ignored sensor, the charger of a car behind this one) that has not reported for this long, with a
# value that is not 0, is taken to be stuck (a hung integration is not 'unavailable'): subtracting it for ever would take
# the house consumption down to 0, so it is not subtracted - the higher consumption is the safe side.
STALE_REPORT_SECONDS = 30 * 60

# set (True) in power_values() when a charger's most stood in for a missing reading; never stored
ESTIMATED_KEY = "estimated"


class EnergyPower:
    """Power measurements and the power database."""

    def _ends_after_stuck(self, entity_id, from_time_stamp, to_time_stamp) -> bool | None:
        """Does the window end after the entity got stuck? None when it is not stuck (it reported within the limit, or is unknown).

        Stuck = no report for STALE_REPORT_SECONDS, i.e. from last report + that limit. A window that ended before that was
        recorded while it reported and stays as it was; the live window and the hours since are corrected. Some callers give
        the two ends the other way round (timeFrom=0, timeTo=X), so the later one counts; no window at all is the live one.
        """
        age = (getattr(self._snap, "report_ages", None) or {}).get(entity_id)
        if self._snap is None or age is None or age <= STALE_REPORT_SECONDS:
            return None
        if entity_id not in (self.config["home"].get("charger_entity_ids") or []):
            return None  # only a charger is checked: a plug or a template sensor of a steady load may report only when it changes
        ends = [stamp for stamp in (from_time_stamp, to_time_stamp) if stamp is not None]
        stuck_since = self.now() - datetime.timedelta(seconds=age - STALE_REPORT_SECONDS)
        return not ends or max(ends) > stuck_since

    def _current_reading(self, entity_id) -> float | None:
        """What the entity reads now (the number of its state, in its own unit), None when it is not a number."""
        try:
            return float(self._snap.states.get(entity_id))
        except (TypeError, ValueError):
            return None

    def power_from_ignored(self, from_time_stamp, to_time_stamp) -> float:
        total = 0.0
        stale = self.__dict__.setdefault("_stale_consumers", set())  # the ones already warned about, until they report again
        try:
            for entity_id in unique_entity_ids(self.config["home"]["entity_ids"]["ignore_consumption_from_entity_ids"]):
                try:
                    value = float(self.history.get_average_value(entity_id, from_time_stamp, to_time_stamp, convert_to="W", error_state=0.0))
                    after_stuck = self._ends_after_stuck(entity_id, from_time_stamp, to_time_stamp)
                    if after_stuck is None:
                        if self._snap is not None and entity_id in (self._snap.report_ages or {}):
                            stale.discard(entity_id)  # it reports: a new stop is a new episode
                    elif after_stuck and self._current_reading(entity_id):
                        # stuck = it shows a number that is not 0 NOW; one that shows 0 now was only above 0 earlier in the window
                        if entity_id not in stale:
                            stale.add(entity_id)
                            _LOGGER.warning(
                                "%s has not reported for over %s minutes and still reads %s: it is not subtracted from the house consumption until it reports again",
                                entity_id, STALE_REPORT_SECONDS // 60, self._snap.states.get(entity_id),
                            )
                        continue
                    total += value
                except Exception:  # noqa: BLE001
                    pass
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Cant get ignore values from %s to %s: %s", from_time_stamp, to_time_stamp, err)
        return round(total, 2)

    def _reading_now_w(self, entity_id, from_time_stamp, to_time_stamp) -> float | None:
        """What a sensor without rows in the window reads now (W), only for a window that ends now: a state that has not changed
        since before the recorder's buffer (or a sensor the recorder leaves out) is still a number. None when it is no number,
        no power unit, or the window is in the past (the state now says nothing about it)."""
        if self._snap is None:
            return None
        ends = [stamp for stamp in (from_time_stamp, to_time_stamp) if isinstance(stamp, datetime.datetime)]
        try:
            if ends and max(ends) < self.now() - datetime.timedelta(minutes=5):
                return None
        except TypeError:
            return None
        reading = self._current_reading(entity_id)
        if reading is None or not math.isfinite(reading):
            return None
        watt = power_to_w(reading, (self._snap.units or {}).get(entity_id))
        return abs(watt) if watt is not None and math.isfinite(watt) else None

    def _warn_no_reading(self, entity_id, watt) -> None:
        """One warning per sensor while it has no number and its most is counted in the house (until it has one again)."""
        warned = self.__dict__.setdefault("_no_reading_warned", set())
        if entity_id not in warned:
            warned.add(entity_id)
            _LOGGER.warning(
                "%s has no number: the house consumption counts its most (%s W) until it has one again, so less solar is"
                " counted for this car", entity_id, watt,
            )

    def _power_from_added(self, from_time_stamp, to_time_stamp) -> tuple[float, bool]:
        """The chargers on their own meter of the cars ahead (home.added_consumption, set by the coordinator), added to the
        house: with one meter the house sensor would include their charging. Without a number in the window a charger counts
        its most (the safe side: never more solar for this car than with one meter), and the second value says so. A sensor
        that has a number but has not reported for a while counts that number (a charger that is idle and only reports
        changes reads 0 for hours)."""
        total = 0.0
        estimated = False
        warned = self.__dict__.setdefault("_no_reading_warned", set())
        for entity_id, most in (self.config["home"].get("added_consumption") or {}).items():
            try:
                value = self.history.get_average_value(entity_id, from_time_stamp, to_time_stamp, convert_to="W", error_state=None)
                value = abs(float(value)) if value is not None else None
            except Exception:  # noqa: BLE001
                value = None
            if value is None or not math.isfinite(value):
                value = self._reading_now_w(entity_id, from_time_stamp, to_time_stamp)
            if value is None:
                self._warn_no_reading(entity_id, most)
                value = float(most)
                estimated = True
            else:
                warned.discard(entity_id)
            total += value
        return round(total, 2), estimated

    def powerwall_max_charging_power(self, period=60) -> float:
        if not self.is_powerwall_configured():
            return 0.0
        to_time_stamp = self.now()
        from_time_stamp = to_time_stamp - datetime.timedelta(minutes=period)
        max_power = self.config["solar"]["powerwall_charging_power_limit"]
        entity_id = self.config["home"]["entity_ids"]["powerwall_watt_flow_entity_id"]
        try:
            values = self.history.get_values(entity_id, from_time_stamp, to_time_stamp, float_type=True, convert_to="W", error_state=None)
            if values is None or len(values) == 0:
                raise ValueError(f"No powerwall values for {entity_id} found from {from_time_stamp} to {to_time_stamp}")
            if self.config["home"]["invert_powerwall_watt_flow_entity_id"]:
                max_power = abs(max(get_specific_values(values, positive_only=True)))
            else:
                max_power = abs(min(get_specific_values(values, negative_only=True)))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Cant get powerwall values for %s from %s to %s: %s", entity_id, from_time_stamp, to_time_stamp, err)
        return max_power

    def get_powerwall_values(self, from_timestamp, to_timestamp) -> list:
        entity_id = self.config["home"]["entity_ids"]["powerwall_watt_flow_entity_id"]
        return self.history.get_values(entity_id, from_timestamp, to_timestamp, float_type=True, convert_to="W", error_state=[0.0])

    def charge_from_powerwall(self, powerwall_values) -> float:
        try:
            if self.config["home"]["invert_powerwall_watt_flow_entity_id"]:
                return abs(round(average(get_specific_values(powerwall_values, positive_only=True)), 0))
            return abs(round(average(get_specific_values(powerwall_values, negative_only=True)), 0))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Cant get powerwall charging consumption from values: %s", err)
            return 0.0

    def discharge_from_powerwall(self, powerwall_values) -> float:
        try:
            if self.config["home"]["invert_powerwall_watt_flow_entity_id"]:
                return abs(round(average(get_specific_values(powerwall_values, negative_only=True)), 0))
            return abs(round(average(get_specific_values(powerwall_values, positive_only=True)), 0))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Cant get powerwall discharging consumption from values: %s", err)
            return 0.0

    def power_values(self, from_time_stamp=None, to_time_stamp=None, period=None) -> dict:
        """Average power (W) of consumption, battery, EV and solar in a window. Memoised per job."""
        if period is not None:
            to_time_stamp = self.now()
            from_time_stamp = to_time_stamp - datetime.timedelta(minutes=period)
        cache = self._job_cache.setdefault("power_values", {})
        key = (from_time_stamp, to_time_stamp)
        if key not in cache:
            cache[key] = self._calculate_power_values(from_time_stamp, to_time_stamp, period)
        return dict(cache[key])

    def _calculate_power_values(self, from_time_stamp, to_time_stamp, period) -> dict:
        home = self.config["home"]
        history = self.history
        result = dict.fromkeys(POWER_VALUE_KEYS, 0.0)
        estimated = False
        try:
            power_consumption = abs(round(float(history.get_average_value(home["entity_ids"]["power_consumption_entity_id"], from_time_stamp, to_time_stamp, convert_to="W", error_state=0.0)), 2))
            ignored_consumption = abs(self.power_from_ignored(from_time_stamp, to_time_stamp))
            powerwall_values = self.get_powerwall_values(from_time_stamp, to_time_stamp)
            powerwall_charging_consumption = self.charge_from_powerwall(powerwall_values)
            powerwall_discharging_consumption = self.discharge_from_powerwall(powerwall_values)
            charger_entity = self.config["charger"]["entity_ids"]["charger_power_entity_id"]
            ev_reading = history.get_average_value(charger_entity, from_time_stamp, to_time_stamp, convert_to="W", error_state=None)
            if ev_reading is None and charger_entity and not home.get("charger_in_house_meter", True):
                ev_reading = self._reading_now_w(charger_entity, from_time_stamp, to_time_stamp)
            ev_used_consumption = abs(round(float(ev_reading), 2)) if ev_reading is not None else 0.0
            solar_production = abs(round(float(history.get_average_value(self.config["solar"]["entity_ids"]["production_entity_id"], from_time_stamp, to_time_stamp, convert_to="W", error_state=0.0)), 2))

            if not home["power_consumption_entity_id_include_powerwall_discharging"]:
                power_consumption += powerwall_discharging_consumption
            if home["power_consumption_entity_id_include_powerwall_charging"]:
                power_consumption -= powerwall_charging_consumption
            if not home.get("charger_in_house_meter", True):
                # the charger is on its own meter: the house consumption is taken to include it, as with the home battery above,
                # so everything below (the house without the EV, the solar left for the EV) reads the same as with one meter.
                # Without a number from a configured charger sensor the house counts the charger's most (the EV stays 0): with
                # one meter a missing charger reading made the house without the EV too high, never too low, and so does this
                if ev_reading is None and charger_entity:
                    power_consumption += float(self.max_watt_charging)
                    self._warn_no_reading(charger_entity, self.max_watt_charging)
                    estimated = True
                else:
                    self.__dict__.setdefault("_no_reading_warned", set()).discard(charger_entity)
                    power_consumption += ev_used_consumption
            added, added_estimated = self._power_from_added(from_time_stamp, to_time_stamp)
            power_consumption += added
            estimated = estimated or added_estimated

            power_consumption_without_ignored = round(power_consumption - ignored_consumption, 2)
            power_consumption_without_ignored_ev = round(power_consumption_without_ignored - ev_used_consumption, 2)
            result.update(
                power_consumption=power_consumption,
                ignored_consumption=ignored_consumption,
                powerwall_charging_consumption=powerwall_charging_consumption,
                powerwall_discharging_consumption=powerwall_discharging_consumption,
                ev_used_consumption=ev_used_consumption,
                solar_production=solar_production,
                power_consumption_without_ignored=power_consumption_without_ignored,
                power_consumption_without_ignored_ev=power_consumption_without_ignored_ev,
                # the battery charging is already left out above, so it is not subtracted again
                power_consumption_without_all_exclusion=max(power_consumption_without_ignored_ev, 0.0),
            )
            if estimated:
                # a charger's most stood in for a missing reading: right for the plan now (the safe side), but no measurement
                # of the house, so power_values_to_db does not store it (the consumption forecast would learn it for weeks)
                result[ESTIMATED_KEY] = True
        except Exception as err:  # noqa: BLE001
            message = f"Failed to get power values from {from_time_stamp} to {to_time_stamp} or period {period}: {err} {type(err)}"
            _LOGGER.error(message)
            self.persistent_notification(message, title=f"{self.title} error", notification_id=f"{self.name}_power_values")
        return result

    def load_power_values_db(self, database) -> None:
        database =deepcopy(database) if isinstance(database, dict) else {}
        database.pop("version", None)
        self.power_values_db = database
        for h in range(24):
            if h not in self.power_values_db:
                self.power_values_db[h] = {}
        self.mark_dirty("power_values")

    def power_values_to_db(self, data) -> None:
        # Recorded whenever the house consumption entity works; ev.py also required solar
        # and a Solcast forecast, which left the history empty without solar.
        entity_id = self.config["home"]["entity_ids"]["power_consumption_entity_id"]
        if not data or not self.is_entity_configured(entity_id):
            return
        if data.get(ESTIMATED_KEY):
            _LOGGER.info("The power values of this hour are not stored: a charger without a reading was counted with its most")
            return
        if not self.is_entity_available(entity_id):
            _LOGGER.error("Power consumption not available (%s = %s): power_values_to_db(%s)", entity_id, self.get_state(entity_id), data)
            return
        if len(self.power_values_db) == 0:
            self.load_power_values_db(None)
        hour = self.now().hour
        keep = self.config["database"]["power_values_db_data_to_save"]
        for key, value in data.items():
            if key not in self.power_values_db[hour]:
                self.power_values_db[hour][key] = []
            if key == "power_consumption_without_all_exclusion":
                self.analysis_consumption_forecast(self.power_values_db[hour][key], value)
            self.power_values_db[hour][key].insert(0, [self.now(), value])
            self.power_values_db[hour][key] = self.power_values_db[hour][key][:keep]
        self.mark_dirty("power_values")

    def note_charger_meter(self) -> None:
        """The verdict whether the house consumption sensor includes the charger (JobResult.charger_meter, the repair
        charger_outside_house_meter of management/health_checks.py). Only in a job, only with a house sensor and a charger power
        sensor, and only while the option says the charger is in the house consumption (otherwise there is nothing to tell)."""
        if self._out is None or not self.config["home"].get("charger_in_house_meter", True):
            return
        if not self.config["home"]["entity_ids"]["power_consumption_entity_id"] or not self.config["charger"]["entity_ids"]["charger_power_entity_id"]:
            return
        try:  # only information: it must never stop the start or the end of the hour (the economy that follows)
            verdict = charger_meter_verdict(self.power_values_db, self.now())
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not judge whether the house consumption includes the charger: %s", err)
            return
        if verdict["verdict"] is False and self.config["home"].get("added_consumption"):
            # the stored house includes the chargers of the cars ahead on their own meter: it may reach the charger without
            # including it, so it is no proof (a True still is: what was added only makes the house higher)
            verdict = {**verdict, "verdict": None}
        self._out.charger_meter = verdict

    def inverter_available(self, error="") -> bool:
        if not self.is_solar_configured():
            return False
        entity_id = self.config["home"]["entity_ids"]["power_consumption_entity_id"]
        if not self.is_entity_configured(entity_id) or self.is_entity_available(entity_id):
            return True
        _LOGGER.error("Inverter not available (%s = %s): %s", entity_id, self.get_state(entity_id), error)
        return False
