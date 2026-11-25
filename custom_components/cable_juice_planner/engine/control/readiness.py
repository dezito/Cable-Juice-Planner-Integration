"""Push notifications before a departure: the car will not be ready, or it is not plugged in for the planned charging.

The planner records a shortfall on the plan day when even every hour up to the departure is not enough
(record_departure_shortfall, called by scheduled_planner). The cron reads the stored plan, so a notice also goes out
when the plan hash skips the calculation.

While the plan charges, the cron also checks what the car does, so a plan that looks fine but that the car does not follow
is reported while there is still time to act: the car's own charge limit is below the need (the automation did not set
it), the car stops charging below the need, or it charges so much slower than planned that even every hour up to the
departure is not enough. The planner is not changed by these checks.

Each departure gets each notice once: the sent notices are kept in the runtime state and survive a restart.
"""
from __future__ import annotations

import datetime
import logging
import math

from ...const import (
    CHARGER_CHARGING_STATUS,
    CHARGER_COMPLETED_STATUS,
    CHARGER_NOT_READY_STATUS,
    CHARGER_READY_STATUS,
    ENTITY_UNAVAILABLE_STATES,
    EV_UNPLUGGED_STATES,
)
from ...utils.helpers import reset_time_to_hour, to_datetime
from ...utils.power import power_to_w
from .schedule import NOT_FOLLOWING_AFTER_TEST

_LOGGER = logging.getLogger(__name__)

# notified from this long before the departure: in time to act (from the day before a morning departure); the planner's
# shortfall also needs the prices of every hour up to the departure, so in practice from when tomorrow's prices are known
DEPARTURE_NOTICE_HOURS = 24
# the charging checks: a state must hold this long, and at least two cron intervals, before it is reported (the automation
# sets the car's limit, the car starts, a cloud status catches up)
HOLD_MINUTES = 10
# the car's limit without the car's own time (its last update entity): a cloud integration may show it this late
CAR_LIMIT_UNTIMED_MINUTES = 60
# the car's own time may be this far ahead of HA's clock; further ahead it is not known
CAR_CLOCK_SKEW_MINUTES = 5
# the slow charging check: the charging power over this many minutes, and below this share of the planned power
SLOW_MINUTES = 15
SLOW_RATIO = 0.8
# the plug-in reminder: the planned charging for the departure starts within this many minutes (or has started)
PLUG_IN_NOTICE_MINUTES = 60
# the plug-in reminder: unplugged at least this long (not a car that was just unplugged to leave early)
UNPLUGGED_MINUTES = 15
# a smaller shortfall is rounding or the cold weather margin, no reason to disturb
MIN_SHORTFALL_PERCENT = 2.0
# a sent notice is forgotten this long after its departure
KEEP_NOTICE = datetime.timedelta(days=1)

NOT_READY = "not_ready"
PLUG_IN = "plug_in"
CAR_LIMIT = "car_limit"
CAR_STOPPED = "car_stopped"
SLOW = "slow"
REASON_HOURS = "hours"
REASON_LIMIT = "limit"
WORK = "work"
TRIP = "trip"


def _for_departure(hour: dict) -> bool:
    """A planned charge for a work day or a trip (not a cheap or solar top-up)."""
    return any(value is True and (key == "trip" or key.endswith("workday_preparation")) for key, value in hour.items())


class ControlReadiness:
    """Notify when the car will not be ready for the next departure, or when it needs to be plugged in."""

    # ------------------------------------------------------------------ #
    # planner side
    # ------------------------------------------------------------------ #

    def record_departure_shortfall(self, ctx, day: int, departure: datetime.datetime, kwh_missing: float) -> None:
        """scheduled_planner: every hour up to the departure is used and kWh are still missing."""
        plan_day = ctx.charging_plan[day]
        if departure is None:
            return
        work_needed, trip_needed, both, trip_after_work = self._departure_needs(plan_day)
        before_work = sum(plan_day["battery_level_before_work"])
        if trip_after_work and before_work >= work_needed - 0.5:
            # a trip after the homecoming from work: the work day is covered, what is missing is the trip's
            departure = plan_day["trip_goto"]
        kind, set_departure = self._departure_of(plan_day, departure)
        # every hour up to the departure must have a price: hours without one are not planned, so "too few hours" would be
        # missing prices, not a fact. The notice waits for the prices
        hour = reset_time_to_hour(ctx.current_hour)
        while hour < reset_time_to_hour(departure):
            if hour not in ctx.grid_prices:
                _LOGGER.info("Departure %s: short of charging, but the price of %s is not known: no notice yet", departure, hour)
                return
            hour += datetime.timedelta(hours=1)
        cap = self._plan_cap(plan_day)
        # the charge limit is the reason when the need itself reaches it (work and a trip that is not after the homecoming
        # share one charge); otherwise the hours up to the departure ran out
        needed = [work_needed, trip_needed] + ([both] if both is not None else [])
        at_limit = any(level >= cap - 0.5 for level in needed)
        if not plan_day["trip"]:
            # a work day's level before the departure is the planned one (the Ready at departure analysis uses it too)
            at_limit = at_limit or before_work >= cap - 1.0
        plan_day["departure_shortfall"] = {
            "departure": departure,
            "set_departure": set_departure,
            "kind": kind,
            "missing": round(self.kwh_to_percentage(kwh_missing, include_charging_loss=True), 1),
            "reason": REASON_LIMIT if at_limit else REASON_HOURS,
            "limit": round(cap),
        }

    def _plan_cap(self, plan_day: dict) -> float:
        """The level the plan charges a plan day to at most: a trip may be charged above the max recommended charge limit
        (trip_battery_level_above_max)."""
        if plan_day.get("trip") and plan_day.get("trip_battery_level_above_max", 0.0) > 0.0:
            return 100.0
        return self.get_max_recommended_charge_limit_battery_level()

    def _virtual_level_not_entered(self, level: float) -> bool:
        """A car without its integration charges from the level 0 of an unplugged car: no level was entered, the level is
        only what was counted since (the anchor's base is 0 and the level is still the counted one)."""
        anchor = getattr(self, "virtual_charge_anchor", None)
        return isinstance(anchor, dict) and anchor.get("base", 0.0) <= 0.0 and abs(level - anchor.get("level", 0.0)) <= 0.5

    def _departure_needs(self, plan_day: dict) -> tuple[float, float, float | None, bool]:
        """The levels a plan day needs: (work, trip, both, trip after work). both: work and a trip that is not after the
        homecoming share one charge (None otherwise); a trip after the homecoming is charged for in between."""
        min_daily = self.get_min_daily_battery_level()
        workday, trip = bool(plan_day.get("workday")), bool(plan_day.get("trip"))
        work_needed = min(plan_day.get("work_battery_level_needed", 0.0) + min_daily, 100.0) if workday else 0.0
        trip_needed = plan_day.get("trip_battery_level_needed", 0.0) + plan_day.get("trip_battery_level_above_max", 0.0) if trip else 0.0
        trip_after_work = workday and trip and plan_day["trip_goto"] > plan_day["work_homecoming"] > plan_day["work_goto"]
        both = work_needed + trip_needed - min_daily if workday and trip and not trip_after_work else None
        return work_needed, trip_needed, both, trip_after_work

    def _needed_level(self, plan_day: dict, kind: str) -> float:
        """The level the departure needs (the charging checks)."""
        work_needed, trip_needed, both, _ = self._departure_needs(plan_day)
        if both is not None:
            return min(both, 100.0)
        return min(work_needed if kind == WORK else trip_needed, 100.0)

    @staticmethod
    def _departure_of(plan_day: dict, departure: datetime.datetime) -> tuple[str, datetime.datetime]:
        """Work or trip, and the departure as set: work_goto and trip_goto are moved later for a car still plugged in after it."""
        if plan_day.get("workday") and departure == plan_day.get("work_goto"):
            return WORK, plan_day.get("work_departure") or departure
        if plan_day.get("trip") and departure == plan_day.get("trip_goto"):
            return TRIP, plan_day.get("trip_departure") or departure
        return WORK if plan_day.get("workday") else TRIP, departure

    # ------------------------------------------------------------------ #
    # cron side
    # ------------------------------------------------------------------ #

    def notify_departure_readiness(self) -> None:
        """Every cron (charge_trigger): never lets a failure here stop the rest of the cron."""
        try:
            self._notify_departure_readiness()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Departure readiness notification failed")

    def _notify_departure_readiness(self) -> None:
        now = self.now()
        self._forget_departure_notices(now)
        if not self.config.get("notification", {}).get("departure_readiness", True):
            self._readiness_seen.clear()
            self._plug_in_seen.clear()
            return
        # the level the plan uses: an unknown level is planned as 0 %, and a car without its integration has the level 0
        # while unplugged, counted up from there while it charges when no level was entered. There is no real level to
        # judge from, and a notice would be wrong
        level = self.battery_level(unknown=None)
        if level is None or (not self.is_ev_configured() and (level <= 0.0 or self._virtual_level_not_entered(level))):
            self._readiness_seen.clear()
            self._plug_in_seen.clear()
            return
        plan = getattr(self, "charging_plan", None) or {}
        days = [plan[day] for day in sorted(key for key in plan if isinstance(key, int)) if isinstance(plan[day], dict)]
        horizon = now + datetime.timedelta(hours=DEPARTURE_NOTICE_HOURS)
        shortfalls = [
            shortfall for day in days
            if isinstance(shortfall := day.get("departure_shortfall"), dict)
            and now < shortfall["set_departure"] <= horizon and shortfall["missing"] >= MIN_SHORTFALL_PERCENT
        ]
        for shortfall in sorted(shortfalls, key=lambda shortfall: shortfall["set_departure"]):
            self._notify_not_ready(now, shortfall)
        departures = self._departures(now, days, horizon)
        self._notify_plug_in(now, departures)
        self._notify_while_charging(now, days, departures, level)
        self._notify_car_limit_at_plug_in(now, days, departures, level)

    def _departures(self, now: datetime.datetime, days: list[dict], horizon: datetime.datetime) -> list[tuple[dict, str, datetime.datetime]]:
        """(plan day, kind, departure as set) of the departures after now and within the horizon, the first first."""
        found = []
        for plan_day in days:
            if plan_day.get("workday") and plan_day.get("work_goto"):
                found.append((plan_day, *self._departure_of(plan_day, plan_day["work_goto"])))
            if plan_day.get("trip") and plan_day.get("trip_goto"):
                found.append((plan_day, *self._departure_of(plan_day, plan_day["trip_goto"])))
        return sorted(
            (item for item in found if isinstance(item[2], datetime.datetime) and now < item[2] <= horizon),
            key=lambda item: item[2],
        )

    def _notify_not_ready(self, now: datetime.datetime, shortfall: dict) -> None:
        departure = shortfall["set_departure"]
        if not self._new_departure_notice(NOT_READY, shortfall["kind"], departure):
            return
        t = self.t
        plan_action = {"action": "URI", "title": t("ui.notify_departure.action_plan"), "uri": f"entityId:{self.own_entity_id('sensor', 'current_charging_rule')}"}
        if shortfall["reason"] == REASON_LIMIT:
            reason = t("ui.notify_departure.reason_limit", limit=shortfall["limit"])
            # at 100 % (a trip above the max recommended limit) there is no limit left to raise
            action = plan_action if shortfall["limit"] >= 100 else {
                "action": "URI", "title": t("ui.notify_departure.action_limit"),
                "uri": f"entityId:{self.own_entity_id('number', 'max_recommended_charge_limit_battery_level')}",
            }
        else:
            reason = t("ui.notify_departure.reason_hours")
            action = plan_action
        message = t("ui.notify_departure.not_ready", departure=self._departure_text(now, departure), missing=round(shortfall["missing"]))
        _LOGGER.info("Departure %s: not ready, %s %% missing (%s)", departure, shortfall["missing"], shortfall["reason"])
        self.my_notify(f"{message}\n{reason}", title=f"{self.title} {t('ui.notify_departure.title_not_ready')}", data={"actions": [action]}, always=True)

    def _notify_plug_in(self, now: datetime.datetime, departures: list[tuple[dict, str, datetime.datetime]]) -> None:
        if not departures:
            return
        _, kind, departure = departures[0]
        soon = now + datetime.timedelta(minutes=PLUG_IN_NOTICE_MINUTES)
        hours = getattr(self, "charge_hours", None) or {}
        starts = sorted(
            timestamp for timestamp, hour in hours.items()
            if isinstance(timestamp, datetime.datetime) and isinstance(hour, dict) and hour.get("kWh", 0.0) > 0.0
            # not yet over (a slot is at most an hour), starts soon and is before the departure
            and timestamp + datetime.timedelta(hours=1) > now and timestamp <= soon and timestamp < departure
            and _for_departure(hour)
        )
        if not starts or not self._unplugged_at_home(now):
            return
        if not self._new_departure_notice(PLUG_IN, kind, departure):
            return
        t = self.t
        start = max(starts[0], now)
        message = t("ui.notify_departure.plug_in", departure=self._departure_text(now, departure), start=start.strftime("%H:%M"))
        action = {"action": "URI", "title": t("ui.notify_departure.action_plan"), "uri": f"entityId:{self.own_entity_id('sensor', 'current_charging_rule')}"}
        _LOGGER.info("Departure %s: charging planned from %s, the car is not plugged in", departure, starts[0])
        self.my_notify(message, title=f"{self.title} {t('ui.notify_departure.title_plug_in')}", data={"actions": [action]}, always=True)

    def _unplugged_at_home(self, now: datetime.datetime) -> bool:
        """Home, and the charger (or the car) says unplugged now and for the last UNPLUGGED_MINUTES.

        Only the state now counts, not the history's last state for an unavailable one; unknown or unavailable is no
        unplug, so no reminder. A car plugged in within the last minutes was just unplugged, maybe to leave early.
        """
        if self.other_ev_connected() or not self.is_ev_home():
            return False
        charger = self.config["charger"]["entity_ids"]
        car = self.config["ev_car"]["entity_ids"]
        sources = (
            (charger["status_entity_id"], CHARGER_NOT_READY_STATUS),
            (charger["cable_connected_entity_id"], EV_UNPLUGGED_STATES),
            (car["charge_cable_entity_id"], EV_UNPLUGGED_STATES),
            (car["charge_port_door_entity_id"], EV_UNPLUGGED_STATES),
        )
        for entity_id, unplugged in sources:
            if not self.is_entity_configured(entity_id):
                continue
            state = self.get_state(entity_id, try_history=False, error_state="unknown")
            if state in ENTITY_UNAVAILABLE_STATES or state not in unplugged:
                return False
            # the history has the state at the start of the window too: none at all is unknown, no reminder
            recent = self.history.get_values(entity_id, now - datetime.timedelta(minutes=UNPLUGGED_MINUTES), now, include_timestamps=False, error_state=[])
            return bool(recent) and all(value in unplugged for value in recent)
        return False

    # ------------------------------------------------------------------ #
    # while the plan charges: the car does not follow it
    # ------------------------------------------------------------------ #

    def _notify_while_charging(self, now: datetime.datetime, days: list[dict], departures: list[tuple[dict, str, datetime.datetime]], level: float) -> None:
        """A planned slot before the next departure charges now, the car is below what the departure needs, and the car
        does not follow the plan. The car's limit and a car that stops need its integration; slow charging is also checked
        for a car without one (its level is counted from the charger meter)."""
        if not departures or not self._plan_in_charge() or self._after_a_set_departure(now, days):
            self._readiness_seen.clear()
            return
        plan_day, kind, departure = departures[0]
        need = self._needed_level(plan_day, kind)
        if level >= need - MIN_SHORTFALL_PERCENT:
            self._readiness_seen.clear()
            return
        slot = self._planned_slot(departure)
        if slot is None or self.charger_action != "charge":
            # the waits start again in the next planned slot: the automation sends the limit again for a new charge
            self._readiness_seen.clear()
            return
        self._check_car_limit(now, kind, departure, need)
        self._check_car_stopped(now, kind, departure, need, level)
        self._check_slow(now, slot, kind, departure, need, level)

    def _plan_in_charge(self) -> bool:
        """Not while a restart of the charger runs or gave up (it notifies itself), nor the test of the automation, the
        charging loss measurement, manual charging, no charging modes or another car on the charger: not the plan. Nor in
        the hour after the test released the charger: it set the car's limit itself, and the car may still follow it."""
        released = self._minutes_since(self._charger_test_released_at)
        return not (
            self.restarting_charger or self.restarting_charger_count > 0 or self._charger_restart_gave_up
            or (released is not None and round(released) < NOT_FOLLOWING_AFTER_TEST)
            or self.charger_hold_active() or self.is_calculating_charging_loss() or self.no_charging_modes_active()
            or self.manual_charging_enabled() or self.manual_charging_solar_enabled() or self.other_ev_connected()
        )

    def _after_a_set_departure(self, now: datetime.datetime, days: list[dict]) -> bool:
        """After a departure as set, while its time is moved later for a car still plugged in: the charging then is for
        that departure, not the next one."""
        for plan_day in days:
            for flag, goto in (("workday", "work_goto"), ("trip", "trip_goto")):
                if plan_day.get(flag) and isinstance(plan_day.get(goto), datetime.datetime):
                    _, set_departure = self._departure_of(plan_day, plan_day[goto])
                    if isinstance(set_departure, datetime.datetime) and set_departure <= now < plan_day[goto]:
                        return True
        return False

    def _planned_slot(self, departure: datetime.datetime) -> datetime.datetime | None:
        """The planned slot of now (any planned charging: it charges at full power), if it is before the departure."""
        slot = self.current_hour_in_charge_hours()
        hour = (getattr(self, "charge_hours", None) or {}).get(slot) if isinstance(slot, datetime.datetime) else None
        if not isinstance(hour, dict) or hour.get("kWh", 0.0) <= 0.0 or slot >= departure:
            return None
        return slot

    def _seen_minutes(self, name: str, store: dict | None = None) -> float | None:
        seen = (self._readiness_seen if store is None else store).get(name)
        return self._minutes_since(seen[1]) if seen is not None else None

    def _car_report_minutes(self) -> float | None:
        """Minutes since the car itself last reported: its last update entity (the car's own time). None without one. Not
        the time HA last got the state: a cloud integration writes its cached values again on every poll."""
        entity_id = self.config["ev_car"]["entity_ids"]["last_update_entity_id"]
        if not self.is_entity_configured(entity_id):
            return None
        state = self.get_state(entity_id, try_history=False, error_state=None)
        if state in ENTITY_UNAVAILABLE_STATES:
            return None
        moment = to_datetime(str(state), self._snap.tz if self._snap is not None else None)
        minutes = self._minutes_since(moment) if isinstance(moment, datetime.datetime) else None
        # a time in the future (the car's clock, or a time zone read wrong) is not a report now: not known
        if minutes is None or minutes < -CAR_CLOCK_SKEW_MINUTES:
            return None
        return max(minutes, 0.0)

    def _level_change_minutes(self, entity_id: str) -> float | None:
        """Minutes since the car's level last changed in the history: without the car's own time only a new value shows
        that the car reported. None when it has not changed in the history kept."""
        previous = changed = None
        for timestamp, value in self.history.buffers.get(entity_id) or []:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(number):  # "nan" and "inf" are a broken value, not a report
                continue
            if previous is not None and number != previous:
                changed = timestamp
            previous = number
        if changed is None:
            return None
        return max((self.history._ts(self.now()) - changed) / 60.0, 0.0)

    def _level_minutes_old(self) -> float | None:
        """How old the car's battery level is: the minutes since the car reported (its own time), else since the level
        last changed. None when the level is no value now (the plan then uses the last one from the history) or its age
        is not known: then the level can't be judged. The simulation's level is an estimate too: not counted as new. A
        car without its integration: 0, CJP counts its level from the charger meter every cron."""
        if not self.is_ev_configured():
            return 0.0
        entity_id = self.config["ev_car"]["entity_ids"]["usable_battery_level_entity_id"]
        level = self.get_state(entity_id, try_history=False, float_type=True, error_state=None)
        if not isinstance(level, float) or not math.isfinite(level):
            return None
        minutes = self._car_report_minutes()
        return minutes if minutes is not None else self._level_change_minutes(entity_id)

    def _held(self, name: str, key, store: dict | None = None) -> bool:
        """The same state (key) seen for at least two cron intervals, and HOLD_MINUTES. Kept in memory only: a restart
        starts the wait again."""
        store = self._readiness_seen if store is None else store
        seen = store.get(name)
        if seen is None or seen[0] != key:
            store[name] = (key, self.now())
            return False
        minutes = self._minutes_since(seen[1])
        return minutes is not None and round(minutes) >= max(2 * self.config["cron_interval"], HOLD_MINUTES)

    def _car_limit(self) -> float | None:
        """The car's own charge limit now; None when it is not configured or not known."""
        entity_id = self.config["ev_car"]["entity_ids"]["charging_limit_entity_id"]
        if not self.is_ev_configured() or not self.is_entity_configured(entity_id):
            return None
        value = self.get_state(entity_id, float_type=True, try_history=False, error_state=None)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and 0.0 < value <= 100.0 else None

    def _published_limit(self) -> float | None:
        """The charge limit CJP asks the car for (sensor.<prefix>_charging_limit, set by the automation in the car)."""
        value = self.sensor_states.get("charging_limit")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def _check_car_limit(self, now: datetime.datetime, kind: str, departure: datetime.datetime, need: float) -> None:
        """The car did not take the limit CJP asks for, and its own limit is below the need: it will stop there. A cloud
        integration shows the car's limit late: the car must have reported (its own time) after CJP's limit was first
        seen with it, or without the car's time the state must hold CAR_LIMIT_UNTIMED_MINUTES."""
        limit, published = self._car_limit(), self._published_limit()
        if limit is None or published is None or limit >= published - 0.5 or limit >= need - MIN_SHORTFALL_PERCENT:
            self._readiness_seen.pop(CAR_LIMIT, None)
            return
        if self._held(CAR_LIMIT, (departure, published, limit)) and self._limit_reported(CAR_LIMIT):
            self._notify_car_limit(now, kind, departure, limit, need)

    def _limit_reported(self, name: str, store: dict | None = None) -> bool:
        """The car's limit seen low by the wait name is the car's own: the car reported (its own time) after it was first
        seen, or without the car's time the low limit has held for CAR_LIMIT_UNTIMED_MINUTES."""
        reported, seen = self._car_report_minutes(), self._seen_minutes(name, store)
        if seen is None:
            return False
        return (reported is not None and reported < seen) or (reported is None and round(seen) >= CAR_LIMIT_UNTIMED_MINUTES)

    def _notify_car_limit_at_plug_in(self, now: datetime.datetime, days: list[dict], departures: list[tuple[dict, str, datetime.datetime]], level: float) -> None:
        """Plugged in for the next departure, and the charger automation has no step that sets the car's charge limit
        (read from automations.yaml; not known is no notice): a car's own limit under the need is reported now, not when
        the planned charging starts in the night. The same notice as while charging (once per departure). The need is at
        most what the plan charges to (the max recommended charge limit): above it, raising the car's limit does not help,
        and the planner's not ready notice says so."""
        store = self._plug_in_seen
        limit = self._car_limit()
        departure = departures[0][2] if departures else None
        need = min(self._needed_level(departures[0][0], departures[0][1]), self._plan_cap(departures[0][0])) if departures else 100.0
        status = self.get_state(self.config["charger"]["entity_ids"]["status_entity_id"], try_history=False, error_state="unavailable")
        hours = getattr(self, "charge_hours", None) or {}
        planned = departure is not None and any(
            isinstance(timestamp, datetime.datetime) and isinstance(hour, dict) and hour.get("kWh", 0.0) > 0.0
            and timestamp + datetime.timedelta(hours=1) > now and timestamp < departure and _for_departure(hour)
            for timestamp, hour in hours.items()
        )
        if (
            getattr(self._snap, "car_limit_steps", None) is not False or limit is None or not planned
            or status not in CHARGER_READY_STATUS + CHARGER_CHARGING_STATUS + CHARGER_COMPLETED_STATUS
            or not self.is_ev_home() or not self._plan_in_charge() or self._after_a_set_departure(now, days)
            or level >= need - MIN_SHORTFALL_PERCENT or limit >= need - MIN_SHORTFALL_PERCENT
        ):
            store.clear()
            return
        if self._held(CAR_LIMIT, (departure, limit), store) and self._limit_reported(CAR_LIMIT, store):
            self._notify_car_limit(now, departures[0][1], departure, limit, need)

    def _check_car_stopped(self, now: datetime.datetime, kind: str, departure: datetime.datetime, need: float, level: float) -> None:
        """The charger says the car is done, below the need and below the limit the plan asks for now (a car that stops at
        the limit set for this hour is not stopped: a later hour asks for more). The level must be newer than the stop: a
        cloud car reports late, and its last level is from before it got there. Not for a car without its integration: its
        level is an estimate, and a car that says done is more likely full than the estimate right."""
        status = self.get_state(self.config["charger"]["entity_ids"]["status_entity_id"], try_history=False, error_state="unavailable")
        published = self._published_limit()
        target = min(need, published) if published is not None else need
        if not self.is_ev_configured() or status not in CHARGER_COMPLETED_STATUS or level >= target - MIN_SHORTFALL_PERCENT:
            self._readiness_seen.pop(CAR_STOPPED, None)
            return
        if not self._held(CAR_STOPPED, departure):
            return
        old, seen = self._level_minutes_old(), self._seen_minutes(CAR_STOPPED)
        if old is None or seen is None or old >= seen:
            return
        limit = self._car_limit()
        if limit is not None and limit < need - MIN_SHORTFALL_PERCENT and level >= limit - 1.0:
            # stopped at its own limit
            self._notify_car_limit(now, kind, departure, limit, need)
        else:
            self._notify_car_stopped(now, kind, departure, level, need)

    def _check_slow(self, now: datetime.datetime, slot: datetime.datetime, kind: str, departure: datetime.datetime, need: float, level: float) -> None:
        """Charging well below the planned power, so slow that even every hour up to the departure at this power is not
        enough (slower but in time is no notice: the plan adds hours itself as the level falls behind). The power now and
        over the last SLOW_MINUTES must both be low, with the status charging all that time. The planned power is at most
        what the site allows now (load balancing is no slow car). The level is counted up for the time since the car
        reported it, at the higher power: an old level never makes the shortfall larger."""
        charger = self.config["charger"]["entity_ids"]
        power_id, status_id = charger["charger_power_entity_id"], charger["status_entity_id"]
        start = now - datetime.timedelta(minutes=SLOW_MINUTES)
        status = self.get_state(status_id, try_history=False, error_state="unavailable")
        statuses = self.history.get_values(status_id, start, now, error_state=[]) if status in CHARGER_CHARGING_STATUS else []
        readable = self.is_entity_configured(power_id) and self._charger_power_readable(power_id)
        state = self.get_state(power_id, float_type=True, try_history=False, error_state=None) if readable else None
        current = power_to_w(state, self.history.units.get(power_id)) if state is not None else None
        average = self.history.get_average_value(power_id, start, now, convert_to="W", error_state=None) if current is not None else None
        missing = 0.0
        actual_kw = planned_kw = 0.0
        old = self._level_minutes_old()
        until = self._minutes_since(departure)  # across a change of the clock
        if statuses and all(value in CHARGER_CHARGING_STATUS for value in statuses) and average is not None and old is not None and until is not None:
            actual_kw = max(abs(current), abs(float(average))) / 1000.0
            planned_kw = self.period_charge_capacity(slot, level, departure)[1]
            allowed_kw = sum(self.current_charging_amps) * self.config["charger"]["power_voltage"] / 1000.0
            if allowed_kw > 0.0:
                planned_kw = min(planned_kw, allowed_kw)
            if planned_kw > 0.0 and actual_kw < SLOW_RATIO * planned_kw:
                real = self.battery_level(real=True, unknown=None) or 0.0
                counted = real + self.kwh_to_percentage(max(planned_kw, actual_kw) * old / 60.0, include_charging_loss=True)
                level = min(max(level, counted), 100.0)
                hours_left = max(-until / 60.0, 0.0)
                missing_kwh = self.kwh_needed_for_charging(need, level) - actual_kw * hours_left
                missing = self.kwh_to_percentage(max(missing_kwh, 0.0), include_charging_loss=True)
        if missing < MIN_SHORTFALL_PERCENT:
            self._readiness_seen.pop(SLOW, None)
            return
        if self._held(SLOW, departure):
            self._notify_slow(now, kind, departure, actual_kw, planned_kw, missing)

    def _notify_car_limit(self, now: datetime.datetime, kind: str, departure: datetime.datetime, limit: float, need: float) -> None:
        entity_id = self.config["ev_car"]["entity_ids"]["charging_limit_entity_id"]
        action = {"action": "URI", "title": self.t("ui.notify_departure.action_car_limit"), "uri": f"entityId:{entity_id}"}
        message = self.t("ui.notify_departure.car_limit", departure=self._departure_text(now, departure), limit=round(limit), needed=round(need))
        if getattr(self._snap, "car_limit_steps", None) is False:
            # what to do about it: the automation can't set the car's limit
            message = f"{message}\n{self.t('ui.notify_departure.no_limit_steps')}"
        self._send_charging_notice(CAR_LIMIT, kind, departure, message, action)

    def _notify_car_stopped(self, now: datetime.datetime, kind: str, departure: datetime.datetime, level: float, need: float) -> None:
        message = self.t("ui.notify_departure.car_stopped", departure=self._departure_text(now, departure), level=round(level), needed=round(need))
        self._send_charging_notice(CAR_STOPPED, kind, departure, message)

    def _notify_slow(self, now: datetime.datetime, kind: str, departure: datetime.datetime, actual_kw: float, planned_kw: float, missing: float) -> None:
        message = self.t(
            "ui.notify_departure.slow", departure=self._departure_text(now, departure),
            actual=f"{actual_kw:.1f}", planned=f"{planned_kw:.1f}", missing=round(missing),
        )
        self._send_charging_notice(SLOW, kind, departure, message)

    def _send_charging_notice(self, notice: str, kind: str, departure: datetime.datetime, message: str, action: dict | None = None) -> None:
        if not self._new_departure_notice(notice, kind, departure):
            return
        t = self.t
        action = action or {"action": "URI", "title": t("ui.notify_departure.action_plan"), "uri": f"entityId:{self.own_entity_id('sensor', 'current_charging_rule')}"}
        _LOGGER.info("Departure %s: %s", departure, message)
        self.my_notify(message, title=f"{self.title} {t('ui.notify_departure.title_not_ready')}", data={"actions": [action]}, always=True)

    def _departure_text(self, now: datetime.datetime, departure: datetime.datetime) -> str:
        key = "today" if departure.date() == now.date() else "tomorrow" if departure.date() == now.date() + datetime.timedelta(days=1) else "later"
        return self.t(f"ui.notify_departure.{key}", time=departure.strftime("%H:%M"), date=departure.strftime("%d/%m"))

    # ------------------------------------------------------------------ #
    # sent notices (runtime state)
    # ------------------------------------------------------------------ #

    def _new_departure_notice(self, notice: str, kind: str, departure: datetime.datetime) -> bool:
        """True once per notice and departure; remembered in the runtime state. A work day by its date, not the time: the
        learned departure can give way to the entered one during the day, and that is the same departure. A trip by its
        time as set: a second trip the same day is another departure."""
        key = f"{notice} {kind} {departure.date().isoformat() if kind == WORK else departure.isoformat()}"
        if key in self.departure_notices:
            return False
        self.departure_notices[key] = departure.isoformat()
        self.mark_dirty("runtime")
        return True

    def _forget_departure_notices(self, now: datetime.datetime) -> None:
        kept = {}
        for key, departure in self.departure_notices.items():
            try:
                if datetime.datetime.fromisoformat(departure) + KEEP_NOTICE > now:
                    kept[key] = departure
            except (TypeError, ValueError):
                continue
        if kept != self.departure_notices:
            self.departure_notices = kept
            self.mark_dirty("runtime")

    def restore_departure_notices(self, saved) -> None:
        self.departure_notices = {key: value for key, value in (saved or {}).items() if isinstance(key, str) and isinstance(value, str)} if isinstance(saved, dict) else {}
