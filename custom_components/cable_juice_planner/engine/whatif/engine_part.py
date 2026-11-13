"""What-if Scenario A in the engine: collects the stored data, replays it and publishes the result.

Runs as an ordinary engine job (a year of history replays in about 0.1 s). Nothing here
changes the live plan, a setting, the car or the charger: the replay only reads CJP's
databases and its result is saved separately (economy database, key "what_if") and shown on
sensor.<prefix>_what_if.

The starting SOC is read by the coordinator from the recorder (see coordinator/what_if.py) or
entered by the user; without it the replay is unavailable. It is never defaulted.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from ...const import WEEKDAYS
from ..soc_power import planning_power_cap
from .derive import derive_windows
from .inputs import build_replay_input, loss_factor_from_table, power_cap_from_model
from .replay import PolicyResult, ReplayResult, replay, replay_plan

_LOGGER = logging.getLogger(__name__)

WHAT_IF_DAYS = (7, 365)  # the shortest, and the lowest the longest can be (see what_if_max_days: the days kept, 403 by default)
WHAT_IF_DEFAULT_DAYS = 30
DAY_SELECTIONS = ("all", "work_plan", "monday_friday", "weekend")
WHAT_IF_WINDOW_ROWS = 60  # the newest connection windows kept in the result for the dashboard


def _round(value: Any, digits: int) -> float | None:
    return round(float(value), digits) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


class WhatIf:
    """Scenario A: daily smart charging to the maximum recommended battery level."""

    # ------------------------------------------------------------------ #
    # settings
    # ------------------------------------------------------------------ #

    def what_if_max_days(self) -> int:
        """The longest period the data reaches back over: all the days kept (the months of history of the setup plus a month,
        so the same month a year ago is complete), never under 365."""
        return max(WHAT_IF_DAYS[1], self.economy_keep_days())

    def what_if_days(self) -> int:
        value = self.setting("what_if_days", WHAT_IF_DEFAULT_DAYS)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return WHAT_IF_DEFAULT_DAYS
        return int(min(max(value, WHAT_IF_DAYS[0]), self.what_if_max_days()))

    def what_if_target(self) -> tuple[float, str]:
        """The target battery level: the temporary override, else the live maximum recommended level."""
        override = self.setting("what_if_target_battery_level", 0.0)
        if isinstance(override, (int, float)) and not isinstance(override, bool) and math.isfinite(override) and override > 0:
            return float(min(override, 100.0)), "override"
        return float(self.get_max_recommended_charge_limit_battery_level()), "live_setting"

    def what_if_smart_weekdays(self) -> frozenset[int] | None:
        selection = self.setting("what_if_day_selection", "all")
        if selection == "work_plan":
            return frozenset(index for index, day in enumerate(WEEKDAYS) if self.switch_state(f"workday_{day}") == "on")
        if selection == "selected":
            return frozenset(index for index, day in enumerate(WEEKDAYS) if self.switch_state(f"what_if_day_{day}") == "on")
        if selection == "monday_friday":
            return frozenset(range(5))
        if selection == "weekend":
            return frozenset((5, 6))
        return None

    # ------------------------------------------------------------------ #
    # job: what_if_prepare (tells the coordinator where to read the starting SOC)
    # ------------------------------------------------------------------ #

    def what_if_period(self) -> tuple[float, float]:
        end = self._economy_ts(self.now())
        return end - self.what_if_days() * 86400.0, end

    def _what_if_build(self, period_start: float, period_end: float, start_soc: float | None, source: str):
        """The replay input for the period and what the data covered (shared by prepare and run)."""
        target, target_source = self.what_if_target()
        data, coverage, sessions = self._what_if_inputs(
            period_start, period_end, start_soc, source, target, self.what_if_smart_weekdays()
        )
        return data, coverage, target, target_source, sessions

    def _what_if_inputs(
        self, period_start: float, period_end: float, start_soc: float | None, source: str,
        target: float, smart_weekdays: frozenset[int] | None,
    ):
        """The replay input and coverage for a target and day selection (Scenarios A and B)."""
        battery = self.config["ev_car"].get("battery_size")
        max_kw = float(self.max_kwh_charging)
        kws = {round(watt / 1000.0, 3) for watt in self.charging_power_dict()} | {round(max_kw, 3)}
        loss_table: dict[float, float] = {}
        for kw in kws:
            try:
                loss_table[kw] = float(self.charging_loss_factor(kw))
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("What-if: no charging loss at %s kW: %s", kw, err)
        model = self.soc_power_model if self.soc_power_learning_enabled() else None
        sessions = self._economy_sessions()
        public = [{"ts": s["ts"], "kwh": s["public_kwh"]} for s in sessions if s["public"] and s["public_kwh"] > 0]
        stored = self.economy_db.get("prices", {})
        # the repeated autumn hour is kept apart (not in the monthly economy): a real price always wins over its copy
        copies = {ts: price for ts, price in self.economy_db.get("prices_dst_copy", {}).items() if ts not in stored}
        # hours CJP did not store itself: the price sensor's recorded hourly statistics (kept by the daily economy
        # update), the last choice. A stored price or a copy always wins; the hours are counted and flagged.
        history = {
            float(ts): price for ts, price in self.economy_db.get("prices_history", {}).items()
            if ts not in stored and ts not in copies and isinstance(price, (int, float)) and math.isfinite(price)
        }
        data, coverage = build_replay_input(
            windows=self.economy_db["windows"],
            prices={**history, **copies, **stored},
            history_hours=list(history),
            refund=self._solar_refund() or 0.0,
            derived=self.economy_db.get("derived_windows"),
            work_plan=self.what_if_work_plan(),
            drives=self.analysis_db.get("drives", []),
            public_sessions=public,
            period_start=period_start,
            period_end=period_end,
            battery_kwh=float(battery) if isinstance(battery, (int, float)) and not isinstance(battery, bool) else math.nan,
            max_kw=max_kw,
            phases=int(self.config["charger"]["charging_phases"]),
            start_soc=start_soc,
            target_soc=target,
            loss_factor=loss_factor_from_table(loss_table),
            power_cap=power_cap_from_model(model, planning_power_cap),
            prices_averaged_since=self.economy_db.get("prices_averaged_since"),
            start_soc_source=source,
            tz=self._snap.tz,
            smart_weekdays=smart_weekdays,
            drive_history_limit=self.drive_history_limit(),
            dst_copied_hours=list(copies),
        )
        return data, coverage, sessions

    def what_if_work_plan(self) -> dict[str, Any] | None:
        """The work plan as it is set now, per weekday (Monday = 0): workday or not, the departure and homecoming time in seconds
        after local midnight and the distance needed (the day's own distance, else the typical one; the live battery range is
        not applied). It describes today's plan, not what the plan was in the past."""
        days: dict[int, dict[str, Any]] = {}
        for index, day in enumerate(WEEKDAYS):
            try:
                entry: dict[str, Any] = {
                    "workday": self.switch_state(f"workday_{day}", error_state="off") == "on",
                    "km": float(self.get_entity_daily_distance(day_text=day, ignore_realistic_estimated_range=True)),
                }
                departure, homecoming = self.setting(f"workday_departure_{day}"), self.setting(f"workday_homecoming_{day}")
                if departure is not None and homecoming is not None:
                    entry["departure"] = departure.hour * 3600 + departure.minute * 60
                    entry["homecoming"] = homecoming.hour * 3600 + homecoming.minute * 60
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("What-if: no work plan for %s: %s", day, err)
                continue
            days[index] = entry
        return {"days": days} if days else None

    def store_derived_windows(self, data: dict[str, Any]) -> None:
        """economy_update: the connections from before CJP's own records, estimated from the daily-read statistics of the
        battery level and the charger power (derive.py). Without "ev_hours" in the data (the statistics could not be read)
        what was stored stays."""
        hours = data.get("ev_hours")
        if not isinstance(hours, dict):
            return
        measured = [(w["start"], w["end"]) for w in self.economy_db["windows"] if w.get("end") is not None]
        result = derive_windows(hours, measured, self.what_if_work_plan(), self._snap.tz)
        result["computed_at"] = self._economy_ts(self.now())
        self.economy_db["derived_windows"] = result
        self.mark_dirty("economy")

    def what_if_prepare(self) -> dict[str, Any]:
        """Fixes the period and the first connection the starting level belongs to (the same selection the run uses)."""
        period_start, period_end = self.what_if_period()
        _, coverage, _, _, _ = self._what_if_build(period_start, period_end, None, "unavailable")
        manual = self.setting("what_if_start_battery_level", 0.0)
        manual = float(manual) if isinstance(manual, (int, float)) and not isinstance(manual, bool) and math.isfinite(manual) and manual > 0 else None
        self._what_if_attempt("running")
        return {
            "first_window_start": coverage["first_window"], "manual_start_soc": manual, "period": [period_start, period_end],
        }

    # ------------------------------------------------------------------ #
    # job: what_if_run
    # ------------------------------------------------------------------ #

    def what_if_run(self, start: dict[str, Any]) -> dict[str, Any]:
        """start: {"soc": percent | None, "source": text, and from what_if_prepare "period" and
        "first_window_start"}. Returns the result and publishes it."""
        now = self._economy_ts(self.now())
        prepared_period = start.get("period")
        period_start, period_end = (float(prepared_period[0]), float(prepared_period[1])) if prepared_period else self.what_if_period()
        data, coverage, target, target_source, sessions = self._what_if_build(
            period_start, period_end, start.get("soc"), str(start.get("source") or "unavailable")
        )
        weekdays = self.what_if_smart_weekdays()
        reason = None
        if "first_window_start" in start and coverage["first_window"] != start["first_window_start"]:
            reason = "start_level_stale"  # a connection ended meanwhile: the level read belongs to another first connection
        elif weekdays is not None and not weekdays:
            reason = "no_days_selected"
        elif coverage["windows"] > 1 and coverage["drives_in_gaps"] == 0:
            reason = "no_drives"  # without a drive that can be applied the demand is unknown, not zero
        if reason is not None:
            result = ReplayResult(
                status="unavailable", reason=reason, windows_total=coverage["windows"], provenance=dict(data.provenance),
                warnings=data.warnings,
            )
        else:
            result = replay(data)
        published = self._what_if_result(result, coverage, now, period_start, period_end, target, target_source, sessions)
        self._what_if_add_current(published, data, result)
        self._what_if_attempt(result.status, result.reason)
        if result.status in ("ok", "partial"):
            # only a successful calculation replaces the saved one
            self.economy_db["what_if"] = published
            self.mark_dirty("economy")
        self.publish_what_if(published if result.status in ("ok", "partial") else None)
        return published

    # ------------------------------------------------------------------ #
    # result
    # ------------------------------------------------------------------ #

    def _what_if_add_current(self, published: dict[str, Any], data: Any, result: ReplayResult) -> None:
        """The third column of the table: your current daily reserve and maximum recommended level replayed over the same
        period (the planned grid-only charging of Scenario B/D, shared replay). It leaves out the last connection (the next
        drive's need is not known), so it covers one connection less. Never changes anything live; a failure only leaves
        the column out, and the reason is kept."""
        published["settings"] = None
        published["current"] = None
        published["current_reason"] = None
        if result.status not in ("ok", "partial"):
            return
        try:
            live, _, _ = self.what_if_settings_values()
            published["settings"] = {"reserve": live.reserve, "maximum": live.maximum}
            plan = replay_plan(data, live)
            if plan.status == "ok":
                published["current"] = {
                    **self._plan_policy(plan), "windows_replayed": plan.windows_replayed, "estimated_windows": plan.estimated_windows,
                }
                self._what_if_same_span(published, plan.plan, result)
            else:
                published["current_reason"] = plan.reason
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("What-if: the current settings column could not be calculated: %s", err)
            published["current_reason"] = "internal_error"

    def _what_if_same_span(self, published: dict[str, Any], current: PolicyResult | None, result: ReplayResult) -> None:
        """The differences against the current settings over the connections all three cover: the current settings leave out
        the last connection, so a difference of the plain totals would hold one connection more for smart and immediate
        charging. Totals and per month; the month rows keep their plain numbers too."""
        if current is None or result.smart is None or result.asap is None:
            return
        count = min(len(current.windows), len(result.smart.windows), len(result.asap.windows))
        if count == 0:
            return
        pairs = list(zip(current.windows[:count], result.smart.windows[:count], result.asap.windows[:count]))
        total = {"windows": count}
        for name, index in (("current", 0), ("smart", 1), ("asap", 2)):
            rows = [p[index] for p in pairs]
            total[f"{name}_cost"] = _round(sum(w.cost for w in rows), 3)
            total[f"{name}_grid_kwh"] = _round(sum(w.grid_kwh for w in rows), 3)
            total[f"{name}_loss_kwh"] = _round(sum(w.loss_kwh for w in rows), 3)
        published["current"]["same"] = total
        months: dict[str, dict[str, float]] = {}
        for cur, smart, asap in pairs:
            month = months.setdefault(
                self._economy_local(smart.end).strftime("%Y-%m"), {"windows": 0, "current": 0.0, "smart": 0.0, "asap": 0.0}
            )
            month["windows"] += 1
            month["current"] += cur.cost
            month["smart"] += smart.cost
            month["asap"] += asap.cost
        for row in published.get("months") or []:
            same = months.get(row["month"])
            row["current_cost"] = _round(same["current"], 3) if same else None
            row["same_windows"] = int(same["windows"]) if same else 0
            row["same_smart_cost"] = _round(same["smart"], 3) if same else None
            row["same_asap_cost"] = _round(same["asap"], 3) if same else None

    def _policy(self, policy: PolicyResult | None) -> dict[str, Any] | None:
        if policy is None:
            return None
        return {
            "cost": _round(policy.cost, 2), "grid_kwh": _round(policy.grid_kwh, 3), "charged_kwh": _round(policy.charged_kwh, 3),
            "loss_kwh": _round(policy.loss_kwh, 3), "unmet_target_kwh": _round(policy.unmet_target_kwh, 3),
            "unmet_driving_kwh": _round(policy.unmet_driving_kwh, 3), "targets_met": policy.targets_met,
            "windows": len(policy.windows), "start_soc": _round(policy.start_soc, 1), "final_soc": _round(policy.final_soc, 1),
            "lowest_soc": _round(policy.lowest_soc, 1),
        }

    def _what_if_result(
        self, result: ReplayResult, coverage: dict, now: float, period_start: float, period_end: float,
        target: float, target_source: str, sessions: list[dict],
    ) -> dict[str, Any]:
        published: dict[str, Any] = {
            "model_version": result.model_version,
            "status": result.status,
            "reason": result.reason,
            "computed_at": now,
            "period": {"start": period_start, "end": period_end, "days": self.what_if_days()},
            "target": {"battery_level": target, "source": target_source},
            "day_selection": self.setting("what_if_day_selection", "all"),
            "battery_kwh": self.config["ev_car"].get("battery_size"),
            "max_kw": round(self.max_kwh_charging, 3),
            "coverage": coverage,
            "windows": {"total": result.windows_total, "replayed": result.windows_replayed, "stopped_at": result.stopped_at},
            "comparable": result.comparable,
            "comparable_reason": result.comparable_reason,
            "method": result.method,
            "estimated_windows": result.estimated_windows,
            "price_estimate": _round(result.price_estimate, 4),
            "calibrated_start": result.calibrated_start,
            "warnings": list(result.warnings),
            "notes": list(result.notes),
            "provenance": dict(result.provenance),
            "ignored_events": result.ignored_events,
            "conservation_error_kwh": _round(result.max_conservation_error_kwh, 9),
            "smart": self._policy(result.smart),
            "asap": self._policy(result.asap),
            "average_price": _round(result.average_price, 4),
            "difference": {key: _round(value, 3) for key, value in result.difference.items()} if result.difference else None,
            "months": [],
            "rows": [],
            "recorded_reference": None,
            "derived": None,
            "settings": None,
            "current": None,
            "current_reason": None,
        }
        if result.smart is None or result.asap is None:
            return published

        months: dict[str, dict[str, Any]] = {}
        rows = []
        for smart, asap in zip(result.smart.windows, result.asap.windows):
            key = self._economy_local(smart.end).strftime("%Y-%m")
            month = months.setdefault(key, {"month": key, "windows": 0, "derived_windows": 0, "smart_cost": 0.0, "asap_cost": 0.0, "grid_kwh": 0.0, "targets_met": 0, "unmet_target_kwh": 0.0})
            month["windows"] += 1
            month["derived_windows"] += 1 if smart.derived else 0
            month["smart_cost"] += smart.cost
            month["asap_cost"] += asap.cost
            month["grid_kwh"] += smart.grid_kwh
            month["targets_met"] += 1 if smart.unmet_target_kwh <= 1e-6 else 0
            month["unmet_target_kwh"] += smart.unmet_target_kwh
            rows.append({
                "start": smart.start, "end": smart.end, "soc_in": _round(smart.soc_in, 1), "soc_out": _round(smart.soc_out, 1),
                "smart_cost": _round(smart.cost, 2), "asap_cost": _round(asap.cost, 2), "kwh": _round(smart.grid_kwh, 2),
                "unmet_kwh": _round(smart.unmet_target_kwh, 2),
            })
        published["months"] = [
            {k: (_round(v, 3) if isinstance(v, float) else v) for k, v in month.items()} for _, month in sorted(months.items())
        ]
        published["rows"] = rows[-WHAT_IF_WINDOW_ROWS:]
        # the connections estimated from the battery level statistics, apart from the measured ones
        older = [(s, a) for s, a in zip(result.smart.windows, result.asap.windows) if s.derived]
        published["derived"] = {
            "windows": len(older),
            "modelled": sum(1 for s, _ in older if s.modelled),
            "smart_cost": _round(sum(s.cost for s, _ in older), 2),
            "asap_cost": _round(sum(a.cost for _, a in older), 2),
            "difference": _round(sum(s.cost - a.cost for s, a in older), 3),
            "grid_kwh": _round(sum(s.grid_kwh for s, _ in older), 3),
        } if older else None

        # what CJP recorded in the same span: a reference only, never subtracted (see TODO_WHAT_IF.md)
        first, last = result.smart.windows[0].start, result.smart.windows[-1].end
        inside = [s for s in sessions if first <= s["ts"] < last]
        published["recorded_reference"] = {
            "kwh": _round(sum(s["kwh"] for s in inside), 3),
            "cost": _round(sum(s["cost"] for s in inside), 2) if inside and all(s["cost_valid"] for s in inside) else None,
            "sessions": len(inside),
        }
        return published

    # ------------------------------------------------------------------ #
    # publishing
    # ------------------------------------------------------------------ #

    def _what_if_attempt(self, status: str, reason: str | None = None) -> None:
        self.economy_db["what_if_attempt"] = {"status": status, "reason": reason, "at": self._economy_ts(self.now())}
        self.mark_dirty("economy")
        self.set_attr("what_if", "attempt", self.economy_db["what_if_attempt"])

    def what_if_fail(self, reason: str) -> None:
        """job: the coordinator could not prepare the calculation (the saved result stays)."""
        self._what_if_attempt("unavailable", reason)
        self.publish_what_if()

    def load_what_if(self) -> None:
        """Startup: a calculation that was running when Home Assistant stopped did not finish."""
        saved = self.economy_db.get("what_if")
        if not isinstance(saved, dict):
            self.economy_db.pop("what_if", None)
        attempt = self.economy_db.get("what_if_attempt")
        if isinstance(attempt, dict) and attempt.get("status") == "running":
            self.economy_db["what_if_attempt"] = {**attempt, "status": "unavailable", "reason": "interrupted"}
        elif not isinstance(attempt, dict):
            self.economy_db.pop("what_if_attempt", None)
        self.publish_what_if()

    def publish_what_if(self, result: dict[str, Any] | None = None) -> None:
        """The latest successful calculation (or the saved one after a restart) on sensor what_if."""
        result = result or self.economy_db.get("what_if")
        # the state is the whole-period figure: a replay that stopped early has no such number
        complete = (result or {}).get("status") == "ok" and (result or {}).get("comparable")
        difference = (result or {}).get("difference") if complete else None
        self.set_state("what_if", difference.get("adjusted_cost") if difference else None)
        self.set_attr("what_if", "result", result)
        self.set_attr("what_if", "attempt", self.economy_db.get("what_if_attempt"))
