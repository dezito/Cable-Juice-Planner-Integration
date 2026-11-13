"""What-if Scenario D in the engine: the EV's solar energy replaced by grid energy (see solar.py).

A continuous calculation like the monthly economy comparison (no button, no start level), shown on
sensor.<prefix>_what_if_solar. It only reads CJP's databases and changes nothing live.

Each finished session is priced once, in the hour after it ended, and kept in economy_db["solar_ledger"]:
CJP merges its charging history per day once it is a week old (a session would become a day-long span
with pauses) and a price or the refund can change later. The months are summed from the ledger.
"""
from __future__ import annotations

import datetime
import logging
import math
from typing import Any

from .solar import evaluate_session, solar_from_power_hours, summarize

_LOGGER = logging.getLogger(__name__)

LEDGER_WINDOW = 5 * 86400.0  # s : a session is priced while it ended less than this ago (before CJP merges the history)
PENDING_GIVE_UP = 48 * 3600.0  # s : a session whose stored prices are still missing this long after it ended is left out
# the days kept follow the months of history of the setup: see economy_keep_days (engine/economy.py)


def _float_keys(table: Any) -> dict[float, float]:
    """A stored {timestamp: value} table (JSON keys may be text); entries that are not numbers are dropped."""
    result: dict[float, float] = {}
    if not isinstance(table, dict):
        return result
    for key, value in table.items():
        try:
            result[float(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return result


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _overlaps(spans: list[tuple[float, float]], start: float, end: float) -> bool:
    return any(s < max(end, start + 1.0) and start < max(e, s + 1.0) for s, e in spans)


class WhatIfSolar:
    """Scenario D: the solar advantage of the EV's charging, per month."""

    def _solar_sessions(self) -> list[dict[str, Any]]:
        """CJP's charging sessions as the comparison needs them (UTC seconds)."""
        sessions = []
        for key, value in self.charging_history_db.items():
            if not isinstance(key, datetime.datetime) or not isinstance(value, dict):
                continue
            local = key if key.tzinfo is None else key.astimezone(self._snap.tz).replace(tzinfo=None)
            ended = value.get("ended")
            end: float | None
            if ended == ">":
                end = None  # still charging
            else:
                try:
                    hours, minutes = str(ended).split(":")
                    finish = datetime.datetime.combine(local.date(), datetime.time(int(hours), int(minutes)))
                    if finish < local.replace(second=0, microsecond=0):  # "ended" has no seconds
                        finish += datetime.timedelta(days=1)
                    end = self._economy_ts(finish)
                except (TypeError, ValueError):
                    end = math.nan  # an unreadable end time: the session is left out as invalid
            public = "public_charging" in str(value.get("charging_type", "")) or bool(value.get("kWh_from_public"))
            sessions.append({
                "start": self._economy_ts(local), "end": end, "public": public,
                "kwh": value.get("kWh") or 0.0, "local": value.get("kWh_from_local_energy") or 0.0,
                "solar": value.get("solar_kwh_of_local_energy") or 0.0, "powerwall": value.get("powerwall_kwh_of_local_energy") or 0.0,
            })
        return sessions

    def _solar_refund(self) -> float | None:
        provider = getattr(self, "price_provider", None)
        refund = getattr(provider, "refund", None)
        if provider is None or isinstance(refund, bool) or not isinstance(refund, (int, float)) or not math.isfinite(refund):
            return None
        return float(refund)

    def _solar_month(self, ts: float) -> str:
        return self._economy_local(ts).strftime("%Y-%m")

    def store_solar_statistics(self, data: dict[str, Any]) -> None:
        """economy_update: keep the hourly solar production, house consumption and charger power (W) and the sell price
        history that the coordinator read from the recorder's long-term statistics, 400 days of it."""
        oldest = self._economy_ts(self.now()) - self.economy_keep_days() * 86400.0
        hours = data.get("power_hours")
        if isinstance(hours, dict) and hours:
            stored = self.economy_db.setdefault("power_hours", {})
            for ts, values in hours.items():
                if ts >= oldest and all(_finite(values.get(key)) for key in ("solar", "house", "ev")):
                    stored[float(ts)] = [round(values["solar"], 1), round(values["house"], 1), round(values["ev"], 1)]
            for ts in [ts for ts in stored if float(ts) < oldest]:
                del stored[ts]
        sell = data.get("sell_history")
        if isinstance(sell, dict) and sell:
            self._store_sell_history(sell, oldest, data.get("sell_history_entity_id"))
        self.mark_dirty("economy")

    def _store_sell_history(self, raw: dict[float, float], oldest: float, entity_id: str | None = None) -> None:
        """The recorded sell sensor (entity_id) turned into CJP's sell price, once per hour, with the tariffs of the
        hour's own day.

        Only hours CJP has not stored itself. The tariffs are the ones CJP kept for that local day
        (economy_db["sell_tariff_days"], see economy_store_sell_tariffs): Energi Data Service does not record its tariffs
        attribute, so there is no other source. An hour of a day without them is never converted with another day's
        tariffs or with none: it is left out (the comparison counts it as a missing sell price), and the hours left out
        are counted in economy_db["sell_tariff_unknown_hours"] (counted again on every run). The provider's notes of the
        live sell prices are put back, so this never changes what the plan sees."""
        provider = self.price_provider
        convert = getattr(provider, "convert_recorded_sell_price", None)
        if convert is None:
            return
        if not entity_id:
            entity_id = next(iter(provider.sell_price_entity_ids()), "")
        days = (self.economy_db.get("sell_tariff_days") or {}).get(entity_id)
        days = days if isinstance(days, dict) else {}
        stored = self.economy_db.get("sell_prices", {})
        history = self.economy_db.setdefault("sell_prices_history", {})
        notes = [
            (holder, dict(holder.sell_price_details)) for holder in (provider, *getattr(provider, "providers", []))
            if isinstance(getattr(holder, "sell_price_details", None), dict)
        ]
        unknown = 0
        try:
            for ts, price in raw.items():
                if ts < oldest or ts in history or ts in stored or not _finite(price):
                    continue
                moment = self._economy_local(float(ts))
                converted = convert(entity_id, moment, float(price), days.get(moment.date().isoformat()))
                if _finite(converted):
                    history[float(ts)] = round(float(converted), 4)
                else:
                    unknown += 1
        finally:
            for holder, saved in notes:
                holder.sell_price_details.clear()
                holder.sell_price_details.update(saved)
        self.economy_db["sell_tariff_unknown_hours"] = unknown
        for ts in [ts for ts in history if float(ts) < oldest or ts in stored]:
            del history[ts]

    def update_solar_ledger(self) -> None:
        """Price the sessions that ended recently and are not in the ledger yet (hourly, after the prices were stored)."""
        if not self.is_solar_configured():
            return
        refund = self._solar_refund()
        if refund is None:
            return
        now = self._economy_ts(self.now())
        ledger = self.economy_db.setdefault("solar_ledger", {})
        # the buy price CJP stored itself, else the price sensor's recorded statistics (never over a stored price)
        buy = {**_float_keys(self.economy_db.get("prices_history")), **_float_keys(self.economy_db.get("prices"))}
        sell = _float_keys(self.economy_db.get("sell_prices"))
        production = _float_keys(self.economy_db.get("sell_production_price"))
        since = self.economy_db.get("solar_energy_since")
        spans = [(float(e["start"]), float(e["end"])) for e in ledger.values() if isinstance(e, dict) and _finite(e.get("start")) and _finite(e.get("end"))]
        for session in sorted(self._solar_sessions(), key=lambda s: s["start"]):
            start, end = session["start"], session["end"]
            if end is not None and _finite(end) and now - end > LEDGER_WINDOW:
                continue  # too old: it may already have been merged into a day (see summary: not captured)
            key = str(int(start))
            if key in ledger:
                continue
            if end is not None and _finite(end) and _overlaps(spans, start, end):
                continue  # priced already, as a part of a session that was merged since
            result = evaluate_session(session, buy, sell, production, refund, since)
            if result["outcome"] == "live":
                continue
            if result["outcome"] == "pending":
                if end is None or now - end < PENDING_GIVE_UP:
                    continue  # the hour's price may still be stored
                result = {"outcome": "left_out", "reason": result["reason"]}
            entry_end = float(end) if end is not None and _finite(end) else start
            ledger[key] = {
                "start": start, "end": entry_end, "month": self._solar_month(start),
                **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in result.items()},
            }
            spans.append((start, entry_end))
        oldest = now - self.economy_keep_days() * 86400.0
        for key in [key for key, entry in ledger.items() if not isinstance(entry, dict) or float(entry.get("start", 0.0)) < oldest]:
            del ledger[key]
        self.mark_dirty("economy")

    def what_if_solar_result(self) -> dict[str, Any]:
        now = self._economy_ts(self.now())
        base: dict[str, Any] = {
            "computed_at": now, "sell_prices_since": self.economy_db.get("sell_prices_since"),
            "solar_energy_since": self.economy_db.get("solar_energy_since"),
        }
        if not self.is_solar_configured():
            return {**base, "status": "unavailable", "reason": "no_solar_configured"}
        if self._solar_refund() is None:
            return {**base, "status": "unavailable", "reason": "no_price_provider"}
        ledger = [entry for entry in (self.economy_db.get("solar_ledger") or {}).values() if isinstance(entry, dict)]
        spans = [(float(e["start"]), float(e["end"])) for e in ledger if _finite(e.get("start")) and _finite(e.get("end"))]
        since = self.economy_db.get("solar_energy_since")
        power_hours = self.economy_db.get("power_hours")
        power_hours = {float(ts): values for ts, values in power_hours.items()} if isinstance(power_hours, dict) else {}
        still_charging = not_captured = 0
        waiting: list[tuple[float, float]] = []  # sessions the ledger will still price: their hours are left to them
        for session in self._solar_sessions():
            start, end = session["start"], session["end"]
            if end is None:
                still_charging += 1
                waiting.append((start, now))
            elif _finite(end) and now - end <= LEDGER_WINDOW and not _overlaps(spans, start, end):
                waiting.append((start, end))
            elif (
                _finite(end) and not session["public"] and isinstance(since, (int, float)) and start >= since
                and now - end > LEDGER_WINDOW and min(session["solar"], session["local"], session["kwh"]) > 1e-9
                and not _overlaps(spans, start, end) and not self._solar_hours_known(power_hours, start, end)
            ):
                # merged into the daily history before it was priced (for example after a long outage), and the hourly
                # statistics do not reach it either
                not_captured += 1
        extra = {"still_charging": still_charging, "not_captured": not_captured}
        summary = summarize(ledger, self._solar_month(now), extra)
        if power_hours:
            # the hourly solar, house and charger power from the recorder: the whole history at once, no waiting. The
            # priced sessions are kept wherever they cover the time; the other charging hours are estimated from the
            # hourly means, an upper bound of their solar energy (see solar.py, power_hour_entries)
            buy = {**_float_keys(self.economy_db.get("prices_history")), **_float_keys(self.economy_db.get("prices"))}
            sell = {**_float_keys(self.economy_db.get("sell_prices_history")), **_float_keys(self.economy_db.get("sell_prices"))}
            try:
                assumed = float(self.config["solar"]["production_price"])
            except (KeyError, TypeError, ValueError):
                assumed = -1.0
            summary = solar_from_power_hours(
                power_hours, buy, sell,
                _float_keys(self.economy_db.get("sell_production_price")), assumed, self._solar_refund() or 0.0, now - 3600.0,
                self._solar_month(now), self._solar_month, ledger, extra, waiting,
            )
            if summary["hours_counted"] and summary["assumed_production_hours"]:
                summary["notes"].append("production_price_assumed_current")
        return {**base, **summary}

    @staticmethod
    def _solar_hours_known(power_hours: dict[float, Any], start: float, end: float) -> bool:
        """The hourly statistics hold an hour of [start, end) (that time is then estimated from them)."""
        hour = start - start % 3600.0
        while hour < max(end, start + 1.0):
            if hour in power_hours:
                return True
            hour += 3600.0
        return False

    def publish_what_if_solar(self) -> None:
        """The state is the solar advantage of the completed months; nothing when there is none."""
        try:
            result = self.what_if_solar_result()
        except Exception as err:  # noqa: BLE001
            # a failing comparison must never break the job that calls this
            _LOGGER.warning("The What-if solar comparison could not be calculated: %s %s", err, type(err))
            result = {"status": "unavailable", "reason": "internal_error"}
        unknown = self.economy_db.get("sell_tariff_unknown_hours")
        if isinstance(unknown, int) and not isinstance(unknown, bool):
            # recorded sell price hours left out because the tariffs of their day are not known (see _store_sell_history)
            result = {**result, "sell_tariff_unknown_hours": unknown}
        total = result.get("total") or {}
        self.set_state("what_if_solar", total.get("advantage") if result.get("status") == "ok" and total.get("months") else None)
        self.set_attr("what_if_solar", "result", result)
