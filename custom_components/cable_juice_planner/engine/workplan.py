"""Learned departure and homecoming per weekday (engine part).

The connection windows of the charger (database economy: plugged in at home -> unplugged) give, per workday, when the car
really left and came back:

- Departure: the first unplug of a day between 04:00 and 12:00 (local time). The 25th percentile of that weekday over
  the last 8 weeks, rounded down to 15 minutes.
- Homecoming: the plug-in that follows the same day. The 75th percentile, rounded up to 15 minutes.

Only measured windows count (never the modelled ones of the What-if, which are built from this very work plan). A day
counts only when the weekday is a workday of the work plan, the car came back the same local day after at least
LEARN_MIN_AWAY_MINUTES (a short unplug and plug-in is a re-plug, not a trip), and no trip of the trip settings covers it.
At least 3 days of the weekday are needed.

The suggestions are always shown as the ``learned_time`` attribute of the departure and homecoming time entities. With
switch.<prefix>_learn_departure_time on, the plan uses the earlier of the entered and the learned departure and the later
of the entered and the learned homecoming (:meth:`effective_workday_times`): a learned time only moves the departure
earlier or the homecoming later than entered. The entered times are never changed. They are used instead on a day with a
trip, and the entered departure is used today while the car is still plugged in after the learned departure (an open
connection window: positive proof that the car is here, a status that is missing proves nothing).

The trip settings are reset two hours after a trip starts, so the dates of every trip that has started are kept
(``trip_days``, in the runtime database) for the days the learning looks back.
"""
from __future__ import annotations

import datetime
import logging
import math
from typing import Any

from ..const import WEEKDAYS
from ..utils.helpers import reset_datetime

_LOGGER = logging.getLogger(__name__)

LEARN_WEEKS = 8
LEARN_MIN_WEEKDAY_SAMPLES = 3
LEARN_DEPARTURE_FROM = 4 * 60  # minutes of the day: an unplug from here ...
LEARN_DEPARTURE_TO = 12 * 60  # ... until here is the departure of the day
LEARN_MIN_AWAY_MINUTES = 30  # a shorter unplug and plug-in is a re-plug of the cable, not a departure
LEARN_DEPARTURE_FRACTION = 0.25
LEARN_HOMECOMING_FRACTION = 0.75
LEARN_STEP_MINUTES = 15
LAST_MINUTE_OF_DAY = 23 * 60 + 59


def _percentile(values: list[float], fraction: float) -> float:
    """Linear interpolation between the closest ranks."""
    values = sorted(values)
    position = (len(values) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


def _number(value: Any) -> bool:
    """A real, finite number (not a bool, not NaN or infinity)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _time(minutes: int) -> datetime.time:
    return datetime.time(minutes // 60, minutes % 60)


def window_samples(
    windows: list[dict[str, Any]],
    tz: datetime.tzinfo,
    today: datetime.date,
    workdays: set[int],
    excluded: set[datetime.date],
) -> dict[int, list[tuple[int, int]]]:
    """Per weekday (0 = Monday) the (departure, homecoming) of every counted day, in minutes of the local day.

    windows: the measured connection windows {"start": plug-in timestamp, "end": unplug timestamp or None}."""
    rows = sorted(
        (float(w["start"]), float(w["end"]) if w.get("end") is not None else None)
        for w in windows
        if isinstance(w, dict) and _number(w.get("start")) and (w.get("end") is None or _number(w.get("end")))
    )
    oldest = today - datetime.timedelta(weeks=LEARN_WEEKS)
    samples: dict[int, list[tuple[int, int]]] = {}
    seen: set[datetime.date] = set()
    for index, (_, end) in enumerate(rows):
        if end is None:
            continue
        left = datetime.datetime.fromtimestamp(end, tz)
        day = left.date()
        minute = left.hour * 60 + left.minute
        if day in seen or not oldest <= day < today or not LEARN_DEPARTURE_FROM <= minute < LEARN_DEPARTURE_TO:
            continue
        seen.add(day)  # the first unplug of the day in the range is the day's departure, whatever follows
        if day.weekday() not in workdays or day in excluded or index + 1 >= len(rows):
            continue
        back = rows[index + 1][0]
        returned = datetime.datetime.fromtimestamp(back, tz)
        if returned.date() != day or back - end < LEARN_MIN_AWAY_MINUTES * 60:
            continue  # not back the same day (a trip, a night away) or only a re-plug
        samples.setdefault(day.weekday(), []).append((minute, returned.hour * 60 + returned.minute))
    return samples


def learned_times(samples: dict[int, list[tuple[int, int]]]) -> dict[str, dict[str, Any]]:
    """Per weekday name the learned departure and homecoming (datetime.time) and the number of days they come from."""
    result = {}
    for weekday, rows in samples.items():
        departures = [departure for departure, _ in rows]
        homecomings = [homecoming for _, homecoming in rows]
        departure = math.floor(_percentile(departures, LEARN_DEPARTURE_FRACTION) / LEARN_STEP_MINUTES) * LEARN_STEP_MINUTES
        homecoming = math.ceil(_percentile(homecomings, LEARN_HOMECOMING_FRACTION) / LEARN_STEP_MINUTES) * LEARN_STEP_MINUTES
        result[WEEKDAYS[weekday]] = {
            "departure": _time(departure),
            "homecoming": _time(min(homecoming, LAST_MINUTE_OF_DAY)),
            "samples": len(rows),
        }
    return result


class Workplan:
    """Learned work plan times and the times the plan uses."""

    def learn_departure_time_enabled(self) -> bool:
        return self.switch_state("learn_departure_time") == "on"

    def _current_trip_dates(self) -> set[datetime.date]:
        """The dates the trip settings cover now."""
        try:
            start, back = self.get_trip_date_time(), self.get_trip_homecoming_date_time()
            if not isinstance(start, datetime.datetime) or start == reset_datetime():
                return set()
            end = back if isinstance(back, datetime.datetime) and back != reset_datetime() and back >= start else start
            return {start.date() + datetime.timedelta(days=offset) for offset in range((end.date() - start.date()).days + 1)}
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Learned work plan: the trip dates could not be read: %s", err)
            return set()

    def _started_trip_dates(self) -> set[datetime.date]:
        """The dates of the trip now set that have begun (up to today): a trip that is only planned, moved or cancelled before it
        starts leaves nothing behind, and never more than the days that have gone."""
        try:
            start = self.get_trip_date_time()
            if not isinstance(start, datetime.datetime) or start == reset_datetime() or self.now() < start:
                return set()
        except Exception:  # noqa: BLE001
            return set()
        today = self.now().date()
        return {date for date in self._current_trip_dates() if date <= today}

    def _plugged_in_at_home(self) -> bool:
        """The charger has seen the car plugged in at home and no unplug since (the newest connection window is open). Only
        a positive sign: a status or port entity that is unavailable is not an unplug, so it never counts as the car having left."""
        windows = self.economy_db.get("windows") or []
        return bool(windows) and isinstance(windows[-1], dict) and windows[-1].get("end") is None

    def record_trip_days(self) -> None:
        """Keep the dates of every trip that has started (the settings are reset two hours after it starts), for the days the
        learning looks back and a little more."""
        dates = {date.isoformat() for date in self._started_trip_dates()}
        oldest = (self.now().date() - datetime.timedelta(weeks=LEARN_WEEKS + 1)).isoformat()
        kept = {date for date in {*self.trip_days, *dates} if date >= oldest}
        if kept != self.trip_days:
            self.trip_days = kept
            self.mark_dirty("runtime")

    def trip_dates(self) -> set[datetime.date]:
        """The dates of the trips now set and of those seen before."""
        dates = set(self._current_trip_dates())
        for text in self.trip_days:
            try:
                dates.add(datetime.date.fromisoformat(str(text)))
            except ValueError:
                continue
        return dates

    def learned_workplan(self) -> dict[str, dict[str, Any]]:
        """Per weekday name {"departure", "homecoming", "samples"} from the measured connection windows, calculated once per
        job (every reader of a job sees the same answer)."""
        cached = self._job_cache.get("learned_workplan")
        if cached is None:
            workdays = {index for index, day in enumerate(WEEKDAYS) if self.switch_state(f"workday_{day}") == "on"}
            windows = self.economy_db.get("windows") or []
            cached = learned_times(window_samples(windows, self._snap.tz, self.now().date(), workdays, self.trip_dates()))
            self._job_cache["learned_workplan"] = cached
        return cached

    def effective_workday_times(self, day_text: str, date: datetime.date | None = None) -> tuple[datetime.time, datetime.time]:
        """(departure, homecoming) of a weekday as the plan uses them: the entered times, and with learning on and at least
        LEARN_MIN_WEEKDAY_SAMPLES days the earlier departure and the later homecoming of the entered and the learned.

        date: the day the times are for. On a day with a trip the entered times are used (the order of the trip and the work
        plan depends on them). Today, while the car is still plugged in after the learned departure, the departure is the entered one:
        it did not leave then, so today's work plan is still ahead. Anything that goes wrong gives the entered times."""
        departure = self.setting(f"workday_departure_{day_text}")
        homecoming = self.setting(f"workday_homecoming_{day_text}")
        try:
            if not self.learn_departure_time_enabled() or not isinstance(departure, datetime.time) or not isinstance(homecoming, datetime.time):
                return departure, homecoming
            if departure >= homecoming:
                return departure, homecoming  # a night shift: the day window the learning looks at does not describe it
            if date is not None and date in self.trip_dates():
                return departure, homecoming
            learned = self.learned_workplan().get(day_text)
            if learned is None or learned["samples"] < LEARN_MIN_WEEKDAY_SAMPLES:
                return departure, homecoming
            used_departure = min(departure, learned["departure"])
            if date is not None and date == self.now().date() and used_departure < departure and self.now().time() >= used_departure and self._plugged_in_at_home():
                used_departure = departure
            return used_departure, max(homecoming, learned["homecoming"])
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("The learned work plan times could not be used, the entered ones are: %s", err)
            return departure, homecoming

    def update_learned_workplan(self) -> None:
        """Keep the trip dates and publish the suggestions as the attributes learned_time and learned_days of the time entities."""
        self.record_trip_days()
        try:
            learned = self.learned_workplan()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("The learned work plan times could not be calculated: %s", err)
            return
        for day in WEEKDAYS:
            row = learned.get(day)
            for key, field in ((f"workday_departure_{day}", "departure"), (f"workday_homecoming_{day}", "homecoming")):
                self.set_attr(key, "learned_time", row[field].strftime("%H:%M") if row else None)
                self.set_attr(key, "learned_days", row["samples"] if row else 0)
