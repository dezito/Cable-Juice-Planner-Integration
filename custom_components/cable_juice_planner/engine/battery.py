"""Battery levels, range, distance and charging loss."""
from __future__ import annotations

import datetime
import logging
import math

from ..const import CHARGER_CHARGING_STATUS, CHARGER_COMPLETED_STATUS, CHARGER_READY_STATUS, ENTITY_UNAVAILABLE_STATES, EV_UNPLUGGED_STATES
from ..utils.helpers import float_or_none, round_up

_LOGGER = logging.getLogger(__name__)

# How long ago the car's battery level sensor may have stopped giving numbers for its last number to still be used
BATTERY_LEVEL_MAX_AGE = datetime.timedelta(hours=6)


class Battery:
    """Battery levels, range, distance and charging loss."""

    def get_min_daily_battery_level(self) -> float:
        return self._number_or_config("min_daily_battery_level", ("ev_car", "min_daily_battery_level"), "daily battery level")

    def get_min_trip_battery_level(self) -> float:
        return self._number_or_config("min_trip_battery_level", ("ev_car", "min_trip_battery_level"), "min trip battery level")

    def get_min_charge_limit_battery_level(self) -> float:
        return self._number_or_config("min_charge_limit_battery_level", ("ev_car", "min_charge_limit_battery_level"), "min charge limit battery level")

    def get_max_recommended_charge_limit_battery_level(self) -> float:
        return self._number_or_config(
            "max_recommended_charge_limit_battery_level", ("ev_car", "max_recommended_charge_limit_battery_level"), "max recommended charge limit battery level"
        )

    def get_very_cheap_grid_charging_max_battery_level(self) -> float:
        return self._number_or_config(
            "very_cheap_grid_charging_max_battery_level", ("ev_car", "very_cheap_grid_charging_max_battery_level"), "very cheap grid charging max battery level"
        )

    def get_ultra_cheap_grid_charging_max_battery_level(self) -> float:
        return self._number_or_config(
            "ultra_cheap_grid_charging_max_battery_level", ("ev_car", "ultra_cheap_grid_charging_max_battery_level"), "ultra cheap grid charging max battery level"
        )

    def get_powerwall_discharge_above_needed(self):
        if not self.is_powerwall_configured():
            return False
        return self.switch_state("powerwall_discharge_above_needed", error_state="off") == "on"

    def get_powerwall_battery_level(self) -> float:
        if not self.is_powerwall_configured():
            return 100.0
        entity_id = self.config["home"]["entity_ids"]["powerwall_battery_level_entity_id"]
        try:
            return float(self.get_state(entity_id, float_type=True, error_state=None))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Cant get powerwall battery level from %s, using default value 100.0: %s", entity_id, err)
            return 100.0

    def get_ev_charge_after_powerwall_battery_level(self) -> float:
        if not self.is_powerwall_configured():
            return 100.0
        value = self.number_state("ev_charge_after_powerwall_battery_level")
        if value is None:
            try:
                return float(self.config["solar"]["ev_charge_after_powerwall_battery_level"])
            except Exception:  # noqa: BLE001
                return 100.0
        return float(value)

    def get_completed_battery_level(self) -> float:
        try:
            if not self.is_ev_configured():
                return float(self.number_state("completed_battery_level"))
            return 100.0
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Using default charge completed battery level 100.0: %s", err)
            return 100.0

    def get_estimated_total_range(self) -> float:
        estimated_total_range = 0.0
        try:
            if self.is_ev_configured():
                calc = self.get_forecast_value(self.km_kwh_efficiency_db, self.drive_forecast_type)
                if calc == 0.0:
                    raise ValueError(f"calc is invalid: {calc}")
                estimated_total_range = self.km_kwh_to_km_percentage(calc) * 100.0
            else:
                try:
                    estimated_total_range = float(self.number_state("estimated_total_range"))
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error("estimated_total_range is not set to a total range: %s", err)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Using default estimated total range 0.0: %s", err)
        return estimated_total_range

    def charging_loss_factor(self, kw: float | None = None) -> float:
        """kWh from the charger = kWh in the car * (1 + this).

        The configured charging loss, or with use_measured_charging_loss the measured one
        (engine/loss.py) once there are measurements: at kw when given and the model knows
        more than one power, else at the max charging power. Calculated once per job and kw.
        """
        if self._snap is not None and self.switch_state("use_measured_charging_loss", error_state="off") == "on":
            cache = self._job_cache.setdefault("charging_loss_factor", {})
            key = None if kw is None else round(kw, 2)
            if key not in cache:
                cache[key] = self.measured_charging_loss_factor(kw)
            if cache[key] is not None:
                return cache[key]
        return abs(self.config["charger"]["charging_loss"])

    def kwh_to_percentage(self, kwh, include_charging_loss=False, kw: float | None = None):
        effective_kwh = kwh
        if include_charging_loss:
            effective_kwh = kwh / (1 + self.charging_loss_factor(kw))
        return effective_kwh / self.config["ev_car"]["battery_size"] * 100

    def percentage_to_kwh(self, percentage, include_charging_loss=False, kw: float | None = None):
        kwh = percentage * self.config["ev_car"]["battery_size"] / 100
        if include_charging_loss:
            kwh = kwh * (1 + self.charging_loss_factor(kw))
        return kwh

    def solar_charging_kw(self) -> float:
        """A typical solar charging power: the middle of the 1 phase solar charging range."""
        solar = self.config["solar"]
        amps = (float(solar["charging_single_phase_min_amp"]) + float(solar["charging_single_phase_max_amp"])) / 2
        return amps * float(self.config["charger"]["power_voltage"]) / 1000

    def km_kwh_to_km_percentage(self, kwh):
        return self.percentage_to_kwh(kwh, include_charging_loss=False)

    def km_percentage_to_km_kwh(self, percentage):
        return self.kwh_to_percentage(percentage, include_charging_loss=False)

    def distance_per_percentage(self) -> float:
        output = 3.0
        if not self.is_ev_configured():
            try:
                calc = round(self.get_estimated_total_range() / 100, 2)
                if calc <= 0.0:
                    # number.<prefix>_estimated_total_range is 0 until the user enters the car's range
                    raise ValueError(f"estimated_total_range is not set: {calc * 100}")
                output = calc
            except Exception as err:  # noqa: BLE001
                if not self._job_cache.get("distance_per_percentage_warned"):
                    self._job_cache["distance_per_percentage_warned"] = True
                    _LOGGER.warning("Using default value 3.0: %s", err)
        else:
            try:
                if self.km_kwh_efficiency_db == {}:
                    raise ValueError("No data yet")
                calc = self.get_forecast_value(self.km_kwh_efficiency_db, self.drive_forecast_type)
                if calc == 0.0:
                    raise ValueError(f"calc is invalid: {calc}")
                output = self.km_kwh_to_km_percentage(calc)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Using default value 3.0: %s", err)
        return output

    def calc_distance_to_battery_level(self, distance):
        return distance / self.distance_per_percentage()

    def calc_battery_level_to_distance(self, battery_level):
        return battery_level * self.distance_per_percentage()

    def battery_level(self, real: bool = False) -> float:
        """The car's battery level (the virtual one without a car integration).

        With simulate_battery_level_while_charging the planning and charger control get the
        simulated level; measurements and history (drive efficiency, charging loss, public
        charging, departures, full charge) ask for the car's own level with real=True.
        """
        try:
            if not self.is_ev_configured():
                return float(self.number_state("battery_level", error_state=0.0))
            entity_id = self.config["ev_car"]["entity_ids"]["usable_battery_level_entity_id"]
            if not self.is_entity_available(entity_id):
                return 0.0
            level = self.get_state(entity_id, try_history=False, float_type=True, error_state=None)
            if not isinstance(level, float) or not math.isfinite(level):
                # unavailable, unknown, text, "nan" or "inf": the last number the sensor gave (also one that
                # never changed since), else 0 % (plans more charging, never less). A car that is unavailable
                # or unknown (e.g. asleep) keeps the window of a day; text, "nan" and "inf" are a broken value
                # and only count 6 hours back for planning. Measurements (real=True) always look a day back.
                unavailable = self.get_state(entity_id, try_history=False, error_state=None) in ENTITY_UNAVAILABLE_STATES
                max_age = datetime.timedelta(days=1) if real or unavailable else BATTERY_LEVEL_MAX_AGE
                level = self.history.get_last_value(entity_id, float_type=True, error_state=None, max_age=max_age, warn=False)
                if not isinstance(level, float) or not math.isfinite(level):
                    # asked ~40 times per job: warn once
                    if not self._job_cache.get("battery_level_warned"):
                        self._job_cache["battery_level_warned"] = True
                        _LOGGER.warning("No battery level from %s the last %s, using 0%%", entity_id, max_age)
                    return 0.0
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(err)
            return 0.0
        return level if real else self.simulated_battery_level(level)

    def simulated_battery_level(self, real_level: float) -> float:
        """real_level plus what the charger's kWh meter counted since the car reported it.

        For car integrations that update the battery level only every 30-60 minutes while
        charging. Every new value from the car (or a new time in its last update sensor)
        moves the anchor (level, meter), so the simulation is corrected and rises from
        there. It stays 1 % under the charging limit, the car's own level confirms the end.
        A level entered on number.<prefix>_battery_level replaces it until the car reports again.
        Kept in memory only: after a restart it starts from the car's level.
        """
        if not self.battery_simulation_enabled or real_level <= 0.0:
            return real_level
        if "simulated_battery_level" not in self._job_cache:
            try:
                level = self._simulate_battery_level(real_level)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Battery level simulation failed, using the car's %s%%: %s", real_level, err)
                self.battery_simulation = {}
                level = real_level
            self._job_cache["simulated_battery_level"] = level
        return self._job_cache["simulated_battery_level"]

    def _simulate_battery_level(self, real_level: float) -> float:
        meter_id = self.config["charger"]["entity_ids"]["kwh_meter_entity_id"]
        meter = self.get_state(meter_id, try_history=False, float_type=True, error_state=None) if self.is_entity_configured(meter_id) else None
        if meter is None:
            self.battery_simulation = {}
            return real_level
        meter *= {"Wh": 0.001, "MWh": 1000.0}.get(self._snap.units.get(meter_id) if self._snap else None, 1.0)
        update_id = self.config["ev_car"]["entity_ids"]["last_update_entity_id"]
        reported = str(self.get_state(update_id, try_history=False, error_state="")) if self.is_entity_configured(update_id) else ""
        if reported in ENTITY_UNAVAILABLE_STATES:
            reported = ""
        sim = self.battery_simulation
        if not sim or real_level != sim["real"] or (reported and reported != sim["reported"]):
            # the car reported: start again from its level
            # base/meter: where the counting starts, counted: kWh counted before it since the car reported,
            # manual: entered by the user, published: the value last written to the number entity
            self.battery_simulation = {
                "real": real_level, "reported": reported, "meter": meter, "base": real_level, "level": real_level,
                "counted": 0.0, "kwh": 0.0, "seen": self.now(), "manual": False, "published": None,
            }
            return real_level
        entered = float_or_none(self.setting("battery_level"))
        if entered is not None and sim["published"] is not None and abs(entered - sim["published"]) >= 0.05:
            # entered on the number entity (e.g. the car's level is 30 minutes behind at plug-in):
            # rise from there until the car reports again
            entered = min(max(entered, 0.0), 100.0)
            sim.update(base=entered, level=entered, meter=meter, counted=sim["kwh"], manual=True, published=entered)
            return entered
        if meter < sim["meter"] or not self._battery_simulation_connected():
            # meter reset, or the meter counts without this car: keep the level, count from here
            sim["base"], sim["meter"], sim["counted"] = sim["level"], meter, sim["kwh"]
            return sim["level"]
        sim["kwh"] = sim["counted"] + meter - sim["meter"]
        added = self.kwh_to_percentage(meter - sim["meter"], include_charging_loss=True)
        # an entered level may be under the car's (it can be behind), and above the ceiling
        floor = 0.0 if sim["manual"] else real_level
        sim["level"] = round(max(floor, min(sim["base"] + added, max(self._battery_simulation_ceiling(), sim["base"]))), 1)
        return sim["level"]

    def _battery_simulation_connected(self) -> bool:
        """The charger's kWh meter counts for this car: the charger is connected and the car's cable (if known) too."""
        charger_status = self.get_state(self.config["charger"]["entity_ids"]["status_entity_id"], try_history=False, error_state="unavailable")
        if charger_status not in CHARGER_READY_STATUS + CHARGER_CHARGING_STATUS + CHARGER_COMPLETED_STATUS:
            return False
        cable_id = self.config["ev_car"]["entity_ids"]["charge_cable_entity_id"]
        if self.is_entity_configured(cable_id):
            return self.get_state(cable_id, try_history=False, error_state="unknown") not in EV_UNPLUGGED_STATES
        return True

    def _battery_simulation_ceiling(self) -> float:
        """1 % under the car's charging limit (else the one CJP publishes, else 100 %)."""
        limit = None
        limit_id = self.config["ev_car"]["entity_ids"]["charging_limit_entity_id"]
        if self.is_entity_configured(limit_id):
            limit = self.get_state(limit_id, try_history=False, float_type=True, error_state=None)
        if limit is None:
            limit = self.own_state("charging_limit", float_type=True, error_state=None)
        return max(min(float(limit) if limit else 100.0, 100.0) - 1.0, 0.0)

    def publish_simulated_battery_level(self) -> None:
        """number.<prefix>_battery_level with a car integration: the simulated level the plan uses
        (the user can enter one), the car's own in the attributes."""
        if not self.battery_simulation_enabled:
            return
        key = "battery_level"
        real = self.battery_level(real=True)
        level = self.battery_level()
        sim = self.battery_simulation
        if sim:
            sim["published"] = level
        self.set_setting(key, level if real > 0.0 else None)
        self.set_attr(key, "real_battery_level", real if real > 0.0 else None)
        self.set_attr(key, "simulating", level != real)
        self.set_attr(key, "entered", bool(sim.get("manual")))
        self.set_attr(key, "added_percentage", round(level - real, 1))
        self.set_attr(key, "kwh_since_car_update", round(sim["kwh"], 2) if sim else 0.0)
        self.set_attr(key, "car_update_seen", sim["seen"].isoformat() if sim else None)

    def battery_range(self) -> float:
        distance = 0.0
        if not self.is_ev_configured():
            distance = self.distance_per_percentage() * self.battery_level()
        else:
            entity_id = self.config["ev_car"]["entity_ids"]["estimated_battery_range_entity_id"]
            try:
                if not self.is_entity_available(entity_id):
                    return distance
                distance = float(self.get_state(entity_id, float_type=True, error_state=0.0))
            except Exception as err:  # noqa: BLE001
                _LOGGER.error(err)
        return distance

    def range_to_battery_level(self, extraRange=None, batteryBuffer=None, date=None):  # noqa: N803 - names from ev.py
        min_range_in_battery_level = self.get_min_charge_limit_battery_level()
        extraRange = extraRange if extraRange is not None else self.get_entity_daily_distance(day_text=None, date=date)
        batteryBuffer = batteryBuffer if batteryBuffer is not None else self.get_min_daily_battery_level()
        try:
            if not isinstance(extraRange, (int, float)):
                raise ValueError(f"extraRange should be a number, but got: {extraRange}")
            min_range_in_battery_level = round_up(self.calc_distance_to_battery_level(extraRange)) + batteryBuffer
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error calculating battery level - extraRange: %s, batteryBuffer: %s: %s", extraRange, batteryBuffer, err)
        return min(min_range_in_battery_level, 100.0)

    def kwh_needed_for_charging(self, targetLevel=None, battery=None):  # noqa: N803
        targetLevel = targetLevel if targetLevel is not None else self.get_min_daily_battery_level()
        battery = battery if battery is not None else self.battery_level()
        return max(round(self.percentage_to_kwh(targetLevel - battery, include_charging_loss=True), 2), 0.0)

    def verify_charge_limit(self, limit):
        entity_id = self.config["ev_car"]["entity_ids"]["charging_limit_entity_id"]
        if not self.is_entity_configured(entity_id):
            return limit
        try:
            if self.get_integration(entity_id) in ("kia_uvo", "cupra_we_connect"):
                limit = float(round_up(limit / 10) * 10)
            limit = min(max(limit, self.get_min_charge_limit_battery_level()), 100.0)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error verifying charge limit: %s", err)
            limit = self.get_max_recommended_charge_limit_battery_level()
        return limit
