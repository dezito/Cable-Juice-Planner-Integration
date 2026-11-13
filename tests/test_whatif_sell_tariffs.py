"""The recorded sell price is converted with the tariffs of its own day (What-if solar), never with today's or none.

Energi Data Service does not record its tariffs attribute, so CJP keeps each day's tariffs itself
(economy_db["sell_tariff_days"]) and an hour of a day without them is left out and counted.
"""
from __future__ import annotations

import datetime
import json

import pytest

from custom_components.cable_juice_planner import const
from custom_components.cable_juice_planner.engine import CJPEngine
from custom_components.cable_juice_planner.engine.economy import SELL_PRICES_HISTORY_VERSION
from custom_components.cable_juice_planner.prices import create_price_provider
from custom_components.cable_juice_planner.utils.codec import decode, encode

from .test_economy import _engine, _ts
from .test_engine import DAY, OPTIONS, TEXTS, TZ, snapshot
from .test_whatif_solar import _data, _solar_engine

TODAY = datetime.date(2026, 10, 30)
NOW = datetime.datetime.combine(TODAY, datetime.time(16, 40))


def _iso(day: datetime.date, hour: int) -> str:
    return datetime.datetime.combine(day, datetime.time(hour), tzinfo=TZ).isoformat()


def _raw_rows(day: datetime.date) -> list[dict]:
    return [{"hour": _iso(day, hour), "price": 2.0 + hour / 10} for hour in range(24)]


def _eds_tariffs(net=0.06, system=0.07, tax=0.9, hourly=lambda hour: 0.6 if 17 <= hour <= 20 else 0.2) -> dict:
    """The tariffs attribute of an Energi Data Service sensor."""
    return {
        "additional_tariffs": {"transmissions_nettarif": net, "systemtarif": system, "elafgift": tax},
        "tariffs": {str(hour): hourly(hour) for hour in range(24)},
    }


def _provider(sell: dict, buy: dict | None = None, sell_fee: float = 0.03):
    provider = create_price_provider(const.build_config({
        **const.default_options(),
        "energidataservice_buy_entity_id": "sensor.eds",
        "energidataservice_sell_entity_id": "sensor.eds_sell",
        "sell_fee": sell_fee,
    }))
    provider.set_now(NOW)
    provider.set_snapshot({"attributes": {"sensor.eds": buy or {"raw_today": _raw_rows(TODAY)}, "sensor.eds_sell": sell}, "values": {}})
    return provider


def _record(net: float, system: float, tax: float, hourly, fee: float = 0.0) -> dict:
    """A kept day of a sell sensor with the tariffs in its price."""
    return {
        "tariffs": {"transmissions_nettarif": net, "systemtarif": system, "elafgift": tax, "hours": {str(h): hourly(h) for h in range(24)}},
        "sell_costs": {"transmissions_nettarif": net, "systemtarif": system},
        "sell_fee": fee,
    }


def _convert(engine, snap, history: dict) -> None:
    data = {"prices": {}, "windows": [], "odometer_months": {}, "sell_history": history, "sell_history_entity_id": "sensor.price"}
    result = engine.run_job(snap, engine.economy_update, data)
    assert result.error is None, result.error


# --------------------------------------------------------------------------- the provider


@pytest.mark.parametrize("raw_sensor", [False, True], ids=["tariffs_in_price", "raw_sell_sensor"])
def test_a_kept_day_converts_a_price_exactly_like_the_live_sell_price(raw_sensor: bool) -> None:
    """The conversion of a recorded price with its day's record is the live formula: same number for every hour."""
    if raw_sensor:
        # a raw sell sensor: no tariffs in its price, the selling costs (net and system tariff) from the buy sensor
        provider = _provider({"raw_today": _raw_rows(TODAY)}, {"raw_today": _raw_rows(TODAY), "tariffs": _eds_tariffs()})
    else:
        provider = _provider({"raw_today": _raw_rows(TODAY), "tariffs": _eds_tariffs()})
    live = provider.get_sell_real_prices()
    notes = {moment: dict(details) for moment, details in provider.sell_price_details.items()}
    days = provider.sell_tariff_days()
    assert list(days) == ["sensor.eds_sell"]
    date, record = days["sensor.eds_sell"]
    assert date == TODAY.isoformat() and (record["tariffs"] is None) is raw_sensor
    assert len(live) == 24
    for hour in range(24):
        moment = datetime.datetime.combine(TODAY, datetime.time(hour))
        assert provider.convert_recorded_sell_price("sensor.eds_sell", moment, 2.0 + hour / 10, record) == live[moment]
    evening = datetime.datetime.combine(TODAY, datetime.time(18))
    # 3.8 - tariffs in the price (0.06 + 0.07 + 0.9 + 0.6, none for the raw sensor) - (fee 0.03 + net 0.06 + system 0.07)
    assert live[evening] == pytest.approx(3.64 if raw_sensor else 2.01)
    assert provider.sell_price_details == notes  # converting never touches the notes of the live sell prices


_ROWS = _raw_rows(TODAY)
_MISSING_HOUR = {**_eds_tariffs(), "tariffs": {str(hour): 0.2 for hour in range(23)}}


@pytest.mark.parametrize(
    ("sell", "buy"),
    [
        ({"raw_today": _raw_rows(TODAY - datetime.timedelta(days=1)), "tariffs": _eds_tariffs()}, None),  # attributes from before midnight
        ({"raw_today": [], "tariffs": _eds_tariffs()}, None),  # no prices: the day of the tariffs is unknown
        ({"raw_today": _ROWS, "tariffs": _MISSING_HOUR}, None),
        ({"raw_today": _ROWS, "tariffs": _eds_tariffs(tax=None)}, None),
        ({"raw_today": _ROWS, "tariffs": _eds_tariffs(net=float("nan"))}, None),
        ({"raw_today": _ROWS, "tariffs": _eds_tariffs(hourly=lambda hour: "0.2")}, None),
        ({"raw_today": _ROWS, "tariffs": None}, None),  # an empty tariffs attribute
        ({"raw_today": _ROWS}, {"raw_today": _ROWS}),  # a raw sell sensor and a buy sensor without tariffs: selling costs unknown
    ],
    ids=["yesterday", "no_prices", "missing_hour", "missing_tax", "nan", "text", "empty", "raw_without_costs"],
)
def test_a_day_is_only_kept_when_all_its_tariffs_are_known_numbers(sell: dict, buy: dict | None) -> None:
    """A missing or broken tariff is never kept as 0, and attributes from another day are never kept as today's."""
    assert _provider(sell, buy).sell_tariff_days() == {}


def test_a_recorded_price_is_never_converted_without_its_own_days_record() -> None:
    provider = _provider({"raw_today": _ROWS, "tariffs": _eds_tariffs()})
    _, record = provider.sell_tariff_days()["sensor.eds_sell"]
    moment = datetime.datetime.combine(TODAY, datetime.time(10))
    assert provider.convert_recorded_sell_price("sensor.eds_sell", moment, 2.0, record) is not None
    assert provider.convert_recorded_sell_price("sensor.eds_sell", moment, 2.0, None) is None
    assert provider.convert_recorded_sell_price("sensor.other", moment, 2.0, record) is None  # another sensor's day
    assert provider.convert_recorded_sell_price("sensor.eds_sell", moment, float("nan"), record) is None
    assert provider.convert_recorded_sell_price("sensor.eds_sell", moment, 2.0, {**record, "sell_costs": {"transmissions_nettarif": 0.06}}) is None
    assert provider.convert_recorded_sell_price("sensor.eds_sell", moment, 2.0, {**record, "tariffs": {**record["tariffs"], "hours": {}}}) is None
    assert provider.convert_recorded_sell_price("sensor.eds_sell", moment, 2.0, {**record, "sell_fee": None}) is None


def test_a_kept_day_survives_the_storage_encoding() -> None:
    _, record = _provider({"raw_today": _ROWS}, {"raw_today": _ROWS, "tariffs": _eds_tariffs()}).sell_tariff_days()["sensor.eds_sell"]
    database = {"sell_tariff_days": {"sensor.eds_sell": {TODAY.isoformat(): record}}}
    assert decode(json.loads(json.dumps(encode(database)))) == database


# --------------------------------------------------------------------------- the engine


def test_each_recorded_hour_is_converted_with_the_tariffs_of_its_own_day() -> None:
    """The regression: one day's tariffs were frozen into every recorded hour."""
    engine, snap = _solar_engine()
    winter, summer, unknown = (DAY - datetime.timedelta(days=days) for days in (20, 10, 5))
    engine.economy_db["sell_tariff_days"] = {"sensor.price": {
        winter.isoformat(): _record(0.05, 0.1, 0.8, lambda h: 0.9 if 17 <= h <= 20 else 0.3),
        summer.isoformat(): _record(0.04, 0.05, 0.01, lambda h: 0.1, fee=0.02),
    }}
    own = _ts(summer, 12)
    engine.economy_db["sell_prices"] = {own: 0.33}  # stored by CJP itself: never replaced
    notes = dict(engine.price_provider.sell_price_details)
    _convert(engine, snap, {
        _ts(winter, 18): 3.0, _ts(winter, 3): 2.0, _ts(summer, 18): 1.0, own: 9.0, _ts(unknown, 18): 2.5, _ts(unknown, 3): 2.5,
    })
    sell = engine.economy_db["sell_prices_history"]
    assert sell == {
        _ts(winter, 18): pytest.approx(3.0 - (0.05 + 0.1 + 0.8 + 0.9) - (0.05 + 0.1)),  # 1.0
        _ts(winter, 3): pytest.approx(2.0 - (0.05 + 0.1 + 0.8 + 0.3) - (0.05 + 0.1)),  # 0.6
        _ts(summer, 18): pytest.approx(1.0 - (0.04 + 0.05 + 0.01 + 0.1) - (0.02 + 0.04 + 0.05)),  # 0.69
    }
    # the day without kept tariffs is left out and counted, never converted with another day's tariffs
    assert engine.economy_db["sell_tariff_unknown_hours"] == 2
    assert _data(engine, snap)["sell_tariff_unknown_hours"] == 2
    assert engine.price_provider.sell_price_details == notes  # the live sell prices are untouched
    # a later run counts again (it does not add up) and keeps what was converted
    _convert(engine, snap, {_ts(winter, 18): 3.0, _ts(unknown, 18): 2.5})
    assert engine.economy_db["sell_tariff_unknown_hours"] == 1 and _ts(winter, 3) in engine.economy_db["sell_prices_history"]


def test_a_neighbouring_days_tariffs_are_never_used() -> None:
    """The distance limit is the hour's own local day: the next or the previous day's tariffs are not used."""
    engine, snap = _solar_engine()
    day = DAY - datetime.timedelta(days=8)
    engine.economy_db["sell_tariff_days"] = {"sensor.price": {day.isoformat(): _record(0.05, 0.1, 0.8, lambda h: 0.3)}}
    before, first, last, after = _ts(day, 0) - 3600.0, _ts(day, 0), _ts(day, 23), _ts(day + datetime.timedelta(days=1), 0)
    _convert(engine, snap, {before: 2.0, first: 2.0, last: 2.0, after: 2.0})
    assert set(engine.economy_db["sell_prices_history"]) == {first, last}
    assert engine.economy_db["sell_tariff_unknown_hours"] == 2


def test_hours_left_out_count_as_a_missing_sell_price_in_the_comparison() -> None:
    engine, snap = _solar_engine()
    engine.config["solar"]["production_price"] = -1.0
    prior = DAY - datetime.timedelta(days=40)
    ts = _ts(prior, 11)
    engine.economy_db["prices"] = {ts: 1.5}
    engine.economy_db["power_hours"] = {ts: [6000.0, 3000.0, 2000.0]}
    _convert(engine, snap, {ts: 0.9})  # no kept tariffs for that day
    assert ts not in engine.economy_db["sell_prices_history"]
    data = _data(engine, snap)
    assert data["sell_tariff_unknown_hours"] == 1
    # no result is calculated with a wrong sell price: the hour has no sell price, so nothing is counted
    assert data["status"] != "ok" and not (data.get("total") or {}).get("months")


def test_with_a_fixed_production_price_the_days_own_tariffs_decide_the_sign() -> None:
    """A fixed production price is a settlement (fixed - hourly sell price): today's low tariffs would flip its sign."""
    engine, snap = _solar_engine()
    engine.config["solar"]["production_price"] = 1.2
    winter = DAY - datetime.timedelta(days=40)
    ts = _ts(winter, 18)
    engine.economy_db["sell_tariff_days"] = {"sensor.price": {
        winter.isoformat(): _record(0.05, 0.1, 0.8, lambda h: 0.9 if 17 <= h <= 20 else 0.3),
        DAY.isoformat(): _record(0.04, 0.05, 0.01, lambda h: 0.1),  # today's tariffs: 3.0 would become 2.71
    }}
    engine.economy_db["prices"] = {ts: 2.0}
    engine.economy_db["sell_production_price"] = {ts: 1.2}
    engine.economy_db["power_hours"] = {ts: [6000.0, 3000.0, 2000.0]}  # 2 kWh of solar for the EV
    _convert(engine, snap, {ts: 3.0})
    assert engine.economy_db["sell_prices_history"][ts] == pytest.approx(1.0)
    data = _data(engine, snap)
    assert data["status"] == "ok"
    month = next(row for row in data["months"] if row["month"] == winter.strftime("%Y-%m"))
    # 2 kWh x (1.2 - 1.0) = +0.4; with today's tariffs it would have been 2 x (1.2 - 2.71) = -3.02
    assert month["sell_value"] == pytest.approx(2.0 * 0.2)


def test_todays_tariffs_are_kept_every_hour_and_old_days_are_dropped() -> None:
    engine, snap = _solar_engine()
    snap.price_data["attributes"]["sensor.price"]["tariffs"] = _eds_tariffs()
    too_old = (DAY - datetime.timedelta(days=engine.economy_keep_days() + 2)).isoformat()
    engine.economy_db["sell_tariff_days"] = {"sensor.price": {too_old: _record(0.1, 0.1, 0.1, lambda h: 0.1)}, "sensor.gone": "broken"}
    result = engine.run_job(snap, engine.economy_store_prices)
    assert result.error is None, result.error
    days = engine.economy_db["sell_tariff_days"]
    assert list(days) == ["sensor.price"] and list(days["sensor.price"]) == [DAY.isoformat()]
    record = days["sensor.price"][DAY.isoformat()]
    assert record["tariffs"]["hours"]["18"] == 0.6 and record["tariffs"]["elafgift"] == 0.9
    assert record["sell_costs"] == {"transmissions_nettarif": 0.06, "systemtarif": 0.07}


def test_attributes_from_before_midnight_are_not_kept_as_the_new_day() -> None:
    engine, snap = _solar_engine()
    after_midnight = snapshot(datetime.datetime.combine(DAY + datetime.timedelta(days=1), datetime.time(0, 30)))
    # the price snapshot is still yesterday's
    after_midnight.price_data["attributes"]["sensor.price"].update(raw_today=snap.price_data["attributes"]["sensor.price"]["raw_today"], tariffs=_eds_tariffs())
    result = engine.run_job(after_midnight, engine.economy_store_sell_tariffs, after_midnight.now)
    assert result.error is None, result.error
    assert not engine.economy_db.get("sell_tariff_days")


def test_nothing_is_kept_without_solar() -> None:
    engine, snap = _engine()  # solar is not set up
    snap.price_data["attributes"]["sensor.price"]["tariffs"] = _eds_tariffs()
    assert engine.run_job(snap, engine.economy_store_prices).error is None
    assert "sell_tariff_days" not in engine.economy_db


def _started(databases: dict) -> CJPEngine:
    engine = CJPEngine(const.build_config(OPTIONS), "cjp_test", TEXTS)
    result = engine.run_job(snapshot(datetime.datetime.combine(DAY, datetime.time(12, 0))), engine.startup, databases)
    assert result.error is None, result.error
    return engine


def test_sell_prices_converted_by_an_earlier_version_are_dropped_and_older_data_still_loads() -> None:
    """Data stored before the tariffs were kept per day (also a restored backup) loads; its converted sell prices
    were given one day's tariffs for every hour and are dropped. Everything else is kept."""
    old = {"windows": [], "version": 2, "sell_prices": {1.0: 0.5}, "sell_prices_history": {2.0: 0.4}, "prices": {3.0: 1.0}}
    database = _started({"economy": old}).economy_db
    assert "sell_prices_history" not in database and "sell_tariff_days" not in database
    assert database["sell_prices"] == {1.0: 0.5} and database["prices"] == {3.0: 1.0}
    assert database["sell_prices_history_version"] == SELL_PRICES_HISTORY_VERSION
    # converted the new way: kept on the next load, a broken store of the days is dropped
    database["sell_prices_history"] = {2.0: 0.4}
    database["sell_tariff_days"] = "broken"
    again = _started({"economy": database}).economy_db
    assert again["sell_prices_history"] == {2.0: 0.4} and "sell_tariff_days" not in again
