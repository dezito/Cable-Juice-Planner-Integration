"""The test of the user's own charger automation: does the charger do what the amps and phases of the plan ask for?

CJP never sends a command; the user's automation (the charger blueprint) does, when the action sensor and its phase
attributes change. To test it CJP publishes a chosen current on one or three phases on ``sensor.<prefix>_charger_action``,
on purpose, and the coordinator reads the charger's power sensor to see what the charger then does. Only the published
action and amps are held: the plan, the settings and the rules are not touched.

* The steps are the maximum first (does charging work at all), then the lowest and highest steps of the solar calculation
  (``charging_power_dict``). A step that fails is searched two amps at a time towards the middle of its group and then
  one amp back (next_amps), so the result is the current closest to the step that works.
* Power that does not rise with more amps is the car's or the charger's limit, not a setting to change (power_is_capped),
  and a car that charges on fewer phases than asked is judged on its own phases (car_phases_of).
* Power clearly above what was published ends the test at once: the load balancing counts on the published amps.
* Without a way to set the current (no steps for the current in the blueprint, or no solar steps) only the maximum
  and the stop are tried.
* The hold is engine state that every path publishing the action reads: the cron, a forced calculation, a changed
  setting and the load balancing. It is dropped by the first publish after hold seconds (a lost runner can not leave the
  charger at a forced current), and the action, enabled and amps from before the test are put back. Load balancing
  still limits the amps (a limited step is not tested, never pushed past the fuse).
"""
from __future__ import annotations

import datetime
import logging

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
SKIP_REASONS = ("no_steps", "no_data", "site_limited", "no_baseline", "not_ready", "not_reached")
# more than the published amps give by this much is a charger that ignores the amps or the phases (load balancing risk)
OVERDRAW_SHARE = 0.25
OVERDRAW_MIN_W = 500.0


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
            self._charger_test_restore()
            return False
        return True

    def _charger_test_restore(self) -> None:
        """Put back the action, enabled and amps from before the test, so a plan that returns early (another car) does not
        leave the test's values published. The plan publishes its own on its next run."""
        saved, self._charger_saved = self._charger_saved, None
        if saved is None:
            return
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
        return self._charger_hold["kind"] if self.charger_hold_active() else None

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
        balancing}. Nothing is held when the cable is out or the car is full."""
        status = self.get_state(self.config["charger"]["entity_ids"]["status_entity_id"], try_history=False, error_state="unavailable")
        if status not in CHARGER_READY_STATUS + CHARGER_CHARGING_STATUS:
            return {"status": "not_ready", "requested": [0, 0, 0], "published": [0, 0, 0]}
        requested = amps_list(amps, phases) if kind == "charge" else [0, 0, 0]
        now = self.now()
        if self._charger_saved is None or not self.charger_hold_active():
            self._charger_saved = {
                "action": self.charger_action, "enabled": self.charger_enabled,
                "circuit": list(self.current_circuit_amps), "charger": list(self.current_charging_amps),
            }
        self._charger_hold = {"kind": kind, "amps": requested, "until": now + datetime.timedelta(seconds=seconds), "since": now}
        self._charger_abort = None
        # the load balancing and the stop logic read the action: during the test it is what the test publishes
        self.charger_action, self.charger_enabled = kind, True
        self._charger_test_publish()
        return {"status": "ok", "requested": requested, "published": [int(a) for a in self.current_charging_amps]}

    def charger_test_release(self) -> None:
        """End the hold, put back what was published before, and let the plan publish its own action and amps again.

        Forced: a setting changed during the test was not planned with (charge_if_needed waits while held), and the plan
        hash ignores most settings."""
        self._charger_hold = None
        self._charger_abort = None
        self._charger_test_restore()
        self.charge_trigger(force=True)


def build_charger_test_markdown(t, state: dict) -> str:
    """The result as markdown (the texts are ui.charger_test.*): a table row per step and a summary.

    state: {"status", "steps": [step records of the coordinator], "reason", "automation": found | unknown | None, "amps_control"}.
    """
    if state["status"] == "idle" and not state["steps"]:
        return t("ui.charger_test.not_run")
    icons = {"ok": "✅", "adjusted": "⚠️", "fail": "❌", "skip": "🔧"}
    lines = [f"**{t('ui.charger_test.status.' + state['status'])}**", ""]
    if not state["steps"] and state["status"] != "running":
        if state.get("reason"):
            lines += [t(f"ui.charger_test.stop_reason.{state['reason']}"), ""]
        return "\n".join([*lines, t("ui.charger_test.put_back"), "", t("ui.charger_test.legend")])
    lines += [f"| | {t('ui.charger_test.column_step')} | {t('ui.charger_test.column_result')} |", "|:-:|:--|:--|"]
    counts = {"ok": 0, "adjusted": 0, "fail": 0, "skip": 0}
    for step in state["steps"]:
        counts[step["status"]] += 1
        label = t(f"ui.charger_test.step.{step['name']}", amps=step.get("configured_amps", 0), phases=step.get("phases", 0))
        lines.append(f"| {icons[step['status']]} | {label} | {charger_step_text(t, step)} |")
    if state["status"] == "running":
        lines += ["", t("ui.charger_test.running_note")]
    else:
        lines += ["", t("ui.charger_test.summary", ok=counts["ok"] + counts["adjusted"], fail=counts["fail"], skip=counts["skip"], total=len(state["steps"]))]
        if state.get("reason"):
            lines.append(t(f"ui.charger_test.stop_reason.{state['reason']}"))
        if state.get("car_phases"):
            lines.append(t("ui.charger_test.car_phases", phases=state["car_phases"]))
        if state.get("amps_control") is False:
            lines.append(t("ui.charger_test.no_amps_control"))
        if state.get("automation") == "unknown":
            lines.append(t("ui.charger_test.automation_unknown"))
        if counts["fail"]:
            lines.append(t("ui.charger_test.fail_hint"))
        lines.append(t("ui.charger_test.put_back"))
    lines += ["", t("ui.charger_test.legend")]
    return "\n".join(lines)


def charger_step_text(t, step: dict) -> str:
    """The result column of a step: what the charger delivered, the amps that were tried, or why it was not tested."""
    if step["status"] == "skip":
        return t(f"ui.charger_test.skip.{step['reason']}")
    values = {key: round(step.get(key) or 0) for key in ("amps", "configured_amps", "watt", "expected_w", "phases")}
    if step.get("judged_phases"):  # the car charges on fewer phases: the power was expected on those
        values["phases"] = round(step["judged_phases"])
    if step["status"] in ("ok", "adjusted"):
        kind = "stop" if step["name"] == "stop" else "limited" if step.get("limited") else "started" if not step.get("expected_w") else step["status"]
        text = t(f"ui.charger_test.ok.{kind}", **values)
        return f"{text} {t('ui.charger_test.ok.already_charging')}" if step.get("already_charging") else text
    if step.get("reason") == "overdraw":
        return t("ui.charger_test.fail.overdraw", **values)
    text = t(f"ui.charger_test.fail.{'stop' if step['name'] == 'stop' else 'charge'}", **values)
    tried = ", ".join(t("ui.charger_test.attempt", amps=attempt["amps"], watt=attempt["watt"]) for attempt in step.get("attempts") or [])
    return f"{text} {t('ui.charger_test.tried', tried=tried)}" if len(step.get("attempts") or []) > 1 else text
