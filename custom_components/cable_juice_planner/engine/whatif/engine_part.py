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
from collections import Counter
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
from .replay import GAP_TOLERANCE, REPLAY_MODEL_VERSION, PolicyResult, Recorded, ReplayResult, replay_segments

_LOGGER = logging.getLogger(__name__)

OUTSIDE_KWH_TOLERANCE = 1.0  # kWh of CJP charging that lies outside every connection before the card says so
OUTSIDE_SHARE = 0.1  # of the kWh CJP charged during the estimated connections: more than this outside them and they are not used
ESTIMATE_COVERAGE_KEYS = ("derived_status", "derived_reason", "modelled_status", "modelled_reason")


def _hours_of(window: Any):
    hour = window.start - window.start % 3600.0
    while hour < window.end:
        yield hour
        hour += 3600.0
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

        recorded (scenario A): the connections the charging history reaches and that can be read, each with what CJP charged in it
        (ReplayInput.recorded, from the charging history) and the planned departure, and the stretches they form without a gap
        (coverage["recorded_segments"]; see _what_if_calculations for the replay input of each)."""
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
        def build(windows, estimates=False, start=None, keep_unpriced=False):
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
                keep_unpriced=keep_unpriced,
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
        """Every connection of the period that the charging history of CJP reaches and that can be read, each with what CJP
        charged in it and the planned departure, in stretches without a gap (a connection that cannot be read ends a stretch,
        the comparison goes on after it): the measured connections, and the older ones estimated from the battery statistics or
        modelled from the work plan (marked as estimates) as far back as the period asked for and the history reach."""
        measured, measured_coverage = self._what_if_select(data, coverage)
        if not data.windows:
            return measured, {**measured_coverage, "recorded_estimates": None, "recorded_estimated_windows": 0}
        estimates, estimate_coverage = build([{"start": w.start, "end": w.end} for w in data.windows], estimates=True, keep_unpriced=True)
        kept = {key: estimate_coverage.get(key) for key in ESTIMATE_COVERAGE_KEYS}
        if not any(w.derived for w in estimates.windows):
            return measured, {**measured_coverage, **kept, "recorded_estimates": "none_available"}
        selected, selected_coverage = self._what_if_select(estimates, estimate_coverage)
        used = selected_coverage["recorded_estimated_windows"]

        def without(reason: str) -> tuple[Any, dict[str, Any]]:
            return measured, {
                **measured_coverage, **kept, "recorded_estimates": reason, "recorded_estimated_windows": 0,
                "estimated_unpriced_left_out": selected_coverage["estimated_unpriced_left_out"],
                "estimated_unpriced_until": selected_coverage["estimated_unpriced_until"],
                "recorded_estimated_outside_kwh": selected_coverage["recorded_estimated_outside_kwh"],
                **(
                    {"recorded_reason": selected_coverage["recorded_reason"], "recorded_stop_at": selected_coverage["recorded_stop_at"]}
                    if reason == "no_record" else {}
                ),
            }

        if not used:
            derived = sum(1 for w in estimates.windows if w.derived)
            return without("no_prices" if selected_coverage["estimated_unpriced_left_out"] >= derived else "no_record")
        outside_estimated = selected_coverage["recorded_estimated_outside_kwh"]
        if outside_estimated > OUTSIDE_KWH_TOLERANCE and outside_estimated > OUTSIDE_SHARE * (selected_coverage["recorded_estimated_kwh"] + outside_estimated):
            return without("outside_charging")
        return selected, {**selected_coverage, "recorded_estimates": "used"}

    def _what_if_select(self, data: Any, coverage: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        """The connections of `data` that can be read from the charging history, with what CJP charged in each, and the stretches
        they form (a connection that cannot be read, or an estimated one without a stored price, ends a stretch). The ones
        left out are counted with their reason."""
        windows = list(data.windows)
        estimated = {w.start for w in windows if w.derived}
        records, reasons, split, outside = self._what_if_recorded_charging(windows, estimated)
        # an estimated connection is not priced by estimate: no price is made up for something that is itself an estimate
        unpriced = {w.start for w in windows if w.derived and any(h not in data.prices for h in _hours_of(w))}
        skipped: dict[float, str] = {}
        for window in windows:
            if window.start not in records:
                skipped[window.start] = reasons.get(window.start, "invalid_history")
            elif window.start in unpriced:
                skipped[window.start] = "no_prices"
        segments: list[list[Any]] = []
        previous = False
        for window in windows:
            if window.start in skipped:
                previous = False
                continue
            if previous:
                segments[-1].append(window)
            else:
                segments.append([window])
            previous = True
        readable = [replace(w, deadline=self._what_if_deadline(w)) for w in windows if w.start not in skipped]
        counts = dict(Counter(skipped.values()))
        left_out = sum(1 for reason in skipped.values() if reason != "not_settled")  # (one that is not settled yet is no gap)
        newest = windows[-1] if windows else None
        base = {
            **coverage, "estimated_unpriced_left_out": len(unpriced),
            "estimated_unpriced_until": max((w.end for w in windows if w.start in unpriced), default=None),
            "recorded_candidates": len(windows), "recorded_windows": len(readable), "recorded_left_out": left_out,
            "recorded_skipped": counts, "recorded_estimated_outside_kwh": 0.0, "recorded_estimated_kwh": 0.0,
        }
        if not readable:
            reason_window = next((w for w in reversed(windows) if w.start in skipped), newest)
            return replace(data, windows=(), recorded={}), {
                **base, "windows": 0, "first_window": None, "last_window": None, "recorded_segments": [], "covered_days": 0.0,
                "recorded_reason": skipped.get(reason_window.start) if reason_window is not None else None,
                "recorded_stop_at": reason_window.end if reason_window is not None else None,
                "recorded_outside_kwh": 0.0, "recorded_split_entries": 0, "recorded_measured_from": None, "recorded_estimated_windows": 0,
            }
        first, last = readable[0].start, readable[-1].end
        before = [w for w in windows if w.start < first and w.start in skipped]  # why the data does not reach further back
        stop = before[-1] if before else None
        outside_total, split_days = outside(first, last), split(first, last)
        warnings = list(data.warnings)
        for flag, wanted in (
            ("windows_before_recorded_gap_left_out", bool(left_out)),
            ("estimated_connections_without_prices_left_out", bool(unpriced)),
            ("recorded_sessions_outside_connections", outside_total > OUTSIDE_KWH_TOLERANCE),
            ("recorded_days_split", bool(split_days)),
            ("recorded_segments", len(segments) > 1),
        ):
            if wanted and flag not in warnings:
                warnings.append(flag)
        spans = [{"start": s[0].start, "end": s[-1].end, "windows": len(s), "estimated": sum(1 for w in s if w.derived)} for s in segments]
        events = [e.ts for e in data.events if any(s[0].start <= e.ts < s[-1].end for s in segments)]
        gaps = [(a.end - GAP_TOLERANCE, b.start) for s in segments for a, b in zip(s, s[1:])]
        needed = {h for w in readable for h in _hours_of(w)}
        covered = sum(1 for h in needed if h in data.prices)
        derived = [w for w in readable if w.derived]
        measured = [w for w in readable if not w.derived]
        coverage = {
            **base, "windows": len(readable), "first_window": first, "last_window": last, "recorded_segments": spans,
            "covered_days": round(sum(s["end"] - s["start"] for s in spans) / 86400.0, 3),
            "drives": len(events), "drives_in_gaps": sum(1 for ts in events if any(lo <= ts < hi for lo, hi in gaps)),
            "connection_gaps": len(gaps), "price_hours_needed": len(needed), "price_hours_covered": covered,
            "price_hours_missing": len(needed) - covered,
            "recorded_reason": reasons.get(stop.start) or skipped.get(stop.start) if stop is not None else None,
            "recorded_stop_at": stop.end if stop is not None else None,
            "recorded_outside_kwh": round(outside_total, 3), "recorded_split_entries": split_days,
            "recorded_measured_from": measured[0].start if measured else None, "recorded_estimated_windows": len(derived),
            "recorded_estimated_kwh": round(sum(records[w.start].kwh for w in derived), 3),
            "recorded_estimated_outside_kwh": round(outside(derived[0].start, derived[-1].end), 3) if derived else 0.0,
        }
        return replace(
            data, windows=tuple(readable), warnings=tuple(warnings), recorded={w.start: records[w.start] for w in readable}
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
        """Fixes the period and the stretches of connections the levels belong to (the same selection the run uses): the battery
        level is read at the start and at the end of every stretch."""
        period_start, period_end = self.what_if_period()
        _, coverage, _, _, _ = self._what_if_build(period_start, period_end, None, "unavailable")
        manual = self.setting("what_if_start_battery_level", 0.0)
        manual = float(manual) if isinstance(manual, (int, float)) and not isinstance(manual, bool) and math.isfinite(manual) and manual > 0 else None
        self._what_if_attempt("running")
        return {
            "first_window_start": coverage["first_window"], "last_window_end": coverage["last_window"],
            "manual_start_soc": manual, "period": [period_start, period_end],
            "segments": [[s["start"], s["end"]] for s in coverage.get("recorded_segments") or []],
        }

    # ------------------------------------------------------------------ #
    # job: what_if_run
    # ------------------------------------------------------------------ #

    def what_if_run(self, start: dict[str, Any]) -> dict[str, Any]:
        """start: {"soc": percent | None, "source": text, "levels": [{"soc", "source", "end_soc", "end_source"} per stretch] and
        from what_if_prepare "period", "first_window_start" and "segments"}. Returns the result and publishes it.

        The three steps run one after the other here; the coordinator runs the middle one (the replay) on its own
        thread so the charger control is never held up by it."""
        prepared = self.what_if_collect(start)
        result = prepared["result"] if prepared["result"] is not None else replay_segments(prepared["calculations"])
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
        segments = [[s["start"], s["end"]] for s in coverage.get("recorded_segments") or []]
        if (
            ("first_window_start" in start and coverage["first_window"] != start["first_window_start"])
            or ("last_window_end" in start and coverage["last_window"] != start["last_window_end"])
            or ("segments" in start and [list(s) for s in start["segments"]] != segments)
        ):
            reason = "start_level_stale"  # a connection ended meanwhile: a level read belongs to another stretch
        elif not data.recorded and coverage.get("recorded_candidates"):
            reason = "no_recorded_charging"  # CJP has no complete record of its own charging for any connection of the period
        elif weekdays is not None and not weekdays:
            reason = "no_days_selected"
        elif coverage["windows"] > 1 and coverage["drives_in_gaps"] == 0:
            reason = "no_drives"  # without a drive that can be applied the demand is unknown, not zero
        result = None
        calculations: list[Any] = []
        if reason is not None:
            result = ReplayResult(
                status="unavailable", reason=reason, windows_total=coverage["windows"], provenance=dict(data.provenance),
                warnings=data.warnings,
            )
        else:
            calculations = self._what_if_calculations(data, coverage, start)
        return {
            "now": now, "period_start": period_start, "period_end": period_end, "data": data, "coverage": coverage,
            "target": target, "target_source": target_source, "sessions": sessions, "result": result,
            "calculations": calculations,
        }

    def _what_if_calculations(self, data: Any, coverage: dict[str, Any], start: dict[str, Any]) -> list[Any]:
        """One replay input per stretch of connections without a gap, each with its own start and end level.

        The level at the start of a stretch is the one read for it, the statistics' one when it begins with an estimated
        connection (a modelled one has none: it only brings the three to a common level), and the end level is the one read at
        its last unplug. Never made up: without a start level the first stretch is refused, the others calibrate."""
        levels = start.get("levels")
        if levels is None:  # (the level at the first plug-in and the one at the last unplug: the first and the last stretch)
            levels = [{} for _ in coverage.get("recorded_segments") or []]
            if levels:
                levels[0] = {**levels[0], "soc": start.get("soc"), "source": start.get("source")}
                levels[-1] = {**levels[-1], "end_soc": start.get("end_soc"), "end_source": start.get("end_source")}

        def number(value: Any) -> float | None:
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and 0.0 <= value <= 100.0 else None

        arrival = float(self.get_min_daily_battery_level())
        calculations = []
        for index, segment in enumerate(coverage.get("recorded_segments") or []):
            windows = tuple(w for w in data.windows if segment["start"] <= w.start and w.end <= segment["end"])
            events = tuple(e for e in data.events if segment["start"] <= e.ts < segment["end"])
            level = levels[index] if index < len(levels) and isinstance(levels[index], dict) else {}
            first = windows[0]
            if first.modelled:
                soc, source = None, "unavailable"
            elif first.derived:
                soc = data.start_levels.get(first.start)
                source = "battery_statistics" if soc is not None else "unavailable"
            else:
                soc = number(level.get("soc"))
                source = str(level.get("source") or "unavailable") if soc is not None else "unavailable"
            last = windows[-1]
            if last.modelled:
                end_soc, end_source = None, "unavailable"
            elif last.derived:  # its unplug time is an estimate: the level at its end is the statistics' one, not one read there
                end_soc = data.end_levels.get(last.start)
                end_source = "battery_statistics" if end_soc is not None else "unavailable"
            else:
                end_soc = number(level.get("end_soc"))
                end_source = str(level.get("end_source") or "unavailable") if end_soc is not None else "unavailable"
            warnings = data.warnings
            if "statistics_hour_mean" in (source, end_source) and "levels_approximate" not in warnings:
                # the mean of the hour of a plug-in or unplug holds the driving next to it: the energy balance rests on it
                warnings = (*warnings, "levels_approximate")
            calculations.append(replace(
                data, windows=windows, events=events, start_soc=soc, end_soc=end_soc, arrival_reserve=arrival, warnings=warnings,
                recorded={w.start: data.recorded[w.start] for w in windows}, may_calibrate=index > 0,
                provenance={**data.provenance, "start_soc": source, "end_soc": end_source},
            ))
        return calculations

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
        # the level change of a month is taken inside each stretch (a gap leaves the level after it unknown)
        groups: dict[tuple[str, int], dict[str, dict[str, float]]] = {}
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
                level = groups.setdefault((key, cjp.segment), {}).setdefault(name, {"in": window.soc_in, "out": window.soc_out, "cost": 0.0})
                level["out"] = window.soc_out  # the first level in the month stays, the last one follows
                level["cost"] += window.cost
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
        # is therefore adjusted the way the difference of the whole period is (stretch by stretch): the change of the battery
        # level in the month is valued at what the one that holds more energy paid per kWh, so the months of a pair add up to
        # its adjusted difference. Without the end level of a stretch nothing is adjusted: no values.
        parts = result.parts or [result]
        battery = float(self.config["ev_car"]["battery_size"])
        for first_name, second_name in (("cjp", "smart"), ("cjp", "asap"), ("smart", "asap")):
            per_percent: list[float | None] = []
            for part in parts:
                first_policy, second_policy = getattr(part, first_name), getattr(part, second_name)
                charged = first_policy.charged_kwh + second_policy.charged_kwh
                ratio = (first_policy.grid_kwh + second_policy.grid_kwh) / charged if charged > 1e-9 else 1.0
                holder = first_policy if first_policy.final_soc >= second_policy.final_soc else second_policy
                paid = holder.cost / holder.grid_kwh if holder.grid_kwh > 1e-9 else part.average_price
                balanced = bool(part.differences and part.differences["cjp_asap"].get("adjusted_cost") is not None)
                per_percent.append(battery / 100.0 * ratio * paid if balanced and paid is not None else None)
            if any(rate is None for rate in per_percent):
                per_percent = [None] * len(per_percent)  # the period has no adjusted difference: nor has a month
            for key, month in months.items():
                total: float | None = 0.0
                for (group_month, segment), levels in groups.items():
                    if group_month != key or total is None:
                        continue
                    rate = per_percent[segment] if segment < len(per_percent) else None
                    if rate is None:
                        total = None
                        continue
                    first_level, second_level = levels[first_name], levels[second_name]
                    total += (
                        first_level["cost"] - second_level["cost"]
                        - ((first_level["out"] - first_level["in"]) - (second_level["out"] - second_level["in"])) * rate
                    )
                month[f"{first_name}_vs_{second_name}_adjusted"] = total
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
        published["segments"] = result.segments
        published["skipped_segments"] = [{"segment": index, "reason": reason} for index, reason in result.skipped_segments]
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
        covered = coverage.get("covered_days")  # (the days the stretches cover: gaps between them are not data)
        days = float(covered) if isinstance(covered, (int, float)) and covered > 0 else (float(last) - float(first)) / 86400.0
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
