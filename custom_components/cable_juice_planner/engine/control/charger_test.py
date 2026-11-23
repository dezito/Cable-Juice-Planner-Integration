"""The test of the user's own charger automation: does the charger do what the amps and phases of the plan ask for?

CJP never sends a command; the user's automation (the charger blueprint) does, when the action sensor and its phase
attributes change. To test it CJP publishes a chosen current on one or three phases on ``sensor.<prefix>_charger_action``,
on purpose, and the coordinator reads the charger's power sensor to see what the charger then does. Only the published
action and amps are held, and with a charge limit entity the published charge limit for a moment (the car's own is put
back after it): the plan, the settings and the rules are not touched.

* The steps are the maximum first (does charging work at all), then the lowest and highest steps of the solar calculation
  (``charging_power_dict``). A step that fails is searched two amps at a time towards the middle of its group and then
  one amp back (next_amps), so the result is the current closest to the step that works.
* Power that does not rise with more amps is the car's or the charger's limit (power_is_capped). A car that charges on
  fewer phases than asked is judged on its own phases (car_phases_of), once two currents at clearly other powers matched
  them (a car that limits its power can match fewer phases at one current). When the power was the same at every current
  of every step, no step proves that the amps are set (amps_unproven), and the steps that "worked" are not counted.
* Power clearly above what was published ends the test at once: the load balancing counts on the published amps.
* Without a way to set the current (no steps for the current in the blueprint, or no solar steps) only the maximum
  and the stop are tried.
* The hold is engine state that every path publishing the action reads: the cron, a forced calculation, a changed
  setting and the load balancing. It is dropped by the first publish after hold seconds (a lost runner can not leave the
  charger at a forced current), and the action, enabled and amps from before the test are put back. Load balancing
  still limits the amps (a limited step is not tested, never pushed past the fuse), and a held charge the site has no
  current for is published as stop (site_stop, set by the load balancing in actions.py).
"""
from __future__ import annotations

import datetime
import logging

from ...utils.helpers import round_up
from ...const import CHARGER_CHARGING_STATUS, CHARGER_COMPLETED_STATUS, CHARGER_NOT_READY_STATUS, CHARGER_READY_STATUS, ENTITY_UNAVAILABLE_STATES

_LOGGER = logging.getLogger(__name__)

# seconds
HOLD_MARGIN_SECONDS = 120
# at most this many currents are tried after the step's own before the step is given up
MAX_ADJUSTMENTS = 4
# W
TOLERANCE_SHARE = 0.05  # one amp is 6 % of 16 A, so a step that is one amp off is seen; the voltage setting must be right
TOLERANCE_MIN_W = 150.0
STOPPED_W = 100.0
MOVED_W = 1000.0  # the charger must have charged at least this before a stop proves anything
# the reasons a test ends early (ui.charger_test.stop_reason.*) and a step is not tested (ui.charger_test.skip.*)
STOP_REASONS = (
    "not_configured", "unavailable", "not_connected", "completed", "not_ready", "other_ev", "running", "stopped", "no_charging",
    "overdraw", "internal_error",
)
SKIP_REASONS = ("no_steps", "no_data", "site_limited", "no_baseline", "not_ready", "not_reached", "amps_unproven", "no_limit", "limit_no_data")
# the charge limit step asks the car for a limit this much from its own
LIMIT_STEP = 10.0
# more than the published amps give by this much is a charger that ignores the amps or the phases (load balancing risk)
OVERDRAW_SHARE = 0.25
OVERDRAW_MIN_W = 500.0
# the voltage the charger really charges at, from the power per amp (the grid's voltage under the load, not the setting)
VOLTAGE_SPREAD = 0.02  # every current's power per amp within this share: a charger an amp short drifts more over the span
VOLTAGE_SPAN = 1.5  # the highest current at least this many times the lowest: close currents can't tell an amp short
VOLTAGE_BAND = (0.85, 1.10)  # the share of the setting a measured voltage may be (else fewer phases, a limit, a wrong sensor)
# the direction each charge step searches in (build_steps)
STEP_SEARCH = {"max": -1, "single_max": -1, "min": 1, "single_min": 1}


def amps_list(amps: float, phases: int) -> list[int]:
    """The amps per phase of a current on phases phases (phase 1 first, like calc_charging_amps)."""
    return [int(amps) if index < phases else 0 for index in range(3)]


def tolerance_w(expected_w: float) -> float:
    return max(TOLERANCE_SHARE * expected_w, TOLERANCE_MIN_W)


def judge_power(expected_w: float, measured_w: float) -> bool:
    """Whether the charger delivered the power of the amps asked for (the voltage is configured, not measured)."""
    return abs(measured_w - expected_w) <= tolerance_w(expected_w)


def overdraw_w(published_w: float) -> float:
    """Power above this is clearly more than the published amps give."""
    return published_w + max(OVERDRAW_SHARE * published_w, OVERDRAW_MIN_W)


def car_phases_of(amps: int, phases: int, voltage: float, measured_w: float) -> int | None:
    """The number of phases the car charges on when it is fewer than phases and the power matches the amps on them."""
    for car_phases in range(phases - 1, 0, -1):
        if judge_power(amps * car_phases * voltage, measured_w):
            return car_phases
    return None


def power_is_capped(direction: int, attempts: list[dict], best: dict | None, threshold_w: float) -> bool:
    """Whether the power stopped rising with the amps: the car (a full or cold battery) or the charger limits it.

    With a current that worked (best): every higher current that failed gave the same power as best (a higher current that
    gives less, or nothing, is one the charger did not take: not a limit). Without one: the car charges (threshold_w), but
    at the same power whatever the amps. attempts: {"amps", "watt", "ok"}."""
    if best is not None:
        higher = [attempt for attempt in attempts if not attempt["ok"] and attempt["amps"] > best["amps"]]
        return direction < 0 and bool(higher) and all(abs(attempt["watt"] - best["watt"]) <= tolerance_w(best["watt"]) for attempt in higher)
    watts = [attempt["watt"] for attempt in attempts]
    return len(watts) > 1 and min(watts) >= threshold_w and max(watts) - min(watts) <= tolerance_w(max(watts))


def amps_unproven(steps: list[dict], voltage: float, threshold_w: float) -> bool:
    """Whether no step shows that the power follows the amps: within every number of phases that was published, every
    current that charged gave the same power, although different currents were asked for. A charger stuck at one current
    (an automation that never sets it) then looks like one that works at the step that happens to match, and like a car
    that limits the power at the others. It is judged per number of phases: a charger that switches the phases but not
    the current gives other powers on one and on three phases. One current asked for proves nothing either way."""
    groups: dict[int, list[tuple[int, float]]] = {}
    for step in steps:
        if step["name"] == "stop" or step["status"] == "skip":
            continue
        for attempt in step.get("attempts") or []:
            if attempt["watt"] >= threshold_w:
                groups.setdefault(int(step["phases"]), []).append((attempt["amps"], attempt["watt"]))
    judged = [group for group in groups.values() if len({amps for amps, _ in group}) >= 2]
    if not judged:
        return False
    return all(max(w for _, w in group) - min(w for _, w in group) <= tolerance_w(max(w for _, w in group)) for group in judged)


def measured_voltage(attempts: list[dict], phases: int, voltage: float, threshold_w: float) -> float | None:
    """The voltage per phase the charger charged at, when the power followed every current at one voltage; else None.

    Only the attempts that charged (threshold_w) count. They must span currents far apart (VOLTAGE_SPAN) with the same
    power per amp (VOLTAGE_SPREAD): a charger that gives an amp less than it is asked for has about the same power per amp
    at two close currents, but not over a wide span. A car that limits its power has another power per amp at every
    current. A voltage far from the setting (VOLTAGE_BAND) is no grid voltage but fewer phases or a sensor in other units.
    voltage: the voltage setting."""
    charged = [attempt for attempt in attempts if attempt["amps"] > 0 and attempt["watt"] >= threshold_w]
    amps = [attempt["amps"] for attempt in charged]
    if phases < 1 or len(set(amps)) < 2 or max(amps) < VOLTAGE_SPAN * min(amps):
        return None
    per_amp = [attempt["watt"] / (attempt["amps"] * phases) for attempt in charged]
    if max(per_amp) - min(per_amp) > VOLTAGE_SPREAD * max(per_amp):
        return None
    measured = sum(per_amp) / len(per_amp)
    if not VOLTAGE_BAND[0] * voltage <= measured <= VOLTAGE_BAND[1] * voltage:
        return None
    return round(measured, 1)


def judge_at_measured_voltage(steps: list[dict], voltage: float, threshold_w: float) -> list[dict]:
    """The charge steps judged again at the voltage the charger really charged at, per number of phases.

    The power is compared with amps x the voltage setting, so a setting that is not the grid's voltage (240 V on a 230 V
    grid) fails currents that the charger took exactly, and the search ends on a lower one that happens to be within the
    tolerance. When the attempts of every step on that number of phases give one voltage (measured_voltage) that is more
    than VOLTAGE_SPREAD from the setting, each such step is judged again at it: its result is the current closest to the
    step that matches, with "voltage" (the measured) and "voltage_setting". Steps that were not run, an overdraw, a stop
    and the charge limit are kept; so is a step none of whose currents match. Returns a new list; steps judged before are
    judged the same again (from the attempts' power, not their earlier verdict)."""

    def charge(step: dict) -> bool:
        return step.get("name") in STEP_SEARCH and step.get("status") != "skip" and step.get("reason") != "overdraw"

    def phases_of(step: dict) -> int:
        return int(step.get("judged_phases") or step.get("phases") or 0)

    groups: dict[int, list[dict]] = {}
    for step in steps:
        if charge(step):
            groups.setdefault(phases_of(step), []).extend(step.get("attempts") or [])
    voltages: dict[int, float] = {}
    for phases, attempts in groups.items():
        measured = measured_voltage(attempts, phases, voltage, threshold_w)
        if measured is not None and abs(measured - voltage) > VOLTAGE_SPREAD * voltage:
            voltages[phases] = measured
    judged = []
    for step in steps:
        measured = voltages.get(phases_of(step)) if charge(step) else None
        if measured is None:
            judged.append(step)
            continue
        phases = phases_of(step)
        attempts = [{**attempt, "ok": judge_power(attempt["amps"] * phases * measured, attempt["watt"])} for attempt in step.get("attempts") or []]
        working = [attempt for attempt in attempts if attempt["ok"]]
        if not working:
            judged.append(step)
            continue
        search = STEP_SEARCH[step["name"]]
        best = (min if search > 0 else max)(working, key=lambda attempt: attempt["amps"])
        own = best["amps"] == step.get("configured_amps") and not step.get("already_charging")
        judged.append({
            **step, "attempts": attempts, "status": "ok" if own else "adjusted", "amps": best["amps"], "watt": best["watt"],
            "expected_w": round(best["amps"] * phases * measured), "limited": power_is_capped(search, attempts, best, threshold_w),
            "voltage": measured, "voltage_setting": voltage,
        })
    return judged


def limit_test_value(
    current: float, battery: float | None, low: float, high: float, rounding, published: float | None = None, own: float | None = None
) -> float | None:
    """The charge limit the test asks the car for, None when there is none to ask for.

    LIMIT_STEP above the car's limit now first: a higher limit never ends a charge. Else LIMIT_STEP below, only while that
    is above a known battery level (a limit under it makes the car stop or report completed, and the test would end with
    the car at the test's limit; an unknown level never allows it). Rounded like the plan rounds the car's limit
    (verify_charge_limit), within low..high, never the car's limit now (that would prove nothing), never the limit
    published already (the automation only sends a change) and never the car's own from before the test (own: putting
    it back would then be no change either)."""
    for candidate in (current + LIMIT_STEP, current - LIMIT_STEP):
        value = float(rounding(candidate))
        if abs(value - current) < 0.5 or not low <= value <= high:
            continue
        if any(other is not None and abs(value - other) < 0.5 for other in (published, own)):
            continue
        if value < current and (battery is None or value <= battery + 1.0):
            continue
        return value
    return None


def build_steps(power_dict: dict, max_amp: float, phases: int, voltage: float, solar_steps: bool) -> list[dict]:
    """The steps of the test, in the order they are run, from the solar calculation and the charger's maximum.

    Every charge step: {"name", "phases", "amps", "low", "high", "search": +1 (more amps if it fails) or -1}.
    low and high are the lowest and highest amps the search may use for that number of phases."""
    max_amp = int(max_amp)
    steps = [{"name": "max", "phases": phases, "amps": max_amp, "low": max_amp, "high": max_amp, "search": -1}]
    if solar_steps and power_dict:
        groups: dict[int, list[int]] = {}
        for step in power_dict.values():
            groups.setdefault(int(step["phase"]), []).append(int(step["amp"]))
        many = groups.get(phases) or []
        if many:
            steps[0].update(low=min(many), high=max_amp)
            if min(many) != max_amp:
                steps.append({"name": "min", "phases": phases, "amps": min(many), "low": min(many), "high": max_amp, "search": 1})
        single = groups.get(1) or []
        if single and phases > 1:
            steps.append({"name": "single_max", "phases": 1, "amps": max(single), "low": min(single), "high": max(single), "search": -1})
            if min(single) != max(single):
                steps.append({"name": "single_min", "phases": 1, "amps": min(single), "low": min(single), "high": max(single), "search": 1})
    for step in steps:
        step["expected_w"] = step["amps"] * step["phases"] * voltage
    return steps


def next_amps(step: dict, attempts: list[dict]) -> int | None:
    """The amps to try next, None when the search is over. attempts: {"amps", "ok"} of what has been tried, in order.

    A step that fails is searched two amps at a time towards the middle of its group, until a current works. Then the one
    amp between that current and the nearest failing one is tried, so the result is the current closest to the step that
    works. At most MAX_ADJUSTMENTS currents are tried after the step's own."""
    if not attempts or len(attempts) > MAX_ADJUSTMENTS:
        return None
    direction = step["search"]
    tried = {attempt["amps"] for attempt in attempts}
    working = [attempt["amps"] for attempt in attempts if attempt["ok"]]
    if not working:
        candidate = max(step["low"], min(step["high"], attempts[-1]["amps"] + 2 * direction))
    else:
        best = min(working) if direction > 0 else max(working)
        failing = [attempt["amps"] for attempt in attempts if not attempt["ok"] and (attempt["amps"] < best if direction > 0 else attempt["amps"] > best)]
        if not failing:
            return None  # the step's own current works, or nothing nearer to the step was tried
        candidate = best - direction
    return candidate if candidate not in tried and step["low"] <= candidate <= step["high"] else None


class ChargerTest:
    """The jobs of the engine that hold a current and release it again. The coordinator runs the steps."""

    def charger_hold_active(self) -> bool:
        """Whether a test holds the charger now. A hold that ran out is dropped here (everything that publishes asks)."""
        hold = self._charger_hold
        if hold is None:
            return False
        if self.now() >= hold["until"]:
            _LOGGER.warning("The charger test hold ran out and is dropped")
            self._charger_hold, self._charger_abort = None, "expired"
            self._charger_test_released_at = self.now()
            self._charger_test_restore()
            self._charger_restart_reset(full=False)
            return False
        return True

    def _charger_test_restore(self) -> None:
        """Put back the action, enabled and amps from before the test, so a plan that returns early (another car) does not
        leave the test's values published. The plan publishes its own on its next run."""
        saved, self._charger_saved = self._charger_saved, None
        if saved is None:
            return
        # the plan's rule from before the test, until the plan's next run sets its own (a hold that ran out outside a
        # calculation would otherwise leave the test's rule up to a cron interval). Without one (a rule the test made
        # obsolete): that the test has ended, until the plan's next run
        self.set_charging_rule(saved.get("rule") or self.t("ui.charger_test.rule_ended"), fresh=False)
        if saved.get("car_limit") is not None:
            # the car's own limit from before the limit step. The runner puts it back on its own first (with the action
            # unchanged, so the automation sends it); here, with the action changing too, the automation may not send it
            self._publish_test_limit(saved["car_limit"])
        self.charger_action, self.charger_enabled = saved["action"], saved["enabled"]
        self._publish_charger_action()
        self.set_charger_phase_limits(saved["circuit"], saved["charger"])

    def charger_test_published(self) -> list[int] | None:
        """The amps per phase published now while a test holds the charger (the load balancing may have limited them since)."""
        return [int(a) for a in self.current_charging_amps] if self.charger_hold_active() else None

    def charger_hold_amps(self) -> list[int] | None:
        """The amps per phase the test holds (zeros for a stop), None when no test holds the charger."""
        return list(self._charger_hold["amps"]) if self.charger_hold_active() else None

    def charger_hold_action(self) -> str | None:
        """The held action as it is published: a held charge the site has no current for is a stop (site_stop)."""
        if not self.charger_hold_active():
            return None
        return "stop" if self._charger_hold.get("site_stop") else self._charger_hold["kind"]

    def _charger_test_rule(self) -> None:
        """The rule while a test holds the charger: 🧪 with what is published (a stop, or the requested amps and phases;
        the load balancing may still give fewer amps, which the charger_action attributes show)."""
        hold = self._charger_hold
        if hold is None:
            return
        amps = [int(a) for a in hold["amps"] if a > 0]
        if hold["kind"] == "stop" or hold.get("site_stop") or not amps:
            action = self.t("ui.charger_test.step.stop")
        else:
            action = self.t("ui.charger_test.rule_charge", amps=max(amps), phases=len(amps))
        if hold.get("limit") is not None:
            # the charge limit step (and the put-back of the car's own limit): the limit that is published with the action
            action = self.t("ui.charger_test.rule_limit", action=action, limit=round(hold["limit"]))
        self.set_charging_rule(f"🧪{self.t('ui.charger_test.rule', action=action)}")

    def _charger_test_publish(self) -> None:
        """Publish the held action and amps (the load balancing may still limit them)."""
        self._publish_charger_action()
        self.set_charger_phase_limits()
        # what charge_if_needed publishes after every calculation: the blueprint's fail-safe watches the heartbeat, and the
        # load balancing must know what this car asks for while the plan is not calculated
        self.publish_site()
        self.set_attr("charger_action", "mode", "normal")
        self.set_attr("charger_action", "updated", self.now().replace(tzinfo=self._snap.tz).isoformat() if self._snap else None)

    def charger_test_begin(self) -> dict:
        """Whether a test can run now: {"ok", "reason", "steps", "solar_steps", "voltage", "power_entity"}."""
        charger = self.config["charger"]
        power_entity = charger["entity_ids"]["charger_power_entity_id"]
        out = {"ok": False, "reason": None, "steps": [], "voltage": float(charger["power_voltage"]), "power_entity": power_entity}
        if not self.is_entity_configured(power_entity):
            out["reason"] = "not_configured"
            return out
        status = self.get_state(charger["entity_ids"]["status_entity_id"], try_history=False, error_state="unavailable")
        if status in ENTITY_UNAVAILABLE_STATES:
            out["reason"] = "unavailable"
        elif status in CHARGER_NOT_READY_STATUS:
            out["reason"] = "not_connected"
        elif status in CHARGER_COMPLETED_STATUS:
            out["reason"] = "completed"
        elif status not in CHARGER_READY_STATUS + CHARGER_CHARGING_STATUS:
            out["reason"] = "not_ready"
        elif self.other_ev_connected():
            out["reason"] = "other_ev"
        elif self.charger_hold_active():
            out["reason"] = "running"
        if out["reason"]:
            return out
        solar_steps = self.is_solar_configured() and bool(self.charging_power_dict())
        out["solar_steps"] = solar_steps
        out["steps"] = build_steps(
            self.charging_power_dict() if solar_steps else {}, charger["charging_max_amp"], int(charger["charging_phases"]),
            float(charger["power_voltage"]), solar_steps,
        )
        out["ok"] = True
        return out

    def charger_test_hold(self, kind: str, amps: int, phases: int, seconds: float) -> dict:
        """Publish kind ("charge" at amps on phases phases, or "stop") for the next seconds.

        Returns {"status": "ok" | "not_ready", "requested": amps per phase, "published": amps per phase after the load
        balancing}. Nothing new is held when the cable is out or the car is full; a stop over a hold the test already has is
        always taken (a stop is harmless, and the held charge must not stay published when the car resumes or is plugged in)."""
        status = self.get_state(self.config["charger"]["entity_ids"]["status_entity_id"], try_history=False, error_state="unavailable")
        if status not in CHARGER_READY_STATUS + CHARGER_CHARGING_STATUS and not (kind == "stop" and self.charger_hold_active()):
            return {"status": "not_ready", "requested": [0, 0, 0], "published": [0, 0, 0]}
        requested = amps_list(amps, phases) if kind == "charge" else [0, 0, 0]
        now = self.now()
        if self._charger_saved is None or not self.charger_hold_active():
            restarting = self.restarting_charger
            # a restart (is_charging) in progress ends: the test decides what is published, and the charger is not left
            # disabled by the restart (enabled before it is saved)
            self._charger_restart_reset(full=False)
            rule = self.last_charging_rule_text
            if rule.startswith("🧪") or (restarting and self._charger_restart_rule and rule.startswith(self._charger_restart_rule)):
                # never the rule of an earlier test (one that ran out before the plan had set its own again), nor the
                # text of a restart the test has just ended (a give-up is kept, and so is its rule)
                rule = ""
            # what the plan asked for: with a site the published amps are the clamped ones, and putting those back would
            # keep the car at them when the site has more again (the site's own job applies the requested amps). Only for a
            # charge: a plan the site had stopped asked for amps it was not given, and "stop" with them would start a
            # charger that is set by its current alone (the site's job only acts on a charge)
            # requested_* is only set by the load balancing: zeros when the site came after the plan's last run
            site = self.site_active() and self.charger_action == "charge" and sum(self.requested_charger_amps) > 0
            self._charger_saved = {
                "action": self.charger_action, "enabled": self.charger_enabled,
                "rule": rule,
                "circuit": list(self.requested_circuit_amps if site else self.current_circuit_amps),
                "charger": list(self.requested_charger_amps if site else self.current_charging_amps),
            }
        self._charger_hold = {"kind": kind, "amps": requested, "until": now + datetime.timedelta(seconds=seconds), "since": now}
        self._charger_abort = None
        # the load balancing and the stop logic read the action: during the test it is what the test publishes
        self.charger_action, self.charger_enabled = kind, True
        self._charger_test_publish()
        return {"status": "ok", "requested": requested, "published": [int(a) for a in self.current_charging_amps]}

    def _publish_test_limit(self, value: float) -> None:
        """The charge limit on the sensor and the action (in the decision, so the automation sees the change)."""
        self.set_state("charging_limit", value)
        self.set_attr("charger_action", "charging_limit", value)

    def charger_test_limit_plan(self, own: float | None = None) -> dict:
        """The charge limit step: {"entity": the car's charge limit entity ("" without one: no step), "current": its limit
        now (None without a number), "test": the limit to ask for (None when there is none, limit_test_value)}. own: the
        car's limit from before the test, which the test value must not be either."""
        ids = self.config["ev_car"]["entity_ids"]
        entity_id = ids["charging_limit_entity_id"] if self.is_ev_configured() and self.is_entity_configured(ids["charging_limit_entity_id"]) else ""
        out = {"entity": entity_id, "current": None, "test": None}
        if not entity_id:
            return out
        current = self.get_state(entity_id, try_history=False, float_type=True, error_state=None)
        if not isinstance(current, (int, float)) or current != current or abs(current) == float("inf"):
            return out
        out["current"] = float(current)
        attributes = self.get_attr(entity_id, error_state={}) or {}

        def bound(key, default):
            value = attributes.get(key)
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default

        low = max(bound("min", 0.0), float(self.get_min_charge_limit_battery_level()))
        high = min(bound("max", 100.0), 100.0)
        rounding_ten = self.get_integration(entity_id) in ("kia_uvo", "cupra_we_connect")

        def rounding(value):  # like verify_charge_limit
            return round_up(value / 10) * 10 if rounding_ten else value

        published = self.sensor_states.get("charging_limit")
        published = float(published) if isinstance(published, (int, float)) and not isinstance(published, bool) else None
        battery = self.battery_level(unknown=None)  # no level is no level, not 0 % (that would allow a lower limit)
        battery = float(battery) if isinstance(battery, (int, float)) and battery == battery else None
        out["test"] = limit_test_value(out["current"], battery, low, high, rounding, published, own)
        return out

    def charger_test_limit(self, value: float, car_limit: float, seconds: float) -> dict:
        """Publish the charge limit value while the test holds the charger, with the held action and amps unchanged (the
        automation only sends a changed limit on its own, or with a new charge). The hold is kept for the next seconds.

        No charger status check: putting the car's own limit car_limit back must also work when the car is full or
        unplugged. car_limit is kept for the restore of a hold that runs out. {"status": "ok" | "no_hold"}."""
        if not self.charger_hold_active():
            return {"status": "no_hold"}
        if self._charger_saved is not None:
            self._charger_saved.setdefault("car_limit", car_limit)
        hold = self._charger_hold
        hold["until"] = max(hold["until"], self.now() + datetime.timedelta(seconds=seconds))
        hold["limit"] = value
        self._publish_test_limit(value)
        self._charger_test_publish()
        return {"status": "ok"}

    def charger_test_release(self) -> None:
        """End the hold, put back what was published before, and let the plan publish its own action and amps again.

        Forced: a setting changed during the test was not planned with (charge_if_needed waits while held), and the plan
        hash ignores most settings."""
        self._charger_hold = None
        self._charger_abort = None
        # the test may have ended on a stop, and the charger takes a while to start again on the plan's action: no restart
        # (is_charging) for the grace after a new session, and none from a mark set before the test (the restarts made and
        # a give-up are kept: a test is no reason to notify again)
        self._charger_test_released_at = self.now()
        self._charger_test_restore()
        self._charger_restart_reset(full=False)
        self.charge_trigger(force=True)


def build_charger_test_markdown(t, state: dict) -> str:
    """The result as markdown (the texts are ui.charger_test.*): a table row per step and a summary.

    state: {"status", "steps": [step records of the coordinator], "reason", "automation": found | unknown | None, "amps_control",
    "plan": [{"name", "amps", "phases"}: every step of a running test in order], "current": the name of the step that is
    measured right now or None}.
    While the test runs, the steps that have no result yet are rows too: waiting, and the one that is tested now.
    """
    if state["status"] == "idle" and not state["steps"]:
        return t("ui.charger_test.not_run")
    icons = {"ok": "✅", "adjusted": "⚠️", "fail": "❌", "skip": "🔧"}
    lines = [f"**{t('ui.charger_test.status.' + state['status'])}**", ""]
    # the car's own charge limit after the limit step (ok None: the test limit is out and not put back yet, shown once the
    # test has ended, also after a restart during it)
    put_back = state.get("limit_put_back")
    limit_note: list[str] = []
    if isinstance(put_back, dict) and put_back.get("limit") is not None and isinstance(put_back.get("ok"), bool):
        key = "limit_put_back" if put_back["ok"] else "limit_not_put_back"
        limit_note = ["", t(f"ui.charger_test.{key}", limit=round(put_back["limit"]))]
    if not state["steps"] and state["status"] != "running":
        if state.get("reason"):
            lines += [t(f"ui.charger_test.stop_reason.{state['reason']}"), ""]
        return "\n".join([*lines, t("ui.charger_test.put_back"), *limit_note, "", t("ui.charger_test.legend")])
    lines += [f"| | {t('ui.charger_test.column_step')} | {t('ui.charger_test.column_result')} |", "|:-:|:--|:--|"]
    counts = {"ok": 0, "adjusted": 0, "fail": 0, "skip": 0}
    for step in state["steps"]:
        counts[step["status"]] += 1
        label = t(f"ui.charger_test.step.{step['name']}", amps=step.get("configured_amps", 0), phases=step.get("phases", 0))
        lines.append(f"| {icons[step['status']]} | {label} | {charger_step_text(t, step)} |")
    if state["status"] == "running":
        plan = [step for step in state.get("plan") or [] if isinstance(step, dict) and step.get("name")]
        done = {step["name"] for step in state["steps"]}
        for step in plan:
            if step["name"] in done:
                continue
            icon, text = ("▶️", t("ui.charger_test.testing")) if step["name"] == state.get("current") else ("⏳", t("ui.charger_test.waiting"))
            label = t(f"ui.charger_test.step.{step['name']}", amps=step.get("amps", 0), phases=step.get("phases", 0))
            lines.append(f"| {icon} | {label} | {text} |")
        if plan:  # the progress sits between the status and the table
            lines[2:2] = [t("ui.charger_test.progress", done=len(done & {step["name"] for step in plan}), total=len(plan)), ""]
        lines += ["", t("ui.charger_test.running_note"), "", t("ui.charger_test.legend_running" if plan else "ui.charger_test.legend")]
        return "\n".join([*lines[:-2], *limit_note, *lines[-2:]])  # the note on the car's limit sits above the legend, as at the end
    else:
        lines += ["", t("ui.charger_test.summary", ok=counts["ok"] + counts["adjusted"], fail=counts["fail"], skip=counts["skip"], total=len(state["steps"]))]
        if state.get("reason"):
            lines.append(t(f"ui.charger_test.stop_reason.{state['reason']}"))
        if state.get("car_phases"):
            lines.append(t("ui.charger_test.car_phases", phases=state["car_phases"]))
        # the voltage the steps were judged at, once per number of phases (judge_at_measured_voltage)
        voltages: dict[int, tuple[float, float]] = {}
        for step in state["steps"]:
            if step["status"] != "skip" and step.get("voltage") and step.get("voltage_setting"):
                voltages.setdefault(int(step.get("judged_phases") or step["phases"]), (step["voltage"], step["voltage_setting"]))
        for phases, (measured, setting) in sorted(voltages.items(), reverse=True):
            lines.append(t("ui.charger_test.voltage", voltage=round(measured), phases=phases, setting=round(setting)))
        if state.get("amps_control") is False:
            lines.append(t("ui.charger_test.no_amps_control"))
        if state.get("automation") == "unknown":
            lines.append(t("ui.charger_test.automation_unknown"))
        if counts["fail"]:
            lines.append(t("ui.charger_test.fail_hint"))
        lines.append(t("ui.charger_test.put_back"))
    lines += [*limit_note, "", t("ui.charger_test.legend")]
    return "\n".join(lines)


def charger_step_text(t, step: dict) -> str:
    """The result column of a step: what the charger delivered, the amps that were tried, or why it was not tested."""
    values = {key: round(step.get(key) or 0) for key in ("amps", "configured_amps", "watt", "expected_w", "phases")}
    # the highest current that was asked for (a lowest step searches upwards from its own)
    values["tried_amps"] = max([round(attempt.get("amps") or 0) for attempt in step.get("attempts") or []] + [values["configured_amps"]])
    if step["status"] == "skip":
        return t(f"ui.charger_test.skip.{step['reason']}", **values)
    if step["name"] == "limit":
        limits = {key: round(step.get(key) or 0) for key in ("limit_from", "limit_to", "limit_seen")}
        return t(f"ui.charger_test.{'ok' if step['status'] == 'ok' else 'fail'}.limit", seconds=round(step.get("seconds") or 0), **limits)
    if step.get("judged_phases"):  # the car charges on fewer phases: the power was expected on those
        values["phases"] = round(step["judged_phases"])
    if step["status"] in ("ok", "adjusted"):
        # a lowest step that stays at one power: the car takes less than that step, so no maximum to lower
        limited = "limited_low" if step["name"] in ("min", "single_min") else "limited"
        kind = "stop" if step["name"] == "stop" else limited if step.get("limited") else "started" if not step.get("expected_w") else step["status"]
        text = t(f"ui.charger_test.ok.{kind}", **values)
        if step.get("voltage") and kind in ("ok", "adjusted", "limited", "limited_low"):
            text = f"{text} {t('ui.charger_test.ok.voltage', voltage=round(step['voltage']))}"
        return f"{text} {t('ui.charger_test.ok.already_charging')}" if step.get("already_charging") else text
    if step.get("reason") == "overdraw":
        return t("ui.charger_test.fail.overdraw", **values)
    text = t(f"ui.charger_test.fail.{'stop' if step['name'] == 'stop' else 'charge'}", **values)
    tried = ", ".join(t("ui.charger_test.attempt", amps=attempt["amps"], watt=attempt["watt"]) for attempt in step.get("attempts") or [])
    return f"{text} {t('ui.charger_test.tried', tried=tried)}" if len(step.get("attempts") or []) > 1 else text
