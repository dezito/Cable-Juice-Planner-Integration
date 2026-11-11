"""What-if Scenario D in the engine: the planned charging with the grid only (no solar) over the selected period.

The solar energy is taken out completely: the period's connections are planned again, with CJP's current
reserve and maximum level and the recorded driving, and all energy is bought from the grid at the hourly
price (engine/whatif/replay.py, replay_plan). The recorded charging of the same period (with solar, valued
the way CJP values it) is shown next to it as a reference, and the recorded energy priced at the planned
grid price gives an estimate of what the solar was worth. Nothing live is changed. The result is saved
separately (economy database, key "what_if_no_solar") and shown on sensor.<prefix>_what_if_no_solar.

It shares the period, the starting battery level and the input data with Scenario A and B.
"""
from __future__ import annotations

import math
from typing import Any

from .engine_part import _round
from .replay import CjpSettings, PlanResult, replay_plan


class WhatIfNoSolar:
    """Scenario D: what the charging would cost from the grid only."""

    def what_if_no_solar_settings(self) -> CjpSettings:
        return CjpSettings(
            reserve=float(self.get_min_daily_battery_level()), maximum=float(self.get_max_recommended_charge_limit_battery_level())
        )

    # ------------------------------------------------------------------ #
    # jobs
    # ------------------------------------------------------------------ #

    def what_if_no_solar_prepare(self) -> dict[str, Any]:
        period_start, period_end = self.what_if_period()
        settings = self.what_if_no_solar_settings()
        _, coverage, _ = self._what_if_inputs(period_start, period_end, None, "unavailable", settings.maximum, None)
        manual = self.setting("what_if_start_battery_level", 0.0)
        manual = float(manual) if isinstance(manual, (int, float)) and not isinstance(manual, bool) and math.isfinite(manual) and manual > 0 else None
        self._what_if_no_solar_attempt("running")
        return {"first_window_start": coverage["first_window"], "manual_start_soc": manual, "period": [period_start, period_end]}

    def what_if_no_solar_run(self, start: dict[str, Any]) -> dict[str, Any]:
        """start: {"soc": percent | None, "source": text, "period", "first_window_start"}. Returns the result and publishes it."""
        now = self._economy_ts(self.now())
        prepared_period = start.get("period")
        period_start, period_end = (float(prepared_period[0]), float(prepared_period[1])) if prepared_period else self.what_if_period()
        settings = self.what_if_no_solar_settings()
        data, coverage, sessions = self._what_if_inputs(
            period_start, period_end, start.get("soc"), str(start.get("source") or "unavailable"), settings.maximum, None
        )
        reason = None
        if "first_window_start" in start and coverage["first_window"] != start["first_window_start"]:
            reason = "start_level_stale"  # a connection ended meanwhile: the level read belongs to another first connection
        elif coverage["windows"] > 1 and coverage["drives_in_gaps"] == 0:
            reason = "no_drives"  # without a drive that can be applied the demand is unknown, not zero
        if reason is not None:
            result = PlanResult(
                status="unavailable", reason=reason, windows_total=coverage["windows"], provenance=dict(data.provenance), warnings=data.warnings,
            )
        else:
            result = replay_plan(data, settings)
        published = self._what_if_no_solar_result(result, coverage, now, period_start, period_end, settings, sessions)
        self._what_if_no_solar_attempt(result.status, result.reason)
        if result.status == "ok":
            self.economy_db["what_if_no_solar"] = published  # only a calculation with a result replaces the saved one
            self.mark_dirty("economy")
        self.publish_what_if_no_solar(published if result.status == "ok" else None)
        return published

    # ------------------------------------------------------------------ #
    # result
    # ------------------------------------------------------------------ #

    @staticmethod
    def _plan_policy(result: PlanResult) -> dict[str, Any] | None:
        policy, readiness = result.plan, result.readiness
        if policy is None or readiness is None:
            return None
        return {
            "cost": _round(policy.cost, 2), "grid_kwh": _round(policy.grid_kwh, 3), "charged_kwh": _round(policy.charged_kwh, 3),
            "loss_kwh": _round(policy.loss_kwh, 3), "unmet_driving_kwh": _round(policy.unmet_driving_kwh, 3),
            "unit": _round(policy.cost / policy.grid_kwh, 3) if policy.grid_kwh > 1e-9 else None,
            "estimated_cost": _round(policy.estimated_cost, 2), "start_soc": _round(policy.start_soc, 1),
            "final_soc": _round(policy.final_soc, 1), "lowest_soc": _round(policy.lowest_soc, 1), "arrivals": readiness.arrivals,
            "arrivals_below_reserve": readiness.arrivals_below_reserve, "lowest_arrival": _round(readiness.lowest_arrival, 1),
        }

    def _what_if_no_solar_result(
        self, result: PlanResult, coverage: dict, now: float, period_start: float, period_end: float, settings: CjpSettings,
        sessions: list[dict],
    ) -> dict[str, Any]:
        published: dict[str, Any] = {
            "model_version": result.model_version,
            "status": result.status,
            "reason": result.reason,
            "computed_at": now,
            "period": {"start": period_start, "end": period_end, "days": self.what_if_days()},
            "settings": {"reserve": settings.reserve, "maximum": settings.maximum},
            "battery_kwh": self.config["ev_car"].get("battery_size"),
            "coverage": coverage,
            "windows": {"total": result.windows_total, "replayed": result.windows_replayed},
            "method": result.method,
            "estimated_windows": result.estimated_windows,
            "price_estimate": _round(result.price_estimate, 4),
            "calibrated_start": result.calibrated_start,
            "warnings": list(result.warnings),
            "notes": list(result.notes),
            "provenance": dict(result.provenance),
            "conservation_error_kwh": _round(result.max_conservation_error_kwh, 9),
            "plan": self._plan_policy(result),
            "average_price": _round(result.average_price, 4),
            "recorded": None,
            "estimate": None,
            "months": [],
        }
        if result.plan is None:
            return published

        # the calibrating first connection charges nothing in the plan: the recorded span starts after it
        first = result.plan.windows[1].start if result.plan.calibrated and len(result.plan.windows) > 1 else result.plan.windows[0].start
        last = result.plan.windows[-1].end
        inside = [s for s in sessions if first <= s["ts"] < last and not s["public"]]
        kwh = sum(s["kwh"] for s in inside)
        cost_valid = bool(inside) and all(s["cost_valid"] for s in inside)
        recorded_cost = sum(s["cost"] for s in inside) if cost_valid else None
        solar = sum(s["solar"] for s in inside)
        published["recorded"] = {
            "sessions": len(inside), "kwh": _round(kwh, 3), "cost": _round(recorded_cost, 2), "solar_kwh": _round(solar, 3),
            "powerwall_kwh": _round(sum(s["powerwall"] for s in inside), 3),
            "unit": _round(recorded_cost / kwh, 3) if recorded_cost is not None and kwh > 1e-9 else None,
        }
        plan = published["plan"]
        # what the recorded energy would have cost from the grid at the price the planned charging paid per kWh: an estimate,
        # because the recorded energy also holds drives and standby that the replay does not have
        # only with recorded solar energy: without it there is nothing to value, and a difference would only be timing
        if recorded_cost is not None and kwh > 1e-9 and solar > 1e-9 and plan and plan["unit"] is not None:
            without_solar = kwh * plan["unit"]
            published["estimate"] = {
                "cost_without_solar": _round(without_solar, 2), "recorded_cost": _round(recorded_cost, 2),
                "solar_advantage": _round(without_solar - recorded_cost, 2), "kwh": _round(kwh, 3), "unit": plan["unit"],
            }

        months: dict[str, dict[str, Any]] = {}

        def month_of(key: str) -> dict[str, Any]:
            return months.setdefault(key, {"month": key, "windows": 0, "planned_kwh": 0.0, "planned_cost": 0.0, "recorded_kwh": 0.0, "recorded_cost": 0.0, "recorded_valid": True})

        # both sides by the start of the connection / the session
        for row in result.plan.windows:
            month = month_of(self._economy_local(row.start).strftime("%Y-%m"))
            month["windows"] += 1
            month["planned_kwh"] += row.grid_kwh
            month["planned_cost"] += row.cost
        for session in inside:
            month = month_of(self._economy_local(session["ts"]).strftime("%Y-%m"))
            month["recorded_kwh"] += session["kwh"]
            month["recorded_cost"] += session["cost"]
            month["recorded_valid"] = month["recorded_valid"] and session["cost_valid"]
        published["months"] = [
            {
                "month": m["month"], "windows": m["windows"], "planned_kwh": _round(m["planned_kwh"], 2), "planned_cost": _round(m["planned_cost"], 2),
                "recorded_kwh": _round(m["recorded_kwh"], 2), "recorded_cost": _round(m["recorded_cost"], 2) if m["recorded_valid"] else None,
            }
            for _, m in sorted(months.items())
        ]
        return published

    # ------------------------------------------------------------------ #
    # publishing
    # ------------------------------------------------------------------ #

    def _what_if_no_solar_attempt(self, status: str, reason: str | None = None) -> None:
        self.economy_db["what_if_no_solar_attempt"] = {"status": status, "reason": reason, "at": self._economy_ts(self.now())}
        self.mark_dirty("economy")
        self.set_attr("what_if_no_solar", "attempt", self.economy_db["what_if_no_solar_attempt"])

    def what_if_no_solar_fail(self, reason: str) -> None:
        """job: the coordinator could not prepare the calculation (the saved result stays)."""
        self._what_if_no_solar_attempt("unavailable", reason)
        self.publish_what_if_no_solar()

    def load_what_if_no_solar(self) -> None:
        """Startup: a calculation that was running when Home Assistant stopped did not finish."""
        if not isinstance(self.economy_db.get("what_if_no_solar"), dict):
            self.economy_db.pop("what_if_no_solar", None)
        attempt = self.economy_db.get("what_if_no_solar_attempt")
        if isinstance(attempt, dict) and attempt.get("status") == "running":
            self.economy_db["what_if_no_solar_attempt"] = {**attempt, "status": "unavailable", "reason": "interrupted"}
        elif not isinstance(attempt, dict):
            self.economy_db.pop("what_if_no_solar_attempt", None)
        self.publish_what_if_no_solar()

    def publish_what_if_no_solar(self, result: dict[str, Any] | None = None) -> None:
        """The latest successful calculation (or the saved one after a restart); the state is the estimated solar advantage."""
        result = result or self.economy_db.get("what_if_no_solar")
        estimate = (result or {}).get("estimate")
        self.set_state("what_if_no_solar", estimate.get("solar_advantage") if estimate else None)
        self.set_attr("what_if_no_solar", "result", result)
        self.set_attr("what_if_no_solar", "attempt", self.economy_db.get("what_if_no_solar_attempt"))
