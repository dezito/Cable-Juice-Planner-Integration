"""What-if Scenario A in the engine: collects the stored data, replays it and publishes the result.

Runs as three jobs: what_if_collect (a quick copy of the data the replay reads), the replay itself (Smart and charging at
once over the connections, next to what CJP actually charged: the coordinator runs it on its own thread, never the worker
the charger control uses) and what_if_finish (publish). Nothing here changes the live plan, a setting, the car or the
charger: the replay only reads CJP's databases and its result is saved separately (economy database, key "what_if") and
shown on sensor.<prefix>_what_if.

The starting SOC is read by the coordinator from the recorder (see coordinator/what_if.py) or
entered by the user; without it the replay is unavailable. It is never defaulted.
"""
from __future__ import annotations

import datetime
import logging
import math
from collections.abc import Callable, Collection
from copy import deepcopy
from dataclasses import replace
from typing import Any

from ...const import WEEKDAYS
from ..economy import ECONOMY_WINDOW_SETTLE
from ..soc_power import planning_power_cap
from .derive import derive_windows
from .inputs import build_replay_input, loss_factor_from_table, power_cap_from_model
from .recorded import TOLERANCE, allocate, history_rows, outside_kwh
from .replay import REPLAY_MODEL_VERSION, PolicyResult, Recorded, ReplayResult, replay

_LOGGER = logging.getLogger(__name__)

OUTSIDE_KWH_TOLERANCE = 1.0  # kWh of CJP charging that lies outside every connection before the card says so
OUTSIDE_SHARE = 0.1  # of the kWh CJP charged during the estimated connections: more than this outside them and they are not used
ESTIMATE_COVERAGE_KEYS = ("derived_status", "derived_reason", "modelled_status", "modelled_reason")
ANNUAL_MIN_DAYS = 30  # under this a year is not estimated: the connections say too little about a year
ANNUAL_MEASURED_DAYS = 360  # from this the connections cover a year and the figure is the last year, not an estimate
DEFAULT_DEPARTURE_SECONDS = 7 * 3600  # with Fill up the car is taken to leave at 07:00 (like the planner assumes)
WHAT_IF_DAYS = (7, 365)  # the shortest, and the lowest the longest can be (see what_if_max_days: the days kept, 403 by default)
WHAT_IF_DEFAULT_DAYS = 30
DAY_SELECTIONS = ("all", "work_plan", "monday_friday", "weekend")
WHAT_IF_WINDOW_ROWS = 60  # the newest connection windows kept in the result for the dashboard


def _round(value: Any, digits: int) -> float | None:
    return round(float(value), digits) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


class WhatIf:
    """Scenario A: what CJP actually charged (its charging history, as booked) against Smart and charging at once (replayed)."""

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
        """The target battery level: the What-if maximum recommended level (shared with the settings comparison), else the live one."""
        override = self.setting("what_if_max_recommended_battery_level", 0.0)
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
            period_start, period_end, start_soc, source, target, self.what_if_smart_weekdays(), recorded=True
        )
        return data, coverage, target, target_source, sessions

    def _what_if_inputs(
        self, period_start: float, period_end: float, start_soc: float | None, source: str,
        target: float, smart_weekdays: frozenset[int] | None, recorded: bool = False,
    ):
        """The replay input and coverage for a target and day selection (Scenarios A and B).

        recorded (scenario A): the measured connections the charging history reaches, as the newest unbroken stretch of
        them, each with what CJP charged in it (ReplayInput.recorded, from the charging history) and the planned departure."""
        battery = self.config["ev_car"].get("battery_size")
        max_kw = float(self.max_kwh_charging)
        kws = {round(watt / 1000.0, 3) for watt in self.charging_power_dict()} | {round(max_kw, 3)}
        loss_table: dict[float, float] = {}
        for kw in kws:
            try:
                loss_table[kw] = float(self.charging_loss_factor(kw))
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("What-if: no charging loss at %s kW: %s", kw, err)
        model = deepcopy(self.soc_power_model) if self.soc_power_learning_enabled() else None
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
        def build(windows, estimates=False, start=None):
            """estimates: with the connections estimated from the battery statistics and modelled from the work plan (always
            so for scenario B); start: the period starts here instead (the older estimated connections are left out)."""
            use = estimates or not recorded
            return build_replay_input(
                windows=windows,
                prices={**history, **copies, **stored},
                history_hours=list(history),
                refund=self._solar_refund() or 0.0,
                derived=self.economy_db.get("derived_windows") if use else None,
                work_plan=self.what_if_work_plan() if use else None,
                # the model never covers a time a measured connection exists in, whichever of them are compared
                all_windows=self.economy_db["windows"] if estimates else None,
                drives=self.analysis_db.get("drives", []),
                public_sessions=public,
                period_start=period_start if start is None else start,
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

        data, coverage = build(self.economy_db["windows"])
        if recorded:
            data, coverage = self._what_if_with_recorded(data, coverage, build)
        return data, coverage, sessions

    # ------------------------------------------------------------------ #
    # scenario A: what CJP itself charged
    # ------------------------------------------------------------------ #

    def _what_if_with_recorded(self, data: Any, coverage: dict[str, Any], build) -> tuple[Any, dict[str, Any]]:
        """The newest unbroken stretch of connections that the charging history of CJP reaches (and that can be read), each with
        what CJP charged in it and the planned departure: the measured connections first, then the estimated ones before them
        (from the battery statistics or the work plan, marked as estimates) as far back as the history reaches."""
        data, coverage = self._what_if_measured_stretch(data, coverage, build)
        if not data.windows:
            return data, {**coverage, "recorded_estimates": None, "recorded_estimated_windows": 0}
        if coverage["recorded_left_out"]:
            # an older measured connection could not be read: an estimate before it would leave a hole in the middle
            return data, {**coverage, "recorded_estimates": "not_all_measured", "recorded_estimated_windows": 0}
        return self._what_if_estimated_stretch(data, coverage, build)

    def _what_if_measured_stretch(self, data: Any, coverage: dict[str, Any], build) -> tuple[Any, dict[str, Any]]:
        """The measured connections: the newest unbroken stretch that the charging history reaches and that can be read."""
        all_windows = list(data.windows)
        records, reasons, split, outside = self._what_if_recorded_charging(all_windows)
        # the newest connections that are not settled yet are left aside: they do not break the stretch, the next run has them
        newest = len(all_windows)
        while newest and reasons.get(all_windows[newest - 1].start) == "not_settled":
            newest -= 1
        stretch: list[Any] = []
        for window in reversed(all_windows[:newest]):
            if window.start not in records:
                break
            stretch.append(window)
        stretch.reverse()
        left_out = newest - len(stretch)
        # why the stretch stops: the reason of the newest connection before it that has no record
        stop = all_windows[newest - len(stretch) - 1] if left_out else None
        reason = reasons.get(stop.start) if stop is not None else None
        stop_at = stop.end if stop is not None else None
        if not stretch:
            return replace(data, windows=(), recorded={}), {
                **coverage, "windows": 0, "first_window": None, "last_window": None, "recorded_candidates": len(all_windows),
                "recorded_windows": 0, "recorded_left_out": left_out, "recorded_reason": reason, "recorded_stop_at": stop_at,
                "recorded_outside_kwh": 0.0, "recorded_split_entries": 0, "recorded_measured_from": None,
            }
        data, coverage = build([{"start": w.start, "end": w.end} for w in stretch])
        windows = tuple(replace(w, deadline=self._what_if_deadline(w)) for w in data.windows)
        warnings = (*data.warnings, *(("windows_before_recorded_gap_left_out",) if left_out else ()))
        # the entries of the history that no connection of the stretch holds, and the merged days split between two
        first, last = windows[0].start, windows[-1].end
        outside_total = outside(first, last)
        split_days = split(first, last)
        if outside_total > OUTSIDE_KWH_TOLERANCE:
            warnings = (*warnings, "recorded_sessions_outside_connections")
        if split_days:
            warnings = (*warnings, "recorded_days_split")
        coverage = {
            **coverage, "recorded_candidates": len(all_windows), "recorded_windows": len(windows),
            "recorded_left_out": left_out, "recorded_reason": reason, "recorded_stop_at": stop_at,
            "recorded_outside_kwh": round(outside_total, 3), "recorded_split_entries": split_days,
            "recorded_measured_from": first,
        }
        return replace(data, windows=windows, warnings=warnings, recorded={w.start: records[w.start] for w in windows}), coverage

    def _what_if_estimated_stretch(self, data: Any, coverage: dict[str, Any], build) -> tuple[Any, dict[str, Any]]:
        """Add the connections estimated from the battery statistics and modelled from the work plan before the measured ones,
        as far back as the charging history reaches them (the card marks them as estimates). Nothing is added, and the reason
        is kept, when there are none, when the history does not reach any, or when much of what CJP charged lies outside them
        (that energy would be missing from CJP's column and make it look cheaper)."""
        measured = [{"start": w.start, "end": w.end} for w in data.windows]
        estimates, estimate_coverage = build(measured, estimates=True)
        kept = {key: estimate_coverage.get(key) for key in ESTIMATE_COVERAGE_KEYS}

        def without(reason: str, **extra: Any) -> tuple[Any, dict[str, Any]]:
            return data, {**coverage, **kept, "recorded_estimates": reason, "recorded_estimated_windows": 0, **extra}

        if not any(w.derived for w in estimates.windows):
            # (every estimated connection can have been left out for want of a stored price: no price is made up for an estimate)
            return without("no_prices" if estimate_coverage.get("estimated_unpriced_left_out") else "none_available")
        first_count = len(estimates.windows)
        stop: tuple[float, str | None] | None = None
        for _ in range(3):
            windows = list(estimates.windows)
            records, reasons, split, outside = self._what_if_recorded_charging(windows, {w.start for w in windows if w.derived})
            run: list[Any] = []
            for window in reversed(windows):
                if window.start not in records:
                    break
                run.append(window)
            run.reverse()
            if len(run) == len(windows):
                break
            # the run stops at an older connection that has no record: build again from the oldest one of the run, so that the
            # drives and the start level are the ones of that period
            broken = windows[len(windows) - len(run) - 1]
            stop = (broken.end, reasons.get(broken.start))
            if not any(w.derived for w in run):
                return without("no_record", recorded_reason=stop[1], recorded_stop_at=stop[0])
            estimates, estimate_coverage = build(measured, estimates=True, start=run[0].start)
        else:
            return without("not_stable")
        derived = [w for w in windows if w.derived]
        if not derived:  # (the build from the oldest connection of the run did not give that estimated connection again)
            return without("not_stable")
        estimated_kwh = sum(records[w.start].kwh for w in derived)
        outside_estimated = outside(windows[0].start, derived[-1].end)
        if outside_estimated > OUTSIDE_KWH_TOLERANCE and outside_estimated > OUTSIDE_SHARE * (estimated_kwh + outside_estimated):
            return without("outside_charging", recorded_estimated_outside_kwh=round(outside_estimated, 3))
        windows = [replace(w, deadline=self._what_if_deadline(w)) for w in windows]
        dropped = first_count - len(windows)
        first, last = windows[0].start, windows[-1].end
        outside_total = outside(first, last)
        split_days = split(first, last)
        warnings = (*estimates.warnings, *(("windows_before_recorded_gap_left_out",) if dropped else ()))
        # (the measured stretch said these about its own period: it is part of this one)
        warnings = (*warnings, *(w for w in data.warnings if w.startswith("recorded_") and w not in warnings))
        if outside_total > OUTSIDE_KWH_TOLERANCE and "recorded_sessions_outside_connections" not in warnings:
            warnings = (*warnings, "recorded_sessions_outside_connections")
        if split_days and "recorded_days_split" not in warnings:
            warnings = (*warnings, "recorded_days_split")
        coverage = {
            **estimate_coverage, **{k: v for k, v in coverage.items() if k.startswith("recorded_")},
            "recorded_windows": len(windows), "recorded_estimated_windows": len(derived), "recorded_estimates": "used",
            "recorded_candidates": first_count, "recorded_left_out": dropped,
            "recorded_reason": stop[1] if stop else None, "recorded_stop_at": stop[0] if stop else None,
            "recorded_outside_kwh": round(outside_total, 3), "recorded_split_entries": split_days,
            "recorded_estimated_outside_kwh": round(outside_estimated, 3),
        }
        return replace(
            estimates, windows=tuple(windows), warnings=warnings, recorded={w.start: records[w.start] for w in windows}
        ), coverage

    def _what_if_recorded_charging(
        self, windows: list[Any], estimated: Collection[float] = ()
    ) -> tuple[dict[float, Recorded], dict[float, str], Callable[[float, float], int], Callable[[float, float], float]]:
        """What CJP charged in each connection, from its charging history (engine/whatif/recorded.py: every entry laid over the
        connections by overlap in time, public charging taken out). A connection that cannot be read is not in the first dict
        and says why in the second (not settled, invalid history, before the history). The last two are the number of merged
        days split between two connections, and the home kWh outside every connection, between two moments.
        estimated: the starts of the connections that are estimates (their edges are only known to about two hours)."""
        battery = self.config["ev_car"].get("battery_size")
        rows = history_rows(
            self.charging_history_db, self._economy_ts, float(battery) if isinstance(battery, (int, float)) and not isinstance(battery, bool) else None
        )
        # every measured connection takes part in the allocation, also the ones the comparison leaves out (before the period,
        # before the drive history, still open): an entry that reaches into a neighbour gives that neighbour its share, so the
        # edges of the comparison neither get energy that belongs to a connection outside it nor lose any that is theirs
        now = self._economy_ts(self.now())
        every = {
            w["start"]: (w["start"], w["end"] if w.get("end") is not None else now)
            for w in self.economy_db["windows"] if isinstance(w.get("start"), (int, float))
        }
        every.update({w.start: (w.start, w.end) for w in windows})
        allocation = allocate(rows, sorted(every.values()), now, ECONOMY_WINDOW_SETTLE, estimated)
        records = {
            start: Recorded(kwh=r["kwh"], percent=r["percent"], cost=r["cost"], local_kwh=r["local_kwh"])
            for start, r in allocation["records"].items()
        }
        weights = allocation["weights"]

        def split(first: float, last: float) -> int:
            return sum(1 for row, shares in zip(rows, weights) if len(shares) > 1 and first - TOLERANCE <= row["ts"] <= last)

        return records, allocation["reasons"], split, lambda first, last: outside_kwh(rows, weights, first, last)

    def _what_if_deadline(self, window: Any) -> float | None:
        """The planned departure inside a connection, for the charger "smart" stands for: the work plan's departure of the
        day the car leaves when that is a workday, else 07:00 with Fill up on. None: it is told no departure (or the car came
        after it, or it leaves at the real unplug first) and may use the whole connection."""
        tz = self._snap.tz
        end = datetime.datetime.fromtimestamp(window.end, tz)
        plan = (self.what_if_work_plan() or {}).get("days", {}).get(end.weekday())
        seconds = None
        if plan and plan.get("workday") and "departure" in plan:
            seconds = plan["departure"]
        elif self.switch_state("fill_up", error_state="off") == "on":
            seconds = DEFAULT_DEPARTURE_SECONDS
        if seconds is None:
            return None
        deadline = (datetime.datetime.combine(end.date(), datetime.time(0), tzinfo=tz) + datetime.timedelta(seconds=seconds)).timestamp()
        return deadline if window.start < deadline < window.end else None

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
            "first_window_start": coverage["first_window"], "last_window_end": coverage["last_window"],
            "manual_start_soc": manual, "period": [period_start, period_end],
        }

    # ------------------------------------------------------------------ #
    # job: what_if_run
    # ------------------------------------------------------------------ #

    def what_if_run(self, start: dict[str, Any]) -> dict[str, Any]:
        """start: {"soc": percent | None, "source": text, and from what_if_prepare "period" and
        "first_window_start"}. Returns the result and publishes it.

        The three steps run one after the other here; the coordinator runs the middle one (the replay) on its own
        thread so the charger control is never held up by it."""
        prepared = self.what_if_collect(start)
        result = prepared["result"] if prepared["result"] is not None else replay(prepared["calculation"])
        return self.what_if_finish(prepared, result)

    def what_if_collect(self, start: dict[str, Any]) -> dict[str, Any]:
        """Step 1 (a quick job): the replay input, or why there is nothing to replay.

        Nothing in the returned data refers to the live engine, so the replay can run on another thread."""
        now = self._economy_ts(self.now())
        prepared_period = start.get("period")
        period_start, period_end = (float(prepared_period[0]), float(prepared_period[1])) if prepared_period else self.what_if_period()
        data, coverage, target, target_source, sessions = self._what_if_build(
            period_start, period_end, start.get("soc"), str(start.get("source") or "unavailable")
        )
        weekdays = self.what_if_smart_weekdays()
        reason = None
        if ("first_window_start" in start and coverage["first_window"] != start["first_window_start"]) or (
            "last_window_end" in start and coverage["last_window"] != start["last_window_end"]
        ):
            reason = "start_level_stale"  # a connection ended meanwhile: a level read belongs to another first or last connection
        elif not data.recorded and coverage.get("recorded_candidates"):
            reason = "no_recorded_charging"  # CJP has no complete record of its own charging for any connection of the period
        elif weekdays is not None and not weekdays:
            reason = "no_days_selected"
        elif coverage["windows"] > 1 and coverage["drives_in_gaps"] == 0:
            reason = "no_drives"  # without a drive that can be applied the demand is unknown, not zero
        result = calculation = None
        if reason is not None:
            result = ReplayResult(
                status="unavailable", reason=reason, windows_total=coverage["windows"], provenance=dict(data.provenance),
                warnings=data.warnings,
            )
        else:
            end_soc = start.get("end_soc")
            end_soc = float(end_soc) if isinstance(end_soc, (int, float)) and not isinstance(end_soc, bool) and 0.0 <= end_soc <= 100.0 else None
            data.provenance["end_soc"] = str(start.get("end_source") or "unavailable") if end_soc is not None else "unavailable"
            if "statistics_hour_mean" in (start.get("source"), start.get("end_source")):
                # the mean of the hour of a plug-in or unplug holds the driving next to it: the energy balance rests on it
                data = replace(data, warnings=(*data.warnings, "levels_approximate"))
            calculation = replace(data, end_soc=end_soc, arrival_reserve=float(self.get_min_daily_battery_level()))
        return {
            "now": now, "period_start": period_start, "period_end": period_end, "data": data, "coverage": coverage,
            "target": target, "target_source": target_source, "sessions": sessions, "result": result, "calculation": calculation,
        }

    def what_if_finish(self, prepared: dict[str, Any], result: ReplayResult) -> dict[str, Any]:
        """Step 3 (a quick job): the published result, saved and shown on the sensor."""
        published = self._what_if_result(
            result, prepared["coverage"], prepared["now"], prepared["period_start"], prepared["period_end"], prepared["target"],
            prepared["target_source"], prepared["sessions"],
        )
        self._what_if_attempt(result.status, result.reason)
        if result.status in ("ok", "partial"):
            # only a successful calculation replaces the saved one
            self.economy_db["what_if"] = published
            self.mark_dirty("economy")
        self.publish_what_if(published if result.status in ("ok", "partial") else None)
        return published

    def what_if_progress(self, done: int, total: int) -> None:
        """job: the replay has finished `done` of `total` connections. Shown on the sensor only, never saved."""
        attempt = self.economy_db.get("what_if_attempt")
        if not isinstance(attempt, dict) or attempt.get("status") != "running":
            return
        self.set_attr("what_if", "attempt", {**attempt, "progress": {"done": int(done), "total": int(total)}})

    # ------------------------------------------------------------------ #
    # result
    # ------------------------------------------------------------------ #

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
            "cjp": self._policy(result.cjp),
            "smart": self._policy(result.smart),
            "asap": self._policy(result.asap),
            "average_price": _round(result.average_price, 4),
            "difference": self._differences_rounded(result.difference),
            "differences": {key: self._differences_rounded(value) for key, value in result.differences.items()} if result.differences else None,
            "energy": {key: _round(value, 2) for key, value in result.energy.items()} if result.energy else None,
            "unit_prices": {key: _round(value, 4) for key, value in result.unit_prices.items()} if result.unit_prices else None,
            "annual": None,
            "months": [],
            "rows": [],
            "derived": None,
        }
        if result.smart is None or result.asap is None or result.cjp is None:
            return published

        months: dict[str, dict[str, Any]] = {}
        levels_by_month: dict[str, dict[str, list[float]]] = {}
        rows = []
        for cjp, smart, asap in zip(result.cjp.windows, result.smart.windows, result.asap.windows):
            key = self._economy_local(smart.end).strftime("%Y-%m")
            month = months.setdefault(key, {
                "month": key, "windows": 0, "estimated_connections": 0, "cjp_cost": 0.0, "smart_cost": 0.0, "asap_cost": 0.0, "grid_kwh": 0.0,
                "smart_grid_kwh": 0.0, "asap_grid_kwh": 0.0, "targets_met": 0, "unmet_target_kwh": 0.0,
            })
            month["windows"] += 1
            month["estimated_connections"] += 1 if cjp.derived else 0  # (the connection itself is an estimate, not its prices)
            for name, window in (("cjp", cjp), ("smart", smart), ("asap", asap)):
                levels = levels_by_month.setdefault(key, {}).setdefault(name, [window.soc_in, window.soc_out])
                levels[1] = window.soc_out  # the first level in the month stays, the last one follows
            month["cjp_cost"] += cjp.cost
            month["smart_cost"] += smart.cost
            month["asap_cost"] += asap.cost
            month["grid_kwh"] += cjp.grid_kwh
            month["smart_grid_kwh"] += smart.grid_kwh
            month["asap_grid_kwh"] += asap.grid_kwh
            month["targets_met"] += 1 if cjp.unmet_target_kwh <= 1e-6 else 0
            month["unmet_target_kwh"] += cjp.unmet_target_kwh
            rows.append({
                "start": smart.start, "end": smart.end, "soc_in": _round(cjp.soc_in, 1), "soc_out": _round(cjp.soc_out, 1),
                "cjp_cost": _round(cjp.cost, 2), "smart_cost": _round(smart.cost, 2), "asap_cost": _round(asap.cost, 2),
                "kwh": _round(cjp.grid_kwh, 2), "unmet_kwh": _round(cjp.unmet_target_kwh, 2),
                "estimated": cjp.derived, "modelled": cjp.modelled,
            })
        # A month's cost is not comparable between the three: they end it at different battery levels. The difference of two
        # is therefore adjusted the way the difference of the whole period is: the change of the battery level in the month
        # is valued at what the one that holds more energy paid per kWh, so the months of a pair add up to its adjusted
        # difference. Without the end level of the period nothing is adjusted: no values.
        policies = {"cjp": result.cjp, "smart": result.smart, "asap": result.asap}
        balanced = bool(result.differences and result.differences["cjp_asap"].get("adjusted_cost") is not None)
        for first_name, second_name in (("cjp", "smart"), ("cjp", "asap"), ("smart", "asap")):
            first_policy, second_policy = policies[first_name], policies[second_name]
            charged = first_policy.charged_kwh + second_policy.charged_kwh
            ratio = (first_policy.grid_kwh + second_policy.grid_kwh) / charged if charged > 1e-9 else 1.0
            holder = first_policy if first_policy.final_soc >= second_policy.final_soc else second_policy
            paid = holder.cost / holder.grid_kwh if holder.grid_kwh > 1e-9 else result.average_price
            per_percent = float(self.config["ev_car"]["battery_size"]) / 100.0 * ratio * paid if paid is not None else None
            for key, month in months.items():
                (first_in, first_out), (second_in, second_out) = levels_by_month[key][first_name], levels_by_month[key][second_name]
                month[f"{first_name}_vs_{second_name}_adjusted"] = (
                    month[f"{first_name}_cost"] - month[f"{second_name}_cost"] - ((first_out - first_in) - (second_out - second_in)) * per_percent
                    if balanced and per_percent is not None else None
                )
        # When the three do not deliver the same only an indication is shown (like the price per kWh of the whole period): the
        # kWh CJP bought in the month at the month's price per kWh of the other, and what CJP paid minus that. Nothing when
        # one of them bought no energy in the month.
        for month in months.values():
            for other in ("smart", "asap"):
                other_kwh = month[f"{other}_grid_kwh"]
                month[f"cjp_vs_{other}_unit"] = (
                    month["cjp_cost"] - month[f"{other}_cost"] / other_kwh * month["grid_kwh"] if other_kwh > 1e-9 and month["grid_kwh"] > 1e-9 else None
                )
        published["months"] = [
            {k: (_round(v, 3) if isinstance(v, float) or v is None else v) for k, v in month.items()} for _, month in sorted(months.items())
        ]
        published["rows"] = rows[-WHAT_IF_WINDOW_ROWS:]
        published["annual"] = self._what_if_annual(result, coverage, months, any(row.derived for row in result.cjp.windows))
        published["derived"] = self._what_if_estimated_part(result)

        return published

    @staticmethod
    def _what_if_estimated_part(result: ReplayResult) -> dict[str, Any] | None:
        """The connections that are themselves estimates (from the battery statistics or the work plan), on their own: how many,
        and what each calculation paid in them. Their cost is not adjusted for the battery level: it is only shown."""
        if result.cjp is None or result.smart is None or result.asap is None:
            return None
        indexes = [i for i, row in enumerate(result.cjp.windows) if row.derived]
        if not indexes:
            return None

        def part(policy: PolicyResult) -> tuple[float, float]:
            rows = [policy.windows[i] for i in indexes if i < len(policy.windows)]
            return sum(r.cost for r in rows), sum(r.grid_kwh for r in rows)

        (cjp_cost, cjp_kwh), (smart_cost, smart_kwh), (asap_cost, asap_kwh) = part(result.cjp), part(result.smart), part(result.asap)
        return {
            "windows": len(indexes), "modelled": sum(1 for i in indexes if result.cjp.windows[i].modelled),
            "first": result.cjp.windows[indexes[0]].start, "last": result.cjp.windows[indexes[-1]].end,
            "cjp_cost": _round(cjp_cost, 2), "smart_cost": _round(smart_cost, 2), "asap_cost": _round(asap_cost, 2),
            "cjp_kwh": _round(cjp_kwh, 3), "smart_kwh": _round(smart_kwh, 3), "asap_kwh": _round(asap_kwh, 3),
        }

    @staticmethod
    def _differences_rounded(difference: dict[str, float | None] | None) -> dict[str, float | None] | None:
        return {key: _round(value, 3) for key, value in difference.items()} if difference else None

    def _what_if_annual(
        self, result: ReplayResult, coverage: dict, months: dict[str, dict[str, Any]], has_estimated_connections: bool = False
    ) -> dict[str, Any] | None:
        """What the differences between the three would be per year, from the connections the comparison covers.

        Only the adjusted differences are scaled: the raw costs end at different battery levels, and scaling that difference
        by 365 / the days would multiply it. A year or more of connections is the last year (scaled to exactly 365 days);
        less is an ESTIMATE that scales by the days the connections span and says nothing about the seasons the data does
        not cover. Connections that are themselves estimates make it an ESTIMATE too, however long. Without the end level
        there is no adjusted difference and no estimate."""
        first, last = coverage.get("first_window"), coverage.get("last_window")
        if not first or not last or result.cjp is None or not result.comparable or not result.differences:
            return None
        if result.windows_replayed < result.windows_total:
            return None  # a partial result: the days of the whole stretch do not belong to the differences
        days = (float(last) - float(first)) / 86400.0
        if days < ANNUAL_MIN_DAYS:
            return {"available": False, "basis_days": round(days, 1), "minimum_days": ANNUAL_MIN_DAYS}
        scale = 365.0 / days
        adjusted = {key: value.get("adjusted_cost") for key, value in result.differences.items()}
        if any(value is None for value in adjusted.values()):
            return {"available": False, "basis_days": round(days, 1), "minimum_days": ANNUAL_MIN_DAYS, "reason": "not_balanced"}
        return {
            "available": True,
            "method": "measured" if days >= ANNUAL_MEASURED_DAYS and not has_estimated_connections else "estimated",
            "basis_days": round(days, 1), "scale": round(scale, 3), "months_covered": len(months),
            "cjp_vs_asap": _round(adjusted["cjp_asap"] * scale, 0), "cjp_vs_smart": _round(adjusted["cjp_smart"] * scale, 0),
            "smart_vs_asap": _round(adjusted["smart_asap"] * scale, 0),
        }

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
        elif not isinstance(saved.get("model_version"), int) or saved["model_version"] < REPLAY_MODEL_VERSION:
            # a result of an earlier model would be shown under the labels of this one: it is dropped, and the card asks for
            # a new calculation
            self.economy_db.pop("what_if", None)
            self.mark_dirty("economy")
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
