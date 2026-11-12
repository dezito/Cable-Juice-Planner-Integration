"""Charging economy per month (engine part): what the charging cost compared with other ways.

A connection window runs from plugging in to unplugging at home. The energy charged in it
(CJP's charging history, solar and home battery valued like in the plan) is compared with
the same kWh charged

- at the window's average price,
- like most smart chargers ("daily charging", alternate_charging_planner): in the cheapest
  hours until the next departure, without moving it to a cheaper night or using solar,
- at the lowest possible price: the cheapest hours of the whole window,

always at the max charging power and with the hourly prices from the price sensor's
long-term statistics. The score is how close the achieved price came to the lowest,
measured from the average: (average - achieved) / (average - lowest), 100 % = the cheapest
hours. (Against daily charging it is often undefined: a window from the evening to the
departure gives daily = lowest.)

Per month also the energy per source (grid, solar, home battery, public) and the price per
km (all charging incl. public divided by the odometer's distance).
"""
from __future__ import annotations

import datetime
import logging
import math
from collections.abc import Iterable
from typing import Any

from ..const import WEEKDAYS
from .analysis.helpers import ANALYSIS_DRIVES
from ..prices.intervals import hourly_price_averages

_LOGGER = logging.getLogger(__name__)

ECONOMY_MONTHS = 13
ECONOMY_DB_VERSION = 2
ECONOMY_PRICE_DAYS = 400  # hourly prices kept for the comparisons: at least this many days (see economy_keep_days)
ECONOMY_WINDOW_SETTLE = 2 * 3600  # s after unplugging before a window is calculated (the last hour is recalculated)


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def economy_keep_days(config) -> int:
    """How many days of hourly prices, sell prices, power statistics and solar sessions the What-if comparisons keep (and read
    back from the recorder): the months the charging history is kept for (the option of the setup, default 12) plus one
    month, at least ECONOMY_PRICE_DAYS. The extra month keeps the whole month of a year ago (the 1st to the end of it)
    in reach while the current month runs. The comparison can only reach as far back as the data is kept."""
    try:
        months = float(config["database"]["charging_history_db_data_to_save"])
    except (KeyError, TypeError, ValueError):
        months = 0.0
    months = months if math.isfinite(months) and months > 0 else 0.0
    return max(ECONOMY_PRICE_DAYS, int(months * 31) + 31)


def _is_measured_price(source: Any) -> bool:
    """Only a price the provider published as real counts: not an estimate from the price
    history ("database"/"offline"), not a forecast and not one without a known source."""
    return str(source or "").endswith(":real")


def _complete_hours(moments: Iterable[datetime.datetime]) -> set[datetime.datetime]:
    """Hours whose prices are all there: one price per equal step from :00 (1 x 60, 2 x 30, 4 x 15 minutes).

    The step of a day is the shortest spacing of that day's prices (60 minutes when it has no two prices
    in the same hour), so an hour with only its first quarter on a quarter-hour day is incomplete. An
    interval never stretches over a missing price: a missing quarter is a missing hour.
    """
    ordered = sorted(set(moments))
    steps: dict[datetime.date, int] = {}
    for earlier, later in zip(ordered, ordered[1:]):
        gap = int((later - earlier).total_seconds())
        if 0 < gap <= 3600:
            steps[earlier.date()] = min(steps.get(earlier.date(), 3600), gap)
    offsets: dict[datetime.datetime, list[int]] = {}
    for moment in ordered:
        hour = moment.replace(minute=0, second=0, microsecond=0)
        offsets.setdefault(hour, []).append(int((moment - hour).total_seconds()))
    complete = set()
    for hour, found in offsets.items():
        step = steps.get(hour.date(), 3600)
        if 3600 % step == 0 and found == list(range(0, 3600, step)):
            complete.add(hour)
    return complete


def fill_cheapest(prices: dict[float, float], start: float, end: float, kwh: float, kw: float) -> float | None:
    """Cost of kwh charged at kw in the cheapest hours of [start, end) (hour start -> price).

    More energy than fits is priced at the average of the hours. None without prices.
    """
    slots = []
    hour = start - start % 3600
    while hour < end:
        overlap = (min(hour + 3600, end) - max(hour, start)) / 3600
        if overlap > 0 and hour in prices:
            slots.append((prices[hour], kw * overlap))
        hour += 3600
    if not slots:
        return None
    cost, left = 0.0, kwh
    for price, capacity in sorted(slots):
        used = min(capacity, left)
        cost += used * price
        left -= used
        if left <= 1e-9:
            break
    if left > 1e-9:
        cost += left * sum(p for p, _ in slots) / len(slots)
    return cost


def average_price(prices: dict[float, float], start: float, end: float) -> float | None:
    hour = start - start % 3600
    values = []
    while hour < end:
        if hour in prices:
            values.append(prices[hour])
        hour += 3600
    return sum(values) / len(values) if values else None


class Economy:
    """Connection windows (database economy) and the monthly economy sensor."""

    def economy_keep_days(self) -> int:
        return economy_keep_days(self.config)

    def drive_history_limit(self) -> int:
        """How many drives are kept: two a day for the days the What-if data is kept, never fewer than the analysis uses."""
        return max(ANALYSIS_DRIVES, 2 * self.economy_keep_days())

    def load_economy_db(self, database) -> None:
        self.economy_db = database if isinstance(database, dict) and isinstance(database.get("windows"), list) else {"windows": []}
        if self.economy_db.get("version") != ECONOMY_DB_VERSION:
            # version 2: daily charging no longer counts hours without a price as free: calculate the windows again
            for window in self.economy_db["windows"]:
                for key in ("kwh", "achieved", "average", "daily", "lowest"):
                    window.pop(key, None)
            self.economy_db["version"] = ECONOMY_DB_VERSION
        # Sessions that started before this did not record their solar energy as energy (see
        # engine/energy/solar.py calc_local_energy_kwh), so the What-if solar comparison does not use them
        self.economy_db.setdefault("solar_energy_since", self._economy_ts(self.now()))
        if not isinstance(self.economy_db.get("derived_windows"), dict):
            self.economy_db.pop("derived_windows", None)

    # connection windows ------------------------------------------------ #

    def economy_plugged_in(self) -> None:
        """charger_status_changed: plugged in at home."""
        windows = self.economy_db["windows"]
        if windows and windows[-1].get("end") is None:
            return
        windows.append({"start": self._economy_ts(self.now()), "end": None})
        self.mark_dirty("economy")

    def economy_unplugged(self) -> None:
        windows = self.economy_db["windows"]
        if windows and windows[-1].get("end") is None:
            windows[-1]["end"] = self._economy_ts(self.now())
            self.mark_dirty("economy")

    def _economy_ts(self, moment: datetime.datetime) -> float:
        return moment.replace(tzinfo=self._snap.tz).timestamp() if moment.tzinfo is None else moment.timestamp()

    def _economy_local(self, ts: float) -> datetime.datetime:
        return datetime.datetime.fromtimestamp(ts, self._snap.tz).replace(tzinfo=None)

    def _next_departure(self, start: float, end: float) -> float:
        """The first workday departure (work plan) after start, else the end of the window."""
        if not self.workplan_charging_enabled():
            return end
        moment = self._economy_local(start)
        for day in range(8):
            date = (moment + datetime.timedelta(days=day)).date()
            day_text = WEEKDAYS[date.weekday()]
            departure_time = self.setting(f"workday_departure_{day_text}")
            if self.switch_state(f"workday_{day_text}") != "on" or not isinstance(departure_time, datetime.time):
                continue
            departure = self._economy_ts(datetime.datetime.combine(date, departure_time))
            if departure > start:
                return min(departure, end)
        return end

    def economy_mark_backfilled(self) -> None:
        """The connection windows of the recorder's history were read once."""
        self.economy_db["backfilled"] = True
        self.mark_dirty("economy")

    def economy_store_prices(self) -> None:
        """hour_end: keep the plan's grid prices of the hours that have passed (incl. fees, the
        prices the achieved cost was charged at), so the comparison does not depend on the
        recorder's history of the price sensor."""
        stored = self.economy_db.setdefault("prices", {})
        now = self.now()
        grid_prices = self.get_grid_prices() or {}
        # (read after the prices: fetching them may have replaced the sources)
        sources = getattr(self, "grid_price_cache", {}).get("sources") or {}
        # only measured prices: an hour filled from the price history ("database"/"offline") or a
        # forecast is an estimate and must never be kept as if it were the price paid
        real = {
            moment: float(price) for moment, price in grid_prices.items()
            if isinstance(moment, datetime.datetime) and _finite(price) and _is_measured_price(sources.get(moment))
        }
        # an hour is the duration-weighted average of its intervals (quarter-hour prices are
        # not "the last quarter"). Only an hour with every price published and already started:
        # a published price is final, and this runs at minute 59, so the hour that is ending counts
        complete = _complete_hours(real)
        copies = self.economy_db.setdefault("prices_dst_copy", {})
        for hour, price in hourly_price_averages(real, full_hours_only=True).items():
            if hour in complete and hour <= now:
                stored[self._economy_ts(hour)] = round(price, 4)
                # the autumn clock change repeats a local hour, but the price vector has one price for
                # it. The second real hour is kept apart as a copy (only the What-if replay reads it, and
                # flags it): it must never replace a real price of the monthly economy comparison
                repeated = hour.replace(tzinfo=self._snap.tz, fold=1).timestamp()
                if repeated != self._economy_ts(hour):
                    copies[repeated] = round(price, 4)
        # hours stored before the copy existed (the autumn change inside the kept 400 days)
        for ts in [ts for ts in stored if (ts + 3600.0) not in stored and (ts + 3600.0) not in copies]:
            first = datetime.datetime.fromtimestamp(ts, self._snap.tz)
            if first.fold == 0 and first.replace(fold=1).utcoffset() != first.utcoffset():
                copies[ts + 3600.0] = stored[ts]
        self.economy_db.setdefault("prices_averaged_since", self._economy_ts(now))
        oldest = self._economy_ts(now) - self.economy_keep_days() * 86400
        for ts in [ts for ts in stored if ts < oldest]:
            del stored[ts]
        for ts in [ts for ts in copies if ts < oldest]:
            del copies[ts]
        self.mark_dirty("economy")
        try:
            # the What-if solar comparison must never stop the storing of the buy prices above
            self._economy_store_sell_prices(now)
            self.update_solar_ledger()
            self.publish_what_if_solar()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("The What-if solar data could not be stored: %s %s", err, type(err))

    def _economy_store_sell_prices(self, now: datetime.datetime) -> None:
        """The raw hourly sell prices (and the fixed production price in effect) of the hours that have passed.

        Kept for the What-if solar comparison, which needs the value of the solar energy at the time. Only
        measured prices of complete hours, like the buy prices. The raw price is stored: sell_value() is
        applied once, when the comparison is calculated, so a later change of the production price can not
        be applied to an earlier hour.
        """
        if not self.is_solar_configured():
            return
        try:
            production = float(self.config["solar"]["production_price"])
        except (KeyError, TypeError, ValueError):
            return
        if not _finite(production):
            return
        sell_prices = self.get_sell_prices() or {}
        sources = getattr(self, "sell_price_cache", {}).get("sources") or {}
        real = {
            moment: float(price) for moment, price in sell_prices.items()
            if isinstance(moment, datetime.datetime) and _finite(price) and _is_measured_price(sources.get(moment))
        }
        complete = _complete_hours(real)
        stored = self.economy_db.setdefault("sell_prices", {})
        fixed = self.economy_db.setdefault("sell_production_price", {})
        for hour, price in hourly_price_averages(real, full_hours_only=True).items():
            if hour in complete and hour <= now:
                ts = self._economy_ts(hour)
                if ts not in stored:
                    # an hour is stored once, with the production price in effect then: a later change of the
                    # production price (or a refreshed price vector that still holds this hour) never rewrites it
                    stored[ts] = round(price, 4)
                    fixed[ts] = production
        self.economy_db.setdefault("sell_prices_since", self._economy_ts(now))
        oldest = self._economy_ts(now) - self.economy_keep_days() * 86400
        for table in (stored, fixed):
            for ts in [ts for ts in table if ts < oldest]:
                del table[ts]

    # calculation ------------------------------------------------------- #

    def economy_update(self, data: dict) -> None:
        """data from the coordinator: hourly prices {ts: currency/kWh}, odometer km per month {"YYYY-MM": km}
        and (the first time) connection windows from the recorder [(start, end)]."""
        windows = self.economy_db["windows"]
        for start, end in data.get("windows") or []:
            if not any(w["start"] < end and start < (w["end"] or start + 1) for w in windows):
                windows.append({"start": start, "end": end})
        windows.sort(key=lambda w: w["start"])
        # the price sensor's history (statistics or raw), the plan's own prices win
        prices = {float(ts): float(price) for ts, price in (data.get("prices") or {}).items()}
        # keep the statistics prices CJP did not store itself: the What-if replays use them for hours without a stored price
        history = self.economy_db.setdefault("prices_history", {})
        stored_prices = self.economy_db.get("prices", {})
        for ts, price in prices.items():
            if ts not in stored_prices and _finite(price):
                history[ts] = round(price, 4)
        prices.update({float(ts): float(price) for ts, price in stored_prices.items()})
        oldest_price = self._economy_ts(self.now()) - self.economy_keep_days() * 86400
        for ts in [ts for ts in history if float(ts) < oldest_price or ts in stored_prices]:
            del history[ts]
        kw = int(self.config["charger"]["charging_phases"]) * float(self.config["charger"]["charging_max_amp"]) * float(self.config["charger"]["power_voltage"]) / 1000
        now = self._economy_ts(self.now())
        sessions = self._economy_sessions()

        for window in windows:
            if "achieved" in window or window["end"] is None or now - window["end"] < ECONOMY_WINDOW_SETTLE:
                continue
            inside = [s for s in sessions if window["start"] - 600 <= s["ts"] <= window["end"] and not s["public"]]
            if any(not session["cost_valid"] for session in inside):
                continue  # incomplete history must never become a favourable zero-cost comparison
            kwh = sum(s["kwh"] for s in inside)
            if kwh <= 0.0:
                window["achieved"] = 0.0
                window["kwh"] = 0.0
                continue
            lowest = fill_cheapest(prices, window["start"], window["end"], kwh, kw)
            average = average_price(prices, window["start"], window["end"])
            if lowest is None or average is None:
                continue  # prices not (yet) in the statistics
            departure = self._next_departure(window["start"], window["end"])
            daily_first = min(kwh, kw * max(departure - window["start"], 0) / 3600)
            daily_rest = kwh - daily_first

            def part(start, end, part_kwh):
                # a part without hourly prices at the window's average (never for free)
                if part_kwh <= 1e-9:
                    return 0.0
                cost = fill_cheapest(prices, start, end, part_kwh, kw)
                return cost if cost is not None else part_kwh * average

            # the lowest price is a subset of the same hours: daily can never be cheaper
            daily = max(part(window["start"], departure, daily_first) + part(departure, window["end"], daily_rest), lowest)
            window.update(
                kwh=round(kwh, 3), achieved=round(sum(s["cost"] for s in inside), 3),
                average=round(average * kwh, 3), daily=round(daily, 3), lowest=round(lowest, 3),
            )
        # windows older than the charging history kept are of no use
        oldest = min((s["ts"] for s in sessions), default=now) - 86400
        self.economy_db["windows"] = [w for w in windows if (w["end"] or now) >= oldest]
        self.mark_dirty("economy")
        self.publish_economy(data.get("odometer_months") or {})
        try:
            self.store_derived_windows(data)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("The What-if older connections could not be estimated: %s %s", err, type(err))
        try:
            self.store_solar_statistics(data)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("The What-if solar statistics could not be stored: %s %s", err, type(err))
        self.publish_what_if_solar()

    def _economy_sessions(self) -> list[dict]:
        sessions = []
        for key, value in self.charging_history_db.items():
            if not isinstance(key, datetime.datetime) or not isinstance(value, dict):
                continue
            public = "public_charging" in str(value.get("charging_type", "")) or bool(value.get("kWh_from_public"))
            kwh = float(value.get("kWh") or 0.0)
            local = float(value.get("kWh_from_local_energy") or 0.0)
            cost_valid = _finite(value.get("cost"))
            sessions.append({
                "ts": self._economy_ts(key), "month": key.strftime("%Y-%m"), "public": public,
                "kwh": kwh, "cost": float(value["cost"]) if cost_valid else 0.0, "cost_valid": cost_valid,
                "grid": 0.0 if public else max(float(value.get("kWh_from_grid", kwh - local) or 0.0), 0.0),
                "solar": float(value.get("solar_kwh_of_local_energy") or 0.0),
                "powerwall": float(value.get("powerwall_kwh_of_local_energy") or 0.0),
                "public_kwh": float(value.get("kWh_from_public") or (kwh if public else 0.0)),
            })
        return sessions

    def publish_economy(self, odometer_months: dict[str, float] | None = None) -> None:
        if odometer_months is not None:
            self.economy_db["odometer_months"] = {**self.economy_db.get("odometer_months", {}), **odometer_months}
        odometer_months = self.economy_db.get("odometer_months", {})
        months: dict[str, dict[str, Any]] = {}

        def month(key: str) -> dict:
            return months.setdefault(key, {
                "month": key, "kwh": 0.0, "cost": 0.0, "grid": 0.0, "solar": 0.0, "powerwall": 0.0, "public": 0.0, "public_cost": 0.0,
                "compared_kwh": 0.0, "achieved": 0.0, "average": 0.0, "daily": 0.0, "lowest": 0.0, "windows": 0,
                "charging_sessions": 0, "cost_valid": True,
            })

        for s in self._economy_sessions():
            m = month(s["month"])
            m["kwh"] += s["kwh"]
            m["cost"] += s["cost"]
            m["charging_sessions"] += 1
            m["cost_valid"] = m["cost_valid"] and s["cost_valid"]
            m["grid"] += s["grid"]
            m["solar"] += s["solar"]
            m["powerwall"] += s["powerwall"]
            m["public"] += s["public_kwh"]
            if s["public"]:
                m["public_cost"] += s["cost"]
        for window in self.economy_db["windows"]:
            if not window.get("kwh") or "lowest" not in window:
                continue
            m = month(self._economy_local(window["start"]).strftime("%Y-%m"))
            m["compared_kwh"] += window["kwh"]
            m["windows"] += 1
            for key in ("achieved", "average", "daily", "lowest"):
                m[key] += window[key]

        result = []
        subscription_price = self.setting("subscription_monthly_price", 0.0)
        subscription_price = float(subscription_price) if _finite(subscription_price) and subscription_price > 0.0 else 0.0
        current_month = self.now().strftime("%Y-%m")
        for key in sorted(months)[-ECONOMY_MONTHS:]:
            m = months[key]
            km = odometer_months.get(key)
            compared = m["compared_kwh"]
            row = {k: round(v, 3) if isinstance(v, float) else v for k, v in m.items()}
            row["cost"] = round(m["cost"], 3) if m["cost_valid"] else None
            row["km"] = round(km, 1) if km else None
            row["price_per_km"] = round(m["cost"] / km, 3) if m["cost_valid"] and km else None
            row["unit_price"] = round(m["cost"] / m["kwh"], 3) if m["cost_valid"] and m["kwh"] else None
            for ref in ("achieved", "average", "daily", "lowest"):
                row[f"{ref}_unit"] = round(m[ref] / compared, 3) if compared else None
            row["saving_daily"] = round(m["daily"] - m["achieved"], 2) if compared else None
            possible = m["average"] - m["lowest"]
            row["score"] = round(min(max((m["average"] - m["achieved"]) / possible, -1.0), 1.0), 3) if compared and possible > 0.01 else None
            row["subscription_recorded_cost"] = round(m["cost"], 2) if m["cost_valid"] and m["charging_sessions"] else None
            row["subscription_price"] = round(subscription_price, 2) if subscription_price else None
            row["subscription_saving"] = None
            if not subscription_price:
                row["subscription_status"] = "disabled"
            elif key >= current_month:
                row["subscription_status"] = "current"
            elif not m["cost_valid"] or not m["charging_sessions"]:
                row["subscription_status"] = "incomplete"
            else:
                row["subscription_status"] = "recorded"
                row["subscription_saving"] = round(m["cost"] - subscription_price, 2)
            result.append(row)

        current = result[-1] if result else {}
        self.set_state("economy", current.get("saving_daily"))
        self.set_attr("economy", "months", result)
        totals = {key: round(sum(r[key] or 0.0 for r in result), 2) for key in ("kwh", "achieved", "daily", "lowest", "average", "compared_kwh")}
        totals["cost"] = None if any(row["cost"] is None for row in result) else round(
            sum(row["cost"] for row in result), 2)
        totals["saving_daily"] = round(totals["daily"] - totals["achieved"], 2)
        possible = totals["average"] - totals["lowest"]
        totals["score"] = round(min(max((totals["average"] - totals["achieved"]) / possible, -1.0), 1.0), 3) if possible > 0.01 else None
        comparable = [row for row in result if row["subscription_status"] == "recorded"]
        subscription_total = {
            "enabled": bool(subscription_price),
            "comparable_months": len(comparable),
            "recorded_cost": round(sum(row["subscription_recorded_cost"] for row in comparable), 2),
            "price": round(subscription_price * len(comparable), 2),
            "saving": round(sum(row["subscription_saving"] for row in comparable), 2),
        }
        self.set_attr("economy", "total", totals)
        self.set_attr("economy", "subscription_total", subscription_total)
        self.set_attr("economy", "open_window", bool(self.economy_db["windows"] and self.economy_db["windows"][-1].get("end") is None))
