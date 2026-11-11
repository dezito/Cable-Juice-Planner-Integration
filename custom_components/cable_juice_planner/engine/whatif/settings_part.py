"""What-if Scenario B in the engine: two sets of CJP settings replayed over the same history.

The baseline is the CURRENT settings (never the plan CJP made at the time: settings history is
not stored); the alternative overrides the daily homecoming reserve (min_daily_battery_level)
and/or the maximum recommended level for the calculation only. Neither the live settings, the
plan, the car nor the charger are touched. The result is saved separately (economy database, key
"what_if_settings") and shown on sensor.<prefix>_what_if_settings.

It shares the period, the starting battery level and the input data with Scenario A (see
engine_part.py); the policy is the small modelled one in replay.py (replay_settings).
"""
from __future__ import annotations

import math
from typing import Any

from .engine_part import WHAT_IF_WINDOW_ROWS, _round
from .replay import CjpSettings, PolicyResult, Readiness, SettingsResult, replay_settings


def _positive(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return float(min(value, 100.0))
    return None


class WhatIfSettings:
    """Scenario B: the effect of the daily homecoming reserve and the maximum recommended level."""

    # ------------------------------------------------------------------ #
    # settings
    # ------------------------------------------------------------------ #

    def what_if_settings_values(self) -> tuple[CjpSettings, CjpSettings, dict[str, str]]:
        """(baseline, alternative, where each alternative value came from). 0 means "the live setting"."""
        live = CjpSettings(
            reserve=float(self.get_min_daily_battery_level()),
            maximum=float(self.get_max_recommended_charge_limit_battery_level()),
        )
        reserve = _positive(self.setting("what_if_min_daily_battery_level", 0.0))
        maximum = _positive(self.setting("what_if_max_recommended_battery_level", 0.0))
        alternative = CjpSettings(reserve if reserve is not None else live.reserve, maximum if maximum is not None else live.maximum)
        sources = {
            "reserve": "override" if reserve is not None else "live_setting",
            "maximum": "override" if maximum is not None else "live_setting",
        }
        return live, alternative, sources

    # ------------------------------------------------------------------ #
    # job: what_if_settings_prepare (tells the coordinator where to read the starting level)
    # ------------------------------------------------------------------ #

    def what_if_settings_prepare(self) -> dict[str, Any]:
        """Fixes the period and the first connection the starting level belongs to (the same selection the run uses)."""
        period_start, period_end = self.what_if_period()
        baseline, _, _ = self.what_if_settings_values()
        _, coverage, _ = self._what_if_inputs(period_start, period_end, None, "unavailable", baseline.maximum, None)
        manual = self.setting("what_if_start_battery_level", 0.0)
        manual = float(manual) if _positive(manual) is not None else None
        self._what_if_settings_attempt("running")
        return {"first_window_start": coverage["first_window"], "manual_start_soc": manual, "period": [period_start, period_end]}

    # ------------------------------------------------------------------ #
    # job: what_if_settings_run
    # ------------------------------------------------------------------ #

    def what_if_settings_run(self, start: dict[str, Any]) -> dict[str, Any]:
        """start: {"soc": percent | None, "source": text, and from what_if_settings_prepare "period" and
        "first_window_start"}. Returns the result and publishes it."""
        now = self._economy_ts(self.now())
        prepared_period = start.get("period")
        period_start, period_end = (float(prepared_period[0]), float(prepared_period[1])) if prepared_period else self.what_if_period()
        baseline, alternative, sources = self.what_if_settings_values()
        data, coverage, _ = self._what_if_inputs(
            period_start, period_end, start.get("soc"), str(start.get("source") or "unavailable"), baseline.maximum, None
        )
        reason = None
        if "first_window_start" in start and coverage["first_window"] != start["first_window_start"]:
            reason = "start_level_stale"  # a connection ended meanwhile: the level read belongs to another first connection
        elif baseline == alternative:
            reason = "no_change"  # nothing to compare: both overrides are 0 or equal to the live settings
        elif coverage["windows"] > 1 and coverage["drives_in_gaps"] == 0:
            reason = "no_drives"  # without a drive that can be applied the demand is unknown, not zero
        if reason is not None:
            result = SettingsResult(
                status="unavailable", reason=reason, windows_total=coverage["windows"], provenance=dict(data.provenance),
                warnings=data.warnings,
            )
        else:
            result = replay_settings(data, baseline, alternative)
        published = self._what_if_settings_result(result, coverage, now, period_start, period_end, baseline, alternative, sources)
        self._what_if_settings_attempt(result.status, result.reason)
        if result.status in ("ok", "partial"):
            # only a successful calculation replaces the saved one
            self.economy_db["what_if_settings"] = published
            self.mark_dirty("economy")
        self.publish_what_if_settings(published if result.status in ("ok", "partial") else None)
        return published

    # ------------------------------------------------------------------ #
    # result
    # ------------------------------------------------------------------ #

    @staticmethod
    def _settings_policy(policy: PolicyResult | None, readiness: Readiness | None) -> dict[str, Any] | None:
        if policy is None or readiness is None:
            return None
        return {
            "cost": _round(policy.cost, 2), "grid_kwh": _round(policy.grid_kwh, 3), "charged_kwh": _round(policy.charged_kwh, 3),
            "loss_kwh": _round(policy.loss_kwh, 3), "unmet_target_kwh": _round(policy.unmet_target_kwh, 3),
            "unmet_driving_kwh": _round(policy.unmet_driving_kwh, 3), "targets_met": policy.targets_met,
            "windows": len(policy.windows), "start_soc": _round(policy.start_soc, 1), "final_soc": _round(policy.final_soc, 1),
            "lowest_soc": _round(policy.lowest_soc, 1), "arrivals": readiness.arrivals,
            "arrivals_below_reserve": readiness.arrivals_below_reserve, "lowest_arrival": _round(readiness.lowest_arrival, 1),
        }

    def _what_if_settings_result(
        self, result: SettingsResult, coverage: dict, now: float, period_start: float, period_end: float,
        baseline: CjpSettings, alternative: CjpSettings, sources: dict[str, str],
    ) -> dict[str, Any]:
        published: dict[str, Any] = {
            "model_version": result.model_version,
            "status": result.status,
            "reason": result.reason,
            "computed_at": now,
            "period": {"start": period_start, "end": period_end, "days": self.what_if_days()},
            "settings": {
                "baseline": {"reserve": baseline.reserve, "maximum": baseline.maximum},
                "alternative": {"reserve": alternative.reserve, "maximum": alternative.maximum, "sources": sources},
            },
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
            "baseline": self._settings_policy(result.baseline, result.baseline_readiness),
            "alternative": self._settings_policy(result.alternative, result.alternative_readiness),
            "average_price": _round(result.average_price, 4),
            "difference": {key: _round(value, 3) for key, value in result.difference.items()} if result.difference else None,
            "months": [],
            "rows": [],
        }
        if result.baseline is None or result.alternative is None:
            return published

        months: dict[str, dict[str, Any]] = {}
        rows = []
        for index, (base, alt) in enumerate(zip(result.baseline.windows, result.alternative.windows)):
            key = self._economy_local(base.end).strftime("%Y-%m")
            month = months.setdefault(key, {"month": key, "windows": 0, "baseline_cost": 0.0, "alternative_cost": 0.0, "baseline_kwh": 0.0, "alternative_kwh": 0.0})
            month["windows"] += 1
            month["baseline_cost"] += base.cost
            month["alternative_cost"] += alt.cost
            month["baseline_kwh"] += base.grid_kwh
            month["alternative_kwh"] += alt.grid_kwh
            rows.append({
                "start": base.start, "end": base.end,
                "baseline_target": _round(result.targets["baseline"][index], 1), "alternative_target": _round(result.targets["alternative"][index], 1),
                "baseline_out": _round(base.soc_out, 1), "alternative_out": _round(alt.soc_out, 1),
                "baseline_cost": _round(base.cost, 2), "alternative_cost": _round(alt.cost, 2),
            })
        published["months"] = [
            {k: (_round(v, 3) if isinstance(v, float) else v) for k, v in month.items()} for _, month in sorted(months.items())
        ]
        published["rows"] = rows[-WHAT_IF_WINDOW_ROWS:]
        return published

    # ------------------------------------------------------------------ #
    # publishing
    # ------------------------------------------------------------------ #

    def _what_if_settings_attempt(self, status: str, reason: str | None = None) -> None:
        self.economy_db["what_if_settings_attempt"] = {"status": status, "reason": reason, "at": self._economy_ts(self.now())}
        self.mark_dirty("economy")
        self.set_attr("what_if_settings", "attempt", self.economy_db["what_if_settings_attempt"])

    def what_if_settings_fail(self, reason: str) -> None:
        """job: the coordinator could not prepare the calculation (the saved result stays)."""
        self._what_if_settings_attempt("unavailable", reason)
        self.publish_what_if_settings()

    def load_what_if_settings(self) -> None:
        """Startup: a calculation that was running when Home Assistant stopped did not finish."""
        saved = self.economy_db.get("what_if_settings")
        if not isinstance(saved, dict):
            self.economy_db.pop("what_if_settings", None)
        attempt = self.economy_db.get("what_if_settings_attempt")
        if isinstance(attempt, dict) and attempt.get("status") == "running":
            self.economy_db["what_if_settings_attempt"] = {**attempt, "status": "unavailable", "reason": "interrupted"}
        elif not isinstance(attempt, dict):
            self.economy_db.pop("what_if_settings_attempt", None)
        self.publish_what_if_settings()

    def publish_what_if_settings(self, result: dict[str, Any] | None = None) -> None:
        """The latest successful calculation (or the saved one after a restart) on sensor what_if_settings."""
        result = result or self.economy_db.get("what_if_settings")
        # the state is the whole-period figure: a replay that stopped early has no such number
        complete = (result or {}).get("status") == "ok" and (result or {}).get("comparable")
        difference = (result or {}).get("difference") if complete else None
        self.set_state("what_if_settings", difference.get("adjusted_cost") if difference else None)
        self.set_attr("what_if_settings", "result", result)
        self.set_attr("what_if_settings", "attempt", self.economy_db.get("what_if_settings_attempt"))
