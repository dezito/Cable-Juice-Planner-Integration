"""More push notifications, each with its own switch under Notifications (config["notification"]):

- the charger has not answered for a while, and charging for the next departure is planned (``notify_charger_offline``);
- cheap charging (an ultra or very cheap price, or a price below 0) is planned soon, the car is home and not plugged in
  (``notify_cheap_power``);
- manual charging is still on and charges in one of the day's most expensive hours (``notify_manual_expensive``);
- the month's charging, cost and saving, once the month is over (``notify_monthly_summary``, from economy_update);
- load balancing stopped or lowered the planned charging (``notify_site_limit``, from _apply_site_limit);
- the car's battery has stood at 99 % or more for a long time (``notify_battery_full``);
- the measured kWh per 1 % rose clearly against 3-6 months back, and against the same months a year earlier when they
  have enough measurements (``notify_charging_loss``).

Each event is sent once: the sent notices are kept in the runtime state with the time each is forgotten, so a condition
that lasts days is not sent again every day. Missing, unknown or stale data never sends a notice.
"""
from __future__ import annotations

import datetime
import logging
import math

from ...const import CHARGER_CHARGING_STATUS, CHARGER_READY_STATUS, ENTITY_UNAVAILABLE_STATES, price_decimals
from ...prices.intervals import naive_timestamp, price_interval_at
from ..loss import _outlier_segments
from .readiness import DEPARTURE_NOTICE_HOURS, PLUG_IN_NOTICE_MINUTES, WORK, _for_departure

_LOGGER = logging.getLogger(__name__)

# the charger's status has been unavailable or unknown this long
CHARGER_OFFLINE_MINUTES = 30
# the cheap charging starts within this many minutes (or has started)
CHEAP_NOTICE_MINUTES = PLUG_IN_NOTICE_MINUTES
# the planner's cheap rules (charge_plan.add_to_charge_hours); a price below 0 counts too
CHEAP_FLAGS = ("half_min_avg_price", "under_min_avg_price")
# the battery counts as full from this level (the full charge of efficiency.py)
BATTERY_FULL_LEVEL = 99.0
BATTERY_FULL_HOURS = 12
# with a full charge every X days (the car is charged to 100 % on purpose, often an LFP battery that stands it well)
BATTERY_FULL_HOURS_FULL_CHARGE = 24
# the car's own last report may be this old; older, the level may be one the integration no longer updates
BATTERY_FULL_REPORT_HOURS = 24
# a battery still full this long after the notice gets it again
BATTERY_FULL_REMEMBER_DAYS = 30
# the charging loss: the last days against the months this far back, only the groups where the car's fixed load (heating
# the battery in winter) weighs least, and a rise of at least this share
LOSS_RECENT_DAYS = 30
LOSS_BASE_MONTHS = (3, 6)
LOSS_GROUPS = ("4-7 kW", "> 7 kW")
LOSS_RISE = 0.15
LOSS_MIN_RECENT_SEGMENTS = 3
LOSS_MIN_RECENT_SOC = 20.0
LOSS_MIN_BASE_SEGMENTS = 5
LOSS_MIN_BASE_SOC = 40.0
# the same season a year earlier (the months of the last LOSS_RECENT_DAYS, 12 and 13 months back): with enough
# measurements there (the base minimums), the rise must hold against it too, so a cold winter alone sends nothing
LOSS_YEAR_MONTHS = (12, 13)
# the monthly summary waits this many days for a connection of the last month that is not compared yet
MONTHLY_WAIT_DAYS = 3
# the full month names of ui.calendar.month_names (not strftime: it follows the system locale)
MONTH_KEYS = ("january", "february", "march", "april", "may_full", "june", "july", "august", "september", "october", "november", "december")
CHARGER_OFFLINE = "charger_offline"
CHEAP_POWER = "cheap_power"
MANUAL_EXPENSIVE = "manual_expensive"
MONTHLY_SUMMARY = "monthly_summary"
SITE_LIMIT = "site_limit"
SITE_LIMIT_CALL = "site_limit call"  # _notice_seen: the time of the last load balancing decision
BATTERY_FULL = "battery_full"
CHARGING_LOSS = "charging_loss"


def _month(moment: datetime.datetime, back: int = 0) -> str:
    """'YYYY-MM' of the month `back` months before the month of moment."""
    index = moment.year * 12 + moment.month - 1 - back
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _next_midnight(moment: datetime.datetime) -> datetime.datetime:
    return datetime.datetime.combine(moment.date() + datetime.timedelta(days=1), datetime.time(0, 0), tzinfo=moment.tzinfo)


class ControlNotices:
    """The notices beside the departure notices (control/readiness.py)."""

    def notify_notices(self) -> None:
        """The cron: the notices read from the plan, the states and the history. One failing never stops the others or
        the cron."""
        for name, check in (
            (CHARGER_OFFLINE, self._notice_charger_offline),
            (CHEAP_POWER, self._notice_cheap_power),
            (MANUAL_EXPENSIVE, self._notice_manual_expensive),
            (BATTERY_FULL, self._notice_battery_full),
            (CHARGING_LOSS, self._notice_charging_loss),
        ):
            try:
                check(self.now())
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Notification %s failed", name)

    def _notice_on(self, key: str) -> bool:
        return bool(self.config.get("notification", {}).get(key, True))

    def _send_notice(self, title_key: str, message: str, entity_id: str | None = None, action_key: str = "action_open") -> None:
        t = self.t
        data = {"actions": [{"action": "URI", "title": t(f"ui.notify_more.{action_key}"), "uri": f"entityId:{entity_id}"}]} if entity_id else None
        _LOGGER.info("Notification: %s", message)
        self.my_notify(message, title=f"{self.title} {t(f'ui.notify_more.{title_key}')}", data=data, always=True)

    def _plan_days(self) -> list[dict]:
        plan = getattr(self, "charging_plan", None) or {}
        return [plan[day] for day in sorted(key for key in plan if isinstance(key, int)) if isinstance(plan[day], dict)]

    def _price_interval(self, moment: datetime.datetime) -> tuple[datetime.datetime | None, float | None]:
        """The end and the grid price of the price interval at moment (an hour, 15 minutes, ...); None for what is not
        known (a price is never 0 for that)."""
        interval = price_interval_at(self.get_grid_prices(), naive_timestamp(moment))
        if interval is None:
            return None, None
        price = interval[2]
        return interval[1], float(price) if isinstance(price, (int, float)) and not isinstance(price, bool) and math.isfinite(price) else None

    def _price_at(self, moment: datetime.datetime) -> float | None:
        return self._price_interval(moment)[1]

    # ------------------------------------------------------------------ #
    # the charger does not answer before a departure
    # ------------------------------------------------------------------ #

    def _notice_charger_offline(self, now: datetime.datetime) -> None:
        """The departure notices are silent while the charger's status is not known; this one says so, once per departure,
        while charging for it is planned and not over."""
        if not self._notice_on("notify_charger_offline"):
            return
        status_id = self.config["charger"]["entity_ids"]["status_entity_id"]
        if not self.is_entity_configured(status_id):
            return
        if self.get_state(status_id, try_history=False, error_state="unavailable") not in ENTITY_UNAVAILABLE_STATES:
            return
        minutes = self._unavailable_minutes(status_id)
        if minutes is None or minutes < CHARGER_OFFLINE_MINUTES:
            return
        hours = getattr(self, "charge_hours", None) or {}
        horizon = now + datetime.timedelta(hours=DEPARTURE_NOTICE_HOURS)
        for _, kind, departure in self._departures(now, self._plan_days(), horizon):
            planned = any(
                isinstance(timestamp, datetime.datetime) and isinstance(hour, dict) and hour.get("kWh", 0.0) > 0.0
                and timestamp + datetime.timedelta(hours=1) > now and timestamp < departure and _for_departure(hour)
                for timestamp, hour in hours.items()
            )
            if not planned:
                continue
            # a work day by its date (the learned departure can give way to the entered one), a trip by its time
            # (readiness._new_departure_notice)
            event = departure.date().isoformat() if kind == WORK else departure.isoformat()
            if self._new_notice(f"{CHARGER_OFFLINE} {kind} {event}", departure + datetime.timedelta(days=1)):
                message = self.t("ui.notify_more.charger_offline", minutes=round(minutes), departure=self._departure_text(now, departure))
                self._send_notice("title_charger_offline", message, status_id)
            return

    def _unavailable_minutes(self, entity_id: str) -> float | None:
        """Minutes the entity has been unavailable or unknown in the history kept; None when the history has no such
        stretch up to now (not known how long)."""
        since = None
        for timestamp, value in self.history.buffers.get(entity_id) or []:
            if value in ENTITY_UNAVAILABLE_STATES:
                since = timestamp if since is None else since
            else:
                since = None
        if since is None:
            return None
        return max((self.history._ts(self.now()) - since) / 60.0, 0.0)

    # ------------------------------------------------------------------ #
    # cheap charging planned, the car is not plugged in
    # ------------------------------------------------------------------ #

    def _notice_cheap_power(self, now: datetime.datetime) -> None:
        """Cheap charging (the planner's ultra or very cheap price, or a price below 0) starts within an hour, and the car
        is home and unplugged. Not for the charging a departure needs (the plug-in reminder), at most once a day."""
        if not self._notice_on("notify_cheap_power"):
            return
        level = self.battery_level(unknown=None)
        if level is None or (not self.is_ev_configured() and level <= 0.0):
            return  # no real level: an unplugged car without its integration is at 0 % and always has room
        hours = getattr(self, "charge_hours", None) or {}
        soon = now + datetime.timedelta(minutes=CHEAP_NOTICE_MINUTES)
        planned = []
        for timestamp, hour in sorted((ts, h) for ts, h in hours.items() if isinstance(ts, datetime.datetime) and isinstance(h, dict)):
            if not hour.get("kWh", 0.0) > 0.0:
                continue
            # a planned slot lasts its price interval (the plan is per interval: an hour, or 15 minutes); without a
            # price an hour, and its price is not known
            end, price = self._price_interval(timestamp)
            end = end if end is not None and end > timestamp else timestamp + datetime.timedelta(hours=1)
            if end > now:
                planned.append((timestamp, end, price, hour))
        if any(start <= soon and _for_departure(hour) for start, _, _, hour in planned):
            return  # the plug-in reminder of the departure covers it
        cheap = [
            (start, end, price) for start, end, price, hour in planned
            if not _for_departure(hour) and (any(hour.get(flag) for flag in CHEAP_FLAGS) or (price is not None and price < 0.0))
        ]
        if not cheap or cheap[0][0] > soon or not self._unplugged_at_home(now):
            return
        start, end, prices = cheap[0][0], cheap[0][0], []
        for slot_start, slot_end, price in cheap:
            if slot_start != end:
                break
            end = slot_end
            prices.append(price)
        if any(price is None for price in prices):
            return  # a price of the window is not known
        if not self._new_notice(f"{CHEAP_POWER} {start.date().isoformat()}", start + datetime.timedelta(days=1)):
            return
        t = self.t
        message = t(
            "ui.notify_more.cheap_power", start=max(start, now).strftime("%H:%M"), end=end.strftime("%H:%M"),
            price=f"{min(prices):.{price_decimals(self.config)}f}", valuta=t("ui.common.valuta"),
        )
        self._send_notice("title_cheap_power", message, self.own_entity_id("sensor", "current_charging_rule"), "action_plan")

    # ------------------------------------------------------------------ #
    # manual charging in an expensive hour
    # ------------------------------------------------------------------ #

    def _notice_manual_expensive(self, now: datetime.datetime) -> None:
        """Manual charging stays on until the next plug-in: it charges now in one of the day's 4 most expensive hours.
        Once a day."""
        if not self._notice_on("notify_manual_expensive") or not self.manual_charging_enabled():
            return
        status_id = self.config["charger"]["entity_ids"]["status_entity_id"]
        if self.get_state(status_id, try_history=False, error_state="unavailable") not in CHARGER_CHARGING_STATUS:
            return
        hour = naive_timestamp(now).replace(minute=0, second=0, microsecond=0)
        if hour not in self.get_expensive_hours(0):
            return
        price = self._price_at(now)
        if price is None:
            return
        if not self._new_notice(f"{MANUAL_EXPENSIVE} {now.date().isoformat()}", _next_midnight(now) + datetime.timedelta(days=1)):
            return
        t = self.t
        message = t("ui.notify_more.manual_expensive", price=f"{price:.{price_decimals(self.config)}f}", valuta=t("ui.common.valuta"))
        self._send_notice("title_manual_expensive", message, self.own_entity_id("switch", "allow_manual_charging_now"), "action_manual")

    # ------------------------------------------------------------------ #
    # the month's charging (economy_update)
    # ------------------------------------------------------------------ #

    def notify_monthly_summary(self) -> None:
        """After the daily economy update: last month's charging, once. Waits a few days for a connection of last month
        that is still open (its comparison is not done), and is never sent for a month CJP did not see from its start or
        without a known cost."""
        try:
            self._notice_monthly_summary(self.now())
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Notification %s failed", MONTHLY_SUMMARY)

    def _notice_monthly_summary(self, now: datetime.datetime) -> None:
        if not self._notice_on("notify_monthly_summary"):
            return
        month = _month(now, 1)
        key = f"{MONTHLY_SUMMARY} {month}"
        if key in self.sent_notices:
            return
        windows = [w for w in (getattr(self, "economy_db", None) or {}).get("windows") or [] if isinstance(w, dict) and isinstance(w.get("start"), (int, float))]
        # a connection of last month not compared yet: still open, closed within the settle time, or its prices are not
        # in the statistics yet (economy_update gives a compared one "achieved")
        pending = any("achieved" not in w and self._economy_local(w["start"]).strftime("%Y-%m") == month for w in windows)
        if pending and now.day <= MONTHLY_WAIT_DAYS:
            return
        # CJP's data must begin before the month: the charging history or the connections (also those read back from the
        # recorder the first time)
        starts = [key.strftime("%Y-%m") for key in self.charging_history_db if isinstance(key, datetime.datetime)]
        starts += [self._economy_local(w["start"]).strftime("%Y-%m") for w in windows]
        if not starts or min(starts) >= month:
            return  # CJP did not see the month from its start: not the month's charging
        row = next((row for row in getattr(self, "economy_months", None) or [] if row.get("month") == month), None)
        if row is None or not row.get("kwh") or row.get("cost") is None:
            return
        if not self._new_notice(key, now + datetime.timedelta(days=40)):
            return
        t = self.t
        valuta = t("ui.common.valuta")
        name = t(f"ui.calendar.month_names.{MONTH_KEYS[int(month[5:]) - 1]}")
        lines = [t("ui.notify_more.monthly_summary", month=name, kwh=round(row["kwh"]), cost=f"{row['cost']:.2f}", valuta=valuta)]
        if row.get("unit_price") is not None:
            lines.append(t("ui.notify_more.monthly_unit_price", price=f"{row['unit_price']:.{price_decimals(self.config)}f}", valuta=valuta))
        saving = row.get("saving_daily")
        if saving is not None:
            text = "monthly_saving" if saving >= 0 else "monthly_extra"
            lines.append(t(f"ui.notify_more.{text}", amount=f"{abs(saving):.2f}", valuta=valuta))
        self._send_notice("title_monthly_summary", "\n".join(lines), self.own_entity_id("sensor", "economy"))

    # ------------------------------------------------------------------ #
    # load balancing (_apply_site_limit)
    # ------------------------------------------------------------------ #

    def notify_site_limit(self, requested: list, limited: list, stopped: bool) -> None:
        """Every load balancing decision (_apply_site_limit, not during the charger test): it stopped or lowered the
        charging of a car that is plugged in. Only when the limit has held for two cron intervals and 10 minutes (a
        kettle is no notice; readiness._held, in memory), then each kind at most once a day. A pause without decisions
        (the charger test, another car, the site unloaded) starts the wait again: an old first sighting proves nothing."""
        try:
            last = self._notice_seen.get(SITE_LIMIT_CALL)
            self._notice_seen[SITE_LIMIT_CALL] = self.now()
            since = None if last is None else self._minutes_since(last)
            if since is None or since > 2 * self.config["cron_interval"]:
                self._notice_seen.pop(SITE_LIMIT, None)
            status =self.get_state(self.config["charger"]["entity_ids"]["status_entity_id"], try_history=False, error_state="unavailable")
            if (
                limited == requested or self.charger_action != "charge" or not self._notice_on("notify_site_limit")
                or (status not in CHARGER_CHARGING_STATUS and status not in CHARGER_READY_STATUS)
            ):
                self._notice_seen.pop(SITE_LIMIT, None)
                return
            kind = "stopped" if stopped else "reduced"
            # each kind must hold: a short stop during a longer reduction is no stop notice
            if not self._held(SITE_LIMIT, kind, self._notice_seen):
                return
            now = self.now()
            if not self._new_notice(f"{SITE_LIMIT} {kind} {now.date().isoformat()}", _next_midnight(now) + datetime.timedelta(days=1)):
                return
            # as power: the limit can also change the phases (3 phases at 6 A -> 1 phase at 16 A)
            volt = float(self.config["charger"]["power_voltage"])
            before, after = sum(int(a) for a in requested) * volt / 1000, sum(int(a) for a in limited) * volt / 1000
            # the time the limit began (the first sighting of the kind), not the end of the wait
            began = self._notice_seen[SITE_LIMIT][1]
            message = self.t(f"ui.notify_more.site_{kind}", time=began.strftime("%H:%M"), before=f"{before:.1f}", after=f"{after:.1f}")
            self._send_notice("title_site_limit", message, self.own_entity_id("sensor", "charger_action"))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Notification %s failed", SITE_LIMIT)

    # ------------------------------------------------------------------ #
    # the battery stands full
    # ------------------------------------------------------------------ #

    def _notice_battery_full(self, now: datetime.datetime) -> None:
        """The car's own level has stood at BATTERY_FULL_LEVEL or more for BATTERY_FULL_HOURS (BATTERY_FULL_HOURS_FULL_CHARGE
        with a full charge every X days). Once per time it is full: a level below it starts a new one. Only with a car
        integration, and not from a car that has not reported for long (its level may be an old one)."""
        if not self._notice_on("notify_battery_full") or not self.is_ev_configured():
            return
        level = self.battery_level(real=True, unknown=None)
        if level is None or not math.isfinite(level):
            return
        if level < BATTERY_FULL_LEVEL:
            self._forget_notice(BATTERY_FULL)
            return
        if self.is_entity_configured(self.config["ev_car"]["entity_ids"]["last_update_entity_id"]):
            reported = self._car_report_minutes()
            if reported is None or reported > BATTERY_FULL_REPORT_HOURS * 60:
                return
        since = below = None
        for timestamp, value in self.history.buffers.get(self.config["ev_car"]["entity_ids"]["usable_battery_level_entity_id"]) or []:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(number):
                continue
            if number >= BATTERY_FULL_LEVEL:
                since = since if since is not None else timestamp
            else:
                since, below = None, timestamp
        if since is None:
            return
        hours = (self.history._ts(now) - since) / 3600.0
        full_charge = self.number_state("full_charge_recommended", error_state=None)
        # not known: the longer time (a notice too early is the one to avoid)
        if hours < (BATTERY_FULL_HOURS if full_charge is not None and full_charge <= 0.0 else BATTERY_FULL_HOURS_FULL_CHARGE):
            return
        sent = self.sent_notices.get(BATTERY_FULL)
        if sent is not None:
            try:
                sent_at = datetime.datetime.fromisoformat(sent) - datetime.timedelta(days=BATTERY_FULL_REMEMBER_DAYS)
            except (TypeError, ValueError):
                sent_at = None
            # a level below full in the history after the notice (a cron did not see it): a new time. Not the start of the
            # full stretch: after a restart the history begins with the state at its window's start
            if sent_at is not None and below is not None and below > self.history._ts(sent_at.replace(tzinfo=None)):
                self._forget_notice(BATTERY_FULL)
        if not self._new_notice(BATTERY_FULL, now + datetime.timedelta(days=BATTERY_FULL_REMEMBER_DAYS)):
            return
        message = self.t("ui.notify_more.battery_full", level=round(level), hours=math.floor(hours))
        self._send_notice("title_battery_full", message, self.own_entity_id("number", "max_recommended_charge_limit_battery_level"))

    # ------------------------------------------------------------------ #
    # the charging loss rose
    # ------------------------------------------------------------------ #

    def _notice_charging_loss(self, now: datetime.datetime) -> None:
        """kWh from the charger per 1 % over the last LOSS_RECENT_DAYS against the months LOSS_BASE_MONTHS back, per power
        group of LOSS_GROUPS, with enough measurements in both: a rise of LOSS_RISE or more is reported once a month. When the
        months LOSS_YEAR_MONTHS back have enough measurements too, the rise must also hold against them (the same season)."""
        if not self._notice_on("notify_charging_loss"):
            return
        db = getattr(self, "charging_loss_db", None) or {}
        months = db.get("months") or {}
        recent_from = self.history._ts(now) - LOSS_RECENT_DAYS * 86400
        segments, _ = _outlier_segments([s for s in db.get("segments") or [] if isinstance(s, dict)])
        base_months = [_month(now, back) for back in range(LOSS_BASE_MONTHS[0], LOSS_BASE_MONTHS[1] + 1)]
        year_months = [_month(now, back) for back in LOSS_YEAR_MONTHS]

        def per_percent(group: str, wanted: list[str]) -> float | None:
            """kWh per 1 % of the group over the months wanted, None without enough measurements."""
            rows = [months[month][group] for month in wanted if isinstance(months.get(month), dict) and isinstance(months[month].get(group), dict)]
            soc, kwh = sum(v.get("soc", 0.0) for v in rows), sum(v.get("kwh", 0.0) for v in rows)
            if sum(len(v.get("starts") or []) for v in rows) < LOSS_MIN_BASE_SEGMENTS or soc < LOSS_MIN_BASE_SOC or kwh <= 0.0:
                return None
            return kwh / soc

        found = []
        for group in LOSS_GROUPS:
            recent = [s for s in segments if s.get("group") == group and s.get("start", 0) >= recent_from]
            recent_soc, recent_kwh = sum(s["soc_delta"] for s in recent), sum(s["kwh"] for s in recent)
            if len(recent) < LOSS_MIN_RECENT_SEGMENTS or recent_soc < LOSS_MIN_RECENT_SOC:
                continue
            before_per_percent = per_percent(group, base_months)
            if before_per_percent is None:
                continue
            now_per_percent, year_per_percent = recent_kwh / recent_soc, per_percent(group, year_months)
            if now_per_percent < before_per_percent * (1.0 + LOSS_RISE):
                continue
            if year_per_percent is not None and now_per_percent < year_per_percent * (1.0 + LOSS_RISE):
                continue  # as high as the same season a year earlier: the weather, not the charging
            found.append((now_per_percent / before_per_percent, group, now_per_percent, before_per_percent, year_per_percent))
        if not found or not self._new_notice(f"{CHARGING_LOSS} {_month(now)}", now + datetime.timedelta(days=40)):
            return
        rise, group, now_per_percent, before_per_percent, year_per_percent = max(found, key=lambda item: (item[0], item[1]))
        message = self.t(
            "ui.notify_more.charging_loss", group=group, now=f"{now_per_percent:.3f}", before=f"{before_per_percent:.3f}",
            rise=round((rise - 1.0) * 100), days=LOSS_RECENT_DAYS,
        )
        if year_per_percent is not None:
            message = f"{message} {self.t('ui.notify_more.charging_loss_year', year=f'{year_per_percent:.3f}')}"
        self._send_notice("title_charging_loss", message, self.own_entity_id("sensor", "charging_loss_model"))

    # ------------------------------------------------------------------ #
    # sent notices (runtime state)
    # ------------------------------------------------------------------ #

    def _new_notice(self, key: str, forget_at: datetime.datetime) -> bool:
        """True once per key until forget_at; remembered in the runtime state."""
        self._forget_sent_notices()
        if key in self.sent_notices:
            return False
        self.sent_notices[key] = forget_at.isoformat()
        self.mark_dirty("runtime")
        return True

    def _forget_notice(self, key: str) -> None:
        if self.sent_notices.pop(key, None) is not None:
            self.mark_dirty("runtime")

    def _forget_sent_notices(self) -> None:
        now = self.now()
        kept = {}
        for key, forget_at in self.sent_notices.items():
            try:
                moment = datetime.datetime.fromisoformat(forget_at)
            except (TypeError, ValueError):
                continue
            if (moment.tzinfo is None) != (now.tzinfo is None):
                moment = moment.replace(tzinfo=now.tzinfo) if moment.tzinfo is None else moment.replace(tzinfo=None)
            if moment > now:
                kept[key] = forget_at
        if kept != self.sent_notices:
            self.sent_notices = kept
            self.mark_dirty("runtime")

    def restore_sent_notices(self, saved) -> None:
        self.sent_notices = {key: value for key, value in saved.items() if isinstance(key, str) and isinstance(value, str)} if isinstance(saved, dict) else {}
