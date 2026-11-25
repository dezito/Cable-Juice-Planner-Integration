"""The review fixes of the providers of published prices: the roles of the sensors, rows of another period, Octopus without an export tariff in the
whole horizon, the history of a sensor in a wrong unit, the meter point in the debug export, and the clock changes."""
from __future__ import annotations

import datetime

import pytest

from custom_components.cable_juice_planner.prices import (
    EpexSpotPriceProvider,
    GeSpotPriceProvider,
    NordpoolPriceProvider,
    OctopusPriceProvider,
    create_price_provider,
    price_sensor_errors,
)

from .test_published_providers import (
    COPENHAGEN,
    DAY_3,
    EUR,
    GESPOT,
    LONDON,
    MARKET,
    NORD,
    NORD_SELL,
    NOW,
    OCT_EXPORT_TODAY,
    OCT_EXPORT_TOMORROW,
    OCT_TODAY,
    OCT_TOMORROW,
    TODAY,
    TOMORROW,
    TOTAL,
    _at,
    _config,
    _epex,
    _gespot,
    _nord,
    _octopus,
    _starts,
    _State,
)

EPF, EPF_SELL = "sensor.epf_retail", "sensor.epf_wholesale"


def _run(provider, attributes: dict, now=NOW, tz=COPENHAGEN) -> None:
    provider.shared_data = {"attributes": attributes, "values": {}, "version": 1}
    provider.now, provider.tz = now, tz
    provider.update_prices()


def _combine(combined, attributes: dict, now=NOW, tz=COPENHAGEN) -> None:
    combined.set_now(now, tz)
    combined.set_snapshot({"attributes": attributes, "values": {}})


def _epf_rows(days, price: float, step: int = 15) -> list[dict]:
    return [{"start": s.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z"), "end": (s + datetime.timedelta(minutes=step)).astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z"), "value": price} for s in _starts(days, step)]


def test_the_rows_of_another_period_of_a_later_provider_are_not_mixed_into_an_hour_with_a_real_price_of_a_provider_before_it() -> None:
    """Nord Pool has hourly prices, Energy Price Forecast published quarters: the hour is Nord Pool's whole, 12:15 is not an estimate."""
    combined = create_price_provider(_config(nordpool_buy_entity_id=NORD, nordpool_sell_entity_id=NORD_SELL, energypriceforecast_buy_entity_id=EPF, energypriceforecast_sell_entity_id=EPF_SELL))
    unit = {"unit_of_measurement": "DKK/kWh"}
    attributes = {
        NORD: {**unit, "raw_today": _nord((TODAY,), False), "raw_tomorrow": _nord((TOMORROW,), False)},
        NORD_SELL: {**unit, "raw_today": _nord((TODAY,), True), "raw_tomorrow": _nord((TOMORROW,), True)},
        EPF: {**unit, "raw_today": _epf_rows((TODAY,), 9.0), "raw_tomorrow": _epf_rows((TOMORROW,), 9.0), "raw_forecast": _epf_rows((DAY_3,), 9.0), "retail_source": "x"},
        EPF_SELL: {**unit, "raw_today": _epf_rows((TODAY,), 8.0), "raw_tomorrow": _epf_rows((TOMORROW,), 8.0), "raw_forecast": _epf_rows((DAY_3,), 8.0)},
    }
    _combine(combined, attributes)
    for kind in (combined.get_buy_real_prices(), combined.get_sell_real_prices()):
        assert {start for start in kind if start.date() == TODAY and start.hour == 12} == {_at(TODAY, 12)}  # the quarters are not Energy Price Forecast's
        assert all(price < 5 for start, price in kind.items() if start.date() in (TODAY, TOMORROW))  # nothing of the 9.0 and 8.0 of the other provider
    assert combined.price_source(_at(DAY_3, 12, 15)).startswith("energypriceforecast")  # the days after are its forecast, quarters and all


def test_octopus_without_an_export_tariff_sells_for_nothing_in_the_whole_horizon_and_with_one_the_other_provider_fills_the_days_after() -> None:
    """A provider with a forecast has sell prices for the days after tomorrow: a user who cannot sell must not get them."""
    unit = {"unit_of_measurement": "GBP/kWh"}
    epf = {
        EPF: {**unit, "raw_today": [], "raw_forecast": _epf_rows((TOMORROW, DAY_3), 0.3, 60), "retail_source": "x"},
        EPF_SELL: {**unit, "raw_today": [], "raw_forecast": _epf_rows((TOMORROW, DAY_3), 0.25, 60)},
    }
    options = {"energypriceforecast_buy_entity_id": EPF, "energypriceforecast_sell_entity_id": EPF_SELL, "octopus_buy_entity_id": OCT_TODAY, "octopus_buy_next_day_entity_id": OCT_TOMORROW}
    octopus = {OCT_TODAY: {"rates": _octopus((TODAY,))}, OCT_TOMORROW: {"rates": []}}
    combined = create_price_provider(_config(**options))
    _combine(combined, {**octopus, **epf}, tz=LONDON)
    sell = combined.get_sell_prices()
    assert {start.date() for start in sell} == {TODAY, TOMORROW, DAY_3}
    assert set(sell.values()) == {-0.03}  # 0 less the sell fee in every hour, today, tomorrow (no rates yet) and the forecast days
    assert combined.get_buy_prices()[_at(DAY_3, 8)] > 0.3  # the buy prices of the forecast days are still the other provider's
    # with an export tariff the export rates are used where Octopus has them, and the other provider's sell prices fill the days after
    exported = {**octopus, OCT_EXPORT_TODAY: {"rates": _octopus((TODAY,), True)}, OCT_EXPORT_TOMORROW: {"rates": []}}
    combined = create_price_provider(_config(**options, octopus_sell_entity_id=OCT_EXPORT_TODAY, octopus_sell_next_day_entity_id=OCT_EXPORT_TOMORROW))
    _combine(combined, {**exported, **epf}, tz=LONDON)
    assert combined.get_sell_prices()[_at(TODAY, 8)] < 0.1 and combined.get_sell_prices()[_at(DAY_3, 8)] == pytest.approx(0.25 - 0.03 - 2 / 30, abs=0.01)


def test_a_configured_export_entity_that_is_gone_is_an_error_not_a_free_sale() -> None:
    provider = OctopusPriceProvider(_config(octopus_buy_entity_id=OCT_TODAY, octopus_buy_next_day_entity_id=OCT_TOMORROW, octopus_sell_entity_id=OCT_EXPORT_TODAY))
    with pytest.raises(ValueError, match="not loaded"):
        _run(provider, {OCT_TODAY: {"rates": _octopus((TODAY,))}}, tz=LONDON)
    assert OctopusPriceProvider(_config(octopus_buy_entity_id=OCT_TODAY)).sells_nothing is True
    assert OctopusPriceProvider(_config(octopus_buy_entity_id=OCT_TODAY, octopus_sell_entity_id=OCT_EXPORT_TODAY)).sells_nothing is False


def test_the_sell_sensor_is_another_sensor_than_the_buy_sensor() -> None:
    assert price_sensor_errors({"nordpool_buy_entity_id": NORD, "nordpool_sell_entity_id": NORD}, {}.get) == {"nordpool_sell_entity_id": "price_sensor_same_sensor"}
    assert price_sensor_errors({"epexspot_buy_entity_id": TOTAL, "epexspot_sell_entity_id": TOTAL}, {}.get) == {"epexspot_sell_entity_id": "price_sensor_same_sensor"}
    assert price_sensor_errors({"nordpool_buy_entity_id": NORD, "nordpool_sell_entity_id": NORD_SELL}, {}.get) == {}
    octopus = {"octopus_buy_entity_id": OCT_TODAY, "octopus_buy_next_day_entity_id": OCT_TOMORROW, "octopus_sell_entity_id": OCT_TODAY, "octopus_sell_next_day_entity_id": OCT_TOMORROW}
    assert price_sensor_errors(octopus, {}.get) == {"octopus_sell_entity_id": "price_sensor_same_sensor", "octopus_sell_next_day_entity_id": "price_sensor_same_sensor"}
    assert price_sensor_errors({"octopus_buy_entity_id": OCT_TODAY, "octopus_buy_next_day_entity_id": OCT_TODAY}, {}.get) == {"octopus_buy_next_day_entity_id": "price_sensor_same_sensor"}


def test_a_sensor_in_the_wrong_field_is_refused_by_its_unique_id() -> None:
    ids = {
        TOTAL: "epex_spot Awattar de Total Price", MARKET: "epex_spot Awattar de MarketPrice",
        OCT_TODAY: "octopus_energy_electricity_1_2_current_day_rates", OCT_TOMORROW: "octopus_energy_electricity_1_2_next_day_rates",
        OCT_EXPORT_TODAY: "octopus_energy_electricity_1_2_export_current_day_rates", OCT_EXPORT_TOMORROW: "octopus_energy_electricity_1_2_export_next_day_rates",
        "event.gas_today": "octopus_energy_gas_1_2_current_day_rates",
    }
    epex = {"epexspot_buy_entity_id": TOTAL, "epexspot_sell_entity_id": MARKET}
    assert price_sensor_errors(epex, {}.get, ids.get) == {}
    assert price_sensor_errors({"epexspot_buy_entity_id": MARKET, "epexspot_sell_entity_id": TOTAL}, {}.get, ids.get) == {"epexspot_buy_entity_id": "price_sensor_wrong_role", "epexspot_sell_entity_id": "price_sensor_wrong_role"}
    rights = {"octopus_buy_entity_id": OCT_TODAY, "octopus_buy_next_day_entity_id": OCT_TOMORROW, "octopus_sell_entity_id": OCT_EXPORT_TODAY, "octopus_sell_next_day_entity_id": OCT_EXPORT_TOMORROW}
    assert price_sensor_errors(rights, {}.get, ids.get) == {}
    swapped = {"octopus_buy_entity_id": OCT_EXPORT_TODAY, "octopus_buy_next_day_entity_id": OCT_TODAY, "octopus_sell_entity_id": OCT_TODAY, "octopus_sell_next_day_entity_id": "event.gas_today"}
    assert set(price_sensor_errors(swapped, {}.get, ids.get)) == set(swapped)  # export as buy, the wrong day, import as sell, gas
    assert price_sensor_errors(rights, {}.get) == {}  # a sensor that is not in the registry is not judged by it


@pytest.mark.parametrize("unit", ["öre/kWh", "cents/kWh", "EUR/MWh"])
def test_the_recorded_history_of_a_sensor_in_a_wrong_unit_is_left_out_for_the_new_providers(unit) -> None:
    combined = create_price_provider(_config(epexspot_buy_entity_id=TOTAL, epexspot_sell_entity_id=MARKET, gespot_buy_entity_id=GESPOT, nordpool_buy_entity_id=NORD, nordpool_sell_entity_id=NORD_SELL))
    states = {entity: _State("1", {"unit_of_measurement": unit}) for entity in (TOTAL, MARKET, GESPOT, NORD, NORD_SELL)}
    assert combined.price_unit_divisors(states.get) == dict.fromkeys(states)  # None: the history is not read as a price per kWh
    good = {entity: _State("1", {"unit_of_measurement": "SEK/kWh"}) for entity in states}
    assert combined.price_unit_divisors(good.get) == {}


def test_the_meter_point_and_the_tariff_code_of_octopus_are_not_in_a_debug_export() -> None:
    from custom_components.cable_juice_planner.management.debug_export import _SENSITIVE_KEY_RE

    for key in ("mpan", "serial_number", "tariff_code"):
        assert _SENSITIVE_KEY_RE.search(key), key
    assert not _SENSITIVE_KEY_RE.search("rates") and not _SENSITIVE_KEY_RE.search("value_inc_vat")


@pytest.mark.parametrize(("day", "rows"), [(datetime.date(2026, 10, 25), 25), (datetime.date(2026, 3, 29), 23)])
def test_a_day_with_a_clock_change_gives_one_price_per_local_hour_and_the_first_of_a_repeated_hour_wins(day, rows) -> None:
    """EPEX Spot's rows have 25 and 23 hours on these days: the repeated hour is one start in local time, the first row of it wins."""
    attributes = {TOTAL: {**EUR, "data": _epex((day,), False)}, MARKET: {**EUR, "data": _epex((day,), True)}}
    assert len(attributes[TOTAL]["data"]) == rows
    provider = EpexSpotPriceProvider(_config(epexspot_buy_entity_id=TOTAL, epexspot_sell_entity_id=MARKET))
    now = datetime.datetime.combine(day, datetime.time(1, 30))
    _run(provider, attributes, now)
    assert len(provider.buy_real_prices) == (24 if rows == 25 else 23) and len(provider.sell_real_prices) == len(provider.buy_real_prices)
    if rows == 25:  # the repeated 02:00: the first row (before the change) has the price of hour 2, not of the second 02:00 (which is the same hour of the day)
        assert provider.buy_real_prices[datetime.datetime.combine(day, datetime.time(2))] == pytest.approx(attributes[TOTAL]["data"][2]["price_per_kwh"])
    gespot = GeSpotPriceProvider(_config(gespot_buy_entity_id=GESPOT))
    _run(gespot, {GESPOT: {"unit_of_measurement": "SEK/kWh", "today_interval_prices": _gespot((day,)), "tomorrow_valid": False, "data_source": "nordpool"}}, now)
    assert len(gespot.buy_real_prices) == (96 if rows == 25 else 92)  # the repeated hour's quarters are four starts, not eight
